# Implementation Plan — 飞书/Lark记忆连接

**Date** 2026-11-13

**Spec** [spec.md](spec.md)

## Technical Context

复用 Python/FastAPI、现有 Store 与原生 JS 管理台，不引入新的常驻服务或前端依赖。连接工具为官方 lark-cli，托管安装在用户数据目录，凭据由官方 CLI 与系统 Keychain 管理。

## Constitution Check

统一记忆权威不变。外部内容保持 T4，默认private，Store 在入口脱敏并原子写事件与outbox。模型调用继续位于数据库事务之外。来源、游标、配置持久化，授权挑战只驻内存。全流程受本地服务回环边界保护。显式授权导入的原文作为T4/episode引用快照可检索，不作为active规则，复用Store原子写入及现有reconcile=false任务跳过路径。验证使用隔离配置、协议替身与公开真实 CLI help，私人业务读取由实际使用者在向导选择后触发。

## Project Structure

- `src/bmem/feishu.py` 连接配置、官方工具安装、profile选择、认证子进程、状态和同步编排。
- `src/bmem/feishu_history.py` 最小只读scope、聊天和文档分页、正文转换、来源ID、增量游标。
- `src/bmem/store.py` 内部reference_episode入口与既有事务/outbox复用。
- `src/bmem/api.py` 单例连接管理器、HTTP契约及后台周期同步生命周期。
- `src/bmem/cli.py` 扩展connect别名及feishu/lark配置命令，复用本地HTTP服务。
- `src/bmem/static/` 来源页连接向导、设置摘要，复用既有组件和样式。
- `tests/test_feishu.py` 公开API/CLI与真实导入器的协议边界回归。

## Execution Ownership

DSH A 管理feishu.py与安装认证测试，DSH B管理feishu_history.py与历史导入测试，DSH C管理三份static文件。Codex管理API、CLI、规格、集成测试、浏览器验收和文档。模型使用已授权Ark经济型号，关闭思考，分包限制读取。Claude用现有第三方模型、工具关闭的代码片段窄审。

## State and Background Work

Web和CLI共享服务端配置。耗时操作由管理器拥有的后台线程及子进程执行。保存范围后才请求对应历史。连接或同步正在执行时返回当前状态，服务退出终止自己的认证进程。周期同步仅处理已配置、已启用、有范围的连接，失败显示可恢复状态。

## Verification

先验证安装/认证状态、八种范围组合、正确分页、scope拒绝、重放去重与部分失败继续，再验证真实API/CLI和浏览器布局。全套原有92项回归、Python/JS语法、wheel资源检查继续执行。官方CLI及真实用户认证与读取分别记录，不把协议替身通过当作用户账号已接通。
