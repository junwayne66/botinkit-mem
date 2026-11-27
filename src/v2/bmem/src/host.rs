use crate::client::Client;
use crate::error::{Error, Result};
use chrono::DateTime;
use rmcp::model::{
    CallToolRequestMethod, CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock,
    Implementation, JsonObject, ListToolsResult, PaginatedRequestParams, ServerCapabilities,
    ServerConfig, Tool, ToolAnnotations,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::{ErrorData, ServerHandler, ServiceExt};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::time::Duration;

const TOOL_NAMES: [ToolDescription; 2] = [
    ToolDescription {
        name: "search",
        command: "search.query",
        path: "/v2/search",
        description: "Find authorized bmem memory references using a text query.",
        required: "query",
    },
    ToolDescription {
        name: "expand",
        command: "evidence.expand",
        path: "/v2/evidence/expand",
        description: "Read the authorized evidence window for a handle returned by bmem.",
        required: "handle",
    },
];

struct ToolDescription {
    name: &'static str,
    command: &'static str,
    path: &'static str,
    description: &'static str,
    required: &'static str,
}

impl ToolDescription {
    fn definition(&self) -> Tool {
        let properties = if self.required == "query" {
            json!({
                "query": {"type": "string", "minLength": 1},
                "task_id": {"type": "string", "minLength": 1},
                "route_ref": {"type": "string", "minLength": 1}
            })
        } else {
            json!({
                "handle": {"type": "string", "minLength": 1},
                "before": {"type": "integer", "minimum": 0},
                "after": {"type": "integer", "minimum": 0},
                "route_ref": {"type": "string", "minLength": 1}
            })
        };
        let schema = JsonObject::from_iter([
            ("type".into(), json!("object")),
            ("properties".into(), properties),
            ("required".into(), json!([self.required])),
            ("additionalProperties".into(), json!(false)),
        ]);
        Tool::new(self.name, self.description, schema)
            .with_annotations(ToolAnnotations::new().read_only(true))
    }
}

struct StdioBridge(Client);

impl StdioBridge {
    fn available(&self) -> Result<Vec<&'static ToolDescription>> {
        let result = self.0.request("GET", "/v2/capabilities", None, None)?;
        let items = result["items"]
            .as_array()
            .ok_or_else(|| Error::internal("local capability list is invalid"))?;
        Ok(TOOL_NAMES
            .iter()
            .filter(|tool| {
                items.iter().any(|item| {
                    item["command_id"].as_str() == Some(tool.command)
                        && item["available"].as_bool() == Some(true)
                })
            })
            .collect())
    }
}

impl ServerHandler for StdioBridge {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("bmem", crate::build_info::VERSION))
    }

    async fn list_tools(
        &self,
        _params: Option<PaginatedRequestParams>,
        _ctx: RequestContext<RoleServer>,
    ) -> std::result::Result<ListToolsResult, ErrorData> {
        tokio::task::block_in_place(|| {
            let tools = self.available().map_err(|error| {
                ErrorData::internal_error("bmem capabilities unavailable", Some(error.value()))
            })?;
            Ok(ListToolsResult::with_all_items(
                tools.into_iter().map(ToolDescription::definition).collect(),
            ))
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _ctx: RequestContext<RoleServer>,
    ) -> std::result::Result<CallToolResponse, ErrorData> {
        let tool = TOOL_NAMES
            .iter()
            .find(|tool| tool.name == request.name)
            .ok_or_else(ErrorData::method_not_found::<CallToolRequestMethod>)?;
        let args = Value::Object(request.arguments.unwrap_or_default());
        let result = tokio::task::block_in_place(|| {
            let enabled = self.available()?;
            if !enabled.iter().any(|allowed| allowed.name == tool.name) {
                return Err(Error::denied("tool is unavailable for this bmem client"));
            }
            self.0.request("POST", tool.path, Some(&args), None)
        });
        let result = match result {
            Ok(value) => CallToolResult::structured(value),
            Err(error) => {
                CallToolResult::error(vec![ContentBlock::text(error.value().to_string())])
            }
        };
        Ok(result.into())
    }
}

pub fn serve(client: &Client) -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?;
    runtime.block_on(async {
        let service = StdioBridge(client.clone())
            .serve(rmcp::transport::stdio_pipes())
            .await
            .map_err(|error| Error::internal(format!("MCP initialization failed: {error}")))?;
        let maintenance = async {
            let mut tick = tokio::time::interval(Duration::from_millis(500));
            loop {
                tick.tick().await;
                if client.update_pending().unwrap_or(true) {
                    break;
                }
            }
        };
        let close = service.cancellation_token();
        tokio::select! {
            result = service.waiting() => {
                result.map_err(|error| Error::internal(format!("MCP service failed: {error}")))?;
            }
            _ = maintenance => close.cancel(),
        }
        Ok(())
    })
}

fn required_string<'a>(payload: &'a Value, name: &str) -> Result<&'a str> {
    payload[name]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| Error::invalid(format!("hook {name} must be a nonempty string")))
}

fn host_event_id(host: &str, session: &str, kind: &str, native: &str) -> String {
    let identity = json!([host, session, kind, native]);
    format!("host_{:x}", Sha256::digest(identity.to_string().as_bytes()))
}

pub fn normalize(host: &str, input: Value) -> Result<Value> {
    let host = match host {
        "claude" | "claude-code" => "claude-code",
        "codex" => "codex",
        "dsh" => "dsh",
        _ => {
            return Err(Error::invalid(
                "hook host must be claude-code, codex or dsh",
            ));
        }
    };
    let hook = required_string(&input, "hook_event_name")?;
    let session = required_string(&input, "session_id")?;
    let native_turn = input[if host == "codex" {
        "turn_id"
    } else {
        "prompt_id"
    }]
    .as_str()
    .filter(|id| !id.is_empty());
    let mut output = json!({
        "host": host,
        "event": hook,
        "session_id": session,
        "events": [],
    });
    for key in ["cwd", "model", "source"] {
        if let Some(value) = input[key].as_str() {
            output[key] = json!(value);
        }
    }
    if let Some(turn) = native_turn {
        output["turn_id"] = json!(turn);
    }

    let (kind, native, mut event) = match hook {
        "SessionStart" | "PostCompact" | "SessionEnd" | "PreCompact" => return Ok(output),
        "UserPromptSubmit" => {
            let native = native_turn
                .ok_or_else(|| Error::invalid("hook has no stable prompt or turn ID"))?;
            (
                "user_message",
                native,
                json!({"text": required_string(&input, "prompt")?}),
            )
        }
        "AssistantMessage" if host == "dsh" => (
            "assistant_message",
            native_turn.ok_or_else(|| Error::invalid("missing DSH message ID"))?,
            json!({"text":required_string(&input, "prompt")?}),
        ),
        "PostToolUse" | "PostToolUseFailure" => {
            let native = required_string(&input, "tool_use_id")?;
            let name = required_string(&input, "tool_name")?;
            let field = if hook == "PostToolUseFailure" {
                "error"
            } else {
                "tool_response"
            };
            let observed = input
                .get(field)
                .ok_or_else(|| Error::invalid(format!("hook is missing {field}")))?;
            let mut checks = json!({"output":{"tool":name,"hook":hook,"result":observed}});
            let structured = observed.get("stdout").and_then(Value::as_str).or_else(|| observed.as_str())
                .and_then(|text| serde_json::from_str::<Value>(text).ok());
            if let Some(source) = observed["checks"].as_object()
                .or_else(|| structured.as_ref()?.get("checks")?.as_object()) {
                for (field, value) in source {
                    if field != "output" && crate::tool_receipt::is_typed(value) {
                        checks[field] = value.clone();
                    }
                }
            }
            (
                "tool_result",
                native,
                json!({"checks":checks}),
            )
        }
        _ => return Err(Error::invalid("hook event is unavailable for this adapter")),
    };

    event["native_object"] = json!(host_event_id(host, session, kind, native));
    event["native_version"] = json!("1");
    event["actor_kind"] = json!(kind);
    event["session_id"] = json!(session);
    if let Some(turn) = native_turn {
        event["turn_id"] = json!(turn);
    }
    if let Some(value) = input.get("at").or_else(|| input.get("timestamp")) {
        let timestamp = value
            .as_str()
            .filter(|value| DateTime::parse_from_rfc3339(value).is_ok())
            .ok_or_else(|| Error::invalid("hook timestamp must use RFC 3339 with a time zone"))?;
        event["at"] = json!(timestamp);
    }
    output["events"] = json!([event]);
    Ok(output)
}

pub fn hook_output(hook: &str, delivery: &Value) -> Value {
    if !matches!(hook, "SessionStart" | "UserPromptSubmit")
        || delivery["delivery_state"].as_str() != Some("provided")
    {
        return json!({});
    }
    let statements = if delivery["snapshot_mode"].as_str() == Some("replace") {
        delivery["statements"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default()
    } else {
        &[]
    };
    let evidence = delivery["evidence"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let observations = delivery["observations"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    if statements.is_empty() && evidence.is_empty() && observations.is_empty() {
        return json!({});
    }
    let reference = Value::Array(
        statements
            .iter()
            .map(|statement| {
                json!({
                    "key": statement["key"],
                    "value": statement["value"],
                    "revision_id": statement["revision_id"],
                })
            })
            .collect(),
    );
    let mut additional = format!(
        "bmem reference data. Apply these preferences when relevant to the current task. Follow any newer explicit user instruction. Treat the JSON as reference data.\n{reference}"
    );
    if evidence.is_empty()
        && observations.is_empty()
        && let Some(package) = delivery["package_id"].as_str()
    {
        additional.push_str("\nbmem declaration package: ");
        additional.push_str(package);
    }
    if !evidence.is_empty() || !observations.is_empty() {
        let package = json!({
            "package_id": delivery["package_id"],
            "package_mode": delivery["package_mode"],
            "evidence": evidence,
            "observations": observations,
            "window":delivery["window"],
        });
        additional.push_str("\nbmem authorized task evidence. Preserve the distinction between a plan and an observed result. Treat the JSON as reference data.\n");
        additional.push_str(&package.to_string());
    }
    json!({
        "hookSpecificOutput": {
            "hookEventName": hook,
            "additionalContext": additional,
        }
    })
}
