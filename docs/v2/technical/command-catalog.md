# bmem 子命令与 Web 操作契约

日常使用先记住六个入口。首次用 `bmem init`，接入资料用 `bmem connector`，配置模型用 `bmem model`，追加本人声明用 `bmem declare`，找回资料用 `bmem search`，排障用 `bmem doctor`。在 `bmem web` 中修改同一设置、声明、权限或作业，另一端刷新后应看到同一对象、修订和处理进度。

本文规定 V2 计划实现的命令。以下表格省略命令前缀 `bmem` 和 HTTP 路径前缀 `/v2`。尖括号表示必填值，方括号表示可选项。U1–U6 对应[管理台页面](../ui/README.md)。设置、运行状态、安装向导是管理台的系统操作。列在 P1/P2 的命令随对应功能开放。

## 同一次操作怎样从两个入口执行

- **服务停机时提交。** `declare`、`config set` 等业务写命令先定位已初始化实例；若服务停机则拉起或复用该实例，验证身份后按原幂等键提交。拉起失败时报告未受理及排障入口。未初始化时提示 `bmem init`。浏览器断线保留草稿，由 `bmem web` 重开同一实例，再用原键重试；`doctor` 可在停机现场诊断。[实例定位与启动规则](cli-and-platform.md)
- **每项修改一个处理器。** CLI 解析参数，Web 表单采集同一组字段，调用同一服务处理器。两个入口共享输入类型、修订检查、身份授权、错误码和回执。CLI 不直接改 TOML 或 SQLite。网页刷新读取服务结果；后台操作返回单据或待办 ID，两个入口都能继续查询。
- **命令与能力。** `command_id` 是稳定的能力名（如 `grant.change`）。专用表单与专用 CLI 可以共用一个窄字段操作，例如编辑模型端点和 `config set` 都由 `config.patch` 校验。复合向导有自己的 `kind=workflow` 描述符，顺序调用已登记的子能力并复用各步骤的回执。预览与提交有不同的能力与结果。下表给出 CLI、Web、服务路由及入参如何选中该动作。[能力描述符与扩展步骤](extension-contract.md)
- **确认与重试。** 所有者由已认证身份解析。Web 与 CLI 的同一本机所有者会话归为同一主体，宿主凭据属于各自客户端。写操作适用稳定事件 ID 或 `Idempotency-Key`，命令行可用 `--idempotency-key <key>` 恢复一次相同提交，Web 在第一次提交前生成并在重试中沿用。作用域为主体和动作，同键异文返回 `409 key_conflict`。回放按当前授权裁剪。不同意图不共用键。
- **并发编辑。** 设置页打开时保存 `expected_revision`，交互 CLI 读同一修订再提交。脚本可用 `--expected-revision <n>`。过期修改返回 `409 revision_changed`、可见的现行修订与重读入口。授权和恢复分别使用预览绑定的策略版本、目标修订或库水位。声明按追加语义提交；原话及其逐维度回执可重读。[服务错误与回执](contracts.md)
- **输出与发现。** 管理命令支持 `--json`。正常结果写 stdout，诊断写 stderr；成功或受理为 0，执行失败为 1，命令或参数错误为 2。后台动作受理时返回可查询的 ID 和状态，完成读回另列。分页命令接受 `--cursor`、`--limit`。帮助使用 `bmem --help` 或 `bmem <组> --help`。脚本用能力目录的 `stage`、可用状态、输入字段与输出版本判定是否能调用。

## P0 · 安装、配置和接入

| CLI | command_id 与路由 | Web 入口与回读结果 |
|---|---|---|
| `install`；`--help`；`--version` | `platform.install`、`cli.help`、`cli.version`，本机平台或 CLI | 安装器进入当前用户目录，帮助和版本可离线使用。已有服务的「关于」显示运行版本及发行目标 |
| `init [--non-interactive]` | `setup.init`，向导 | 初次使用可直接运行此命令。也可先运行 `web`，由同一引导程序只准备本机数据根、所有者和回环服务，再在浏览器完成其余步骤。网页「初始设置／继续设置」与 CLI 走相同的配置、接入和授权步骤，可重跑、可接续 |
| `web [--no-open]`；`service start`；`service run` | `web.open`、`service.start`、`service.run`，本机进程启动 | 前者打开已有或新启动的回环服务；后两项分别在后台和前台托管。空机器须先运行已安装程序。打开管理台后可完成所有持久设置 |
| 裸命令；`status`；`service status` | `health.read`，在线时 `GET /health` | 「运行状态」按能力展示实例、版本、就绪度、积压和最近错误。服务停机时 CLI 直接读取本机定位和进程状态，显示已停止，服务保持停机。裸命令在未初始化时给出 `bmem init`。[停机诊断](cli-and-platform.md) |
| `doctor [--json]` | `health.diagnose`，`POST /diagnostics` | 「运行诊断」回传各项观测、原因及修复入口。服务停机时 CLI 可直接报告本机目录和进程状态；向量关闭时跳过嵌入探测 |
| `service stop` | `service.stop`，`POST /service/stop` | 「运行状态 → 停止」先回受理，再停止服务。浏览器显示连接关闭及重新启动命令；仅启动时需要本机程序 |
| `service enable`；`service disable` | `service.autostart`，`PATCH /service/autostart`，字段 `enabled` | 「登录时启动」调用同一平台适配器。回读当前用户启动项的**实际登记结果**，无用户服务管理器时显示原因 |
| `update --check`；`update` | `update.check`，`POST /updates/check`；`update.apply`，`POST /updates/apply` | 「程序更新 → 检查／安装」选择同一签名制品，返回目标版本、格式前提与操作号。停服前将操作号与升级步骤写入本机更新日志，重启后由同一操作号核对实际版本及回退状态 |
| `uninstall` | `platform.uninstall`，`POST /platform/uninstall` | 「关于 → 卸载程序」与 CLI 展示同一份已安装组件清单，确认后移除本程序、它登记的登录启动项及宿主接入项。用户数据根和自有模型保留，卸载结果由本地维护程序写入单据，再次安装后可查询 |
| `capabilities list`；`capabilities show <command_id>` | `capability.list`，`GET /capabilities`；`capability.show`，`GET /capabilities/{command_id}` | 「可用能力」按本构建、平台、角色列出版本、输入输出、CLI 用法、Web 入口和就绪条件。已注册但待配的能力注明原因 |
| `config get [<key>]`；`config set <key> <value>` | `config.read`，`GET /config`；`config.patch`，`PATCH /config` | 「设置」修改同一份已声明类型的字段。P0 常用 `profile`（`data`、`balanced`、`high`）与 `vector_search.enabled`。返回新修订、生效值以及相关后台处理状态 |
| `model list`；`model add <name>`；`model configure <name>`；`model remove <name>` | `config.read`（模型列表）；`config.patch`（`models` 的新增、字段更改、移除） | 「设置 → 模型」以名称选择配置，填写协议、端点、模型 ID、凭据引用。已绑定模型先解除用途再移除；本机运行时和权重由其所有者保管 |
| `model test <name> [--for <purpose>]`；`model use <name> --for <purpose>`；`model unbind --for <purpose>` | `model.test`，`POST /models/{id}/test`；`config.patch`（设置或清空对应 `bindings` 项） | 「测试模型／设为提炼或嵌入模型／解除绑定」。名称解析为稳定配置 ID。`purpose` 为 `extract` 或 `embed`，测试默认 `extract`。向量未启用时服务对 `--for embed` 的测试请求统一返回功能已关闭，CLI 与 Web 均显示原因，不探测端点、不发出嵌入调用。分别报告最近验证和实际绑定 |
| `model pull <tag> --for <purpose>` | `model.pull`，`POST /models/pull`，输入已选本地 Ollama 端点、标签、用途 | 「本地模型 → 下载」显示空间、已知大小、下载及能力测试进度。完成后可绑定。默认关闭向量时嵌入下载要求先显式开启；任何设置入口均无自动嵌入下载 |
| `connector list`；`connector show <source_id>` | `source.list`，`GET /sources`；`source.show`，`GET /sources/{id}` | U4 列来源实例及能力，详情给出实际账户、采集范围、覆盖缺口和游标，宿主来源另关联注册客户端 |
| `connector add <type>`；`connector configure <source_id>` | `connector.add`、`connector.configure`，向导 | U4「接入来源／编辑」。根据来源分别登记设置、来源身份、可选的宿主绑定和授权。每一步的提交边界见下文。账号换人时另建来源 |
| `connector test <source_id>`；`connector auth <source_id>` | `connector.test`，`POST /sources/{id}/test`；`connector.authorize`，`POST /sources/{id}/authorization` | U4「测试连接／重新授权」。按来源能力完成本机宿主探测或提供方登录流程；显示同一实际账号及最后观测时间。提供方登录与 bmem 用途许可分别回读 |
| `connector pause <source_id>`；`connector resume <source_id>` | `capture.stop`、`capture.resume`，均 `POST /operations`，分别为 `stop_capture`、`resume_capture` | U4「停止／恢复采集」。显示原游标、覆盖时间、操作号。停止后历史资料按既有授权读取 |
| `connector sync <source_id> [--since <time>]` | `capture.sync`，`POST /operations`，`kind=sync_source` | U4「立即同步／回填」。从已提交游标增量同步；有能力且已获该时段权限时可请求历史范围。返回操作号、实际覆盖区间、缺口 |

配置修改携带类型化补丁。示例 `config set profile high --expected-revision 7` 与网页选择「高压缩／保存」都提交 `{"expected_revision":7,"patch":{"profile":"high"}}`。成功返回 `config_revision=8` 及当前值。模型操作使用同一 `patch` 内的 `models` 或 `bindings` 变更。服务检查可修改字段和模型约束；原文、声明、授权和游标由各自的业务命令修改。凭据由服务写入平台凭据存储并在配置中只保留引用，提交中断后当前已生效配置仍能定位原凭据。[配置存储与平台差异](cli-and-platform.md)

| 输入来源 | 允许发送的 `patch` 部分 | 写入边界 |
|---|---|---|
| 通用 `config set` 与 Web 的标量控件 | P0 公开可写路径为 `profile`、`vector_search.enabled`。后者转换为 `{"vector_search":{"enabled":true}}` 或 false | 由已注册键解析枚举／布尔，拼错键或类型返回字段错误。GET 只输出授权可见的配置及密钥已配置状态 |
| `model add/configure/remove`、`model use/unbind` 与 Web 模型表单 | `models.op` 取 `add`、`update`、`remove`。添加提交名字、协议、端点和模型 ID，由服务分配稳定 ID。更新、移除按稳定 ID。绑定提交 `bindings.extract` 或 `bindings.embed`，以稳定 ID 设置、以 null 解绑 | 服务检查模型用途与能力，返回对应配置修订。新目的地应另有该来源／用途的发送授权 |
| `connector add/configure` 与 U4 连接参数表单 | `connectors` 接收 `source_id` 及 `settings`。首发设置可含显示名称、支持分批来源的页面大小、凭据引用；清除本机引用时将 `credential_ref` 置为 null，对应专用表单或 `connector configure <id> --clear-login` | 类别和回填范围的实际许可用 `grant.change`；客户端解绑用 `client.configure`；采集状态、游标、证据及遗忘单均由领域库维护。通用 `config set` 不接收这些专用对象的任意路径 |

专用字段的互斥关系、凭据处理和完整 JSON Schema 见[配置修改契约](interfaces/config-patch.md)。

模型或连接器表单里如需填写密钥，CLI 的 `model configure <name> --credential-stdin`、`connector configure <source_id> --credential-stdin` 与 Web 的「录入／替换凭据」都先调 `credential.store`（`POST /credentials`）。该步骤仅对所有者开放，输入走掩码输入或标准输入，返回新凭据引用及状态，再由同一次操作调用 `config.patch`。环境变量引用可直接填变量名。二者都不在参数、配置回显、重试记录或日志中留下密钥明文。[设置写入契约](interfaces/config-patch.md)

### 向导的子步骤

| 工作流 | 子能力和条件 | 中断后的结果 |
|---|---|---|
| `setup.init` | 引导程序准备本机服务；已认证会话读取 `config.read`，按选择执行 `config.patch`、`connector.add`、`model.pull`（仅用户选本地下载）、`model.test`、`service.autostart` | 最后一次已完成的步骤可重读，重新运行只补缺项。选择跳过模型时报告“待配模型”，已有声明仍可用 |
| `connector.add` | `source.register`（`POST /sources`）登记来源，`config.patch` 保存连接参数。宿主类型另用 `client.register`（`POST /clients`）登记指定路由和宿主接入项；需要来源登录时调用 `connector.authorize`。最后通过 `operation.preview`、`grant.change` 提交选定范围 | 返回稳定来源/客户端 ID，各步骤关联同一次接入意图。连接成功且用途许可仍欠缺时显示「待授权」，暂停采集与出站；重试从已有 ID 继续 |
| `connector.configure` | 读取 `source.show` 和 `config.read`。普通连接参数提交 `config.patch`。来源支持的类别/回填范围或授予用途改变时，指引同一次 `operation.preview`、`grant.change`；变更宿主接收路径时编辑经过登记的绑定所用的 `client.configure`（`PATCH /clients/{id}`），新路由另验授权 | 返回配置修订和授权版本。仅填写端点或勾选类别时，服务继续按当前已提交的授权版裁决。界面分项显示实际完成步骤 |

接入流程在产生外部副作用前保存稳定步骤键，并读回已有来源、宿主登记和登录流程结果。涉及系统宿主配置文件时记录实际写入结果和恢复方法。CLI 或 Web 的续办使用同一来源与步骤状态，不会重复插入宿主接入项。宿主路由或模型目的地改变需完成相应范围授权后才可发送资料。

### 一个来源停用后怎样保留或清理

1. 暂停某个账号采集，用 `bmem connector pause <id>` 或 U4「停止采集」。需要长期断开时，在 U4「用途」或 `grant set` 分别预览并确认撤销该来源的采集、指定接收端读取或模型发送许可。用 `grant list --source <id>` 逐项核对实际结束的许可。
2. 要移走本机登录，在同一来源的「连接参数 → 清除本机登录」或 `connector configure <id> --clear-login` 移除 bmem 的本地引用；共享的操作系统／上游凭据按其持有者管理。已注册宿主还可在「助手接入 → 解除本机绑定」或 `connector configure <id> --unbind-client <client_id>` 调 `client.configure` 停用该绑定、拒绝旧客户端凭据，移走本工具管理的接入项并回报实际结果。来源标识、历史、游标及既有投递记录仍可按当前授权查账。
3. 需要清理已持有资料时，另用 U5「遗忘该来源」或 `forget --source <id> --preview` 查看截至预览水位的来源对象与版本、派生依赖及位置；确认绑定同一范围并在写事务中复查。新到对象或权限变化影响清单时要求重新预览。继续采集新对象依当前采集许可决定，若希望以后同样不采集，另行停止采集或撤销采集用途。单条资料可改用 `--root <root_id>`。治理单区分立即禁读、受管文件清理和已发往外部。重新加入同一来源须重新授权，重导仍遵守原墓碑。

## P0 · 记忆、接力和证据

| CLI | command_id 与路由 | Web 入口与回读结果 |
|---|---|---|
| `declare <原话> [--scope <scope>] [--share <policy>] [--corrects <revision_id>] [--corrects-attempt <attempt_id>]` | `statement.append`，`POST /statements` | U1「追加声明／纠正」。输出 statement_id 和每个事实维度的修订及生效状态。修正时追加原话，前项映射 `corrects_revision_id`，后项或网页「关联这次误用」映射 `corrects_attempt_id`。关联尝试后可从投递与任务记录回查，保留历史。省略范围或共享选择时交互确认；脚本可明确指定 |
| `statement list [--valid-at <time>] [--known-at <time>]`；`statement history <fact_id>` | `statement.list`，`GET /statements`；`fact.history`，`GET /facts/{fact_id}/history` | U1「当前声明／历史与依据」。列表和声明回执返回服务签发的 `fact_id` 及可读的维度 `key`。两端均将所选条目的 `fact_id` 原样传给历史接口，同名维度在不同范围可有不同 ID |
| `search <query> [--task <id>] [--route <ref>]`；`evidence expand <handle> [--before <n>] [--after <n>] [--route <ref>]` | `search.query`，`POST /search`；`evidence.expand`，`POST /evidence/expand` | U3「搜索／补读原文」。服务端按已登记目的地、用途、时点和预算返回命中、邻近证据与缺口。默认接收者是当前操作的所有者本机界面；选宿主时须核对该路由可见范围 |
| `task list [--project <id>]`；`task handoff <task_id> [--route <ref>]` | `task.list`，`GET /tasks`；`task.handoff`，`GET /tasks/{id}/handoff` | U3「接手任务」。分别看合法候选、工件和目标、计划、已验证结果、未解问题与可补读出处 |
| `client list`；`delivery list [--client <id>] [--attempt <id>]`；`delivery show <delivery_id>` | `client.list`，`GET /clients`；后两项 `delivery.read`，`GET /deliveries`（单条带 `delivery_id`） | U2「助手／投递详情」。并列当前应取版本、实际提供、宿主装入、可验证采用及独立任务结果，展示凭据不足的阶段 |

`POST /v2/search` 支持可选的 `exclude` 字符串数组。来源可选 `feishu`、`codex`、`claude-code`、`dsh`，内容类型可选 `statement`、`user_message`、`assistant_message`、`tool_result`，飞书类别可选 `group`、`dm`、`docs`、`feishu_unclassified`。空数组保留默认检索范围。例如 `{"query":"设备","mode":"vector","exclude":["group","tool_result"]}` 会同时排除已标明类别的飞书群聊和所有工具回执。回退到关键词时沿用这些条件。

所有者从程序或网页使用“接手／查看”触发的读取，不产生代替宿主的装入或采用证明。宿主与验证器专用动作通过各自的可信协议采集，管理台和 CLI 查看实际回执，不提供填写“已装入／任务通过”的生产操作。[宿主调用顺序](contracts.md)

宿主安装清单引用两类程序入口。`bmem mcp serve --client <id>`（`host.mcp_stdio`）通过标准输入输出转送已绑定宿主的 MCP 请求；`bmem hook <host> --client <id>`（`host.hook`）读取该宿主适配版本声明支持的回调输入，调用事件、上下文或回执协议。客户端 ID 只用于定位绑定，身份由本机受控凭据建立。二者均登记为本地程序入口，在线动作沿用宿主授权、输出及重试契约。U4 的「接入助手」注册入口，U2 查看实际观测；可用回调以已验证的宿主版本为准。

日常接力可以从以下几条命令开始。正文和标识均为合成示例；查询返回的真实任务 ID、原文句柄供下一步使用。

```text
bmem declare "以后报告先写结论" --scope global --share authorized_assistants
bmem statement list --json
bmem statement history f_72
bmem task list --project creek
bmem search "入库超时" --task t_creek_import
bmem task handoff t_creek_import
bmem evidence expand h_run_2 --before 1 --after 1
```

## P0 · 权限、作业和恢复

| CLI | command_id 与路由 | Web 入口与回读结果 |
|---|---|---|
| `grant list [--source <id>]` | `grant.list`，`GET /grants` | U4 按来源、接收路由和用途读取权限版本 |
| `grant set --source <id> [--route <ref>] --purpose <purpose> --allow/--deny --preview`；同参以 `--confirm <preview_id>` 替换 `--preview` | `operation.preview`，`POST /operations/preview`；`grant.change`，`POST /operations`，均取 `kind=change_grant` | U4「修改用途 → 查看影响 → 确认」。新建或收窄展示到字段级的范围和各接收端变化；预览只绑定目标授权行与模型目的地，无关活动不使预览失效；extract/embed 授权无就绪路由时预览即返回 `409 model_not_ready`；确认绑定同一目标、策略版本与影响，`--confirm` 幂等键由 `preview_id` 派生，丢失回执可重试回放 |
| `forget --root <root_id> --preview` 或 `forget --source <source_id> --preview`；同目标 `--confirm <preview_id>` | `operation.preview`；`governance.forget`，`POST /operations`，均取 `kind=forget` | U5「遗忘」。按明确范围受理，立即生效水位和各位置清理进度分别显示；预览绑定目标声明修订或来源自身游标与已接收页水位，无关活动不使预览失效，`--confirm` 幂等键同样由 `preview_id` 派生。原文引用不足时先查找／展开定位目标 |
| `operation list [--kind <kind>]`；`operation show <id>`；`operation retry <id> --location <location>` | `operation.list`，`GET /operations`；`operation.show`，`GET /operations/{id}`；`operation.retry`，`POST /operations`，`kind=retry` | U5 或「运行状态 → 操作记录」。只重试该单据允许重试的失败位置，沿原号继续观测 |
| `job list [--state <state>]`；`job show <job_id>`；`job retry <job_id>` | `job.list`，`GET /jobs`；`job.show`，`GET /jobs/{id}`；`job.retry`，`POST /jobs/{id}/retry` | 「运行状态 → 后台作业」，记忆库「查看全部提炼结果」会以 `GET /v2/jobs?has_candidates=true` 分页列出当前仍可查看、已产出候选的完成作业。重试可重试的停候作业，重新核验输入版本、来源授权和实际目的地，返回相同作业的下一次尝试 |
| `index status`；`index rebuild <projection>` | `health.read`（投影水位）；`index.rebuild`，`POST /operations`，`kind=rebuild_index` | 「运行状态 → 索引」。输入 `lexical` 或已启用且就绪的 `vector`，返回重建输入水位和单据，校验后切换 |
| `backup create [--only revocations]`；`export create --source <source_id>` | `backup.create`、`export.create`，`POST /operations`，分别取 `kind=backup`（完整包或独立撤回清单）、`kind=export`（授权范围） | U5「创建快照／另存撤回清单／导出数据」。产物先进入受管位置；按单据读回范围、截止点、清单及摘要 |
| `operation download <id> --out <path>` | `operation.download`，`GET /operations/{id}/artifact` | U5「下载产物」。服务校验所属单据、完成状态、当前许可与可交付版本，按流发送；CLI 本地写文件，浏览器使用下载保存。过期受管包列出需重制原因 |
| `restore preview <bundle> [--revocations <manifest>] [--target-home <绝对路径>]`；`restore apply <preview_id>` | `restore.preview`（输入包向导，见下）；`restore.apply`，`POST /operations`，`kind=restore_apply` | U5「选择包／预览／开放合格恢复」。默认恢复至已登记根；迁移时网页「目标数据目录」与 CLI 均提交 `target_home`。预览绑定目标、实例及撤回水位；展示隔离完整性、缺口和切换资格，待确认后生效 |

`grant set` 的 `--allow` 与 `--deny` 互斥。涉及类别、字段、历史时间段时两端可填 `--category <type>`、`--field <path>`（均可重复）及 `--from/--until <time>`，由相同输入类型校验。初次登记各类来源时按注册类型展示该用途所需维度，例如采集按来源绑定、向模型外发按已登记接收路由绑定。命令行交互式省略模式选项时先显示预览并等待明确确认，无交互写入必须引用预览。上游主动撤回由受信来源的事件协议处理。

`restore.preview` 先把用户选择的包流式交给 `artifact.stage`（`POST /artifacts/stage`），返回有界保留的受管标识及摘要。CLI 读取本机文件，Web 使用文件选择器，服务只处理提交的字节。若选择独立撤回清单，按同一方式暂存并关联。随后调用 `restore.inspect`（`POST /operations`、`kind=restore_preview`），按操作号查询终态及 `preview_id`。放弃该流程时活动库继续工作，未采用的隔离暂存可按保留期清理。孤本仅提供可证明截止点的隔离历史查看。[恢复条件](operations.md)

## 后续开放的子命令

| 阶段与 CLI | command_id 与路由 | Web 入口与约束 |
|---|---|---|
| P1 `evaluation list`；`evaluation run --baseline <id> --candidate <id> --suite <id>` | `evaluation.list`，`GET /evaluations`；`evaluation.run`，`POST /operations`、`kind=evaluate` | U6「冻结对照」。固定任务、输入快照、用途与预算，读回样本分母、退化、成本及归因缺口 |
| P2 `dataset build --scope <id> --purpose <purpose>`；`training run --dataset <id> --baseline <id>` | `dataset.build`、`training.run`，`POST /operations`，分别取 `kind=build_dataset`、`kind=train_candidate` | U6「创建数据集／训练候选」。作用域清单指定根来源、任务族、计算环境、冻结截止点和种子。仅返回该用途获准的输入版本、运行和工件 |
| P2 `release list`；`release activate <id> --scope <id> --mode <mode>`；`release rollback <id> --scope <id>` | `release.list`，`GET /releases`；`release.activate`、`release.rollback`，`POST /operations`，取 `kind=activate`、`kind=rollback` | U6「影子／限定试用／发布／回退」。`mode` 为 `shadow`、`trial` 或 `active`。引用通过的报告与目标范围，区分期望版本与运行端实报 |
| P2 `training revoke --source <id> --preview` 或同目标 `--confirm <preview_id>` | `operation.preview`；`training.revoke`，`POST /operations`，均取 `kind=revoke_training` | U5/U6「撤回训练用途」。返回受影响数据集、父子工件、部署、已停止使用的水位和不可核验副本 |

这些扩展沿用能力登记、权限和回执契约。P1 的关系路径、受控融合等查询策略可以给 `search.query` 增加可选输入，复用已有查询入口。尚未安装或本版本未交付的动作不显示为可执行。已登记但来源端不支持历史回填、缺少生成模型、无训练授权等情形给出对应不可用状态。当前实施设计中，扩展由随版本编译的注册项提供。

## 跨入口验收样例

| 两端按此顺序操作 | 必须核对的服务结果 |
|---|---|
| CLI 执行 `bmem config set profile high --expected-revision 7`，Web 刷新；再从 Web 改为平衡，执行 `bmem config get profile --json` | 第一次返回 8 及 `high`，第二次为 9 及 `balanced`。从停在 7 的页面再次提交得到 409，两次生效修改均在服务重启后可读 |
| 两端选择同一来源、embed 模型及端点；中断后在另一端继续 | 只有一个来源身份。向量关闭时 embedding 请求、下载、建索引数均为零。显式启用并测试后，CLI/Web 显示相同绑定、测试结果和索引水位 |
| CLI 追加“以后报告先写结论”，Web 打开新修订；Web 只更正开头，CLI 用 history 回读该维度，再让两个真实宿主接力 | 语言维度保留。两端的声明、依据、提供与装入修订一致；宿主回执缺失时两端呈现同一缺口 |
| 一端预览遗忘，另一端变更权限后尝试确认旧预览；随后用新预览确认，同键从另一端重试 | 旧预览被拒。新预览只受理一次且返回同一操作号；从两端使用旧句柄均立即禁读，合法对照仍可查 |
| 从 Web 发起备份，CLI 下载该单据产物并预览，再从 Web 查看预览。另在 Web 发起程序更新，CLI 重连读取结果 | 同一包摘要、撤回水位、缺件与切换资格；更新单展示签名制品、实际启动版本或可验证回退结果 |
| 从管理清单枚举本版本所有所有者可执行的持久修改，逐项用 CLI→Web、Web→CLI 操作独立空库，再比对同一规范化输入 | 每项都有可操作的两种入口、同一授权拒绝和修订语义；比较领域对象、回执类型、后台作用次数与最终状态。跨独立数据根生成的随机 ID 按对象对应关系比较。同一服务同键重试应得到完全相同的领域 ID |

宿主专有的事件采集、装入回报和独立验证器提交结果按绑定主体单独验收；未提供这些能力的宿主在两端都显示实际缺口。三系统分别验证真实平台动作，首发声明与提炼分别在 Claude Code、Codex 的真实会话联调。[真实任务验收](../product/acceptance.md)
