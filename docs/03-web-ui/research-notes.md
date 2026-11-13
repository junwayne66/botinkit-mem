# bmem web · UI 设计参考调研

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-12 |
| 目的 | 为 `bmem web`（127.0.0.1:8790 本地记忆仓储 dashboard）收集视觉与交互参考 |
| 方法 | `web_fetch` 抓取官方 README / 文档 / GitHub API 定位官方截图 → `curl` 下载 → `file` 验证 |
| 素材 | 8 张官方公开截图，原来源链接见[附录](#附录公开素材来源) |
| 范围 | 界面设计分析与公开素材来源整理 |

> bmem 的关键界面需求：记忆条目浏览/检索、来源与版本追溯、生命周期状态可视化（proposed / active / disputed / superseded / retracted）、注入台账、导入覆盖率。以下调研均围绕这五点展开。

---

## 0. TL;DR

| 项目 | 一句话定位 | 对 bmem web 的最大价值 |
|---|---|---|
| Letta (MemGPT) ADE | 有状态 Agent 的无代码开发/观测环境 | 记忆块编辑器 + 版本历史；"Web UI 连本地服务"的同构架构 |
| Hindsight | 会学习的 Agent 记忆系统（facts/experiences/observations） | 证据链与 proof count 展示；记忆图谱；召回策略分解 |
| Langfuse | AI 可观测性平台（trace/eval/dashboard） | **注入台账 = trace 视图**；指标 dashboard 范式 |
| Mem0 | 记忆层平台（user/session/agent 三级） | 记忆操作 activity feed；图谱记忆视图 |
| claude-mem | 编码 Agent 的本地记忆 worker + viewer | 与 bmem 场景最像：会话/观察/注入的 IA、渐进式检索 |
| Zep / Graphiti | 时间知识图谱框架（事实带生效窗口） | 生命周期/版本可视化：事实失效不删除、时间滑杆、溯源 |
| Khoj | 个人 AI 第二大脑（chat + 语义搜索） | 个人工具的侧边栏 IA、统一搜索入口、来源过滤 |

**三条最高优先级借鉴**：① Langfuse 式 trace 呈现注入台账；② Graphiti 式时间图谱 + 生命周期着色；③ Letta 式三栏记忆检查器（列表 / 详情 / 版本与来源）。

---

## 1. Letta (MemGPT) — ADE (Agent Development Environment)

> 仓库已迁移：主仓库 README 现指向 `letta-ai/letta-code`（CLI/桌面形态）；ADE 截图取自旧版 tag `0.6.27` 的官方 README。另有官方开源桌面 UI `letta-ai/letta-oss-ui`（含记忆视图截图）。

[Letta ADE 暗色截图](https://raw.githubusercontent.com/letta-ai/letta/0.6.27/assets/example_ade_screenshot.png)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | Agent 列表与服务器切换、对话流、**记忆块（persona / human 等）内容**、工具调用过程、Agent 配置（模型/工具/记忆） |
| 布局模式 | 三栏：左侧 agent/服务器导航 → 中间对话 → 右侧记忆与工具检查器；ADE 可连本地服务器（localhost:8283）或云端，**与 bmem web 连 bmemd 完全同构** |
| 配色与字体 | 官方提供暗色/亮色双主题截图（本图为暗色版）；开发者工具风格，等宽字体呈现代码与工具调用 |
| standout 交互 | ① 记忆块**可直接在 UI 编辑**，Agent 也会通过工具调用（`core_memory_replace` 等）自改记忆，改动在消息流中可见；② 多 Agent 共享记忆块；③ letta-oss-ui 的 Memory 视图：左侧记忆文件列表 + 右侧渲染，**每次修改存版本，History 可回滚**，违规修改自动撤销并说明原因 |
| 对 bmem web 的可借鉴点 | 记忆详情 = 可编辑块 + 版本历史 + 回滚；把"Agent 写入/纠正记忆"作为一等事件显示在时间线；本地服务连接状态面板（服务器地址/健康度） |

**来源**：[letta-ai/letta（0.6.27 README）](https://github.com/letta-ai/letta/blob/0.6.27/README.md) · [letta-ai/letta-oss-ui](https://github.com/letta-ai/letta-oss-ui) · [docs.letta.com](https://docs.letta.com) · [ADE 介绍博客](https://www.letta.com/blog/introducing-the-agent-development-environment)

---

## 2. Hindsight — 记忆银行 + 图谱 UI

> 自托管 UI 在 `localhost:9999`（API 在 8888），云端版 ui.hindsight.vectorize.io。记忆分 world facts / experiences / observations / mental models，按 bank 隔离。

[Hindsight 记忆图谱（官方文档配图）](https://raw.githubusercontent.com/vectorize-io/hindsight/main/hindsight-docs/static/img/memory-graph.jpg)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | bank 内记忆条目、实体/关系图谱（见官方文档配图，同目录有配套演示视频 memory-graph.mp4）、mental model "pages"（wiki 式活文档）、recall/reflect 结果 |
| 布局模式 | bank 切换器 + 列表/图谱双视图；pages 按文件夹组织（wiki 隐喻） |
| 配色与字体 | 官方文档素材含明暗两套 leaderboard 图，产品 UI 走现代 SaaS 风格；图谱以节点-边网络呈现 |
| standout 交互 | ① **Observations 带证据链**：每条聚合信念保留支持引文（exact quotes）与 proof count，新证据是"强化/弱化/扩展"而非覆盖；② recall 四路并行（语义/BM25/图/时间）+ RRF 融合 + 重排，过程可解释；③ 时间查询（"What happened in June?"） |
| 对 bmem web 的可借鉴点 | fact 详情页的**证据面板**（引文 + 支持/反对计数）与 disputed 状态天然契合；召回结果按策略分组展示命中原因；"纠正=更新证据权重"的可视化叙事 |

**来源**：[vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) · [hindsight.vectorize.io](https://hindsight.vectorize.io) · [Observations 文档](https://hindsight.vectorize.io/developer/observations)

---

## 3. Langfuse — Trace 瀑布 + 指标 Dashboard

> 开源 AI 工程平台：trace 可观测、prompt 管理、evals、自定义 dashboard。README 官方 trace 截图见下方来源链接。

[Langfuse trace 详情视图](https://github.com/user-attachments/assets/8f6995b7-285f-441a-b52f-1331d13ceb45)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | trace 列表 → trace 详情：**span 树/瀑布图**（LLM 调用、检索、agent 动作逐级展开），每个 span 的 input/output、token、成本、延迟；sessions 聚合；dashboards（延迟/成本/质量） |
| 布局模式 | 左侧项目导航 + 数据密集表格列表 + 右侧/下钻详情面板；"列表 → 详情 → span 级 input/output"三级下钻 |
| 配色与字体 | 明暗双主题；克制的中性底色 + 语义色标注（错误红、成功绿、类型徽章）；等宽字体展示 JSON payload |
| standout 交互 | ① trace 瀑布图按时间轴展开嵌套 span，一眼看出耗时与调用结构；② 从 trace 一键跳 playground 复现；③ score/反馈挂在 trace 上（人工标注、LLM-as-judge）；④ 自定义 dashboard + 指标 API |
| 对 bmem web 的可借鉴点 | **注入台账直接套用 trace 模型**：一次注入 = 一条 trace，span = 召回→过滤→排序→截断→注入，每步记录候选记忆 ID、token 预算、命中原因；会话 = session 聚合；用户纠正 = score 信号 |

**来源**：[langfuse/langfuse](https://github.com/langfuse/langfuse) · [Observability 文档](https://langfuse.com/docs/observability/overview) · [Metrics 文档](https://langfuse.com/docs/metrics/overview) · [在线 Demo](https://langfuse.com/docs/demo)

---

## 4. Mem0 — 平台 Dashboard + 图谱记忆

> 记忆层平台：user / session / agent 三级记忆；自托管 server 自带 dashboard（localhost:3000），云平台 app.mem0.ai。官方仓库 `docs/images/platform/` 提供平台截图。

[Mem0 平台 Activity 视图](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/images/platform/activity.png)

[Mem0 图谱记忆视图](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/images/graph-platform.png)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | Activity 视图：**记忆操作事件流**（ADD/UPDATE/DELETE 等）与请求分析；记忆列表按 user/agent/run 过滤；Graph Memory：实体节点 + 关系边的图谱视图；API key 管理 |
| 布局模式 | 平台式左导航（Memories / Activity / Graph / API Keys / Settings）+ 主区表格/图谱；筛选器按 user_id / agent_id / run_id 三级作用域 |
| 配色与字体 | 简洁 SaaS 风格，事件类型用彩色徽章区分；图谱视图节点按实体着色 |
| standout 交互 | ① 每条记忆可追溯触发它的对话与操作事件（ADD/UPDATE/DELETE 事件链）；② 图谱记忆把扁平记忆升级为实体关系网络；③ v3 算法：ADD-only 抽取 + 实体链接 + 多信号检索（语义/BM25/实体）+ 时间推理 |
| 对 bmem web 的可借鉴点 | dashboard 首页 = **记忆操作 activity feed**（新增/更新/取代/撤回徽章 + 来源链接）；三级作用域筛选器对应 bmem 的"来源标签"（项目/设备/Agent 只是标签而非分区）；实体图谱作为检索增强视图 |

**来源**：[mem0ai/mem0](https://github.com/mem0ai/mem0) · [docs.mem0.ai](https://docs.mem0.ai) · [自托管 dashboard 文档](https://docs.mem0.ai/open-source/overview) · [平台](https://app.mem0.ai)

---

## 5. claude-mem — 本地 Viewer + My Work

> **与 bmem 场景最接近**：hooks 采集编码 Agent（Claude Code / Codex / DeepSeek Harness 等）活动 → 本地 worker（Bun）+ SQLite/FTS5 + Chroma → observer 生成 observations → 下次会话注入。自带 Web viewer（`/viewer`：memory / sessions / settings）与 My Work（`/`：实时请求、进度、review 队列）。

[claude-mem 官方预览（viewer 动图）](https://raw.githubusercontent.com/thedotmack/claude-mem/main/docs/public/cm-preview.gif)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | observations 列表（带类型：decision / bugfix / security_alert / sensitive 等）、按会话分组的历史、注入内容预览、设置（注入策略/来源开关）、My Work 实时工作流 |
| 布局模式 | 本地 worker 内嵌 Web UI：侧边导航 + 列表 + 详情；每条 observation 有可引用 ID，可从 API/CLI 直达 viewer 对应条目 |
| 配色与字体 | 开发者工具审美（官方预览为动图，细节建议打开 GIF 查看）；信息密度高、面向"扫读" |
| standout 交互 | ① **三步渐进检索**：search 拿紧凑索引（~50-100 token/条）→ timeline 看时间上下文 → get_observations 只取选中详情（~500-1000 token/条）；② 会话启动注入分层索引，header 显示每层 token 成本；③ `<private>` 标签脱敏；④ 跨 harness 来源开关（"Include all sources at session start"） |
| 对 bmem web 的可借鉴点 | IA 直接参考：记忆 / 会话 / 注入 / 设置四区；检索 UI 做"索引行 → 时间线 → 详情"渐进展开并显示 token 成本；注入预览面板（本次会话将注入什么、各层多少 token）；来源（harness）过滤器 |

**来源**：[thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) · [docs.claude-mem.ai](https://docs.claude-mem.ai/) · [Progressive Disclosure](https://docs.claude-mem.ai/progressive-disclosure) · [My Work 指南](https://docs.claude-mem.ai/usage/my-work)

---

## 6. Zep / Graphiti — 时间知识图谱

> Graphiti 是构建**时间上下文图谱**的开源框架（Zep 的图谱引擎）：实体=节点，事实=带生效窗口的边，episode=原始溯源数据。官方 README 动图展示图谱随时间演化。

[Graphiti 时间图谱演化解说（官方动图）](https://raw.githubusercontent.com/getzep/graphiti/main/images/graphiti-graph-intro.gif)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | 实体-关系图谱动画：新 episode 注入 → 抽取实体/事实 → 旧事实被失效（invalidated）而非删除；Zep 商业版 dashboard 提供图谱可视化、debug 日志、API 日志 |
| 布局模式 | 画布式图谱（节点-边网络）+ 时间维度；开发侧配合 Neo4j/FalkorDB 浏览器查看 |
| 配色与字体 | 官方动图为 1920×1080 演示风格；图谱以颜色区分实体类型与边状态 |
| standout 交互 | ① **双时间轴（bi-temporal）**：每条事实有 valid_at / invalid_at，可查"现在什么是真的"与"某时刻什么是真的"；② 矛盾处理=自动失效旧事实并保留历史；③ 全链路溯源：任何实体/边 → 产生它的 episodes |
| 对 bmem web 的可借鉴点 | **与 bmem 生命周期语义几乎一一对应**：active=有效窗口内、superseded=被新事实失效、retracted=撤回；图谱视图加**时间滑杆**回看历史状态；superseded 边灰显+虚线、点击看取代链；fact → episode → 原始会话的三级溯源跳转 |

**来源**：[getzep/graphiti](https://github.com/getzep/graphiti) · [论文 arXiv:2501.13956](https://arxiv.org/abs/2501.13956) · [help.getzep.com/graphiti](https://help.getzep.com/graphiti) · [getzep.com](https://www.getzep.com/)

---

## 7. Khoj — 个人第二大脑 Web App

> 个人 AI 应用：chat + 个人文档语义搜索 + agents + 自动化。Web/Obsidian/Emacs/桌面多端，开源自托管。

[Khoj Web 首页](https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/assets/img/khoj_web_app_home.png)

| 维度 | 观察 |
|---|---|
| 界面展示什么 | 对话首页：历史会话侧边栏、聊天主区、搜索模式（带文件/来源过滤器）、agents 管理页、自动化（定时研究/通知）配置 |
| 布局模式 | 经典个人工具布局：可折叠左 sidebar（历史 + 功能入口）+ 中央内容区；chat 与 search 双模式切换 |
| 配色与字体 | 亲和的消费级风格（非开发者黑话），明暗主题；圆角卡片、留白充足 |
| standout 交互 | ① 聊天内引用个人文档并给出来源；② 搜索按文件类型/来源过滤；③ agents=自定义知识+人设+工具；④ 自动化任务（个人 newsletter） |
| 对 bmem web 的可借鉴点 | "记忆属于人"的产品气质：bmem web 首页可以有一个**自然语言检索框**（问"我上次为什么决定 X？"）而非只有表格；来源过滤器（按 harness/项目/时间）；后台提炼任务可视化为"自动化"卡片 |

**来源**：[khoj-ai/khoj](https://github.com/khoj-ai/khoj) · [docs.khoj.dev](https://docs.khoj.dev) · [app.khoj.dev](https://app.khoj.dev)

---

## 8. 可选补充：Open WebUI / AnythingLLM

> 本轮未下载其截图（8 张配额已满），仅基于官方 README 记录与记忆/知识管理相关的设计点。LobeChat 未在本轮深入抓取，留待后续。

| 项目 | 相关界面/机制 | 对 bmem web 的启发 | 来源 |
|---|---|---|---|
| Open WebUI | 自托管 ChatGPT 式 UI；Knowledge Base（工作区文档集合）+ 离线优先；官方演示 GIF（cdn.openwebui.com/open-webui-demo.gif） | 知识集合的"工作区"组织方式；离线可用的本地工具定位与 bmem 一致 | [open-webui/open-webui](https://github.com/open-webui/open-webui) · [docs.openwebui.com](https://docs.openwebui.com/) |
| AnythingLLM | workspace + 文档拖拽上传与来源引用；**Automatic & User Managed Memories**（自动+手动记忆）；True Dark 等自定义主题 | "自动记忆 vs 用户手动记忆"双通道在 UI 上分开呈现（对应 bmem 的 Hooks 自动通路 + MCP 主动通路）；主题系统 | [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) · [Memories 文档](https://docs.anythingllm.com/features/memories) |

---

## 9. 横向对比表

| 项目 | 视觉风格 | 核心可视化 | 布局范式 | 对 bmem web 的借鉴点 |
|---|---|---|---|---|
| Letta ADE | 暗/亮双主题、开发者工具 | 记忆块编辑器、消息流中的记忆修改 | 三栏（导航/对话/检查器） | 记忆编辑+版本回滚；本地服务连接面板 |
| Hindsight | 现代 SaaS + 图谱 | 记忆图谱、证据链/proof count | bank 切换 + 列表/图谱/wiki | fact 证据面板；召回策略分解 |
| Langfuse | 中性底色、数据密集表格 | **trace 瀑布图**、指标 dashboard | 列表→详情→span 三级下钻 | 注入台账=trace；纠正=score |
| Mem0 | 简洁 SaaS、彩色事件徽章 | activity feed、实体图谱 | 平台左导航 + 作用域筛选 | 操作事件流首页；来源标签筛选 |
| claude-mem | 开发者审美、高信息密度 | 会话时间线、渐进式检索 | 侧边导航 + 列表 + 详情 | 整体 IA；索引→时间线→详情；token 成本显示 |
| Graphiti/Zep | 演示动画/图谱画布 | **时间图谱**、事实生效窗口 | 画布 + 时间轴 | 生命周期着色；时间滑杆；取代链溯源 |
| Khoj | 消费级亲和、圆角卡片 | 聊天+搜索双模式 | 可折叠 sidebar + 中央内容 | 自然语言检索入口；来源过滤 |
| Open WebUI | ChatGPT 式 | 知识库工作区 | sidebar + chat | 离线本地工具气质 |
| AnythingLLM | 可定制主题（True Dark） | workspace 文档+引用 | workspace 侧栏 | 自动/手动记忆双通道分区 |

---

## 10. 借鉴清单（按对 bmem web 的价值排序）

| # | 建议 | 参考 | 落地要点 |
|---|---|---|---|
| 1 | **注入台账做成 Langfuse 式 trace 视图** | Langfuse | 每次注入=一条 trace：span 依次为召回→状态过滤→排序→token 截断→注入；记录候选/命中记忆 ID、各步耗时与 token 数；按会话（session）聚合；支持从台账反查记忆详情 |
| 2 | **生命周期状态机全局着色 + 时间滑杆图谱** | Graphiti | 五态统一色板：proposed=琥珀、active=绿、disputed=红、superseded=灰（虚线边）、retracted=深灰删除线；图谱/列表/详情共用；时间滑杆回看"某时刻的记忆状态"，superseded 显示取代链 |
| 3 | **三栏记忆检查器：列表 / 详情 / 版本与来源** | Letta ADE + letta-oss-ui | 详情含正文、适用条件、版本 diff、来源链（harness→会话→episode 原文）；版本可回滚（回滚本身记为新版本）；编辑走 proposed→审核流 |
| 4 | **首页 = 记忆操作 activity feed + 指标卡** | Mem0 + Langfuse | 事件流（新增/更新/取代/撤回/注入，彩色徽章+来源）；指标卡：记忆总数、active 占比、本周注入次数、token 节省估计、disputed 待处理数 |
| 5 | **fact 证据面板：引文 + 支持/反对计数** | Hindsight | 每条 fact/decision 挂证据列表（原文引文+出处+时间）；disputed 状态并排展示冲突证据；"纠正"操作=追加反证并触发状态流转，而非静默覆盖 |
| 6 | **渐进式检索 UI：索引行→时间线→详情** | claude-mem | 搜索结果默认紧凑行（类型/摘要/来源/时间，标注 token 量）；点击展开时间线上下文；再展开正文与版本；显示本次检索的 token 成本 |
| 7 | **导入覆盖率 dashboard** | Langfuse dashboards | 按来源（Claude Code / Codex / DSH / 手动）显示：已导入会话数/估算总数、覆盖率进度条、最近同步时间、失败项；一键重跑导入 |
| 8 | **来源徽章与三级溯源跳转** | Graphiti + Mem0 | 所有记忆条目常驻来源 chips（harness 图标+项目+设备+时间）；点击逐层下钻：记忆→episode→原始会话证据（内容哈希寻址） |
| 9 | **暗色优先 + 数据密集表格 + 等宽 ID** | Langfuse + claude-mem | 暗色为默认（炫酷但克制）：中性深底、语义色只用于状态/类型；ID/版本/hash 用等宽字体；表格支持排序/筛选/批量操作；提供亮色切换 |
| 10 | **注入预览面板（会话启动前可见）** | claude-mem | 展示"下次会话将注入什么"：分层索引、每层 token 成本、来源开关（是否含全部 harness）；对应 bmem 双通路（Hooks 自动 / MCP 主动）分别可视化 |
| 11 | **自然语言检索入口 + 召回解释** | Khoj + Hindsight | 首页顶部问答式搜索框（"我为什么决定用 SQLite？"）；结果标注命中策略（关键词/向量/图/时间）与适用条件匹配原因 |
| 12 | **bmemd 服务状态栏** | Letta 服务器面板 + Hindsight monitoring | 顶栏常驻：服务健康、SQLite 大小/WAL、索引重建进度、后台提炼队列深度、模型端点可用性（本地 Ollama 优先状态） |

**落地节奏建议**：v0.1 先做 #1 #3 #4 #9（台账+检查器+首页+暗色骨架），#2 #5 #8 紧随其后（生命周期与溯源是 bmem 的差异化卖点），#6 #7 #10–12 迭代补齐。

---

## 附录：公开素材来源

> 以下链接指向各项目官方仓库或官方 README 的公开截图，素材版权归原作者所有。

| 公开来源 | 内容 | 尺寸 | 大小 |
|---|---|---|---|
| [Letta ADE](https://raw.githubusercontent.com/letta-ai/letta/0.6.27/assets/example_ade_screenshot.png) | 暗色主界面 | 3088×2240 | 1.1 MB |
| [Hindsight](https://raw.githubusercontent.com/vectorize-io/hindsight/main/hindsight-docs/static/img/memory-graph.jpg) | 记忆图谱（官方文档配图） | 964×720 | 73 KB |
| [Langfuse](https://github.com/user-attachments/assets/8f6995b7-285f-441a-b52f-1331d13ceb45) | trace 详情（README 官方示例） | 1600×833 | 421 KB |
| [Mem0 Activity](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/images/platform/activity.png) | 平台 Activity 视图 | 3164×1908 | 1.1 MB |
| [Mem0 Graph](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/images/graph-platform.png) | 图谱记忆视图 | 1154×850 | 72 KB |
| [claude-mem](https://raw.githubusercontent.com/thedotmack/claude-mem/main/docs/public/cm-preview.gif) | viewer 官方预览动图 | 640×360 | 2.1 MB |
| [Graphiti](https://raw.githubusercontent.com/getzep/graphiti/main/images/graphiti-graph-intro.gif) | 时间图谱演化官方动图 | 1920×1080 | 169 KB |
| [Khoj](https://raw.githubusercontent.com/khoj-ai/khoj/master/documentation/assets/img/khoj_web_app_home.png) | Web 应用首页 | 1284×1229 | 214 KB |

**验证**：每张均经 `file` 确认为真实图片（PNG/JPEG/GIF）且 >10 KB。

**失败与排除项**：

| 项 | 情况 | 处理 |
|---|---|---|
| docs.letta.com/ade/overview | 404（文档重构，ADE 页并入 v1-sdk/legacy 且为客户端渲染，无静态图） | 改用 0.6.27 tag README 中的官方 ADE 截图直链，成功 |
| letta-ai/letta 主分支 README | 已无截图（项目迁移至 letta-code） | 用历史 tag + letta-oss-ui 补充 |
| hindsight-list-view.png（guides 目录） | 下载后发现为 1200×628 博客封面尺寸，非 UI 截图 | 已删除，替换为 letta-ade.png |
| grep.app 代码搜索 | 429 限流 | 改用 GitHub API + raw 文件直接核对 |

**备注**：两张 GIF 为官方动画预览（静态帧即界面截图）；hindsight-memory-graph.jpg 为官方文档静态配图。本环境模型不支持读图，界面细节描述基于官方 README/文档的文字说明，配色与字体的像素级核对建议人工打开图片确认。
