"""Fact validity time stays separate from message time and arrival order."""

import pytest
from fastapi.testclient import TestClient

from bmem.api import create_app
from bmem.models import DomainError
from bmem.runtime import Runtime, Worker
from bmem.store import Store


@pytest.fixture
def fact_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    with TestClient(create_app(tmp_path, start_worker=False), base_url="http://127.0.0.1") as client:
        yield client, Store(tmp_path)


@pytest.fixture
def disputed_port(fact_authority):
    client, store = fact_authority
    declarations = [store.declare({
        "body": f"port {port}", "fact_key": "service.port",
        "effective_at": "2026-05-01T00:00:00Z",
    }) for port in (1, 2)]
    memories = [store.get_memory(item["memory"]["memory_id"]) for item in declarations]
    assert all(memory["status"] == "disputed" for memory in memories)
    return client, store, memories


@pytest.mark.parametrize("explicit_target", [False, True])
def test_new_fact_replaces_by_effective_time_without_changing_event_time(fact_authority, explicit_target):
    client, store = fact_authority
    original = client.post("/api/declarations", json={
        "body": "服务端口8790", "fact_key": "service.port",
        "occurred_at": "2026-05-01T00:00:00Z", "effective_at": "2026-01-01T00:00:00Z",
    }).json()["memory"]
    fields = {"memory_id": original["memory_id"], "expected_revision": 1} if explicit_target else {}

    response = client.post("/api/declarations", json={
        "body": "服务端口8791", "fact_key": "service.port", **fields,
        "occurred_at": "2026-04-01T00:00:00Z", "effective_at": "2026-02-01T00:00:00Z",
    })

    assert response.status_code == 200, response.text
    memory = response.json()["memory"]
    assert memory["memory_id"] == original["memory_id"]
    assert memory["body"] == "服务端口8791"
    assert memory["effective_at"].startswith("2026-02-01T")
    assert [revision["effective_at"][:10] for revision in memory["revisions"]] == ["2026-01-01", "2026-02-01"]
    event = store.get_event(response.json()["event_id"])
    assert event["occurred_at"].startswith("2026-04-01T")
    assert event["effective_at"].startswith("2026-02-01T")
    assert store.search("8791")[0]["memory_id"] == original["memory_id"]


@pytest.mark.parametrize("entry", ["declaration_key", "declaration_id", "candidate_key", "candidate_id"])
def test_late_old_fact_keeps_current_claim_and_does_not_add_false_support(fact_authority, entry):
    client, store = fact_authority
    original = store.declare({
        "body": "服务端口8791", "fact_key": "service.port",
        "occurred_at": "2026-01-01T00:00:00Z", "effective_at": "2026-05-01T00:00:00Z",
    })["memory"]
    fields = {"memory_id": original["memory_id"], "expected_revision": 1} if entry.endswith("_id") else {}
    if entry.startswith("declaration"):
        response = client.post("/api/declarations", json={
            "body": "服务端口8790", "fact_key": "service.port", **fields,
            "occurred_at": "2026-06-01T00:00:00Z", "effective_at": "2026-04-01T00:00:00Z",
        })
        assert response.status_code == 200, response.text
        event_id = response.json()["event_id"]
    else:
        event_id = store.ingest({"body": "旧配置晚到", "source_type": "verification",
                                 "occurred_at": "2026-06-01T00:00:00Z"})["event_id"]
        store.apply_candidate(event_id, {
            "body": "服务端口8790", "kind": "fact", "fact_key": "service.port",
            "action": "UPDATE", "effective_at": "2026-04-01T00:00:00Z", **fields,
        }, "fixture")

    current = store.get_memory(original["memory_id"])
    assert current["body"] == original["body"]
    assert current["revision_no"] == 1
    assert current["effective_at"].startswith("2026-05-01T")
    assert current["evidence_count"] == original["evidence_count"]
    assert event_id in current["decisions"][-1]["evidence_ids"]
    assert store.get_event(event_id)["body"]


@pytest.mark.parametrize("entry", ["declaration", "candidate"])
def test_same_value_confirmation_advances_watermark(fact_authority, entry):
    _, store = fact_authority
    original = store.declare({"body": "服务端口8791", "fact_key": "service.port",
                               "effective_at": "2026-03-01T00:00:00Z"})["memory"]
    if entry == "declaration":
        confirmation = store.declare({"body": original["body"], "fact_key": "service.port",
                                       "effective_at": "2026-05-01T00:00:00Z"})
    else:
        confirmation = store.ingest({"body": "确认当前端口", "source_type": "verification"})
        store.apply_candidate(confirmation["event_id"], {
            "body": original["body"], "kind": "fact", "fact_key": "service.port",
            "action": "ADD", "effective_at": "2026-05-01T00:00:00Z",
        }, "fixture")
    store.declare({"body": "服务端口8790", "fact_key": "service.port",
                   "effective_at": "2026-04-01T00:00:00Z"})

    current = store.get_memory(original["memory_id"])
    assert current["body"] == original["body"]
    assert current["effective_at"].startswith("2026-05-01T")
    assert current["revision_no"] == 1
    assert current["revisions"][0]["effective_at"] == current["effective_at"]
    assert confirmation["event_id"] in {source["event_id"] for source in current["sources"]}


@pytest.mark.parametrize("action", ["ADD", "UPDATE", "DISPUTE"])
def test_verified_newer_candidate_reconciles_current_fact(fact_authority, action):
    _, store = fact_authority
    original = store.declare({"body": "服务端口8790", "fact_key": "service.port",
                               "effective_at": "2026-01-01T00:00:00Z"})["memory"]
    event = store.ingest({"body": "验证新端口", "source_type": "verification"})
    result = store.apply_candidate(event["event_id"], {
        "body": "服务端口8791", "kind": "fact", "fact_key": "service.port", "action": action,
        "effective_at": "2026-02-01T00:00:00Z", "confidence": 0.95,
    }, "fixture")
    assert result["action"] == "UPDATE"
    current = store.get_memory(original["memory_id"])
    assert current["body"] == "服务端口8791"
    assert current["status"] == "active"
    assert current["effective_at"].startswith("2026-02-01T")


def test_low_trust_confirmation_cannot_move_user_fact_watermark(fact_authority):
    _, store = fact_authority
    original = store.declare({"body": "明确事实", "fact_key": "protected.fact",
                               "effective_at": "2026-03-01T00:00:00Z"})["memory"]
    event = store.ingest({"body": "助手推测", "source_type": "agent_inference"})
    for body in (original["body"], "相反推测"):
        store.apply_candidate(event["event_id"], {
            "body": body, "kind": "fact", "fact_key": "protected.fact", "action": "UPDATE",
            "memory_id": original["memory_id"], "expected_revision": 1,
            "effective_at": "2026-06-01T00:00:00Z", "confidence": 0.2,
        }, "fixture")
    current = store.get_memory(original["memory_id"])
    assert current["body"] == original["body"]
    assert current["effective_at"] == original["effective_at"]
    assert current["status"] == "active"
    assert any(memory["body"] == "相反推测" for memory in store.list_memories(status="proposed"))


def test_conditions_stay_independent_and_candidate_cannot_cross_target_scope(fact_authority):
    _, store = fact_authority
    memories = [store.declare({"body": name, "fact_key": "service.port", "conditions": {"env": name},
                               "effective_at": "2026-03-01T00:00:00Z"})["memory"] for name in ("dev", "prod")]
    store.declare({"body": "prod新版", "fact_key": "service.port", "conditions": {"env": "prod"},
                   "effective_at": "2026-04-01T00:00:00Z"})
    assert store.get_memory(memories[0]["memory_id"])["body"] == "dev"
    assert store.get_memory(memories[1]["memory_id"])["body"] == "prod新版"
    event = store.ingest({"body": "模型指定了另一环境", "source_type": "verification"})
    with pytest.raises(DomainError, match="conditions"):
        store.apply_candidate(event["event_id"], {
            "body": "错分支", "kind": "fact", "action": "UPDATE", "conditions": {"env": "prod"},
            "memory_id": memories[0]["memory_id"], "expected_revision": 1,
        }, "fixture")


@pytest.mark.parametrize("occurred_at", [None, "2026-01-01T00:00:00Z"])
def test_unspecified_fact_time_uses_original_event_time(fact_authority, occurred_at):
    _, store = fact_authority
    fields = {"occurred_at": occurred_at} if occurred_at else {}
    result = store.declare({"body": "没有明确生效日期", **fields})
    assert result["memory"]["effective_at"] == store.get_event(result["event_id"])["occurred_at"]


def test_worker_preserves_model_fact_time_through_real_runtime(fact_authority, monkeypatch):
    _, store = fact_authority
    original = store.declare({"body": "服务端口8790", "fact_key": "service.port",
                               "effective_at": "2026-01-01T00:00:00Z"})["memory"]
    for job in store.records("jobs"):
        store.finish_job(job["job_id"])
    event = store.ingest({"body": "服务端口8791于三月生效", "source_type": "verification",
                          "occurred_at": "2026-05-01T00:00:00Z"})
    runtime = Runtime(store.home)

    def model_response(prompt, schema, **_kwargs):
        assert "effective_at" in schema["properties"]["candidates"]["items"]["properties"]
        return {"candidates": [{
            "body": "服务端口8791", "kind": "fact", "fact_key": "service.port", "conditions": {},
            "action": "UPDATE", "memory_id": original["memory_id"], "derived_from": [],
            "confidence": 0.95, "effective_at": "2026-03-01T00:00:00Z",
        }]}

    monkeypatch.setattr(runtime, "_generate_json", model_response)
    result = Worker(store, runtime).run_once()
    assert result["status"] == "done", result
    current = store.get_memory(original["memory_id"])
    assert current["body"] == "服务端口8791"
    assert current["effective_at"].startswith("2026-03-01T")
    assert store.get_event(event["event_id"])["occurred_at"].startswith("2026-05-01T")


def test_legacy_columns_and_backup_read_original_time(tmp_path):
    store = Store(tmp_path / "original")
    result = store.declare({"body": "旧结构中的事实", "occurred_at": "2026-01-01T00:00:00Z"})
    with store._write() as conn:
        for table in ("events", "memories", "revisions"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN effective_at")
        conn.execute("PRAGMA user_version=1")
    archive = store.backup(tmp_path / "legacy.tar.gz")
    reopened = Store(store.home)
    memory = reopened.get_memory(result["memory"]["memory_id"])
    assert memory["effective_at"].startswith("2026-01-01T")
    assert memory["revisions"][0]["effective_at"] == memory["effective_at"]
    restored = Store(tmp_path / "restored")
    restored.restore(archive)
    assert restored.schema_version == 2
    assert restored.get_memory(memory["memory_id"])["effective_at"] == memory["effective_at"]
    assert restored.get_event(result["event_id"])["has_evidence"] is True


def test_pending_inference_does_not_redirect_later_user_declaration(fact_authority):
    _, store = fact_authority
    original = store.declare({"body": "明确旧事实", "fact_key": "subject.fact",
                               "effective_at": "2026-03-01T00:00:00Z"})["memory"]
    event = store.ingest({"body": "推测", "source_type": "agent_inference"})
    store.apply_candidate(event["event_id"], {
        "body": "未经确认的推测", "kind": "fact", "fact_key": "subject.fact", "action": "ADD",
        "effective_at": "2026-06-01T00:00:00Z", "confidence": 0.2,
    }, "fixture")
    updated = store.declare({"body": "明确新事实", "fact_key": "subject.fact",
                              "effective_at": "2026-05-01T00:00:00Z"})["memory"]
    assert updated["memory_id"] == original["memory_id"]
    assert {item["memory_id"] for item in store.list_memories(status="active")} == {original["memory_id"]}


def test_invalid_fact_time_rejected_before_capture(fact_authority):
    client, store = fact_authority
    before = store.stats()
    response = client.post("/api/declarations", json={"body": "无效时间", "effective_at": "not-a-date"})
    assert response.status_code == 422
    assert store.stats()["events"] == before["events"]


@pytest.mark.parametrize("entry", ["declaration_key", "candidate_key", "candidate_id"])
def test_equal_fact_time_keeps_unresolved_claims(fact_authority, entry):
    _, store = fact_authority
    original = store.declare({"body": "端口8790", "fact_key": "service.port",
                               "effective_at": "2026-05-01T00:00:00Z"})["memory"]
    if entry == "declaration_key":
        store.declare({"body": "端口8791", "fact_key": "service.port",
                       "effective_at": "2026-05-01T00:00:00Z"})
    else:
        event = store.ingest({"body": "同日另一配置", "source_type": "verification"})
        target = {"memory_id": original["memory_id"], "expected_revision": 1} if entry == "candidate_id" else {}
        store.apply_candidate(event["event_id"], {
            "body": "端口8791", "kind": "fact", "fact_key": "service.port", "action": "UPDATE",
            "effective_at": "2026-05-01T00:00:00Z", **target,
        }, "fixture")
    current = store.get_memory(original["memory_id"])
    assert current["body"] == original["body"]
    assert current["revision_no"] == 1
    assert current["status"] == "disputed"
    assert {item["body"] for item in store.list_memories(status="disputed")} == {"端口8790", "端口8791"}
    assert current["evidence_count"] == 1


def test_explicit_user_revision_resolves_equal_time(fact_authority):
    _, store = fact_authority
    original = store.declare({"body": "端口8790", "fact_key": "service.port",
                               "effective_at": "2026-05-01T00:00:00Z"})["memory"]
    result = store.declare({
        "body": "端口8791", "memory_id": original["memory_id"], "expected_revision": 1,
        "effective_at": "2026-05-01T00:00:00Z",
    })["memory"]
    assert result["revision_no"] == 2
    assert result["status"] == "active"
    assert result["body"] == "端口8791"


def test_same_value_equal_time_can_upgrade_privacy(fact_authority):
    client, store = fact_authority
    declaration = {"body": "同值事实", "fact_key": "private.fact",
                   "effective_at": "2026-05-01T00:00:00Z"}
    original = store.declare(declaration)["memory"]
    result = store.declare({**declaration, "sensitivity": "private"})["memory"]
    assert result["memory_id"] == original["memory_id"]
    assert result["sensitivity"] == "private"
    assert result["status"] == "active"
    assert client.get(f"/api/memories/{result['memory_id']}?target=cloud").status_code == 404


@pytest.mark.parametrize("effective_at", [None, "2026-06-01T00:00:00Z"])
def test_recompute_uses_candidate_time_or_existing_watermark(fact_authority, effective_at):
    _, store = fact_authority
    original = store.declare({"body": "当前派生事实", "fact_key": "derived.fact",
                               "effective_at": "2026-01-01T00:00:00Z"})
    current = store.declare({"body": "当前派生事实", "fact_key": "derived.fact",
                              "effective_at": "2026-05-01T00:00:00Z"})["memory"]
    result = store.apply_candidate(original["event_id"], {
        "body": "重算后的派生事实", "kind": "fact", "action": "UPDATE",
        "memory_id": current["memory_id"], "expected_revision": current["revision_no"],
        "derived_from": [source["event_id"] for source in current["sources"]],
        "effective_at": effective_at,
    }, "fixture", recompute=True)["memory"]
    expected = "2026-06-01T" if effective_at else "2026-05-01T"
    assert result["effective_at"].startswith(expected)
    assert result["revisions"][-1]["effective_at"] == result["effective_at"]
    assert store.get_event(original["event_id"])["effective_at"].startswith("2026-01-01T")


@pytest.mark.parametrize("difference", ["effective_at", "conditions"])
def test_runtime_keeps_distinct_fact_times_and_scopes(fact_authority, monkeypatch, difference):
    _, store = fact_authority
    store.ingest({"body": "验证不同日期或环境的配置", "source_type": "verification"})
    candidate = {"body": "服务端口8791", "kind": "fact", "fact_key": "service.port",
                 "conditions": {}, "confidence": 0.95, "derived_from": [], "action": "ADD"}
    if difference == "effective_at":
        candidates = [{**candidate, "effective_at": date} for date in
                      ("2026-01-01T00:00:00Z", "2026-05-01T00:00:00Z")]
    else:
        candidates = [{**candidate, "conditions": {"env": env}} for env in ("dev", "prod")]
    runtime = Runtime(store.home)
    monkeypatch.setattr(runtime, "_generate_json", lambda *_args, **_kwargs: {"candidates": candidates})
    result = Worker(store, runtime).run_once()
    assert result["status"] == "done", result
    assert result["candidates"] == 2
    memories = store.list_memories()
    if difference == "effective_at":
        assert len(memories) == 1
        assert memories[0]["effective_at"].startswith("2026-05-01T")
    else:
        assert {memory["conditions"]["env"] for memory in memories} == {"dev", "prod"}


@pytest.mark.parametrize("entry", ["declaration_key", "declaration_id", "candidate_key", "candidate_id"])
def test_newer_fact_retires_resolved_disputes_from_recall(disputed_port, entry):
    client, store, old_memories = disputed_port
    target = {"memory_id": old_memories[0]["memory_id"], "expected_revision": 1} if entry.endswith("_id") else {}
    fields = {"body": "port 3", "fact_key": "service.port", "effective_at": "2026-06-01T00:00:00Z", **target}
    if entry.startswith("declaration"):
        response = client.post("/api/declarations", json=fields)
        assert response.status_code == 200, response.text
        result = response.json()
    else:
        event = store.ingest({"body": "验证 port 3", "source_type": "verification"})
        result = store.apply_candidate(event["event_id"], {**fields, "kind": "fact", "action": "UPDATE"}, "fixture")
    winner = result["memory"]
    assert winner["body"] == "port 3"
    assert winner["status"] == "active"
    assert {item["memory_id"] for item in store.search("port")} == {winner["memory_id"]}
    stale_vectors = [
        {"memory_id": item["memory_id"], "revision_no": item["revision_no"], "score": 1.0}
        for item in old_memories
    ]
    package = store.recall({"query": "port", "target": "local"}, semantic_hits=stale_vectors)
    assert {item["memory_id"] for item in package["items"]} == {winner["memory_id"]}
    retired = next(item for item in old_memories if item["memory_id"] != winner["memory_id"])
    history = store.get_memory(retired["memory_id"])
    assert history["status"] == "retracted"
    assert history["body"] == retired["body"]
    assert history["sources"] == retired["sources"]
    assert history["revisions"][0]["body"] == retired["body"]
    assert history["decisions"][-1]["reason"] == "resolved_by_newer_fact"
    assert set(history["decisions"][-1]["evidence_ids"]) == {result["event_id"], winner["memory_id"]}
    assert store.get_event(retired["sources"][0]["event_id"])["has_evidence"]


@pytest.mark.parametrize("entry", ["declaration", "candidate"])
def test_newer_trusted_confirmation_resolves_disputed_value(disputed_port, entry):
    _, store, memories = disputed_port
    store.set_settings({"semantic_enabled": True})
    chosen = memories[1]
    fields = {"body": chosen["body"], "memory_id": chosen["memory_id"],
              "expected_revision": 1, "effective_at": "2026-06-01T00:00:00Z"}
    if entry == "declaration":
        result = store.declare(fields)
    else:
        event = store.ingest({"body": "确认 port 2", "source_type": "verification"})
        result = store.apply_candidate(event["event_id"], {**fields, "kind": "fact", "action": "NOOP"}, "fixture")
    current = store.get_memory(chosen["memory_id"])
    assert result["memory"]["status"] == "active"
    assert current["revision_no"] == 1
    assert current["effective_at"].startswith("2026-06-01T")
    assert store.get_memory(memories[0]["memory_id"])["status"] == "retracted"
    assert {item["memory_id"] for item in store.search("port")} == {chosen["memory_id"]}
    assert any(job["kind"] == "embed" and job["payload"].get("memory_id") == chosen["memory_id"]
               for job in store.records("jobs"))


@pytest.mark.parametrize("entry", [
    "declaration_old", "candidate_old", "candidate_weak", "confirmation_old", "confirmation_weak",
])
def test_old_or_weak_input_does_not_retire_disputes(disputed_port, entry):
    _, store, memories = disputed_port
    fields = {
        "body": memories[1]["body"] if entry.startswith("confirmation") else "port 3",
        "fact_key": "service.port",
        "effective_at": "2026-04-01T00:00:00Z" if entry.endswith("old") else "2026-06-01T00:00:00Z",
    }
    if entry == "declaration_old":
        store.declare(fields)
    else:
        event = store.ingest({
            "body": "资料输入", "source_type": "agent_inference" if entry.endswith("weak") else "verification",
        })
        store.apply_candidate(event["event_id"], {
            **fields, "kind": "fact", "action": "UPDATE", "confidence": 0.2,
        }, "fixture")
    for original in memories:
        current = store.get_memory(original["memory_id"])
        assert current["status"] == "disputed"
        assert current["effective_at"] == original["effective_at"]
        assert all(item["reason"] != "resolved_by_newer_fact" for item in current["decisions"])


@pytest.mark.parametrize("entry", ["declaration", "candidate"])
def test_late_confirmation_cannot_borrow_existing_watermark(disputed_port, entry):
    _, store, memories = disputed_port
    winner = store.declare({
        "body": "port 3", "fact_key": "service.port", "effective_at": "2026-06-01T00:00:00Z",
    })["memory"]
    # Existing databases can contain disputes left over by the previous implementation.
    with store._write() as conn:
        store._set_memory_status(conn, memories[0]["memory_id"], "disputed")
    fields = {"body": winner["body"], "fact_key": "service.port", "effective_at": "2026-05-15T00:00:00Z"}
    if entry == "declaration":
        store.declare(fields)
    else:
        event = store.ingest({"body": "迟到的同值确认", "source_type": "verification"})
        store.apply_candidate(event["event_id"], {**fields, "kind": "fact", "action": "NOOP"}, "fixture")
    assert store.get_memory(memories[0]["memory_id"])["status"] == "disputed"
    assert store.get_memory(winner["memory_id"])["effective_at"] == winner["effective_at"]


def test_resolution_keeps_disputes_with_later_fact_time(disputed_port):
    _, store, memories = disputed_port
    future = [store.declare({"body": f"future port {port}", "fact_key": "service.port",
                             "effective_at": "2026-07-01T00:00:00Z"})["memory"] for port in (4, 5)]
    store.declare({"body": "port 3", "memory_id": memories[0]["memory_id"],
                   "expected_revision": 1, "effective_at": "2026-06-01T00:00:00Z"})
    assert all(store.get_memory(item["memory_id"])["status"] == "disputed" for item in future)


@pytest.mark.parametrize("scope", ["fact_key", "conditions"])
def test_dispute_resolution_keeps_other_fact_scopes(disputed_port, scope):
    _, store, memories = disputed_port
    fields = {"fact_key": "another.port"} if scope == "fact_key" else {
        "fact_key": "service.port", "conditions": {"env": "dev"},
    }
    others = [store.declare({
        "body": f"other {port}", "effective_at": "2026-05-01T00:00:00Z", **fields,
    })["memory"] for port in (4, 5)]
    store.declare({"body": "port 3", "fact_key": "service.port", "effective_at": "2026-06-01T00:00:00Z"})
    assert all(store.get_memory(item["memory_id"])["status"] == "disputed" for item in others)
    assert any(store.get_memory(item["memory_id"])["status"] == "retracted" for item in memories)


def test_explicit_equal_time_correction_retires_resolved_disputes(disputed_port):
    _, store, memories = disputed_port
    winner = store.declare({"body": "port 3", "memory_id": memories[1]["memory_id"],
                            "expected_revision": 1, "effective_at": "2026-05-01T00:00:00Z"})["memory"]
    assert winner["status"] == "active"
    assert store.get_memory(memories[0]["memory_id"])["status"] == "retracted"


@pytest.mark.parametrize("shared_root", [False, True])
def test_resolved_dispute_invalidates_old_branch_and_protects_winner(disputed_port, shared_root):
    _, store, memories = disputed_port
    if shared_root:
        event = store.ingest({"body": "共同来源的相反配置", "source_type": "verification"})
        fields = {"kind": "fact", "fact_key": "shared.port", "action": "ADD", "effective_at": "2026-05-01T00:00:00Z"}
        first = store.apply_candidate(event["event_id"], {**fields, "body": "shared port 1"}, "fixture")["memory"]
        conflict = store.apply_candidate(event["event_id"], {**fields, "body": "shared port 2"}, "fixture")
        memories = [first, store.get_memory(conflict["conflicting_memory_id"])]
    child = store.declare({"body": "旧分支派生", "fact_key": "derived.port",
                           "derived_from": [memories[0]["memory_id"]]})["memory"]
    winner = store.declare({"body": "port 3", "memory_id": memories[1]["memory_id"],
                            "expected_revision": 1, "effective_at": "2026-06-01T00:00:00Z"})["memory"]
    assert winner["status"] == "active"
    assert store.get_memory(memories[0]["memory_id"])["status"] == "retracted"
    assert store.get_memory(child["memory_id"])["status"] in ("proposed", "retracted")
    assert child["memory_id"] not in {item["memory_id"] for item in store.search("旧分支派生")}
    assert not any(job["payload"].get("recompute") and job["payload"].get("memory_id") == winner["memory_id"]
                   for job in store.records("jobs"))
