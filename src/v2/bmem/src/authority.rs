use crate::error::{Error, Result};
use chrono::{
    DateTime, Datelike, Duration, LocalResult, NaiveDateTime, SecondsFormat, TimeZone, Utc,
};
use chrono_tz::Tz;
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
      CREATE TABLE IF NOT EXISTS authority_revision_terms (
        revision_id TEXT PRIMARY KEY REFERENCES authority_revisions(id),
        clause TEXT, valid_until TEXT, timezone TEXT, task_id TEXT, valid_from TEXT
      );
      CREATE TABLE IF NOT EXISTS authority_project_timezones (
        project TEXT PRIMARY KEY, timezone TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS authority_pending_terms (
        statement_id TEXT NOT NULL REFERENCES authority_statements(id),
        ordinal INTEGER NOT NULL, key TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(statement_id,ordinal)
      );
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
    let has_term_start: bool = db.query_row(
        "SELECT EXISTS (SELECT 1 FROM pragma_table_info('authority_revision_terms') WHERE name='valid_from')",
        [],
        |row| row.get(0),
    )?;
    if !has_term_start {
        db.execute_batch("ALTER TABLE authority_revision_terms ADD COLUMN valid_from TEXT")?;
    }
    Ok(())
}

fn time(value: &str) -> Result<String> {
    let time = DateTime::parse_from_rfc3339(value)
        .map_err(|_| Error::invalid("time must be RFC 3339 with a time zone"))?;
    Ok(time
        .with_timezone(&Utc)
        .to_rfc3339_opts(SecondsFormat::Nanos, true))
}

pub fn set_project_timezone(db: &Connection, request: Value) -> Result<Value> {
    let project = request["project"]
        .as_str()
        .filter(|name| !name.trim().is_empty() && name.len() <= 200)
        .ok_or_else(|| Error::invalid("project timezone requires a project"))?;
    let timezone = request["timezone"]
        .as_str()
        .and_then(|name| name.parse::<Tz>().ok())
        .ok_or_else(|| Error::invalid("project timezone must be an IANA time zone"))?;
    db.execute(
        "INSERT INTO authority_project_timezones(project,timezone) VALUES(?1,?2)
         ON CONFLICT(project) DO UPDATE SET timezone=excluded.timezone",
        params![project, timezone.name()],
    )?;
    Ok(json!({"project":project,"timezone":timezone.name()}))
}

fn project_timezone(db: &Connection, project: &str, explicit: Option<Tz>) -> Result<Option<Tz>> {
    if explicit.is_some() || project.is_empty() {
        return Ok(explicit);
    }
    let configured: Option<String> = db
        .query_row(
            "SELECT timezone FROM authority_project_timezones WHERE project=?1",
            [project],
            |row| row.get(0),
        )
        .optional()?;
    configured
        .map(|name| {
            name.parse()
                .map_err(|_| Error::internal("stored project timezone is invalid"))
        })
        .transpose()
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

fn report_dimensions(original: &str) -> Vec<(&'static str, Option<&'static str>)> {
    let lower = original.to_ascii_lowercase();
    let report_text = lower
        .split(['，', ',', '。', '；', ';', '\n'])
        .filter(|clause| !is_code_directive(clause))
        .collect::<Vec<_>>()
        .join(" ");
    let text = report_text.as_str();
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
                        "开头给结论",
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
        .filter(|_| text.contains("报告") || text.contains("report"))
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

fn is_code_directive(text: &str) -> bool {
    text.contains("代码示例") || text.contains("code example") || text.contains("code sample")
}

fn code_example_language(text: &str, project: &str) -> Option<Option<&'static str>> {
    if !is_code_directive(text) {
        return None;
    }
    // These clauses require their own scope or time window before they can become active.
    if (project.is_empty() && text.contains("项目"))
        || [
            "这次",
            "本次",
            "本轮",
            "仅此",
            "那个项目",
            "给个",
            "来个",
            "下一次",
            "这周",
            "本周",
            "下周",
            "截止",
            "之前",
            "周五",
            "周日",
            "结束",
            "到期",
            "当地",
            "起，",
            "起,",
            "如果",
            "除非",
            "暂且",
            "不用",
            "不要",
            "不得",
            "不再",
            "是否",
            "？",
            "?",
            " if ",
            " unless ",
            " until ",
            " only ",
            "this time",
            "this task",
            "this project",
            " for project ",
            " for the project ",
        ]
        .iter()
        .any(|marker| text.contains(marker))
    {
        return Some(None);
    }
    let mut selected = None;
    for clause in text
        .split(['，', ',', '。', '；', ';', '\n'])
        .filter(|clause| is_code_directive(clause))
    {
        let Some(start) = ["代码示例", "code example", "code sample"]
            .iter()
            .find_map(|name| clause.find(name).map(|offset| offset + name.len()))
        else {
            continue;
        };
        let tail = clause[start..].trim_start();
        let command = tail.strip_prefix("s ").unwrap_or(tail);
        let Some(language) = [
            "必须使用",
            "必须用",
            "统一使用",
            "统一用",
            "请使用",
            "请用",
            "使用",
            "用",
            "must use ",
            "should use ",
            "use ",
        ]
        .iter()
        .find_map(|prefix| command.strip_prefix(prefix))
        .or_else(|| {
            clause
                .strip_prefix("use ")
                .and_then(|directive| directive.split_once(" for ").map(|(name, _)| name))
        }) else {
            continue;
        };
        let words = language.split(|c: char| !c.is_ascii_alphanumeric() && c != '+' && c != '#');
        for name in words {
            let canonical = match name {
                "rust" => "rust",
                "python" => "python",
                "go" | "golang" => "go",
                "kotlin" => "kotlin",
                "typescript" => "typescript",
                "javascript" => "javascript",
                "java" => "java",
                "swift" => "swift",
                "bash" => "bash",
                "c++" => "c++",
                "c#" => "c#",
                _ => continue,
            };
            if selected.is_some_and(|prior| prior != canonical) {
                return Some(None);
            }
            selected = Some(canonical);
        }
    }
    selected.map(Some)
}

struct StatementTerm<'a> {
    key: &'static str,
    value: Option<&'static str>,
    project: &'a str,
    clause: Option<&'a str>,
    from: Option<String>,
    until: Option<String>,
    timezone: Option<Tz>,
}

#[derive(Clone, Copy)]
enum CodeWindow<'a> {
    Weekly(&'a str, u32),
    From(&'a str),
    Unsupported,
}

struct StatementTime<'a> {
    timezone: Option<Tz>,
    effective: &'a str,
    valid_until: Option<&'a str>,
    known: &'a str,
}

fn statement_terms<'a>(
    db: &Connection,
    original: &'a str,
    project: &'a str,
    clock: StatementTime<'_>,
) -> Result<(Vec<StatementTerm<'a>>, Vec<Value>)> {
    let mut terms: Vec<_> = report_dimensions(original)
        .into_iter()
        .map(|(key, value)| StatementTerm {
            key,
            value,
            project,
            clause: None,
            from: None,
            until: clock.valid_until.map(str::to_owned),
            timezone: None,
        })
        .collect();
    let mut pending = Vec::new();
    let mut code = BTreeMap::<&str, StatementTerm<'_>>::new();
    let project_examples = (!project.is_empty()).then(|| {
        [
            format!("{project}项目的示例"),
            format!("{project}项目示例"),
            format!("{project}示例"),
        ]
    });
    if is_code_directive(&original.to_ascii_lowercase())
        || project_examples
            .iter()
            .flatten()
            .any(|marker| original.contains(marker))
    {
        for sentence in original
            .split(['。', '；', ';', '\n'])
            .map(str::trim)
            .filter(|part| !part.is_empty())
        {
            let lower_sentence = sentence.to_ascii_lowercase();
            if (is_code_directive(&lower_sentence)
                || project_examples
                    .iter()
                    .flatten()
                    .any(|marker| sentence.contains(marker)))
                && ["如果", "除非", "假设", "视情况", " if ", " unless "]
                    .iter()
                    .any(|marker| lower_sentence.contains(marker))
            {
                pending.push(json!({"key":"code_example.language","reason":"conditional_clause"}));
                continue;
            }
            let mut window = None;
            let mut has_code = false;
            for clause in sentence.split(['，', ',']).map(str::trim) {
                let lower = match project_examples
                    .iter()
                    .flatten()
                    .find_map(|name| clause.strip_prefix(name))
                {
                    Some(tail) => format!("代码示例{}", tail.to_ascii_lowercase()),
                    None => clause.to_ascii_lowercase(),
                };
                let explicit = is_code_directive(&lower);
                let qualifier = weekly_deadline(clause, project);
                let (directive, scoped_project, timing) = if let Some((scope, date, rest)) =
                    qualifier
                {
                    if rest.is_empty() {
                        window = Some(CodeWindow::Weekly(scope, date));
                        continue;
                    }
                    if project.is_empty() && !has_code && !explicit {
                        continue;
                    }
                    (
                        format!("代码示例{}", rest.to_ascii_lowercase()),
                        scope,
                        Some(CodeWindow::Weekly(scope, date)),
                    )
                } else if explicit {
                    let scoped_project = match window {
                        Some(CodeWindow::Weekly(scope, _)) => scope,
                        _ => project,
                    };
                    (lower, scoped_project, window)
                } else {
                    if let Some(start) = clause
                        .strip_prefix('从')
                        .and_then(|part| part.strip_suffix('起'))
                    {
                        window = Some(CodeWindow::From(start.trim()));
                    } else if (clause.contains('从') && clause.ends_with('起'))
                        || ["明天", "今晚", "下周"]
                            .iter()
                            .any(|marker| clause.contains(marker))
                    {
                        window = Some(CodeWindow::Unsupported);
                    } else if clause.contains("流程图") && clause.contains('用') {
                        pending
                            .push(json!({"key":"diagram.color","reason":"unsupported_dimension"}));
                    }
                    continue;
                };
                has_code = true;
                let Some(Some(value)) = code_example_language(&directive, scoped_project) else {
                    let reason = if scoped_project.is_empty() && clause.contains("项目") {
                        "project_required"
                    } else {
                        "unresolved_clause"
                    };
                    pending.push(json!({"key":"code_example.language","reason":reason}));
                    continue;
                };
                let mut term = StatementTerm {
                    key: "code_example.language",
                    value: Some(value),
                    project: scoped_project,
                    clause: Some(clause),
                    from: None,
                    until: clock.valid_until.map(str::to_owned),
                    timezone: None,
                };
                if let Some(timing) = timing {
                    let timezone = project_timezone(db, scoped_project, clock.timezone)?;
                    if let Err(reason) = resolve_window(&mut term, timing, timezone, &clock) {
                        pending.push(json!({"key":"code_example.language","reason":reason,"project":scoped_project}));
                        continue;
                    }
                    // Keep the start qualifier conservative for following directives in the same sentence.
                    if matches!(timing, CodeWindow::Weekly(_, _)) && qualifier.is_none() {
                        window = None;
                    }
                }
                add_code_term(&mut code, &mut pending, term);
            }
        }
    }
    terms.extend(code.into_values());
    Ok((terms, pending))
}

fn weekly_deadline<'a>(text: &'a str, project: &'a str) -> Option<(&'a str, u32, &'a str)> {
    let (scope, weekday) = text
        .split_once("这周")
        .or_else(|| text.split_once("本周"))?;
    let scope = scope.trim().trim_end_matches('到').trim();
    let scope = if let Some(name) = scope
        .strip_prefix("项目")
        .or_else(|| scope.strip_suffix("项目"))
    {
        name.trim()
    } else if scope.is_empty() {
        project
    } else {
        return None;
    };
    if scope.is_empty()
        || scope.len() > 200
        || (!project.is_empty() && scope != project)
        || scope
            .chars()
            .any(|c| !c.is_alphanumeric() && c != '_' && c != '-')
    {
        return None;
    }
    let mut parts = weekday.chars();
    let day = match parts.next()? {
        '一' => 0,
        '二' => 1,
        '三' => 2,
        '四' => 3,
        '五' => 4,
        '六' => 5,
        '日' | '天' => 6,
        _ => return None,
    };
    let rest = parts.as_str().strip_prefix("结束")?;
    Some((scope, day, rest.trim_start_matches('前').trim()))
}

fn resolve_window(
    term: &mut StatementTerm<'_>,
    timing: CodeWindow<'_>,
    timezone: Option<Tz>,
    clock: &StatementTime<'_>,
) -> std::result::Result<(), &'static str> {
    match timing {
        CodeWindow::Weekly(_, weekday) => {
            let zone = timezone.ok_or("timezone")?;
            let received =
                DateTime::parse_from_rfc3339(clock.known).map_err(|_| "unresolved_window")?;
            let local = received.with_timezone(&zone).date_naive();
            let days = i64::from(weekday) - i64::from(local.weekday().num_days_from_monday()) + 1;
            let boundary = local
                .checked_add_signed(Duration::days(days))
                .and_then(|date| date.and_hms_opt(0, 0, 0))
                .ok_or("unresolved_window")?;
            let until = resolve_local(zone, boundary)?;
            if term.until.as_ref().is_none_or(|prior| until < *prior) {
                term.until = Some(until);
            }
        }
        CodeWindow::From(start) => {
            let instant = if let Ok(absolute) = time(start) {
                absolute
            } else {
                let local = start
                    .rsplit_once("当地")
                    .map_or(start, |(_, value)| value)
                    .trim();
                let local = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
                    .iter()
                    .find_map(|format| NaiveDateTime::parse_from_str(local, format).ok())
                    .ok_or("unresolved_window")?;
                resolve_local(timezone.ok_or("timezone")?, local)?
            };
            term.from = Some(instant);
        }
        CodeWindow::Unsupported => return Err("unresolved_window"),
    }
    if term
        .until
        .as_deref()
        .is_some_and(|until| until <= term.from.as_deref().unwrap_or(clock.effective))
    {
        return Err("unresolved_window");
    }
    term.timezone = timezone;
    Ok(())
}

fn resolve_local(zone: Tz, local: NaiveDateTime) -> std::result::Result<String, &'static str> {
    match zone.from_local_datetime(&local) {
        LocalResult::Single(instant) => Ok(instant
            .with_timezone(&Utc)
            .to_rfc3339_opts(SecondsFormat::Nanos, true)),
        LocalResult::Ambiguous(_, _) => Err("ambiguous_local_time"),
        LocalResult::None => Err("nonexistent_local_time"),
    }
}

fn add_code_term<'a>(
    code: &mut BTreeMap<&'a str, StatementTerm<'a>>,
    pending: &mut Vec<Value>,
    term: StatementTerm<'a>,
) {
    use std::collections::btree_map::Entry;
    match code.entry(term.project) {
        Entry::Vacant(entry) => {
            entry.insert(term);
        }
        Entry::Occupied(mut entry)
            if entry.get().value.is_some()
                && (entry.get().value != term.value
                    || entry.get().from != term.from
                    || entry.get().until != term.until) =>
        {
            entry.get_mut().value = None;
            entry.get_mut().from = None;
            entry.get_mut().until = None;
            pending.push(json!({"key":"code_example.language","reason":"conflicting_values","project":term.project}));
        }
        Entry::Occupied(_) => {}
    }
}

pub fn sequence(db: &Connection) -> Result<i64> {
    Ok(
        db.query_row("SELECT seq FROM authority_changes WHERE id=1", [], |r| {
            r.get(0)
        })?,
    )
}

pub fn append(db: &mut Connection, actor: &str, key: &str, request: Value) -> Result<Value> {
    let tx = db.transaction()?;
    let result = append_in_transaction(&tx, actor, key, request)?;
    tx.commit()?;
    Ok(result)
}

pub(crate) fn append_in_transaction(
    tx: &Connection,
    actor: &str,
    key: &str,
    request: Value,
) -> Result<Value> {
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
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request)?));
    let clean = redact(original);
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
    let known = crate::clock::now()?;
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
    let valid_until = request
        .get("valid_until")
        .filter(|value| !value.is_null())
        .map(|value| {
            time(
                value
                    .as_str()
                    .ok_or_else(|| Error::invalid("valid_until must be a date string"))?,
            )
        })
        .transpose()?;
    if valid_until
        .as_deref()
        .is_some_and(|until| until <= effective.as_str())
    {
        return Err(Error::invalid("valid_until must be after valid_from"));
    }
    let timezone: Option<Tz> = request
        .get("timezone")
        .filter(|zone| !zone.is_null())
        .map(|zone| {
            zone.as_str()
                .and_then(|name| name.parse().ok())
                .ok_or_else(|| Error::invalid("statement timezone must be an IANA time zone"))
        })
        .transpose()?;
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
    let task = request["task_id"].as_str();
    let mut directive = clean.as_str();
    let project_from_task = task.map(|id| {
        tx.query_row("SELECT t.project FROM context_tasks t LEFT JOIN context_task_closures c ON c.task_id=t.id
            WHERE t.id=?1 AND c.task_id IS NULL", [id], |r| r.get::<_, Option<String>>(0))
            .optional()?.ok_or_else(|| Error::missing("open task unavailable"))
    }).transpose()?.flatten();
    let term_project = if task.is_some() {
        let from_task = project_from_task.as_deref().unwrap_or("");
        if !project.is_empty() && project != from_task {
            return Err(Error::invalid("statement project and task do not match"));
        }
        directive = ["这次任务的", "本次任务的", "本次任务", "这次任务"]
            .iter()
            .find_map(|prefix| directive.strip_prefix(prefix))
            .ok_or_else(|| {
                Error::invalid("task override requires an explicit single-task directive")
            })?;
        from_task
    } else {
        project
    };
    let (mut terms, pending) = statement_terms(
        tx,
        directive,
        term_project,
        StatementTime {
            timezone,
            effective: &effective,
            valid_until: valid_until.as_deref(),
            known: &known,
        },
    )?;
    if task.is_some() {
        terms.retain(|term| term.key == "code_example.language");
    }
    let statement = id("st");
    tx.execute(
        "INSERT INTO authority_statements(id,original,owner,project,share,effective,known,corrects_revision,corrects_attempt) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![statement,clean,actor,project,share,effective,known,corrects,corrects_attempt],
    )?;
    let seq = sequence(tx)? + 1;
    tx.execute("UPDATE authority_changes SET seq=?1 WHERE id=1", [seq])?;
    let mut revisions = Vec::new();
    for term in terms {
        let StatementTerm {
            key: dimension,
            value,
            project,
            clause,
            from,
            until,
            timezone,
        } = term;
        let term_start = from.as_deref().unwrap_or(&effective);
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
        let prev = tx.query_row("SELECT r.id FROM authority_revisions r JOIN authority_statements s ON s.id=r.statement_id
            LEFT JOIN authority_revision_terms t ON t.revision_id=r.id
            WHERE fact_id=?1 AND COALESCE(t.valid_from,s.effective)<=?2 AND t.task_id IS ?3
            ORDER BY COALESCE(t.valid_from,s.effective) DESC, r.seq DESC LIMIT 1",
            params![fact,term_start,task],|r|r.get::<_,String>(0)).optional()?;
        let state = if value.is_none() {
            "pending"
        } else if term_start > known.as_str() {
            "scheduled"
        } else {
            "active"
        };
        let revision = id("r");
        tx.execute("INSERT INTO authority_revisions(id,statement_id,fact_id,value,previous,seq,state) VALUES(?1,?2,?3,?4,?5,?6,?7)",
            params![revision,statement,fact,value,prev,seq,state])?;
        if clause.is_some() || from.is_some() || until.is_some() || task.is_some() {
            tx.execute(
                "INSERT INTO authority_revision_terms(revision_id,clause,valid_until,timezone,task_id,valid_from) VALUES(?1,?2,?3,?4,?5,?6)",
                params![revision,clause,until,timezone.map(|zone| zone.name()),task,from],
            )?;
        }
        revisions.push(json!({"fact_id":fact,"revision_id":revision,"key":dimension,"status":state,"value":value,
            "supersedes":prev,"valid_from":term_start,"valid_until":until,"known_from":known}));
    }
    for (ordinal, item) in pending.iter().enumerate() {
        tx.execute(
            "INSERT INTO authority_pending_terms VALUES(?1,?2,?3,?4)",
            params![
                statement,
                ordinal as i64,
                item["key"].as_str(),
                item["reason"].as_str()
            ],
        )?;
    }
    let result = json!({"statement_id":statement,"dimensions":revisions,"pending":pending,
        "status":if revisions.is_empty() || revisions.iter().all(|r| r["value"].is_null()) {"pending"} else {"recorded"},"change_seq":seq});
    tx.execute(
        "INSERT INTO authority_intents VALUES(?1,?2,?3,?4,?5)",
        params![actor, key, digest, result.to_string(), statement],
    )?;
    Ok(result)
}

pub fn statement_detail(db: &Connection, statement: &str) -> Result<Value> {
    let original: String = db
        .query_row(
            "SELECT original FROM authority_statements WHERE id=?1 AND revoked=0",
            [statement],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| Error::missing("statement is unavailable"))?;
    let mut query = db.prepare(
        "SELECT key,reason FROM authority_pending_terms WHERE statement_id=?1 ORDER BY ordinal",
    )?;
    let pending = query
        .query_map([statement], |row| {
            Ok(json!({"key":row.get::<_,String>(0)?,"reason":row.get::<_,String>(1)?}))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let needs_time_clarification = pending.iter().any(|entry| {
        matches!(
            entry["reason"].as_str(),
            Some(
                "ambiguous_local_time"
                    | "nonexistent_local_time"
                    | "unresolved_window"
                    | "timezone"
            )
        )
    });
    let now = crate::clock::now()?;
    let active_project_revision_count: i64 = db.query_row(
        "SELECT COUNT(*) FROM authority_revisions r JOIN authority_facts f ON f.id=r.fact_id
         JOIN authority_statements s ON s.id=r.statement_id
         LEFT JOIN authority_revision_terms t ON t.revision_id=r.id
         LEFT JOIN context_task_closures c ON c.task_id=t.task_id
         WHERE r.statement_id=?1 AND f.project<>'' AND r.value IS NOT NULL AND s.revoked=0
            AND s.known<=?2 AND COALESCE(t.valid_from,s.effective)<=?2 AND (t.valid_until IS NULL OR t.valid_until>?2)
            AND (c.closed_at IS NULL OR c.closed_at>?2)",
        params![statement, now],
        |row| row.get(0),
    )?;
    Ok(
        json!({"statement_id":statement,"original":original,"pending":pending,
        "needs_time_clarification":needs_time_clarification,"active_project_revision_count":active_project_revision_count}),
    )
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
    current_with_task(db, project, valid_at, known_at, shared_only, None)
}

pub fn current_with_task(
    db: &Connection,
    project: Option<&str>,
    valid_at: Option<&str>,
    known_at: Option<&str>,
    shared_only: bool,
    task: Option<&str>,
) -> Result<Value> {
    let now = crate::clock::now()?;
    let valid = valid_at
        .map(time)
        .transpose()?
        .unwrap_or_else(|| now.clone());
    let known = known_at.map(time).transpose()?.unwrap_or(now);
    let mut query=db.prepare("
      SELECT r.id,f.id,f.dimension,f.project,r.value,s.share,s.revoked,s.id,COALESCE(t.valid_from,s.effective),s.known,s.original,
             t.valid_until,t.timezone,t.clause,t.task_id
      FROM authority_facts f JOIN authority_revisions r ON r.fact_id=f.id JOIN authority_statements s ON s.id=r.statement_id
      LEFT JOIN authority_revision_terms t ON t.revision_id=r.id
      LEFT JOIN context_task_closures c ON c.task_id=t.task_id
      WHERE (f.project='' OR f.project=?1) AND COALESCE(t.valid_from,s.effective)<=?2 AND s.known<=?3
        AND (t.valid_until IS NULL OR t.valid_until>?2)
        AND (t.task_id IS NULL OR (t.task_id=?4 AND
             (c.closed_at IS NULL OR c.closed_at>?2 OR c.closed_at>?3)))
      ORDER BY t.task_id IS NOT NULL DESC,f.project DESC,COALESCE(t.valid_from,s.effective) DESC,r.seq DESC")?;
    let rows = query.query_map(params![project.unwrap_or(""), valid, known, task], |r| {
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
            r.get::<_, Option<String>>(11)?,
            r.get::<_, Option<String>>(12)?,
            r.get::<_, Option<String>>(13)?,
            r.get::<_, Option<String>>(14)?,
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
            valid_until,
            timezone,
            clause,
            task_scope,
        ) = row?;
        items.entry(key.clone()).or_insert_with(|| {
            if revoked || value.is_none() || (shared_only && share!="authorized_assistants") { return None; }
            let scope = match task_scope { Some(task) => json!({"kind":"task","task_id":task,"project":project}),
                None => scope(&project) };
            let mut item=json!({"revision_id":revision,"fact_id":fact,"key":key,"value":value,"scope":scope,"status":"active","valid_from":effective,"known_from":known});
            if !shared_only {
                item["statement_id"]=json!(statement); item["evidence"]=json!(original); item["share"]=json!(share);
                if let Some(valid_until) = valid_until { item["valid_until"] = json!(valid_until); }
                if let Some(timezone) = timezone { item["timezone"] = json!(timezone); }
                if let Some(clause) = clause { item["clause"] = json!(clause); }
            }
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
        "SELECT r.id,r.value,r.previous,s.known,COALESCE(t.valid_from,s.effective),s.original,s.id,r.state,t.valid_until,t.timezone
      FROM authority_revisions r JOIN authority_statements s ON s.id=r.statement_id
      LEFT JOIN authority_revision_terms t ON t.revision_id=r.id
      WHERE r.fact_id=?1 AND s.revoked=0 ORDER BY r.seq DESC",
    )?;
    let results=query.query_map([fact_id],|r| Ok(json!({
        "revision_id":r.get::<_,String>(0)?, "value":r.get::<_,Option<String>>(1)?,
        "supersedes":r.get::<_,Option<String>>(2)?, "known_from":r.get::<_,String>(3)?,
        "valid_from":r.get::<_,String>(4)?,"original":r.get::<_,Option<String>>(5)?,"statement_id":r.get::<_,String>(6)?,"status":r.get::<_,String>(7)?,
        "valid_until":r.get::<_,Option<String>>(8)?,"timezone":r.get::<_,Option<String>>(9)?
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
        "UPDATE authority_revision_terms SET clause=NULL WHERE revision_id IN
           (SELECT id FROM authority_revisions WHERE statement_id=?1)",
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
    let project = task
        .map(|id| {
            db.query_row(
                "SELECT project FROM context_tasks WHERE id=?1",
                [id],
                |row| row.get::<_, Option<String>>(0),
            )
            .optional()
        })
        .transpose()?
        .flatten()
        .flatten();
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
        let code_language = project.as_deref().and_then(|project| {
            let phrase = span["value"].as_str()?.trim();
            let normalized = phrase.replace("项目示例", "项目代码示例");
            code_example_language(&normalized.to_ascii_lowercase(), project).flatten()
        });
        let cited = candidates.iter().any(|candidate| {
            candidate["support"]
                .as_array()
                .is_some_and(|refs| refs.iter().any(|id| id == span_id))
                && (role == "tool_result"
                    || role == "user_message" && (explicit_plan || code_language.is_some()))
        });
        if !cited {
            continue;
        }
        let outcome = match role {
            "tool_result" if field.starts_with("/checks/") => "tool_result",
            "user_message" if field == "/text" && explicit_plan => "plan",
            "user_message" if field == "/text" && code_language.is_some() => "preference_evidence",
            _ => continue,
        };
        written += db.execute("INSERT OR IGNORE INTO authority_observations(event_id,span_id,task_id,role,field,value)
          VALUES(?1,?2,?3,?4,?5,?6)",params![event,span_id,task,outcome,field,span["value"].to_string()])?;
    }
    Ok(written)
}
