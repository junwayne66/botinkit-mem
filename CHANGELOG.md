# 变更日志

本文件记录 Botinkit Memory（`bmem`）的显著变更，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。日期为对应版本完成验收的日期。

## [Unreleased]

### 变更

- 修复技术设计指纹失配：新增 `docs/v2/reviews/technical/current.sha256` 作为 21 个技术文件的当前可校验基线；明确 `round-{1,2,3}.sha256` 为评审时点的不可变证据（自哈希封印保持不变），并同步更新评审索引、验证记录与贡献指南中的校验说明。
- 确立 Apache-2.0 许可证，新增 `LICENSE`、`CONTRIBUTING.md` 与本变更日志。
- 完成 V1（Python/FastAPI 实现）退役后的仓库整理：移除 V1 源码、旧规格（`specs/001–003`）、旧设计文档与本地构建产物，V1 历史可在提交 `ae2c06b` 中查阅。
- 重写 `.gitignore` 与 `.gitattributes` 以匹配 Rust 项目，覆盖 `target/`、Python/Node 缓存、本地数据库与密钥文件。
- 重写 `README.md`：补齐功能特性、技术栈、快速开始、命令速览、项目结构、开发指南、文档导航、迁移说明与路线图。

## [0.2.0] - 2026-11-19

V2 原生重建版本：从空库起步，以单一 Rust 程序替代 V1 的 Python 服务，首发目标为 macOS、Windows、Linux 三平台。核心流程 T001–T013、工作台改版 T017–T019、历史同步与本机切换 T020–T022 已完成本地验收（见 `specs/004-bmem-v2/validation.md`）；T014–T016 的签名发行与完整发布验收仍在进行。

### 新增

- 原生 `bmem` 程序：Rust 2024 单包，CLI、回环 HTTP 服务、后台 worker 与 MCP 服务端合为一个可执行文件，Web 工作台在编译期内嵌（`include_str!`），无前端构建步骤。
- 本地工作区 `~/.bmem` 与持久定位机制：`init` / `status` / `doctor` / `web` / `service run|start|stop|status`，服务仅监听回环地址并带会话授权。
- 本人声明全链路：自然语言 `declare`、逐维修订与纠正、历史与撤回、跨助手词法装入及装入回执观察。
- 来源采集与历史回填：Codex、Claude Code、DSH 会话文件与已授权飞书会话，持久游标断点续传、暂停/恢复与手工 JSON 批次导入。
- 自动提炼：获准片段经 OpenAI 兼容 API 或本机 Ollama 生成带原文定位的记忆候选，支持自动裁决、失败重试与服务恢复后的补提炼。
- 模型与权限治理：模型登记、探针测试、提炼/嵌入用途绑定；采集、提炼、嵌入按来源 × 用途分别授权，授权前可预览模型接收端。
- 检索：SQLite FTS5 词法检索为默认；向量相似检索默认关闭，显式开启后支持服务器 Embedding 或本机 Ollama（如 `bge-m3`）。
- 三档压缩（`data` / `balanced` / `high`）同时作用于磁盘编码与模型上下文，原文可逐字回查。
- 受管备份与恢复预览（`backup create` / `restore preview`）、来源级与单条撤回（`forget`，预览—确认两步）。
- Web 工作台：总览、记忆星图与关系社区、后台作业分页、来源/导入/检索/设置页面，支持窄屏布局、键盘操作与减少动态效果。
- 集成测试体系：`tests/v2` 的 Python `unittest` 端到端用例、Playwright 真实浏览器用例与双宿主（Claude Code / Codex）实测脚本，测试夹具集中在 `tests/v2/data`。

### 安全

- 密钥仅以引用保存于独立设置库，配置回显、重试记录与日志中不出现明文。
- 所有外部模型调用在数据库事务外执行；权限检查以当前授权版本为准，撤回变化会拒绝旧快照恢复。

### 已知限制

- Windows 本机运行/安装/升级、macOS x64、Linux x64、Windows arm64 的成品与签名发行尚未完成验收。
- 受信旧备份的完整恢复执行、独立撤回清单与全量导出管理仍在补齐。
- 生产模型路由校准、真实材料抽取准确率与三档全库体积/交接准确率的配对测试待补。

## [0.1.0] - 2026-11-13

首个单机版本，基于 Python + FastAPI 实现（已在 0.2.0 中退役，源码保留于 git 历史 `ae2c06b` 及之前提交）。

### 新增

- 单机个人记忆服务与 `bmem` 命令行入口，SQLite 存储与 MCP 接入。
- 飞书（Lark）记忆连接器与飞书历史采集。
- 可追溯的记忆图谱关系、按生效时间仲裁可信事实。
- 提炼批次原子提交、输出边界控制、生成失败诊断与嵌入输入限制的有界恢复。

[Unreleased]: https://github.com/junwayne66/botinkit-mem/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/junwayne66/botinkit-mem/releases/tag/v0.2.0
[0.1.0]: https://github.com/junwayne66/botinkit-mem/releases/tag/v0.1.0
