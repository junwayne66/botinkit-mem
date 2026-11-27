//! D1 — durable, extraction-ready event intake.
//!
//! This module owns the ingestion slice of the store: source registration,
//! capture/extract grant flags, idempotent page acceptance, deduplication by
//! (root, version), extraction job creation, the leased queue, and the
//! candidate ledger written by D3 finishes. It is pure SQLite: **no remote
//! calls ever happen inside a write here.** Model HTTP lives in
//! [`crate::providers`] and runs between `claim` and `finish`.
//!
//! Contract highlights implemented here (see
//! `docs/v2/technical/domains/ingestion.md`, `authority.md`, `contracts.md`):
//!
//! * A page is committed as one transaction: event versions, provenance, the
//!   dedup receipt, new extraction jobs, and the source cursor all move
//!   together, or nothing does.
//! * Idempotency: same (source, actor, key) with the *same canonical page
//!   content* (compared before any cursor check) replays the original receipt.
//!   Same key with different content returns `409 key_conflict` and writes
//!   nothing — no events, no jobs, no cursor move.
//! * Roots are keyed by the source-account namespace plus the native object
//!   id; versions by root plus native version, or — when the source gives no
//!   native version — by a canonical fingerprint over the content and
//!   change kind (transport noise excluded). Re-reading an already accepted
//!   version on another page reuses the original event id and creates no
//!   second job.
//! * Capture starts disabled; pages arriving while capture is disabled or
//!   while the actor is not bound to the source are rejected without moving
//!   the cursor (`stop_capture` / unauthenticated adapter semantics).
//! * Explicit upstream revocation (`change_kind:"revoked"`) blocks reads and
//!   queued work for that exact version fail-closed: the version row goes
//!   inactive, pending/failed jobs for it are cancelled, and a later replay
//!   of the revoked version can never resurrect it.
//! * `claim` and `finish` recheck that the input version is still active and
//!   the source's extraction grant is still active; anything unverifiable is
//!   treated as not runnable.
//! * Candidates stored by `finish` are proposals only. Authoritative fact
//!   writes belong to D2; nothing in this module promotes a candidate to a
//!   memory.
//!
//! Integration seams owned by other maintainers:
//! * The grant rows (`ing_grants`) and actor bindings (`ing_actors`) are the
//!   narrow interface through which the authority owner expresses revocation
//!   and re-binding of capture/extract purposes; they flip `active` in their
//!   own transactions, and every write path here re-reads them.
//! * Credentials are removed from payloads before anything is persisted
//!   (best-effort key-name redaction; see [`accept`]).

use chrono::{Duration, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::error::{Error, Result};

/// Seconds a claimed job is leased before another worker may pick it up.
pub const LEASE_SECONDS: i64 = 300;
pub const MAX_RETRIES: i64 = 3;

/// JSON member names treated as credentials at the intake boundary. Any
/// object member whose key matches (case-insensitively) is removed before
/// the page is fingerprinted, persisted, or queued.
const SECRET_KEYS: &[&str] = &[
    "auth",
    "authorization",
    "apikey",
    "api_key",
    "access_token",
    "credential",
    "credentials",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "token",
];

/// Actor kinds accepted by the intake port. These drive source eligibility
/// downstream; role words inside message bodies can never change them.
const ACTOR_KINDS: &[&str] = &["user_message", "assistant_message", "tool_result"];

/// Change kinds a source page may declare. Anything else is rejected at the
/// entrance (`invalid_input`).
const CHANGE_KINDS: &[&str] = &["created", "updated", "revoked"];

/// A leased extraction job handed to a background worker. `intake_version`
/// is the immutable input version (native version or canonical fingerprint)
/// captured at acceptance time; `finish` revalidates against it.
#[derive(Debug, Clone)]
pub struct Job {
    pub id: String,
    pub event_id: String,
    pub input: Value,
    pub intake_version: String,
    attempt: i64,
}

/// Create the D1 tables and indexes. Idempotent (`IF NOT EXISTS`); the
/// storage owner sequences this migration before authority consumers run.
pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS ing_sources (
            source_id   TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            account     TEXT NOT NULL,
            namespace   TEXT NOT NULL UNIQUE,
            categories  TEXT NOT NULL,            -- canonical JSON array
            cursor      TEXT NOT NULL DEFAULT '', -- last committed cursor
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ing_grants (
            source_id  TEXT NOT NULL,
            purpose    TEXT NOT NULL,             -- 'capture' | 'extract'
            active     INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_id, purpose)
        );
        CREATE TABLE IF NOT EXISTS ing_model_routes (
            source_id TEXT PRIMARY KEY REFERENCES ing_sources(source_id),
            fingerprint TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ing_actors (
            source_id  TEXT NOT NULL,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL,             -- 'accept'
            active     INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_id, actor, action)
        );

        CREATE TABLE IF NOT EXISTS ing_ingests (
            ingest_id     TEXT PRIMARY KEY,
            source_id     TEXT NOT NULL,
            actor         TEXT NOT NULL,
            idem_key      TEXT NOT NULL,
            fingerprint   TEXT NOT NULL,          -- canonical page content
            receipt       TEXT NOT NULL,          -- stored acceptance receipt
            cursor_before TEXT NOT NULL,
            cursor_after  TEXT NOT NULL,
            committed_at  TEXT NOT NULL,
            UNIQUE (source_id, actor, idem_key)
        );

        CREATE TABLE IF NOT EXISTS ing_roots (
            root_id       TEXT PRIMARY KEY,
            source_id     TEXT NOT NULL,
            namespace     TEXT NOT NULL,
            native_object TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            UNIQUE (namespace, native_object)
        );

        CREATE TABLE IF NOT EXISTS ing_root_revocations(
            root_id TEXT PRIMARY KEY REFERENCES ing_roots(root_id),
            withdrawn_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ing_extract_denials (root_id TEXT PRIMARY KEY REFERENCES ing_roots(root_id));
        CREATE TABLE IF NOT EXISTS ing_extract_pauses (source_id TEXT PRIMARY KEY REFERENCES ing_sources(source_id));
        CREATE TABLE IF NOT EXISTS ing_event_versions (
            event_id       TEXT PRIMARY KEY,
            root_id        TEXT NOT NULL,
            source_id      TEXT NOT NULL,
            native_object  TEXT NOT NULL,
            native_version TEXT,                  -- NULL when source gave none
            version_key    TEXT NOT NULL,         -- 'v:<ver>' | 'fp:<canonical>'
            actor_kind     TEXT NOT NULL,
            occurred_at    TEXT NOT NULL,
            change_kind    TEXT NOT NULL,
            task_id        TEXT,
            content        TEXT NOT NULL,         -- sanitized canonical JSON
            active         INTEGER NOT NULL DEFAULT 1,
            received_at    TEXT NOT NULL,
            UNIQUE (root_id, version_key)
        );

        CREATE TABLE IF NOT EXISTS ing_jobs (
            job_id          TEXT PRIMARY KEY,
            source_id       TEXT NOT NULL,
            event_id        TEXT NOT NULL UNIQUE,  -- one job per input version
            intake_version  TEXT NOT NULL,
            input           TEXT NOT NULL,         -- evidence window, immutable
            status          TEXT NOT NULL,         -- pending|running|retrying|done|failed|cancelled
            attempts        INTEGER NOT NULL DEFAULT 0,
            available_at    TEXT NOT NULL,
            lease_expires_at TEXT,
            fail_reason     TEXT,
            result          TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_claim ON ing_jobs (status, available_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_source_created
            ON ing_jobs(source_id, created_at, status, available_at, lease_expires_at, event_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_runnable_by_source ON ing_jobs(
            source_id, CASE WHEN status='running' THEN lease_expires_at ELSE available_at END
        ) WHERE status IN ('pending','retrying','running');
        CREATE INDEX IF NOT EXISTS idx_jobs_last_attempt_by_source
            ON ing_jobs(source_id, updated_at DESC) WHERE attempts>0;
        CREATE INDEX IF NOT EXISTS idx_events_source_received
            ON ing_event_versions(source_id, received_at);
        CREATE TABLE IF NOT EXISTS ing_job_attempts (
            job_id TEXT NOT NULL REFERENCES ing_jobs(job_id),
            attempt INTEGER NOT NULL,
            route TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT NOT NULL,
            error_code TEXT,
            PRIMARY KEY(job_id,attempt)
        );
        CREATE INDEX IF NOT EXISTS idx_job_attempts_route_result
            ON ing_job_attempts(route,finished_at DESC) WHERE outcome IN ('retrying','failed','done');

        -- Candidate ledger: proposals produced by finished jobs. Storage of a
        -- candidate is *not* acceptance of a memory; D2 owns adjudication.
        CREATE TABLE IF NOT EXISTS ing_candidates (
            candidate_id TEXT PRIMARY KEY,
            job_id       TEXT NOT NULL,
            ordinal      INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            subject      TEXT NOT NULL,
            key          TEXT NOT NULL,
            value        TEXT NOT NULL,            -- canonical JSON scalar
            condition    TEXT,
            support      TEXT NOT NULL,            -- JSON array of span ids
            created_at   TEXT NOT NULL,
            UNIQUE (job_id, ordinal)
        );
        "#,
    )?;
    let has_budget: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('ing_jobs') WHERE name='retry_failures')",
        [],
        |row| row.get(0),
    )?;
    if !has_budget {
        let tx = conn.unchecked_transaction()?;
        tx.execute_batch(
            "ALTER TABLE ing_jobs ADD COLUMN retry_failures INTEGER NOT NULL DEFAULT 0;
                          DROP INDEX IF EXISTS idx_jobs_due_by_source;",
        )?;
        // Legacy attempt counters include leases and model outages. Preserve them for audit and
        // apply a fresh bounded budget to unfinished work whose old outcomes cannot be separated.
        tx.execute(
            "UPDATE ing_jobs SET status='retrying' WHERE status='failed'",
            [],
        )?;
        tx.commit()?;
    }
    crate::extraction::migrate(conn)
}

// ---------------------------------------------------------------------------
// Source registration and control
// ---------------------------------------------------------------------------

/// Register a capture source: `{"type","account","categories"?:[..],
/// "actors"?:[..]}`. Idempotent per (type, account): re-registering the same
/// account with the same categories returns the existing record; a different
/// category set is a conflict (the account namespace is already bound).
///
/// Capture is **disabled initially**; a bound adapter may register but the
/// source will not accept pages until [`set_capture`] (or the authority
/// owner's `resume_capture` operation, which flips the same grant row).
/// `actors`, when given, are pre-bound to submit pages for this source.
pub fn register(conn: &mut Connection, request: Value) -> Result<Value> {
    let obj = request
        .as_object()
        .ok_or_else(|| Error::invalid("source registration must be a JSON object"))?;
    let kind = require_str(obj, "type")?;
    let account = require_str(obj, "account")?;
    let categories = optional_str_array(obj, "categories")?;
    let actors = optional_str_array(obj, "actors")?;
    let namespace = format!("{kind}:{account}");
    let categories_canon = canonical(&json!(categories));

    let tx = conn.transaction()?;
    let existing: Option<(String, String, String)> = tx
        .query_row(
            "SELECT source_id, categories, cursor FROM ing_sources WHERE namespace = ?1",
            params![namespace],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let (source_id, cursor) = match existing {
        Some((source_id, stored, cursor)) => {
            if stored != categories_canon {
                return Err(Error::conflict(format!(
                    "source for account '{namespace}' is already registered as '{source_id}' with different categories"
                )));
            }
            (source_id, cursor)
        }
        None => {
            let source_id = new_id("src");
            let now = now_utc();
            tx.execute(
                "INSERT INTO ing_sources (source_id, kind, account, namespace, categories, cursor, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, '', ?6, ?6)",
                params![source_id, kind, account, namespace, categories_canon, now],
            )?;
            // Capture off at registration; extraction follows capture, both
            // revocable by the authority owner through ing_grants.
            tx.execute(
                "INSERT INTO ing_grants (source_id, purpose, active, updated_at) VALUES (?1, 'capture', 0, ?2)",
                params![source_id, now],
            )?;
            tx.execute(
                "INSERT INTO ing_grants (source_id, purpose, active, updated_at) VALUES (?1, 'extract', 0, ?2)",
                params![source_id, now],
            )?;
            for actor in &actors {
                bind_actor_in(&tx, &source_id, actor)?;
            }
            (source_id, String::new())
        }
    };
    for actor in &actors {
        if tx
            .query_row(
                "SELECT 1 FROM ing_actors WHERE source_id = ?1 AND actor = ?2 AND action = 'accept'",
                params![source_id, actor],
                |_| Ok(()),
            )
            .optional()?
            .is_none()
        {
            bind_actor_in(&tx, &source_id, actor)?;
        }
    }
    let mut record = fetch_source(&tx, &source_id)?;
    record["capture_enabled"] = json!(grant_active_in(&tx, &source_id, "capture")?);
    record["extract_enabled"] = json!(grant_active_in(&tx, &source_id, "extract")?);
    record["actors"] = Value::Array(actor_ids_in(&tx, &source_id)?);
    tx.commit()?;
    let _ = cursor; // cursor is already part of `record`
    Ok(record)
}

/// List registered sources with control state and committed cursors.
pub fn list(conn: &Connection, extract_route: Option<&str>) -> Result<Value> {
    let mut stmt = conn.prepare(
        "SELECT source_id, kind, account, namespace, categories, cursor, created_at, updated_at,
                EXISTS (SELECT 1 FROM ing_vector_routes v WHERE v.source_id=ing_sources.source_id),
                EXISTS (SELECT 1 FROM ing_model_routes m
                        WHERE m.source_id=ing_sources.source_id AND m.fingerprint<>?1)
         FROM ing_sources ORDER BY created_at",
    )?;
    let rows = stmt.query_map([extract_route], |r| {
        let categories: String = r.get(4)?;
        Ok(json!({
            "source_id": r.get::<_, String>(0)?,
            "type": r.get::<_, String>(1)?,
            "account": r.get::<_, String>(2)?,
            "namespace": r.get::<_, String>(3)?,
            "categories": parse_json(&categories),
            "cursor": r.get::<_, String>(5)?,
            "created_at": r.get::<_, String>(6)?,
            "updated_at": r.get::<_, String>(7)?,
            "embed_enabled": r.get::<_, bool>(8)?,
            "extract_reauthorization_required": r.get::<_, bool>(9)?,
        }))
    })?;
    let mut out = Vec::new();
    for row in rows {
        let mut value = row?;
        let sid = value["source_id"].as_str().unwrap_or_default().to_string();
        value["capture_enabled"] = json!(grant_active_for(conn, &sid, "capture")?);
        value["extract_enabled"] = json!(grant_active_for(conn, &sid, "extract")?);
        out.push(value);
    }
    Ok(Value::Array(out))
}

/// Enable or disable capture for a source (the `stop_capture` /
/// `resume_capture` control state). This is the helper the connector owner
/// uses; the authority owner may instead flip the `ing_grants` row directly
/// inside its own governance transaction — both write the same flag.
pub fn set_capture(conn: &Connection, source_id: &str, enabled: bool) -> Result<Value> {
    let now = now_utc();
    let changed = conn.execute(
        "UPDATE ing_grants SET active = ?1, updated_at = ?2
         WHERE source_id = ?3 AND purpose = 'capture'",
        params![enabled as i64, now, source_id],
    )?;
    if changed == 0 {
        return Err(Error::missing(format!(
            "source '{source_id}' is not registered"
        )));
    }
    conn.execute(
        "UPDATE ing_sources SET updated_at = ?1 WHERE source_id = ?2",
        params![now, source_id],
    )?;
    Ok(json!({ "source_id": source_id, "capture_enabled": enabled }))
}

// ---------------------------------------------------------------------------
// Page acceptance (D1 → D2 submission boundary)
// ---------------------------------------------------------------------------

/// Accept one page of source changes inside a single transaction.
///
/// * `actor` — authenticated adapter identity (resolved by the HTTP layer;
///   this module only checks the binding, it never trusts names in bodies).
/// * `key` — `Idempotency-Key`, scoped to (source, actor, operation).
/// * `page` — `{"source_id","cursor","next_cursor","events":[...]}` where
///   each event is `{"native_object","native_version"?,"actor_kind","at",
///   "task_id"?,"text"|"checks"?,"change_kind"?}`.
///
/// Order of checks (each fail-closed, no partial writes):
/// 1. Shape and field types of the page envelope.
/// 2. Credential removal from event content; the *sanitized canonical page*
///    is the fingerprint basis.
/// 3. Replay: same key + same fingerprint → the original receipt, byte for
///    byte, before any cursor comparison; same key + different fingerprint →
///    `key_conflict`.
/// 4. Source bound to actor; capture grant active; page start cursor equals
///    the committed cursor (otherwise the connector must re-read from the
///    committed cursor reported in the error).
/// 5. Events: roots and versions deduplicated, revocations applied in the
///    same transaction, extraction jobs queued for new active versions, and
///    the source cursor advanced last.
pub fn accept(
    conn: &mut Connection,
    actor: &str,
    key: &str,
    page: Value,
    profile: &str,
) -> Result<Value> {
    let tx = conn.transaction()?;
    let receipt = accept_in(&tx, actor, key, page, profile)?;
    tx.commit()?;
    Ok(receipt)
}

pub(crate) fn accept_in(
    tx: &rusqlite::Transaction<'_>,
    actor: &str,
    key: &str,
    page: Value,
    profile: &str,
) -> Result<Value> {
    accept_page(tx, actor, key, page, profile, None)
}

pub(crate) fn accept_history_task(
    tx: &rusqlite::Transaction<'_>,
    task_id: &str,
    key: &str,
    page: Value,
    profile: &str,
) -> Result<Value> {
    accept_page(tx, "owner", key, page, profile, Some(task_id))
}

fn accept_page(
    tx: &rusqlite::Transaction<'_>,
    actor: &str,
    key: &str,
    page: Value,
    profile: &str,
    history_task: Option<&str>,
) -> Result<Value> {
    if actor.trim().is_empty() {
        return Err(Error::invalid("actor must not be empty"));
    }
    if key.trim().is_empty() {
        return Err(Error::invalid("idempotency key must not be empty"));
    }
    let obj = page
        .as_object()
        .ok_or_else(|| Error::invalid("page must be a JSON object"))?;
    let source_id = require_str(obj, "source_id")?.to_string();
    let cursor_in = match obj.get("cursor") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => {
            return Err(Error::invalid(
                "page field 'cursor' must be a string or null",
            ));
        }
    };
    let next_cursor = require_str(obj, "next_cursor")?.to_string();
    let events_in = obj
        .get("events")
        .and_then(Value::as_array)
        .filter(|events| !events.is_empty() && events.len() <= 100)
        .ok_or_else(|| Error::invalid("page must contain 1 to 100 events"))?
        .clone();

    // Sanitize first: credentials never reach the fingerprint, the event
    // rows, or the job inputs.
    let events: Vec<Value> = events_in
        .iter()
        .map(|e| {
            let mut e = e.clone();
            redact_secrets(&mut e);
            e
        })
        .collect();
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(canonical(&json!({
            "source_id": source_id,
            "cursor": cursor_in,
            "next_cursor": next_cursor,
            "events": events,
        })))
    );

    let source = fetch_source_row(tx, &source_id)?
        .ok_or_else(|| Error::missing(format!("source '{source_id}' is not registered")))?;

    // Replay / key_conflict — content equality is decided *before* the
    // cursor check so that resends after a cursor move still return the
    // original receipt and never roll the cursor back.
    if let Some((stored_fp, stored_receipt)) = tx
        .query_row(
            "SELECT fingerprint, receipt FROM ing_ingests
             WHERE source_id = ?1 AND actor = ?2 AND idem_key = ?3",
            params![source_id, actor, key],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()?
    {
        if stored_fp != fingerprint {
            return Err(Error::conflict(format!(
                "key_conflict: idempotency key '{key}' for source '{source_id}' was already committed with different page content"
            )));
        }
        return Ok(serde_json::from_str(&stored_receipt)?);
    }

    // Entrance authorization.
    let bound: Option<i64> = tx
        .query_row(
            "SELECT active FROM ing_actors
             WHERE source_id = ?1 AND actor = ?2 AND action = 'accept'",
            params![source_id, actor],
            |r| r.get(0),
        )
        .optional()?;
    if bound != Some(1) {
        return Err(Error::denied(format!(
            "action_denied: adapter '{actor}' is not bound to source '{source_id}' for page submission"
        )));
    }
    let history_kind: Option<String> = if let Some(task_id) = history_task {
        tx.query_row(
            "SELECT kind FROM history_task_jobs
              WHERE task_id=?1 AND source_id=?2 AND state='running'
                AND (targets='[]' OR kind='archive')",
            params![task_id, source_id],
            |row| row.get(0),
        )
        .optional()?
    } else {
        None
    };
    let permitted = if history_task.is_some() {
        history_kind.is_some()
    } else {
        grant_active_in(tx, &source_id, "capture")?
    };
    if !permitted {
        return Err(Error::denied(format!(
            "action_denied: capture is not enabled for source '{source_id}'"
        )));
    }
    if source.cursor != cursor_in {
        return Err(Error::conflict(format!(
            "cursor_mismatch: page starts at '{}' but the committed cursor is '{}'; resume from the committed cursor",
            cursor_in, source.cursor
        )));
    }

    let received_at = now_utc();
    let mut dispositions = Vec::with_capacity(events.len());
    let mut jobs_created = 0usize;

    for (ordinal, event) in events.iter().enumerate() {
        // A rejected row rolls back the whole page, including earlier versions.
        let p = plan_event(tx, &source, event, ordinal)?;
        if p.change_kind == "revoked" {
            withdraw_root(tx, &p.root_id, true)?;
        }
        let disposition = p.action.label();
        match p.action {
            EventAction::Duplicate { event_id }
            | EventAction::RevokedBlocked { event_id }
            | EventAction::RevokedApply { event_id } => {
                dispositions.push(json!({
                    "ordinal": p.ordinal,
                    "native_object": p.native_object,
                    "event_id": event_id,
                    "disposition": disposition,
                }));
            }
            EventAction::Insert | EventAction::RevokeUnknown | EventAction::RootBlocked => {
                let active = matches!(p.action, EventAction::Insert);
                tx.execute(
                    "INSERT INTO ing_event_versions
                     (event_id, root_id, source_id, native_object, native_version,
                      version_key, actor_kind, occurred_at, change_kind, task_id,
                      content, active, received_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                    params![
                        p.event_id,
                        p.root_id,
                        source_id,
                        p.native_object,
                        p.native_version,
                        p.version_key,
                        p.actor_kind,
                        p.occurred_at,
                        p.change_kind,
                        p.task_id,
                        if active { p.content.as_str() } else { "{}" },
                        active as i64,
                        received_at,
                    ],
                )?;
                if active {
                    let job_id = new_id("job");
                    let inserted = tx.execute(
                        "INSERT OR IGNORE INTO ing_jobs
                         (job_id, source_id, event_id, intake_version, input,
                          status, attempts, available_at, created_at, updated_at)
                         VALUES (?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, ?6, ?6)",
                        params![
                            job_id,
                            source_id,
                            p.event_id,
                            p.version_key,
                            canonical(&p.job_input.expect("active events carry a job input")),
                            received_at,
                        ],
                    )?;
                    jobs_created += inserted;
                }
                dispositions.push(json!({
                    "ordinal": p.ordinal,
                    "native_object": p.native_object,
                    "event_id": p.event_id,
                    "disposition": disposition,
                }));
            }
        }
    }

    // Owner-confirmed archive pages have their own transactional checkpoint.
    // An archive import cannot advance the continuous adapter's source cursor.
    if history_kind.as_deref() != Some("archive") {
        tx.execute(
            "UPDATE ing_sources SET cursor = ?1, updated_at = ?2 WHERE source_id = ?3",
            params![next_cursor, received_at, source_id],
        )?;
    }

    let receipt = json!({
        "ingest_id": new_id("ing"),
        "source_id": source_id,
        "actor": actor,
        "status": "accepted",
        "replayed": false,
        "cursor": next_cursor,
        "jobs_created": jobs_created,
        "events": dispositions,
        "committed_at": received_at,
    });
    crate::retrieval::refresh(tx, profile, &receipt)?;
    tx.execute(
        "INSERT INTO ing_ingests
         (ingest_id, source_id, actor, idem_key, fingerprint, receipt,
          cursor_before, cursor_after, committed_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            receipt["ingest_id"].as_str().unwrap_or_default(),
            source_id,
            actor,
            key,
            fingerprint,
            canonical(&receipt),
            cursor_in,
            next_cursor,
            received_at,
        ],
    )?;
    Ok(receipt)
}
// ---------------------------------------------------------------------------
// D3 leased queue
// ---------------------------------------------------------------------------

pub fn job_candidates(conn: &Connection, job_id: &str) -> Result<Value> {
    let input: String = conn
        .query_row(
            "SELECT j.input FROM ing_jobs j
             JOIN ing_event_versions e ON e.event_id=j.event_id AND e.active=1
             WHERE j.job_id=?1 AND j.status='done'",
            [job_id],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("job candidates are unavailable"))?;
    let mut query = conn.prepare_cached(
        "SELECT candidate_id, kind, subject, key, value, condition, support
         FROM ing_candidates WHERE job_id=?1 ORDER BY ordinal",
    )?;
    let candidates = query
        .query_map([job_id], |r| {
            Ok(json!({
                "candidate_id": r.get::<_, String>(0)?,
                "kind": r.get::<_, String>(1)?,
                "subject": r.get::<_, String>(2)?,
                "key": r.get::<_, String>(3)?,
                "value": parse_json(&r.get::<_, String>(4)?),
                "condition": r.get::<_, Option<String>>(5)?,
                "support": parse_json(&r.get::<_, String>(6)?),
            }))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let cited: std::collections::HashSet<_> = candidates
        .iter()
        .flat_map(|candidate| candidate["support"].as_array().into_iter().flatten())
        .filter_map(Value::as_str)
        .collect();
    let input = parse_json(&input);
    let evidence: Vec<_> = input["inputs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|span| cited.contains(span["span_id"].as_str().unwrap_or_default()))
        .collect();
    Ok(json!({
        "job_id": job_id,
        "source_id": input["source_id"],
        "event_id": input["event_id"],
        "intake_version": input["intake_version"],
        "occurred_at": input["at"],
        "candidates": candidates,
        "evidence": evidence,
    }))
}

/// Serve the least recently processed source first. Within that source, claim
/// the earliest due input. Repeated failures move to their retry time so they
/// cannot occupy the front of the queue solely because they were created first.
///
/// Runnable means: (`pending` or `retrying` and due, or `running` with an
/// expired lease) **and** the input version is still active **and** the
/// source's extraction grant is still active. Revoked or ungrantable inputs
/// stay invisible to workers — fail-closed, they are never handed out again.
/// Returns `Ok(None)` when the queue is empty or everything is blocked.
pub fn claim(conn: &mut Connection, route: &str) -> Result<Option<Job>> {
    let now = now_utc();
    let lease_until = utc_plus(LEASE_SECONDS);
    let tx = conn.transaction()?;
    let found = tx
        .query_row(
            "SELECT j.job_id,j.event_id,j.input,j.intake_version,j.attempts
             FROM ing_jobs j
             WHERE j.rowid=(
                 SELECT candidate.rowid
                 FROM ing_model_routes m JOIN ing_grants g
                   ON g.source_id=m.source_id AND g.purpose='extract' AND g.active=1
                 JOIN ing_jobs candidate ON candidate.rowid=(
                     SELECT q.rowid FROM ing_jobs q
                     WHERE q.source_id=m.source_id
                       AND q.status IN ('pending','retrying','running')
                       AND (CASE WHEN q.status='running' THEN q.lease_expires_at ELSE q.available_at END)<=?1
                       AND EXISTS(SELECT 1 FROM ing_event_versions e
                                  WHERE e.event_id=q.event_id AND e.active=1
                                  AND NOT EXISTS(SELECT 1 FROM ing_extract_denials d WHERE d.root_id=e.root_id))
                     ORDER BY (CASE WHEN q.status='running' THEN q.lease_expires_at ELSE q.available_at END),q.rowid LIMIT 1
                 )
                 WHERE m.fingerprint=?2
                 AND NOT EXISTS(SELECT 1 FROM ing_extract_pauses pause WHERE pause.source_id=m.source_id)
                 ORDER BY (SELECT recent.updated_at FROM ing_jobs recent
                           WHERE recent.source_id=m.source_id AND recent.attempts>0
                           ORDER BY recent.updated_at DESC LIMIT 1),
                          candidate.created_at,candidate.rowid LIMIT 1
             )",
            params![now, route],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            },
        )
        .optional()?;
    let Some((id, event_id, input, intake_version, attempts)) = found else {
        // Dropping the transaction releases the read; nothing was written.
        return Ok(None);
    };
    tx.execute(
        "UPDATE ing_jobs SET status = 'running', lease_expires_at = ?1,
                attempts = attempts + 1, updated_at = ?2
         WHERE job_id = ?3",
        params![lease_until, now, id],
    )?;
    tx.execute(
        "UPDATE ing_job_attempts SET finished_at=?1,outcome='interrupted'
         WHERE job_id=?2 AND attempt=?3 AND outcome='running'",
        params![now, id, attempts],
    )?;
    let attempt = attempts + 1;
    tx.execute(
        "INSERT INTO ing_job_attempts(job_id,attempt,route,started_at,outcome)
         VALUES (?1,?2,?3,?4,'running')",
        params![id, attempt, route, now],
    )?;
    tx.commit()?;
    Ok(Some(Job {
        id,
        event_id,
        input: parse_json(&input),
        intake_version,
        attempt,
    }))
}

/// Atomically persist a job's completion together with its candidates.
///
/// `candidates` is either the array itself or `{"candidates":[...]}` (the
/// shape [`crate::providers::extract`] returns). Every candidate must locate
/// support in a span of the job's frozen input window; unknown span ids are
/// rejected per the parse-stage rules so a model cannot invent provenance.
///
/// Inside the write transaction the version is rechecked still active and
/// the extraction grant still active, and the lease must not have expired
/// (otherwise another worker may already own the job). On revocation or
/// grant loss the call fails and nothing is persisted.
///
/// **Storing a candidate does not make it a memory.** Acceptance, fact
/// revisions, and adjudication are D2's, in their own transaction.
pub fn finish(conn: &mut Connection, job: &Job, candidates: Value, route: &str) -> Result<Value> {
    let list = match &candidates {
        Value::Array(items) => items.clone(),
        Value::Object(map) => map
            .get("candidates")
            .and_then(Value::as_array)
            .cloned()
            .ok_or_else(|| {
                Error::invalid("candidates must be an array or an object with a 'candidates' array")
            })?,
        _ => {
            return Err(Error::invalid(
                "candidates must be an array or an object with a 'candidates' array",
            ));
        }
    };
    if list.len() > 64 {
        return Err(Error::invalid(
            "too many candidates in one finish batch (max 64)",
        ));
    }

    // Spans known to the frozen input window; support must locate there.
    let known_spans: Vec<String> = job
        .input
        .get("inputs")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|i| i.get("span_id").and_then(Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();

    struct Candidate {
        kind: String,
        subject: String,
        key: String,
        value: String,
        condition: Option<String>,
        spans: Vec<String>,
        invalid_support: bool,
    }
    let mut prepared = Vec::with_capacity(list.len());
    for (ordinal, c) in list.iter().enumerate() {
        let mut proposal = c.clone();
        redact_secrets(&mut proposal);
        let obj = proposal
            .as_object()
            .ok_or_else(|| Error::invalid(format!("candidate {ordinal} must be an object")))?;
        let kind = require_str(obj, "kind")?.to_string();
        let subject = require_str(obj, "subject")?.to_string();
        let key = require_str(obj, "key")?.to_string();
        let value = canonical(obj.get("value").unwrap_or(&Value::Null));
        let condition = match obj.get("condition") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                return Err(Error::invalid(format!(
                    "candidate {ordinal}: condition must be a string or null"
                )));
            }
        };
        let support = obj
            .get("support")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                Error::invalid(format!(
                    "candidate {ordinal}: support must be an array of span ids"
                ))
            })?;
        let mut spans = Vec::with_capacity(support.len());
        let mut invalid_support = support.is_empty();
        for s in support {
            let sid = s.as_str().ok_or_else(|| {
                Error::invalid(format!(
                    "candidate {ordinal}: support entries must be strings"
                ))
            })?;
            if known_spans.iter().any(|k| k == sid) {
                spans.push(sid.to_string());
            } else {
                invalid_support = true;
            }
        }
        prepared.push(Candidate {
            kind,
            subject,
            key,
            value,
            condition,
            spans,
            invalid_support,
        });
    }

    let now = now_utc();
    let tx = conn.transaction()?;
    let row = tx
        .query_row(
            "SELECT status, lease_expires_at, source_id, event_id, attempts
             FROM ing_jobs WHERE job_id = ?1",
            params![job.id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            },
        )
        .optional()?
        .ok_or_else(|| Error::missing(format!("job '{}' no longer exists", job.id)))?;
    let (status, lease, job_source, job_event, attempt) = row;
    if status != "running" || attempt != job.attempt {
        return Err(Error::conflict(format!(
            "job '{}' is '{status}'; only a running job can be finished",
            job.id
        )));
    }
    if lease.as_deref().map(|l| l <= now.as_str()).unwrap_or(true) {
        return Err(Error::conflict(format!(
            "lease for job '{}' has expired or is missing; the result was not persisted",
            job.id
        )));
    }
    // Fail-closed rechecks against durable state, not the passed snapshot.
    let version_active: Option<i64> = tx
        .query_row(
            "SELECT active AND NOT EXISTS(SELECT 1 FROM ing_extract_denials d WHERE d.root_id=e.root_id)
             FROM ing_event_versions e WHERE event_id = ?1",
            params![job_event],
            |r| r.get(0),
        )
        .optional()?;
    if version_active != Some(1) {
        return Err(Error::conflict(format!(
            "input version for job '{}' is no longer active (revoked or removed); result discarded",
            job.id
        )));
    }
    let route_granted: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_model_routes WHERE source_id=?1 AND fingerprint=?2)",
        params![job_source, route],
        |r| r.get(0),
    )?;
    if !route_granted || !grant_active_in(&tx, &job_source, "extract")? {
        return Err(Error::denied(format!(
            "extraction grant for source '{job_source}' is not active; result discarded"
        )));
    }

    let valid = list
        .iter()
        .zip(&prepared)
        .filter(|(_, candidate)| !candidate.invalid_support)
        .map(|(proposal, _)| proposal.clone())
        .collect::<Vec<_>>();
    let verified = crate::authority::adjudicate(&tx, &job.input, &valid)?;
    let verified_spans = if valid.is_empty() {
        std::collections::BTreeSet::new()
    } else {
        let mut query =
            tx.prepare("SELECT span_id FROM authority_observations WHERE event_id=?1")?;
        query
            .query_map([job.event_id.as_str()], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<std::collections::BTreeSet<_>>>()?
    };
    let mut credited = std::collections::BTreeSet::new();
    let mut decisions = Vec::new();
    for (
        ordinal,
        Candidate {
            kind,
            subject,
            key,
            value,
            condition,
            spans,
            invalid_support,
        },
    ) in prepared.into_iter().enumerate()
    {
        let candidate_id = new_id("cand");
        let (decision, reason) = if invalid_support {
            ("rejected", "missing_source_span")
        } else if !spans.iter().any(|span| verified_spans.contains(span)) {
            ("pending", "source_does_not_verify_proposal")
        } else {
            let mut added_credit = false;
            // Credit every verified span, even after the first new one is found.
            for span in spans.iter().filter(|span| verified_spans.contains(*span)) {
                added_credit |= credited.insert(span.clone());
            }
            if added_credit {
                ("adopted", "source_observation_verified")
            } else {
                ("merged", "same_verified_source_span")
            }
        };
        // Retain the disposition and a stable candidate ID without persisting
        // unsupported proposal text or a model-invented source reference.
        let (subject, key, value, condition, spans) = if invalid_support {
            (
                String::new(),
                String::new(),
                "null".into(),
                None,
                Vec::new(),
            )
        } else {
            (subject, key, value, condition, spans)
        };
        tx.execute(
            "INSERT INTO ing_candidates
             (candidate_id, job_id, ordinal, kind, subject, key, value, condition, support, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                candidate_id,
                job.id,
                ordinal as i64,
                kind,
                subject,
                key,
                value,
                condition,
                canonical(&json!(spans)),
                now,
            ],
        )?;
        decisions.push(json!({"candidate_id":candidate_id,"kind":decision,"reason":reason}));
    }
    let recorded = decisions.len();
    tx.execute(
        "UPDATE ing_jobs SET status = 'done', lease_expires_at = NULL, fail_reason = NULL,
                result = ?1, updated_at = ?2
         WHERE job_id = ?3",
        params![
            canonical(
                &json!({ "candidates_recorded": recorded, "observations_verified": verified, "decisions":decisions })
            ),
            now,
            job.id
        ],
    )?;
    record_attempt(&tx, job, "done", None, &now)?;
    tx.commit()?;
    Ok(json!({
        "job_id": job.id,
        "event_id": job.event_id,
        "intake_version": job.intake_version,
        "status": "done",
        "candidates_recorded": recorded,
        "observations_verified": verified,
    }))
}
/// Item failures have a separate budget from interrupted leases and model outages.
pub fn fail(conn: &Connection, job: &Job, reason: &str) -> Result<()> {
    let tx = conn.unchecked_transaction()?;
    let failures: Option<i64> = tx.query_row(
        "SELECT retry_failures FROM ing_jobs WHERE job_id=?1 AND status='running' AND attempts=?2",
        params![job.id, job.attempt],
        |row| row.get(0),
    ).optional()?;
    if let Some(failures) = failures {
        let failures = failures + 1;
        let status = if failures > MAX_RETRIES {
            "failed"
        } else {
            "retrying"
        };
        let now = now_utc();
        let due = if status == "retrying" {
            utc_plus(30i64 << failures)
        } else {
            now.clone()
        };
        tx.execute(
            "UPDATE ing_jobs SET status=?1,retry_failures=?2,fail_reason=?3,
                 lease_expires_at=NULL,available_at=?4,updated_at=?5 WHERE job_id=?6",
            params![status, failures, reason, due, now, job.id],
        )?;
        record_attempt(&tx, job, status, Some(reason), &now)?;
    }
    tx.commit()?;
    Ok(())
}

pub fn defer(conn: &Connection, job: &Job, outcome: &str, reason: Option<&str>) -> Result<()> {
    let now = now_utc();
    conn.execute(
        "UPDATE ing_jobs SET status='pending',lease_expires_at=NULL,available_at=?1,
             fail_reason=?2,updated_at=?1 WHERE job_id=?3 AND status='running' AND attempts=?4",
        params![now, reason, job.id, job.attempt],
    )?;
    record_attempt(conn, job, outcome, reason, &now)?;
    Ok(())
}

fn record_attempt(
    conn: &Connection,
    job: &Job,
    outcome: &str,
    reason: Option<&str>,
    now: &str,
) -> Result<()> {
    conn.execute(
        "UPDATE ing_job_attempts SET finished_at=?1,outcome=?2,error_code=?3
         WHERE job_id=?4 AND attempt=?5 AND outcome='running'",
        params![now, outcome, reason, job.id, job.attempt],
    )?;
    Ok(())
}

/// Requeue a blocked or failed job immediately (the management console's
/// retry entry). Completed and revoked jobs are refused; a job currently
/// under an unexpired lease must first expire so two workers never finish
/// the same input version.
pub fn retry(conn: &Connection, id: &str) -> Result<Value> {
    let now = now_utc();
    let row = conn
        .query_row(
            "SELECT j.status,j.lease_expires_at,
                    e.active AND NOT EXISTS(SELECT 1 FROM ing_extract_denials d WHERE d.root_id=e.root_id)
                    AND EXISTS(SELECT 1 FROM ing_grants g WHERE g.source_id=j.source_id AND g.purpose='extract' AND g.active=1)
             FROM ing_jobs j JOIN ing_event_versions e ON j.event_id=e.event_id WHERE job_id=?1",
            params![id],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, bool>(2)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing(format!("job '{id}' not found")))?;
    let (status, lease, authorized) = row;
    match status.as_str() {
        "done" => {
            return Err(Error::conflict(format!(
                "job '{id}' is already done; its recorded candidates are the durable result"
            )));
        }
        "cancelled" => {
            return Err(Error::conflict(format!(
                "job '{id}' was cancelled by an upstream revocation; replay the revocation target as a new input version instead"
            )));
        }
        "running" if lease.as_deref().map(|l| l > now.as_str()).unwrap_or(false) => {
            return Err(Error::conflict(format!(
                "job '{id}' is leased until {}; wait for expiry before retrying",
                lease.unwrap_or_default()
            )));
        }
        "retrying" => {
            return Err(Error::conflict("该作业仍处于自动重试周期"));
        }
        _ => {}
    }
    if !authorized {
        return Err(Error::denied("当前输入或提炼授权已失效，无法重新排队"));
    }
    if status == "pending" {
        return fetch_job(conn, id);
    }
    conn.execute(
        "UPDATE ing_jobs SET status = 'pending', available_at = ?1,
                lease_expires_at = NULL, fail_reason = NULL, retry_failures=0, updated_at = ?1
         WHERE job_id = ?2",
        params![now, id],
    )?;
    fetch_job(conn, id)
}

// ---------------------------------------------------------------------------
// Internals: page planning
// ---------------------------------------------------------------------------

struct SourceRow {
    source_id: String,
    kind: String,
    account: String,
    namespace: String,
    categories: String,
    cursor: String,
    created_at: String,
    updated_at: String,
}

enum EventAction {
    /// Already accepted and still active: reuse the original event id.
    Duplicate {
        event_id: String,
    },
    /// Upstream revoked this exact version before; replays stay blocked.
    RevokedBlocked {
        event_id: String,
    },
    /// This page carries the proven revocation of a known version.
    RevokedApply {
        event_id: String,
    },
    /// New version row plus one queued extraction job.
    Insert,
    /// Revocation names a version we never accepted: recorded inert.
    RevokeUnknown,
    RootBlocked,
}

impl EventAction {
    fn label(&self) -> &'static str {
        match self {
            EventAction::Duplicate { .. } => "duplicate",
            EventAction::RevokedBlocked { .. } => "revoked_blocked",
            EventAction::RevokedApply { .. } => "revoked",
            EventAction::Insert => "accepted",
            EventAction::RevokeUnknown => "revoked_unknown",
            EventAction::RootBlocked => "revoked_blocked",
        }
    }
}

struct PlannedEvent {
    ordinal: usize,
    native_object: String,
    native_version: Option<String>,
    version_key: String,
    actor_kind: String,
    occurred_at: String,
    change_kind: String,
    task_id: Option<String>,
    content: String,
    root_id: String,
    event_id: String,
    action: EventAction,
    job_input: Option<Value>,
}

/// Validate one sanitized event and resolve its dedup state. Runs entirely
/// before any write of the page so a single invalid event rejects the page
/// without side effects.
fn plan_event(
    conn: &Connection,
    source: &SourceRow,
    event: &Value,
    ordinal: usize,
) -> Result<PlannedEvent> {
    let bad = |m: String| Error::invalid(format!("events[{ordinal}]: {m}"));
    let obj = event
        .as_object()
        .ok_or_else(|| bad("event must be a JSON object".into()))?;
    let native_object = require_str(obj, "native_object")
        .map_err(|_| bad("native_object must be a non-empty string".into()))?
        .to_string();
    let actor_kind = require_str(obj, "actor_kind")
        .map_err(|_| bad("actor_kind must be a non-empty string".into()))?
        .to_string();
    if !ACTOR_KINDS.contains(&actor_kind.as_str()) {
        return Err(bad(format!(
            "actor_kind '{actor_kind}' must be one of {ACTOR_KINDS:?}"
        )));
    }
    let occurred_at = match obj.get("at") {
        None => now_utc(),
        Some(at) => at
            .as_str()
            .and_then(|at| chrono::DateTime::parse_from_rfc3339(at).ok())
            .map(|at| at.with_timezone(&Utc).to_rfc3339())
            .ok_or_else(|| bad("at must be a timezone-aware ISO 8601 string".into()))?,
    };
    let change_kind = match obj.get("change_kind") {
        None | Some(Value::Null) => "created".to_string(),
        Some(Value::String(s)) if CHANGE_KINDS.contains(&s.as_str()) => s.clone(),
        Some(Value::String(s)) => {
            return Err(bad(format!(
                "change_kind '{s}' must be one of {CHANGE_KINDS:?}"
            )));
        }
        Some(_) => return Err(bad("change_kind must be a string".into())),
    };
    let task_id = match obj.get("task_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if !s.trim().is_empty() => Some(s.clone()),
        Some(_) => return Err(bad("task_id must be a non-empty string".into())),
    };
    let mut native_version = match obj.get("native_version") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if !s.trim().is_empty() => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        Some(_) => return Err(bad("native_version must be a string or number".into())),
    };

    // Content by kind: prose for user/assistant turns, field map for tool
    // results. Bodies never override the authenticated actor_kind.
    let has_text = obj.contains_key("text");
    let has_checks = obj.contains_key("checks");
    if has_text && has_checks {
        return Err(bad("event must not carry both text and checks".into()));
    }
    let content: Value = if change_kind == "revoked" {
        if native_version.is_none() {
            return Err(bad(
                "a revoked event must name the native_version it revokes".into(),
            ));
        }
        json!({"revokes": native_version})
    } else if actor_kind == "tool_result" {
        if !has_checks {
            return Err(bad("tool_result events must carry a checks object".into()));
        }
        let checks = obj
            .get("checks")
            .and_then(Value::as_object)
            .ok_or_else(|| bad("checks must be an object".into()))?;
        if checks.is_empty() {
            return Err(bad("checks must not be empty".into()));
        }
        // These objects have already passed the same recursive credential redaction as every event.
        if checks.iter().any(|(field, value)| {
            crate::tool_receipt::is_typed(value)
                && (!crate::tool_receipt::valid_field(field)
                    || !crate::tool_receipt::valid_marker_check(value))
        }) {
            return Err(bad("invalid typed artifact check".into()));
        }
        json!({"checks": checks})
    } else {
        if !has_text {
            return Err(bad(format!("{actor_kind} events must carry a text string")));
        }
        let text = obj
            .get("text")
            .and_then(Value::as_str)
            .ok_or_else(|| bad("text must be a string".into()))?;
        json!({"text": text})
    };

    // Historical Claude records can share a turn's promptId with a different
    // displayed message. Keep v1 for the matching live Hook or prior import.
    if source.kind == "claude-code"
        && source.account == "local"
        && actor_kind == "user_message"
        && change_kind == "created"
        && native_version.as_deref() == Some("1")
        && let Some(message_id) = obj.get("transcript_message_id").and_then(Value::as_str)
    {
        let previous: Option<String> = conn.query_row(
            "SELECT json_extract(e.content,'$.text') FROM ing_roots r JOIN ing_event_versions e USING(root_id)
             WHERE r.namespace=?1 AND r.native_object=?2 AND e.version_key='v:1' AND e.active=1",
            params![source.namespace, native_object], |row| row.get(0),
        ).optional()?;
        if let Some(previous) = previous
            && !crate::history_hosts::matches_claude_prompt(
                &previous,
                content["text"].as_str().unwrap_or_default(),
            )
        {
            native_version = Some(format!("message:{message_id}"));
        }
    }

    // Stable version identity: native version, else canonical fingerprint
    // over content plus change kind (transport fields excluded).
    let version_key = match &native_version {
        Some(v) => format!("v:{v}"),
        None => format!(
            "fp:{}",
            format_args!(
                "{:x}",
                Sha256::digest(canonical(&json!({
                    "actor_kind": actor_kind,
                    "change_kind": change_kind,
                    "task_id": task_id,
                    "content": content,
                })))
            )
        ),
    };

    // Root: account namespace + native object id.
    let root_id: String =
        match conn
            .query_row(
                "SELECT root_id FROM ing_roots WHERE namespace = ?1 AND native_object = ?2",
                params![source.namespace, native_object],
                |r| r.get(0),
            )
            .optional()?
        {
            Some(root_id) => root_id,
            None => {
                let root_id = new_id("root");
                conn.execute(
                "INSERT INTO ing_roots (root_id, source_id, namespace, native_object, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![root_id, source.source_id, source.namespace, native_object, now_utc()],
            )?;
                root_id
            }
        };

    let existing: Option<(String, i64)> = conn
        .query_row(
            "SELECT event_id, active FROM ing_event_versions
             WHERE root_id = ?1 AND version_key = ?2",
            params![root_id, version_key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;

    let withdrawn: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_root_revocations WHERE root_id=?1)",
        [&root_id],
        |r| r.get(0),
    )?;
    let new_event_id = new_id("evt");
    let (event_id, action, job_input) = if change_kind == "revoked" {
        match existing {
            Some((existing_id, _)) => (
                existing_id.clone(),
                EventAction::RevokedApply {
                    event_id: existing_id,
                },
                None,
            ),
            None => (new_event_id.clone(), EventAction::RevokeUnknown, None),
        }
    } else {
        match existing {
            Some((existing_id, 1)) => (
                existing_id.clone(),
                EventAction::Duplicate {
                    event_id: existing_id,
                },
                None,
            ),
            Some((existing_id, _)) => (
                existing_id.clone(),
                EventAction::RevokedBlocked {
                    event_id: existing_id,
                },
                None,
            ),
            None if withdrawn => (new_event_id.clone(), EventAction::RootBlocked, None),
            None => (
                new_event_id.clone(),
                EventAction::Insert,
                Some(build_job_input(
                    &new_event_id,
                    source,
                    (&format!("{native_object}@{version_key}"), &version_key),
                    &actor_kind,
                    &occurred_at,
                    task_id.as_deref(),
                    &content,
                )),
            ),
        }
    };

    Ok(PlannedEvent {
        ordinal,
        native_object,
        native_version,
        version_key,
        actor_kind,
        occurred_at,
        change_kind,
        task_id,
        content: canonical(&content),
        root_id,
        event_id,
        action,
        job_input,
    })
}

/// Freeze the per-version evidence window a D3 job will extract from: one
/// span per independently locatable field (the whole `text`, or one entry
/// per `checks/<field>` of a tool receipt), so a model answer can cite —
/// and only cite — positions that exist in this input version.
fn build_job_input(
    event_id: &str,
    source: &SourceRow,
    identity: (&str, &str),
    actor_kind: &str,
    occurred_at: &str,
    task_id: Option<&str>,
    content: &Value,
) -> Value {
    let (event_ref, version_key) = identity;
    let mut inputs = Vec::new();
    if let Some(text) = content.get("text").and_then(Value::as_str) {
        inputs.push(json!({
            "event_ref": event_ref,
            "kind": actor_kind,
            "span_id": format!("{event_id}#/text"),
            "field": "/text",
            "value": text,
        }));
    }
    if let Some(checks) = content.get("checks").and_then(Value::as_object) {
        let mut keys: Vec<&String> = checks.keys().collect();
        keys.sort();
        for k in keys {
            inputs.push(json!({
                "event_ref": event_ref,
                "kind": actor_kind,
                "span_id": format!("{event_id}#/checks/{k}"),
                "field": format!("/checks/{k}"),
                "value": checks[k],
            }));
        }
    }
    json!({
        "source_id": source.source_id,
        "event_id": event_id,
        "intake_version": version_key,
        "actor_kind": actor_kind,
        "at": occurred_at,
        "task_id": task_id,
        "inputs": inputs,
    })
}
// ---------------------------------------------------------------------------
// Internals: small helpers
// ---------------------------------------------------------------------------

fn now_utc() -> String {
    Utc::now().to_rfc3339()
}

fn utc_plus(seconds: i64) -> String {
    (Utc::now() + Duration::seconds(seconds)).to_rfc3339()
}

fn new_id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4().simple())
}

/// Deterministic canonical JSON: object keys sorted recursively so two
/// transports of the same page fingerprint identically and floats/whitespace
/// of the wire format never leak into dedup state.
fn canonical(value: &Value) -> String {
    serde_json::to_string(&sort_value(value))
        .expect("canonical JSON of a serde_json::Value cannot fail to serialize")
}

fn sort_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for k in keys {
                out.insert(k.clone(), sort_value(&map[k]));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_value).collect()),
        other => other.clone(),
    }
}

fn parse_json(text: &str) -> Value {
    serde_json::from_str(text).unwrap_or_else(|_| Value::String(text.to_string()))
}

/// Remove credential-named members anywhere in a payload. This is the
/// intake-boundary hygiene rule: nothing secret is fingerprinted, stored,
/// or queued. Name-based redaction is best-effort by design; the HTTP owner
/// additionally guarantees values never appear in logs.
fn redact_secrets(value: &mut Value) {
    match value {
        Value::Object(map) => {
            map.retain(|k, _| !is_secret_key(k));
            for (_, v) in map.iter_mut() {
                redact_secrets(v);
            }
        }
        Value::Array(items) => {
            for v in items.iter_mut() {
                redact_secrets(v);
            }
        }
        Value::String(text) => *text = crate::authority::redact(text),
        _ => {}
    }
}

fn is_secret_key(key: &str) -> bool {
    let lower = key.to_lowercase();
    SECRET_KEYS.contains(&lower.as_str())
}

fn require_str<'a>(obj: &'a Map<String, Value>, field: &str) -> Result<&'a str> {
    obj.get(field)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| Error::invalid(format!("field '{field}' must be a non-empty string")))
}

fn optional_str_array(obj: &Map<String, Value>, field: &str) -> Result<Vec<String>> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|i| {
                i.as_str()
                    .filter(|s| !s.trim().is_empty())
                    .map(str::to_string)
                    .ok_or_else(|| {
                        Error::invalid(format!("field '{field}' entries must be non-empty strings"))
                    })
            })
            .collect(),
        Some(_) => Err(Error::invalid(format!(
            "field '{field}' must be an array of strings"
        ))),
    }
}

fn fetch_source_row(conn: &Connection, source_id: &str) -> Result<Option<SourceRow>> {
    conn.query_row(
        "SELECT source_id, kind, account, namespace, categories, cursor, created_at, updated_at
         FROM ing_sources WHERE source_id = ?1",
        params![source_id],
        |r| {
            Ok(SourceRow {
                source_id: r.get(0)?,
                kind: r.get(1)?,
                account: r.get(2)?,
                namespace: r.get(3)?,
                categories: r.get(4)?,
                cursor: r.get(5)?,
                created_at: r.get(6)?,
                updated_at: r.get(7)?,
            })
        },
    )
    .optional()
    .map_err(Error::from)
}

fn fetch_source(conn: &Connection, source_id: &str) -> Result<Value> {
    let row = fetch_source_row(conn, source_id)?
        .ok_or_else(|| Error::missing(format!("source '{source_id}' is not registered")))?;
    Ok(json!({
        "source_id": row.source_id,
        "type": row.kind,
        "account": row.account,
        "namespace": row.namespace,
        "categories": parse_json(&row.categories),
        "cursor": row.cursor,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }))
}

fn grant_active_in(conn: &Connection, source_id: &str, purpose: &str) -> Result<bool> {
    Ok(conn
        .query_row(
            "SELECT active FROM ing_grants WHERE source_id = ?1 AND purpose = ?2",
            params![source_id, purpose],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        == Some(1))
}

fn grant_active_for(conn: &Connection, source_id: &str, purpose: &str) -> Result<bool> {
    grant_active_in(conn, source_id, purpose)
}

pub(crate) fn bind_actor_in(conn: &Connection, source_id: &str, actor: &str) -> Result<()> {
    if actor.trim().is_empty() {
        return Err(Error::invalid("actor must not be empty"));
    }
    conn.execute(
        "INSERT INTO ing_actors (source_id, actor, action, active, updated_at)
         VALUES (?1, ?2, 'accept', 1, ?3)
         ON CONFLICT (source_id, actor, action)
         DO UPDATE SET active = 1, updated_at = excluded.updated_at",
        params![source_id, actor, now_utc()],
    )?;
    Ok(())
}

fn actor_ids_in(conn: &Connection, source_id: &str) -> Result<Vec<Value>> {
    let mut stmt = conn.prepare(
        "SELECT actor FROM ing_actors
         WHERE source_id = ?1 AND action = 'accept' AND active = 1 ORDER BY actor",
    )?;
    let rows = stmt.query_map(params![source_id], |r| r.get::<_, String>(0))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(Value::String(row?));
    }
    Ok(out)
}

fn job_row_to_value(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": r.get::<_, String>(0)?,
        "source_id": r.get::<_, String>(1)?,
        "event_id": r.get::<_, String>(2)?,
        "intake_version": r.get::<_, String>(3)?,
        "input": parse_json(&r.get::<_, String>(4)?),
        "status": r.get::<_, String>(5)?,
        "attempts": r.get::<_, i64>(6)?,
        "available_at": r.get::<_, String>(7)?,
        "lease_expires_at": r.get::<_, Option<String>>(8)?,
        "fail_reason": r.get::<_, Option<String>>(9)?,
        "created_at": r.get::<_, String>(10)?,
        "updated_at": r.get::<_, String>(11)?,
        "candidates_recorded": r.get::<_, i64>(12)?,
        "retry_count": (r.get::<_, i64>(13)? - 1).clamp(0, MAX_RETRIES),
    }))
}

const JOB_SELECT: &str = "SELECT j.job_id, j.source_id, j.event_id, j.intake_version, j.input,
            j.status, j.attempts, j.available_at, j.lease_expires_at,
            CASE WHEN j.status<>'done' THEN j.fail_reason END, j.created_at, j.updated_at,
            (SELECT COUNT(*) FROM ing_candidates c WHERE c.job_id = j.job_id), j.retry_failures
     FROM ing_jobs j";

pub fn fetch_job(conn: &Connection, id: &str) -> Result<Value> {
    let sql = format!("{JOB_SELECT} WHERE j.job_id = ?1");
    let mut job = conn
        .query_row(&sql, params![id], job_row_to_value)
        .optional()?
        .ok_or_else(|| Error::missing(format!("job '{id}' not found")))?;
    let mut stmt = conn.prepare_cached(
        "SELECT attempt,started_at,finished_at,outcome,error_code FROM ing_job_attempts
         WHERE job_id=?1 ORDER BY attempt DESC LIMIT 50",
    )?;
    job["attempt_history"] = json!(
        stmt.query_map([id], |row| Ok(json!({
            "attempt":row.get::<_,i64>(0)?, "started_at":row.get::<_,String>(1)?,
            "finished_at":row.get::<_,Option<String>>(2)?, "outcome":row.get::<_,String>(3)?,
            "error_code":row.get::<_,Option<String>>(4)?,
        })))?
        .collect::<rusqlite::Result<Vec<_>>>()?
    );
    Ok(job)
}

pub fn set_extract(
    conn: &Connection,
    source_id: &str,
    enabled: bool,
    route: Option<&str>,
) -> Result<Value> {
    let route = if enabled {
        Some(route.ok_or_else(|| {
            Error::new(
                409,
                "model_not_ready",
                "请先在模型设置中测试并绑定提炼模型，可使用服务器 API 或本地 Ollama。",
            )
        })?)
    } else {
        None
    };
    let updated = conn.execute(
        "UPDATE ing_grants SET active=?2,updated_at=?3 WHERE source_id=?1 AND purpose='extract'",
        params![source_id, enabled, now_utc()],
    )?;
    if updated == 0 {
        return Err(Error::missing("source is unavailable"));
    }
    if let Some(route) = route {
        conn.execute("INSERT INTO ing_model_routes VALUES(?1,?2) ON CONFLICT(source_id) DO UPDATE SET fingerprint=excluded.fingerprint",params![source_id,route])?;
    } else {
        conn.execute(
            "DELETE FROM ing_model_routes WHERE source_id=?1",
            [source_id],
        )?;
    }
    Ok(json!({"source_id":source_id,"extract_enabled":enabled}))
}

fn erase_event(db: &Connection, id: &str, clean_vectors: bool) -> Result<()> {
    db.execute(
        "UPDATE ing_event_versions SET active=0,content='{}' WHERE event_id=?1",
        [id],
    )?;
    crate::retrieval::drop_event(db, id)?;
    if clean_vectors {
        crate::vector::drop_event(db, id)?;
    }
    db.execute("DELETE FROM authority_observations WHERE event_id=?1", [id])?;
    db.execute("DELETE FROM ing_candidates WHERE job_id IN (SELECT job_id FROM ing_jobs WHERE event_id=?1)",[id])?;
    db.execute("UPDATE ing_jobs SET status='cancelled',input='{}',result=NULL,fail_reason='input_withdrawn',lease_expires_at=NULL,updated_at=?2 WHERE event_id=?1",params![id,now_utc()])?;
    Ok(())
}

fn withdraw_root(db: &Connection, root: &str, clean_vectors: bool) -> Result<usize> {
    db.execute(
        "INSERT OR IGNORE INTO ing_root_revocations VALUES(?1,?2)",
        params![root, now_utc()],
    )?;
    let ids = {
        let mut q =
            db.prepare("SELECT event_id FROM ing_event_versions WHERE root_id=?1 AND active=1")?;
        q.query_map([root], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
    };
    for id in &ids {
        erase_event(db, id, clean_vectors)?;
    }
    Ok(ids.len())
}

pub fn forget_root(db: &Connection, root: &str) -> Result<Value> {
    if !db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_roots WHERE root_id=?1)",
        [root],
        |row| row.get::<_, bool>(0),
    )? {
        return Err(Error::missing("source root is unavailable"));
    }
    let withdrawn_events = withdraw_root(db, root, false)?;
    crate::governance::enqueue_vector_cleanup(db, root)?;
    Ok(json!({"root_id":root,"withdrawn_events":withdrawn_events}))
}

pub fn forget(db: &Connection, id: &str) -> Result<Value> {
    if !db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_sources WHERE source_id=?1)",
        [id],
        |row| row.get::<_, bool>(0),
    )? {
        return Err(Error::missing("source is unavailable"));
    }
    let roots = {
        let mut q = db.prepare("SELECT root_id FROM ing_roots WHERE source_id=?1")?;
        q.query_map([id], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
    };
    let mut withdrawn = 0;
    for root in roots {
        withdrawn += withdraw_root(db, &root, true)?;
    }
    Ok(json!({"source_id":id,"withdrawn_events":withdrawn}))
}

#[cfg(test)]
#[path = "../../../../tests/v2/ingestion_queue.rs"]
mod queue_tests;
