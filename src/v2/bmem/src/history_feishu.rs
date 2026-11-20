use crate::error::{Error, Result};
use crate::feishu;
use chrono::DateTime;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::time::Duration;

const CHAT_PAGE_SIZE: &str = "50";
const MESSAGE_PAGE_SIZE: u16 = 16;

pub struct Account {
    binary: PathBuf,
    profile: String,
    key: String,
}

pub struct ChatPage {
    pub ids: Vec<String>,
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
            Err(error) if !stop() && error.status == 503 => continue,
            Err(error) => return Err(error),
        };
        if account_fingerprint(&status).as_deref() == Some(account_key) {
            return Ok(Account {
                binary,
                profile: name.to_owned(),
                key: account_key.to_owned(),
            });
        }
    }
    Err(Error::denied("请在数据来源中重新授权这个飞书账号"))
}

impl Account {
    pub fn chats(&self, page_token: Option<&str>, stop: &impl Fn() -> bool) -> Result<ChatPage> {
        let mut args = vec![
            "im",
            "+chat-list",
            "--types",
            "p2p,group",
            "--page-size",
            CHAT_PAGE_SIZE,
        ];
        if let Some(token) = page_token {
            args.extend(["--page-token", token]);
        }
        let response = self.request(args, stop)?;
        let (items, next_token) = page(&response, "chats", page_token)?;
        let ids = items
            .iter()
            .map(|chat| {
                text(&chat["chat_id"])
                    .filter(|id| id.starts_with("oc_"))
                    .map(str::to_owned)
                    .ok_or_else(|| Error::unavailable("飞书会话页缺少有效会话 ID"))
            })
            .collect::<Result<_>>()?;
        Ok(ChatPage { ids, next_token })
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
        self.read_messages("chat", chat_id, Some((start, end)), page_token, stop)
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
        self.read_messages("thread", thread_id, None, page_token, stop)
    }

    fn read_messages(
        &self,
        container: &str,
        id: &str,
        window: Option<(i64, i64)>,
        page_token: Option<&str>,
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
            if let Some(event) = normalize(item)? {
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
        if account_fingerprint(&status).as_deref() != Some(self.key.as_str()) {
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

fn account_fingerprint(status: &Value) -> Option<String> {
    let user = &status["identities"]["user"];
    if status["brand"] != "feishu" || user["status"] != "ready" || user["available"] != true {
        return None;
    }
    let app = text(&status["appId"])?;
    let open_id = text(&user["openId"])?;
    let hash = format!(
        "{:x}",
        Sha256::digest(json!(["feishu", app, open_id]).to_string())
    );
    Some(format!("feishu-{}", &hash[..20]))
}

fn page<'a>(
    response: &'a Value,
    field: &str,
    previous_token: Option<&str>,
) -> Result<(&'a [Value], Option<String>)> {
    let data = &response["data"];
    if response["ok"] != true || response["identity"] != "user" || !data.is_object() {
        return Err(Error::unavailable("飞书 CLI 未返回已授权的用户数据"));
    }
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

fn normalize(item: &Value) -> Result<Option<Value>> {
    let recalled = item["deleted"] == true;
    let kind = item["msg_type"].as_str();
    if !recalled && !matches!(kind, Some("text" | "post")) {
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
        parsed =
            serde_json::from_str(body).map_err(|_| Error::unavailable("飞书文本消息格式无效"))?;
        &parsed
    } else {
        payload
    };
    let body = if kind == Some("text") {
        text(&content["text"]).unwrap_or_default().to_owned()
    } else {
        rich_text(content)
    };
    if body.is_empty() {
        return Ok(None);
    }
    if item["updated"] == true || version > created.timestamp_millis() {
        event["change_kind"] = json!("updated");
    }
    event["text"] = json!(body);
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

#[cfg(test)]
#[path = "../../../../tests/v2/history_feishu.rs"]
mod tests;
