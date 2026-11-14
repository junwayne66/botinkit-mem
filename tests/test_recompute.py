"""Dependency invalidation and queue convergence against the real local store."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from bmem.runtime import Runtime, Worker
from bmem.store import Store


@pytest.fixture
def shared_source_memories(tmp_path):
    store = Store(tmp_path)
    store.set_settings({"semantic_enabled": True})
    shared = store.ingest({"body": "共同的旧环境信息", "source_type": "user_dialogue"})
    memories = []
    for name in ("root", "first", "second"):
        candidate = {"body": f"{name} 独立事实", "kind": "fact", "fact_key": name,
                     "action": "ADD", "confidence": 0.9}
        memory = store.apply_candidate(shared["event_id"], candidate, "fixture")["memory"]
        if name != "root":
            own = store.ingest({"body": candidate["body"], "source_type": "user_dialogue"})
            store.apply_candidate(own["event_id"], {
                **candidate, "action": "UPDATE", "memory_id": memory["memory_id"],
                "expected_revision": memory["revision_no"],
            }, "fixture")
            store.finish_job(own["job_id"])
        memories.append(store.get_memory(memory["memory_id"]))
    store.finish_job(shared["job_id"])
    return store, memories


@pytest.fixture
def model_endpoint(monkeypatch):
    recomputed = []

    class ModelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/embed":
                output = {"embeddings": [[1.0] + [0.0] * 1023 for _ in payload["input"]]}
            elif self.path == "/api/generate":
                schema = payload["format"]
                item = schema["properties"]["candidates"]["items"]
                candidates = []
                if item["properties"]["action"].get("enum") == ["UPDATE"]:
                    target = item["properties"]["memory_id"]["enum"][0]
                    recomputed.append(target)
                    candidates.append({
                        "body": f"重算后的独立事实 {target}", "kind": "fact",
                        "conditions": {}, "confidence": 0.9, "action": "UPDATE",
                        "memory_id": target,
                        "derived_from": item["properties"]["derived_from"]["items"]["enum"],
                    })
                output = {"response": json.dumps({"candidates": candidates})}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BMEM_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield recomputed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("edge", ["root", "derived_from"])
@pytest.mark.parametrize("invalidated", [False, True])
def test_only_valid_sources_trigger_recompute(tmp_path, edge, invalidated):
    store = Store(tmp_path)
    parent_result = store.declare({"body": "父事实旧版", "fact_key": "parent"})
    parent = parent_result["memory"]
    child_result = store.declare({"body": "独立子事实", "fact_key": "child"})
    child = child_result["memory"]
    # Exercise both current roots and legacy derived_from-only source rows.
    with store._write() as conn:
        conn.execute(
            "INSERT INTO sources(memory_id,event_id,root_source_id,derived_from,"
            "source_type,trust_tier,created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?)",
            (child["memory_id"], child_result["event_id"],
             parent_result["event_id"] if edge == "root" else "ref:legacy-parent",
             json.dumps([parent["memory_id"]]) if edge == "derived_from" else "[]",
             "derived", "T1", "2026-01-01T00:00:00Z",
             "2026-01-02T00:00:00Z" if invalidated else None),
        )

    store.declare({"body": "父事实新版", "memory_id": parent["memory_id"],
                   "expected_revision": parent["revision_no"]})

    jobs = [job for job in store.records("jobs") if job["payload"].get("recompute")]
    assert len(jobs) == (0 if invalidated else 1)
    detail = store.get_memory(child["memory_id"])
    assert detail["status"] == ("active" if invalidated else "proposed")
    assert detail["revision_no"] == child["revision_no"]


def test_shared_invalidated_source_converges_and_history_continues(
    shared_source_memories, model_endpoint,
):
    store, (root, first, second) = shared_source_memories
    historical = store.ingest({
        "body": "历史工具输出等待正常处理", "source_type": "external",
        "metadata": {"origin": "codex-rollout", "history_import": True},
    })
    store.declare({"body": "共同环境已修正", "memory_id": root["memory_id"],
                   "expected_revision": root["revision_no"]})
    for memory in (first, second):
        assert store.get_memory(memory["memory_id"])["status"] == "proposed"

    worker = Worker(store, Runtime(store.home))
    results = []
    for _ in range(20):
        result = worker.run_once()
        results.append(result)
        assert result["status"] != "failed", result
        if result["status"] == "idle":
            break

    assert results[-1]["status"] == "idle", results
    # The corrected root's shared source is initially still valid, so it also
    # receives one legitimate invalidation before the graph converges.
    assert Counter(model_endpoint) == Counter([
        root["memory_id"], first["memory_id"], second["memory_id"],
    ])
    for memory in (first, second):
        detail = store.get_memory(memory["memory_id"])
        assert detail["revision_no"] == memory["revision_no"] + 1
        assert detail["status"] == "active"
    vectors = worker._index().search([1.0] + [0.0] * 1023, limit=10)
    assert {item["memory_id"] for item in vectors} == {
        root["memory_id"], first["memory_id"], second["memory_id"],
    }
    for item in vectors:
        assert item["revision_no"] == store.get_memory(item["memory_id"])["revision_no"]
    history_job = next(job for job in store.records("jobs")
                       if job["job_id"] == historical["job_id"])
    assert history_job["status"] == "done"
    assert store.get_event(historical["event_id"])["body"] == "历史工具输出等待正常处理"
    assert all(job["status"] == "done" for job in store.records("jobs"))
    assert worker.run_once()["status"] == "idle"


def test_valid_transitive_dependencies_still_recompute(tmp_path, model_endpoint):
    store = Store(tmp_path)
    root = store.declare({"body": "根事实旧版", "fact_key": "root"})["memory"]
    child = store.declare({"body": "子事实", "fact_key": "child",
                           "derived_from": [root["memory_id"]]})["memory"]
    grandchild = store.declare({"body": "孙事实", "fact_key": "grandchild",
                                "derived_from": [child["memory_id"]]})["memory"]
    store.declare({"body": "根事实新版", "memory_id": root["memory_id"],
                   "expected_revision": root["revision_no"]})
    jobs = [job for job in store.records("jobs") if job["payload"].get("recompute")]
    assert {job["payload"]["memory_id"] for job in jobs} == {
        child["memory_id"], grandchild["memory_id"],
    }
    for job in jobs:
        assert store.get_memory(job["payload"]["memory_id"])["status"] == "proposed"
    worker = Worker(store, Runtime(store.home))
    for _ in range(20):
        result = worker.run_once()
        assert result["status"] != "failed", result
        if result["status"] == "idle":
            break
    assert result["status"] == "idle"
    assert Counter(model_endpoint) == Counter([child["memory_id"], grandchild["memory_id"]])
    for memory in (child, grandchild):
        assert store.get_memory(memory["memory_id"])["revision_no"] == memory["revision_no"] + 1


@pytest.mark.parametrize("edge", ["root", "derived_from"])
def test_forget_keeps_invalidated_source_history_in_erasure(shared_source_memories, edge):
    store, (root, first, second) = shared_source_memories
    store.declare({"body": "共同环境已修正", "memory_id": root["memory_id"],
                   "expected_revision": root["revision_no"]})
    if edge == "derived_from":
        with store._write() as conn:
            conn.execute(
                "UPDATE sources SET root_source_id=?,derived_from=? "
                "WHERE memory_id IN (?,?) AND invalidated_at IS NOT NULL",
                ("ref:legacy-shared", json.dumps([root["memory_id"]]),
                 first["memory_id"], second["memory_id"]),
            )

    result = store.forget(root["memory_id"])

    assert set(result["affected"]) == {first["memory_id"], second["memory_id"]}
    for memory in (root, first, second):
        assert store.get_memory(memory["memory_id"])["status"] == "deleted"
        assert store.get_memory(memory["memory_id"])["body"] == ""
    with store._read() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE body!=''").fetchone()[0] == 0
    assert list((store.home / "evidence").rglob("*.json")) == []
