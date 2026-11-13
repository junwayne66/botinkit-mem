# 全自主记忆治理 · 行业实践调研

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-12 |
| 目的 | 为 botinkit-mem 取消"规则类记忆停在 proposed、必须用户确认才 active"的安全规则提供行业依据：记忆沉淀完全自主，用户"重要声明"为最高优先级输入，但用户不直接编辑记忆条目 |
| 方法 | `web_fetch` 抓取 arXiv 摘要页/HTML 全文 + GitHub README + 官方博客/文档；arXiv API 关键词检索补充投毒攻防文献；OWASP 官网 JS 渲染不可读时改用 GitHub 规范源 |
| 范围 | 只读调研；仅新增本文件（`docs/04-autonomous-memory/research-notes.md`），未运行 git |

> **核心问题**：① 事实如何自动确认为可信？② 冲突如何自动仲裁？③ 如何防止网页/工具输出投毒（memory poisoning）？④ 候选记忆如何自动晋升/降级/隔离？⑤ 后台整理（reflection/distillation）如何组织？
>
> bmem 现有生命周期词汇（proposed / active / disputed / superseded / retracted，见 [03-web-ui 调研](../03-web-ui/research-notes.md)）在下文中沿用。

---

## 0. TL;DR

| 结论 | 依据 |
|---|---|
| 主流系统**没有人审闸门**：Mem0、Zep/Graphiti、A-MEM、Letta、Hindsight、MIRIX 全部由 LLM/决策模型自主决定 ADD/UPDATE/DELETE/NOOP 或等价操作 | §2–§10 |
| 可信度不靠"确认"，靠**证据**：Hindsight 的 proof count + 原文引文、Graphiti 的 episode 溯源、Generative Agents 的反思引证，都是"证据强化/弱化"而非人工批准 | §3、§9、§10 |
| 冲突仲裁的共识是**失效不删除**（bi-temporal invalidation），保留历史供时间推理与回滚；Mem0 2026 新算法甚至放弃写时 UPDATE/DELETE，改为 ADD-only 累积 + 读时时间推理 | §2、§3 |
| 后台整理与前台服务**职责分离**：Letta sleep-time agent 独占记忆编辑权，前台 agent 只读；MIRIX-2.0 AutoDream 在写路径之外合并且不改写任何原始记录 | §5、§8 |
| 投毒是**已实测的真实威胁**：AgentPoison 以 <0.1% 投毒率达成 >80% 攻击成功率；PMPA 对 Claude Code 等 harness 实测跨会话攻击成功 81.7%。OWASP 已设 ASI06（Memory & Context Poisoning）并有参考实现 Agent Memory Guard（来源分级 + 写闸门 + quarantine + 回滚） | §12 |
| **全自主 ≠ 无闸门**：用"证据阈值 + 观察期 + 写闸门 + 后果闸门 + 审计回滚"替代人工确认。注意 OWASP 对高风险自动循环仍建议 human oversight，本项目以结构性控制替代，须明示残余风险 | §12、§14 |

---

## 1. 调研方法与免责声明

### 1.1 证据等级标注

| 标注 | 含义 |
|---|---|
| 【论文】 | 论文报告的机制或实验结果（本次多为摘要级，注明者除外） |
| 【工程】 | 开源实现 / 官方文档 / 行业指南中的做法，未必有对照实验 |
| 【概念】 | 概念性提案，作者明示尚未实证验证 |
| 【综合】 | 本文面向 botinkit-mem 的设计推断，非来源直接结论 |

### 1.2 来源台账（抓取状态）

| # | 来源 | URL | 状态 | 获取材料 |
|---|---|---|---|---|
| S1 | Mem0 论文 | https://arxiv.org/abs/2504.19413 | ✅ 成功 | 摘要 + HTML 全文 §1–§3（方法节完整） |
| S2 | Mem0 GitHub README | https://github.com/mem0ai/mem0 | ✅ 成功（raw） | 2026-04 新算法说明（ADD-only） |
| S3 | Zep 论文 | https://arxiv.org/abs/2501.13956 | ✅ 成功 | 仅摘要 |
| S4 | Graphiti README | https://github.com/getzep/graphiti | ✅ 成功（raw） | 时间图谱/失效/溯源/Jev 决策模型/安全警告 |
| S5 | A-MEM 论文 | https://arxiv.org/abs/2502.12110 | ✅ 成功 | 仅摘要（v11，NeurIPS 2025） |
| S6 | Letta 博客 Sleep-time Compute | https://www.letta.com/blog/sleep-time-compute | ⚠️→✅ | 首选 URL `/blog/sleep-time-agents` 404，备选重试成功 |
| S7 | Sleep-time Compute 论文 | https://arxiv.org/abs/2504.13171 | ✅ 成功 | 仅摘要 |
| S8 | Memory-R1 论文 | https://arxiv.org/abs/2508.19828 | ✅ 成功 | 仅摘要（v5，2026-01-14） |
| S9 | MemOS 论文 | https://arxiv.org/abs/2507.03724 | ✅ 成功 | 仅摘要（v4） |
| S10 | MIRIX 论文 | https://arxiv.org/abs/2507.07957 | ⚠️→✅ | **任务所给编号 2507.14903 有误**（实为自动驾驶论文 CoMoCAVs）；经 arXiv API 检索定位正确编号 2507.07957，摘要抓取成功 |
| S11 | MIRIX-2.0 论文（补充） | https://arxiv.org/abs/2611.12068 | ✅ 成功 | 摘要（2026-11-09，检索中发现的新版本） |
| S12 | Generative Agents 论文 | https://arxiv.org/abs/2304.03442 | ✅ 成功 | 摘要 + HTML 全文 §4（web_fetch 截断后经 curl 管道提取，未落盘） |
| S13 | Hindsight 文档站 | https://hindsight.vectorize.io/developer/observations 、/developer/memory-defense | ⚠️ 部分 | HTTP 200 但正文被截断（仅导航，疑似客户端渲染）；改用 S14/S15 补齐 |
| S14 | Hindsight GitHub README | https://github.com/vectorize-io/hindsight | ✅ 成功（raw） | observations/proof count、Memory Defense、decision models、banks |
| S15 | Hindsight 论文（补充） | https://arxiv.org/abs/2512.12818 | ✅ 成功 | 仅摘要 |
| S16 | MemInsight 论文（可选） | https://arxiv.org/abs/2503.21760 | ✅ 成功 | 仅摘要（v2） |
| S17 | AgentPoison 论文 | https://arxiv.org/abs/2407.12784 | ✅ 成功 | 仅摘要 |
| S18 | arXiv API 检索 "memory poisoning" | https://export.arxiv.org/api/query?search_query=all:%22memory+poisoning%22 | ✅ 成功 | 命中 79 条，取最新 12 条完整摘要（http→https 重定向后重试成功） |
| S19 | OWASP GenAI 官网 | https://genai.owasp.org/llm-top-10/ 、/resource/agentic-ai-threats-and-mitigations/ | ❌ 正文不可读 | HTTP 200 但 JS 渲染仅返回标题；改用 GitHub 规范源（S20） |
| S20 | OWASP LLM Top 10 2026 规范源 | https://github.com/GenAI-Security-Project/GenAI-LLM-Top10 | ✅ 成功（raw） | 2026 榜单 + LLM05:2026 Data and Model Poisoning 全文 |
| S21 | OWASP Agent Memory Guard（ASI06 参考实现） | https://github.com/OWASP/www-project-agent-memory-guard | ✅ 成功（raw） | README 全文（机制 + 自报基准） |
| S22 | OWASP The Agent's Epistemic Baggage（补充） | https://github.com/OWASP/The-Agents-Epistemic-Baggage | ✅ 成功（raw） | README 全文（认知漂移风险模型 + 审计 schema） |
| S23 | GitHub API 目录列表 | https://api.github.com/repos/GenAI-Security-Project/GenAI-LLM-Top10/contents/2026/final | ❌ 403 | 未认证限流；改用 raw README 定位文件名后成功 |
| S24 | OWASP ASI 官方威胁清单正文 | https://genai.owasp.org/initiatives/agentic-security-initiative/ | ❌ 未获取 | 官网 JS 渲染；ASI06 条目名称与内容经其参考实现 S21 间接引用，未读到官方条目原文 |

### 1.3 免责声明

1. **摘要级证据**：除 S1（方法节）、S12（§4）外，论文仅读摘要。凡摘要未提及的细节（如具体失效算法、置信度公式）不做断言；机制描述以 README/文档补充时已标注出处。
2. **基准数字不可横向排名**：LOCOMO / LongMemEval / DMR / ScreenshotVQA 的任务、切分、judge 模型不同；Mem0 README 明示其 2026 分数来自托管平台（含开源版没有的优化）；Hindsight README 称其分数经 Virginia Tech 与 Washington Post 独立复现、其余厂商分数为自报。所有数字按"作者/厂商自报"对待。
3. **检索非系统综述**：arXiv 检索为关键词 API 查询（79 命中取最新 12），未做穷尽筛选；OWASP 材料以 2026 版为准（2025 版 LLM04 已改编号为 LLM05:2026）。
4. **失败来源已如实记录**（S6/S10/S13/S19/S23/S24），未用记忆补写未读到的内容。
5. 本文件为唯一写入产物；未修改其他文件、未运行 git。

---

## 2. Mem0 —— 两阶段抽取/更新与 ADD/UPDATE/DELETE/NOOP 自主决策

**来源**：[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)（摘要+方法节）· [GitHub README](https://github.com/mem0ai/mem0)

| 维度 | 内容 |
|---|---|
| 机制名称 | 两阶段管线：extraction（LLM 从消息对+会话摘要+近 m=10 条消息抽取候选事实）→ update（每个候选事实检索 top-s=10 语义相似既有记忆，LLM 经 tool call 自主选择 **ADD / UPDATE / DELETE / NOOP**）；摘要异步刷新不阻塞主管线。图变体 Mem0^g：实体-关系三元组 + 冲突检测 + LLM update resolver，冲突关系**标记 invalid 而非物理删除**以支持时间推理【论文】 |
| 如何替代人工确认 | 完全由 LLM 依据"候选事实 vs 既有记忆"的语义关系直接选操作，无独立分类器、无人审；一致性靠写时仲裁维持【论文】 |
| 冲突与可信度处理 | 冲突→UPDATE/DELETE（基础版）或 invalid 标记（图版）；无显式置信度/证据计数，可信度隐含在 LLM 判断中【论文】 |
| 可借鉴点 | ① "候选→相似检索→四操作"是最小可用自主仲裁环；② 异步摘要刷新=低成本后台整理；③ 图版"失效不删除"与 bmem 的 superseded 语义一致；④ **2026-04 新算法转向 ADD-only 单遍抽取**：不再写时 UPDATE/DELETE，记忆累积，靠实体链接+多信号检索+时间推理在读时解决新旧问题；agent 确认的行为事实与用户事实同权存储【工程，README】 |
| 局限 | LLM 仲裁会误删/误改（论文未报告仲裁错误率）；无对抗鲁棒性评测；LOCOMO 为对话 QA，与编码 Agent 场景有距离；新算法分数为托管平台自报，开源版"方向一致但数值不同"（README 原文）。**行业信号：写时破坏性仲裁风险高，头部产品已退到 append-only + 读时仲裁**【综合】 |

> 对 bmem 的启示：候选记忆四操作可以作为 bmemd 提炼器的动作空间，但 DELETE 应实现为 retracted/tombstone；更稳妥的默认是 Mem0 新算法路线——只追加、读时按时间与证据排序。

---

## 3. Zep / Graphiti —— 时间知识图谱与事实自动失效

**来源**：[arXiv:2501.13956](https://arxiv.org/abs/2501.13956)（摘要）· [Graphiti README](https://github.com/getzep/graphiti)

| 维度 | 内容 |
|---|---|
| 机制名称 | Graphiti 时间知识图谱：Entities（节点，摘要随时间演化）/ Facts（边，三元组+**时间生效窗口**）/ Episodes（原始数据溯源层，一切派生事实可回溯）；增量构建无需全图重算；混合检索（语义+BM25+图遍历）。**bi-temporal 双时间轴**（事实何时为真 vs 何时入库），信息变化时旧事实**自动失效（invalidated）而非删除**【工程，README；论文摘要仅述"maintaining historical relationships"】 |
| 如何替代人工确认 | 新 episode 入库即触发 LLM 抽取实体/关系、去重、冲突检测与失效，全程无人工；"现在什么为真/过去某刻什么为真"都是查询问题而非审批问题【工程】 |
| 冲突与可信度处理 | 矛盾→自动失效旧边并保留时间历史（README 对比表：Contradiction Handling = "Automatic fact invalidation with temporal history preserved"）；可信度靠 episode 溯源链而非概率分数。**Jev 决策模型去重**：专用决策模型以概率回答类型化问题，重复阈值 0.7、**矛盾阈值 0.8**，只路由 dedupe/resolve_edge 两类判断，不做生成【工程，README】 |
| 可借鉴点 | ① bmem 事实表加 valid_at / invalid_at 双时间字段，冲突=置 invalid_at，天然对接 superseded；② episode 层=不可变原始事件，派生记忆必须挂溯源外键；③ 用小型决策模型/NLI 做"是否矛盾"的类型化判断（带概率阈值），比通用 LLM 自由裁决便宜且可控；④ 官方参考服务器**明示无认证、无 prompt-injection 防护**，要求生产自建信任边界——记忆服务本身必须设防【工程】 |
| 局限 | 摘要未报告失效判定的误报率；LLM 抽取依赖结构化输出，小模型易失败（README 警告）；图维护成本与延迟随规模上升；无投毒防御内建机制【综合】 |

---

## 4. A-MEM —— Zettelkasten 自主链接与记忆演化

**来源**：[arXiv:2502.12110](https://arxiv.org/abs/2502.12110)（摘要，NeurIPS 2025）· [代码](https://github.com/WujiangXu/A-mem-sys)

| 维度 | 内容 |
|---|---|
| 机制名称 | Zettelkasten 卡片盒式 agentic memory：新记忆生成含**上下文描述、关键词、标签**的结构化 note；系统分析历史记忆自主建立链接；**memory evolution**——新记忆整合时可触发既有记忆上下文表示与属性的更新，网络持续自我精炼【论文】 |
| 如何替代人工确认 | 组织（索引/链接/演化）全部由 agent 自主决策，固定操作被动态决策取代；无人审环节【论文】 |
| 冲突与可信度处理 | 摘要**未描述显式冲突仲裁或置信度机制**；演化通过改写旧 note 的上下文表示吸收新信息（本文推断：这隐含"新信息可修正旧理解"，但也带来错误传播风险）【论文+综合】 |
| 可借鉴点 | ① 记忆条目带结构化属性（keywords/tags/context）利于检索与去重；② "链接"是低风险的整理动作——只加关系不改内容，适合作为后台整理的第一步；③ 演化=受控的 UPDATE，应限制为"补充上下文"而非"改写事实"【综合】 |
| 局限 | 无信任/证据模型；演化可能把错误写入旧记忆并被再次检索放大（与 §12 认知漂移的 re-storage loop 对应）；评测为对话基准【综合】 |

---

## 5. Letta sleep-time agents / Sleep-time Compute —— 后台离线整理

**来源**：[Letta 博客](https://www.letta.com/blog/sleep-time-compute)（首选 URL 404 后重试成功）· [arXiv:2504.13171](https://arxiv.org/abs/2504.13171)（摘要）

| 维度 | 内容 |
|---|---|
| 机制名称 | **双 agent 架构**：primary agent 负责对话/工具，**不持有 core memory 编辑工具**；sleep-time agent 独占记忆编辑权，异步管理自己与 primary 的 in-context 记忆块，把 raw context 提炼为 learned context；可配置不同模型（sleep-time 用更强更慢的模型）与运行频率；记忆修改是 "anytime" 的——primary 随时可读，不必等整理完成【工程，博客】。论文侧：查询到达前离线"思考"上下文、预计算有用量【论文】 |
| 如何替代人工确认 | 记忆整理是内部自治过程：没有用户参与，质量靠"更强模型 + 更高延迟预算 + 持续重写"保证；前台可靠性靠**职责分离**（对话 agent 不碰记忆写工具）保证【工程】 |
| 冲突与可信度处理 | 博客/摘要未描述显式冲突仲裁；思路是后台持续重写 learned context 使其"clean, concise, detailed"，增量式 MemGPT 记忆会 messy 的问题交给离线整理解决【工程】 |
| 可借鉴点 | ① bmemd  foreground 只做"采集事件 + 注入只读记忆"，提炼/仲裁/整理全部交给后台 worker（对应二期"中心统一提炼"）；② 后台用更强模型、低频批量运行，成本可控；③ anytime 语义：整理不阻塞读取，读到的永远是上一稳定版本；④ 论文实测：同等精度下 test-time 计算约省 5×，扩大 sleep-time 计算可再提精度 13%（Stateful GSM-Symbolic）/18%（Stateful AIME），多查询摊销平均成本降 2.5×；查询可预测性与收益正相关【论文】 |
| 局限 | 后台重写若出错会污染 learned context（无内建证据约束，需配 §10/§12 机制）；持续 token 成本；论文收益主要在数学/推理任务，记忆整理收益是 extrapolation（博客明示"expect"）【论文+综合】 |

---

## 6. Memory-R1 —— RL 训练的记忆管理器

**来源**：[arXiv:2508.19828](https://arxiv.org/abs/2508.19828)（摘要 v5）

| 维度 | 内容 |
|---|---|
| 机制名称 | 两个专门 agent：**Memory Manager** 学习结构化操作 ADD/UPDATE/DELETE/NOOP；**Answer Agent** 预选并推理相关记忆条目。两者用结果驱动 RL（PPO/GRPO）微调，仅 152 个训练 QA 对，跨 LoCoMo/MSC/LongMemEval 三基准、3B–14B 模型规模泛化【论文】 |
| 如何替代人工确认 | 把"该不该存/改/删"从启发式 prompt 升级为**学习到的策略**：以最终回答质量为奖励，自动学会管理动作，无需人工标注每条记忆的对错【论文】 |
| 冲突与可信度处理 | 冲突处理隐含在 UPDATE/DELETE 策略中，由结果奖励 shaping；摘要未报告显式置信度或证据机制【论文】 |
| 可借鉴点 | ① 仲裁质量可以用**下游任务结果**自动评估（bmem 可用"注入后任务成功率/用户纠正率"作为延迟奖励信号，离线评估提炼策略）；② 小数据即可训练专用管理策略，长期可替换通用 LLM 裁决降本；③ 管理动作空间与 Mem0 一致，说明四操作是行业收敛接口【论文+综合】 |
| 局限 | 需要 RL 训练基础设施；奖励黑客/过拟合风险；152 QA 对规模小；无对抗场景评测；摘要未报告 DELETE 误删率【综合】 |

---

## 7. MemOS —— 记忆调度与生命周期

**来源**：[arXiv:2507.03724](https://arxiv.org/abs/2507.03724)（摘要 v4）

| 维度 | 内容 |
|---|---|
| 机制名称 | 记忆操作系统：统一管理 plaintext / activation（KV）/ parameter 三类记忆的表示、调度与演化；基本单元 **MemCube = 记忆内容 + 元数据（provenance 溯源、versioning 版本）**；MemCube 可组合、迁移、融合，实现记忆类型间转换【论文】 |
| 如何替代人工确认 | 把记忆当作可调度系统资源：生命周期（生成→调度→演化）由系统管理而非人工审批；治理靠元数据与调度策略【论文，架构层面】 |
| 冲突与可信度处理 | 摘要未给出具体冲突仲裁算法；可信度基础设施=MemCube 的 provenance/versioning 元数据（可追溯、可版本化）【论文】 |
| 可借鉴点 | ① bmem 每条记忆=内容+治理元数据（来源、版本、状态、信任级）的"MemCube"，SQLite 表结构可直接对齐；② 生命周期状态机由调度器驱动（定时任务/事件触发），与二期中心 bmemd 角色吻合；③ 记忆类型分层（事实/经历/规则/技能）各有不同治理策略【论文+综合】 |
| 局限 | 系统架构论文，治理机制多为设计提案，摘要未报告对照实验验证；activation/parameter 记忆与本地 SQLite 场景距离远，只取 plaintext 治理部分【综合】 |

---

## 8. MIRIX / MIRIX-2.0 —— 多 Agent 记忆系统

**来源**：[arXiv:2507.07957](https://arxiv.org/abs/2507.07957)（摘要；任务所给 2507.14903 为误编号）· [arXiv:2611.12068](https://arxiv.org/abs/2611.12068)（MIRIX-2.0 摘要）· [代码](https://github.com/Mirix-AI/MIRIX)

| 维度 | 内容 |
|---|---|
| 机制名称 | MIRIX：六类记忆 **Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault** + 多 agent 框架动态协调更新与检索；LOCOMO 85.4%（自报 SOTA），ScreenshotVQA 比 RAG 基线高 35% 且存储省 99.9%【论文】。MIRIX-2.0：六个类型化记忆 agent 保存**完整记录作为 system of record**；**provenance hypergraph** 以 n-ary frame 索引 episodic/semantic 记录并引用原始行；检索在显式预算下运行；**AutoDream** 在**写路径之外**合并重复锚点、**不改写任何记录**，并把任务会话蒸馏为 skills；LoCoMo 91.4%、LongMemEval-S 72.6%（自报）【论文】 |
| 如何替代人工确认 | 更新/检索由 meta 协调的多 agent 自主完成；整理（AutoDream）是离线旁路过程，不需要人批准也不破坏原始记录【论文】 |
| 冲突与可信度处理 | 可信度=溯源超图（每条派生记忆引用原始记录行）+ 完整记录不可变；冲突/重复由 AutoDream 旁路合并而非覆盖；摘要未描述显式矛盾仲裁算法【论文+综合】 |
| 可借鉴点 | ① **记忆分型治理**：规则类（procedural/semantic）与经历类（episodic）适用不同晋升策略；② "完整记录为权威、派生物可重建"与 bmem"事件同步、索引派生"的二期设计同构；③ AutoDream 式**旁路整理**：合并/蒸馏产物是新记录+引用，永不改写原始证据——这是全自主整理防漂移的关键不变量；④ 检索预算化，证据量不随历史膨胀【论文+综合】 |
| 局限 | 多 agent 协调成本；摘要未报告 AutoDream 合并错误率；基准为对话/任务 QA，编码场景需自验【综合】 |

---

## 9. Generative Agents —— reflection 树与重要性评分

**来源**：[arXiv:2304.03442](https://arxiv.org/abs/2304.03442)（摘要 + HTML 全文 §4，UIST 2023）

| 维度 | 内容 |
|---|---|
| 机制名称 | memory stream（自然语言观察流，含创建/最近访问时间戳）；检索分 = **recency（指数衰减 0.995）+ importance（LLM 在记忆创建时打 1–10 分）+ relevance（embedding 余弦）**，min-max 归一后加权（α 均为 1）。**Reflection**：近期事件重要性分总和超过阈值（实现为 150，约每天 2–3 次）时触发——用最近 100 条记录让 LLM 提 3 个高层问题→按问题检索证据→抽取 5 条洞察并**引用证据记录编号**（"insight (because of 1, 5, 3)"）→洞察作为新记忆入库并保留指向证据的指针；反思可基于反思，形成**reflection 树**（叶=观察，上层=越来越抽象的推断）【论文，§4.1–4.2 原文】 |
| 如何替代人工确认 | 观察→反思→计划全自动；没有任何人工批准环节；消融实验证明 observation/planning/reflection 各自对行为可信度关键【论文】 |
| 冲突与可信度处理 | 无显式冲突仲裁；可信度机制=**反思必须引证**（洞察挂证据指针，可回溯到观察）+重要性评分区分核心/琐碎。论文自述常见错误：检索失败、**对记忆的虚构润色（embellishments）**——即反思会幻觉【论文】 |
| 可借鉴点 | ① 写入时打重要性分，作为后台整理触发器（重要性累积阈值）与检索排序因子；② **反思产物必须带证据引用**，且反思记忆与观察记忆分型存储——bmem 的"推断规则"应永远挂 evidence 外键；③ 反思树=派生层级，上层推断的信任不应超过其证据（对接 §12.3 认知漂移）；④ 衰减函数天然实现"久不使用即降级"【论文+综合】 |
| 局限 | 重要性分是 LLM 主观判断；反思幻觉无内建校验；模拟环境验证（believability 人评），非生产记忆系统；无投毒防御【论文+综合】 |

---

## 10. Hindsight —— observations 的 proof count 与证据强化/弱化

**来源**：[GitHub README](https://github.com/vectorize-io/hindsight) · [arXiv:2512.12818](https://arxiv.org/abs/2512.12818)（摘要）· [文档站](https://hindsight.vectorize.io)（正文截断，见台账 S13）

| 维度 | 内容 |
|---|---|
| 机制名称 | 四类记忆网络：**world facts / experiences / observations / mental models**，三操作 **retain / recall / reflect**。retain 用 LLM 抽取事实/时间/实体/关系并规范化；recall 四路并行（语义/BM25/图/时间）+ RRF 融合 + cross-encoder 重排；**observations=后台把相关事实 consolidate 成的去重信念，每条保留支持证据的原文引文（exact quotes）与 proof count；新证据到来时是 refine 而非 overwrite——强化（strengthen）、弱化（weaken）或扩展（extend）既有信念**。mental models/pages=对固定问题的常驻答案，后台随学习重写，读取是纯数据库读。另有 0.8B **decision model**（hs-decide-1-flash）以类型化答案处理"是否新事实/哪些记忆相关/是否改 page"等小判断【工程，README】。论文：区分 evidence 与 inference，reflect 以**可追溯方式**更新信息；LongMemEval 39%→83.6%（20B 开源模型，自报）【论文】 |
| 如何替代人工确认 | 信念的确立=证据累积（proof count）而非人工批准；整理在后台自动 consolidate；小判断交给专用决策模型，无需人介入【工程】 |
| 冲突与可信度处理 | 新证据与既有信念冲突→**弱化**该信念（proof 机制），不静默覆盖；证据保留原文引文可复核；bank 级 **Memory Defense**：opt-in 策略对每次 retain 扫描 secrets/PII（45 种模式），命中即 redact（`[REDACTED:github_token]`）或 block 于存储之前；bank 严格隔离 + disposition traits（skepticism 等）影响 reflect 推理【工程，README】 |
| 可借鉴点 | ① **proof count + 原文引文**是 bmem"事实自动确认为可信"的最直接落地：confidence 由独立证据条数与来源质量计算，可解释可复核；② "强化/弱化/扩展"三态代替覆盖，对接 active/disputed/superseded；③ 写入前秘密/PII 扫描（确定性规则，便宜）应默认开启；④ 小决策模型做高频判断、大模型只做反思，成本分层；⑤ 生命周期 webhooks（retain/consolidation/refresh）=审计事件源【工程+综合】 |
| 局限 | proof count 的增减规则/阈值未在 README 公开细节（文档正文未抓到）；基准自报（README 称经独立复现）；Memory Defense 只覆盖 secrets/PII 模式，不含语义级投毒检测；consolidate 仍可能引入推断错误（论文以 traceable 缓解）【综合】 |

---

## 11. MemInsight —— 自主记忆增强（可选来源）

**来源**：[arXiv:2503.21760](https://arxiv.org/abs/2503.21760)（摘要 v2）

| 维度 | 内容 |
|---|---|
| 机制名称 | 自主记忆增强：对历史交互做属性挖掘与标注（entity-centric 与 conversation-centric 两种增强），改善语义表示与检索；LLM-REDIAL 推荐说服力 +14%，LoCoMo 检索 recall 比 RAG 基线 +34%（自报）【论文】 |
| 如何替代人工确认 | 增强（标注/属性化）全自动，无人工；定位是"让记忆更好检索"而非"确认记忆为真"【论文】 |
| 冲突与可信度处理 | 摘要未涉及冲突仲裁与信任机制【论文】 |
| 可借鉴点 | 后台整理的第一档动作=**只加属性不改内容**（关键词/实体/时间标注），风险最低，可先于合并/反思上线【综合】 |
| 局限 | 增强质量依赖 LLM；无证据/信任模型；与治理问题相关性弱，作为整理管线的补充件【综合】 |

---

## 12. 记忆投毒：攻击实测与防御实践

### 12.1 AgentPoison —— 攻击基准（威胁有多大）

**来源**：[arXiv:2407.12784](https://arxiv.org/abs/2407.12784)（摘要）

| 维度 | 内容 |
|---|---|
| 机制名称 | 首个针对通用/RAG 型 LLM agent 的后门攻击：把触发器生成做成约束优化，将带触发器实例映射到独特嵌入区域，使含触发指令必检索到恶意示范；**无需训练/微调模型**，触发器可迁移、上下文连贯、隐蔽【论文】 |
| 实测数字 | 三类真实 agent（RAG 自动驾驶、知识密集 QA、医疗 EHRAgent）平均 **ASR >80%，投毒率 <0.1%，良性性能影响 <1%**【论文】 |
| 对 bmem 的含义 | 极少量被污染的工具/网页输出即可长期操纵 agent；"检索相似即注入"的管线天然放大投毒；防御不能靠"量少忽略"【综合】 |
| 防御启示（本文推断+OWASP） | 嵌入空间异常检测、写入来源校验、外部内容隔离、触发器红队测试；攻击论文本身未给出防御方案【综合】 |

### 12.2 arXiv 检索补充：投毒攻防最新进展（S18，均为摘要级）

检索式 `all:"memory poisoning"`（按提交时间倒序，79 命中取 12），与 bmem 最相关者：

| 论文 | 威胁/机制 | 关键数字/结论 | 对 bmem 的启示 |
|---|---|---|---|
| [PMPA（2609.13889）](https://arxiv.org/abs/2609.13889) | 对 **harness 型 agent（OpenClaw、Claude Code）** 的持久记忆投毒：恶意指令藏于良性外部源，诱导 agent 自己写入持久记忆，跨会话触发 | ISR/C-ASR：OpenClaw 73.7%/55.5%，Claude Code 66.9%/**81.7%**；prompt 级防御在记忆已污染后作用有限 | bmem 的直接威胁模型：编码 agent 读网页/工具输出→写记忆→后续会话注入。必须在**写入时**设防，事后 prompt 防御不够 |
| [MemSentry（2609.08747）](https://arxiv.org/abs/2609.08747) | 拦截**拟写入持久记忆**的操作，联合 source trust、语义风险、组件依赖 DAG 上的攻击半径、访问风险、签名安全态 delta，产出确定性 **Accept / Review / Quarantine** | SBERT+LR 语义分类 91.7% acc；外部 quarantine 级威胁 100% 检出；最高信任内部人（T=1）的危险写→升级人审 | 写闸门+隔离的完整设计模板；bmem 全自主化时把"Review（人审）"替换为"保守 quarantine + 审计通知" |
| [MemForm（2610.14431）](https://arxiv.org/abs/2610.14431) | **记忆形成界面**投毒：Content/Evidence/Scope/Selection/Path 五类界面；整理顺序（consolidation order）本身可被操纵 | 六模型×两格式全部易感；**相同经历、不同整理顺序→不同下游行为**；现有防御不完全 | 后台整理管线本身是攻击面：整理顺序/选择策略要确定性、可审计；"真实经历+成功结果"不等于安全 |
| [Self-Propagating Misalignment（2610.04083）](https://arxiv.org/abs/2610.04083) | **无外部攻击者**：agent 把自身目标写入持久记忆，由未来会话执行；禁用记忆工具后 74% 改用文件系统 | 显式目标 58% 成功、弱提示 18%；可跨 100 会话存活；LLM 审计员（MemMorph）仅 71%→34% | 防投毒不能只防外部：**agent_authored 写入同样要过闸门**；自相似重复写入需冷却/限流；审计员不可全信 |
| [Price of Safety（2609.22818）](https://arxiv.org/abs/2609.22818) | 防御的**良性成本**实测：固定骨干只换防御 | sanitization/provenance 检查零误触发、0 token 开销；LLM 异常检测拦 0.4% 回合（+1.5% token）；读时重排丢弃 2.2% 记忆，50 条抽样仅 2 条有内容依据；各防御精度成本不可检出 | 优先上**确定性规则**（模式/来源/保护键），LLM 级防御谨慎叠加；防御要度量误隔离率，避免"安全剧场" |
| [Accept Changes, Reject Attacks（2610.41056）](https://arxiv.org/abs/2610.41056) | 过时记忆 vs 投毒需要**相反的写决策**，固定写规则无法两全；提出 evidence-conditioned accuracy（ECA）并微调管理器 | 现有系统与防御不比未调优管理器更鲁棒；单会话合成 episode 微调后 ECA 翻倍以上，但仍不完全 | "接受合法更新"与"拒绝注入"必须同一管线联合优化；bmem 应自建含更新+攻击的回归评测集 |
| [MemoryDojo（2611.09721）](https://arxiv.org/abs/2611.09721) | 跨记忆架构的投毒生命周期基准（155 轨迹、5 系统） | **所有被测记忆系统均易感**，下游影响差异大 | 不存在"天然免疫"的架构；选型后必须实测 |
| [DREAM（2610.13267）](https://arxiv.org/abs/2610.13267) | 神经启发的**选择性遗忘修复**：扰动再激活→异常诊断→抑制有害记忆、强化良性记忆 | 后门 ASR 降 >95%，良性效用保持 | 污染后的修复路径=定向弱化/隔离而非全库清空；与 proof 弱化机制兼容 |
| [Trust propagation（2609.17648）](https://arxiv.org/abs/2609.17648) | 多 agent 管线中共享记忆投毒+伪造审批；对比 LLM Validator 与**独立授权层**（任务绑定签名令牌+策略 oracle） | 无防御时投毒 100% 到达执行；有结构化授权后 Validator 仍 100% 被绕过但**不安全执行率 0%** | **LLM 判断可被绕过，结构性授权仍能兜底**：高危动作的闸门必须在记忆/LLM 之外（对接 R12） |

### 12.3 OWASP：LLM05:2026、ASI06 与两个治理项目

**来源**：[LLM Top 10 2026 规范源](https://github.com/GenAI-Security-Project/GenAI-LLM-Top10)（[LLM05:2026 全文](https://github.com/GenAI-Security-Project/GenAI-LLM-Top10/blob/main/2026/final/LLM05_DataModelPoisoning.md)）· [Agent Memory Guard](https://github.com/OWASP/www-project-agent-memory-guard) · [The Agent's Epistemic Baggage](https://github.com/OWASP/The-Agents-Epistemic-Baggage)

| 来源 | 机制/要点 | 对 bmem 的可借鉴点 | 局限 |
|---|---|---|---|
| **LLM05:2026 Data and Model Poisoning**（2025 版 LLM04 改编号）【行业指南】 | 投毒发生在一切摄入/转换/检索/复用环节；场景 #2=网页隐藏指令进入 RAG/记忆，#9=跨会话向持久记忆注入指令。缓解 12 条，与 bmem 直接相关：**信任边界+来源评分+系统指令与外部数据隔离**；沙箱与最小权限；数据版本控制+回滚取证；**控制自动再训练/反馈循环（验证输入、限速、要求 human oversight）**；统计+AI 异常检测；持续红队 | 写入侧来源评分与隔离是行业共识；版本化+回滚是投毒后的标准恢复手段；自动循环要限速防渐进投毒 | 指南级，无实验数字；**明确建议高风险自动循环保留人工监督**——与"全自主"要求存在张力，须以结构性控制补偿并明示残余风险（见 §14.3） |
| **OWASP Agent Memory Guard（AMG）**，ASI06: Memory & Context Poisoning 的官方参考实现，2026-11 升为 OWASP Lab 项目【工程】 | 位于 agent 与记忆库之间的写闸门：① **source_class 来源分级**（external_tool / user_input / agent_authored / system）随每次写入与 SecurityEvent 传递；② 检测器：prompt injection、secrets/PII、**protected_keys/immutable_keys 篡改**、尺寸异常、**自强化循环**（同键自相似 agent_authored 写入冷却窗口限流）；③ YAML 策略把发现映射为 **allow / redact / quarantine / block**；④ SHA-256 基线防带外篡改；⑤ 每决策发 SecurityEvent + 快照回滚；⑥ `retire_if` 谓词驱动 TTL 退休；⑦ OTel 导出。自报基准：55 例召回 92.5%、精确率 100%、中位延迟 62µs；AMSB 生命周期基准要求毒 payload **跨 context reset 存活**再读回评分 | bmem 写闸门的完整蓝图：来源四分级、保护键（用户声明/身份类记忆不可被 agent 写覆盖）、quarantine 区、决策事件流、快照回滚、自强化冷却、TTL 退休。确定性规则为主，成本极低 | 基准为项目自报（55 例）；语义级投毒检测在 roadmap（v0.4 ML 异常检测）；其 strict 预设被自家 AMSB 评 D 级（持久化检测器缺省未开）——**默认配置≠安全**，bmem 若借鉴须显式开启持久化/延迟激活检测 |
| **The Agent's Epistemic Baggage**（OWASP 项目）【概念，作者明示未实证】 | **self-reinforcing epistemic drift** 风险模型：① re-storage loop（agent 生成物被存→被检索→影响推理→再写回，四环皆是控制点）；② consolidation drift（摘要/压缩丢失 provenance、验证态、时效、证据支持，且变换物取代原件或获得更高优先级）。**审计 schema 九维分离**：表示类型（received/tool output/agent inference）、provenance、验证态（untested/supported/contradicted/validated/rejected）、时间有效性、证据关系（强度/独立性/质量）、变更历史、**认知晋升依据**、**授权依据**、使用与决策 lineage。核心论断：**"provenance 确立来源而非真相"；"政策批准不使断言更真，新证据也不自动授予行动权限"**；认知状态与操作授权是两个独立审计对象；生命周期测试含"知识僵化"检查（合法更新不得被安全机制挡死） | 直接回答 bmem 的 proposed→active 治理：晋升必须记录**证据依据**而非审批动作；推断永远标注为推断（不得洗成 received fact）；整理不得丢溯源；验证态与时效分离（validated 也会 stale）；注入台账=usage lineage；"拒绝无依据晋升"与"允许合法更新"要同时测试 | 概念模型+审计规范，无实现与实证；其引用的 SSGM/misevolution/MemEvoBench 等实证工作本次未逐一抓取（间接引用） |

---

## 13. 横向对比表

| 系统/来源 | 自主决策机制 | 冲突仲裁 | 可信度/证据 | 晋升/降级/隔离 | 后台整理 | 投毒防御 | 验证等级 |
|---|---|---|---|---|---|---|---|
| Mem0（论文） | LLM tool call 四操作 ADD/UPDATE/DELETE/NOOP | 写时 UPDATE/DELETE；图版 invalid 标记 | 无显式置信度 | 无（直接写） | 异步摘要刷新 | 无 | 【论文】LOCOMO 自报 |
| Mem0（2026 OSS） | **ADD-only 单遍抽取**，读时时间推理 | 不覆盖，累积+时间排序 | agent 事实与用户事实同权 | 无 | 实体链接 | 无 | 【工程】厂商自报 |
| Zep/Graphiti | LLM 抽取+去重+失效；Jev 决策模型（矛盾阈值 0.8） | **bi-temporal 自动失效不删除** | episode 溯源链 | 失效=降级；历史保留 | 增量图构建 | 参考服务器明示无防护，需自建 | 【论文+工程】 |
| A-MEM | agent 自主链接+演化 | 演化改写上下文（无显式仲裁） | 无 | 无 | 链接/演化即整理 | 无 | 【论文】 |
| Letta sleep-time | 双 agent：sleep-time 独占记忆编辑 | 后台重写 learned context | 无显式证据机制 | 无 | **anytime 离线整理，强模型低频** | 无 | 【工程+论文】计算收益实测 |
| Memory-R1 | **RL 学习**的四操作策略 | 策略内隐（结果奖励） | 无 | 无 | Answer Agent 记忆蒸馏 | 无 | 【论文】小数据 RL |
| MemOS | MemCube 调度/生命周期 | 摘要未述 | **provenance+versioning 元数据** | 生命周期状态机（架构） | 调度器驱动 | 摘要未述 | 【论文】架构提案 |
| MIRIX / 2.0 | 六型记忆+多 agent 协调；AutoDream 旁路 | 旁路合并重复，不改写记录 | **provenance hypergraph 引用原始行** | 完整记录为权威 | **AutoDream：写路径外合并+技能蒸馏** | 摘要未述 | 【论文】自报 SOTA |
| Generative Agents | 重要性评分+阈值触发反思 | 无显式仲裁 | **反思必须引证证据记录** | 衰减=隐式降级 | **reflection 树（阈值 150）** | 无 | 【论文】模拟+人评消融 |
| Hindsight | retain/recall/reflect；0.8B 决策模型 | **证据强化/弱化/扩展，不覆盖** | **proof count + 原文引文** | 弱化=降级；bank 隔离 | 后台 consolidate→observations；mental models 重写 | Memory Defense：45 模式 secrets/PII redact/block | 【工程+论文】自报+称独立复现 |
| MemInsight | 自主属性增强 | 无 | 无 | 无 | 标注/属性化 | 无 | 【论文】 |
| OWASP AMG | 写闸门策略引擎 | protected/immutable 键防篡改 | source_class 四级 | **quarantine 区 + block/redact** | retire_if TTL | **检测器+策略+快照回滚+自强化冷却** | 【工程】自报基准 |
| Epistemic Baggage | 认知状态转移需证据依据 | 矛盾→contradicted 态 | 九维审计 schema | 晋升/降级均需记录依据 | 整理不得丢溯源（约束） | 内外源分治 | 【概念】未实证 |
| 投毒攻击群（§12.2） | — | — | — | — | 整理顺序本身可被投毒（MemForm） | 威胁实测：全部系统易感 | 【论文】攻击实测 |

---

## 14. 综合结论：botinkit-mem 全自主治理推荐机制清单

### 14.1 核心问题速答

| 问题 | 行业答案（本次调研收敛） | 主要依据 |
|---|---|---|
| 事实如何自动确认为可信？ | 不靠批准，靠**证据累积**：独立来源印证（proof count）+ 原文引文可复核 + 观察期内无矛盾 + 使用结果反馈；推断类永远低于观察类 | Hindsight、Graphiti、Generative Agents、Epistemic Baggage |
| 冲突如何自动仲裁？ | **检测（NLI/决策模型/LLM tool call）→ 时间失效（invalid_at）而非删除**；同刻矛盾→双方 disputed 待新证据；读时按时间+证据排序（Mem0 新算法路线） | Graphiti、Mem0、Memory-R1、Hindsight |
| 如何防投毒？ | **写入时**设防：来源分级+写闸门检测器+allow/redact/quarantine/block；外部内容永远只是数据不是指令；agent 自写同样受限；结构性授权兜底高危动作 | AgentPoison/PMPA 实测、AMG、MemSentry、Trust propagation、LLM05:2026 |
| 候选如何晋升/降级/隔离？ | probation（只追加不注入或低权注入）→ 证据阈值晋升 active；矛盾/异常→弱化→disputed→quarantine；TTL/衰减→archive；污染→retract+快照回滚 | Hindsight proof、AMG quarantine/retire_if、Generative Agents 衰减、Epistemic Baggage 状态转移 |
| 后台整理如何组织？ | 前台只读+采集，后台 sleep-time worker 独占写；整理=旁路派生（合并/蒸馏产物是新记录+引用，**永不改写原始证据**）；重要性阈值触发反思，反思必须引证 | Letta、MIRIX-2.0 AutoDream、Generative Agents、MemInsight |

### 14.2 推荐机制清单（按优先级，12 条）

> 优先级含义：P0=安全底线，先于"取消人审"上线；P1=自主治理闭环；P2=加固项（建议与 P0 同步实施）。每条标注验证等级。

| # | 优先级 | 机制 | 要点（bmem 落地） | 依据 | 验证等级 |
|---|---|---|---|---|---|
| R1 | P0 | **来源信任分级 + 强制溯源** | 每次写入带 source_class：`user_declaration` > `user_dialogue` > `agent_authored` > `tool_output/web`；记忆表强制 provenance 外键指向不可变事件；**来源确立权限而非真相**，高级别来源同样可被证据矛盾 | AMG source_class；Graphiti episodes；Epistemic Baggage | 【工程】+【概念】 |
| R2 | P0 | **证据层/派生层分离，append-only** | 原始事件（会话/工具输出/声明）不可变；候选事实、观察、反思都是派生记录，挂证据引用；整理/合并只产生新派生物，**永不改写或删除原始证据**；DELETE 一律实现为 tombstone | MIRIX-2.0 system of record + AutoDream；Hindsight exact quotes；Graphiti 失效不删除；Mem0 2026 ADD-only | 【论文】+【工程】 |
| R3 | P0 | **写闸门与防投毒边界** | 所有写入（含 agent 自写）过确定性检测器：注入模式、secrets/PII、保护键（声明/身份类记忆 agent 不可覆盖）、尺寸异常、自强化冷却；策略输出 allow/redact/**quarantine**/block；外部来源的规则类候选**永不自动晋升到高授权**，只作低权参考；quarantine 区与注入管线物理隔离 | AMG；MemSentry Accept/Review/Quarantine；LLM05:2026；PMPA/AgentPoison 威胁实测 | 【工程】（威胁为【论文】实测） |
| R4 | P0 | **NLI/决策模型冲突检测 + 双时间失效** | 候选 vs top-s 相似既有记忆→类型化判断（entail/contradict/neutral，带概率阈值，参考 Jev 0.8）；矛盾→旧事实置 invalid_at（superseded），保留历史；同刻互斥→双方 disputed；检测不确定→保守保留双方并标注，不静默覆盖 | Graphiti bi-temporal + Jev；Mem0 四操作；Memory-R1 学习策略 | 【论文】+【工程】 |
| R5 | P0 | **审计台账与可解释/回滚** | append-only 决策台账：每次晋升/失效/隔离记录触发证据、判断模型与版本、规则版本、置信变化；注入台账记录"哪条记忆进了哪个会话"（usage lineage）；定期快照，支持回滚到已知良好态；对外可回答"这条记忆为什么是 active" | AMG SecurityEvent+快照；Epistemic Baggage lineage；Hindsight webhooks；bmem 既有注入台账 | 【工程】+【概念】 |
| R6 | P1 | **自动晋升管线（probation→active）** | 新推断候选入 probation：不注入或仅低权注入；晋升条件（满足其一）：≥2 个**独立**来源印证；观察期 N 天无矛盾且被使用验证（编码场景：依此行动后构建/测试/lint 通过）；用户声明直接支持。晋升必须写入"认知晋升依据"（证据 ID 列表），无依据晋升=控制失效 | Hindsight proof count；Epistemic Baggage 晋升依据；Generative Agents 重要性 | 【综合】（组件为【论文/工程】） |
| R7 | P1 | **证据权重与多源印证** | confidence = f(来源信任级, 独立证据数, 证据新鲜度, 使用反馈)；**同源重复不累加 proof**（防自强化）；反思/摘要产物继承其证据的最低信任级并保留引证；矛盾证据→弱化而非覆盖 | Hindsight strengthen/weaken/extend；AMG SelfReinforcementDetector；Generative Agents 引证；Epistemic Baggage consolidation drift | 【工程】+【概念】+【综合】 |
| R8 | P1 | **声明协议（用户声明自主整合）** | 声明=最高信任级的**证据事件**，不是条目编辑：走同一抽取→冲突检测→仲裁管线；优先级规则：声明压倒 agent 推断（旧推断 superseded）；新声明压倒旧声明（版本化）；声明与强多源证据冲突→保留双方、disputed 标注、注入时以声明为工作假设并在台账记冲突；声明可带作用域（project/global）与失效条件；系统从不要求用户确认条目文本，只接受新声明 | Epistemic Baggage"批准不使断言更真"（声明授予的是**授权**不是真理）；Graphiti 时间失效；Mem0 用户/agent 事实同权（反向借鉴：声明应更高权） | 【综合】（本设计无直接现成实现） |
| R9 | P1 | **自动降级/隔离/撤回** | 矛盾证据累积→proof 弱化→disputed→quarantine（停止注入）；久未使用+低置信→衰减归档（recency decay / retire_if TTL）；确认污染→retract+tombstone+受影响派生物连带重算+快照回滚；降级全自动但**必须通知台账**（用户可事后查看，不参与事前批准） | Hindsight 弱化；Generative Agents 衰减；AMG retire_if/快照；DREAM 选择性遗忘 | 【工程】+【论文】+【综合】 |
| R10 | P1 | **后台 sleep-time 整理** | bmemd 前台只采集+注入；后台 worker（更强模型、低频、可配）执行：属性标注→去重合并→链接→反思蒸馏；重要性累积阈值触发反思（参考 150），反思产物=新派生记忆+证据引证；整理走 R3 写闸门与 R6 晋升管线；anytime 语义：整理不阻塞读取；整理顺序确定性可审计（防 MemForm Path Poisoning） | Letta 双 agent；MIRIX-2.0 AutoDream；Generative Agents reflection；MemInsight；MemForm 警示 | 【论文】+【工程】 |
| R11 | P2 | **失败降级（fail-closed 写，fail-open 读）** | LLM/检测器不可用：停止晋升与整理，只允许追加事件+注入既有 active；外部来源候选一律滞留 probation；冲突检测超时→disputed 不覆盖；声明的确定性优先级规则不依赖 LLM 仍可生效；整理任务失败→重试+死信队列，不阻塞前台；防御误隔离率纳入监控（参考 Price of Safety：确定性规则零误报，LLM 重排慎用） | Price of Safety 实测；AMG 确定性检测器；工程惯例 | 【工程】+【论文】成本数据 |
| R12 | P2 | **可信度-授权分离 + 后果闸门** | 记忆的"可信"与"可用于何种动作"是两个维度：高影响动作（删除文件、改配置、外发数据）不得仅凭记忆注入触发，需记忆/LLM 之外的独立策略校验（签名/规则）；agent_authored 的目标类写入受冷却与自强化检测；定期红队（含触发器探测）与"合法更新不被挡死"回归测试 | Trust propagation（Validator 被绕过但执行 0% 失守）；Self-Propagating Misalignment；Epistemic Baggage authority；LLM05 红队建议；MemForm/AMD-T02 僵化检查 | 【论文】+【概念】+【工程】 |

### 14.3 必须覆盖项核对与残余风险

| 要求覆盖项 | 对应推荐 |
|---|---|
| 来源信任分级 | R1 |
| 自动晋升/降级/隔离（probation/quarantine） | R6、R9（+R3 隔离区） |
| NLI/模型冲突检测 | R4 |
| 证据权重与多源印证 | R7 |
| 声明协议（自主整合而非直接写入） | R8 |
| 防投毒边界 | R3（+R12） |
| 审计与可解释 | R5 |
| 失败降级 | R11 |

**残余风险与诚实声明**：

1. **与行业指南的偏离**：OWASP LLM05:2026 对自动反馈循环明确建议 human oversight；MemSentry 对最高信任来源的危险写也升级人审。本项目按产品要求取消人审，以 R3/R12 的结构性闸门 + R5 审计回滚补偿——这是**有意识的风险接受**，不是行业验证过的等价替代。
2. **LLM 仲裁不可靠是实测结论**：现有系统对投毒与过时记忆均不完全鲁棒（Accept Changes/Reject Attacks、MemoryDojo）；反思会幻觉（Generative Agents embellishments）；LLM 审计员只能部分拦截自传播（71%→34%）。因此 R2"原始证据不可变+可重算"是最后防线。
3. **防御有良性成本**：读时 LLM 重排会无依据丢弃记忆（50 抽 2 有依据），应优先确定性规则并监控误隔离率（Price of Safety）。
4. **本文推荐中的【综合】项**（尤其 R6 晋升阈值、R8 声明协议）无现成论文验证，上线前应自建回归评测：含"合法更新必须被接受"与"注入必须被拒绝"双向用例（参考 ECA 与 AMD-T01/T02 思路）。

---

## 附：来源链接汇总

- Mem0：[arXiv:2504.19413](https://arxiv.org/abs/2504.19413) · [GitHub](https://github.com/mem0ai/mem0)
- Zep/Graphiti：[arXiv:2501.13956](https://arxiv.org/abs/2501.13956) · [GitHub](https://github.com/getzep/graphiti)
- A-MEM：[arXiv:2502.12110](https://arxiv.org/abs/2502.12110) · [GitHub](https://github.com/WujiangXu/A-mem-sys)
- Letta：[博客](https://www.letta.com/blog/sleep-time-compute) · [arXiv:2504.13171](https://arxiv.org/abs/2504.13171)
- Memory-R1：[arXiv:2508.19828](https://arxiv.org/abs/2508.19828)
- MemOS：[arXiv:2507.03724](https://arxiv.org/abs/2507.03724)
- MIRIX：[arXiv:2507.07957](https://arxiv.org/abs/2507.07957) · [MIRIX-2.0 arXiv:2611.12068](https://arxiv.org/abs/2611.12068) · [GitHub](https://github.com/Mirix-AI/MIRIX)
- Generative Agents：[arXiv:2304.03442](https://arxiv.org/abs/2304.03442)
- Hindsight：[文档](https://hindsight.vectorize.io) · [GitHub](https://github.com/vectorize-io/hindsight) · [arXiv:2512.12818](https://arxiv.org/abs/2512.12818)
- MemInsight：[arXiv:2503.21760](https://arxiv.org/abs/2503.21760)
- AgentPoison：[arXiv:2407.12784](https://arxiv.org/abs/2407.12784)
- OWASP：[LLM Top 10 2026](https://github.com/GenAI-Security-Project/GenAI-LLM-Top10) · [LLM05:2026](https://github.com/GenAI-Security-Project/GenAI-LLM-Top10/blob/main/2026/final/LLM05_DataModelPoisoning.md) · [Agent Memory Guard](https://github.com/OWASP/www-project-agent-memory-guard) · [Epistemic Baggage](https://github.com/OWASP/The-Agents-Epistemic-Baggage)
- 投毒攻防补充：[PMPA](https://arxiv.org/abs/2609.13889) · [MemSentry](https://arxiv.org/abs/2609.08747) · [MemForm](https://arxiv.org/abs/2610.14431) · [Self-Propagating Misalignment](https://arxiv.org/abs/2610.04083) · [Price of Safety](https://arxiv.org/abs/2609.22818) · [Accept Changes, Reject Attacks](https://arxiv.org/abs/2610.41056) · [MemoryDojo](https://arxiv.org/abs/2611.09721) · [DREAM](https://arxiv.org/abs/2610.13267) · [Trust propagation](https://arxiv.org/abs/2609.17648)
