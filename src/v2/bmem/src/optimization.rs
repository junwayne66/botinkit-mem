use crate::error::{Error, Result};
use crate::{authority, clock};
use chrono::DateTime;
use rusqlite::{Connection, OptionalExtension, params};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

const STABLE: &str = "stable";

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS optimization_permissions(
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id), purpose TEXT NOT NULL,
            PRIMARY KEY(root_id,purpose)
         );
         CREATE TABLE IF NOT EXISTS optimization_feedback(
            id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES context_deliveries(id),
            attempt_id TEXT NOT NULL REFERENCES context_attempts(id), note TEXT, created_at TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS optimization_cases(
            id TEXT PRIMARY KEY, attempt_id TEXT REFERENCES context_attempts(id),
            feedback_id TEXT REFERENCES optimization_feedback(id), query TEXT,
            required TEXT NOT NULL, forbidden TEXT NOT NULL, unanswerable INTEGER NOT NULL,
            family TEXT NOT NULL, observed_at TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS optimization_case_roots(
            case_id TEXT NOT NULL REFERENCES optimization_cases(id),
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id), PRIMARY KEY(case_id,root_id)
         );
         CREATE INDEX IF NOT EXISTS optimization_case_lookup ON optimization_cases(attempt_id);
         CREATE INDEX IF NOT EXISTS optimization_cases_by_root ON optimization_case_roots(root_id,case_id);
         CREATE TABLE IF NOT EXISTS optimization_datasets(id TEXT PRIMARY KEY, purpose TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS optimization_dataset_cases(
            dataset_id TEXT NOT NULL REFERENCES optimization_datasets(id),
            case_id TEXT NOT NULL REFERENCES optimization_cases(id), PRIMARY KEY(dataset_id,case_id)
         );
         CREATE TABLE IF NOT EXISTS optimization_dataset_revocations(
            dataset_id TEXT PRIMARY KEY REFERENCES optimization_datasets(id),
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id)
         );
         CREATE TABLE IF NOT EXISTS optimization_runs(
            id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES optimization_datasets(id),
            state TEXT NOT NULL, model_calls INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS optimization_run_calls(
            run_id TEXT NOT NULL REFERENCES optimization_runs(id),
            provider_call_ref TEXT NOT NULL, PRIMARY KEY(run_id,provider_call_ref)
         );
         CREATE TABLE IF NOT EXISTS optimization_policies(
            id TEXT PRIMARY KEY, parent TEXT REFERENCES optimization_policies(id),
            evidence_token_cap INTEGER
         );
         INSERT OR IGNORE INTO optimization_policies(id) VALUES ('stable');
         CREATE TABLE IF NOT EXISTS optimization_training_roots(
            policy_id TEXT NOT NULL REFERENCES optimization_policies(id),
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id), PRIMARY KEY(policy_id,root_id)
         );
         CREATE TABLE IF NOT EXISTS optimization_policy_revocations(
            policy_id TEXT PRIMARY KEY REFERENCES optimization_policies(id),
            root_id TEXT NOT NULL REFERENCES ing_roots(root_id)
         );
         CREATE TABLE IF NOT EXISTS optimization_serving(
            host TEXT NOT NULL, project TEXT NOT NULL,
            active TEXT NOT NULL REFERENCES optimization_policies(id),
            fallback TEXT NOT NULL REFERENCES optimization_policies(id),
            PRIMARY KEY(host,project)
         );
         CREATE TABLE IF NOT EXISTS optimization_trials(
            id TEXT PRIMARY KEY, host TEXT NOT NULL, project TEXT NOT NULL,
            baseline TEXT NOT NULL REFERENCES optimization_policies(id),
            candidate TEXT NOT NULL REFERENCES optimization_policies(id),
            dataset TEXT REFERENCES optimization_datasets(id), families TEXT NOT NULL,
            thresholds TEXT NOT NULL, authorized INTEGER NOT NULL,
            assessment TEXT
         );
         CREATE TABLE IF NOT EXISTS optimization_intents(
            action TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL,
            PRIMARY KEY(action,key)
         );
         CREATE TRIGGER IF NOT EXISTS optimization_purpose_withdrawn
         AFTER DELETE ON optimization_permissions BEGIN
            INSERT OR IGNORE INTO optimization_dataset_revocations
            SELECT d.id, OLD.root_id FROM optimization_datasets d
            JOIN optimization_dataset_cases m ON m.dataset_id=d.id
            JOIN optimization_case_roots r ON r.case_id=m.case_id
            WHERE r.root_id=OLD.root_id
              AND (OLD.purpose='evaluation' OR d.purpose=OLD.purpose);
            INSERT OR IGNORE INTO optimization_policy_revocations
            SELECT policy_id, OLD.root_id FROM optimization_training_roots
            WHERE root_id=OLD.root_id AND OLD.purpose='training';
         END;
         CREATE TRIGGER IF NOT EXISTS optimization_root_withdrawn
         AFTER INSERT ON ing_root_revocations BEGIN
            UPDATE optimization_cases SET query=NULL,required='[]',forbidden='[]' WHERE id IN (
                SELECT case_id FROM optimization_case_roots WHERE root_id=NEW.root_id);
            UPDATE optimization_feedback SET note=NULL WHERE id IN (
                SELECT c.feedback_id FROM optimization_cases c
                JOIN optimization_case_roots r ON r.case_id=c.id WHERE r.root_id=NEW.root_id
                UNION
                SELECT f.id FROM optimization_feedback f JOIN context_delivery_evidence d
                    ON d.delivery_id=f.delivery_id
                    JOIN json_each(d.handles) h JOIN recall_events r ON r.handle=h.value
                    JOIN ing_event_versions e ON e.event_id=r.event_id WHERE e.root_id=NEW.root_id
            );
            DELETE FROM optimization_permissions WHERE root_id=NEW.root_id;
         END;",
    )?;
    Ok(())
}

fn id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4().simple())
}

fn text<'a>(body: &'a Value, field: &str) -> Result<&'a str> {
    body[field]
        .as_str()
        .filter(|text| !text.trim().is_empty() && text.len() <= 4096 && !text.contains('\0'))
        .ok_or_else(|| Error::invalid(format!("{field} requires nonempty text up to 4 KiB")))
}

fn parse<T: DeserializeOwned>(body: &Value) -> Result<T> {
    Ok(serde_json::from_value(body.clone())?)
}

fn read<T: DeserializeOwned>(value: &str) -> Result<T> {
    serde_json::from_str(value).map_err(|error| Error::internal(error.to_string()))
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Purpose {
    Evaluation,
    Training,
}

impl Purpose {
    fn key(self) -> &'static str {
        match self {
            Self::Evaluation => "evaluation",
            Self::Training => "training",
        }
    }
}

fn eligible_root(db: &Connection, root: &str, purpose: Purpose) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM optimization_permissions p
          WHERE p.root_id=?1 AND p.purpose=?2
            AND NOT EXISTS(SELECT 1 FROM ing_root_revocations WHERE root_id=p.root_id)
            AND EXISTS(SELECT 1 FROM ing_event_versions e WHERE e.root_id=p.root_id AND e.active=1))",
        params![root, purpose.key()],
        |row| row.get(0),
    )?)
}

fn change_permission(db: &Connection, body: &Value) -> Result<Value> {
    let root = text(body, "root_id")?;
    let purpose: Purpose = parse(&body["purpose"])?;
    let allow = body["allow"]
        .as_bool()
        .ok_or_else(|| Error::invalid("allow is required"))?;
    if allow {
        let active: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM ing_event_versions WHERE root_id=?1 AND active=1)
              AND NOT EXISTS(SELECT 1 FROM ing_root_revocations WHERE root_id=?1)",
            [root],
            |row| row.get(0),
        )?;
        if !active {
            return Err(Error::missing("root unavailable"));
        }
        db.execute(
            "INSERT OR IGNORE INTO optimization_permissions VALUES (?1,?2)",
            params![root, purpose.key()],
        )?;
    } else {
        db.execute(
            "DELETE FROM optimization_permissions WHERE root_id=?1 AND purpose=?2",
            params![root, purpose.key()],
        )?;
    }
    Ok(json!({"root_id":root,"purpose":purpose.key(),"allowed":allow}))
}

fn root_for_handle(db: &Connection, handle: &str) -> Result<String> {
    db.query_row(
        "SELECT e.root_id FROM recall_events r JOIN ing_event_versions e ON e.event_id=r.event_id
         WHERE r.handle=?1 AND e.active=1",
        [handle],
        |row| row.get(0),
    )
    .optional()?
    .ok_or_else(|| Error::missing("evidence unavailable"))
}

#[derive(Deserialize)]
struct CaseInput {
    query: String,
    query_ref: String,
    #[serde(default)]
    required_refs: BTreeSet<String>,
    #[serde(default)]
    forbidden_refs: BTreeSet<String>,
    #[serde(default)]
    unanswerable: bool,
    family: String,
    observed_at: String,
}

fn add_case(
    db: &Connection,
    body: &Value,
    attempt: Option<&str>,
    feedback: Option<&str>,
) -> Result<Value> {
    let data: CaseInput = parse(body)?;
    if data.query.trim().is_empty()
        || data.query.len() > 4096
        || data.family.trim().is_empty()
        || data.family.len() > 512
        || !data.required_refs.is_disjoint(&data.forbidden_refs)
        || (data.unanswerable && !data.required_refs.is_empty())
    {
        return Err(Error::invalid("invalid evaluation query, family or labels"));
    }
    let observed = DateTime::parse_from_rfc3339(&data.observed_at)
        .map_err(|_| Error::invalid("observed_at must be RFC 3339"))?
        .with_timezone(&chrono::Utc)
        .to_rfc3339_opts(chrono::SecondsFormat::Nanos, true);
    let roots: BTreeSet<_> = data
        .required_refs
        .iter()
        .chain(&data.forbidden_refs)
        .chain(std::iter::once(&data.query_ref))
        .map(|handle| root_for_handle(db, handle))
        .collect::<Result<_>>()?;
    for root in &roots {
        if !eligible_root(db, root, Purpose::Evaluation)? {
            return Err(Error::denied("all inputs require evaluation permission"));
        }
    }
    let case = id("case");
    db.execute(
        "INSERT INTO optimization_cases VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![
            case,
            attempt,
            feedback,
            authority::redact(&data.query),
            serde_json::to_string(&data.required_refs)?,
            serde_json::to_string(&data.forbidden_refs)?,
            data.unanswerable,
            data.family,
            observed
        ],
    )?;
    for root in roots {
        db.execute(
            "INSERT INTO optimization_case_roots VALUES (?1,?2)",
            params![case, root],
        )?;
    }
    Ok(json!({"case_id":case}))
}

fn feedback(db: &Connection, body: Value, key: &str) -> Result<Value> {
    let delivery = text(&body, "delivery_id")?;
    let note = text(&body, "text")?;
    let (attempt, project) = db.query_row(
        "SELECT a.id,t.project FROM context_deliveries d JOIN context_attempts a ON a.id=d.attempt_id
         JOIN context_tasks t ON t.id=a.task_id WHERE d.id=?1",
        [delivery],
        |row| Ok((row.get::<_,String>(0)?,row.get::<_,Option<String>>(1)?)),
    ).optional()?.ok_or_else(|| Error::missing("attempt delivery unavailable"))?;
    let mut statement = Value::Null;
    if body["correct_declaration"] == true {
        let project = project
            .as_deref()
            .ok_or_else(|| Error::invalid("a correction requires a project"))?;
        let mut correction = json!({"original":note,"scope":{"kind":"project","project":project},
            "share":"authorized_assistants","corrects_attempt_id":attempt});
        if let Some(revision) = body["corrects_revision_id"].as_str() {
            correction["corrects_revision_id"] = json!(revision);
        }
        statement =
            authority::append_in_transaction(db, "owner", &format!("feedback:{key}"), correction)?;
    }
    let feedback = id("feedback");
    db.execute(
        "INSERT INTO optimization_feedback VALUES (?1,?2,?3,?4,?5)",
        params![
            feedback,
            delivery,
            attempt,
            authority::redact(note),
            clock::now()?
        ],
    )?;
    let evaluation = match body.get("case") {
        Some(case) => match add_case(db, case, Some(&attempt), Some(&feedback)) {
            Ok(response) => response,
            Err(error) if error.status == 403 || error.status == 404 => {
                json!({"status":"not_granted"})
            }
            Err(error) => return Err(error),
        },
        None => Value::Null,
    };
    Ok(
        json!({"feedback_id":feedback,"attempt_id":attempt,"statement_id":statement["statement_id"],
        "correction_status":statement["status"],"evaluation":evaluation}),
    )
}

fn case_roots(db: &Connection, case: &str) -> Result<Vec<String>> {
    let mut query = db
        .prepare("SELECT root_id FROM optimization_case_roots WHERE case_id=?1 ORDER BY root_id")?;
    Ok(query
        .query_map([case], |row| row.get(0))?
        .collect::<rusqlite::Result<_>>()?)
}

fn case_eligible(db: &Connection, case: &str, purpose: Purpose) -> Result<bool> {
    let roots = case_roots(db, case)?;
    if roots.is_empty() {
        return Ok(false);
    }
    for root in roots {
        if !eligible_root(db, &root, Purpose::Evaluation)?
            || (purpose == Purpose::Training && !eligible_root(db, &root, Purpose::Training)?)
        {
            return Ok(false);
        }
    }
    Ok(true)
}

fn case_view(db: &Connection, case: &str) -> Result<Value> {
    let mut detail = db.query_row(
        "SELECT c.attempt_id,t.anchor,c.query,c.required,c.forbidden,c.unanswerable,c.family,c.observed_at
         FROM optimization_cases c LEFT JOIN context_attempts a ON a.id=c.attempt_id
         LEFT JOIN context_tasks t ON t.id=a.task_id WHERE c.id=?1 AND c.query IS NOT NULL",
        [case],
        |row| Ok(json!({"id":case,"attempt":row.get::<_,Option<String>>(0)?,"task":row.get::<_,Option<String>>(1)?,
            "query":row.get::<_,String>(2)?,"required_refs":row.get::<_,String>(3)?,
            "forbidden_refs":row.get::<_,String>(4)?,"unanswerable":row.get::<_,bool>(5)?,
            "family":row.get::<_,String>(6)?,"observed_at":row.get::<_,String>(7)?})),
    ).optional()?.ok_or_else(|| Error::missing("evaluation case unavailable"))?;
    for field in ["required_refs", "forbidden_refs"] {
        detail[field] = read(detail[field].as_str().unwrap_or("[]"))?;
    }
    Ok(detail)
}

fn list_cases(db: &Connection, body: &Value) -> Result<Value> {
    let filter = body["attempt_id"].as_str();
    let mut query = db.prepare(
        "SELECT id FROM optimization_cases WHERE (?1 IS NULL OR attempt_id=?1) ORDER BY id",
    )?;
    let cases: Vec<String> = query
        .query_map([filter], |row| row.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    let mut visible = Vec::new();
    for case in cases {
        if case_eligible(db, &case, Purpose::Evaluation)? {
            visible.push(case_view(db, &case)?);
        }
    }
    Ok(json!({"case_count":visible.len(),"cases":visible}))
}

fn freeze(db: &Connection, body: &Value) -> Result<Value> {
    let purpose: Purpose = parse(&body["purpose"])?;
    let cases: BTreeSet<String> = parse(&body["case_ids"])?;
    if cases.is_empty() || cases.len() > 512 {
        return Err(Error::invalid("a dataset requires 1 to 512 cases"));
    }
    let id = id("dataset");
    db.execute(
        "INSERT INTO optimization_datasets VALUES (?1,?2)",
        params![id, purpose.key()],
    )?;
    let mut included = 0;
    for case in cases {
        if case_eligible(db, &case, purpose)? {
            db.execute(
                "INSERT INTO optimization_dataset_cases VALUES (?1,?2)",
                params![id, case],
            )?;
            included += 1;
        }
    }
    Ok(json!({"dataset_id":id,"included_count":included}))
}

fn dataset(db: &Connection, id: &str) -> Result<Value> {
    let purpose = db
        .query_row(
            "SELECT purpose FROM optimization_datasets WHERE id=?1",
            [id],
            |row| row.get::<_, String>(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("dataset unavailable"))?;
    let purpose = match purpose.as_str() {
        "evaluation" => Purpose::Evaluation,
        "training" => Purpose::Training,
        _ => return Err(Error::internal("invalid stored dataset purpose")),
    };
    if db.query_row(
        "SELECT EXISTS(SELECT 1 FROM optimization_dataset_revocations WHERE dataset_id=?1)",
        [id],
        |row| row.get::<_, bool>(0),
    )? {
        return Ok(
            json!({"usable":false,"purpose":purpose.key(),"members":[],"reason":"permission_changed"}),
        );
    }
    let mut query = db.prepare(
        "SELECT case_id FROM optimization_dataset_cases WHERE dataset_id=?1 ORDER BY case_id",
    )?;
    let ids = query
        .query_map([id], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for case in &ids {
        if !case_eligible(db, case, purpose)? {
            return Ok(
                json!({"usable":false,"purpose":purpose.key(),"members":[],"reason":"permission_changed"}),
            );
        }
    }
    let members = ids
        .iter()
        .map(|case| case_view(db, case))
        .collect::<Result<Vec<_>>>()?;
    Ok(json!({"usable":!members.is_empty(),"purpose":purpose.key(),"members":members}))
}

fn schedule(db: &Connection, dataset_id: &str) -> Result<Value> {
    let state = dataset(db, dataset_id)?;
    if state["purpose"] != "evaluation" {
        return Err(Error::invalid(
            "only an evaluation dataset can be scheduled",
        ));
    }
    let id = id("eval");
    let status = if state["usable"] == true {
        "scheduled"
    } else {
        "blocked"
    };
    db.execute(
        "INSERT INTO optimization_runs (id,dataset_id,state,created_at) VALUES (?1,?2,?3,?4)",
        params![id, dataset_id, status, clock::now()?],
    )?;
    Ok(json!({"run_id":id,"state":status}))
}

fn run_status(db: &Connection, run_id: &str) -> Result<Value> {
    let (dataset_id, planned, calls): (String, String, i64) = db
        .query_row(
            "SELECT dataset_id,state,model_calls FROM optimization_runs WHERE id=?1",
            [run_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("evaluation run unavailable"))?;
    let usable = planned == "scheduled" && dataset(db, &dataset_id)?["usable"] == true;
    Ok(json!({"run_id":run_id,"usable":usable,"model_calls":calls,
        "state":if usable {"scheduled"} else {"blocked"}}))
}

fn checkout_run(db: &Connection, body: &Value) -> Result<Value> {
    let run = text(body, "run_id")?;
    if run_status(db, run)?["usable"] != true {
        return Err(Error::denied("evaluation run is unavailable"));
    }
    let case = text(body, "case_id")?;
    if !db.query_row(
        "SELECT EXISTS(SELECT 1 FROM optimization_runs r JOIN optimization_dataset_cases c
         ON c.dataset_id=r.dataset_id WHERE r.id=?1 AND c.case_id=?2)",
        params![run, case],
        |row| row.get::<_, bool>(0),
    )? {
        return Err(Error::missing("case is not a member of this frozen run"));
    }
    let case = case_view(db, case)?;
    let required: Vec<String> = parse(&case["required_refs"])?;
    let budget = body["evidence_budget"]
        .as_u64()
        .filter(|tokens| *tokens <= 262_144)
        .ok_or_else(|| Error::invalid("evidence_budget is required"))?;
    let tokenizer = text(body, "tokenizer")?;
    if !matches!(tokenizer, "utf8_bytes" | "whitespace") {
        return Err(Error::invalid("invalid evaluation tokenizer"));
    }
    let mut evidence = Vec::new();
    let mut used = 0_u64;
    for handle in required {
        let item = crate::retrieval::expand(db, &handle)?;
        let content = text(&item, "text")?;
        let count = if tokenizer == "whitespace" {
            content.split_whitespace().count()
        } else {
            content.len()
        };
        used += count as u64;
        if used > budget {
            return Err(Error::denied(
                "registered evaluation inputs exceed the evidence budget",
            ));
        }
        evidence.push(json!({"handle":handle,"text":content}));
    }
    Ok(
        json!({"query":case["query"],"evidence":evidence,"evidence_tokens":used,"tokenizer":tokenizer}),
    )
}

fn observe_call(db: &Connection, body: &Value) -> Result<Value> {
    let run = text(body, "run_id")?;
    let call = text(body, "provider_call_ref")?;
    if run_status(db, run)?["usable"] != true {
        return Err(Error::denied("evaluation run is unavailable"));
    }
    if db.execute(
        "INSERT OR IGNORE INTO optimization_run_calls VALUES (?1,?2)",
        params![run, call],
    )? == 1
    {
        db.execute(
            "UPDATE optimization_runs SET model_calls=model_calls+1 WHERE id=?1",
            [run],
        )?;
    }
    run_status(db, run)
}

#[derive(Serialize)]
pub struct ServingPolicy {
    pub version: String,
    pub evidence_token_cap: Option<usize>,
}

fn policy_cap(db: &Connection, version: &str) -> Result<Option<Option<usize>>> {
    db.query_row(
        "SELECT evidence_token_cap FROM optimization_policies WHERE id=?1",
        [version],
        |row| {
            row.get::<_, Option<u32>>(0)
                .map(|cap| cap.map(|n| n as usize))
        },
    )
    .optional()
    .map_err(Into::into)
}

fn policy_eligible(db: &Connection, version: &str) -> Result<bool> {
    if policy_cap(db, version)?.is_none() {
        return Ok(false);
    }
    let mut query = db.prepare(
        "WITH RECURSIVE lineage(id) AS (
           VALUES (?1) UNION SELECT p.parent FROM optimization_policies p
           JOIN lineage ON lineage.id=p.id WHERE p.parent IS NOT NULL
         )
         SELECT DISTINCT r.root_id, EXISTS(
            SELECT 1 FROM optimization_policy_revocations p WHERE p.policy_id=r.policy_id
         ) FROM optimization_training_roots r JOIN lineage ON r.policy_id=lineage.id",
    )?;
    let roots = query
        .query_map([version], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, bool>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (root, revoked) in roots {
        if revoked || !eligible_root(db, &root, Purpose::Training)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn route_policy(db: &Connection, host: &str, project: &str) -> Result<ServingPolicy> {
    let ids: Option<(String, String)> = db
        .query_row(
            "SELECT active,fallback FROM optimization_serving WHERE host=?1 AND project=?2",
            params![host, project],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    if let Some((active, fallback)) = ids {
        for id in [active, fallback] {
            if policy_eligible(db, &id)? {
                return Ok(ServingPolicy {
                    evidence_token_cap: policy_cap(db, &id)?.flatten(),
                    version: id,
                });
            }
        }
    }
    Ok(ServingPolicy {
        version: STABLE.into(),
        evidence_token_cap: None,
    })
}

pub fn serving(db: &Connection, client: &str, project: Option<&str>) -> Result<ServingPolicy> {
    let Some(project) = project else {
        return route_policy(db, "", "");
    };
    let host: String = db.query_row(
        "SELECT host FROM context_clients WHERE id=?1",
        [client],
        |row| row.get(0),
    )?;
    route_policy(db, &host, project)
}

fn register_policy(db: &Connection, body: &Value) -> Result<Value> {
    let version = text(body, "version")?;
    let parent = body["parent"].as_str().unwrap_or(STABLE);
    let cap = match body["evidence_token_cap"] {
        Value::Null => None,
        _ => Some(
            body["evidence_token_cap"]
                .as_u64()
                .filter(|limit| *limit <= 262_144)
                .ok_or_else(|| Error::invalid("invalid evidence_token_cap"))? as u32,
        ),
    };
    if !policy_eligible(db, parent)? || policy_cap(db, version)?.is_some() {
        return Err(Error::conflict(
            "policy version exists or parent is ineligible",
        ));
    }
    let roots: BTreeSet<String> = if body["training_roots"].is_null() {
        BTreeSet::new()
    } else {
        parse(&body["training_roots"])?
    };
    for root in &roots {
        if !eligible_root(db, root, Purpose::Training)? {
            return Err(Error::denied(
                "all model dependencies require training permission",
            ));
        }
    }
    db.execute(
        "INSERT INTO optimization_policies VALUES (?1,?2,?3)",
        params![version, parent, cap],
    )?;
    for root in roots {
        db.execute(
            "INSERT INTO optimization_training_roots VALUES (?1,?2)",
            params![version, root],
        )?;
    }
    Ok(json!({"version":version,"parent":parent,"evidence_token_cap":cap}))
}

#[derive(Deserialize)]
struct Thresholds {
    min_independently_scored_pairs: usize,
    min_net_gained: Option<i64>,
    max_candidate_cost_units: Option<u64>,
    max_forbidden_hits: usize,
    min_paired_gain_lower_bound: Option<f64>,
    max_cost_per_success: Option<f64>,
}

fn scope(body: &Value) -> Result<(&str, &str)> {
    let host = text(body, "host")?;
    let project = text(body, "project")?;
    if host.len() > 128 || project.len() > 200 {
        return Err(Error::invalid("invalid host or project scope"));
    }
    Ok((host, project))
}

fn begin_trial(db: &Connection, body: &Value) -> Result<Value> {
    let (host, project) = scope(&body["scope"])?;
    let candidate = text(body, "candidate")?;
    let baseline = route_policy(db, host, project)?.version;
    if candidate == baseline || !policy_eligible(db, candidate)? {
        return Err(Error::conflict(
            "candidate must be eligible and distinct from current policy",
        ));
    }
    let thresholds: Thresholds = parse(&body["thresholds"])?;
    if thresholds.min_independently_scored_pairs == 0
        || thresholds.min_independently_scored_pairs > 512
        || thresholds.min_net_gained.is_none() && thresholds.min_paired_gain_lower_bound.is_none()
        || thresholds.max_candidate_cost_units.is_none()
            && thresholds.max_cost_per_success.is_none()
        || thresholds
            .min_paired_gain_lower_bound
            .is_some_and(|gain| !(-1.0..=1.0).contains(&gain))
        || thresholds
            .max_cost_per_success
            .is_some_and(|cost| cost < 0.0)
    {
        return Err(Error::invalid("invalid sample, gain or cost threshold"));
    }
    let authorized = body["authorized"].as_bool().unwrap_or(false);
    let mut families: BTreeSet<String> = if body["holdout_families"].is_null() {
        BTreeSet::new()
    } else {
        parse(&body["holdout_families"])?
    };
    let dataset_id = body["dataset_id"].as_str();
    if let Some(dataset_id) = dataset_id {
        let frozen = dataset(db, dataset_id)?;
        if frozen["usable"] != true || frozen["purpose"] != "evaluation" {
            return Err(Error::denied("trial requires a usable evaluation dataset"));
        }
        let members = frozen["members"]
            .as_array()
            .ok_or_else(|| Error::internal("invalid stored dataset members"))?;
        let derived = members
            .iter()
            .map(|item| {
                item["family"]
                    .as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| Error::internal("case family missing"))
            })
            .collect::<Result<BTreeSet<_>>>()?;
        if !families.is_empty() && families != derived {
            return Err(Error::invalid(
                "holdout families do not match the frozen dataset",
            ));
        }
        families = derived;
    }
    if families.is_empty()
        || families.len() > 512
        || families
            .iter()
            .any(|family| family.trim().is_empty() || family.len() > 512)
    {
        return Err(Error::invalid(
            "trial requires 1 to 512 frozen holdout families",
        ));
    }
    let trial = id("trial");
    db.execute(
        "INSERT INTO optimization_trials VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,NULL)",
        params![
            trial,
            host,
            project,
            baseline,
            candidate,
            dataset_id,
            serde_json::to_string(&families)?,
            body["thresholds"].to_string(),
            authorized
        ],
    )?;
    Ok(json!({"trial_id":trial,"baseline":baseline,"candidate":candidate,"scope":body["scope"]}))
}

fn revert(db: &Connection, body: &Value) -> Result<Value> {
    let (host, project) = scope(&body["scope"])?;
    let fallback: String = db
        .query_row(
            "SELECT fallback FROM optimization_serving WHERE host=?1 AND project=?2",
            params![host, project],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("no previous policy at this scope"))?;
    let target = if policy_eligible(db, &fallback)? {
        fallback.as_str()
    } else {
        STABLE
    };
    db.execute(
        "UPDATE optimization_serving SET active=?3,fallback=?3 WHERE host=?1 AND project=?2",
        params![host, project, target],
    )?;
    Ok(json!({"active_policy":target,"scope":body["scope"]}))
}

#[derive(Clone, Copy, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Verdict {
    Passed,
    Failed,
    Unknown,
}

fn pair(baseline: Option<Verdict>, candidate: Option<Verdict>) -> Option<i64> {
    let [baseline, candidate] = [baseline?, candidate?];
    if baseline == Verdict::Unknown || candidate == Verdict::Unknown {
        return None;
    }
    Some(i64::from(candidate == Verdict::Passed) - i64::from(baseline == Verdict::Passed))
}

#[derive(Deserialize)]
struct Attempt {
    task: String,
    baseline: Option<Verdict>,
    candidate: Option<Verdict>,
    cost_units: u64,
    #[serde(default)]
    forbidden_refs: Vec<String>,
}

fn assess(db: &Connection, body: &Value) -> Result<Value> {
    let trial = text(body, "trial_id")?;
    let attempts: Vec<Attempt> = parse(&body["attempts"])?;
    let (host, project, baseline, candidate, dataset_id, families, thresholds, authorized, previous) =
        db.query_row("SELECT host,project,baseline,candidate,dataset,families,thresholds,authorized,assessment
            FROM optimization_trials WHERE id=?1", [trial], |row| Ok((
            row.get::<_,String>(0)?,row.get::<_,String>(1)?,row.get::<_,String>(2)?,
            row.get::<_,String>(3)?,row.get::<_,Option<String>>(4)?,row.get::<_,String>(5)?,
            row.get::<_,String>(6)?,row.get::<_,bool>(7)?,row.get::<_,Option<String>>(8)?,
        ))).optional()?.ok_or_else(|| Error::missing("trial unavailable"))?;
    if previous.is_some() {
        return Err(Error::conflict("trial already assessed"));
    }
    let expected: BTreeSet<String> = read(&families)?;
    let observed: BTreeSet<_> = attempts.iter().map(|a| a.task.clone()).collect();
    if attempts.len() != observed.len() || observed != expected {
        return Err(Error::invalid(
            "exactly one usage row per preregistered task family is required",
        ));
    }
    let limits: Thresholds = read(&thresholds)?;
    let mut gains = Vec::new();
    let mut unknown = Vec::new();
    let mut blocking = Vec::new();
    let mut cost = 0_u64;
    let mut forbidden = 0;
    let mut successes = 0;
    for attempt in &attempts {
        cost = cost
            .checked_add(attempt.cost_units)
            .ok_or_else(|| Error::invalid("combined cost is too large"))?;
        successes += usize::from(attempt.candidate == Some(Verdict::Passed));
        forbidden += attempt.forbidden_refs.len();
        if !attempt.forbidden_refs.is_empty() {
            blocking.push(attempt.task.as_str());
        }
        if let Some(delta) = pair(attempt.baseline, attempt.candidate) {
            gains.push(delta);
        } else {
            unknown.push(attempt.task.as_str());
        }
    }
    let count = gains.len();
    let gained = gains.iter().filter(|gain| **gain == 1).count();
    let net = gains.iter().sum::<i64>();
    let lower_bound = limits
        .min_paired_gain_lower_bound
        .and_then(|_| bootstrap_lower_bound(&gains));
    let cost_per_success = (successes > 0).then(|| cost as f64 / successes as f64);
    let active = route_policy(db, &host, &project)?.version;
    let mut reasons = Vec::new();
    if count < limits.min_independently_scored_pairs {
        reasons.push("insufficient_pairs");
    }
    if limits.min_net_gained.is_some_and(|limit| net < limit)
        || limits
            .min_paired_gain_lower_bound
            .is_some_and(|limit| lower_bound.is_none_or(|bound| bound < limit))
    {
        reasons.push("insufficient_gain");
    }
    if limits
        .max_candidate_cost_units
        .is_some_and(|limit| cost > limit)
        || limits
            .max_cost_per_success
            .is_some_and(|limit| cost_per_success.is_none_or(|ratio| ratio > limit))
    {
        reasons.push("cost_budget");
    }
    if forbidden > limits.max_forbidden_hits {
        reasons.push("forbidden_evidence");
    }
    if !authorized {
        reasons.push("scope_not_authorized");
    }
    if active != baseline {
        reasons.push("baseline_changed");
    }
    if !policy_eligible(db, &candidate)? {
        reasons.push("policy_ineligible");
    }
    if let Some(id) = dataset_id
        && dataset(db, &id)?["usable"] != true
    {
        reasons.push("dataset_unavailable");
    }
    let released = reasons.is_empty();
    let active = if released {
        db.execute("INSERT INTO optimization_serving VALUES (?1,?2,?3,?4)
                    ON CONFLICT(host,project) DO UPDATE SET active=excluded.active,fallback=excluded.fallback",
                    params![host,project,candidate,baseline])?;
        candidate
    } else {
        active
    };
    let summary = json!({"trial_id":trial,"active_policy":active,"released":released,"scored_pairs":count,
        "unknown_pairs":unknown,"gained_count":gained,"regressed_count":gains.iter().filter(|gain|**gain==-1).count(),
        "net_gained":net,"candidate_cost_units":cost,"cost_per_verified_success":cost_per_success,
        "paired_gain_lower_bound":lower_bound,"blocking_reasons":reasons,"blocking_tasks":blocking});
    db.execute(
        "UPDATE optimization_trials SET assessment=?2 WHERE id=?1",
        params![trial, summary.to_string()],
    )?;
    Ok(summary)
}

// Convolve the empirical distribution of paired deltas to enumerate the bootstrap
// probability mass without iterating over every sequence of draws.
fn bootstrap_lower_bound(gains: &[i64]) -> Option<f64> {
    let size = gains.len();
    if size == 0 {
        return None;
    }
    let mut weights = [0_f64; 3];
    for gain in gains {
        weights[(gain + 1) as usize] += 1.0 / size as f64;
    }
    let mut probabilities = vec![1.0];
    for _ in 0..size {
        let mut next = vec![0.0; probabilities.len() + 2];
        for (index, probability) in probabilities.iter().enumerate() {
            for (delta, weight) in weights.iter().enumerate() {
                next[index + delta] += probability * weight;
            }
        }
        probabilities = next;
    }
    let target = 0.05 * probabilities.iter().sum::<f64>();
    let mut cumulative = 0.0;
    for (index, probability) in probabilities.iter().enumerate() {
        cumulative += probability;
        if cumulative >= target {
            return Some((index as f64 - size as f64) / size as f64);
        }
    }
    Some(1.0)
}

#[derive(Deserialize)]
struct PairRow {
    host: String,
    task: String,
    baseline: Option<Verdict>,
    candidate: Option<Verdict>,
}

#[derive(Default, Serialize)]
struct HostReport {
    gained: Vec<String>,
    regressed: Vec<String>,
    unknown: Vec<String>,
    unchanged: Vec<String>,
}

fn pair_report(body: &Value) -> Result<Value> {
    let rows: Vec<PairRow> = parse(&body["rows"])?;
    let mut hosts = BTreeMap::<String, HostReport>::new();
    let mut seen = BTreeSet::new();
    for row in rows {
        if !seen.insert((row.host.clone(), row.task.clone())) {
            return Err(Error::invalid("duplicate host/task pair"));
        }
        let host = hosts.entry(row.host).or_default();
        let group = match pair(row.baseline, row.candidate) {
            Some(1) => &mut host.gained,
            Some(-1) => &mut host.regressed,
            Some(_) => &mut host.unchanged,
            None => &mut host.unknown,
        };
        group.push(row.task);
    }
    Ok(serde_json::to_value(hosts)?)
}

fn split(db: &Connection, body: &Value) -> Result<Value> {
    let frozen = dataset(db, text(body, "dataset_id")?)?;
    if frozen["usable"] != true || frozen["purpose"] != "evaluation" {
        return Err(Error::denied("split requires a usable evaluation dataset"));
    }
    let dev: BTreeSet<String> = parse(&body["dev_ids"])?;
    let cutoff = DateTime::parse_from_rfc3339(text(body, "cutoff")?)
        .map_err(|_| Error::invalid("cutoff must be RFC 3339"))?;
    let members = frozen["members"]
        .as_array()
        .ok_or_else(|| Error::internal("invalid frozen dataset members"))?;
    let mut roots = BTreeSet::new();
    let mut families = BTreeSet::new();
    let mut observed_dev = BTreeSet::new();
    for row in members {
        let case = text(row, "id")?;
        if dev.contains(case) {
            let time = DateTime::parse_from_rfc3339(text(row, "observed_at")?)
                .map_err(|_| Error::internal("invalid stored observation time"))?;
            if time >= cutoff {
                return Err(Error::invalid("development set crosses the holdout cutoff"));
            }
            roots.extend(case_roots(db, case)?);
            families.insert(text(row, "family")?);
            observed_dev.insert(case.to_owned());
        }
    }
    if observed_dev != dev {
        return Err(Error::invalid(
            "development set must be a subset of the frozen dataset",
        ));
    }
    let mut holdout = Vec::new();
    for row in members {
        let case = text(row, "id")?;
        let time = DateTime::parse_from_rfc3339(text(row, "observed_at")?)
            .map_err(|_| Error::internal("invalid stored observation time"))?;
        if time >= cutoff
            && !families.contains(text(row, "family")?)
            && case_roots(db, case)?
                .iter()
                .all(|root| !roots.contains(root))
        {
            holdout.push(case);
        }
    }
    Ok(json!({"dev_ids":dev,"holdout_ids":holdout}))
}

#[derive(Deserialize)]
struct ObservedAnswer {
    id: String,
    output: String,
    #[serde(default)]
    cited: BTreeSet<String>,
}

fn score_answers(body: &Value) -> Result<Value> {
    let required: BTreeSet<String> = parse(&body["required"])?;
    let forbidden: BTreeSet<String> = parse(&body["forbidden"])?;
    let rows: Vec<ObservedAnswer> = parse(&body["observed_calls"])?;
    let unanswerable = body["unanswerable"] == true;
    let mut summary = BTreeMap::new();
    for row in rows {
        let hits: Vec<_> = row.cited.intersection(&forbidden).collect();
        let output = row
            .output
            .trim()
            .trim_end_matches(['。', '.', '！', '!'])
            .to_ascii_lowercase();
        let abstains = row.cited.is_empty()
            && matches!(
                output.as_str(),
                "没有可验证回执"
                    | "没有足够的授权依据"
                    | "no verified evidence"
                    | "insufficient authorized evidence"
            );
        let found = summary.insert(
            row.id,
            json!({"correct_abstention":unanswerable && abstains &&
            hits.is_empty(),"forbidden_hits":hits,
            "required_coverage":row.cited.intersection(&required).count()}),
        );
        if found.is_some() {
            return Err(Error::invalid("duplicate observed answer"));
        }
    }
    Ok(serde_json::to_value(summary)?)
}

#[derive(Deserialize)]
struct Usage {
    cost_units: u64,
    authorized_sufficient: bool,
    input_complete: bool,
    asked_user: bool,
    verdict: Option<Verdict>,
}

#[derive(Deserialize, PartialEq)]
struct Declaration {
    value: String,
    scope: String,
    until: Option<String>,
}

#[derive(Deserialize)]
struct RequiredDeclaration {
    id: String,
    expected: Declaration,
    actual: Declaration,
}

#[derive(Deserialize)]
struct TemporaryDeclaration {
    observed_scope: String,
}

fn usage_report(body: &Value) -> Result<Value> {
    let attempts: Vec<Usage> = parse(&body["usage_log"])?;
    let declarations: Vec<RequiredDeclaration> = parse(&body["required_declarations"])?;
    let temporary: Vec<TemporaryDeclaration> = parse(&body["temporary_declarations"])?;
    let mut value = Vec::new();
    let mut scope = Vec::new();
    let mut validity = Vec::new();
    let mut correct = 0;
    for declaration in &declarations {
        if declaration.expected == declaration.actual {
            correct += 1;
        }
        if declaration.expected.value != declaration.actual.value {
            value.push(declaration.id.as_str());
        }
        if declaration.expected.scope != declaration.actual.scope {
            scope.push(declaration.id.as_str());
        }
        if declaration.expected.until != declaration.actual.until {
            validity.push(declaration.id.as_str());
        }
    }
    let sufficient = attempts
        .iter()
        .filter(|row| row.authorized_sufficient)
        .count();
    let complete = attempts
        .iter()
        .filter(|row| row.authorized_sufficient && row.input_complete)
        .count();
    let passed = attempts
        .iter()
        .filter(|row| row.verdict == Some(Verdict::Passed))
        .count();
    let unassisted = attempts
        .iter()
        .filter(|row| row.verdict == Some(Verdict::Passed) && !row.asked_user)
        .count();
    let unknown = attempts
        .iter()
        .filter(|row| {
            row.verdict
                .is_none_or(|verdict| verdict == Verdict::Unknown)
        })
        .count();
    let total = attempts.iter().try_fold(0_u64, |acc, row| {
        acc.checked_add(row.cost_units)
            .ok_or_else(|| Error::invalid("combined cost is too large"))
    })?;
    Ok(json!({"correct_declarations":[correct,declarations.len()],
        "incorrect_declaration_ids":{"value":value,"scope":scope,"validity":validity},
        "temporary_promoted":temporary.iter().filter(|row|row.observed_scope=="global").count(),
        "complete_input":[complete,sufficient],"unassisted_verified":[unassisted,attempts.len()],
        "unknown_outcomes":unknown,"cost_per_verified_success":(passed>0).then(||total as f64/passed as f64)}))
}

#[derive(Deserialize)]
struct Handoff {
    id: String,
    asked_for_background: usize,
}

fn telemetry_report(body: &Value) -> Result<Value> {
    let mut ms: Vec<u64> = parse(&body["query_duration_ms"])?;
    if ms.is_empty() || body["limits"]["percentile"] != "nearest_rank" {
        return Err(Error::invalid(
            "telemetry requires duration samples and nearest-rank percentile",
        ));
    }
    ms.sort_unstable();
    let p95 = ms[(95 * ms.len()).div_ceil(100) - 1];
    let time = |field| {
        DateTime::parse_from_rfc3339(text(&body["event_times"], field)?)
            .map_err(|_| Error::invalid("telemetry event times must be RFC 3339"))
    };
    let elapsed = time("usable")?.signed_duration_since(time("accepted")?);
    if elapsed.num_milliseconds() < 0 {
        return Err(Error::invalid("usable time precedes accepted time"));
    }
    let limit: u64 = parse(&body["limits"]["p95_ms"])?;
    let handoffs: Vec<Handoff> = parse(&body["handoffs"])?;
    let mut rebrief = BTreeMap::new();
    for row in handoffs {
        if rebrief.insert(row.id, row.asked_for_background).is_some() {
            return Err(Error::invalid("duplicate handoff"));
        }
    }
    Ok(json!({"p95_ms":p95,"latency_budget_met":p95<=limit,
        "capture_to_ready_seconds":elapsed.num_seconds(),"rebrief_by_handoff":rebrief}))
}

fn evidence_ledger(db: &Connection, delivery: &str) -> Result<Value> {
    let (attempt, client, task, handles): (String, String, String, String) = db
        .query_row(
            "SELECT d.attempt_id,d.client_id,d.task_id,e.handles FROM context_deliveries d
         JOIN context_delivery_evidence e ON e.delivery_id=d.id
         WHERE d.id=?1 AND d.attempt_id IS NOT NULL",
            [delivery],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("evidence delivery unavailable"))?;
    let mut provided = BTreeSet::new();
    for handle in read::<BTreeSet<String>>(&handles)? {
        if crate::retrieval::client_can_expand(db, &handle, &client, Some(&task))? {
            provided.insert(handle);
        }
    }
    let mut query =
        db.prepare("SELECT result FROM context_receipts WHERE delivery_id=?1 ORDER BY rowid")?;
    let mut loaded = BTreeSet::new();
    let mut used = BTreeSet::new();
    for row in query.query_map([delivery], |row| row.get::<_, String>(0))? {
        let document: Value = read(&row?)?;
        for (field, target) in [
            ("loaded_evidence_refs", &mut loaded),
            ("used_evidence_refs", &mut used),
        ] {
            if let Some(refs) = document[field].as_array() {
                target.extend(
                    refs.iter()
                        .filter_map(Value::as_str)
                        .filter(|handle| provided.contains(*handle))
                        .map(str::to_owned),
                );
            }
        }
    }
    let outcome: String = db.query_row(
        "SELECT COALESCE((SELECT json_extract(result,'$.status') FROM context_outcomes
          WHERE attempt_id=?1 ORDER BY rowid DESC LIMIT 1),'unknown')",
        [attempt],
        |row| row.get(0),
    )?;
    Ok(
        json!({"provided_count":provided.len(),"loaded_refs":loaded,"used_refs":used,
        "independent_outcome":outcome}),
    )
}

pub fn handle(db: &mut Connection, action: &str, body: Value, key: Option<&str>) -> Result<Value> {
    let tx = db.transaction()?;
    let response = match action {
        "cases" => list_cases(&tx, &body)?,
        "dataset" => dataset(&tx, text(&body, "dataset_id")?)?,
        "run-status" => run_status(&tx, text(&body, "run_id")?)?,
        "checkout" => checkout_run(&tx, &body)?,
        "split" => split(&tx, &body)?,
        "pairs" => pair_report(&body)?,
        "score" => score_answers(&body)?,
        "usage" => usage_report(&body)?,
        "telemetry" => telemetry_report(&body)?,
        "ledger" => evidence_ledger(&tx, text(&body, "delivery_id")?)?,
        "policy-status" => json!({"version":text(&body,"version")?,
            "eligible":policy_eligible(&tx,text(&body,"version")?)?}),
        "permission" | "feedback" | "case" | "freeze" | "policy" | "trial" | "assess"
        | "revert" | "schedule" | "observe-call" => {
            let key = key
                .filter(|key| !key.is_empty() && key.len() <= 128)
                .ok_or_else(|| {
                    Error::invalid("an idempotency key of at most 128 bytes is required")
                })?;
            let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&body)?));
            if let Some((previous, result)) = tx
                .query_row(
                    "SELECT digest,receipt FROM optimization_intents WHERE action=?1 AND key=?2",
                    params![action, key],
                    |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
                )
                .optional()?
            {
                if digest != previous {
                    return Err(Error::conflict(
                        "key_conflict: optimization intent has changed",
                    ));
                }
                return read(&result);
            }
            let result = match action {
                "permission" => change_permission(&tx, &body),
                "feedback" => feedback(&tx, body, key),
                "case" => add_case(&tx, &body, None, None),
                "freeze" => freeze(&tx, &body),
                "schedule" => schedule(&tx, text(&body, "dataset_id")?),
                "observe-call" => observe_call(&tx, &body),
                "policy" => register_policy(&tx, &body),
                "trial" => begin_trial(&tx, &body),
                "assess" => assess(&tx, &body),
                "revert" => revert(&tx, &body),
                _ => unreachable!(),
            }?;
            tx.execute(
                "INSERT INTO optimization_intents VALUES (?1,?2,?3,?4)",
                params![action, key, digest, result.to_string()],
            )?;
            result
        }
        _ => return Err(Error::missing("optimization endpoint unavailable")),
    };
    tx.commit()?;
    Ok(response)
}
