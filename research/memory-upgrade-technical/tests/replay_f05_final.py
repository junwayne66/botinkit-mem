"""Replay owner intersection, archive cutoff and managed cleanup at the native API."""

import hashlib
import json
import sqlite3
from contextlib import closing
from time import monotonic, sleep
from urllib.request import ProxyHandler, Request, build_opener

from baseline_probe import BrowserSession, embedding_server, events, fixture, load_bound, native, post
from replay_delivery_retry import evidence_manifest


CASES = {"F05-04", "F05-13", "F05-15"}


def change(service, owner, **fields):
    key = "-".join(str(value) for value in fields.values())
    return service.operation(owner, {"kind": "change_grant", **fields}, key)


def load(reader, label, task, query):
    status, result = load_bound(*reader, label, task, evidence_query=query)
    if status != 200:
        raise ValueError((label, status, result))
    return result


def replay(case, service, owner):
    data = case["fixture"]
    args = {step["id"]: step["args"] for step in case["steps"]}
    task = post(owner, "/v2/tasks", {"anchor": data["task"], "project": "青禾"}, "task")["task_id"]
    roots = [{**row, "native_object": row["id"], "task": data["task"], "ref": row["version"],
              "source": "discussion", "at": data["now"]} for row in data["roots"]]
    aliases, sources = events(owner, roots, {data["task"]: task})
    database = service.root / "data/bmem.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        root_ids = dict(db.execute("SELECT native_object,root_id FROM ing_roots"))
    root_by_id = {root: label for label, root in root_ids.items()}
    readers = {}
    prior = data.get("prior_deliveries", [])
    read_grants = data["read_grants"]
    for label, allowed in read_grants["task_delivery"].items():
        recipient = next((row["recipient"] for row in prior if row["recipient"].endswith("-" + label)),
                         f"loopback-{label}")
        client = post(owner, "/v2/clients", {
            "host": "governance-replay", "destination": recipient,
            "capabilities": {"load_context": True, "observe_loaded": True},
        }, "reader-" + label)
        auth = {"Authorization": "Bearer " + client["token"], "X-Bmem-Instance": service.instance_id}
        readers[label] = (BrowserSession(service.endpoint), auth, client)
        change(service, owner, client_id=client["client_id"], task_id=task, allow=True)
        change(service, owner, client_id=client["client_id"], source_id=sources["discussion"],
               purpose="task_delivery", allow=True)
        for denied in root_ids.keys() - set(allowed):
            change(service, owner, root_id=root_ids[denied], client_id=client["client_id"],
                   purpose="task_delivery", allow=False)
    for denied in root_ids.keys() - set(read_grants["owner_read"][data["owner"]]):
        change(service, owner, root_id=root_ids[denied], purpose="owner_read", allow=False)

    command_args = args["pv"]["command"]
    root = root_ids[command_args["root"]]
    if case["id"] == "F05-04":
        comparison = args["pv"]["comparison"]
        action = {"kind": "change_grant", "root_id": root, "allow": False,
                  "purpose": command_args["purpose"], "client_id": readers[command_args["reader"]][2]["client_id"],
                  "comparison": {"reader_ids": [readers[label][2]["client_id"] for label in comparison["readers"]],
                                 "task_id": task, "query": comparison["query"]}}
        preview = post(owner, "/v2/operations/preview", action, "compare-only")
        b_args = args["b"]
        package = load(readers[b_args["reader"]], "unchanged-reader", task, b_args["query"])
        forbidden = {item["version"] for item in roots if item["id"] not in read_grants["owner_read"][data["owner"]]}
        hidden_event, = (event for event, alias in aliases.items() if alias in forbidden)
        hidden_handle = "event_" + hidden_event
        expanded = owner.request("/v2/evidence/expand", "POST", {"handle": hidden_handle})
        search = post(owner, "/v2/search", {"query": comparison["query"], "mode": "lexical",
                                            "task_id": task}, "owner-current-cut")
        if expanded[0] != 404 or hidden_handle in {row.get("handle") for row in search["results"]}:
            raise AssertionError("The owner can still read evidence excluded from the comparison")
        return {"pv": preview, "b": package, "owner_evidence": expanded, "owner_search": search}, {
            "pv": {"owner_visible_before_ids": [aliases[event] for event in preview["owner_visible_before_ids"]],
                   "rendered": json.dumps(preview, ensure_ascii=False)},
            "b": {"root_versions": [aliases[item["event_id"]] for item in package["evidence"]]},
        }

    action = {"kind": "forget", "target": {"root_id": root}}
    raw, projected = {}, {}
    if case["id"] == "F05-13":
        for sequence in range(data["authority_watermark"]):
            post(owner, "/v2/statements", {"original": f"Fixture authority checkpoint {sequence + 1}",
                                          "scope": {"kind": "global"}}, f"watermark-{sequence}")
        backup = raw["backup"] = service.cli("backup", "create")
        backup_path = service.root / "backups" / backup["backup_id"] / "data.sqlite3"
        search_path = f"/v2/backups/{backup['backup_id']}/preview/search"
        terms = args["search"]["terms"]
        raw["archived_before"] = {term: post(owner, search_path, {"query": term}, f"before-{i}")
                                  for i, term in enumerate(terms)}
        actual_versions = {item["version"] for result in raw["archived_before"].values() for item in result["results"]}
        if actual_versions != set(data["backup"]["versions"]):
            raise AssertionError("The backup does not contain the original frozen versions")
    else:
        prior_rows = [row for row in prior if row["delivered"]]
        raw["prior"] = [external_delivery(service, owner, readers[row["recipient"].rsplit("-", 1)[1]],
                                          row, task, args["after"]["query"], aliases) for row in prior_rows]
        raw["vectors"] = install_cleanup_fault(service, owner, data, roots, sources["discussion"], root, database)

    raw["pv"] = projected["pv"] = post(owner, "/v2/operations/preview", action, "forget-preview")
    confirmation = {**action, "preview_id": raw["pv"]["preview_id"]}
    raw["ok"] = projected["ok"] = post(owner, "/v2/operations", confirmation, "forget-confirm")
    if not raw["ok"]["effective"]:
        raise AssertionError("The withdrawal did not commit")

    if case["id"] == "F05-13":
        restored = raw["restore"] = service.cli("restore", "preview", backup["backup_id"])
        raw["search"] = {term: post(owner, search_path, {"query": term}, f"after-{i}")
                         for i, term in enumerate(terms)}
        if hashlib.sha256(backup_path.read_bytes()).hexdigest() != backup["sha256"]:
            raise AssertionError("The archive was mutated during preview")
        production = args["prod"]
        package = raw["prod"] = load(readers[production["reader"]], "current-delivery", task, production["query"])
        projected.update(
            restore={**restored, "applied_tombstones": [root_by_id[root] for root in restored["applied_tombstones"]]},
            search={term: {"hit_ids": [aliases[item["event_id"]] for item in result["results"]]}
                    for term, result in raw["search"].items()},
            prod={"root_versions": [aliases[item["event_id"]] for item in package["evidence"]]},
        )
    else:
        cleanup_path = f"/v2/governance/roots/{root}/cleanup"
        receipt_path = f"/v2/governance/roots/{root}/receipt"
        raw["cleanup"] = projected["cleanup"] = post(owner, cleanup_path, {}, "cleanup-failed")
        status, result = owner.request(receipt_path)
        if status != 200:
            raise ValueError(result)
        raw["status"] = projected["status"] = result
        if not result["vector_cleanup_error_code"]:
            raise AssertionError("The failed SQLite operation has no reason in the audit")
        after = args["after"]
        package = raw["after"] = load(readers[after["reader"]], "after-failed-cleanup", task, after["query"])
        projected["after"] = {"rendered": json.dumps(package, ensure_ascii=False)}
        for copy in raw["prior"]:
            recipient = copy["copy"]["recipient"].rsplit("-", 1)[1]
            browser, auth, _ = readers[recipient]
            expanded = browser.request("/v2/evidence/expand", "POST", {"handle": copy["handle"]}, authentication=auth)
            if expanded[0] != 404:
                raise AssertionError("A withdrawn handle remained readable after vector cleanup failed")
            raw.setdefault("previous_handle_checks", []).append(expanded)
        with closing(sqlite3.connect(database)) as db:
            remaining = db.execute("SELECT v.event_id,e.active FROM vector_projection v "
                                   "JOIN ing_event_versions e USING(event_id) WHERE e.root_id=?", (root,)).fetchall()
            if not remaining or any(active for _, active in remaining):
                raise AssertionError("The fault did not leave an inaccessible, uncleared projection")
            db.execute("DROP TRIGGER fail_managed_vector_cleanup")
        raw["inaccessible_projection"] = remaining
        raw["retried"] = post(owner, cleanup_path, {}, "cleanup-recovered")
        if raw["retried"]["vector_cleanup"] != "complete" or raw["retried"]["vector_cleanup_attempts"] <= result["vector_cleanup_attempts"]:
            raise AssertionError("The failed attempt was lost or never recovered")
        raw["replayed_confirmation"] = post(owner, "/v2/operations", confirmation, "forget-confirm")
        if raw["replayed_confirmation"] != raw["ok"]:
            raise AssertionError("Retry rewrote the logical withdrawal receipt")
    return raw, projected


def external_delivery(service, owner, reader, prior, task, query, aliases):
    packet = load(reader, "previous-external-delivery", task, query)
    wire = {name: packet[name] for name in ("evidence", "observations", "statements")}
    with native.fixture_server(fixture("native_provider.json")) as (endpoint, captured):
        body = {"model": "fixture", "tools": [], "input": [{"role": "user", "content": [
            {"type": "input_text", "text": json.dumps(wire, ensure_ascii=False)},
        ]}]}
        with build_opener(ProxyHandler({})).open(Request(
            endpoint + "/v1/responses", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=5) as response:
            response.read()
        observed = json.loads(captured["codex"][-1]["input"][0]["content"][0]["text"])
    handle = next(item["handle"] for item in observed["evidence"] if aliases[item["event_id"]] == prior["version"])
    digest, handles = evidence_manifest(observed)
    browser, auth, client = reader
    delivery = packet["delivery_id"]
    status, receipt = browser.request(f"/v2/deliveries/{delivery}/receipts", "POST", {
        "kind": "loaded", "event_id": prior["id"], "context_epoch": "1", "route_ref": client["route_ref"],
        "model_call_ref": f"fixture-{prior['id']}", "snapshot_id": packet["snapshot_id"],
        "package_id": packet["package_id"], "revision_ids": [row["revision_id"] for row in observed["statements"]],
        "loaded_evidence_refs": handles, "loaded_manifest_digest": digest,
    }, authentication=auth)
    if status != 200 or not receipt.get("evidence_complete"):
        raise ValueError((status, receipt))
    record = post(owner, "/v2/governance/external-copy", {
        "delivery_id": delivery, "receipt_id": receipt["event_id"], "handle": handle,
    }, "record-" + prior["id"])
    if record["recipient"] != prior["recipient"]:
        raise AssertionError("The indexed copy has the wrong transport recipient")
    return {"model_input": observed, "loaded": receipt, "copy": record, "handle": handle}


def install_cleanup_fault(service, owner, data, roots, source, root, database):
    if data["fault"]["vector_cleanup"] != "io_error":
        raise ValueError("Unsupported vector cleanup fault")
    embedding = {"events": roots, "embedding_stub": {
        "model": "isolated-cleanup-fixture",
        "document_vectors": {row["ref"]: [i + 1, 1] for i, row in enumerate(roots)},
    }}
    server, _ = embedding_server(service, embedding)
    service.cli("model", "add", "cleanup-embedding", "--protocol", "ollama", "--endpoint",
                f"http://127.0.0.1:{server.server_port}", "--model-id", embedding["embedding_stub"]["model"])
    service.cli("config", "set", "vector_search.enabled", "true")
    service.cli("model", "test", "cleanup-embedding", "--for", "embed")
    service.cli("model", "use", "cleanup-embedding", "--for", "embed")
    change(service, owner, source_id=source, purpose="embed", allow=True)
    with closing(sqlite3.connect(database)) as db:
        until = monotonic() + 10
        while (count := db.execute("SELECT count(*) FROM vector_projection").fetchone()[0]) != len(roots):
            if monotonic() >= until:
                raise RuntimeError("The managed vector projection was never populated")
            sleep(0.05)
        quoted, = db.execute("SELECT quote(?)", (root,)).fetchone()
        db.execute(f"""CREATE TRIGGER fail_managed_vector_cleanup BEFORE DELETE ON vector_projection
            WHEN EXISTS(SELECT 1 FROM ing_event_versions
                        WHERE event_id=OLD.event_id AND root_id={quoted})
            BEGIN SELECT RAISE(ABORT,'fixture I/O failure on managed vector erase'); END""")
    return {"projected": count, "fault": "managed_vector_delete_aborted"}
