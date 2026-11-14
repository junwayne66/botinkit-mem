"""Real SQLite vector-index behavior using small, explicit numeric fixtures."""

import math
import io
import json
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bmem.runtime import (DISTILL_MODEL, EMBEDDING_SPACE, MODEL_SPECS, Runtime, VectorIndex,
                          _target_snapshot, _validate_distill_targets)


def test_historical_external_outputs_remain_evidence_without_generation(tmp_path, monkeypatch):
    from bmem.runtime import Worker
    from bmem.store import Store

    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    store = Store(tmp_path)
    event = store.ingest({
        "body": "Historical build output preserved as evidence",
        "harness": "codex", "session_id": "imported-session",
        "source_type": "external", "metadata": {"origin": "codex-rollout"},
    })
    worker = Worker(store, Runtime(tmp_path))
    result = worker.run_once()
    assert result["status"] == "done"
    assert result["skipped"] == "historical_external_evidence"
    assert store.get_event(event["event_id"])["body"] == "Historical build output preserved as evidence"
    assert store.stats()["memories"] == 0
    worker.run_once()  # The first cycle also schedules daily maintenance.

    store.ingest({
        "body": "New external observation still follows ordinary arbitration",
        "harness": "codex", "session_id": "live-session",
        "source_type": "external", "metadata": {"origin": "native-live"},
    })
    assert worker.run_once()["status"] == "failed"


def test_busy_embedding_falls_back_before_hook_deadline_and_keeps_background_budget(tmp_path, monkeypatch):
    from bmem.runtime import Worker
    from bmem.store import Store

    request_paths = []
    vector = [1.0] + [0.0] * 1023

    class DelayedEmbeddingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            request_paths.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(2.25)
            payload = json.dumps({"embeddings": [vector]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DelayedEmbeddingHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("BMEM_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        store = Store(tmp_path)
        memory = store.declare({"body": "语义拥塞时仍召回山楂糕", "fact_key": "recall.deadline"})["memory"]
        store.set_settings({"semantic_enabled": True})
        runtime = Runtime(tmp_path)
        worker = Worker(store, runtime)

        started = time.monotonic()
        recalled = worker.recall({"query": "山楂糕", "session_id": "delayed-embedding"})
        elapsed = time.monotonic() - started

        # The 0.75s semantic deadline leaves room inside the host hook budget;
        # allow a small scheduling margin on shared CI machines.
        assert elapsed < 1.75, elapsed
        assert recalled["degraded"] is True
        assert [item["memory_id"] for item in recalled["items"]] == [memory["memory_id"]]
        assert len(store.records("injections")) == 1
        assert runtime.embed(["普通回填继续等待慢端点"]) == [vector]
        assert request_paths == ["/api/embed", "/api/embed"]
        assert len(store.records("injections")) == 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_truncated_generation_retries_once_with_larger_budget(tmp_path, monkeypatch):
    requests = []
    schema = {"type": "object", "properties": {"candidates": {"type": "array"}},
              "required": ["candidates"]}

    class GenerationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            if payload["prompt"] == "truncated" and payload["options"]["num_predict"] == 3200:
                output = {"response": '{"candidates": []}', "done_reason": "stop"}
            else:
                reason = "length" if payload["prompt"] == "truncated" else "stop"
                if payload["prompt"] == "unknown_reason":
                    reason = "untrusted finish metadata"
                output = {"response": '{"candidates": [', "done_reason": reason}
            encoded = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), GenerationHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("BMEM_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        runtime = Runtime(tmp_path)
        assert runtime._generate_json("truncated", schema) == {"candidates": []}
        with pytest.raises(RuntimeError, match="requested JSON schema") as invalid:
            runtime._generate_json("invalid", schema)
        assert "done_reason=stop" in str(invalid.value)
        assert "num_predict=800" in str(invalid.value)
        with pytest.raises(RuntimeError, match="requested JSON schema") as truncated:
            runtime._generate_json("truncated", schema, num_predict=64)
        assert "done_reason=length" in str(truncated.value)
        assert "num_predict=64" in str(truncated.value)
        with pytest.raises(RuntimeError, match="requested JSON schema") as unknown:
            runtime._generate_json("unknown_reason", schema, num_predict=64)
        assert "done_reason=unknown" in str(unknown.value)
        assert "untrusted finish metadata" not in str(unknown.value)
        assert '{"candidates"' not in str(unknown.value)
        assert [path for path, _ in requests] == ["/api/generate"] * 5
        assert [(payload["prompt"], payload["options"]["num_predict"]) for _, payload in requests] == [
            ("truncated", 800), ("truncated", 3200), ("invalid", 800),
            ("truncated", 64),
            ("unknown_reason", 64),
        ]
        assert all(payload["format"] == schema for _, payload in requests)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_cosine_index_order_and_revision_replacement(tmp_path):
    index = VectorIndex(tmp_path)
    vector = [1.0] + [0.0] * 1023
    orthogonal = [0.0, 1.0] + [0.0] * 1022
    opposite = [-1.0] + [0.0] * 1023
    index.upsert({"memory_id": "near", "revision_no": 1}, vector)
    index.upsert({"memory_id": "orthogonal", "revision_no": 1}, orthogonal)
    index.upsert({"memory_id": "opposite", "revision_no": 1}, opposite)
    results = index.search(vector, limit=3)
    assert [item["memory_id"] for item in results] == ["near", "orthogonal", "opposite"]
    assert results[0]["score"] > results[1]["score"] > results[2]["score"]
    index.upsert({"memory_id": "near", "revision_no": 2}, vector)
    replaced = index.search(vector, limit=3)
    assert replaced[0]["revision_no"] == 2
    assert sum(item["memory_id"] == "near" for item in replaced) == 1


@pytest.mark.parametrize("vector", [[1.0], [math.nan] * 1024, [math.inf] * 1024])
def test_invalid_embedding_boundary_rejected(tmp_path, vector):
    index = VectorIndex(tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        index.upsert({"memory_id": "invalid", "revision_no": 1}, vector)


def test_macos_distribution_keeps_runner_and_nested_libraries(tmp_path):
    archive = tmp_path / "official-layout.tgz"
    members = {"ollama": b"cli", "llama-server": b"runner",
               "libggml-base.dylib": b"library",
               "mlx_metal_v3/libmlx.dylib": b"mlx",
               "mlx_metal_v3/mlx.metallib": b"metal"}
    with tarfile.open(archive, "w:gz") as handle:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o755
            handle.addfile(member, io.BytesIO(content))
    binary = Runtime(tmp_path / "data")._extract_runtime(archive, {"flavor": "macos-tar"})
    for name, content in members.items():
        assert (binary.parent / name).read_bytes() == content


@pytest.mark.parametrize("tag,digest,ready", [
    ("qwen3:8b", MODEL_SPECS[DISTILL_MODEL]["expected_digest"], False),
    ("qwen3:4b", "sha256:359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7", False),
    (DISTILL_MODEL, "sha256:" + "0" * 64, False),
    (DISTILL_MODEL, MODEL_SPECS[DISTILL_MODEL]["expected_digest"], True),
])
def test_runtime_readiness_requires_exact_tag_and_digest(tmp_path, tag, digest, ready):
    status = Runtime(tmp_path)._models_status([{"name": tag, "digest": digest}], {})
    assert status[DISTILL_MODEL]["present"] is ready


def test_other_tag_digest_cannot_satisfy_wrong_requested_tag(tmp_path):
    tags = [{"name": DISTILL_MODEL, "digest": "sha256:" + "0" * 64},
            {"name": "qwen3:8b", "digest": MODEL_SPECS[DISTILL_MODEL]["expected_digest"]}]
    assert not Runtime(tmp_path)._models_status(tags, {})[DISTILL_MODEL]["present"]


def test_distill_targets_match_the_advertised_memory_limit(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    captured = {}

    def generate(prompt, schema):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return []

    monkeypatch.setattr(runtime, "_generate_candidates", generate)
    existing = [{"memory_id": f"memory-{index}", "revision_no": 1,
                 "body": f"配置记录 {index}", "status": "active", "trust_tier": "T1"}
                for index in range(12)]
    assert runtime.distill({"source_type": "user_declaration", "body": "请记住当前配置"}, existing) == []
    item = captured["schema"]["properties"]["candidates"]["items"]
    advertised_ids = {memory["memory_id"] for memory in existing[:8]}
    advertised_ids.add(None)
    assert set(item["properties"]["memory_id"]["enum"]) == advertised_ids
    assert "memory-8" not in captured["prompt"]


def test_vector_space_is_canonical(tmp_path):
    index = VectorIndex(tmp_path)
    vector = [1.0] + [0.0] * 1023
    index.upsert({"memory_id": "canonical", "revision_no": 1,
                  "space": "unrelated-embedding-space"}, vector)
    assert index.search(vector, limit=1)[0]["memory_id"] == "canonical"
    with index._connect() as connection:
        space = connection.execute("SELECT space FROM vec_memories WHERE memory_id='canonical'").fetchone()[0]
    assert space == EMBEDDING_SPACE


def test_target_binding_uses_snapshot_revision_and_excludes_self_ancestry():
    snapshot = _target_snapshot([{"memory_id": "target", "revision_no": 3,
                                 "body": "原有规范主张", "kind": "fact",
                                 "conditions": {"phase": "m1"}, "fact_key": "canonical.claim"},
                                 {"memory_id": "ancestor", "revision_no": 1}])
    candidate = {"action": "NOOP", "memory_id": "target", "expected_revision": 0,
                 "body": "模型改写", "kind": "method", "conditions": {}, "fact_key": "other.claim",
                 "derived_from": ["target", "ancestor"]}
    result = _validate_distill_targets([candidate], snapshot)[0]
    assert result["expected_revision"] == 3
    assert result["derived_from"] == ["ancestor"]
    assert result["body"] == "原有规范主张"
    assert result["kind"] == "fact"
    assert result["conditions"] == {"phase": "m1"}
    assert result["fact_key"] == "canonical.claim"


@pytest.fixture
def distill_batch(tmp_path, monkeypatch):
    from bmem.runtime import Worker
    from bmem.store import Store

    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    store = Store(tmp_path)
    store.set_settings({"semantic_enabled": False})
    target = store.declare({"body": "batch original", "fact_key": "batch.fact",
                            "effective_at": "2026-01-01T00:00:00Z"})["memory"]
    for job in store.records("jobs"):
        store.finish_job(job["job_id"])
    store.ingest({"body": "batch", "source_type": "verification"})
    runtime = Runtime(tmp_path)
    candidate = {"body": "batch updated", "kind": "fact", "fact_key": "batch.fact",
                 "conditions": {}, "derived_from": [], "action": "UPDATE",
                 "memory_id": target["memory_id"], "effective_at": "2026-02-01T00:00:00Z"}
    return store, runtime, Worker(store, runtime), target, candidate


@pytest.mark.parametrize("last_action", ["UPDATE", "NOOP", "DISPUTE"])
def test_distill_batch_uses_own_committed_revisions(distill_batch, monkeypatch, last_action):
    store, runtime, worker, target, candidate = distill_batch
    last_body = "batch disputed" if last_action == "DISPUTE" else candidate["body"]
    candidates = [
        {"body": "batch independent", "kind": "fact", "fact_key": "batch.extra",
         "conditions": {}, "derived_from": [], "action": "ADD"},
        candidate,
        {**candidate, "body": last_body, "action": last_action, "effective_at": "2026-03-01T00:00:00Z"},
    ]
    monkeypatch.setattr(runtime, "_generate_json", lambda *_args, **_kwargs: {"candidates": candidates})
    result = worker.run_once()
    assert result["status"] == "done", result
    current = store.get_memory(target["memory_id"])
    assert current["body"] == last_body
    assert current["revision_no"] == (3 if last_action == "DISPUTE" else 2)
    expected_time = "2026-02-01T" if last_action == "NOOP" else "2026-03-01T"
    assert current["effective_at"].startswith(expected_time)
    assert len(store.list_memories()) == 2


def test_distill_noop_keeps_revision_for_next_update(distill_batch, monkeypatch):
    store, runtime, worker, target, candidate = distill_batch
    candidates = [{**candidate, "action": "NOOP", "effective_at": "2026-01-01T00:00:00Z"}, candidate]
    monkeypatch.setattr(runtime, "_generate_json", lambda *_args, **_kwargs: {"candidates": candidates})
    assert worker.run_once()["status"] == "done"
    current = store.get_memory(target["memory_id"])
    assert current["revision_no"] == 2
    assert current["body"] == candidate["body"]


def test_distill_batch_still_rejects_external_revision(distill_batch, monkeypatch):
    store, runtime, worker, target, candidate = distill_batch
    candidates = [candidate, {**candidate, "body": "batch later", "effective_at": "2026-03-01T00:00:00Z"}]
    monkeypatch.setattr(runtime, "_generate_json", lambda *_args, **_kwargs: {"candidates": candidates})
    apply = store.apply_candidate
    inserted_external = False

    def apply_with_external_write(event_id, item, model_id):
        nonlocal inserted_external
        result = apply(event_id, item, model_id)
        if not inserted_external:
            inserted_external = True
            store.declare({"body": "batch external", "memory_id": target["memory_id"],
                           "expected_revision": result["memory"]["revision_no"],
                           "effective_at": "2026-04-01T00:00:00Z"})
        return result

    monkeypatch.setattr(store, "apply_candidate", apply_with_external_write)
    result = worker.run_once()
    assert result["status"] == "failed"
    assert "revision conflict" in result["error"]
    current = store.get_memory(target["memory_id"])
    assert current["revision_no"] == 3
    assert current["body"] == "batch external"


def test_untargeted_add_does_not_rebase_snapshot(distill_batch, monkeypatch):
    store, runtime, worker, target, candidate = distill_batch
    added = {key: value for key, value in candidate.items() if key != "memory_id"}
    added.update({"action": "ADD", "body": "batch added", "effective_at": "2026-03-01T00:00:00Z"})

    def generate(*_args, **_kwargs):
        store.declare({"body": "batch external", "memory_id": target["memory_id"],
                       "expected_revision": 1, "effective_at": "2026-02-15T00:00:00Z"})
        return {"candidates": [added, {**candidate, "effective_at": "2026-04-01T00:00:00Z"}]}

    monkeypatch.setattr(runtime, "_generate_json", generate)
    result = worker.run_once()
    assert result["status"] == "failed"
    assert "revision conflict" in result["error"]
    current = store.get_memory(target["memory_id"])
    assert current["revision_no"] == 3
    assert current["body"] == added["body"]
