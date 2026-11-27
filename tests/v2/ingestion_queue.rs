use super::*;

fn capture(db: &mut Connection, account: &str, names: &[&str]) -> Result<Vec<String>> {
    let source = register(
        db,
        json!({"type":"test","account":account,"actors":["owner"]}),
    )?;
    let id = source["source_id"].as_str().unwrap();
    set_capture(db, id, true)?;
    set_extract(db, id, true, Some("fixture-route"))?;
    let events: Vec<_> = names.iter().map(|name| {
        json!({"native_object":name, "native_version":"1", "actor_kind":"user_message", "text":name})
    }).collect();
    let receipt = accept(
        db,
        "owner",
        "capture",
        json!({"source_id":id,"next_cursor":"1","events":events}),
        "low",
    )?;
    Ok(receipt["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|event| event["event_id"].as_str().unwrap().to_owned())
        .collect())
}

#[test]
fn due_retries_and_backlog_share_the_worker_across_sources() -> Result<()> {
    let mut db = Connection::open_in_memory()?;
    migrate(&db)?;
    crate::retrieval::migrate(&mut db)?;
    let a = capture(&mut db, "a", &["first", "second"])?;
    let first = claim(&mut db, "fixture-route")?.unwrap();
    assert_eq!(first.event_id, a[0]);
    fail(&db, &first, "model_timeout")?;
    // Advance this retry's deadline without sleeping through the backoff.
    db.execute(
        "UPDATE ing_jobs SET available_at=?1 WHERE job_id=?2",
        params![now_utc(), first.id],
    )?;
    let b = capture(&mut db, "b", &["third", "fourth"])?;
    for expected in [&b[0], &a[1], &b[1]] {
        assert_eq!(
            &claim(&mut db, "fixture-route")?.unwrap().event_id,
            expected
        );
    }
    let retry = claim(&mut db, "fixture-route")?.unwrap();
    assert_eq!(retry.event_id, a[0]);
    finish(&mut db, &retry, json!([]), "fixture-route")?;
    let reason: Option<String> = db.query_row(
        "SELECT fail_reason FROM ing_jobs WHERE job_id=?1",
        [&retry.id],
        |r| r.get(0),
    )?;
    assert!(reason.is_none());
    // Older workers left the prior failure on jobs that recovered. Both public
    // views must still report those as successful without changing stored history.
    db.execute(
        "UPDATE ing_jobs SET fail_reason='temporarily_unavailable' WHERE job_id=?1",
        [&retry.id],
    )?;
    assert!(fetch_job(&db, &retry.id)?["fail_reason"].is_null());
    let page = crate::analytics::job_page(&db, "20", None, Some("done"), None)?;
    assert_eq!(page["jobs"][0]["id"], retry.id);
    assert!(page["jobs"][0]["fail_reason"].is_null());
    assert!(claim(&mut db, "fixture-route")?.is_none());
    Ok(())
}

#[test]
fn retry_budget_and_manual_requeue_keep_job_and_history() -> Result<()> {
    let mut db = Connection::open_in_memory()?;
    migrate(&db)?;
    crate::retrieval::migrate(&mut db)?;
    let events = capture(&mut db, "retry", &["poison"])?;
    for index in 0..=MAX_RETRIES {
        let job = claim(&mut db, "fixture-route")?.unwrap();
        assert_eq!(job.event_id, events[0]);
        fail(&db, &job, "model_invalid_output")?;
        let state = fetch_job(&db, &job.id)?;
        assert_eq!(
            state["status"],
            if index == MAX_RETRIES {
                "failed"
            } else {
                "retrying"
            }
        );
        assert_eq!(
            state["attempt_history"].as_array().unwrap().len(),
            (index + 1) as usize
        );
        db.execute(
            "UPDATE ing_jobs SET available_at=?1 WHERE job_id=?2",
            params![now_utc(), job.id],
        )?;
    }
    assert!(claim(&mut db, "fixture-route")?.is_none());
    let failed = crate::analytics::job_page(&db, "20", None, Some("failed"), None)?;
    let id = failed["jobs"][0]["id"].as_str().unwrap();
    assert_eq!(retry(&db, id)?["status"], "pending");
    assert_eq!(retry(&db, id)?["attempts"], 4);
    let job = claim(&mut db, "fixture-route")?.unwrap();
    assert_eq!(job.id, id);
    fail(&db, &job, "model_invalid_output")?;
    assert_eq!(fetch_job(&db, id)?["status"], "retrying");
    Ok(())
}

#[test]
fn three_distinct_failed_jobs_open_only_their_route() -> Result<()> {
    use crate::extraction::{self, Work};
    let mut db = Connection::open_in_memory()?;
    migrate(&db)?;
    crate::retrieval::migrate(&mut db)?;
    capture(&mut db, "batch", &["one", "two", "three", "four"])?;
    for _ in 0..3 {
        let job = claim(&mut db, "fixture-route")?.unwrap();
        fail(&db, &job, "model_invalid_output")?;
    }
    assert_eq!(extraction::action(&db, "fixture-route")?, Work::Probe);
    assert_eq!(
        extraction::action(&db, "healthy-route")?,
        Work::Claim { trial_reason: None }
    );
    extraction::waiting(&db, "fixture-route", "model_unreachable", false)?;
    assert_eq!(extraction::action(&db, "fixture-route")?, Work::Idle);
    assert_eq!(
        extraction::summary(&db, Some("fixture-route"))?["queued"],
        4
    );
    extraction::probe_passed(&db, "fixture-route")?;
    assert_eq!(
        extraction::action(&db, "fixture-route")?,
        Work::Claim {
            trial_reason: Some("model_unreachable".into())
        }
    );
    let job = claim(&mut db, "fixture-route")?.unwrap();
    finish(&mut db, &job, json!([]), "fixture-route")?;
    extraction::ready(&db, "fixture-route")?;
    assert_eq!(
        extraction::action(&db, "fixture-route")?,
        Work::Claim { trial_reason: None }
    );
    Ok(())
}

#[test]
fn legacy_job_migration_preserves_attempts_and_blocks_withdrawn_requeue() -> Result<()> {
    let mut db = Connection::open_in_memory()?;
    migrate(&db)?;
    crate::retrieval::migrate(&mut db)?;
    capture(&mut db, "legacy", &["old"])?;
    let job = claim(&mut db, "fixture-route")?.unwrap();
    db.execute_batch(
        "DELETE FROM ing_job_attempts;
                      ALTER TABLE ing_jobs DROP COLUMN retry_failures;
                      UPDATE ing_jobs SET attempts=9,status='failed',lease_expires_at=NULL,
                         fail_reason='model_http_503';",
    )?;
    migrate(&db)?;
    let observed = fetch_job(&db, &job.id)?;
    assert_eq!(observed["attempts"], 9);
    assert_eq!(observed["fail_reason"], "model_http_503");
    assert_eq!(observed["status"], "retrying");
    assert_eq!(observed["retry_count"], 0);
    // A migrated queue can still contain a stale ready row for an inactive version.
    db.execute(
        "UPDATE ing_event_versions SET active=0 WHERE event_id=?1",
        [&job.event_id],
    )?;
    assert!(retry(&db, &job.id).is_err());
    assert!(claim(&mut db, "fixture-route")?.is_none());
    Ok(())
}
