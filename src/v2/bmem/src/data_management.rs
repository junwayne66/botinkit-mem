use crate::error::{Error, Result};
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

const MAX_EXPORT_EVENTS: usize = 1_000;
const MAX_EXPORT_BYTES: usize = 4 * 1024 * 1024;

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS data_transfers (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('import','export')),
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id),
            count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            receipt TEXT NOT NULL
         );",
    )?;
    Ok(())
}

pub fn records(db: &Connection) -> Result<Value> {
    let mut stmt = db.prepare(
        "SELECT id,kind,source_id,count,created_at,receipt
         FROM data_transfers ORDER BY created_at DESC,rowid DESC LIMIT 100",
    )?;
    let mut rows = stmt.query([])?;
    let mut records = Vec::new();
    while let Some(row) = rows.next()? {
        let receipt = serde_json::from_str::<Value>(&row.get::<_, String>(5)?)?;
        records.push(json!({
            "id":row.get::<_, String>(0)?,"kind":row.get::<_, String>(1)?,
            "source_id":row.get::<_, String>(2)?,"count":row.get::<_, i64>(3)?,
            "created_at":row.get::<_, String>(4)?,"receipt":receipt,"status":"completed",
        }));
    }
    Ok(json!({"records":records}))
}

pub fn record_import(db: &Connection, source_id: &str, receipt: &Value) -> Result<()> {
    let id = receipt["ingest_id"]
        .as_str()
        .ok_or_else(|| Error::internal("missing ingest ID"))?;
    let committed = receipt["committed_at"]
        .as_str()
        .ok_or_else(|| Error::internal("missing ingest time"))?;
    let count = receipt["events"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|event| event["disposition"] == "accepted")
        .count();
    db.execute(
        "INSERT OR IGNORE INTO data_transfers(id,kind,source_id,count,created_at,receipt)
         VALUES(?1,'import',?2,?3,?4,?5)",
        params![id, source_id, count as i64, committed, receipt.to_string()],
    )?;
    Ok(())
}

pub fn export(db: &mut Connection, body: &Value) -> Result<Value> {
    let source_id = body["source_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .ok_or_else(|| Error::invalid("请选择导出来源"))?;
    let from = timestamp(body, "from")?;
    let to = timestamp(body, "to")?;
    if from >= to {
        return Err(Error::invalid("导出开始时间必须早于结束时间"));
    }

    let tx = db.transaction()?;
    let source = tx
        .query_row(
            "SELECT kind,account FROM ing_sources WHERE source_id=?1",
            [source_id],
            |row| {
                Ok(
                    json!({"source_id":source_id,"type":row.get::<_, String>(0)?,
                        "account":row.get::<_, String>(1)?}),
                )
            },
        )
        .optional()?
        .ok_or_else(|| Error::missing("导出来源不存在"))?;
    let events = export_events(&tx, source_id, &from.to_rfc3339(), &to.to_rfc3339())?;
    let id = format!("export_{}", Uuid::new_v4().simple());
    let filename = format!("bmem-{}-{}.json", from.format("%Y-%m-%d"), &id[7..19]);
    let now = Utc::now().to_rfc3339();
    let document = json!({"format":"bmem-v2-events","source":source,
        "from":from.to_rfc3339(),"to":to.to_rfc3339(),"exported_at":now,"events":events});
    let bytes = serde_json::to_vec(&document)?;
    if bytes.len() > MAX_EXPORT_BYTES {
        return Err(Error::invalid("导出文件超过 4 MiB，请缩小日期范围"));
    }
    let count = events.len();
    let receipt = json!({"export_id":id,"source_id":source_id,"count":count,
        "filename":filename,"sha256":format!("{:x}",Sha256::digest(&bytes)),"exported_at":now,
        "from":from.to_rfc3339(),"to":to.to_rfc3339()});
    tx.execute(
        "INSERT INTO data_transfers(id,kind,source_id,count,created_at,receipt)
         VALUES(?1,'export',?2,?3,?4,?5)",
        params![id, source_id, count as i64, now, receipt.to_string()],
    )?;
    tx.commit()?;
    Ok(json!({"filename":filename,"document":document,"receipt":receipt}))
}

fn timestamp(body: &Value, key: &str) -> Result<DateTime<Utc>> {
    body[key]
        .as_str()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc))
        .ok_or_else(|| Error::invalid(format!("{key} 必须是带时区的 RFC3339 时间")))
}

fn export_events(db: &Connection, source: &str, from: &str, to: &str) -> Result<Vec<Value>> {
    let mut stmt = db.prepare(
        "SELECT e.native_object,e.native_version,e.actor_kind,e.occurred_at,e.change_kind,e.content
         FROM ing_event_versions e
         WHERE e.source_id=?1 AND e.active=1
           AND julianday(e.occurred_at)>=julianday(?2) AND julianday(e.occurred_at)<julianday(?3)
           AND NOT EXISTS (SELECT 1 FROM ing_root_revocations r WHERE r.root_id=e.root_id)
           AND NOT EXISTS (
               SELECT 1 FROM ing_event_versions n WHERE n.root_id=e.root_id AND n.active=1
               AND (julianday(n.occurred_at)>julianday(e.occurred_at)
                    OR (julianday(n.occurred_at)=julianday(e.occurred_at) AND n.rowid>e.rowid))
           )
         ORDER BY julianday(e.occurred_at),e.rowid LIMIT ?4",
    )?;
    let mut rows = stmt.query(params![source, from, to, (MAX_EXPORT_EVENTS + 1) as i64])?;
    let mut events = Vec::new();
    let mut content_bytes = 0;
    while let Some(row) = rows.next()? {
        let raw: String = row.get(5)?;
        content_bytes += raw.len();
        if events.len() >= MAX_EXPORT_EVENTS || content_bytes > MAX_EXPORT_BYTES {
            return Err(Error::invalid(
                "导出范围超过 1000 条事件或 4 MiB，请缩小日期范围",
            ));
        }
        let content: Value = serde_json::from_str(&raw)?;
        let mut event = json!({"native_object":row.get::<_, String>(0)?,
            "actor_kind":row.get::<_, String>(2)?,"at":row.get::<_, String>(3)?,
            "change_kind":row.get::<_, String>(4)?});
        if let Some(version) = row.get::<_, Option<String>>(1)? {
            event["native_version"] = json!(version);
        }
        for key in ["text", "checks"] {
            if let Some(value) = content.get(key) {
                event[key] = value.clone();
            }
        }
        events.push(event);
    }
    Ok(events)
}

#[cfg(test)]
#[path = "../../../../tests/v2/data_management.rs"]
mod tests;
