"""Bounded embedding recovery through a real HTTP endpoint and local worker."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bmem.runtime import Runtime, VectorIndex, Worker
from bmem.store import Store


@pytest.fixture
def embedding_endpoint(tmp_path, monkeypatch):
    state = {"limit": 2048, "status": 413, "requests": []}

    class EmbeddingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(payload)
            texts = payload["input"]
            if any(len(text) > state["limit"] for text in texts):
                status = state["status"]
                output = {"error": "the input length exceeds the context length"}
            else:
                status = 200
                output = {"embeddings": [[float(ord(text[0]))] + [0.0] * 1023 for text in texts]}
            encoded = json.dumps(output).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield Runtime(tmp_path), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_embedding_413_recovers_with_bounded_unicode_prefixes(embedding_endpoint):
    runtime, state = embedding_endpoint
    texts = ["合成测试" * 1488]
    vectors = runtime.embed(texts)
    assert len(vectors) == 1
    assert vectors[0][0] == ord("合")
    assert [len(request["input"][0]) for request in state["requests"]] == [5952, 2976, 1488]
    assert all(request["truncate"] is True for request in state["requests"])
    assert texts == ["合成测试" * 1488]


def test_batch_413_keeps_order_and_full_short_inputs(embedding_endpoint):
    runtime, state = embedding_endpoint
    texts = ["short before", "合成测试" * 1488, "tail after"]
    vectors = runtime.embed(texts)
    assert [vector[0] for vector in vectors] == [ord("s"), ord("合"), ord("t")]
    assert state["requests"][0]["input"] == texts
    assert state["requests"][1]["input"] == [texts[0]]
    assert state["requests"][-1]["input"] == [texts[2]]
    assert len(state["requests"]) == 6


@pytest.mark.parametrize("text,request_count", [("合" * 16, 4), ("合", 1)])
def test_persistent_413_stops_after_bounded_recovery(embedding_endpoint, text, request_count):
    runtime, state = embedding_endpoint
    state["limit"] = 0
    with pytest.raises(RuntimeError, match="HTTP 413"):
        runtime.embed([text])
    assert len(state["requests"]) == request_count
    assert all(request["input"][0] for request in state["requests"])


@pytest.mark.parametrize("status", [400, 500])
def test_other_embedding_errors_propagate_without_shortening(embedding_endpoint, status):
    runtime, state = embedding_endpoint
    state.update({"limit": 0, "status": status})
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        runtime.embed(["合成测试"])
    assert len(state["requests"]) == 1


def test_normal_embedding_keeps_one_full_batch(embedding_endpoint):
    runtime, state = embedding_endpoint
    texts = ["short before", "tail after"]
    assert len(runtime.embed(texts)) == 2
    assert len(state["requests"]) == 1
    assert state["requests"][0]["input"] == texts


def test_recovery_observes_existing_deadline(embedding_endpoint, monkeypatch):
    runtime, state = embedding_endpoint
    state["limit"] = 0
    ticks = iter([100.0, 101.0])
    monkeypatch.setattr("bmem.runtime.time", SimpleNamespace(monotonic=lambda: next(ticks, 101.0)))
    with pytest.raises(RuntimeError, match="HTTP 413"):
        runtime.embed(["合成测试"], timeout=0.1)
    assert len(state["requests"]) == 1


def test_worker_indexes_recovered_embedding_and_retains_full_body(embedding_endpoint):
    runtime, state = embedding_endpoint
    store = Store(runtime.home)
    store.set_settings({"semantic_enabled": True})
    text = "合成测试 " * 1190
    memory = store.declare({"body": text})["memory"]
    result = Worker(store, runtime).run_once()
    assert result["status"] == "done"
    assert result["embedded"] == 1
    assert store.get_memory(memory["memory_id"])["body"] == text
    assert store.search("合成测试")[0]["memory_id"] == memory["memory_id"]
    hits = VectorIndex(store.home).search([1.0] + [0.0] * 1023)
    assert hits[0]["memory_id"] == memory["memory_id"]
    assert len(state["requests"]) == 3
