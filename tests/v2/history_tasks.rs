use super::*;
use rusqlite::Connection;
use serde_json::json;

fn setup() -> Connection {
    let mut db = Connection::open_in_memory().expect("open in-memory sqlite");
    db.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
    crate::authority::migrate(&db).unwrap();
    crate::context::migrate(&db).unwrap();
    crate::ingestion::migrate(&db).unwrap();
    crate::retrieval::migrate(&mut db).unwrap();
    crate::vector::migrate(&db).unwrap();
    migrate(&db).unwrap();
    db
}

fn register_source(db: &mut Connection, kind: &str, account: &str) -> String {
    let source = ingestion::register(
        db,
        json!({"type":kind,"account":account,"actors":["owner"]}),
    )
    .unwrap();
    let id = source["source_id"].as_str().unwrap().to_owned();
    ingestion::set_capture(db, &id, true).unwrap();
    id
}

#[test]
fn withdrawing_source_stops_and_disables_retry_of_explicit_bulk_tasks() -> Result<()> {
    let mut db = setup();
    let source = register_source(&mut db, "feishu", "feishu-test");
    ingestion::set_capture(&db, &source, false)?;
    let batch = create_all(
        &mut db,
        &json!({"source_id":source,"kinds":["group"]}),
        "bulk",
    )?;
    let id = batch["jobs"][0]["id"].as_str().unwrap();
    assert_eq!(batch["jobs"][0]["scope"], "all");
    assert!(select(&db)?.is_some());
    withdraw_source(&db, &source)?;
    assert!(select(&db)?.is_none());
    assert_eq!(job_view(&db, id)?["state"], "withdrawn");
    assert_eq!(change(&mut db, id, "retry").unwrap_err().status, 409);
    Ok(())
}

#[test]
fn migrate_creates_tables_idempotently() -> Result<()> {
    let db = setup();
    // Second migrate should succeed (idempotent)
    migrate(&db)?;
    let table_count: i64 = db.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'history_task_%'",
        [],
        |r| r.get(0),
    )?;
    assert!(
        table_count >= 3,
        "Expected at least 3 task tables, found {table_count}"
    );
    Ok(())
}

#[test]
fn preview_validates_request_without_writing() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target1"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z",
        "content": "messages"
    });

    let preview_result = preview(&db, &body)?;
    assert_eq!(preview_result["source_id"], src_feishu.as_str());
    assert_eq!(preview_result["kind"], "dm");
    assert_eq!(preview_result["target_count"], 1);
    assert_eq!(preview_result["capture_enabled"], true);

    // Verify nothing was written
    let task_count: i64 =
        db.query_row("SELECT COUNT(*) FROM history_task_jobs", [], |r| r.get(0))?;
    assert_eq!(task_count, 0, "preview should not create tasks");

    Ok(())
}

#[test]
fn preview_rejects_mismatched_adapter() {
    let mut db = setup();
    let src_local = register_source(&mut db, "codex", "local");

    // codex/local doesn't support dm/group/docs
    let body = json!({
        "source_id": src_local,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let err = preview(&db, &body).unwrap_err();
    assert_eq!(err.code, "invalid_input");
    assert!(err.message.contains("不支持"));
}

#[test]
fn preview_rejects_empty_targets() {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": [],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let err = preview(&db, &body).unwrap_err();
    assert_eq!(err.code, "invalid_input");
}

#[test]
fn preview_rejects_invalid_time_window() {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    // from >= to
    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-02T00:00:00Z",
        "to": "2026-01-01T00:00:00Z"
    });

    let err = preview(&db, &body).unwrap_err();
    assert_eq!(err.code, "invalid_input");
    assert!(err.message.contains("from < to"));
}

#[test]
fn preview_allows_docs_without_time_window() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "docs",
        "targets": ["doc_target"]
    });

    let result = preview(&db, &body)?;
    assert_eq!(result["kind"], "docs");
    assert!(result["from"].is_null());
    assert!(result["to"].is_null());

    Ok(())
}

#[test]
fn create_writes_task_and_items() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "group",
        "targets": ["oc_target1", "oc_target2"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z",
        "content": "messages"
    });

    let result = create(&mut db, &body, "idem-key-1")?;
    assert!(result["id"].as_str().unwrap().starts_with("htask_"));
    assert_eq!(result["state"], "queued");
    assert_eq!(result["targets"].as_array().unwrap().len(), 2);
    assert_eq!(result["items"].as_array().unwrap().len(), 2);

    Ok(())
}

#[test]
fn create_is_idempotent_with_same_fingerprint() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let first = create(&mut db, &body, "idem-key")?;
    let second = create(&mut db, &body, "idem-key")?;

    assert_eq!(
        first["id"], second["id"],
        "Same request with same key should return same task"
    );
    assert_eq!(first["state"], second["state"]);

    Ok(())
}

#[test]
fn create_conflicts_with_different_fingerprint() {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body1 = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target1"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    create(&mut db, &body1, "idem-key").unwrap();

    // Different targets with same key
    let body2 = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target2"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let err = create(&mut db, &body2, "idem-key").unwrap_err();
    assert_eq!(err.code, "key_conflict");
}

#[test]
fn list_returns_all_tasks_ordered() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    for i in 0..3 {
        let body = json!({
            "source_id": src_feishu,
            "kind": "dm",
            "targets": [format!("oc_target{i}")],
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-01-02T00:00:00Z"
        });
        create(&mut db, &body, &format!("key-{i}"))?;
    }

    let result = list(&db)?;
    let jobs = result["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 3);

    // Verify ordering by created_at
    let first_created = jobs[0]["created_at"].as_str().unwrap();
    let last_created = jobs[2]["created_at"].as_str().unwrap();
    assert!(first_created <= last_created);

    Ok(())
}

#[test]
fn cancel_stops_queued_task() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let created = create(&mut db, &body, "key")?;
    let task_id = created["id"].as_str().unwrap();

    let cancelled = change(&mut db, task_id, "cancel")?;
    assert_eq!(cancelled["state"], "cancelled");
    assert!(cancelled["finished_at"].is_string());

    // Items should also be cancelled
    let items = cancelled["items"].as_array().unwrap();
    assert!(items.iter().all(|i| i["state"] == "cancelled"));

    Ok(())
}

#[test]
fn cancel_rejects_completed_task() {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let created = create(&mut db, &body, "key").unwrap();
    let task_id = created["id"].as_str().unwrap();

    // Manually mark as completed
    db.execute(
        "UPDATE history_task_jobs SET state = 'completed' WHERE task_id = ?1",
        params![task_id],
    )
    .unwrap();

    let err = change(&mut db, task_id, "cancel").unwrap_err();
    assert_eq!(err.status, 409);
}

#[test]
fn retry_resumes_failed_targets() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target1", "oc_target2"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let created = create(&mut db, &body, "key")?;
    let task_id = created["id"].as_str().unwrap();

    // Mark as failed
    db.execute(
        "UPDATE history_task_jobs SET state = 'failed' WHERE task_id = ?1",
        params![task_id],
    )
    .unwrap();
    db.execute(
        "UPDATE history_task_items SET state = 'failed', last_error = '{\"code\":\"test\",\"message\":\"test\"}' WHERE task_id = ?1",
        params![task_id],
    )
    .unwrap();

    let retried = change(&mut db, task_id, "retry")?;
    assert_eq!(retried["state"], "queued");
    assert!(retried["finished_at"].is_null());

    // Items should be back to pending
    let items = retried["items"].as_array().unwrap();
    assert!(items.iter().all(|i| i["state"] == "pending"));

    Ok(())
}

#[test]
fn run_next_processes_one_page() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let mut db_mut = db;
    let created = create(&mut db_mut, &body, "key")?;
    let task_id = created["id"].as_str().unwrap().to_string();

    let database = Mutex::new(db_mut);
    let stop = || false;
    let read = |_: &str,
                _: &str,
                _: &str,
                _: &str,
                _: Option<&str>,
                _: Option<&str>,
                _: &str,
                _: Option<&Value>| {
        Ok((
            vec![
                json!({"native_object":"event","native_version":"1","actor_kind":"user_message","text":"fixture"}),
            ],
            None,
        ))
    };

    let processed = run_next(&database, "test", &stop, &read)?;
    assert!(processed, "Should process one page");

    // Verify task progressed
    let db = lock(&database)?;
    let job = job_view(&db, &task_id)?;
    assert_eq!(job["accepted"], 1);
    assert_eq!(job["items"][0]["state"], "completed");

    Ok(())
}

#[test]
fn interrupted_read_can_resume_after_restart() -> Result<()> {
    use std::cell::Cell;

    let mut db = setup();
    let source = register_source(&mut db, "feishu", "feishu-test");
    let task = create(
        &mut db,
        &json!({"source_id":source,"kind":"dm","targets":["oc_target"],
        "from":"2026-01-01T00:00:00Z","to":"2026-01-02T00:00:00Z"}),
        "restart",
    )?;
    let id = task["id"].as_str().unwrap();
    let database = Mutex::new(db);
    let stopped = Cell::new(false);
    let stop = || stopped.get();
    let interrupted = |_: &str,
                       _: &str,
                       _: &str,
                       _: &str,
                       _: Option<&str>,
                       _: Option<&str>,
                       _: &str,
                       _: Option<&Value>| {
        stopped.set(true);
        Err(Error::unavailable("service stopping"))
    };
    assert!(!run_next(&database, "balanced", &stop, &interrupted)?);
    assert_eq!(job_view(&*lock(&database)?, id)?["state"], "running");
    stopped.set(false);
    let resumed = |_: &str,
                   _: &str,
                   _: &str,
                   _: &str,
                   _: Option<&str>,
                   _: Option<&str>,
                   _: &str,
                   cursor: Option<&Value>| {
        assert!(cursor.is_none());
        Ok((vec![], None))
    };
    assert!(run_next(&database, "balanced", &stop, &resumed)?);
    assert_eq!(job_view(&*lock(&database)?, id)?["state"], "completed");
    Ok(())
}

#[test]
fn run_next_respects_stop_flag() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    create(&mut db, &body, "key")?;
    let database = Mutex::new(db);
    let stop = || true; // Always stop
    let read = |_: &str,
                _: &str,
                _: &str,
                _: &str,
                _: Option<&str>,
                _: Option<&str>,
                _: &str,
                _: Option<&Value>| { Ok((vec![], None)) };

    let processed = run_next(&database, "test", &stop, &read)?;
    assert!(!processed, "Should not process when stopped");

    Ok(())
}

#[test]
fn run_next_handles_read_error() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let created = create(&mut db, &body, "key")?;
    let task_id = created["id"].as_str().unwrap().to_string();

    let database = Mutex::new(db);
    let stop = || false;
    let read = |_: &str,
                _: &str,
                _: &str,
                _: &str,
                _: Option<&str>,
                _: Option<&str>,
                _: &str,
                _: Option<&Value>| { Err(Error::invalid("simulated read error")) };

    let processed = run_next(&database, "test", &stop, &read)?;
    assert!(processed, "Should record error");

    // Verify item failed
    let db = lock(&database)?;
    let job = job_view(&db, &task_id)?;
    assert_eq!(job["items"][0]["state"], "failed");
    assert!(!job["errors"].as_array().unwrap().is_empty());

    Ok(())
}

#[test]
fn run_next_resumes_from_checkpoint() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    let created = create(&mut db, &body, "key")?;
    let task_id = created["id"].as_str().unwrap().to_string();

    // Set initial cursor
    db.execute(
        "UPDATE history_task_items SET cursor = '{\"page\":1}' WHERE task_id = ?1",
        params![task_id],
    )
    .unwrap();

    let database = Mutex::new(db);
    let stop = || false;
    let read = |_: &str,
                _: &str,
                _: &str,
                _: &str,
                _: Option<&str>,
                _: Option<&str>,
                _: &str,
                cursor: Option<&Value>| {
        // Verify cursor was passed
        assert!(cursor.is_some());
        let cursor_obj = cursor.unwrap().as_object().unwrap();
        assert_eq!(cursor_obj["page"], 1);
        Ok((vec![], None))
    };

    run_next(&database, "test", &stop, &read)?;

    Ok(())
}

#[test]
fn capture_revocation_pauses_task() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    create(&mut db, &body, "key")?;

    // Revoke capture
    db.execute(
        "UPDATE ing_grants SET active = 0 WHERE source_id = ?1 AND purpose = 'capture'",
        [&src_feishu],
    )
    .unwrap();

    let database = Mutex::new(db);
    let stop = || false;
    let read = |_: &str,
                _: &str,
                _: &str,
                _: &str,
                _: Option<&str>,
                _: Option<&str>,
                _: &str,
                _: Option<&Value>| { Ok((vec![], None)) };

    // Should not process when capture is revoked
    let processed = run_next(&database, "test", &stop, &read)?;
    assert!(!processed);

    Ok(())
}

#[test]
fn deduplication_counts_real_duplicates() -> Result<()> {
    let mut db = setup();
    let src_feishu = register_source(&mut db, "feishu", "feishu-test");

    let body = json!({
        "source_id": src_feishu,
        "kind": "dm",
        "targets": ["oc_target"],
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-02T00:00:00Z"
    });

    // An earlier import knew the source and message ID, but not the conversation kind.
    let legacy = ingestion::accept(
        &mut db,
        "owner",
        "legacy",
        json!({"source_id": src_feishu, "next_cursor":"before-import",
            "events":[{"native_object":"obj1","native_version":"1","actor_kind":"user_message","text":"event1"}]}),
        "test",
    )?;
    let created = create(&mut db, &body, "key")?;
    let task_id = created["id"].as_str().unwrap().to_string();

    let database = Mutex::new(db);
    let stop = || false;

    let read1 = |_: &str,
                 _: &str,
                 _: &str,
                 _: &str,
                 _: Option<&str>,
                 _: Option<&str>,
                 _: &str,
                 _: Option<&Value>| {
        Ok((
            vec![
                json!({"native_object": "obj1", "native_version": "1", "actor_kind":"user_message", "text": "event1"}),
                json!({"native_object": "obj2", "native_version": "1", "actor_kind":"user_message", "text": "event2"}),
            ],
            None,
        ))
    };

    run_next(&database, "test", &stop, &read1)?;

    let db = lock(&database)?;
    let job = job_view(&db, &task_id)?;
    assert_eq!(job["accepted"], 1);
    assert_eq!(job["duplicates"], 1);
    let exclusions =
        crate::retrieval::SearchExclusions::parse(Some(&json!(["feishu_unclassified"])))?;
    let found = crate::retrieval::search_owner(&db, "event1", None, &exclusions)?;
    assert_eq!(found["results"].as_array().unwrap().len(), 1);
    assert_eq!(
        found["results"][0]["event_id"],
        legacy["events"][0]["event_id"]
    );
    let exclusions = crate::retrieval::SearchExclusions::parse(Some(&json!(["dm"])))?;
    let found = crate::retrieval::search_owner(&db, "event1", None, &exclusions)?;
    assert_eq!(found["results"], json!([]));

    Ok(())
}

/// A bulk batch shares one `created_at`, so the drain order must come from
/// insertion order and not from the random `task_id`. The scheduler used to tie
/// on the UUID, which made the order of a three-job batch random every run; one
/// third of the time the last job finished after the caller's budget and the
/// data-console restart test failed for no reproducible reason.
#[test]
fn bulk_jobs_with_equal_created_at_drain_in_insertion_order() -> Result<()> {
    let mut db = setup();
    let source = register_source(&mut db, "feishu", "feishu-test");

    // Kinds arrive out of order; create_all sorts the batch and inserts in that
    // order, so the response order is the insertion order the caller sees.
    let batch = create_all(
        &mut db,
        &json!({"source_id":source,"kinds":["group","dm","docs"]}),
        "bulk",
    )?;
    let batch_jobs = batch["jobs"].as_array().expect("bulk batch lists jobs");
    let ids: Vec<String> = batch_jobs
        .iter()
        .map(|job| job["id"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(ids.len(), 3, "expected three jobs, got {ids:?}");

    let ties: i64 = db.query_row(
        "SELECT COUNT(*) - COUNT(DISTINCT created_at) FROM history_task_jobs",
        [],
        |row| row.get(0),
    )?;
    assert!(ties >= 2, "batch must share created_at, ties={ties}");

    let expected: Vec<String> = batch_jobs
        .iter()
        .map(|job| job["kind"].as_str().unwrap().to_owned())
        .collect();

    // The task list the UI reads must follow the same order.
    let listed: Vec<String> = list(&db)?["jobs"]
        .as_array()
        .expect("list returns jobs")
        .iter()
        .map(|job| job["id"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(listed, ids);

    let mut drained = Vec::with_capacity(ids.len());
    for _ in &ids {
        let pick = select(&db)?.expect("a queued job is selectable");
        drained.push(pick.kind.clone());
        db.execute(
            "UPDATE history_task_items SET state = 'completed' WHERE task_id = ?1",
            rusqlite::params![pick.task_id],
        )?;
        db.execute(
            "UPDATE history_task_jobs SET state = 'completed' WHERE task_id = ?1",
            rusqlite::params![pick.task_id],
        )?;
    }
    assert_eq!(drained, expected, "drain order must equal insertion order");
    assert!(select(&db)?.is_none(), "no runnable job is left");

    Ok(())
}
