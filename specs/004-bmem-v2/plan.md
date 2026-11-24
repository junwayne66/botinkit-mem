# bmem V2 实施计划

实施 R3 [技术方案](../../docs/v2/technical/README.md)，源文件实际落在 `src/v2/bmem` 和 `src/v2/web`，测试和夹具分置于 `tests/v2`、`tests/v2/data`。Rust 采用单 Cargo 包中彼此独立的领域模块，编译为一个原生程序，静态嵌入 Web 资源。批准版设计保留原文和指纹，实施位置以用户本轮目录约定为准。

| 合流点 | 依赖与验收 |
|---|---|
| A 初始化和配置 | 平台定位、唯一实例、回环服务、CAS 与幂等设置、静态 Web。验证真实 CLI/HTTP/浏览器 |
| B 声明和跨宿主 | 证据、逐维修订、时态、撤回。权限绑定、快照、MCP 与已验证生命周期，记录装入是否可观察 |
| C 来源和提炼 | 原生事件、持久接受与游标，真实模型端口，候选裁决、任务接手、可复算证据 |
| D 运营与分发 | 受控恢复、三档、词法/可选向量、三平台发布与真实端上线门槛 |

生产使用 SQLite WAL 权威、当前权限检查、外部模型调用在事务外、凭据引用及隔离测试根。依赖方向、用户故事、数据模型、协议、独立验收直接采用 [模块集成设计](../../docs/v2/technical/module-integration.md) 和 [服务契约](../../docs/v2/technical/contracts.md)。Claude Code 按互斥文件在独立目录开发。Codex 装配路由、核对差异、运行集成测试并维护台账。

## 数据模块改版

用户于 2026-11-21 通过 Figma 审核。Codex 从 `8113482` 建立六个独立 Git worktree 并派发给本机 Claude Code，负责 HTTP 路由、CLI、导航、静态嵌入与合流。项目使用原有 Rust、原生 JavaScript、Python 集成测试及 Chrome 浏览器测试。用户指定本轮由 Claude Code 开发，覆盖项目宪章中的默认执行器分工。

### 并行工作包

| 工作包 | 文件边界 | 接口及独立验证 |
|---|---|---|
| A 来源状态服务 | `src/v2/bmem/src/{integrations,feishu,ingestion}.rs`、来源专用后端测试 | SQLite 保存授权与集成映射，读取采集、共享声明与历史同步的当前值。通过现有来源和集成 GET 提供组合状态，保留旧字段。验证旧库识别、两种采集开关方向、重启及飞书状态回读。 |
| B 数据管理服务 | 新增独立的数据管理 Rust 模块及对应后端测试 | 复用现有导入提交和回执，提供有界的导入导出记录及受控 JSON 下载。既有导入记录从持久回执读取，新导出记录入 SQLite。验证来源、时间、撤回过滤、文件字段和错误响应。 |
| C 来源和数据管理界面 | `src/v2/web/intake.{js,css}` 与对应来源/导入浏览器测试 | 用冻结契约和隔离样本开发卡片、表格、入库图、刷新与失败状态、导入预览及导出。复用现有治理预览、飞书授权和历史同步控件。验证已有链接、配置草稿、键盘和窄屏。 |
| D 注入台账 | `src/v2/bmem/src/context.rs` 的投递查询、独立台账 JS/CSS 及对应测试 | 现有投递接口保持兼容，新增可筛选分页与汇总的读取契约。按 SQLite 实际提供、装入、采用回执计算状态和计数。验证无回执、部分装入、有装入无采用、有采用及分页边界。 |
| E 历史任务 | `src/v2/bmem/src/history_tasks.rs`、专用任务测试 | 一次性范围、检查点、跳过和重试使用 SQLite；实际入库复用现行来源权限与事件规范化。 |
| F 历史适配器 | `src/v2/bmem/src/history_{feishu,hosts}.rs`、专用适配器测试 | 飞书私聊、群聊、话题与文档；三个 Harness 会话发现和导入分页。 |

### 合流和验收

- 状态契约以 SQLite 业务数据为准。继续写宿主必须的本机 hook/MCP 文件，运行时探测信息单列。兼容老安装映射及飞书登记，在能够核验当前账号的情况下补齐 SQLite 状态。已有 API 客户端继续读取原字段。
- 页面首次进入、操作成功和回到前台时立即读取。可见页面按约 3 秒轮询，一次只保留一轮读取。离开或隐藏页面后释放定时器，后台响应不得覆盖正在编辑的表单或更新已卸载视图。导出日期边界随界面选择的本地日期转换为明确的起止时间，服务端按区间查询。
- 正式切换前先在隔离实例执行 Rust 检查、Python HTTP 集成与 Chrome 浏览器回归。用原生 CLI 修改采集状态，在打开的页面、刷新及重启后分别核对；对账合成事件、导入和导出文件、台账计数与实际回执。再按授权范围核对本机实例。
- Codex 使用 OCR 委托审查逐文件核对改动和原需求，记录 Claude Code 自报、审查覆盖、实际运行命令和未测项。用户审核通过原型后开始上述实现，合流验收完成后再提交用户做最终审核。

### 历史导入和连接器研究

Claude Code 只读核对 Botinkit Memory 的历史读取、现有界面和隔离夹具。DeepSeek Harness 只读核对官方实现与社区 MCP Connector。Codex 复核关键位置，维护规格、原型和验收任务。

| 能力 | 已有证据和实施边界 |
|---|---|
| 飞书消息 | `history_feishu.rs` 已用授权用户枚举 `p2p,group` 并读取聊天及话题页。当前 `ChatPage` 只保留 ID，`history.rs` 负责周期抓取和检查点。需要目录元数据、对象/时间选择、一次性任务与回执。以 [飞书官方 CLI 会话列表](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md) 和 [消息历史接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list) 核对实际授权和分页。 |
| 飞书文档 | 新增独立的文档发现与读取适配器，先支持由链接定位并读取当前有权访问的新版文档。文档列表仅覆盖当前授权可见范围，权限不足可定位到对象。变更版本只在上游提供且可核验时记入。参考 [飞书文档 API](https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/docs)。 |
| 本机 Harness | `history_hosts.rs` 已从三个本机宿主的会话文件读取并归一化。添加项目、对象、范围和消息类型筛选，使用稳定的会话标识定位本机配置中的路径。验证重复导入与持续扫描的交叉区间。 |
| 连接器目录 | 参考 [DeepSeek Harness MCP 实现](https://github.com/deepseek-ai/deepseek-harness/blob/2e39e70/docs/subsystems/mcp.md) 的按服务连接和生命周期，以及 [社区 MCP Connector](https://github.com/duhu2000/dsh-mcp-connector/blob/6bb9be1/README.en.md) 的目录与账户入口。Botinkit Memory 用内置能力定义和 SQLite 持久连接实例承接数据导入。目录声明、连接状态、用户授权的导入范围及执行状态分别建模。 |
| Hermes / OpenClaw | Hermes 先验官方 JSONL 会话导出。OpenClaw 按版本核对官方会话读取能力，当前官方文档区分运行时 SQLite、归档和较早的 JSONL。先用公开合成夹具验证，适配器成功后再将目录项标为可用。参考 [Hermes 会话文档](https://hermes-agent.nousresearch.com/docs/user-guide/sessions) 和 [OpenClaw 会话文档](https://docs.openclaw.ai/sessions)。 |
| 第三方 MCP | 首个试点考虑显式配置且已授权的 Streamable HTTP 服务。使用 `resources/list` 和 `resources/read` 验证可导入的资源与类型。若需要某个工具读取历史，对具体服务、方法及入参单独验证，`readOnlyHint` 只作为提示。通用增量采集要求稳定对象标识、分页或版本契约。参考 [MCP 资源规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/server/resources.mdx) 和 [工具规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-06-18/server/tools.mdx)。 |

### 历史导入实现分工

原型审核通过后，Codex 先确认对象、授权检查、事件标识、任务结果与检查点的契约。公共任务调度、飞书消息/文档、本机会话和 Web 交互可依据契约在独立工作区并行；Web 和原数据来源工作包统一管理 `intake` 文件，避免重复编辑。事件复用当前摄取路径。一次性范围有自己的检查点，持续同步保留既有水位，两者共用原生对象和版本的去重规则。SQLite 保存任务、选定范围、进度和回执，外部读取在事务外，单页写入与该页检查点在同一事务提交。Hermes、OpenClaw 与 MCP 先分别做只读兼容性验证，可与首期实现并行。

### 数据模块接口

| 路由 | 输入与输出 |
|---|---|
| `GET /v2/integrations` | 兼容原宿主和飞书流程字段，补充 SQLite 采集、共享权限和授权账号，以及 `source_activity`。 |
| `GET /v2/history/options` | 输入 `source_id`、`kind`、可选 `query` 与 `cursor`，返回可选 `targets` 及 `next_cursor`。 |
| `POST /v2/history/preview`、`POST /v2/history/jobs` | `source_id`、`kind`、`targets`、`from`、`to`、`content`。`kind` 为 `dm`、`group`、`docs` 或 `sessions`。消息时间是 RFC3339 半开区间，文档可省略。提交使用幂等键。 |
| `POST /v2/history/jobs` 全部历史 | Web 提交 `source_id` 和非空 `kinds`。所选类别在一个事务中入队，按同一幂等键返回 `jobs`。后台任务分页登记对象并保存目录游标，读取内容后与对象检查点一起提交。一次性任务凭已提交的历史授权写入，运行中撤回来源会终止该来源待完成任务。 |
| `GET /v2/history/jobs`、`POST /v2/history/jobs/{id}/{cancel,retry}` | 返回 SQLite 任务状态、已接收数、重复数、失败数和对象错误。 |
| `POST /v2/events?import=true` | 按现有 V2 事件页协议写入，显式文件导入与回执登记在同一事务。 |
| `GET /v2/data/records`、`POST /v2/data/export` | 读持久回执。导出按 `source_id` 及 RFC3339 半开时间区间，返回可下载的 `document`、文件名与回执。 |
| `GET /v2/deliveries/ledger` | 按 `host`、`loaded`、`used` 和 `page` 读取最近七天分页及计数；旧投递 API 兼容。 |
