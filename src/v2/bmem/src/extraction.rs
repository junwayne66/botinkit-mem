use crate::error::Result;
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS ing_extract_health (
           route TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT,
           probes INTEGER NOT NULL, next_probe_at INTEGER
         );",
    )?;
    Ok(())
}

pub fn unavailable(code: &str) -> bool {
    matches!(
        code,
        "model_unreachable" | "model_timeout" | "temporarily_unavailable"
    ) || code
        .strip_prefix("model_http_")
        .and_then(|s| s.parse::<u16>().ok())
        .is_some_and(|s| matches!(s, 401 | 403 | 404 | 408 | 429 | 500..=599))
}

#[derive(Debug, PartialEq, Eq)]
pub enum Work {
    Idle,
    Probe,
    Claim { trial_reason: Option<String> },
}

pub fn action(db: &Connection, route: &str) -> Result<Work> {
    let state = db
        .query_row(
            "SELECT state,reason,next_probe_at FROM ing_extract_health WHERE route=?1",
            [route],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<i64>>(2)?,
                ))
            },
        )
        .optional()?;
    match state {
        Some((state, _, Some(due))) if matches!(state.as_str(), "probing" | "waiting_model") => {
            return Ok(if due <= Utc::now().timestamp() {
                Work::Probe
            } else {
                Work::Idle
            });
        }
        Some((state, reason, _)) if state == "trial" => {
            return Ok(Work::Claim {
                trial_reason: reason,
            });
        }
        _ => {}
    }
    let mut query = db.prepare_cached(
        "SELECT job_id,error_code,unixepoch(finished_at) FROM ing_job_attempts
         WHERE route=?1 AND outcome IN ('retrying','failed','done')
         ORDER BY finished_at DESC,rowid DESC LIMIT 3",
    )?;
    let recent = query
        .query_map([route], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, i64>(2)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if let [a, b, c] = recent.as_slice()
        && let Some(reason) = a.1.as_deref()
        && a.1 == b.1
        && a.1 == c.1
        && a.0 != b.0
        && a.0 != c.0
        && b.0 != c.0
        && c.2 >= Utc::now().timestamp() - 600
    {
        waiting(db, route, reason, true)?;
        return Ok(Work::Probe);
    }
    Ok(Work::Claim { trial_reason: None })
}

pub fn waiting(db: &Connection, route: &str, reason: &str, check_now: bool) -> Result<()> {
    let attempts = db
        .query_row(
            "SELECT probes FROM ing_extract_health WHERE route=?1",
            [route],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .unwrap_or(0);
    let delay = if check_now {
        0
    } else {
        (30i64 << attempts.clamp(0, 6)).min(1800)
    };
    db.execute(
        "INSERT INTO ing_extract_health VALUES(?1,?2,?3,?4,?5)
         ON CONFLICT(route) DO UPDATE SET state=excluded.state,reason=excluded.reason,
           probes=excluded.probes,next_probe_at=excluded.next_probe_at",
        params![
            route,
            if check_now {
                "probing"
            } else {
                "waiting_model"
            },
            reason,
            if check_now {
                attempts
            } else {
                (attempts + 1).min(7)
            },
            Utc::now().timestamp() + delay
        ],
    )?;
    Ok(())
}

pub fn probe_passed(db: &Connection, route: &str) -> Result<()> {
    db.execute(
        "UPDATE ing_extract_health SET state='trial',next_probe_at=NULL WHERE route=?1",
        [route],
    )?;
    Ok(())
}

pub fn ready(db: &Connection, route: &str) -> Result<()> {
    db.execute("DELETE FROM ing_extract_health WHERE route=?1", [route])?;
    Ok(())
}

pub fn summary(db: &Connection, route: Option<&str>) -> Result<Value> {
    let Some(route) = route else {
        return Ok(json!({"state":"unconfigured"}));
    };
    let (state, reason, due) = db
        .query_row(
            "SELECT state,reason,next_probe_at FROM ing_extract_health WHERE route=?1",
            [route],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<i64>>(2)?,
                ))
            },
        )
        .optional()?
        .unwrap_or_else(|| ("ready".into(), None, None));
    let (queued, oldest) = db.query_row(
        "SELECT count(*),min(j.created_at) FROM ing_jobs j
         JOIN ing_model_routes r ON r.source_id=j.source_id AND r.fingerprint=?1
         JOIN ing_grants g ON g.source_id=j.source_id AND g.purpose='extract' AND g.active=1
         JOIN ing_event_versions e ON e.event_id=j.event_id AND e.active=1
         WHERE j.status IN ('pending','retrying','running')
           AND NOT EXISTS(SELECT 1 FROM ing_extract_pauses p WHERE p.source_id=j.source_id)
           AND NOT EXISTS(SELECT 1 FROM ing_extract_denials d WHERE d.root_id=e.root_id)",
        [route],
        |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?)),
    )?;
    let last_success: Option<String> = db.query_row(
        "SELECT finished_at FROM ing_job_attempts WHERE route=?1 AND outcome IN ('retrying','failed','done')
          AND outcome='done' ORDER BY finished_at DESC LIMIT 1", [route], |r| r.get(0),
    ).optional()?;
    Ok(
        json!({"state":state,"reason":reason,"queued":queued,"oldest_queued_at":oldest,
              "last_success_at":last_success,
              "next_probe_at":due.and_then(|instant| DateTime::from_timestamp(instant, 0)).map(|v| v.to_rfc3339()),
              "configuration_attention":reason.as_deref().is_some_and(|code| matches!(code,
                  "model_http_401" | "model_http_403" | "model_http_404"))}),
    )
}
