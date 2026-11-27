use crate::context;
use crate::error::{Error, Result};
use crate::{retrieval, vector};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Mode {
    Lexical,
    Vector,
    Union,
}

#[derive(Deserialize)]
pub(crate) struct Selection {
    pub task_id: String,
    pub query: String,
    pub mode: Mode,
    candidate_limit: usize,
    evidence_slots: usize,
    evidence_byte_limit_utf8: usize,
    semantic_cosine_floor: f64,
}

impl Selection {
    pub fn validate(&self) -> Result<()> {
        if self.query.trim().is_empty()
            || self.query.len() > 4096
            || !(1..=20).contains(&self.candidate_limit)
            || !(1..=50).contains(&self.evidence_slots)
            || !(1..=262_144).contains(&self.evidence_byte_limit_utf8)
            || !(-1.0..=1.0).contains(&self.semantic_cosine_floor)
        {
            return Err(Error::invalid(
                "invalid query, evidence limits, or cosine floor",
            ));
        }
        Ok(())
    }
}

#[derive(Deserialize, Serialize)]
struct FrozenSeeds {
    scope: String,
    mode: Mode,
    candidate_limit: usize,
    slots: usize,
    bytes: usize,
    handles: Vec<String>,
}

pub(crate) fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS recall_seed_sets (
             id TEXT PRIMARY KEY, client_id TEXT NOT NULL, task_id TEXT NOT NULL,
             snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,
             FOREIGN KEY (client_id) REFERENCES context_clients(id),
             FOREIGN KEY (task_id) REFERENCES context_tasks(id)
         );",
    )?;
    Ok(())
}

fn scope_fingerprint(db: &Connection, client: &str, task: &str) -> Result<String> {
    let mut statement = db.prepare(
        "SELECT r.handle,e.version_key,l.run,l.call,l.role
         FROM recall_events r JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1
         JOIN recall_grants a ON a.source_id=r.source_id AND a.client_id=?1
         JOIN context_task_access t ON t.task_id=r.task_id AND t.client_id=a.client_id
         LEFT JOIN recall_tool_links l ON l.handle=r.handle
         WHERE r.task_id=?2
           AND NOT EXISTS(SELECT 1 FROM recall_root_denials d WHERE d.client_id=?1 AND d.root_id=e.root_id)
         ORDER BY r.handle",
    )?;
    let mut rows = statement.query(params![client, task])?;
    let mut hasher = Sha256::new();
    hasher.update(serde_json::to_vec(&(client, task))?);
    while let Some(row) = rows.next()? {
        let fields = (0..5)
            .map(|index| row.get::<_, Option<String>>(index))
            .collect::<rusqlite::Result<Vec<_>>>()?;
        hasher.update(serde_json::to_vec(&fields)?);
    }
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

pub(crate) fn freeze(
    db: &Connection,
    writable: &mut Connection,
    reader: &str,
    request: Selection,
    embedding: Option<(&str, &[f32])>,
) -> Result<Value> {
    if !context::task_access(db, &request.task_id, reader)? {
        return Err(Error::missing("task is unavailable"));
    }
    let lexical = if matches!(request.mode, Mode::Lexical | Mode::Union) {
        retrieval::lexical_for_client(
            db,
            &request.task_id,
            reader,
            &request.query,
            request.candidate_limit,
        )?
    } else {
        Vec::new()
    };
    let semantic = if let Some((route, embedding)) = embedding {
        vector::search_for_client(
            db,
            route,
            embedding,
            &request.task_id,
            reader,
            request.semantic_cosine_floor,
        )?
    } else {
        Vec::new()
    };
    let evidence = match request.mode {
        Mode::Lexical => lexical,
        Mode::Vector => semantic,
        Mode::Union => retrieval::fuse_evidence(
            db,
            &request.query,
            &lexical,
            &semantic,
            request.candidate_limit,
        )?["results"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
    };
    let frozen = FrozenSeeds {
        scope: scope_fingerprint(db, reader, &request.task_id)?,
        mode: request.mode,
        candidate_limit: request.candidate_limit,
        slots: request.evidence_slots,
        bytes: request.evidence_byte_limit_utf8,
        handles: evidence
            .iter()
            .take(request.candidate_limit)
            .map(|value| {
                value["handle"].as_str().map(str::to_owned).ok_or_else(|| {
                    Error::internal("retrieval returned an invalid evidence reference")
                })
            })
            .collect::<Result<_>>()?,
    };
    let payload = serde_json::to_string(&frozen)?;
    let fingerprint = format!("sha256:{:x}", Sha256::digest(payload.as_bytes()));
    let id = format!("seed_{}", uuid::Uuid::new_v4().simple());
    let tx = writable.transaction()?;
    if !context::task_access(&tx, &request.task_id, reader)?
        || scope_fingerprint(&tx, reader, &request.task_id)? != frozen.scope
    {
        return Err(Error::conflict("visible evidence changed during selection"));
    }
    tx.execute(
        "INSERT INTO recall_seed_sets VALUES (?1,?2,?3,?4,?5)",
        params![id, reader, request.task_id, payload, fingerprint],
    )?;
    tx.commit()?;
    Ok(
        json!({"seed_set":id,"seed_fingerprint":fingerprint,"scope_fingerprint":frozen.scope,
        "candidate_limit":frozen.candidate_limit,"read_budget":{"slots":frozen.slots,"bytes":frozen.bytes},
        "mode":frozen.mode,"seed_refs":frozen.handles,"semantic_used":embedding.is_some()}),
    )
}

pub(crate) fn replay(
    db: &Connection,
    id: &str,
    reader: &str,
    task: &str,
    neighbor: bool,
) -> Result<Value> {
    let (payload, fingerprint): (String,String) = db.query_row(
        "SELECT snapshot,fingerprint FROM recall_seed_sets WHERE id=?1 AND client_id=?2 AND task_id=?3",
        params![id, reader, task], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()?.ok_or_else(|| Error::missing("seed set is unavailable"))?;
    let frozen: FrozenSeeds = serde_json::from_str(&payload)?;
    if scope_fingerprint(db, reader, task)? != frozen.scope {
        return Err(Error::conflict(
            "visible evidence changed since the seed set was frozen",
        ));
    }
    let seed_rows = frozen
        .handles
        .iter()
        .map(|handle| retrieval::expand_for_client(db, handle, reader))
        .collect::<Result<Vec<_>>>()?;
    let neighbors = if neighbor {
        retrieval::same_run_neighbors(db, task, Some(reader), &frozen.handles)?
    } else {
        Vec::new()
    };
    let mut evidence = Vec::new();
    let mut expanded_refs = Vec::new();
    let mut used = 0;
    let mut evidence_gap = false;
    for (is_neighbor, row) in seed_rows
        .into_iter()
        .map(|row| (false, row))
        .chain(neighbors.into_iter().map(|row| (true, row)))
    {
        let bytes = row["text"]
            .as_str()
            .ok_or_else(|| Error::internal("evidence text is missing"))?
            .len();
        if evidence.len() >= frozen.slots || used + bytes > frozen.bytes {
            evidence_gap = true;
            continue;
        }
        used += bytes;
        if is_neighbor {
            expanded_refs.push(row["handle"].clone());
        }
        evidence.push(row);
    }
    Ok(json!({"evidence":evidence,"observations":[],"window":{
        "frozen_seed_set":id,"seed_fingerprint":fingerprint,"scope_fingerprint":frozen.scope,
        "ranked_seed_refs":frozen.handles,"expanded_refs":expanded_refs,"evidence_gap":evidence_gap,
        "candidate_limit":frozen.candidate_limit,"read_budget":{"slots":frozen.slots,"bytes":frozen.bytes},
        "mode":frozen.mode,"neighbor":neighbor,"evidence_bytes_utf8":used,"result_status":"unknown"
    }}))
}
