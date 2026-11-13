# Tasks — 飞书/Lark记忆连接

## Phase 1 Setup

- [x] T001 核对官方安装、认证、历史协议并固定规格契约于 specs/002-feishu-memory/。

## Phase 2 Foundation

- [x] T002 为显式导入的外部资料增加原子引用快照入口与回归于 src/bmem/store.py、tests/test_feishu.py。

## Phase 3 US1 一键连接

独立验收为安装、品牌/profile复用、授权挑战、权限检查与失败恢复。

- [x] T003 [P] [US1] DSH实现官方工具托管安装、配置和授权编排于 src/bmem/feishu.py。
- [x] T004 [US1] Codex验证协议替身及真实官方CLI的help/状态解析于 tests/test_feishu_auth.py。

## Phase 4 US2 历史范围

独立验收为八种来源组合、分页、重复、部分失败和来源隐私。

- [x] T005 [P] [US2] DSH实现只读scope、消息和文档历史、游标和去重于 src/bmem/feishu_history.py。
- [x] T006 [US2] Codex编写并执行真实导入器协议边界测试于 tests/test_feishu_history.py。

## Phase 5 US3 双端配置

独立验收为真实CLI与HTTP共享配置、浏览器向导与状态恢复。

- [x] T007 [P] [US3] DSH实现来源页向导、认证URL/二维码、多选和设置摘要于 src/bmem/static/。
- [x] T008 [US3] Codex接入单例管理器、HTTP与周期同步生命周期于 src/bmem/api.py。
- [x] T009 [US3] Codex扩展connect别名和命令行交互/非交互配置于 src/bmem/cli.py。
- [x] T010 [US3] Codex通过API、CLI和浏览器验收双端流程于 tests/test_feishu.py。

## Phase 6 Validation

- [x] T011 完成Claude第三方窄审、全套回归与wheel隔离安装，记录 specs/002-feishu-memory/validation.md。
- [x] T012 补充使用说明并按规格收敛剩余工作于 README.md、specs/002-feishu-memory/tasks.md。

## Dependencies and Parallelism

T003/T005/T007依据冻结契约并行。T002与T008/T009由Codex完成，T010在三模块集成后执行。每个执行器仅写指定文件，完成后同步任务状态。T011/T012在公开入口验收后完成。
