"""Run the frozen community extractor scenario against bmem's real worker."""

import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from urllib.request import Request, urlopen

from baseline_probe import isolated, post, source
from replay_optimization import bound, load, task
from score import catalogue, grade

SUPPORTED = {"X01-04", "X01-05", "X01-11"}


def model_fixture(service, proposal):
    calls = []

    class Extractor(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/api/chat":
                self.send_error(404)
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.loads(data["messages"][-1]["content"])
            if payload["event_id"] == "probe" or proposal is None:
                candidates = []
            else:
                calls.append(payload)
                if len(calls) == 1:
                    self.send_error(504, "fixture upstream timeout")
                    return
                candidates = [{
                    "kind": "fact", "subject": proposal["proposed_kind"], "key": "code_example.language",
                    "value": proposal["text"], "support": proposal["support"],
                }]
            response = {"done": True, "done_reason": "stop",
                "message": {"content": json.dumps({"candidates": candidates}, ensure_ascii=False)}}
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Extractor)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def close():
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()

    service.addCleanup(close)
    return f"http://127.0.0.1:{server.server_port}", calls


def failed_job(owner, job_id=None, attempts=1):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if job_id is None:
            status, result = owner.request("/v2/jobs")
            jobs = result["jobs"] if status == 200 else []
        else:
            status, result = owner.request("/v2/jobs/" + job_id)
            jobs = [result] if status == 200 else []
        matches = [job for job in jobs if job["status"] in ("failed", "done") and job["attempts"] >= attempts]
        if matches:
            one, = matches
            return one
        sleep(0.05)
    raise RuntimeError("extraction job did not reach an observable terminal state")


def distinct_runs(service, owner, data):
    task_id = task(owner, "independent runs", "community-deduplication")
    endpoint, _ = model_fixture(service, None)
    service.cli("model", "add", "dedup-sidecar", "--protocol", "ollama",
                "--endpoint", endpoint, "--model-id", "dedup-sidecar")
    service.cli("model", "test", "dedup-sidecar")
    service.cli("model", "use", "dedup-sidecar", "--for", "extract")
    source_id = source(owner, "independent-runs")
    service.operation(owner, {"kind": "change_grant", "source_id": source_id,
                              "purpose": "extract", "allow": True}, "allow-method-observation")
    runner = bound(service, owner, "fixture-runner", task_id, label="runner")
    verifier = bound(service, owner, "fixture-verifier", task_id, label="verifier", verify=True)
    subject = {"task_id": task_id, "artifact": "verification-run", "method": "check"}
    condition = "已采集校验回执"
    version = "1"
    handles, observations, actual_runs = {}, {}, {}
    cursor = None
    for entry in data["events"]:
        label = entry["id"]
        verified = entry["role"] == "tool"
        event = {"native_object": ("run-" + entry["run"]) if verified else label,
                 "native_version": "1", "task_id": task_id, "at": "2026-11-22T00:00:00Z"}
        if verified:
            event.update(actor_kind="tool_result", checks={"result_text": entry["text"]})
        else:
            event.update(actor_kind="assistant_message", text=entry["text"])
        captured = post(owner, "/v2/events", {"source_id": source_id, "events": [event],
            "cursor": cursor, "next_cursor": label}, "capture-" + label)
        cursor = label
        handle = handles[label] = "event_" + captured["events"][0]["event_id"]
        params = {"artifact": subject["artifact"], "method": subject["method"], "version": version,
                  "handle": handle, "observed_condition": condition}
        if verified:
            delivery = load(runner, "independent-run-" + entry["run"], task_id)
            browser, authentication, registration = verifier
            status, result = browser.request(f"/v2/attempts/{delivery['attempt_id']}/outcomes", "POST", {
                "event_id": label, "status": "passed", "validator_ref": "independent-verification",
                "artifact_ref": json.dumps({"artifact": subject["artifact"], "version": version}),
                "evidence_ref": handle,
            }, "verify-" + label, authentication=authentication)
            if status != 200:
                raise ValueError((status, result))
            actual_runs[label] = result
            params.update(kind="outcome", verifier_id=registration["client_id"], outcome_event_id=result["event_id"])
        else:
            params.update(kind="reported", cites=handles[entry["quotes"]])
        observations[label] = post(owner, "/v2/methods/observe", params, "record-" + label)
    suggestion = post(owner, "/v2/methods/propose", {**subject, "precondition": condition,
        "text": data["candidate_strategy"], "supported_versions": [version],
        "citations": list(handles.values())}, "text-similarity-proposal")
    accepted = post(owner, "/v2/methods/judge", {**subject, "candidate_id": suggestion["candidate_id"]}, "decision")
    if accepted["status"] != "accepted":
        raise ValueError("two independent runs should support the adjudicated method")
    outcome = post(owner, "/v2/methods/query", {**subject, "version": version, "conditions": [condition]}, "method-check")
    supported = outcome["version_results"][version]
    aliases = {handle: alias for alias, handle in handles.items()}
    return {"apply": {"observations": observations, "verifier_receipts": actual_runs, "suggestion": suggestion,
        "decision": accepted, "authority": outcome}}, {"apply": {
        "independent_roots": supported["independent_passed"],
        "verified_roots": [aliases[handle] for handle in supported["support_ids"]],
    }}


def isolated_embedding(service, owner, data):
    models = data["versions"]
    old = models["old"]["model"]
    candidate = models["candidate"]["model"]
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received = request["model"]
            dimensions = next(model["dimension"] for model in models.values() if model["model"] == received)
            calls.append(request)
            vector = [0.0] * dimensions
            vector[0 if received == old else 1] = 1.0
            body = json.dumps({"embeddings": [vector]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def close():
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()

    service.addCleanup(close)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    service.cli("config", "set", "vector_search.enabled", "true")
    for model in (old, candidate):
        service.cli("model", "add", model, "--protocol", "ollama", "--endpoint", endpoint, "--model-id", model)
        service.cli("model", "test", model, "--for", "embed")
    service.cli("model", "use", old, "--for", "embed")
    candidate_id = next(row["id"] for row in service.cli("model", "list")["models"] if row["model_id"] == candidate)
    source_id = source(owner, "embedding-shadow")
    service.operation(owner, {"kind": "change_grant", "source_id": source_id,
                              "purpose": "embed", "allow": True}, "initial-embedding")
    task_id = task(owner, "vector isolation", "community-embedding")
    bound_reader = bound(service, owner, data["reader"], task_id, [source_id])
    browser, authentication, _ = bound_reader
    controls = {}
    cursor = None

    def submit(row):
        nonlocal cursor
        label = row.get("id", row.get("withdraw"))
        event = {"native_object": label, "native_version": "1", "actor_kind": "user_message",
                 "at": "2026-11-22T00:00:00Z", "task_id": task_id}
        event.update({"change_kind": "revoked"} if "withdraw" in row else {"text": row["text"]})
        response = post(owner, "/v2/events", {"source_id": source_id, "cursor": cursor,
            "next_cursor": str(row["seq"]), "events": [event]}, "capture-" + str(row["seq"]))
        cursor = str(row["seq"])
        if "withdraw" not in row:
            controls[label] = response["events"][0]["event_id"]
        return response

    old_input = [submit(row) for row in data["source_sequence"] if row["seq"] <= data["candidate_applied_through"]]
    uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    deadline = monotonic() + 15
    with sqlite3.connect(uri, uri=True) as db:
        while db.execute("SELECT count(*) FROM vector_projection").fetchone()[0] != len(controls):
            if monotonic() >= deadline:
                raise RuntimeError("old model did not index the original snapshot")
            sleep(0.05)

    def build(key):
        snapshot = post(owner, "/v2/community/embed-inputs", {"source_id": source_id}, key + "-input")
        vectors = []
        for item in snapshot["items"]:
            request = Request(endpoint + "/api/embed", json.dumps({"model": candidate, "input": [item["text"]]}).encode(),
                              {"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as reply:
                vectors.append({"event_id": item["event_id"], "vector": json.load(reply)["embeddings"][0]})
        result = post(owner, "/v2/community/embed-stage", {"source_id": source_id, "model_id": candidate_id,
            "authority_cut": snapshot["authority_cut"], "vectors": vectors}, key + "-stage")
        return {"snapshot": snapshot, "result": result}

    staged = build("original-watermark")
    if int(staged["result"]["applied_through"]) != data["candidate_applied_through"]:
        raise ValueError("candidate projection used the wrong fixture watermark")
    withdrawn = [submit(row) for row in data["source_sequence"] if row["seq"] > data["candidate_applied_through"]]
    request = {"source_id": source_id, "model_id": candidate_id}
    code, attempted = owner.request("/v2/community/embed-activate", "POST", request)
    observed = {}
    for label, event_id in controls.items():
        status, result = browser.request("/v2/evidence/expand", "POST", {"handle": "event_" + event_id},
                                        authentication=authentication)
        observed[label] = {"status": status, "body": result}
    current = post(owner, "/v2/community/embed-inputs", {"source_id": source_id}, "still-serving")
    lexical_query = next(row["text"] for row in data["source_sequence"] if "text" in row)
    serving = post(owner, "/v2/search", {"query": lexical_query, "mode": "vector", "task_id": task_id}, "live-vector")
    with sqlite3.connect(uri, uri=True) as db:
        live_routes = db.execute("SELECT DISTINCT route,dimensions FROM vector_projection").fetchall()
        shadow_routes = db.execute("SELECT DISTINCT route,dimensions FROM vector_shadow_rows").fetchall()
    if not serving["results"] or serving["retrieval"] != "vector":
        raise ValueError("active model did not serve its indexed control")
    if attempted.get("code") != "projection_stale":
        raise ValueError({"expected_stale_rejection": attempted, "status": code})
    service.cli("config", "set", "vector_search.enabled", "false")
    fallback_result = post(owner, "/v2/search", {"query": lexical_query, "mode": "vector", "task_id": task_id}, "fallback")
    caller_result = browser.request("/v2/evidence/window", "POST", {"query": lexical_query,
        "task_id": task_id}, authentication=authentication)
    aliases = {event_id: label for label, event_id in controls.items()}
    service.cli("config", "set", "vector_search.enabled", "true")
    service.cli("model", "test", candidate, "--for", "embed")
    refreshed = build("fresh-watermark-control")
    service.cli("model", "use", candidate, "--for", "embed")
    activated = post(owner, "/v2/community/embed-activate", request, "fresh-activate")
    subsequent = post(owner, "/v2/search", {"query": lexical_query, "mode": "vector", "task_id": task_id}, "new-vector")
    with sqlite3.connect(uri, uri=True) as db:
        refreshed_namespaces = db.execute("SELECT DISTINCT route FROM vector_projection").fetchall()
    if (activated["namespace"] != refreshed["result"]["namespace"] or subsequent["retrieval"] != "vector"
            or refreshed_namespaces != [(activated["namespace"],)] or not subsequent["results"]
            or any(aliases[item["event_id"]] == "s1" for item in subsequent["results"])):
        raise ValueError("current, rebuilt namespace did not serve exclusively")
    return {"probe": {"old_ingest": old_input, "candidate": staged, "withdrawn": withdrawn,
        "activation": {"status": code, "body": attempted}, "direct": observed,
        "serving_projection": live_routes, "shadow_projection": shadow_routes,
        "serving_namespace": current["serving_namespace"], "live_query": serving,
        "provider_models": [request["model"] for request in calls]},
        "back": {"fallback": fallback_result, "bound_lexical": caller_result},
        "positive_cutover_control": {"staged": refreshed, "activated": activated,
                                     "served": subsequent, "namespaces": refreshed_namespaces}}, {
        "probe": {"candidate_enabled": code == 200 and attempted.get("enabled") is True,
            "mixed_embedding_spaces": len(live_routes) != 1 or len(shadow_routes) != 1 or
                live_routes[0][0] == shadow_routes[0][0] or live_routes[0][1] != shadow_routes[0][1],
            "visible_ids": [label for label, result in observed.items() if result["status"] == 200]},
        "back": {"channel": fallback_result["retrieval"],
            "visible_ids": [aliases[item["event_id"]] for item in fallback_result["results"]
                            if item.get("event_id") in aliases]},
    }


def replay_community_opt(case, binary):
    if case["id"] not in SUPPORTED:
        return {}, {}
    fixture = case["fixture"]
    with isolated() as (service, owner):
        if case["id"] == "X01-04":
            return distinct_runs(service, owner, fixture)
        if case["id"] == "X01-11":
            return isolated_embedding(service, owner, fixture)
        timeout, proposal = fixture["scripted_responses"]
        if timeout != "timeout":
            raise ValueError("unsupported fixture timeout")
        endpoint, calls = model_fixture(service, proposal)
        route = fixture["route"]
        service.cli("model", "add", route, "--protocol", "ollama", "--endpoint", endpoint, "--model-id", route)
        service.cli("model", "test", route)
        service.cli("model", "use", route, "--for", "extract")
        admitted = source(owner, "authorized")
        blocked = source(owner, "forbidden")
        service.operation(owner, {"kind": "change_grant", "source_id": admitted,
                                  "purpose": "extract", "allow": True}, "allow-extract")
        captures = {}
        for label, sid in [("forbidden", blocked), ("authorized", admitted)]:
            row = fixture[label]
            captures[label] = post(owner, "/v2/events", {"source_id": sid, "next_cursor": "first",
                "events": [{"native_object": row["id"], "native_version": "1",
                    "actor_kind": "user_message", "text": row["text"], "at": "2026-11-23T00:00:00Z"}]}, label)
        first = failed_job(owner)
        job_id = first.get("job_id") or first["id"]
        retrying = post(owner, "/v2/jobs/" + job_id + "/retry", {}, "retry")
        final = failed_job(owner, job_id, attempts=2)
        status, authority = owner.request("/v2/statements")
        if status != 200:
            raise ValueError((status, authority))
        with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            count, = db.execute("SELECT count(*) FROM ing_event_versions WHERE source_id=?",
                                [admitted]).fetchone()
            decision_log, = db.execute("SELECT result FROM ing_jobs WHERE job_id=?", (job_id,)).fetchone()
        decisions = json.loads(decision_log)["decisions"]
        accepted_count = sum(row["kind"] in ("adopted", "merged") for row in decisions)
        if len(calls) != 2 or accepted_count:
            raise ValueError("expected two actual model calls and no accepted candidates")
        if fixture["authorized"]["text"] not in json.dumps(calls[0], ensure_ascii=False):
            raise ValueError("the first model request did not have the authorized fixture source")
        return {
            "attempt": {"capture": captures, "provider_request": calls[0], "job": first, "transport": "HTTP 504"},
            "retry": {"request": calls[1], "proposal": proposal, "admission": retrying, "job": final,
                      "statements": authority, "recorded_source_versions": count,
                      "accepted_candidates": accepted_count, "decisions": decisions},
        }, {
            "attempt": {"outbound_text": json.dumps(calls[0], ensure_ascii=False),
                        "retryable": first["status"] == "failed" and retrying["status"] == "pending"},
            "retry": {"global_values": [str(row["value"]).title() for row in authority["statements"]
                                               if row["scope"]["kind"] == "global"],
                      "recorded_source_versions": count},
        }


if __name__ == "__main__":
    binary = Path(sys.argv[1]).resolve()
    os.environ["BMEM_V2_TEST_BIN"] = str(binary)
    case = catalogue()[sys.argv[2] if len(sys.argv)>2 else "X01-05"]
    _, projected = replay_community_opt(case, binary)
    result, failures = grade(case, projected)
    print(case["id"], result, failures)
    raise SystemExit(result != "PASS")
