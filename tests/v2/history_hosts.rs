use super::*;
use std::io::Write;
use tempfile::Builder;

const TIME: &str = "2026-11-20T00:10:00Z";

#[test]
fn codex_retains_visible_layer_and_waits_for_partial_lines() -> Result<()> {
    let mut file = Builder::new().suffix(".jsonl").tempfile()?;
    let records = [
        json!({"type":"session_meta","payload":{"id":"codex-test"}}),
        json!({"timestamp":TIME,"type":"event_msg","payload":{"type":"user_message","turn_id":"turn-1","message":"A visible prompt"}}),
        json!({"timestamp":TIME,"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"A visible prompt"}]}}),
        json!({"timestamp":TIME,"type":"response_item","payload":{"type":"reasoning","summary":[{"text":"hidden"}]}}),
        json!({"timestamp":TIME,"type":"response_item","payload":{"type":"function_call_output","call_id":"tool-1","output":{"stdout":"probe","exit_code":1}}}),
    ];
    for record in records {
        writeln!(file, "{record}")?;
    }
    let partial = json!({"timestamp":TIME,"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"A visible answer"}]}}).to_string();
    file.write_all(&partial.as_bytes()[..11])?;
    let page = read_page("codex", file.path(), 0, 100)?;
    assert!(page.eof);
    assert_eq!(page.events.len(), 2);
    assert_eq!(page.events[0]["text"], "A visible prompt");
    assert_eq!(page.events[1]["checks"]["output"]["exit_code"], 1);
    assert_eq!(page.offset + 11, file.as_file().metadata()?.len());

    writeln!(file, "{}", &partial[11..])?;
    let tail = read_page("codex", file.path(), page.offset, 100)?;
    assert_eq!(tail.events.len(), 1);
    assert_eq!(tail.events[0]["text"], "A visible answer");
    Ok(())
}

#[test]
fn codex_preserves_text_accompanying_large_images_across_page_boundaries() -> Result<()> {
    let mut file = Builder::new().suffix(".jsonl").tempfile()?;
    let receipt = json!({"type":"response_item","payload":{
        "type":"function_call_output","call_id":"large-receipt","output":"x".repeat(3 * MAX_RECORD_BYTES as usize)
    }});
    writeln!(file, "{receipt}")?;
    let prompt_offset = file.as_file().metadata()?.len();
    let with_image = json!({"type":"response_item","payload":{
        "type":"message","role":"user","content":[
            {"type":"input_text","text":"compare these photos"},
            {"type":"input_image","image_url":"x".repeat(MAX_RECORD_BYTES as usize)}
        ]
    }});
    writeln!(file, "{with_image}")?;
    let text_only = json!({"type":"response_item","payload":{
        "type":"message","role":"user","content":[{"type":"input_text","text":"x".repeat(MAX_RECORD_BYTES as usize)}]
    }});
    writeln!(file, "{text_only}")?;
    let first = read_page("codex", file.path(), 0, 100)?;
    assert_eq!(first.offset, prompt_offset);
    assert!(first.events.is_empty());
    let next = read_page("codex", file.path(), first.offset, 100)?;
    assert!(next.eof);
    assert_eq!(next.events.len(), 1);
    assert_eq!(next.events[0]["text"], "compare these photos");
    assert!(!next.events[0].to_string().contains("image_url"));
    Ok(())
}

#[test]
fn claude_excludes_thinking_and_injected_context_but_preserves_tool_errors() -> Result<()> {
    let mut file = Builder::new().suffix(".jsonl").tempfile()?;
    let records = [
        json!({"type":"user","timestamp":TIME,"sessionId":"session","uuid":"user","message":{"role":"user","content":[{"type":"text","text":"<system-reminder>hidden</system-reminder>"},{"type":"text","text":"用户问题"}]}}),
        json!({"type":"assistant","timestamp":TIME,"sessionId":"session","uuid":"assistant","message":{"role":"assistant","content":[{"type":"thinking","thinking":"hidden"},{"type":"text","text":"用户可见答复"}]}}),
        json!({"type":"user","timestamp":TIME,"sessionId":"session","uuid":"result","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_123","is_error":true,"content":"failed"}]}}),
    ];
    for record in records {
        writeln!(file, "{record}")?;
    }
    let page = read_page("claude-code", file.path(), 0, 100)?;
    assert_eq!(page.events.len(), 3);
    assert_eq!(page.events[0]["text"], "用户问题");
    assert_eq!(page.events[1]["text"], "用户可见答复");
    assert_eq!(page.events[2]["checks"]["output"]["is_error"], true);
    assert_eq!(page.events[2]["checks"]["output"]["result"], "failed");
    assert!(!serde_json::to_string(&page.events)?.contains("hidden"));
    Ok(())
}

#[test]
fn claude_distinguishes_prompt_text_from_notices_and_replays_occupied_turns() -> Result<()> {
    let session = "fixture";
    let prompt_id = "same-turn";
    let mut file = Builder::new().suffix(".jsonl").tempfile()?;
    for (uuid, content) in [
        (
            "stdout",
            json!("<local-command-stdout>output</local-command-stdout>"),
        ),
        (
            "notification",
            json!("<task-notification>done</task-notification>"),
        ),
        ("interrupted", json!("[Request interrupted by user]")),
        (
            "real",
            json!([{"type":"text","text":"<ide_opened_file>file</ide_opened_file>"},{"type":"text","text":"first question"}]),
        ),
        ("follow-up", json!("second question")),
    ] {
        writeln!(
            file,
            "{}",
            json!({"timestamp":TIME,"type":"user","sessionId":session,"promptId":prompt_id,"uuid":uuid,"message":{"role":"user","content":content}})
        )?;
    }
    let events = read_page("claude-code", file.path(), 0, 100)?.events;
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["text"], "first question");
    assert_eq!(events[1]["text"], "second question");
    let hook = crate::host::normalize(
        "claude-code",
        json!({
            "hook_event_name":"UserPromptSubmit", "session_id":session, "prompt_id":prompt_id,
            "prompt":"first question", "at":TIME,
        }),
    )?;
    assert_eq!(
        hook["events"][0]["native_object"],
        events[0]["native_object"]
    );

    for legacy_echo in [false, true] {
        let mut db = rusqlite::Connection::open_in_memory()?;
        crate::authority::migrate(&db)?;
        crate::context::migrate(&db)?;
        crate::ingestion::migrate(&db)?;
        crate::retrieval::migrate(&mut db)?;
        let source = crate::ingestion::register(
            &mut db,
            json!({"type":"claude-code","account":"local","actors":["owner"]}),
        )?;
        let source_id = source["source_id"].as_str().unwrap();
        crate::ingestion::set_capture(&db, source_id, true)?;
        let mut accept = |key: &str, prev: &str, events: &Value| {
            crate::ingestion::accept(
                &mut db,
                "owner",
                key,
                json!({
                    "source_id":source_id,"cursor":prev,"next_cursor":key,"events":events,
                }),
                "balanced",
            )
        };
        let mut prior = hook["events"].clone();
        if legacy_echo {
            prior[0]["text"] = json!("<local-command-stdout>legacy output</local-command-stdout>");
        }
        accept("first", "", &prior)?;
        assert_eq!(
            accept("second", "first", &json!(events))?["jobs_created"],
            if legacy_echo { 2 } else { 1 }
        );
        assert_eq!(
            accept("replay", "second", &json!(events))?["jobs_created"],
            0
        );
    }
    Ok(())
}

#[test]
fn dsh_compressed_history_resumes_and_agrees_with_live_identity() -> Result<()> {
    let mut file = Builder::new().suffix(".jsonl.zstd").tempfile()?;
    let rows = [
        json!({"type":"session","id":"test-session"}),
        json!({"type":"user/message","time":TIME,"seq":1,"data":{"id":"dsh-user","source":{"kind":"user-rpc"},"content":"原生用户消息"}}),
        json!({"type":"assistant/message","time":TIME,"seq":2,"data":{"message":{"id":"dsh-assistant","source":{"kind":"model"},"content":[{"type":"text","text":"原生答复"}]}}}),
    ];
    let plain = rows
        .iter()
        .map(|row| format!("{row}\n"))
        .collect::<String>();
    file.write_all(&zstd::stream::encode_all(plain.as_bytes(), 1)?)?;
    let first = read_page("dsh", file.path(), 0, 1)?;
    assert_eq!(first.events.len(), 1);
    let live = crate::host::normalize(
        "dsh",
        json!({
            "session_id":"test-session","prompt_id":"dsh-user",
            "hook_event_name":"UserPromptSubmit","prompt":"原生用户消息","at":TIME
        }),
    )?;
    assert_eq!(
        first.events[0],
        json!({
            "native_object":live["events"][0]["native_object"], "native_version":"1",
            "actor_kind":"user_message","at":"2026-11-20T00:10:00+00:00","text":"原生用户消息"
        })
    );
    let rest = read_page("dsh", file.path(), first.offset, 100)?;
    assert!(rest.eof);
    assert_eq!(rest.events.len(), 1);
    assert_eq!(rest.events[0]["text"], "原生答复");
    assert_eq!(rest.offset, plain.len() as u64);
    Ok(())
}
