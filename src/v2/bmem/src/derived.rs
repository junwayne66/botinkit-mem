use crate::error::{Error, Result};
use crate::{authority, context, retrieval};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use uuid::Uuid;

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS derived_revisions(
            id TEXT PRIMARY KEY, family TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES context_tasks(id),
            input_groups TEXT NOT NULL, text_inputs TEXT NOT NULL, text TEXT NOT NULL, digest TEXT NOT NULL
         );
         CREATE INDEX IF NOT EXISTS derived_task ON derived_revisions(task_id,family);",
    )?;
    Ok(())
}

type Groups = Vec<Vec<String>>;

struct Revision {
    family: String,
    groups: Groups,
    wording: Vec<String>,
    text: String,
}

fn revision(db: &Connection, task: &str, id: &str) -> Result<Option<Revision>> {
    let row: Option<(String, String, String, String)> = db
        .query_row(
            "SELECT r.family,r.input_groups,r.text_inputs,r.text FROM derived_revisions r
         WHERE r.task_id=?1 AND r.id=?2 AND NOT EXISTS(SELECT 1 FROM derived_revisions n
              WHERE n.task_id=r.task_id AND n.family=r.family AND n.rowid>r.rowid)",
            params![task, id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    row.map(|(family, groups, wording, text)| {
        Ok(Revision {
            family,
            groups: serde_json::from_str(&groups)?,
            wording: serde_json::from_str(&wording)?,
            text,
        })
    })
    .transpose()
}

struct View {
    text: String,
    evidence: BTreeSet<String>,
}

struct Reader<'a> {
    db: &'a Connection,
    task: &'a str,
    client: Option<&'a context::BoundClient>,
}

impl Reader<'_> {
    fn input(&self, id: &str, depth: usize) -> Result<Option<View>> {
        if id.starts_with("event_") {
            let result = if let Some(reader) = self.client {
                retrieval::expand_for_client(self.db, id, &reader.id)
            } else {
                retrieval::expand_for_owner(self.db, id)
            };
            let evidence = match result {
                Ok(value) if value["task_id"] == self.task => value,
                Ok(_) => return Ok(None),
                Err(error) if error.status == 404 => return Ok(None),
                Err(error) => return Err(error),
            };
            return Ok(Some(View {
                text: evidence["text"].as_str().unwrap_or_default().to_owned(),
                evidence: BTreeSet::from([id.to_owned()]),
            }));
        }
        if depth >= 32 {
            return Ok(None);
        }
        let Some(current) = revision(self.db, self.task, id)? else {
            return Ok(None);
        };
        self.display(&current, depth + 1)
    }

    fn complete(&self, group: &[String], depth: usize) -> Result<Option<View>> {
        let mut contents = Vec::new();
        let mut evidence = BTreeSet::new();
        for id in group {
            let Some(input) = self.input(id, depth)? else {
                return Ok(None);
            };
            contents.push(input.text);
            evidence.extend(input.evidence);
        }
        Ok(Some(View {
            text: contents.join("\n"),
            evidence,
        }))
    }

    fn alternative(&self, groups: &Groups, depth: usize) -> Result<Option<(Vec<String>, View)>> {
        for group in groups {
            if let Some(found) = self.complete(group, depth)? {
                return Ok(Some((group.clone(), found)));
            }
        }
        Ok(None)
    }

    fn display(&self, current: &Revision, depth: usize) -> Result<Option<View>> {
        let Some((_, support)) = self.alternative(&current.groups, depth)? else {
            return Ok(None);
        };
        let Some(wording) = self.complete(&current.wording, depth)? else {
            return Ok(None);
        };
        Ok(Some(View {
            text: current.text.clone(),
            evidence: support.evidence.union(&wording.evidence).cloned().collect(),
        }))
    }
}

fn task(body: &Value) -> Result<&str> {
    body["task_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .ok_or_else(|| Error::invalid("provide task_id"))
}

fn heads(db: &Connection, task: &str) -> Result<Vec<String>> {
    let mut q=db.prepare("SELECT r.id FROM derived_revisions r WHERE r.task_id=?1 AND NOT EXISTS(
        SELECT 1 FROM derived_revisions n WHERE n.task_id=r.task_id AND n.family=r.family AND n.rowid>r.rowid)
        ORDER BY r.rowid")?;
    Ok(q.query_map([task], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?)
}

pub fn visible(
    db: &Connection,
    client: Option<&context::BoundClient>,
    task: &str,
) -> Result<Vec<Value>> {
    let reader = Reader { db, task, client };
    let mut output = Vec::new();
    for id in heads(db, task)? {
        if let Some(content) = reader.input(&id, 0)? {
            output.push(json!({"revision_id":id,"text":content.text,"evidence":content.evidence}));
        }
    }
    Ok(output)
}

pub fn read(
    db: &Connection,
    client: Option<&context::BoundClient>,
    action: &str,
    body: &Value,
) -> Result<Value> {
    let task = task(body)?;
    if let Some(reader) = client
        && !context::task_access(db, task, &reader.id)?
    {
        return Err(Error::missing("task unavailable"));
    }
    if action == "list" {
        return Ok(json!({"derived":visible(db,client,task)?}));
    }
    let id = body["revision_id"]
        .as_str()
        .filter(|id| id.starts_with("derived_"))
        .ok_or_else(|| Error::invalid("provide revision_id"))?;
    let content = Reader { db, task, client }
        .input(id, 0)?
        .ok_or_else(|| Error::missing("derived revision unavailable"))?;
    Ok(json!({"revision_id":id,"text":content.text,"evidence":content.evidence}))
}

pub fn write(conn: &mut Connection, action: &str, body: &Value, key: &str) -> Result<Value> {
    let task = task(body)?;
    let db = conn.transaction()?;
    let reader = Reader {
        db: &db,
        task,
        client: None,
    };
    let result = match action {
        "put" => {
            let id = format!(
                "derived_{:x}",
                Sha256::digest(format!("{task}\0{key}").as_bytes())
            );
            let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(body)?));
            let prior: Option<String> = db
                .query_row(
                    "SELECT digest FROM derived_revisions WHERE id=?1",
                    [&id],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(old) = prior {
                if old != digest {
                    return Err(Error::conflict("key_conflict: derivation key already used"));
                }
                return Ok(json!({"revision_id":id}));
            }
            let text = body["text"]
                .as_str()
                .filter(|v| !v.is_empty() && v.len() <= 16_384)
                .ok_or_else(|| Error::invalid("provide bounded derived text"))?;
            let groups: Groups = serde_json::from_value(body["support_groups"].clone())?;
            if groups.is_empty()
                || groups.len() > 16
                || groups
                    .iter()
                    .any(|group| group.is_empty() || group.len() > 16)
            {
                return Err(Error::invalid("provide non-empty bounded support groups"));
            }
            // Initial prose may combine any support group. Until rebuilt, every
            // possible wording dependency must remain readable.
            let wording = groups
                .iter()
                .flatten()
                .cloned()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>();
            if reader.complete(&wording, 0)?.is_none() {
                return Err(Error::missing("derived input unavailable"));
            }
            db.execute(
                "INSERT INTO derived_revisions VALUES (?1,?1,?2,?3,?4,?5,?6)",
                params![
                    id,
                    task,
                    serde_json::to_string(&groups)?,
                    serde_json::to_string(&wording)?,
                    authority::redact(text),
                    digest
                ],
            )?;
            json!({"revision_id":id})
        }
        "rebuild" => {
            let mut rebuilt = Vec::new();
            for id in heads(&db, task)? {
                let Some(current) = revision(&db, task, &id)? else {
                    continue;
                };
                if reader.display(&current, 0)?.is_some() {
                    continue;
                }
                let Some((group, extracted)) = reader.alternative(&current.groups, 0)? else {
                    continue;
                };
                let revision = format!("derived_{}", Uuid::new_v4().simple());
                db.execute(
                    "INSERT INTO derived_revisions VALUES (?1,?2,?3,?4,?5,?6,'')",
                    params![
                        revision,
                        current.family,
                        task,
                        serde_json::to_string(&current.groups)?,
                        serde_json::to_string(&group)?,
                        extracted.text
                    ],
                )?;
                rebuilt.push(revision);
            }
            json!({"rebuilt_revision_ids":rebuilt})
        }
        _ => return Err(Error::missing("derived action unavailable")),
    };
    db.commit()?;
    Ok(result)
}
