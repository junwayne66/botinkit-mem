# 开源 Agent 记忆系统比较

2026-11-26，针对跨会话、跨来源的个人与项目记忆。五条路线回答了不同的问题。Mem0 展示了轻量事实提炼可以走多远，Graphiti 显式记录关系在何时成立，Hindsight 保留综合判断的依据，Letta 把可审阅的文件直接作为工作的上下文，LangMem 则把提炼时机与更新策略交给应用。选型时，先看一句旧话如何被纠正、怎样回到当时的原话，再看常规问答分数。

## 项目与版本

以下星数来自 GitHub 官方 REST 返回的 `stargazers_count`，查询响应时间为 2026-11-26 22:06–22:07（上海）。星数记录当时的关注度。活跃用户和检索质量需要其他证据。提交链接固定本次所读版本。LangMem 与 LangGraph、Letta 新旧仓的数字分别列出。

| 仓库 | 星数 | 仓库根许可 | 本次核对的代码 |
|---|---:|---|---|
| Mem0 | 69,503 | Apache-2.0 | Python 包 2.2.1，新提炼算法在代码里称 V3。[提交](https://github.com/mem0ai/mem0/commit/a330119bda245ad05bf1d5e6779fc588b5f19443) |
| Hindsight | 63,599 | MIT | hindsight-api-slim 1.3.0。[提交](https://github.com/vectorize-io/hindsight/commit/0aca9f80ec87237756c445ac27bf9e5affdc67fe) |
| LangGraph | 45,456 | MIT | 此处比较其长期 Store 协议，下行列出提炼 SDK。[提交](https://github.com/langchain-ai/langgraph/commit/3e3dbd42c5329dd404669ccd923c005c814bee3a) |
| Graphiti | 33,110 | Apache-2.0 | graphiti-core 0.32.1。[提交](https://github.com/getzep/graphiti/commit/c66d168a54b6c25b7522f0c6f6a625889d8c3603) |
| Letta 历史仓 | 25,560 | Apache-2.0 | 主分支指向当前代码，旧 V1 API Server 保存在 archive 分支。[交接说明](https://github.com/letta-ai/letta/blob/8816d3a8516893bfdc0fa14bfb1235226012e295/README.md) |
| Letta Code 当前仓 | 3,919 | Apache-2.0 | Agent harness、MemFS、本地或 Cloud 后端。[提交](https://github.com/letta-ai/letta-code/commit/5ea8ffcf6b5c6a38f8f3fb9a115631a052a0c0a6) |
| LangMem | 1,793 | MIT | 0.0.30。相同应用可更换写入策略与后端，适合做对照。[提交](https://github.com/langchain-ai/langmem/commit/4beb9c36a40953f09357e5b2dd62ecc726f22d13) |

## 一条记忆怎样流转

| 项目 | 采集和提炼 | 存储单元 | 检索与注入 | 更正、淘汰与长期优化 |
|---|---|---|---|---|
| Mem0 OSS | 应用对带 user、agent 或 run 范围的消息调用 `add`。默认事实路径读相近旧记忆与近几条消息，单次模型调用提出新增事实，按文本哈希去重。可通过 `infer=False` 显式写入 | 带作用域和元数据的向量记录、实体索引，以及保存历史的 SQLite | 语义候选按词法、实体信号加分，可选二次重排。后端和 NLP 依赖决定信号可用性 | 自动提炼追加事实。应用可显式更新、删除或设置截止日期。连续的相反声明仍需判定哪一条当前有效。[写入路径](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/memory/main.py#L881-L1054) · [迁移说明](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/docs/migration/oss-v2-to-v3.mdx) |
| Graphiti | 应用提交具有参考时间的 episode，提取实体和关系，再根据已有图进行归并、矛盾识别 | 事件节点、实体、带来源 episode ID 和有效期的关系边，使用可替换图后端 | 基础 `search` 返回关系边，默认对 BM25 与向量排名作 RRF。高级配方可选择图遍历、其他重排和对象类型 | 新证据可使旧边失效，保留当时的历史。删除来源有单独入口。持续写入能更新图，实体消歧仍会出错。[数据模型](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/edges.py#L229-L255) · [基础搜索](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/graphiti.py#L2316-L2375) · [配方](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/search/search_config_recipes.py#L110-L126) |
| Hindsight | 调 `retain` 摄入文档或对话，按用途选原片段或模型提炼。事实区分智能体自身经历与外部世界，后台跨事实归纳 observation | memory bank 内保留文档、事实、实体和观察依据，可自托管，例如 PostgreSQL + pgvector | `recall` 融合语义、关键词、图、时间候选，按配置及时间约束启用相关通道。可重排并按 token 预算裁切。`reflect` 用记忆回答问题 | 删除来源后，受影响的 observation 作废，其他来源可参与下轮重建。后台有积压时应看归纳延时及重算费用。[写入与检索](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/README.md#L275-L356) · [存储后端](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/README.md#L416-L425) · [融合实现](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/fusion.py#L29-L100) · [来源删除](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-docs/docs/developer/observations.mdx#L312-L333) |
| Letta Code | Harness 保存历史消息。智能体整理有长期价值的内容，可配置按步数或上下文压缩触发后台 dreaming，复查近期对话 | Git 支撑的 MemFS。root 布局中根层 Markdown 放进上下文，子目录放细节、索引与技能 | 当前对话读核心内容。查细节时按文件、索引或会话搜索。如需持久摘要，智能体可以改写文件 | 合并、拆分或重写文件可从版本差异审阅。删除当前文件内容后，已有 Git 历史仍须另行处理。后台复核会消耗额外模型 token。[当前实现说明](https://github.com/letta-ai/letta-code/blob/5ea8ffcf6b5c6a38f8f3fb9a115631a052a0c0a6/README.md#L15-L45) · [root 布局的提示](https://github.com/letta-ai/letta-code/blob/5ea8ffcf6b5c6a38f8f3fb9a115631a052a0c0a6/src/agent/prompts/letta_root_memfs.md#L10-L47) · [Dreaming 配置](https://docs.letta.com/configuration/memory) |
| LangMem + LangGraph | 应用提交会话和已有状态，可在交互内写，也可把工作排给 ReflectionExecutor。用 Pydantic 定义事实、案例等结构，配置提炼模型和允许的操作 | LangMem 核心可独立使用。有状态的管理器把记录按 namespace 和 key 写入 LangGraph Store。持久性取决于后端 | 可按 key、过滤条件及可用的语义索引检索。应用决定何时把结果放入上下文 | 普通 `create_memory_manager` 默认允许新增、修改，关闭删除。Store 是否支持 TTL 取决于后端。可用反馈优化提示词，应另测旧任务是否退化。[提炼接口](https://github.com/langchain-ai/langmem/blob/4beb9c36a40953f09357e5b2dd62ecc726f22d13/src/langmem/knowledge/extraction.py#L536-L570) · [Store 协议](https://github.com/langchain-ai/langgraph/blob/3e3dbd42c5329dd404669ccd923c005c814bee3a/libs/checkpoint/langgraph/store/base/__init__.py#L708-L730) · [写入与反馈](https://github.com/langchain-ai/langmem/blob/4beb9c36a40953f09357e5b2dd62ecc726f22d13/docs/docs/concepts/conceptual_guide.md#L303-L352) |

## 会改变设计取舍的细节

**相同的“混合检索”可以拥有不同的召回上限。** Mem0 Python 的普通搜索从语义结果生成候选。BM25 与实体对已有候选加分，语义分过低的记录先被过滤。Hindsight 的 RRF 逐个遍历参与融合的候选列表，关键词列表独有的记录也可进入合并池，实际能否排进最终上下文还取决于候选上限、重排、预算。对项目代号、工单号和中文专名，建议记录证据最早出现在哪一步，并单看“只在关键词列表出现”的子集。[Mem0 候选构造](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/memory/main.py#L1642-L1701) · [Mem0 打分](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/utils/scoring.py#L60-L119) · [Hindsight 合并候选](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/fusion.py#L29-L106)

**小型复核。** 用 Python 直接调用上述固定提交的原始 `score_and_rank` 函数，送入语义候选 `semantic=0.8, below=0.09`、词法得分 `keyword-only=1, below=1`、空实体得分和 `top_k=3`。阈值 0.1 时返回 `[semantic]`，降为 0.0 后返回 `[below, semantic]`。两次均与断言相符。这个试验覆盖打分行为。它不涉及分词、向量库、端到端问题准确率或服务延迟。可用下载的原文件执行下列断言。[原始函数](https://raw.githubusercontent.com/mem0ai/mem0/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/utils/scoring.py)

```python
import runpy

rank = runpy.run_path("scoring.py")["score_and_rank"]
candidates = [{"id": "semantic", "score": 0.8}, {"id": "below", "score": 0.09}]
keywords = {"keyword-only": 1.0, "below": 1.0}
for threshold, expected in [(0.1, ["semantic"]), (0.0, ["below", "semantic"])]:
    rows = rank(candidates, keywords, {}, threshold=threshold, top_k=3)
    assert [row["id"] for row in rows] == expected
```

**过期、矛盾和删除回答三个不同的问题。** 以“五月起由乙负责，八月才补录”为例，Graphiti 的 `invalid_at` 可以表达甲的负责人关系在五月结束，`expired_at` 表示系统何时将其标记失效。其基础搜索未传过滤器时使用空的 `SearchFilters`，有效期条件默认空。应用需要分别验证“六月实际负责人”“六月系统当时知道什么”“现在负责人”的查询语义。Mem0 OSS 则可指定 `expiration_date` 从默认列表和搜索隐藏数据，调用方仍可选择显示过期记录。按访问频度衰减属于另一个能力，OSS 的 `project.update(decay=True)` 会抛错。若用户要求擦除，验收对象应包括原文、向量、派生摘要、缓存和各自承诺的备份处理范围。[Graphiti 时间字段](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/edges.py#L229-L255) · [默认时间过滤器](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/search/search_filters.py#L55-L68) · [Mem0 过期过滤与衰减开关](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/memory/main.py#L427-L484)

**已存原文可能无法通过问答找回。** Hindsight 文档记录了一个反例。提炼规则过窄时，`retain` 可以成功且产生零条事实。按文档 ID 仍能定位原文并重处理，但 `recall`、`reflect` 都搜不到该文档。应用可以在采集队列统计“总文档数、零事实文档数”，对零事实抽样判断是合理舍弃还是关键遗漏。如选择增加受权限控制的原文检索通道，应同时验证已撤回文档不会沿该通道再次出现。[零事实情形](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-docs/docs/developer/retain.md#L297-L323)

**结构校验、出处和事实裁定各有用途。** LangMem 的 schema 能约束输出字段，Graphiti 的边能指回若干 episode，Hindsight 能从观察回查支持事实。上述机制都还需要检验“原话是否支持所声称的结论”。例如“一次任务请用英文回复”可以成功填入语言偏好字段，来源甚至完全正确，但把它提升到全局偏好会作用于错误范围。归纳层宜保留说话者、原句位置、适用对象与条件，抽检从结论回到原文，再问范围和时效是否相符。[LangMem 提炼](https://github.com/langchain-ai/langmem/blob/4beb9c36a40953f09357e5b2dd62ecc726f22d13/src/langmem/knowledge/extraction.py#L536-L575) · [Graphiti 来源边](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/edges.py#L229-L243) · [Hindsight 归纳依据](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-docs/docs/developer/observations.mdx#L312-L333)

**项目名称也会掩盖版本变化。** Mem0 旧算法由模型决定 ADD/UPDATE/DELETE，当前 Python 包的事实提炼路径已改为追加。旧方法描述甚至仍出现在该函数的 docstring，因此本次以迁移说明和执行体交叉确认。原先的图存储接口已从 OSS 移至平台，OSS 的实体加分仍在。Letta V1 的 archival memory 文档可解释早期方案，评估当前自托管路线应看 Letta Code 的本地后端和 MemFS。Graphiti 由应用自建采集、权限与运维，Zep 的托管服务有自己的系统边界。这三种版本或产品混用都会高估现成能力。[Mem0 提炼实现](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/memory/main.py#L930-L989) · [Mem0 OSS 迁移](https://github.com/mem0ai/mem0/blob/a330119bda245ad05bf1d5e6779fc588b5f19443/docs/migration/oss-v2-to-v3.mdx#L312-L349) · [Letta 迁移](https://github.com/letta-ai/letta/blob/8816d3a8516893bfdc0fa14bfb1235226012e295/README.md) · [Graphiti 和 Zep](https://github.com/getzep/graphiti/blob/c66d168a54b6c25b7522f0c6f6a625889d8c3603/README.md#graphiti-and-zep)

## 本项目可先比较的三种做法

| 成本由什么触发 | 对照实验 | 何种结果支持采用 |
|---|---|---|
| 为每次查询增加召回通道 | 相同语料、候选总数和注入 token 数下，对照纯词法、纯向量、语义池加词法分、两池并集 RRF。按中文专名、精确 ID、改写、否定分别记结果 | 并集找回原方案漏掉的来源，最终证据命中和答案准确率一起提升，额外延迟在预算内。只提高中间召回时优先排查裁剪 |
| 为后补/更正维护时间字段 | 对现有事实关联“何时生效”“系统何时获得证据”，与按最新写入优先对照。测试迟到记录、先后冲突、同名不同对象 | 当前和历史两类提问均有改善。若简单的作用域和时间过滤已答对，优先保留它。关系链仍频繁失败时再试时态图 |
| 为长期归纳增加模型调用 | 用同一批有依据的显式更正，对照原始证据检索、后台压缩、带来源的可重算归纳，随后撤回其中一条来源 | 在等量注入预算下，后续任务的遗漏变少，撤回后不再引用旧结论。另记后台排队时间、每条有效归纳的调用成本，以及对无关记忆的误改率 |

## 检索与核对记录

本次为按架构差异选样的工程调研。2026-11-26 用公开网页搜索以下关键词组合，随后沿项目维护者的官方链接查阅 GitHub README、迁移指南、源码和 API。对版本敏感的行为直接读取上述固定提交的原始文件。星数取 GitHub REST 的仓库元数据，项目变动后数值会变化。未部署五套系统进行统一性能测试，也未以项目方排行榜推断效果。

| 检索词组合 | 来源和阅读层级 | 复核重点与界限 |
|---|---|---|
| `Mem0 open source add extract search delete graph` | 官方 OSS 迁移指南全文、固定提交的 main.py、scoring.py 与衰减开关测试 | 对照默认入口、旧 docstring、实际提炼与排序代码。仅对排序函数执行合成输入 |
| `Graphiti temporal knowledge graph hybrid search invalidation episodes` | 官方 README、edges.py、search_filters.py、graphiti.py、search_config_recipes.py | 核基本查询和数据模型，图后端在实际负载上的性能未测 |
| `Hindsight retain recall reflect consolidation observations delete documents` | 官方 README、retain/observations 文档、检索和融合源码 | 追溯零事实、RRF、撤回来源后的归纳。删除传播结论限于文档所述观察层 |
| `Letta memory blocks archival memory sleep time agents`，续查 `Letta MemFS root local git memory dreaming` | 旧仓交接说明、当前仓 README、root MemFS 提示与官方机制说明 | 将两个仓的星数、产品代际、本地与 Cloud 能力分别核对。提示文件说明期望行为 |
| `LangMem hot path background namespace prompt optimizer` | 官方概念指南、提炼 SDK、LangGraph Store 接口 | 核对两个长期存储构件如何组合、默认删除开关、TTL 的后端条件，调优收益需任务评测 |
