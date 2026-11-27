use crate::error::{Error, Result};
use crate::{authority, retrieval};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

#[derive(Clone, Debug)]
pub struct BoundClient {
    pub id: String,
    pub route_ref: String,
    capabilities: Capabilities,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Capabilities {
    load_context: bool,
    observe_loaded: bool,
    observe_used: bool,
    verify_outcomes: bool,
    manage_tasks: bool,
}

pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS context_policy (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO context_policy VALUES (1, 1);
        CREATE TABLE IF NOT EXISTS context_clients (
            id TEXT PRIMARY KEY, host TEXT NOT NULL, route TEXT NOT NULL UNIQUE,
            destination TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            capabilities TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS context_intents (
            actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
            request_hash TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(actor, operation, key)
        );
        CREATE TABLE IF NOT EXISTS context_tasks (
            id TEXT PRIMARY KEY, project TEXT, anchor TEXT NOT NULL, creator TEXT NOT NULL, title TEXT
        );
        CREATE TABLE IF NOT EXISTS context_task_closures (
            task_id TEXT PRIMARY KEY REFERENCES context_tasks(id),
            closed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_task_access (
            task_id TEXT NOT NULL REFERENCES context_tasks(id),
            client_id TEXT NOT NULL REFERENCES context_clients(id),
            PRIMARY KEY(task_id, client_id)
        );
        CREATE TABLE IF NOT EXISTS context_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES context_tasks(id),
            client_id TEXT NOT NULL REFERENCES context_clients(id), session TEXT NOT NULL,
            epoch TEXT NOT NULL, route TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_deliveries (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES context_clients(id),
            session TEXT NOT NULL, epoch TEXT NOT NULL, route TEXT NOT NULL,
            task_id TEXT REFERENCES context_tasks(id), attempt_id TEXT REFERENCES context_attempts(id),
            policy INTEGER NOT NULL, snapshot TEXT NOT NULL, package TEXT NOT NULL, mode TEXT NOT NULL,
            profile TEXT NOT NULL, package_state TEXT NOT NULL, required_budget INTEGER NOT NULL,
            provided TEXT NOT NULL, created_at TEXT NOT NULL, request_key TEXT, request_hash TEXT,
            UNIQUE(client_id, request_key)
        );
        CREATE INDEX IF NOT EXISTS context_delivery_lookup ON context_deliveries
            (client_id, session, epoch, snapshot);
        CREATE TABLE IF NOT EXISTS context_delivery_evidence (
            delivery_id TEXT PRIMARY KEY REFERENCES context_deliveries(id),
            fingerprint TEXT NOT NULL, handles TEXT NOT NULL,
            effective_profile TEXT, estimated_tokens INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_evidence_manifests (
            delivery_id TEXT PRIMARY KEY REFERENCES context_deliveries(id),
            entries TEXT NOT NULL,
            digest TEXT NOT NULL,
            base_delivery TEXT REFERENCES context_deliveries(id)
        );
        CREATE TABLE IF NOT EXISTS context_receipts (
            client_id TEXT NOT NULL REFERENCES context_clients(id), event_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL REFERENCES context_deliveries(id), request_hash TEXT NOT NULL,
            kind TEXT NOT NULL, result TEXT NOT NULL, complete INTEGER NOT NULL, model_call TEXT,
            PRIMARY KEY(client_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS context_receipt_delivery ON context_receipts (delivery_id, kind);
        CREATE TABLE IF NOT EXISTS context_outcomes (
            verifier_id TEXT NOT NULL REFERENCES context_clients(id), event_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES context_attempts(id), request_hash TEXT NOT NULL,
            result TEXT NOT NULL, PRIMARY KEY(verifier_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS context_outcome_attempt ON context_outcomes (attempt_id);",
    )?;
    let has_task_title: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM pragma_table_info('context_tasks') WHERE name='title')",
        [],
        |row| row.get(0),
    )?;
    if !has_task_title {
        conn.execute_batch("ALTER TABLE context_tasks ADD COLUMN title TEXT")?;
    }
    Ok(())
}

fn opaque_id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4().simple())
}

fn hash(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}

fn check_object(value: &Value) -> Result<()> {
    if value.is_object() {
        Ok(())
    } else {
        Err(Error::invalid("expected a JSON object"))
    }
}

fn optional_text<'a>(input: &'a Value, field: &str) -> Result<Option<&'a str>> {
    match input.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if !s.trim().is_empty() && s.len() <= 512 && !s.contains('\0') => {
            Ok(Some(s))
        }
        _ => Err(Error::invalid(format!(
            "{field} must be a nonempty string of at most 512 bytes"
        ))),
    }
}

fn text<'a>(input: &'a Value, field: &str) -> Result<&'a str> {
    optional_text(input, field)?.ok_or_else(|| Error::invalid(format!("{field} is required")))
}

fn validate_key(key: &str) -> Result<()> {
    if key.is_empty() || key.len() > 256 || key.contains('\0') {
        return Err(Error::invalid("invalid idempotency key"));
    }
    Ok(())
}

fn request_hash(request: &Value) -> Result<String> {
    Ok(hash(serde_json::to_vec(request)?))
}

fn policy_version(conn: &Connection) -> Result<i64> {
    Ok(conn.query_row(
        "SELECT version FROM context_policy WHERE singleton=1",
        [],
        |row| row.get(0),
    )?)
}

fn advance_policy(conn: &Connection) -> Result<i64> {
    conn.execute(
        "UPDATE context_policy SET version=version+1 WHERE singleton=1",
        [],
    )?;
    policy_version(conn)
}

fn replay_intent(
    tx: &Transaction<'_>,
    actor: &str,
    operation: &str,
    key: &str,
    digest: &str,
) -> Result<Option<Value>> {
    let previous = tx
        .query_row(
            "SELECT request_hash, result FROM context_intents WHERE actor=?1 AND operation=?2 AND key=?3",
            params![actor, operation, key],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?;
    match previous {
        Some((previous, _)) if previous != digest => Err(Error::new(
            409,
            "key_conflict",
            "same idempotency key used with different input",
        )),
        Some((_, result)) => Ok(Some(serde_json::from_str(&result)?)),
        None => Ok(None),
    }
}

fn store_intent(
    tx: &Transaction<'_>,
    actor: &str,
    operation: &str,
    key: &str,
    digest: &str,
    result: &Value,
) -> Result<()> {
    tx.execute(
        "INSERT INTO context_intents(actor, operation, key, request_hash, result) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![actor, operation, key, digest, serde_json::to_string(result)?],
    )?;
    Ok(())
}

pub fn register(conn: &mut Connection, key: &str, request: Value) -> Result<Value> {
    validate_key(key)?;
    check_object(&request)?;
    let host = optional_text(&request, "host")?
        .or(optional_text(&request, "host_type")?)
        .ok_or_else(|| Error::invalid("host is required"))?;
    let destination = text(&request, "destination")?;
    let capabilities = match request.get("capabilities") {
        Some(value) => serde_json::from_value::<Capabilities>(value.clone())
            .map_err(|_| Error::invalid("invalid client capabilities"))?,
        None => Capabilities {
            load_context: true,
            ..Default::default()
        },
    };
    if capabilities.observe_used && !capabilities.observe_loaded {
        return Err(Error::invalid("observe_used requires observe_loaded"));
    }
    if capabilities.observe_loaded && !capabilities.load_context {
        return Err(Error::invalid("observe_loaded requires load_context"));
    }
    let digest = request_hash(&json!({
        "host": host, "destination": destination, "capabilities": capabilities
    }))?;
    let tx = conn.transaction()?;
    if let Some(previous) = replay_intent(&tx, "owner", "client.register", key, &digest)? {
        return Ok(previous);
    }
    let client_id = opaque_id("client");
    let route_ref = opaque_id("route");
    let token = format!("{}.{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    tx.execute(
        "INSERT INTO context_clients(id, host, route, destination, token_hash, capabilities)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            client_id,
            host,
            route_ref,
            destination,
            hash(&token),
            serde_json::to_string(&capabilities)?
        ],
    )?;
    let mut result =
        json!({"client_id": client_id, "route_ref": route_ref, "shared_statements": false});
    store_intent(&tx, "owner", "client.register", key, &digest, &result)?;
    tx.commit()?;
    result["token"] = json!(token);
    Ok(result)
}

pub fn bind(conn: &Connection, token: &str) -> Result<BoundClient> {
    if token.is_empty() || token.len() > 128 {
        return Err(Error::new(
            401,
            "authentication_required",
            "client authentication failed",
        ));
    }
    let (id, route_ref, capabilities) = conn
        .query_row(
            "SELECT id, route, capabilities FROM context_clients WHERE token_hash=?1",
            [hash(token)],
            |row| Ok((row.get(0)?, row.get(1)?, row.get::<_, String>(2)?)),
        )
        .optional()?
        .ok_or_else(|| {
            Error::new(
                401,
                "authentication_required",
                "client authentication failed",
            )
        })?;
    Ok(BoundClient {
        id,
        route_ref,
        capabilities: serde_json::from_str(&capabilities)?,
    })
}

pub fn clients(conn: &Connection) -> Result<Value> {
    let mut query = conn.prepare(
        "SELECT id, host, route, destination, capabilities, shared FROM context_clients ORDER BY id",
    )?;
    let mut rows = query.query([])?;
    let mut clients = Vec::new();
    while let Some(row) = rows.next()? {
        clients.push(json!({
            "client_id": row.get::<_, String>(0)?,
            "host": row.get::<_, String>(1)?,
            "route_ref": row.get::<_, String>(2)?,
            "destination": row.get::<_, String>(3)?,
            "capabilities": serde_json::from_str::<Value>(&row.get::<_, String>(4)?)?,
            "shared_statements": row.get::<_, bool>(5)?,
        }));
    }
    Ok(json!({"clients": clients, "policy_version": policy_version(conn)?}))
}

pub fn shared_enabled(conn: &Connection, client: &BoundClient) -> Result<bool> {
    conn.query_row(
        "SELECT shared FROM context_clients WHERE id=?1 AND route=?2",
        params![client.id, client.route_ref],
        |row| row.get(0),
    )
    .optional()?
    .ok_or_else(|| Error::denied("client binding is no longer available"))
}

pub fn set_shared(conn: &Connection, client_id: &str, allow: bool) -> Result<Value> {
    let previous: bool = conn
        .query_row(
            "SELECT shared FROM context_clients WHERE id=?1",
            [client_id],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("client unavailable"))?;
    let version = if previous == allow {
        policy_version(conn)?
    } else {
        conn.execute(
            "UPDATE context_clients SET shared=?1 WHERE id=?2",
            params![allow, client_id],
        )?;
        advance_policy(conn)?
    };
    Ok(json!({"client_id": client_id, "shared_statements": allow, "policy_version": version}))
}

pub fn task_access(conn: &Connection, task_id: &str, client_id: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM context_task_access WHERE task_id=?1 AND client_id=?2)",
        params![task_id, client_id],
        |row| row.get(0),
    )?)
}

pub fn set_task_access(
    conn: &Connection,
    task_id: &str,
    client_id: &str,
    allow: bool,
) -> Result<Value> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM context_tasks WHERE id=?1)
         AND EXISTS(SELECT 1 FROM context_clients WHERE id=?2)",
        params![task_id, client_id],
        |row| row.get(0),
    )?;
    if !exists {
        return Err(Error::missing("task or client unavailable"));
    }
    let changed = if allow {
        conn.execute(
            "INSERT OR IGNORE INTO context_task_access(task_id, client_id) VALUES (?1, ?2)",
            params![task_id, client_id],
        )?
    } else {
        conn.execute(
            "DELETE FROM context_task_access WHERE task_id=?1 AND client_id=?2",
            params![task_id, client_id],
        )?
    };
    let version = if changed != 0 {
        advance_policy(conn)?
    } else {
        policy_version(conn)?
    };
    Ok(
        json!({"task_id": task_id, "client_id": client_id, "allowed": allow, "policy_version": version}),
    )
}

pub fn create_task(
    conn: &mut Connection,
    client: Option<&BoundClient>,
    key: &str,
    request: Value,
) -> Result<Value> {
    validate_key(key)?;
    check_object(&request)?;
    if client.is_some_and(|client| !client.capabilities.manage_tasks) {
        return Err(Error::denied("client cannot register tasks"));
    }
    let project = optional_text(&request, "project")?;
    let anchor = optional_text(&request, "anchor")?
        .or(optional_text(&request, "artifact_ref")?)
        .ok_or_else(|| Error::invalid("anchor is required"))?;
    let title = optional_text(&request, "title")?;
    let actor = client.map_or("owner", |client| client.id.as_str());
    let mut intent = json!({"project": project, "anchor": anchor});
    if let Some(title) = title {
        intent["title"] = title.into();
    }
    let digest = request_hash(&intent)?;
    let tx = conn.transaction()?;
    if let Some(previous) = replay_intent(&tx, actor, "task.create", key, &digest)? {
        return Ok(previous);
    }
    let task_id = opaque_id("task");
    tx.execute(
        "INSERT INTO context_tasks(id, project, anchor, creator, title) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![task_id, project, anchor, actor, title],
    )?;
    intent["task_id"] = task_id.into();
    let result = intent;
    store_intent(&tx, actor, "task.create", key, &digest, &result)?;
    tx.commit()?;
    Ok(result)
}

pub fn close_task(conn: &Connection, task: &str) -> Result<Value> {
    if !conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM context_tasks WHERE id=?1)",
        [task],
        |row| row.get::<_, bool>(0),
    )? {
        return Err(Error::missing("task unavailable"));
    }
    conn.execute(
        "INSERT OR IGNORE INTO context_task_closures(task_id,closed_at) VALUES (?1,?2)",
        params![task, crate::clock::now()?],
    )?;
    Ok(json!({"task_id":task,"state":"closed"}))
}

pub fn tasks(conn: &Connection) -> Result<Value> {
    let mut query =
        conn.prepare("SELECT id, project, anchor, title FROM context_tasks ORDER BY rowid DESC")?;
    let items = query
        .query_map([], |row| {
            let mut item = json!({
                "task_id": row.get::<_, String>(0)?,
                "project": row.get::<_, Option<String>>(1)?,
                "anchor": row.get::<_, String>(2)?,
            });
            if let Some(title) = row.get::<_, Option<String>>(3)? {
                item["title"] = title.into();
            }
            Ok(item)
        })?
        .collect::<rusqlite::Result<Vec<Value>>>()?;
    Ok(json!({"tasks": items}))
}

pub fn task_candidates(conn: &Connection, client: &BoundClient, name: &str) -> Result<Value> {
    if !client.capabilities.load_context {
        return Err(Error::denied("client cannot select context tasks"));
    }
    if name.trim().is_empty() || name.len() > 512 {
        return Err(Error::invalid(
            "task name must be nonempty and at most 512 bytes",
        ));
    }
    let mut query = conn.prepare(
        "SELECT t.id,t.project,t.anchor,COALESCE(t.title,t.anchor)
         FROM context_task_access a JOIN context_tasks t ON t.id=a.task_id
         LEFT JOIN context_task_closures c ON c.task_id=t.id
         WHERE a.client_id=?1 AND COALESCE(t.title,t.anchor)=?2 AND c.task_id IS NULL
         ORDER BY t.project,t.anchor,t.id LIMIT 50",
    )?;
    let items = query
        .query_map(params![client.id, name], |row| {
            Ok(json!({
                "task_id":row.get::<_,String>(0)?,"project":row.get::<_,Option<String>>(1)?,
                "anchor":row.get::<_,String>(2)?,"title":row.get::<_,String>(3)?,
            }))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(json!({"tasks":items,"requires_selection":!items.is_empty()}))
}

pub fn create_attempt(
    conn: &mut Connection,
    client: Option<&BoundClient>,
    task_id: &str,
    key: &str,
    request: Value,
) -> Result<Value> {
    validate_key(key)?;
    check_object(&request)?;
    let session = text(&request, "session_id")?;
    let epoch = text(&request, "context_epoch")?;
    let route = text(&request, "route_ref")?;
    let client_id = if let Some(client) = client {
        if !client.capabilities.load_context
            || client.route_ref != route
            || optional_text(&request, "client_id")?.is_some_and(|id| id != client.id)
        {
            return Err(Error::denied("attempt does not match client binding"));
        }
        client.id.as_str()
    } else {
        text(&request, "client_id")?
    };
    let actor = client.map_or("owner", |client| client.id.as_str());
    let digest = request_hash(&json!({
        "task_id": task_id, "client_id": client_id, "session_id": session,
        "context_epoch": epoch, "route_ref": route
    }))?;
    let tx = conn.transaction()?;
    let route_registered: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM context_clients WHERE id=?1 AND route=?2)",
        params![client_id, route],
        |row| row.get(0),
    )?;
    if !route_registered || !task_access(&tx, task_id, client_id)? {
        return Err(Error::missing("task or route unavailable"));
    }
    if let Some(previous) = replay_intent(&tx, actor, "attempt.create", key, &digest)? {
        return Ok(previous);
    }
    let attempt_id = opaque_id("attempt");
    tx.execute(
        "INSERT INTO context_attempts(id, task_id, client_id, session, epoch, route)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![attempt_id, task_id, client_id, session, epoch, route],
    )?;
    let result = json!({"attempt_id": attempt_id, "task_id": task_id, "client_id": client_id, "route_ref": route});
    store_intent(&tx, actor, "attempt.create", key, &digest, &result)?;
    tx.commit()?;
    Ok(result)
}

fn bound_project(
    conn: &Connection,
    client: &BoundClient,
    task_id: Option<&str>,
    attempt_id: Option<&str>,
    session: &str,
    epoch: &str,
) -> Result<Option<String>> {
    match (task_id, attempt_id) {
        (None, None) => Ok(None),
        (Some(task), Some(attempt)) => {
            let project = conn
                .query_row(
                    "SELECT t.project FROM context_attempts a
                     JOIN context_tasks t ON t.id=a.task_id
                     JOIN context_task_access g ON g.task_id=t.id AND g.client_id=a.client_id
                     WHERE a.id=?1 AND t.id=?2 AND a.client_id=?3 AND a.route=?4
                     AND a.session=?5 AND a.epoch=?6",
                    params![attempt, task, client.id, client.route_ref, session, epoch],
                    |row| row.get::<_, Option<String>>(0),
                )
                .optional()?
                .ok_or_else(|| Error::missing("context unavailable"))?;
            Ok(project)
        }
        _ => Err(Error::invalid(
            "task_id and attempt_id must be supplied together",
        )),
    }
}

fn visible_statements(
    conn: &Connection,
    client: &BoundClient,
    project: Option<&str>,
    task: Option<&str>,
) -> Result<Vec<Value>> {
    if !shared_enabled(conn, client)? {
        return Ok(Vec::new());
    }
    let current = authority::current_with_task(conn, project, None, None, true, task)?;
    let rows = current["statements"]
        .as_array()
        .ok_or_else(|| Error::internal("invalid authority projection"))?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let revision_id = text(row, "revision_id")?;
        let key = text(row, "key")?;
        let value = row
            .get("value")
            .ok_or_else(|| Error::internal("missing shared value"))?;
        result.push(json!({"revision_id": revision_id, "key": key, "value": value}));
    }
    result.sort_by(|a, b| {
        a["key"]
            .as_str()
            .cmp(&b["key"].as_str())
            .then(a["revision_id"].as_str().cmp(&b["revision_id"].as_str()))
    });
    Ok(result)
}

fn token_budget(budget: &Value, field: &str) -> Result<u64> {
    budget
        .get(field)
        .and_then(Value::as_u64)
        .filter(|amount| *amount <= 262_144)
        .ok_or_else(|| Error::invalid(format!("budget.{field} must be an integer within bounds")))
}

pub(crate) struct EvidencePackage {
    state: &'static str,
    pub(crate) evidence: Vec<Value>,
    observations: Vec<Value>,
    handles: Vec<String>,
    estimated_tokens: usize,
    effective_profile: Option<&'static str>,
    fallback_reason: Option<&'static str>,
    window: Value,
}

impl EvidencePackage {
    fn empty(state: &'static str) -> Self {
        Self {
            state,
            evidence: Vec::new(),
            observations: Vec::new(),
            handles: Vec::new(),
            estimated_tokens: 0,
            effective_profile: None,
            fallback_reason: None,
            window: Value::Null,
        }
    }

    fn fingerprint(&self) -> Result<String> {
        let mut payload = json!({
            "state": self.state, "evidence": self.evidence, "observations": self.observations,
            "effective_profile": self.effective_profile, "fallback_reason": self.fallback_reason,
        });
        if self.window.is_object() {
            payload["window"] = self.window.clone();
        }
        request_hash(&payload)
    }

    fn manifest(&self) -> Result<BTreeMap<String, String>> {
        self.evidence
            .iter()
            .map(|item| {
                let handle = text(item, "handle")?;
                let observations: Vec<_> = self
                    .observations
                    .iter()
                    .filter(|row| row["handle"] == handle)
                    .collect();
                Ok((
                    handle.to_owned(),
                    request_hash(&json!([item, observations]))?,
                ))
            })
            .collect()
    }
}

fn evidence_manifest(db: &Connection, delivery: &str) -> Result<Option<BTreeMap<String, String>>> {
    let result: Option<String> = db
        .query_row(
            "SELECT entries FROM context_evidence_manifests WHERE delivery_id=?1",
            [delivery],
            |row| row.get(0),
        )
        .optional()?;
    Ok(result
        .map(|encoded| serde_json::from_str(&encoded))
        .transpose()?)
}

fn task_package(
    db: &Connection,
    client: &BoundClient,
    task_id: Option<&str>,
    profile: &str,
    budget: usize,
    query: Option<(&str, Option<&str>)>,
    window: Option<(usize, usize)>,
    frozen: Option<(&str, bool)>,
) -> Result<EvidencePackage> {
    if budget == 0 {
        return Ok(EvidencePackage::empty("budget_zero"));
    }
    let Some(task_id) = task_id else {
        if frozen.is_some() {
            return Err(Error::invalid("a frozen evidence seed set requires a bound task"));
        }
        return Ok(EvidencePackage::empty("evidence_unavailable"));
    };
    let result = if let Some((seed_set,neighbor))=frozen {
        Ok(crate::evidence_snapshot::replay(db,seed_set,&client.id,task_id,neighbor)?)
    } else { match query {
        Some((text, skip)) => match window {
            Some((seeds, slots)) => {
                retrieval::evidence_window(db, task_id, Some(&client.id), text, skip, seeds, slots)
            }
            None => retrieval::matching_for_client(db, task_id, &client.id, text, skip),
        },
        None => retrieval::for_client(db, task_id, &client.id),
    }};
    let authorized = match result {
        Ok(package) => package,
        Err(error) if error.status == 404 => {
            return Ok(EvidencePackage::empty("evidence_unavailable"));
        }
        Err(error) => return Err(error),
    };
    let mut fitted = fit_evidence(
        &authorized,
        task_id,
        profile,
        EvidenceBudget::TransportBytes(budget),
    )?;
    fitted.window = authorized["window"].clone();
    if fitted.window.get("evidence_bytes_utf8").is_some() {
        fitted.window["evidence_bytes_utf8"] = json!(fitted.evidence.iter()
            .filter_map(|item| item["text"].as_str()).map(str::len).sum::<usize>());
    }
    if fitted.window.is_object() && fitted.state == "budget_truncated" {
        fitted.window["evidence_gap"] = json!(true);
        if let Some(expanded) = fitted.window["expanded_refs"].as_array_mut() {
            expanded.retain(|reference| {
                reference.as_str().is_some_and(|reference| {
                    fitted.handles.iter().any(|handle| handle == reference)
                })
            });
        }
    }
    Ok(fitted)
}

pub(crate) enum EvidenceBudget {
    TransportBytes(usize),
    ExcerptTokens(usize),
}

impl EvidenceBudget {
    fn cost(&self, item: &Value, content: &str, separator: bool) -> Result<usize> {
        match self {
            Self::TransportBytes(_) => Ok(serde_json::to_vec(item)?.len() + usize::from(separator)),
            Self::ExcerptTokens(_) => Ok(content.chars().count() + usize::from(separator)),
        }
    }
}

pub(crate) fn fit_evidence(
    authorized: &Value,
    task_id: &str,
    profile: &str,
    budget: EvidenceBudget,
) -> Result<EvidencePackage> {
    let items = authorized["evidence"]
        .as_array()
        .ok_or_else(|| Error::internal("invalid task evidence projection"))?;
    if items.is_empty() {
        return Ok(EvidencePackage::empty("evidence_unavailable"));
    }

    let mut observations = BTreeMap::<&str, Vec<&Value>>::new();
    for observation in authorized["observations"].as_array().into_iter().flatten() {
        if let Some(event_id) = observation["event_id"].as_str() {
            observations.entry(event_id).or_default().push(observation);
        }
    }

    // Transport retains its encoded cap. Read-only comparison budgets the rendered
    // source and observation content, whose cost is independent of opaque IDs.
    let mut package = EvidencePackage::empty("provided");
    let (limit, mut used) = match budget {
        EvidenceBudget::TransportBytes(limit) => {
            (limit, r#"{"evidence":[],"observations":[]}"#.len())
        }
        EvidenceBudget::ExcerptTokens(limit) => (limit, 0),
    };
    let mut chosen = Vec::new();
    let latest_check = items
        .iter()
        .rposition(|item| item["actor_kind"] == "tool_result");
    let priority = latest_check.into_iter().chain(
        (0..items.len())
            .rev()
            .filter(|index| Some(*index) != latest_check),
    );
    for index in priority {
        let event = &items[index];
        let event_id = text(event, "event_id")?;
        let handle = text(event, "handle")?;
        let actor = text(event, "actor_kind")?;
        let original = event["text"]
            .as_str()
            .ok_or_else(|| Error::internal("task evidence is missing source text"))?;
        let item = json!({
            "event_id": event_id, "handle": handle, "actor_kind": actor,
            "text": original, "at": event["at"].as_str(), "task_id": task_id,
        });
        let cost = budget.cost(&item, original, !chosen.is_empty())?;
        if used + cost > limit {
            package.state = "budget_truncated";
            continue;
        }
        used += cost;
        chosen.push((index, item));
    }
    chosen.sort_by_key(|(index, _)| *index);
    for (_, item) in chosen {
        package.handles.push(text(&item, "handle")?.to_owned());
        package.evidence.push(item);
    }
    for event in &package.evidence {
        let event_id = event["event_id"].as_str().unwrap_or_default();
        let Some(rows) = observations.get(event_id) else {
            continue;
        };
        for row in rows {
            let Some(field) = row["field"].as_str() else {
                continue;
            };
            let Some(value) = row.get("value") else {
                continue;
            };
            let role = match (event["actor_kind"].as_str(), row["actor_kind"].as_str()) {
                (Some("user_message"), Some("plan")) => "plan",
                (Some("tool_result"), Some("tool_result")) => "tool_result",
                _ => continue,
            };
            if row["handle"] != event["handle"] || value.is_array() || value.is_object() {
                continue;
            }
            let item = json!({
                "event_id": event_id, "field": field, "value": value, "span_id": row["span_id"].as_str(),
                "actor_kind": role, "handle": event["handle"],
            });
            let cost = budget.cost(
                &item,
                &format!("{field}:{value}"),
                !package.observations.is_empty(),
            )?;
            if used + cost > limit {
                package.state = "budget_truncated";
                break;
            }
            used += cost;
            package.observations.push(item);
        }
    }
    if !package.evidence.is_empty() {
        package.estimated_tokens = used;
        package.effective_profile = Some(if profile == "balanced" {
            "balanced"
        } else {
            "data"
        });
        if profile == "high" {
            package.fallback_reason = Some("high_profile_quality_unverified");
        }
    }
    Ok(package)
}

fn enforce_append_only_session(
    db: &Connection,
    client_id: &str,
    session_id: &str,
    statements: &[Value],
    task_id: Option<&str>,
) -> Result<()> {
    let desired: BTreeSet<_> = statements
        .iter()
        .filter_map(|statement| statement["revision_id"].as_str())
        .collect();
    let mut query = db.prepare(
        "SELECT provided FROM context_deliveries WHERE client_id=?1 AND session=?2 AND mode='replace'",
    )?;
    let rows = query.query_map(params![client_id, session_id], |row| {
        row.get::<_, String>(0)
    })?;
    for row in rows {
        let revisions: Vec<String> = serde_json::from_str(&row?)?;
        if revisions
            .iter()
            .any(|revision| !desired.contains(revision.as_str()))
        {
            return Err(Error::new(
                409,
                "fresh_session_required",
                "previously supplied statements changed or were withdrawn; start a fresh session",
            ));
        }
    }
    let mut query=db.prepare("SELECT e.handles FROM context_delivery_evidence e JOIN context_deliveries d ON e.delivery_id=d.id
        WHERE d.client_id=?1 AND d.session=?2")?;
    for row in query.query_map(params![client_id, session_id], |row| {
        row.get::<_, String>(0)
    })? {
        let supplied: Vec<String> = serde_json::from_str(&row?)?;
        for handle in supplied {
            if task_id.is_none() || !retrieval::client_can_expand(db, &handle, client_id, task_id)?
            {
                return Err(Error::new(
                    409,
                    "fresh_session_required",
                    "previously supplied task evidence is no longer authorized; start a fresh session",
                ));
            }
        }
    }
    Ok(())
}

pub fn load(
    conn: &mut Connection,
    client: &BoundClient,
    key: Option<&str>,
    request: Value,
) -> Result<Value> {
    check_object(&request)?;
    if !client.capabilities.load_context {
        return Err(Error::denied(
            "client does not have context loading capability",
        ));
    }
    if text(&request, "route_ref")? != client.route_ref {
        return Err(Error::denied("route does not match client binding"));
    }
    if let Some(key) = key {
        validate_key(key)?;
    }
    let session = text(&request, "session_id")?;
    let epoch = text(&request, "context_epoch")?;
    let task = optional_text(&request, "task_id")?;
    let attempt = optional_text(&request, "attempt_id")?;
    let known = optional_text(&request, "known_snapshot_id")?;
    let append_only = match request.get("append_only") {
        None => false,
        Some(Value::Bool(append_only)) => *append_only,
        _ => return Err(Error::invalid("append_only must be a boolean")),
    };
    let profile = optional_text(&request, "requested_profile")?.unwrap_or("balanced");
    if !matches!(profile, "data" | "balanced" | "high") {
        return Err(Error::invalid("invalid requested_profile"));
    }
    let statement_tokens = token_budget(&request["budget"], "statement_tokens")?;
    let evidence_tokens = token_budget(&request["budget"], "evidence_tokens")?;
    let evidence_query = optional_text(&request, "evidence_query")?;
    let evidence_window = request
        .get("evidence_window")
        .map(|window| {
            let parse = |name: &str| {
                window[name]
                    .as_u64()
                    .filter(|limit| (1..=50).contains(limit))
                    .map(|n| n as usize)
                    .ok_or_else(|| Error::invalid(format!("evidence_window.{name} must be 1..50")))
            };
            Ok::<_, Error>((parse("candidate_limit")?, parse("evidence_slots")?))
        })
        .transpose()?;
    let frozen = request.get("frozen_evidence").map(|frozen| {
        let seed_set = text(frozen,"seed_set")?;
        let neighbor = frozen["neighbor"].as_bool().ok_or_else(||Error::invalid("frozen_evidence.neighbor must be boolean"))?;
        Ok::<_,Error>((seed_set,neighbor))
    }).transpose()?;
    if frozen.is_some() && (evidence_query.is_some() || evidence_window.is_some()) {
        return Err(Error::invalid("a frozen evidence seed set cannot be reranked"));
    }
    let skip_event = optional_text(&request, "evidence_skip_event_id")?;
    let retained_delivery = optional_text(&request, "retained_delivery_id")?;
    let retained_digest = optional_text(&request, "retained_manifest_digest")?;
    let digest = key.map(|_| request_hash(&request)).transpose()?;
    let tx = conn.transaction()?;
    let project = bound_project(&tx, client, task, attempt, session, epoch)?;
    let policy = policy_version(&tx)?;
    let strategy = crate::optimization::serving(&tx, &client.id, project.as_deref())?;
    let statements = visible_statements(&tx, client, project.as_deref(), task)?;
    let all_revisions: Vec<&Value> = statements
        .iter()
        .map(|statement| &statement["revision_id"])
        .collect();
    let snapshot = format!(
        "snapshot_{}",
        request_hash(&json!({
            "client": client.id, "route": client.route_ref, "project": project, "task": task,
            "policy": policy, "strategy": strategy.version, "revisions": all_revisions,
        }))?
    );
    let required_budget = if statements.is_empty() {
        0
    } else {
        serde_json::to_vec(&statements)?.len()
    };
    let evidence = task_package(
        &tx,
        client,
        task,
        profile,
        (evidence_tokens as usize).min(strategy.evidence_token_cap.unwrap_or(usize::MAX)),
        evidence_query.map(|query| (query, skip_event)),
        evidence_window,
        frozen,
    )?;
    if append_only {
        enforce_append_only_session(&tx, &client.id, session, &statements, task)?;
    }
    let fingerprint = evidence.fingerprint()?;
    let manifest = evidence.manifest()?;
    let manifest_digest = format!("sha256:{}", hash(serde_json::to_vec(&manifest)?));
    // UTF-8 byte length is a conservative estimate when the host tokenizer is unavailable.
    let respond = |delivery_id: &str,
                   package_id: &str,
                   mode: &str,
                   base: Option<(&str, &BTreeMap<String, String>)>| {
        let included = |item: &&Value| {
            base.is_none_or(|(_, entries)| {
                item["handle"]
                    .as_str()
                    .is_some_and(|handle| !entries.contains_key(handle))
            })
        };
        let mut response = json!({
            "delivery_id": delivery_id,
            "delivery_state": "provided",
            "policy_version": policy,
            "strategy_version": strategy.version,
            "snapshot_id": snapshot,
            "snapshot_mode": mode,
            "statements": if mode == "replace" { statements.as_slice() } else { &[] },
            "estimated_statement_tokens": if mode == "replace" { required_budget } else { 0 },
            "package_id": package_id,
            "package_mode": if base.is_some() {"delta"} else {"full"},
            "base_delivery_id": base.map(|(id, _)| id),
            "evidence_manifest_digest": manifest_digest,
            "evidence_manifest": manifest,
            "evidence": evidence.evidence.iter().filter(included).collect::<Vec<_>>(),
            "observations": evidence.observations.iter().filter(included).collect::<Vec<_>>(),
            "package_state": evidence.state,
            "estimated_evidence_tokens": evidence.estimated_tokens,
            "requested_profile": profile,
            "effective_profile": evidence.effective_profile,
            "fallback_reason": evidence.fallback_reason,
            "template_version": evidence.effective_profile.map(|_| "literal-v1"),
        });
        if evidence.window.is_object() {
            response["window"] = evidence.window.clone();
        }
        if mode == "deferred" {
            response["reason"] = json!("statement_budget");
            response["required_statement_tokens"] = json!(required_budget);
        }
        response
    };
    if let Some(key) = key {
        let previous = tx
            .query_row(
                "SELECT d.id, d.package, d.snapshot, d.mode, d.request_hash, e.fingerprint,m.base_delivery
             FROM context_deliveries d LEFT JOIN context_delivery_evidence e ON e.delivery_id=d.id
             LEFT JOIN context_evidence_manifests m ON m.delivery_id=d.id
             WHERE d.client_id=?1 AND d.request_key=?2",
                params![client.id, key],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, Option<String>>(5)?,
                        row.get::<_, Option<String>>(6)?,
                    ))
                },
            )
            .optional()?;
        if let Some((id, package, old_snapshot, mode, old_digest, old_fingerprint, base)) = previous
        {
            if digest.as_deref() != Some(&old_digest) {
                return Err(Error::new(
                    409,
                    "key_conflict",
                    "same idempotency key used with different input",
                ));
            }
            if snapshot != old_snapshot
                || old_fingerprint
                    .as_ref()
                    .map_or(!evidence.handles.is_empty(), |previous| {
                        previous != &fingerprint
                    })
            {
                return Err(Error::conflict("revision_changed"));
            }
            let prior = base
                .as_deref()
                .map(|id| evidence_manifest(&tx, id))
                .transpose()?
                .flatten();
            return Ok(respond(
                &id,
                &package,
                &mode,
                base.as_deref().zip(prior.as_ref()),
            ));
        }
    }
    let known_loaded: bool = if known == Some(snapshot.as_str()) {
        tx.query_row(
            "SELECT EXISTS(
                SELECT 1 FROM context_deliveries d JOIN context_receipts r ON r.delivery_id=d.id
                WHERE d.client_id=?1 AND d.session=?2 AND d.epoch=?3 AND d.snapshot=?4
                  AND r.kind='loaded' AND r.complete=1
            )",
            params![client.id, session, epoch, snapshot],
            |row| row.get(0),
        )?
    } else {
        false
    };
    let mode = if known_loaded {
        "unchanged"
    } else if required_budget as u64 > statement_tokens {
        "deferred"
    } else {
        "replace"
    };
    let id = opaque_id("delivery");
    let package = opaque_id("package");
    let base = if let (Some(base), Some(expected), Some(task)) =
        (retained_delivery, retained_digest, task)
    {
        let confirmed: bool = tx.query_row(
            "SELECT COALESCE((
              SELECT json_extract(r.result,'$.evidence_complete')
              FROM context_deliveries d JOIN context_evidence_manifests m ON m.delivery_id=d.id
              JOIN context_receipts r ON r.delivery_id=d.id AND r.client_id=d.client_id AND r.kind='loaded'
              WHERE d.id=?1 AND d.client_id=?2 AND d.session=?3 AND d.epoch=?4 AND d.route=?5
                AND d.task_id=?6 AND m.digest=?7
              ORDER BY r.rowid DESC LIMIT 1), 0) = 1",
            params![base,client.id,session,epoch,client.route_ref,task,expected], |row| row.get(0),
        )?;
        if confirmed {
            evidence_manifest(&tx, base)?
                .filter(|entries| {
                    !entries.is_empty()
                        && entries
                            .iter()
                            .all(|(key, value)| manifest.get(key) == Some(value))
                })
                .map(|entries| (base, entries))
        } else {
            None
        }
    } else {
        None
    };
    let provided = if mode == "replace" {
        all_revisions
    } else {
        Vec::new()
    };
    tx.execute(
        "INSERT INTO context_deliveries(
            id, client_id, session, epoch, route, task_id, attempt_id, policy, snapshot, package,
            mode, profile, package_state, required_budget, provided, created_at, request_key, request_hash
         ) VALUES (
            ?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,
            strftime('%Y-%m-%dT%H:%M:%fZ','now'),?16,?17
         )",
        params![id, client.id, session, epoch, client.route_ref, task, attempt, policy, snapshot,
            package, mode, profile, evidence.state, required_budget as i64, serde_json::to_string(&provided)?,
            key, digest],
    )?;
    tx.execute(
        "INSERT INTO context_delivery_evidence(delivery_id,fingerprint,handles,effective_profile,estimated_tokens)
         VALUES (?1,?2,?3,?4,?5)",
        params![id, fingerprint, serde_json::to_string(&evidence.handles)?, evidence.effective_profile,
            evidence.estimated_tokens as i64],
    )?;
    tx.execute(
        "INSERT INTO context_evidence_manifests(delivery_id,entries,digest,base_delivery) VALUES(?1,?2,?3,?4)",
        params![id, serde_json::to_string(&manifest)?, manifest_digest, base.as_ref().map(|(id,_)| id)],
    )?;
    tx.commit()?;
    Ok(respond(
        &id,
        &package,
        mode,
        base.as_ref().map(|(id, entries)| (*id, entries)),
    ))
}

fn revision_ids(request: &Value) -> Result<BTreeSet<String>> {
    let values = request["revision_ids"]
        .as_array()
        .ok_or_else(|| Error::invalid("revision_ids must be a list"))?;
    let mut set = BTreeSet::new();
    for value in values {
        let id = value
            .as_str()
            .filter(|id| !id.is_empty() && id.len() <= 128)
            .ok_or_else(|| Error::invalid("invalid revision_id"))?;
        if !set.insert(id.to_owned()) {
            return Err(Error::invalid("duplicate revision_id"));
        }
    }
    Ok(set)
}

#[derive(Debug)]
struct Delivery {
    session: String,
    epoch: String,
    route: String,
    policy: i64,
    snapshot: String,
    package: String,
    mode: String,
    provided: BTreeSet<String>,
    evidence_handles: Vec<String>,
    task: Option<String>,
    attempt: Option<String>,
}

fn delivery_for_receipt(conn: &Connection, id: &str, client_id: &str) -> Result<Delivery> {
    let (session, epoch, route, policy, snapshot, package, mode, provided, task, attempt, handles) = conn
        .query_row(
            "SELECT d.session, d.epoch, d.route, d.policy, d.snapshot, d.package, d.mode, d.provided,
                    d.task_id, d.attempt_id, COALESCE(e.handles, '[]')
             FROM context_deliveries d LEFT JOIN context_delivery_evidence e ON e.delivery_id=d.id
             WHERE d.id=?1 AND d.client_id=?2",
            params![id, client_id],
            |row| Ok((
                row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?,
                row.get(5)?, row.get(6)?, row.get::<_, String>(7)?, row.get(8)?, row.get(9)?,
                row.get::<_, String>(10)?
            )),
        )
        .optional()?
        .ok_or_else(|| Error::missing("delivery unavailable"))?;
    Ok(Delivery {
        session,
        epoch,
        route,
        policy,
        snapshot,
        package,
        mode,
        provided: serde_json::from_str(&provided)?,
        evidence_handles: serde_json::from_str(&handles)?,
        task,
        attempt,
    })
}

pub fn handoff_summary(conn: &Connection, client: &BoundClient, id: &str) -> Result<Value> {
    let delivery = delivery_for_receipt(conn, id, &client.id)?;
    let task = delivery
        .task
        .as_deref()
        .filter(|_| delivery.route == client.route_ref)
        .ok_or_else(|| Error::missing("task delivery unavailable"))?;
    if !task_access(conn, task, &client.id)? {
        return Err(Error::missing("task delivery unavailable"));
    }
    let mut plans = Vec::new();
    let mut result_evidence_refs = Vec::new();
    let mut complete = true;
    for handle in delivery.evidence_handles {
        if !retrieval::client_can_expand(conn, &handle, &client.id, Some(task))? {
            complete = false;
            continue;
        }
        let evidence = retrieval::expand(conn, &handle)?;
        if evidence["actor_kind"] == "tool_result" {
            result_evidence_refs.push(handle);
        } else if conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM authority_observations WHERE event_id=?1 AND role='plan')",
            [text(&evidence, "event_id")?],
            |row| row.get::<_, bool>(0),
        )? {
            plans.push(text(&evidence, "text")?.to_owned());
        }
    }
    let result: Option<String> = if complete {
        conn.query_row(
            "SELECT json_extract(result,'$.status') FROM context_outcomes
             WHERE attempt_id=?1 AND EXISTS(SELECT 1 FROM json_each(result,'$.delivery_ids') WHERE value=?2)
             ORDER BY rowid DESC LIMIT 1",
            params![delivery.attempt, id], |row| row.get(0),
        ).optional()?
    } else {
        None
    };
    let previous: Option<String> = conn
        .query_row(
            "SELECT COALESCE(e.handles,'[]') FROM context_deliveries d
         LEFT JOIN context_delivery_evidence e ON e.delivery_id=d.id
         WHERE d.client_id=?1 AND d.route=?2 AND d.task_id=?3
           AND d.rowid<(SELECT rowid FROM context_deliveries WHERE id=?4)
           AND (SELECT json_extract(result,'$.evidence_complete') FROM context_receipts
                WHERE kind='loaded' AND delivery_id=d.id ORDER BY rowid DESC LIMIT 1)=1
         ORDER BY d.rowid DESC LIMIT 1",
            params![client.id, client.route_ref, task, id],
            |row| row.get(0),
        )
        .optional()?;
    let mut retained = false;
    if let Some(handles) = previous {
        let handles: Vec<String> = serde_json::from_str(&handles)?;
        retained = !handles.is_empty();
        for handle in handles {
            if !retrieval::client_can_expand(conn, &handle, &client.id, Some(task))? {
                retained = false;
                break;
            }
        }
    }
    Ok(
        json!({"delivery_id":id,"plan_text":plans.join("\n"),"result_evidence_refs":result_evidence_refs,
        "execution_result":result.as_deref().unwrap_or("unknown"),
        "previous_handoff":if retained {"observed"} else {"unknown"}}),
    )
}

pub fn receipt(
    conn: &mut Connection,
    client: &BoundClient,
    id: &str,
    request: Value,
) -> Result<Value> {
    check_object(&request)?;
    let event_id = text(&request, "event_id")?;
    let kind = text(&request, "kind")?;
    let epoch = text(&request, "context_epoch")?;
    let snapshot = text(&request, "snapshot_id")?;
    let package = text(&request, "package_id")?;
    let route = text(&request, "route_ref")?;
    let call = text(&request, "model_call_ref")?;
    let observed = revision_ids(&request)?;
    let reason = optional_text(&request, "missing_reason")?;
    let evidence = optional_text(&request, "evidence_ref")?;
    let evidence_refs: Option<BTreeSet<String>> = request
        .get("loaded_evidence_refs")
        .map(|value| {
            serde_json::from_value(value.clone())
                .map_err(|_| Error::invalid("loaded_evidence_refs must be a list of handles"))
        })
        .transpose()?;
    let used_evidence: Option<BTreeSet<String>> = request
        .get("used_evidence_refs")
        .map(|value| {
            serde_json::from_value(value.clone())
                .map_err(|_| Error::invalid("used_evidence_refs must be a list of handles"))
        })
        .transpose()?;
    let artifact_check = request.get("artifact_check_ref")
        .map(crate::tool_receipt::ArtifactCheckRef::parse).transpose()?;
    let loaded_digest = optional_text(&request, "loaded_manifest_digest")?;
    if (evidence_refs.is_some() || loaded_digest.is_some()) && kind != "loaded" {
        return Err(Error::invalid(
            "evidence manifest is only accepted on loaded observations",
        ));
    }
    if used_evidence
        .as_ref()
        .is_some_and(|refs| refs.is_empty() || kind != "used")
    {
        return Err(Error::invalid(
            "used_evidence_refs requires a nonempty used observation",
        ));
    }
    if let Some(reference) = &artifact_check
        && (kind != "used" || evidence.is_none()
            || !used_evidence.as_ref().is_some_and(|handles| handles.contains(&reference.handle))) {
        return Err(Error::invalid("artifact_check_ref requires used evidence and an artifact reference"));
    }
    if artifact_check.is_some() && evidence.is_some_and(|path| authority::redact(path) != path) {
        return Err(Error::invalid("artifact reference would expose credential text"));
    }
    match kind {
        "loaded" if client.capabilities.observe_loaded => {}
        "used" if client.capabilities.observe_used => {}
        "loaded" | "used" => return Err(Error::denied("client cannot report this observation")),
        _ => return Err(Error::invalid("invalid observation kind")),
    }
    if kind == "used"
        && ((observed.is_empty() && used_evidence.is_none())
            || (!observed.is_empty() && evidence.is_none()))
    {
        return Err(Error::invalid(
            "used requires revision_ids and evidence_ref",
        ));
    }
    let mut observation_request = json!({
        "delivery_id": id, "event_id": event_id, "kind": kind, "context_epoch": epoch,
        "snapshot_id": snapshot, "package_id": package, "route_ref": route,
        "revision_ids": observed, "model_call_ref": call, "missing_reason": reason, "evidence_ref": evidence,
    });
    if evidence_refs.is_some() || loaded_digest.is_some() {
        observation_request["loaded_evidence_refs"] = json!(evidence_refs);
        observation_request["loaded_manifest_digest"] = json!(loaded_digest);
    }
    if let Some(refs) = &used_evidence {
        observation_request["used_evidence_refs"] = json!(refs);
    }
    if let Some(reference) = &artifact_check {
        observation_request["artifact_check_ref"] = json!(reference);
    }
    let digest = request_hash(&observation_request)?;
    let tx = conn.transaction()?;
    let delivery = delivery_for_receipt(&tx, id, &client.id)?;
    if delivery.route != route
        || delivery.epoch != epoch
        || delivery.snapshot != snapshot
        || delivery.package != package
        || client.route_ref != route
    {
        return Err(Error::conflict("observation does not match the delivery"));
    }
    let project = bound_project(
        &tx,
        client,
        delivery.task.as_deref(),
        delivery.attempt.as_deref(),
        &delivery.session,
        &delivery.epoch,
    )?;
    if delivery.policy != policy_version(&tx)? {
        return Err(Error::conflict("revision_changed"));
    }
    if let Some(task_id) = delivery
        .task
        .as_deref()
        .filter(|_| !delivery.evidence_handles.is_empty())
    {
        for handle in &delivery.evidence_handles {
            if !retrieval::client_can_expand(&tx, handle, &client.id, Some(task_id))? {
                return Err(Error::conflict(
                    "some provided evidence is no longer available",
                ));
            }
        }
    }
    if let Some(reference) = &artifact_check {
        if !delivery.evidence_handles.contains(&reference.handle) {
            return Err(Error::conflict("artifact check is outside this delivery"));
        }
        let item = retrieval::expand(&tx, &reference.handle)?;
        let fields = item["text"].as_str()
            .and_then(|content| serde_json::from_str::<Value>(content).ok());
        if item["actor_kind"] != "tool_result" || !fields.as_ref().is_some_and(|checks| {
            crate::tool_receipt::valid_marker_check(&checks[&reference.field])
        }) {
            return Err(Error::conflict("artifact check is unavailable in this evidence version"));
        }
    }
    let available = visible_statements(&tx, client, project.as_deref(), delivery.task.as_deref())?;
    let active: BTreeSet<_> = available
        .iter()
        .filter_map(|v| v["revision_id"].as_str())
        .collect();
    if !observed.iter().all(|id| active.contains(id.as_str())) {
        return Err(Error::missing("revision unavailable"));
    }
    if (delivery.mode != "replace"
        && (!observed.is_empty() || evidence_refs.is_none() && used_evidence.is_none()))
        || !observed.is_subset(&delivery.provided)
    {
        return Err(Error::conflict(
            "observation includes revisions outside the provided snapshot",
        ));
    }
    let previous = tx
        .query_row(
            "SELECT request_hash, result FROM context_receipts WHERE client_id=?1 AND event_id=?2",
            params![client.id, event_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?;
    if let Some((previous, result)) = previous {
        if previous != digest {
            return Err(Error::new(
                409,
                "key_conflict",
                "same idempotency key used with different input",
            ));
        }
        return Ok(serde_json::from_str(&result)?);
    }
    let complete = kind == "loaded" && delivery.mode == "replace" && observed == delivery.provided;
    if kind == "loaded" && delivery.mode == "replace" && !complete && reason.is_none() {
        return Err(Error::invalid("partial loading requires missing_reason"));
    }
    let evidence_complete = if let Some(refs) = &evidence_refs {
        let delivered: BTreeSet<_> = delivery.evidence_handles.iter().cloned().collect();
        if !refs.is_subset(&delivered) {
            return Err(Error::conflict(
                "observation includes evidence outside the provided manifest",
            ));
        }
        let expected: Option<String> = tx
            .query_row(
                "SELECT digest FROM context_evidence_manifests WHERE delivery_id=?1",
                [id],
                |row| row.get(0),
            )
            .optional()?;
        Some(
            !delivered.is_empty()
                && *refs == delivered
                && expected.as_deref().is_some()
                && loaded_digest == expected.as_deref(),
        )
    } else {
        None
    };
    if kind == "used" {
        let mut query = tx.prepare(
            "SELECT result FROM context_receipts
             WHERE client_id=?1 AND delivery_id=?2 AND kind='loaded' AND model_call=?3",
        )?;
        let rows = query.query_map(params![client.id, id, call], |row| row.get::<_, String>(0))?;
        let mut verified = false;
        for row in rows {
            let value: Value = serde_json::from_str(&row?)?;
            let evidence_observed = used_evidence.as_ref().is_none_or(|used| {
                value["loaded_evidence_refs"]
                    .as_array()
                    .is_some_and(|loaded| {
                        used.iter()
                            .all(|handle| loaded.iter().any(|entry| entry == handle))
                    })
            });
            let matching_check = artifact_check.is_none() || value["evidence_complete"] == true;
            if observed.is_subset(&revision_ids(&value)?) && evidence_observed && matching_check {
                verified = true;
                break;
            }
        }
        if !verified {
            return Err(Error::conflict("no matching loaded observation"));
        }
    }
    let mut result = json!({
        "delivery_id": id, "event_id": event_id, "kind": kind, "revision_ids": observed,
        "complete": complete, "missing_reason": reason, "model_call_ref": call, "evidence_ref": evidence,
    });
    if let Some(evidence_complete) = evidence_complete {
        result["evidence_complete"] = json!(evidence_complete);
        result["loaded_evidence_refs"] = json!(evidence_refs);
    }
    if let Some(refs) = used_evidence {
        result["used_evidence_refs"] = json!(refs);
    }
    if let Some(reference) = artifact_check {
        result["artifact_check_ref"] = json!(reference);
    }
    tx.execute(
        "INSERT INTO context_receipts(client_id, event_id, delivery_id, request_hash, kind, result, complete, model_call)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
        params![client.id, event_id, id, digest, kind, serde_json::to_string(&result)?, complete, call],
    )?;
    tx.commit()?;
    Ok(result)
}

pub fn outcome(
    conn: &mut Connection,
    verifier: &BoundClient,
    attempt_id: &str,
    request: Value,
) -> Result<Value> {
    check_object(&request)?;
    if !verifier.capabilities.verify_outcomes {
        return Err(Error::denied("client cannot verify outcomes"));
    }
    let event_id = text(&request, "event_id")?;
    let status = text(&request, "status")?;
    if !matches!(status, "passed" | "failed" | "unknown") {
        return Err(Error::invalid("invalid outcome status"));
    }
    let validator = text(&request, "validator_ref")?;
    let artifact = text(&request, "artifact_ref")?;
    let evidence = optional_text(&request, "evidence_ref")?;
    let cancelled = match request.get("cancelled") {
        None => false,
        Some(Value::Bool(cancelled)) => *cancelled,
        _ => return Err(Error::invalid("cancelled must be a boolean")),
    };
    if cancelled && status != "unknown" || status != "unknown" && evidence.is_none() {
        return Err(Error::invalid(
            "invalid cancelled outcome, or verdict lacks evidence_ref",
        ));
    }
    let delivery_ids: BTreeSet<&str> = match request.get("delivery_ids") {
        None => BTreeSet::new(),
        Some(Value::Array(ids)) if ids.len() <= 100 => ids
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|id| !id.is_empty() && id.len() <= 128)
                    .ok_or_else(|| Error::invalid("invalid delivery_id"))
            })
            .collect::<Result<_>>()?,
        _ => return Err(Error::invalid("delivery_ids must be a list")),
    };
    let result = json!({
        "event_id": event_id, "attempt_id": attempt_id, "status": status,
        "validator_ref": validator, "artifact_ref": artifact, "evidence_ref": evidence,
        "delivery_ids": delivery_ids, "attribution": "unknown", "cancelled": cancelled,
    });
    let digest = request_hash(&result)?;
    let tx = conn.transaction()?;
    let (task_id, actor_id): (String, String) = tx
        .query_row(
            "SELECT task_id, client_id FROM context_attempts WHERE id=?1",
            [attempt_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("attempt unavailable"))?;
    if actor_id == verifier.id {
        return Err(Error::denied(
            "outcome verifier must be independent of the attempt",
        ));
    }
    if !task_access(&tx, &task_id, &verifier.id)? {
        return Err(Error::missing("attempt unavailable"));
    }
    for delivery_id in delivery_ids {
        let belongs: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM context_deliveries WHERE id=?1 AND attempt_id=?2)",
            params![delivery_id, attempt_id],
            |row| row.get(0),
        )?;
        if !belongs {
            return Err(Error::conflict("delivery does not belong to the attempt"));
        }
    }
    let previous = tx.query_row(
        "SELECT request_hash, result FROM context_outcomes WHERE verifier_id=?1 AND event_id=?2",
        params![verifier.id, event_id],
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    ).optional()?;
    if let Some((previous, result)) = previous {
        if previous != digest {
            return Err(Error::new(
                409,
                "key_conflict",
                "same idempotency key used with different input",
            ));
        }
        return Ok(serde_json::from_str(&result)?);
    }
    tx.execute(
        "INSERT INTO context_outcomes(verifier_id,event_id,attempt_id,request_hash,result)
         VALUES (?1,?2,?3,?4,?5)",
        params![
            verifier.id,
            event_id,
            attempt_id,
            digest,
            serde_json::to_string(&result)?
        ],
    )?;
    tx.commit()?;
    Ok(result)
}

pub fn deliveries(conn: &Connection) -> Result<Value> {
    let mut query = conn.prepare(
        "SELECT d.id, d.client_id, d.session, d.epoch, d.route, d.task_id, d.attempt_id, d.policy,
            d.snapshot, d.package, d.mode, d.profile, d.package_state, d.provided, d.created_at,
            (SELECT r.result FROM context_receipts r WHERE r.delivery_id=d.id AND r.kind='loaded'
             ORDER BY r.rowid DESC LIMIT 1),
            (SELECT r.result FROM context_receipts r WHERE r.delivery_id=d.id AND r.kind='used'
             ORDER BY r.rowid DESC LIMIT 1),
            (SELECT o.result FROM context_outcomes o WHERE o.attempt_id=d.attempt_id
             ORDER BY o.rowid DESC LIMIT 1),
            COALESCE(e.handles,'[]'), e.effective_profile, COALESCE(e.estimated_tokens,0)
         FROM context_deliveries d LEFT JOIN context_delivery_evidence e ON e.delivery_id=d.id
         ORDER BY d.rowid DESC LIMIT 100",
    )?;
    let mut rows = query.query([])?;
    let mut items = Vec::new();
    while let Some(row) = rows.next()? {
        let provided: Value = serde_json::from_str(&row.get::<_, String>(13)?)?;
        let loaded = row
            .get::<_, Option<String>>(15)?
            .map(|s| serde_json::from_str::<Value>(&s))
            .transpose()?;
        let used = row
            .get::<_, Option<String>>(16)?
            .map(|s| serde_json::from_str::<Value>(&s))
            .transpose()?;
        let outcome = row
            .get::<_, Option<String>>(17)?
            .map(|s| serde_json::from_str::<Value>(&s))
            .transpose()?;
        items.push(json!({
            "delivery_id": row.get::<_, String>(0)?,
            "client_id": row.get::<_, String>(1)?,
            "session_id": row.get::<_, String>(2)?,
            "context_epoch": row.get::<_, String>(3)?,
            "route_ref": row.get::<_, String>(4)?,
            "task_id": row.get::<_, Option<String>>(5)?,
            "attempt_id": row.get::<_, Option<String>>(6)?,
            "policy_version": row.get::<_, i64>(7)?,
            "snapshot_id": row.get::<_, String>(8)?,
            "package_id": row.get::<_, String>(9)?,
            "snapshot_mode": row.get::<_, String>(10)?,
            "requested_profile": row.get::<_, String>(11)?,
            "package_state": row.get::<_, String>(12)?,
            "provided_evidence_handles": serde_json::from_str::<Vec<String>>(&row.get::<_, String>(18)?)?,
            "effective_profile": row.get::<_, Option<String>>(19)?,
            "estimated_evidence_tokens": row.get::<_, i64>(20)?,
            "evidence_loaded_state": "unknown",
            "delivery_state": "provided",
            "provided_revision_ids": provided,
            "provided_at": row.get::<_, String>(14)?,
            "loaded": loaded,
            "used": used,
            "outcome": outcome,
            "loaded_state": if loaded.is_some() { "observed" } else { "unknown" },
            "used_state": if used.is_some() { "observed" } else { "unknown" },
            "outcome_state": outcome.as_ref().and_then(|o| o["status"].as_str()).unwrap_or("unknown"),
        }));
    }
    Ok(json!({"deliveries": items}))
}

/// Fixed ledger page size; keeping it constant makes page numbers stable
/// across refreshes while new deliveries arrive.
const LEDGER_PAGE_SIZE: usize = 20;

/// Completeness of the newest loaded receipt for a delivery, NULL when the
/// host never reported loading. Receipts are append-only, so the newest rowid
/// is the current observation; `partial` always corresponds to a real receipt
/// recorded with complete=0.
const LEDGER_LATEST_LOADED: &str = "(SELECT r.complete FROM context_receipts r \
    WHERE r.delivery_id=d.id AND r.kind='loaded' ORDER BY r.rowid DESC LIMIT 1)";
const LEDGER_HAS_USED: &str =
    "EXISTS(SELECT 1 FROM context_receipts r WHERE r.delivery_id=d.id AND r.kind='used')";

/// Owner-facing ledger of the durable deliveries recorded in the last 7 days.
/// `loaded` filters pending|partial|observed, `used` filters pending|observed;
/// an empty host filter means all hosts. Summary counts and `total` cover the
/// whole filtered window, never just the returned page. The legacy
/// `deliveries` projection above is intentionally left unchanged.
pub fn delivery_ledger(
    db: &Connection,
    host: Option<&str>,
    loaded: Option<&str>,
    used: Option<&str>,
    page: usize,
) -> Result<Value> {
    if loaded.is_some_and(|state| !matches!(state, "pending" | "partial" | "observed")) {
        return Err(Error::invalid(
            "loaded must be pending, partial, or observed",
        ));
    }
    if used.is_some_and(|state| !matches!(state, "pending" | "observed")) {
        return Err(Error::invalid("used must be pending or observed"));
    }
    if page == 0 || page > 10_000 {
        return Err(Error::invalid("page must be between 1 and 10000"));
    }
    let host = host.filter(|value| !value.is_empty());
    let mut filters =
        String::from(" WHERE d.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')");
    filters.push_str(" AND (?1 IS NULL OR c.host=?1)");
    match loaded {
        Some("pending") => filters.push_str(&format!(" AND {LEDGER_LATEST_LOADED} IS NULL")),
        Some("partial") => filters.push_str(&format!(" AND {LEDGER_LATEST_LOADED}=0")),
        Some("observed") => filters.push_str(&format!(" AND {LEDGER_LATEST_LOADED}=1")),
        _ => {}
    }
    match used {
        Some("pending") => filters.push_str(&format!(" AND NOT {LEDGER_HAS_USED}")),
        Some("observed") => filters.push_str(&format!(" AND {LEDGER_HAS_USED}")),
        _ => {}
    }
    let (total, loaded_count, used_count): (i64, i64, i64) = db.query_row(
        &format!(
            "SELECT COUNT(*),
                    COALESCE(SUM(CASE WHEN {LEDGER_LATEST_LOADED} IS NOT NULL THEN 1 ELSE 0 END),0),
                    COALESCE(SUM(CASE WHEN {LEDGER_HAS_USED} THEN 1 ELSE 0 END),0)
             FROM context_deliveries d JOIN context_clients c ON c.id=d.client_id{filters}"
        ),
        params![host],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    let offset = ((page - 1) * LEDGER_PAGE_SIZE) as i64;
    let mut query = db.prepare(&format!(
        "SELECT d.id, c.host, d.route, d.session, d.epoch, d.task_id, d.attempt_id, d.mode,
                d.provided, d.created_at,
                (SELECT r.result FROM context_receipts r WHERE r.delivery_id=d.id AND r.kind='loaded'
                 ORDER BY r.rowid DESC LIMIT 1),
                {LEDGER_LATEST_LOADED},
                (SELECT r.result FROM context_receipts r WHERE r.delivery_id=d.id AND r.kind='used'
                 ORDER BY r.rowid DESC LIMIT 1),
                COALESCE(e.handles,'[]')
         FROM context_deliveries d JOIN context_clients c ON c.id=d.client_id
         LEFT JOIN context_delivery_evidence e ON e.delivery_id=d.id{filters}
         ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?2 OFFSET ?3"
    ))?;
    let mut rows = query.query(params![host, LEDGER_PAGE_SIZE as i64, offset])?;
    let mut items = Vec::new();
    while let Some(row) = rows.next()? {
        let loaded = row
            .get::<_, Option<String>>(10)?
            .map(|s| serde_json::from_str::<Value>(&s))
            .transpose()?;
        let complete = row.get::<_, Option<i64>>(11)?;
        let used = row
            .get::<_, Option<String>>(12)?
            .map(|s| serde_json::from_str::<Value>(&s))
            .transpose()?;
        items.push(json!({
            "delivery_id": row.get::<_, String>(0)?,
            "host": row.get::<_, String>(1)?,
            "route_ref": row.get::<_, String>(2)?,
            "session_id": row.get::<_, String>(3)?,
            "context_epoch": row.get::<_, String>(4)?,
            "task_id": row.get::<_, Option<String>>(5)?,
            "attempt_id": row.get::<_, Option<String>>(6)?,
            "snapshot_mode": row.get::<_, String>(7)?,
            "provided_revision_ids": serde_json::from_str::<Vec<String>>(&row.get::<_, String>(8)?)?,
            "provided_at": row.get::<_, String>(9)?,
            "provided_evidence_handles": serde_json::from_str::<Vec<String>>(&row.get::<_, String>(13)?)?,
            "loaded": loaded,
            "used": used,
            "loaded_state": match complete {
                None => "pending",
                Some(0) => "partial",
                Some(_) => "observed",
            },
            "used_state": if used.is_some() { "observed" } else { "pending" },
        }));
    }
    Ok(json!({
        "deliveries": items,
        "summary": {"provided": total, "loaded": loaded_count, "used": used_count},
        "total": total,
        "page": page,
        "page_size": LEDGER_PAGE_SIZE,
        "window_days": 7,
    }))
}

#[cfg(test)]
#[path = "../../../../tests/v2/context_ledger.rs"]
mod tests;
