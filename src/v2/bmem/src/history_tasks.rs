//! T034 — durable history backfill tasks.
//!
//! Owner-requested backfills over a selected time window or all available
//! history within chosen categories, executed one page at a time. This
//! module is independent of the continuous history
//! sync in [`crate::history`]: it keeps its **own per-target watermarks**
//! (`history_task_items.cursor`) and never reads or writes the sync tables.
//! Both paths share the intake port: events produced by the injected `read`
//! adapter are passed untouched to [`crate::ingestion::accept_in`], so native
//! object/version normalization, deduplication, revocation, and extraction
//! queueing are exactly the continuous sync's.
//!
//! Contract highlights:
//!
//! * `create` is idempotent per (source, key): the same canonical request
//!   replays the existing task; a different request under the same key is a
//!   `key_conflict` and writes nothing.
//! * A page is committed as one transaction: accepted event versions, the
//!   shared source cursor, the per-target checkpoint, and the task counters
//!   move together, or nothing does. `read` itself runs **outside** the
//!   SQLite lock and transaction.
//! * Every page rechecks, inside the commit transaction, that the task is
//!   still running and capture is still granted. A cancel or a capture
//!   revocation during an in-flight read discards the page without advancing
//!   the checkpoint — restart and retry re-read it, and intake deduplication
//!   makes the re-read safe (counted as real duplicates).
//! * Errors are recorded per target ("就近分项"): one failing target does not
//!   stop the others. The task settles to `completed`, `partial` (some
//!   targets failed, some completed), or `failed` (no target completed).
//! * Restart takes over `running` tasks and items from their stored
//!   checkpoints; no page is skipped. Cancel and retry never drop completed
//!   targets or their counters; retry resumes failed/cancelled targets from
//!   their last committed checkpoint.
//!
//! Integration seams owned by other maintainers: the worker loop and HTTP
//! routes call [`run_next`] / [`preview`] / [`create`] / [`list`] / [`change`];
//! the `read` closure is the adapter bridge (Feishu CLI, host session files).

use crate::error::{Error, Result};
use crate::{ingestion, server::lock};
use chrono::{DateTime, SecondsFormat, Utc};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::sync::Mutex;
use uuid::Uuid;

/// Upper bound on targets per task; keeps one task's fan-out bounded.
const MAX_TARGETS: usize = 200;
/// Upper bound on a single target id (characters).
const MAX_TARGET_CHARS: usize = 512;
/// Per-task error ledger cap; newest entries are kept.
const ERRORS_KEPT: usize = 64;
/// Intake accepts 1..=100 events per page; larger adapter pages are chunked
/// inside the same transaction.
const EVENTS_PER_PAGE: usize = 100;

const KINDS: &[&str] = &["dm", "group", "docs", "sessions", "archive"];
const CONTENTS: &[&str] = &["messages", "all"];
const DISCOVER: &str = "@discover";
type DiscoveryPage = (Vec<(String, Option<Value>)>, Option<String>);

/// Create the task tables. Idempotent; sequenced after `ingestion::migrate`.
pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS history_task_jobs (
            task_id     TEXT PRIMARY KEY,
            source_id   TEXT NOT NULL REFERENCES ing_sources(source_id),
            kind        TEXT NOT NULL,              -- dm|group|docs|sessions
            targets     TEXT NOT NULL,              -- canonical JSON array, empty for full discovery
            content     TEXT NOT NULL,              -- messages|all
            from_at     TEXT,                       -- RFC3339; NULL for docs without window
            to_at       TEXT,
            state       TEXT NOT NULL,              -- queued|running|completed|partial|failed|cancelled
            accepted    INTEGER NOT NULL DEFAULT 0, -- cumulative new versions
            duplicates  INTEGER NOT NULL DEFAULT 0, -- cumulative intake duplicates
            failed      INTEGER NOT NULL DEFAULT 0, -- targets currently failed
            idem_key    TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            started_at  TEXT,
            finished_at TEXT,
            UNIQUE (source_id, idem_key)
         );
         CREATE TABLE IF NOT EXISTS history_task_items (
            task_id    TEXT NOT NULL REFERENCES history_task_jobs(task_id),
            ordinal    INTEGER NOT NULL,
            target     TEXT NOT NULL,
            state      TEXT NOT NULL,               -- pending|running|completed|failed|cancelled
            cursor     TEXT,                        -- opaque adapter checkpoint (JSON)
            accepted   INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,                        -- JSON {code,message}
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, target)
         );
         CREATE TABLE IF NOT EXISTS history_task_errors (
            error_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL REFERENCES history_task_jobs(task_id),
            target     TEXT NOT NULL,
            code       TEXT NOT NULL,
            message    TEXT NOT NULL,
            created_at TEXT NOT NULL
         );
         CREATE INDEX IF NOT EXISTS idx_history_task_errors
            ON history_task_errors (task_id, error_id);
         CREATE INDEX IF NOT EXISTS idx_history_task_pending
            ON history_task_items (task_id, ordinal) WHERE state IN ('pending','running');
         CREATE INDEX IF NOT EXISTS idx_history_task_order
            ON history_task_items (task_id, ordinal);
         CREATE TABLE IF NOT EXISTS history_task_batches (
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id),
            idem_key TEXT NOT NULL, kinds TEXT NOT NULL, jobs TEXT NOT NULL,
            PRIMARY KEY(source_id, idem_key)
         );
         CREATE TABLE IF NOT EXISTS history_job_pages (
            page_seq INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            target TEXT NOT NULL,
            ingest_id TEXT NOT NULL UNIQUE REFERENCES ing_ingests(ingest_id),
            FOREIGN KEY(task_id,target) REFERENCES history_task_items(task_id,target)
         );
         CREATE INDEX IF NOT EXISTS history_job_pages_order ON history_job_pages(task_id,page_seq);
         CREATE TABLE IF NOT EXISTS history_archive_submissions (
            task_id TEXT NOT NULL REFERENCES history_task_jobs(task_id),
            idem_key TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL,
            PRIMARY KEY(task_id,idem_key)
         );
         CREATE TABLE IF NOT EXISTS history_archive_relay (
            event_id TEXT PRIMARY KEY REFERENCES ing_event_versions(event_id),
            origin_event_id TEXT NOT NULL REFERENCES ing_event_versions(event_id)
         );",
    )?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Request validation (shared by preview and create)
// ---------------------------------------------------------------------------

struct Request {
    source_id: String,
    source_type: String,
    account: String,
    namespace: String,
    kind: String,
    targets: Vec<String>,
    from: Option<String>,
    to: Option<String>,
    content: String,
}

impl Request {
    fn fingerprint(&self) -> String {
        canonical(&json!({
            "source_id": self.source_id,
            "kind": self.kind,
            "targets": self.targets,
            "from": self.from,
            "to": self.to,
            "content": self.content,
        }))
    }
}

/// Whether the registered adapter can serve this task kind. Feishu accounts
/// read chats and docs; local host sources read their own session files.
fn compatible(source_type: &str, account: &str, kind: &str) -> bool {
    match source_type {
        "feishu" if account.starts_with("feishu-") => matches!(kind, "dm" | "group" | "docs"),
        "codex" | "claude-code" | "dsh" if account == "local" => kind == "sessions",
        "archive" => kind == "archive",
        _ => false,
    }
}

fn source_info(db: &Connection, source: &str) -> Result<(String, String, String)> {
    db.query_row(
        "SELECT kind, account, namespace FROM ing_sources WHERE source_id = ?1",
        [source],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )
    .optional()?
    .ok_or_else(|| Error::missing("历史导入来源未登记"))
}

fn validate(db: &Connection, body: &Value) -> Result<Request> {
    let obj = body
        .as_object()
        .ok_or_else(|| Error::invalid("历史任务请求必须是 JSON 对象"))?;
    let source_id = require_str(obj, "source_id")?.to_string();
    let (source_type, account, namespace) = source_info(db, &source_id)?;

    let kind = require_str(obj, "kind")?.to_string();
    if !KINDS.contains(&kind.as_str()) {
        return Err(Error::invalid(format!(
            "kind '{kind}' must be one of {KINDS:?}"
        )));
    }
    if !compatible(&source_type, &account, &kind) {
        return Err(Error::invalid(format!(
            "来源 '{source_id}'（{source_type}）不支持 '{kind}' 历史任务，请选择匹配的适配器"
        )));
    }

    let items = obj
        .get("targets")
        .and_then(Value::as_array)
        .ok_or_else(|| Error::invalid("targets 必须是非空字符串数组"))?;
    if items.is_empty() || items.len() > MAX_TARGETS {
        return Err(Error::invalid(format!(
            "targets 数量必须在 1 到 {MAX_TARGETS} 之间"
        )));
    }
    let mut targets: Vec<String> = Vec::new();
    for item in items {
        let target = item
            .as_str()
            .filter(|t| !t.trim().is_empty() && *t != DISCOVER)
            .ok_or_else(|| Error::invalid("targets 条目必须是非空字符串"))?;
        if target.chars().count() > MAX_TARGET_CHARS {
            return Err(Error::invalid(format!(
                "targets 条目长度不能超过 {MAX_TARGET_CHARS} 个字符"
            )));
        }
        if source_type == "feishu"
            && matches!(kind.as_str(), "dm" | "group")
            && !target.starts_with("oc_")
        {
            return Err(Error::invalid(format!(
                "飞书 '{kind}' 任务的 target 必须是 oc_ 开头的会话 ID"
            )));
        }
        let target = if kind == "docs" {
            crate::history_feishu::normalize_document_target(target)?
        } else {
            target.to_owned()
        };
        if !targets.contains(&target) {
            targets.push(target);
        }
    }

    let from = optional_time(obj, "from")?;
    let to = optional_time(obj, "to")?;
    match (&from, &to) {
        (Some(f), Some(t)) if f >= t => {
            return Err(Error::invalid(
                "时间窗口必须满足 from < to（区间为 [from, to)）",
            ));
        }
        (None, None) if !matches!(kind.as_str(), "docs" | "archive") => {
            return Err(Error::invalid(
                "只有 docs 和 archive 任务可以省略 from/to 时间窗口",
            ));
        }
        (Some(_), None) | (None, Some(_)) => {
            return Err(Error::invalid("from 与 to 必须同时提供或同时省略"));
        }
        _ => {}
    }

    let content = match obj.get("content") {
        None | Some(Value::Null) => "messages".to_string(),
        Some(Value::String(s)) if CONTENTS.contains(&s.as_str()) => s.clone(),
        Some(_) => {
            return Err(Error::invalid(format!(
                "content must be one of {CONTENTS:?}"
            )));
        }
    };

    Ok(Request {
        source_id,
        source_type,
        account,
        namespace,
        kind,
        targets,
        from,
        to,
        content,
    })
}

fn optional_time(obj: &Map<String, Value>, field: &str) -> Result<Option<String>> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => {
            let time = DateTime::parse_from_rfc3339(s)
                .map_err(|_| Error::invalid(format!("{field} 必须是带时区的 RFC3339 时间")))?;
            Ok(Some(
                time.with_timezone(&Utc)
                    .to_rfc3339_opts(SecondsFormat::Secs, true),
            ))
        }
        Some(_) => Err(Error::invalid(format!("{field} 必须是 RFC3339 字符串"))),
    }
}

// ---------------------------------------------------------------------------
// Owner-facing operations
// ---------------------------------------------------------------------------

/// Validate a task request without writing anything. The result is bound to
/// the selected source: identity fields come from the registered source row,
/// never from the request body, so the console can confirm the adapter match
/// before creating the task.
pub fn preview(db: &Connection, body: &Value) -> Result<Value> {
    let request = validate(db, body)?;
    let capture_enabled = capture_active(db, &request.source_id)?;
    Ok(json!({
        "source_id": request.source_id,
        "type": request.source_type,
        "account": request.account,
        "namespace": request.namespace,
        "kind": request.kind,
        "targets": request.targets,
        "target_count": request.targets.len(),
        "from": request.from,
        "to": request.to,
        "content": request.content,
        "capture_enabled": capture_enabled,
    }))
}

/// Create a durable backfill task. Idempotent per (source, key): the same
/// canonical request returns the existing task (in its current state); a
/// different request under a committed key is a `key_conflict`. The worker
/// actor (`owner`) is bound here so intake accepts the task's pages; capture
/// may still be off — the task stays queued until the grant is active.
pub fn create(db: &mut Connection, body: &Value, key: &str) -> Result<Value> {
    if key.trim().is_empty() {
        return Err(Error::invalid("idempotency key must not be empty"));
    }
    let request = validate(db, body)?;
    let fingerprint = request.fingerprint();
    let now = now_utc();
    let tx = db.transaction()?;
    if let Some((task_id, stored)) = tx
        .query_row(
            "SELECT task_id, fingerprint FROM history_task_jobs
             WHERE source_id = ?1 AND idem_key = ?2",
            params![request.source_id, key],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()?
    {
        if stored != fingerprint {
            return Err(Error::conflict(format!(
                "key_conflict: idempotency key '{key}' for source '{}' already created a different history task",
                request.source_id
            )));
        }
        let view = job_view(&tx, &task_id)?;
        tx.commit()?;
        return Ok(view);
    }
    let task_id = insert_task(&tx, &request, key, &fingerprint, &now)?;
    tx.commit()?;
    job_view(db, &task_id)
}

/// Queue every selected category together. No adapter is consulted until a
/// worker picks the committed discovery item.
pub fn create_all(db: &mut Connection, body: &Value, key: &str) -> Result<Value> {
    let obj = body
        .as_object()
        .ok_or_else(|| Error::invalid("历史任务请求必须是 JSON 对象"))?;
    if obj
        .keys()
        .any(|field| !matches!(field.as_str(), "source_id" | "kinds"))
    {
        return Err(Error::invalid("全量历史导入只接受来源和所选类别"));
    }
    let source_id = require_str(obj, "source_id")?.to_owned();
    let (source_type, account, namespace) = source_info(db, &source_id)?;
    let items = obj
        .get("kinds")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty() && items.len() <= KINDS.len())
        .ok_or_else(|| Error::invalid("请选择要导入的历史数据类别"))?;
    let mut kinds = Vec::with_capacity(items.len());
    for item in items {
        let kind = item
            .as_str()
            .filter(|kind| *kind != "archive" && compatible(&source_type, &account, kind))
            .ok_or_else(|| Error::invalid("当前来源不支持该历史数据类别"))?;
        if !kinds.contains(&kind) {
            kinds.push(kind);
        }
    }
    kinds.sort_unstable();
    let selected = serde_json::to_string(&kinds)?;
    let tx = db.transaction()?;
    if let Some((stored, ids)) = tx
        .query_row(
            "SELECT kinds, jobs FROM history_task_batches WHERE source_id=?1 AND idem_key=?2",
            params![source_id, key],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?
    {
        if stored != selected {
            return Err(Error::conflict(
                "key_conflict: 已有请求使用同一幂等键提交了其他类别",
            ));
        }
        let jobs = serde_json::from_str::<Vec<String>>(&ids)?
            .iter()
            .map(|id| job_view(&tx, id))
            .collect::<Result<Vec<_>>>()?;
        return Ok(json!({"jobs":jobs}));
    }
    let now = now_utc();
    let until =
        (Utc::now() + chrono::Duration::seconds(1)).to_rfc3339_opts(SecondsFormat::Secs, true);
    let mut ids = Vec::with_capacity(kinds.len());
    for kind in kinds {
        let request = Request {
            source_id: source_id.clone(),
            source_type: source_type.clone(),
            account: account.clone(),
            namespace: namespace.clone(),
            kind: kind.to_owned(),
            targets: Vec::new(),
            from: None,
            to: matches!(kind, "dm" | "group").then(|| until.clone()),
            content: "all".into(),
        };
        ids.push(insert_task(
            &tx,
            &request,
            &new_id("bulk"),
            &request.fingerprint(),
            &now,
        )?);
    }
    tx.execute(
        "INSERT INTO history_task_batches(source_id,idem_key,kinds,jobs) VALUES (?1,?2,?3,?4)",
        params![source_id, key, selected, serde_json::to_string(&ids)?],
    )?;
    let jobs = ids
        .iter()
        .map(|id| job_view(&tx, id))
        .collect::<Result<Vec<_>>>()?;
    tx.commit()?;
    Ok(json!({"jobs":jobs}))
}

fn insert_task(
    tx: &Transaction<'_>,
    request: &Request,
    key: &str,
    fingerprint: &str,
    now: &str,
) -> Result<String> {
    ingestion::bind_actor_in(tx, &request.source_id, "owner")?;
    let task_id = new_id("htask");
    tx.execute(
        "INSERT INTO history_task_jobs
         (task_id, source_id, kind, targets, content, from_at, to_at, state,
          idem_key, fingerprint, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'queued', ?8, ?9, ?10, ?10)",
        params![
            task_id,
            request.source_id,
            request.kind,
            canonical(&json!(request.targets)),
            request.content,
            request.from,
            request.to,
            key,
            fingerprint,
            now,
        ],
    )?;
    let discovery = [DISCOVER.to_owned()];
    let targets = if request.targets.is_empty() {
        &discovery[..]
    } else {
        &request.targets
    };
    for (ordinal, target) in targets.iter().enumerate() {
        tx.execute(
            "INSERT INTO history_task_items (task_id, ordinal, target, state, updated_at)
             VALUES (?1, ?2, ?3, 'pending', ?4)",
            params![task_id, ordinal as i64, target, now],
        )?;
    }
    Ok(task_id)
}

/// List all tasks, oldest first: `{jobs:[...]}` with per-target progress and
/// the capped per-target error ledger.
pub fn list(db: &Connection) -> Result<Value> {
    let ids: Vec<String> = {
        // A bulk batch shares one `created_at`, so ties resolve by insertion
        // order (rowid) to match the order the scheduler drains them in.
        let mut stmt =
            db.prepare("SELECT task_id FROM history_task_jobs ORDER BY created_at, rowid")?;
        stmt.query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?
    };
    let mut jobs = Vec::with_capacity(ids.len());
    for id in ids {
        jobs.push(job_view(db, &id)?);
    }
    Ok(json!({ "jobs": jobs }))
}

/// Cancel or retry a task.
///
/// * `cancel` — from `queued`/`running`/`partial`/`failed`: stops the worker
///   (the commit-time state recheck discards any in-flight page) and moves
///   unfinished targets to `cancelled`. Completed targets keep their
///   checkpoints and counters.
/// * `retry` — from `failed`/`partial`/`cancelled`: requeues only the
///   failed/cancelled targets, resuming from their last committed checkpoint;
///   completed targets and cumulative counters are untouched.
pub fn change(db: &mut Connection, id: &str, action: &str) -> Result<Value> {
    let now = now_utc();
    let tx = db.transaction()?;
    let state: String = tx
        .query_row(
            "SELECT state FROM history_task_jobs WHERE task_id = ?1",
            params![id],
            |r| r.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing(format!("history task '{id}' not found")))?;
    match action {
        "cancel" => {
            if !matches!(state.as_str(), "queued" | "running" | "partial" | "failed") {
                return Err(Error::conflict(format!(
                    "history task '{id}' is '{state}' and can no longer be cancelled"
                )));
            }
            tx.execute(
                "UPDATE history_task_jobs SET state = 'cancelled', finished_at = ?2, updated_at = ?2
                 WHERE task_id = ?1",
                params![id, now],
            )?;
            tx.execute(
                "UPDATE history_task_items SET state = 'cancelled', updated_at = ?2
                 WHERE task_id = ?1 AND state IN ('pending', 'running')",
                params![id, now],
            )?;
        }
        "retry" => {
            if !matches!(state.as_str(), "failed" | "partial" | "cancelled") {
                return Err(Error::conflict(format!(
                    "history task '{id}' is '{state}'; only failed, partial, or cancelled tasks can be retried"
                )));
            }
            tx.execute(
                "UPDATE history_task_items SET state = 'pending', last_error = NULL, updated_at = ?2,
                 cursor = CASE WHEN json_extract(last_error,'$.code')='feishu_cursor_expired'
                               THEN NULL ELSE cursor END
                 WHERE task_id = ?1 AND state IN ('failed', 'cancelled')",
                params![id, now],
            )?;
            tx.execute(
                "UPDATE history_task_jobs SET state = 'queued', failed = 0,
                        finished_at = NULL, updated_at = ?2
                 WHERE task_id = ?1",
                params![id, now],
            )?;
        }
        _ => {
            return Err(Error::invalid(format!(
                "action '{action}' must be 'cancel' or 'retry'"
            )));
        }
    }
    tx.commit()?;
    job_view(db, id)
}

pub fn withdraw_source(db: &Connection, source: &str) -> Result<()> {
    let now = now_utc();
    db.execute(
        "UPDATE history_task_jobs SET state='withdrawn', finished_at=?2, updated_at=?2
         WHERE source_id=?1 AND state IN ('queued','running','partial','failed','cancelled')",
        params![source, now],
    )?;
    db.execute(
        "UPDATE history_task_items SET state='cancelled', cursor=NULL, updated_at=?2
         WHERE task_id IN (SELECT task_id FROM history_task_jobs WHERE source_id=?1 AND state='withdrawn')
           AND state IN ('pending','running','failed','cancelled')",
        params![source, now],
    )?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

/// Execute one page of the oldest runnable task target. Returns `Ok(true)`
/// when a page was committed (or a target failure recorded), `Ok(false)` when
/// there is nothing runnable or the pass was stopped.
///
/// `read(source_type, account, kind, target, from, to, content, cursor)` is
/// the adapter bridge; it is called **outside** the SQLite lock. Its events
/// must already carry intake-normalized native ids (the same normalization
/// the continuous sync uses). The returned checkpoint is opaque JSON; `None`
/// means the target is exhausted.
///
/// Selection requires an active capture grant and the bound `owner` actor, so
/// a revoked source is never read remotely. The commit transaction rechecks
/// task state and capture again: a concurrent cancel or revocation discards
/// the page without moving the checkpoint, and a later pass re-reads it.
pub fn run_next(
    database: &Mutex<Connection>,
    profile: &str,
    stop: &impl Fn() -> bool,
    read: &impl Fn(
        &str,
        &str,
        &str,
        &str,
        Option<&str>,
        Option<&str>,
        &str,
        Option<&Value>,
    ) -> Result<(Vec<Value>, Option<Value>)>,
) -> Result<bool> {
    if stop() {
        return Ok(false);
    }
    let pick = {
        let db = lock(database)?;
        let pick = select(&db)?;
        let Some(pick) = pick else {
            return Ok(false);
        };
        let now = now_utc();
        db.execute(
            "UPDATE history_task_jobs SET state = 'running',
                    started_at = COALESCE(started_at, ?2), updated_at = ?2
             WHERE task_id = ?1 AND state IN ('queued', 'running')",
            params![pick.task_id, now],
        )?;
        db.execute(
            "UPDATE history_task_items SET state = 'running', updated_at = ?2
             WHERE task_id = ?1 AND target = ?3 AND state = 'pending'",
            params![pick.task_id, now, pick.target],
        )?;
        pick
    };
    if stop() {
        return Ok(false);
    }
    let cursor: Option<Value> = pick
        .cursor
        .as_deref()
        .map(serde_json::from_str)
        .transpose()?;
    // The remote read happens with no SQLite lock held.
    let discovery = pick.target == DISCOVER;
    let outcome = if discovery {
        let token = cursor.as_ref().and_then(Value::as_str);
        discover(&pick, token, stop).map(Page::Discovery)
    } else if pick.all && pick.kind == "sessions" {
        crate::history_hosts::read_discovered_page(&pick.source_type, cursor.as_ref(), stop)
            .map(|(events, next)| Page::Events(events, next))
    } else {
        read(
            &pick.source_type,
            &pick.account,
            &pick.kind,
            &pick.target,
            pick.from.as_deref(),
            pick.to.as_deref(),
            &pick.content,
            cursor.as_ref(),
        )
        .map(|(events, next)| Page::Events(events, next))
    };
    if stop() {
        return Ok(false);
    }

    let mut db = lock(database)?;
    match outcome {
        Err(error) => {
            let tx = db.transaction()?;
            if !task_running(&tx, &pick.task_id)? {
                return Ok(false);
            }
            fail_item(&tx, &pick.task_id, &pick.target, &error)?;
            tx.commit()?;
            Ok(true)
        }
        Ok(Page::Discovery((targets, token))) => {
            let tx = db.transaction()?;
            if !task_running(&tx, &pick.task_id)? {
                return Ok(false);
            }
            let now = now_utc();
            let ordinal: i64 = tx.query_row(
                "SELECT COALESCE(MAX(ordinal),0) FROM history_task_items WHERE task_id=?1",
                [&pick.task_id],
                |row| row.get(0),
            )?;
            let mut insert = tx.prepare_cached(
                "INSERT INTO history_task_items(task_id,ordinal,target,state,cursor,updated_at)
                 VALUES (?1,?2,?3,'pending',?4,?5) ON CONFLICT(task_id,target) DO NOTHING",
            )?;
            for (index, (target, checkpoint)) in targets.into_iter().enumerate() {
                insert.execute(params![
                    pick.task_id,
                    ordinal + index as i64 + 1,
                    target,
                    checkpoint.map(|value| value.to_string()),
                    now
                ])?;
            }
            drop(insert);
            save_checkpoint(&tx, &pick, token.map(Value::String), 0, 0, &now)?;
            settle(&tx, &pick.task_id, &now)?;
            tx.commit()?;
            Ok(true)
        }
        Ok(Page::Events(events, next)) => {
            let tx = db.transaction()?;
            if !task_running(&tx, &pick.task_id)? {
                return Ok(false);
            }
            if !pick.all && !capture_active(&tx, &pick.source_id)? {
                // Permission lost mid-read: keep the checkpoint, pause the
                // task; selection skips it until capture is granted again.
                return Ok(false);
            }
            if next.is_some() && next == cursor {
                drop(tx);
                let tx = db.transaction()?;
                if !task_running(&tx, &pick.task_id)? {
                    return Ok(false);
                }
                fail_item(
                    &tx,
                    &pick.task_id,
                    &pick.target,
                    &Error::conflict("cursor_not_advancing: adapter returned the same checkpoint"),
                )?;
                tx.commit()?;
                return Ok(true);
            }
            let mut accepted = 0i64;
            let mut duplicates = 0i64;
            for chunk in events.chunks(EVENTS_PER_PAGE) {
                let source_cursor: String = tx.query_row(
                    "SELECT cursor FROM ing_sources WHERE source_id = ?1",
                    params![pick.source_id],
                    |r| r.get(0),
                )?;
                let key = Uuid::new_v4().to_string();
                let page = json!({
                    "source_id": pick.source_id,
                    "cursor": source_cursor,
                    "next_cursor": key,
                    "events": chunk,
                });
                let receipt = match if pick.all {
                    ingestion::accept_history_task(&tx, &pick.task_id, &key, page, profile)
                } else {
                    ingestion::accept_in(&tx, "owner", &key, page, profile)
                } {
                    Ok(receipt) => receipt,
                    Err(error) if error.code == "action_denied" => {
                        // Capture or binding revoked mid-read: discard the
                        // page, keep the checkpoint, pause without failing.
                        return Ok(false);
                    }
                    Err(error) => {
                        drop(tx);
                        let tx = db.transaction()?;
                        if !task_running(&tx, &pick.task_id)? {
                            return Ok(false);
                        }
                        fail_item(&tx, &pick.task_id, &pick.target, &error)?;
                        tx.commit()?;
                        return Ok(true);
                    }
                };
                if pick.source_type == "feishu" {
                    crate::retrieval::record_feishu_kind(&tx, &receipt, &pick.kind)?;
                }
                let (new, existing) = link_page(&tx, &pick.task_id, &pick.target, &receipt)?;
                accepted += new;
                duplicates += existing;
            }
            let now = now_utc();
            save_checkpoint(&tx, &pick, next, accepted, duplicates, &now)?;
            tx.execute(
                "UPDATE history_task_jobs SET accepted = accepted + ?2,
                        duplicates = duplicates + ?3, updated_at = ?4
                 WHERE task_id = ?1",
                params![pick.task_id, accepted, duplicates, now],
            )?;
            settle(&tx, &pick.task_id, &now)?;
            tx.commit()?;
            Ok(true)
        }
    }
}

fn link_page(tx: &Transaction<'_>, job: &str, target: &str, receipt: &Value) -> Result<(i64, i64)> {
    tx.execute(
        "INSERT INTO history_job_pages(task_id,target,ingest_id) VALUES(?1,?2,?3)",
        params![job, target, receipt["ingest_id"].as_str()],
    )?;
    let (mut accepted, mut duplicates) = (0, 0);
    for row in receipt["events"].as_array().into_iter().flatten() {
        match row["disposition"].as_str() {
            Some("accepted") => accepted += 1,
            Some("duplicate") => duplicates += 1,
            _ => {}
        }
    }
    Ok((accepted, duplicates))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ArchivePage {
    target: String,
    cursor: String,
    next_cursor: String,
    #[serde(default)]
    complete: bool,
    #[serde(default)]
    events: Vec<Value>,
    source_error: Option<String>,
    #[serde(default)]
    relays: Vec<ArchiveRelay>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ArchiveRelay {
    native_object: String,
    native_version: String,
    cites_source_id: String,
    cites_object: String,
    cites_version: String,
    cites_field: String,
}

fn record_archive_relays(
    db: &Connection,
    source: &str,
    receipt: &Value,
    relays: Vec<ArchiveRelay>,
) -> Result<()> {
    for relay in relays {
        let child: Option<(String,i64)> = db.query_row(
            "SELECT e.event_id,e.rowid FROM ing_event_versions e JOIN json_each(?4,'$.events') accepted
             ON e.event_id=json_extract(accepted.value,'$.event_id')
             WHERE e.source_id=?1 AND e.native_object=?2 AND e.native_version=?3 AND e.active=1 AND e.actor_kind='assistant_message'",
            params![source,relay.native_object,relay.native_version,receipt.to_string()],
            |row|Ok((row.get(0)?,row.get(1)?)),
        ).optional()?;
        let Some((child, ordinal)) = child else {
            continue;
        };
        let cited: Option<String> = db.query_row(
            "SELECT e.event_id FROM ing_event_versions e JOIN ing_jobs j ON j.event_id=e.event_id
             JOIN json_each(j.input,'$.inputs') span ON json_extract(span.value,'$.field')=?4
             WHERE e.source_id=?1 AND e.native_object=?2 AND e.native_version=?3 AND e.active=1 AND e.rowid<?5",
            params![relay.cites_source_id,relay.cites_object,relay.cites_version,relay.cites_field,ordinal],
            |row|row.get(0),
        ).optional()?;
        if let Some(cited) = cited {
            let prior: Option<String> = db
                .query_row(
                    "SELECT origin_event_id FROM history_archive_relay WHERE event_id=?1",
                    [&child],
                    |row| row.get(0),
                )
                .optional()?;
            match prior {
                Some(prior) if prior != cited => {
                    return Err(Error::conflict(
                        "archive relay already has another verified origin",
                    ));
                }
                Some(_) => {}
                None => {
                    db.execute(
                        "INSERT INTO history_archive_relay VALUES(?1,?2)",
                        params![child, cited],
                    )?;
                }
            }
        }
    }
    Ok(())
}

pub fn control_extract(db: &mut Connection, id: &str, body: &Value) -> Result<Value> {
    let tx = db.transaction()?;
    let source: String = tx
        .query_row(
            "SELECT source_id FROM history_task_jobs WHERE task_id=?1 AND kind='archive'",
            [id],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("archive job unavailable"))?;
    let result = if let Some(paused) = body["paused"]
        .as_bool()
        .filter(|_| body.get("root_id").is_none())
    {
        if paused {
            tx.execute(
                "INSERT OR IGNORE INTO ing_extract_pauses VALUES(?1)",
                [&source],
            )?;
        } else {
            tx.execute(
                "DELETE FROM ing_extract_pauses WHERE source_id=?1",
                [&source],
            )?;
        }
        json!({"paused":paused})
    } else if let (Some(root), Some(deny)) = (body["root_id"].as_str(), body["deny"].as_bool()) {
        if !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM ing_roots WHERE root_id=?1 AND source_id=?2)",
            params![root, source],
            |row| row.get::<_, bool>(0),
        )? {
            return Err(Error::missing("archive root unavailable"));
        }
        if deny {
            tx.execute(
                "INSERT OR IGNORE INTO ing_extract_denials VALUES(?1)",
                [root],
            )?;
        } else {
            tx.execute("DELETE FROM ing_extract_denials WHERE root_id=?1", [root])?;
        }
        json!({"root_id":root,"denied":deny})
    } else {
        return Err(Error::invalid("provide paused or a root_id and deny"));
    };
    tx.commit()?;
    Ok(result)
}

/// Commit one page of a registered archive adapter's explicitly selected job.
/// The receipt remains replayable after the target advances or completes.
pub fn submit_page(
    db: &mut Connection,
    id: &str,
    body: &Value,
    key: &str,
    profile: &str,
) -> Result<Value> {
    let page: ArchivePage = serde_json::from_value(body.clone())?;
    if page.events.len() > EVENTS_PER_PAGE
        || page.relays.len() > EVENTS_PER_PAGE
        || page.cursor.len() > 4096
        || page.next_cursor.len() > 4096
        || page.next_cursor.is_empty()
        || page.cursor == page.next_cursor
        || page
            .source_error
            .as_ref()
            .is_some_and(|error| error.is_empty() || error.len() > 1000 || !page.events.is_empty())
    {
        return Err(Error::invalid(
            "invalid archive page, checkpoint or source error",
        ));
    }
    let digest = format!("{:x}", Sha256::digest(canonical(body)));
    let tx = db.transaction()?;
    let prior: Option<(String, String)> = tx.query_row(
        "SELECT digest,receipt FROM history_archive_submissions WHERE task_id=?1 AND idem_key=?2",
        params![id,key], |row| Ok((row.get(0)?,row.get(1)?)),
    ).optional()?;
    if let Some((prior_digest, result)) = prior {
        if prior_digest != digest {
            return Err(Error::conflict(
                "key_conflict: archive page key already has a different payload",
            ));
        }
        return Ok(serde_json::from_str(&result)?);
    }
    let (source, from, to, checkpoint): (String, Option<String>, Option<String>, Option<String>) =
        tx.query_row(
            "SELECT j.source_id,j.from_at,j.to_at,i.cursor FROM history_task_jobs j
         JOIN history_task_items i ON i.task_id=j.task_id
         WHERE j.task_id=?1 AND j.kind='archive' AND j.state IN ('queued','running')
           AND i.target=?2 AND i.state IN ('pending','running')",
            params![id, page.target],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("active archive target unavailable"))?;
    let checkpoint: Option<String> = checkpoint
        .map(|value| serde_json::from_str(&value))
        .transpose()?;
    if checkpoint.as_deref().unwrap_or("start") != page.cursor {
        return Err(Error::conflict(
            "cursor_mismatch: archive page does not continue the target",
        ));
    }
    let now = now_utc();
    tx.execute("UPDATE history_task_jobs SET state='running',started_at=coalesce(started_at,?2),updated_at=?2
                WHERE task_id=?1", params![id,now])?;
    let result = if let Some(source_error) = page.source_error {
        fail_item(
            &tx,
            id,
            &page.target,
            &Error::new(502, "archive_source_unavailable", source_error),
        )?;
        json!({"checkpoint":page.cursor,"state":"failed"})
    } else {
        let events = if let Some((from, to)) = from.zip(to) {
            let from = DateTime::parse_from_rfc3339(&from)
                .map_err(|_| Error::internal("invalid archive job window"))?;
            let to = DateTime::parse_from_rfc3339(&to)
                .map_err(|_| Error::internal("invalid archive job window"))?;
            page.events
                .into_iter()
                .map(|event| {
                    let at = event["at"]
                        .as_str()
                        .and_then(|at| DateTime::parse_from_rfc3339(at).ok())
                        .ok_or_else(|| {
                            Error::invalid("a dated archive event requires RFC3339 at")
                        })?;
                    Ok((at, event))
                })
                .collect::<Result<Vec<_>>>()?
                .into_iter()
                .filter(|(at, _)| &from <= at && at < &to)
                .map(|(_, event)| event)
                .collect::<Vec<_>>()
        } else {
            page.events
        };
        let (mut accepted, mut duplicates) = (0, 0);
        let receipt = if events.is_empty() {
            Value::Null
        } else {
            let source_cursor: String = tx.query_row(
                "SELECT cursor FROM ing_sources WHERE source_id=?1",
                [&source],
                |row| row.get(0),
            )?;
            let intake_key = Uuid::new_v4().to_string();
            let receipt = ingestion::accept_history_task(
                &tx,
                id,
                &intake_key,
                json!({
                    "source_id":source,"cursor":source_cursor,"next_cursor":intake_key,"events":events,
                }),
                profile,
            )?;
            (accepted, duplicates) = link_page(&tx, id, &page.target, &receipt)?;
            record_archive_relays(&tx, &source, &receipt, page.relays)?;
            receipt
        };
        let state = if page.complete {
            "completed"
        } else {
            "running"
        };
        tx.execute("UPDATE history_task_items SET state=?3,cursor=?4,accepted=accepted+?5,duplicates=duplicates+?6,updated_at=?7
                    WHERE task_id=?1 AND target=?2",
                    params![id,page.target,state,serde_json::to_string(&page.next_cursor)?,accepted,duplicates,now])?;
        tx.execute("UPDATE history_task_jobs SET accepted=accepted+?2,duplicates=duplicates+?3 WHERE task_id=?1",
                    params![id,accepted,duplicates])?;
        settle(&tx, id, &now)?;
        json!({"checkpoint":page.next_cursor,"state":state,"intake":receipt})
    };
    tx.execute(
        "INSERT INTO history_archive_submissions VALUES(?1,?2,?3,?4)",
        params![id, key, digest, serde_json::to_string(&result)?],
    )?;
    tx.commit()?;
    Ok(result)
}

enum Page {
    Discovery(DiscoveryPage),
    Events(Vec<Value>, Option<Value>),
}

fn discover(pick: &Pick, token: Option<&str>, stop: &impl Fn() -> bool) -> Result<DiscoveryPage> {
    if pick.source_type == "feishu" {
        let (targets, next) =
            crate::history_feishu::discover_targets(&pick.account, &pick.kind, token, stop)?;
        Ok((
            targets.into_iter().map(|target| (target, None)).collect(),
            next,
        ))
    } else {
        Ok((
            crate::history_hosts::discover_sessions(&pick.source_type, stop)?,
            None,
        ))
    }
}

fn save_checkpoint(
    tx: &Transaction<'_>,
    pick: &Pick,
    next: Option<Value>,
    accepted: i64,
    duplicates: i64,
    now: &str,
) -> Result<()> {
    let state = if next.is_some() {
        "running"
    } else {
        "completed"
    };
    tx.execute(
        "UPDATE history_task_items SET cursor=?3,state=?4,accepted=accepted+?5,
                duplicates=duplicates+?6,updated_at=?7 WHERE task_id=?1 AND target=?2",
        params![
            pick.task_id,
            pick.target,
            next.map(|value| value.to_string()),
            state,
            accepted,
            duplicates,
            now
        ],
    )?;
    Ok(())
}

struct Pick {
    task_id: String,
    source_id: String,
    source_type: String,
    account: String,
    kind: String,
    content: String,
    from: Option<String>,
    to: Option<String>,
    target: String,
    cursor: Option<String>,
    all: bool,
}

/// Oldest runnable task's first unfinished target. `running` jobs and items
/// are taken over as-is: their checkpoints are the resume points, so a
/// restart never skips a page.
fn select(db: &Connection) -> Result<Option<Pick>> {
    db.query_row(
        "SELECT j.task_id, j.source_id, s.kind, s.account, j.kind, j.content,
                j.from_at, j.to_at, i.target, i.cursor, j.targets='[]'
         FROM history_task_jobs j
         JOIN ing_sources s ON s.source_id = j.source_id
         LEFT JOIN ing_grants g ON g.source_id = j.source_id
              AND g.purpose = 'capture' AND g.active = 1
         JOIN history_task_items i ON i.task_id = j.task_id
              AND i.state IN ('pending', 'running')
         WHERE j.state IN ('queued', 'running')
           AND j.kind<>'archive'
           AND (j.targets='[]' OR g.active=1)
           AND EXISTS (SELECT 1 FROM ing_actors a
                       WHERE a.source_id = j.source_id AND a.actor = 'owner'
                         AND a.action = 'accept' AND a.active = 1)
         ORDER BY j.created_at, j.rowid, i.ordinal
         LIMIT 1",
        [],
        |r| {
            Ok(Pick {
                task_id: r.get(0)?,
                source_id: r.get(1)?,
                source_type: r.get(2)?,
                account: r.get(3)?,
                kind: r.get(4)?,
                content: r.get(5)?,
                from: r.get(6)?,
                to: r.get(7)?,
                target: r.get(8)?,
                cursor: r.get(9)?,
                all: r.get(10)?,
            })
        },
    )
    .optional()
    .map_err(Error::from)
}

fn task_running(tx: &Transaction<'_>, task_id: &str) -> Result<bool> {
    Ok(tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM history_task_jobs WHERE task_id = ?1 AND state = 'running')",
        params![task_id],
        |r| r.get(0),
    )?)
}

fn capture_active(conn: &Connection, source_id: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_grants
                       WHERE source_id = ?1 AND purpose = 'capture' AND active = 1)",
        params![source_id],
        |r| r.get(0),
    )?)
}

/// Record a per-target failure: append to the capped error ledger, mark the
/// item failed, and settle the task. Other targets keep running.
fn fail_item(tx: &Transaction<'_>, task_id: &str, target: &str, error: &Error) -> Result<()> {
    let now = now_utc();
    let message = crate::authority::redact(&error.message);
    tx.execute(
        "INSERT INTO history_task_errors (task_id, target, code, message, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![task_id, target, error.code, message, now],
    )?;
    tx.execute(
        "DELETE FROM history_task_errors WHERE task_id = ?1 AND error_id NOT IN
         (SELECT error_id FROM history_task_errors WHERE task_id = ?1
          ORDER BY error_id DESC LIMIT ?2)",
        params![task_id, ERRORS_KEPT as i64],
    )?;
    tx.execute(
        "UPDATE history_task_items SET state = 'failed', last_error = ?3, updated_at = ?4
         WHERE task_id = ?1 AND target = ?2",
        params![
            task_id,
            target,
            json!({"code": error.code, "message": message}).to_string(),
            now,
        ],
    )?;
    tx.execute(
        "UPDATE history_task_jobs SET failed =
         (SELECT COUNT(*) FROM history_task_items WHERE task_id = ?1 AND state = 'failed')
         WHERE task_id = ?1",
        params![task_id],
    )?;
    settle(tx, task_id, &now)
}

/// Derive the task state from its targets: any unfinished target keeps the
/// task running; otherwise it settles to completed / partial / failed.
fn settle(tx: &Transaction<'_>, task_id: &str, now: &str) -> Result<()> {
    let active: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM history_task_items WHERE task_id=?1 AND state IN ('pending','running'))",
        params![task_id],
        |r| r.get(0),
    )?;
    if active {
        return Ok(());
    }
    let (failed, completed): (i64, bool) = tx.query_row(
        "SELECT failed, accepted+duplicates>0 OR EXISTS(SELECT 1 FROM history_task_items
                WHERE task_id=?1 AND state='completed' AND target<>?2)
         FROM history_task_jobs WHERE task_id=?1",
        params![task_id, DISCOVER],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let state = match (failed, completed) {
        (0, _) => "completed",
        (_, true) => "partial",
        _ => "failed",
    };
    tx.execute(
        "UPDATE history_task_jobs SET state = ?2, finished_at = ?3, updated_at = ?3
         WHERE task_id = ?1",
        params![task_id, state, now],
    )?;
    Ok(())
}

fn lineage(db: &Connection, event_id: &str) -> Result<Value> {
    let endpoint: Option<(String, String, String, Option<String>, i64)> = db.query_row(
        "WITH RECURSIVE chain(event_id,depth) AS (
           SELECT ?1,0 UNION ALL
           SELECT relay.origin_event_id,chain.depth+1 FROM chain
           JOIN history_archive_relay relay ON relay.event_id=chain.event_id
           JOIN ing_event_versions e ON e.event_id=relay.origin_event_id AND e.active=1
           WHERE chain.depth<64
         )
         SELECT e.event_id,e.native_object,e.actor_kind,
                CASE WHEN e.actor_kind='tool_result' THEN json_extract(e.content,'$.checks.run_id') END,
                chain.depth FROM chain JOIN ing_event_versions e ON e.event_id=chain.event_id AND e.active=1
         ORDER BY chain.depth DESC LIMIT 1",
        [event_id], |row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?)),
    ).optional()?;
    let Some((origin, object, actor, run, depth)) = endpoint else {
        return Ok(json!({"relation":"unavailable"}));
    };
    Ok(match (actor.as_str(), depth, run) {
        ("user_message", 0, _) => {
            json!({"relation":"source","origin_event_id":origin,"origin_object":object})
        }
        ("user_message", _, _) => {
            json!({"relation":"reported","origin_event_id":origin,"origin_object":object})
        }
        ("tool_result", 0, Some(run)) => json!({"relation":"tool_run","run_id":run}),
        _ => json!({"relation":"unknown"}),
    })
}

// ---------------------------------------------------------------------------
// Views and small helpers
// ---------------------------------------------------------------------------

pub fn results(
    db: &Connection,
    id: &str,
    cursor: Option<&str>,
    limit: Option<&str>,
) -> Result<Value> {
    let job = job_view(db, id)?;
    let head: i64 = db.query_row(
        "SELECT coalesce(max(page_seq),0) FROM history_job_pages WHERE task_id=?1",
        [id],
        |row| row.get(0),
    )?;
    let (watermark, offset) = match cursor {
        None => (head, 0),
        Some(cursor) => cursor
            .split_once(':')
            .and_then(|(head, offset)| {
                Some((head.parse::<i64>().ok()?, offset.parse::<i64>().ok()?))
            })
            .filter(|(watermark, offset)| (0..=head).contains(watermark) && *offset >= 0)
            .ok_or_else(|| Error::invalid("invalid history result cursor"))?,
    };
    let limit = match limit {
        None => 40_i64,
        Some(limit) => limit
            .parse::<i64>()
            .ok()
            .filter(|n| (1..=100).contains(n))
            .ok_or_else(|| Error::invalid("history result limit must be between 1 and 100"))?,
    };
    let mut query = db.prepare(
        "SELECT p.target,p.page_seq,row.value,v.native_version,v.active,j.status,j.result,j.fail_reason,
                EXISTS(SELECT 1 FROM ing_grants g WHERE g.source_id=i.source_id AND g.purpose='extract' AND g.active=1)
                  AND NOT EXISTS(SELECT 1 FROM ing_extract_denials d WHERE d.root_id=v.root_id),
                EXISTS(SELECT 1 FROM ing_extract_pauses pause WHERE pause.source_id=i.source_id)
         FROM history_job_pages p JOIN history_task_items t ON t.task_id=p.task_id AND t.target=p.target
         JOIN ing_ingests i ON i.ingest_id=p.ingest_id JOIN json_each(i.receipt,'$.events') row
         LEFT JOIN ing_event_versions v ON v.event_id=json_extract(row.value,'$.event_id')
         LEFT JOIN ing_jobs j ON j.event_id=v.event_id
         WHERE p.task_id=?1 AND p.page_seq<=?2
         ORDER BY t.ordinal,p.page_seq,CAST(row.key AS INTEGER) LIMIT ?3 OFFSET ?4",
    )?;
    let mut rows = Vec::new();
    let mut records = query.query(params![id, watermark, limit + 1, offset])?;
    while let Some(row) = records.next()? {
        let receipt: Value = serde_json::from_str(&row.get::<_, String>(2)?)?;
        let extracted = row
            .get::<_, Option<String>>(6)?
            .map(|value| serde_json::from_str::<Value>(&value))
            .transpose()?
            .unwrap_or(Value::Null);
        let active = row.get::<_, Option<bool>>(4)?;
        let granted = row.get::<_, bool>(8)?;
        let provenance = if active == Some(true) && job["kind"] == "archive" {
            lineage(db, receipt["event_id"].as_str().unwrap_or_default())?
        } else {
            Value::Null
        };
        rows.push(json!({
            "target":row.get::<_,String>(0)?, "page_seq":row.get::<_,i64>(1)?,
            "native_object":receipt["native_object"], "event_id":receipt["event_id"],
            "native_version":row.get::<_,Option<String>>(3)?,
            "capture":{"disposition":receipt["disposition"],"present":active == Some(true)},
            "extract":{"state":row.get::<_,Option<String>>(5)?, "error":row.get::<_,Option<String>>(7)?, "paused":row.get::<_,bool>(9)?,
                "eligibility":if active != Some(true) {"unavailable"} else if granted {"allowed"} else {"denied"}},
            "candidate_counts":{"total":extracted["candidates_recorded"],"verified":extracted["observations_verified"]},
            "decisions":extracted["decisions"], "provenance":provenance,
        }));
    }
    let more = rows.len() > limit as usize;
    rows.truncate(limit as usize);
    Ok(json!({
        "job":job, "watermark":watermark,"rows":rows,
        "next_cursor":more.then(||format!("{watermark}:{}", offset + limit)),
        "mapping_coverage":if head == 0 && (job["accepted"].as_i64().unwrap_or(0) > 0 ||
            job["duplicates"].as_i64().unwrap_or(0) > 0) {"summary_only"} else {"mapped"},
    }))
}

fn job_view(conn: &Connection, id: &str) -> Result<Value> {
    let mut job = conn
        .query_row(
            "SELECT j.task_id, j.source_id, j.kind, j.targets, j.content, j.from_at, j.to_at, j.state,
                    j.accepted, j.duplicates, j.failed, j.created_at, j.updated_at, j.started_at,
                    j.finished_at, s.kind
             FROM history_task_jobs j JOIN ing_sources s ON s.source_id=j.source_id WHERE j.task_id = ?1",
            params![id],
            |r| {
                Ok(json!({
                    "id": r.get::<_, String>(0)?,
                    "source_id": r.get::<_, String>(1)?,
                    "kind": r.get::<_, String>(2)?,
                    "targets": parse_json(&r.get::<_, String>(3)?),
                    "content": r.get::<_, String>(4)?,
                    "from": r.get::<_, Option<String>>(5)?,
                    "to": r.get::<_, Option<String>>(6)?,
                    "state": r.get::<_, String>(7)?,
                    "accepted": r.get::<_, i64>(8)?,
                    "duplicates": r.get::<_, i64>(9)?,
                    "failed": r.get::<_, i64>(10)?,
                    "created_at": r.get::<_, String>(11)?,
                    "updated_at": r.get::<_, String>(12)?,
                    "started_at": r.get::<_, Option<String>>(13)?,
                    "finished_at": r.get::<_, Option<String>>(14)?,
                    "source_type": r.get::<_, String>(15)?,
                }))
            },
        )
        .optional()?
        .ok_or_else(|| Error::missing(format!("history task '{id}' not found")))?;
    if job["targets"].as_array().is_some_and(Vec::is_empty) {
        let (target_count, completed, discovered): (i64, i64, bool) = conn.query_row(
            "SELECT count(*) FILTER (WHERE target<>?2),
                    count(*) FILTER (WHERE target<>?2 AND state='completed'),
                    EXISTS(SELECT 1 FROM history_task_items WHERE task_id=?1 AND target=?2 AND state='completed')
             FROM history_task_items WHERE task_id=?1",
            params![id, DISCOVER],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        job["scope"] = json!("all");
        job["target_count"] = json!(target_count);
        job["completed_targets"] = json!(completed);
        job["discovered"] = json!(discovered);
    } else {
        let mut items = conn.prepare(
            "SELECT target, state, accepted, duplicates, last_error
         FROM history_task_items WHERE task_id = ?1 ORDER BY ordinal",
        )?;
        job["items"] = Value::Array(
            items
                .query_map(params![id], |r| {
                    Ok(json!({
                        "target": r.get::<_, String>(0)?,
                        "state": r.get::<_, String>(1)?,
                        "accepted": r.get::<_, i64>(2)?,
                        "duplicates": r.get::<_, i64>(3)?,
                        "error": r.get::<_, Option<String>>(4)?.map(|e| parse_json(&e)),
                    }))
                })?
                .collect::<rusqlite::Result<_>>()?,
        );
    }
    let mut errors = conn.prepare(
        "SELECT target, code, message, created_at
         FROM history_task_errors WHERE task_id = ?1 ORDER BY error_id",
    )?;
    job["errors"] = Value::Array(
        errors
            .query_map(params![id], |r| {
                Ok(json!({
                    "target": r.get::<_, String>(0)?,
                    "code": r.get::<_, String>(1)?,
                    "message": r.get::<_, String>(2)?,
                    "at": r.get::<_, String>(3)?,
                }))
            })?
            .collect::<rusqlite::Result<_>>()?,
    );
    Ok(job)
}

fn now_utc() -> String {
    Utc::now().to_rfc3339()
}

fn new_id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4().simple())
}

fn require_str<'a>(obj: &'a Map<String, Value>, field: &str) -> Result<&'a str> {
    obj.get(field)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| Error::invalid(format!("field '{field}' must be a non-empty string")))
}

fn parse_json(text: &str) -> Value {
    serde_json::from_str(text).unwrap_or_else(|_| Value::String(text.to_string()))
}

/// Deterministic canonical JSON (recursively sorted keys), matching the
/// intake fingerprint convention so transport key order never changes
/// idempotency identity.
fn canonical(value: &Value) -> String {
    fn sort(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut out = Map::new();
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for k in keys {
                    out.insert(k.clone(), sort(&map[k]));
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(sort).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_string(&sort(value))
        .expect("canonical JSON of a serde_json::Value cannot fail to serialize")
}

#[cfg(test)]
#[path = "../../../../tests/v2/history_tasks.rs"]
mod tests;
