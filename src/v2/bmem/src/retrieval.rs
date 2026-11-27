use crate::error::{Error, Result};
use crate::{authority, context, ingestion};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
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
        CREATE TABLE IF NOT EXISTS recall_root_denials(
            client_id TEXT NOT NULL REFERENCES context_clients(id),
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id),
            PRIMARY KEY(client_id,root_id)
        );
        CREATE TABLE IF NOT EXISTS recall_cooled_roots (root_id TEXT PRIMARY KEY REFERENCES ing_roots(root_id));
        CREATE TABLE IF NOT EXISTS governance_owner_read_denials (
            root_id TEXT PRIMARY KEY REFERENCES ing_roots(root_id)
        );
        CREATE TABLE IF NOT EXISTS recall_tool_links (handle TEXT PRIMARY KEY REFERENCES recall_events(handle) ON DELETE CASCADE,
            run TEXT NOT NULL,call TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('call','result','plan')));
        CREATE INDEX IF NOT EXISTS recall_tool_links_run ON recall_tool_links(run,call,role);
        CREATE TABLE IF NOT EXISTS governance_discussion_roots (discussion_id TEXT NOT NULL,
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id), PRIMARY KEY(discussion_id,root_id));
        CREATE TABLE IF NOT EXISTS governance_previews (
            id TEXT PRIMARY KEY, request TEXT NOT NULL, footprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS governance_comparison_snapshots (
            preview_id TEXT PRIMARY KEY REFERENCES governance_previews(id) ON DELETE CASCADE,
            snapshot_digest TEXT NOT NULL,
            model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls = 0)
        );
        CREATE TABLE IF NOT EXISTS governance_operations (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL
        );")?;
    let has_kind: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('recall_events') WHERE name='feishu_kind')",
        [],
        |row| row.get(0),
    )?;
    if !has_kind {
        db.execute_batch("ALTER TABLE recall_events ADD COLUMN feishu_kind TEXT")?;
    }
    Ok(())
}

pub fn record_feishu_kind(db: &Connection, receipt: &Value, kind: &str) -> Result<()> {
    // Re-observed duplicates acquire provenance without changing their event identity or index.
    db.execute(
        "UPDATE recall_events SET feishu_kind=?1 WHERE feishu_kind IS NULL AND event_id IN
         (SELECT json_extract(value,'$.event_id') FROM json_each(?2))",
        params![kind, receipt["events"].to_string()],
    )?;
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

pub(crate) fn fts_expression(query: &str) -> String {
    words(query)
        .iter()
        .take(24)
        .map(|token| format!("\"{token}\""))
        .collect::<Vec<_>>()
        .join(" AND ")
}

// In an unsegmented CJK query, adjacent characters can belong to separate
// search terms. Require their presence without requiring that artificial pair.
fn task_fts_expression(query: &str) -> String {
    let words = words(query);
    words
        .iter()
        .filter(|word| {
            let mut chars = word.chars();
            let first = chars.next().unwrap_or_default();
            !('\u{3400}'..='\u{9fff}').contains(&first) || chars.next().is_none()
        })
        .take(24)
        .map(|token| format!("\"{token}\""))
        .collect::<Vec<_>>()
        .join(" AND ")
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

pub fn rebuild_lexical(db: &mut Connection) -> Result<Value> {
    let tx = db.transaction()?;
    tx.execute("DELETE FROM recall_fts", [])?;
    let count = {
        let mut reading = tx.prepare(
            "SELECT r.handle,r.body,r.encoding FROM recall_events r
            JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1 ORDER BY r.handle",
        )?;
        let mut writing = tx.prepare("INSERT INTO recall_fts(handle,terms) VALUES(?1,?2)")?;
        let mut rows = reading.query([])?;
        let mut count = 0;
        while let Some(row) = rows.next()? {
            let original = event_body(row.get(1)?, row.get(2)?)?;
            writing.execute(params![row.get::<_, String>(0)?, terms(&original)])?;
            count += 1;
        }
        count
    };
    tx.commit()?;
    Ok(json!({"rebuilt":count}))
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

#[derive(Default)]
pub struct SearchExclusions {
    omit_statements: bool,
    pub evidence: Option<String>,
}

impl SearchExclusions {
    pub fn parse(value: Option<&Value>) -> Result<Self> {
        let Some(value) = value else {
            return Ok(Self::default());
        };
        let items = value
            .as_array()
            .ok_or_else(|| Error::invalid("exclude must be a list of search categories"))?;
        let (mut sources, mut actors, mut kinds) = (Vec::new(), Vec::new(), Vec::new());
        let mut omit_statements = false;
        for item in items {
            match item.as_str() {
                Some(key @ ("feishu" | "codex" | "claude-code" | "dsh")) => sources.push(key),
                Some(key @ ("user_message" | "assistant_message" | "tool_result")) => {
                    actors.push(key)
                }
                Some(key @ ("group" | "dm" | "docs" | "feishu_unclassified")) => kinds.push(key),
                Some("statement") => omit_statements = true,
                _ => return Err(Error::invalid("unknown search exclusion")),
            }
        }
        let evidence = (!sources.is_empty() || !actors.is_empty() || !kinds.is_empty())
            .then(|| json!({"sources": sources, "actors": actors, "kinds": kinds}).to_string());
        Ok(Self {
            omit_statements,
            evidence,
        })
    }
}

// Both retrieval modes apply this predicate before ranking or limiting evidence.
pub const INCLUDED_EVIDENCE: &str = "(?3 IS NULL OR (
    s.kind NOT IN (SELECT value FROM json_each(?3,'$.sources')) AND
    r.actor_kind NOT IN (SELECT value FROM json_each(?3,'$.actors')) AND
    (s.kind <> 'feishu' OR coalesce(r.feishu_kind, CASE
        WHEN e.native_object GLOB 'doc:*' OR e.native_object GLOB 'docx:*' THEN 'docs'
        ELSE 'feishu_unclassified' END) NOT IN (SELECT value FROM json_each(?3,'$.kinds')))
))";

pub fn search_shared(db: &Connection, query: &str, exclude: &SearchExclusions) -> Result<Value> {
    let results = if exclude.omit_statements {
        Vec::new()
    } else {
        match_statements(db, query, true)?
    };
    Ok(json!({"results":results,"retrieval":"lexical"}))
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
            let key = v["key"].as_str().unwrap_or("");
            let label = match key {
                "report.language" => "报告语言",
                "report.opening" => "报告开头",
                "code_example.language" => "代码示例语言",
                _ => "",
            };
            let candidates = words(&format!(
                "{key} {label} {} {}",
                v["value"],
                v["evidence"].as_str().unwrap_or("")
            ));
            !requested.is_empty() && requested.is_subset(&candidates)
        })
        .take(25)
        .cloned()
        .collect())
}

pub fn search_owner(
    db: &Connection,
    query: &str,
    task: Option<&str>,
    exclude: &SearchExclusions,
) -> Result<Value> {
    let mut results = if task.is_none() && !exclude.omit_statements {
        match_statements(db, query, false)?
    } else {
        Vec::new()
    };
    let expr = fts_expression(query);
    if expr.is_empty() {
        return Ok(json!({"results":results,"retrieval":"lexical"}));
    }
    let mut statement = db.prepare(&format!(
        "SELECT r.handle,r.event_id,r.task_id,r.actor_kind,r.occurred_at
         FROM recall_fts JOIN recall_events r ON r.handle=recall_fts.handle
         JOIN ing_event_versions e ON e.event_id=r.event_id JOIN ing_sources s ON s.source_id=r.source_id
         WHERE recall_fts MATCH ?1 AND (?2 IS NULL OR r.task_id=?2) AND e.active=1 AND {INCLUDED_EVIDENCE}
           AND NOT EXISTS(SELECT 1 FROM governance_owner_read_denials d WHERE d.root_id=e.root_id)
         ORDER BY rank, r.event_id LIMIT 30"))?;
    let hits = statement.query_map(params![expr, task, exclude.evidence], |r| Ok(json!({
        "handle":r.get::<_,String>(0)?,"event_id":r.get::<_,String>(1)?,"task_id":r.get::<_,Option<String>>(2)?,
        "actor_kind":r.get::<_,String>(3)?,"at":r.get::<_,Option<String>>(4)?
    })))?.collect::<rusqlite::Result<Vec<Value>>>()?;
    results.extend(hits);
    Ok(json!({"results":results,"retrieval":"lexical"}))
}

pub fn hybrid_owner(
    db: &Connection,
    query: &str,
    task: Option<&str>,
    exclude: &SearchExclusions,
    semantic: &[Value],
    limit: usize,
) -> Result<Value> {
    let lexical = search_owner(db, query, task, exclude)?;
    let lexical = lexical["results"].as_array().map_or(&[][..], Vec::as_slice);
    fuse_evidence(db, query, lexical, semantic, limit)
}

pub fn fuse_evidence(
    db: &Connection,
    query: &str,
    lexical: &[Value],
    semantic: &[Value],
    limit: usize,
) -> Result<Value> {
    let mut candidates = BTreeMap::<(&str, &str), (Value, usize, usize)>::new();
    for (rank, row) in lexical.iter().enumerate() {
        if let Some(handle) = row["handle"].as_str() {
            candidates
                .entry(("event", handle))
                .or_insert_with(|| (row.clone(), rank + 1, 0));
        } else if let Some(revision) = row["revision_id"].as_str() {
            candidates
                .entry(("statement", revision))
                .or_insert_with(|| (row.clone(), rank + 1, 0));
        }
    }
    for (rank, row) in semantic.iter().enumerate() {
        if let Some(handle) = row["handle"].as_str() {
            let item = candidates
                .entry(("event", handle))
                .or_insert_with(|| (row.clone(), 0, 0));
            if item.2 == 0 {
                item.2 = rank + 1;
            }
        }
    }
    let identifiers = query
        .split(|c| !identifier_char(c))
        .filter(|token| {
            token.chars().any(|c| c.is_ascii_digit())
                && token.chars().any(|c| c.is_ascii_alphabetic())
        })
        .map(str::to_ascii_lowercase)
        .collect::<BTreeSet<_>>();
    let mut fused = Vec::new();
    for ((kind, key), (mut row, lexical, semantic)) in candidates {
        let mut exact = false;
        if kind == "event" {
            let evidence = match expand(db, key) {
                Ok(item) => item,
                Err(error) if error.status == 404 => continue,
                Err(error) => return Err(error),
            };
            let quote = evidence["text"].as_str().unwrap_or_default();
            exact = quote
                .split(|c| !identifier_char(c))
                .any(|piece| identifiers.contains(&piece.to_ascii_lowercase()));
            row["quote"] = evidence["text"].clone();
            row["version"] = db
                .query_row(
                    "SELECT native_version FROM ing_event_versions WHERE event_id=?1",
                    [field(&evidence, "event_id")?],
                    |r| r.get::<_, Option<String>>(0),
                )?
                .into();
        }
        let mut channels = Vec::new();
        let mut ranks = serde_json::Map::new();
        if exact {
            channels.push("exact");
        }
        for (name, rank) in [("lexical", lexical), ("semantic", semantic)] {
            if rank > 0 {
                channels.push(name);
                ranks.insert(name.into(), rank.into());
            }
        }
        row["channels"] = json!(channels);
        row["ranks"] = json!(ranks);
        fused.push((exact, fused_score(60, lexical, semantic), key, row));
    }
    Ok(json!({"results":ranked_rows(fused,limit),"retrieval":"hybrid"}))
}

fn fused_score(k: usize, lexical: usize, semantic: usize) -> f64 {
    [lexical, semantic]
        .into_iter()
        .filter(|rank| *rank != 0)
        .map(|rank| 1.0 / (k + rank) as f64)
        .sum()
}

fn ranked_rows(mut fused: Vec<(bool, f64, &str, Value)>, limit: usize) -> Vec<Value> {
    fused.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| b.1.total_cmp(&a.1))
            .then_with(|| a.2.cmp(b.2))
    });
    fused
        .into_iter()
        .take(limit)
        .map(|(_, _, _, row)| row)
        .collect()
}

pub fn replay_ranked_channels(body: &Value) -> Result<Value> {
    let limit = body["candidate_limit"]
        .as_u64()
        .filter(|limit| (1..=250).contains(limit))
        .ok_or_else(|| Error::invalid("candidate_limit must be 1..250"))? as usize;
    let k = body["k"].as_u64().unwrap_or(60).clamp(1, 10000) as usize;
    let mut candidates = BTreeMap::<&str, [usize; 3]>::new();
    for (channel, index) in [("exact", 0), ("lexical", 1), ("semantic", 2)] {
        if let Some(rows) = body["channels"][channel].as_array() {
            if rows.len() > 250 {
                return Err(Error::invalid("channel has too many candidates"));
            }
            for (rank, key) in rows.iter().enumerate() {
                let key = key
                    .as_str()
                    .filter(|key| !key.is_empty())
                    .ok_or_else(|| Error::invalid("invalid candidate key"))?;
                let ranks = candidates.entry(key).or_default();
                if ranks[index] == 0 {
                    ranks[index] = rank + 1;
                }
            }
        }
    }
    Ok(
        json!({"results":ranked_rows(candidates.into_iter().map(|(key,[exact,lexical,semantic])|
        (exact!=0,fused_score(k,lexical,semantic),key,json!({"handle":key,"ranks":{"lexical":lexical,"semantic":semantic}})))
        .collect(),limit)}),
    )
}

fn identifier_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-' || c == '_'
}

pub fn lexical_for_client(
    db: &Connection,
    task: &str,
    client: &str,
    query: &str,
    limit: usize,
) -> Result<Vec<Value>> {
    let expression = task_fts_expression(query);
    if expression.is_empty() {
        return Ok(Vec::new());
    }
    let mut prepared = db.prepare(
        "SELECT r.handle,r.event_id FROM recall_fts JOIN recall_events r ON r.handle=recall_fts.handle
         JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1
         JOIN recall_grants a ON a.source_id=r.source_id AND a.client_id=?2
         JOIN context_task_access t ON t.task_id=r.task_id AND t.client_id=a.client_id
         WHERE recall_fts MATCH ?1 AND r.task_id=?3
           AND NOT EXISTS(SELECT 1 FROM recall_root_denials d WHERE d.client_id=?2 AND d.root_id=e.root_id)
         ORDER BY rank,r.handle LIMIT ?4",
    )?;
    Ok(prepared.query_map(params![expression,client,task,limit as i64], |r| {
        Ok(json!({"handle":r.get::<_,String>(0)?,"event_id":r.get::<_,String>(1)?}))
    })?.collect::<rusqlite::Result<_>>()?)
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

pub fn expand_for_owner(db: &Connection, handle: &str) -> Result<Value> {
    let root = db.query_row("SELECT e.root_id FROM recall_events r
        JOIN ing_event_versions e ON e.event_id=r.event_id WHERE r.handle=?1 AND e.active=1",
        [handle], |row| row.get::<_,String>(0)).optional()?
        .ok_or_else(|| Error::missing("evidence is unavailable"))?;
    if !owner_can_read_root(db, &root)? {
        return Err(Error::missing("evidence is unavailable"));
    }
    expand(db, handle)
}

pub fn handoff(db: &Connection, task_id: &str) -> Result<Value> {
    task_package(db, task_id, None, None, None, None, None)
}

pub fn for_client(db: &Connection, task_id: &str, client_id: &str) -> Result<Value> {
    task_package(db, task_id, Some(client_id), None, None, None, None)
}

pub fn historical_handoff(
    db: &Connection,
    task_id: &str,
    client: Option<&str>,
    as_of: &str,
) -> Result<Value> {
    let date = chrono::DateTime::parse_from_rfc3339(as_of)
        .map_err(|_| Error::invalid("as_of must be an RFC3339 timestamp"))?
        .to_rfc3339();
    task_package(db, task_id, client, None, None, None, Some(&date))
}

pub fn matching_for_client(
    db: &Connection,
    task_id: &str,
    client_id: &str,
    query: &str,
    skip_event: Option<&str>,
) -> Result<Value> {
    let expression = task_fts_expression(query);
    task_package(
        db,
        task_id,
        Some(client_id),
        Some(&expression),
        skip_event,
        None,
        None,
    )
}

pub fn record_tool_link(db: &Connection, body: &Value) -> Result<Value> {
    let handle = field(body, "handle")?;
    let item = expand(db, handle)?;
    if item["task_id"].is_null() {
        return Err(Error::invalid("tool link requires task evidence"));
    }
    let run = field(body, "run_id")?;
    let role = field(body, "role")?;
    let call = body["tool_call_id"].as_str().unwrap_or_default();
    if run.len() > 200
        || call.len() > 200
        || !matches!(role, "call" | "result" | "plan")
        || (role == "result") != (item["actor_kind"] == "tool_result")
    {
        return Err(Error::invalid("tool link does not match the evidence role"));
    }
    let inserted = db.execute(
        "INSERT OR IGNORE INTO recall_tool_links VALUES (?1,?2,?3,?4)",
        params![handle, run, call, role],
    )?;
    if inserted==0 && !db.query_row("SELECT EXISTS(SELECT 1 FROM recall_tool_links WHERE handle=?1 AND run=?2 AND call=?3 AND role=?4)",
        params![handle,run,call,role],|row|row.get::<_,bool>(0))? {
        return Err(Error::conflict("tool link already bound to a different run"));
    }
    Ok(json!({"handle":handle,"run_id":run,"tool_call_id":call,"role":role}))
}

pub fn evidence_window(
    db: &Connection,
    task: &str,
    client: Option<&str>,
    query: &str,
    skip_event: Option<&str>,
    seed_limit: usize,
    slots: usize,
) -> Result<Value> {
    let exact_query = query
        .split(|c| !identifier_char(c))
        .find(|word| {
            word.as_bytes().first().is_some_and(u8::is_ascii_uppercase)
                && word.chars().any(|c| c.is_ascii_digit())
                && word.chars().any(|c| c.is_ascii_alphabetic())
        })
        .unwrap_or(query);
    let expression = task_fts_expression(exact_query);
    let initial = task_package(db, task, client, Some(&expression), skip_event, None, None)?;
    let mut evidence = Vec::new();
    let mut seeds = Vec::new();
    let mut counts = 0;
    for row in initial["evidence"].as_array().into_iter().flatten() {
        if exact_query != query
            && !row["text"]
                .as_str()
                .unwrap_or_default()
                .split(|c| !identifier_char(c))
                .any(|word| word.eq_ignore_ascii_case(exact_query))
        {
            continue;
        }
        counts += 1;
        if evidence.len() < seed_limit.min(slots) {
            seeds.push(field(row, "handle")?.to_owned());
            evidence.push(row.clone());
        }
    }
    let additions = same_run_neighbors(db, task, client, &seeds)?;
    let evidence_gap = evidence.len() + additions.len() > slots;
    let expanded = additions
        .into_iter()
        .take(slots.saturating_sub(evidence.len()))
        .collect::<Vec<_>>();
    let expanded_handles: Vec<_> = expanded
        .iter()
        .filter_map(|row| row["handle"].as_str().map(str::to_owned))
        .collect();
    evidence.extend(expanded);
    let observations = initial["observations"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|row| seeds.iter().any(|handle| row["handle"] == handle.as_str()))
        .collect::<Vec<_>>();
    Ok(
        json!({"evidence":evidence,"observations":observations,"window":{
            "ranked_seed_refs":seeds,"expanded_refs":expanded_handles,"evidence_gap":evidence_gap,
            "recipient_visible_count":counts,"seed_reasons":vec!["directed_match"; seeds.len()],
            "result_status":"unknown",
        }}),
    )
}

pub fn same_run_neighbors(
    db: &Connection,
    task: &str,
    client: Option<&str>,
    seeds: &[String],
) -> Result<Vec<Value>> {
    let mut additions = BTreeMap::<String, Value>::new();
    for seed in seeds {
        let mut linked = db.prepare("SELECT target.handle FROM recall_tool_links start
            JOIN recall_events s ON s.handle=start.handle
            JOIN recall_tool_links target ON target.run=start.run
              AND ((target.role='result' AND start.call!='' AND target.call=start.call) OR (target.role='plan' AND start.call=''))
            JOIN recall_events r ON r.handle=target.handle AND r.task_id=?1 AND r.source_id=s.source_id
            JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1
            WHERE start.handle=?2 AND target.handle<>start.handle ORDER BY r.occurred_at DESC,target.handle")?;
        for row in linked.query_map(params![task, seed], |row| row.get::<_, String>(0))? {
            let handle = row?;
            if seeds.contains(&handle) || additions.contains_key(&handle) {
                continue;
            }
            if let Some(client) = client
                && !client_can_expand(db, &handle, client, Some(task))?
            {
                continue;
            }
            let item = if client.is_some() {
                expand(db, &handle)?
            } else {
                match expand_for_owner(db,&handle) {
                    Ok(item) =>item,
                    Err(error) if error.status==404 => continue,
                    Err(error)=>return Err(error),
                }
            };
            additions.insert(handle, item);
        }
    }
    Ok(additions.into_values().collect())
}

pub fn client_can_expand(
    db: &Connection,
    handle: &str,
    client_id: &str,
    task_id: Option<&str>,
) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM recall_events r JOIN recall_grants g ON r.source_id=g.source_id
           JOIN context_task_access t ON r.task_id=t.task_id AND t.client_id=g.client_id
           JOIN ing_event_versions e ON r.event_id=e.event_id
           WHERE r.handle=?1 AND g.client_id=?2 AND e.active=1 AND (?3 IS NULL OR r.task_id=?3)
           AND NOT EXISTS(SELECT 1 FROM recall_root_denials d
             WHERE d.client_id=?2 AND d.root_id=e.root_id))",
        params![handle, client_id, task_id],
        |r| r.get(0),
    )?)
}

pub fn expand_for_client(db: &Connection, handle: &str, client_id: &str) -> Result<Value> {
    if !client_can_expand(db, handle, client_id, None)? {
        return Err(Error::missing("evidence is unavailable"));
    }
    expand(db, handle)
}

fn task_package(
    db: &Connection,
    task_id: &str,
    client: Option<&str>,
    expression: Option<&str>,
    skip_event: Option<&str>,
    excluded_root: Option<&str>,
    as_of: Option<&str>,
) -> Result<Value> {
    task_package_with_view(db, task_id, client, expression, skip_event, excluded_root, as_of, client.is_none())
}

fn task_package_with_view(
    db: &Connection,
    task_id: &str,
    client: Option<&str>,
    expression: Option<&str>,
    skip_event: Option<&str>,
    excluded_root: Option<&str>,
    as_of: Option<&str>,
    owner_view: bool,
) -> Result<Value> {
    let task = context::tasks(db)?["tasks"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|row| row["task_id"] == task_id)
        .cloned()
        .ok_or_else(|| Error::missing("task is unavailable"))?;
    if expression == Some("") {
        return Ok(
            json!({"task":task,"evidence":[],"observations":[],"verification":"per_source_observation"}),
        );
    }
    let mut cursor = db.prepare(
        "WITH admitted AS (
           SELECT r.handle,r.event_id,r.occurred_at,r.actor_kind
           FROM recall_events r JOIN ing_event_versions e ON e.event_id=r.event_id
           WHERE r.task_id=?1 AND e.active=1 AND (?4 IS NULL OR r.event_id<>?4)
             AND (?5 IS NULL OR e.root_id<>?5)
             AND (?6 IS NULL OR julianday(e.occurred_at)<=julianday(?6))
             AND (NOT ?7 OR NOT EXISTS(SELECT 1 FROM governance_owner_read_denials d WHERE d.root_id=e.root_id))
             AND (?3 IS NOT NULL OR ?6 IS NOT NULL OR NOT EXISTS(
               SELECT 1 FROM recall_cooled_roots c WHERE c.root_id=e.root_id))
             AND (?2 IS NULL OR EXISTS(
               SELECT 1 FROM recall_grants g JOIN context_task_access t
                 ON t.client_id=g.client_id AND t.task_id=r.task_id
               WHERE g.source_id=r.source_id AND g.client_id=?2
               AND NOT EXISTS(SELECT 1 FROM recall_root_denials d
                 WHERE d.client_id=?2 AND d.root_id=e.root_id)))
             AND (?3 IS NULL OR r.handle IN (
               SELECT handle FROM recall_fts WHERE recall_fts MATCH ?3))
         ), latest_check AS (
           SELECT handle FROM admitted WHERE actor_kind='tool_result'
           ORDER BY occurred_at DESC,event_id DESC LIMIT 1
         ), selected AS (
           SELECT * FROM admitted ORDER BY handle IN (SELECT handle FROM latest_check) DESC,
             occurred_at DESC,event_id DESC LIMIT 50
         )
         SELECT handle FROM selected ORDER BY occurred_at,event_id",
    )?;
    let handles = cursor
        .query_map(
            params![
                task_id,
                client,
                expression,
                skip_event,
                excluded_root,
                as_of,
                owner_view
            ],
            |r| r.get::<_, String>(0),
        )?
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
        "deny_discussion" => {
            let reader = field(command, "client_id")?;
            let name = field(command, "discussion_id")?;
            if command["purpose"] != "task_delivery" || !client_exists(db, reader)? {
                return Err(Error::invalid(
                    "discussion restriction requires a registered reader and task_delivery",
                ));
            }
            let mut query = db.prepare("SELECT root_id FROM governance_discussion_roots WHERE discussion_id=?1 ORDER BY root_id")?;
            let selected = query
                .query_map([name], |row| row.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            if selected.is_empty() {
                return Err(Error::missing("discussion has no registered roots"));
            }
            let rows = selected
                .iter()
                .map(|root| {
                    grant_footprint(
                        db,
                        &json!({"root_id":root,"client_id":reader,"purpose":"task_delivery"}),
                        None,
                    )
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(json!({"selected_roots":selected,"root_states":rows}))
        }
        "cool_episode" | "restore_default_handoff" => {
            let root = field(command, "root_id")?;
            let mut target = root_footprint(db, root)?;
            if target["withdrawn"] == true {
                return Err(Error::missing("source root is unavailable"));
            }
            let cooled: bool = db.query_row(
                "SELECT EXISTS(SELECT 1 FROM recall_cooled_roots WHERE root_id=?1)",
                [root],
                |row| row.get(0),
            )?;
            target["cooled"] = json!(cooled);
            Ok(target)
        }
        "forget" => {
            if let Some(statement) = command["target"]["statement_id"].as_str() {
                statement_footprint(db, statement)
            } else if let Some(source_id) = command["target"]["source_id"].as_str() {
                source_footprint(db, source_id)
            } else if let Some(root) = command["target"]["root_id"].as_str() {
                root_footprint(db, root)
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

pub fn attach_discussion_root(db: &Connection, body: &Value) -> Result<Value> {
    let discussion = field(body, "discussion_id")?;
    let root = field(body, "root_id")?;
    if discussion.len() > 200 || root_footprint(db, root)?["withdrawn"] == true {
        return Err(Error::invalid(
            "discussion identifier is too long or root has been withdrawn",
        ));
    }
    let added = db.execute(
        "INSERT OR IGNORE INTO governance_discussion_roots VALUES (?1,?2)",
        params![discussion, root],
    )?;
    Ok(json!({"discussion_id":discussion,"root_id":root,"added":added != 0}))
}

fn root_footprint(db: &Connection, root: &str) -> Result<Value> {
    let source: String = db
        .query_row(
            "SELECT source_id FROM ing_roots WHERE root_id=?1",
            [root],
            |r| r.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("source root is unavailable"))?;
    let mut query = db.prepare(
        "SELECT event_id FROM ing_event_versions WHERE root_id=?1 AND active=1 ORDER BY event_id",
    )?;
    let versions = query
        .query_map([root], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let withdrawn: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_root_revocations WHERE root_id=?1)",
        [root],
        |r| r.get(0),
    )?;
    Ok(json!({"source_id":source,"versions":versions,"withdrawn":withdrawn}))
}

fn compare_root_change(db: &Connection, command: &Value, comparison: &Value) -> Result<Value> {
    let root = command["root_id"]
        .as_str()
        .or_else(|| command["target"]["root_id"].as_str())
        .ok_or_else(|| Error::invalid("comparison requires one source root"))?;
    let change = match command["kind"].as_str() {
        Some("forget") => None,
        Some("change_grant")
            if command["purpose"] == "task_delivery" && command["allow"] == false =>
        {
            Some(field(command, "client_id")?)
        }
        _ => {
            return Err(Error::invalid(
                "comparison supports root forget and restriction",
            ));
        }
    };
    let task = field(comparison, "task_id")?;
    let expression = task_fts_expression(field(comparison, "query")?);
    let readers = comparison["reader_ids"]
        .as_array()
        .filter(|list| list.len() == 2)
        .ok_or_else(|| Error::invalid("comparison requires two reader_ids"))?;
    let budget = match comparison.get("budget") {
        Some(value) => value.as_u64().filter(|n| (1..=262_144).contains(n))
            .ok_or_else(|| Error::invalid("comparison evidence budget must be between 1 and 262144"))? as usize,
        None => 4096,
    };
    let mut results = serde_json::Map::new();
    for reader in readers {
        let reader = reader
            .as_str()
            .ok_or_else(|| Error::invalid("invalid comparison reader"))?;
        if results.contains_key(reader) || !client_exists(db, reader)? {
            return Err(Error::invalid(
                "comparison requires two distinct registered readers",
            ));
        }
        let select = |excluded| -> Result<Vec<Value>> {
            let package = task_package_with_view(
                db,
                task,
                Some(reader),
                Some(&expression),
                None,
                excluded,
                None,
                true,
            )?;
            Ok(context::fit_evidence(
                &package,
                task,
                "data",
                context::EvidenceBudget::ExcerptTokens(budget),
            )?
            .evidence)
        };
        let before = select(None)?;
        let after = if change.is_none_or(|target| target == reader) {
            select(Some(root))?
        } else {
            before.clone()
        };
        results.insert(reader.into(), json!({"before":before,"after":after}));
    }
    Ok(Value::Object(results))
}

fn grant_footprint(db: &Connection, command: &Value, model_route: Option<&str>) -> Result<Value> {
    if let Some(root) = command["root_id"].as_str() {
        if command["purpose"] == "owner_read" && command.get("client_id").is_none() {
            let mut footprint = root_footprint(db, root)?;
            footprint["owner_read_denied"] = json!(!owner_can_read_root(db, root)?);
            return Ok(footprint);
        }
        let client = field(command, "client_id")?;
        if command["purpose"] != "task_delivery" || !client_exists(db, client)? {
            return Err(Error::invalid(
                "root grants require a registered client and task_delivery",
            ));
        }
        let mut footprint = root_footprint(db, root)?;
        footprint["root_denied"] = json!(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM recall_root_denials WHERE client_id=?1 AND root_id=?2)",
            params![client, root],
            |row| row.get::<_, bool>(0),
        )?);
        footprint["source_granted"] = json!(db.query_row(
            "SELECT EXISTS(SELECT 1 FROM recall_grants WHERE client_id=?1 AND source_id=?2)",
            params![client, footprint["source_id"].as_str()],
            |row| row.get::<_, bool>(0),
        )?);
        return Ok(footprint);
    }
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

fn owner_can_read_root(db: &Connection, root: &str) -> Result<bool> {
    Ok(db.query_row("SELECT NOT EXISTS(SELECT 1 FROM governance_owner_read_denials WHERE root_id=?1)",
        [root], |r| r.get(0))?)
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
    let forgotten_root = (!preview
        && request["kind"].as_str() == Some("forget")
        && request["target"]["statement_id"].as_str().is_none())
        .then(|| request["target"]["root_id"].as_str().map(str::to_owned))
        .flatten();
    let tx = db.transaction()?;
    let result = operate_committed(&tx, preview, key, request, model_route)?;
    tx.commit()?;
    if let Some(root) = forgotten_root {
        // A physical cleanup failure must never roll back the read cutoff.
        crate::governance::start_vector_cleanup(db, &root);
    }
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
    let comparison = command
        .as_object_mut()
        .and_then(|object| object.remove("comparison"));
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
    let state = preview_footprint(db, kind, &command, model_route)?;
    let footprint = state.to_string();
    if preview {
        let comparison = comparison
            .map(|params| compare_root_change(db, &command, &params))
            .transpose()?;
        let id = Uuid::new_v4().to_string();
        db.execute(
            "INSERT INTO governance_previews(id,request,footprint) VALUES(?1,?2,?3)",
            params![id, encoded, footprint],
        )?;
        let mut output = json!({"preview_id":id,"kind":kind,"scope":command,"effective":false});
        if kind == "deny_discussion" {
            output["selected_roots"] = state["selected_roots"].clone();
        }
        if let Some(comparison) = comparison {
            use sha2::{Digest, Sha256};
            let snapshot = format!(
                "sha256:{:x}",
                Sha256::digest(serde_json::to_vec(&comparison)?)
            );
            db.execute(
                "INSERT INTO governance_comparison_snapshots(preview_id,snapshot_digest) VALUES(?1,?2)",
                params![id, snapshot],
            )?;
            output["comparison_snapshot"] = json!(snapshot);
            output["owner_visible_before_ids"] = json!(comparison.as_object().into_iter().flatten()
                .flat_map(|(_, result)| result["before"].as_array().into_iter().flatten())
                .filter_map(|row| row["event_id"].as_str()).collect::<BTreeSet<_>>());
            output["comparison"] = comparison;
        }
        return Ok(output);
    }
    if matches!(
        kind,
        "change_grant" | "forget" | "cool_episode" | "restore_default_handoff" | "deny_discussion"
    ) {
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
        "deny_discussion" => {
            db.execute(
                "INSERT OR IGNORE INTO recall_root_denials(client_id,root_id)
                SELECT ?1,value FROM json_each(?2)",
                params![
                    field(&request, "client_id")?,
                    state["selected_roots"].to_string()
                ],
            )?;
            json!({"selected_roots":state["selected_roots"],"root_denied":true})
        }
        "cool_episode" | "restore_default_handoff" => {
            let root = field(&request, "root_id")?;
            let cooled = kind == "cool_episode";
            if cooled {
                db.execute(
                    "INSERT OR IGNORE INTO recall_cooled_roots VALUES (?1)",
                    [root],
                )?;
            } else {
                db.execute("DELETE FROM recall_cooled_roots WHERE root_id=?1", [root])?;
            }
            json!({"root_id":root,"cooled":cooled,"effect":"default_handoff"})
        }
        "change_grant" => {
            if let Some(root) = request["root_id"].as_str() {
                let allow = request["allow"]
                    .as_bool()
                    .ok_or_else(|| Error::invalid("allow must be boolean"))?;
                if request["purpose"] == "owner_read" {
                    if allow {
                        db.execute("DELETE FROM governance_owner_read_denials WHERE root_id=?1", [root])?;
                    } else {
                        db.execute("INSERT OR IGNORE INTO governance_owner_read_denials VALUES(?1)", [root])?;
                    }
                    json!({"root_id":root,"owner_read":allow})
                } else {
                    let client = field(&request, "client_id")?;
                    if allow {
                        db.execute("DELETE FROM recall_root_denials WHERE client_id=?1 AND root_id=?2", params![client, root])?;
                    } else {
                        db.execute("INSERT OR IGNORE INTO recall_root_denials(client_id,root_id) VALUES(?1,?2)", params![client, root])?;
                    }
                    json!({"root_id":root,"client_id":client,"root_denied":!allow})
                }
            } else if let Some(source_id) = request["source_id"].as_str() {
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
            } else if let Some(root) = request["target"]["root_id"].as_str() {
                ingestion::forget_root(db, root)?
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
