"""Replay compatibility variants against an actual old daemon on a disposable vault."""

import hashlib
import os
from pathlib import Path
import sqlite3
from time import monotonic, sleep


CASE_IDS = frozenset({"F03-16", "X01-15"})


def legacy_daemon_setup(case, binary):
    from baseline_probe import REPO, dimensions, isolated, lookup, post

    old = Path(os.environ.get("BMEM_REPLAY_BASE_BIN", REPO / "src/v2/bmem/target/release/bmem")).resolve()
    if not old.is_file() or old == binary:
        return {"legacy_binary": "unavailable"}, {}

    data = case["fixture"]
    compatibility = case["id"] == "X01-15"
    if compatibility:
        count = data["legacy"]["history_job"]["accepted"]
    else:
        count = data["legacy_job"]["stored_summary_count"]
    session = "frozen-legacy-journal"
    transcript = [{"timestamp": "2026-11-20T00:00:00Z", "type": "session_meta", "payload": {"id": session}}]
    transcript.extend({"timestamp": f"2026-11-20T00:00:{i:02}Z", "type": "event_msg",
                       "payload": {"type": "user_message", "turn_id": f"legacy-{i}", "message": f"历史回填条目 {i}"}}
                      for i in range(1, count + 1))

    candidate_env = os.environ["BMEM_V2_TEST_BIN"]
    os.environ["BMEM_V2_TEST_BIN"] = str(old)
    try:
        with isolated(transcript) as (service, owner):
            database = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            job = old_history(service, owner, count)
            receipt = old_receipt(service, owner, data["legacy"]) if compatibility else None
            old_binary = {"sha256": hashlib.sha256(old.read_bytes()).hexdigest(),
                          "history": job, "receipt": receipt}

            service.stop()
            service.binary = str(binary)
            service.start()
            owner, _ = service.web()
            history_status, history = owner.request(f"/v2/history/jobs/{job['id']}/results")
            actual = {"legacy": old_binary, "history": {"status": history_status, "body": history}}
            if history_status != 200:
                return actual, {}
            if not compatibility:
                raw, projected = backfill_preserving_capture(case, service, owner)
                raw["old"] = actual
                projected["old"] = {"mapping_coverage": history["mapping_coverage"],
                                    "summary": {"stored_count": history["job"]["accepted"]},
                                    "rows": history["rows"]}
                return raw, projected

            statement_status, statement = lookup(owner, {"valid_at": "2026-11-30T23:59:59Z"})
            delivery_status, ledger = owner.request("/v2/deliveries")
            actual["statement"] = {"status": statement_status, "body": statement}
            actual["delivery"] = {"status": delivery_status}
            if statement_status != 200 or delivery_status != 200:
                return {"upgrade": actual}, {}
            delivery = next(row for row in ledger["deliveries"] if row["delivery_id"] == receipt["delivery_id"])
            actual["delivery"]["body"] = delivery
            language = dimensions(statement)["dimensions"][data["legacy"]["statement"]["dimension"]]["value"]
            before = count_legacy_writes(database)
            with sqlite3.connect(database, uri=True) as db:
                epoch = db.execute("PRAGMA user_version").fetchone()[0]
            service.stop()
            service.binary = str(old)
            try:
                service.start()
            except AssertionError as exc:
                # The daemon must fail for the specific compatibility fence,
                # not merely for a missing dependency or environment failure.
                fence = next((line for line in str(exc).splitlines() if "bmem_writer_epoch" in line), None)
                actual["legacy_writer"] = {"startup": "refused", "diagnostic": fence}
                if fence is None or epoch == 0:
                    return {"upgrade": actual}, {}
            else:
                old_owner, _ = service.web()
                attempted, response = old_owner.request("/v2/statements", "POST", {
                    "original": "兼容性复测：报告用中文。", "scope": {"kind": "global"}, "share": "owner",
                }, "legacy-writer-after-migration")
                actual["legacy_writer"] = {"startup": "running", "status": attempted, "body": response}
            after = count_legacy_writes(database)
            actual["legacy_writer"].update(epoch=epoch, rows_before=before, rows_after=after)
            return {"upgrade": actual}, {"upgrade": {
                "current_report_language": {"zh": "中文", "en": "英文"}.get(language, language),
                "evidence_loaded": delivery.get("evidence_loaded_state"),
                "history_detail": history["mapping_coverage"],
                "legacy_write_allowed": after > before,
            }}
    finally:
        os.environ["BMEM_V2_TEST_BIN"] = candidate_env


def count_legacy_writes(database):
    with sqlite3.connect(database, uri=True) as db:
        return db.execute("SELECT count(*) FROM authority_statements WHERE original LIKE ?",
                          ("兼容性复测：%",)).fetchone()[0]


def old_history(service, owner, count):
    from baseline_probe import post

    source = service.cli("connector", "add", "codex", "--account", "local")["source_id"]
    service.cli("connector", "resume", source)
    target = service.cli("connector", "history-options", source, "--kind", "sessions")["targets"][0]["id"]
    request = {"source_id": source, "kind": "sessions", "targets": [target], "content": "messages",
               "from": "2026-11-20T00:00:00Z", "to": "2026-11-20T01:00:00Z"}
    job = post(owner, "/v2/history/jobs", request, "legacy-journal")
    deadline = monotonic() + 12
    while monotonic() < deadline:
        status, listed = owner.request("/v2/history/jobs")
        if status != 200:
            raise ValueError((status, listed))
        job = next(row for row in listed["jobs"] if row["id"] == job["id"])
        if job["state"] == "completed":
            if job["accepted"] != count:
                raise ValueError(("legacy history fixture count", count, job))
            return job
        if job["state"] in ("failed", "partial", "cancelled"):
            break
        sleep(0.05)
    raise ValueError(("legacy history fixture did not complete", job))


def old_receipt(service, owner, fixture):
    from baseline_probe import events, load_bound, new_bound, post

    row = fixture["statement"]
    statement = post(owner, "/v2/statements", {"original": "报告用" + row["value"] + "。",
        "scope": {"kind": "global"}, "share": "authorized_assistants",
        "valid_from": row["effective"] + "T00:00:00Z"}, "legacy-statement")
    task = post(owner, "/v2/tasks", {"anchor": "legacy-receipt"}, "legacy-task")["task_id"]
    _, sources = events(owner, [{"id": "legacy-evidence", "text": "旧装入快照附带证据", "task": "legacy"}], {"legacy": task})
    browser, auth, client = new_bound(service, owner, task)
    service.operation(owner, {"kind": "change_grant", "source_id": sources["default"], "purpose": "task_delivery",
                              "client_id": client["client_id"], "allow": True}, "legacy-evidence-grant")
    status, delivery = load_bound(browser, auth, client, "legacy-context", task)
    if status != 200 or not delivery["evidence"]:
        raise ValueError((status, delivery))
    receipt = fixture["receipt"]
    refs = {receipt["revision_ids"][0]: statement["dimensions"][0]["revision_id"]}
    protocol = {key: delivery[key] for key in ("snapshot_id", "package_id")}
    protocol.update(context_epoch="1", route_ref=client["route_ref"])
    status, body = browser.request(f"/v2/deliveries/{delivery['delivery_id']}/receipts", "POST", {
        **protocol, "kind": receipt["kind"], "event_id": "legacy-statement-receipt",
        "model_call_ref": "legacy-call", "revision_ids": [refs[key] for key in receipt["revision_ids"]],
    }, authentication=auth)
    if status != 200 or body["complete"] != receipt["complete"]:
        raise ValueError((status, body))
    return {"delivery_id": delivery["delivery_id"], "response": body,
            "provided_handles": [item["handle"] for item in delivery["evidence"]]}


def backfill_preserving_capture(case, service, owner):
    from baseline_probe import post

    data = case["fixture"]
    steps = {step["id"]: step["args"] for step in case["steps"]}
    old = data["legacy_job"]["target"]
    records = {row["native_object"]: {"native_object": row["native_object"], "native_version": row["version_key"],
               "actor_kind": "user_message", "at": "2026-11-20T00:00:00Z", "text": row["text"]}
               for row in data["source_events"]}
    database = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    raw, projected = {}, {}
    for label, name, enabled, imported, pushed in (
        ("next", data["default_source"], data["continuous_capture"], steps["import"]["objects"], steps["next"]["object"]),
        ("control", steps["control"]["source"], steps["control"]["initial_continuous_capture"],
         [steps["control"]["backfill_native_object"]], steps["control"]["push_native_object"]),
    ):
        source = post(owner, "/v2/sources", {"type": "archive", "account": name,
                             "categories": ["user_message"]}, "source-" + label)["source_id"]
        if enabled:
            service.operation(owner, {"kind": "resume_capture", "source_id": source}, "enable-" + label)
        request = {"source_id": source, "kind": "archive", "targets": [old], "content": "all"}
        preview = post(owner, "/v2/history/preview", request, "history-preview-" + label)
        job = post(owner, "/v2/history/jobs", request, "history-job-" + label)
        page = post(owner, f"/v2/history/jobs/{job['id']}/pages", {"target": old, "cursor": "start",
                    "next_cursor": "archive-end", "events": [records[key] for key in imported], "complete": True},
                    "history-page-" + label)
        results_status, results = owner.request(f"/v2/history/jobs/{job['id']}/results")
        pushed_status, push = owner.request("/v2/events", "POST", {"source_id": source,
            "next_cursor": "live-first", "events": [records[pushed]]}, "live-" + label)
        expected = (200, 201, 202) if enabled else (403,)
        if results_status != 200 or pushed_status not in expected:
            raise ValueError((results_status, results, pushed_status, push))
        status, sources = owner.request("/v2/sources")
        if status != 200:
            raise ValueError((status, sources))
        source_row = next(row for row in sources["sources"] if row["source_id"] == source)
        with sqlite3.connect(database, uri=True) as db:
            versions = [row[0] for row in db.execute("SELECT native_object FROM ing_event_versions WHERE source_id=? AND active=1 ORDER BY rowid", (source,))]
        raw[label] = {"preview": preview, "page": page, "results": results, "push": {"status": pushed_status, "body": push},
                      "source": source_row, "captured_native_objects": versions}
        projected[label] = {"continuous_capture": source_row["capture_enabled"], "captured_native_objects": versions}
    projected["import"] = raw["next"]["page"]
    return raw, projected
