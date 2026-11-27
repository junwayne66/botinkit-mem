use crate::error::{Error, Result};
use crate::{authority, context, retrieval};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS method_observations (
            event_id TEXT PRIMARY KEY REFERENCES ing_event_versions(event_id),
            scope TEXT NOT NULL, artifact TEXT NOT NULL, method TEXT NOT NULL, version TEXT NOT NULL,
            kind TEXT NOT NULL, run_id TEXT, cited_event TEXT REFERENCES method_observations(event_id),
            condition TEXT, other_conditions TEXT NOT NULL
         );
         CREATE INDEX IF NOT EXISTS method_observations_lookup
           ON method_observations(scope,artifact,method,event_id);
         CREATE TABLE IF NOT EXISTS method_cards (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, artifact TEXT NOT NULL, method TEXT NOT NULL,
            precondition TEXT NOT NULL, UNIQUE(scope,artifact,method)
         );
         CREATE TABLE IF NOT EXISTS method_revisions (
            id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES method_cards(id),
            previous_id TEXT REFERENCES method_revisions(id), inputs TEXT NOT NULL,
            strategy TEXT NOT NULL
         );
         CREATE INDEX IF NOT EXISTS method_revision_card ON method_revisions(card_id);
         CREATE TABLE IF NOT EXISTS method_candidates (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, artifact TEXT NOT NULL, method TEXT NOT NULL,
            precondition TEXT NOT NULL, versions TEXT NOT NULL, citations TEXT NOT NULL,
            status TEXT NOT NULL, digest TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS method_strategy (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1), current TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS method_owner_guidance (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES context_tasks(id), artifact TEXT NOT NULL,
            method TEXT NOT NULL, guidance TEXT, UNIQUE(task_id,artifact,method)
         );
         CREATE TABLE IF NOT EXISTS method_owner_guidance_evidence (
            guidance_id TEXT NOT NULL REFERENCES method_owner_guidance(id),
            event_id TEXT NOT NULL REFERENCES ing_event_versions(event_id),
            PRIMARY KEY(guidance_id,event_id)
         );
         CREATE INDEX IF NOT EXISTS method_owner_guidance_source ON method_owner_guidance_evidence(event_id);
         CREATE TRIGGER IF NOT EXISTS method_owner_guidance_forget AFTER INSERT ON ing_root_revocations BEGIN
            UPDATE method_owner_guidance SET guidance=NULL WHERE id IN (
              SELECT g.guidance_id FROM method_owner_guidance_evidence g
              JOIN ing_event_versions e ON e.event_id=g.event_id WHERE e.root_id=NEW.root_id
            );
         END;
         INSERT OR IGNORE INTO method_strategy VALUES (1,'counterexample.2');",
    )?;
    Ok(())
}

#[derive(Deserialize)]
struct MethodKey {
    task_id: String,
    artifact: String,
    method: String,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
struct Observation {
    #[serde(flatten)]
    key: ObservationKey,
    version: String,
    handle: String,
    #[serde(flatten)]
    evidence: Verification,
    observed_condition: Option<String>,
    #[serde(default)]
    extra_conditions: BTreeSet<String>,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
struct ObservationKey {
    artifact: String,
    method: String,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Verification {
    Outcome {
        verifier_id: String,
        outcome_event_id: String,
    },
    Prerequisite,
    Reported {
        cites: Option<String>,
    },
}

struct Fact {
    event_id: String,
    root: String,
    version: String,
    kind: String,
    run_id: Option<String>,
    cited_event: Option<String>,
    condition: Option<String>,
    extra_conditions: BTreeSet<String>,
    handle: String,
    text: String,
    stages: Value,
}

#[derive(Deserialize, Serialize)]
struct RevisionInputs {
    events: BTreeSet<String>,
}

struct Card {
    method: String,
    artifact: String,
    precondition: String,
    revision: String,
    parent: Option<String>,
    inputs: RevisionInputs,
}

fn task_scope(
    db: &Connection,
    task: &str,
    reader: Option<&context::BoundClient>,
) -> Result<String> {
    if let Some(reader) = reader
        && !context::task_access(db, task, &reader.id)?
    {
        return Err(Error::missing("task unavailable"));
    }
    db.query_row(
        "SELECT CASE WHEN project IS NULL OR project='' THEN 'task:'||id ELSE 'project:'||project END
         FROM context_tasks WHERE id=?1",
        [task],
        |row| row.get(0),
    )
    .optional()?
    .ok_or_else(|| Error::missing("task unavailable"))
}

fn strategy(db: &Connection) -> Result<String> {
    Ok(db.query_row(
        "SELECT current FROM method_strategy WHERE singleton=1",
        [],
        |row| row.get(0),
    )?)
}

fn label(value: &str) -> Result<String> {
    let value = value.trim();
    if value.is_empty() || value.len() > 1000 || authority::redact(value) != value {
        return Err(Error::invalid("invalid method label or condition"));
    }
    Ok(value.to_owned())
}

fn live_event(db: &Connection, handle: &str) -> Result<(String, String, String)> {
    db.query_row(
        "SELECT e.event_id,e.task_id,e.actor_kind
         FROM recall_events r JOIN ing_event_versions e ON e.event_id=r.event_id
         JOIN ing_grants g ON g.source_id=e.source_id AND g.purpose='extract' AND g.active=1
         WHERE r.handle=?1 AND e.active=1 AND e.task_id IS NOT NULL",
        [handle],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )
    .optional()?
    .ok_or_else(|| Error::missing("method evidence unavailable"))
}

fn observe(db: &Connection, body: &Value) -> Result<Value> {
    let request: Observation = serde_json::from_value(body.clone())?;
    let artifact = label(&request.key.artifact)?;
    let method = label(&request.key.method)?;
    let version = label(&request.version)?;
    let condition = request
        .observed_condition
        .as_deref()
        .map(label)
        .transpose()?;
    let extra_conditions = request
        .extra_conditions
        .iter()
        .map(|item| label(item))
        .collect::<Result<BTreeSet<_>>>()?;
    let (event, task, actor) = live_event(db, &request.handle)?;
    let scope = task_scope(db, &task, None)?;
    let (kind, run_id, cited_event) = match request.evidence {
        Verification::Outcome {
            verifier_id,
            outcome_event_id,
        } => {
            let (run, result): (String, String) = db
                .query_row(
                    "SELECT a.id,o.result FROM context_outcomes o
                     JOIN context_attempts a ON a.id=o.attempt_id
                     JOIN context_task_access g ON g.task_id=a.task_id AND g.client_id=o.verifier_id
                     WHERE o.verifier_id=?1 AND o.event_id=?2 AND a.task_id=?3 AND a.client_id<>o.verifier_id",
                    params![verifier_id, outcome_event_id, task],
                    |row| Ok((row.get(0)?,row.get(1)?)),
                )
                .optional()?
                .ok_or_else(|| Error::missing("verified outcome unavailable"))?;
            let result: Value = serde_json::from_str(&result)?;
            let target: Value =
                serde_json::from_str(result["artifact_ref"].as_str().unwrap_or_default())
                    .unwrap_or_default();
            if result["evidence_ref"] != request.handle
                || target["artifact"] != artifact
                || target["version"] != version
            {
                return Err(Error::invalid(
                    "verified outcome does not bind to this evidence and artifact version",
                ));
            }
            let kind = if result["cancelled"] == true {
                "cancelled"
            } else {
                match result["status"].as_str() {
                    Some("passed") if actor == "tool_result" => "passed",
                    Some("failed") if actor == "tool_result" => "failed",
                    Some("unknown") => "unknown",
                    _ => return Err(Error::invalid("conclusive outcomes require a tool receipt")),
                }
            };
            (kind, Some(run), None)
        }
        Verification::Prerequisite => {
            if condition.is_none() {
                return Err(Error::invalid(
                    "prerequisite evidence must identify the condition",
                ));
            }
            ("prerequisite", None, None)
        }
        Verification::Reported { cites } => {
            let original = if let Some(handle) = cites {
                let (original, _, _) = live_event(db, &handle)?;
                if original == event || !db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM method_observations
                                    WHERE event_id=?1 AND scope=?2 AND artifact=?3 AND method=?4 AND version=?5)",
                    params![original, scope, artifact, method, version],
                    |row| row.get::<_, bool>(0),
                )? {
                    return Err(Error::missing("cited method observation unavailable"));
                }
                Some(original)
            } else {
                None
            };
            ("reported", None, original)
        }
    };
    let conditions = serde_json::to_string(&extra_conditions)?;
    let existing_matches = db
        .query_row(
            "SELECT artifact=?2 AND method=?3 AND version=?4 AND kind=?5
                    AND run_id IS ?6 AND cited_event IS ?7 AND condition IS ?8 AND other_conditions=?9
             FROM method_observations WHERE event_id=?1",
            params![event, artifact, method, version, kind, run_id, cited_event, condition, conditions],
            |row| row.get::<_, bool>(0),
        )
        .optional()?;
    if existing_matches == Some(false) {
        return Err(Error::conflict(
            "method observation already has another interpretation",
        ));
    }
    if existing_matches.is_none() {
        db.execute(
            "INSERT INTO method_observations VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![
                event,
                scope,
                artifact,
                method,
                version,
                kind,
                run_id,
                cited_event,
                condition,
                conditions
            ],
        )?;
    }
    Ok(json!({"event_ref":request.handle,"kind":kind}))
}

fn facts(
    db: &Connection,
    scope: &str,
    artifact: &str,
    method: &str,
    reader: Option<&context::BoundClient>,
) -> Result<Vec<Fact>> {
    let mut stmt = db.prepare(
        "SELECT e.event_id,e.root_id,o.version,o.kind,o.run_id,o.cited_event,o.condition,
                o.other_conditions,r.handle,e.content
         FROM method_observations o JOIN ing_event_versions e ON e.event_id=o.event_id
         JOIN recall_events r ON r.event_id=e.event_id
         JOIN ing_grants g ON g.source_id=e.source_id AND g.purpose='extract' AND g.active=1
         WHERE e.active=1 AND o.scope=?1 AND o.artifact=?2 AND o.method=?3
         ORDER BY o.kind='failed' DESC,o.rowid",
    )?;
    let mut rows = stmt.query(params![scope, artifact, method])?;
    let mut facts = Vec::new();
    while let Some(row) = rows.next()? {
        let handle: String = row.get(8)?;
        if let Some(reader) = reader
            && !retrieval::client_can_expand(db, &handle, &reader.id, None)?
        {
            continue;
        }
        let content: Value = serde_json::from_str(&row.get::<_, String>(9)?)?;
        facts.push(Fact {
            event_id: row.get(0)?,
            root: row.get(1)?,
            version: row.get(2)?,
            kind: row.get(3)?,
            run_id: row.get(4)?,
            cited_event: row.get(5)?,
            condition: row.get(6)?,
            extra_conditions: serde_json::from_str(&row.get::<_, String>(7)?)?,
            handle,
            text: content["text"]
                .as_str()
                .or(content["checks"]["result_text"].as_str())
                .unwrap_or_default()
                .to_owned(),
            stages: content["checks"]["stages"].clone(),
        });
    }
    Ok(facts)
}

fn latest_card(db: &Connection, scope: &str, artifact: &str, method: &str) -> Result<Card> {
    let (precondition, revision, parent, encoded): (String, String, Option<String>, String) = db
        .query_row(
            "SELECT c.precondition,r.id,r.previous_id,r.inputs
             FROM method_cards c JOIN method_revisions r ON r.card_id=c.id
             WHERE c.scope=?1 AND c.artifact=?2 AND c.method=?3 ORDER BY r.rowid DESC LIMIT 1",
            params![scope, artifact, method],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("method unavailable"))?;
    Ok(Card {
        method: method.to_owned(),
        artifact: artifact.to_owned(),
        precondition,
        revision,
        parent,
        inputs: serde_json::from_str(&encoded)?,
    })
}

fn recompute(db: &Connection, key: &MethodKey, precondition: &str) -> Result<Value> {
    let scope = task_scope(db, &key.task_id, None)?;
    let artifact = label(&key.artifact)?;
    let method = label(&key.method)?;
    let precondition = label(precondition)?;
    let facts = facts(db, &scope, &artifact, &method, None)?;
    let (card, registered_condition): (String, String) = db.query_row(
        "INSERT INTO method_cards VALUES (?1,?2,?3,?4,?5)
         ON CONFLICT(scope,artifact,method) DO UPDATE SET id=id RETURNING id,precondition",
        params![
            format!("method_{}", Uuid::new_v4().simple()),
            scope,
            artifact,
            method,
            precondition
        ],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    if precondition != registered_condition {
        return Err(Error::conflict(
            "method has a different registered prerequisite",
        ));
    }
    let current = strategy(db)?;
    let events = facts.iter().map(|fact| fact.event_id.clone()).collect();
    let inputs = serde_json::to_string(&RevisionInputs { events })?;
    let previous: Option<(String, String)> = db
        .query_row(
            "SELECT id,inputs FROM method_revisions WHERE card_id=?1 ORDER BY rowid DESC LIMIT 1",
            [card.as_str()],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    if let Some((prior_id, _)) = previous.as_ref().filter(|(_, prior)| prior == &inputs) {
        return Ok(json!({"method_id":card,"revision_id":prior_id,"changed":false}));
    }
    let revision = format!("method_rev_{}", Uuid::new_v4().simple());
    db.execute(
        "INSERT INTO method_revisions VALUES (?1,?2,?3,?4,?5)",
        params![revision, card, previous.map(|(id, _)| id), inputs, current],
    )?;
    Ok(json!({"method_id":card,"revision_id":revision,"changed":true}))
}

#[derive(Default)]
struct VersionCases<'a> {
    passed: Vec<&'a Fact>,
    failed: Vec<&'a Fact>,
    preconditions: Vec<&'a Fact>,
}

fn case_versions<'a>(
    facts: impl IntoIterator<Item = &'a Fact>,
) -> BTreeMap<&'a str, VersionCases<'a>> {
    let mut runs = BTreeSet::new();
    let mut roots = BTreeSet::new();
    let mut versions = BTreeMap::<&str, VersionCases<'_>>::new();
    for fact in facts {
        if fact.kind == "prerequisite" {
            versions
                .entry(&fact.version)
                .or_default()
                .preconditions
                .push(fact);
        }
        if let Some(run) = fact
            .run_id
            .as_ref()
            .filter(|_| matches!(fact.kind.as_str(), "passed" | "failed"))
            && !runs.contains(run)
            && !roots.contains(&fact.root)
        {
            runs.insert(run);
            roots.insert(&fact.root);
            let result = versions.entry(&fact.version).or_default();
            if fact.kind == "passed" {
                result.passed.push(fact);
            } else {
                result.failed.push(fact);
            }
        }
    }
    versions
}

fn case_facts<'a>(card: &Card, facts: &'a [Fact]) -> Vec<&'a Fact> {
    facts
        .iter()
        .filter(|fact| card.inputs.events.contains(&fact.event_id))
        .collect()
}

fn projection(
    card: &Card,
    facts: &[Fact],
    version: Option<&str>,
    conditions: Option<&Value>,
    authoritative_counterexample: bool,
) -> Value {
    let active = case_facts(card, facts);
    let versions = case_versions(active.iter().copied());
    let visible: BTreeMap<_, _> = active
        .iter()
        .map(|fact| (fact.event_id.as_str(), *fact))
        .collect();
    let group_support = |runs: &VersionCases<'_>, passed: &Fact| {
        passed.condition.as_deref() == Some(&card.precondition)
            || runs
                .preconditions
                .iter()
                .any(|fact| fact.condition.as_deref() == Some(&card.precondition))
    };
    let requested = version.and_then(|name| versions.get(name));
    let failure = requested.and_then(|runs| {
        runs.failed.iter().find(|fact| {
            fact.extra_conditions.is_empty()
                || conditions.is_none_or(|conditions| {
                    fact.extra_conditions.iter().all(|condition| {
                        conditions
                            .as_array()
                            .is_some_and(|items| items.iter().any(|item| item == condition))
                    })
                })
        })
    });
    let supported =
        requested.is_some_and(|runs| runs.passed.iter().any(|row| group_support(runs, row)));
    // Conditions only name observed truths. Omitting a condition does not disprove it.
    let no_counterexamples = !authoritative_counterexample;
    let confirmed = no_counterexamples
        && requested.is_some_and(|runs| {
            runs.passed
                .iter()
                .filter(|row| group_support(runs, row))
                .count()
                >= 2
                && conditions.is_some_and(|value| {
                    value
                        .as_array()
                        .is_some_and(|list| list.iter().any(|item| item == &card.precondition))
                })
        });
    let version_results: BTreeMap<_,_> = versions.iter().map(|(version,runs)|
        (*version,json!({
            "independent_passed":runs.passed.len(),
            "independent_failed":runs.failed.len(),
            "support_ids":runs.passed.iter().map(|fact| &fact.handle).collect::<Vec<_>>(),
            "counterexample_ids":runs.failed.iter().map(|fact| &fact.handle).collect::<Vec<_>>(),
        }))
    ).collect();
    let evidence = active
        .iter()
        .filter(|fact| {
            matches!(fact.kind.as_str(), "passed" | "failed")
                || fact.kind == "prerequisite"
                    && fact.condition.as_deref() == Some(&card.precondition)
        })
        .collect::<Vec<_>>();
    json!({
        "artifact":card.artifact,
        "version_results":version_results,
        "preconditions":if supported { vec![&card.precondition] } else { Vec::new() },
        "steps":if supported || failure.is_some() { vec![&card.method] } else { Vec::new() },
        "evidence_ids":evidence.iter().map(|fact|&fact.handle).collect::<Vec<_>>(),
        "unresolved_evidence_ids":active.iter().filter(|fact| fact.kind=="reported"
            && original_evidence(fact, &visible).is_none()
        ).map(|fact|&fact.handle).collect::<Vec<_>>(),
        "confirmed_fix_ids":if confirmed { vec![&card.method] } else { Vec::new() },
        "applicability":if failure.is_some() {"known_failure"} else if confirmed {"verified"} else {"needs_verification"},
        "default_delivery":supported && no_counterexamples,
        "missing_conditions":if version.is_none() {vec!["artifact_version"]} else {Vec::new()},
        "active_revision_ids":if supported || failure.is_some() {vec![&card.revision]} else {Vec::new()},
        "revision_ancestry":card.parent.iter().collect::<Vec<_>>(),
        "first_section":if failure.is_some() {"known_failure"} else if supported {"verified_cases"} else {"needs_verification"},
        "first_section_text":failure.map(|fact| fact.text.as_str()).unwrap_or_default(),
        "first_evidence_ids": failure.map(|fact|vec![&fact.handle]).unwrap_or_default(),
        "next_step_status":"needs_verification",
        "rendered":evidence.iter().map(|fact|fact.text.as_str()).collect::<Vec<_>>().join("\n"),
    })
}

fn has_counterexample(
    db: &Connection,
    scope: &str,
    card: &Card,
    version: Option<&str>,
) -> Result<bool> {
    let Some(version) = version else {
        return Ok(false);
    };
    // A newly verified failure blocks stale recommendations immediately. The query
    // deliberately returns only a gate. Presentation still uses reader-visible evidence.
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM method_observations o
         JOIN ing_event_versions e ON e.event_id=o.event_id AND e.active=1
         JOIN ing_grants g ON g.source_id=e.source_id AND g.purpose='extract' AND g.active=1
         WHERE o.scope=?1 AND o.artifact=?2 AND o.method=?3 AND o.version=?4 AND o.kind='failed')",
        params![scope, card.artifact, card.method, version],
        |row| row.get(0),
    )?)
}

fn original_evidence<'a>(
    mut fact: &'a Fact,
    current: &BTreeMap<&str, &'a Fact>,
) -> Option<&'a Fact> {
    while fact.kind == "reported" {
        // A citation can only point to a previously registered observation.
        fact = current.get(fact.cited_event.as_deref()?)?;
    }
    Some(fact)
}

#[derive(Deserialize)]
struct Query {
    #[serde(flatten)]
    key: MethodKey,
    version: Option<String>,
    conditions: Option<Value>,
}

fn validate_conditions(conditions: Option<&Value>) -> Result<Option<&Value>> {
    let conditions = conditions.filter(|value| !value.is_null());
    if conditions.is_some_and(|conditions| {
        conditions
            .as_array()
            .is_none_or(|items| items.len() > 100 || !items.iter().all(Value::is_string))
    }) {
        return Err(Error::invalid("conditions must be a list of strings"));
    }
    Ok(conditions)
}

#[derive(Deserialize)]
struct Candidate {
    #[serde(flatten)]
    key: MethodKey,
    precondition: String,
    text: String,
    supported_versions: BTreeSet<String>,
    citations: BTreeSet<String>,
}

fn propose(db: &Connection, body: &Value, key: &str) -> Result<Value> {
    let candidate: Candidate = serde_json::from_value(body.clone())?;
    let scope = task_scope(db, &candidate.key.task_id, None)?;
    let artifact = label(&candidate.key.artifact)?;
    let method = label(&candidate.key.method)?;
    let precondition = label(&candidate.precondition)?;
    label(&candidate.text)?;
    if candidate.supported_versions.is_empty() || candidate.citations.is_empty() {
        return Err(Error::invalid(
            "candidate must identify versions and cite their evidence",
        ));
    }
    for version in &candidate.supported_versions {
        label(version)?;
    }
    let known = facts(db, &scope, &artifact, &method, None)?;
    if candidate
        .citations
        .iter()
        .any(|handle| !known.iter().any(|fact| &fact.handle == handle))
    {
        return Err(Error::missing(
            "candidate cites unavailable method evidence",
        ));
    }
    let id = format!("method_proposal_{:x}", Sha256::digest(key.as_bytes()));
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(body)?));
    let existing: Option<String> = db
        .query_row(
            "SELECT digest FROM method_candidates WHERE id=?1",
            [id.as_str()],
            |row| row.get(0),
        )
        .optional()?;
    if let Some(existing) = existing {
        if existing != digest {
            return Err(Error::conflict(
                "key_conflict: method candidate already has another payload",
            ));
        }
    } else {
        db.execute(
            "INSERT INTO method_candidates VALUES(?1,?2,?3,?4,?5,?6,?7,'pending',?8)",
            params![
                id,
                scope,
                artifact,
                method,
                precondition,
                serde_json::to_string(&candidate.supported_versions)?,
                serde_json::to_string(&candidate.citations)?,
                digest
            ],
        )?;
    }
    Ok(json!({"candidate_id":id}))
}

fn judge(db: &Connection, body: &Value) -> Result<Value> {
    let key: MethodKey = serde_json::from_value(body.clone())?;
    let scope = task_scope(db, &key.task_id, None)?;
    let id = body["candidate_id"]
        .as_str()
        .ok_or_else(|| Error::invalid("provide candidate_id"))?;
    let (precondition, versions, citations, status): (String, String, String, String) = db
        .query_row(
            "SELECT precondition,versions,citations,status FROM method_candidates
         WHERE id=?1 AND scope=?2 AND artifact=?3 AND method=?4",
            params![id, scope, key.artifact, key.method],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("candidate unavailable"))?;
    if status != "pending" {
        return Ok(json!({"candidate_id":id,"status":status}));
    }
    let versions: BTreeSet<String> = serde_json::from_str(&versions)?;
    let citations: BTreeSet<String> = serde_json::from_str(&citations)?;
    let known = facts(db, &scope, &key.artifact, &key.method, None)?;
    let cases = case_versions(&known);
    let existing = match latest_card(db, &scope, &key.artifact, &key.method) {
        Ok(card) => Some(card),
        Err(error) if error.status == 404 => None,
        Err(error) => return Err(error),
    };
    let admitted = existing
        .as_ref()
        .is_none_or(|card| card.precondition == precondition)
        && versions.iter().all(|version| {
            cases.get(version.as_str()).is_some_and(|rows| {
                rows.failed.is_empty()
                    && rows
                        .passed
                        .iter()
                        .filter(|row| {
                            citations.contains(&row.handle)
                                && (row.condition.as_deref() == Some(&precondition)
                                    || rows.preconditions.iter().any(|e| {
                                        citations.contains(&e.handle)
                                            && e.condition.as_deref() == Some(&precondition)
                                    }))
                        })
                        .count()
                        >= 2
            })
        });
    let status = if admitted { "accepted" } else { "rejected" };
    db.execute(
        "UPDATE method_candidates SET status=?1 WHERE id=?2",
        params![status, id],
    )?;
    if admitted {
        recompute(db, &key, &precondition)?;
    }
    Ok(json!({"candidate_id":id,"status":status}))
}

fn audit(db: &Connection, body: &Value) -> Result<Value> {
    let key: MethodKey = serde_json::from_value(body.clone())?;
    let scope = task_scope(db, &key.task_id, None)?;
    let known = facts(db, &scope, &key.artifact, &key.method, None)?;
    let mut statements = db.prepare(
        "SELECT id,status FROM method_candidates WHERE scope=?1 AND artifact=?2 AND method=?3 ORDER BY rowid",
    )?;
    let candidates = statements
        .query_map(params![scope, key.artifact, key.method], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let outcomes: BTreeMap<_, _> = known
        .iter()
        .filter(|fact| fact.run_id.is_some())
        .map(|fact| {
            (
                &fact.handle,
                json!({"outcome":fact.kind, "verified_outcome":fact.kind}),
            )
        })
        .collect();
    let card = match latest_card(db, &scope, &key.artifact, &key.method) {
        Ok(card) => Some(card),
        Err(error) if error.status == 404 => None,
        Err(error) => return Err(error),
    };
    Ok(json!({
        "current_strategy":strategy(db)?,
        "current_revision":card.as_ref().map(|card|&card.revision),
        "parent_revision":card.as_ref().and_then(|card|card.parent.as_ref()),
        "pending_candidates":candidates.iter().filter(|(_,status)|status=="pending")
            .map(|(id,_)|id).collect::<Vec<_>>(),
        "effective_candidate_ids":candidates.iter().filter(|(_,status)|status=="accepted")
            .map(|(id,_)|id).collect::<Vec<_>>(),
        "outcomes":outcomes,
    }))
}

#[derive(Deserialize)]
struct OwnerGuidance {
    #[serde(flatten)]
    key: MethodKey,
    text: String,
    requires_all: BTreeSet<String>,
}

fn curate(db: &Connection, body: &Value) -> Result<Value> {
    let request: OwnerGuidance = serde_json::from_value(body.clone())?;
    let task = request.key.task_id;
    task_scope(db, &task, None)?;
    let artifact = label(&request.key.artifact)?;
    let method = label(&request.key.method)?;
    let text = label(&request.text)?;
    if request.requires_all.is_empty() || request.requires_all.len() > 32 {
        return Err(Error::invalid(
            "owner guidance requires one to 32 evidence handles",
        ));
    }
    let mut events = BTreeSet::new();
    for handle in request.requires_all {
        let evidence = retrieval::expand(db, &handle)?;
        if evidence["task_id"] != task {
            return Err(Error::missing("method evidence unavailable"));
        }
        let event = evidence["event_id"]
            .as_str()
            .ok_or_else(|| Error::internal("method evidence lacks ID"))?;
        events.insert(event.to_owned());
    }
    let id = format!("guidance_{}", Uuid::new_v4().simple());
    if db.execute(
        "INSERT OR IGNORE INTO method_owner_guidance VALUES(?1,?2,?3,?4,?5)",
        params![id, task, artifact, method, text],
    )? == 0
    {
        return Err(Error::conflict(
            "task already has owner guidance for this method",
        ));
    }
    for event in events {
        db.execute(
            "INSERT INTO method_owner_guidance_evidence VALUES(?1,?2)",
            params![id, event],
        )?;
    }
    Ok(
        json!({"guidance_id":id,"method":method,"provenance":"owner_guidance","verified_outcome":"unknown"}),
    )
}

fn visible_guidance(
    db: &Connection,
    task: &str,
    artifact: &str,
    method: Option<&str>,
    reader: Option<&context::BoundClient>,
) -> Result<Vec<Value>> {
    let mut query = db.prepare(
        "SELECT id,method,guidance FROM method_owner_guidance
                                WHERE task_id=?1 AND artifact=?2 AND guidance IS NOT NULL
                                  AND (?3 IS NULL OR method=?3) ORDER BY method",
    )?;
    let mut citations = db.prepare("SELECT g.event_id,e.active FROM method_owner_guidance_evidence g
                                    JOIN ing_event_versions e ON e.event_id=g.event_id WHERE g.guidance_id=?1 ORDER BY g.event_id")?;
    let mut visible = Vec::new();
    for result in query.query_map(params![task, artifact, method], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })? {
        let (id, name, text) = result?;
        let mut handles = Vec::new();
        let mut permitted = true;
        for citation in citations.query_map([id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, bool>(1)?))
        })? {
            let (event, live) = citation?;
            let handle = format!("event_{event}");
            if !live
                || (if let Some(reader) = reader {
                    !retrieval::client_can_expand(db, &handle, &reader.id, Some(task))?
                } else {
                    false
                })
            {
                permitted = false;
                break;
            }
            handles.push(handle);
        }
        if permitted && !handles.is_empty() {
            visible.push(json!({"method":name,"rendered":text,"evidence_ids":handles,"provenance":"owner_guidance",
                                "applicability":"owner_guidance","default_delivery":false,
                                "verified_outcome":"unknown","confirmed_fix_ids":[]}));
        }
    }
    Ok(visible)
}

pub fn write(conn: &mut Connection, action: &str, body: &Value, key: &str) -> Result<Value> {
    let db = conn.transaction()?;
    let output = match action {
        "observe" => observe(&db, body)?,
        "curate" => curate(&db, body)?,
        "recompute" => {
            let request: MethodKey = serde_json::from_value(body.clone())?;
            let condition = body["precondition"]
                .as_str()
                .ok_or_else(|| Error::invalid("provide precondition"))?;
            recompute(&db, &request, condition)?
        }
        "audit" => audit(&db, body)?,
        "propose" => propose(&db, body, key)?,
        "judge" => judge(&db, body)?,
        "strategy" => {
            let target = body["target"]
                .as_str()
                .filter(|target| matches!(*target, "baseline" | "counterexample.2"))
                .ok_or_else(|| Error::invalid("unknown method strategy"))?;
            db.execute(
                "UPDATE method_strategy SET current=?1 WHERE singleton=1",
                [target],
            )?;
            json!({"current_strategy":target})
        }
        _ => return Err(Error::missing("method action unavailable")),
    };
    db.commit()?;
    Ok(output)
}

pub fn read(
    db: &Connection,
    reader: Option<&context::BoundClient>,
    action: &str,
    body: &Value,
) -> Result<Value> {
    match action {
        "query" => {
            let query: Query = serde_json::from_value(body.clone())?;
            let conditions = validate_conditions(query.conditions.as_ref())?;
            let scope = task_scope(db, &query.key.task_id, reader)?;
            let unavailable = || {
                json!({"applicability":"needs_verification","default_delivery":false,
                                       "confirmed_fix_ids":[],"evidence_ids":[],"rendered":""})
            };
            let card = match latest_card(db, &scope, &query.key.artifact, &query.key.method) {
                Ok(card) => card,
                Err(error) if error.status == 404 => {
                    let mut curated = visible_guidance(
                        db,
                        &query.key.task_id,
                        &query.key.artifact,
                        Some(&query.key.method),
                        reader,
                    )?;
                    return Ok(curated.pop().unwrap_or_else(unavailable));
                }
                Err(error) => return Err(error),
            };
            let authorized = facts(db, &scope, &query.key.artifact, &query.key.method, reader)?;
            if reader.is_some() && case_facts(&card, &authorized).is_empty() {
                return Ok(unavailable());
            }
            let result = projection(
                &card,
                &authorized,
                query.version.as_deref(),
                conditions,
                has_counterexample(db, &scope, &card, query.version.as_deref())?,
            );
            Ok(result)
        }
        "evidence" => {
            let query: MethodKey = serde_json::from_value(body.clone())?;
            let scope = task_scope(db, &query.task_id, reader)?;
            let card = latest_card(db, &scope, &query.artifact, &query.method)?;
            let authorized = facts(db, &scope, &query.artifact, &query.method, reader)?;
            let handle = body["handle"]
                .as_str()
                .ok_or_else(|| Error::invalid("provide handle"))?;
            let found = case_facts(&card, &authorized)
                .into_iter()
                .find(|fact| fact.handle == handle)
                .ok_or_else(|| Error::missing("method evidence unavailable"))?;
            Ok(
                json!({"handle":found.handle,"text":found.text,"stages":found.stages,"outcome":found.kind}),
            )
        }
        "handoff" => {
            let task = body["task_id"]
                .as_str()
                .ok_or_else(|| Error::invalid("provide task_id"))?;
            let artifact = body["artifact"]
                .as_str()
                .ok_or_else(|| Error::invalid("provide artifact"))?;
            let scope = task_scope(db, task, reader)?;
            let conditions = validate_conditions(body.get("conditions"))?;
            let mut stmt = db.prepare(
                "SELECT method FROM method_cards WHERE scope=?1 AND artifact=?2 ORDER BY id",
            )?;
            let mut methods = Vec::new();
            let mut evidence = BTreeSet::new();
            let mut adjudicated = BTreeSet::new();
            for item in stmt.query_map(params![scope, artifact], |row| row.get::<_, String>(0))? {
                let method = item?;
                adjudicated.insert(method.clone());
                let card = latest_card(db, &scope, artifact, &method)?;
                let authorized = facts(db, &scope, artifact, &method, reader)?;
                let projected = projection(
                    &card,
                    &authorized,
                    body["version"].as_str(),
                    conditions,
                    has_counterexample(db, &scope, &card, body["version"].as_str())?,
                );
                if projected["default_delivery"] == true {
                    methods.push(method);
                    for handle in projected["evidence_ids"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                    {
                        evidence.insert(handle.to_owned());
                    }
                }
            }
            let curated = visible_guidance(db, task, artifact, None, reader)?
                .into_iter()
                .filter(|item| {
                    item["method"]
                        .as_str()
                        .is_some_and(|name| !adjudicated.contains(name))
                })
                .collect::<Vec<_>>();
            for item in &curated {
                if let Some(name) = item["method"].as_str() {
                    methods.push(name.to_owned());
                    if let Some(handles) = item["evidence_ids"].as_array() {
                        evidence
                            .extend(handles.iter().filter_map(Value::as_str).map(str::to_owned));
                    }
                }
            }
            Ok(json!({"active_methods":methods,"evidence_ids":evidence,"owner_guidance":curated}))
        }
        _ => Err(Error::missing("method action unavailable")),
    }
}
