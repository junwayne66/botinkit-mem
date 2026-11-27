use crate::error::{Error, Result};
use crate::feishu;
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::time::Duration;
use url::Url;

const CHAT_PAGE_SIZE: &str = "50";
const MESSAGE_PAGE_SIZE: u16 = 16;

pub struct Account {
    binary: PathBuf,
    profile: String,
    pub identity: feishu::Account,
}

/// One chat as returned by the authorized user's chat list: its stable native
/// id, the Feishu `chat_mode` (p2p/group) used to restrict dm vs group, and the
/// human-readable name shown as the import target label.
pub struct ChatMeta {
    pub id: String,
    pub mode: String,
    pub name: String,
}

pub struct ChatListPage {
    pub chats: Vec<ChatMeta>,
    pub next_token: Option<String>,
}

pub struct MessagePage {
    pub events: Vec<Value>,
    pub thread_ids: Vec<String>,
    pub next_token: Option<String>,
    pub oldest_timestamp: Option<i64>,
}

pub fn account(account_key: &str, stop: &impl Fn() -> bool) -> Result<Account> {
    let binary = feishu::cli().ok_or_else(|| Error::missing("请先安装飞书 CLI"))?;
    let profiles = command(&binary, &["profile", "list"], stop)?;
    let profiles = profiles
        .as_array()
        .ok_or_else(|| Error::unavailable("飞书 CLI 配置列表无效"))?;
    let mut unavailable = None;
    for profile in profiles
        .iter()
        .filter(|profile| profile["brand"] == "feishu")
    {
        let Some(name) = text(&profile["name"]) else {
            continue;
        };
        let status = match command(
            &binary,
            &["auth", "status", "--json", "--profile", name],
            stop,
        ) {
            Ok(status) => status,
            Err(error) if !stop() && error.status == 503 => {
                unavailable = Some(error);
                continue;
            }
            Err(error) => return Err(error),
        };
        if let Some(identity) = feishu::authorized_account(&status)
            && identity.key == account_key
        {
            return Ok(Account {
                binary,
                profile: name.to_owned(),
                identity,
            });
        }
    }
    Err(unavailable.unwrap_or_else(|| Error::denied("请在数据来源中重新授权这个飞书账号")))
}

impl Account {
    /// List one page of the authorized user's chats restricted to `types`
    /// (`p2p`, `group`, or `p2p,group`), carrying each chat's mode and name so
    /// the import picker can label targets and keep dm/group apart.
    pub fn chat_list(
        &self,
        types: &str,
        page_token: Option<&str>,
        stop: &impl Fn() -> bool,
    ) -> Result<ChatListPage> {
        let mut args = vec![
            "im",
            "+chat-list",
            "--types",
            types,
            "--page-size",
            CHAT_PAGE_SIZE,
        ];
        if let Some(token) = page_token {
            args.extend(["--page-token", token]);
        }
        let response = self.request(args, stop)?;
        let (items, next_token) = page(&response, "chats", page_token)?;
        let mut chats = Vec::new();
        for chat in items {
            let id = text(&chat["chat_id"])
                .filter(|id| id.starts_with("oc_"))
                .ok_or_else(|| Error::unavailable("飞书会话页缺少有效会话 ID"))?;
            chats.push(ChatMeta {
                id: id.to_owned(),
                mode: text(&chat["chat_mode"]).unwrap_or_default().to_owned(),
                name: text(&chat["name"]).unwrap_or_default().to_owned(),
            });
        }
        Ok(ChatListPage { chats, next_token })
    }

    pub fn messages(
        &self,
        chat_id: &str,
        start: i64,
        end: i64,
        page_token: Option<&str>,
        stop: &impl Fn() -> bool,
    ) -> Result<MessagePage> {
        if !chat_id.starts_with("oc_") || start < 0 || end <= start {
            return Err(Error::invalid("飞书会话或历史时间窗口无效"));
        }
        self.read_messages("chat", chat_id, Some((start, end)), page_token, false, stop)
    }

    pub fn thread_messages(
        &self,
        thread_id: &str,
        page_token: Option<&str>,
        stop: &impl Fn() -> bool,
    ) -> Result<MessagePage> {
        if !thread_id.starts_with("omt_") {
            return Err(Error::invalid("飞书话题 ID 无效"));
        }
        self.read_messages("thread", thread_id, None, page_token, false, stop)
    }

    /// Read one ascending page of a thread inside a fixed time window. Used by
    /// selective import so thread replies are filtered server-side to the chosen
    /// window and paginated with a readable cursor, mirroring [`Self::messages`].
    pub fn thread_messages_window(
        &self,
        thread_id: &str,
        start: i64,
        end: i64,
        page_token: Option<&str>,
        include_metadata: bool,
        stop: &impl Fn() -> bool,
    ) -> Result<MessagePage> {
        if !thread_id.starts_with("omt_") || start < 0 || end <= start {
            return Err(Error::invalid("飞书话题或历史时间窗口无效"));
        }
        self.read_messages(
            "thread",
            thread_id,
            Some((start, end)),
            page_token,
            include_metadata,
            stop,
        )
    }

    fn read_messages(
        &self,
        container: &str,
        id: &str,
        window: Option<(i64, i64)>,
        page_token: Option<&str>,
        include_metadata: bool,
        stop: &impl Fn() -> bool,
    ) -> Result<MessagePage> {
        let order = if window.is_some() {
            "ByCreateTimeAsc"
        } else {
            "ByCreateTimeDesc"
        };
        let mut params = json!({
            "container_id_type": container, "container_id": id,
            "sort_type": order, "page_size": MESSAGE_PAGE_SIZE,
        });
        if let Some((start, end)) = window {
            if start != 0 {
                params["start_time"] = json!(start.to_string());
            }
            params["end_time"] = json!(end.to_string());
            params["only_thread_root_messages"] = json!(false);
        }
        if let Some(token) = page_token {
            params["page_token"] = json!(token);
        }
        let encoded = params.to_string();
        let response = self.request(
            vec![
                "api",
                "GET",
                "/open-apis/im/v1/messages",
                "--params",
                &encoded,
            ],
            stop,
        )?;
        let (items, next_token) = page(&response, "items", page_token)?;
        let mut events = Vec::new();
        let mut thread_ids = Vec::new();
        let mut oldest_timestamp = None;
        for item in items {
            if let Some(timestamp) = millis(&item["create_time"]).map(|time| time.div_euclid(1000))
            {
                oldest_timestamp =
                    Some(oldest_timestamp.map_or(timestamp, |oldest: i64| oldest.min(timestamp)));
            }
            if let Some(event) = normalize(item, include_metadata)? {
                events.push(event);
            }
            if window.is_some()
                && let Some(id) = text(&item["thread_id"]).filter(|id| id.starts_with("omt_"))
                && !thread_ids.iter().any(|found| found == id)
            {
                thread_ids.push(id.to_owned());
            }
        }
        Ok(MessagePage {
            events,
            thread_ids,
            next_token,
            oldest_timestamp,
        })
    }

    fn request<'a>(&'a self, mut args: Vec<&'a str>, stop: &impl Fn() -> bool) -> Result<Value> {
        let status = command(
            &self.binary,
            &["auth", "status", "--json", "--profile", &self.profile],
            stop,
        )?;
        if !feishu::authorized_account(&status)
            .is_some_and(|identity| identity.key == self.identity.key)
        {
            return Err(Error::denied("飞书历史读取账号已变更，请重新登记数据来源"));
        }
        args.extend([
            "--as",
            "user",
            "--format",
            "json",
            "--profile",
            &self.profile,
        ]);
        command(&self.binary, &args, stop)
    }
}

fn command(binary: &Path, args: &[&str], stop: &impl Fn() -> bool) -> Result<Value> {
    if stop() {
        return Err(Error::unavailable("飞书历史读取已暂停"));
    }
    let failure = RefCell::new(None);
    let output = feishu::run(
        binary,
        args,
        None,
        Duration::from_secs(25),
        stop,
        |stderr| {
            if let Some(error) = cli_error(stderr) {
                *failure.borrow_mut() = Some(error);
            }
        },
    )
    .map_err(|error| failure.into_inner().unwrap_or(error))?;
    feishu::json_output(output)
}

fn cli_error(stderr: &str) -> Option<Error> {
    let response: Value = serde_json::from_str(stderr).ok()?;
    let error = &response["error"];
    let code = error["code"].as_i64();
    let detail = error["message"]
        .as_str()
        .unwrap_or_default()
        .to_ascii_lowercase();
    if code == Some(230001) && (detail.contains("page_token") || detail.contains("page token")) {
        return Some(Error::new(
            409,
            "feishu_cursor_expired",
            "飞书分页游标已失效，请从本次时间窗口起点重新读取",
        ));
    }
    if matches!(code, Some(230002 | 230013 | 230073 | 230110 | 231203))
        || (code == Some(230001) && detail.contains("invalid container_id"))
    {
        return Some(Error::new(
            404,
            "feishu_container_unavailable",
            "当前账号无法读取这个飞书会话或话题",
        ));
    }
    if matches!(code, Some(99991676 | 99991677 | 99991679)) || error["type"] == "authorization" {
        return Some(Error::new(
            403,
            "feishu_authorization_required",
            "请检查飞书账号及历史读取权限",
        ));
    }
    None
}

/// Validate that a CLI reply is authorized user data and return its `data`
/// object. Shared by paginated reads and single-object doc reads so a bot
/// identity or an error envelope can never be mistaken for user content.
fn user_data(response: &Value) -> Result<&Value> {
    let data = &response["data"];
    if response["ok"] != true || response["identity"] != "user" || !data.is_object() {
        return Err(Error::unavailable("飞书 CLI 未返回已授权的用户数据"));
    }
    Ok(data)
}

fn page<'a>(
    response: &'a Value,
    field: &str,
    previous_token: Option<&str>,
) -> Result<(&'a [Value], Option<String>)> {
    let data = user_data(response)?;
    let has_more = data["has_more"]
        .as_bool()
        .ok_or_else(|| Error::unavailable("飞书返回了无效的分页状态"))?;
    let rows = match &data[field] {
        Value::Array(rows) => rows.as_slice(),
        Value::Null if !has_more => &[],
        _ => return Err(Error::unavailable("飞书返回了无效的分页数据")),
    };
    let token = if has_more {
        let token = text(&data["page_token"])
            .filter(|token| Some(*token) != previous_token)
            .ok_or_else(|| Error::unavailable("飞书分页游标缺失或未推进"))?;
        Some(token.to_owned())
    } else {
        None
    };
    Ok((rows, token))
}

fn text(value: &Value) -> Option<&str> {
    value.as_str().filter(|value| !value.trim().is_empty())
}

fn millis(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| value.as_str()?.parse().ok())
}

fn normalize(item: &Value, include_metadata: bool) -> Result<Option<Value>> {
    let recalled = item["deleted"] == true;
    let kind = item["msg_type"].as_str().unwrap_or("unknown");
    let textual = matches!(kind, "text" | "post");
    if !recalled && !textual && !include_metadata {
        return Ok(None);
    }
    let message_id =
        text(&item["message_id"]).ok_or_else(|| Error::unavailable("飞书消息缺少 ID"))?;
    let created = millis(&item["create_time"])
        .and_then(DateTime::from_timestamp_millis)
        .ok_or_else(|| Error::unavailable("飞书消息缺少有效时间"))?;
    let version = millis(&item["update_time"]).unwrap_or(created.timestamp_millis());
    let actor = if item["sender"]["sender_type"] == "user" {
        "user_message"
    } else {
        "assistant_message"
    };
    let mut event = json!({
        "native_object": message_id,
        "native_version": version.to_string(),
        "actor_kind": actor,
        "at": created.to_rfc3339(),
    });
    if recalled {
        event["change_kind"] = json!("revoked");
        return Ok(Some(event));
    }
    let payload = &item["body"]["content"];
    let parsed;
    let content = if let Some(body) = payload.as_str() {
        parsed = if textual {
            serde_json::from_str(body).map_err(|_| Error::unavailable("飞书文本消息格式无效"))?
        } else {
            serde_json::from_str(body).unwrap_or(Value::Null)
        };
        &parsed
    } else {
        payload
    };
    let body = match kind {
        "text" => text(&content["text"]).unwrap_or_default().to_owned(),
        "post" => rich_text(content),
        _ => String::new(),
    };
    if body.is_empty() && !include_metadata {
        return Ok(None);
    }
    if item["updated"] == true || version > created.timestamp_millis() {
        event["change_kind"] = json!("updated");
    }
    event["text"] = if body.is_empty() {
        let name = text(&content["file_name"]).or_else(|| text(&content["title"]));
        let category = match kind {
            "image" => "图片",
            "file" => "文件",
            "audio" => "语音",
            "media" => "视频",
            "sticker" => "表情",
            "interactive" => "卡片",
            "text" | "post" => "空消息",
            _ => "其他消息",
        };
        json!(match name {
            Some(name) => format!("飞书{category} · {name}"),
            None => format!("飞书{category}"),
        })
    } else {
        json!(body)
    };
    Ok(Some(event))
}

fn rich_text(body: &Value) -> String {
    let body = if body["content"].is_object() && !body["content"]["content"].is_null() {
        &body["content"]
    } else if body["content"].is_array() {
        body
    } else if let Some(localized) = body.as_object().and_then(|variants| {
        variants
            .values()
            .find(|variant| variant["content"].is_array())
    }) {
        localized
    } else {
        return String::new();
    };
    let title = text(&body["title"]);
    let paragraphs = body["content"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let line = row
                .as_array()?
                .iter()
                .filter_map(|node| text(&node["text"]))
                .collect::<String>();
            (!line.is_empty()).then_some(line)
        });
    title
        .map(str::to_owned)
        .into_iter()
        .chain(paragraphs)
        .collect::<Vec<_>>()
        .join("\n")
}

// ---------------------------------------------------------------------------
// Selective history import (T035)
// ---------------------------------------------------------------------------

/// List import targets for an authorized Feishu account. `kind` is `dm`,
/// `group`, or `docs`; `account_or_host` is the registered account key
/// (`feishu-…`). dm/group paginate the user's chat list with a readable label
/// and an optional query over the name. Documents use Drive search in the
/// same authorized user profile, with a direct-link alternative.
///
/// Returns `{"targets":[{"id","label"}], "next_cursor":<string|null>}`.
pub fn options(
    account_or_host: &str,
    kind: &str,
    query: Option<&str>,
    cursor: Option<&str>,
    stop: &impl Fn() -> bool,
) -> Result<Value> {
    match kind {
        "dm" | "group" => {
            let account = account(account_or_host, stop)?;
            let (types, mode, fallback) = if kind == "dm" {
                ("p2p", "p2p", "私聊会话")
            } else {
                ("group", "group", "群聊")
            };
            let list = account.chat_list(types, cursor, stop)?;
            let needle = query
                .map(|q| q.trim().to_lowercase())
                .filter(|q| !q.is_empty());
            let mut targets = Vec::new();
            for chat in list.chats {
                // Keep dm/group apart; trust the server-side --types filter
                // when the CLI omits chat_mode.
                if !chat_matches_mode(&chat, mode) {
                    continue;
                }
                let label = if chat.name.is_empty() {
                    fallback.to_owned()
                } else {
                    chat.name
                };
                if let Some(n) = &needle
                    && !label.to_lowercase().contains(n)
                {
                    continue;
                }
                targets.push(json!({"id": chat.id, "label": label}));
            }
            Ok(json!({"targets": targets, "next_cursor": list.next_token}))
        }
        "docs" => {
            let account = account(account_or_host, stop)?;
            let query = query.unwrap_or_default();
            if query.chars().count() > 30 {
                return Err(Error::invalid("文档搜索词最多 30 个字符"));
            }
            let (items, next) = search_documents(&account, query, cursor, stop)?;
            let mut targets = Vec::new();
            for item in items {
                let (url, label) = listed_document(&item)?;
                targets.push(json!({"id":url,"label":label}));
            }
            Ok(json!({"targets":targets,"next_cursor":next}))
        }
        _ => Err(Error::invalid("飞书历史导入仅支持 dm、group 或 docs")),
    }
}

fn search_documents(
    account: &Account,
    query: &str,
    cursor: Option<&str>,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Option<String>)> {
    let mut args = vec![
        "drive",
        "+search",
        "--doc-types",
        "docx,doc,wiki",
        "--query",
        query,
        "--page-size",
        "20",
    ];
    if let Some(token) = cursor {
        args.extend(["--page-token", token]);
    }
    let reply = account.request(args, stop)?;
    let (items, next) = page(&reply, "results", cursor)?;
    Ok((items.to_vec(), next))
}

fn listed_document(item: &Value) -> Result<(String, &str)> {
    let url = text(&item["result_meta"]["url"])
        .or_else(|| text(&item["url"]))
        .ok_or_else(|| Error::unavailable("飞书文档目录缺少可读取的文档链接"))?;
    let label = text(&item["title"]).unwrap_or("飞书文档");
    Ok((normalize_document_target(url)?, label))
}

pub fn discover_targets(
    account_key: &str,
    kind: &str,
    cursor: Option<&str>,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<String>, Option<String>)> {
    let account = account(account_key, stop)?;
    if kind == "docs" {
        let (items, next) = search_documents(&account, "", cursor, stop)?;
        let targets = items
            .iter()
            .map(|item| listed_document(item).map(|(url, _)| url))
            .collect::<Result<Vec<_>>>()?;
        return Ok((targets, next));
    }
    let mode = if kind == "dm" { "p2p" } else { "group" };
    let page = account.chat_list(mode, cursor, stop)?;
    let targets = page
        .chats
        .into_iter()
        .filter(|chat| chat_matches_mode(chat, mode))
        .map(|chat| chat.id)
        .collect();
    Ok((targets, page.next_token))
}

fn chat_matches_mode(chat: &ChatMeta, mode: &str) -> bool {
    chat.mode.is_empty() || chat.mode == mode || (mode == "group" && chat.mode == "topic")
}

/// Read one page of importable events from a Feishu target. dm/group read the
/// chat inside the `[from,to)` window and then its readable threads, paginated
/// by an opaque JSON `cursor`; docs read the authorized document body once.
/// Events keep their stable native ids/versions and the standard
/// intake fields. Returns `(events, next_cursor)`; `next_cursor == None` means
/// the target is exhausted. An empty page with a cursor means "keep reading".
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
    if !matches!(kind, "dm" | "group" | "docs") {
        return Err(Error::invalid("飞书历史导入仅支持 dm、group 或 docs"));
    }
    let account = account(account_or_host, stop)?;
    let include_metadata = content == "all";
    match kind {
        "docs" => read_doc(&account, target, stop),
        _ => read_chat(&account, target, from, to, include_metadata, cursor, stop),
    }
}

struct Container {
    kind: String,
    id: String,
}

/// Resume state for a chat import: the chat first, then any threads discovered
/// while reading it, each paginated inside the same fixed window.
struct ImportState {
    containers: Vec<Container>,
    index: usize,
    token: Option<String>,
    start: i64,
    end: i64,
}

impl ImportState {
    fn parse(value: &Value) -> Result<Self> {
        let invalid = || Error::invalid("飞书导入游标无效");
        let rows = value["containers"].as_array().ok_or_else(invalid)?;
        let mut containers = Vec::new();
        for row in rows {
            let kind = row["kind"]
                .as_str()
                .filter(|kind| matches!(*kind, "chat" | "thread"))
                .ok_or_else(invalid)?;
            let id = row["id"]
                .as_str()
                .filter(|id| !id.is_empty())
                .ok_or_else(invalid)?;
            containers.push(Container {
                kind: kind.to_owned(),
                id: id.to_owned(),
            });
        }
        if containers.is_empty() {
            return Err(invalid());
        }
        let index = value["index"].as_u64().ok_or_else(invalid)? as usize;
        if index >= containers.len() {
            return Err(invalid());
        }
        let token = match &value["token"] {
            Value::Null => None,
            Value::String(token) => Some(token.clone()),
            _ => return Err(invalid()),
        };
        let start = value["start"].as_i64().ok_or_else(invalid)?;
        let end = value["end"].as_i64().ok_or_else(invalid)?;
        if start < 0 || end <= start {
            return Err(invalid());
        }
        Ok(Self {
            containers,
            index,
            token,
            start,
            end,
        })
    }

    fn to_value(&self) -> Value {
        json!({
            "containers": self.containers.iter()
                .map(|c| json!({"kind": c.kind, "id": c.id})).collect::<Vec<_>>(),
            "index": self.index,
            "token": self.token,
            "start": self.start,
            "end": self.end,
        })
    }
}

fn read_chat(
    account: &Account,
    target: &str,
    from: Option<&str>,
    to: Option<&str>,
    include_metadata: bool,
    cursor: Option<&Value>,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Option<Value>)> {
    let mut state = match cursor {
        Some(value) => ImportState::parse(value)?,
        None => {
            if !target.starts_with("oc_") {
                return Err(Error::invalid("飞书会话目标无效"));
            }
            let start = match from {
                Some(from) => parse_time_bound(from)?.timestamp(),
                None => 0,
            };
            let end = match to {
                Some(to) => parse_time_bound(to)?.timestamp(),
                None => Utc::now().timestamp(),
            };
            if start < 0 || end <= start {
                return Err(Error::invalid("飞书历史时间窗口无效"));
            }
            ImportState {
                containers: vec![Container {
                    kind: "chat".to_owned(),
                    id: target.to_owned(),
                }],
                index: 0,
                token: None,
                start,
                end,
            }
        }
    };
    let (kind, id) = {
        let container = &state.containers[state.index];
        (container.kind.clone(), container.id.clone())
    };
    let page = if kind == "chat" {
        account.read_messages(
            "chat",
            &id,
            Some((state.start, state.end)),
            state.token.as_deref(),
            include_metadata,
            stop,
        )?
    } else {
        account.thread_messages_window(
            &id,
            state.start,
            state.end,
            state.token.as_deref(),
            include_metadata,
            stop,
        )?
    };
    for thread in page.thread_ids {
        if !state.containers.iter().any(|c| c.id == thread) {
            state.containers.push(Container {
                kind: "thread".to_owned(),
                id: thread,
            });
        }
    }
    let events = page
        .events
        .into_iter()
        .filter(|event| in_window(event, state.start, state.end))
        .collect::<Vec<_>>();
    let next = if let Some(token) = page.next_token {
        state.token = Some(token);
        Some(state.to_value())
    } else {
        state.index += 1;
        state.token = None;
        (state.index < state.containers.len()).then(|| state.to_value())
    };
    Ok((events, next))
}

fn read_doc(
    account: &Account,
    target: &str,
    stop: &impl Fn() -> bool,
) -> Result<(Vec<Value>, Option<Value>)> {
    let (mut kind, mut document_id) = document_target(target)?;
    let mut wiki_created_at = None;
    if kind == "wiki" {
        let reply = account.request(
            vec!["wiki", "+node-get", "--node-token", &document_id],
            stop,
        )?;
        let data = user_data(&reply)?;
        let node = data.get("node").unwrap_or(data);
        match node["obj_type"].as_str() {
            Some("docx") => kind = "docx",
            Some("doc") => kind = "docs",
            Some(_) => return Ok((Vec::new(), None)),
            None => return Err(Error::unavailable("飞书知识库未返回文档类型")),
        }
        document_id = text(&node["obj_token"])
            .filter(|token| is_docx_token(token))
            .ok_or_else(|| Error::unavailable("飞书知识库未返回文档标识"))?
            .to_owned();
        wiki_created_at = millis(&node["obj_create_time"]);
    }
    let modern = kind == "docx";
    let raw_path = if modern {
        format!("/open-apis/docx/v1/documents/{document_id}/raw_content")
    } else {
        format!("/open-apis/doc/v2/{document_id}/raw_content")
    };
    let response = account.request(vec!["api", "GET", &raw_path], stop)?;
    let text = user_data(&response)?["content"]
        .as_str()
        .ok_or_else(|| Error::unavailable("飞书未返回可读取的文档正文"))?;
    if text.trim().is_empty() {
        return Ok((Vec::new(), None));
    }
    // Best-effort meta/revision. The body above already proved the authorized
    // account, so a missing meta only drops the revision — it never relaxes the
    // account check or falls back to another identity.
    let meta_path = format!("/open-apis/docx/v1/documents/{document_id}");
    let document = if modern {
        account
            .request(vec!["api", "GET", &meta_path], stop)
            .ok()
            .and_then(|reply| user_data(&reply).ok().map(|data| data["document"].clone()))
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let at = millis(&document["create_time"])
        .or(wiki_created_at)
        .and_then(|time| {
            if time.unsigned_abs() > 100_000_000_000 {
                DateTime::from_timestamp_millis(time)
            } else {
                DateTime::from_timestamp(time, 0)
            }
        })
        .unwrap_or_else(Utc::now);
    let canonical_kind = if modern { "docx" } else { "doc" };
    let mut canonical_link = Url::parse(&document_link(target, &document_id))
        .map_err(|_| Error::invalid("飞书文档链接无效"))?;
    canonical_link.set_path(&format!("/{kind}/{document_id}"));
    let mut event = json!({
        "native_object": format!("{canonical_kind}:{document_id}"),
        "actor_kind": "user_message",
        "at": at.to_rfc3339(),
        "text": format!("飞书文档 · {canonical_link}\n\n{text}"),
    });
    if let Some(revision) = document["revision_id"].as_i64() {
        event["native_version"] = json!(revision.to_string());
    }
    Ok((vec![event], None))
}

fn document_link(target: &str, token: &str) -> String {
    let Ok(mut url) = Url::parse(target) else {
        return format!("https://www.feishu.cn/docx/{token}");
    };
    url.set_query(None);
    url.set_fragment(None);
    url.to_string()
}

pub(crate) fn normalize_document_target(target: &str) -> Result<String> {
    let (_, token) = document_target(target)?;
    Ok(document_link(target.trim(), &token))
}

/// Accept a canonical Feishu doc link (https on a feishu.cn/larksuite.com host,
/// `/docx/`, `/docs/`, or `/wiki/` path) or a bare docx token, and return the
/// stable document id. Anything else — other hosts, credentials in the URL, or
/// malformed tokens — is rejected so an arbitrary link can never be read.
fn document_target(target: &str) -> Result<(&'static str, String)> {
    let target = target.trim();
    if target.is_empty() {
        return Err(Error::invalid("飞书文档链接无效"));
    }
    if !target.contains('/') && !target.contains(':') {
        return is_docx_token(target)
            .then(|| ("docx", target.to_owned()))
            .ok_or_else(|| Error::invalid("飞书文档 ID 无效"));
    }
    let url = Url::parse(target).map_err(|_| Error::invalid("飞书文档链接无效"))?;
    let host = url
        .host_str()
        .ok_or_else(|| Error::invalid("飞书文档链接无效"))?;
    if url.scheme() != "https"
        || !feishu_host(host)
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(Error::invalid("飞书文档链接无效"));
    }
    let segments: Vec<&str> = url
        .path_segments()
        .map(|parts| parts.collect())
        .unwrap_or_default();
    for (index, segment) in segments.iter().enumerate() {
        if matches!(*segment, "docx" | "docs" | "wiki")
            && let Some(token) = segments
                .get(index + 1)
                .copied()
                .filter(|t| is_docx_token(t))
        {
            let kind = match *segment {
                "docs" => "docs",
                "wiki" => "wiki",
                _ => "docx",
            };
            return Ok((kind, token.to_owned()));
        }
    }
    Err(Error::invalid("飞书文档链接缺少有效的文档 ID"))
}

fn is_docx_token(token: &str) -> bool {
    (6..=128).contains(&token.len())
        && token
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn feishu_host(host: &str) -> bool {
    ["feishu.cn", "larksuite.com"].iter().any(|domain| {
        host == *domain
            || host
                .strip_suffix(domain)
                .is_some_and(|prefix| prefix.ends_with('.'))
    })
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

fn in_window(event: &Value, start: i64, end: i64) -> bool {
    let Some(at) = event["at"]
        .as_str()
        .and_then(|at| DateTime::parse_from_rfc3339(at).ok())
    else {
        return true;
    };
    let seconds = at.timestamp();
    seconds >= start && seconds < end
}

#[cfg(test)]
#[path = "../../../../tests/v2/history_feishu.rs"]
mod tests;
