use super::*;

#[test]
fn accepts_user_envelopes_and_rejects_nonadvancing_tokens() {
    let response = json!({"ok":true,"identity":"user","data":{
        "chats":[{"chat_id":"oc_fixture"}],"has_more":true,"page_token":"next"
    }});
    assert_eq!(
        page(&response, "chats", None).unwrap().1.as_deref(),
        Some("next")
    );
    assert!(page(&response, "chats", Some("next")).is_err());
    let empty = json!({"ok":true,"identity":"user","data":{"has_more":false}});
    assert!(page(&empty, "items", None).unwrap().0.is_empty());
    assert!(
        page(
            &json!({"ok":true,"identity":"bot","data":{}}),
            "items",
            None
        )
        .is_err()
    );
}

#[test]
fn maps_edited_text_and_recalled_messages_with_stable_ids() {
    let mut item = json!({
        "message_id":"om_fixture","msg_type":"text","create_time":"1763337600000",
        "update_time":"1763337608000","sender":{"sender_type":"user"},
        "body":{"content":"{\"text\":\"已经复验\"}"},
    });
    let event = normalize(&item, false).unwrap().unwrap();
    assert_eq!(event["actor_kind"], "user_message");
    assert_eq!(event["native_object"], "om_fixture");
    assert_eq!(event["native_version"], "1763337608000");
    assert_eq!(event["change_kind"], "updated");
    assert_eq!(event["text"], "已经复验");
    item["deleted"] = json!(true);
    let revoked = normalize(&item, false).unwrap().unwrap();
    assert_eq!(revoked["native_object"], event["native_object"]);
    assert_eq!(revoked["change_kind"], "revoked");
    assert!(revoked.get("text").is_none());
}

#[test]
fn bulk_import_keeps_attachment_record_without_the_download_key() {
    let item = json!({
        "message_id":"om_document","msg_type":"file","create_time":"1763337600000",
        "sender":{"sender_type":"user"},
        "body":{"content":{"file_name":"fixture.pdf","file_key":"private-fixture-key"}},
    });
    assert!(normalize(&item, false).unwrap().is_none());
    let event = normalize(&item, true).unwrap().unwrap();
    assert_eq!(event["native_object"], "om_document");
    assert_eq!(event["text"], "飞书文件 · fixture.pdf");
    assert!(!event.to_string().contains("private-fixture-key"));
}

#[test]
fn retains_visible_rich_text_without_attachment_artifacts() {
    let rich = json!({"zh_cn":{"title":"项目决策","content":[[
            {"tag":"text","text":"详细说明"},
            {"tag":"img","image_key":"img_fixture"}]]}});
    assert_eq!(rich_text(&rich), "项目决策\n详细说明");
}

#[test]
fn maps_recoverable_errors_without_returning_upstream_identifiers() {
    let stale = r#"{"error":{"code":230001,"message":"ext=invalid page_token: fixture"}}"#;
    let missing = r#"{"error":{"code":230001,"message":"ext=invalid container_id: oc_fixture"}}"#;
    assert_eq!(cli_error(stale).unwrap().code, "feishu_cursor_expired");
    let error = cli_error(missing).unwrap();
    assert_eq!(error.code, "feishu_container_unavailable");
    assert!(!error.message.contains("oc_fixture"));
}

#[cfg(unix)]
#[test]
fn reads_an_individually_paginated_thread_as_the_registered_user() {
    use std::os::unix::fs::PermissionsExt;

    let fixture = tempfile::tempdir().unwrap();
    let binary = fixture.path().join("lark-cli");
    let status = json!({"brand":"feishu","appId":"cli_fixture","identities":{"user":{
        "openId":"ou_fixture","available":true,"status":"ready"
    }}});
    let reply = json!({"ok":true,"identity":"user","data":{
        "items":[{"message_id":"om_fixture","msg_type":"text","create_time":"1763337600000",
            "sender":{"sender_type":"user"},"body":{"content":{"text":"fixture"}}}],
        "has_more":true,"page_token":"next"
    }});
    std::fs::write(
        &binary,
        format!(
            r#"#!/bin/sh
case "$1 $2" in
  'auth status')
    [ "$4 $5" = '--profile pinned' ] || exit 3
    printf '%s' '{status}'
    ;;
  'api GET')
    [ "$3 $4" = '/open-apis/im/v1/messages --params' ] || exit 4
    [ "$6 $7 $8 $9 ${{10}} ${{11}}" = '--as user --format json --profile pinned' ] || exit 5
    case "$5" in *'"sort_type":"ByCreateTimeDesc"'*) ;; *) exit 6 ;; esac
    case "$5" in *'"container_id_type":"thread"'*) ;; *) exit 7 ;; esac
    case "$5" in *'"page_token":"resume"'*) ;; *) exit 8 ;; esac
    case "$5" in *'"start_time"'*|*'"end_time"'*) exit 9 ;; esac
    printf '%s' '{reply}'
    ;;
  *) exit 10 ;;
esac
"#
        ),
    )
    .unwrap();
    std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o700)).unwrap();
    let account = Account {
        binary,
        profile: "pinned".into(),
        identity: feishu::authorized_account(&status).unwrap(),
    };
    let page = account
        .thread_messages("omt_fixture", Some("resume"), &|| false)
        .unwrap();
    assert_eq!(page.oldest_timestamp, Some(1763337600));
    assert_eq!(page.next_token.as_deref(), Some("next"));
    assert_eq!(page.events[0]["native_object"], "om_fixture");
}
