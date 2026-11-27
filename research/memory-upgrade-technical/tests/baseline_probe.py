"""Replay frozen evaluation cases against a selected bmem binary in temporary vaults."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

from score import catalogue, grade


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests" / "v2"))
from test_integration import BmemIntegration, BrowserSession, fixture
import native_hosts as native


def dimensions(body):
    return {"dimensions": {row["key"]: row for row in body["statements"]}}


def lookup(owner, args):
    query = {k: v for k, v in args.items() if k in ("project", "valid_at", "known_at", "task_id")}
    return owner.request("/v2/statements?" + urlencode({**query, "explain": "true"}))


@contextmanager
def isolated(transcript=None):
    service = BmemIntegration()
    try:
        service.setUp()
        if transcript is not None:
            configuration = service.work / "host-fixtures"
            sessions = configuration / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "rollout-fixture.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in transcript) + "\n",
                encoding="utf-8",
            )
            service.environment["BMEM_CONFIG_ROOT"] = str(configuration)
        service.start()
        owner, _ = service.web()
        yield service, owner
    finally:
        service.doCleanups()


def post(owner, path, body, key):
    status, response = owner.request(path, "POST", body, key)
    if status not in (200, 201, 202):
        raise ValueError((path, status, response))
    return response


def source(owner, label):
    registered = post(owner, "/v2/sources", {
        "type": "fixture", "account": label,
        "categories": ["user_message", "assistant_message", "tool_result"],
    }, "source-" + label)
    sid = registered["source_id"]
    command = {"kind": "resume_capture", "source_id": sid}
    preview = post(owner, "/v2/operations/preview", command, "preview-" + label)
    post(owner, "/v2/operations", {**command, "preview_id": preview["preview_id"]}, "enable-" + label)
    return sid


def events(owner, entries, tasks=None):
    by_source = defaultdict(list)
    input_aliases = {}
    for item in entries:
        row = {
            "native_object": item.get("native_object", item.get("root", item.get("ref", item.get("id")))),
            "native_version": item.get("native_version", item.get("version", item.get("version_key", "1"))),
            "actor_kind": item.get("actor_kind", "user_message"),
            "at": item.get("at", "2026-11-23T01:00:00Z"),
        }
        for key in ("text", "checks"):
            if key in item:
                row[key] = item[key]
        if tasks and item.get("task") in tasks:
            row["task_id"] = tasks[item["task"]]
        label = item.get("source", "default")
        by_source[label].append(row)
        input_aliases[label, row["native_object"], row["native_version"]] = item.get("ref", row["native_object"])
    captured = {}
    sources = {}
    for label, rows in by_source.items():
        sid = sources[label] = source(owner, label)
        receipt = post(owner, "/v2/events", {"source_id": sid, "next_cursor": "c1", "events": rows}, label)
        if len(receipt["events"]) != len(rows):
            raise ValueError("the capture receipt does not account for every submitted event")
        captured.update({entry["event_id"]: input_aliases[label, row["native_object"], row["native_version"]]
                         for entry, row in zip(receipt["events"], rows)})
    return captured, sources


def new_bound(service, owner, task=None, label=None):
    suffix = "-" + label if label else ""
    client = post(owner, "/v2/clients", {
        "host": "baseline-test-adapter", "destination": "loopback" + suffix,
        "capabilities": {"load_context": True, "observe_loaded": True},
    }, "baseline-client" + suffix)
    service.operation(owner, {"kind": "change_grant", "client_id": client["client_id"],
                              "share_statements": True}, "share" + suffix)
    if task:
        service.operation(owner, {"kind": "change_grant", "client_id": client["client_id"],
                                  "task_id": task, "allow": True}, "task-access" + suffix)
    auth = {"Authorization": "Bearer " + client["token"], "X-Bmem-Instance": service.instance_id}
    return BrowserSession(service.endpoint), auth, client


def load_bound(browser, auth, client, label, task=None, evidence_budget=4096, **request_extra):
    request = {"session_id": label, "context_epoch": "1", "route_ref": client["route_ref"],
               "budget": {"statement_tokens": 4096, "evidence_tokens": evidence_budget}}
    if task:
        attempt = browser.request(f"/v2/tasks/{task}/attempts", "POST", request, "attempt-" + label, authentication=auth)
        if attempt[0] != 201:
            raise ValueError(attempt)
        request.update(task_id=task, attempt_id=attempt[1]["attempt_id"])
    return browser.request("/v2/context/load", "POST", {**request, **request_extra}, "load-" + label, authentication=auth)


def collect_statements(case, service, owner, *, freeze=None, web_snapshot=None):
    raw = {}
    projected = {}
    written = {}
    statements = {row["id"]: row for row in case["fixture"]["statements"]}
    tasks = {row["id"]: post(owner, "/v2/tasks", row, row["id"])["task_id"]
             for row in case["fixture"].get("tasks", [])}
    for step in case["steps"]:
        name, args = step["id"], dict(step["args"])
        if args.get("task_id") in tasks:
            args["task_id"] = tasks[args["task_id"]]
        if args.get("statement") in statements:
            item = statements[args["statement"]]
            if freeze is not None:
                freeze(item["received_at"])
            body = {k: item[k] for k in ("original", "scope", "share", "valid_from", "timezone") if k in item}
            if reference := args.get("corrects_revision_id"):
                step_id, dimension = reference.removeprefix("@").split(":", 1)
                revision = next((row["revision_id"] for row in written.get(step_id, {}).get("dimensions", [])
                                 if row["key"] == dimension), None)
                if revision is None:
                    continue
                body["corrects_revision_id"] = revision
            if item.get("task_id") in tasks:
                body["task_id"] = tasks[item["task_id"]]
            status, response = owner.request("/v2/statements", "POST", body, args.get("idempotency_key", name))
            raw[name] = {"status": status, "body": response}
            written[name] = projected[name] = response
        elif name == "propose":
            bodies = {}
            for label in args["statements"]:
                item = statements[label]
                if freeze is not None:
                    freeze(item["received_at"])
                bodies[label] = post(owner, "/v2/statements", {
                    k: item[k] for k in ("original", "scope", "share", "valid_from", "timezone") if k in item
                }, label)
                written[f"{name}:{label}"] = bodies[label]
            raw[name] = projected[name] = bodies
        elif name in ("before", "after") and case["id"] == "F01-13":
            audits = {route: owner.request("/v2/" + route)[1] for route in ("statements", "deliveries")}
            db_path = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            with sqlite3.connect(db_path, uri=True) as db:
                audits["receipt_count"] = db.execute("SELECT count(*) FROM context_receipts").fetchone()[0]
            raw[name] = audits
            if name == "before":
                projected[name] = audits
            else:
                previous = raw["before"]
                projected[name] = {"changes": {
                    "authority_seq": audits["statements"]["change_seq"] - previous["statements"]["change_seq"],
                    "deliveries": len(audits["deliveries"]["deliveries"]) - len(previous["deliveries"]["deliveries"]),
                    "receipts": audits["receipt_count"] - previous["receipt_count"],
                }}
        elif name == "cli":
            body = service.cli("statement", "list", "--project", args["project"], "--valid-at", args["valid_at"])
            raw[name] = body
            selected = dimensions(body)["dimensions"].get("code_example.language", {})
            projected[name] = {"language": selected.get("value")}
        elif name in ("detail", "pending", "explain") and args.get("statement", "").startswith("@"):
            item = written.get(args["statement"][1:], {})
            if not item.get("statement_id"):
                continue
            status, body = owner.request(f"/v2/statements/{item['statement_id']}")
            raw[name] = {"status": status, "body": body}
            if status == 200:
                projected[name] = {
                    **body,
                    "original": body["original"], "pending_text": body["original"],
                    "pending_keys": [x["key"] for x in body["pending"]],
                    "pending_reasons": [x["reason"] for x in body["pending"]],
                    "missing_fields": ["project" if x["reason"] == "project_required" else x["reason"]
                                       for x in body["pending"]],
                }
        elif name == "proof" and case["id"] == "F01-02":
            matched = projected.get("fri", {}).get("dimensions", {}).get("code_example.language", {})
            boundary = matched.get("valid_until")
            if boundary:
                projected[name] = {"timezone": matched.get("timezone"),
                                   "valid_until_utc": re.sub(r"\.0+Z$", "Z", boundary),
                                   "clause": matched.get("clause"), "has_revision_id": bool(matched.get("revision_id"))}
        elif name == "forget" and "replace" in written and written["replace"].get("statement_id"):
            command = {"kind": "forget", "target": {"statement_id": written["replace"]["statement_id"]}}
            raw[name] = projected[name] = service.operation(owner, command, "forget-rule")
        elif name == "close":
            status, body = owner.request(f"/v2/tasks/{args['task_id']}/close", "POST", {}, "close")
            raw[name] = {"status": status, "body": body}
            if status == 200:
                projected[name] = body
        elif name == "web" and web_snapshot is not None:
            observation = web_snapshot(args)
            raw[name] = observation
            if "revision_id" in observation:
                cli_chosen = dimensions(raw["cli"])["dimensions"].get("code_example.language", {})
                projected[name] = {
                    "language": observation["language"],
                    "same_revision_as_cli": observation["revision_id"] == cli_chosen.get("revision_id"),
                }
        elif args.get("project"):
            status, response = lookup(owner, args)
            raw[name] = {"status": status, "body": response}
            if status == 200:
                projected[name] = dimensions(response)
                if name == "web" and case["id"] == "F01-13":
                    chosen = projected[name]["dimensions"].get("code_example.language", {})
                    projected[name]["language"] = chosen.get("value")
                    if chosen:
                        cli_chosen = dimensions(raw["cli"])["dimensions"].get("code_example.language", {})
                        projected[name]["same_revision_as_cli"] = chosen["revision_id"] == cli_chosen.get("revision_id")
    if case["id"] == "F01-10":
        with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            count = db.execute("SELECT count(*) FROM authority_revisions").fetchone()[0]
        projected["audit"] = {
            "same_statement_and_revisions": all(written["first"][key] == written["retry"][key]
                                               for key in ("statement_id", "dimensions")),
            "revision_count": count,
        }
    return raw, projected


def no_resurrection(case, service, owner):
    data = case["fixture"]
    task = post(owner, "/v2/tasks", {"anchor": data["task"], "project": "青禾"}, "task")["task_id"]
    _, sources = events(owner, [{**root, "task": data["task"]} for root in data["roots"]], {data["task"]: task})
    aliases = {}
    for item in data["rules"]:
        linked = item.get("corrects_revision_id")
        body = {"original": item["text"], "scope": {"kind": item["scope"]}, "share": "authorized_assistants"}
        if linked:
            body["corrects_revision_id"] = aliases[linked]["dimensions"][0]["revision_id"]
        aliases[item["id"]] = post(owner, "/v2/statements", body, item["id"])
    ids = {entry["dimensions"][0]["revision_id"]: alias for alias, entry in aliases.items()}
    bound = new_bound(service, owner, task)
    service.operation(owner, {"kind": "change_grant", "source_id": sources["default"],
                              "purpose": "task_delivery", "client_id": bound[2]["client_id"],
                              "allow": True}, "source-read")
    before_status, before = load_bound(*bound, "before", task)
    payload = {"kind": "forget", "target": {"statement_id": aliases["new"]["statement_id"]}}
    preview = post(owner, "/v2/operations/preview", payload, "pv")
    done = post(owner, "/v2/operations", {**payload, "preview_id": preview["preview_id"]}, "ok")
    after_status, after = load_bound(*bound, "after", task)
    if before_status != 200 or after_status != 200:
        raise ValueError((before, after))
    with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        lineage = db.execute("SELECT id, previous, state FROM authority_revisions ORDER BY seq").fetchall()
    return {"seed": aliases, "before": before, "pv": preview, "ok": done, "after": after,
            "lineage_rows": lineage}, {
        "before": {"active_rule_ids": [ids[row["revision_id"]] for row in before["statements"]]},
        "pv": preview, "ok": done,
        "after": {"active_rule_ids": [ids[row["revision_id"]] for row in after["statements"]]},
        "audit": {"superseded_by": next((ids[child] for child, previous, _ in lineage
                                        if ids.get(previous) == "old"), None)},
    }


def root_comparison_probe(case, service, owner):
    data = case["fixture"]
    sample = case["steps"][0]["args"]
    task = post(owner, "/v2/tasks", {"anchor": data["task"], "project": "青禾"}, "task")["task_id"]
    aliases, sources = events(owner, [{"native_object": row["id"], "native_version": row["version"],
                                      "text": row["text"], "task": "T", "source": "discussion"}
                                     for row in data["roots"]], {"T": task})
    readers = {label: new_bound(service, owner, task, label) for label in sample["comparison"]["readers"]}
    for label, (_, _, reader) in readers.items():
        service.operation(owner, {"kind": "change_grant", "client_id": reader["client_id"],
                                  "source_id": sources["discussion"], "purpose": "task_delivery",
                                  "allow": True}, "source-" + label)
    db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    event = next(event for event, alias in aliases.items() if alias == sample["command"]["root"])
    with sqlite3.connect(db_uri, uri=True) as db:
        rid = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (event,)).fetchone()[0]
    command = {"kind": "change_grant", "client_id": readers[sample["command"]["reader"]][2]["client_id"],
               "purpose": sample["command"]["purpose"], "root_id": rid, "allow": False}
    params = {"task_id": task, "reader_ids": [bound[2]["client_id"] for bound in readers.values()]}

    def preview(query, budget, key):
        return post(owner, "/v2/operations/preview", {**command, "comparison": {
            **params, "query": query, "budget": budget}}, key)

    versions = {row["id"]: row["version"] for row in data["roots"]}

    def refs(items):
        return [versions[aliases[item["event_id"]]] for item in items]

    def comparison(response):
        return {label: {phase: refs(items) for phase, items in response["comparison"][bound[2]["client_id"]].items()}
                for label, bound in readers.items()}

    exact = preview(sample["comparison"]["query"], sample["comparison"]["budget"], "original")
    with sqlite3.connect(db_uri, uri=True) as db:
        stored = db.execute("SELECT request FROM governance_previews WHERE id=?", (exact["preview_id"],)).fetchall()
        if "comparison_snapshot" in exact:
            snapshots = db.execute("SELECT model_calls FROM governance_comparison_snapshots WHERE preview_id=? AND snapshot_digest=?",
                                   (exact["preview_id"], exact["comparison_snapshot"])).fetchall()
        else:
            snapshots = []
        deliveries = db.execute("SELECT count(*) FROM context_deliveries").fetchone()[0]
        receipts = db.execute("SELECT count(*) FROM context_receipts").fetchone()[0]
    audit = {"registered_preview_count": len(stored), "new_deliveries": deliveries, "new_receipts": receipts,
             "stored_command": {"kind": "deny_root", "reader": sample["command"]["reader"],
                                "purpose": command["purpose"], "root": aliases[event]}
             if stored and json.loads(stored[0][0]) == command else None}
    if snapshots:
        audit.update(comparison_snapshot_count=len(snapshots), model_calls=sum(row[0] for row in snapshots))
    actual_args = case["steps"][2]["args"]
    status, actual = load_bound(*readers[actual_args["reader"]], "actual", task,
                                evidence_query=actual_args["query"])
    return {"preview": exact, "audit": {**audit, "stored_request": stored},
            "actual": {"status": status, "body": actual}}, {
        "preview": comparison(exact), "audit": audit,
        "actual": {"root_versions": refs(actual["evidence"])} if status == 200 else {"status": status},
    }


def replay_cooling(case, service, owner):
    data = case["fixture"]
    community = case["id"] == "X01-09"
    label = "retention-candidate" if community else data["task"]
    task = post(owner, "/v2/tasks", {"anchor": label, "project": label}, "retention-task")["task_id"]
    roots = ([item for item in data["records"] if item["kind"] == "episode"] if community else data["roots"])
    aliases, sources = events(owner, [{**root, "task": label} for root in roots], {label: task})
    bound = new_bound(service, owner, task)
    for source_id in sources.values():
        service.operation(owner, {"kind": "change_grant", "source_id": source_id, "purpose": "task_delivery",
                                  "client_id": bound[2]["client_id"], "allow": True}, "retention-grant")
    rule = next(item for item in data["records"] if item["kind"] == "active_declaration") if community else data["rules"][0]
    statement = post(owner, "/v2/statements", {"original": rule["text"], "scope": {"kind": "project", "project": label},
                                               "share": "authorized_assistants"}, "retention-rule")
    revisions = {item["revision_id"] for item in statement["dimensions"]}
    db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        rows = list(db.execute("SELECT root_id,event_id FROM ing_event_versions WHERE task_id=?", (task,)))
        roots_by_alias = {aliases[event]: root for root, event in rows}
    versions = {root["id"]: root.get("version", root["id"]) for root in roots}

    def labels(package):
        return [versions[aliases[item["event_id"]]] for item in package["evidence"] if item["event_id"] in aliases]

    def rules(package):
        return [rule["id"] for item in package["statements"] if item["revision_id"] in revisions]

    def load(name, **extra):
        status, package = load_bound(*bound, name, task, **extra)
        if status != 200:
            raise RuntimeError(f"retention task package status {status}")
        return package

    if community:
        receipts = [service.operation(owner, {"kind": "cool_episode", "root_id": roots_by_alias[entry]}, "cool-" + entry)
                    for entry in data["retention_proposal"]["cool"] if entry in roots_by_alias]
        default = load("default")
        exact = load("search", evidence_query="E170")
        with sqlite3.connect(db_uri, uri=True) as db:
            tombstones = db.execute("SELECT count(*) FROM ing_root_revocations").fetchone()[0]
        return {"cool": {"receipts": receipts, "default": default, "explicit": exact, "tombstones": tombstones}}, {
            "cool": {"current_declarations": rules(default), "default_episode_refs": labels(default),
                     "explicit_search_refs": labels(exact), "physical_deletions": tombstones},
        }

    replay, view = {}, {}
    for step in case["steps"]:
        n, args = step["id"], step["args"]
        if "command" in args:
            command = {"kind": args["command"]["kind"], "root_id": roots_by_alias[args["command"]["root"]]}
            if "preview_step" in args:
                response = post(owner, "/v2/operations", {**command, "preview_id": replay[args["preview_step"]]["preview_id"]}, n)
            else:
                response = post(owner, "/v2/operations/preview", command, n)
            replay[n] = view[n] = response
        elif n == "find":
            replay[n] = load(n, evidence_query=args["query"])
            view[n] = {"text": "\n".join(item["text"] for item in replay[n]["evidence"])}
        else:
            replay[n] = load(n)
            view[n] = {"root_versions": labels(replay[n]), "active_rule_ids": rules(replay[n])}
    return replay, view


def replay_root_restriction(case, service, owner):
    data = case["fixture"]
    label = data["task"]
    task = post(owner, "/v2/tasks", {"anchor": label}, "history-task")["task_id"]
    aliases, sources = events(owner, [{**row, "task": label} for row in data["roots"]], {label: task})
    browser, authorization, reader = new_bound(service, owner, task)
    for sid in sources.values():
        service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "task_delivery",
                                  "client_id": reader["client_id"], "allow": True}, "history-reader")
    if case["id"] == "F05-02":
        second = new_bound(service, owner, task, "second-recipient")
        for sid in sources.values():
            service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "task_delivery",
                                      "client_id": second[2]["client_id"], "allow": True}, "second-reader")
        args = {row["id"]: row["args"] for row in case["steps"]}
        versions = {row["id"]:row["version"] for row in data["roots"]}
        target = next(event for event, alias in aliases.items() if alias == args["prepare"]["root"])
        uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            rid, = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (target,)).fetchone()
        status, prepared = load_bound(browser, authorization, reader, "prepared", task)
        if status != 200:
            raise RuntimeError(f"prepare context status {status}")
        command = {"kind": "change_grant", "root_id": rid, "purpose": args["pv"]["command"]["purpose"],
                   "client_id": reader["client_id"], "allow": False}
        pv = post(owner, "/v2/operations/preview", command, "prepared-preview")
        ok = post(owner, "/v2/operations", {**command, "preview_id": pv["preview_id"]}, "prepared-commit")
        old_status, old = browser.request("/v2/evidence/expand", "POST", {"handle": "event_"+target}, authentication=authorization)
        resent_status, resent = load_bound(browser, authorization, reader, "prepared", task)
        if resent_status != 409:
            raise RuntimeError(f"stale queued delivery unexpectedly returned {resent_status}")
        a_status, a = load_bound(browser, authorization, reader, "replacement", task, evidence_query=args["a"]["query"])
        b_status, b = load_bound(*second, "independent-reader", task, evidence_query=args["b"]["query"])
        if a_status != 200 or b_status != 200:
            raise RuntimeError(f"scoped readers did not load ({a_status},{b_status})")
        with native.fixture_server(fixture("native_provider.json")) as (endpoint, captured):
            body = json.dumps({"model":"fixture","messages":[{"role":"user","content":json.dumps(a["evidence"],ensure_ascii=False)}]}).encode()
            with build_opener(ProxyHandler({})).open(Request(endpoint+"/v1/messages", data=body, headers={"Content-Type":"application/json"}),timeout=5) as response:
                response.read()
        submitted = captured["claude-code"][0]["messages"][0]["content"]

        def refs(package):
            return [versions[aliases[item["event_id"]]] for item in package["evidence"]]

        return {"prepare":prepared,"pv":pv,"ok":ok,"open":{"status":old_status,"body":old},
                "deliver":{"stale_status":resent_status,"stale_response":resent,"actual_model_input":submitted},
                "a":a,"b":b}, {"prepare":{"prepared_versions":refs(prepared)},"pv":pv,"ok":ok,"open":old,
                    "deliver":{"model_input":submitted},"a":{"root_versions":refs(a)},"b":{"root_versions":refs(b)}}
    if case["id"] == "F05-08":
        uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            roots = {aliases[event]: root for root, event in db.execute(
                "SELECT root_id,event_id FROM ing_event_versions WHERE task_id=?", (task,))}
        refs = {root: alias for alias, root in roots.items()}
        versions = {item["id"]: item["version"] for item in data["roots"]}
        requested = case["steps"][0]["args"]["command"]
        discussion = requested["discussion"]
        for label in data["discussions"][discussion]:
            post(owner, "/v2/governance/discussion-root", {"discussion_id": discussion, "root_id": roots[label]}, "initial-" + label)
        command = {"kind": requested["kind"], "client_id": reader["client_id"], "purpose": requested["purpose"],
                   "discussion_id": discussion}
        preview = post(owner, "/v2/operations/preview", command, "discussion-preview")
        diff = case["steps"][1]["args"]
        change = post(owner, "/v2/governance/discussion-root", {"discussion_id": diff["discussion"], "root_id": roots[diff["add_root"]]}, "discussion-drift")
        status, rejected = owner.request("/v2/operations", "POST", {**command, "preview_id": preview["preview_id"]}, "old-preview")
        loaded, package = load_bound(browser, authorization, reader, "after-drift", task,
                                     evidence_query=case["steps"][3]["args"]["query"])
        fresh = post(owner, "/v2/operations/preview", command, "new-preview")
        return {"pv": preview, "change": change, "confirm_old": {"status": status, "body": rejected},
                "actual": {"status": loaded, "body": package}, "fresh": fresh}, {
                    "pv": {"selected_roots": [refs[root] for root in preview["selected_roots"]]}, "change": change,
                    "confirm_old": {"requires_new_preview": status == 409 and rejected.get("code") == "revision_changed"},
                    "actual": {"root_versions": [versions[aliases[item["event_id"]]] for item in package.get("evidence", [])]},
                    "fresh": {"selected_roots": [refs[root] for root in fresh["selected_roots"]]},
                }
    root = case["steps"][0]["args"]["command"]["root"]
    target = next(event for event, alias in aliases.items() if alias == root)
    uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        root_id, = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (target,)).fetchone()
    command = {"kind": "change_grant", "client_id": reader["client_id"], "purpose": "task_delivery",
               "root_id": root_id, "allow": False}
    preview = post(owner, "/v2/operations/preview", command, "history-preview")
    committed = post(owner, "/v2/operations", {**command, "preview_id": preview["preview_id"]}, "history-commit")
    if case["id"] == "F05-03":
        recipient = new_bound(service, owner, task, "comparison-other")[2]
        status, package = load_bound(browser, authorization, reader, "restricted", task,
                                     evidence_query=case["steps"][2]["args"]["query"])
        requested = {**command, "comparison": {"task_id": task, "reader_ids": [reader["client_id"], recipient["client_id"]],
                                               "query": case["steps"][3]["args"]["query"], "budget": 128}}
        denied, response = browser.request("/v2/operations/preview", "POST", requested, "restricted-comparison", authentication=authorization)
        projected = {"rendered": "\n".join(row["text"] for row in package.get("evidence", [])),
                     **{key: package[key] for key in ("blocked_count", "blocked_items") if key in package}}
        return {"pv": preview, "ok": committed, "a": {"status": status, "body": package},
                "compare": {"status": denied, "body": response}}, {"pv": preview, "ok": committed, "a": projected,
                "compare": {**response, "status": "forbidden" if denied == 403 else denied}}
    status, history = browser.request(f"/v2/tasks/{task}/handoff?" + urlencode({
        "as_of": case["steps"][2]["args"]["as_of"]}), authentication=authorization)
    current = post(owner, "/v2/evidence/expand", {"handle": "event_" + target}, "history-owner")
    evidence = history.get("evidence", [])
    versions = {row["id"]: row["version"] for row in data["roots"]}
    return {"pv": preview, "ok": committed, "history": {"status": status, "body": history}, "owner": current}, {
        "pv": preview, "ok": committed, "history": {"status": status,
            "root_versions": [versions[aliases[item["event_id"]]] for item in evidence],
            "rendered": "\n".join(item["text"] for item in evidence)}, "owner": current,
    }


def replay_empty_restore(case):
    from replay_clock import FrozenClock

    service = BmemIntegration()
    data = case["fixture"]
    try:
        service.setUp()
        clock = FrozenClock(service.work, service.environment, data["backup"]["at"])
        service.start()
        owner, _ = service.web()
        events(owner, [{**row, "at": clock.history[0]} for row in data["roots"]])
        backup = service.cli("backup", "create")
        with isolated() as (empty, other_owner):
            folder = service.root / "backups" / backup["backup_id"]
            shutil.copytree(folder, empty.root / "backups" / folder.name)
            restored = empty.cli("restore", "preview", folder.name)
            reader = post(other_owner, "/v2/clients", {"host": "restore-check", "destination": "empty-vault"}, "restore-check")
            browser = BrowserSession(empty.endpoint)
            auth = {"Authorization": "Bearer " + reader["token"], "X-Bmem-Instance": empty.instance_id}
            status, response = browser.request("/v2/search", "POST", {"query": case["steps"][1]["args"]["query"]},
                                               authentication=auth)
            return {"backup": backup, "restore": restored, "read": {"status": status, "body": response}}, {
                        "restore": restored, "read": {**response, "normal_read_enabled": status == 200},
                    }
    finally:
        service.doCleanups()


def replay_inflight_forget(case, service, owner):
    from replay_history import extractor

    data = case["fixture"]
    steps = {row["id"]: row["args"] for row in case["steps"]}
    candidate = steps["return"]["candidate"]
    calls, entered, release, responded = extractor(service, {"on_call": "block_until_release",
        "candidate_inputs": [{"proposal": candidate["text"]}]})
    label = data["task"]
    task = post(owner, "/v2/tasks", {"anchor": label}, "late-worker-task")["task_id"]
    sid = source(owner, "late-worker")
    bound = new_bound(service, owner, task)
    service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "task_delivery",
                              "client_id": bound[2]["client_id"], "allow": True}, "late-worker-reader")
    service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "extract", "allow": True}, "late-worker-grant")
    roots = {row["id"]: row for row in data["roots"]}

    def page(aliases, cursor, end):
        return post(owner, "/v2/events", {"source_id": sid, "cursor": cursor, "next_cursor": end,
            "events": [{"native_object": roots[ref]["id"], "native_version": roots[ref]["version"],
                        "text": roots[ref]["text"], "task_id": task, "actor_kind": "user_message"} for ref in aliases]}, end)

    root = steps["pv"]["command"]["root"]
    first = page([root], None, "first-root")
    if not entered.wait(8):
        raise RuntimeError("the approved worker did not claim the fixture input")
    second = page([ref for ref in roots if ref != root], "first-root", "remaining-roots")
    uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    event = first["events"][0]["event_id"]
    with sqlite3.connect(uri, uri=True) as db:
        root_id, = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (event,)).fetchone()
        job, state = db.execute("SELECT job_id,status FROM ing_jobs WHERE event_id=?", (event,)).fetchone()
    command = {"kind": "forget", "target": {"root_id": root_id}}
    preview = post(owner, "/v2/operations/preview", command, "late-worker-preview")
    applied = post(owner, "/v2/operations", {**command, "preview_id": preview["preview_id"]}, "late-worker-apply")
    release.set()
    if not responded.wait(8):
        raise RuntimeError("fixture extractor did not return the in-flight proposal")
    daemon = service.daemon
    service.stop()
    if daemon.returncode != 0:
        raise RuntimeError("service did not drain the late proposal")
    service.start()
    status, after = load_bound(*bound, "late-worker-after", task)
    with sqlite3.connect(uri, uri=True) as db:
        reason, = db.execute("SELECT fail_reason FROM ing_jobs WHERE job_id=?", (job,)).fetchone()
        version = dict(db.execute("SELECT event_id,native_version FROM ing_event_versions WHERE task_id=?", (task,)))
        publications = [row[0] for row in db.execute("SELECT candidate_id FROM ing_candidates WHERE job_id=?", (job,))]
    audit = {"published_candidate_ids": publications, "termination_reason": reason}
    return {"lease": {"job_id":job,"state":state,"call":calls[0]}, "pv":preview,"ok":applied,
            "return":{"worker_returned":True,"captured_candidate":candidate["id"]},
            "after":{"status":status,"body":after},"audit":audit,"pages":[first,second]}, {
        "lease":{"job_id":job,"state":state},"pv":preview,"ok":applied,"return":{"worker_returned":True},
        "after":{"root_versions":[version[item["event_id"]] for item in after.get("evidence",[])],
                 "rendered":"\n".join(item["text"] for item in after.get("evidence",[]))},"audit":audit,
    }


def upgrade_old_statements(case, binary):
    old = Path(os.environ.get("BMEM_REPLAY_BASE_BIN", REPO / "src/v2/bmem/target/release/bmem")).resolve()
    if not old.is_file() or old == binary:
        return {}, {}
    original = os.environ["BMEM_V2_TEST_BIN"]
    os.environ["BMEM_V2_TEST_BIN"] = str(old)
    try:
        with isolated() as (service, owner):
            data = case["fixture"]["statements"]
            def append(browser, row):
                body = {key: row[key] for key in ("original", "scope", "share")}
                return post(browser, "/v2/statements", body, row["id"])
            saved = append(owner, data[0])
            service.stop()
            service.binary = str(binary)
            service.start()
            owner, _ = service.web()
            before_status, before = lookup(owner, {"project": "青禾"})
            if before_status != 200:
                raise ValueError(before)
            added = append(owner, data[1])
            after_status, after = lookup(owner, {"project": "青禾"})
            if after_status != 200:
                raise ValueError(after)
            return {"old_write": saved, "upgrade": {"old_sha256": hashlib.sha256(old.read_bytes()).hexdigest()},
                    "legacy": before, "new": added, "all": after}, {
                "old_write": saved, "upgrade": {"restarted": True}, "legacy": dimensions(before),
                "new": added, "all": dimensions(after),
            }
    finally:
        os.environ["BMEM_V2_TEST_BIN"] = original


def lexical_probe(case, service, owner):
    fixture_data = case["fixture"]
    mapping, sources = events(owner, fixture_data.get("events", []))
    raw, projected = {}, {}
    stub = fixture_data.get("embedding_stub")
    if case["id"] == "F04-02":
        server, calls = embedding_server(service, fixture_data)
        service.cli("model", "add", "fixture-candidate", "--protocol", "ollama", "--endpoint",
                    f"http://127.0.0.1:{server.server_port}", "--model-id", stub["model"])
        service.cli("config", "set", "vector_search.enabled", "true")
        service.cli("model", "test", "fixture-candidate", "--for", "embed")
        service.cli("model", "use", "fixture-candidate", "--for", "embed")
        for label, sid in sources.items():
            service.operation(owner, {"kind": "change_grant", "source_id": sid,
                                      "purpose": "embed", "allow": True}, "embed-" + label)
        deadline = monotonic() + 20
        db_path = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(db_path, uri=True) as db:
            while db.execute("SELECT count(*) FROM vector_projection").fetchone()[0] < len(mapping):
                if monotonic() >= deadline:
                    raise RuntimeError(f"Projection is incomplete for {case['id']}")
                sleep(0.05)
        raw["embedding_calls"] = calls
    for step in case["steps"]:
        args = step["args"]
        if args.get("reader") != fixture_data.get("owner") or "mode" not in args:
            continue
        body = {k: args[k] for k in ("query", "mode", "candidate_limit") if k in args}
        status, result = owner.request("/v2/search", "POST", body, step["id"])
        raw[step["id"]] = {"status": status, "body": result}
        if status == 200:
            hits = result["results"]
            projected[step["id"]] = {"refs": [mapping.get(hit["event_id"], hit["event_id"])
                                             for hit in hits if "event_id" in hit], "hits": hits}
    if case["id"] == "F04-16":
        event = next(identifier for identifier, alias in mapping.items() if alias == "old")
        status, evidence = owner.request("/v2/evidence/expand", "POST", {"handle": "event_" + event})
        raw["history"] = {"status": status, "body": evidence}
        if status == 200:
            db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            with sqlite3.connect(db_uri, uri=True) as db:
                version = db.execute("SELECT native_version FROM ing_event_versions WHERE event_id=?",
                                     (evidence["event_id"],)).fetchone()[0]
            projected["history"] = {"version": version, "text": evidence["text"]}
    return raw, projected


def replay_vector_timeout(case, service, owner):
    data = case["fixture"]
    aliases, sources = events(owner, data["events"])
    server, calls = embedding_server(service, data, slow_query=case["steps"][2]["args"]["query"])
    raw, view = {}, {}
    deadline_ms = case["steps"][1]["args"]["query_timeout_ms"]
    for step in case["steps"]:
        name, args = step["id"], step["args"]
        if name == "toggle":
            service.cli("model", "add", "timeout-candidate", "--protocol", "ollama", "--endpoint",
                        f"http://127.0.0.1:{server.server_port}", "--model-id", data["embedding_stub"]["model"])
            service.cli("config", "set", "vector_search.enabled", "true")
            service.cli("model", "test", "timeout-candidate", "--for", "embed")
            service.cli("model", "use", "timeout-candidate", "--for", "embed")
            for label, sid in sources.items():
                service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "embed", "allow": True}, "embed-" + label)
            deadline = monotonic() + 15
            uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as db:
                while not db.execute("SELECT count(*) FROM vector_projection").fetchone()[0]:
                    if monotonic() >= deadline:
                        raise RuntimeError("embedding did not index the sample document")
                    sleep(0.05)
            raw[name] = view[name] = {"indexed": True, "query_timeout_ms": deadline_ms}
        else:
            started = monotonic()
            status, response = owner.request("/v2/search", "POST", {"query": args["query"],
                "mode": args["mode"], "query_timeout_ms": deadline_ms}, name)
            raw[name] = {"status": status, "body": response, "elapsed_ms": round((monotonic()-started)*1000), "calls": list(calls)}
            if status == 200:
                view[name] = {"refs": [aliases.get(row.get("event_id")) for row in response["results"]],
                              "vector_status": response.get("vector_status"), "query_embedding_calls": calls.count(args["query"])}
    return raw, view


def embedding_server(service, data, slow_query=None):
    stub = data["embedding_stub"]
    vectors = {row["text"]: stub["document_vectors"][row["ref"]] for row in data["events"]}
    vectors.update(stub.get("query_vectors", {}))
    calls = []

    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = payload["input"][0]
            calls.append(text)
            if text == slow_query:
                sleep(0.1)
            response = json.dumps({"embeddings": [vectors.get(text, [1, 0])]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def close():
        server.shutdown()
        thread.join()
        server.server_close()

    service.addCleanup(close)
    return server, calls


def expand_events(data):
    gen = data.get("generate_before_events")
    if not gen:
        return data["events"]
    start = datetime.fromisoformat(gen["at"].split(" + ")[0].replace("Z", "+00:00"))
    return [
        {**gen, "native_object": gen["native_object"].format(n=n), "text": gen["text"].format(n=n),
         "at": (start + timedelta(seconds=n)).isoformat()}
        for n in range(gen["start"], gen["stop_inclusive"] + 1)
    ] + data["events"]


def evidence_refs(items, aliases):
    refs = []
    for item in items:
        native_id = aliases.get(item["event_id"])
        if native_id is None:
            continue
        if item["actor_kind"] == "tool_result":
            checks = json.loads(item["text"])
            checks = checks.get("checks", checks)
            refs.extend(f"{native_id}#/checks/{field}" for field in checks)
        else:
            refs.append(native_id + "#/text")
    return refs


def replay_handoff_api(case, service, owner):
    data = case["fixture"]
    tasks = {t["id"]: post(owner, "/v2/tasks", {"anchor": t["anchor"], "project": t["project"]},
                           t["id"])["task_id"] for t in data["tasks"]}
    task = tasks["T1"]
    aliases, sources = events(owner, expand_events(data), tasks)
    bound = new_bound(service, owner, task)
    for label, sid in sources.items():
        service.operation(owner, {"kind": "change_grant", "source_id": sid, "purpose": "task_delivery",
                                  "client_id": bound[2]["client_id"], "allow": True}, "grant-" + label)
    for n, text in enumerate(data.get("rules", [])):
        post(owner, "/v2/statements", {"scope": {"kind": "global"}, "original": text,
                                     "share": "authorized_assistants"}, "rule-" + str(n))
    raw, projected = {}, {"seed": {"accepted_event_count": len(aliases)}}
    for step in case["steps"]:
        if "budget" not in step["args"]:
            continue
        label = step["id"]
        amount = step["args"]["budget"]["evidence_tokens"]
        status, body = load_bound(*bound, label, task=task, evidence_budget=amount)
        raw[label] = {"status": status, "body": body}
        if status == 200:
            projected[label] = {"refs": evidence_refs(body["evidence"], aliases), **body}
    if case["id"] == "F02-08":
        tiny = projected["tiny"]
        projected["audit"] = {"evidence_budget_exhausted": tiny["package_state"] in ("budget_zero", "budget_truncated"),
                              "statement_values": [entry["value"] for entry in tiny["statements"]]}
    if case["id"] == "F02-09":
        with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            ordered = db.execute("SELECT native_object FROM ing_event_versions WHERE task_id=? AND active=1 "
                                 "ORDER BY occurred_at,event_id", (task,)).fetchall()
        projected["count"] = {"active_events": len(ordered), "e51_rank_by_time": ordered.index(("e51",)) + 1}
        projected["expand"] = {"count": len(ordered)}
    return raw, projected


def replay_native(case, binary):
    data = case["fixture"]
    identifier = case["id"]
    provider = fixture("native_provider.json")
    traces, projected = {}, {}
    host = "codex"
    with tempfile.TemporaryDirectory(prefix="bmem-native-baseline-") as tmp:
        vault = native.VaultFixture(binary, Path(tmp).resolve())
        with vault.running(), native.fixture_server(provider) as (endpoint, captured):
            client, hook_source = vault.register(host)
            url = vault.cli("web", "--no-open")["url"]
            owner = BrowserSession(url.split("/session", 1)[0], url)
            tasks = {t["id"]: vault.cli("task", "create", t["anchor"], "--project", t["project"])["task_id"]
                     for t in data.get("tasks", [{"id": "tQ", "anchor": "tQ", "project": "Q"}])}
            for real_id in tasks.values():
                vault.grant("set", "--client", client, "--task", real_id, "--allow", "true")
            alias, source_ids = events(owner, expand_events(data), tasks)
            for sid in source_ids.values():
                vault.grant("source", sid, "--purpose", "task_delivery", "--client", client, "--allow", "true")
            if identifier == "F04-08":
                # Use the same records as the question in both the owner and host reads.
                args = case["steps"][0]["args"]
                status, response = owner.request("/v2/search", "POST", {"mode": args["mode"], "query": args["query"]})
                traces["find"] = {"status": status, "body": response}
                projected["find"] = {"refs": [alias.get(row.get("event_id")) for row in response["results"]]}
            else:
                projected["seed"] = {"accepted_event_count": len(alias)}
            for task_label, task_id in tasks.items():
                prompt = (case["steps"][1]["args"]["query"] if identifier == "F04-08"
                          else ("继续青禾的导入排查。" if task_label == "T1" else "继续青禾的导出排查。"))
                offset = len(captured[host])
                native.run_host(host, vault.command(host, client, hook_source, task_id), prompt,
                                endpoint, vault.work, vault.env, provider, identifier + "-" + task_label)
                marker = "bmem authorized task evidence."
                blocks = [block for call in captured[host][offset:] for block in native.text_blocks(call)
                          if marker in block]
                if not captured[host][offset:]:
                    raise ValueError(f"No loopback request for {identifier}/{task_label}")
                packages = [json.JSONDecoder().raw_decode(block.split(marker, 1)[1].split("\n", 1)[1])[0]
                            for block in blocks]
                items = {item["event_id"]: item for package in packages for item in package["evidence"]}.values()
                key = "input" if task_label in ("T1", "tQ") else "other"
                traces[key] = {"loopback_requests": len(captured[host]) - offset, "evidence_blocks": blocks}
                if identifier == "F04-08":
                    projected[key] = {"model_input_evidence_refs": [alias[item["event_id"]] for item in items
                                                                     if item["event_id"] in alias],
                                      "verified_result_refs": [ref for ref in evidence_refs(items, alias)
                                                               if "/checks/" in ref]}
                else:
                    projected[key] = {"refs": evidence_refs(items, alias)}
                    if identifier == "F02-10" and key == "input" and packages:
                        projected["local"] = packages[0]
    return traces, projected


def replay_permission_search(case, binary):
    data = case["fixture"]
    provider = fixture("native_provider.json")
    observations, traces = {}, {}
    with tempfile.TemporaryDirectory(prefix="bmem-permission-search-") as tmp:
        vault = native.VaultFixture(binary, Path(tmp))
        with vault.running(), native.fixture_server(provider) as (endpoint, captured):
            registrations = {"hA": ("codex", *vault.register("codex"))}
            if case["id"] == "F04-14":
                registrations["hB"] = ("claude-code", *vault.register("claude-code"))
            tasks = {}
            for event in data["events"]:
                if event["task"] not in tasks:
                    tasks[event["task"]] = vault.cli("task", "create", event["task"], "--project", event["project"])["task_id"]
            url = vault.cli("web", "--no-open")["url"]
            owner = BrowserSession(url.split("/session", 1)[0], url)
            aliases, source_ids = events(owner, data["events"], tasks)
            for reader, (_, client, _) in registrations.items():
                task = tasks["tQ"]
                vault.grant("set", "--client", client, "--task", task, "--allow", "true")
                grants = data.get("grants")
                labels = data["initial_source_grants_to_hA"] if grants is None else [
                    src for src, purposes in grants[reader].items() if "task_delivery" in purposes
                ]
                for source_name in labels:
                    vault.grant("source", source_ids[source_name], "--client", client,
                                "--purpose", "task_delivery", "--allow", "true")
            for step in case["steps"]:
                label, args = step["id"], step["args"]
                if label == "narrow":
                    client = registrations[args["client"]][1]
                    observations[label] = traces[label] = vault.grant(
                        "source", source_ids[args["source"]], "--client", client, "--purpose",
                        "task_delivery", "--allow", "false")
                    if observations[label] is None:
                        observations[label] = {"changes_applied_by": "cli"}
                elif label == "owner":
                    status, result = owner.request("/v2/search", "POST", {"query": args["query"], "mode": args["mode"]})
                    traces[label] = {"status": status, "body": result}
                    if status == 200:
                        observations[label] = {"refs": [aliases.get(item.get("event_id")) for item in result["results"]]}
                else:
                    host, client, source = registrations[args["client"]]
                    native.run_host(host, vault.command(host, client, source, tasks[args["task"]]),
                                    args["query"], endpoint, vault.work, vault.env, provider, case["id"] + "-" + label)
                    marker = "bmem authorized task evidence."
                    blocks = [chunk for request in captured[host] for chunk in native.text_blocks(request) if marker in chunk]
                    traces[label] = {"evidence_blocks": blocks, "loopback_requests": len(captured[host])}
                    if not captured[host]:
                        continue
                    packages = [json.JSONDecoder().raw_decode(block.split(marker, 1)[1].split("\n", 1)[1])[0]
                                for block in blocks]
                    items = {item["event_id"]: item for package in packages for item in package["evidence"]}.values()
                    observations[label] = {
                        "model_input_evidence_refs": [aliases[item["event_id"]] for item in items if item["event_id"] in aliases],
                        "model_input_evidence_text": "\n".join(item["text"] for item in items),
                    }
    return traces, observations


def replay_hindsight():
    example = (HERE.parent / "replay-example.md").read_text(encoding="utf-8")
    command = re.search(r"~~~sh\n(.*?)\n~~~", example, re.S).group(1)
    result = {"upstream": {}, "local": {}}
    with tempfile.TemporaryDirectory(prefix="bmem-rrf-baseline-") as folder:
        completed = subprocess.run(["/bin/sh", "-c", command], cwd=folder, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, check=True, timeout=55)
    result["raw_stdout"] = completed.stdout.splitlines()
    for line in result["raw_stdout"]:
        name, upstream, local, *scores = line.split()
        result["upstream"][name] = upstream.split("=")[1]
        result["local"][name] = local.split("=")[1]
        if name == "overlap":
            result["overlap_score_x"] = dict(pair.split("=") for pair in scores)["X"]
    return {"replay": result}


def intake_probe(case, service, owner):
    data = case["fixture"]
    sid = source(owner, data.get("default_source", "input"))
    rows = [{"native_object": item["native_object"], "native_version": item["version_key"],
             "actor_kind": "user_message", "at": "2026-11-20T00:00:00Z", "text": item["text"]}
            for item in data["source_events"]]
    request = {"source_id": sid, "events": rows, "next_cursor": data.get("page", {}).get("source_cursor", "c1")}
    first = post(owner, "/v2/events", request, "first")
    if case["id"] == "F03-03":
        second = post(owner, "/v2/events", request, "first")
    else:
        second = post(owner, "/v2/events", {**request, "cursor": request["next_cursor"],
                                           "next_cursor": "next"}, "new-page")
    return {"accepted_page": first, "replay": second, "jobs": owner.request("/v2/jobs")[1],
            "source_records": owner.request("/v2/sources")[1]}, {}


def reimport_history(case):
    data = case["fixture"]
    transcript = [{"timestamp": "2026-11-20T00:00:00Z", "type": "session_meta",
                   "payload": {"id": "upgrade-history-fixture"}}]
    transcript.extend(
        {"timestamp": f"2026-11-20T00:00:{index:02}Z", "type": "event_msg",
         "payload": {"type": "user_message", "turn_id": row["native_object"], "message": row["text"]}}
        for index, row in enumerate(data["source_events"], 1)
    )
    with isolated(transcript) as (service, owner):
        source_id = service.cli("connector", "add", "codex", "--account", "local")["source_id"]
        service.cli("connector", "resume", source_id)
        targets = service.cli("connector", "history-options", source_id, "--kind", "sessions")
        body = {"source_id": source_id, "kind": "sessions", "targets": [targets["targets"][0]["id"]],
                "from": "2026-11-20T00:00:00Z", "to": "2026-11-20T01:00:00Z", "content": "messages"}
        seen = {}
        for label in ("import", "again"):
            preview = post(owner, "/v2/history/preview", body, "preview-" + label)
            job = post(owner, "/v2/history/jobs", body, label)
            deadline = monotonic() + 15
            while True:
                jobs = owner.request("/v2/history/jobs")[1]["jobs"]
                current = next(row for row in jobs if row["id"] == job["id"])
                if current["state"] not in ("queued", "running"):
                    break
                if monotonic() >= deadline:
                    raise RuntimeError(f"History fixture stayed {current['state']}")
                sleep(0.05)
            seen[label] = {"preview": preview, "job": current}
        status, result = owner.request(f"/v2/history/jobs/{job['id']}/results")
        raw = {**seen, "audit": {"status": status, "body": result}, "transcript": transcript}
        if status != 200:
            return raw, {}
        db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as db:
            versions = db.execute("SELECT count(*) FROM ing_event_versions WHERE source_id=?", (source_id,)).fetchone()[0]
            queue = db.execute("SELECT count(*) FROM ing_jobs WHERE source_id=?", (source_id,)).fetchone()[0]
        return raw, {**seen, "audit": {"rows": result["rows"],
                                         "store": {"distinct_versions": versions, "extract_job_count": queue}}}


def root_forget_replay(case, service, owner):
    data = case["fixture"]
    task = post(owner, "/v2/tasks", {"anchor": data["task"], "project": "青禾"}, "task")["task_id"]
    items = [{**item, "task": data["task"]} for item in data["roots"]]
    _, source_ids = events(owner, items, {data["task"]: task})
    source_id = source_ids["default"]
    db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        root = db.execute("SELECT root_id FROM ing_roots WHERE native_object='r1'").fetchone()[0]
    cmd = {"kind": "forget", "target": {"root_id": root}}
    status, preview = owner.request("/v2/operations/preview", "POST", cmd, "root-preview")
    if status != 202 or not preview.get("preview_id"):
        return {"pv": {"status": status, "body": preview}}, {}
    status, done = owner.request("/v2/operations", "POST", {**cmd, "preview_id": preview["preview_id"]}, "root-confirm")
    if status != 202:
        return {"pv": preview, "ok": {"status": status, "body": done}}, {}
    time = "2026-11-23T01:00:00Z"
    page = {"source_id": source_id, "cursor": data["page"]["cursor"], "next_cursor": "resubmitted",
            "events": [{"native_object": row["id"], "native_version": row["version"], "actor_kind": "user_message",
                        "task_id": task, "text": row["text"], "at": time} for row in items]}
    replayed = [post(owner, "/v2/events", page, "replay") for _ in range(2)]
    bound = new_bound(service, owner, task)
    service.operation(owner, {"kind": "change_grant", "source_id": source_id, "purpose": "task_delivery",
                              "client_id": bound[2]["client_id"], "allow": True}, "source-read")
    status, package = load_bound(*bound, "after-root-withdrawal", task)
    if status != 200:
        raise ValueError(package)
    with sqlite3.connect(db_uri, uri=True) as db:
        versions = dict(db.execute("SELECT event_id,native_version FROM ing_event_versions"))
        outcome = {
            "tombstoned_roots": [n for n, in db.execute("SELECT native_object FROM ing_roots JOIN ing_root_revocations USING(root_id)")],
            "readable_version_ids": [n for n, in db.execute("SELECT native_version FROM ing_event_versions WHERE active=1 ORDER BY native_version")],
            "pending_distill_inputs": [n for n, in db.execute("SELECT native_version FROM ing_jobs JOIN ing_event_versions USING(event_id) WHERE status IN ('pending','running')")],
        }
    return {"pv": preview, "ok": done, "import": replayed, "roots": outcome, "after": package}, {
        "pv": preview, "ok": done, "import": {"receipts": replayed}, "roots": outcome,
        "after": {"root_versions": [versions[item["event_id"]] for item in package["evidence"]]},
    }


def archive_probe(case, service, owner):
    data = case["fixture"]
    events(owner, data["roots"])
    backup = service.cli("backup", "create")
    raw = {"backup": backup}
    if case["id"] == "F05-13":
        with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            root = db.execute("SELECT root_id FROM ing_roots WHERE native_object='r1'").fetchone()[0]
        command = {"kind": "forget", "target": {"root_id": root}}
        status, response = owner.request("/v2/operations/preview", "POST", command, "root-forget")
        raw["root_forget"] = {"status": status, "body": response}
    else:
        with isolated() as (empty, other_owner):
            folder = service.root / "backups" / backup["backup_id"]
            shutil.copytree(folder, empty.root / "backups" / folder.name)
            raw["restore"] = empty.cli("restore", "preview", folder.name)
            raw["lookup"] = other_owner.request("/v2/search", "POST", {"query": "灰雀", "mode": "lexical"})
    return raw, {}


def interface_probes(service, owner):
    result = {}
    for label, path, method, body in [
        ("history_list", "/v2/history/jobs", "GET", None),
        ("history_results", "/v2/history/jobs/baseline-fixture/results", "GET", None),
        ("hybrid", "/v2/search", "POST", {"query": "E417", "mode": "hybrid"}),
        ("root_denial", "/v2/operations/preview", "POST",
         {"kind": "deny_root", "reader": "A", "purpose": "task_delivery", "root": "r1",
          "comparison": {"query": "写入耗时", "task": "T1", "readers": ["A", "B"]}}),
        ("cooling", "/v2/operations/preview", "POST", {"kind": "cool", "root_id": "r1"}),
        ("capabilities", "/v2/capabilities", "GET", None),
    ]:
        status, response = owner.request(path, method, body, "interface-" + label if body else None)
        result[label] = {"method": method, "path": path, "status": status, "body": response}
    database = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(database, uri=True) as db:
        result["tables"] = [name for name, in db.execute("SELECT name FROM sqlite_master "
                                                        "WHERE type='table' ORDER BY name")]
    return result


def legacy_receipt_probe(case, service, owner):
    data = case["fixture"]
    task_record = data["tasks"][0]
    task = post(owner, "/v2/tasks", {"anchor": task_record["anchor"], "project": task_record["project"]},
                "task")["task_id"]
    aliases, source_ids = events(owner, data["events"], {"T1": task})
    source_id = source_ids["main"]
    browser, auth, client = new_bound(service, owner, task)
    service.operation(owner, {"kind": "change_grant", "source_id": source_id, "purpose": "task_delivery",
                              "client_id": client["client_id"], "allow": True}, "delivery")
    post(owner, "/v2/statements", {"original": data["rules"][0], "scope": {"kind": "global"},
                                 "share": "authorized_assistants"}, "fixture-statement")
    request = {"session_id": "s1", "context_epoch": "1", "route_ref": client["route_ref"],
               "task_id": task, "budget": case["steps"][1]["args"]["budget"]}
    status, attempt = browser.request(f"/v2/tasks/{task}/attempts", "POST", request, "attempt", authentication=auth)
    if status != 201:
        raise ValueError(attempt)
    request["attempt_id"] = attempt["attempt_id"]
    status, first = browser.request("/v2/context/load", "POST", request, "first", authentication=auth)
    if status != 200:
        raise ValueError(first)
    ack = {k: first[k] for k in ("snapshot_id", "package_id")}
    ack.update(kind="loaded", event_id="statement-only", context_epoch=request["context_epoch"],
               route_ref=client["route_ref"], model_call_ref="fixture-statement-call",
               revision_ids=[item["revision_id"] for item in first["statements"]])
    path = f"/v2/deliveries/{first['delivery_id']}/receipts"
    receipt_status, receipt = browser.request(path, "POST", ack, authentication=auth)
    additions = [{k: row[k] for k in ("native_object", "native_version", "actor_kind", "at", "checks")}
                 for row in data["later_events"]]
    later = post(owner, "/v2/events", {"source_id": source_id, "cursor": "c1", "next_cursor": "c2",
                                     "events": [{**row, "task_id": task} for row in additions]}, "later")
    aliases.update({row["event_id"]: row["native_object"] for row in later["events"]})
    next_status, next_load = browser.request("/v2/context/load", "POST",
                                             {**request, "known_snapshot_id": first["snapshot_id"]},
                                             "next", authentication=auth)
    result = {"refs": evidence_refs(next_load["evidence"], aliases), "snapshot_mode": next_load["snapshot_mode"]}
    if "package_mode" in next_load:
        result["package_mode"] = next_load["package_mode"]
    return {"first": first, "old": {"status": receipt_status, "body": receipt},
            "more": later, "next": {"status": next_status, "body": next_load}}, {
        "seed": {"accepted": len(aliases) - len(later["events"])},
        "first": first,
        "old": {"complete": receipt.get("complete")},
        "more": later,
        "next": result,
    }


def evidence_receipt_probe(case, service, owner):
    data = case["fixture"]
    case_id = case["id"]
    task = post(owner, "/v2/tasks", data["tasks"][0], "task")["task_id"]
    aliases, sources = events(owner, data["events"], {"T1": task})
    browser, auth, client = new_bound(service, owner, task)
    for label, source_id in sources.items():
        service.operation(owner, {"kind": "change_grant", "source_id": source_id,
                                  "purpose": "task_delivery", "client_id": client["client_id"],
                                  "allow": True}, "grant-" + label)
    for index, rule in enumerate(data.get("rules", [])):
        post(owner, "/v2/statements", {"original": rule, "scope": {"kind": "global"},
                                     "share": "authorized_assistants"}, f"rule-{index}")
    calls = {step["id"]: step["args"] for step in case["steps"]}
    responses = {}
    projections = {"seed": {"accepted": len(aliases)}}
    with native.fixture_server(fixture("native_provider.json")) as (endpoint, captured):
        def delivery(step, retained=None):
            args = calls[step]
            key = args["session_id"] + "-" + args["context_epoch"]
            request = {"session_id": args["session_id"], "context_epoch": args["context_epoch"],
                       "route_ref": client["route_ref"], "budget": args["budget"], "task_id": task}
            response_status, attempt = browser.request(f"/v2/tasks/{task}/attempts", "POST", request,
                                                       "attempt-" + key, authentication=auth)
            if response_status != 201:
                raise ValueError(attempt)
            request["attempt_id"] = attempt["attempt_id"]
            if retained:
                if "known_snapshot_id" in args:
                    request["known_snapshot_id"] = retained["snapshot_id"]
                if "retained_delivery_id" in args:
                    request.update(retained_delivery_id=retained["delivery_id"],
                                   retained_manifest_digest=(args["retained_manifest_digest"] if
                                    args["retained_manifest_digest"].startswith("sha256:") else
                                    retained["evidence_manifest_digest"]))
            status, response = browser.request("/v2/context/load", "POST", request, step, authentication=auth)
            if status != 200:
                raise ValueError(response)
            responses[step] = response
            projections[step] = {**response, "refs": evidence_refs(response["evidence"], aliases)}
            return request, response

        def observed(label, sent, evidence, observations, acknowledge):
            wire = {"evidence": evidence, "observations": observations, "statements": sent["statements"]}
            encoded = json.dumps({"model": "fixture", "input": [{"role": "user", "content": [
                {"type": "input_text", "text": json.dumps(wire, ensure_ascii=False)},
            ]}], "tools": []}).encode("utf-8")
            req = Request(endpoint + "/v1/responses", data=encoded, headers={"Content-Type": "application/json"})
            with build_opener(ProxyHandler({})).open(req, timeout=5) as model:
                model.read()
            actual = json.loads(captured["codex"][-1]["input"][0]["content"][0]["text"])
            responses[label] = {"actual_model_input": actual}
            result = {"refs": evidence_refs(actual["evidence"], aliases),
                      "statement_text": json.dumps(actual["statements"], ensure_ascii=False)}
            if acknowledge:
                canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                      ensure_ascii=False).encode("utf-8")
                manifest = {item["handle"]: hashlib.sha256(canonical([item, [
                    row for row in actual["observations"] if row["handle"] == item["handle"]
                ]])).hexdigest() for item in actual["evidence"]}
                digest = "sha256:" + hashlib.sha256(canonical(manifest)).hexdigest()
                request = {key: sent[key] for key in ("snapshot_id", "package_id")}
                request.update(kind="loaded", context_epoch=calls["first"]["context_epoch"],
                               event_id=label, route_ref=client["route_ref"], model_call_ref=label,
                               revision_ids=[row["revision_id"] for row in actual["statements"]],
                               loaded_evidence_refs=list(manifest), loaded_manifest_digest=digest)
                status, response = browser.request(f"/v2/deliveries/{sent['delivery_id']}/receipts",
                                                   "POST", request, authentication=auth)
                if status != 200:
                    raise ValueError(response)
                result.update(actual_manifest_digest=digest, evidence_complete=response["evidence_complete"])
                responses[label]["receipt"] = response
            projections[label] = result
            return actual

        initial, first = delivery("first")
        if "evidence_manifest" not in first:
            return responses, projections
        retained_items = first["evidence"][:1] if case_id == "F02-07" else first["evidence"]
        retained = observed("ack", first, retained_items, [
            row for row in first["observations"] if any(item["handle"] == row["handle"] for item in retained_items)
        ], True)
        additions = [{key: row[key] for key in ("native_object", "native_version", "actor_kind", "at", "checks")}
                     for row in data["later_events"]]
        receipt = post(owner, "/v2/events", {"source_id": sources["main"], "cursor": "c1", "next_cursor": "c2",
                                           "events": [{**row, "task_id": task} for row in additions]}, "later")
        aliases.update({row["event_id"]: row["native_object"] for row in receipt["events"]})
        responses["more"] = projections["more"] = receipt
        if "compact" in calls:
            projections["compact"] = {"next_epoch": calls["next"]["context_epoch"]}
        name = "new" if "new" in calls else "wrongdigest" if "wrongdigest" in calls else "next"
        _, followup = delivery(name, {**first, "evidence_manifest_digest": projections["ack"]["actual_manifest_digest"]})
        if case_id == "F02-03":
            observed("merged", followup, retained["evidence"] + followup["evidence"],
                     retained["observations"] + followup["observations"], True)
        else:
            observed("input", followup, followup["evidence"], followup["observations"], False)
        if case_id == "F02-15":
            status, body = browser.request("/v2/context/load", "POST", {
                **initial, "route_ref": calls["wrongroute"]["route_ref"],
            }, authentication=auth)
            responses["wrongroute"] = projections["wrongroute"] = {"status": status, **body}
    return responses, projections


def main():
    from replay_clock import FROZEN_CASES, replay_clock
    from replay_community import COMMUNITY_CASES, replay_community
    from replay_method import replay as replay_method
    from replay_optimization import SUPPORTED as OPTIMIZATION_CASES, replay_optimization
    from replay_community_opt import SUPPORTED as WORKER_COMMUNITY_CASES, replay_community_opt
    from replay_authority_hosts import AUTHORITY_HOST_CASES, replay_authority_host
    from replay_end_to_end import replay_import_handoff
    from replay_e2e_revoke import replay_revoke_handoff
    from replay_history import CASE_IDS as ARCHIVE_HISTORY_CASES, replay as replay_history
    from replay_task_lookup import TASK_LOOKUP_CASES, replay_task_lookup
    from replay_delivery_retry import DELIVERY_RETRY_CASES, replay_delivery_retry
    from replay_delivery_metrics import replay_delivery_metrics
    from replay_legacy import CASE_IDS as LEGACY_CASES, legacy_daemon_setup
    from replay_evidence_windows import WINDOW_CASES, replay_windows
    from replay_derived import CASES as DERIVED_CASES, replay as replay_derived
    from replay_f04_17 import replay_f04_17
    from replay_f05_final import CASES as GOVERNANCE_FINAL_CASES, replay as replay_f05_final
    from replay_x01_final import SUPPORTED as UPSTREAM_CASES, replay_x01_final

    binary = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2])
    os.environ["BMEM_V2_TEST_BIN"] = str(binary)
    suite = catalogue()
    complete = {}
    traces = {}
    execution = {}
    selected = set(sys.argv[3:])
    unknown = selected - suite.keys()
    if unknown:
        raise ValueError("unknown cases: " + ", ".join(sorted(unknown)))
    with isolated() as (service, owner):
        interfaces = interface_probes(service, owner)
    for key, case in suite.items():
        if selected and key not in selected:
            continue
        raw, projected = {}, {}
        try:
            if key == "X01-01":
                raw = projected = replay_hindsight()
            elif key in FROZEN_CASES and "authority_project_timezones" in interfaces["tables"]:
                raw, projected = replay_clock(case, BmemIntegration, collect_statements,
                    evidence_helpers=(post, lookup, dimensions, events, new_bound, load_bound))
            elif key in COMMUNITY_CASES and "community_note_heads" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_community(case, service, owner)
            elif key in ("F05-05", "X01-09") and "recall_cooled_roots" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_cooling(case, service, owner)
            elif key in DERIVED_CASES and "derived_revisions" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_derived(case, service, owner)
            elif key in ("F05-02", "F05-03", "F05-08", "F05-16") and "governance_discussion_roots" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_root_restriction(case, service, owner)
            elif key == "F05-14" and "recall_cooled_roots" in interfaces["tables"]:
                raw, projected = replay_empty_restore(case)
            elif key == "F05-11" and "history_archive_submissions" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_inflight_forget(case, service, owner)
            elif key.startswith("F06-") and "method_cards" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_method(case, service, owner)
            elif key in ARCHIVE_HISTORY_CASES and "history_archive_submissions" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_history(case, service, owner)
            elif key in LEGACY_CASES and "history_archive_submissions" in interfaces["tables"]:
                raw, projected = legacy_daemon_setup(case, binary)
            elif key in OPTIMIZATION_CASES and "optimization_permissions" in interfaces["tables"]:
                raw, projected = replay_optimization(case, binary)
            elif key in WORKER_COMMUNITY_CASES and "community_note_heads" in interfaces["tables"]:
                raw, projected = replay_community_opt(case, binary)
            elif key in UPSTREAM_CASES and "context_evidence_manifests" in interfaces["tables"]:
                raw, projected = replay_x01_final(case, binary)
            elif key in TASK_LOOKUP_CASES and "authority_project_timezones" in interfaces["tables"]:
                raw, projected = replay_task_lookup(case)
            elif key in DELIVERY_RETRY_CASES and "context_evidence_manifests" in interfaces["tables"]:
                raw, projected = replay_delivery_retry(case)
            elif key == "F02-16" and "context_evidence_manifests" in interfaces["tables"]:
                raw, projected = replay_delivery_metrics(case)
            elif key in AUTHORITY_HOST_CASES and "authority_project_timezones" in interfaces["tables"]:
                raw, projected = replay_authority_host(case, binary)
            elif key in WINDOW_CASES and "recall_tool_links" in interfaces["tables"]:
                raw, projected = replay_windows(case, binary)
            elif key == "F04-17" and "recall_seed_sets" in interfaces["tables"]:
                raw, projected = replay_f04_17(case, binary)
            elif key in GOVERNANCE_FINAL_CASES and "governance_vector_cleanup_jobs" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_f05_final(case, service, owner)
            elif key == "E2E-01" and "context_evidence_manifests" in interfaces["tables"]:
                try:
                    raw, projected = replay_import_handoff(case, binary)
                except (ValueError, RuntimeError, AssertionError, IndexError, StopIteration, native.HostTimeout) as exc:
                    raw = {"native_error": str(exc) or type(exc).__name__}
            elif key == "E2E-02" and "method_owner_guidance" in interfaces["tables"]:
                raw, projected = replay_revoke_handoff(case, binary)
            elif key in ("F02-01", "F02-02") and "context_evidence_manifests" in interfaces["tables"]:
                from replay_native_handoff import replay_native_handoff
                try:
                    raw, projected = replay_native_handoff(case, binary)
                except (ValueError, RuntimeError, AssertionError, IndexError, StopIteration, native.HostTimeout) as exc:
                    raw = {"native_error": str(exc) or type(exc).__name__}
            elif key == "F01-12":
                raw, projected = upgrade_old_statements(case, binary)
            elif key in ("F04-10", "F04-14"):
                try:
                    raw, projected = replay_permission_search(case, binary)
                except (ValueError, RuntimeError, AssertionError, native.HostTimeout) as exc:
                    raw = {"native_error": str(exc)}
            elif key == "F03-04":
                raw, projected = reimport_history(case)
            elif key == "F04-09" and "community_note_heads" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = replay_vector_timeout(case, service, owner)
            elif key == "F05-01" and "recall_root_denials" in interfaces["tables"]:
                with isolated() as (service, owner):
                    raw, projected = root_comparison_probe(case, service, owner)
            elif key == "F05-12":
                with isolated() as (service, owner):
                    raw, projected = root_forget_replay(case, service, owner)
            elif key == "F02-06":
                with isolated() as (service, owner):
                    raw, projected = legacy_receipt_probe(case, service, owner)
            elif key in ("F02-03", "F02-04", "F02-05", "F02-07", "F02-15"):
                with isolated() as (service, owner):
                    raw, projected = evidence_receipt_probe(case, service, owner)
            elif key in ("F03-03", "F05-13", "F05-14"):
                with isolated() as (service, owner):
                    raw, projected = (intake_probe if key.startswith("F03-") else archive_probe)(case, service, owner)
            elif key.startswith("F01-") or key in ("F05-07", "F02-08", "F02-09", "F02-12") or (
                    key.startswith("F04-") and key[4:] in ("01", "02", "08", "09", "16")):
                if key.startswith("F01-"):
                    probe = collect_statements
                elif key == "F05-07":
                    probe = no_resurrection
                elif key.startswith("F02-"):
                    probe = replay_handoff_api
                else:
                    probe = lexical_probe
                with isolated() as (service, owner):
                    raw, projected = probe(case, service, owner)
            else:
                if key not in ("F02-10",):
                    execution[key] = "blocked_without_probe"
                    print(key, "NOT_RUN", flush=True)
                    continue
            if key in ("F02-09", "F02-10", "F04-08"):
                try:
                    native_raw, native_view = replay_native(case, binary)
                    raw["native"] = native_raw
                    projected.update(native_view)
                except (ValueError, RuntimeError, AssertionError, native.HostTimeout) as exc:
                    raw["native_error"] = str(exc)
        except (OSError, ValueError, TypeError, RuntimeError, AssertionError, IndexError, KeyError,
                StopIteration, sqlite3.Error, subprocess.SubprocessError, native.HostTimeout) as exc:
            raw["execution_error"] = str(exc) or type(exc).__name__
        traces[key] = {"actual": raw, "normalized": projected}
        required = {step["id"] for step in case["steps"]}
        # The installed executable owns received_at. Fixed-clock cases are partial.
        fixed_clock_unavailable = (key in ("F01-03", "F01-08", "F01-17", "F01-18")
                                   and "authority_project_timezones" not in interfaces["tables"])
        legacy_without_mode = key == "F02-06" and "package_mode" not in projected.get("next", {})
        if required <= projected.keys() and not fixed_clock_unavailable and not legacy_without_mode:
            complete[key] = projected
        execution[key] = grade(case, complete[key])[0] if key in complete else "PARTIAL"
        print(key, execution[key], flush=True)
    target.write_text(json.dumps({
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "interface_probes": interfaces, "replays": traces, "complete_observations": complete, "execution": execution,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
