# bmem V2 开发台账

## 准备与共享基础

- [x] T001 在 `src/v2/bmem/Cargo.toml` 装配原生程序和开发检查
- [x] T002 [P] 在 `src/v2/bmem/src/platform.rs` 实现持久定位、同根复用、停机诊断及监听发现
- [x] T003 在 `src/v2/bmem/src/{main,client,server,error}.rs` 合流回环、身份验证、网页会话和 CLI

## US1 CLI 与 Web 设置

- [x] T004 [P] [US1] 在 `src/v2/bmem/src/settings.rs` 实现配置保存、CAS、凭据引用与重放
- [x] T005 [P] [US1] 在 `src/v2/web` 实现真实设置、声明、历史和健康页面
- [x] T006 [P] [US1] 在 `tests/v2` 验证双向配置、冲突、权限、重启与数据根不分裂

## US2 声明、检索和宿主

- [x] T007 [P] [US2] 在 `src/v2/bmem/src/authority.rs` 实现声明证据、逐维修订、历史及撤回
- [x] T008 [P] [US2] 在 `src/v2/bmem/src/context.rs` 实现授权、快照、投递与结果观察
- [x] T009 [P] [US2] 在 `src/v2/bmem/src/host.rs` 实现宿主协议和归一化，按实测验证真实双宿主
- [x] T010 [US2] 在 `src/v2/bmem/src` 交付授权词法、任务接手与双宿主端到端

## US3 来源和自动提炼

- [x] T011 [P] [US3] 在 `src/v2/bmem/src/ingestion.rs` 实现源、游标、收件、重试和撤回
- [x] T012 [P] [US3] 在 `src/v2/bmem/src/providers.rs` 实现模型探针与真实端点提炼
- [x] T013 [US3] 在 `src/v2/bmem/src` 合流候选裁决、来源证据和恢复后补提炼，在 `tests/v2` 覆盖

## US4 运营及发布验证

- [ ] T014 [US4] 在 `src/v2/bmem/src` 实现受控备份恢复、治理清理、三档和可选向量
- [ ] T015 [US4] 在 `src/v2/bmem` 交付平台安装、签名、升级、卸载及目标验证
- [ ] T016 在 `tests/v2` 完成整体真实性验收并在 `specs/004-bmem-v2/validation.md` 记录证据与缺口

## 验收状态

T001–T013 已完成本地核心流程并经独立复验。T014–T016 继续记录受控恢复、完整发布和质量门槛。三平台签名成品、真实目标路由和压缩质量证据见[验证记录](validation.md)。

## 所有者工作台改版

- [x] T017 新增总览、关系图与作业分页的有界 SQLite 查询。
- [x] T018 重构 `src/v2/web` 的导航、图谱、图表、记忆、来源、导入、检索和配置交互。
- [x] T019 在隔离实例用 Chrome 和原生 CLI 验证真实读写、草稿冲突、超时重放、动画和窄屏操作。

## V2 历史同步和本机切换

- [x] T020 接入 Codex、Claude Code、DSH 会话文件与飞书已授权会话，保存续读进度。
- [x] T021 在 CLI 和 Web 提供同步控制、进度与提炼候选原文，并分开调度提炼与嵌入。
- [x] T022 在 macOS 以 launchd 接管 V2 后台运行，切换命令及宿主配置，并归档 V1 数据后退役旧程序。
