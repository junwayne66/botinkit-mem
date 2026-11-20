use crate::error::{Error, Result};
use chrono::{DateTime, SecondsFormat, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use uuid::Uuid;

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch("
      CREATE TABLE IF NOT EXISTS authority_changes (id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL);
      INSERT OR IGNORE INTO authority_changes VALUES(1,0);
      CREATE TABLE IF NOT EXISTS authority_statements(
        id TEXT PRIMARY KEY, original TEXT, owner TEXT NOT NULL, project TEXT NOT NULL, share TEXT NOT NULL,
        effective TEXT NOT NULL, known TEXT NOT NULL, corrects_revision TEXT, corrects_attempt TEXT, revoked INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS authority_facts(
        id TEXT PRIMARY KEY, dimension TEXT NOT NULL, project TEXT NOT NULL, UNIQUE(dimension,project)
      );
      CREATE TABLE IF NOT EXISTS authority_revisions(
        id TEXT PRIMARY KEY, statement_id TEXT NOT NULL REFERENCES authority_statements(id), fact_id TEXT NOT NULL REFERENCES authority_facts(id),
        value TEXT, previous TEXT, seq INTEGER NOT NULL, state TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS authority_revisions_fact ON authority_revisions(fact_id,seq);
      CREATE TABLE IF NOT EXISTS authority_intents(
        actor TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, statement_id TEXT NOT NULL REFERENCES authority_statements(id),
        PRIMARY KEY(actor,key)
      );
      CREATE TABLE IF NOT EXISTS authority_observations(
        event_id TEXT NOT NULL, span_id TEXT NOT NULL, task_id TEXT,
        role TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY(event_id,span_id)
      );
      CREATE INDEX IF NOT EXISTS authority_observations_task ON authority_observations(task_id,event_id);")?;
    Ok(())
}

fn time(value: &str) -> Result<String> {
    let time = DateTime::parse_from_rfc3339(value)
        .map_err(|_| Error::invalid("time must be RFC 3339 with a time zone"))?;
    Ok(time
        .with_timezone(&Utc)
        .to_rfc3339_opts(SecondsFormat::Nanos, true))
}

fn now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Nanos, true)
}

fn id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4().simple())
}

pub fn redact(original: &str) -> String {
    original
        .lines()
        .map(|line| {
            let lower = line.to_ascii_lowercase();
            if [
                "authorization:",
                "api_key=",
                "api-key:",
                "api_key:",
                "api key:",
                "password=",
                "password:",
                "secret_access_key",
                "-----begin",
                "bearer ",
                "token=",
            ]
            .iter()
            .any(|pattern| lower.contains(pattern))
            {
                return "[redacted credential line]".to_owned();
            }
            let mut start = 0;
            let mut output = String::new();
            for (offset, token) in
                line.split_inclusive(char::is_whitespace)
                    .scan(0_usize, |offset, token| {
                        let old = *offset;
                        *offset += token.len();
                        Some((old, token))
                    })
            {
                let value = token
                    .trim_matches(|c: char| !c.is_ascii_alphanumeric() && c != '_' && c != '-');
                if value.len() >= 16
                    && ["sk-", "ghp_", "github_pat_", "xoxb-", "AIza", "AKIA"]
                        .iter()
                        .any(|prefix| value.starts_with(prefix))
                {
                    output.push_str(&line[start..offset]);
                    output.push_str("[redacted] ");
                    start = offset + token.len();
                }
            }
            output.push_str(&line[start..]);
            output
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn dimensions(original: &str) -> Vec<(&'static str, Option<&'static str>)> {
    let text = original.to_ascii_lowercase();
    if !text.contains("报告") && !text.contains("report") {
        return vec![];
    }
    let names = [
        (
            "report.language",
            vec![
                ("zh", vec!["用中文", "使用中文", "in chinese"]),
                ("en", vec!["用英文", "使用英文", "in english"]),
            ],
        ),
        (
            "report.opening",
            vec![
                (
                    "actions",
                    vec!["先列行动项", "先写行动项", "action items first"],
                ),
                (
                    "conclusion",
                    vec![
                        "先写结论",
                        "先给结论",
                        "结论先行",
                        "lead with the conclusion",
                        "conclusion first",
                    ],
                ),
            ],
        ),
    ];
    let ambiguous = [
        "如果",
        "除非",
        "暂且",
        "不要",
        "不得",
        "不再",
        "不用",
        "假设",
        "不要引用",
        " not ",
        " unless ",
        " if ",
    ]
    .iter()
    .any(|marker| text.contains(marker));
    names
        .into_iter()
        .filter_map(|(key, values)| {
            let matches = values
                .into_iter()
                .filter(|(_, terms)| terms.iter().any(|t| text.contains(t)))
                .map(|(val, _)| val)
                .collect::<Vec<_>>();
            if matches.is_empty() {
                None
            } else {
                Some((
                    key,
                    (!ambiguous && matches.len() == 1).then_some(matches[0]),
                ))
            }
        })
        .collect()
}

pub fn sequence(db: &Connection) -> Result<i64> {
    Ok(
        db.query_row("SELECT seq FROM authority_changes WHERE id=1", [], |r| {
            r.get(0)
        })?,
    )
}

pub fn append(db: &mut Connection, actor: &str, key: &str, request: Value) -> Result<Value> {
    let original = request["original"]
        .as_str()
        .filter(|v| !v.trim().is_empty() && v.len() <= 65536)
        .ok_or_else(|| Error::invalid("statement needs original text up to 64 KiB"))?;
    let scope = &request["scope"];
    let project = match scope["kind"].as_str() {
        Some("global") if scope["project"].is_null() => "",
        Some("project") => scope["project"]
            .as_str()
            .filter(|s| !s.trim().is_empty() && s.len() <= 200)
            .ok_or_else(|| Error::invalid("project scope requires a project"))?,
        _ => return Err(Error::invalid("scope must be global or project")),
    };
    let share = request["share"].as_str().unwrap_or("owner");
    if !["owner", "authorized_assistants"].contains(&share) {
        return Err(Error::invalid("invalid statement sharing policy"));
    }
    let known = now();
    let effective = match request
        .get("valid_from")
        .or_else(|| request.get("effective_at"))
    {
        Some(date) => time(
            date.as_str()
                .ok_or_else(|| Error::invalid("valid_from must be a date string"))?,
        )?,
        None => known.clone(),
    };
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request)?));
    let clean = redact(original);
    let tx = db.transaction()?;
    let replay: Option<(String,String,bool)> = tx.query_row(
        "SELECT i.digest,i.result,s.revoked FROM authority_intents i JOIN authority_statements s ON s.id=i.statement_id WHERE actor=?1 AND key=?2",
        params![actor,key],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    if let Some((prior, result, revoked)) = replay {
        if prior != digest {
            return Err(Error::new(
                409,
                "key_conflict",
                "statement key already used with different content",
            ));
        }
        if revoked {
            return Err(Error::missing("statement is unavailable"));
        }
        return Ok(serde_json::from_str(&result)?);
    }
    let corrects = request["corrects_revision_id"].as_str();
    let corrects_attempt = request["corrects_attempt_id"].as_str();
    if let Some(corrects) = corrects
        && !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM authority_revisions WHERE id=?1)",
            [corrects],
            |r| r.get::<_, bool>(0),
        )?
    {
        return Err(Error::missing("revision is unavailable"));
    }
    if let Some(attempt) = corrects_attempt
        && !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM context_attempts WHERE id=?1)",
            [attempt],
            |r| r.get::<_, bool>(0),
        )?
    {
        return Err(Error::missing("attempt is unavailable"));
    }
    let statement = id("st");
    tx.execute(
        "INSERT INTO authority_statements(id,original,owner,project,share,effective,known,corrects_revision,corrects_attempt) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![statement,clean,actor,project,share,effective,known,corrects,corrects_attempt],
    )?;
    let seq = sequence(&tx)? + 1;
    tx.execute("UPDATE authority_changes SET seq=?1 WHERE id=1", [seq])?;
    let mut revisions = Vec::new();
    for (dimension, value) in dimensions(&clean) {
        let fact = tx
            .query_row(
                "SELECT id FROM authority_facts WHERE dimension=?1 AND project=?2",
                params![dimension, project],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        let fact = match fact {
            Some(id) => id,
            None => {
                let fact = id("f");
                tx.execute(
                    "INSERT INTO authority_facts VALUES(?1,?2,?3)",
                    params![fact, dimension, project],
                )?;
                fact
            }
        };
        let prev = tx.query_row("SELECT r.id FROM authority_revisions r JOIN authority_statements s ON s.id=r.statement_id WHERE fact_id=?1 AND s.effective<=?2 ORDER BY s.effective DESC, r.seq DESC LIMIT 1",
            params![fact,effective],|r|r.get::<_,String>(0)).optional()?;
        let state = if value.is_none() {
            "pending"
        } else if effective > known {
            "scheduled"
        } else {
            "active"
        };
        let revision = id("r");
        tx.execute("INSERT INTO authority_revisions(id,statement_id,fact_id,value,previous,seq,state) VALUES(?1,?2,?3,?4,?5,?6,?7)",
            params![revision,statement,fact,value,prev,seq,state])?;
        revisions.push(json!({"fact_id":fact,"revision_id":revision,"key":dimension,"status":state,"value":value,"supersedes":prev}));
    }
    let result = json!({"statement_id":statement,"dimensions":revisions,"status":if revisions.is_empty() {"pending"} else {"recorded"},"change_seq":seq});
    tx.execute(
        "INSERT INTO authority_intents VALUES(?1,?2,?3,?4,?5)",
        params![actor, key, digest, result.to_string(), statement],
    )?;
    tx.commit()?;
    Ok(result)
}

fn scope(project: &str) -> Value {
    if project.is_empty() {
        json!({"kind":"global"})
    } else {
        json!({"kind":"project","project":project})
    }
}

pub fn current(
    db: &Connection,
    project: Option<&str>,
    valid_at: Option<&str>,
    known_at: Option<&str>,
    shared_only: bool,
) -> Result<Value> {
    let valid = valid_at.map(time).transpose()?.unwrap_or_else(now);
    let known = known_at.map(time).transpose()?.unwrap_or_else(now);
    let mut query=db.prepare("
      SELECT r.id,f.id,f.dimension,f.project,r.value,s.share,s.revoked,s.id,s.effective,s.known,s.original
      FROM authority_facts f JOIN authority_revisions r ON r.fact_id=f.id JOIN authority_statements s ON s.id=r.statement_id
      WHERE (f.project='' OR f.project=?1) AND s.effective<=?2 AND s.known<=?3
      ORDER BY f.project DESC,s.effective DESC,r.seq DESC")?;
    let rows = query.query_map(params![project.unwrap_or(""), valid, known], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
            r.get::<_, Option<String>>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, bool>(6)?,
            r.get::<_, String>(7)?,
            r.get::<_, String>(8)?,
            r.get::<_, String>(9)?,
            r.get::<_, Option<String>>(10)?,
        ))
    })?;
    let mut items = BTreeMap::new();
    for row in rows {
        let (
            revision,
            fact,
            key,
            project,
            value,
            share,
            revoked,
            statement,
            effective,
            known,
            original,
        ) = row?;
        items.entry(key.clone()).or_insert_with(|| {
            if revoked || value.is_none() || (shared_only && share!="authorized_assistants") { return None; }
            let mut item=json!({"revision_id":revision,"fact_id":fact,"key":key,"value":value,"scope":scope(&project),"status":"active","valid_from":effective,"known_from":known});
            if !shared_only { item["statement_id"]=json!(statement); item["evidence"]=json!(original); item["share"]=json!(share); }
            Some(item)
        });
    }
    Ok(
        json!({"statements":items.into_values().flatten().collect::<Vec<_>>(),"change_seq":sequence(db)?}),
    )
}

pub fn history(db: &Connection, fact_id: &str) -> Result<Value> {
    let (key, project) = db
        .query_row(
            "SELECT dimension,project FROM authority_facts WHERE id=?1",
            [fact_id],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("fact is unavailable"))?;
    let mut query = db.prepare(
        "SELECT r.id,r.value,r.previous,s.known,s.effective,s.original,s.id,r.state
      FROM authority_revisions r JOIN authority_statements s ON s.id=r.statement_id
      WHERE r.fact_id=?1 AND s.revoked=0 ORDER BY r.seq DESC",
    )?;
    let results=query.query_map([fact_id],|r| Ok(json!({
        "revision_id":r.get::<_,String>(0)?, "value":r.get::<_,Option<String>>(1)?,
        "supersedes":r.get::<_,Option<String>>(2)?, "known_from":r.get::<_,String>(3)?,
        "valid_from":r.get::<_,String>(4)?,"original":r.get::<_,Option<String>>(5)?,"statement_id":r.get::<_,String>(6)?,"status":r.get::<_,String>(7)?
    })))?.collect::<rusqlite::Result<Vec<_>>>()?;
    if results.is_empty() {
        return Err(Error::missing("fact history is unavailable"));
    }
    Ok(json!({"fact_id":fact_id,"key":key,"scope":scope(&project),"revisions":results}))
}

pub fn revoke_statement(db: &Connection, statement: &str) -> Result<Value> {
    let updated = db.execute(
        "UPDATE authority_statements SET revoked=1,original=NULL WHERE id=?1 AND revoked=0",
        [statement],
    )?;
    if updated == 0 {
        return Err(Error::missing("statement is unavailable"));
    }
    db.execute(
        "UPDATE authority_revisions SET value=NULL,state='revoked' WHERE statement_id=?1",
        [statement],
    )?;
    db.execute(
        "UPDATE authority_intents SET result='{}' WHERE statement_id=?1",
        [statement],
    )?;
    db.execute("UPDATE authority_changes SET seq=seq+1 WHERE id=1", [])?;
    let seq = sequence(db)?;
    Ok(json!({"statement_id":statement,"revoked":true,"change_seq":seq}))
}

pub fn adjudicate(db: &Connection, input: &Value, candidates: &[Value]) -> Result<usize> {
    let Some(event) = input["event_id"].as_str() else {
        return Err(Error::invalid("missing evidence event identity"));
    };
    let task = input["task_id"].as_str();
    let mut written = 0;
    for span in input["inputs"].as_array().into_iter().flatten() {
        let Some(span_id) = span["span_id"].as_str() else {
            continue;
        };
        let role = span["kind"].as_str().unwrap_or_default();
        let field = span["field"].as_str().unwrap_or_default();
        let explicit_plan = span["value"].as_str().is_some_and(|phrase| {
            let phrase = phrase.trim_start();
            [
                "准备",
                "计划",
                "打算",
                "我准备",
                "我计划",
                "我打算",
                "I plan to ",
                "I will ",
            ]
            .iter()
            .any(|prefix| phrase.starts_with(prefix))
        });
        let cited = candidates.iter().any(|candidate| {
            candidate["support"]
                .as_array()
                .is_some_and(|refs| refs.iter().any(|id| id == span_id))
                && (role == "tool_result" || (role == "user_message" && explicit_plan))
        });
        if !cited {
            continue;
        }
        let outcome = match role {
            "tool_result" if field.starts_with("/checks/") => "tool_result",
            "user_message" if field == "/text" => "plan",
            _ => continue,
        };
        written += db.execute("INSERT OR IGNORE INTO authority_observations(event_id,span_id,task_id,role,field,value)
          VALUES(?1,?2,?3,?4,?5,?6)",params![event,span_id,task,outcome,field,span["value"].to_string()])?;
    }
    Ok(written)
}
