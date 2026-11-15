"""Real Store/Worker transaction and retry boundaries with synthetic candidates."""

from copy import deepcopy
import json

import pytest

from bmem.models import CandidateInput, DomainError
from bmem.runtime import Worker
from bmem.store import Store


class CandidateStub:
    model_id = "synthetic-transaction-boundary"

    def __init__(self, home, candidates):
        self.home = home
        self.candidates = candidates
        self.calls = 0
        self.error = None

    def distill(self, *_args):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return deepcopy(self.candidates)


def candidate(body, **fields):
    return {
        "body": body, "kind": "fact", "action": "ADD", "conditions": {},
        "confidence": 0.9, "derived_from": [], **fields,
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    authority = Store(tmp_path / "memory")
    authority.set_settings({"semantic_enabled": True})
    authority.maintain()
    finish_existing_jobs(authority)
    return authority


def finish_existing_jobs(store):
    for item in store.records("jobs"):
        store.finish_job(item["job_id"])


def seed_target(store):
    target = store.declare({
        "body": "synthetic original", "fact_key": "synthetic.key",
        "conditions": {"env": "prod"},
        "occurred_at": "2026-01-01T00:00:00Z",
        "effective_at": "2026-01-01T00:00:00Z",
    })["memory"]
    finish_existing_jobs(store)
    return target


def make_worker(store, runtime):
    worker = Worker(store, runtime)
    worker._mark_maintenance()
    return worker


def read_job(store, job_id):
    with store._read() as conn:
        result = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    result["payload"] = json.loads(result["payload"])
    return result


def distill_job_id(store, event_id):
    with store._read() as conn:
        return conn.execute(
            "SELECT job_id FROM jobs WHERE event_id=? AND kind='distill'", (event_id,)
        ).fetchone()[0]


def make_due(store, job_id):
    with store._write() as conn:
        conn.execute("UPDATE jobs SET available_at=? WHERE job_id=?",
                     ("2000-01-01T00:00:00Z", job_id))


def authority_snapshot(store, job_id):
    with store._read() as conn:
        snapshot = {
            name: [tuple(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in ("events", "memories", "revisions", "sources", "arbitration")
        }
        snapshot["fts"] = [tuple(row) for row in conn.execute(
            "SELECT * FROM memory_fts ORDER BY memory_id")]
        snapshot["outbox"] = [tuple(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE job_id!=? ORDER BY rowid", (job_id,))]
    return snapshot


def effects(store, event_id):
    with store._read() as conn:
        return {
            "memories": conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM sources WHERE event_id=?", (event_id,)
            ).fetchone()[0],
            "sources": conn.execute("SELECT COUNT(*) FROM sources WHERE event_id=?", (event_id,)).fetchone()[0],
            "revisions": conn.execute(
                "SELECT COUNT(*) FROM revisions WHERE source_event_id=?", (event_id,)
            ).fetchone()[0],
        }


def test_later_conditions_failure_rolls_back_candidates_audit_outbox_and_fts(store):
    target = seed_target(store)
    event = store.ingest({"body": "synthetic observation", "source_type": "verification"})
    runtime = CandidateStub(store.home, [
        candidate("synthetic independent prefix"),
        candidate("synthetic invalid update", action="UPDATE", memory_id=target["memory_id"],
                  conditions={"env": "stage"}),
    ])
    worker = make_worker(store, runtime)
    before = authority_snapshot(store, event["job_id"])

    for expected_calls in (1, 2):
        result = worker.run_once()
        assert result["status"] == "failed", result
        assert "conditions" in result["error"]
        assert runtime.calls == expected_calls
        assert authority_snapshot(store, event["job_id"]) == before
        assert read_job(store, event["job_id"])["status"] == "failed"
        make_due(store, event["job_id"])


def test_later_cas_failure_rolls_back_entire_store_batch(store):
    target = seed_target(store)
    event = store.ingest({"body": "synthetic CAS observation", "source_type": "verification"})
    claimed = store.claim_job()
    assert claimed["job_id"] == event["job_id"]
    before = authority_snapshot(store, event["job_id"])

    with pytest.raises(DomainError, match="revision conflict"):
        store.apply_distill_batch(event["job_id"], event["event_id"], [
            candidate("synthetic CAS prefix"),
            candidate("synthetic stale update", action="UPDATE", memory_id=target["memory_id"],
                      expected_revision=0, conditions={"env": "prod"}),
        ], CandidateStub.model_id)

    assert authority_snapshot(store, event["job_id"]) == before
    assert read_job(store, event["job_id"])["status"] == "running"


def test_finish_marker_failure_rolls_back_candidates_and_outbox(store, monkeypatch):
    event = store.ingest({"body": "synthetic marker observation", "source_type": "verification"})
    runtime = CandidateStub(store.home, [candidate("synthetic marker claim")])
    worker = make_worker(store, runtime)
    before = authority_snapshot(store, event["job_id"])
    seen = []

    def fail_finish(conn, job_id):
        assert conn.in_transaction
        assert job_id == event["job_id"]
        seen.append(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        raise OSError("synthetic completion marker failure")

    monkeypatch.setattr(store, "_finish_job", fail_finish)
    result = worker.run_once()

    assert result["status"] == "failed", result
    assert "completion marker failure" in result["error"]
    assert seen == [1]
    assert runtime.calls == 1
    assert authority_snapshot(store, event["job_id"]) == before
    assert read_job(store, event["job_id"])["status"] == "failed"


def test_exit_after_batch_commit_recovery_does_not_replay(store, monkeypatch):
    store.set_settings({"semantic_enabled": False})
    event = store.ingest({"body": "synthetic committed observation", "source_type": "verification"})
    runtime = CandidateStub(store.home, [candidate("synthetic committed claim")])
    worker = make_worker(store, runtime)
    apply_batch = store.apply_distill_batch

    def exit_after_commit(*args, **kwargs):
        apply_batch(*args, **kwargs)
        assert read_job(store, event["job_id"])["status"] == "done"
        raise SystemExit("synthetic process interruption after commit")

    monkeypatch.setattr(store, "apply_distill_batch", exit_after_commit)
    with pytest.raises(SystemExit, match="after commit"):
        worker.run_once()

    assert runtime.calls == 1
    committed = authority_snapshot(store, event["job_id"])
    reopened = Store(store.home)
    reopened.recover_jobs()
    retry_runtime = CandidateStub(reopened.home, [candidate("synthetic duplicate must not run")])
    result = make_worker(reopened, retry_runtime).run_once()
    assert result["status"] == "idle", result
    assert retry_runtime.calls == 0
    assert read_job(reopened, event["job_id"])["status"] == "done"
    assert authority_snapshot(reopened, event["job_id"]) == committed
    assert effects(reopened, event["event_id"]) == {"memories": 1, "sources": 1, "revisions": 1}


@pytest.mark.parametrize("legacy_kind", ["unkeyed_add", "lower_trust_split"])
def test_legacy_partial_retry_is_held_before_generation(store, legacy_kind):
    if legacy_kind == "lower_trust_split":
        seed_target(store)
    event = store.ingest({"body": "synthetic legacy observation", "source_type": "user_dialogue"})
    claimed = store.claim_job()
    assert claimed["job_id"] == event["job_id"]
    item = candidate("synthetic unkeyed partial claim")
    if legacy_kind == "lower_trust_split":
        item = candidate("synthetic contrary partial claim", fact_key="synthetic.key",
                         conditions={"env": "prod"})
    applied = store.apply_candidate(event["event_id"], item, CandidateStub.model_id)
    if legacy_kind == "lower_trust_split":
        assert applied["reason"] == "lower_trust_retained"
    original_error = "synthetic legacy failure after one committed candidate"
    store.fail_job(event["job_id"], original_error)
    make_due(store, event["job_id"])
    # Settle the existing split's promotion before measuring retry effects.
    store.promote_due()
    before = authority_snapshot(store, event["job_id"])
    runtime = CandidateStub(store.home, [item])

    result = make_worker(store, runtime).run_once()

    assert runtime.calls == 0
    assert result["status"] == "failed", result
    assert result["error"] == original_error
    held = read_job(store, event["job_id"])
    assert held["status"] == "failed"
    assert held["last_error"] == original_error
    assert held["payload"]["distill_review_required"] is True
    assert authority_snapshot(store, event["job_id"]) == before


def test_targeted_updates_share_transaction_and_carry_revision(store, monkeypatch):
    target = seed_target(store)
    event = store.ingest({"body": "synthetic observation", "source_type": "verification",
                          "occurred_at": "2026-04-01T00:00:00Z"})
    runtime = CandidateStub(store.home, [
        candidate("synthetic February", action="UPDATE", memory_id=target["memory_id"],
                  conditions={"env": "prod"}, effective_at="2026-02-01T00:00:00Z"),
        candidate("synthetic March", action="UPDATE", memory_id=target["memory_id"],
                  conditions={"env": "prod"}, effective_at="2026-03-01T00:00:00Z"),
    ])
    apply = store._apply_candidate
    seen = []

    def observe(conn, event_id, item, model_id, *, recompute=False):
        assert conn.in_transaction
        assert isinstance(item, CandidateInput)
        seen.append((conn, item.expected_revision))
        return apply(conn, event_id, item, model_id, recompute=recompute)

    monkeypatch.setattr(store, "_apply_candidate", observe)
    result = make_worker(store, runtime).run_once()

    assert result["status"] == "done", result
    assert len(seen) == 2 and seen[0][0] is seen[1][0]
    assert [revision for _, revision in seen] == [1, 2]
    current = store.get_memory(target["memory_id"])
    assert current["revision_no"] == 3
    assert current["body"] == "synthetic March"
    assert read_job(store, event["job_id"])["status"] == "done"
    assert effects(store, event["event_id"]) == {"memories": 1, "sources": 1, "revisions": 2}


def test_public_candidate_stable_key_compatibility(store):
    event = store.ingest({"body": "synthetic stable observation", "source_type": "verification"})
    item = candidate("synthetic stable claim", fact_key="synthetic.stable")
    first = store.apply_candidate(event["event_id"], item, CandidateStub.model_id)
    second = store.apply_candidate(event["event_id"], item, CandidateStub.model_id)
    assert first["memory"]["memory_id"] == second["memory"]["memory_id"]
    assert second["reason"] == "duplicate_claim"
    assert effects(store, event["event_id"]) == {"memories": 1, "sources": 1, "revisions": 1}


def test_structured_t1_skips_candidate_generation(store):
    store.set_settings({"semantic_enabled": False})
    declaration = store.declare({"body": "synthetic structured declaration", "fact_key": "synthetic.structured"})
    job_id = distill_job_id(store, declaration["event_id"])
    runtime = CandidateStub(store.home, [candidate("synthetic unexpected claim")])
    result = make_worker(store, runtime).run_once()
    assert result["status"] == "done", result
    assert runtime.calls == 0
    assert read_job(store, job_id)["status"] == "done"
    assert store.stats()["memories"] == 1


def test_free_text_t1_retry_reconciles_policy_immediate_record(store):
    target = seed_target(store)
    store.set_settings({"semantic_enabled": False})
    declaration = store.declare({"body": "synthetic original now corrected",
                                 "occurred_at": "2026-04-01T00:00:00Z"})
    job_id = distill_job_id(store, declaration["event_id"])
    immediate = store.get_memory(declaration["memory"]["memory_id"])
    assert immediate["model_id"].startswith("policy:t1")
    runtime = CandidateStub(store.home, [
        candidate("synthetic corrected", action="UPDATE", memory_id=target["memory_id"],
                  conditions={"env": "prod"}, effective_at="2026-03-01T00:00:00Z"),
    ])
    runtime.error = OSError("synthetic model boundary unavailable")
    worker = make_worker(store, runtime)
    assert worker.run_once()["status"] == "failed"
    make_due(store, job_id)
    runtime.error = None

    result = worker.run_once()

    assert result["status"] == "done", result
    assert runtime.calls == 2
    assert not read_job(store, job_id)["payload"].get("distill_review_required")
    current = store.get_memory(target["memory_id"])
    assert current["revision_no"] == 2
    assert current["body"] == "synthetic corrected"


def test_policy_declaration_retry_survives_another_events_model_update(store):
    store.set_settings({"semantic_enabled": False})
    declaration = store.declare({
        "body": "synthetic policy declaration", "occurred_at": "2026-04-01T00:00:00Z",
    })
    event_id = declaration["event_id"]
    job_id = distill_job_id(store, event_id)
    memory_id = declaration["memory"]["memory_id"]
    assert read_job(store, job_id)["payload"]["reconcile"] is True
    runtime = CandidateStub(store.home, [])
    runtime.error = OSError("synthetic first declaration distillation failure")
    worker = make_worker(store, runtime)
    assert worker.run_once()["status"] == "failed"
    assert runtime.calls == 1

    later = store.ingest({
        "body": "synthetic later verification", "source_type": "verification",
        "occurred_at": "2026-05-01T00:00:00Z",
    })
    updated = store.apply_candidate(later["event_id"], candidate(
        "synthetic updated by another event", action="UPDATE", memory_id=memory_id,
        expected_revision=1, effective_at="2026-05-01T00:00:00Z",
    ), CandidateStub.model_id)
    assert updated["memory"]["revision_no"] == 2
    assert updated["memory"]["model_id"] == CandidateStub.model_id
    store.finish_job(later["job_id"])
    with store._read() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE source_event_id=? AND model_id NOT LIKE 'policy:%'",
            (event_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM arbitration a,json_each(a.evidence_ids) e "
            "WHERE e.value=? AND a.model_id NOT LIKE 'policy:%'", (event_id,),
        ).fetchone()[0] == 0
    make_due(store, job_id)
    runtime.error = None
    before = authority_snapshot(store, job_id)

    result = worker.run_once()

    assert runtime.calls == 2
    assert result["status"] == "done", result
    completed = read_job(store, job_id)
    assert completed["status"] == "done"
    assert not completed["payload"].get("distill_review_required")
    assert authority_snapshot(store, job_id) == before


def test_audit_only_legacy_noop_is_held_before_generation(store):
    target = seed_target(store)
    store.set_settings({"semantic_enabled": False})
    event = store.ingest({
        "body": "synthetic contrary no-change observation", "source_type": "verification",
        "occurred_at": "2026-04-01T00:00:00Z",
    })
    assert store.claim_job()["job_id"] == event["job_id"]
    applied = store.apply_candidate(event["event_id"], candidate(
        "synthetic contrary no-change claim", action="NOOP", memory_id=target["memory_id"],
        expected_revision=1, conditions={"env": "prod"}, fact_key="synthetic.key",
    ), CandidateStub.model_id)
    assert applied["reason"] == "contrary_noop"
    assert effects(store, event["event_id"]) == {"memories": 0, "sources": 0, "revisions": 0}
    with store._read() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM arbitration a,json_each(a.evidence_ids) e "
            "WHERE e.value=? AND a.model_id=?", (event["event_id"], CandidateStub.model_id),
        ).fetchone()[0] == 1
    original_error = "synthetic failure after model audit only"
    store.fail_job(event["job_id"], original_error)
    make_due(store, event["job_id"])
    before = authority_snapshot(store, event["job_id"])
    runtime = CandidateStub(store.home, [candidate("synthetic unexpected retry claim")])

    result = make_worker(store, runtime).run_once()

    assert runtime.calls == 0
    assert result["status"] == "failed", result
    assert result["error"] == original_error
    held = read_job(store, event["job_id"])
    assert held["status"] == "failed"
    assert held["last_error"] == original_error
    assert held["payload"]["distill_review_required"] is True
    assert authority_snapshot(store, event["job_id"]) == before
