# 组件选型与复用边界

首版沿用 Rust 服务、SQLite 权威库与已有宿主适配器。建议新增的运行时依赖只有处理 IANA 地区时区的 chrono-tz。第二批先测现有词法和可选向量通道的并集，再根据失败题决定是否换算法或索引。下表版本来自本仓锁文件或所链接的上游快照。新增组件均为设计选项，产品在真实工作负载上的收益待实施验证。[本仓 Cargo.lock](../../src/v2/bmem/Cargo.lock) · [七项产品验收](../PRD-memory-upgrade.md)

## 直接复用

| 功能 | 组件、许可与运行边界 | 具体接法与校验 |
|---|---|---|
| 采集、修订、有效期、撤权和回执 | Rust 进程内 rusqlite 0.38.0（MIT）、libsqlite3-sys 0.36.0（MIT），锁定依赖所带 SQLite 3.51.1（公有领域） | 在既有事务中写权威记录及来源依赖。给既有库运行升级迁移，重放重复源版本，故障重试后验证版本数、当前声明与审计记录。索引可由授权有效的根记录重建。[本仓依赖](../../src/v2/bmem/Cargo.toml) · [rusqlite](https://docs.rs/rusqlite/0.38.0/rusqlite/) · [SQLite 许可](https://sqlite.org/copyright.html) |
| 错误码、命令和原句检索 | 同一 SQLite 进程内的 FTS5。现有 recall_fts 已用 unicode61 | 沿用词项生成，新增含标点标识符的原文精确校验。FTS5 的 bm25 依赖索引全库的文档数和平均长度，数值越小越优。仅在索引语料等于读者获准语料时直接使用该排名。共享索引供受限读者查匹配时，先限定可见域，再以原词命中、命中词项数和稳定 ID 排候选，最后融合通道名次。用加入大量不可读文档的对照核验名次。[现有检索](../../src/v2/bmem/src/retrieval.rs) · [FTS5 排名定义](https://sqlite.org/fts5.html#the_bm25_function) |
| “这个项目到周五结束” | 既有 chrono 0.4.45，建议增加 Rust 进程内 chrono-tz 0.10.4，两者均为 MIT 或 Apache-2.0 | 用入口获得的地区时区和声明参考时间确定当地截止日。“周五结束”取当地周六起点作为排他上界。持久化原始时间表达、地区和解析出的 UTC 边界。当地时间有两个映射或无映射时要求补充具体时间，约定保持待确认。升级时区数据后用固定样本比较 UTC 边界，保留已生效修订的解释。[chrono 地区时间结果](https://docs.rs/chrono/0.4.45/chrono/offset/enum.LocalResult.html) · [chrono-tz 及 IANA 版本常量](https://docs.rs/chrono-tz/0.10.4/chrono_tz/) |
| Harness、HTTP、MCP 与模型接入 | axum 0.8.9（MIT）、rmcp 3.11.0（Apache-2.0）、reqwest 0.12.28（MIT 或 Apache-2.0），均为既有 Rust 进程内依赖 | 各传输适配器调用同一用例。沿用当前 provider 的授权、端点与模型配置。本机宿主验证装入，网络模型故障时保留已采集版本和待处理状态。依赖升级后重放 CLI／HTTP／MCP 对同一读者和任务的结果。[扩展契约](../../docs/v2/technical/extension-contract.md) · [provider 实现](../../src/v2/bmem/src/providers.rs) · [rmcp 文档](https://docs.rs/rmcp/3.11.0/rmcp/) |
| 作用域、时效和冲突回归 | TimlMemBench 固定提交 447b195，离线评测。代码和测试 MIT，数据、文档、提示、配置及 schema 为 CC BY 4.0 | 保留样本 ID、使用的版本、署名与改动说明。将“应取／应避”标签映射为本机断言，在开发集选择策略后做时间留出。宿主真实输入、旧句柄撤权与权限外标题泄露仍需本项目夹具。旧 Python 回放仅作历史参照，候选版本应从实际 Rust 入口跑。[项目及协议](https://github.com/lyirs/TimlMemBench/tree/447b195344a2feaf995176446379e60632831f94) · [分项许可](https://raw.githubusercontent.com/lyirs/TimlMemBench/447b195344a2feaf995176446379e60632831f94/LICENSE) · [本仓评测协议](../reassessment-2026-11/evaluation.md) |

## 有条件接入的外部组件

遇到下一个社区项目，可按能力界面决定直接包装、按算法复现，或先把整套系统当独立基准。下列组件无需作为一个整体采用。[接入决策与 A-MEM 自进化实例](community-integration.md) · [Hindsight 的可运行对照](replay-example.md)

| 候选与许可 | 接口与运行代价 | 何时启用与怎样退出 |
|---|---|---|
| sqlite-vec v0.1.6，MIT 或 Apache-2.0。固定提交 639fca5 | SQLite C 扩展，提供 Rust 绑定。可先在现有已授权候选上用标量 vec_distance_cosine 对比现有 Rust 余弦，再独立评估是否维护 vec0 派生表。静态注册涉及各发布平台的链接和扩展初始化 | 仅当同一语料与机器上，向量阶段占据明显延迟且现行查询 p95 超出预登记预算时做试验。比较召回、耗时、包体和撤回后可见性。各支持平台可构建、旧库可打开后才纳入发布。可切回现有向量实现。[当前向量搜索](../../src/v2/bmem/src/vector.rs) · [固定版本与许可](https://github.com/asg017/sqlite-vec/tree/639fca5739fe056fdc98f3d539c4cd79328d7dc7) · [Rust 注册入口](https://raw.githubusercontent.com/asg017/sqlite-vec/639fca5739fe056fdc98f3d539c4cd79328d7dc7/bindings/rust/src/lib.rs) |
| LangMem 0.0.30，MIT。固定提交 4beb9c3 | Python、LangChain 与模型依赖。包装 create_memory_manager，为已准许提炼的片段和目标 schema 返回结构化候选。适配器补上来源版本和原句位置，交回 Rust 裁定 | 当已标注的漏提炼样本集中在现行方案不能表达的跨轮条件时，与现有 provider 盲比。要求能回指原句、识别转述及范围，统计零候选率、错升长期率、模型费用。独立进程可关闭，保留原文与原提炼策略。[提炼器接口](https://raw.githubusercontent.com/langchain-ai/langmem/4beb9c36a40953f09357e5b2dd62ecc726f22d13/src/langmem/knowledge/extraction.py) · [版本与许可](https://github.com/langchain-ai/langmem/tree/4beb9c36a40953f09357e5b2dd62ecc726f22d13) |
| Graphiti 0.32.1，Apache-2.0。固定提交 c66d168 | Python 服务及图数据库。用有参考时间的 episode 构建派生图，边带来源 episode、有效和失效时间。适配器把每跳映射回本地证据句柄，由服务核验当前读者能否读取路径上每项依据 | 仅对平面检索稳定漏掉且能标注每跳的题族启用。须证明可以在读者获准范围内展开候选，验证一条边撤回及分支仍可用，再比较增益与维护成本。不能表达所需访问范围时只用隔离的合成语料。移除投影后主检索仍可用。[时态关系模型](https://raw.githubusercontent.com/getzep/graphiti/c66d168a54b6c25b7522f0c6f6a625889d8c3603/graphiti_core/edges.py) · [运行依赖与许可](https://github.com/getzep/graphiti/tree/c66d168a54b6c25b7522f0c6f6a625889d8c3603) |

## 借鉴算法与对照组

| 来源与快照 | 可借鉴的实现 | 本项目采纳标准 |
|---|---|---|
| Hindsight 1.3.0，MIT，Python。固定提交 0aca9f8 | reciprocal_rank_fusion 对各排序表从 1 开始计名次，用 Σ 1/(k + rank) 累计同一 ID 的得分。上游默认 k=60。可在 Rust 中写这个小公式，使用规范化后的证据句柄，记录通道与名次。单通道内部先去重，分数相同用稳定句柄打破并列 | 开发集先比精确／词法、向量及两路并集，再比相同阅读预算下的 RRF 顺序。报告补齐的任务与被挤掉的关键句。向量停用时仍返回词法与精确结果。仅借鉴融合函数，整套 Python 服务的上线维护没有首版收益依据。[融合源码](https://raw.githubusercontent.com/vectorize-io/hindsight/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/fusion.py) · [版本与许可](https://github.com/vectorize-io/hindsight/tree/0aca9f80ec87237756c445ac27bf9e5affdc67fe) |
| Mem0 OSS 2.2.1，Apache-2.0，Python。固定提交 a330119 | 读其词法／实体加分和时间相关评分，用作旧记录降权的离线消融。将降权限制在默认主动装入，对“查旧决定”保留定向历史查询 | 分别核对旧而仍有效的项目约定、已过期的例外、被撤回的来源。只在主动推荐题收益明确时采用排序信号。生效、访问与遗忘始终由权威规则决定，可回退整个排序版本。[排序实现](https://raw.githubusercontent.com/mem0ai/mem0/a330119bda245ad05bf1d5e6779fc588b5f19443/mem0/utils/scoring.py) · [版本与许可](https://github.com/mem0ai/mem0/tree/a330119bda245ad05bf1d5e6779fc588b5f19443) |

## 组件更换的验收约定

| 变更 | 升级时核对 | 回退支点 |
|---|---|---|
| Rust crate、SQLite、时区数据 | 记录锁文件与时区数据版本，用带既有修订、旧回执和待提炼版本的库副本跑迁移与读取。单测含夏令时重复／缺失、当地跨日和已到期例外 | 保留迁移前副本，已发布权威修订按当时解析边界读取 |
| 模型、提炼器或检索器 | 记录模型、提示、schema、索引及策略版本。固定读者、时间、阅读预算。仅将具有相应用途授权的样本进入回放，按任务族报告新增成功、退化、尾延迟及成本 | 切回上一个策略版本，旧结果按当前授权复查，已追加的用户纠正继续生效 |
| 引入带存储的外部进程 | 对每个派生对象建立到根证据的依赖。演练单根撤回、共享依据仍有效、超时重试和整个索引重建，验证旧读者句柄与新会话实际装入 | 禁用该读通道或提炼器，用权威数据重新派生。对已发副本、服务内派生物和备份分别记录可证明的清理范围 |

版本和许可由依赖清单、上游文档和固定提交核对。上文的外部适配器、跨平台构建、时区迁移和质量阈值属于实施验收项，本轮未部署或测量。[前期工程核查](../landscape-2026-11-26/engineering.md) · [开源项目的逐项代码证据](../landscape-2026-11-26/open-source.md)
