# Tasks — 一期个人记忆中心

**Input** [spec.md](spec.md)、[plan.md](plan.md)、[contracts/interfaces.md](contracts/interfaces.md)

## Phase 1 Setup

- [x] T001 盘点现有文档与资产并创建规格及设计于 specs/001-personal-memory/。
- [x] T002 创建 Python 包及锁定依赖于 pyproject.toml、src/bmem/__init__.py、uv.lock。

## Phase 2 Foundational

- [x] T003 冻结入口 DTO、错误和脱敏工具于 src/bmem/models.py。
- [x] T004 建立 SQLite schema、短事务与事件/outbox 原子写入于 src/bmem/schema.sql、src/bmem/store.py。

## Phase 3 US1 跨工具共享

独立验收为两个 Harness 原生回调完成声明、召回与纠正。

- [x] T005 [US1] 实现立即声明、事实键、条件和 CAS 版本于 src/bmem/store.py。
- [x] T006 [US1] 实现 CJK FTS、来源展开及预算召回于 src/bmem/store.py。
- [x] T007 [P] [US1] 实现 HTTP API、七个 MCP 工具与服务生命周期于 src/bmem/api.py。
- [x] T008 [US1] 实现命令及 daemon 入口于 src/bmem/cli.py。
- [x] T009 [P] [US1] 探测并实现三个 Harness 配置、回调与部分接入报告于 src/bmem/adapters.py。
- [x] T010 [US1] Codex 执行两个真实 Harness 自动共享功能测试于 tests/test_interfaces.py。

## Phase 4 US2 历史归集

独立验收为三类历史重复导入和注入引用识别。

- [x] T011 [US2] 实现注入去重、标记及防回灌于 src/bmem/store.py。
- [x] T012 [P] [US2] 实现 JSONL/Zstd/Markdown 导入、清单与游标于 src/bmem/adapters.py。
- [x] T013 [US2] 实现持久任务恢复、退避与重试于 src/bmem/store.py、src/bmem/runtime.py。
- [x] T014 [US2] Codex 验证重复导入、覆盖率和 Worker 追赶于 tests/test_acceptance.py。

## Phase 5 US3 自主治理

独立验收为 T3 晋升、T4 隔离、纠正传播、遗忘及恢复。

- [x] T015 [US3] 实现证据根去重、信任策略、冲突和仲裁台账于 src/bmem/store.py。
- [x] T016 [US3] 实现本地结构化蒸馏与候选整合于 src/bmem/runtime.py。
- [x] T017 [US3] 实现晋升、冷藏、每日整理及每周降权于 src/bmem/store.py、src/bmem/runtime.py。
- [x] T018 [US3] 实现派生传播、隐私清除、tombstone 和一致性备份恢复于 src/bmem/store.py。
- [x] T019 [US3] Codex 通过公开入口验证 CAS、治理、私密边界及恢复于 tests/test_acceptance.py。

## Phase 6 US4 模型与管理台

独立验收为真实模型检索和七页真实数据操作。

- [x] T020 [P] [US4] 实现 Ollama 发现/托管下载、版本锁、健康、安装进度与系统服务于 src/bmem/runtime.py。
- [x] T021 [US4] 实现 bge-m3、sqlite-vec、语义开关与存量回填于 src/bmem/runtime.py、src/bmem/store.py。
- [x] T022 [P] [US4] 将现有概念稿适配七页真实 API、状态及可访问交互于 src/bmem/static/。
- [x] T023 [US4] Codex 验证真实模型安装与五组中文改写并记录于 specs/001-personal-memory/validation.md。
- [x] T024 [US4] Codex 验证桌面/375px、键盘、减少动效和七页功能于 specs/001-personal-memory/validation.md。

## Phase 7 Review and Convergence

- [x] T025 Claude 分组审查全部业务源文件，记录发现与覆盖于 specs/001-personal-memory/validation.md。
- [x] T026 DSH 修复审查和功能测试发现，Codex 重跑回归于 tests/。
- [x] T027 Codex 执行打包、安装及全功能回归，更新实际使用说明于 README.md、specs/001-personal-memory/validation.md。

## Dependencies and Parallel Execution

T001–T004 是基础。契约固定后，A 处理 store/models/schema，B 处理 api/cli，C 处理 adapters，三路并行。
B/C 可先按契约实现，等待 A 后联调。第二波 D 处理 runtime，E 处理 static，Codex 同时编写并执行功能用例。
同一源文件只交给一个执行器，后续修复串行。代码自检结果、Claude 审查与 Codex 实测分别记录。

## Implementation Strategy

先完成 M1 可运行闭环，再补齐 M2 归集、M3 治理、M4 语义和 UI。每个故事独立测试后继续下一里程碑。
本轮用户已授权连续实施一期，任务未验证前保持未完成。发现缺口由 converge 追加后续任务。

## Phase 8: Convergence

- [x] T028 修复完整运行时分发包、损坏检测、官方模型下载恢复及固定摘要校验，并验证真实推理 per FR-015、SC-004。
- [x] T029 修复真实会话复述与派生产物证据计数、Worker 独占恢复及模型长期故障后的任务重试 per FR-009、FR-013、SC-003。
- [x] T030 将每日整理接到幂等汇总事件与既有蒸馏管线，验证来源、私密继承和周降权 per FR-020。已实现至多八条 active episode 的幂等汇总、private 继承、method/decision 动态 schema、有效来源检查及固定 derived_from，Codex 真实 Worker 验收通过。
- [x] T031 修复 DSH 宿主上下文接入和采集/导入身份一致性，完成两个真实 Harness 与管理台交互验收 per FR-007、FR-017、SC-001、SC-005。

## Phase 9: Convergence

- [x] T032 修复真实 qwen3:4b 将提问、确认语及召回复述误当独立事实的问题，保持普通独立观察、自由文本纠正与每日整理有效，并用新隔离目录完成 Claude→DSH→Claude 自动纠正全链路及队列排空验收 per FR-009、FR-011、SC-001、SC-003。
- [x] T033 修复注入引用的仲裁 confidence 误写 token_count，验证引用仍不增加独立证据且台账置信度在合法范围 per FR-010、FR-017。

## Phase 10: Convergence

- [x] T034 为本地助手陈述判断提供消息发生前最近用户输入与实际注入的版本上下文，复用事件、注入台账及 revisions 只读查询，避免确认语和改写复述成为新证据，验证历史版本、时间截止、新独立观察与真实跨 Harness 链路 per FR-006、FR-009、SC-001、SC-003。

## Phase 11: Convergence

- [x] T035 统一模型提示、目标 schema 与修订快照的八条已有记忆上限，拒绝未展示目标，并以模型边界回归验证 per FR-004、FR-011、SC-006。

## Phase 12: Convergence

- [x] T036 对模型 NOOP 从同一目标快照绑定规范正文、类型、条件和事实键，以原始事件保留观察证据，验证不同措辞的独立印证能计数及晋升 per FR-009、FR-010、SC-003。

## Phase 13: Convergence

- [x] T037 校正混合检索中缺失通道的权重，使新增关键词命中不降低语义得分，回归双候选排序并以同一真实八条语料验证目标正文及直接来源，保留原记录 ID 的诊断结果 per FR-005、SC-004、SC-006。

## Phase 14 Local Deployment

- [x] T038 核验并清空本项目隔离验收的测试记忆库，将已校验本地模型迁入持久用户数据目录，安装用户级 CLI 与常驻服务，验证进程重启恢复 per FR-015、FR-019。
- [x] T039 补齐历史发现和增量补扫，保留原生私密标签，按实际数据规模导入三个 Harness 的历史与已有记忆，记录覆盖、错误和沉淀进度 per FR-008、FR-013、FR-014。历史已接收，已有整理记忆可用，完整会话提炼在本地后台继续执行。
- [x] T040 在本机真实配置接通 Claude Code、Codex 和 DSH 自动召回及采集，使用宿主正式信任接口验证，完成变更审查、回归和部署记录 per FR-006、FR-007。

## Phase 15 Repository Preparation

- [x] T041 将 Python 源码与测试规范为 src/bmem、tests，更新打包与规格路径，补全公开 README、整理发布文件并完成回归与 wheel 隔离安装验收，覆盖用户 2026-11-13 仓库整理与推送要求。
