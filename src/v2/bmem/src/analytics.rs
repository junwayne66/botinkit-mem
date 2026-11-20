//! Read projections for the owner workbench. Membership comes from recorded
//! source, scope and assertion keys; drawing a link never asserts a new fact.
use crate::error::{Error, Result};
use chrono::{Days, Utc};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE INDEX IF NOT EXISTS dashboard_statements ON authority_statements(known, id) WHERE revoked=0;
         CREATE INDEX IF NOT EXISTS dashboard_events ON ing_event_versions(received_at, event_id) WHERE active=1;
         CREATE INDEX IF NOT EXISTS dashboard_event_kinds ON ing_event_versions(actor_kind) WHERE active=1;
         CREATE INDEX IF NOT EXISTS dashboard_job_status ON ing_jobs(status);
         CREATE INDEX IF NOT EXISTS dashboard_deliveries ON context_deliveries(created_at);
         CREATE INDEX IF NOT EXISTS dashboard_revision_statement ON authority_revisions(statement_id);",
    )?;
    Ok(())
}

struct Record {
    id: String,
    kind: String,
    label: String,
    detail: String,
    at: String,
    project: String,
    source: Option<String>,
    source_label: String,
}

fn recent(db: &Connection, limit: usize) -> Result<Vec<Record>> {
    // The two partial time indexes let SQLite merge newest-first streams and
    // stop at LIMIT. For large events use the native label without parsing a
    // potentially multi-megabyte tool result to display its preview.
    let mut query = db.prepare_cached(
        "SELECT s.id AS id,'statement',substr(s.original,1,320),
                s.known AS at,s.project,NULL,''
           FROM authority_statements s WHERE s.revoked=0
         UNION ALL
         SELECT e.event_id,e.actor_kind,
                CASE WHEN octet_length(e.content)<=4096 THEN
                    substr(coalesce(json_extract(e.content,'$.text'),json_extract(e.content,'$.checks'),e.native_object),1,320)
                    ELSE substr(e.native_object,1,320) END,
                e.received_at,coalesce(t.project,''),e.source_id,substr(i.kind,1,40)||' · '||substr(i.account,1,80)
           FROM ing_event_versions e JOIN ing_sources i ON i.source_id=e.source_id
           LEFT JOIN context_tasks t ON t.id=e.task_id WHERE e.active=1
         ORDER BY at DESC,id DESC LIMIT ?1",
    )?;
    let rows = query.query_map([limit as i64], |row| {
        let detail: String = row.get(2)?;
        Ok(Record {
            id: row.get(0)?,
            kind: row.get(1)?,
            label: detail.chars().take(88).collect(),
            detail,
            at: row.get(3)?,
            project: row.get(4)?,
            source: row.get(5)?,
            source_label: row.get(6)?,
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

pub fn overview(db: &Connection, days: Option<&str>) -> Result<Value> {
    let days = match days.unwrap_or("7") {
        "7" => 7,
        "30" => 30,
        _ => return Err(Error::invalid("days must be 7 or 30")),
    };
    let now = Utc::now();
    let first = now.date_naive() - Days::new(days - 1);
    // A fixed set of day ranges can count directly on the time indexes,
    // without sorting a temporary row for every event inside the window.
    let mut daily = db.prepare_cached(
        "WITH RECURSIVE days(day) AS (
             VALUES(?1) UNION ALL SELECT date(day,'+1 day') FROM days WHERE day<?2
         ) SELECT day,
             (SELECT count(*) FROM authority_statements WHERE revoked=0 AND known>=day AND known<date(day,'+1 day')),
             (SELECT count(*) FROM ing_event_versions WHERE active=1 AND received_at>=day AND received_at<date(day,'+1 day')),
             (SELECT count(*) FROM context_deliveries WHERE created_at>=day AND created_at<date(day,'+1 day'))
           FROM days ORDER BY day",
    )?;
    let activity = daily
        .query_map(
            params![first.to_string(), now.date_naive().to_string()],
            |r| {
                Ok(json!({
                    "date":r.get::<_,String>(0)?, "statements":r.get::<_,i64>(1)?,
                    "events":r.get::<_,i64>(2)?, "deliveries":r.get::<_,i64>(3)?
                }))
            },
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let (statements, sources, deliveries): (i64, i64, i64) = db.query_row(
        "SELECT (SELECT count(*) FROM authority_statements WHERE revoked=0),
                (SELECT count(*) FROM ing_sources), (SELECT count(*) FROM context_deliveries)",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let mut kinds = db.prepare_cached(
        "SELECT actor_kind,count(*) FROM ing_event_versions WHERE active=1 GROUP BY actor_kind",
    )?;
    let event_counts = kinds
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?
        .collect::<rusqlite::Result<HashMap<_, _>>>()?;
    let mut distribution = vec![json!({"kind":"statement","count":statements})];
    for kind in ["user_message", "assistant_message", "tool_result"] {
        distribution
            .push(json!({"kind":kind, "count":event_counts.get(kind).copied().unwrap_or(0)}));
    }
    let events: i64 = event_counts.values().sum();
    let mut job_counts =
        db.prepare_cached("SELECT status,count(*) FROM ing_jobs GROUP BY status ORDER BY status")?;
    let mut job_total = 0i64;
    let jobs = job_counts
        .query_map([], |r| {
            let count: i64 = r.get(1)?;
            job_total += count;
            Ok(json!({"status":r.get::<_,String>(0)?, "count":count}))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let recent: Vec<_> = recent(db, 8)?
        .into_iter()
        .map(|r| {
            json!({
                "id":r.id, "kind":r.kind, "label":r.label, "at":r.at
            })
        })
        .collect();
    Ok(json!({
        "days":days, "as_of":now.to_rfc3339(), "counts":{
            "statements":statements, "events":events, "sources":sources, "deliveries":deliveries, "jobs":job_total
        }, "activity":activity, "distribution":distribution, "jobs":jobs, "recent":recent
    }))
}

struct Membership {
    id: String,
    label: String,
    kind: &'static str,
}

pub fn graph(db: &Connection, limit: Option<&str>) -> Result<Value> {
    let limit = limit
        .unwrap_or("80")
        .parse::<usize>()
        .ok()
        .filter(|v| (1..=120).contains(v))
        .ok_or_else(|| Error::invalid("graph limit must be between 1 and 120"))?;
    let records = recent(db, limit)?;
    let ids = records
        .iter()
        .filter(|r| r.kind == "statement")
        .map(|r| &r.id)
        .collect::<Vec<_>>();
    let mut topics: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut revisions = db.prepare_cached(
        "SELECT r.statement_id, f.dimension, f.id FROM authority_revisions r
         JOIN authority_facts f ON f.id=r.fact_id
         WHERE r.statement_id IN (SELECT value FROM json_each(?1)) ORDER BY r.statement_id,f.dimension",
    )?;
    let mut rows = revisions.query([serde_json::to_string(&ids)?])?;
    while let Some(r) = rows.next()? {
        topics
            .entry(r.get(0)?)
            .or_default()
            .push((r.get(1)?, r.get(2)?));
    }

    let mut nodes = Vec::new();
    let mut edges = Vec::new();
    let mut communities = BTreeMap::new();
    let shown = records.len();
    for record in records {
        let is_statement = record.kind == "statement";
        let mut groups = Vec::new();
        if let Some(source) = &record.source {
            groups.push(Membership {
                id: format!("source:{source}"),
                label: record.source_label,
                kind: "source",
            });
        }
        if !record.project.is_empty() {
            groups.push(Membership {
                id: format!("project:{}", record.project),
                label: record.project,
                kind: "scope",
            });
        }
        let mut fact_id = None;
        if let Some(dimensions) = topics.remove(&record.id) {
            for (dimension, fact) in dimensions {
                let label = match dimension.as_str() {
                    "report.language" => "报告语言",
                    "report.opening" => "报告开头",
                    _ => &dimension,
                };
                groups.push(Membership {
                    id: format!("topic:{dimension}"),
                    label: label.to_owned(),
                    kind: "topic",
                });
                fact_id.get_or_insert(fact);
            }
        }
        if groups.is_empty() {
            groups.push(Membership {
                id: "scope:global".into(),
                label: "全局声明".into(),
                kind: "scope",
            });
        }
        let mut node = json!({
            "id":record.id, "kind":if is_statement {"statement"} else {"event"},
            "label":record.label, "detail":record.detail, "at":record.at,
            "record_kind":record.kind, "community":groups[0].id
        });
        if let Some(fact) = fact_id {
            node["fact_id"] = json!(fact);
        }
        nodes.push(node);
        for group in groups {
            edges.push(json!({"source":record.id,"target":group.id,"kind":group.kind}));
            let (_, size) = communities
                .entry(group.id.clone())
                .or_insert((group, 0usize));
            *size += 1;
        }
    }
    let mut hubs = Vec::new();
    for (id, (group, size)) in communities {
        nodes.push(json!({"id":id,"kind":"community","label":group.label,"community":id,"detail":group.label}));
        hubs.push(json!({"id":id,"kind":group.kind,"label":group.label,"size":size}));
    }
    let total: i64 = db.query_row(
        "SELECT (SELECT count(*) FROM authority_statements WHERE revoked=0)
              + (SELECT count(*) FROM ing_event_versions WHERE active=1)",
        [],
        |r| r.get(0),
    )?;
    Ok(
        json!({"nodes":nodes, "edges":edges, "communities":hubs, "total_records":total,
              "shown_records":shown, "truncated":total>shown as i64}),
    )
}

/// The console lists metadata, keeping full extraction inputs on the job-detail path.
pub fn job_page(
    db: &Connection,
    limit: &str,
    before: Option<&str>,
    status: Option<&str>,
) -> Result<Value> {
    let limit = limit
        .parse::<usize>()
        .ok()
        .filter(|v| (1..=100).contains(v))
        .ok_or_else(|| Error::invalid("job limit must be between 1 and 100"))?;
    let before = before
        .map(|value| {
            value
                .parse::<i64>()
                .ok()
                .filter(|v| *v > 0)
                .ok_or_else(|| Error::invalid("job cursor must be a positive row identifier"))
        })
        .transpose()?
        .unwrap_or(i64::MAX);
    let extra = match status {
        None => "",
        Some("pending" | "running" | "done" | "failed" | "cancelled") => " AND j.status=?3",
        Some(_) => return Err(Error::invalid("unknown job status")),
    };
    let mut query = db.prepare_cached(&format!(
        "SELECT j.rowid,j.job_id,j.source_id,substr(s.kind,1,40)||' · '||substr(s.account,1,80),
                j.event_id,e.actor_kind,j.status,j.attempts,j.available_at,j.updated_at,substr(j.fail_reason,1,512),
                (SELECT count(*) FROM ing_candidates c WHERE c.job_id=j.job_id)
         FROM ing_jobs j JOIN ing_sources s ON s.source_id=j.source_id
         JOIN ing_event_versions e ON e.event_id=j.event_id
         WHERE j.rowid<?1{extra} ORDER BY j.rowid DESC LIMIT ?2"
    ))?;
    let mut rows = if let Some(status) = status {
        query.query(params![before, limit as i64 + 1, status])?
    } else {
        query.query(params![before, limit as i64 + 1])?
    };
    let mut jobs = Vec::with_capacity(limit);
    let mut last = 0i64;
    while let Some(r) = rows.next()? {
        if jobs.len() == limit {
            return Ok(json!({"jobs":jobs,"next_cursor":last.to_string()}));
        }
        last = r.get(0)?;
        jobs.push(json!({
            "job_id":r.get::<_,String>(1)?, "source_id":r.get::<_,String>(2)?, "source_label":r.get::<_,String>(3)?,
            "event_id":r.get::<_,String>(4)?, "actor_kind":r.get::<_,String>(5)?, "status":r.get::<_,String>(6)?,
            "attempts":r.get::<_,i64>(7)?, "available_at":r.get::<_,String>(8)?, "updated_at":r.get::<_,String>(9)?,
            "fail_reason":r.get::<_,Option<String>>(10)?, "candidates_recorded":r.get::<_,i64>(11)?
        }));
    }
    Ok(json!({"jobs":jobs, "next_cursor":null}))
}
