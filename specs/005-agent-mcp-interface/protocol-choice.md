# 协议选型：MCP 还是 ACP

2026-11-23。结论已采纳 **MCP**，本文记录判断依据、被推翻的说法，以及现行规格对开发计划的直接影响。事实来源为联网实取的官方规格页与仓库（核对日期 2026-11-23），代码依据为本工作区 file:line。

## 一、结论

1. **主接口用 MCP。** 角色语义自洽（bmem 是 server，agent 是 host 里的 client），客户端覆盖最大，且本仓库已有可直接扩展的实现与授权链路。
2. **"ACP" 是三个不同东西的重名**，其中两个已不存在，剩下的那个不适合承载工具面：
   - **Agent Client Protocol**（Zed/JetBrains，agentclientprotocol.com）——编辑器驱动一个会自主干活的 agent，v1 只批准 stdio，没有鉴权层。
   - **Agent Communication Protocol**（IBM/BeeAI，REST）——**2025-08 已并入 A2A**，`i-am-bee/acp` 仓库横幅明写 "ACP is now part of A2A under the Linux Foundation"。
   - **Agent Connect Protocol**（AGNTCY）——与上一个又重名，其 `acp-spec`/`acp-sdk`/`workflow-srv` 三个仓库**已被 owner 归档**，不能作为现役备选。
   - A2A 本身（v1.0.0，2026-03-12；2026-08-17 加入 Agentic AI Foundation）定位是 **agent 之间委托任务**，不是"给 agent 提供工具与上下文"。
3. **`bmem acp` 不是不可能，而是收益不明**。若将来要让 bmem 出现在 Zed/Goland 的 agent 面板里，做一层薄适配在规范上是合法的（见第三节被推翻的说法），但 ACP 客户端不会渲染自定义方法，工具面暴露不出去。

## 二、被推翻的说法（我自己先前给你的判断有两处过强）

**说法 A（已推翻）**：「ACP 的 server 端必须是能自主产出 turn 的编码 agent，只做记忆读写的服务角色错配、只能伪装。」
核查结果：
- ACP 对 Agent 的定义是**描述性散文**（"Agents are programs that use generative AI to autonomously modify code."），不是规范条款；规范层面的硬约束只有方法面（所有 Agent MUST 支持 `session/new`、`session/prompt`、`session/cancel`、`session/load` 等）。
- **ACP v2 把会话面降级为可选能力**：原文 "Omitted or null means the Agent does not support session methods"，并且 RFD 明文为"不支持会话的 agent"保留了位置。
- 官方 agents 索引里收录了 `acp-llm-adapter` 的后端表格含 "mock | none: canned replies, no key" —— 无模型、固定回复的 ACP agent 是被官方承认的现实存在。
- 在 ACP 连接上跑自定义 RPC 是规范许可（下划线前缀方法名保留给扩展），不算"伪装"。
- 官方还在引入第三种角色：ACP proxies（介于 client 与 agent 之间）。

**所以正确表述是**：bmem 接 ACP 在规范上可行，但**没有可用的调用面**——ACP v1 的 Agent 侧方法是 `initialize/authenticate/session/*/fs-*/terminal-*`，**没有通用的 `tools/call` 或 `resources/read`**，工具调用被嵌在 prompt 回合内部经 `session/update` 上报。把 `search/expand/declare/receipt` 塞进去要么伪装成一次对话回合（语义扭曲、结果不可结构化寻址），要么走自定义方法（编辑器不会显示、不会调用）。

**说法 B（已推翻）**：「rmcp 3.11 启用 `transport-streamable-http-server` 就能直接挂进现有 axum 服务。」
核查结果有两处落差：
- 本仓库 rmcp 3.11.0 认识的协议版本上限是 **2025-11-25**，而 MCP 现行稳定版是 **2026-07-28**（第五个 revision，俗称 MCP v2），两者语义差异很大（见第四节）。
- 启用该 feature 需要联网拉取 `sse-stream`、`tokio-stream`、`tower` 等依赖（本地 registry 无缓存），我先前只核了源码没核编译。挂载方式还需实测 axum 0.8.9 / tower 0.5.3 的边界。

## 三、为什么仍然选 MCP（四条硬理由）

| 判据 | MCP | ACP（Agent Client Protocol） |
|---|---|---|
| 角色语义 | 记忆/知识服务的正确角色就是 server，暴露 tools（模型控制）/resources（应用控制）/prompts（用户控制）三类原语；spec 明确"声明某 capability 即 MUST 实现对应方法" | Agent 侧方法面向"被编辑器驱动的自主程序"；没有面向调用方的工具目录与结构化寻址 |
| 客户端覆盖 | 12/12 被点名的主流 agentic coding 客户端官方文档同时写明支持 stdio 与 Streamable HTTP 远程 server（"HTTP 支持面比 stdio 窄"在主流集合上**不成立**；真正变窄的是已被正式 Deprecated 的 HTTP+SSE，Codex 与 OpenCode 干脆不列 SSE） | 原生消费面集中在 Zed 及若干编辑器插件/适配器 |
| 鉴权与授权方向 | 有独立授权规范（HTTP 侧 SHOULD 遵循 OAuth 2.1；stdio 侧 SHOULD NOT 走该规范、从运行环境取凭证），方向与我们的"服务方对调用方逐项收窄授权"一致 | **没有规定的鉴权/授权层**；权限语义方向相反——`session/request_permission` 是 agent 向 client/人**要**许可，不是服务方限制调用方 |
| 本仓库可复用度 | 已有 `bmem mcp serve --client <id>`、rmcp 依赖、`context::bind` 身份分级、`/v2/capabilities` 门控与逐请求判权（[host.rs:112-131](../../src/v2/bmem/src/host.rs#L112-L131)、[client.rs:19-96](../../src/v2/bmem/src/client.rs#L19-L96)）；`docs/v2/technical/contracts.md:3` 早已规定"MCP 只注册该客户端获准使用的工具" | docs/v2 全文零次提及 ACP；需新建一整套无对应实现的会话/回合语义 |

## 四、MCP 现行规格（2026-07-28）对本设计的直接影响

这一节是本轮调研最实质的产出：**MCP 已经在这一版把"有状态"这件事废掉了**，与我们的授权与会话模型高度相关。

| 规格变化 | 事实 | 对本项目的直接影响 |
|---|---|---|
| 握手移除 | `initialize/notifications/initialized` 被删除；每个请求必须在 `params._meta` 自带 `io.modelcontextprotocol/protocolVersion` 与 `clientCapabilities`（两者 MUST，`clientInfo` SHOULD），缺失即畸形请求，服务端 MUST 返回 -32022/HTTP 400 | 不能依赖"连接建立时鉴权"。**每请求鉴权**从设计偏好升级为规格要求——正好与交叉评审提出的"禁止 initialize 缓存身份、撤销需下一请求生效"同向 |
| 协议级会话移除 | `Mcp-Session-Id` 与协议会话删除；list 结果 MUST NOT 随连接变化，但 MAY 随请求携带的授权/scope 变化；**跨调用状态只能用服务端自铸的句柄作为普通 tool 参数传递** | 我们的 `delivery_id`、`handle`、`preview_id`、`backup_id` 全部是服务端自铸句柄，天然符合新模型。T054 的 preview→confirm 必须继续用"显式两个工具 + 句柄参数"，**不要**改成协议会话态 |
| 服务端不再发起 JSON-RPC 请求 | `roots/list`、`sampling/createMessage`、`elicitation/create` 一律改为 `InputRequiredResult`（`resultType=input_required` + `inputRequests` 映射 + 不透明 `requestState`），客户端用**新的 JSON-RPC id** 重发原请求并附 `inputResponses` | 这是"agent 触发治理动作前要求人类确认"的正规机制。但 rmcp 3.11 不支持，因此本期仍用两工具方案；`input_required` 记为升级后的替换项，不进本期关键路径 |
| Roots / Sampling / Logging 正式 Deprecated（SEP-2577） | 最早移除时间为 2027-07-28 之后首个 revision；官方迁移建议分别是"用 tool 参数/资源 URI/服务端配置传目录""直接对接 LLM 供应商 API""stdio 写 stderr 或 OpenTelemetry"；新实现 SHOULD NOT 采用 | 计划里不得出现依赖 sampling/roots 的设计。当前没有，保持 |
| Streamable HTTP 形状变化 | 单 MCP endpoint，只支持 POST，GET/DELETE 一律 405；每个请求是独立 POST，响应为单 JSON 或"仅属于该请求"的 SSE 流；**强制头** `MCP-Protocol-Version`、`Mcp-Method`（所有请求）、`Mcp-Name`（tools/call、resources/read、prompts/get），缺失或不一致 MUST 返回 400 + -32020 HeaderMismatch；SSE 断线重放（Last-Event-ID）移除 | T056 的实现细节要按目标 era 分别写。若继续停在 2025-11-25 一代，这些头不是必需项，但升级后是硬性 400，**必须预留** |
| 变更通知改为订阅 | 客户端 `subscriptions/listen`（`toolsListChanged` 等过滤字段），服务端 MUST 先回 `notifications/subscriptions/acknowledged`；进程重启后客户端 MUST 重新订阅 | T050 里"工具清单变更通知"的实现路径随 era 变化；本期按旧 era 的 `notifications/tools/list_changed`，并在契约里注明新 era 写法 |
| 授权章节是可选的 | "Authorization is OPTIONAL for MCP implementations."；HTTP-based transport 只是 SHOULD conform；**OAuth 2.1 的 MUST 落在 authorization server 而非 MCP server**；但 MCP server 作为 OAuth 2.1 resource server 的义务确实是 MUST：实现 RFC 9728 Protected Resource Metadata、校验 token audience（RFC 8707）、MUST NOT 透传他人令牌；stdio 明确 SHOULD NOT 走该规范、从运行环境取凭证 | 我们"只允许本机 + 自建 Bearer + `X-Bmem-Instance`"的路线**不与规范冲突**（授权可选、stdio 不走 OAuth）。但 T056 若上 HTTP 传输，文档必须**明写这是非规范的本机自建鉴权、不实现 RFC 9728**，并把风险与理由登记成决策记录，不能默默偏离 |
| 兼容矩阵 | Legacy client（走 initialize，即 2025-11-25 及更早）× Modern-only server = **Fails**；要同时覆盖现网与新客户端需实现 dual-era；stdio 上客户端 SHOULD 用 `server/discover` 探测时代；旗舰客户端也尚未全面切新版（Claude Code 文档区分 v1/v2 runtime，且 stdio 默认仍请求旧 revision） | **rmcp 3.11 属于 legacy era**，因此 bmem 现状天然兼容现网客户端。本期不要追新 era：把"era 姿态"写进契约（我们是 legacy-era server），并留一个升级判据（rmcp 支持 2026-07-28 且四级 Tier-1 SDK 到位后再评估 dual-era） |

## 五、因此开发计划需要的修订（待与统一版计划合并）

1. **T045 契约冻结**新增一条：显式声明 bmem 的目标协议时代（legacy-era，≤2025-11-25）、`server/discover` 与 `_meta` 强制头属新 era 要求、本期不实现；并声明 MCP HTTP 传输采用非规范的自建鉴权且**不透传任何令牌**。
2. **T054 治理动作**明确采用"两个工具 + 服务端自铸句柄（preview_id）"的无状态形式，理由同时来自规格（跨调用状态只能靠 tool 参数）与安全（撤销要下一请求生效）。
3. **T050 工具表**在契约里同时写两种变更通知路径（现 era 的 `list_changed` 与新 era 的 `subscriptions/listen`），避免升级时改公开语义。
4. **T056 Streamable HTTP**降级判定：在 rmcp 只到 2025-11-25 的前提下，它给"只允许本机"带来的唯一实际收益是"不支持 stdio 的自研 agent 也能连"；而该收益可以**直接复用已有 `/v2` HTTP API ＋ generic token** 取得。是否保留 T056 交用户拍板（见统一版计划"待确认"）。
5. **依赖预检**：`transport-streamable-http-server` 的启用需联网拉 `sse-stream`/`tokio-stream`/`tower` 等依赖，属未验证事项，应在 T044 里做一次编译预检而不是写在风险表里当"退路"。

## 六、本机外部评审执行体的可用性（影响计划的模型分工）

- 可用：`claude 2.1.308`、`codex-cli 0.171.0`（`codex exec --sandbox read-only --skip-git-repo-check` 实测通过）、`dsh 0.2.2-rc.2`（`dsh headless` 实测完成一份真实评审）、会话内 Claude 各档（opus、sonnet 可用）。
- 不可用（本轮实测证据）：`agy 1.5.15` 三次调用、两个模型均被服务端以账号地区资格拒绝（"not currently available in your location"）；`gemini` CLI 返回 "This client is no longer supported … please migrate to the Antigravity suite"；opencodex 路由的 `gpt-5.6-sol / gpt-5.6-terra / gpt-5.5 / gpt-5.4-mini` 与 `fable` 档全部返回 `Model not exist`。
- 影响：计划 §5 原分工表里凡是用 AGY / SUB-sol / SUB-terra / SUB-luna / SUB-mini 的 lane **都要重派**；原先设想"两个独立模型给同一结论才合流"的隐私复核闸门需要换成"opus ＋ 外部 codex ＋ dsh"三家交叉，否则该闸门不可执行。

## 七、依据

- MCP：`https://modelcontextprotocol.io/specification/2026-07-28/{versioning,deprecated,basic/index,basic/transports/stdio,basic/transports/streamable-http,basic/authorization/index,basic/patterns/mrtr,basic/patterns/subscriptions,server/index}.md`、`.../changelog.md`、`https://blog.modelcontextprotocol.io/posts/2026-07-28/`
- ACP：`https://agentclientprotocol.com/protocol/v1/{overview,transports,security,methods,prompt-turn}`、`/protocol/v2/…`、`/rfds/streamable-http`、`/rfds/proxy-ch…`、`/get-started/agents`
- A2A 与 IBM/AGNTCY 归档：`https://github.com/i-am-bee/acp`、`https://lfaidata.foundation/communityblog/2025-…`、`https://aaif.io/blog/a2a-joins-aaif`、`https://a2a-protocol.org/latest/specification`、`https://github.com/a2aproject/A2A/releases`、`https://github.com/agntcy/{acp-spec,acp-sdk}`
- 代码与规格：[host.rs](../../src/v2/bmem/src/host.rs)、[client.rs](../../src/v2/bmem/src/client.rs)、[server.rs](../../src/v2/bmem/src/server.rs)、[docs/v2/technical/contracts.md:3](../../docs/v2/technical/contracts.md#L3)、[Cargo.toml:28](../../src/v2/bmem/Cargo.toml#L28)、rmcp 3.11.0 本地 crate 源（协议版本常量至 `V_2025_11_25`）
