#!/bin/sh
case "$1:$2" in
  profile:list) printf '%s' '[{"brand":"feishu","name":"fixture","active":true}]' ;;
  auth:status)
    printf '{"brand":"feishu","appId":"fixture-app","identities":{"user":{"available":true,"status":"%s","openId":"fixture-user","userName":"测试飞书账号"}}}' "${BMEM_TEST_FEISHU_STATUS:-ready}" ;;
  auth:login)
    if [ "$3" = '--device-code' ]; then
      printf '%s' '{"ok":true}'
    else
      printf '%s' '{"verification_url":"https://accounts.feishu.cn/fixture","device_code":"fixture-code","expires_in":30}'
    fi ;;
  im:+chat-list)
    case " $* " in
      *' --types p2p '*' --page-token '*)
        printf '%s' '{"ok":true,"identity":"user","data":{"chats":[{"chat_id":"oc_private_2","chat_mode":"p2p","name":"Older private"}],"has_more":false}}' ;;
      *' --types p2p '*)
        printf '%s' '{"ok":true,"identity":"user","data":{"chats":[{"chat_id":"oc_private","chat_mode":"p2p","name":"Fixture private"}],"has_more":true,"page_token":"dm-next"}}' ;;
      *' --page-token '*)
        if [ -n "${BMEM_TEST_FEISHU_DISCOVERY_FAILURE_FILE:-}" ] &&
           [ -e "$BMEM_TEST_FEISHU_DISCOVERY_FAILURE_FILE" ]; then
          exit 1
        fi
        printf '%s' '{"ok":true,"identity":"user","data":{"chats":[{"chat_id":"oc_group_2","chat_mode":"topic","name":"Older group"}],"has_more":false}}' ;;
      *)
        printf '%s' '{"ok":true,"identity":"user","data":{"chats":[{"chat_id":"oc_group","chat_mode":"group","name":"Fixture group"}],"has_more":true,"page_token":"group-next"}}' ;;
    esac ;;
  drive:+search)
    case " $* " in
      *' --page-token '*)
        printf '%s' '{"ok":true,"identity":"user","data":{"results":[{"result_meta":{"url":"https://demo.feishu.cn/wiki/wikcnFixture"},"title":"Wiki document"},{"result_meta":{"url":"https://demo.feishu.cn/docs/doccnLegacy"},"title":"Legacy document"}],"has_more":false}}' ;;
      *) printf '%s' '{"ok":true,"identity":"user","data":{"results":[{"result_meta":{"url":"https://demo.feishu.cn/docx/doxcnFixture"},"title":"Fixture document"}],"has_more":true,"page_token":"docs-next"}}' ;;
    esac ;;
  wiki:+node-get)
    printf '%s' '{"ok":true,"identity":"user","data":{"obj_type":"docx","obj_token":"doxcnWiki","obj_create_time":"1763337600"}}' ;;
  api:GET)
    case "$3" in
      */im/v1/messages)
        case "$*" in
          *'omt_fixture'*) mid=om_thread ;;
          *'oc_private_2'*) mid=om_dm_second ;;
          *'oc_private'*) mid=om_dm_first ;;
          *'oc_group_2'*'group-message-next'*) mid=om_group_third ;;
          *'oc_group_2'*)
            printf '%s' '{"ok":true,"identity":"user","data":{"items":[{"message_id":"om_group_second","thread_id":"omt_fixture","msg_type":"text","create_time":"1763337600000","sender":{"sender_type":"user"},"body":{"content":{"text":"Fixture topic root"}}}],"has_more":true,"page_token":"group-message-next"}}'
            exit 0 ;;
          *) mid=om_fixture ;;
        esac
        printf '{"ok":true,"identity":"user","data":{"items":[{"message_id":"%s","msg_type":"text","create_time":"1763337600000","sender":{"sender_type":"user"},"body":{"content":{"text":"Fixture Feishu message"}}}],"has_more":false}}' "$mid" ;;
      */docx/v1/documents/doxcnWiki/raw_content) printf '%s' '{"ok":true,"identity":"user","data":{"content":"Wiki fixture contents"}}' ;;
      */doc/v2/doccnLegacy/raw_content) printf '%s' '{"ok":true,"identity":"user","data":{"content":"Legacy fixture contents"}}' ;;
      */raw_content) printf '%s' '{"ok":true,"identity":"user","data":{"content":"Fixture Feishu document"}}' ;;
      */documents/*) printf '%s' '{"ok":true,"identity":"user","data":{"document":{"revision_id":7,"create_time":"1763337600"}}}' ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
