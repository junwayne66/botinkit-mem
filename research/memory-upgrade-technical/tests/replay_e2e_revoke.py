"""Exercise current root grants across stored handles, methods, and real hosts."""

from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile

from baseline_probe import BrowserSession, events, fixture, native, post
from replay_native_handoff import incoming, make_host


def replay_revoke_handoff(case, binary):
    data = case["fixture"]
    readers = dict(zip(data["readers"], ("codex", "claude-code")))
    rejected, allowed = readers
    primary, fallback = data["method"]["requires_both"]
    fragments = {item["id"]: item["text"] for item in data["roots"]}
    provider = {**fixture("native_provider.json"), "command": None}
    with tempfile.TemporaryDirectory(prefix="bmem-e2e-revoke-") as folder:
        vault = native.VaultFixture(binary, Path(folder))
        with vault.running(), native.fixture_server(provider) as (endpoint, captured):
            web = vault.cli("web", "--no-open")["url"]
            url = web.split("/session", 1)[0]
            owner, browser = BrowserSession(url, web), BrowserSession(url)
            task = post(owner, "/v2/tasks", {"project": data["project"], "anchor": data["work_task"]}, "revoke-task")["task_id"]
            credentials = {alias: make_host(vault, owner, name, task) for alias, name in readers.items()}
            aliases, sources = events(owner, [{"id": row["id"], "actor_kind": "tool_result", "task": "work",
                "checks": {"output":row["text"]}} for row in data["roots"]], {"work": task})
            source = sources["default"]
            instance = vault.cli("status")["instance_id"]
            for label in data["all_roots_readable_by"]:
                client, _ = credentials[label]
                vault.grant("source", source, "--purpose", "task_delivery", "--client", client["client_id"], "--allow", "true")
            headers = {alias: {"Authorization": "Bearer " + found[0]["token"], "X-Bmem-Instance": instance}
                       for alias, found in credentials.items()}
            handles = {alias: "event_" + event for event, alias in aliases.items()}
            lookup = {"task_id": task, "artifact": data["work_task"], "method": data["method"]["id"]}
            authored = post(owner, "/v2/methods/curate", {**lookup,"text":data["method"]["text"],
                "requires_all":[handles[alias] for alias in data["method"]["requires_both"]]}, "work-method")
            if authored["verified_outcome"] != "unknown":
                raise ValueError("owner-authored method incorrectly established a verified outcome")

            def request(reader, path, payload):
                status, response = browser.request(path, "POST", payload, authentication=headers[reader])
                return {"status": status, "body": response}

            def read(reader, alias):
                return request(reader, "/v2/evidence/expand", {"handle": handles[alias]})

            db = (vault.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            with sqlite3.connect(db, uri=True) as conn:
                root, = conn.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (handles[primary][6:],)).fetchone()
            command = {"kind":"change_grant","purpose":"task_delivery","root_id":root,
                       "client_id":credentials[rejected][0]["client_id"],"allow":False}
            query = fragments[primary].split("：", 1)[0]
            preview = post(owner, "/v2/operations/preview", {**command, "comparison": {"task_id":task,"query":query,
                "reader_ids":[credentials[key][0]["client_id"] for key in readers],"budget":128}}, "preview-deny")
            old_before = {label:read(label,primary) for label in readers}
            confirmed = post(owner, "/v2/operations", {**command,"preview_id":preview["preview_id"]}, "confirm-deny")
            old_after = {label:read(label,primary) for label in readers}
            search = post(owner, "/v2/search", {"query":query,"task_id":task,"mode":"hybrid"}, "hybrid-current")
            methods = {label:request(label, "/v2/methods/handoff", lookup) for label in readers}
            for label, host in readers.items():
                bound, hook_source = credentials[label]
                native.run_host(host, vault.command(host, bound["client_id"], hook_source, task), "继续这个工作任务。",
                                endpoint, vault.work, vault.env, provider, f"E2E-02-{label}")
            native_reads = {}
            for label, host in readers.items():
                calls = captured[host]
                if not calls:
                    raise ValueError(f"native request was not captured for {label}")
                text = "\n".join(chunk for call in calls for chunk in native.text_blocks(call))
                packages = [package for call in calls for package in incoming(call)]
                excerpt = "\n".join(item["text"] for package in packages for item in package["evidence"])
                marker_present = query in text
                # Check the entire wire input, but retain only this fixture's
                # evidence and a witness if the marker appeared elsewhere.
                if marker_present and query not in excerpt:
                    excerpt += "\n" + query
                native_reads[label] = {"endpoint_host":host,"request_count":len(calls),"request_digest":sha256(text.encode()).hexdigest(),
                    "evidence_packages":packages,"actual_input":excerpt,"primary_marker_seen_in_full_request":marker_present}
            other = request(rejected, "/v2/evidence/window", {"task_id":task,"query":fragments[fallback],"candidate_limit":1})
            fallback_evidence = other["body"].get("evidence", [])
            raw = {"preview": {"governance":preview,"old_handles":old_before,"authored_method":authored},
                   "commit": {"confirmed":confirmed,"old_handles":old_after,"owner_hybrid":search,"methods":methods,
                              "native":native_reads}, "control": {allowed:old_after[allowed],rejected:other}}
            return raw, {
                "preview": {label:{"real_access":old_before[label]["status"] == 200} for label in readers},
                "commit": {label:{"old_handle_readable":old_after[label]["status"] == 200,
                           "actual_input":native_reads[label]["actual_input"],
                           "active_methods":methods[label]["body"].get("active_methods")} for label in readers},
                "control": {allowed:{"root":aliases.get(old_after[allowed]["body"].get("event_id"))},
                            rejected:{"root":aliases.get(fallback_evidence[0]["event_id"]) if fallback_evidence else None}},
            }
