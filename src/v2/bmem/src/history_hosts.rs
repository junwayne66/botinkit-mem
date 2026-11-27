use crate::error::{Error, Result};
use chrono::{DateTime, Utc};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

const MAX_RECORD_BYTES: u64 = 1024 * 1024;
const MAX_PAGE_BYTES: u64 = 4 * MAX_RECORD_BYTES;
const MAX_PAGE_RECORDS: usize = 512;
const MAX_TEXT_BYTES: usize = 12_000;

pub struct HistoryPage {
    pub offset: u64,
    pub events: Vec<Value>,
    pub eof: bool,
}

#[derive(Clone, Copy)]
enum Host {
    Codex,
    Claude,
    Dsh,
}

impl Host {
    fn parse(name: &str) -> Result<Self> {
        match name {
            "codex" => Ok(Self::Codex),
            "claude-code" => Ok(Self::Claude),
            "dsh" => Ok(Self::Dsh),
            _ => Err(Error::invalid(
                "history sync requires a supported local assistant",
            )),
        }
    }

    fn home(self) -> Result<PathBuf> {
        let (variable, directory) = match self {
            Self::Codex => ("CODEX_HOME", ".codex"),
            Self::Claude => ("CLAUDE_CONFIG_DIR", ".claude"),
            Self::Dsh => ("DSH_HOME", ".dsh"),
        };
        if let Some(root) = std::env::var_os("BMEM_CONFIG_ROOT") {
            return Ok(PathBuf::from(root).join(directory));
        }
        if let Some(root) = std::env::var_os(variable).filter(|value| !value.is_empty()) {
            return Ok(PathBuf::from(root));
        }
        Ok(dirs::home_dir()
            .ok_or_else(|| Error::unavailable("user home is unavailable"))?
            .join(directory))
    }
}

pub fn files(host: &str) -> Result<Vec<PathBuf>> {
    let host = Host::parse(host)?;
    let home = host.home()?;
    let Some(metadata) = directory_metadata(&home)? else {
        return Ok(Vec::new());
    };
    if !metadata.is_dir() {
        return Ok(Vec::new());
    }
    let roots: &[&str] = match host {
        Host::Codex => &["sessions", "archived_sessions", "rollouts"],
        Host::Claude => &["projects"],
        Host::Dsh => &[""],
    };
    let mut pending: Vec<_> = roots.iter().map(|root| home.join(root)).collect();
    let mut found = Vec::new();
    while let Some(path) = pending.pop() {
        let Some(metadata) = directory_metadata(&path)? else {
            continue;
        };
        if metadata.is_dir() {
            let entries = match fs::read_dir(&path) {
                Ok(entries) => entries,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            for entry in entries {
                pending.push(entry?.path());
            }
        } else if metadata.is_file()
            && path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with(".jsonl") || name.ends_with(".jsonl.zstd"))
        {
            found.push(path);
        }
    }
    found.sort_unstable();
    Ok(found)
}

fn directory_metadata(path: &Path) -> Result<Option<fs::Metadata>> {
    match path.symlink_metadata() {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub fn read_page(host: &str, path: &Path, offset: u64, max_events: usize) -> Result<HistoryPage> {
    let adapter = Host::parse(host)?;
    let metadata = path.symlink_metadata().map_err(transcript_io)?;
    if !metadata.is_file() {
        return Err(Error::invalid(
            "history path must be a regular transcript file",
        ));
    }
    let file_id = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| Error::invalid("history file name must be Unicode"))?;
    let compressed = file_id.ends_with(".jsonl.zstd");
    let file_id = file_id.trim_end_matches(".zstd").trim_end_matches(".jsonl");
    let fallback_time = metadata
        .modified()
        .map(DateTime::<Utc>::from)
        .unwrap_or_default();
    let (mut reader, mut cursor, mut discarding) = open_at(path, compressed, offset)?;
    let prefix = inspect_prefix(adapter, path, compressed, file_id)?;
    let mut line = Vec::new();
    let max_events = max_events.clamp(1, 100);
    let mut events = Vec::with_capacity(max_events);
    let mut eof = false;
    let start = cursor;
    // Codex embeds images in the same JSON line as their accompanying prompt.
    let record_limit = if matches!(adapter, Host::Codex) {
        MAX_PAGE_BYTES
    } else {
        MAX_RECORD_BYTES
    };
    for _ in 0..MAX_PAGE_RECORDS {
        if cursor - start >= MAX_PAGE_BYTES || events.len() == max_events {
            break;
        }
        line.clear();
        let available = record_limit.min(MAX_PAGE_BYTES - (cursor - start));
        let count = reader
            .by_ref()
            .take(available)
            .read_until(b'\n', &mut line)? as u64;
        if count == 0 {
            eof = true;
            break;
        }
        let complete = line.last() == Some(&b'\n');
        if !complete && count < available {
            eof = true;
            break;
        }
        if !complete && available < record_limit && !discarding {
            break; // Read this record from its start with a fresh page budget.
        }
        if complete
            && !discarding
            && let Ok(record) = serde_json::from_slice::<Value>(&line)
            && (count <= MAX_RECORD_BYTES || codex_image_prompt(&record))
            && let Some(event) = event_for(adapter, host, &prefix, cursor, &record, fallback_time)
        {
            events.push(event);
        }
        cursor += count;
        discarding = !complete;
    }
    Ok(HistoryPage {
        offset: cursor,
        events,
        eof,
    })
}

fn codex_image_prompt(record: &Value) -> bool {
    let payload = &record["payload"];
    record["type"] == "response_item"
        && payload["type"] == "message"
        && payload["role"] == "user"
        && payload["content"]
            .as_array()
            .is_some_and(|parts| parts.iter().any(|part| part["type"] == "input_image"))
}

fn transcript_io(error: io::Error) -> Error {
    if error.kind() == io::ErrorKind::NotFound {
        return Error::missing("history transcript is no longer available");
    }
    error.into()
}

fn open_at(path: &Path, compressed: bool, pos: u64) -> Result<(Box<dyn BufRead>, u64, bool)> {
    let mut file = File::open(path).map_err(transcript_io)?;
    if !compressed && pos > file.metadata()?.len() {
        return Ok((Box::new(BufReader::new(file)), 0, false));
    }
    if !compressed && pos > 0 {
        file.seek(SeekFrom::Start(pos - 1))?;
    }
    let mut reader: Box<dyn BufRead> = if compressed {
        Box::new(BufReader::new(zstd::stream::read::Decoder::new(file)?))
    } else {
        Box::new(BufReader::new(file))
    };
    if pos == 0 {
        return Ok((reader, pos, false));
    }
    if compressed && io::copy(&mut reader.by_ref().take(pos - 1), &mut io::sink())? != pos - 1 {
        return open_at(path, compressed, 0);
    }
    let mut before = [0];
    if reader.read(&mut before)? == 0 {
        return open_at(path, compressed, 0);
    }
    // A page may end while streaming past an oversized record.
    Ok((reader, pos, before[0] != b'\n'))
}

struct Prefix {
    session: String,
    codex_user_events: bool,
    codex_assistant_events: bool,
}

fn inspect_prefix(host: Host, path: &Path, compressed: bool, file_id: &str) -> Result<Prefix> {
    let mut context = Prefix {
        session: file_id.to_owned(),
        codex_user_events: false,
        codex_assistant_events: false,
    };
    if matches!(host, Host::Claude) {
        return Ok(context);
    }
    let (reader, _, _) = open_at(path, compressed, 0)?;
    let mut head = BufReader::new(reader.take(2 * MAX_RECORD_BYTES));
    let mut line = Vec::new();
    for _ in 0..128 {
        line.clear();
        if head.read_until(b'\n', &mut line)? == 0 {
            break;
        }
        let Ok(record) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        match (host, record["type"].as_str()) {
            (Host::Codex, Some("session_meta")) => {
                if let Some(session) = first_string(&record["payload"], &["id", "session_id"]) {
                    context.session = session.to_owned();
                }
            }
            (Host::Codex, Some("event_msg")) => match record["payload"]["type"].as_str() {
                Some("user_message") => context.codex_user_events = true,
                Some("agent_message") => context.codex_assistant_events = true,
                _ => {}
            },
            (Host::Dsh, Some("session")) => {
                if let Some(session) = first_string(&record, &["id", "session_id"]) {
                    context.session = session.to_owned();
                }
                break;
            }
            _ => {}
        }
        if context.codex_user_events && context.codex_assistant_events {
            break;
        }
    }
    Ok(context)
}

fn event_for(
    adapter: Host,
    host: &str,
    prefix: &Prefix,
    offset: u64,
    record: &Value,
    fallback_time: DateTime<Utc>,
) -> Option<Value> {
    let fallback_id = format!("history-byte:{offset}");
    let session = match adapter {
        Host::Claude => {
            first_string(record, &["sessionId", "session_id"]).unwrap_or(&prefix.session)
        }
        _ => &prefix.session,
    };
    let (kind, native, body) = match adapter {
        Host::Codex => codex_event(prefix, record, &fallback_id)?,
        Host::Claude => claude_event(record, &fallback_id)?,
        Host::Dsh => dsh_event(record)?,
    };
    let identity = json!([host, session, kind, native]);
    let native_object = format!("host_{:x}", Sha256::digest(identity.to_string().as_bytes()));
    let mut event = json!({
        "native_object": native_object,
        "native_version": "1",
        "actor_kind": kind,
        "at": timestamp(record, fallback_time),
    });
    if matches!(adapter, Host::Claude)
        && kind == "user_message"
        && first_string(record, &["promptId", "prompt_id"]).is_some()
        && let Some(uuid) = first_string(record, &["uuid"])
    {
        event["transcript_message_id"] = json!(uuid);
    }
    let field = if kind == "tool_result" {
        "checks"
    } else {
        "text"
    };
    event[field] = body;
    Some(event)
}

fn codex_event(
    prefix: &Prefix,
    record: &Value,
    fallback: &str,
) -> Option<(&'static str, String, Value)> {
    let payload = &record["payload"];
    let row_type = record["type"].as_str()?;
    let item_type = payload["type"].as_str()?;
    let text_kind = match (row_type, item_type, payload["role"].as_str()) {
        ("event_msg", "user_message", _) if prefix.codex_user_events => Some("user_message"),
        ("event_msg", "agent_message", _) if prefix.codex_assistant_events => {
            Some("assistant_message")
        }
        ("response_item", "message", Some("user")) if !prefix.codex_user_events => {
            Some("user_message")
        }
        ("response_item", "message", Some("assistant")) if !prefix.codex_assistant_events => {
            Some("assistant_message")
        }
        _ => None,
    };
    if let Some(kind) = text_kind {
        let content = payload.get("message").unwrap_or(&payload["content"]);
        let body = visible_text(content, false, MAX_TEXT_BYTES)?;
        let native = if kind == "user_message" {
            first_string(payload, &["turn_id", "turnId", "id"])
        } else {
            first_string(payload, &["id"])
        };
        return Some((kind, native.unwrap_or(fallback).to_owned(), json!(body)));
    }
    if row_type != "response_item"
        || !matches!(
            item_type,
            "function_call_output" | "custom_tool_call_output"
        )
    {
        return None;
    }
    let output = tool_result(&payload["output"], MAX_TEXT_BYTES)?;
    let native = first_string(payload, &["call_id", "id"]).unwrap_or(fallback);
    Some(("tool_result", native.to_owned(), json!({"output":output})))
}

fn claude_event(record: &Value, fallback: &str) -> Option<(&'static str, String, Value)> {
    let native_type = record["type"].as_str()?;
    if record["isMeta"] == true
        || record["isCompactSummary"] == true
        || record["isSidechain"] == true
        || !matches!(native_type, "user" | "assistant")
    {
        return None;
    }
    let message = &record["message"];
    let role = message["role"].as_str().unwrap_or(native_type);
    let content = &message["content"];
    if role == "user"
        && let Some(parts) = content.as_array()
    {
        let tools: Vec<_> = parts
            .iter()
            .filter(|item| item["type"] == "tool_result")
            .take(8)
            .collect();
        if !tools.is_empty() {
            let mut results = Map::new();
            for (index, item) in tools.iter().enumerate() {
                if let Some(output) = tool_result(&item["content"], MAX_TEXT_BYTES / tools.len()) {
                    let key = if tools.len() == 1 {
                        "output".to_owned()
                    } else {
                        format!("output_{index}")
                    };
                    results.insert(
                        key,
                        json!({"result":output,"is_error":item["is_error"] == true}),
                    );
                }
            }
            if results.is_empty() {
                return None;
            }
            let native = if tools.len() == 1 {
                first_string(tools[0], &["tool_use_id"])
            } else {
                None
            };
            let native = native
                .or_else(|| first_string(record, &["uuid"]))
                .unwrap_or(fallback);
            return Some(("tool_result", native.to_owned(), Value::Object(results)));
        }
    }
    let kind = match role {
        "user" => "user_message",
        "assistant" => "assistant_message",
        _ => return None,
    };
    let native = first_string(record, &["promptId", "prompt_id", "uuid"]).unwrap_or(fallback);
    let text = visible_text(content, false, MAX_TEXT_BYTES)?;
    let text = if role == "user" {
        if record["origin"]["kind"] == "task-notification" {
            return None;
        }
        claude_prompt_text(&text)
    } else {
        &text
    };
    (!text.is_empty()).then(|| (kind, native.to_owned(), json!(text)))
}

/// Claude attaches IDE context to real prompts and also emits standalone UI
/// notices in the user role. Only the text following those notices is a prompt.
pub(crate) fn claude_prompt_text(mut text: &str) -> &str {
    loop {
        text = text.trim();
        let Some((open, close)) = [
            ("<ide_opened_file>", "</ide_opened_file>"),
            ("<local-command-stdout>", "</local-command-stdout>"),
            ("<local-command-stderr>", "</local-command-stderr>"),
            ("<task-notification>", "</task-notification>"),
        ]
        .into_iter()
        .find(|(open, _)| text.starts_with(open)) else {
            return match text {
                "[Request interrupted by user]" | "[Request interrupted by user for tool use]" => {
                    ""
                }
                prompt => prompt,
            };
        };
        text = text[open.len()..]
            .split_once(close)
            .map_or("", |(_, after)| after);
    }
}

pub(crate) fn matches_claude_prompt(previous: &str, incoming: &str) -> bool {
    let previous = claude_prompt_text(previous);
    previous == incoming
        || incoming
            .strip_suffix("\n[bmem truncated]")
            .is_some_and(|prefix| previous.starts_with(prefix))
        || previous
            .strip_suffix("\n[bmem truncated]")
            .is_some_and(|prefix| incoming.starts_with(prefix))
}

fn dsh_event(record: &Value) -> Option<(&'static str, String, Value)> {
    let kind = match record["type"].as_str()? {
        "user/message" => "user_message",
        "assistant/message" => "assistant_message",
        "tool/result" => "tool_result",
        _ => return None,
    };
    let message = if kind == "user_message" {
        &record["data"]
    } else {
        &record["data"]["message"]
    };
    let source = message["source"]["kind"].as_str().unwrap_or("");
    if !source.is_empty()
        && !matches!(
            (kind, source),
            ("user_message", "user" | "user-rpc")
                | ("assistant_message", "model")
                | ("tool_result", "tool")
        )
    {
        return None;
    }
    let body = visible_text(&message["content"], kind == "tool_result", MAX_TEXT_BYTES)?;
    let native = first_string(message, &["id"])
        .map(str::to_owned)
        .or_else(|| record["seq"].as_i64().map(|seq| seq.to_string()))
        .or_else(|| record["seq"].as_str().map(str::to_owned))
        .unwrap_or_else(|| format!("{:x}", Sha256::digest(body.as_bytes())));
    let value = if kind == "tool_result" {
        json!({"output":body})
    } else {
        json!(body)
    };
    Some((kind, native, value))
}

fn first_string<'a>(record: &'a Value, keys: &[&str]) -> Option<&'a str> {
    keys.iter()
        .find_map(|key| record[*key].as_str().filter(|value| !value.is_empty()))
}

fn timestamp(record: &Value, fallback: DateTime<Utc>) -> String {
    ["timestamp", "time", "ts"]
        .iter()
        .find_map(|key| {
            let value = &record[*key];
            if let Some(text) = value.as_str() {
                return DateTime::parse_from_rfc3339(text)
                    .ok()
                    .map(|time| time.with_timezone(&Utc));
            }
            let number = value.as_i64()?;
            if number.unsigned_abs() > 100_000_000_000 {
                DateTime::from_timestamp_millis(number)
            } else {
                DateTime::from_timestamp(number, 0)
            }
        })
        .unwrap_or(fallback)
        .to_rfc3339()
}

fn visible_text(content: &Value, tools: bool, limit: usize) -> Option<String> {
    let mut joined = String::new();
    match content {
        Value::String(text) => append_visible(&mut joined, text, limit),
        Value::Array(blocks) => {
            for block in blocks {
                if joined.len() >= limit {
                    break;
                }
                if matches!(
                    block["type"].as_str(),
                    Some("text" | "input_text" | "output_text")
                ) {
                    if let Some(text) = block["text"].as_str() {
                        append_visible(&mut joined, text, limit);
                    }
                } else if tools
                    && matches!(
                        block["type"].as_str(),
                        Some("tool_result" | "tool-output" | "tool_output")
                    )
                    && let Some(text) = visible_text(
                        block.get("content").unwrap_or(&block["output"]),
                        false,
                        limit.saturating_sub(joined.len()),
                    )
                {
                    append_visible(&mut joined, &text, limit);
                }
            }
        }
        _ => {}
    }
    (!joined.is_empty()).then_some(joined)
}

fn append_visible(out: &mut String, text: &str, limit: usize) {
    let text = text
        .split_once("[bmem-injection:")
        .map_or(text, |(before, _)| before)
        .trim();
    if text.is_empty()
        || [
            "<system-reminder",
            "<environment_context",
            "<user_instructions",
            "# AGENTS.md instructions",
            "<local-command-caveat",
            "<command-name",
        ]
        .iter()
        .any(|marker| text.starts_with(marker))
    {
        return;
    }
    let separator = usize::from(!out.is_empty());
    let room = limit.saturating_sub(out.len() + separator);
    if room == 0 {
        return;
    }
    if separator != 0 {
        out.push('\n');
    }
    if text.len() > room {
        let suffix = "\n[bmem truncated]";
        if room > suffix.len() {
            out.push_str(&text[..text.floor_char_boundary(room - suffix.len())]);
            out.push_str(suffix);
        }
    } else {
        out.push_str(text);
    }
}

fn tool_result(output: &Value, limit: usize) -> Option<Value> {
    if output.is_string() || output.is_array() {
        return visible_text(output, true, limit).map(Value::String);
    }
    output.as_object()?;
    match output["type"].as_str() {
        Some("text" | "input_text" | "output_text") => {
            return visible_text(&output["text"], false, limit).map(Value::String);
        }
        Some("tool_result" | "tool-output" | "tool_output") => {
            let content = output.get("content").unwrap_or(&output["output"]);
            return visible_text(content, false, limit).map(Value::String);
        }
        _ => {}
    }
    // A structured tool receipt is useful as evidence without unrelated call metadata.
    let mut result = Map::new();
    let mut remaining = limit;
    for key in [
        "exit_code",
        "status",
        "passed",
        "success",
        "output",
        "stdout",
        "stderr",
        "text",
        "content",
    ] {
        match &output[key] {
            Value::Number(_) | Value::Bool(_) => {
                result.insert(key.to_owned(), output[key].clone());
            }
            value => {
                if let Some(text) = visible_text(value, true, remaining) {
                    remaining = remaining.saturating_sub(text.len());
                    result.insert(key.to_owned(), Value::String(text));
                }
            }
        }
    }
    (!result.is_empty()).then_some(Value::Object(result))
}

// ---------------------------------------------------------------------------
// Selective history import (T036)
// ---------------------------------------------------------------------------

const SESSION_PAGE_SIZE: usize = 50;
const IMPORT_PAGE_EVENTS: usize = 100;

/// List local assistant sessions as import targets. `account_or_host` is the
/// host name (`codex`, `claude-code`, `dsh`) and `kind` must be `sessions`.
/// Targets carry a stable opaque id (a hash of the transcript path, never the
/// path itself), a readable session label, and an optional project folder name.
/// `query` filters label/project case-insensitively; `cursor` is the opaque
/// index returned as `next_cursor`. Filesystem paths are never exposed.
///
/// Returns `{"targets":[{"id","label","project"?}], "next_cursor":<string|null>}`.
pub fn options(
    account_or_host: &str,
    kind: &str,
    query: Option<&str>,
    cursor: Option<&str>,
    stop: &impl Fn() -> bool,
) -> Result<Value> {
    if kind != "sessions" {
        return Err(Error::invalid("本机历史导入仅支持 sessions"));
    }
    let adapter = Host::parse(account_or_host)?;
    let paths = files(account_or_host)?;
    let start = cursor
        .and_then(|c| c.parse::<usize>().ok())
        .unwrap_or(0)
        .min(paths.len());
    let end = (start + SESSION_PAGE_SIZE).min(paths.len());
    let needle = query
        .map(|q| q.trim().to_lowercase())
        .filter(|q| !q.is_empty());
    let mut targets = Vec::new();
    for path in &paths[start..end] {
        if stop() {
            return Err(Error::unavailable("历史导入已暂停"));
        }
        let (label, project) = describe_session(adapter, path);
        if let Some(n) = &needle {
            let haystack =
                format!("{label} {}", project.clone().unwrap_or_default()).to_lowercase();
            if !haystack.contains(n) {
                continue;
            }
        }
        let mut target = json!({"id": target_id(path), "label": label});
        if let Some(project) = project {
            target["project"] = json!(project);
        }
        targets.push(target);
    }
    let next_cursor = if end < paths.len() {
        json!(end.to_string())
    } else {
        Value::Null
    };
    Ok(json!({"targets": targets, "next_cursor": next_cursor}))
}

/// Read one page of importable events from a local session. `target` must be an
/// opaque id previously returned by [`options`] — it is resolved against the
/// current transcript set, so an arbitrary path can never be read. `from`/`to`
/// bound the event time and `content` is an optional case-insensitive keyword
/// filter over text and tool receipts; filtering never changes the native dedup
/// ids. `cursor` is the byte offset returned as `next_cursor`. Returns
/// `(events, next_cursor)`; `None` means the transcript is exhausted.
pub fn read_import_page(
    account_or_host: &str,
    kind: &str,
    target: &str,
    window: (Option<&str>, Option<&str>),
    content: &str,
    cursor: Option<&Value>,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Option<Value>)> {
    let (from, to) = window;
    if kind != "sessions" {
        return Err(Error::invalid("本机历史导入仅支持 sessions"));
    }
    Host::parse(account_or_host)?;
    let path = files(account_or_host)?
        .into_iter()
        .find(|path| target_id(path) == target)
        .ok_or_else(|| Error::invalid("会话目标无效或已不可用"))?;
    if stop() {
        return Err(Error::unavailable("历史导入已暂停"));
    }
    let from = from.map(parse_time_bound).transpose()?;
    let to = to.map(parse_time_bound).transpose()?;
    let offset = cursor.and_then(Value::as_u64).unwrap_or(0);
    let page = read_page(account_or_host, &path, offset, IMPORT_PAGE_EVENTS)?;
    let events = page
        .events
        .into_iter()
        .filter(|event| {
            in_window(event, from, to) && (content == "all" || event["actor_kind"] != "tool_result")
        })
        .collect::<Vec<_>>();
    let next = if page.eof {
        None
    } else {
        Some(json!(page.offset))
    };
    Ok((events, next))
}

pub fn discover_sessions(
    host: &str,
    stop: &impl Fn() -> bool,
) -> Result<Vec<(String, Option<Value>)>> {
    let mut targets = Vec::new();
    let home = Host::parse(host)?.home()?;
    for path in files(host)? {
        if stop() {
            return Err(Error::unavailable("历史导入已暂停"));
        }
        let relative = path
            .strip_prefix(&home)
            .ok()
            .and_then(Path::to_str)
            .ok_or_else(|| Error::invalid("本机会话路径无效"))?;
        targets.push((target_id(&path), Some(json!({"file":relative,"offset":0}))));
    }
    Ok(targets)
}

pub fn read_discovered_page(
    host: &str,
    cursor: Option<&Value>,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Option<Value>)> {
    let cursor = cursor.ok_or_else(|| Error::internal("历史会话缺少检查点"))?;
    let file = cursor["file"]
        .as_str()
        .ok_or_else(|| Error::internal("历史会话路径无效"))?;
    let offset = cursor["offset"]
        .as_u64()
        .ok_or_else(|| Error::internal("历史会话偏移无效"))?;
    let invalid = || Error::missing("本机会话文件已移动或不可读");
    let mut path = Host::parse(host)?.home()?;
    for component in Path::new(file).components() {
        let std::path::Component::Normal(name) = component else {
            return Err(invalid());
        };
        path.push(name);
        let metadata = directory_metadata(&path)?.ok_or_else(invalid)?;
        if metadata.file_type().is_symlink() {
            return Err(invalid());
        }
    }
    if stop() {
        return Err(Error::unavailable("历史导入已暂停"));
    }
    let page = read_page(host, &path, offset, IMPORT_PAGE_EVENTS)?;
    let checkpoint = (!page.eof).then(|| json!({"file":file,"offset":page.offset}));
    Ok((page.events, checkpoint))
}

/// Stable opaque target id for a transcript path. The id reveals nothing about
/// the filesystem and is only ever resolved by matching against [`files`].
fn target_id(path: &Path) -> String {
    format!(
        "sess_{:x}",
        Sha256::digest(path.to_string_lossy().as_bytes())
    )
}

/// Read a small prefix of a transcript to derive a readable session label and
/// an optional project folder name (the last component of the working directory,
/// never the full path). Falls back to the file stem when the head is unreadable.
fn describe_session(adapter: Host, path: &Path) -> (String, Option<String>) {
    let stem = path
        .file_stem()
        .and_then(|name| name.to_str())
        .unwrap_or("session");
    let fallback = stem.trim_end_matches(".jsonl").to_owned();
    let compressed = path.to_string_lossy().ends_with(".jsonl.zstd");
    let Ok((reader, _, _)) = open_at(path, compressed, 0) else {
        return (fallback, None);
    };
    let mut head = BufReader::new(reader.take(256 * 1024));
    let mut line = Vec::new();
    let mut label = None;
    let mut cwd = None;
    for _ in 0..6 {
        line.clear();
        if head.read_until(b'\n', &mut line).unwrap_or(0) == 0 {
            break;
        }
        let Ok(record) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        match adapter {
            Host::Codex => {
                if record["type"] == "session_meta" {
                    label =
                        first_string(&record["payload"], &["id", "session_id"]).map(str::to_owned);
                    cwd = first_string(&record["payload"], &["cwd"]).map(str::to_owned);
                }
            }
            Host::Claude => {
                if label.is_none() {
                    label = first_string(&record, &["sessionId", "session_id"]).map(str::to_owned);
                }
                if cwd.is_none() {
                    cwd = first_string(&record, &["cwd"]).map(str::to_owned);
                }
            }
            Host::Dsh => {
                if record["type"] == "session" {
                    label = first_string(&record, &["id", "session_id"]).map(str::to_owned);
                    cwd = first_string(&record, &["cwd"]).map(str::to_owned);
                }
            }
        }
        if label.is_some() && cwd.is_some() {
            break;
        }
    }
    (
        label.unwrap_or(fallback),
        cwd.as_deref().and_then(project_name),
    )
}

/// The readable project folder name from a working directory: its last non-empty
/// component, rejecting anything that still looks like a path or a hidden dir so
/// no full filesystem path leaks into a target label.
fn project_name(cwd: &str) -> Option<String> {
    let name = cwd.rsplit(['/', '\\']).find(|part| !part.is_empty())?;
    if name == "." || name == ".." || name.starts_with('.') || name.contains(['/', '\\']) {
        return None;
    }
    Some(name.to_owned())
}

fn parse_time_bound(value: &str) -> Result<DateTime<Utc>> {
    let value = value.trim();
    if let Ok(time) = DateTime::parse_from_rfc3339(value) {
        return Ok(time.with_timezone(&Utc));
    }
    if let Ok(number) = value.parse::<i64>() {
        let time = if number.unsigned_abs() > 100_000_000_000 {
            DateTime::from_timestamp_millis(number)
        } else {
            DateTime::from_timestamp(number, 0)
        };
        if let Some(time) = time {
            return Ok(time);
        }
    }
    Err(Error::invalid("历史时间窗口格式无效"))
}

fn in_window(event: &Value, from: Option<DateTime<Utc>>, to: Option<DateTime<Utc>>) -> bool {
    let Some(at) = event["at"]
        .as_str()
        .and_then(|at| DateTime::parse_from_rfc3339(at).ok())
    else {
        return true;
    };
    let at = at.with_timezone(&Utc);
    if let Some(from) = from
        && at < from
    {
        return false;
    }
    if let Some(to) = to
        && at >= to
    {
        return false;
    }
    true
}

#[cfg(test)]
#[path = "../../../../tests/v2/history_hosts.rs"]
mod tests;
