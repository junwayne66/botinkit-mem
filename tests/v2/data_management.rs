use super::*;
use crate::{authority, context, ingestion, retrieval, vector};

fn vault() -> Result<(Connection, String)> {
    let mut db = Connection::open_in_memory()?;
    db.execute_batch("PRAGMA foreign_keys=ON")?;
    authority::migrate(&db)?;
    context::migrate(&db)?;
    ingestion::migrate(&db)?;
    retrieval::migrate(&mut db)?;
    vector::migrate(&db)?;
    migrate(&db)?;
    let source = ingestion::register(
        &mut db,
        json!({"type":"fixture","account":"export-test","actors":["owner"]}),
    )?;
    let source_id = source["source_id"].as_str().unwrap().to_owned();
    ingestion::set_capture(&db, &source_id, true)?;
    Ok((db, source_id))
}

#[test]
fn export_only_includes_current_unwithdrawn_events_and_persists_no_body() -> Result<()> {
    let (mut db, source) = vault()?;
    let receipt = ingestion::accept(
        &mut db,
        "owner",
        "fixture-import",
        json!({
            "source_id":source,"cursor":"","next_cursor":"next-1","events":[
                {"native_object":"updated","native_version":"1","actor_kind":"user_message",
                 "at":"2026-11-19T10:00:00+08:00","text":"previous fixture version"},
                {"native_object":"updated","native_version":"2","change_kind":"updated",
                 "actor_kind":"user_message","at":"2026-11-19T10:01:00+08:00","text":"latest fixture version"},
                {"native_object":"withdrawn","native_version":"1","actor_kind":"user_message",
                 "at":"2026-11-19T10:02:00+08:00","text":"withdrawn fixture text"},
                {"native_object":"outside","native_version":"1","actor_kind":"user_message",
                 "at":"2026-11-19T10:03:00+08:00","text":"outside half-open range"}
            ]
        }),
        "balanced",
    )?;
    record_import(&db, &source, &receipt)?;
    record_import(&db, &source, &receipt)?;
    ingestion::accept(
        &mut db,
        "owner",
        "withdraw",
        json!({
            "source_id":source,"cursor":"next-1","next_cursor":"next-2","events":[
                {"native_object":"withdrawn","native_version":"1","actor_kind":"user_message",
                 "change_kind":"revoked","at":"2026-11-19T10:02:30+08:00"}
            ]
        }),
        "balanced",
    )?;
    let result = export(
        &mut db,
        &json!({"source_id":source,
        "from":"2026-11-19T10:00:00+08:00","to":"2026-11-19T02:03:00Z"}),
    )?;
    let events = result["document"]["events"].as_array().unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["native_version"], "2");
    assert_eq!(events[0]["text"], "latest fixture version");
    let audit = records(&db)?;
    assert_eq!(audit["records"].as_array().unwrap().len(), 2);
    assert!(!audit.to_string().contains("latest fixture version"));
    assert!(!result.to_string().contains("withdrawn fixture text"));
    Ok(())
}

#[test]
fn export_rejects_invalid_scope_without_recording_a_transfer() -> Result<()> {
    let (mut db, source) = vault()?;
    let request = json!({"source_id":source,"from":"2026-11-19T10:00:00Z",
                         "to":"2026-11-19T09:00:00Z"});
    assert_eq!(export(&mut db, &request).unwrap_err().status, 422);
    assert_eq!(records(&db)?["records"], json!([]));
    Ok(())
}
