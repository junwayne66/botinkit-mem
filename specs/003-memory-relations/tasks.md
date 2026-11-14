# Tasks · 持续更新的事实与真实记忆星图

## Setup

- [x] T001 读取现有实现与统计，完成 specs/003-memory-relations/spec.md 和 plan.md 的最小方案。

## 第一切片 · 真实关系与详情

- [x] T002 [US2] 在 tests/test_graph.py 添加真实关系、修订、事件详情和隐私回归。
- [x] T003 [US2] 在 src/bmem/store.py 投影派生、共享来源和最近版本替代的真实证据。
- [x] T004 [US2] 在 src/bmem/api.py 暴露带目标边界的只读事件详情及图谱。
- [x] T005 [US2] 在 src/bmem/static/app.js、app.css 复用星图和抽屉，关联端点优先、筛选后截取、节点与边查看详情。
- [x] T006 [US3] 在 src/bmem/store.py 修正自定义根的间接引用遍历，并在 tests/test_recompute.py 覆盖。
- [x] T007 在 specs/003-memory-relations/validation.md 记录第一切片结果，交独立review后本地提交与部署。

## 第二切片 · 当前事实时间

- [x] T008 [US1] 在 tests/ 增补同一范围的新旧事实、显式目标、迟到旧资料、确认时间和可信度回归。
- [x] T009 [US1] 在 src/bmem/models.py、schema.sql、store.py 实现事实时间、兼容读取和统一更新规则。
- [x] T010 [US1] 在 src/bmem/runtime.py、api.py、cli.py 与现有界面入口传递事实时间和来源。
- [x] T011 [US1] 验证旧备份及原有调用，完成独立review后本地提交部署并更新 specs/003-memory-relations/validation.md。

## 运行评估

- [x] T012 [US3] 对现有 src/bmem/runtime.py、store.py 与队列做只读分类，报告实际写入与任务计数，独立采样由指定观察方负责。

## Dependencies and validation

T002→T003→T004→T005→T007，T006纳入第一切片。第一切片先交付review，第二切片再按T008至T011推进。本轮同文件由当前会话顺序修改。任务完成、独立审查与实际测试分别记录。

## Phase 1 · Convergence

- [x] T013 按FR-005在tests/test_fact_time.py覆盖同时间相反内容，并在store.py保留未消歧冲突，允许用户显式版本纠正（partial）。
- [x] T014 按FR-002在tests/test_fact_time.py覆盖重算候选与首条证据日期不同的情况，在store.py采用重算候选明确时间，缺省保留当前水位（partial）。
- [x] T015 按独立审查及FR-003至FR-006，在tests/test_fact_time.py复现较新可信事实接受后旧争议继续召回，修复同键精确条件的争议状态与审计，覆盖声明、候选、同值确认和较旧/低可信输入边界。
- [x] T016 按运行失败诊断在Runtime.embed加入HTTP413的有界前缀恢复，保持结果顺序与调用时限，在tests/test_embedding_limits.py用合成HTTP及真实Store/Worker验证，整理失败分类、重试幂等性和差异供独立审查。
