# Botinkit Memory

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Rust Edition](https://img.shields.io/badge/rust-2024-orange.svg)](src/v2/bmem/Cargo.toml)
[![Status](https://img.shields.io/badge/status-P0%20%E5%8F%AF%E7%94%A8%2F%E5%8F%91%E5%B8%83%E9%AA%8C%E8%AF%81%E4%B8%AD-yellow.svg)](specs/004-bmem-v2/validation.md)

> 面向个人工作站的本地长期记忆服务。一个原生程序 `bmem`，同时提供命令行与 Web 工作台，让 Codex、Claude Code、DeepSeek Harness（DSH）等编码助手与已授权的飞书账号共享同一份可追溯、可撤回、可恢复的个人记忆。

Botinkit Memory 在本机用户目录（默认 `~/.bmem`）维护一份个人记忆库：它从各助手的会话与工具回执中采集资料，对**明确获准**的内容调用你自己配置的模型提炼为带原文出处的记忆，并在后续任务中把当前声明与相关证据投递给获准助手。所有数据默认留在本机，外发范围逐项授权、可预览、可撤回。

## 功能特性

- **单一原生程序**：Rust 编写，CLI、本地 HTTP 服务、后台 worker 与内嵌 Web 工作台编译为同一个可执行文件，无外部运行时依赖（SQLite 经 rusqlite bundled 静态编入）。
- **本人声明与跨助手装入**：用自然语言追加或纠正偏好声明，获准助手在下一次可控调用中即可拿到新值，不依赖模型停机或索引重建；逐维度保留修订历史与原话证据。
- **自动提炼、出处可查**：普通对话和工具回执经获准模型提炼为记忆候选，自动裁决或人工确认，每条记忆都可回查原文片段与来源。
- **多来源采集**：支持 Codex、Claude Code、DSH 会话文件（Hook / MCP / 插件接入）以及经官方 `lark-cli` 授权的飞书会话，带持久游标断点续传。
- **权限分级与隐私治理**：采集、模型提炼、向量嵌入按来源 × 用途分别授权；模型接收端变更需重新确认；支持来源暂停、单条/来源级撤回（`bmem forget`）与隔离恢复。
- **词法检索 + 可选向量检索**：默认启用 SQLite FTS5 词法检索，向量相似检索默认关闭，显式开启后可接服务器 Embedding API 或本机 Ollama（如 `bge-m3`）。
- **三档压缩与备份恢复**：`data` / `balanced` / `high` 三档同时作用于磁盘编码与模型上下文；支持受管备份（`bmem backup`）与恢复预览校验。
- **仅监听回环地址**：服务只绑定 `127.0.0.1`，浏览器访问带本地会话授权，密钥仅存引用、不落明文日志。

## 技术栈

| 层 | 技术 |
|---|---|
| 核心程序 | Rust 2024 edition，单 Cargo 包，按领域拆分 |
| 服务与接口 | axum（回环 HTTP）、clap（CLI）、rmcp（MCP 服务端） |
| 存储 | SQLite（WAL 模式 + FTS5，rusqlite bundled）、zstd 压缩 |
| 模型访问 | reqwest（rustls），兼容 OpenAI 风格服务器 API 与本机 Ollama |
| Web 工作台 | 原生 HTML / CSS / JavaScript，编译期 `include_str!` 内嵌，无前端构建步骤 |
| 集成测试 | Python 3 `unittest`（端到端 CLI/HTTP）、Node.js（真实浏览器 Playwright 用例） |

## 快速开始

1. 从 [GitHub Releases](https://github.com/junwayne66/botinkit-mem/releases) 下载适合系统与架构的包，并按 [安装与 Quick Start](docs/quick-start.md) 核对 `SHA256SUMS`。
2. 解压到长期使用的目录。把 `bmem` 放进 PATH，或始终使用它的绝对路径。执行 `bmem init`，再执行 `bmem web` 打开本机工作台。
3. 先试用声明、检索与工作台，再根据需要配置模型、选择来源和授权。[智能体与 MCP 接入](docs/agent-integration.md)

公开预览版通过 GitHub Release 手动下载，适合全新安装。macOS 与 Windows 的预览包没有正式平台签名，操作系统可能提示或限制首次运行。已有数据的用户先备份并查阅[升级与恢复验证](specs/007-bmem-release-update/validation.md)。正式 `bmem update` 使用 [007 签名渠道](scripts/release/README.md)。

```sh
bmem --version
bmem init
bmem web
bmem declare '所有助手的报告用中文'
bmem statement list
bmem search '报告语言'
```

从源码编译仍可在仓库根运行 `cargo install --locked --path src/v2/bmem`。Rust、C 工具链与验收依赖见[贡献指南](CONTRIBUTING.md)。

### 声明与检索

```sh
# 追加一条全局声明（偏好/事实），可指定共享范围与纠正对象
bmem declare '所有助手的报告用中文，先列行动项' --share authorized_assistants

# 词法检索（默认）；启用向量后可用 --mode vector
bmem search '报告格式偏好'
bmem search '季度复盘的数据源' --mode vector
```

### 备份与撤回

```sh
bmem backup create                          # 生成受管记忆库备份
bmem restore preview BACKUP_ID              # 恢复前预览校验
bmem forget --source SOURCE_ID --preview    # 预览来源级撤回
bmem forget --source SOURCE_ID --confirm <PREVIEW_ID>
```

## 常用命令速览

| 命令 | 用途 |
|---|---|
| `bmem init` / `status` / `doctor` | 初始化、状态检查、故障诊断 |
| `bmem web` / `bmem service run\|start\|stop\|status` | 工作台与后台常驻服务 |
| `bmem config get\|set` | 读取/修改配置（压缩档位、向量开关等） |
| `bmem model list\|add\|test\|use\|pull` | 模型登记、探针、用途绑定与本机拉取 |
| `bmem connector list\|add\|sync\|pause\|resume` | 来源管理与历史同步 |
| `bmem grant source ... --preview/--confirm` | 来源 × 用途授权（提炼/嵌入） |
| `bmem declare` / `bmem statement` | 本人声明的追加、纠正与历史 |
| `bmem search` / `bmem evidence` | 检索与原文证据回查 |
| `bmem job` / `bmem task` / `bmem delivery` / `bmem operation` | 后台作业、任务接力与投递观察 |
| `bmem backup` / `bmem restore` / `bmem forget` | 备份、恢复预览与撤回治理 |
| `bmem capabilities` / `bmem mcp` / `bmem hook` | 能力目录、MCP 与宿主 Hook |

完整的 CLI ↔ Web ↔ HTTP 路由对照见[子命令与 Web 操作契约](docs/v2/technical/command-catalog.md)，所有命令支持 `bmem <组> --help` 与 `--json` 输出。

## 项目结构

```
botinkit-mem/
├── src/
│   └── v2/
│       ├── bmem/                 # Rust 原生程序：CLI、回环服务、worker、宿主适配
│       │   ├── Cargo.toml        # bmem 包定义与开发特性
│       │   ├── Cargo.lock        # 锁定依赖（二进制项目提交入仓）
│       │   └── src/              # 领域模块与升级器
│       └── web/                  # 内嵌 Web 工作台（编译期 include_str! 打包）
├── tests/
│   └── v2/                       # 集成测试与共用夹具
│       ├── test_*.py             # Python unittest 端到端用例
│       ├── browser_*.cjs         # Playwright 真实浏览器用例
│       ├── native_hosts.py       # Claude Code / Codex 双宿主实测
│       ├── release/              # 安装、签名渠道、升级和回退验收
│       └── data/                 # 测试夹具（JSON/JSONL）
├── docs/
│   └── v2/                       # 冻结设计：产品、技术方案、UI 原型、评审记录
├── specs/
│   ├── 004-bmem-v2/              # V2 实现与验证
│   ├── 005-agent-mcp-interface/  # 宿主接口与测试门禁
│   ├── 006-memory-upgrade/       # 记忆功能升级
│   └── 007-bmem-release-update/  # 安装、更新与发布验收
├── research/                     # 产品设计、架构研究、冻结样本与评测证据
├── scripts/release/              # 发布候选、TUF 签发与核验
├── .github/workflows/            # 发布构建与原生验收
├── .specify/                     # Spec Kit 工作流脚手架（规格模板与脚本）
├── .agents/skills/               # speckit 技能定义
├── LICENSE
├── CONTRIBUTING.md
└── CHANGELOG.md
```

核心模块（`src/v2/bmem/src/`）按六个领域组织：接入采集（`ingestion` / `host` / `history*` / `feishu` / `integrations` / `platform`）、权威写入（`authority` / `context`）、提炼巩固（`providers` / `analytics`）、检索（`retrieval` / `vector`）、运营治理（`archive` / `settings` / `server` / `client`）与公共基座（`main` / `error`）。模块边界与数据交接见[模块并行实施与集成](docs/v2/technical/module-integration.md)。

## 开发

```sh
# 构建与静态检查（从仓库根目录）
cargo build --locked --manifest-path src/v2/bmem/Cargo.toml
cargo fmt --check --manifest-path src/v2/bmem/Cargo.toml
cargo clippy --locked --manifest-path src/v2/bmem/Cargo.toml -- -D warnings
cargo test --locked --manifest-path src/v2/bmem/Cargo.toml

# 端到端集成测试：先构建出二进制，再通过环境变量指定被测程序
cargo build --locked --manifest-path src/v2/bmem/Cargo.toml
BMEM_V2_TEST_BIN="$PWD/src/v2/bmem/target/debug/bmem" \
  python3 -m unittest discover -s tests/v2 -v

# 浏览器用例（需本机 Chrome 与 Playwright，模块路径通过环境变量传入）
BMEM_V2_TEST_BIN="$PWD/src/v2/bmem/target/debug/bmem" \
BMEM_V2_PLAYWRIGHT_MODULE=/path/to/playwright \
  node tests/v2/browser_smoke.cjs
```

隔离实例技巧：设置 `BMEM_LOCATOR_DIR` 与 `BMEM_DEFAULT_HOME` 指向临时目录，测试不会触碰真实的 `~/.bmem`。完整的隔离构建、双宿主与真实模型复验步骤见[本地构建与复验](specs/004-bmem-v2/quickstart.md)，已执行的验收证据与已知缺口见[验证记录](specs/004-bmem-v2/validation.md)。

## 文档导航

| 文档 | 读者 | 内容 |
|---|---|---|
| [安装与 Quick Start](docs/quick-start.md) | 用户 | 二进制选型、校验与首次使用 |
| [智能体与 MCP 接入](docs/agent-integration.md) | 用户与集成开发者 | Codex、Claude Code、DSH、Cursor 与通用 MCP |
| [V2 设计总览](docs/v2/README.md) | 所有人 | 架构冻结说明、首发路径与设计验证结论 |
| [技术方案](docs/v2/technical/README.md) | 开发者 | 选型决策、六域十八模块、服务边界图 |
| [产品功能设计](docs/v2/product/README.md) | 产品/开发者 | 功能分期、验收场景、治理与反馈设计 |
| [UI 交互设计](docs/v2/ui/README.md) | 前端/产品 | 离线原型（`prototype.html` 可直接浏览器打开） |
| [技术评审记录](docs/v2/reviews/technical/README.md) | 开发者 | 三轮独立评审意见、处理与文件指纹 |
| [架构研究](research/README.md) | 开发者 | 重建前的模块调研、知识图谱与评测复现 |
| [实施规格](specs/004-bmem-v2/spec.md) | 开发者 | V2 首发用户故事与成功标准 |
| [正式发布与签名](scripts/release/README.md#申请正式签名) | 发布维护者 | 证书申请、平台签名、TUF 稳定渠道和验收 |

## 从 V1（Python 版）迁移

V1 的 Python/FastAPI 实现已退役，V2 从空库起步、工作区格式与 V1 相互独立。迁移要点：

1. 在旧服务可读时先创建本地备份，核对归档中的数据库、证据与撤回记录；
2. 安装 V2 后执行 `bmem init`，历史回填按 V2 的来源授权重新接入；
3. 用 `bmem backup create` + `bmem restore preview` 验证 V2 备份链路；
4. 确认 V2 命令、数据根、来源集成与常驻进程均可用后，再停用 V1 启动项与旧程序。

V1 的完整源码与历史文档可在 git 历史提交 `ae2c06b` 中按仓库路径查阅。

## 路线图

当前状态与后续分期以设计文档为准：

- **P0（进行中/首发日用版）**：本人声明 → 跨助手装入 → 结果回执；普通对话与工具回执自动提炼；撤回、备份与隔离恢复。核心流程 T001–T013、T017–T022 已完成并通过独立复验。
- **P1**：语义融合检索、跨案例方法归纳、关系社区图谱的收益对照。
- **P2**：在有获准样本、独立评测与可回退版本时，引入离线学习与受控发布。

剩余的三平台签名成品、Windows 本机运行验收、生产路由校准等门槛见[验证记录「尚待首发验收」](specs/004-bmem-v2/validation.md)。

## 贡献

欢迎提交 Issue 与 Pull Request。开发环境、测试要求、提交规范与评审流程见 [CONTRIBUTING.md](CONTRIBUTING.md)；版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 发布。
