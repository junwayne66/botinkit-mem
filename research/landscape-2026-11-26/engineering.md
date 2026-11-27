# 记忆生命周期的工程取舍

调研日期为 2026-11-26。这里用云服务接口、工程团队的实践和公开评测回答六个实现问题。接口说明能证明某条路径存在，论文可以检验特定条件下的效果；表中面向 bmem 的做法是据此提出的选择。

## 从一次事件到一次可用的回答

| 环节 | 公开实践与适用条件 | 对 bmem 的建议 |
|---|---|---|
| 采集什么 | AWS 提供两条入口。会话事件可以保留原文再异步提炼。2026 年 9 月发布的 `IngestData` 也允许直接提交内容给长期提炼策略，省去短期事件副本。二者的重放条件不同。[AWS 发布说明](https://aws.amazon.com/about-aws/whats-new/2026/09/agentcore-memory-direct-ingest/) · [事件保留语义](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-create-a-memory-store.html) | 按来源决定留授权原文、可回源的定位信息，或仅留获准的最小证据片段。记录来源角色、原生对象、版本、发生时间、接收时间和使用范围。源端已删、离线或版本改变时，明确标记原文还能否核对。本人声明、助手转述、工具结果分别保留身份。 |
| 怎样提炼 | AWS 语义策略先提候选，再决定新增或合并；它只处理用户、助手两类消息。Google Memory Bank 在同一 scope 内整合候选，修订可记录提取的中间结果及最终事实。[AWS 策略](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/semantic-memory-strategy.html) · [Google 生成过程](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/generate-memories) | 把「原话是否支持候选」和「候选是否改变当前事实」分成两次可验判断。一句“下周试 Rust”只能支持计划；助手复述也不能把它晋升为用户长期偏好。纠正一个字段时要保留同一来源中仍成立的其他字段。 |
| 存哪些对象 | Google 的修订能解释一次变更并可回滚；历史修订有单独的保留期。Anthropic 在长任务中用上下文外的结构化笔记或对话压缩保持连续性，并指出过度压缩会遗漏稍后才显重要的细节。[Google 修订](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/revisions) · [Anthropic 上下文工程](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) | 分别读写源证据、当前断言及其修订、用于交接的短摘要。事实记录适用范围和有效期；迟到导入还需保留系统何时获知。索引、向量、摘要保留生成版本和根来源，换模型或源文纠正后可重建。临时交接笔记有自己的读写范围。 |
| 哪些内容退场 | AWS 的工程案例用访问记录、时间和定时作业筛选整合对象，且指出合并会丢细节。这是参考实现。SQLite FTS5 默认删除后，旧全文索引条目在合并前仍可留存；正常查询看不到与介质级清除是两种可独立检验的结果。[AWS 生命周期案例](https://aws.amazon.com/blogs/machine-learning/designing-lifecycle-policies-for-agentcore-memory/) · [SQLite FTS5 §6.13](https://sqlite.org/fts5.html#the_secure_delete_configuration_option) | 冷经历可退出主动注入，仍按权限回查。事实过期按生效区间，纠正按修订。来源撤回按其证据依赖重算。用户要求擦除时逐层核对正文、摘要、索引、队列和备份恢复策略，说明每项能证明的范围。长期少用的明确声明继续有效。 |
| 何时检索、给多少 | Anthropic 介绍按需读取和从笔记恢复上下文；UTILMEM 在保留金标证据时加入相似干扰会话，发现检索覆盖和最终作答可以分离。其金标证据回答是共享参照，长文任务的裁判仍有局限。[Anthropic 按需读取](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) · [UTILMEM §5、§7](https://aclanthology.org/2026.emnlp-main.1639.pdf) | 当前偏好按主体、项目、时间读有效版本。专名和错误码先做精确／词法基线；语义改写试向量；多个事件之间的关系才试沿边展开。先选来源，再在相同阅读预算内比命中句、邻近动作和结果；允许证据不足时不注入。 |
| 怎么长期改好 | Anthropic 区分执行轨迹与任务结束后的真实环境状态。LEDGER 在合成编码历史中观察到回答器能够从掺杂错误值的证据里答对；该预印本用作者自建规则和英文生成数据，在 LongMemEval 的模式选择上也存在部分样本重叠。[Anthropic 评测方法](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) · [LEDGER §VI–VII](https://arxiv.org/html/2611.20927v1) | 记录提供、装入、实际引用、独立核验结果和纠正，把它们作为不同事件。宿主缺少装入或使用回执时，保留“未知”。按根来源、任务和时间留出，比较同一题开关记忆的结果。先改有错误证据的环节，再在同一冻结集及新留出的任务上确认回归、成本与可逆性。 |

### 一个能暴露错误的时间线

以下是待验收的合成例子。V2 目前可表达全局和项目级的报告语言，客户条件、例外时段及跨助手转述在这个组合下仍需验证。周一用户说“项目 A 的报告用中文”，周二补充“本周给海外客户的那份用英文”，周三从另一个助手导入周一那句话的转述。当前项目 A 的普通报告应继续使用中文；本周那份客户报告应使用英文。周三的转述应链接到可核实的原始出处，不增加一票独立佐证，也不改变原话的生效时间。下周若询问上周那次例外，授权历史查询仍应能解释当时的依据。

如果用户随后撤回客户报告的来源，仅依赖它的提要和索引应退出普通读取；同时依赖多个来源的摘要，应基于仍有授权的证据重算。周一的声明仍有独立证据。若只撤销某一助手的读取授权，其他有权助手的当前事实继续可用。已发给第三方的旧上下文、仍在用户手中的旧备份要逐项标明可控范围。读者只看到“已经忘记”时，无法区分这几种结果。[Google 修订的回收窗口](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/revisions) · [LEDGER 的删除与评估范围](https://arxiv.org/html/2611.20927v1)

## 公开数据复核：一次回答里常常既要记住，也要避开

TimlMemBench 作者发布中英双语的合成对话。标准记忆池轨道给系统准备好的记忆，原始会话轨道要求各系统自行摄入历史。两轨的题与评分口径不同，直接相减不能计算提炼损失。原始会话轨道中的预期／禁止状态和证据标注须保留在评分端。[论文附录 A](https://aclanthology.org/2026.findings-emnlp.507.pdf) · [作者评测协议](https://github.com/lyirs/TimlMemBench/blob/447b195344a2feaf995176446379e60632831f94/docs/evaluation.md)

本次从作者仓库固定提交 `447b195` 读取四份治理与激活 JSONL，逐行统计两个指定数组都非空的用例。可从这些行抽取同时包含正反要求的用例。禁止使用某条记忆的原因可能是它过时、超出范围，或与当次任务无关。[中英文原始文件](https://github.com/lyirs/TimlMemBench/tree/447b195344a2feaf995176446379e60632831f94/data/timlmembench)

| 数据与判据 | 中文 | 英文 | 合计 |
|---|---:|---:|---:|
| 治理题，同时带期望当前状态 ID 和禁止状态 ID | 221 / 551 | 204 / 489 | 425 / 1,040 |
| 激活题，同时列出应该激活和不该激活的项目 | 407 / 411 | 367 / 370 | 774 / 781 |

这 774 题可以揭露两种投机行为：全部禁用记忆会漏掉应激活的项目，无差别回放则会带出不该激活的项目。统计单位是发布文件中的用例，同一用例可挂多个评分指标，也可与其他用例共享同一段历史。这些计数不能估计系统失误率；作者的语义裁判从输出评分，观察内部是否检索或装入还要有系统自身的记录。中英样本分别撰写，并非平行译文；真实多宿主的授权、故障、延迟和恢复还需另验。[数据说明与许可](https://github.com/lyirs/TimlMemBench/blob/447b195344a2feaf995176446379e60632831f94/docs/data_card.md)

从作者仓库上述固定提交的根目录可用 Python 标准库复算，过程无需模型。

```python
import json
from pathlib import Path

assets = [
    ("governance_cases_v1", "expected_current_state_ids", "forbidden_state_ids"),
    ("activation_cases_v1", "should_activate", "should_not_activate"),
]
for language in ("zh", "en"):
    directory = Path("data/timlmembench") / language / "canonical"
    for dataset, required, forbidden in assets:
        rows = [json.loads(line) for line in (directory / f"{dataset}.jsonl").read_text(encoding="utf-8").splitlines()]
        paired = sum(bool(row.get(required)) and bool(row.get(forbidden)) for row in rows)
        print(language, dataset, paired, len(rows))
```

## 检索范围与证据等级

2026-11-26 定向复核，下表记录能重做检索的关键词和实际阅读范围。Google、AWS 文档是滚动更新页面，以链接中的接口和章节定位；公开技术报告与产品文档没有提供跨系统同预算的对照结果。

| 检索源与主要检索词 | 实际阅读及采用范围 |
|---|---|
| AWS 官方站点，`AgentCore Memory extraction consolidation namespace`、`direct ingestion`、`designing lifecycle policies` | 阅读接口文档、带发布日期的 2026-09-08 公告、2026-09-04 工程示例。分别用于界定平台行为和开发团队可选择实现的策略。 |
| Google Cloud 官方文档，`Memory Bank generate memories revisions retention scope consolidation` | 阅读 Generate memories 的两阶段流程、Memory revisions 的审查、回滚及过期段。产品自身的恢复窗口仅用于说明语义差异。 |
| Anthropic Engineering，`effective context engineering structured note-taking`、`demystifying evals outcomes graders`；SQLite 官方手册，`FTS5 secure-delete rebuild` | 阅读长任务压缩、结构化笔记、结果裁判与 FTS5 旧索引行为原文。它们分别提供实现经验和数据库语义。 |
| ACL Anthology、arXiv、作者 GitHub，`2026 evidence utilization governed use long-term memory`、`auditable agent memory under change` | 核对 UTILMEM、TimlMemBench 正文与局限，以及 LEDGER v1 的实验和有效性讨论。另读取 TimlMemBench 固定版本的数据卡、协议与双语数据，直接复算上表。 |
