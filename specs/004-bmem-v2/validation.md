# V2 核心流程验证

2026-11-19，macOS arm64。对应 `src/v2` 的 19 个文件，对排序后的「SHA-256、路径」清单再计算 SHA-256 为 `93adbff5c51adac538fffc35ee300a98ca53717f034c8ceb108d8cd9c6e85e04`。开发使用五个独立的本地 Claude Code 会话，合流时逐文件检查，遇到无产出或接口不能通过的部分由 Codex 和验证代理补齐并复测。验收依据为实际程序与请求。

## 已运行

| 范围 | 命令或证据 | 结果 |
|---|---|---|
| Rust | `cargo fmt --check --manifest-path src/v2/bmem/Cargo.toml`、`cargo clippy --manifest-path src/v2/bmem/Cargo.toml --locked -- -D warnings`、`cargo test --manifest-path src/v2/bmem/Cargo.toml --locked`、优化构建 | 格式、Clippy、编译通过。行为测试在 `tests/v2`，Cargo 内置测试目标运行 0 项 |
| macOS arm64 | `BMEM_V2_TEST_BIN=<binary> BMEM_V2_TEST_REAL_MODEL=qwen3:4b-instruct-2507-q4_K_M python3 -m unittest discover -s tests/v2 -v` | 13 项通过。涵盖 CLI/API 配置、幂等重放、权限、撤回、备份预览、任务接力、预算和向量启停 |
| 真实 Chrome | `BMEM_V2_TEST_BIN=<binary> BMEM_V2_PLAYWRIGHT_MODULE=<playwright> node tests/v2/browser_smoke.cjs` | 桌面及 390px 布局。双向更改设置、冲突保留草稿、连续重试复用同键同请求、无设置基线时可见错误、声明纠正和 CLI 读回均通过 |
| 实际双宿主 | `BMEM_V2_TEST_BIN=<binary> python3 -u tests/v2/native_hosts.py` | Claude Code 2.1.308、Codex 0.167.0 在隔离会话中实际调用 hook。回环模型端点捕获声明修订、原生工具回执以及对方获准来源的任务证据。另测 MCP、跨来源身份、同键异文和结构化凭据脱敏。回环端点是测试夹具，宿主没有独立装入或采用回执的记录仍为未知 |
| 真实提炼 | 上述 macOS 用例调用本机 Ollama 0.40.16 与实际 Qwen 模型 | 获准普通用户输入形成带原文的计划。结构化回执分别形成「解析通过」和「入库超时」两条观察。没有产生未获证据支持的整体成功结论 |
| Linux arm64 | Docker 内以普通用户运行锁定依赖构建和 Python 全套发现 | 发现 13 项，12 项通过，真实 Ollama 用例因容器未接入端点跳过。ELF SHA-256 `0ae43fdbc2890a96e72c64c396a21b0818f895d40e0a0d795ed8dc58349c7c17` |
| Windows x64 | Linux 容器用 MinGW 和 Rust `x86_64-pc-windows-gnu` 锁定依赖交叉构建 | PE32+ 生成成功，SHA-256 `8dd1b632b8fa5f1fad1fd3eb46b6e4f509f9dad17cff2ef4d5d71bb5225f942f` |
| 本机性能抽样 | macOS 优化二进制预热后启动 31 次 `--version`。另将一条 16,200 字节可重复文本写入三个隔离库，经词法命中和补读比较原文 | 启动 p50 3.24 ms，p95 5.02 ms。三个档位原文逐字相同，词法命中一致。检索载荷在数据档为 16,200 字节，平衡和高压缩均为 45 字节。样本高度重复，仅测该载荷 |
| 设计冻结与代码审读 | `shasum -a 256 -c docs/v2/reviews/technical/round-3.sha256`，`ocr delegate preview` 和 `ocr delegate rule` | 21 项技术设计指纹匹配（评审时点）。OCR 对本次 V2 源码与测试列出 24 个可审查项，排除生成的 Cargo 锁文件和三个 Python 测试文件。Codex 审读实现、测试与规则，重点复核授权、撤回与事务。真实执行结果见上表。**2026-11-20 补记**：`round-3.sha256` 是评审时点的不可变证据；实施期 3 个技术文件细化后，当前工作区基线改用 `shasum -a 256 -c docs/v2/reviews/technical/current.sha256` 校验（21 项全 OK），时点清单与当前文件的差异属预期 |

## 退回与回归

- 配置回执曾在 TOML 替换后才加到内存，重启后同键返回冲突。修订后把配置和回执原子落盘，并通过重启回放。
- Claude 的个别开发回合结束时没有源码，或产物持有未通过编译的接口。分别重派和补做，再用同一集成入口验证。
- 首轮提炼只保留工具观察，遗漏普通用户计划。修订后根据原文的明确计划措辞与候选证据引用确认计划，保留原句。模型的整体成功推断无法覆盖实际工具失败。
- 代码审读发现结构化工具结果提前字符串化、撤回未覆盖首次迟到的旧版本、旧草稿丢失，以及接收页与索引分开提交。对应修正保留了结构化凭据过滤，根级墓碑与读索引同步生效，Web 重试保留原请求，来源页、游标、待办及词法投影共同提交。迟到版本、双宿主和浏览器均已有回归。

## 尚待首发验收

- Windows 本机运行、安装、登录启动与更新，以及 macOS x64、Linux x64、Windows arm64 的成品验证。签名发行需要实际发行渠道和签名材料。
- 受信旧备份的恢复执行、独立撤回清单、旧归档清理，以及完整导出和管理命令。当前交付受管备份与隔离预览，出现撤回变化会拒绝使用旧快照恢复。
- 本地已验证端点之外的生产路由校准，以及真实项目材料的抽取准确率。当前自动提炼按已验证的本地模型路由授权，系统凭据库与真实远程目的地的完整能力需另验。
- 三档全库体积、交接准确率和真实 token 的配对测试。高压缩上下文在质量未验证时使用保真表示。引用的压缩抽样只覆盖检索原文载荷。

## Botinkit Memory 工作台改版验收

2026-11-19，在 macOS 上使用原生 Release 程序、临时数据根和真实 Chromium 测试。总览与星图的 Claude Code 独立设计审查通过。根据评审意见将设置资源按需加载，补上图表类别的默认颜色，并加入快速切页时在途图谱响应的回归测试。

| 检查 | 实测结果 |
|---|---|
| 构建与静态检查 | `cargo build --release --locked`、`cargo fmt --check`、`cargo clippy --all-targets -- -D warnings` 和前端 JavaScript 语法检查通过。 |
| 集成测试 | `python3 -m unittest discover -s tests/v2 -v` 的 15 项全部通过，包含已安装的 Ollama 模型 `qwen3:4b-instruct-2507-q4_K_M` 处理普通对话和工具回执。 |
| 浏览器与 CLI | `node tests/v2/browser_smoke.cjs` 驱动真实 Chromium 和原生服务。覆盖空库示例、真实节点与社区、7/30 天、键盘缩放、低动态、375px 手机布局、路由往返、快速切页、备份预检、采集与授权、文件导入丢回执重试、中文检索、按需读原文、设置双向同步、保留草稿及版本冲突。运行期间没有页面脚本异常或对外资源请求。 |
| 助手协议 | `python3 tests/v2/native_hosts.py` 通过两款本地宿主的 hooks、接手、权限和回执检查。宿主模型协议使用回环夹具。 |

**读接口耗时。** 本机 Release，隔离库内有 10,000 条有效事件和 10,000 项后台作业，均通过服务写入。每个入口先预热一次，再发 30 次串行已认证回环请求。下表包含响应传输和 JSON 解码耗时。浏览器绘制与模型调用另测。

| 请求 | P50 | P95 |
|---|---:|---:|
| 30 日总览 | 0.93 ms | 1.71 ms |
| 星图，80 条记录 | 0.55 ms | 0.67 ms |
| 等待中的作业，40 项 | 0.38 ms | 0.53 ms |

界面截图使用独立合成工作区。此次改版的操作系统和浏览器覆盖为 macOS 上的 Chromium，Windows、Linux 和其他浏览器保留为独立验证项。[界面与交互说明](../../docs/v2/ui/README.md)
