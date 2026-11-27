use crate::error::{Error, Result};
use crate::{context::BoundClient, retrieval};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::collections::HashSet;

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS community_note_revisions(
        note_id TEXT NOT NULL, revision TEXT NOT NULL, parent TEXT, event_id TEXT NOT NULL,
        context TEXT, PRIMARY KEY(note_id,revision),
        FOREIGN KEY(event_id) REFERENCES ing_event_versions(event_id));
      CREATE TABLE IF NOT EXISTS community_note_heads(note_id TEXT PRIMARY KEY,revision TEXT NOT NULL,
        FOREIGN KEY(note_id,revision) REFERENCES community_note_revisions(note_id,revision));
      CREATE TABLE IF NOT EXISTS community_graph_edges (graph_id TEXT NOT NULL,edge_id TEXT NOT NULL,
        source TEXT NOT NULL,target TEXT NOT NULL,handle TEXT NOT NULL,PRIMARY KEY(graph_id,edge_id));
      CREATE INDEX IF NOT EXISTS community_notes_by_event ON community_note_revisions(event_id);
      CREATE TRIGGER IF NOT EXISTS community_notes_forget AFTER INSERT ON ing_root_revocations BEGIN
        UPDATE community_note_revisions SET context=NULL WHERE event_id IN (
          SELECT event_id FROM ing_event_versions WHERE root_id=NEW.root_id);
        DELETE FROM community_graph_edges WHERE handle IN (
          SELECT 'event_' || event_id FROM ing_event_versions WHERE root_id=NEW.root_id);
      END;")?;
    Ok(())
}

pub fn slice(db: &Connection, reader: Option<&BoundClient>, body: &Value) -> Result<Value> {
    let handle = field(body, "handle")?;
    let original = match reader {
        Some(reader) => retrieval::expand_for_client(db, handle, &reader.id)?,
        None => retrieval::expand(db, handle)?,
    };
    let splitter = match body["splitter"].as_str() {
        Some(name @ ("sentence" | "clause")) => name,
        _ => return Err(Error::invalid("splitter must be sentence or clause")),
    };
    let text = original["text"].as_str().unwrap_or_default();
    let parts = text.split_inclusive(|c| {
        matches!(c, '。' | '\n')
            || (splitter == "clause" && matches!(c, '！' | '？' | '；' | '!' | '?' | ';'))
    });
    let mut start = 0;
    let mut spans = Vec::new();
    for part in parts.take(256) {
        let end = start + part.len();
        spans.push(json!({"byte_start": start, "byte_end": end, "quote": part}));
        start = end;
    }
    let version: String = db.query_row(
        "SELECT native_version FROM ing_event_versions WHERE event_id=?1",
        [original["event_id"].as_str()],
        |row| row.get(0),
    )?;
    Ok(
        json!({"handle":handle,"source_version":version,"splitter":splitter,
        "parts":spans,"partial":start<text.len()}),
    )
}

pub fn view_graph(db: &Connection, reader: Option<&BoundClient>, body: &Value) -> Result<Value> {
    let graph = field(body, "graph")?;
    let start = field(body, "start")?;
    let mut edges = db.prepare("SELECT edge_id,source,target,handle FROM community_graph_edges WHERE graph_id=?1 ORDER BY edge_id")?;
    let found = edges
        .query_map([graph], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut admitted = Vec::new();
    for (edge, from, to, handle) in found {
        let visible = match reader {
            Some(client) => retrieval::client_can_expand(db, &handle, &client.id, None)?,
            None => match retrieval::expand(db, &handle) {
                Ok(_) => true,
                Err(error) if error.status == 404 => false,
                Err(error) => return Err(error),
            },
        };
        if visible {
            admitted.push((edge, from, to));
        }
    }
    let mut reachable = std::collections::BTreeSet::from([start]);
    let mut used = std::collections::BTreeSet::new();
    loop {
        let mut changed = false;
        for (edge, from, to) in &admitted {
            if reachable.contains(from.as_str()) {
                used.insert(edge);
                changed |= reachable.insert(to.as_str());
            }
        }
        if !changed {
            break;
        }
    }
    reachable.remove(start);
    Ok(json!({"reachable":reachable,"expanded_refs":used}))
}

fn field<'a>(body: &'a Value, key: &str) -> Result<&'a str> {
    body[key]
        .as_str()
        .filter(|value| !value.trim().is_empty() && value.len() <= 4096)
        .ok_or_else(|| Error::invalid(format!("{key} must be a nonempty string")))
}

fn note(db: &Connection, id: &str) -> Result<Value> {
    db.query_row("SELECT r.revision,r.context FROM community_note_heads h
        JOIN community_note_revisions r ON r.note_id=h.note_id AND r.revision=h.revision
        JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1
        WHERE h.note_id=?1 AND r.context IS NOT NULL", [id], |row| {
        Ok(json!({"id":id,"revision":row.get::<_,String>(0)?,"context":row.get::<_,String>(1)?}))
    }).optional()?.ok_or_else(|| Error::missing("active note is unavailable"))
}

pub fn invoke(db: &mut Connection, action: &str, body: &Value) -> Result<Value> {
    match action {
        "ranked-replay" => retrieval::replay_ranked_channels(body),
        "replace-graph" => {
            let graph = field(body, "graph")?;
            let edges = body["edges"]
                .as_array()
                .filter(|items| items.len() <= 256)
                .ok_or_else(|| Error::invalid("edges must be a bounded array"))?;
            let tx = db.transaction()?;
            tx.execute(
                "DELETE FROM community_graph_edges WHERE graph_id=?1",
                [graph],
            )?;
            for edge in edges {
                let handle = field(edge, "evidence_handle")?;
                retrieval::expand(&tx, handle)?;
                tx.execute(
                    "INSERT INTO community_graph_edges VALUES (?1,?2,?3,?4,?5)",
                    params![
                        graph,
                        field(edge, "id")?,
                        field(edge, "from")?,
                        field(edge, "to")?,
                        handle
                    ],
                )?;
            }
            tx.commit()?;
            Ok(json!({"graph":graph,"edges_accepted":edges.len(),"contract_version":1}))
        }
        "capture-bridge" => {
            let source = field(body, "source_id")?;
            let previous = field(body, "opaque")?;
            let boundary = field(body, "native")?;
            if body["candidate_contract"] != "native-page-v2"
                || !boundary.starts_with("page-")
                || previous.starts_with("page-")
            {
                return Err(Error::invalid(
                    "capture bridge requires a v2 native page boundary",
                ));
            }
            let current: String = db
                .query_row(
                    "SELECT cursor FROM ing_sources WHERE source_id=?1",
                    [source],
                    |row| row.get(0),
                )
                .optional()?
                .ok_or_else(|| Error::missing("capture source unavailable"))?;
            if previous != current {
                return Err(Error::conflict("capture cursor changed"));
            }
            let overlap = body["overlap"]
                .as_array()
                .filter(|rows| !rows.is_empty() && rows.len() <= 100)
                .ok_or_else(|| {
                    Error::invalid("capture bridge requires existing native versions")
                })?;
            for row in overlap {
                let old: bool = db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM ing_event_versions
                    WHERE source_id=?1 AND native_object=?2 AND native_version=?3)",
                    params![
                        source,
                        field(row, "native_object")?,
                        field(row, "native_version")?
                    ],
                    |r| r.get(0),
                )?;
                if !old {
                    return Err(Error::conflict(
                        "capture replay overlap is not in the authority store",
                    ));
                }
            }
            Ok(
                json!({"adapter":"native-page-v2","legacy_cursor_accepted":false,
                "expected_cursor":current,"replay_through":boundary}),
            )
        }
        "root-replay" => {
            let roots = body["roots"]
                .as_array()
                .filter(|items| items.len() <= 10_000)
                .ok_or_else(|| Error::invalid("roots must be a bounded array"))?;
            let roots = roots
                .iter()
                .map(|item| {
                    item.as_str()
                        .filter(|root| !root.is_empty())
                        .ok_or_else(|| Error::invalid("invalid root reference"))
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(
                json!({"roots":first_per_root(roots.into_iter()),"implementation":"local-first-per-root-v1"}),
            )
        }
        "note" => {
            let id = field(body, "id")?;
            let current = note(db, id)?;
            let mut statement = db.prepare(
                "SELECT r.revision FROM community_note_revisions r
                JOIN ing_event_versions e ON e.event_id=r.event_id AND e.active=1
                WHERE r.note_id=?1 AND r.context IS NOT NULL ORDER BY r.rowid",
            )?;
            let revisions = statement
                .query_map([id], |row| row.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            Ok(json!({"note":current,"preserved_revision_ids":revisions}))
        }
        "neighbor-proposal" => {
            if body["reply"]["action"] != "update_neighbor" {
                return Err(Error::invalid("unsupported neighbor proposal action"));
            }
            let neighbors = body["retrieved"]
                .as_array()
                .filter(|items| !items.is_empty() && items.len() <= 64)
                .ok_or_else(|| Error::invalid("retrieved neighbors are required"))?;
            let target = field(&neighbors[0], "id")?;
            let expected = field(&neighbors[0], "revision")?;
            if note(db, target)?["revision"] != expected {
                return Err(Error::conflict("neighbor revision changed"));
            }
            Ok(
                json!({"target_id":target,"expected_revision":expected,"context":field(&body["reply"],"context")?,
                "adapter":"stable-neighbor-id","contract_version":1}),
            )
        }
        "apply-note" => {
            let id = field(body, "id")?;
            let revision = field(body, "revision")?;
            let content = field(body, "context")?;
            let expected = body["expected_revision"].as_str();
            let tx = db.transaction()?;
            let current: Option<String> = tx
                .query_row(
                    "SELECT revision FROM community_note_heads WHERE note_id=?1",
                    [id],
                    |row| row.get(0),
                )
                .optional()?;
            if current.as_deref() != expected {
                return Err(Error::conflict("neighbor revision changed"));
            }
            let evidence = retrieval::expand(&tx, field(body, "evidence_handle")?)?;
            let event = field(&evidence, "event_id")?;
            if expected.is_some() {
                note(&tx, id)?;
            }
            if tx.execute(
                "INSERT OR IGNORE INTO community_note_revisions VALUES (?1,?2,?3,?4,?5)",
                params![
                    id,
                    revision,
                    expected,
                    event,
                    crate::authority::redact(content)
                ],
            )? != 1
            {
                return Err(Error::conflict("note revision already exists"));
            }
            tx.execute("INSERT INTO community_note_heads VALUES (?1,?2) ON CONFLICT(note_id) DO UPDATE SET revision=excluded.revision",
                params![id,revision])?;
            tx.commit()?;
            note(db, id)
        }
        _ => Err(Error::missing("community operation is unavailable")),
    }
}

/// Keep the first appearance of an independently resolved root. The caller
/// decides whether two source versions belong to the same root before fusion.
pub fn first_per_root<'a>(roots: impl Iterator<Item = &'a str>) -> Vec<&'a str> {
    let mut seen = HashSet::new();
    roots.filter(|root| seen.insert(*root)).collect()
}
