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

/// Target-scoped state snapshot bound into a governance preview. Only the
/// rows the confirmed operation reads or writes are bound, so background
/// sync on other sources, unrelated governance traffic, task/client
/// registrations and declaration commits cannot invalidate a pending
/// preview. The confirm-time compare stays byte-exact: a genuine change to
/// the bound target state (the grant rows themselves, the approved model
/// destination, the forget target's own watermark or revision) still
/// returns 409 revision_changed. Global counters, other sources' cursors
/// and full task/client lists are deliberately not bound.
fn preview_footprint(
    db: &Connection,
    kind: &str,
    command: &Value,
    model_route: Option<&str>,
) -> Result<Value> {
    match kind {
        "change_grant" => grant_footprint(db, command, model_route),
        "forget" => {
            if let Some(statement) = command["target"]["statement_id"].as_str() {
                statement_footprint(db, statement)
            } else if let Some(source_id) = command["target"]["source_id"].as_str() {
                source_footprint(db, source_id)
            } else {
                Ok(json!({}))
            }
        }
        // stop/resume confirm without a preview compare; the stored snapshot
        // only documents the previewed target and never adds a conflict.
        "stop_capture" | "resume_capture" => match command["source_id"].as_str() {
            Some(source_id) => source_footprint(db, source_id),
            None => Ok(json!({})),
        },
        _ => Err(Error::missing("governance action is unavailable")),
    }
}

fn grant_footprint(db: &Connection, command: &Value, model_route: Option<&str>) -> Result<Value> {
    if let Some(source_id) = command["source_id"].as_str() {
        let mut snapshot = json!({ "source_exists": source_exists(db, source_id)? });
        match command["purpose"].as_str() {
            Some("extract") => {
                // Bind the grant row itself: source-side grant writes do not
                // advance policy_version, so the version alone is not enough.
                snapshot["extract_enabled"] = json!(grant_active(db, source_id, "extract")?);
                snapshot["source_model_route"] =
                    json!(route_fingerprint(db, "ing_model_routes", source_id)?);
                // The destination being approved; a rebind before confirm must conflict.
                snapshot["model_route"] = json!(model_route);
            }
            Some("embed") => {
                snapshot["source_vector_route"] =
                    json!(route_fingerprint(db, "ing_vector_routes", source_id)?);
                snapshot["model_route"] = json!(model_route);
            }
            Some("task_delivery") => {
                let client_id = command["client_id"].as_str().unwrap_or_default();
                snapshot["client_exists"] = json!(client_exists(db, client_id)?);
                snapshot["recall_grant"] = json!(db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM recall_grants WHERE source_id=?1 AND client_id=?2)",
                    params![source_id, client_id],
                    |r| r.get::<_, bool>(0),
                )?);
            }
            _ => {}
        }
        return Ok(snapshot);
    }
    let client_id = command["client_id"].as_str().unwrap_or_default();
    let mut snapshot = json!({ "client_exists": client_exists(db, client_id)? });
    if command["share_statements"].as_bool().is_some() {
        snapshot["shared_statements"] = json!(
            db.query_row(
                "SELECT shared FROM context_clients WHERE id=?1",
                [client_id],
                |r| r.get::<_, bool>(0),
            )
            .optional()?
        );
    } else if let Some(task_id) = command["task_id"].as_str() {
        snapshot["task_access"] = json!(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM context_task_access WHERE task_id=?1 AND client_id=?2)",
            params![task_id, client_id],
            |r| r.get::<_, bool>(0),
        )?);
    }
    Ok(snapshot)
}

/// Statement identity plus its own revisions and the current head revision
/// of every fact it touches: revocation or a superseding correction of the
/// target changes the snapshot, while unrelated declarations do not.
fn statement_footprint(db: &Connection, statement_id: &str) -> Result<Value> {
    let statement = db
        .query_row(
            "SELECT id,owner,project,share,effective,known,corrects_revision,corrects_attempt,revoked,original
             FROM authority_statements WHERE id=?1",
            [statement_id],
            |r| {
                Ok(json!({
                    "id": r.get::<_, String>(0)?,
                    "owner": r.get::<_, String>(1)?,
                    "project": r.get::<_, String>(2)?,
                    "share": r.get::<_, String>(3)?,
                    "effective": r.get::<_, String>(4)?,
                    "known": r.get::<_, String>(5)?,
                    "corrects_revision": r.get::<_, Option<String>>(6)?,
                    "corrects_attempt": r.get::<_, Option<String>>(7)?,
                    "revoked": r.get::<_, bool>(8)?,
                    "original": r.get::<_, Option<String>>(9)?,
                }))
            },
        )
        .optional()?;
    let mut revisions = Vec::new();
    let mut facts = BTreeSet::new();
    let mut query = db.prepare(
        "SELECT id,fact_id,value,previous,seq,state FROM authority_revisions WHERE statement_id=?1 ORDER BY seq,id",
    )?;
    for row in query.query_map([statement_id], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, Option<String>>(3)?,
            r.get::<_, i64>(4)?,
            r.get::<_, String>(5)?,
        ))
    })? {
        let (id, fact, value, previous, seq, state) = row?;
        facts.insert(fact.clone());
        revisions.push(
            json!({"id":id,"fact_id":fact,"value":value,"previous":previous,"seq":seq,"state":state}),
        );
    }
    let mut heads = Vec::new();
    for fact in facts {
        let head = db
            .query_row(
                "SELECT r.id FROM authority_revisions r JOIN authority_statements s ON s.id=r.statement_id
                 WHERE r.fact_id=?1 ORDER BY s.effective DESC, r.seq DESC, r.id DESC LIMIT 1",
                [&fact],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        heads.push(json!({"fact_id":fact,"head_revision":head}));
    }
    Ok(json!({"statement":statement,"revisions":revisions,"fact_heads":heads}))
}

/// Source identity, its OWN committed cursor/watermark and its grant state.
/// New objects on the same source legitimately change a forget cleanup scope
/// (re-preview required); other sources' cursors and the global change_seq
/// are not bound. `updated_at` bookkeeping is excluded. The accepted-page
/// watermark covers pages that commit objects without moving the cursor
/// (`next_cursor == cursor` is a valid acceptance): every newly committed
/// page inserts exactly one `ing_ingests` row, including same-cursor and
/// duplicate-only pages, while idempotent replays commit nothing and insert
/// nothing. Ingest rows are never deleted, so MAX(rowid) only advances.
fn source_footprint(db: &Connection, source_id: &str) -> Result<Value> {
    let source = db
        .query_row(
            "SELECT source_id,kind,account,namespace,categories,cursor,created_at
             FROM ing_sources WHERE source_id=?1",
            [source_id],
            |r| {
                Ok(json!({
                    "source_id": r.get::<_, String>(0)?,
                    "kind": r.get::<_, String>(1)?,
                    "account": r.get::<_, String>(2)?,
                    "namespace": r.get::<_, String>(3)?,
                    "categories": r.get::<_, String>(4)?,
                    "cursor": r.get::<_, String>(5)?,
                    "created_at": r.get::<_, String>(6)?,
                }))
            },
        )
        .optional()?;
    let mut grants = Vec::new();
    let mut query =
        db.prepare("SELECT purpose,active FROM ing_grants WHERE source_id=?1 ORDER BY purpose")?;
    for row in query.query_map([source_id], |r| {
        Ok(json!({"purpose":r.get::<_,String>(0)?,"active":r.get::<_,bool>(1)?}))
    })? {
        grants.push(row?);
    }
    let accepted_pages = db.query_row(
        "SELECT COALESCE(MAX(rowid),0) FROM ing_ingests WHERE source_id=?1",
        [source_id],
        |r| r.get::<_, i64>(0),
    )?;
    Ok(json!({
        "source": source,
        "grants": grants,
        "accepted_page_watermark": accepted_pages,
        "source_model_route": route_fingerprint(db, "ing_model_routes", source_id)?,
        "source_vector_route": route_fingerprint(db, "ing_vector_routes", source_id)?,
    }))
}

fn source_exists(db: &Connection, source_id: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_sources WHERE source_id=?1)",
        [source_id],
        |r| r.get(0),
    )?)
}

fn client_exists(db: &Connection, client_id: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM context_clients WHERE id=?1)",
        [client_id],
        |r| r.get(0),
    )?)
}

fn grant_active(db: &Connection, source_id: &str, purpose: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_grants WHERE source_id=?1 AND purpose=?2 AND active=1)",
        params![source_id, purpose],
        |r| r.get(0),
    )?)
}

fn route_fingerprint(db: &Connection, table: &str, source_id: &str) -> Result<Option<String>> {
    Ok(db
        .query_row(
            &format!("SELECT fingerprint FROM {table} WHERE source_id=?1"),
            [source_id],
            |r| r.get(0),
        )
        .optional()?)
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
    let footprint = preview_footprint(db, kind, &command, model_route)?.to_string();
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
                let source = field(&request["target"], "source_id")?;
                let result = ingestion::forget(db, source)?;
                crate::history_tasks::withdraw_source(db, source)?;
                result
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
