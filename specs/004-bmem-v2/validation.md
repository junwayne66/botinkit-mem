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

## 治理预览与 Web 控制台修复验收

2026-11-20，macOS arm64（cargo/rustc 1.100.0、Node v22.22.3、Python 3.9.6）。分支 `fix/web-ux-grant-preview`，HEAD `ae2c06b` 加工作树未提交改动：治理预览足迹收窄到目标范围（`retrieval.rs`）、forget-source 已接收页水位、预览期模型就绪检查（`server.rs`）、CLI 确认幂等键（`main.rs`）、Web 控制台交互改版及三处评审修复。七道门禁在合并后的工作树上全部通过（all_pass=true）。调试二进制在门禁前后均核验为最新：`target/debug/bmem` mtime Nov 20 18:41（46,258,808 字节），`find src/v2/bmem/src src/v2/web -newer <binary>` 前后均为 0 个文件，Cargo.toml/Cargo.lock 不更新，无需重建；Python 与浏览器用例因此运行在完全相同的代码树上。门禁未改动任何仓库文件，写入仅限 cargo `target/` 产物与 `/tmp/bmem-final-verify-20261120/` 日志（gate1-build 至 gate7-nodecheck）。真实 `~/.bmem` 未被触碰：测试剥离继承的 BMEM_* 环境变量，在 mkdtemp/TemporaryDirectory 新建隔离工作区运行，事后扫描最近 60 分钟 0 个文件被修改。

| 门禁 | 命令或证据 | 结果 |
|---|---|---|
| 二进制新鲜度（前置） | `target/debug/bmem` mtime Nov 20 18:41（46,258,808 字节）；`find src/v2/bmem/src src/v2/web -newer binary` | 门禁前后均为 0 个更新文件；Cargo.toml/Cargo.lock 不更新，无需重建 |
| 1. 构建 | `cargo build --locked` | Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.25s；exit 0（增量，树已最新） |
| 2. 格式 | `cargo fmt --check` | 无格式差异输出；exit 0 |
| 3. 静态检查 | `cargo clippy --locked -- -D warnings` | Checking bmem v0.2.0 … Finished `dev` profile in 1.38s；零警告零拒绝；exit 0 |
| 4. Rust 单元测试 | `cargo test --locked` | unittests src/main.rs：`8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out`，finished in 0.56s（history_feishu 4 + history_hosts 3 + 分页线程 1）。单一测试目标，无 doc-tests；exit 0 |
| 5. 集成测试 | `python3 -m unittest discover -s tests/v2 -v` | `Ran 33 tests in 19.937s — OK (skipped=1)`。分模块：test_governance_preview 13/13（含授权预览绑定、水位/游标用例）、test_integration 14（13 过 + 1 跳过）、test_task_delivery 3/3、test_vectors 1/1、test_workbench 2/2。唯一跳过项 test_real_local_model_processes_fixture_page 需 `BMEM_V2_TEST_REAL_MODEL` 显式启用本机 Ollama，属预期 opt-in 而非失败；exit 0 |
| 6. 浏览器用例 | `node tests/v2/browser_smoke.cjs`（Playwright，Chrome headless） | 7/7 阶段通过，exit 0；0 pageerror、0 外部请求（见下） |
| 7. JS 语法 | `node --check src/v2/web/*.js tests/v2/*.cjs` | checked=12 fail=0；exit 0，全部 Web JS 与浏览器测试 CJS 解析通过 |

**浏览器七阶段。**

| 阶段 | 实测覆盖 |
|---|---|
| 1 dashboard | 实时指标、图谱导航、可访问控件 |
| 2 intake | 来源控制、预览期模型就绪、授权对话框预览/确认（确认期间游标实时推进）、修订变化重新预览上限、焦点归还、导入校验/重放/放弃、375px 布局 |
| 3 workbench | 过期修订 409、档位保存、声明纠正、390px 移动布局 |
| 4 search | 原生事实检索、关键词回退、按需补读原文 |
| 5 config failures | 过期草稿保留、503 以同一幂等键与同一请求体重试两次、缺失基线安全报错 |
| 6 live settings | 跟随 CLI 写入、跨修订独立草稿 |
| 7 integrations | 3 个隔离宿主安装、轮询下的忙碌状态、GET 失败后轮询存活、飞书设备流/二维码/来源登记 |

末行 `Browser verified settings and declarations against the native CLI.` 仅在 `assert.deepEqual(errors,[])` 与 `assert.deepEqual(external,[])` 之后打印，确认零页面脚本异常、零对外资源请求。

**两轮对抗评审与修复。**

- 第一轮（后端治理语义）：复现同游标页缺口——`ingestion::accept_in` 只校验 `page.cursor` 等于已提交游标，`next_cursor == cursor` 的页仍会提交新对象而游标不动；forget-source 预览若只绑游标会漏掉这类页，确认时清理范围可能大于预览范围。修复：来源足迹增绑 `accepted_page_watermark = COALESCE(MAX(rowid),0) FROM ing_ingests WHERE source_id`（每个新提交页无条件插一行，含同游标页与纯重复页；幂等回放不插行；该表无 DELETE，rowid 单调）。回归用例 `test_forget_source_same_cursor_page_requires_fresh_preview`。
- 第二轮（Web 控制台）：三处已复现问题全部修复并复验。① 设置页键控增量刷新以行内容 JSON 作签名，模型卡片的嵌入测试/移除门禁取自全局配置上下文（vectorEnabled/bindings），上下文变化时未变行不重建、门禁长期陈旧；修复为把行包成 `{model, vectorEnabled, bindings}` 让上下文进入签名（settings.js renderModelsList）。② confirmDialog 输入确认只 trim 输入不 trim 目标名，带首尾空白的名称永无法确认；修复为比较与展示标签同用 trim 后的目标，纯空白目标退化为普通确认（app.js）。③ intake.js 每次 open() 重挂都新增 document visibilitychange 监听器，旧闭包滞留已分离视图树；修复为每次挂载一个 AbortController，重开前 abort，监听器与轮询定时器绑定 signal。CDP `DOMDebugger.getEventListeners` 实测 4 轮往返监听器数恒定（sources=5/imports=3）。

**安全语义。**

- 仍返回 `409 revision_changed`：预览与确认之间绑定目标状态的真实变化——目标授权行变化（含同目标并发授权修改）、模型目的地重绑、遗忘目标声明被撤回/取代或自身修订变化、遗忘来源新接收页（游标推进或同游标接收页）。确认时的足迹比对保持逐字节精确，不放松。
- 不再返回 409：无关活动——其他来源的采集与游标推进、全局声明 change_seq、治理单计数、任务/客户登记及其他治理流量，均不再使待确认预览失效。
- 预览期 `model_not_ready`：extract/embed 的 allow 预览在无就绪路由时即返回 `409 model_not_ready` 与配置指引（与确认路径同码）；embed 另要求先开启向量检索。
- 回放：CLI `--confirm` 的幂等键固定派生为 `grant-confirm-{preview_id}`，丢失回执后重试同一预览回放已存治理单（同一 operation_id）；预览确认成功后即被消费、只受理一次，不同意图不共用键。stop/resume capture 无预览确认的行为不变。
- 回归：`tests/v2/test_governance_preview.py` 13 项覆盖上述绑定、水位、就绪与回放语义。

**冻结界面文档。** `docs/v2/ui/` 为冻结的呈现层设计资料，本轮刻意未改动：Web 改版保留其描述的页面、操作与授权/预览—确认语义，仅演进呈现方式（错误中文化与下一步指引、轻量通知、原生 `<dialog>` 确认含授权影响预览与数据去向警示、忙碌状态、键控增量刷新、请求超时、乐观开关与回滚、自动刷新）。呈现演进以本记录与浏览器用例为证据。

## 数据模块改版验收 · 2026-11-21

用户确认 Figma 原型后，从 `8113482` 为本机 Claude Code 建立六个独立工作区。五个工作包返回完成报告，来源状态工作包达到会话轮次上限后由 Codex 接手完成。工作包报告仅作为合流输入，下列结果来自 Codex 在合流工作区运行的检查。

| 检查 | 独立验证结果 |
|---|---|
| 构建和静态检查 | `cargo build --locked`、`cargo fmt --check`、`cargo clippy --locked --all-targets -- -D warnings`、修改的 Web 脚本 `node --check`、`git diff --check` 通过。 |
| Rust | `cargo test --locked` 31 项通过，覆盖历史任务提交与取消、失败重试、检查点续传、服务中断恢复、去重、撤回后导出过滤、台账分页和回执筛选。 |
| 原生 CLI 与 HTTP | `python3 -m unittest discover -s tests/v2 -v` 35 项通过，1 项可选 Ollama 用例跳过。通过同一隔离 SQLite 验证了采集开关和飞书授权随重启回读、一次性导入和持续同步重叠去重、飞书私聊群聊和新版文档选项、群聊和文档入库、导出时间边界、回执和未登录拒绝。飞书由隔离的 `lark-cli` 合成夹具提供响应。 |
| Chrome | `node tests/v2/browser_smoke.cjs` 使用实际嵌入 Web、浏览器和原生服务。覆盖 CLI 改动网页自动回读、刷新后状态保持、历史导入预览和提交、下载文件解析、实际台账行及详情、装入筛选和窄屏。 |
| 原生宿主 | `python3 tests/v2/native_hosts.py` 验证本机 Claude Code 与 Codex 经过隔离服务的原生 Hook、投递、回执及跨宿主接手，模型响应由回环夹具提供。 |
| 代码评审 | `ocr delegate preview`、`ocr delegate rule` 筛出 18 个文件，Codex 检查改动和相关测试，修正集成开关副本、导出正文重复持久化、停机续传、台账结果可见性和响应式表格。Python、Rust 测试夹具及规格说明通过相应用例和人工核对。 |

本轮使用隔离实例。飞书真实租户、Hermes、OpenClaw 和第三方 MCP 服务的正式接入验证仍待开展。导出是按来源及日期生成的 V2 事件快照，文件导入的 V2 批次使用来源游标和下一游标。

## 本机部署与状态读取修复 · 2026-11-21

旧服务的正常状态接口在三轮并发请求下等待约 5.7–16.0 秒。进程采样定位到向量待办查询长期占用共享连接。旧版不带分页参数的作业列表还曾返回约 345 MB。新版状态请求读独立的 SQLite WAL 快照，总览汇总随业务事务更新，向量和提炼待办按授权来源走索引，作业列表默认只返回一页元数据。

在备份 SQLite 和旧程序后，使用 `cargo build --release --locked --manifest-path src/v2/bmem/Cargo.toml` 构建并替换同一实例的服务程序。实例保留原数据根和回环端口。验收时 SQLite 约有 9.2 万条有效事件和 9.2 万条提炼作业，对比业务表和持久汇总的实际计数完全一致。

| 检查 | 结果 |
|---|---|
| 真实实例 HTTP | 三轮并发覆盖健康、设置、来源、工作台、星图、作业、任务、记忆、注入台账、操作记录、历史任务和数据管理，42 次请求均返回 200，单次最大约 129 毫秒。 |
| 真实 Chrome | 验证十个页面的状态接口与界面载入。数据来源首次载入加一次刷新合计约 134 毫秒，三个 Harness 的采集开关读回已开启，飞书显示「已授权 · 待续期」。 |
| 隔离回归 | Rust 31 项通过，Python 37 项通过，1 项可选 Ollama 跳过。包含 SQLite 写入等待时的并发读取、旧库汇总补建与重启、飞书续期与失效状态。Chrome 全套回归、Rust 格式与 Clippy、JS 语法和 `git diff --check` 通过。 |

## 历史数据按类别导入

Claude Code 完成了 Web 勾选界面的初稿，Codex 实现后台任务并审查和完善实际入口。通过 OCR 委托规则核对生产改动，验收以独立执行结果为准。

| 检查 | 结果 |
|---|---|
| Rust | `cargo test --locked --manifest-path src/v2/bmem/Cargo.toml` 33 项通过。追加覆盖飞书附件消息记录和来源撤回后停止未完成作业。 |
| CLI 与 HTTP | `python3 -m unittest discover -s tests/v2 -v` 38 项通过，1 项可选 Ollama 跳过。验证三个 Harness 全部本机会话、归档、工具回执、不打开持续采集，以及飞书多页私聊、群聊与话题、三类文档正文、重复提交和目录读取中断后从检查点恢复。飞书内容使用合成 CLI 响应。 |
| 浏览器 | `node tests/v2/browser_smoke.cjs` 全套通过。验证打开和勾选均不入队，确认后可查看任务、导出和回执。 |
| 本机部署 | 备份旧程序和 SQLite，安装 release 构建并重启同一实例。Chrome 在 390px 打开飞书、Codex、Claude Code 和 DSH 的实际入口，核对所有类别默认未选中且任务数量没有变化。 |
| 静态检查 | release 构建、Rust 格式、Clippy、JS 和夹具脚本语法以及 `git diff --check` 通过。 |

## 门禁复跑与三处陈旧记录更正 · 2026-11-24

本机 macOS arm64（cargo/rustc 1.100.0、Python 3.9.6、Node v22.22.3、Chrome via Playwright 1.64.1）。一条命令复跑：`BMEM_V2_TEST_REAL_MODEL=qwen3:4b-instruct-2507-q4_K_M BMEM_V2_TEST_NATIVE_HOSTS=1 BMEM_V2_PLAYWRIGHT_MODULE=<playwright> python3 tests/v2/run_gates.py --profile assembly`。数字取自 `tests/v2/artifacts/validation_numbers.json`（runner 机械写入，禁手填），绑定代码基线 HEAD `04da93e8633c`、被测二进制 sha256 `e6e0ee08623c…`；台账文档提交后于 `0183c4f7d28f`（binary `d640bf9f46ea…`）复跑，14 步同样全 PASS、`all_pass=true`、Python 全证据档 146 run / 0 skip / 0 失败。本节只追加，不改写上表的历史时点记录。

| 检查 | 实测结果 |
|---|---|
| 构建与静态检查 | `cargo fmt --check` 无 diff；`cargo clippy --locked --all-targets -D warnings` 0 警告（步 2 前 `cargo clean -p bmem` 强制本 crate 重新分析，不再拿缓存诊断冒充新改动）；`cargo build --release --locked` 通过 |
| Rust | 1 个测试目标 **37 passed / 0 failed / 0 ignored**。计数含 `tests/v2/{context_ledger,data_management,history_feishu,history_hosts,history_tasks,platform}.rs`，它们经 `#[cfg(test)] #[path=…]` 内联进 crate |
| Python | **146 用例**。基线档 skip=7（6 个双宿主用例＋1 个真实模型用例，与 AST 守卫求得的期望集相符，任何意外 skip 判失败）；全证据档 **146 run / 0 skip / 0 失败 / 123.9s**，含真实 `qwen3:4b-instruct-2507-q4_K_M` 提炼与真实双宿主 |
| 浏览器 | `tests/v2/browser_smoke.cjs` 为唯一入口（其余 6 个 `browser_*.cjs` 只导出验证函数，逐文件直跑是 0 秒空跑，runner 显式拒绝把它当证据），46.5s 通过，含 data_console 与 integrations 阶段 |
| 双宿主 | claude 2.1.308 与 codex-cli 0.171.0 实跑，现为 6 个被 `unittest discover` 计数的用例（声明装入 initial／corrected、MCP、hook 重放、原生脱敏、跨宿主接手），不再是「跑过但没进门禁」的一次性脚本 |
| 台账对账 | 步 12 文件边界 changed=declared、offenders=0；`Error::conflict(` 调用点 64→64 未增（T049 迁移前作为下限哨兵） |

**本轮更正三条陈旧记录**（均在本文档前文，保留原文不改写）：

1. 第 9 行「行为测试在 `tests/v2`，Cargo 内置测试目标运行 0 项」已过时：现 `cargo test` 报告 1 个目标 37 passed，因 `tests/v2/*.rs` 经 `#[path]` 内联进 crate 编译。
2. 第 10 行「13 项通过」与后续各轮的 15/33/35/37/38 均为当时快照：现全量 discover 为 **146 用例**，其中 `tests/v2/gates/` 是门禁自身的单测。
3. 「真实模型用例默认 skipped=1」的记法在 assembly 档不再适用：该档要求零跳过，缺 `BMEM_V2_TEST_REAL_MODEL` 时 runner 直接以「缺证据」判失败，而不是静默通过。

**本轮顺带修掉的三个真实缺陷**（原先都被当成"测试不稳"或"外部因素"）：

- **daemon 启动锁竞争（可达产品故障）**：`serve()` 一次性 `try_lock_exclusive` 失败即致命退出，而每次 CLI 活性探针都短暂持同一把排它锁。隔离实测碰撞率 6/140 ≈ 4.3%/次启动（无探针对照 0/60），每轮 discover 约 50 次启动 → 单轮至少一次失败的概率 ≈88%，且失败用例逐轮随机。修为 daemon 侧 20ms×2s 有界重试（`lock_service_for_daemon`），`lock_service`／`discover` 的"一次性、拿到锁即 stopped"语义保持不变并写成测试。
- **批量历史作业排空顺序随机**：同批三个 job 共用一个 `created_at`，tie-break 落在随机 `task_id` 上，导致消息批次有 1/3 概率排到最后、串行 12.4s 超过调用方 10s 预算。改为 `rowid`（插入序 FIFO），调度序与 `list()` 展示序一致。
- **宿主与门禁子进程继承 stdin**：非 TTY（nohup／CI）下 stdin 是永不关闭的管道，`codex exec` 打印 `Reading additional input from stdin...` 阻塞到预算耗尽，**曾被误判为第三方 CLI 的耗时尾部**并据污染样本算出 113s 预算。补 `stdin=DEVNULL` 后重测 9 轮 54 次宿主调用零尾部，预算按清洁样本重算为 86s，污染观测从样本剔除并在注释与台账中记录。
