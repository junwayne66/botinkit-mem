"""Measure observed coverage, actual use, and independently verified outcomes."""

from collections import Counter
import hashlib
import json
import re
from urllib.request import ProxyHandler, Request, build_opener

from baseline_probe import BrowserSession, events, fixture, isolated, load_bound, native, post, source
from replay_delivery_retry import evidence_manifest
from replay_task_lookup import completed_extractions, configure_extractor


def encode_check(value):
    match = re.fullmatch(r"(passed|failed|timeout|unknown);check-token=([A-Z0-9]{4})", value)
    if match is None:
        return value
    return {"kind": "artifact_check_v1", "status": match[1], "marker": match[2]}


def redaction_controls(owner, legacy_check):
    sid = source(owner, "receipt-redaction-control")
    page = {"source_id": sid, "next_cursor": "c1", "events": [{"native_object": "untyped", "native_version": "1",
            "actor_kind": "tool_result", "checks": {"write": legacy_check}}]}
    row = post(owner, "/v2/events", page, "untyped")["events"][0]
    legacy = post(owner, "/v2/evidence/expand", {"handle": "event_" + row["event_id"]}, "check-untyped")
    malformed = {"kind": "artifact_check_v1", "status": "timeout", "marker": "token=synthetic-fixture-credential"}
    request = {**page, "cursor": "c1", "next_cursor": "c2", "events": [{**page["events"][0],
               "native_object": "invalid", "checks": {"write": malformed}}]}
    status, response = owner.request("/v2/events", "POST", request, "invalid-marker")
    if json.loads(legacy["text"])["write"] != "[redacted credential line]" or status != 422:
        raise RuntimeError("typed receipt accepted credential-shaped data or changed free-text redaction")
    return {"untyped": legacy, "invalid": {"status": status, "response": response}}


def replay_delivery_metrics(case):
    data = case["fixture"]
    with isolated() as (service, owner):
        task = post(owner, "/v2/tasks", data["tasks"][0], "task")["task_id"]
        # Encode the frozen legacy connector value into the new on-wire check type.
        # The independent verifier still reads its answer from the immutable source fixture.
        encoded_events = [{**entry, "checks": {field: encode_check(value) for field, value in entry["checks"].items()}}
                          for entry in data["events"]]
        aliases, sources = events(owner, encoded_events, {"T1": task})
        source = sources["main"]
        browser = BrowserSession(service.endpoint)

        def register(host, capabilities):
            actor = post(owner, "/v2/clients", {"host": host, "destination": "fixture-" + host,
                         "capabilities": capabilities}, host)
            service.operation(owner, {"kind": "change_grant", "task_id": task, "client_id": actor["client_id"],
                                      "allow": True}, "task-" + host)
            headers = {"Authorization": "Bearer " + actor["token"], "X-Bmem-Instance": service.instance_id}
            return actor, headers

        hosts = {name: register(name, {"load_context": True, **(
            {"observe_loaded": True, "observe_used": True} if name == "Codex" else {}
        )}) for name in data["hosts"]}
        _, verifier = register("verifier", {"verify_outcomes": True})
        for name in data["grants"]["main"]:
            service.operation(owner, {"kind": "change_grant", "source_id": source, "purpose": "task_delivery",
                                      "client_id": hosts[name][0]["client_id"], "allow": True}, "deliver-" + name)
        configure_extractor(service)
        service.operation(owner, {"kind": "change_grant", "source_id": source, "purpose": "extract",
                                  "allow": True}, "extract")
        jobs = completed_extractions(owner, aliases)
        raw = {"seed": {"aliases": aliases, "extractions": jobs, "typed_events": encoded_events,
                        "redaction_controls": redaction_controls(owner, data["events"][0]["checks"]["write"])},
               "run": {}, "verify": {}}
        if not jobs:
            return raw, {}
        required = {row["native_object"] + "#/checks/" + name: value
                    for row in encoded_events for name, value in row["checks"].items()}
        observations = {}
        with native.fixture_server(fixture("native_provider.json")) as (endpoint, captured):
            for run in data["metric_runs"]:
                label = run["id"]
                actor, headers = hosts[run["host"]]
                status, package = load_bound(browser, headers, actor, label, task)
                raw["run"][label] = {"status": status, "delivery": package}
                if status != 200:
                    return raw, {}
                raw["run"][label]["followups"] = []
                observations[label] = None
                if run["tool"] is None:
                    continue
                wire = {key: package[key] for key in ("statements", "evidence", "observations")}
                if run["user_followups"]:
                    filtered = []
                    for row in wire["evidence"]:
                        contents = json.loads(row["text"])
                        checks = {name: value for name, value in contents.get("checks", contents).items() if name != "write"}
                        filtered.append({**row, "text": json.dumps(checks, ensure_ascii=False, separators=(",", ":"))})
                    wire = {**wire, "evidence": filtered,
                            "observations": [row for row in wire["observations"] if row["field"] != "/checks/write"]}
                request = Request(endpoint + "/v1/responses", headers={"Content-Type": "application/json"},
                                  data=json.dumps({"model": "fixture", "tools": [], "input": [
                                      {"role": "user", "content": [{"type": "input_text",
                                       "text": json.dumps(wire, ensure_ascii=False)}]}
                                  ]}).encode("utf-8"))
                with build_opener(ProxyHandler({})).open(request, timeout=5) as result:
                    result.read()
                actual = json.loads(captured["codex"][-1]["input"][0]["content"][0]["text"])
                received_fields = {}
                for row in actual["evidence"]:
                    contents = json.loads(row["text"])
                    prefix = aliases[row["event_id"]] + "#/checks/"
                    received_fields.update({prefix + name: value for name, value in contents.get("checks", contents).items()})
                observations[label] = {ref for ref, value in required.items() if received_fields.get(ref) == value}
                digest, handles = evidence_manifest(actual)
                receipt = {key: package[key] for key in ("snapshot_id", "package_id")}
                receipt.update(model_call_ref=f"loopback-{label}", revision_ids=[],
                               route_ref=actor["route_ref"], context_epoch="1")
                path = f"/v2/deliveries/{package['delivery_id']}/receipts"
                raw["run"][label]["actual"] = actual
                status, loaded = browser.request(path, "POST", {**receipt, "event_id": f"loaded-{label}",
                    "kind": "loaded", "loaded_evidence_refs": handles, "loaded_manifest_digest": digest,
                    **({"missing_reason": "host_budget"} if run["user_followups"] else {})}, authentication=headers)
                raw["run"][label]["loaded"] = {"status": status, "body": loaded}
                if status != 200:
                    return raw, {}
                artifact = service.work / f"result-{label}"
                if run["user_followups"]:
                    artifact.write_text("", encoding="utf-8")
                    for index, text in enumerate(run["user_followups"]):
                        captured_turn = post(owner, "/v2/events", {"source_id": source, "cursor": "c1",
                            "next_cursor": f"followup-{index}", "events": [{"native_object": f"{label}-{index}",
                             "native_version": "1", "actor_kind": "user_message", "task_id": task,
                             "at": data["clock"], "text": text}]}, f"followup-{label}-{index}")
                        raw["run"][label]["followups"].append(captured_turn)
                else:
                    output = next(row for row in actual["evidence"] if "write" in json.loads(row["text"]))
                    check = json.loads(output["text"])["write"]
                    if not isinstance(check, dict) or check.get("kind") != "artifact_check_v1":
                        raw["run"][label]["tool_blocked"] = "required marker absent from observed source evidence"
                        continue
                    artifact.write_text(check["marker"], encoding="utf-8")
                    used_request = {**receipt, "event_id": f"used-{label}", "kind": "used",
                        "used_evidence_refs": [output["handle"]], "evidence_ref": artifact.name,
                        "artifact_check_ref": {"handle": output["handle"], "field": "write"}}
                    wrong_status, wrong = browser.request(path, "POST", {**used_request, "event_id": f"wrong-field-{label}",
                        "artifact_check_ref": {"handle": output["handle"], "field": "parse"}}, authentication=headers)
                    raw["run"][label]["wrong_field_control"] = {"status": wrong_status, "body": wrong}
                    if wrong_status != 409:
                        raise RuntimeError("an untyped check was accepted as an artifact marker")
                    status, used = browser.request(path, "POST", used_request,
                        authentication=headers)
                    raw["run"][label]["used"] = {"status": status, "body": used}
        expected = re.search(r"check-token=([A-Z0-9]+)", data["events"][0]["checks"]["write"])[1]
        deliveries = {entry["delivery_id"]: entry for entry in owner.request("/v2/deliveries")[1]["deliveries"]}
        for run in data["metric_runs"]:
            label = run["id"]
            file = service.work / f"result-{label}"
            if not file.exists():
                continue
            readback = file.read_bytes()
            package = raw["run"][label]["delivery"]
            result = "passed" if readback == expected.encode("utf-8") else "failed"
            attempt = deliveries[package["delivery_id"]]["attempt_id"]
            status, outcome = browser.request(f"/v2/attempts/{attempt}/outcomes", "POST", {
                "event_id": f"verify-{label}", "status": result, "validator_ref": "independent-fixture",
                "artifact_ref": file.name, "evidence_ref": "sha256:" + hashlib.sha256(readback).hexdigest(),
                "delivery_ids": [package["delivery_id"]]}, authentication=verifier)
            raw["verify"][label] = {"status": status, "body": outcome, "artifact_text": readback.decode("utf-8")}
        status, response = owner.request("/v2/deliveries")
        raw["metrics"] = {"status": status, "body": response}
        if status != 200:
            return raw, {}
        named = {item["delivery"]["delivery_id"]: label for label, item in raw["run"].items()}
        ledger = [item for item in response["deliveries"] if item["delivery_id"] in named]
        counter = lambda values, keys: {key: values[key] for key in keys}
        coverage = Counter(
            "unknown" if row["loaded"] is None or observations[named[row["delivery_id"]]] is None
            else "yes" if required.keys() <= observations[named[row["delivery_id"]]] else "no" for row in ledger)
        outcome = Counter(row["outcome_state"] for row in ledger)
        used = Counter(row["used_state"] for row in ledger)
        success = sum(row["outcome_state"] == "passed" and not raw["run"][named[row["delivery_id"]]]["followups"]
                      for row in ledger)
        metrics = {"provided": len(ledger), "all_required_in_final_input": counter(coverage, ("yes", "no", "unknown")),
                   "use_observations": counter(used, ("observed", "unknown")),
                   "outcomes": counter(outcome, ("passed", "failed", "unknown")),
                   "no_rebrief_and_verified": {"numerator": success, "denominator": len(ledger)}}
        return raw, {**raw, "metrics": metrics}
