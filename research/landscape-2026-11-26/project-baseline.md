# Botinkit Memory 已经能记住什么

2026-11-26 核对。当前最值得验证的是两处转换。第一，来自会话的候选能否成为有出处、有适用范围的稳定事实。第二，搜到的记录能否在限定预算内给下一个助手交付充分证据。V2 已有来源版本、词法和向量索引、撤回及投递回执，可以直接观测这两处转换。

**证据口径。** 下表依据本地 Rust V2 工作树，基准提交为 `12ba926`，包含当天未提交的修改。动态检查使用仓库已有的 release 二进制，SHA-256 为 `d0e36329b8de767013e0dd3a58c220df67e1d634aa04d9efb6689509226f02b1`。本次未从源码重新构建。11 月 19 日旧研究的 10 项文件指纹本次逐项核对相符，旧实验的实现与分母见后文。[原研究冻结清单](../../docs/v2/research.sha256)

## 六个环节的当前边界

| 环节 | 当前工作树可定位的行为 | 仍待回答的问题 |
|---|---|---|
| 采集 | 自动扫描覆盖本机 Codex、Claude Code、DSH 与飞书来源。一次性历史任务支持选会话，或飞书私聊、群聊、文档。事件按来源命名空间和原生对象归根，按版本去重。采集页、任务与游标在事务中提交，采集与提炼可分别开关。见 `compatible`、`accept_page`、`plan_event`。[历史任务](../../src/v2/bmem/src/history_tasks.rs) · [事件入口](../../src/v2/bmem/src/ingestion.rs) | 事件去重解决同一个原生对象反复抵达。同一说法被两位助手转述、跨来源复制后，是否仍能辨认共同的原始依据，需用同根转述与独立佐证的配对样本衡量。 |
| 提炼 | 单个事件版本形成证据窗，正文或每个检查字段各有片段 ID。模型提出候选，`finish` 核对所引片段存在、来源授权和模型路由。随后 `adjudicate` 从被引用的原字段写入工具检查或显式计划。单独的用户声明入口由 `dimensions` 识别报告语言、报告开头两个维度。[输入与候选](../../src/v2/bmem/src/ingestion.rs) · [模型适配](../../src/v2/bmem/src/providers.rs) · [裁定与声明](../../src/v2/bmem/src/authority.rs) | 片段 ID 可以定位，仍须判定内容是否支持结论，特别是条件句、否定、意向与已完成。广义偏好及流程经验有多少能正确生效，需要按维度量。下节两句合成声明展示了差异。 |
| 存储 | SQLite 分开保存根事件、事件版本、提炼任务与候选，以及声明、事实和修订。`authority::current` 同时按生效时间、知晓时间筛选，并考虑全局或项目范围。词法和向量走各自投影。控制台图的边来自记录与来源、项目、声明维度的隶属关系。[声明与版本](../../src/v2/bmem/src/authority.rs) · [索引](../../src/v2/bmem/src/retrieval.rs) · [向量](../../src/v2/bmem/src/vector.rs) · [控制台图](../../src/v2/bmem/src/analytics.rs) | 如要采用实体关系图，应先指定现有项目/任务过滤和两种时间查询仍无法回答的真实问题，测是否需要跨来源消歧或多跳取证。 |
| 淘汰与撤回 | 适用值变更由新修订接替，旧修订可查。主动撤回声明清空原文及值。根事件撤回留下标记，清空活动事件正文、检索与向量投影、候选和观察，取消提炼任务。`archive::preview` 校验备份与当前撤回状态，API 仅支持恢复预览。[声明撤回](../../src/v2/bmem/src/authority.rs) · [事件撤回](../../src/v2/bmem/src/ingestion.rs) · [备份校验](../../src/v2/bmem/src/archive.rs) | 长期未访问、被更新、用户要求删除对应三种效果。当前路径可检验后两类，未见按热度自动降权的策略。现有检查的范围是逻辑可见性与备份预览，磁盘清除、外发副本或全新实例实际恢复须另测。 |
| 检索与交付 | 所有者词法路径以 FTS5 查询，中文拆单字和相邻双字，查询词采用 AND。显式向量模式按授权投影计算余弦相似度，返回最多 20 条，配置不可用或超时可降级词法并附原因。`owner_search` 每次选择一条成功路线。接入助手走共享声明检索或经任务和来源双重授权的交付，证据按预算组包。[检索路由](../../src/v2/bmem/src/server.rs) · [词法、授权、展开](../../src/v2/bmem/src/retrieval.rs) · [向量排序](../../src/v2/bmem/src/vector.rs) · [交付预算](../../src/v2/bmem/src/context.rs) | 所有者搜索中命中一条证据，并不代表它已进入绑定助手的上下文。融合排序、邻近证据补读、相关性重排应分别与当前入口对照，固定交付预算并检查最终证据。 |
| 长期优化 | `context::receipt` 分开记录 loaded 与 used。used 需要同次模型调用对应的 loaded 记录和证据引用。`outcome` 要求与执行者独立的核验者，记录任务结果，将记忆归因保留为 unknown。技术设计把冻结集、离线训练和策略发布列在 P2。[反馈入口](../../src/v2/bmem/src/context.rs) · [分期设计](../../docs/v2/technical/domains/feedback.md) | 可以沿轨迹定位“已提供但未观察装入”“已装入但使用未知”。要估计记忆带来的任务增益，还要加入关闭记忆、直接提供金标证据等同任务对照，并保留没有历史可用的任务。 |

## 本次独立复查

在临时空库、合成来源与合成宿主上，通过公共 CLI/API 运行以下六项现有集成用例。**6/6 通过，1.047 秒。** 它们覆盖页幂等、分维度修订、搜索排除与词法降级、送达回执与撤权、迟到旧版本受阻、撤回后旧快照预览受阻。本组没有调用真实模型，也没有估计任务成功率或线上发生率。[用例与隔离设置](../../tests/v2/test_integration.py)

```sh
BMEM_V2_TEST_BIN="${BMEM_REPLAY_BASE_BIN:?请指定已核对摘要的 0.2.0 二进制}" \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests/v2 python3 -m unittest -v \
  test_integration.BmemIntegration.test_source_page_and_outbox_commit_once_without_a_model \
  test_integration.BmemIntegration.test_statement_correction_preserves_other_facts_and_isolates_scope \
  test_integration.BmemIntegration.test_search_exclusions_before_limit_and_during_fallback \
  test_integration.BmemIntegration.test_synthetic_bound_host_receipts_and_revocation \
  test_integration.BmemIntegration.test_source_revocation_blocks_an_unseen_older_version \
  test_integration.BmemIntegration.test_backup_preview_refuses_pre_forget_state
```

另起一个临时空库，依次执行 `bmem --json declare '<合成声明>' --scope global`，最后执行 `bmem --json statement list`。相同入口可以将原话登记为声明，但只有识别出的维度出现在当前有效列表。

| 本次输入 | CLI 回执 | 两次写入后的有效维度 |
|---|---|---|
| 全局报告用中文。 | recorded，得到 `report.language` | `report.language` |
| 今后所有代码示例使用 Rust。 | pending，维度列表为空 | `report.language` |

这两个观察可复查“声明已收到”和“偏好已生效”的区别。它们只覆盖显式声明入口。模型从普通会话中自动提炼、跨会话合并多个证据以及真实宿主遵循程度均需单独评测。

## 11 月 19 日的回放能说明什么

旧研究在 `ae2c06b` 的 Python 工作树上，用 LongMemEval-S cleaned 的 500 题测英文来源定位，排除了 30 道拒答题。原始会话正文配 BM25 在 470 道可回答问题上有 **392 道**找全前五个会话所需的全部金标来源。进一步限定到金标会话都有显式消息标注的 438 题，按消息打分、会话去重后，**369/438** 找全金标会话，只有 **203/438** 的每个金标会话都送出了带标注的那条首选消息。相邻的未标注消息仍可能有用。复查依据是保存的结果 JSON 和实验脚本，本次未重跑旧 Python 引擎。[旧实验设置](../reassessment-2026-11/local-evidence.md) · [分组计数](../reassessment-2026-11/longmemeval-results.json) · [原回放脚本](../reassessment-2026-11/replay_longmemeval.py)

对 V2，最直接的验证是用同一条任务轨迹分别检查**适用修订是否产生、目标来源是否找全、实际交付片段是否充分、下游任务是否完成**。把这些分母分开，才能判断下一笔投入应落在提炼、召回、组包或宿主使用上。旧实现的英文检索分数只能为实验设计提供参照。
