"""Generation failure metadata through synthetic HTTP and real Store/Worker."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
import traceback

import httpx
import pytest

from bmem.runtime import Runtime, Worker
from bmem.store import Store


TOKEN_REPEAT = "prediction aborted, token repeat limit reached"
PROMPT = "合成诊断🐾 SOURCE_BODY_MARKER"
UPSTREAM_SECRET = "UPSTREAM_PRIVATE_SENTINEL_7391"
GENERATED_FRAGMENT = "GENERATED_FRAGMENT_SENTINEL_5182"
TRANSPORT_SECRET = "TRANSPORT_PRIVATE_SENTINEL_8046"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    return Store(tmp_path / "memory")


@pytest.fixture
def generation_server(monkeypatch):
    state = {"responses": [], "requests": [], "delay": 0}

    class GenerationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, payload))
            status, body = state["responses"].pop(0) if state["responses"] else (
                500, {"error": "unexpected fixture request"}
            )
            time.sleep(state["delay"])
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), GenerationHandler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    monkeypatch.setenv("BMEM_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_diagnostics(message, request, stage, category):
    payload = request[1]
    assert request[0] == "/api/generate"
    for name, expected in (("stage", stage), ("error_category", category)):
        match = re.search(r"\b" + name + r"=([a-z_]+)\b", message)
        assert match is not None, message
        assert match.group(1) == expected
    for name, expected in (("num_predict", payload["options"]["num_predict"]),
                           ("prompt_chars", len(payload["prompt"]))):
        match = re.search(r"\b" + name + r"=(\d+)\b", message)
        assert match is not None, message
        assert int(match.group(1)) == expected
    elapsed = re.search(r"\belapsed_ms=(\d+)\b", message)
    assert elapsed is not None, message
    assert int(elapsed.group(1)) >= 0
    assert PROMPT not in message
    assert UPSTREAM_SECRET not in message
    assert GENERATED_FRAGMENT not in message
    assert TRANSPORT_SECRET not in message


def run_event(store, source_type, runtime=None):
    event = store.ingest({"body": PROMPT, "source_type": source_type})
    worker = Worker(store, runtime or Runtime(store.home))
    worker._mark_maintenance()
    return event, worker.run_once()


def assert_failed_event(store, event, result):
    assert result["status"] == "failed", result
    saved = store.get_event(event["event_id"])
    assert saved["body"] == PROMPT
    assert saved["has_evidence"] is True
    with store._read() as conn:
        job = conn.execute("SELECT status,last_error FROM jobs WHERE job_id=?",
                           (event["job_id"],)).fetchone()
        assert job["status"] == "failed"
        assert job["last_error"] == result["error"]
    assert store.stats()["memories"] == 0


@pytest.mark.parametrize("stage,source_type,budgets", [
    ("durability_gate", "user_dialogue", [64]),
    ("candidate_generation", "verification", [800]),
    ("candidate_retry", "verification", [800, 3200]),
])
def test_worker_http_repeat_failure_reports_actual_phase_and_budget(
    store, generation_server, stage, source_type, budgets
):
    if stage == "candidate_generation":
        generation_server["delay"] = 0.03
    if stage == "candidate_retry":
        generation_server["responses"].append((200, {
            "response": '{"candidates": [', "done_reason": "length",
        }))
    generation_server["responses"].append((500, {"error": TOKEN_REPEAT}))

    event, result = run_event(store, source_type)

    assert_failed_event(store, event, result)
    requests = generation_server["requests"]
    assert [request[1]["options"]["num_predict"] for request in requests] == budgets
    assert_diagnostics(result["error"], requests[-1], stage, "token_repeat_limit")
    if stage == "candidate_generation":
        assert int(re.search(r"\belapsed_ms=(\d+)\b", result["error"]).group(1)) >= 25
    assert all(request[1]["think"] is False for request in requests)
    assert all(request[1]["options"] == {
        "temperature": 0.1, "top_p": 0.9, "num_predict": budget,
        "num_ctx": 8192, "seed": 42,
    } for request, budget in zip(requests, budgets))
    if stage == "candidate_retry":
        assert requests[0][1]["prompt"] == requests[1][1]["prompt"]
        assert requests[0][1]["format"] == requests[1][1]["format"]


@pytest.mark.parametrize("status,error,category", [
    (500, TOKEN_REPEAT, "token_repeat_limit"),
    (503, TOKEN_REPEAT, "http_error"),
    (500, TOKEN_REPEAT + " extra", "http_error"),
    (502, UPSTREAM_SECRET, "http_error"),
    (404, UPSTREAM_SECRET, "model_not_installed"),
])
def test_http_failure_categories_and_unknown_response_redaction(
    store, generation_server, status, error, category
):
    generation_server["responses"].append((status, {
        "error": error, "response": GENERATED_FRAGMENT,
    }))
    runtime = Runtime(store.home)

    with pytest.raises(RuntimeError) as failed:
        runtime._generate_json(PROMPT)

    assert len(generation_server["requests"]) == 1
    message = str(failed.value)
    assert_diagnostics(message, generation_server["requests"][0], "candidate_generation", category)
    if category == "http_error":
        assert "upstream HTTP error" in message
        assert error not in message
    if status == 404:
        assert "not installed" in message
        assert "runtime install" in message


def test_transport_failure_has_safe_metadata_without_secret_traceback(store, monkeypatch):
    requests = []

    def fail_transport(request):
        requests.append((request.url.path, json.loads(request.content)))
        raise httpx.ConnectError(TRANSPORT_SECRET, request=request)

    runtime = Runtime(store.home)
    monkeypatch.setattr(runtime, "_http", lambda *_args: httpx.Client(
        transport=httpx.MockTransport(fail_transport), trust_env=False,
    ))
    with pytest.raises(RuntimeError) as failed:
        runtime._generate_json(PROMPT)

    assert len(requests) == 1
    assert_diagnostics(str(failed.value), requests[0], "candidate_generation", "transport_error")
    assert TRANSPORT_SECRET not in "".join(traceback.format_exception(failed.value))

    event, result = run_event(store, "verification", runtime)

    assert len(requests) == 2
    assert_failed_event(store, event, result)
    assert_diagnostics(result["error"], requests[-1], "candidate_generation", "transport_error")


def test_http_200_valid_candidates_complete_without_failure_metadata(store, generation_server):
    claim = "Synthetic generation diagnostics successful fact"
    generation_server["responses"].append((200, {
        "response": json.dumps({"candidates": [{
            "body": claim, "kind": "fact", "fact_key": "diagnostic.success",
            "conditions": {}, "confidence": 0.9, "derived_from": [], "action": "ADD",
            "memory_id": None, "expected_revision": None,
        }], "has_more": False}), "done_reason": "stop",
    }))

    event, result = run_event(store, "verification")

    assert result["status"] == "done", result
    assert result["error"] is None
    assert len(generation_server["requests"]) == 1
    assert generation_server["requests"][0][1]["options"]["num_predict"] == 800
    with store._read() as conn:
        job = conn.execute("SELECT status,last_error FROM jobs WHERE job_id=?",
                           (event["job_id"],)).fetchone()
        assert tuple(job) == ("done", None)
    assert store.get_event(event["event_id"])["has_evidence"] is True
    assert store.stats()["memories"] == 1
    memory = store.get_memory(result["applied"][0]["memory_id"])
    assert memory["body"] == claim
    assert memory["kind"] == "fact"


def test_http_200_model_error_is_safe_and_preserves_failed_event(store, generation_server):
    generation_server["responses"].append((200, {
        "error": UPSTREAM_SECRET, "response": GENERATED_FRAGMENT,
    }))

    event, result = run_event(store, "verification")

    assert_failed_event(store, event, result)
    assert len(generation_server["requests"]) == 1
    assert generation_server["requests"][0][1]["options"]["num_predict"] == 800
    assert_diagnostics(result["error"], generation_server["requests"][0],
                       "candidate_generation", "model_error")
