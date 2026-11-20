# 贡献指南

感谢你参与 Botinkit Memory（命令名 `bmem`）。本文说明开发环境、测试要求、提交规范与协作流程。项目以中文撰写设计与用户文档，代码标识符、提交信息与公开 API 使用英文。

## 行为准则

- 围绕事实与可复现证据讨论，设计结论以 `docs/v2/` 的冻结方案与实测记录为准。
- 尊重用户数据边界：本项目的核心价值是本地优先与逐项授权，任何改动不得默认外发用户数据或弱化授权/撤回语义。
- 评审对事不对人，意见具体到文件、行为与复现步骤。

## 开发环境

| 工具 | 用途 | 要求 |
|---|---|---|
| Rust（stable）+ Cargo | 构建核心程序 | edition 2024，含 `rustfmt`、`clippy`；本机需有 C 编译器（rusqlite 使用 bundled 特性编译） |
| Python 3.10+ | 端到端集成测试 | 仅标准库 `unittest`，无需安装第三方包 |
| Node.js 18+ | 浏览器用例与 JS 语法检查 | 浏览器用例另需本机 Chrome/Chromium 与 Playwright |
| Ollama 或 OpenAI 兼容 API | 真实模型提炼（可选） | 不配置时模型相关用例自动跳过 |

仓库不包含 Rust 工具链固定文件，建议使用当前 stable 渠道；三平台构建均以 `--locked` 执行，改动依赖时必须同步更新 `Cargo.lock`。

## 常用开发命令

所有命令从仓库根目录执行，Cargo 命令通过 `--manifest-path` 指向实际包位置。

```sh
# 构建
cargo build --locked --manifest-path src/v2/bmem/Cargo.toml

# 格式与静态检查（CI 门禁，warning 视为失败）
cargo fmt --check --manifest-path src/v2/bmem/Cargo.toml
cargo clippy --locked --manifest-path src/v2/bmem/Cargo.toml -- -D warnings

# Rust 单元测试（行为测试主要在 tests/v2）
cargo test --locked --manifest-path src/v2/bmem/Cargo.toml

# 前端 JS 语法检查（无前端构建步骤）
for f in src/v2/web/*.js tests/v2/*.cjs; do node --check "$f"; done
```

## 测试要求

### 集成测试（必跑）

`tests/v2` 通过环境变量 `BMEM_V2_TEST_BIN` 接收被测二进制，对真实 CLI 与回环 HTTP 服务做端到端验证：

```sh
cargo build --locked --manifest-path src/v2/bmem/Cargo.toml
BMEM_V2_TEST_BIN="$PWD/src/v2/bmem/target/debug/bmem" \
  python3 -m unittest discover -s tests/v2 -v
```

- 测试使用隔离的临时数据根（`BMEM_LOCATOR_DIR` / `BMEM_DEFAULT_HOME`），不得依赖或污染真实的 `~/.bmem`。
- 新增用户可见行为时，先在 `tests/v2` 增加可复现用例，测试夹具统一放 `tests/v2/data/`。
- 真实模型用例通过 `BMEM_V2_TEST_REAL_MODEL` 显式启用，未配置时必须安全跳过，不得让 CI 因缺少模型而失败。

### 浏览器测试（涉及 Web 改动时必跑）

```sh
BMEM_V2_TEST_BIN="$PWD/src/v2/bmem/target/debug/bmem" \
BMEM_V2_PLAYWRIGHT_MODULE=/path/to/playwright \
  node tests/v2/browser_smoke.cjs
```

浏览器用例覆盖桌面与 375px 窄屏布局、设置双向同步、草稿冲突保留、键盘操作与减少动态效果等。无法提供 Chrome 环境时，至少运行 `node --check` 并在 PR 中说明未覆盖项。

### 双宿主测试（涉及接入/协议改动时）

```sh
BMEM_V2_TEST_BIN="$PWD/src/v2/bmem/target/debug/bmem" python3 tests/v2/native_hosts.py
```

该用例需要本机安装 Claude Code 与 Codex，以隔离会话验证 Hook、MCP、任务接手与回执。

## 提交规范

提交信息遵循 [Conventional Commits 1.0](https://www.conventionalcommits.org/)，使用英文类型前缀与简洁的英文祈使句描述：

```
<type>(<optional scope>): <summary>
```

| 类型 | 用途 |
|---|---|
| `feat` | 新功能或新能力 |
| `fix` | 缺陷修复 |
| `docs` | 仅文档变更（`docs/`、`specs/`、`README` 等） |
| `test` | 仅测试用例与夹具变更 |
| `refactor` | 不改变外部行为的代码重构 |
| `perf` | 性能优化 |
| `chore` | 构建、依赖、工具链与杂项 |
| `style` | 格式化，不含逻辑变更 |

规则：

- 摘要行控制在约 72 字符内，末尾不加句号；正文说明「为什么」与可验证的行为变化。
- 一个提交只做一件事；不把格式化与逻辑改动混在同一提交。
- 涉及授权、撤回、凭据、事务提交顺序的改动，正文必须点明安全语义是否变化。
- 不要提交本地数据、密钥、`target/`、`__pycache__/`、`node_modules/` 等产物（见 `.gitignore`）。

## 分支与 Pull Request

1. 从最新 `main` 切出主题分支，命名建议 `feat/<short-topic>`、`fix/<short-topic>`、`docs/<short-topic>`。
2. 一个 PR 聚焦一个主题；保持提交历史可读，必要时 rebase 整理，避免无意义的 merge commit。
3. PR 描述包含：动机与方案、改动范围、测试方式与结果、安全/兼容性影响（数据库格式、授权语义、CLI 参数、HTTP 契约）。
4. 自查清单：
   - [ ] `cargo fmt --check`、`cargo clippy -- -D warnings`、`cargo test` 通过
   - [ ] `python3 -m unittest discover -s tests/v2` 全绿（跳过项有明确原因）
   - [ ] 改动用户可见行为时已补测试，并在 `specs/004-bmem-v2/` 或 `docs/v2/` 同步文档
   - [ ] 新增/变更 CLI 命令、HTTP 路由或权限点时，同步更新[命令契约](docs/v2/technical/command-catalog.md)
   - [ ] 无密钥、个人数据与构建产物入库
5. 评审意见解决后由维护者合入；涉及架构冻结决策的改动需同时更新对应设计文档与评审记录。

## 文档与设计变更

- `docs/v2/` 是冻结的设计资料，部分文件带 SHA-256 指纹，分两类管理：
  - **不可变历史清单**：`docs/v2/research.sha256`（冻结研究输入）与 `reviews/technical/round-{1,2,3}.sha256`（各轮评审时点证据，清单自身哈希是评审索引中的封印值）。这些文件**永不修改**；用它们校验当前工作区出现 FAILED 属预期，只用于复现评审/冻结现场。
  - **活基线清单**：`reviews/technical/current.sha256` 覆盖与 R3 相同的 21 个技术文件，是当前工作区的可校验基线。改动其中任一技术文件后，必须从仓库根重算并更新该清单：
    ```sh
    shasum -a 256 $(awk '{print $2}' docs/v2/reviews/technical/current.sha256) \
      > docs/v2/reviews/technical/current.sha256
    shasum -a 256 -c docs/v2/reviews/technical/current.sha256   # 必须 21 项全 OK
    ```
  - 若改动触及冻结研究文件，需按 [baseline.md](docs/v2/baseline.md) 的流程形成下一版冻结记录，而不是直接改写 `research.sha256`。
- 实施台账在 `specs/004-bmem-v2/tasks.md`，验证证据与缺口记录在 `specs/004-bmem-v2/validation.md`；完成任务后同步勾选并追加实测证据，不允许只标状态不留证据。
- 用户可见的变更在合入时同步更新 [CHANGELOG.md](CHANGELOG.md)。

## 报告问题

提交 Issue 时请附带：

- `bmem --version`、操作系统与架构、安装方式；
- `bmem status` 与 `bmem doctor --json` 的输出（提交前脱敏，移除密钥与个人路径）；
- 复现步骤、预期与实际行为；涉及 UI 时附截图与浏览器/视口宽度。

## 许可证

你提交的贡献按 [Apache License 2.0](LICENSE) 授权，提交 PR 即表示你有权且同意以此许可提供贡献。
