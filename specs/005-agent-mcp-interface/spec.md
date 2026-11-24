# T045 契约冻结 · Agent MCP 接口与第一期收尾

状态：**待用户签字**（签字闸门见 §0 与 §16）。起草：SUB-opus（T045）；本版为 T045 合并者按四份评审（opus-authz / opus-executable / codex 外部实跑 / dsh 外部实跑）逐条亲核 file:line 后的合并修订版；复核 JSON 落 `specs/005-agent-mcp-interface/reviews/T045-{opus,cx,dsh}.json`（tasks.md T045 验收证据）。
代码基线：HEAD `41e7daeeda484692c054daecfed6aa144d793b0a`（本人 `git rev-parse` 核对，工作树仅本文与 schemas/ 未入库）；门禁基线 T044 完成记录绑定 `04da93e8633c`（tasks.md T044）。本文所有 file:line 均在上述 HEAD 上亲自打开核对；与 plan/tasks 原引用行号漂移处在 §2 注明。
配套冻结件：`schemas/matrix.schema.json`（C13）、`schemas/patch.schema.json`（C14）。本文与 patch-format.md 冲突时以本文为准（patch-format.md:4 既有约定）。

## 0. 生效闸门与签字范围

1. 本文经用户在 §16 签字后生效；**签字前波 1（T046/T047）不开**（tasks.md:144 验收状态）。
2. 签字必须逐项确认 plan.md §八 的锁定值：§八.5 三项隐私语义（a/b/c）、§八.6 首发口径、§八.7 T056 拍板与 HTTP 直连条件路由触发（含本文 §15.9 对桥传输路由的必然读法）、§八.10 幂等键来源（本文 §11.1 按工具类分立读法）、§八.11 task bootstrap 二选一（tasks.md T045 验收证据）。本文 §5–§12 已把这些锁定值写成可判定条文；签字即冻结，改值须按 plan.md §八 开头规则在该节登记变更、日期与受影响包号。
3. 本文条款编号 C01–C17 是验收矩阵 `acceptance_row` 的合法定位符（C13 schema 已固化该用法）。
4. 本轮合并新增/收紧的交付（C06.6 change_kind 限制、C07.1a task_id 门、C09 请求单存储与读回、C05 轮换分段发布、T046 user_version 门、C08 agent preview 主体绑定、§7.3 loopback 硬校验、429 交付链、C01 R1 负对照等）均为目标语义，实码今天不存在；归属包文件清单的扩充由 ASM 在 tasks.md 台账修订登记（本轮 worker 禁区），逐条见 §15。

## 1. 范围与非目标（C01）

| 项 | 冻结内容 | 依据 |
|---|---|---|
| 主线 | 把 `bmem mcp` 从 2 个只读工具（search/expand）扩成完整 agent 接口，顺带闭合第一期 P0 首切验收；R1＝MVP 11 项，R2＝一期全量闭合＋治理面 | plan.md:3-5、§二；host.rs:14-29 现状 2 工具 |
| 协议 | 沿用 MCP，rmcp 3.11，stdio 先行；legacy-era ≤2025-11-25（C15） | plan.md §一；Cargo.toml:28；preflight/rmcp_spike.json `max_supported=2025-11-25` |
| 能力面 | 全量冻结（读＋回执＋写入＋治理），分期暴露；未上线动作登记 `available=false`＋`reason` | plan.md §一；extension-contract.md:20 |
| 写入信任 | 外部 agent 写入只进候选/观察层，不直接成为当前有效声明；不复用 owner-only `POST /v2/statements` | plan.md §一；server.rs:521-527 owner 门 |
| 访问边界 | **只允许本机**：服务只 bind 回环、Host 头精确匹配、`X-Bmem-Instance` 核对，三者不变 | server.rs:81（`Ipv4Addr::LOCALHOST`）、:146-155（Host 精确匹配，在 fallback 内）、:387-391（instance 核对）；client.rs:21-23 客户端侧回环强制 |
| 非目标 | 远程/跨机接入、新 era（2026-07-28）server/discover 与强制头、sampling/roots 依赖、T064 第三方 MCP 试点本轮均不做 | plan.md §一/§六；protocol-choice.md §四/五；tasks.md T064 |
| 条件项 | T051b（resources/prompts）、T056（Streamable HTTP）默认不开工；`/v2/agent/candidates` 作为 **stdio 桥传输路由由 T053 无条件交付**（§3.1 candidate.submit 行、§15.9）；「HTTP 直连」签字仅决定该路由是否另作为自研 agent 的公开直连入口登记与验收 | plan.md §八.7；tasks.md T053 条件条款；host.rs:122 桥仅 HTTP 转发（本人核） |

**禁止**把任何非回环地址暴露给 agent 接口。检查点（R1 默认即可跑，不依赖条件包 T056）：`tests/v2/authority_harness.rs::wrong_host_header_403`（T046 交付：对回环地址发 `Host: evil.example` 的原始 HTTP 请求，断言 403 action_denied 且错误体不含调用者输入）、`instance_header_mismatch_409`（篡改 X-Bmem-Instance，断言 409 且 message 不回显 header 值）、`non_loopback_connect_refused`（向本机非回环地址连接被拒）；`test_integration.py:132` 回环 bind 正断言既有；`tests/v2/test_mcp_http.py`（若 T056 开工）`wrong_host_403`/`cross_origin_rejected`；client.rs:21-23 对非回环服务地址直接 `action_denied`。变异＝Host 检查移出 fallback（server.rs:146-155）或 :148 的 `!=` 改「仅要求存在 Host 头」后 wrong_host_header_403 必须红；删 :387 instance 核对后 instance_header_mismatch_409 必须红；server.rs:81 改 `Ipv4Addr::UNSPECIFIED` 后 non_loopback_connect_refused 必须红。

## 2. 已核代码事实基线（引用行号以本表为准）

「当前实码」列均在本文基线 HEAD 上打开核对；「plan/tasks 原引用」列为**历史引用**（记录 plan.md/tasks.md 原文所引行号，可能已漂移甚至失效，如 native_hosts.py:525 在该文件重构后已不存在），仅作对照，正文断言一律以「当前实码」列为准。

| # | 事实 | 当前实码（已核） | plan/tasks 原引用 | 影响条款 |
|---|---|---|---|---|
| F1 | `actor_kind` 由事件正文给出，仅枚举校验；不存在可覆盖的「已认证 actor_kind」 | ingestion.rs:1149-1156（require_str＋ACTOR_KINDS）、:1201-1222（按 kind 分派内容形状）、:1599-1610（planned 结果直接入库）；注释 :77-78、:1187-1188 声明意图但无任何代码路径从身份获得 actor_kind。**变异点必须以 T053 新增的 agent_intake 角色/形状推导函数为唯一点名**，不得以 ingestion.rs:1149-1156 为变异点（该段在 agent 路径被服务端注入绕过，删它不红） | ingestion.rs:1149-1152 | C06 |
| F2 | bound 客户端被锁死在 search 一条；全仓 capabilities() 仅 7 个 command_id | server.rs:1024-1043（bound 仅 :1031 search.query，available=shared_enabled :1026-1030；own 另 6 条 :1033-1040，含 statement.list→/v2/statements :1038） | server.rs:1021-1040 | C02/C04 |
| F3 | `change_grant` 的 task_delivery 可向 recall_grants 插任意 (source_id, client_id)；同路由可改他人 share/开 extract | retrieval.rs:640-653（recall_grants）、:663-674（share/task_access）、:624-638（extract/embed） | retrieval.rs:640-678 | C08 |
| F4 | `governance_operations` 仅 id/request_key/request/response 四列，request_key 全局 UNIQUE；confirm 回放单键；`governance_previews` 仅 id/request/footprint 三列、**无主体列** | retrieval.rs:26-28（表）、:23-25（previews）、:571-588（回放）、:695（插入） | retrieval.rs:24-28/:573/:695 | C08/C12 |
| F5 | history 路由全 owner-only；全量导入以 actor=owner 摄取；history_task_jobs 无 request_id 列 | server.rs:730-792（history 各路由 owner 门）；history_tasks.rs:65-77（表）、:438 `bind_actor_in(…,"owner")` | server.rs:727-737 | C09 |
| F6 | 客户端 token 不可轮换不可解绑：bind 查询无 revoked_at/credential_version；context_clients 无 role 列 | context.rs:247-260（SQL 在 :249，查无行 401 :254-260）；context_clients 表 :33-37 | context.rs:249 | C05/C11 |
| F7 | 全仓无速率控制与 429；error_response 无任何 Retry-After 头写入点 | 本人 rg `429|rate_limit|Retry-After` 于 src/v2/bmem/src 零命中；error.rs:55-62 retryable 仅 503；server.rs:301-307 error_response 仅 status＋Json(err.value()) | plan §八.1 | C10 |
| F8 | MCP 桥写转发 key=None；client 不发 Idempotency-Key；写路由 `key(headers)?` 必 422 | host.rs:122（末参 None）；client.rs:59-61；server.rs:420-424 key() | host.rs:122/client.rs:59-61 | C12 |
| F9 | 409 子码被消息前缀压扁；io/rusqlite/serde 错误直传 to_string() | error.rs:27-37（前缀映射）、:65-81（三个 From） | error.rs:27-37/:65-81 | C03 |
| F10 | P0 装入凭证产品路径拿不到：宿主适配器只调 /v2/context/load；receipts/outcomes 唯一调用者是合成客户端 | main.rs:1190；test_integration.py:430 用例、:516 receipt 路径、:543 outcomes 路径；test_native_hosts.py:202-204 断言 `delivery list` 行 loaded/used==unknown（该 unknown 出自 context.rs:1257-1258 deliveries() 投影，**不是** delivery_ledger 的 {pending,partial,observed}，两套词表不得混用） | main.rs:1190/test_integration.py:506/:533/native_hosts.py:525 | C02/C17 |
| F11 | 凭据库仅 database/env 两 backend；Keychain 未实装；"system" 引用只做形状校验、无解析代码 | settings.rs:249-272（:252 database、:262 env）；:1246-1298 validate_credential_ref（:1266 system 形状）；本人 rg keychain/wincred/secret-service 于 src 零命中 | settings.rs:252/262 | C11/§15 |
| F12 | `PATCH /v2/clients/{id}`（client.configure）目录已承诺但未实装；CLI Clients 子命令仅 List/Register，无 rotate/unbind/configure | server.rs 全文仅 :502 一处 PATCH（config）；main.rs:342-349 enum Clients {List, Register}、:539-540 分派 | command-catalog.md:58 | C05/§15 |
| F13 | 治理 preview 也要求 Idempotency-Key；CLI post() 缺省自铸 UUID；**preview 路径不做键回放、键不落 governance_operations**（仅 confirm 回放） | server.rs:966（preview/confirm 共用 key(headers)?）；main.rs:748-756 post()；retrieval.rs:570-589（`if !preview` 才回放） | — | C12 |
| F14 | 工具名断言 tools==["search"] 现在 test_native_hosts.py:255（T044 重构后位置）；wire_api="responses" 在 native_hosts.py:306 | test_native_hosts.py:255；native_hosts.py:306 | native_hosts.py:367/:248 | C02/C17 |
| F15 | register 默认能力 {load_context:true}；install 原样复用旧 client_id | context.rs:198-201；integrations.rs:211-216（复用）、:218-224（写死 load_context）；main.rs:1054 同写死 | integrations.rs:215/:222/main.rs:1054 | C04 |
| F16 | confirm 幂等键形态 grant-confirm-{preview_id}；preview 一次性消费 | main.rs:1038；retrieval.rs:698-700（confirm 后 DELETE preview） | main.rs:1038 | C08/C12 |
| F17 | 治理实现仅 change_grant/forget/stop_capture/resume_capture 四 kind；其余 kind 404；stop/resume 的 confirm **不做足迹比对**；仅 change_grant/forget 强制 preview_id | retrieval.rs:319-344 preview_footprint（:336-337 注释、:342 其余 missing）、:599-616（kind 判定）；server.rs:916-936 backup/restore_preview 直连分支 | — | C02/C08 |
| F18 | 来源页接受的回放查询**先于**入口授权（accept 绑定/capture）执行；accept_page 同步写集含 recall 索引与游标；revoked 页对任何 bound actor 开放并触发 withdraw_root 永久墓碑 | ingestion.rs:482-497（回放）先于 :500-525（绑定/capture）；:559-642 写 ing_event_versions/ing_jobs/ing_sources.cursor(:609-612)/recall refresh(:625→retrieval.rs:101-110)/ing_ingests；ing_roots 在 plan_event :1254-1260；:539-544 revoked→withdraw_root(:1643-1658)→erase_event(:1630-1641)；plan_event :1279-1289 无角色门 | — | C06/C07/C12 |
| F19 | 来源注册默认 extract inactive；set_capture 只翻转 ing_grants、无停止发起人记录；ing_sources 有 kind 列；事件无 category 字段（categories 仅来源级） | ingestion.rs:272-275（extract 0）、:343-360（set_capture）、:101-106（kind 列）、:1140-1186（plan_event 字段集无 category） | — | C06/C07/C08 |
| F20 | database() 逐次执行各 migrate，**全仓无 PRAGMA user_version 检查**；stdio 桥是独立进程、仅 HTTP 转发；mcp serve 与 hook 共用 run/clients/<id>.json 凭据 | server.rs:40-61；本人 rg user_version 于 src 零命中；main.rs:1087-1096（读凭据文件→Client::bound→host::serve）；host.rs:122 | — | C05/C12/§15 |

## 3. 动作表（C02）

### 3.1 agent 可见动作总表

只列代码真实存在或本计划明确新增的路由。新增路由在「路由」列标 **新**＋归属包。「能力位」列词表见 §4.2；内容级授权（shared/recall_grants/task_access）独立于能力位，两层都过才放行。「键来源」按工具类分立规则见 C12。错误映射通用规则见 §3.3，行内只列特有分支。

| 工具名（MCP） | command_id | HTTP 路由（实码） | 所需能力位 | release_round | 幂等键来源与作用域 | 特有错误分支 |
|---|---|---|---|---|---|---|
| `search` | search.query | POST /v2/search（server.rs:636-658） | search（且 shared_enabled，见 §4.1） | R1 | 只读，无键 | bound route_ref 不符 403（:640-645）；shared 未授权 403（:646-650）；缺 query 422 |
| `expand` | evidence.expand | POST /v2/evidence/expand（server.rs:659-668） | evidence_expand；内容仍经 recall_grants×task_access（retrieval.rs:249-252） | R1 | 只读，无键 | 无权/不存在句柄一律 404 无正文（retrieval.rs:253-255） |
| `statement.list` | **agent.statement.list**（v1；owner 的 statement.list v1 保持 GET /v2/statements 不变，command-catalog.md:73、server.rs:1038 已冻结该 ID，extension-contract.md:22 ID＋主版本唯一） | **新** GET /v2/agent/statements（T051a 装配补丁；shared 投影 authority::current(shared_only=true)，authority.rs:309-363） | statement_list **且 shared_enabled**（与 search 同门，§4.1 补充 1） | R1 | 只读，无键 | 投影字段冻结见 §7 第 5 条；shared 未授予 403（T051a 实现与 server.rs:646-650 同构的门）；**禁止**复用 owner-only GET /v2/statements（server.rs:528-540）。MCP 工具名 statement.list 显式映射到 agent.statement.list |
| `task.handoff` | task.handoff | GET /v2/tasks/{id}/handoff（server.rs:624-635） | task_handoff；内容经 task_access（context.rs:318-324） | R1 | 只读，无键 | 无 task_access 404（server.rs:627-629）；返回形状冻结＝{task, evidence[], observations[], verification:"per_source_observation"}（retrieval.rs:259-301 既有，不新增分组字段）；「计划 vs 实测」区分＝observations[].actor_kind∈{plan,tool_result}（authority.rs:447-451 写入形态），verification 字面量不得由 agent 参数影响 |
| `delivery.ledger.self` | delivery.ledger.self | **新** GET /v2/agent/deliveries（T051a 装配补丁；**新函数** `context::delivery_ledger_for(db, client_id, loaded, used, page)`＝delivery_ledger 的 filters 追加 `AND d.client_id=?`，其余查询/排序/LIMIT/summary 语义逐字节等同；原 delivery_ledger(db,host,loaded,used,page) 无 client_id 入参，context.rs:1283-1290 已核，不得在原签名上「强制」） | delivery_ledger_self | R1 | 只读，无键 | 只返回本客户端行（过滤在服务端）；loaded_state∈{pending,partial,observed}、used_state∈{pending,observed}（context.rs:1366-1370 ledger 投影；`unknown` 属 deliveries() 列表投影 context.rs:1257-1258，不在本工具词表）；检查点 `mcp_surface.rs::delivery_ledger_self_only_own_rows`（2 客户端各 1 行，断言仅返回自身行） |
| `delivery.receipt` | delivery.receipt | POST /v2/deliveries/{delivery_id}/receipts（server.rs:593-596） | kind=loaded→observe_loaded；kind=used→observe_used（context.rs:975-980） | R1 | 正文 event_id；作用域 (client_id,event_id)（context.rs:72-77 PK） | 无能力位 403；epoch/snapshot/package/route 不符 409（context.rs:993-1000）；policy 变更 409（:1009-1011）；used 无同 model_call_ref 在先 loaded 409（:1068-1085）；used 缺 revision_ids/evidence_ref 422（:981-985） |
| `attempt.outcome` | attempt.outcome | POST /v2/attempts/{attempt_id}/outcomes（server.rs:620-623） | verify_outcomes（context.rs:1106-1108） | R1 | 正文 event_id；作用域 (verifier_id,event_id)（context.rs:79-83 PK） | verifier=attempt 主体 403（context.rs:1155-1159）；无 task_access 404（:1160-1162）；status∈{passed,failed,unknown}（:1111） |
| `candidate.submit` | candidate.submit | **新** POST /v2/agent/candidates（T053 **无条件交付**：stdio 桥是独立进程、仅 HTTP 转发（F20），MCP 路径＝桥携 Bearer＋Idempotency-Key 转发本路由→agent_intake 处理器；「HTTP 直连」签字仅决定是否另登记公开直连入口，§15.9）。输入/回执形状冻结见 §6 规则 1 与 §7 | candidate_submit | R1 | Idempotency-Key 必填；调用方入参＞桥派生 UUID；作用域 (source_id,actor,idem_key)（ingestion.rs:482-497 形态） | actor_kind 出现 403（C06.1）；change_kind=revoked 403（C06.6）；task_id 越权 403 整页回滚（C07.1a）；越类别 403 整页回滚（C06.3）；非 intake 来源/未绑定 403（C07.0）；单页超配额 413（C10）；限流 429＋Retry-After（C10）；未知信封字段 422；调用方传 cursor/next_cursor 422（服务端派生）；重放前当前授权复核失败 403（C12/CX-11，§9.1） |
| `task.create` | task.create | POST /v2/tasks（server.rs:601-609，已存在） | manage_tasks（context.rs:370-372） | R2（T054 桥接工具；HTTP 今天可用） | key 必填；context_intents(actor,"task.create",key)（context.rs:38-42,:380） | 无能力位 403；creator 记录为调用者（context.rs:377,:385-386） |
| `attempt.start` | attempt.start | POST /v2/tasks/{id}/attempts（server.rs:610-619，已存在） | load_context（context.rs:422-427）＋task_access（:443） | R2（T054 桥接工具） | key 必填；context_intents(actor,"attempt.create",key)（context.rs:446） | route/client 不符 403；task/route 不可用 404 |
| `operation.preview` | operation.preview | POST /v2/operations/preview（server.rs:912-979；T054 装配补丁加 bound 白名单分支） | operation_preview（agent 限 §8 白名单） | R2（T054） | key 必填（server.rs:966）；调用方入参＞桥派生 UUID；**preview 键不落 governance_operations、不做键回放**（F13），跨主体碰撞无载体 | 白名单外 403＋governance_previews/governance_operations 零增量；extract/embed allow 无就绪模型 409 model_not_ready（server.rs:948-961）；agent preview 记录主体（C08 §8.2.2） |
| `operation.confirm` | operation.confirm | POST /v2/operations（server.rs:912-979；同上） | operation_confirm（依赖 operation_preview，§4.2） | R2（T054） | **键恒为 grant-confirm-{preview_id}**（main.rs:1038 形态）；不接受调用方 idempotency_key（桥忽略不转发）；bound 治理分支拒绝非 grant-confirm-* 键 422；R1 全局 request_key 局限见 C12 | 足迹变化 409 revision_changed（retrieval.rs:611-615）；preview 一次性（:698-700）；change_grant/forget 无 preview_id 422（:599-603）；agent 路径四 kind 均须 preview_id＋主体匹配（§8.2.2） |
| `history.import.request` | history.import.request | **新** POST /v2/agent/history/requests（T054；请求单形态见 C09） | history_import_request（owner 显式授予，非默认） | R2（T054） | key 必填；作用域 (client_id,idem_key)（C09 表 UNIQUE） | 202 返回 request_id＋state=pending_owner；**不创建 history_task_jobs 行**（F5 owner-only 不变） |
| `history.import.request.read` | history.import.request.read | **新** GET /v2/agent/history/requests/{request_id}（T054；仅返回调用者创建的请求单） | history_import_request | R2（T054） | 只读，无键 | 他人请求单 404；返回 {request_id, state∈{pending_owner,confirmed,rejected}, job_id?}（C09） |
| （无 MCP 工具，hook/HTTP 生命周期） | context.load（目录未给 command_id，沿用能力位名） | POST /v2/context/load（server.rs:578-592） | load_context（context.rs:728-732） | R1 既有 | 可选 key（server.rs:588）；(client_id,request_key)（context.rs:63） | route 不符 403（context.rs:733-735）；预算不足 deferred＋reason=statement_budget（context.rs:801-804,:859-865）；append_only 撤权 409 fresh_session_required（context.rs:690-695,:711-715） |

冻结补充：

1. **工具名兼容**：R1 保留现有工具名 `search`/`expand`（host.rs:14-29），其余新工具名＝command_id（statement.list 工具名映射 agent.statement.list 例外见行内）。理由：T050 不得改 test_native_hosts.py，而 tools==["search"] 断言（test_native_hosts.py:255）由 T052 改能力位驱动（tasks.md T050/T052）；提前改名会击穿波 2 门禁。检查点：T050 `tools_list_matches_capabilities_same_identity` 以「同一身份下可用 command_id 集合↔工具集合」比对，search/expand 映射 search.query/evidence.expand。
2. **available=false 登记**：R2 动作在 R1 注册表登记 `available=false`＋`reason`（extension-contract.md:20），工具面不出现；T051b/T056 未过门槛同此（reason='awaiting named client'，tasks.md T051b）。
3. **GET /v2/tasks 保持 owner-only**（server.rs:597-600）：agent 不获得全量任务列表；任务发现经自己创建的 task.create 回执与 task.handoff（需 task_access）。
4. **POST /v2/events 仅向 owner 与 host_adapter 开放**（受信适配路径，ing_actors accept 绑定为前提，ingestion.rs:500-512；host_adapter 的正文 actor_kind 按角色白名单校验，C06.2）。其他 bound 身份（含 agent_writer）POST /v2/events → 403 action_denied＋零增量；agent_writer 写面唯一为 candidate.submit（§4.1 写面列），C10 限流因此覆盖 agent 全部写入入口。`x-bmem-import: file`/`?import=true` 手工导入强制 owner（server.rs:869-877）。**禁止**把裸 /v2/events 当 agent 写面（plan.md §六配套约束）。检查点：`agent_intake.rs::agent_writer_events_route_403`（变异＝放开角色判定后必须红）。
5. 条件包 T056 的 `/mcp` 挂载若开工，按 §12.4 入口规格执行，不在本表新增动作。

### 3.2 owner-only 动作（agent 一律 403，白名单外治理同此）

以下路由今天全部 owner 门（实码已核），R1/R2 均不向 bound 身份开放（除 §8 白名单经 T054 装配补丁开放的治理子集）：

| 域 | 路由（server.rs 行号） |
|---|---|
| 声明 | POST /v2/statements :521；GET /v2/statements :528；GET /v2/facts/{id}/history :541 |
| 客户端管理 | POST /v2/clients :545；GET /v2/clients :552；**新** rotate/unbind/configure（C05，T046） |
| 投递总账 | GET /v2/deliveries :556；GET /v2/deliveries/ledger :560 |
| 任务列表 | GET /v2/tasks :597 |
| 来源/历史 | GET /v2/sources :717；POST /v2/sources :804；POST /v2/sources/{id}/history :730；GET /v2/history/options :739；POST /v2/history/preview :767；GET/POST /v2/history/jobs :771/:775；POST /v2/history/jobs/{id}/cancel|retry :787；**新** GET /v2/agent/history/requests（owner 列表，C09，T054）；**新** POST /v2/agent/history/requests/{id}/reject（owner 拒绝，C09，T054） |
| 作业 | GET /v2/jobs :884；GET /v2/jobs/{id} :896；GET /v2/jobs/{id}/candidates :902；POST /v2/jobs/{id}/retry :906 |
| 治理全量 | POST /v2/operations 的 forget/backup/restore_preview/restore_apply 及 extract/embed/share_statements/他人 task_access（:912-979；kind 实现范围见 F17）；GET /v2/operations :980；GET /v2/operations/{id} :984 |
| 配置/模型/平台 | GET/PATCH /v2/config :498/:502；POST /v2/models/ollama/discover :508；POST /v2/models/{id}/test :515；GET /v2/health :463；POST /v2/diagnostics :473；POST /v2/service/stop :490；GET /v2/overview :449；GET /v2/graph :456 |
| 集成/数据 | GET /v2/integrations :669；POST /v2/integrations/feishu/start :681；POST /v2/integrations/{host}/install :707；GET /v2/data/records :793；POST /v2/data/export :797 |
| 会话 | POST /v2/web/session :477（**仅 Identity::Owner**，Browser 亦拒，:478-480） |

检查点：上表每行至少一条 bound 身份 403 负对照，经 `tests/v2/authority_harness.rs`（T046 交付）逐路由执行；403 响应体不含调用者输入（C03 脱敏）。

### 3.3 错误码 → MCP isError 映射（C03）

冻结目标形态（T050 交付）：工具调用失败一律 `isError=true`，`structuredContent` 携带错误信封 `{status, code, message, request_id, retryable}`（429 另带 `retry_after_seconds`），并保留人读 text block。现状 host.rs:124-129 仅把 error.value() 序列化为 text block，T050 升级为结构化；`reason` 即信封 `code`。

| HTTP / code（实码） | isError | reason(=code) | retriable | 调用方动作 |
|---|---|---|---|---|
| 2xx | false | — | — | structuredContent=响应 JSON（host.rs:125 既有） |
| 401 authentication_required（server.rs:413-417；context.rs:241-245,:254-260） | true | authentication_required | false | 重新绑定/轮换后取新凭据 |
| 403 action_denied（error.rs:43-45） | true | action_denied | false | 动作层拒绝；不泄露目标存在性 |
| 404 not_available（error.rs:39-41；未知路由 server.rs:1009） | true | not_available | false | 无正文语义：不存在/已撤回/无权读共用 |
| 406 unsupported_contract_version（server.rs:377-385） | true | unsupported_contract_version | false | 先读能力目录选版本；本次未提交 |
| 409 key_conflict / cursor_mismatch / revision_changed（error.rs:27-37 前缀映射，T049-core 保留至 callers 阶段） | true | 对应子码 | false | 刷新快照/预览重决；同键只重试相同意图 |
| 409 model_not_ready（server.rs:960；retrieval.rs:628-633） | true | model_not_ready | false | 先绑定并测试模型 |
| 409 fresh_session_required（context.rs:690-695,:711-715） | true | fresh_session_required | false | 干净上下文重建后再投递 |
| 413 input_too_large（server.rs:191-203，4 MiB 体上限；**含 C10 单页超配额情形**，T053） | true | input_too_large | false | 分页/缩减后**新键**重试（消息指明超限计量维度与上限） |
| 422 invalid_input（error.rs:23-25） | true | invalid_input | false | 按指明字段修正 |
| 429 rate_limited（**新增**，T053；error.rs retryable 需扩展覆盖 429） | true | rate_limited | **true** | 按 Retry-After/retry_after_seconds 退避，原键重试。**仅适用于单页可容纳于空窗预算、当前窗口剩余不足的情形**（§9.1）；HTTP 头 Retry-After 与 structuredContent.retry_after_seconds 必须同值（交付链见 §9.3.4） |
| 500 internal_error（error.rs:47-49） | true | internal_error | false | 通用消息＋request_id；原始串只进服务端有界日志（T049-core） |
| 503 temporarily_unavailable（error.rs:51-53） | true | temporarily_unavailable | **true** | 保留原幂等键重试（无 Retry-After） |
| JSON-RPC -32601（未知工具，host.rs:115） | 协议错误 | method_not_found | — | 工具名不在表内 |
| JSON-RPC -32603（能力目录不可用，host.rs:99） | 协议错误 | internal_error | — | 服务不可达 |

冻结约束：

1. **错误体脱敏**（T049-core）：io/rusqlite→internal、serde_json→invalid 的三个 From（error.rs:65-81）一律改通用消息＋request_id；ingestion.rs:493/:510/:525/:529-532 四处删除 {key}/{actor}/{source_id}/{cursor} 反射后缀，**消息前缀冻结**为 `'key_conflict:'`/`'cursor_mismatch:'`/`'action_denied: capture is not enabled'`/`'action_denied: adapter'`——唯一消费者 main.rs:1156 starts_with（本人 rg 复核）与 app.js 按 code 显示不破。检查点：`route_authority.rs::reflection_suffix_removed_prefix_kept`、`io_error_body_has_no_absolute_path`、`unique_conflict_body_has_no_table_or_constraint_name`。
2. **未知路由判权**（authority.md:87 承诺，T049-core 实现）：路由无法验证时只给该身份所有可能目的地的共同可读字段；目的地集合未知时只供应明确允许任意目的地的数据，其余记 route_restricted。检查点：`route_authority.rs::unknown_route_common_visible_set`。
3. `request_id` 每次调用新生成（error.rs:59），只作诊断关联，不作幂等键。
4. 409 子码 11 种枚举（ConflictKind）由 T049-core 定义并覆盖全仓 64 处 conflict() 调用点（plan.md §一.4 计数），callers 阶段（R2）迁移归零；本表子码名以现有可观察 code 为最小集，新增子码不得改变本表 retriable 语义。

## 4. 三档身份与默认授权（C04）

### 4.1 角色与默认能力集

身份判定链不变：Bearer→owner_token 常数时间比较→context::bind（server.rs:386-395）；Browser→会话 cookie＋写请求同源核对（server.rs:397-412）。T046 把 owner() 的 `Owner|Browser=>Ok`（server.rs:352-359）改穷尽 match 并显式化 Browser 差异。

| 角色 | 认证来源 | R1 默认能力位 | R2 追加 | actor_kind 白名单（C06） | 写面 |
|---|---|---|---|---|---|
| owner | owner_token（CLI/本机受控入口） | 全量动作 | 全量 | user_message/assistant_message/tool_result | statements/events/治理全量 |
| browser | 一次性登录码会话 cookie | owner-console 全量，**除**裸 POST /v2/clients（T046 显式 403）与 POST /v2/web/session（今天已仅 Owner，server.rs:478-480）；integrations install 豁免（plan §八.5c） | 同 owner | 同 owner（代理 owner 会话） | 同 owner |
| host_adapter | Bearer→bind，role=host_adapter | load_context, observe_loaded, observe_used, verify_outcomes, manage_tasks（tasks.md T045 冻结）＋ search, evidence_expand, statement_list, task_handoff, delivery_ledger_self | 无默认追加（治理位须 owner 显式授予） | user_message/assistant_message/tool_result（信任边界见 C06 表） | POST /v2/events 来源页（ing_actors 绑定）；**不**默认 candidate_submit |
| agent_writer | Bearer→bind，role=agent_writer | search, statement_list, task_handoff, delivery_ledger_self, candidate_submit, manage_tasks（manage_tasks 为 tasks.md T045 冻结项） | operation_preview, operation_confirm（T054 合流激活，服务端白名单约束） | assistant_message/tool_result | **仅 candidate.submit**（候选层，C07；无 /v2/events 通路，§3.1 补充 4） |
| agent_reader | Bearer→bind，role=agent_reader | **仅 search**（tasks.md T045 冻结） | 无 | ∅（无写面） | 无 |

冻结补充：

1. **能力位≠内容授权**：search.query 的 `available`＝search 位 ∧ shared_enabled（保持 server.rs:1026-1031 现状语义）；shared 未授予时 bound search 403（server.rs:646-650）。**statement.list（agent.statement.list）的 `available`＝statement_list 位 ∧ shared_enabled，与 search 同门**；shared 未授予时 bound statement.list 403（T051a 实现与 server.rs:646-650 同构的门）。shared 投影只裁剪字段，不替代客户端级 share 授权。evidence.expand 内容仍经 recall_grants×task_access；task.handoff 仍经 task_access。注册时 shared 默认 false（context.rs:232），须 owner 经 grant.change share_statements 授予（retrieval.rs:663-664，owner-only）。检查点：`mcp_surface.rs::bound_statement_list_without_share_403`（变异＝移除 shared_enabled 判定后必须红）。
2. **迁移回填**（T046）：存量 host∈{claude-code,codex,dsh}→host_adapter（tasks.md T046）；**其他存量 host→agent_reader**（本 spec 冻结最小权限；影响面见 §15.6）；新注册缺省 role=agent_reader。role 决定默认能力集；owner 注册时仍可显式覆盖 capabilities（context.rs:195-202 既有行为保留），但 actor_kind 白名单按 role 执行、不受能力覆盖影响。迁移必须是带版本号的显式迁移（§5.1 末段）。
3. **依赖规则**：observe_used→observe_loaded→load_context（context.rs:203-208 既有，保留）；新增 operation_confirm→operation_preview（T047 注册校验）。违反依赖的注册 422。
4. Browser 差异检查点：`clients.rs`（T046）新增 `browser_bare_client_register_403` 与 `browser_install_still_registers`（install 路径回归，T057 browser_integrations.cjs 四步含 install）。

### 4.2 能力位词表（冻结，14 位穷尽清单）

| 位 | 来源 | 语义 |
|---|---|---|
| load_context | 既有（context.rs:20） | context.load / attempt.start |
| observe_loaded | 既有（:21） | delivery.receipt kind=loaded |
| observe_used | 既有（:22） | delivery.receipt kind=used |
| verify_outcomes | 既有（:23） | attempt.outcome |
| manage_tasks | 既有（:24） | task.create（POST /v2/tasks） |
| search | 新增（T046/T047） | search.query（内容仍受 shared 门） |
| evidence_expand | 新增 | evidence.expand（内容仍受 recall_grants×task_access） |
| statement_list | 新增 | statement.list 工具 → agent.statement.list（shared 投影＋shared 门） |
| task_handoff | 新增 | task.handoff（内容仍受 task_access） |
| delivery_ledger_self | 新增 | delivery.ledger.self（仅本客户端） |
| candidate_submit | 新增（T053） | candidate.submit |
| operation_preview | 新增（T054） | operation.preview（agent 限白名单） |
| operation_confirm | 新增（T054） | operation.confirm（依赖 operation_preview） |
| history_import_request | 新增（T054） | history.import.request / history.import.request.read（owner 显式授予） |

命名规则冻结：新增位名＝command_id 去点换下划线（agent.statement.list 的位沿用 statement_list，不新造位），上表为穷尽清单；T047 注册表不得引入表外位（owner 动作的 command_id 如 config.patch 不在本 agent 位表内，注册表另按目录登记）。检查点：`tests/v2/test_capability_reconcile.py`（T047 交付，discover 收集，run_gates 步 5/6 内 exit 0，写 artifacts/capability_reconcile.json）比对注册表 agent 位集==冻结位集；冻结位集以 T047 交付的机器可读附件 `tests/v2/data/capability_bits.json`（内容==本表 14 位，脚本生成禁手改）为比对基准。**脚本类检查点必须包装为 discover 收集的 Python 用例**（§14.1.3），不得以裸脚本 exit 0 充当门禁断言。

## 5. 授予、撤销与「立即生效＝下一请求」（C05）

### 5.1 授予/撤销路径（全 owner-only）

| 动作 | 路由/命令 | 归属 | 语义 |
|---|---|---|---|
| 注册 | POST /v2/clients（server.rs:545-551）＋role 字段 | T046 | 按 role 给默认能力位；token 仅首次注册返回（context.rs:235），重放不含 token（:213-215 存储于 :233 之前） |
| 改能力/role | **新** PATCH /v2/clients/{id}（client.configure，目录承诺未实装 F12） | T046 | 显式能力集替换；依赖规则校验 |
| 轮换 | **新** POST /v2/clients/{id}/rotate；CLI `bmem client rotate` | T046 | **分段发布协议（冻结）**：① 先在凭据目录持久化权限受控（0600）的下一版本暂存凭据；② 提交 SQLite（重写 token_hash＋credential_version+1）；③ 发布正式 run/clients/<id>.json（atomic）。②前失败：旧凭据继续有效，清理未提交暂存；②后③中断：启动恢复或同键重试按库内版本发布同一暂存凭据，旧凭据持续拒绝；正式文件与库内版本相符才返回完成，回执不含密钥。旧 token 下一请求 **401**（bind 按 token_hash 查找 miss，context.rs:249-260；轮换经哈希重写生效，无需版本比对入参） |
| 解绑 | **新** POST /v2/clients/{id}/unbind；CLI `bmem client unbind` | T046 | revoked_at=now；旧 token 下一请求 **401 authentication_required**（bind 查询 miss）；候选/回执零增量 |
| 安装升级 | POST /v2/integrations/{host}/install（integrations.rs:191-275） | T057-lite | 复用同一能力策略并**升级**已有绑定（修 :211-216 原样复用）；Browser 豁免 |
| 内容授权 | grant.change（share_statements/task_access/recall_grants） | owner 既有 | 见 §8；撤销即时裁剪读取 |

**context_clients 迁移（T046，本期第一个正式 schema 变更，必须同包交付版本门）**：增列 role/credential_version/revoked_at；`PRAGMA user_version` 由 0 升 1；回填 role 按 host（host∈{claude-code,codex,dsh}→host_adapter，其余→agent_reader，§4.1 补充 2）、credential_version 默认 0、revoked_at 默认 NULL；迁移幂等可重跑。**版本守卫先于任何 migrate/监听**：二进制遇 user_version 高于自身支持值必须拒绝启动并提示升级（现状 database() 无版本检查，F20；旧制品不因此获得 retroactive 保护——早于守卫的制品只允许停服＋恢复对应迁移前备份后启动）。bind() 查询（context.rs:249）增 `revoked_at IS NULL`；bind 返回 credential_version 供 T056 会话模式逐请求重校验（§5.2.4），**credential_version 不作 bind(token) 入参**（tasks.md T046「credential_version 匹配」措辞由 ASM 按此修订）。检查点：`clients.rs::old_binary_rejects_new_user_version`、`migrate_backfills_role_from_host`、`migrate_is_idempotent_rerun`、`rotate_stage_failure_old_token_valid`、`rotate_publish_failure_retry_republishes`、`rotate_final_file_matches_db_version`（T046 新冻结用例名）。

### 5.2 「立即生效＝下一请求」定义（冻结）

1. 撤销/轮换以 SQLite 事务提交为生效点。
2. **每个 HTTP 请求**重新 authenticate→bind（server.rs:441→:395→context.rs:239），不存在跨请求身份缓存。
3. **长驻 MCP stdio 会话**：initialize 成功不缓存授权；每次 tools/call 桥重查 capabilities（host.rs:118-121）且每次转发都是携带 Bearer 的新 HTTP 请求（host.rs:122→client.rs:42-61），故撤销/轮换后**下一次 tools/call 或 tools/list** 即 401（isError 信封）。stdio 进程无需重启。
4. （T056 若开工）HTTP MCP 会话同此：每请求从 extensions 取身份重 bind，session 模式记录 (client_id,credential_version) 每请求重校验，失配终止会话（tasks.md T056 入口规格）。
5. 已在途请求（bind 先于提交完成）按旧授权走完本次快照；「下一请求」指 bind 执行晚于提交的第一个请求。

**期望码冻结**：撤销/轮换后旧 token 请求一律 **401 authentication_required**（bind SQL 过滤查无行，context.rs:254-260 既有形态）。冻结用例名 `revoked_client_next_request_403`/`stdio_revoke_after_initialize_403` 沿用 tasks.md 原名，**断言取 401**，不得按名字断言 403（本轮合并裁决：语义以实码为准，用例名以 tasks.md 冻结为准）。

检查点（红绿成对）：`clients.rs::rotate_rejects_old_token`、`unbind_blocks_candidate_and_receipt`、`revoked_client_next_request_403`（断言 401；变异＝移除 bind() revoked_at 判定，context.rs:249）；`mcp_surface.rs::stdio_revoke_after_initialize_403`（断言 401）、`stdio_rotate_old_token_401`（变异＝逐请求 bind 改 initialize 缓存）；`browser_integrations.cjs` install→capability_readback→unbind→old_token_403 四步（末步断言 401）。

## 6. 角色→actor_kind 白名单（C06）

**服务端按已认证身份执行，正文不得指定。** 现状 F1 不存在已认证 actor_kind 可覆盖——本条是 T053 新增交付，不是既有防线（plan.md:19 已冻结该定性）。

| 身份 | 允许的 actor_kind | user_message 资格 |
|---|---|---|
| owner（CLI/Web） | user_message / assistant_message / tool_result | 有（本人入口） |
| host_adapter | user_message / assistant_message / tool_result | 有——限 host_adapter 角色（Bearer 绑定）且 ing_actors accept 绑定。原生 hook（host.rs:199-241，UserPromptSubmit→user_message）是产品化铸造路径；**HTTP 边界无法区分 hook 出处与同凭据直接提交**（mcp serve 与 hook 共用 run/clients/<id>.json，F20）：host_adapter 凭据持有即信任边界（仅本机、凭据文件 0600、可轮换可解绑 C05）。正文 actor_kind 按角色白名单校验（规则 2）。是否升级为独立采集凭据/登记属未决硬化，见 §15.12 |
| agent_writer | assistant_message / tool_result | **无** |
| agent_reader | ∅ | 无 |

冻结执行规则（T053）：

1. **candidate.submit 输入形状（穷尽冻结）**：信封＝`{source_id, events:[{text|checks, at?, task_id?, category?}], idempotency_key?}`，`additionalProperties:false`。信封级与事件级均**不含 actor_kind 字段**；信封或事件对象出现键名 `actor_kind`（**任何值**，含嵌套伪装；该判定先于未知字段判定执行）→ 403，且 accept_page 写集各表（§7.1）新增行数均 0。其他未知信封字段 → 422 invalid_input 零增量。服务端在调用 accept_page 前逐事件注入：actor_kind 按形状推导——携带 checks→tool_result，携带 text→assistant_message，两者兼有/兼无→422（与 plan_event 形状校验同构，ingestion.rs:1189-1222）；`cursor`/`next_cursor`/`native_object`/`native_version`/`change_kind` 由服务端派生（调用方传入任一 → 422）：cursor＝当前 ing_sources.cursor（server.rs:850-857 同义），next_cursor＝UUIDv4，native_object＝sha256("{client_id}\u001f{source_id}\u001f{actor_kind}\u001f{canonical content}")（确定性，保证同内容重放去重），native_version="1"，change_kind="created"。每次 tools/call 恰好生成一页（1..100 事件）。host_adapter 经 POST /v2/events 的正文 actor_kind 按角色白名单校验（规则 2），原生 hook 按 hook 类型铸造（host.rs:199-241）。**变异点＝T053 新增的 agent_intake 角色/形状推导函数**（F1）。
2. **POST /v2/events 仅向 owner 与 host_adapter 开放**（§3.1 补充 4）；host_adapter 正文 actor_kind ∉ role 白名单 → 403＋零增量（整页回滚，沿用 accept_page 单事务语义）。其他 bound 身份（含 agent_writer）→ 403 action_denied。
3. 来源 categories 逐事件执行（T053 新增检查，现状事件无 category 字段、categories 仅来源级，F19）：intake 来源注册 categories=["agent_candidate"]；事件 category 缺省 "agent_candidate"，显式值 ∉ source.categories → 403 整页回滚。
4. assistant_message 正文写「用户说过 X」**不产生** role=user_message 的观察（正文角色词防线 ingestion.rs:77-78 语义保留，结构化白名单是其上第二层）。
5. 凭据双层脱敏保留：SECRET_KEYS 结构化移除（ingestion.rs:62-75）＋文本 redact（authority.rs:54-104）。
6. **change_kind 限制（T053 新增交付）**：host_adapter 之外的 bound 身份（含 agent_writer）经任何写入路径提交的事件 change_kind 只允许 {created,updated}（candidate.submit 由规则 1 服务端恒铸 created）；出现 change_kind=revoked → 403 action_denied，整页事务回滚，ing_event_versions/ing_jobs/ing_ingests/ing_roots/recall_events/recall_fts/authority_observations/ing_candidates/ing_root_revocations 新增与修改行数均 0。撤回语义仅保留给受信适配器（host_adapter 原生 hook，上游已认证撤回）与 owner 治理（forget）；agent 的撤回诉求走 owner 裁决。依据：现状 revoked 页对任何 bound actor 开放并触发 withdraw_root 永久墓碑＋erase_event（F18），属 forget 级治理效果，绕过 §8.1 owner-only 余集。

检查点：`agent_intake.rs::agent_writer_user_message_403_zero_rows`（变异＝移除 agent_intake 角色推导/白名单判定）、`prose_user_said_x_no_user_observation`、`cross_category_403_page_rollback`、`body_and_envelope_double_impersonation_rejected`（信封级＋事件级同时携带 actor_kind，按规则 1 判定顺序可构造）、`unbound_client_candidate_submit_rejected`、`body_actor_kind_any_value_403`、`agent_writer_shape_derivation_checks_vs_text`、`envelope_unknown_field_422_zero_rows`、`agent_writer_revoked_page_403_zero_rows`、`agent_writer_events_route_403`（后五条为本文新冻结用例名；「零增量」断言集统一为 §7.1 写集各表，不得只点名 intake 路径不会写的表）。

## 7. 候选写入语义（C07）

0. **目标来源限制（T053）**：candidate.submit 的目标来源必须是 `kind='agent-intake'` 且调用者 accept 绑定的 intake 来源（ing_sources.kind 列既有，F19）。T053 交付每客户端 intake 来源的自动注册：首次 candidate.submit 时服务端注册 source_id 确定性的 intake 来源（namespace=`agent-intake:{client_id}`，categories=["agent_candidate"]，extract inactive，调用者 accept 绑定）。非 intake 来源或未绑定 → 403 零增量。host/owner 来源的写入仍走 host_adapter/owner 的 /v2/events。
1. **只进候选/观察层（同步写集如实冻结）**：candidate.submit 复用 accept_page 管线，同步写集＝`ing_event_versions`＋`ing_jobs`(pending)＋`ing_ingests` 回执＋`ing_roots`(新根，plan_event :1254-1260)＋`ing_sources.cursor` 推进(:609-612)＋`recall_events`/`recall_fts` 证据索引(:625→retrieval.rs:101-110)；**不同步写** authority_statements/authority_revisions/authority_observations。recall 索引是观察层投影：不产生当前有效声明，但会立即被持 recall_grants×task_access 的客户端经 expand/handoff 读到，故必须同时执行 1a 的 task_id 门。agent 写入不产生当前有效声明；agent 不得携带 change_kind=revoked（C06.6）。
   1a. **task_id 门（T053）**：candidate.submit 事件 task_id 为空或 ∈ 调用者已持 task_access 的任务（把 server.rs:835-843 的既有 dispatch 层判定下沉到 agent_intake 入口；该检查不在 accept_page 内，复用不会自动继承）；越界 → 403 整页回滚。检查点：`agent_intake.rs::candidate_foreign_task_id_403_page_rollback`（变异＝移除 task_id 判定）。
2. **生效路径仅两条**：(a) 后台提炼仲裁——worker claim→真实模型 extract→finish 权威提交（server.rs:1250-1281；authority.md:26 提交边界 3），前提是该来源已获 extract 授权；(b) 管理台裁决——owner 经 GET /v2/jobs/{id}/candidates（server.rs:902-905）审阅后以 statement.append 引用本人消息追加。
3. **默认不生效**（plan §八.5b 锁定）：agent intake 来源注册时 extract inactive（ingestion.rs:272-275，F19）——**默认无模型目的地**，候选停留 pending（ing_jobs.status=pending＋回执 jobs_created）。提炼生效须 owner 逐来源 grant.change purpose=extract（owner-only，§8.1 余集）；目的地＝当前全局 extract 绑定（server.rs:940-943 model_route），preview 必须返回 model_destination（server.rs:970-977 既有，含 local_endpoint 位）。**「仅 loopback」硬校验为 T053/T054 新增交付（实码今天不存在独立 loopback 限制，不得默示存在）**：对 intake 来源的 extract 授权 confirm 时，若 model_destination.local_endpoint=false，confirm 正文必须携带显式 `remote_destination_ack=true`（owner 在 preview 中可见实际端点后逐来源确认），否则 409 revision_changed 拒绝；该显式确认即 plan §八.5b 要求的「远端目的地逐来源显式授权」。检查点：`agent_intake.rs::intake_extract_inactive_by_default`、`mcp_surface.rs::intake_extract_remote_requires_ack`（变异＝移除 local_endpoint 判定后必须红；本文新冻结用例名）。
4. **不复用** owner-only POST /v2/statements（server.rs:521-527）；不把裸 /v2/events 当 agent 写面（§3.1 补充 4）。
5. 候选层读取投影冻结：bound statement.list 走 shared_only=true，字段集＝{revision_id, fact_id, key, value, scope, status, valid_from, known_from}（authority.rs:355），**不含** statement_id/evidence(原文)/share（authority.rs:356 仅 !shared_only 附加）；特定句柄原文仅经 recall_grants×task_access（retrieval.rs:249-252）。注意 fact_id 在投影内（与 tasks.md T051a「不含 statement_id/evidence/原文/share」一致，fact_id 非敏感原文）。
6. **agent 回执投影**：candidate.submit 回执＝{ingest_id, source_id, status:"accepted", replayed, cursor, jobs_created, events:[{ordinal,native_object,event_id,disposition}], committed_at}——ingestion.rs:614-624 既有回执**删除 actor 字段**（不回显身份；存储回执不变，返回前裁剪，与 §9.1 重放裁剪一致）。

检查点：`mcp_surface.rs::bound_statement_list_shared_projection_no_evidence`（变异＝投影切 shared_only=false 后「无 evidence/原文字段」断言必须红）；`agent_intake.rs` 候选提交后 authority_revisions/authority_observations 零增量断言（提交即时候选态）；生效路径 (a) 的证据＝P0 第二步真实提炼接手样本（R2，T063-full；**T052 native_receipts.json 证明的是装入/采用/独立验证（第一步），不得冒充提炼仲裁链路证据**）。

## 8. 治理动作白名单（C08）与 history.import 请求单（C09）

### 8.1 agent 可发起（R2，T054；全部经 preview→confirm 两工具＋preview_id 句柄）

| 动作 | agent 允许条件（全部满足，服务端执行） | 越权后果 |
|---|---|---|
| grant.change（source 形式） | purpose=task_delivery ∧ source_id∈调用者 ing_actors accept 绑定来源 ∧ client_id=调用者自身 ∧ **task_id∈调用者创建任务**（tasks.md T054 默认放开清单已含该前置，本文补齐；context_tasks.creator=调用者，context.rs:385-386） | 403＋governance_operations/governance_previews/recall_grants 新增行数 0 |
| grant.change（task 形式，无 source_id） | task_id∈调用者创建任务 ∧ client_id=调用者自身 | 403＋context_task_access 零增量 |
| stop_capture / resume_capture | source_id∈调用者 accept 绑定来源 **且该来源为本客户端的 agent intake 来源**（kind='agent-intake'，C07.0）；owner/host 来源的 stop/resume 一律 owner-only。**resume 追加条件**：该来源最近一次 stop_capture 治理单据的 client_id=调用者（T048 迁移后 governance_operations 含主体列；owner 已停止的 intake 来源 agent 不得自行 resume，恢复走 owner 治理或请求单＋owner 确认）。agent 路径仍须 preview→confirm | 403＋两表零增量 |
| history.import | **不直接发起**；仅请求单形态（C09） | 直接 POST /v2/history/jobs 403＋history_task_jobs 零增量 |

owner-only 余集（agent 一律 403）：purpose∈{extract,embed}、share_statements、他人 client 的 task_access/recall_grants、forget、backup/export/restore_preview/restore_apply、retry/rebuild_index（后两者今天未实装，F17）、全部客户端管理。依据：F3（grant.change 能力极大）、F5（history 全 owner-only）、plan §八.5a。残留风险登记：recall_grants 按 (source_id,client_id) 存储、与客户端任意 task_access 组合（retrieval.rs:249-252）——授权一旦成立即跨该客户端全部任务可读；本轮以「自身绑定来源＋自身创建任务＋client_id=自身」三重前置收窄，逐任务读取许可存储属未来硬化（§15.13）。

### 8.2 无状态形式（冻结，protocol-choice §5.2）

1. 显式两个工具 operation.preview / operation.confirm；跨调用状态只以服务端自铸 preview_id 作普通 tool 参数（retrieval.rs:592-597 自铸 UUID）。**禁止**改成协议会话态。
2. **预览主体绑定与足迹比对（分路径冻结）**：
   - owner 路径现状不变：confirm 成功即 DELETE preview（retrieval.rs:698-700）；change_grant/forget 的 confirm 必须携带 preview_id 并逐字节比对足迹（retrieval.rs:599-615），目标状态变化 409 revision_changed 重新预览；无关活动不使预览失效（:310-318 注释语义保持）；stop_capture/resume_capture 的 confirm 现码不做足迹比对（:336-337 注释，preview 仅为信息性快照）。
   - **agent 路径（T054 新增交付）**：governance_previews 增 client_id 列（T048 迁移同窗交付，ASM 登记 tasks.md T048 范围）；agent preview 记录发起主体；agent confirm 在业务事务内重验当前权限＋preview.client_id=调用者＋**四 kind 均**逐字节比对足迹（stop/resume 的状态绑定为 T054 新增判定）。缺 preview_id 422；他人 preview 403 且不消耗句柄；足迹变化 409 revision_changed；仅首次成功确认消耗预览；获准重放返回原单据。
3. confirm 重放走原单据（request_key 回放 retrieval.rs:571-588）；键=grant-confirm-{preview_id}（C12）。
4. R1 期间 agent 无治理位，注册表登记 available=false＋reason='awaiting T054'；owner 治理路径行为不变。

检查点：`mcp_surface.rs` 治理段 `agent_grant_change_owner_source_403`（recall_grants 行数不变）、`agent_grant_change_other_client_403`、`agent_stop_capture_owner_source_403`、`agent_history_import_403_zero_jobs`、正例 `agent_task_create_then_grant_change_succeeds`；每组 403 断言 governance_operations 与 governance_previews 新增 0；变异＝放开白名单任一限定后对应负对照必须红。新增：`agent_resume_owner_stopped_source_403`、`agent_stop_host_source_403`、`agent_confirm_other_subject_preview_403`、`agent_stop_resume_footprint_changed_409`（本文新冻结用例名）。

### 8.3 history.import 请求单（C09，plan §八.5a 默认锁定）

形态冻结（T054；存储/读回/确认机制本文新冻结，tasks.md T054 文件清单经 ASM 扩充：server.rs 装配补丁＋history_tasks.rs）：

1. **提交**：agent 经 `history.import.request`（POST /v2/agent/history/requests）提交请求单→202 {request_id, state:"pending_owner"}。请求单存新表 `history_import_requests(request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending_owner','confirmed','rejected')), idem_key TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT, UNIQUE(client_id, idem_key))`（T054 迁移）。状态机仅允许 pending_owner→confirmed|rejected 一次转移。**请求单不创建 history_task_jobs、不触发摄取**。
2. **owner 读取与裁决**：owner 经 GET /v2/agent/history/requests（owner-only 列表，§3.2）或 CLI `bmem history requests` 查看；经既有 POST /v2/history/preview（server.rs:767）预览——T054 扩展 preview 接受 request_id，预览足迹绑定该请求单正文与当前授权；经 POST /v2/history/jobs（server.rs:775）携带 body.request_id＋preview_id 确认：同一事务核验绑定（preview 与请求单不匹配或授权已变更 → 409，history_task_jobs 零新增）、创建作业（history_task_jobs 增 request_id 列，T054 同一迁移）、置请求单 state=confirmed＋decided_at。owner 拒绝经 POST /v2/agent/history/requests/{id}/reject（owner-only）→ state=rejected。owner 直接发起的既有历史入口（无 request_id）保留原格式。全量导入仍以 actor=owner 摄取（F5，history_tasks.rs:438），与「历史导入由用户主动勾选发起」偏好一致。
3. **agent 读回**：GET /v2/agent/history/requests/{request_id}（能力位 history_import_request，仅调用者创建的请求单，他人 404）返回 {request_id, state, job_id?}；202 回执为初始状态；owner 确认前 state 恒 pending_owner，确认后返回 job_id（作业详情仍 owner-only，F5 不变）。同键重投返回原请求单（UNIQUE(client_id,idem_key)）。

检查点：`agent_history_import_403_zero_jobs`（直接路径）＋`agent_history_request_pending_owner_zero_jobs`（request_id 存在而 history_task_jobs 零增量）＋`owner_confirm_creates_job_binding_request_id`（owner 确认后 state=confirmed 且作业出现，红绿成对）＋`agent_history_request_self_read_only`＋`request_scope_mismatch_409`（本文新冻结用例名，T054）。

## 9. 速率与滥用（C10）

### 9.1 limits 表结构与语义（冻结，T053 实现）

表 `limits`：`(client_id TEXT PRIMARY KEY, window_start TEXT NOT NULL, events INTEGER NOT NULL, bytes INTEGER NOT NULL, updated_at TEXT NOT NULL)`（plan §八.1 结构）。window_start/updated_at 为 UTC ISO 8601（与 context_deliveries.created_at 的 strftime 形态一致，context.rs:879）。

算法（每 candidate.submit 请求，顺序冻结）：

1. bind 认证 → 角色白名单（C06）→ **当前授权复核**（来源 accept 绑定＋capture 许可＋task_id 门；现状回放先于授权执行，F18，T053 新入口必须把授权复核置于回放查询之前）→ 幂等重放查询（ingestion.rs:482-497 形态）。**重放命中：按当前授权裁剪后返回原回执（agent 投影删 actor，§7.6），不计费、不占配额**（即使窗口已满）；绑定/capture 已撤销 → 403，无回执、游标或配额变更；同键异文 → 409 key_conflict。
2. **单页超量判定（先于窗口检查）**：本页事件数 > events_per_minute 或本页字节数 > bytes_per_minute → **413 input_too_large**（retryable=false，消息指明超限的公开计量维度与上限，调用方按配额拆页后以新键重提）。依据：摄取层允许每页 1..100 事件（ingestion.rs:449-453）、HTTP 体上限 4MiB（server.rs:191-203），61–100 事件页或 >1MiB 页在空窗也超预算，若判 429＋retryable=true 会指示调用方无限重试（活锁）。
3. 速率检查（快速路径）：读 limits 行；不存在或 `now - window_start >= 60s` → 开新窗（window_start=now, events=0, bytes=0）。若 `events + 本页事件数 > events_per_minute` 或 `bytes + 本页字节数 > bytes_per_minute` → **429 rate_limited**，`Retry-After: max(1, ceil(window_start + 60s - now))`（整数秒）。429 仅适用于单页可容纳于空窗预算、当前窗口剩余不足的情形。
4. 通过 → **与页接受同一 SQLite 事务内**先重读 limits 行并复核配额（得锁后重检），再原子自增 events/bytes 并写 updated_at；失败回滚即不计数。**配额判定与自增必须同事务**（读快照走独立只读连接、写事务走全局 Mutex，server.rs:64-73/:95；事务外读＋事务内增会让 N 个并发提交同读剩余配额全部通过，超出至多 N 页）；第 3 步读取仅作快速路径，其结果不作最终判定。

计量口径冻结：events＝页内事件条数（1..100）；bytes＝**原始请求体 UTF-8 字节数（脱敏前）**。窗口＝60 秒锚定窗（window_start 锚定、到期重置；plan「滑动窗」在该聚合结构下的可实现语义即此，冻结为唯一算法）。时钟来源＝可注入 Clock（T053），生产实现 UTC 墙钟，测试注入固定时钟。

### 9.2 默认值与可调性

| 参数 | 默认 | settings 键（经既有 PATCH /v2/config，owner-only） |
|---|---|---|
| events_per_minute | 60 | limits.events_per_minute（整数 ≥1） |
| bytes_per_minute | 1048576（1 MiB） | limits.bytes_per_minute（整数 ≥1024） |

不做新 Web 控件（plan §八.1）；T053 必须交付设置字段注册＋持久化＋重启回读用例。

### 9.3 冻结语义边界

1. **计费时机**：仅成功提交的页计费；403/422/409/413/429 拒绝页不占配额（无预扣，故无在途释放问题——「在途释放时机」冻结为「提交时计费、拒绝即不占」）。
2. **「并发 4」删除**（plan §八.1 允许的重定义二选一，本 spec 选删除）：R1 不设每主体在途请求数上限；并发超调由 §9.1.4 同事务判定消除。
3. **作用域**：R1 仅 candidate.submit（含桥传输路由 /v2/agent/candidates，共用同一 limits）；owner 入口与 host_adapter 的 POST /v2/events 适配路径 R1 不限流（受信路径，行为不变）；agent_writer 无 /v2/events 通路（C06.2），限流覆盖 agent 全部写入入口。
4. **MCP 映射与交付链（T053 同包交付；error.rs/client.rs/host.rs 的文件声明经 ASM 台账修订补入 T053）**：429→isError=true，structuredContent `{code:"rate_limited", retryable:true, retry_after_seconds:N}`（§3.3）。链路：agent_intake/server 侧发射 `Retry-After` 头（error_response 仅对 status==429 增该头，其余不改，server.rs:301-307）；client.rs 非 2xx 读取 Retry-After 并随 Error 携带（现状只取响应体 status/code/message，头被丢弃，client.rs:81-95；Error 增 retry_after_seconds 载体，error.rs:8-12）；error.rs retryable 判定扩展覆盖 429（现状仅 503，error.rs:60）；host.rs 错误信封把 retry_after_seconds 透传进 structuredContent，与 HTTP 头整数秒同值。
5. 其他主体不受影响（按 client_id 隔离）。

检查点（表驱动＋变异）：`agent_intake.rs::rate_61st_event_429_retry_after`（HTTP 429＋Retry-After＋错误体 retryable=true）、`rate_window_advance_recovers`、`byte_limit_triggers`、`other_subject_unaffected`、`oversized_page_413_not_retryable`（100 事件页与 >1MiB 页各一断言；变异＝把 413 改回 429 后 retryable 断言必须红）、`rate_limited_mcp_envelope_retry_after`（经桥内进程调用触发 429，断言 isError=true 且 structuredContent.code==rate_limited、retryable==true、retry_after_seconds==Retry-After 整数秒）、`concurrent_submissions_window_not_exceeded`（注入 Clock＋并发提交，断言窗口内计数≤配额）、`revoked_source_replay_403_no_receipt`（保持凭据有效、仅撤销来源权限后原键重投）；重放不计费断言（同键重放满窗仍返回原回执）；变异＝删限流判定必须红。

## 10. 凭据与 token（C11）

1. **generic token 文件交付**（plan §八.2 锁定，T057-lite）：`bmem connector token` stdout **只输出** client_id＋stdio 启动行（形如 `bmem --home <root> mcp serve --client <id>`，integrations.rs:245 形态）；凭据写 `run/clients/<id>.json`，**0600、atomic**（复用 integrations.rs:277-283 store_client＋:319-342 atomic：同目录 NamedTempFile＋persist 原子替换；NamedTempFile 在 Unix 默认 0600——代码无显式 chmod，故 0600 必须作为测试断言而非代码既成事实）。stdout/启动参数/宿主配置明文均不含 token 值。既有 register CLI 已先落盘再从 stdout 剔除 token/client_token/credential（main.rs:1057-1063），connector token 沿用该形态。
2. **轮换/解绑**：见 C05（分段发布协议）；旧 token 下一请求 401；解绑后写入 401 且零候选/回执行。
3. **交付路径不入宿主转录（best-effort 纵深防御）**：token 值不在 stdout/启动参数/宿主配置明文，交付路径本身不向转录引入 token。凭据文件仍可被同用户宿主进程读取（0600 只挡其他用户），agent 主动读文件并打印属交付保证之外；纵深防御为 SECRET_KEYS 结构化移除（ingestion.rs:62-75，只删键名为 token 的对象成员）＋文本 redact（authority.rs:54-104，命中 `token=` 等模式、**不命中字符串内嵌的 JSON 形态 `"token":"…"`**）——回灌防御是 best-effort，不构成「天然无 token」保证。导出（POST /v2/data/export，owner-only）不含凭据值。
4. **模型凭据库现状冻结**（F11）：仅 database/env 两 backend 可解析（settings.rs:252/:262）；cli-and-platform.md:68 的 Keychain/Credential Manager/Secret Service 承诺**未实装**，"system" 引用仅形状校验（settings.rs:1266-1275）无解析代码——文档修订归属见 §15.3。本轮不新增凭据库后端。
5. Windows 0600 等价（ACL）为命名缺口（§15.7），Unix 断言先行。

检查点：T057 `browser_integrations.cjs` 新阶段 `token_file_delivery`（CLI 子进程＋文件回读：stdout JSON 字段集=={client_id, launch_line}、文件 mode&0o777==0o600、stdout 全文不含 token 值、原子性＝替换前后 inode/内容回读）；`clients.rs::rotate_rejects_old_token`；导出无凭据断言并入 data_management 既有脱敏用例。

## 11. 幂等键来源与每主体作用域（C12）

### 11.1 键来源（plan §八.10 锁定，T050 贯通；按工具类分立，非全局回退链）

优先级冻结（**按工具类**）：

1. **非 confirm 写工具**（candidate.submit/task.create/attempt.start/operation.preview/history.import.request）＝调用方 `idempotency_key` 入参 ＞ 桥派生 UUID（修 host.rs:122 key=None 悬空，F8）。preview 键虽必填（server.rs:966）但不落 governance_operations、不做键回放（F13），跨主体碰撞无载体。
2. **confirm 类工具（operation.confirm）**＝键**恒为** `grant-confirm-{preview_id}`（main.rs:1038 既有形态），不接受调用方 idempotency_key——入参存在时桥忽略不转发；服务端 bound 治理分支拒绝非 grant-confirm-* 命名空间键（422 invalid_input＋governance_operations 零增量）。preview_id 服务端自铸 UUID，天然跨主体不碰撞。plan §八.10 三条为按类并列规则，非全局回退链。
3. **只读工具**不收 idempotency_key 字段（inputSchema `additionalProperties:false`，多传即 422）。每个写工具的 inputSchema 必须声明 idempotency_key 是否被采纳（confirm=false、其余写工具=true）。
4. server.rs 写路由 `key(headers)?` 形态不改（server.rs:420-424），由桥保证必有值；键长 1..160 字节（server.rs:422），桥派生键必须 ≤160（context 侧 validate_key 上限 256，context.rs:122，以入口 160 为准）。
5. 写工具经 MCP 调用**不因缺 Idempotency-Key 而 422**（桥已派生）；调用方显式键与既有记录异文 → 409 key_conflict。

### 11.2 每主体作用域（现状冻结表）

| 操作族 | 作用域键 | 实码 |
|---|---|---|
| client.register / task.create / attempt.create | (actor, operation, key) | context.rs:38-42 context_intents PK |
| context.load | (client_id, request_key) | context.rs:63 UNIQUE |
| 来源页接受 | (source_id, actor, idem_key) | ingestion.rs:482-497 ing_ingests |
| 回执 | (client_id, event_id) | context.rs:72-77 |
| 结果 | (verifier_id, event_id) | context.rs:79-83 |
| 治理 confirm | **R1 全局 request_key**（F4）；T048 迁 (主体,动作,键) 复合 UNIQUE | retrieval.rs:27,:571-588 |
| history 请求单 | (client_id, idem_key) | C09 表 UNIQUE（T054） |

冻结补充：跨主体同键互不重放（context_intents 族）；治理族 R1 局限——agent confirm 键**必须**取 grant-confirm-{preview_id} 命名空间（服务端自铸 UUID 保证唯一），禁止 agent 自带裸键进入治理路由（§11.1.2）；T048 迁移后按复合键回放。**T048 迁移存量归属（冻结）**：存量治理单据统一归入非空的本机 owner 主体（旧写入均 owner 发起，server.rs:913 identity.owner()?），按保存的 request.kind 映射冻结动作 ID，保留原键/规范请求/操作号/回执；旧预览作废并提示重新预览。版本门：T046 已交付 user_version 守卫（§5.1），T048 迁移成功 user_version 再 +1；早于守卫的旧制品只允许停服＋恢复对应迁移前备份后启动（守卫无法 retroactive 约束已发布旧制品，如实登记）。

检查点：`clients.rs::cross_subject_same_key_no_replay`；`mcp_surface.rs::write_tool_no_422_without_explicit_key`、`explicit_key_conflict_409`、`governance_confirm_bare_key_422_zero_rows`（本文新冻结用例名）；`governance_actor_scope.rs::cross_subject_same_key_independent`、`revoked_identity_replay_rejected`、`preview_one_shot_consumed`、`user_version_gate_blocks_old_binary`、`migrate_failure_restore_from_backup`（回滚演练真跑）、`legacy_rows_backfilled_owner_subject`（T048，本文新冻结用例名）。

## 12. era 姿态与兼容性（C15）

### 12.1 legacy-era 声明（冻结）

bmem 是 **legacy-era MCP server（协商协议版本 ≤2025-11-25）**：rmcp 3.11（Cargo.toml:28）在模型层已认识 2026-07-28（rmcp-3.11.0/src/model.rs:169/:176，LATEST=V_2026_07_28），但该代际已废除 initialize；rmcp 可完成 initialize 握手的最新版是 2025-11-25（model.rs:201 LATEST_WITH_INITIALIZE；spike 工件 max_supported 即此口径，其 evidence 列表漏列 V_2026_07_28/V_2024_11_05，以源码为准）。bmem stdio 面应答 initialize（host.rs:95-107），故协商 era 必为 legacy。本期**不实现**新 era（2026-07-28）的 `server/discover`、`params._meta` 强制 protocolVersion/clientCapabilities、HTTP 强制头 MCP-Protocol-Version/Mcp-Method/Mcp-Name（protocol-choice.md §四）。legacy client × modern-only server 会 Fails，本期不追新即天然兼容现网客户端（protocol-choice.md §四兼容矩阵）。

**升级判据（冻结）**：rmcp 提供新 era（2026-07-28）**server 侧逐请求 _meta/强制头处理能力**（以 rmcp 官方 changelog＋server transport 源码核实为准，**不是**仅版本常量认识——该条今天已半成立）**且**四级 Tier-1 SDK 到位后再评估 dual-era；届时按 protocol-choice.md §四逐条补齐强制头（缺失 400＋-32020/-32022）与订阅路径。「四级 Tier-1 SDK」的具体枚举在 plan/protocol-choice 均未给出（本人 grep 核实），判据暂不可机械判定——列为命名缺口 §15.14，签字时由用户枚举或接受延后。`input_required` 记为升级后替换项，不进本期关键路径；**禁止**出现依赖 sampling/roots 的设计（SEP-2577 Deprecated）。

### 12.2 两种 list_changed 路径（契约同时写明，本期实现现 era）

| era | 路径 | 语义 |
|---|---|---|
| 现 era（本期实现，T050） | `notifications/tools/list_changed` | 该连接工具面变化时通知；桥每请求重查 /v2/capabilities（host.rs:70,:118），下一次 tools/list 必然反映授权变化，通知为变更提示而非授权依据 |
| 新 era（升级后） | `subscriptions/listen`＋`notifications/subscriptions/acknowledged` | 客户端显式订阅；进程重启后必须重新订阅（protocol-choice.md §四） |

检查点：`mcp_surface.rs::tools_list_matches_capabilities_same_identity`（同一身份集合比对，diff 空集写工件）；era 声明本身以 Cargo.toml:28 钉住＋spike 工件＋model.rs 源码为据（矩阵 evidence_type 含 doc-claim 的部分按 C13 规则上限 partial）。

### 12.3 HTTP 传输鉴权声明（T056 若开工必须明写）

**非规范的本机自建鉴权**：不实现 RFC 9728 Protected Resource Metadata，**不透传任何令牌**（protocol-choice.md §四授权章节；plan §六配套约束）。该声明必须出现在 T056 交付文档并登记决策记录。

### 12.4 T056 入口规格（条件包，默认不开工；拍板输入＝rmcp spike 已 PASS）

/mcp 挂载位于复用 authenticate() 全套的 axum middleware 之后（Host 检查在 fallback 内，nest_service 会绕过——server.rs:104,:146-155）；Identity 注入 request extensions；rmcp handler 每请求取身份重 bind；禁 service_factory 持静态 Client、禁 initialize 缓存会话授权；仅 127.0.0.1；origin validation；按目标 era 分别写强制头。spike 事实：compiles=true、axum 0.8.9/tower 0.5.3 nest_service 兼容、新增依赖 sse-stream/tokio-stream/async-trait（preflight/rmcp_spike.json）；主线 Cargo.toml/Cargo.lock R1 不改。检查点（若开工）：`test_mcp_http.py::no_bearer_401`、`wrong_host_403`、`cross_origin_rejected`、`session_revoke_next_call_403`、`rotate_old_token_401`；变异＝逐请求 bind 改 initialize 缓存后撤销用例必须红。

## 13. task bootstrap 二选一（C16，plan §八.11）

**冻结默认＝选项 A**：T054 同包交付 `task.create`/`attempt.start` MCP 工具（桥接已存在的 POST /v2/tasks、POST /v2/tasks/{id}/attempts，server.rs:601/:610）。能力位映射：task.create→manage_tasks（agent_writer/host_adapter 默认有）；attempt.start→load_context（host_adapter 默认有；agent_writer 须 owner 显式授予）。grant.change 前置「task_id∈调用者创建任务」由 agent_writer 自己的 task.create 满足——stdio-only agent 的 task_delivery 写路径不自锁。

备选 B（仅用户签字改选时生效）：bootstrap 仅 owner/HTTP，grant.change 前置改写为「task_id∈调用者创建任务 ∪ 所有者预授权任务集」，预授权集由 owner 经 task_access 授予并登记。未定死则 stdio-only agent 永远满足不了前置（plan §八.11 完整性批评 missing#3）。

检查点：`mcp_surface.rs::agent_task_create_then_grant_change_succeeds`（bootstrap→grant 链路正例）。

## 14. 验收判据（C17）

### 14.1 总纪律

**未纳入门禁的断言不算完成。** 纳入方式冻结：

1. Python 用例必须被 `python3 -m unittest discover -s tests/v2` 收集（run_gates.py 步 5/6；testsRun==AST 求和、skip 集==期望集，assembly 档步 6 skipped==0）。
2. Rust 用例必须写明 `#[cfg(test)]`/`#[path]` 内联宿主模块（先例 **6** 处已核：context.rs:1385、data_management.rs:163、history_hosts.rs:856、history_feishu.rs:977、history_tasks.rs:1108、platform.rs:638），被 cargo test（步 4）收集。
3. **脚本类检查点（能力对账、矩阵构建、schema 对齐）必须包装为 discover 收集的 Python 用例**：T047 交付 `tests/v2/test_capability_reconcile.py`（调用对账脚本，断言注册表位集==冻结位集且 exit 0，写 artifacts/capability_reconcile.json）；T063 交付 `tests/v2/test_validation_matrix.py`（调用 build_validation_matrix.py，断言 exit 0 且产物过 matrix.schema 校验）；T044 后续交付 `tests/v2/gates/test_patch_schema.py`（C14 对齐 battery，§14.2）。三者计入 AST 求和，**不改 run_gates 步表**（selftest 断言步 ids==range(14)，run_gates.py:641-643）。
4. 计数用例不得掏空：`gates/test_run_gates.py::TestCountedCasesAreNotHollow` AST 检查（T044 已交付）；真实宿主证据硬门＝T052 后 `gates/judge.py p0_evidence_complete` 升为 assembly FAIL，且**必须核对本次运行出处**：receipts 的 run_id==本次 gates run、assembly_commit==本次装配树 HEAD、binary_sha256==本次被测二进制（现状 judge.py:573-599 仅比对标签与身份字符串、无出处校验，test_run_gates.py:642-652 样例无 run_id 亦通过——T052 升级必须堵住）；缺文件、字段不满足、跨运行/错误二进制/缺提炼链路引用一律红。
5. 每包工件 `tests/v2/artifacts/<pkg>.json` 含装配树 commit＋二进制 sha256＋run_id；负对照红/绿 run id 成对；合流登记 manifest.json {pkg,artifact_path,schema_version,acceptance_rows[]}（tasks.md 通用模板）。
6. 只认装配树真跑（`run_gates.py --profile assembly`），不接受工作包自报；「真实/一致」等词不得单独作为验收。

### 14.2 条款→测试映射表

「待交付」文件由归属包创建并纳入门禁；用例名冻结自 tasks.md 对应条目（本文不新造用例名，除注明「本文新冻结」者）。

| 条款 | 测试文件::用例 | 负对照与变异点（点名实码） | 归属包 |
|---|---|---|---|
| C01 本机边界 | authority_harness.rs::wrong_host_header_403、instance_header_mismatch_409、non_loopback_connect_refused（本文新冻结，T046，默认 R1 即跑）；test_integration.py:132 回环 bind 正断言既有；test_mcp_http.py::wrong_host_403/cross_origin_rejected（若 T056 开工） | 变异＝Host 检查移出 fallback（server.rs:146-155）/:148 改仅要求存在、删 :387 instance 核对、:81 改 UNSPECIFIED 后对应用例必须红 | T046/T056/既有 |
| C02 动作表 | mcp_surface.rs::tools_list_matches_capabilities_same_identity、disabled_capability_tool_absent_and_call_rejected、delivery_ledger_self_only_own_rows | 变异＝server.rs:1031 bound 分支裁剪回单条 search.query 后集合比对 diff 非空必须红，且 delivery.ledger.self/statement.list 正例必须红 | T050/T051a |
| C03 错误映射/脱敏 | route_authority.rs::unique_conflict_body_has_no_table_or_constraint_name、io_error_body_has_no_absolute_path、reflection_suffix_removed_prefix_kept、unknown_route_common_visible_set | 变异＝恢复 error.rs:65-81 value.to_string() 直传后对应用例必须红 | T049-core |
| C04 三档身份 | clients.rs::legacy_host_migration_observe_loaded、migrate_backfills_role_from_host、migrate_is_idempotent_rerun、old_binary_rejects_new_user_version；capability_registry.rs::duplicate_id_major_version_rejected、missing_handler_rejected、contradictory_route_rejected、unregistered_wizard_step_rejected、cyclic_steps_rejected；test_capability_reconcile.py（位集对账） | 变异＝保留 bound 硬编码 search（server.rs:1031 push 列表）后能力回读用例必须红；移除 user_version 守卫后 old_binary 用例必须红 | T046/T047 |
| C05 撤销/轮换 | clients.rs::rotate_rejects_old_token、unbind_blocks_candidate_and_receipt、revoked_client_next_request_403（断言 401）、rotate_stage_failure_old_token_valid、rotate_publish_failure_retry_republishes、rotate_final_file_matches_db_version；mcp_surface.rs::stdio_revoke_after_initialize_403（断言 401）、stdio_rotate_old_token_401；browser_integrations.cjs 四步 | 变异＝移除 bind() revoked_at 判定（context.rs:249）；逐请求 bind 改 initialize 缓存（host.rs:118-121）；轮换跳过暂存直接双写后失败注入用例必须红 | T046/T050/T057 |
| C06 角色白名单 | agent_intake.rs::agent_writer_user_message_403_zero_rows、prose_user_said_x_no_user_observation、cross_category_403_page_rollback、body_and_envelope_double_impersonation_rejected、unbound_client_candidate_submit_rejected、body_actor_kind_any_value_403、agent_writer_shape_derivation_checks_vs_text、envelope_unknown_field_422_zero_rows、agent_writer_revoked_page_403_zero_rows、agent_writer_events_route_403 | 变异＝移除 T053 agent_intake 角色/形状推导函数（F1 变异点纪律）后必须红；零增量断言集＝§7.1 写集各表 | T053 |
| C07 候选语义 | mcp_surface.rs::bound_statement_list_shared_projection_no_evidence、bound_statement_list_without_share_403、intake_extract_remote_requires_ack；agent_intake.rs 提交即时候选态断言、candidate_foreign_task_id_403_page_rollback、intake_extract_inactive_by_default；生效路径 (a) 证据＝P0 第二步真实提炼接手样本（R2） | 变异＝投影切 shared_only=false（authority.rs:354-356）/移除 shared_enabled 门/移除 task_id 门/移除 local_endpoint 判定后对应用例必须红 | T051a/T053/T054/T063-full |
| C08 治理白名单 | mcp_surface.rs 治理段四负一正（§8.2 检查点）＋agent_resume_owner_stopped_source_403、agent_stop_host_source_403、agent_confirm_other_subject_preview_403、agent_stop_resume_footprint_changed_409 | 变异＝放开白名单任一限定/移除主体绑定/移除 stop-resume 足迹判定；每组 403 断言两表零增量 | T054/T048 |
| C09 history 请求单 | agent_history_import_403_zero_jobs、agent_history_request_pending_owner_zero_jobs、owner_confirm_creates_job_binding_request_id、agent_history_request_self_read_only、request_scope_mismatch_409（§8.3） | 变异＝请求单直接建 jobs/跳过 preview 绑定核验后零增量与 409 断言必须红 | T054 |
| C10 limits | agent_intake.rs 表驱动断言＋oversized_page_413_not_retryable、rate_limited_mcp_envelope_retry_after、concurrent_submissions_window_not_exceeded、revoked_source_replay_403_no_receipt＋重放不计费（§9.3） | 变异＝删限流判定/413 改回 429/配额判定移出事务/回放先于授权后对应用例必须红；注入 Clock 推进窗口 | T053 |
| C11 token 交付 | browser_integrations.cjs::token_file_delivery（本文新冻结用例名）；clients.rs::rotate_rejects_old_token | 变异＝install 沿用旧 client_id 不升级能力（integrations.rs:211-216）后能力回读必须红；stdout 含 token 即红 | T057/T046 |
| C12 幂等键 | mcp_surface.rs::write_tool_no_422_without_explicit_key、explicit_key_conflict_409、governance_confirm_bare_key_422_zero_rows；clients.rs::cross_subject_same_key_no_replay；governance_actor_scope.rs::cross_subject_same_key_independent、revoked_identity_replay_rejected、preview_one_shot_consumed、user_version_gate_blocks_old_binary、migrate_failure_restore_from_backup（回滚演练真跑）、legacy_rows_backfilled_owner_subject | 变异＝桥恢复 key=None（host.rs:122）后 no-422 必须红；confirm 接受裸键后 422 用例必须红 | T050/T046/T048/T054 |
| C13 矩阵 schema | test_validation_matrix.py（discover 收集，调用 build_validation_matrix.py exit 0＋产物过 schema）；本文交付时已跑 python3 -I 结构校验＋样例往返（§14.4） | 变异＝doc-claim 行标 evidence、synthetic_client_real_model 行标 evidence 后 schema 必须拒 | T063/T045 |
| C14 补丁 schema | gates/test_apply_patch.py 既有 33 用例（工具侧）；新 tests/v2/gates/test_patch_schema.py（T044 后续交付，discover 收集）：加载 schemas/patch.schema.json，把交叉变异 battery（/tmp/t045-check/validate.py 的变异清单入库为夹具）逐组断言 schema 判定==工具结构判定（除 §14.5 登记收紧）；本文交付时已跑交叉变异（§14.4） | 变异＝未知字段/双 insert+generate/未知占位符必须双拒；工具或 schema 任一侧变更后对齐用例必须红 | T045/T044 |
| C15 era/list_changed | mcp_surface.rs::tools_list_matches_capabilities_same_identity；Cargo.toml:28 钉住＋preflight/rmcp_spike.json＋model.rs:176/:201 源码 | 变异＝工具表脱离 capabilities 后集合比对必须红 | T050 |
| C16 bootstrap | mcp_surface.rs::agent_task_create_then_grant_change_succeeds | 变异＝移除 task.create 桥后链路正例必须红 | T054 |
| C17 门禁纪律 | gates/test_run_gates.py::TestCountedCasesAreNotHollow；judge.py p0_evidence_complete 单测（有文件缺字段→FAIL、无文件→FAIL、**旧 run_id/错误 binary_sha256/跨运行工件→FAIL**，T052 新增负对照） | 变异＝断言掏空成 pass 后 AST 检查必须红（T044 变异实验缺口 b 的永久修法）；出处校验移除后旧工件负对照必须红 | T044/T052 |

### 14.3 证据分级与矩阵判定规则（C13 配套）

evidence_kind（T063 细粒度）↔ evidence_type（T045 粗分类）映射冻结（**由 matrix.schema.json `$defs/row/allOf` 机械执行**，构建期不复写同一判定）：

| evidence_kind | evidence_type | verdict 上限 |
|---|---|---|
| real_host_real_model | [real-host, real-model] | evidence（宿主装入/真实提炼样本必须此级） |
| real_host_loopback | [real-host, loopback] | partial |
| synthetic_client_real_model | [real-model, fixture] | partial（宿主装入样本不得用此级冒充） |
| owner_native | [real-host]（真实二进制 CLI/HTTP 回执，receipt_ref 必填） | evidence（仅限 owner 原生动作样本） |
| fixture_only | [fixture]（含 loopback 端点时加 loopback） | partial |
| （仅文档断言） | [doc-claim] | partial/gap，不得 evidence |
| （无运行工件） | []（空数组）＋evidence_kind=null＋artifact_paths=[] | 仅 pending/blocked_external（**双向约束**：pending/blocked_external 行必为此表示；有夹具/文档证据的样本标 partial/gap，不得标 pending） |

构建期规则（build_validation_matrix.py 执行，schema 无法表达的跨行/兄弟值判定）：

1. P0 第一步子步 1–7 与第二步子步 1–5 中要求宿主装入/真实提炼的行，verdict=evidence 必须 real_host_real_model（T052 native_receipts.json 唯一证据源），且 receipt_ref 必须指向 native_receipts.json 内存在的键。
2. A01–A08/A12 沿用 004 证据标 carried_forward＋source_reference；无工件标 pending＋owner 包名；blocked_external 必须 owner=user＋external_materials 清单（禁「待材料」散文）。
3. 数字全部自 gates.json/包工件注入禁手改；红绿 run id 成对且 **red_run_id≠green_run_id**（Draft 2020-12 无法比较兄弟值，构建期判定）。
4. **(sample_id, acceptance_row, row_type) 三元组全局唯一**，重复即 FAIL（跨行去重非 schema 可表达）。
5. **聚合包集＝tests/v2/artifacts/manifest.json 已登记的全部包（机械推导，含 T046/T047/T049-core）**；tasks.md T063 的括注枚举为描述性、非白名单（R1 出口判据③的撤销负对照归 T046，漏聚合会误标 pending）。已交付工件但未登记 manifest 的包按未登记处理（pending+owner）。
6. **执行体名册核对**：real_host_real_model 且 verdict=evidence 的行不得来自 review_only 执行体（codex 0.171.0/dsh 0.2.2-rc.2 产品档结论 review_only，claude 2.1.308 可写）；host_version 与钉住矩阵（claude 2.1.308/codex-cli 0.171.0）核对，漂移即 BLOCKED（schema 只锁字符串形状，不钉版本正则——carried_forward 与未来版本 bump 不得被 schema 误拒）。

### 14.4 本轮 T045 自身验收（合并版，已执行，可复跑）

1. 两份 schema 过 JSON 解析与 Draft 2020-12 关键字子集结构校验（python3 -I；本机无 jsonschema 库，按 extension-contract.md:18 离线校验约定，T047/T063 须把两份 schema 按 $id 登记进离线资源表后做完整元 schema 校验——列入 §15.8）。
2. patch.schema ↔ apply_patch.py 交叉变异：schema 判定与工具 load_patch_files 结构判定逐组一致（除 §14.5 已登记的 schema 收紧）；examples/patch/T050.json＋T051a.json 双过；`python3 scripts/apply_patch.py --list --patches-dir specs/005-agent-mcp-interface/examples/patch`、`--dry-run --wave 2 --patches-dir specs/005-agent-mcp-interface/examples/patch`、`--selftest` 全 PASS（生成 arm 落点 server.rs:545-548）。**注：默认 patches/ 目录尚未创建，不带 --patches-dir 的 --list/--dry-run 返回 ERROR exit 2（补丁目录不存在），不得记为 PASS。**
3. matrix.schema 样例 **10 正 21 负**全过（含本轮新增负例：synthetic_client_real_model＋verdict=evidence 必须拒、尾随换行 sample_id/assembly_commit 必须拒、表外能力位/重复位必须拒、pending/blocked_external 行非空 evidence_type 必须拒、doc-claim＋evidence 必须拒、real_host_real_model 缺 real-model 介质必须拒、owner_native 缺 receipt_ref 必须拒）。
4. 合并版合计 **89 检查全 PASS**（python3 -I /tmp/t045-merge/validate_merge.py：JSON 解析＋Draft 2020-12 关键字合法性＋$id＋4 项样例双过＋10 正 21 负矩阵＋51 组 schema↔工具交叉变异；临时件不入库，永久对齐门禁＝§14.2 C14 的 test_patch_schema.py，T044 后续交付）。51 组中 7 组为 §14.5 登记收紧（sample/notes/规范路径），其余 44 组 schema 判定与工具 load_patch_files 逐组一致。

### 14.5 schema 与实现差异（C14 冻结附件，已登记）

| # | 差异 | 处理 |
|---|---|---|
| 1 | sample：实现 bool() 强转任意值（apply_patch.py:191）；schema 收紧为 boolean | schema 为准；工具对齐属 T044 后续小改。**工具不拦截 sample 包的 apply**（selftest 即以 sample=true 夹具真写，apply_patch.py:869-891）；「禁止对主线 apply」为流程约定（patch-format.md:9，examples 不在默认 patches/ 内）。工具侧 SAMPLE_PACKAGE 拦截（写入模式拒 sample 包）需同步改 selftest 夹具，列为 T044 后续提案，不在本轮静默改行为 |
| 2 | 顶层 notes 实现读取但不用、entry notes 不做类型校验（apply_patch.py:35,:211）；schema 收紧为 string | 同上 |
| 3 | plan §三.3 字段枚举为示意（无 wave/order/anchor/insert/generate 细节）；实现已要求 wave 必填、anchor 结构、insert|generate 二选一 | 以实现＋patch-format.md §九补白为冻结权威（REVIEW-DECISIONS.md:169 同结论）。**不引入 delete/replace/after arm**：工具 oneOf 仅 insert|generate（apply_patch.py:288-292），schema 超前会破坏 C14 对齐；体积优化走 function_scope＋锚点片段写法 |
| 4 | plan「git apply --check 形态」实为内存全量模拟；**「全有或全无」仅对模拟/冲突阶段成立**：execute_apply 写失败时尽力恢复（吞掉恢复失败，apply_patch.py:692-708），EVIDENCE_WRITE_FAILED 时保留目标改动返回 FAIL（:724-733），回滚仅能按已持久化 run 登记查找（:739-758） | 冻结实现语义如实描述；原子性硬化（首写前持久化 run_id＋各目标原内容/前后 SHA-256、恢复核对原哈希、恢复未完成拒绝新 apply）属 scripts 改造，列 §15.15 待用户拍板归 T044 后续 |
| 5 | commit_before/commit_after 同为 apply 时刻 HEAD 只读快照（工具禁提交，apply_patch.py:716-723） | 冻结；ASM 合流提交号由台账/manifest 登记 |
| 6 | 畸形占位符（如 `${method` 未闭合）加载期通过、渲染期 string.Template 抛错 | schema 镜像加载期检查；渲染期失败形态为工具异常，登记为已知边界 |
| 7 | tasks.md T045 文件清单写 `tests/v2/artifacts/matrix_schema.json`/`patches/schema.json`；patch-format.md:15 同 | 冻结权威路径＝本文配套 `specs/005-agent-mcp-interface/schemas/{matrix,patch}.schema.json`；tasks.md/patch-format.md 的路径别名修订归 ASM（本轮文件禁区，§15.4） |
| 8 | 尾随换行：Python re 的 `$` 允许单个尾随 \n（ECMA 不允许），`^[^\n\r]*$` 等模式在 Python 校验路径下泄漏；工具 _check_lines/re.fullmatch 拒绝任何 \n\r（apply_patch.py:137-145,:205,:230） | 两份 schema 的锚定模式追加 `not:{"pattern":"[\n\r]"}` 闭合两种语义；T047/T063 离线校验消费本版 |
| 9 | file_path 非规范路径：工具保留 `./` 前缀、按原路径字符串分组（apply_patch.py:197-209,:347-389），同一目标的 `x.rs` 与 `./x.rs` 会被分别模拟/写入致覆盖 | schema 收紧为规范仓库相对路径（禁 `./`、`.` 段、`//`、尾随 `/`）；工具侧规范化＋分组对齐属 T044 后续小改 |

## 15. 未决项与影响面

以下各条**无法在不改 src/docs/scripts 的前提下由本 spec 单方冻结为既成事实**，逐条列出归属与触发点，不含混：

1. **角色白名单/限流/撤销轮换/change_kind 限制/task_id 门均为新增交付**（F1/F6/F7/F18）：C05/C06/C07/C10 冻结的是目标语义，实码今天不存在；归属 T046/T053，签字后开工。R1 出口前这些条款的矩阵行不得标 evidence。
2. **治理单据主体迁移**（F4）：R1 治理回放仍全局单键；C12 以命名空间键规避跨主体碰撞，复合 UNIQUE、存量 owner 主体回填与 user_version 递增归 T048（R2）；版本守卫本身由 T046 先行交付（§5.1）。T048 与恢复执行器交付之间的回滚窗口局限按 plan §八.9 记台账；早于守卫的已发布旧制品只能停服＋恢复备份（守卫不 retroactive）。
3. **文档修订清单**（本轮 docs/** 禁区，修订归 ASM 在对应包合流的同一受审 diff 执行，门禁步 10 要求 current.sha256 同 diff 更新）：
   - cli-and-platform.md:68 系统凭据库承诺 → 改为 database/env 现状＋Keychain 命名缺口（F11），随 T046 合流 diff；
   - authority.md:87 未知路由共同可见集 → 文档已承诺、实码未实现，T049-core 实现后无需改文档，矩阵行以实码为准；
   - contracts.md:139-147 分位置披露 → cleanup_state 今天恒为字面量 logical_effect_applied（retrieval.rs:693），T059 兑现后文档无需改；
   - command-catalog.md:58 client.configure（PATCH /clients/{id}）→ T046 实装后目录与实码一致（F12）；command-catalog.md:73 statement.list（owner）→ T047 登记 agent.statement.list v1 新条目，owner 条目不变（CX-05）。
4. **路径别名**：tasks.md T045 与 patch-format.md 的 schema 路径（§14.5.7）由 ASM 修订指向本文配套 schemas/；T063 build_validation_matrix.py 与 T047 离线资源表必须消费冻结路径。tasks.md T045「matrix_schema.json 过 Draft 2020-12 离线校验」本轮以关键字子集＋交叉变异代替（§14.4.1），该验收口径别名由 ASM 同步修订。
5. **工具名兼容窗口**：search/expand 旧名保留至 T052 翻转 test_native_hosts.py:255 断言（§3.1 补充 1）；T050 合流时不得提前改名。
6. **存量非三宿主客户端回填 agent_reader**（§4.1 补充 2）会收回其 load_context 默认位——影响面：任何已用 generic register 建立的旧绑定需 owner 显式升级；R1 未发布，接受该破坏性最小权限默认。
7. **Windows 0600 等价**：NamedTempFile 权限语义 Windows 不同，C11 的 0600 断言先限 Unix；Windows ACL 等价验证归 T061 命名真机门槛，未验证前矩阵对应行 partial。
8. **完整 Draft 2020-12 元 schema 离线校验**：本机 python3 无 jsonschema 库（本轮实测 ModuleNotFoundError），T045 以关键字子集结构校验＋交叉变异代替；T047/T063 必须按 extension-contract.md:18 把元 schema 与两份消息 schema 登记离线资源表做完整校验，缺失即契约错误。
9. **T056/HTTP 直连拍板与桥传输路由读法（需用户签字确认）**：spike 已 PASS（§12.4），T056 是否开工由用户拍板（plan §八.7），默认不开工。**stdio 桥是独立进程、仅 HTTP 转发（F20），candidate.submit 的 MCP 路径物理上必须有一条回环 bound HTTP 路由可达服务进程**——故 POST /v2/agent/candidates 由 T053 无条件交付为桥传输路由；plan §八.7/tasks.md T053 的「条件路由」措辞只能落为「是否另作为自研 agent 公开直连入口登记与验收」，该读法变更由 ASM 按 plan §八 开头规则登记。默认＝不公开直连入口，路由仅供桥使用（同一 bound 鉴权/白名单/限流，无额外暴露）。
10. **409 子码 11 种枚举名**：由 T049-core 描述符冻结（覆盖 64 处调用点），本 spec 冻结最小可观察集与 retriable 语义（§3.3），不预造枚举名。
11. **limits「滑动窗」语义**：聚合结构下冻结为 60 秒锚定窗（§9.1），与 plan 措辞的差异在此明示；若要求逐事件真滑动窗，须改 limits 结构（加事件时间戳表），属 T053 范围变更，须按 plan §八 登记。
12. **host_adapter user_message 信任边界（需用户拍板是否硬化）**：本轮冻结「凭据持有＋accept 绑定」即信任边界（C06 表；HTTP 无法区分 hook 出处，F20）。外部评审 CX-01 要求独立采集凭据/受信来源登记（user_message 铸造与通用 MCP 凭据分离）——属新增注册基础设施，超出本轮冻结包范围；若用户要求，按 plan §八 登记变更后归 T053/T057 扩展。残留风险：同用户任何本机进程可读 host_adapter 凭据文件并以 user_message 提交（仅本机信任模型内）。
13. **recall_grants 逐任务读取许可**：现状 (source_id,client_id) 存储与任意 task_access 组合（§8.1 残留风险）；CX-03 的四元组（来源,用途,任务,接收路由）委托存储属新治理面，本轮以三重前置收窄，未来硬化须另立包。
14. **「四级 Tier-1 SDK」枚举缺失（需用户拍板）**：升级判据（§12.1）的该短语在 plan/protocol-choice 均未枚举具体 SDK，判据暂不可机械判定；签字时由用户给出四级名单，或接受「升级评估启动前补齐枚举」的命名缺口。
15. **apply_patch 原子性硬化（需用户拍板）**：§14.5.4 如实登记了写失败尽力恢复/证据失败保留改动的现状；CX-13 的预写 run 登记＋哈希核对恢复＋恢复未完成拒新 apply 属 scripts 改造（连带 selftest），是否立项归 T044 后续由用户决定；未立项前 ASM 合流按「FAIL 后先 --rollback/手工核对目标哈希再继续」规程操作。
16. **T054/T048/T053 文件清单扩充**（本轮 tasks.md 禁区，ASM 台账修订）：T054＋server.rs 装配补丁（请求单两路由＋history/jobs request_id 分支＋bound 治理分支）＋history_tasks.rs（create/create_all＋迁移）；T048＋governance_previews.client_id 列；T053＋error.rs/client.rs/host.rs（429 交付链）＋intake 来源自动注册；T046＋user_version 守卫与分段轮换。tasks.md T054 stop/resume 默认放开清单按 §8.1 收窄同步修订（非 plan §八锁值）。

## 16. 签字块

用户签字即确认以下全部锁定值（逐项勾选，缺项不开波 1）：

- [ ] §7/§8.3/§4.1：plan §八.5 三项隐私语义——(a) history.import 请求单＋所有者 preview→confirm（含 §8.3 存储/读回/绑定机制）；(b) agent intake 默认无 extract、仅 loopback 目的地（含 §7.3 remote_destination_ack 硬校验为 T053/T054 新增交付）；(c) Browser 保留 owner-console 全量、裸 POST /v2/clients 收窄＋install 豁免。
- [ ] §14.3＋本项：plan §八.6 首发口径（macOS 无签名本机真验证＋Linux/Windows fixture partial＋签名/真机/飞书 blocked_external 单列即可宣布 P0 首发）。
- [ ] §12.4/§15.9：plan §八.7 T056 拍板（默认不开工）；**确认 /v2/agent/candidates 作为桥传输路由由 T053 无条件交付的读法**；是否另公开自研 agent HTTP 直连入口（默认否）。
- [ ] §11：plan §八.10 幂等键来源（**按工具类分立**：非 confirm 写工具调用方入参＞桥 UUID；confirm 恒 grant-confirm-{preview_id}；只读不收键）。
- [ ] §13：plan §八.11 task bootstrap 选项 A（T054 同包 task.create/attempt.start）。
- [ ] §3–§14 全部条款（C01–C17）与两份 schema 冻结件；§15.12/§15.14/§15.15 三项未决硬化的默认姿态（信任边界不硬化／Tier-1 枚举延后／apply 原子性暂按现状规程）。

| 角色 | 签署 | 日期 |
|---|---|---|
| 用户（所有者） | ______ | ______ |
| 起草 SUB-opus | 本文交付即签 | 2026-11-24 |
| T045 合并者 | 四份评审逐条亲核合并即签 | 2026-11-24 |
| CX 只读复核 | reviews/T045-cx.json | ______ |
| DSH 只读复核 | reviews/T045-dsh.json | ______ |
| ASM 合流确认 | 三份复核结论一致＋current.sha256 同 diff | ______ |