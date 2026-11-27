"""Replay original F06 cases through the running native service and verified outcomes."""

import json
import sqlite3
from http.server import ThreadingHTTPServer
from threading import Thread


def replay(case, service, owner):
    # Import at runtime to reuse the isolation/auth helpers without an import cycle.
    from baseline_probe import BrowserSession, post, source
    from test_task_delivery import FixtureModel

    fixture = case["fixture"]
    project = next((row["project"] for row in fixture["evidence"]
                    if row["artifact"] == fixture["artifact"] and "project" in row), fixture["task"])
    task = post(owner, "/v2/tasks", {"anchor": fixture["task"], "project": project}, "method-task")["task_id"]
    subject = {"task_id": task, "artifact": fixture["artifact"], "method": fixture["method"]}
    model = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    thread = Thread(target=model.serve_forever, daemon=True)
    thread.start()

    def stop():
        model.shutdown()
        thread.join()
        model.server_close()

    service.addCleanup(stop)
    endpoint = f"http://127.0.0.1:{model.server_port}"
    service.cli("model", "add", "method-fixture", "--protocol", "ollama",
                "--endpoint", endpoint, "--model-id", "method-fixture")
    service.cli("model", "test", "method-fixture", "--for", "extract")
    service.cli("model", "use", "method-fixture", "--for", "extract")

    browser = BrowserSession(service.endpoint)

    def register(label, capabilities):
        registration = post(owner, "/v2/clients", {"host": "method-replay", "destination": label,
                            "capabilities": capabilities}, "client-" + label)
        headers = {"Authorization": "Bearer " + registration["token"], "X-Bmem-Instance": service.instance_id}
        return registration["client_id"], registration["route_ref"], headers

    runner = register("runner", {"load_context": True})
    verifier = register("verifier", {"verify_outcomes": True})
    readers = {name: register(name, {"load_context": True}) for name in fixture["grants"]["task_delivery"]}
    tasks = {project: task}
    captures, verification, facts, aliases = {}, {}, {}, {}
    db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"

    def authenticated(path, body, key, identity):
        status, result = browser.request(path, "POST", body, key, authentication=identity[2])
        if status not in (200, 201):
            raise ValueError((path, status, result))
        return result

    def method(action, body, key):
        return post(owner, "/v2/methods/" + action, body, "method-" + key)

    def seed(row, extra_grant=None):
        name, root = row["id"], row["root"]
        row_project = row.get("project", project)
        if row_project not in tasks:
            tasks[row_project] = post(owner, "/v2/tasks",
                                      {"anchor": row_project, "project": row_project}, "task-" + name)["task_id"]
        evidence_task = tasks[row_project]
        sid = source(owner, "method-" + name)
        for identity, role in ((runner, "runner"), (verifier, "verifier")):
            service.operation(owner, {"kind": "change_grant", "client_id": identity[0],
                                      "task_id": evidence_task, "allow": True}, f"task-{role}-{name}")
        if root in fixture["grants"]["distill"] or (extra_grant and extra_grant.get("distill")):
            service.operation(owner, {"kind": "change_grant", "source_id": sid,
                                      "purpose": "extract", "allow": True}, "extract-" + name)
        for reader, identity in readers.items():
            if root not in fixture["grants"]["task_delivery"][reader] and (
                    not extra_grant or reader not in extra_grant.get("task_delivery", [])):
                continue
            service.operation(owner, {"kind": "change_grant", "client_id": identity[0],
                                      "task_id": evidence_task, "allow": True}, f"task-{reader}-{name}")
            service.operation(owner, {"kind": "change_grant", "client_id": identity[0], "source_id": sid,
                                      "purpose": "task_delivery", "allow": True}, f"source-{reader}-{name}")
        outcome = row["outcome"]
        evidence = {"native_object": root, "native_version": "1", "at": fixture["now"], "task_id": evidence_task}
        if outcome in ("passed", "failed", "cancelled"):
            evidence.update(actor_kind="tool_result",
                            checks={"result_text": row["text"], "stages": row.get("stage_receipts", {})})
        else:
            evidence.update(actor_kind="user_message" if outcome in ("planned", "observed") else "assistant_message",
                            text=row["text"])
        capture = post(owner, "/v2/events", {"source_id": sid, "next_cursor": "1", "events": [evidence]},
                       "capture-" + name)
        captures[name] = capture
        event = capture["events"][0]["event_id"]
        with sqlite3.connect(db_uri, uri=True) as db:
            root_id = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (event,)).fetchone()[0]
        handle = "event_" + event
        aliases[handle] = name
        facts[name] = {"handle": handle, "root_id": root_id}
        registration = {"artifact": row["artifact"], "method": row["step"], "version": row["version"],
                        "handle": handle, "observed_condition": row.get("observed_condition"),
                        "extra_conditions": row.get("extra_conditions", [])}
        if outcome in ("passed", "failed", "cancelled", "planned"):
            attempt = authenticated(f"/v2/tasks/{evidence_task}/attempts", {
                "session_id": "method-" + name, "context_epoch": "1", "route_ref": runner[1],
            }, "method-run-" + name, runner)
            receipt = authenticated(f"/v2/attempts/{attempt['attempt_id']}/outcomes", {
                "event_id": name, "status": outcome if outcome in ("passed", "failed") else "unknown",
                "cancelled": outcome == "cancelled", "validator_ref": f"synthetic-tool-verification/{name}",
                "artifact_ref": json.dumps({"artifact": row["artifact"], "version": row["version"]},
                                           ensure_ascii=False),
                "evidence_ref": handle,
            }, "method-verify-" + name, verifier)
            verification[name] = receipt
            registration.update(kind="outcome", verifier_id=verifier[0], outcome_event_id=receipt["event_id"])
        elif outcome == "observed":
            registration["kind"] = "prerequisite"
        else:
            linked = row.get("cites")
            registration.update(kind="reported", cites=facts[linked]["handle"] if linked else None)
        return method("observe", registration, "observe-" + name)

    def recompute(step, name=None, precondition=None):
        return method("recompute", {**subject, "method": name or fixture["method"],
                                    "precondition": precondition or fixture["precondition"]}, step)

    def read(action, args, step):
        body = {**subject, **{key: args[key] for key in ("version", "conditions") if key in args}}
        if "evidence_id" in args:
            body["handle"] = facts[args["evidence_id"]]["handle"]
        return authenticated("/v2/methods/" + action, body, "read-" + step, readers[args["reader"]])

    def normalize(value):
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {aliases.get(key, key): normalize(data) for key, data in value.items()}
        return value

    raw, projected = {}, {}
    stored = fixture.get("stored_method")
    if "strategies" in fixture:
        settings = fixture["strategies"]
        first_version = next(iter(stored["version_results"]))
        first_ids = stored["version_results"][first_version]["supports"]
        method("strategy", {"target": settings["previous"]}, "seed-previous-strategy")
        raw["seed_initial"] = [seed(row) for row in fixture["evidence"] if row["id"] in first_ids]
        prior = recompute("seed-previous-method")
        aliases[prior["revision_id"]] = stored["parent"]
        method("strategy", {"target": settings["current"]}, "seed-current-strategy")
        raw["seed_extra"] = [seed(row) for row in fixture["evidence"] if row["id"] not in first_ids]
        latest = recompute("seed-current-method")
        aliases[latest["revision_id"]] = stored["revision"]
    else:
        raw["seed"] = [seed(row) for row in fixture["evidence"]]
        if stored:
            prior = recompute("seed-stored-method")
            aliases[prior["revision_id"]] = stored["revision"]

    for step in case["steps"]:
        name, args = step["id"], step["args"]
        if name in ("fit", "initial", "recompute"):
            result = recompute(name)
            for other in fixture.get("method_dependencies", {}):
                if other != fixture["method"]:
                    prerequisite = next(row["observed_condition"] for row in fixture["evidence"]
                                        if row["step"] == other and row["observed_condition"])
                    raw["other_method"] = recompute("fit-other", other, prerequisite)
        elif name in ("drop", "withdraw"):
            root_name = next(row["id"] for row in fixture["evidence"] if row["root"] == args["root"])
            result = service.operation(owner, {"kind": "forget", "target": {"root_id": facts[root_name]["root_id"]}}, name)
        elif name == "add":
            result = seed(args["evidence"], args["grant"])
        elif name in ("audit", "pending"):
            result = method("audit", subject, name)
        elif name == "switch":
            result = method("strategy", {"target": args["target"]}, name)
        elif name == "propose":
            submission = fixture["candidate"]
            result = method("propose", {
                **subject, "precondition": fixture["precondition"], "text": submission["text"],
                "supported_versions": submission["supported_versions"],
                "citations": [facts[item]["handle"] for item in submission["citations"]],
            }, name)
            aliases[result["candidate_id"]] = submission["id"]
        elif name == "judge":
            result = method("judge", {**subject, "candidate_id": raw["propose"]["candidate_id"]}, name)
        elif name == "evidence":
            result = {item: read("evidence", {**args, "evidence_id": item}, name + "-" + item)
                      for item in args["evidence"]}
        elif name == "trace":
            result = read("evidence", args, name)
        elif name == "handoff":
            result = read("handoff", args, name)
        else:
            query = args if "version" in args else {**args, "version": fixture["evidence"][0]["version"]}
            result = read("query", query, name)
        raw[name] = result
        normalized = normalize(result)
        if name == "audit" and "attempts" in args:
            normalized = {item: normalized["outcomes"][item] for item in args["attempts"]}
        projected[name] = normalized

    if case["id"] == "F06-13":
        # Missing information about the additional failure condition is not a disproof.
        args = case["steps"][-1]["args"]
        diagnostic = read("query", {**args, "conditions": [fixture["precondition"]]}, "condition-unknown")
        if diagnostic["confirmed_fix_ids"] or diagnostic["default_delivery"]:
            raise ValueError("unknown failure condition was treated as disproven")
        raw["condition_unknown"] = diagnostic
        failure_case = next(row for row in fixture["evidence"] if row["outcome"] == "failed")
        reader = readers[args["reader"]]
        service.operation(owner, {"kind": "change_grant", "client_id": reader[0],
                                  "root_id": facts[failure_case["id"]]["root_id"],
                                  "purpose": "task_delivery", "allow": False}, "revoke-counterexample")
        denied = read("query", args, "hidden-failure-must-block")
        if denied["confirmed_fix_ids"] or denied["default_delivery"] or denied["first_evidence_ids"]:
            raise ValueError("restricted failure did not gate auto-adoption, or disclosed a hidden handle")
        raw["hidden_same_version_failure"] = denied
    if case["id"] == "F06-14":
        after_rejection = method("audit", subject, "prevent-rejected-authority")
        if after_rejection["current_revision"] is not None or after_rejection["pending_candidates"]:
            raise ValueError("rejected proposal created an authoritative revision or remained pending")
        raw["after_rejection_audit"] = after_rejection
    if case["id"] == "F06-16":
        # Compare a direct lookup of the same opaque evidence handle under both identities.
        handle = facts["b1"]["handle"]
        status_a, denied = browser.request("/v2/methods/evidence", "POST", {**subject, "handle": handle},
                                          "direct-A", authentication=readers["A"][2])
        if status_a != 404:
            raise ValueError("counterexample handle resolved without the reader's grant")
        direct_b = read("evidence", {"reader": "B", "evidence_id": "b1"}, "direct-B")
        raw["counterexample_direct"] = {"A": {"status": status_a, "body": denied}, "B": direct_b}
    return {**raw, "capture_receipts": captures, "verifier_receipts": verification}, projected
