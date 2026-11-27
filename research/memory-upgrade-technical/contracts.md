# 数据与接口契约

[技术设计总览](README.md) · [七项产品定义与验收场景](../PRD-memory-upgrade.md) · [V2 六域与十八个模块](../../docs/v2/technical/README.md)

本文以当前 Rust V2 的 SQLite 和 HTTP 路由为基线。「拟增」表示本次设计建议，字段、表和请求均待实现。所有示例标识和内容为合成数据。004 的执行记录与 005 的待签契约分别见[验证记录](../../specs/004-bmem-v2/validation.md)和[签字摘要](../../specs/005-agent-mcp-interface/SIGNOFF.md)。

## 1　先统一四种标识

| 要回答的问题 | 沿用的标识及粒度 | 在升级中如何使用 |
|---|---|---|
| 资料是不是同一份？ | `ing_roots.root_id` 表示命名空间内的原生对象，`ing_event_versions.event_id` 表示该对象的一个版本 | 引用精确到 `(event_id, span_id)`。本地句柄用于授权展开。跨来源同文只形成待核线索，同源认定须有可回查引用 |
| 谁在何时说过什么？ | `statement_id` 标记原话，`fact_id` 标记维度和项目，`revision_id` 标记该次修订 | 工作约定引用 revision，经历和回执引用证据片段。每次追加保存来由，索引和模型只能提交候选 |
| 哪一次工作、投递或装入？ | `context_tasks.id → context_attempts.id → context_deliveries.id`。回执键是 `(client_id,event_id)` | 每次投递已有 `snapshot_id`（声明）、`package_id`（任务证据）。宿主另报 `model_call_ref` 将装入或采用绑定到一次调用 |
| 这次历史导入读了什么？ | `history_task_jobs.task_id` 是导入作业，`history_task_items` 是作业内的目标，`ing_ingests.ingest_id` 是采集页 | 对外结果称 `import_job_id`，与上述工作任务分开。提炼作业 `ing_jobs.job_id` 绑定一个事件版本。一次导入可关联许多版本或空页 |

实现入口见 [authority.rs](../../src/v2/bmem/src/authority.rs)、[ingestion.rs](../../src/v2/bmem/src/ingestion.rs)、[history_tasks.rs](../../src/v2/bmem/src/history_tasks.rs)、[context.rs](../../src/v2/bmem/src/context.rs) 和 [server.rs](../../src/v2/bmem/src/server.rs)。

| PRD 特性 | 谁写权威状态 | 谁装配或查询结果 |
|---|---|---|
| 1 约定试算 | M05 分句与修订，M06 约束分享 | M12 向会话提供当前值，M13 记录可观测回执 |
| 2 接手续办 | M01/M03 记录宿主与工作任务，M04/M05 保管证据与裁定 | M11 补读，M12 出包，M13 将装入和结果关联到本次尝试 |
| 3 导入去向 | M02/M03 记录导入页与身份，M07/M08 提议和归并，M05 裁定 | M16 所有者按导入作业查去向，M17 展示后台积压 |
| 4 通用取证 | M04/M06 保证原文版本与可见范围 | M10 各路投影，M11 融合与展开，M12 应用接手预算 |
| 5 权限试算 | M16 受理，M06 提交可见性变更及根撤回 | M11/M12 按接收端试算。M18 对照恢复清单 |
| 6 带反例方法 | M09 归纳，M05/M06 裁定版本与依赖 | M11 按当前工件条件取证 |
| 7 可回放纠错 | M13 反馈，M05 处理明确声明。M14 持有用例，M15 持有候选工件 | M14 运行配对评测，M17 发布或回退 |

## 2　特性 1 · 一句话里的默认、项目例外和单次要求

| 环节 | 004 已有 | 拟增契约 |
|---|---|---|
| 接收 | 所有者 `POST /v2/statements`，原话、scope、share、可选 valid_from。`(actor,key)` 幂等。确定性解析 `report.language`、`report.opening` | 增 `code_example.language`。先按明确分句识别维度、作用域及时间，再一次提交原话、修订、未决原因及回执。一句部分可判定时，已判定分句生效，其余原话在所有者详情中可查 |
| 存储 | `authority_statements` 的 project/effective/known 属于整段原话。现有 fact 已以 `(dimension,project)` 唯一 | 拟增 `authority_revision_terms`，主键 `revision_id`，存分句位置、`valid_from / valid_until / timezone`、可选 `task_id`。项目复用对应 fact 的 project。一次性条款须绑定具体工作任务，只有该任务仍打开且 ID 匹配才参与本次查询。同一维度的长期修订链与该任务的临时覆盖分别取当前值。原话、分享与 known 仍取所在 statement |
| 试算 | 所有者 `GET /v2/statements?project=…&valid_at=…&known_at=…`，已有事实历史入口 | 可选 `explain=true` 和显式任务。先按 known、范围、有效窗挑出适用修订，再按「当前任务 > 当前项目 > 全局」选值，同级按有效起点、序号排序。同一次原话在相同作用域、相同起点给出互斥值时保留未决原因。返回所选 revision、分句、窗口和选择理由 |
| 到期、更正、遗忘 | 既有 current 会压制撤回、不可分享或无确定值的现行项。追加支持 `corrects_revision_id` | 临时覆盖到期后重选仍有效的下层默认。永久替换后再撤回，保留既有遮蔽语义，旧值不能借“找上一条”重新出现。任务单次条款随任务关闭退出（拟增任务关闭时间），恢复任务前须显式重申。查询历史也须通过**当前**读取与保留授权 |
| 时间及输出 | 服务记录 known。公开声明按客户端可见范围投递 | RFC 3339 持久化 UTC，原文相对日期以提交日期及声明时区解析，采用 `[from, until)`。时区来自显式参数或已配置项目时区，缺失且结果依赖时区则待确认。所有者详情可看原话，助手只收当前获准值。是否在某次实际装入另查投递回执 |

例如，2026-11-23 在 `Asia/Shanghai` 追加「以后示例用 Rust，青禾到这周五结束用 Python」。全局与青禾各建一个修订。青禾的 `valid_until` 是 `2026-11-27T16:00:00Z`。周六零时起返回全局 Rust。用追加前的 `known_at` 试算，两个新修订都不可见。旧数据无 terms 行时继续按 statement.effective 起算，结束时间为空。[现有权威查询及修订](../../src/v2/bmem/src/authority.rs)

## 3　特性 2 · 换助手交接与实际装入证明

| 请求或事件 | 现状 | 拟增字段和判定 |
|---|---|---|
| `POST /v2/context/load` | 服务绑定 client、route、session、epoch、task、attempt。声明预算与证据预算独立。`snapshot_mode=replace/unchanged/deferred` | 请求可带 `retained_delivery_id` 和宿主当前实际保留清单的摘要 `retained_manifest_digest`。由**识别并能核对实际模型输入**的宿主适配器启用。响应新增 `package_mode=full/delta`、`base_delivery_id`、本次要求的证据引用及完整目标清单摘要 |
| `POST /v2/deliveries/{id}/receipts` | `kind=loaded/used`，`revision_ids`、`model_call_ref`、可选 `evidence_ref`。现有 `complete` 只表示声明装入完备，整包 fingerprint/handles 记录服务提供 | 增 `loaded_evidence_refs`（实际输入中的版本及片段）和独立的 `evidence_complete`。与该投递目标清单、调用、路由逐项核验，部分装入可带原因。在现有 delivery evidence 表扩展目标清单和摘要，回执沿用 context_receipts.result 存实测引用，现有幂等摘要纳入新字段。旧行缺数据时返回 unknown。允许在声明 `unchanged` 时**单独**观察证据装入。沿用旧声明回执逻辑 |
| 同上下文二次交接 | 已能凭声明回执省去同一快照中的声明，证据目前仍送整包 | 服务核对同一 client、route、session、epoch、task 的完整回执、保留清单与当前授权。仅在保留项仍可用、宿主支持差量时省略重复片段。比较不可变的版本、片段、呈现版本及观察值。删改项超出宿主可替换能力时重建上下文。无证明即送全包 |
| 换宿主、压缩、断线 | 已记录投递和尝试，某些宿主只能追加。目前过期已投递内容可触发 `fresh_session_required` | 上次可核验装入的历史位置仅用于标亮变化。新上下文必须送当前必需证据，有预算才补其余时间线。无基线显示「上次位置未知」。必要片段装不下则标记缺口、预算及实际交付项。撤权后仅追加会话沿用重建要求 |
| 计划、验证与采用 | `authority_observations` 保留片段级角色，`context_outcomes` 要求独立 verifier | 交接项分决定、计划、检查结果及阻塞。只接受匹配任务和同次运行、来自真实工具结果的通过/失败。实际采用须由输出或动作证据与同次已装入引用对应，无法观察则记未知。独立结果只归到 attempt |

工具适配器可在 `tool_result.checks` 的一个命名检查项内发送 `{"kind":"artifact_check_v1","status":"timeout","marker":"Q7R4"}`。标记固定为四位大写 ASCII 字母和数字，二者均须出现。递归凭据脱敏照常处理原文和对象。结构无效时拒绝该页，旧式纯文本继续按原脱敏规则处理。

同次实际使用可上报 `kind=used`、`used_evidence_refs`、`artifact_check_ref={handle,field}` 和实际工件的 `evidence_ref`。服务用投递清单与同一调用的完整装入回执核对证据，重新检查当前读权，再核对被引用的检查项。使用回执保存证据位置及工件引用，任务结果由独立验证器提交。

以下为证据扩展的字段摘录，原有身份、预算和声明回执字段按原端点照常提供。

~~~json
{
  "retained_delivery_id": "d_11",
  "retained_manifest_digest": "sha256:<已装入且当前仍保留的清单摘要>"
}
~~~

| 当次调用 | `package_mode` | 送到实际输入的最低内容 |
|---|---|---|
| Codex 新开会话，仅知道昨天曾投递 d_11 | full | 已通过的解析检查、写入超时的回执、最新重试仍失败及适用声明 |
| 同上下文已证明保有前两项，本轮增加一条重试结果 | delta | 变化项与对应依据。记录重建后的目标清单，下一次对该完整清单出具回执 |
| 存在同仓另一任务的“重试成功” | 仍按上述两条路径 | 排除另一任务的成功记录。本任务只有计划时写「执行结果未知」 |

## 4　特性 3、4 · 导入的账目与按原文取证

| 粒度 | 已有记录 | 拟增查询和写入 |
|---|---|---|
| 作业 → 目标 → 采集页 | `history_task_items` 有目标、游标和累计计数。采集回执的 `events[]` 已含序号、native_object、event_id、disposition | 只补关联表 `history_job_pages(import_job_id,target,page_seq,ingest_id)`，作业、目标、页号唯一。同一目标的已关联 ingest_id 重试复用。与 `accept_in/accept_history_task`、目标 checkpoint 同一事务。结果按关联展开原有页回执。空目标按既有完成/失败状态显示，旧作业缺关联则明确「仅历史汇总」 |
| 版本 → 提炼 | 每个 active 新版本创建唯一 `ing_jobs`，工作状态有 pending/running/done/failed/cancelled，结果含 `candidates_recorded`、`observations_verified` | 拟增所有者 `GET /v2/history/jobs/{id}/results`，以目标序号、页号和原页条目序号分页，首屏固定已提交映射水位，刷新后再看新页。采集结果、当前提炼资格、执行状态、产出数量分列。等待和未授权可同时成立。完成且 0 条是可解释的独立结果。源已离线时说明可读取的本地版本 |
| 候选 → 裁定 | `ing_candidates` 存候选和 support。权威观察主键为版本＋片段，目前有作业级核验数量 | 在现有 `finish` 事务写候选裁定与观察引用，区分采纳、合并、待证和不采纳，保留理由。按不同 event 去重计版本，按 candidate 计提议，按唯一观察计核验。两个候选可合并成一个观察 |
| 重导、转述和独立回执 | 同一 `(root_id,version_key)` 复用 event_id。撤回根有墓碑 | 重导在本次作业显示 duplicate，库中版本和提炼任务数均不增加。显式稳定引用能证实转述时登记出处边。缺少版本或授权依据显示「来源关系未确定」，不能当作独立佐证。独立运行产生的工具回执另计 |
| 查找与补读 | 所有者 `POST /v2/search` 可选 lexical/vector。绑定客户端走 shared 声明搜索。任务交接另有 `retrieval::for_client` 与授权展开 | 为所有者搜索拟增 `mode=hybrid`，任务包在原有任务作用域内调用同一候选融合函数。精确标识、FTS5、可用向量各自按**本读者、用途、接收端和任务**限定的候选域召回，再按事件/片段合并，保留每路排名、命中原句和版本。预算或向量不可用时显式记录退化 |
| 邻近窗口 | `recall_events` 已按 task/time 索引，通用相邻时间不能证明同次运行 | 采集时有原生 run/tool_call ID、配对结果或同一执行段位置才展开对应回执。只有错误码 E417 和邻近计划时，返回计划与缺口。定向补读再次核权限与剩余阅读预算，确保可见结果有独立名额 |

结果分页的四组字段为 `capture.disposition`、`extract.eligibility/state`、`candidate_counts`、`decisions[]`。旧行的裁定原因或当时授权未留痕时返回 unknown。今天有权限只能说明今天可读。搜索范围和权限边界见 [retrieval.rs](../../src/v2/bmem/src/retrieval.rs)、[向量投影与队列](../../src/v2/bmem/src/vector.rs)和[两种搜索身份的路由](../../src/v2/bmem/src/server.rs)。

## 5　特性 5、6 · 试算授权、撤回依赖和归纳方法

所有者在已有 `POST /v2/operations/preview` 上可选传 `comparison={query,task_id,reader_ids:[A,B]}`。拟在同一 SQLite 读快照执行四次选择（A/B × 变更前/假设变更后），固定候选、策略版本与预算，生成所有者有权查看的差异。将选择逻辑从投递中提取为只读调用。试算不创建 delivery、receipt 或模型调用。既有治理预览会登记凭单，确认仍携带 `preview_id` 并校验命令及作用范围指纹。比较选项与搜索结果不得写入原有治理命令正文。显示与首次预演绑定的快照。确认遇到影响范围变化时重新预览。[已有治理预览和确认](../../src/v2/bmem/src/retrieval.rs)

| 操作 | 权威提交和立刻读到的结果 | 后续状态 |
|---|---|---|
| 所有者要求少出现 | 拟增经历的默认交接可见性。默认任务包过滤该经历，获准定向搜索仍可读 | 可显式恢复，频率只影响排序。声明生效另按 §2 |
| 收窄某个接收端 | 现有 `change_grant` 的 `task_delivery` 粒度是 source/client。若目标只是一段讨论，拟增按 client、purpose 和 root 的拒读项，覆盖该来源级允许。若选中讨论由多个原生对象组成，预览列明本次选中的根集合，确认按该集合执行。按接收端重验新查询、旧句柄、投递及回执。同一来源的其他对象和独立获准助手可继续读 | 重验已准备投递的包。已真正发出的副本标记「已发」，可重建宿主重载。只向所有者展示受限差异 |
| 遗忘根来源 | 现有所有者 `forget` 的 target 是 statement 或整项 source。拟增 `target.root_id` 并复用已有根墓碑与撤回逻辑，原文及失去必要依据的派生结果即刻不可读。撤回优先于较晚到达的导入页或 worker 完成 | 由现有清理/后台处理清除受管原文、投影和派生副本，记录分位置进度。恢复预览对照撤回清单。实际恢复仍按 F07 单独验收 |
| 依赖重算 | 拟增按「派生版本、支持组、输入版本/片段、角色」登记的边，建输入到派生的反向索引。组内 **AND** 表示全部前提，组间 **OR** 表示各组可独立支持同一结论。对本接收端先验根与用途，再验组。链式派生逐层验 | 旧文本只要含已受限来源的细节即先屏蔽。其余完整支持组可重算仅据获准材料的新版本。无完整支持组则暂停。缺失出处留 unknown，不能凑够通过用例的独立案例数 |
| 提炼可复用方法 | 拟增方法版本，记录工件、适用版本条件、前提、步骤、验证回执、反例。D3 提案，D2 裁定写入。逐项绑定上行支持组。v1 两次独立通过、v2 一次失败属于不同案例，复制总结沿同源边回到原案例 | 查询 v2 优先返回失败条件。未知版本返回待验证。反例与支持证据分别保留，丢失唯一前提时暂停默认交接并重算。重新获得依据产生新方法版本 |

## 6　特性 7 · 将一次纠错变成可回放的独立用例

| 步骤 | 数据与身份 | 失败或撤权结果 |
|---|---|---|
| 纠正当前使用 | 所有者针对错误投递/尝试写反馈。确需改声明时沿 `POST /v2/statements` 的 `corrects_revision_id / corrects_attempt_id` 追加 | 纠错即时生效。未授予评测用途时，到此为止 |
| 构造用例 | 拟增 `eval_cases`，一次尝试、最小查询、应出现/禁止的证据引用、无答案预期、判定规则、根来源族、用途授权。查询本身同样受授权与遗忘约束。复用合成模板或获准引用 | 建集与运行前都校验 evaluation 用途。同根转述归同一组。撤权使相关用例及受影响数据集版本停用。training 需另行授权 |
| 配对运行与回退 | 固定宿主、版本、模型、任务、工具、预算和根/时间留出。每个策略版本保存输入清单、工件哈希、新增成功、退化和成本。按任务检查必需原句、禁止项、装入与独立结果 | 只切换已评测的策略指针。回退不还原事实库，用户纠正与撤回保留。候选工件若使用已撤训练数据，停止启用并标示待重训/核验 |

## 7　兼容与验收边界

| 切换点 | 兼容及验收 |
|---|---|
| 读取旧库 | 新增可空记录和关系，旧 report 维度、无截止期、只含声明的 receipt 及仅有汇总的旧导入作业均有明确解释。只回填能由旧数据证明的关联，旧证据装入默认为 unknown |
| 发布新写入语义 | 先上线兼容读端，再启用复合作用域和新回执写入。会误读新语义的旧服务不可对同库继续写或提供装入。复用项目迁移守卫，版本号与待签 005/T046 统一分配。回滚读写策略时保留全部权威提交与墓碑 |
| 页导入、提炼并发撤权 | 页接受、导入关联、checkpoint 在同一事务。提炼完成时在写事务内重新检查获准目的地、租约、输入版本与根，候选、裁定和 job 完成一起提交。撤权在先则结果不可交付。撤权在后须从下一请求遮蔽 |
| 两个宿主的真实行为 | 一次按计划发出和一次工具验证分别写入，跨项目同码作反例。新会话首包、压缩重建、同 epoch 二次交接各核对最终输入。声明回执、证据装入、已采用、独立任务结果分别出数。无观察能力时报告未知 |
| 对外能力 | 先走 004 已有 owner/绑定客户端入口。拟议的 Agent MCP 能力位、命令矩阵、request/confirm 边界以获签 005 为准。新本地查询可先交付所有者 CLI/Web。绑定客户端只通过获准的宿主操作取得投影 |

[004 执行记录](../../specs/004-bmem-v2/validation.md) · [005 契约与当前实现差距](../../specs/005-agent-mcp-interface/spec.md) · [V2 原产品验收场景](../../docs/v2/product/acceptance.md)
