use crate::error::{Error, Result};
use crate::retrieval::{INCLUDED_EVIDENCE, SearchExclusions};
use reqwest::blocking::Client;
use rusqlite::{Connection, OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::io::Read;
use std::net::{Ipv4Addr, SocketAddr};
use std::time::{Duration, Instant};
use url::{Host, Url};

const MAX_INPUT_BYTES: usize = 16 * 1024;
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_DIMENSIONS: usize = 8192;
const SCAN_BUDGET: Duration = Duration::from_secs(5);
const TOP_K: usize = 20;

pub struct VectorJob {
    pub event_id: String,
    pub source_id: String,
    pub text: String,
}

pub fn embed(
    protocol: &str,
    endpoint: &str,
    model: &str,
    credential: Option<&str>,
    text: &str,
) -> Result<Vec<f32>> {
    embed_with_timeout(
        protocol,
        endpoint,
        model,
        credential,
        text,
        Duration::from_secs(45),
    )
}

pub fn embed_with_timeout(
    protocol: &str,
    endpoint: &str,
    model: &str,
    credential: Option<&str>,
    text: &str,
    timeout: Duration,
) -> Result<Vec<f32>> {
    if text.trim().is_empty() || text.len() > MAX_INPUT_BYTES {
        return Err(Error::invalid(
            "embedding input is empty or exceeds the whole-event limit",
        ));
    }
    if model.trim().is_empty() {
        return Err(Error::invalid("embedding model cannot be empty"));
    }
    let (path, body) = match protocol {
        "ollama" => (
            "/api/embed",
            json!({"model": model, "input": [text], "truncate": false}),
        ),
        "openai_embeddings" => (
            "/embeddings",
            json!({"model": model, "input": [text], "encoding_format": "float"}),
        ),
        _ => return Err(Error::invalid("model protocol does not support embedding")),
    };
    let mut url = Url::parse(endpoint).map_err(|_| Error::invalid("invalid embedding endpoint"))?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(Error::invalid("invalid embedding endpoint"));
    }
    let mut builder = Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(3))
        .timeout(timeout);
    match url.host() {
        Some(Host::Ipv4(address)) if address.is_loopback() => (),
        Some(Host::Ipv6(address)) if address.is_loopback() => (),
        Some(Host::Domain("localhost")) => {
            let port = url
                .port_or_known_default()
                .ok_or_else(|| Error::invalid("invalid endpoint port"))?;
            builder = builder.resolve("localhost", SocketAddr::from((Ipv4Addr::LOCALHOST, port)));
        }
        _ => {
            return Err(Error::denied(
                "automatic embedding requires a loopback endpoint",
            ));
        }
    }
    let path = format!("{}{path}", url.path().trim_end_matches('/'));
    url.set_path(&path);
    let client = builder
        .build()
        .map_err(|_| Error::internal("could not create embedding HTTP client"))?;
    let mut request = client.post(url).json(&body);
    if let Some(credential) = credential {
        request = request.bearer_auth(credential);
    }
    let response = request.send().map_err(|error| {
        if error.is_timeout() {
            Error::new(
                503,
                "vector_query_timeout",
                "configured embedding endpoint timed out",
            )
        } else {
            Error::unavailable("configured embedding endpoint could not be reached")
        }
    })?;
    if response.status().as_u16() == 413 {
        return Err(Error::new(
            422,
            "embedding_input_too_long",
            "embedding input exceeds the model context",
        ));
    }
    if response.status().as_u16() == 400 {
        let detail = serde_json::from_reader::<_, Value>(response.take(8192)).unwrap_or_default();
        let context_limit = match protocol {
            "ollama" => detail["error"] == "the input length exceeds the context length",
            "openai_embeddings" => detail["error"]["code"] == "context_length_exceeded",
            _ => false,
        };
        if context_limit {
            return Err(Error::new(
                422,
                "embedding_input_too_long",
                "embedding input exceeds the model context",
            ));
        }
        return Err(Error::unavailable(
            "configured embedding endpoint returned HTTP 400",
        ));
    }
    if !response.status().is_success() {
        return Err(Error::unavailable(format!(
            "configured embedding endpoint returned HTTP {}",
            response.status().as_u16()
        )));
    }
    let mut raw = Vec::new();
    response
        .take((MAX_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|_| Error::unavailable("embedding response could not be read"))?;
    if raw.len() > MAX_RESPONSE_BYTES {
        return Err(invalid_vector(
            "embedding response exceeds the maximum size",
        ));
    }
    let response: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_vector("embedding response is not JSON"))?;
    let vector = match protocol {
        "ollama" => response["embeddings"]
            .as_array()
            .filter(|rows| rows.len() == 1)
            .and_then(|rows| rows[0].as_array()),
        _ => response["data"]
            .as_array()
            .filter(|rows| rows.len() == 1)
            .and_then(|rows| {
                if rows[0]
                    .get("index")
                    .is_some_and(|index| index.as_u64() != Some(0))
                {
                    None
                } else {
                    rows[0]["embedding"].as_array()
                }
            }),
    }
    .ok_or_else(|| invalid_vector("embedding result must contain exactly one numeric vector"))?;
    if vector.is_empty() || vector.len() > MAX_DIMENSIONS {
        return Err(invalid_vector(
            "embedding dimension is outside the supported range",
        ));
    }
    let vector = vector
        .iter()
        .map(|part| {
            part.as_f64()
                .map(|value| value as f32)
                .filter(|value| value.is_finite())
                .ok_or_else(|| {
                    invalid_vector("embedding contains a non-finite or non-numeric component")
                })
        })
        .collect::<Result<Vec<_>>>()?;
    norm(&vector)?;
    Ok(vector)
}

/// Each accepted slice covers a contiguous range of the original UTF-8 text.
/// Pool unit vectors by character count so a 413 split preserves both halves.
pub fn embed_document(
    text: &str,
    mut embed_chunk: impl FnMut(&str) -> Result<Vec<f32>>,
) -> Result<Vec<f32>> {
    let mut sum: Vec<f64> = Vec::new();
    let mut remaining = text;
    while !remaining.is_empty() {
        let end = remaining.floor_char_boundary(MAX_INPUT_BYTES.min(remaining.len()));
        let (chunk, rest) = remaining.split_at(end);
        remaining = rest;
        let mut pending = vec![(chunk, 0)];
        while let Some((part, depth)) = pending.pop() {
            if part.trim().is_empty() {
                continue;
            }
            let vector = match embed_chunk(part) {
                Ok(value) => value,
                Err(error) if error.code == "embedding_input_too_long" && depth < 8 => {
                    let mid = part.ceil_char_boundary(part.len() / 2);
                    if mid == 0 || mid == part.len() {
                        return Err(error);
                    }
                    let (first, second) = part.split_at(mid);
                    pending.push((second, depth + 1));
                    pending.push((first, depth + 1));
                    continue;
                }
                Err(error) => return Err(error),
            };
            if part.len() == text.len() {
                return Ok(vector);
            }
            if sum.is_empty() {
                sum.resize(vector.len(), 0.0);
            } else if sum.len() != vector.len() {
                return Err(invalid_vector(
                    "model dimensions changed between document slices",
                ));
            }
            let weight = part.chars().count() as f64 / norm(&vector)?;
            for (component, value) in sum.iter_mut().zip(vector) {
                *component += weight * f64::from(value);
            }
        }
    }
    let pooled: Vec<f32> = sum.into_iter().map(|value| value as f32).collect();
    norm(&pooled)?;
    Ok(pooled)
}

fn invalid_vector(message: &str) -> Error {
    Error::new(422, "invalid_embedding", message)
}

fn norm(vector: &[f32]) -> Result<f64> {
    if vector.is_empty()
        || vector.len() > MAX_DIMENSIONS
        || vector.iter().any(|value| !value.is_finite())
    {
        return Err(invalid_vector(
            "embedding dimension or component is outside the supported range",
        ));
    }
    let norm = vector
        .iter()
        .map(|value| {
            let value = f64::from(*value);
            value * value
        })
        .sum::<f64>()
        .sqrt();
    if norm == 0.0 {
        return Err(invalid_vector("embedding must have a nonzero norm"));
    }
    Ok(norm)
}

pub fn migrate(db: &Connection) -> Result<()> {
    let tx = db.unchecked_transaction()?;
    let new_queue: bool = tx.query_row(
        "SELECT NOT EXISTS(SELECT 1 FROM sqlite_schema WHERE name='vector_work')",
        [],
        |row| row.get(0),
    )?;
    tx.execute_batch("
        CREATE TABLE IF NOT EXISTS ing_vector_routes (
            source_id TEXT PRIMARY KEY REFERENCES ing_sources(source_id), fingerprint TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS vector_events_by_source
            ON ing_event_versions(source_id,occurred_at,event_id) WHERE active=1;
        CREATE TABLE IF NOT EXISTS vector_projection (
            event_id TEXT NOT NULL REFERENCES ing_event_versions(event_id),
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id),
            route TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL, norm REAL NOT NULL,
            PRIMARY KEY(route, event_id)
        );
        CREATE INDEX IF NOT EXISTS vector_projection_source ON vector_projection(source_id);
        CREATE INDEX IF NOT EXISTS vector_projection_event ON vector_projection(event_id);
        CREATE TABLE IF NOT EXISTS vector_shadow_batches(
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id), route TEXT NOT NULL,
            cursor TEXT NOT NULL, authority_cut TEXT NOT NULL, PRIMARY KEY(source_id,route)
        );
        CREATE TABLE IF NOT EXISTS vector_shadow_rows(
            source_id TEXT NOT NULL, route TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES ing_event_versions(event_id),
            dimensions INTEGER NOT NULL, vector BLOB NOT NULL, norm REAL NOT NULL,
            PRIMARY KEY(route,event_id),
            FOREIGN KEY (source_id,route) REFERENCES vector_shadow_batches(source_id,route) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS vector_shadow_event ON vector_shadow_rows(event_id);
        CREATE TABLE IF NOT EXISTS vector_work (
            event_id TEXT PRIMARY KEY REFERENCES ing_event_versions(event_id),
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id), route TEXT NOT NULL,
            occurred_at TEXT NOT NULL, available_at INTEGER NOT NULL DEFAULT (unixepoch()),
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS vector_work_next
            ON vector_work(source_id,route,available_at,occurred_at,event_id);
        CREATE TRIGGER IF NOT EXISTS enqueue_vector_evidence AFTER INSERT ON recall_events BEGIN
            INSERT OR IGNORE INTO vector_work(event_id,source_id,route,occurred_at)
            SELECT NEW.event_id,NEW.source_id,g.fingerprint,coalesce(NEW.occurred_at,'')
            FROM ing_vector_routes g WHERE g.source_id=NEW.source_id;
        END;
        CREATE TRIGGER IF NOT EXISTS complete_vector_work AFTER INSERT ON vector_projection BEGIN
            DELETE FROM vector_work WHERE event_id=NEW.event_id AND route=NEW.route;
        END;
    ")?;
    if new_queue {
        enqueue_existing(&tx, None)?;
    }
    tx.commit()?;
    Ok(())
}

fn enqueue_existing(db: &Connection, source: Option<&str>) -> Result<()> {
    db.execute(
        "INSERT OR IGNORE INTO vector_work(event_id,source_id,route,occurred_at)
         SELECT r.event_id,r.source_id,g.fingerprint,coalesce(r.occurred_at,'')
         FROM ing_vector_routes g CROSS JOIN recall_events r ON r.source_id=g.source_id
         JOIN ing_event_versions e ON e.event_id=r.event_id AND e.source_id=r.source_id
         WHERE e.active=1 AND (?1 IS NULL OR g.source_id=?1)
           AND NOT EXISTS(SELECT 1 FROM vector_projection v WHERE v.route=g.fingerprint AND v.event_id=r.event_id)",
        [source],
    )?;
    Ok(())
}

pub fn set_route(db: &Connection, source_id: &str, route: Option<&str>) -> Result<()> {
    let exists: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_sources WHERE source_id=?1)",
        [source_id],
        |row| row.get(0),
    )?;
    if !exists {
        return Err(Error::missing("embedding source is not available"));
    }
    let changed = match route {
        Some(route) if !route.is_empty() => db.execute(
            "INSERT INTO ing_vector_routes(source_id,fingerprint) VALUES(?1,?2)
             ON CONFLICT(source_id) DO UPDATE SET fingerprint=excluded.fingerprint
             WHERE fingerprint<>excluded.fingerprint",
            params![source_id, route],
        )?,
        Some(_) => return Err(Error::invalid("embedding route fingerprint is empty")),
        None => db.execute(
            "DELETE FROM ing_vector_routes WHERE source_id=?1",
            [source_id],
        )?,
    };
    if changed != 0 {
        db.execute(
            "DELETE FROM vector_projection WHERE source_id=?1",
            [source_id],
        )?;
        db.execute("DELETE FROM vector_work WHERE source_id=?1", [source_id])?;
        if route.is_some() {
            enqueue_existing(db, Some(source_id))?;
        } else {
            db.execute(
                "DELETE FROM vector_shadow_batches WHERE source_id=?1",
                [source_id],
            )?;
        }
    }
    Ok(())
}

pub fn drop_event(db: &Connection, event_id: &str) -> rusqlite::Result<()> {
    db.execute(
        "DELETE FROM vector_projection WHERE event_id=?1",
        [event_id],
    )?;
    db.execute("DELETE FROM vector_work WHERE event_id=?1", [event_id])?;
    db.execute(
        "DELETE FROM vector_shadow_rows WHERE event_id=?1",
        [event_id],
    )?;
    Ok(())
}

struct ShadowCut {
    cursor: String,
    active_route: String,
    event_ids: Vec<String>,
    digest: String,
}

fn shadow_cut(db: &Connection, source: &str) -> Result<ShadowCut> {
    let (cursor, active_route): (String, String) = db
        .query_row(
            "SELECT s.cursor,r.fingerprint FROM ing_sources s
         JOIN ing_vector_routes r ON r.source_id=s.source_id WHERE s.source_id=?1",
            [source],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?
        .ok_or_else(|| Error::denied("source does not have embedding permission"))?;
    let mut query = db.prepare(
        "SELECT e.event_id FROM ing_event_versions e JOIN recall_events r ON r.event_id=e.event_id
         WHERE e.source_id=?1 AND e.active=1 ORDER BY e.event_id",
    )?;
    let event_ids = query
        .query_map([source], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let digest = format!(
        "sha256:{:x}",
        Sha256::digest(serde_json::to_vec(&json!([source, cursor, event_ids]))?)
    );
    Ok(ShadowCut {
        cursor,
        active_route,
        event_ids,
        digest,
    })
}

pub fn shadow_inputs(db: &Connection, source: &str) -> Result<Value> {
    let cut = shadow_cut(db, source)?;
    let items = cut
        .event_ids
        .iter()
        .map(|event| crate::retrieval::expand(db, &format!("event_{event}")))
        .collect::<Result<Vec<_>>>()?;
    Ok(
        json!({"source_id":source,"cursor":cut.cursor,"authority_cut":cut.digest,
        "serving_namespace":cut.active_route,"items":items}),
    )
}

#[derive(Deserialize)]
struct ShadowVector {
    event_id: String,
    vector: Vec<f32>,
}

fn required_text<'a>(body: &'a Value, field: &str) -> Result<&'a str> {
    body[field]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= 4096)
        .ok_or_else(|| Error::invalid(format!("{field} is required")))
}

pub fn shadow_commit(db: &mut Connection, route: &str, body: &Value) -> Result<Value> {
    let source = required_text(body, "source_id")?;
    let snapshot = required_text(body, "authority_cut")?;
    let mut staged: Vec<ShadowVector> = serde_json::from_value(body["vectors"].clone())?;
    staged.sort_by(|a, b| a.event_id.cmp(&b.event_id));
    let tx = db.transaction()?;
    let current = shadow_cut(&tx, source)?;
    if snapshot != current.digest {
        return Err(Error::new(
            409,
            "projection_stale",
            "candidate input watermark has changed",
        ));
    }
    if route == current.active_route {
        return Err(Error::conflict(
            "candidate namespace must differ from the active namespace",
        ));
    }
    if staged.is_empty()
        || !staged
            .iter()
            .map(|item| &item.event_id)
            .eq(current.event_ids.iter())
    {
        return Err(Error::invalid(
            "candidate vectors must exactly cover the approved snapshot",
        ));
    }
    tx.execute(
        "DELETE FROM vector_shadow_batches WHERE source_id=?1 AND route=?2",
        params![source, route],
    )?;
    tx.execute(
        "INSERT INTO vector_shadow_batches VALUES (?1,?2,?3,?4)",
        params![source, route, current.cursor, snapshot],
    )?;
    let dimensions = staged[0].vector.len() as i64;
    for row in &staged {
        if row.vector.len() as i64 != dimensions {
            return Err(invalid_vector(
                "one namespace must use a consistent dimension",
            ));
        }
        let norm = norm(&row.vector)?;
        let encoded: Vec<u8> = row
            .vector
            .iter()
            .flat_map(|part| part.to_le_bytes())
            .collect();
        tx.execute(
            "INSERT INTO vector_shadow_rows VALUES (?1,?2,?3,?4,?5,?6)",
            params![source, route, row.event_id, dimensions, encoded, norm],
        )?;
    }
    tx.commit()?;
    Ok(
        json!({"staged":staged.len(),"dimensions":dimensions,"namespace":route,"applied_through":current.cursor}),
    )
}

pub fn shadow_activate(
    db: &mut Connection,
    route: &str,
    service_route: Option<&str>,
    body: &Value,
) -> Result<Value> {
    let source = required_text(body, "source_id")?;
    let tx = db.transaction()?;
    let (staged_cursor,cut): (String,String) = tx.query_row(
        "SELECT cursor,authority_cut FROM vector_shadow_batches WHERE source_id=?1 AND route=?2",
        params![source, route],
        |row|Ok((row.get(0)?,row.get(1)?)),
    ).optional()?.ok_or_else(|| Error::missing("staged candidate namespace unavailable"))?;
    let current = shadow_cut(&tx, source)?;
    if cut != current.digest {
        return Err(Error::new(
            409,
            "projection_stale",
            format!(
                "candidate watermark {staged_cursor} no longer matches authority watermark {}",
                current.cursor
            ),
        ));
    }
    if service_route != Some(route) {
        return Err(Error::conflict(
            "bind the eligible embedding model before activating its staged namespace",
        ));
    }
    let dimensions_agree: bool = tx.query_row(
        "SELECT NOT EXISTS(SELECT 1 FROM vector_projection v JOIN vector_shadow_rows s
         ON v.route=s.route WHERE s.source_id=?1 AND s.route=?2 AND v.dimensions<>s.dimensions)",
        params![source, route],
        |row| row.get(0),
    )?;
    if !dimensions_agree {
        return Err(invalid_vector(
            "candidate dimension disagrees with the live namespace",
        ));
    }
    set_route(&tx, source, Some(route))?;
    let installed = tx.execute(
        "INSERT INTO vector_projection(event_id,source_id,route,dimensions,vector,norm)
         SELECT event_id,source_id,route,dimensions,vector,norm FROM vector_shadow_rows
         WHERE source_id=?1 AND route=?2 ON CONFLICT(route,event_id) DO NOTHING",
        params![source, route],
    )?;
    tx.execute(
        "DELETE FROM vector_shadow_batches WHERE source_id=?1 AND route=?2",
        params![source, route],
    )?;
    tx.commit()?;
    Ok(
        json!({"enabled":true,"namespace":route,"vectors_installed":installed,"applied_through":staged_cursor}),
    )
}

pub fn sources(db: &Connection, route: &str) -> Result<Vec<String>> {
    let mut statement = db.prepare(
        "SELECT source_id FROM ing_vector_routes WHERE fingerprint=?1 ORDER BY source_id",
    )?;
    Ok(statement
        .query_map([route], |row| row.get(0))?
        .collect::<rusqlite::Result<_>>()?)
}

pub fn pending(db: &Connection, route: &str, source: &str) -> Result<Option<VectorJob>> {
    let row = db
        .query_row(
            "SELECT w.event_id,e.content
         FROM vector_work w JOIN ing_event_versions e ON e.event_id=w.event_id
         JOIN ing_vector_routes g ON g.source_id=w.source_id AND g.fingerprint=w.route
         WHERE w.source_id=?1 AND w.route=?2 AND w.available_at<=unixepoch() AND e.active=1
         ORDER BY w.available_at,w.occurred_at,w.event_id LIMIT 1",
            params![source, route],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?;
    let Some((event_id, raw)) = row else {
        return Ok(None);
    };
    let content: Value = serde_json::from_str(&raw)?;
    let text = content
        .as_str()
        .or_else(|| content["text"].as_str())
        .map(str::to_owned)
        .or_else(|| {
            content["checks"]
                .as_object()
                .map(|_| content["checks"].to_string())
        })
        .unwrap_or_default();
    Ok(Some(VectorJob {
        event_id,
        source_id: source.to_owned(),
        text,
    }))
}

pub fn still_pending(db: &Connection, job: &VectorJob, route: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM vector_work WHERE event_id=?1 AND source_id=?2 AND route=?3)",
        params![job.event_id, job.source_id, route],
        |row| row.get(0),
    )?)
}

pub fn defer(db: &Connection, job: &VectorJob, route: &str, reason: &str) -> Result<()> {
    db.execute(
        "UPDATE vector_work SET available_at=unixepoch()+min(3600,30*(1<<min(attempts,7))),
         attempts=attempts+1,last_error=?3 WHERE event_id=?1 AND route=?2",
        params![job.event_id, route, reason],
    )?;
    Ok(())
}

pub fn commit(db: &Connection, job: &VectorJob, route: &str, vector: &[f32]) -> Result<()> {
    let norm = norm(vector)?;
    let encoded: Vec<u8> = vector.iter().flat_map(|part| part.to_le_bytes()).collect();
    let dim = vector.len() as i64;
    let inserted = db.execute(
        "INSERT INTO vector_projection(event_id,source_id,route,dimensions,vector,norm)
         SELECT e.event_id,e.source_id,g.fingerprint,?4,?5,?6
         FROM ing_event_versions e JOIN ing_vector_routes g ON g.source_id=e.source_id
         JOIN recall_events r ON r.event_id=e.event_id AND r.source_id=e.source_id
         WHERE e.event_id=?1 AND e.source_id=?2 AND g.fingerprint=?3 AND e.active=1
           AND coalesce((SELECT v.dimensions FROM vector_projection v WHERE v.route=?3 LIMIT 1),?4)=?4
         ON CONFLICT(route,event_id) DO NOTHING",
        params![job.event_id, job.source_id, route, dim, encoded, norm],
    )?;
    if inserted > 0 {
        return Ok(());
    }
    let exists: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM vector_projection v JOIN ing_event_versions e ON e.event_id=v.event_id
         JOIN ing_vector_routes g ON g.source_id=v.source_id AND g.fingerprint=v.route
         WHERE v.event_id=?1 AND v.route=?2 AND v.dimensions=?3 AND e.active=1 AND v.source_id=?4)",
        params![job.event_id, route, dim, job.source_id], |row| row.get(0),
    )?;
    if exists {
        Ok(())
    } else {
        Err(Error::conflict(
            "embedding source, binding or dimensions changed while processing",
        ))
    }
}

struct Hit {
    score: f64,
    event_id: String,
    result: Value,
}

impl Ord for Hit {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .score
            .total_cmp(&self.score)
            .then_with(|| self.event_id.cmp(&other.event_id))
    }
}

impl PartialOrd for Hit {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for Hit {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for Hit {}

pub fn search(
    db: &Connection,
    route: &str,
    query: &[f32],
    task: Option<&str>,
    exclude: &SearchExclusions,
) -> Result<Vec<Value>> {
    search_with_access(db, route, query, task, exclude, None, -1.0)
}

pub fn search_for_client(
    db: &Connection,
    route: &str,
    query: &[f32],
    task: &str,
    client: &str,
    cosine_floor: f64,
) -> Result<Vec<Value>> {
    search_with_access(
        db,
        route,
        query,
        Some(task),
        &SearchExclusions::default(),
        Some(client),
        cosine_floor,
    )
}

fn search_with_access(
    db: &Connection,
    route: &str,
    query: &[f32],
    task: Option<&str>,
    exclude: &SearchExclusions,
    client: Option<&str>,
    cosine_floor: f64,
) -> Result<Vec<Value>> {
    let query_norm = norm(query)?;
    db.execute_batch("PRAGMA mmap_size=2147418112")?;
    let owner_has_denials = client.is_none()
        && db.query_row(
            "SELECT EXISTS(SELECT 1 FROM governance_owner_read_denials)",
            [],
            |r| r.get::<_, bool>(0),
        )?;
    let filtered =
        task.is_some() || exclude.evidence.is_some() || client.is_some() || owner_has_denials;
    // Read vector blobs in storage order. A scoped search builds its eligible
    // ID set from the projected IDs, even while most source events await indexing.
    let eligibility = if filtered {
        format!(
            "(v.event_id,v.source_id) IN (
            SELECT r.event_id,r.source_id FROM vector_projection p
            CROSS JOIN recall_events r ON r.event_id=p.event_id
            CROSS JOIN ing_vector_routes g ON g.source_id=r.source_id AND g.fingerprint=p.route
            CROSS JOIN ing_sources s ON s.source_id=r.source_id
            CROSS JOIN ing_event_versions e ON e.event_id=r.event_id AND e.source_id=r.source_id
            WHERE p.route=?1 AND e.active=1 AND (?2 IS NULL OR r.task_id=?2)
              AND {INCLUDED_EVIDENCE}
              AND (?4 IS NOT NULL OR NOT EXISTS(SELECT 1 FROM governance_owner_read_denials d WHERE d.root_id=e.root_id))
              AND (?4 IS NULL OR EXISTS (
                   SELECT 1 FROM recall_grants a JOIN context_task_access t
                      ON t.client_id=a.client_id AND t.task_id=r.task_id
                   WHERE a.source_id=r.source_id AND a.client_id=?4
                      AND NOT EXISTS(SELECT 1 FROM recall_root_denials d
                          WHERE d.client_id=?4 AND d.root_id=e.root_id)))
        )"
        )
    } else {
        "v.source_id IN (SELECT source_id FROM ing_vector_routes WHERE fingerprint=?1)".into()
    };
    let mut sql = db.prepare(&format!(
        "SELECT v.event_id,v.dimensions,v.vector,v.norm,v.source_id
        FROM vector_projection v NOT INDEXED WHERE v.route=?1 AND {eligibility}"
    ))?;
    let mut detail = db.prepare("SELECT r.handle,r.task_id,r.actor_kind,r.occurred_at
        FROM recall_events r JOIN ing_event_versions e ON e.event_id=r.event_id AND e.source_id=r.source_id
        WHERE r.event_id=?1 AND r.source_id=?2 AND e.active=1")?;
    let mut rows = if filtered {
        sql.query(params![route, task, exclude.evidence, client])?
    } else {
        sql.query([route])?
    };
    let mut hits: BinaryHeap<Hit> = BinaryHeap::with_capacity(TOP_K);
    // The read connection is private to this search. SQLite also checks the
    // deadline while stepping through candidates that the filters skip.
    let deadline = Instant::now() + SCAN_BUDGET;
    db.progress_handler(1000, Some(move || Instant::now() >= deadline))?;
    let evaluation = (|| -> Result<()> {
        while let Some(row) = rows.next().map_err(scan_error)? {
            let dimensions = usize::try_from(row.get::<_, i64>(1)?)
                .ok()
                .filter(|&width| width <= MAX_DIMENSIONS)
                .ok_or_else(|| Error::internal("stored vector dimension is invalid"))?;
            let bytes = row
                .get_ref(2)?
                .as_blob()
                .map_err(|_| Error::internal("stored vector is invalid"))?;
            let norm: f64 = row.get(3)?;
            if dimensions != query.len()
                || bytes.len() != dimensions * 4
                || !norm.is_finite()
                || norm <= 0.0
            {
                return Err(Error::conflict(
                    "query shape does not match the authorized embedding projection",
                ));
            }
            let dot = query
                .iter()
                .zip(bytes.as_chunks::<4>().0)
                .map(|(a, b)| f64::from(*a) * f64::from(f32::from_le_bytes(*b)))
                .sum::<f64>();
            if !dot.is_finite() {
                return Err(Error::internal("stored vector is invalid"));
            }
            let score = (dot / (norm * query_norm)).clamp(-1.0, 1.0);
            if score < cosine_floor {
                continue;
            }
            let event_id = row
                .get_ref(0)?
                .as_str()
                .map_err(|_| Error::internal("stored vector ID is invalid"))?;
            if hits.len() == TOP_K {
                let worst = hits.peek().unwrap();
                if score
                    .total_cmp(&worst.score)
                    .then_with(|| worst.event_id.as_str().cmp(event_id))
                    .is_le()
                {
                    continue;
                }
            }
            // A stale projection cannot displace an eligible hit. Only prospective
            // winners need an evidence lookup or allocation of the response fields.
            let result = detail
            .query_row(params![event_id, row.get::<_, String>(4)?], |meta| {
                Ok(json!({
                    "handle":meta.get::<_,String>(0)?, "task_id":meta.get::<_,Option<String>>(1)?,
                    "actor_kind":meta.get::<_,String>(2)?, "at":meta.get::<_,Option<String>>(3)?,
                    "event_id":event_id, "score":score,
                }))
            })
            .optional()
            .map_err(scan_error)?;
            if let Some(result) = result {
                if hits.len() == TOP_K {
                    hits.pop();
                }
                hits.push(Hit {
                    score,
                    event_id: event_id.to_owned(),
                    result,
                });
            }
        }
        Ok(())
    })();
    db.progress_handler(0, None::<fn() -> bool>)?;
    evaluation?;
    Ok(hits
        .into_sorted_vec()
        .into_iter()
        .map(|hit| hit.result)
        .collect())
}

fn scan_error(error: rusqlite::Error) -> Error {
    if error.sqlite_error_code() == Some(rusqlite::ErrorCode::OperationInterrupted) {
        Error::new(
            503,
            "vector_scan_timeout",
            "exact vector scan exceeded its time budget",
        )
    } else {
        error.into()
    }
}
