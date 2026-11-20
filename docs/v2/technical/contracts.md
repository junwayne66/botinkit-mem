# 服务接口与宿主协议

v2 的本地 HTTP 接口供管理台和宿主适配器调用，CLI 复用同一应用命令，MCP 只注册该客户端获准使用的工具。下面定义的是待实现的契约。示例中的 ID、云杉/白桦接收器、“溪桥”项目及回执均为合成数据。P0 真实联调固定 Claude Code 与 Codex，两款宿主各自登记实际模型目的地和生命周期能力；接收器示例只用于核对本机/云端的授权差异。

## 调用约定

- 服务只监听回环接口。所有者通过本机已认证入口注册客户端，分配标识、凭据、允许能力、用途和目的地范围。CLI/宿主使用各自的绑定凭据，本地 Web 用所有者会话；浏览器写请求核对会话和同源请求。来源账号与说话者由可信适配器绑定，正文中的角色声明无法授权。进入管理台不等于某宿主的接收模型获准看见相同内容。
- 身份、路由登记和授权版本由服务解析。请求中的 `route_ref` 只能选择已登记的路径，路由能力/真实端点无法证实的处理见[权威读取顺序](authority.md)。数据型用途区分采集、在线检索、证据补读、导出、离线评测及训练，可选模型调用还需目的地授权。
- 业务写入携带 `Idempotency-Key` 或稳定原生事件 ID，作用域为调用身份与操作。相同键且规范输入一致返回同一对象/单据；异文复用返回 `409 key_conflict`。重放时按当前授权裁剪返回字段。批量来源页与游标原子提交。时间使用带时区的 ISO 8601 格式，列表采用不透明游标；权限变化后要求刷新结果集。
- `snapshot_id` 标识该接收域与任务适用的声明版本，`package_id` 标识可选任务证据包，`delivery_id` 标识本次实际提供的上下文。这三个 ID 不可互换。缺证据、仅可查询、证据受限、预算不足和无内容各有可观察状态。

## 入口与结果

路径均以 `/v2` 开头。下表覆盖核心领域协议。平台诊断、启停和升级、模型下载、连接器测试/授权、作业重试与归档传输等管理接口的路径、参数及 `command_id` 见[命令目录](command-catalog.md)。CLI 和 Web 对同一动作使用同一处理函数；受信宿主与验证器有单独的协议身份。能力发现通过 `GET /v2/capabilities` 和 `GET /v2/capabilities/{command_id}` 查询。[扩展接口](extension-contract.md)

| 调用者和操作 | HTTP | 主要输入 → 结果 |
|---|---|---|
| 所有者通过 CLI 或 Web 读取／更改配置 | `GET /v2/config`、`PATCH /v2/config` | 本次修改字段和 `expected_revision` → 新配置修订、当前档位和各能力状态。支持 `profile=data/balanced/high`、`vector_search.enabled`、模型端点及用途绑定。状态只回显凭据是否可用。配置冲突返回 409；向量关闭时跳过模型供应和建索引。[初始化与配置语义](cli-and-platform.md) |
| 所有者测试模型 | `POST /v2/models/{id}/test` | 指定用途 `extract` 或 `embed`，以合成输入核验该用途的协议、模型和输出。分别返回探测结果、模型标识和时间；真实证据仍受来源与接收端许可限制 |
| 所有者登记凭据 | `POST /v2/credentials` | 经本机会话接收掩码表单或标准输入中的密钥，写入系统凭据库的新版本，仅返回引用及状态。CLI 和 Web 以同一引用提交设置变更；无凭据库时可直接配置服务环境变量名。设置服务仅保存引用 |
| 所有者登记来源与宿主 | `POST /v2/sources`；`POST /v2/clients` | 来源类型/账号、资料类别、回填区间（`categories`、`backfill_interval`）及可感知的变更类型；宿主类型、能力和可能接收路径 → `source_id`、`client_id`、已登记 `route_ref`。首次绑定私下交付客户端凭据，列表和设计样例只含标识 |
| 已获准宿主建立工作上下文 | `POST /v2/tasks`；`POST /v2/tasks/{task_id}/attempts` | 项目及稳定任务/工件锚点 → `task_id`；会话 ID、epoch、该客户端与路由 → `attempt_id`。接力复用任务 ID，换宿主新建尝试，重投原创建键复用原 ID |
| 受信适配器提交来源页 | `POST /v2/events` | 来源版本、说话者/消息类别、原生顺序、变更类型、定位、游标 → 每项接收/重复/已撤回屏蔽、已提交游标。普通采集权限只能提交普通来源。普通对话及工具回执提交后触发 P0 提炼，可用回执中的事件和待办 ID 追踪任务 |
| 所有者或受信宿主追加声明 | `POST /v2/statements` | 原话、本人消息引用（本机所有者入口可在本次命令创建）、适用范围、分享意图及可选纠错引用 `corrects_attempt_id`、`corrects_revision_id` → `statement_id`、逐维度状态/新旧修订、原因 |
| 管理台查询当前声明 / 历史 | `GET /v2/statements`；`GET /v2/facts/{fact_id}/history` | 项目/任务、`valid_at`、`known_at`（默认现在） → 适用组合、修订链、授权可见的理由/出处、未决部分。UI 按维度展开 |
| 宿主装入 | `POST /v2/context/load` | 会话、epoch、任务/尝试、接收路由、已装入版本、两个预算 → 当前声明快照、任务包、装入指示及本次投递 ID |
| 查找、补读 | `POST /v2/search`；`POST /v2/evidence/expand` | 查询、任务锚点、时间/预算/路由（P1 可选关系类型、跳数或主题模式）→ 授权修订/定位、相关性与证据是否充分；句柄、范围预算和目的地 → 可见邻句、出处与截断标记。候选为空返回空列表 |
| 管理台接手任务 | `GET /v2/tasks`；`GET /v2/tasks/{task_id}/handoff` | 线索或选定任务 → 授权候选，或目标/动作/已验证结果/未解事项与各项句柄；同仓任务未区分时返回候选与可辨认的工件锚点 |
| 宿主回报装入或采用 | `POST /v2/deliveries/{delivery_id}/receipts` | 实际 epoch、快照/包版本、观测类型、模型调用引用或采用依据 → 已接受观测及可核验阶段 |
| 独立验证器 / 适配器记录结果 | `POST /v2/attempts/{attempt_id}/outcomes` | 结果状态、验证器、可追溯工件版本及结果证据；可选相关投递 ID → 尝试结果及逐项归因状态 |
| 所有者查看装入 | `GET /v2/clients`；`GET /v2/deliveries` | 按 `client_id`、`session_id`、`delivery_id` 或 `attempt_id` 查询 → 能力、接收路由与各条候选/提供/装入/采用的修订 ID、依据及缺口。以尝试查询同时给出结果、是否取消、授权可见的工件版本及验证观察 |
| 所有者管理来源与授权 | `GET /v2/sources`；`GET /v2/grants`；`POST /v2/operations` | 来源账号、连接/登录授权状态、回填/编辑/撤回能力、最后成功覆盖和已提交游标、最近受理单；带预期授权版本的创建/改变/收窄命令 → 新授权版本及单据 |
| 所有者预览治理影响 | `POST /v2/operations/preview` | 新增/调整授权、遗忘或撤回训练用途的命令参数 → `preview_id`、目标与授权版本，以及各接收端新增可见、将禁读或保留的字段范围、受管位置、已送出和不可核验位置。只读，不入清理队列 |
| 所有者执行治理 | `POST /v2/operations`；`GET /v2/operations`；`GET /v2/operations/{operation_id}` | 列表可按来源、动作种类、进度查单（含上游撤回自动产生的 `operation_id`）。见下文的命令参数 → 立即生效水位、范围、待清理位置/重试入口、已送出和不可核验位置 |
| 运行与离线学习 | `GET /v2/health`；`GET /v2/jobs/{job_id}`；`GET /v2/evaluations`；`GET /v2/releases` | 能力、队列和索引水位，P0 可由待办 ID 查输入版本、模型/模板、候选数、裁决及修订或无结果/失败原因；P1/P2 对照、工件/数据集谱系、冻结报告、正在启用的版本。发布或回滚经操作命令完成 |

语义映射示例：管理台「追加声明」和 CLI `bmem declare` 均提交 `statements`；MCP 的 `search`、`expand` 和宿主生命周期入口 `context/load` 分别对应显式查询、原文补读和自动加载。各 API 返回的数据是否展示或注入，均受实际接收域限制。

## 一次跨助手调用

**0. 从空库建立示例身份。** 本机所有者首次运行 CLI 从当前系统用户的受控数据目录取得所有者会话，并在本机管理台或 CLI 建立两个 `clients` 和一个会话 `sources`。以 `POST /v2/operations` 提交 `kind=change_grant`，创建采集、本机原文和共享声明权限，其中白桦的云端路径只获共享偏好字段。云杉在 `tasks` 以项目“溪桥”和“导入超时”的稳定工件锚点换取 `t_creek_import`，在 `events` 上报已授权的真实用户消息与验证回执；白桦接手时在同一任务下创建 `a_8`。示例 ID 代表这些创建调用的结果；宿主自有的会话 ID、上下文 epoch 与消息顺序由其适配器提供。初始“所有助手的报告用中文，先列行动项”也通过下述声明接口写入。

**1. 所有者追加声明。** 已有“报告用中文，先列行动项”。用户通过本机管理台改口时使用所有者会话，服务为原话建立来源；通过云杉改口时附带已认证本人消息的引用。下例展示后者。来源原句留在本机，已授权可共享的维度随作用域交付。`POST /v2/statements` 的请求体如下。

```json
{
  "message_ref": "src_spruce/msg_42@1",
  "original": "以后报告先写结论",
  "scope": {"kind": "global"},
  "share": "authorized_assistants"
}
```

成功响应为 `201`。新值与前版成对返回。原话来自无权提交声明的渠道时，`403` 拒绝该命令；从普通来源采集的相同文本仍按该来源资格处理。

```json
{
  "statement_id": "st_2",
  "dimensions": [
    {"fact_id": "f_72", "key": "report.opening", "status": "active", "revision_id": "r_open2", "supersedes": "r_open1"}
  ],
  "change_seq": 42
}
```

这里的 `f_72` 是权威服务在本例中签发的不透明事实 ID，供 `GET /v2/facts/f_72/history` 使用。`report.opening` 是可读的维度键，须与主体和范围一起解释。当前声明列表与本次追加回执对已识别的事实返回同一 ID。验收时分别提交全局、项目内两个开头偏好，两者可共享维度键；分别按返回 ID 查看历史时只能出现相应事实的修订。

状态枚举为 `active` 已生效、`scheduled` 待生效、`pending` 待归并、`disputed` 有争议、`historical` 仅历史。一个声明的维度可处于不同状态；“已记录”是原话的收录回执。状态清单支持指出取代关系和下一步自动处理原因。

**交付表示。** 任务包记录 `requested_profile`、`effective_profile`、模板版本及必要时的回退原因；逐项绑定修订、原文句柄和实际目的地。切档不改变事实修订号。宿主无法主动补读、当前模型/任务缺少质量验证或提问要求逐字核对时，服务在出站前展开必要材料，并按保真的有效档位记录。预算不足以容纳必要证据时返回缺失原因并申请扩容或作出有界回答。服务记录初次提供的估算 token，宿主有计量时再关联初次、补读和重试的实际 token。详见[三种表示与验收](compression-and-performance.md)。

**2. 白桦开始下一次任务。** 白桦的已登记云模型获准接收共享偏好，不能读取上述原会话。`POST /v2/context/load` 请求如下。首次接入、新会话、压缩重建和断线重连时主动加载；压缩后更换 `context_epoch` 并清空已装入版本。运行中的会话在下一次可控模型调用前检查变化。

```json
{
  "session_id": "birch_session_7", "context_epoch": "epoch_2",
  "task_id": "t_creek_import", "attempt_id": "a_8",
  "route_ref": "birch_cloud", "known_snapshot_id": null,
  "budget": {"statement_tokens": 160, "evidence_tokens": 0}
}
```

下例预算仅示意接口单位。此处任务证据预算为零，用户可以核对云端白桦实际获准收到的字段。

```json
{
  "delivery_id": "d_8", "delivery_state": "provided", "policy_version": 9,
  "snapshot_id": "s_birch_2", "snapshot_mode": "replace",
  "statements": [
    {"revision_id": "r_lang1", "key": "report.language", "value": "zh"},
    {"revision_id": "r_open2", "key": "report.opening", "value": "conclusion"}
  ],
  "package_id": "p_8", "evidence": [], "package_state": "budget_zero",
  "requested_profile": "balanced", "effective_profile": null,
  "template_version": null
}
```

本例只投递声明。任务证据预算为零，未渲染任务证据，故有效档位和模板版本均为空。

同一 epoch 且宿主已装入版本完全匹配时返 `snapshot_mode=unchanged`，宿主保留已有声明段。路由、授权、项目、内容或生效时点改变而使适用集合变化时，返回完整替换；授权缩小后新快照可以为空。变更和历史报告关联到此前实际提供的条目。仅能追加文本的宿主使用替代标记并记载旧文本仍可能留在历史上下文。此次临时任务要求可覆盖已加载的默认偏好。

声明预留预算不足时返回 `snapshot_mode=deferred`、所需预算和 `reason=statement_budget`，记录待送达。宿主可扩大预算重新取，降级期间告知本次任务声明未完整装入。离线、仅搜索模式和无回执分别显示「离线／待送达」「仅可查询」及「已提供／装入未知」。异地保留的旧包不得作为当前权限的新授权。

重载推迟或授权已缩窄时，能替换上下文的适配器先移除失效项。只能追加且无法从下一次模型输入剔除已撤权内容的宿主要在干净上下文重建后再投递，显示旧会话受限；已经发出的内容列入原投递记录。

**3. 装入与结果分别回报。** 白桦在实际构造模型输入后向 `POST /v2/deliveries/d_8/receipts` 发送第一段。仅有服务端选择和响应写出时保留 `provided`。若宿主能定位输出/行动如何采用某条记录，可再报 `used`、`revision_ids` 和相应依据。管理台独立展示各环节。若只装入部分条目，回执列实际 `revision_ids` 和缺失原因；只有本次应装入的全部声明均匹配才更新“完整快照已装入”的计数。任务取消独立记录在尝试终态，任务结果没有独立回执时仍为未知。

```json
{
  "event_id": "ack_b8", "kind": "loaded", "context_epoch": "epoch_2",
  "snapshot_id": "s_birch_2", "package_id": "p_8",
  "revision_ids": ["r_lang1", "r_open2"], "model_call_ref": "call_8",
  "route_ref": "birch_cloud"
}
```

随后可对 `POST /v2/attempts/a_8/outcomes` 提交第二段。任务的 `passed/failed/unknown` 独立于是否有逐项采用证据。宿主自报、查询点击或沉默只计观察；声明是否遵循可用可复核任务断言另测。

```json
{
  "event_id": "result_a8", "status": "passed", "delivery_ids": ["d_8"],
  "validator_ref": "report-check@1", "artifact_ref": "report_8@2",
  "evidence_ref": "test_event_8@1", "attribution": "unknown"
}
```

**4. 自动提炼后用另一宿主补读。** 首轮在声明链路验收后，Claude Code 通过 `POST /v2/events` 上报普通用户话语和对应工具事件，真实获准模型生成候选，权威服务提交结果。Codex 在单独获准的任务范围通过 `tasks/{task_id}/handoff` 读取经历与证据。以实测的宿主输入、提炼器出入站模型版本及实际任务结果核对；不具备该能力的入口记下缺口。再在另一宿主输入普通事件，确认对应真实适配器也能采集原角色与回执。[提炼输入与候选](authority.md)

原文授权的合成对照如下。 获准的云杉本机请求 `tasks/t_creek_import/handoff`，得到“准备复验”（计划）、“解析通过”（有执行回执）、“入库仍超时”（未解）及源句柄。点“原文”后以 `{"handle":"h_run_2","route_ref":"spruce_local","before":1,"after":1}` 调 `evidence/expand`；服务限制窗口范围，按每个片段裁剪。白桦拿同一句柄不能读出原句；过期索引的候选也不能绕过此入口。

## 治理单据与失败语义

| 操作 kind | 必要参数 | 生效点与可观察结果 |
|---|---|---|
| `change_grant`、`stop_capture` | 目标来源/授权、预期版本、用途与接收范围（新建授权含来源和目的地） | 前者提交后新读按新版本裁定；后者停止后续采集，已有资料的保留、共享与删除分别显示 |
| `resume_capture` | 来源、已恢复的登录授权、允许资料类别和历史区间 | 从最后已提交游标续读；重授权后若平台游标已失效，从可证明的覆盖点重新对账，列明重叠与无法补回的区间 |
| `sync_source` | 已授权的来源，按需提供起始时间 | 受理为操作单。从已提交游标增量同步，历史回填受请求区间、来源能力和已授权历史范围共同约束。两端可以查相同的覆盖点和缺口 |
| `forget`、`revoke_training` | 目标对象/用途及删除范围 | 当即停止相应使用；详情分列库、派生、暂存、受管备份、历史已发送、外部不可核验。训练用途另列受影响工件 |
| `backup`、`export`、`restore_preview`、`restore_apply` | 范围、受管位置或已登记包；恢复预览可选 `target_home`（本机卷绝对路径），缺省使用当前根。应用只传通过核验的预览引用并核对绑定的实例、目标与预期现库水位 | 预览返回完整性、附件缺口、撤回截止点和开放条件；目标或水位改变要求重新预览，预览期间普通写仍只进当前根 |
| `retry`、`rebuild_index` | 原单据失败位置或投影类型、目标输入水位 | 复用原单据追踪同一目标，成功位置不重复清理；后台重建生成旁路版本 |
| `build_dataset`、`train_candidate` | 学习用途、计算环境、来源与任务族范围、截止点/种子；候选训练引用获准的数据集及基线版本 | 受理后可从单据查看清单、排除项、划分和输出工件；评测侧隔离冻结集 |
| `evaluate`、`activate`、`rollback` | 指定工件/基线、冻结评测、部署范围或已验证目标 | 授权与工件依赖仍有效才可投放，报告当前实际加载的版本和各环节结果 |

预览示例：`POST /v2/operations/preview` 接收 `{"kind":"forget","target":{"root_id":"root_42"}}`。返回 `preview_id`、`policy_version`、目标修订、当前受影响的来源/派生/投递与各位置状态，不要求显示任何无权原文。确认时 `POST /v2/operations` 携带同一命令内容和 `preview_id`；服务在提交事务中对照预览绑定的目标、权限与影响范围。预览足迹只绑定目标范围状态：授权变更取目标授权行与模型目的地，遗忘取目标修订或来源自身游标与已接收页水位，无关活动不使预览失效。范围发生改变返回 `409 revision_changed` 并重新预览，再由用户确认最新影响。上游主动发来的已认证撤回事件仍通过 `events` 直接禁读。

治理命令 `202` 回执含 `operation_id`、`effective`（已提交禁读或控制变更）、`cleanup_state` 及可查询的处理位置。备份、重建、恢复预览排队时只回报已受理，此时 `effective=false`。详情按位置列完成、待处理、失败可重试、系统外不可验证，附最近尝试时间、错误类别和该位置可执行的重试动作；`effective=true` 不推断全部已擦除。

| HTTP / code | 调用方动作 |
|---|---|
| 401 `authentication_required` / 403 `action_denied` | 重新绑定身份或使用获准入口。无权执行操作只报告动作层拒绝 |
| 404 `not_available` | 具体事实/原文/任务不存在、已经撤回或此接收者无权读取时，共用无正文响应。已认证所有者在操作单查看自己有权获知的原因 |
| 406 `unsupported_contract_version` | 客户端指定的动作契约版本不受支持。先从能力目录读取可用版本，选择支持的版本后重新调用；本次没有提交修改 |
| 409 `revision_changed` / `key_conflict` | 刷新快照或预览并重新决定；相同键仅用于重试相同意图。跨客户端/epoch 或实际版本不符的装入回执不计为已装入 |
| 422 `invalid_input` | 指明无效的公开字段和约束供调用方修正 |
| 503 `temporarily_unavailable` | 服务不可用时保留原幂等键重试；生成端点不可用单独报告能力降级，声明提交和词法查询走本地路径 |

错误体统一返回 `code`、`request_id`、`retryable` 和适用的修复动作；错误信息和诊断日志不携带输入正文。仅供调优的分数、内部表名和向量维度不属于公共契约。
