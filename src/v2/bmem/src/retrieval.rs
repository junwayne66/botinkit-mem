use crate::error::{Error, Result};
use crate::{authority, context, ingestion};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use uuid::Uuid;

pub fn migrate(db: &mut Connection) -> Result<()> {
    db.execute_batch("
        CREATE TABLE IF NOT EXISTS recall_events (
            handle TEXT PRIMARY KEY, source_id TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL,
            task_id TEXT, actor_kind TEXT NOT NULL, body BLOB NOT NULL, encoding INTEGER NOT NULL,
            occurred_at TEXT
        );
        CREATE INDEX IF NOT EXISTS recall_by_task ON recall_events(task_id, occurred_at, event_id);
        CREATE INDEX IF NOT EXISTS recall_by_source ON recall_events(source_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(handle UNINDEXED, terms, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS recall_grants(
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id),
            client_id TEXT NOT NULL REFERENCES context_clients(id),
            PRIMARY KEY(source_id,client_id)
        );
        CREATE TABLE IF NOT EXISTS governance_previews (
            id TEXT PRIMARY KEY, request TEXT NOT NULL, footprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS governance_operations (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL
        );")?;
    Ok(())
}

fn words(text: &str) -> BTreeSet<String> {
    let mut result = BTreeSet::new();
    let mut latin = String::new();
    let mut prev_cjk = None;
    for ch in text.chars().flat_map(char::to_lowercase) {
        if ('\u{3400}'..='\u{9fff}').contains(&ch) {
            if !latin.is_empty() {
                result.insert(std::mem::take(&mut latin));
            }
            if let Some(prev) = prev_cjk {
                result.insert(format!("{prev}{ch}"));
            }
            result.insert(ch.to_string());
            prev_cjk = Some(ch);
        } else {
            prev_cjk = None;
            if ch.is_alphanumeric() {
                latin.push(ch);
            } else if !latin.is_empty() {
                result.insert(std::mem::take(&mut latin));
            }
        }
    }
    if !latin.is_empty() {
        result.insert(latin);
    }
    result
}

fn terms(text: &str) -> String {
    words(text).into_iter().collect::<Vec<_>>().join(" ")
}

fn index_event(db: &Connection, profile: &str, event: &Value) -> Result<()> {
    if event["active"].as_bool() == Some(false) {
        return Ok(());
    }
    let event_id = field(event, "event_id")?;
    let source_id = field(event, "source_id")?;
    let kind = field(event, "actor_kind")?;
    let handle = format!("event_{event_id}");
    let text = event["text"]
        .as_str()
        .map(str::to_owned)
        .or_else(|| {
            event
                .get("checks")
                .filter(|v| !v.is_null())
                .map(Value::to_string)
        })
        .unwrap_or_default();
    if text.is_empty() {
        return Ok(());
    }
    let raw = text.as_bytes();
    let mut data = raw.to_vec();
    let mut encoding = 0;
    let level = match profile {
        "high" => 8,
        "balanced" => 3,
        _ => 0,
    };
    if level != 0 && raw.len() >= 256 {
        let compressed = zstd::bulk::compress(raw, level)?;
        if compressed.len() < raw.len() {
            data = compressed;
            encoding = 1;
        }
    }
    let wrote = db.execute(
        "INSERT OR IGNORE INTO recall_events(handle,source_id,event_id,task_id,actor_kind,body,encoding,occurred_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
        params![handle,source_id,event_id,event["task_id"].as_str(),kind,data,encoding,event["at"].as_str()],
    )?;
    if wrote > 0 {
        db.execute(
            "INSERT INTO recall_fts(handle,terms) VALUES(?1,?2)",
            params![handle, terms(&text)],
        )?;
    }
    Ok(())
}

pub fn drop_event(db: &Connection, event_id: &str) -> Result<()> {
    db.execute("DELETE FROM recall_fts WHERE handle IN (SELECT handle FROM recall_events WHERE event_id=?1)",[event_id])?;
    db.execute("DELETE FROM recall_events WHERE event_id=?1", [event_id])?;
    Ok(())
}

pub fn refresh(db: &Connection, profile: &str, receipt: &Value) -> Result<()> {
    let event_ids = receipt["events"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| row["event_id"].as_str());
    for id in event_ids {
        let row = db.query_row(
            "SELECT source_id,task_id,actor_kind,occurred_at,content,active FROM ing_event_versions WHERE event_id=?1",
            [id], |r| Ok((r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?,
                r.get::<_,String>(3)?,r.get::<_,String>(4)?,r.get::<_,bool>(5)?)),
        ).optional()?;
        if let Some((source, task, kind, at, content, active)) = row {
            if !active {
                drop_event(db, id)?;
                continue;
            }
            let value: Value = serde_json::from_str(&content)?;
            let mut event =
                json!({"source_id":source,"event_id":id,"task_id":task,"actor_kind":kind,"at":at});
            if let Some(text) = value.as_str().or_else(|| value["text"].as_str()) {
                event["text"] = json!(text);
            } else if let Some(checks) = value.get("checks") {
                event["checks"] = checks.clone();
            } else {
                event["text"] = json!(value.to_string());
            }
            index_event(db, profile, &event)?;
        }
    }
    Ok(())
}

fn visible(db: &Connection, id: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_event_versions WHERE event_id=?1 AND active=1)",
        [id],
        |r| r.get(0),
    )?)
}

fn event_body(data: Vec<u8>, encoding: i64) -> Result<String> {
    let raw = match encoding {
        0 => data,
        1 => zstd::bulk::decompress(&data, 4 * 1024 * 1024)?,
        _ => return Err(Error::internal("unknown evidence encoding")),
    };
    String::from_utf8(raw).map_err(|_| Error::internal("stored evidence is not UTF-8"))
}

pub fn search_shared(db: &Connection, query: &str) -> Result<Value> {
    Ok(json!({"results":match_statements(db,query,true)?,"retrieval":"lexical"}))
}

fn match_statements(db: &Connection, query: &str, shared: bool) -> Result<Vec<Value>> {
    let current = authority::current(db, None, None, None, shared)?;
    let requested = words(query);
    Ok(current["statements"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| {
            // The owner projection includes the original wording. Shared projections
            // omit it, so it cannot contribute matches across the sharing boundary.
            let candidates = words(&format!(
                "{} {} {}",
                v["key"].as_str().unwrap_or(""),
                v["value"],
                v["evidence"].as_str().unwrap_or("")
            ));
            !requested.is_empty() && requested.is_subset(&candidates)
        })
        .take(25)
        .cloned()
        .collect())
}

pub fn search_owner(db: &Connection, query: &str, task: Option<&str>) -> Result<Value> {
    let mut results = if task.is_none() {
        match_statements(db, query, false)?
    } else {
        Vec::new()
    };
    let tokens = words(query);
    if tokens.is_empty() {
        return Ok(json!({"results":results,"retrieval":"lexical"}));
    }
    let expr = tokens
        .iter()
        .take(24)
        .map(|s| format!("\"{s}\""))
        .collect::<Vec<_>>()
        .join(" AND ");
    let mut statement = db.prepare(
        "SELECT r.handle,r.event_id,r.task_id,r.actor_kind,r.occurred_at FROM recall_fts JOIN recall_events r ON r.handle=recall_fts.handle
         WHERE recall_fts MATCH ?1 AND (?2 IS NULL OR r.task_id=?2) ORDER BY rank, r.event_id LIMIT 30")?;
    let hits = statement.query_map(params![expr, task], |r| Ok(json!({
        "handle":r.get::<_,String>(0)?,"event_id":r.get::<_,String>(1)?,"task_id":r.get::<_,Option<String>>(2)?,
        "actor_kind":r.get::<_,String>(3)?,"at":r.get::<_,Option<String>>(4)?
    })))?.collect::<rusqlite::Result<Vec<Value>>>()?;
    for hit in hits {
        if visible(db, field(&hit, "event_id")?)? {
            results.push(hit);
        }
    }
    Ok(json!({"results":results,"retrieval":"lexical"}))
}

pub fn expand(db: &Connection, handle: &str) -> Result<Value> {
    let row = db.query_row("SELECT event_id,body,encoding,actor_kind,occurred_at,task_id FROM recall_events WHERE handle=?1",[handle],
        |r| Ok((r.get::<_,String>(0)?,r.get::<_,Vec<u8>>(1)?,r.get::<_,i64>(2)?,r.get::<_,String>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,Option<String>>(5)?)))
        .optional()?.ok_or_else(||Error::missing("evidence is unavailable"))?;
    if !visible(db, &row.0)? {
        return Err(Error::missing("evidence is unavailable"));
    }
    Ok(
        json!({"handle":handle,"event_id":row.0,"text":event_body(row.1,row.2)?,"actor_kind":row.3,"at":row.4,"task_id":row.5}),
    )
}

pub fn handoff(db: &Connection, task_id: &str) -> Result<Value> {
    task_package(db, task_id, None)
}

pub fn for_client(db: &Connection, task_id: &str, client_id: &str) -> Result<Value> {
    task_package(db, task_id, Some(client_id))
}

pub fn expand_for_client(db: &Connection, handle: &str, client_id: &str) -> Result<Value> {
    let admitted = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM recall_events r JOIN recall_grants g ON r.source_id=g.source_id
           JOIN context_task_access t ON r.task_id=t.task_id AND t.client_id=g.client_id
           WHERE r.handle=?1 AND g.client_id=?2)",[handle,client_id],|r|r.get::<_,bool>(0))?;
    if !admitted {
        return Err(Error::missing("evidence is unavailable"));
    }
    expand(db, handle)
}

fn task_package(db: &Connection, task_id: &str, client: Option<&str>) -> Result<Value> {
    let task = context::tasks(db)?["tasks"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|row| row["task_id"] == task_id)
        .cloned()
        .ok_or_else(|| Error::missing("task is unavailable"))?;
    let mut cursor = db.prepare("SELECT r.handle FROM recall_events r WHERE r.task_id=?1 AND (?2 IS NULL OR EXISTS(
      SELECT 1 FROM recall_grants g JOIN context_task_access t ON t.client_id=g.client_id AND t.task_id=r.task_id
      WHERE g.source_id=r.source_id AND g.client_id=?2)) ORDER BY r.occurred_at,r.event_id LIMIT 50")?;
    let handles = cursor
        .query_map(params![task_id, client], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut evidence = Vec::new();
    let mut observations = Vec::new();
    for handle in handles {
        let item = match expand(db, &handle) {
            Ok(item) => item,
            Err(e) if e.status == 404 => continue,
            Err(e) => return Err(e),
        };
        let mut q = db.prepare("SELECT span_id,role,field,value FROM authority_observations WHERE event_id=?1 ORDER BY field,span_id")?;
        for row in q.query_map([field(&item, "event_id")?], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })? {
            let (span, role, field, value) = row?;
            observations.push(
                json!({"event_id":item["event_id"],"handle":handle,"span_id":span,
                "field":field,"actor_kind":role,"value":serde_json::from_str::<Value>(&value)?}),
            );
        }
        evidence.push(item);
    }
    Ok(
        json!({"task":task,"evidence":evidence,"observations":observations,"verification":"per_source_observation"}),
    )
}

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| Error::invalid(format!("missing {key}")))
}

pub fn operate(
    db: &mut Connection,
    preview: bool,
    key: &str,
    request: Value,
    model_route: Option<&str>,
) -> Result<Value> {
    let tx = db.transaction()?;
    let result = operate_committed(&tx, preview, key, request, model_route)?;
    tx.commit()?;
    Ok(result)
}

fn operate_committed(
    db: &Connection,
    preview: bool,
    key: &str,
    request: Value,
    model_route: Option<&str>,
) -> Result<Value> {
    let kind = field(&request, "kind")?;
    let mut command = request.clone();
    let id = command
        .as_object_mut()
        .ok_or_else(|| Error::invalid("operation request must be an object"))?
        .remove("preview_id");
    let encoded = serde_json::to_string(&command)?;
    if !preview {
        let replay = db
            .query_row(
                "SELECT request,response FROM governance_operations WHERE request_key=?1",
                [key],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        if let Some((old, result)) = replay {
            return if old == encoded {
                Ok(serde_json::from_str(&result)?)
            } else {
                Err(Error::new(
                    409,
                    "key_conflict",
                    "operation key already used with another request",
                ))
            };
        }
    }
    let footprint = match kind {
        "change_grant" => json!({"clients":context::clients(db)?,"tasks":context::tasks(db)?,"sources":ingestion::list(db,None)?,
            "operations":db.query_row("SELECT COUNT(*) FROM governance_operations", [], |r|r.get::<_,i64>(0))?,
            "model_route":if matches!(command["purpose"].as_str(),Some("extract"|"embed")) { model_route } else { None }}),
        "stop_capture" | "resume_capture" | "forget" => json!({"sources":ingestion::list(db,None)?,"statements":authority::current(db,None,None,None,false)?["change_seq"]}),
        _ => return Err(Error::missing("governance action is unavailable")),
    }.to_string();
    if preview {
        let id = Uuid::new_v4().to_string();
        db.execute(
            "INSERT INTO governance_previews(id,request,footprint) VALUES(?1,?2,?3)",
            params![id, encoded, footprint],
        )?;
        return Ok(json!({"preview_id":id,"kind":kind,"scope":command,"effective":false}));
    }
    if matches!(kind, "change_grant" | "forget") {
        let preview_id = id
            .as_ref()
            .and_then(Value::as_str)
            .ok_or_else(|| Error::invalid("operation requires a preview"))?;
        let pre = db
            .query_row(
                "SELECT request,footprint FROM governance_previews WHERE id=?1",
                [preview_id],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()?;
        if pre.as_ref() != Some(&(encoded.clone(), footprint)) {
            return Err(Error::conflict(
                "operation target or state changed; request a fresh preview",
            ));
        }
    }
    let result = match kind {
        "change_grant" => {
            if let Some(source_id) = request["source_id"].as_str() {
                let allow = request["allow"]
                    .as_bool()
                    .ok_or_else(|| Error::invalid("allow must be boolean"))?;
                match request["purpose"].as_str() {
                    Some("extract") => ingestion::set_extract(db, source_id, allow, model_route)?,
                    Some("embed") => {
                        let route = if allow {
                            Some(model_route.ok_or_else(|| {
                                Error::new(
                                    409,
                                    "model_not_ready",
                                    "请先开启向量检索，再测试并绑定本机嵌入模型。",
                                )
                            })?)
                        } else {
                            None
                        };
                        crate::vector::set_route(db, source_id, route)?;
                        json!({"source_id":source_id,"embed_enabled":allow})
                    }
                    Some("task_delivery") => {
                        let client_id = field(&request, "client_id")?;
                        if allow {
                            db.execute(
                                "INSERT OR IGNORE INTO recall_grants VALUES(?1,?2)",
                                params![source_id, client_id],
                            )?;
                        } else {
                            db.execute(
                                "DELETE FROM recall_grants WHERE source_id=?1 AND client_id=?2",
                                params![source_id, client_id],
                            )?;
                        }
                        json!({"source_id":source_id,"client_id":client_id,"task_delivery":allow})
                    }
                    _ => {
                        return Err(Error::invalid(
                            "source grant purpose must be extract, embed, or task_delivery",
                        ));
                    }
                }
            } else {
                let client = field(&request, "client_id")?;
                if let Some(shared) = request["share_statements"].as_bool() {
                    context::set_shared(db, client, shared)?
                } else {
                    context::set_task_access(
                        db,
                        field(&request, "task_id")?,
                        client,
                        request["allow"]
                            .as_bool()
                            .ok_or_else(|| Error::invalid("allow must be boolean"))?,
                    )?
                }
            }
        }
        "stop_capture" | "resume_capture" => {
            ingestion::set_capture(db, field(&request, "source_id")?, kind == "resume_capture")?
        }
        "forget" => {
            if let Some(id) = request["target"]["statement_id"].as_str() {
                authority::revoke_statement(db, id)?
            } else {
                ingestion::forget(db, field(&request["target"], "source_id")?)?
            }
        }
        _ => unreachable!(),
    };
    let operation_id = format!("op_{}", Uuid::new_v4().simple());
    let out = json!({"operation_id":operation_id,"kind":kind,"effective":true,"cleanup_state":"logical_effect_applied","result":result});
    db.execute(
        "INSERT INTO governance_operations(id,request_key,request,response) VALUES(?1,?2,?3,?4)",
        params![operation_id, key, encoded, out.to_string()],
    )?;
    if let Some(id) = id.as_ref().and_then(Value::as_str) {
        db.execute("DELETE FROM governance_previews WHERE id=?1", [id])?;
    }
    Ok(out)
}

pub fn operations(db: &Connection) -> Result<Value> {
    let mut statement =
        db.prepare("SELECT response FROM governance_operations ORDER BY rowid DESC LIMIT 100")?;
    let records = statement
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let records = records
        .iter()
        .map(|record| serde_json::from_str::<Value>(record))
        .collect::<serde_json::Result<Vec<_>>>()?;
    Ok(json!({"operations":records}))
}
