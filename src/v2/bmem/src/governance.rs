use crate::error::{Error, Result};
use rusqlite::{Connection, OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS governance_vector_cleanup_jobs (
            root_id TEXT PRIMARY KEY REFERENCES ing_root_revocations(root_id)
        );
        CREATE TABLE IF NOT EXISTS governance_vector_cleanup_attempts (
            id INTEGER PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES governance_vector_cleanup_jobs(root_id),
            state TEXT NOT NULL CHECK(state IN ('complete','failed')),
            attempted_at TEXT NOT NULL,
            sqlite_error_code INTEGER
        );
        CREATE INDEX IF NOT EXISTS governance_vector_cleanup_history
          ON governance_vector_cleanup_attempts(root_id,id);
        CREATE TABLE IF NOT EXISTS governance_external_copies (
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id),
            event_id TEXT NOT NULL REFERENCES ing_event_versions(event_id),
            client_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            PRIMARY KEY(root_id,event_id,client_id,receipt_id),
            FOREIGN KEY(client_id,receipt_id) REFERENCES context_receipts(client_id,event_id)
        );",
    )?;
    Ok(())
}

pub fn enqueue_vector_cleanup(db: &Connection, root: &str) -> Result<()> {
    db.execute(
        "INSERT OR IGNORE INTO governance_vector_cleanup_jobs VALUES(?1)",
        [root],
    )?;
    Ok(())
}

fn cleanup_state(db: &Connection, root: &str) -> Result<String> {
    db.query_row(
        "SELECT coalesce((SELECT state FROM governance_vector_cleanup_attempts a
             WHERE a.root_id=j.root_id ORDER BY id DESC LIMIT 1),'pending')
         FROM governance_vector_cleanup_jobs j WHERE j.root_id=?1",
        [root],
        |r| r.get(0),
    )
    .optional()?
    .ok_or_else(|| Error::missing("managed cleanup is unavailable"))
}

pub fn start_vector_cleanup(db: &mut Connection, root: &str) {
    if cleanup_state(db, root).is_ok_and(|state| state == "pending") {
        let _ = retry_vector_cleanup(db, root);
    }
}

pub fn retry_vector_cleanup(db: &mut Connection, root: &str) -> Result<Value> {
    let mut tx = db.transaction()?;
    if cleanup_state(&tx, root)? != "complete" {
        let attempted_at = crate::clock::now()?;
        let events = {
            let mut query =
                tx.prepare("SELECT event_id FROM ing_event_versions WHERE root_id=?1")?;
            query
                .query_map([root], |r| r.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?
        };
        let mut cleanup = tx.savepoint()?;
        let deletion = events
            .iter()
            .try_for_each(|event| crate::vector::drop_event(&cleanup, event));
        let (state, error_code) = match deletion {
            Ok(()) => {
                cleanup.commit()?;
                ("complete", None)
            }
            Err(error) => {
                cleanup.rollback()?;
                cleanup.commit()?;
                (
                    "failed",
                    error.sqlite_error().map(|cause| cause.extended_code),
                )
            }
        };
        tx.execute(
            "INSERT INTO governance_vector_cleanup_attempts(root_id,state,attempted_at,sqlite_error_code)
             VALUES(?1,?2,?3,?4)",
            params![root,state,attempted_at,error_code],
        )?;
    }
    let result = receipt(&tx, root)?;
    tx.commit()?;
    Ok(result)
}

pub fn receipt(db: &Connection, root: &str) -> Result<Value> {
    let state = cleanup_state(db, root)?;
    let mut query = db.prepare("SELECT receipt_id,delivery_id,recipient FROM governance_external_copies
        WHERE root_id=?1 GROUP BY receipt_id,delivery_id,recipient ORDER BY receipt_id,delivery_id,recipient")?;
    let sent = query
        .query_map([root], |r| {
            Ok(
                json!({"receipt_id":r.get::<_,String>(0)?,"delivery_id":r.get::<_,String>(1)?,
                "recipient":r.get::<_,String>(2)?}),
            )
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let attempts: i64 = db.query_row(
        "SELECT count(*) FROM governance_vector_cleanup_attempts WHERE root_id=?1",
        [root],
        |r| r.get(0),
    )?;
    let cleanup_error: Option<i32> = db.query_row(
        "SELECT (SELECT sqlite_error_code FROM governance_vector_cleanup_attempts
         WHERE root_id=?1 ORDER BY id DESC LIMIT 1)",
        [root],
        |r| r.get(0),
    )?;
    let receipt_ids = sent
        .iter()
        .map(|copy| &copy["receipt_id"])
        .collect::<Vec<_>>();
    Ok(
        json!({"root_id":root,"logical_read":"blocked","vector_cleanup":state,"vector_cleanup_error_code":cleanup_error,
        "vector_cleanup_attempts":attempts,"previously_sent":receipt_ids,"external_copies":sent,
        "external_erase":if sent.is_empty() { "no_known_copy" } else { "unverified" }}),
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalCopy<'a> {
    delivery_id: &'a str,
    receipt_id: &'a str,
    handle: &'a str,
}

pub fn record_external_copy(db: &Connection, body: &Value) -> Result<Value> {
    let request = ExternalCopy::deserialize(body)?;
    let event = request
        .handle
        .strip_prefix("event_")
        .filter(|event| !event.is_empty())
        .ok_or_else(|| Error::invalid("external copy requires a captured evidence handle"))?;
    let (root, client, recipient): (String, String, String) = db
        .query_row(
            "SELECT e.root_id,r.client_id,c.destination
         FROM context_receipts r JOIN context_clients c ON c.id=r.client_id
         JOIN ing_event_versions e ON e.event_id=?3
         WHERE r.delivery_id=?1 AND r.event_id=?2 AND r.kind='loaded'
           AND EXISTS(SELECT 1 FROM json_each(r.result,'$.loaded_evidence_refs') WHERE value=?4)",
            params![
                request.delivery_id,
                request.receipt_id,
                event,
                request.handle
            ],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("no matching observed delivery for this evidence"))?;
    db.execute(
        "INSERT OR IGNORE INTO governance_external_copies VALUES(?1,?2,?3,?4,?5,?6)",
        params![
            root,
            event,
            client,
            request.receipt_id,
            request.delivery_id,
            recipient
        ],
    )?;
    Ok(
        json!({"root_id":root,"delivery_id":request.delivery_id,"receipt_id":request.receipt_id,
        "recipient":recipient,"external_erase":"unverified"}),
    )
}
