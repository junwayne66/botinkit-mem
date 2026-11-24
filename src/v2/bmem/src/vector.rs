use crate::error::{Error, Result};
use reqwest::blocking::Client;
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::io::Read;
use std::net::{Ipv4Addr, SocketAddr};
use std::time::Duration;
use url::{Host, Url};

const MAX_INPUT_BYTES: usize = 16 * 1024;
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_DIMENSIONS: usize = 8192;
const MAX_CANDIDATES: usize = 4096;
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
        .timeout(Duration::from_secs(45));
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
    let response = request
        .send()
        .map_err(|_| Error::unavailable("configured embedding endpoint could not be reached"))?;
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
    db.execute_batch("
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
    ")?;
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
    }
    Ok(())
}

pub fn drop_event(db: &Connection, event_id: &str) -> Result<()> {
    db.execute(
        "DELETE FROM vector_projection WHERE event_id=?1",
        [event_id],
    )?;
    Ok(())
}

pub fn pending(db: &Connection, route: &str) -> Result<Option<VectorJob>> {
    let mut sources =
        db.prepare_cached("SELECT source_id FROM ing_vector_routes WHERE fingerprint=?1")?;
    let mut select = db.prepare_cached(
        "SELECT e.event_id,e.occurred_at,e.content
         FROM ing_event_versions e INDEXED BY vector_events_by_source
         WHERE e.source_id=?1 AND e.active=1
           AND NOT EXISTS(SELECT 1 FROM vector_projection v WHERE v.route=?2 AND v.event_id=e.event_id)
           AND EXISTS(SELECT 1 FROM recall_events r WHERE r.event_id=e.event_id AND r.source_id=e.source_id)
           AND octet_length(e.content)<=?3
         ORDER BY e.occurred_at,e.event_id",
    )?;
    // Each source can stop after its first valid candidate. Compare only
    // those candidates to preserve oldest-first ordering across sources.
    let mut oldest: Option<(String, VectorJob)> = None;
    for source in sources.query_map([route], |row| row.get::<_, String>(0))? {
        let source = source?;
        let mut rows = select.query(params![source, route, MAX_INPUT_BYTES as i64])?;
        while let Some(row) = rows.next()? {
            let event_id: String = row.get(0)?;
            let occurred_at: String = row.get(1)?;
            if oldest
                .as_ref()
                .is_some_and(|(at, job)| (&occurred_at, &event_id) >= (at, &job.event_id))
            {
                break;
            }
            let content: Value = serde_json::from_str(
                row.get_ref(2)?
                    .as_str()
                    .map_err(|_| Error::internal("stored embedding source is not text"))?,
            )?;
            let text = content
                .as_str()
                .or_else(|| content["text"].as_str())
                .filter(|value| !value.trim().is_empty())
                .map(str::to_owned)
                .or_else(|| {
                    content["checks"]
                        .as_object()
                        .map(|_| content["checks"].to_string())
                });
            if let Some(text) = text {
                oldest = Some((
                    occurred_at,
                    VectorJob {
                        event_id,
                        source_id: source,
                        text,
                    },
                ));
                break;
            }
        }
    }
    Ok(oldest.map(|(_, job)| job))
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
           AND NOT EXISTS (SELECT 1 FROM vector_projection v WHERE v.route=?3 AND v.dimensions<>?4)
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
) -> Result<Vec<Value>> {
    let query_norm = norm(query)?;
    let mut sql = db.prepare("
        SELECT r.handle,v.event_id,r.task_id,r.actor_kind,r.occurred_at,v.dimensions,v.vector,v.norm
        FROM vector_projection v JOIN ing_event_versions e ON e.event_id=v.event_id AND e.source_id=v.source_id
        JOIN ing_vector_routes g ON g.source_id=v.source_id AND g.fingerprint=v.route
        JOIN recall_events r ON r.event_id=v.event_id AND r.source_id=v.source_id
        WHERE v.route=?1 AND e.active=1 AND (?2 IS NULL OR r.task_id=?2) LIMIT ?3
    ")?;
    let mut rows = sql.query(params![route, task, (MAX_CANDIDATES + 1) as i64])?;
    let mut hits = BinaryHeap::with_capacity(TOP_K + 1);
    let mut examined = 0;
    while let Some(row) = rows.next()? {
        examined += 1;
        if examined > MAX_CANDIDATES {
            return Err(Error::unavailable(
                "exact vector scan exceeds its bounded candidate budget",
            ));
        }
        let dimensions = usize::try_from(row.get::<_, i64>(5)?)
            .ok()
            .filter(|&width| width <= MAX_DIMENSIONS)
            .ok_or_else(|| Error::internal("stored vector dimension is invalid"))?;
        let bytes = row
            .get_ref(6)?
            .as_blob()
            .map_err(|_| Error::internal("stored vector is invalid"))?;
        let norm: f64 = row.get(7)?;
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
        let event_id: String = row.get(1)?;
        let result = json!({
            "handle": row.get::<_, String>(0)?, "event_id": event_id,
            "task_id": row.get::<_, Option<String>>(2)?, "actor_kind": row.get::<_, String>(3)?,
            "at": row.get::<_, Option<String>>(4)?, "score": score,
        });
        hits.push(Hit {
            score,
            event_id,
            result,
        });
        if hits.len() > TOP_K {
            hits.pop();
        }
    }
    Ok(hits
        .into_sorted_vec()
        .into_iter()
        .map(|hit| hit.result)
        .collect())
}
