"""Generation bounds and incomplete output handling with synthetic content."""

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bmem.runtime import DISTILL_SCHEMA, Runtime, Worker, _normalize_candidates
from bmem.store import Store


def candidate(body="synthetic fact"):
    return {
        "body": body,
        "kind": "fact",
        "conditions": {},
        "confidence": 0.9,
        "derived_from": [],
        "action": "ADD",
        "fact_key": None,
        "memory_id": None,
    }


@pytest.fixture
def generation_endpoint(tmp_path, monkeypatch):
    state = {"requests": [], "outputs": []}

    class GenerationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(payload)
            output = state["outputs"].pop(0)
            encoded = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), GenerationHandler)
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


@pytest.mark.parametrize("mode", ["ordinary", "consolidation", "recompute"])
def test_candidate_schema_bounds_all_generation_modes(generation_endpoint, mode):
    runtime, state = generation_endpoint
    state["outputs"] = [{"response": '{"candidates":[]}', "done_reason": "stop"}]
    existing = [{"memory_id": "known-parent", "body": "synthetic context", "revision_no": 1}]
    if mode == "recompute":
        runtime.distill_recompute(existing[0], [{"event_id": "proof-1", "body": "synthetic proof"}])
    else:
        event = {"body": "synthetic fact", "source_type": "verification"}
        if mode == "consolidation":
            event["metadata"] = {"action": "consolidate", "source_memory_ids": ["known-parent"]}
        runtime.distill(event, existing)
    schema = state["requests"][0]["format"]
    candidates = schema["properties"]["candidates"]
    fields = candidates["items"]["properties"]
    assert candidates["maxItems"] == (1 if mode == "recompute" else 3)
    assert fields["body"]["maxLength"] == 600
    assert fields["fact_key"]["maxLength"] == 512
    assert fields["derived_from"]["maxItems"] == 1
    assert schema["properties"]["has_more"] == {"type": "boolean"}
    assert "has_more" in state["requests"][0]["prompt"]


@pytest.mark.parametrize("closed_json", [False, True])
def test_length_retries_without_accepting_a_partial_fact_set(generation_endpoint, closed_json):
    runtime, state = generation_endpoint
    first = json.dumps({"candidates": [candidate("first fact")]}) if closed_json else '{"candidates":['
    complete = [candidate("first fact"), candidate("tail fact cobalt-73")]
    state["outputs"] = [
        {"response": first, "done_reason": "length"},
        {"response": json.dumps({"candidates": complete}), "done_reason": "stop"},
    ]
    result = runtime._generate_candidates("Synthetic input has first fact and tail fact cobalt-73")
    assert [item["body"] for item in result] == ["first fact", "tail fact cobalt-73"]
    assert [request["options"]["num_predict"] for request in state["requests"]] == [800, 3200]


@pytest.mark.parametrize("closed_json", [False, True])
def test_exhausted_length_keeps_job_failed_and_source_intact(generation_endpoint, closed_json):
    runtime, state = generation_endpoint
    raw = json.dumps({"candidates": [candidate("first fact")]}) if closed_json else '{"candidates":['
    state["outputs"] = [{"response": raw, "done_reason": "length"}] * 2
    store = Store(runtime.home)
    text = "Synthetic source includes first fact and tail fact cobalt-73"
    event = store.ingest({"body": text, "source_type": "verification"})
    result = Worker(store, runtime).run_once()
    assert result["status"] == "failed"
    assert "done_reason=length" in result["error"]
    assert "num_predict=3200" in result["error"]
    assert store.get_event(event["event_id"])["body"] == text
    assert store.get_event(event["event_id"])["has_evidence"] is True
    job = next(item for item in store.records("jobs") if item["job_id"] == event["job_id"])
    assert job["status"] == "failed"
    assert store.stats()["memories"] == 0
    assert [request["options"]["num_predict"] for request in state["requests"]] == [800, 3200]


@pytest.mark.parametrize("overflow", ["four_candidates", "has_more"])
def test_overflow_does_not_silently_commit_only_first_facts(generation_endpoint, overflow):
    runtime, state = generation_endpoint
    items = [candidate(f"synthetic fact {index}") for index in range(4)]
    output = {"candidates": items} if overflow == "four_candidates" else {"candidates": items[:3], "has_more": True}
    state["outputs"] = [{"response": json.dumps(output), "done_reason": "stop"}]
    store = Store(runtime.home)
    text = "Synthetic source facts 0, 1, 2 and tail fact 3"
    event = store.ingest({"body": text, "source_type": "verification"})
    result = Worker(store, runtime).run_once()
    assert result["status"] == "failed"
    assert store.stats()["memories"] == 0
    assert store.get_event(event["event_id"])["body"] == text
    assert store.get_event(event["event_id"])["has_evidence"] is True
    job = next(item for item in store.records("jobs") if item["job_id"] == event["job_id"])
    assert job["status"] == "failed"


def test_complete_output_at_bounds_commits_all_facts(generation_endpoint):
    runtime, state = generation_endpoint
    items = [
        {**candidate("文" * 600), "fact_key": "k" * 512},
        candidate("second synthetic fact"),
        candidate("tail synthetic fact"),
    ]
    state["outputs"] = [{
        "response": json.dumps({"candidates": items, "has_more": False}),
        "done_reason": "stop",
    }]
    store = Store(runtime.home)
    store.ingest({"body": "Synthetic source has three facts", "source_type": "verification"})

    result = Worker(store, runtime).run_once()

    assert result["status"] == "done", result
    assert {item["body"] for item in store.list_memories()} == {item["body"] for item in items}
    assert any(item["fact_key"] == "k" * 512 for item in store.list_memories())


@pytest.mark.parametrize("field,value", [("body", "文" * 601), ("fact_key", "key" * 171)])
def test_model_text_bounds_reject_instead_of_clipping(generation_endpoint, field, value):
    runtime, state = generation_endpoint
    item = {**candidate(), field: value}
    state["outputs"] = [{"response": json.dumps({"candidates": [item]}), "done_reason": "stop"}]
    with pytest.raises(ValueError, match="limit"):
        runtime._generate_candidates("Synthetic field overflow")


def test_recompute_rejects_multiple_distinct_results(generation_endpoint):
    runtime, state = generation_endpoint
    items = [{**candidate(body), "action": "UPDATE", "memory_id": "target"} for body in ("one", "two")]
    state["outputs"] = [{"response": json.dumps({"candidates": items}), "done_reason": "stop"}]
    with pytest.raises(ValueError, match="limit"):
        runtime.distill_recompute({"memory_id": "target", "revision_no": 1}, [{"event_id": "proof-1", "body": "proof"}])


@pytest.mark.parametrize("runtime_override", [False, True])
def test_recompute_overflow_preserves_target_and_evidence(
    generation_endpoint, monkeypatch, runtime_override
):
    runtime, state = generation_endpoint
    store = Store(runtime.home)
    parent = store.declare({"body": "parent version one", "fact_key": "parent"})["memory"]
    child = store.declare({
        "body": "supported child fact",
        "fact_key": "child",
        "derived_from": [parent["memory_id"]],
    })["memory"]
    proof = store.ingest({"body": child["body"], "source_type": "verification"})
    store.apply_candidate(proof["event_id"], {
        **candidate(child["body"]),
        "memory_id": child["memory_id"],
        "action": "UPDATE",
        "expected_revision": child["revision_no"],
    }, "synthetic-model")
    store.finish_job(proof["job_id"])
    store.declare({
        "body": "parent version two",
        "memory_id": parent["memory_id"],
        "expected_revision": parent["revision_no"],
    })
    before = store.get_memory(child["memory_id"])
    items = [
        {**candidate(body), "memory_id": child["memory_id"], "action": "UPDATE"}
        for body in ("replacement one", "replacement two")
    ]
    if runtime_override:
        monkeypatch.setattr(runtime, "distill_recompute", lambda *_args: items)
    else:
        state["outputs"] = [{
            "response": json.dumps({"candidates": items}),
            "done_reason": "stop",
        }]
    worker = Worker(store, runtime)
    for _ in range(10):
        result = worker.run_once()
        if result["status"] in {"failed", "idle"}:
            break

    assert result["status"] == "failed", result
    assert "limit" in result["error"]
    after = store.get_memory(child["memory_id"])
    assert (after["body"], after["revision_no"]) == (before["body"], before["revision_no"])
    assert store.get_event(proof["event_id"])["has_evidence"] is True


def test_legacy_envelopes_and_duplicate_facts_stay_usable():
    item = candidate()
    assert len(_normalize_candidates([deepcopy(item)] * 4)) == 1
    assert _normalize_candidates({"candidates": []}) == []
    with pytest.raises(ValueError, match="has_more"):
        _normalize_candidates({"candidates": [], "has_more": "false"})


def test_short_durability_gate_keeps_its_existing_budget(generation_endpoint):
    runtime, state = generation_endpoint
    state["outputs"] = [{"response": '{"durable":true}', "done_reason": "length"}]
    assert runtime._has_durable_statement("Synthetic durable fact") is True
    assert [request["options"]["num_predict"] for request in state["requests"]] == [64]


def test_schema_constant_is_not_mutated_by_dynamic_context(generation_endpoint):
    runtime, state = generation_endpoint
    before = deepcopy(DISTILL_SCHEMA)
    state["outputs"] = [{"response": '{"candidates":[]}', "done_reason": "stop"}]
    runtime.distill_recompute({"memory_id": "target", "revision_no": 1}, [])
    assert DISTILL_SCHEMA == before
