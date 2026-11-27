# Spec Kit 007 · 发布验收与 0.3.0 候选

2026-11-27 · Asia/Shanghai

本页保留首轮候选证据。恢复点清理、平台启动器和发行核验的后续证据见 [007 补齐轮验收](closure-validation-2026-11-27.md)。

本轮在临时安装目录和数据根实跑。测试渠道每次独立生成 Ed25519 密钥，客户端通过 TUF 验证元数据及目标摘要。真实 0.2.0 从冻结制品或隔离源码构建启动，先写入旧格式业务状态，再经安装器接管。两款宿主均用隔离配置和回环模型入口。接管前后分别核对实例、已确认写入和撤回状态。生产签名制品的验收使用对应原生环境。

## 发布验收

| 场景 | macOS arm64 | Linux arm64 | 验收要点 |
|---|---|---|---|
| U01 初装 | PASS | PASS | 正常签名分发、首次安装、正确实例与稳定目标 |
| U02 接管 | PASS | PASS | 运行真实 0.2.0，保留实例、设置、授权、待办、游标与撤回。macOS 实跑两款宿主的 Hook 和 MCP |
| U03 连续升级 | PASS | PASS | 签名 0.3.0 到 0.3.1、原停止状态、重复更新及新服务构建 |
| U04 拒绝无效输入 | PASS | PASS | 过期、坏签名、不可下载、截断、摘要不符、错误架构、空间不足与已见元数据回退 |
| U05 单写与旧进程 | PASS | PASS | 并发更新、原幂等回执、进程锁、旧写者拒绝，另核对写入世代和权威序号 |
| U06 中断与恢复 | PASS | PASS | 四处返回错误和九处进程中断。核对提交点之前恢复程序与两库，之后继续完成发布 |
| U07 放行后回切 | PASS | PASS | 兼容时返回上一程序，保留最新写入和版本高水位。不兼容旧程序被拒，撤回保持生效 |
| U08 原生占用 | 未运行 | 不适用 | Windows x64、ARM64 的 stdio 进程持有旧 EXE 及恢复助手仍需实机执行 |
| U09 发布目录 | PASS | PASS | 缺失或变更资产时保持原稳定目录。完整签名测试目录开放后由真实 CLI 升级 |
| U10 冻结回归 | 134 / 134 通过 | 见 macOS 候选实跑 | 原 134 条、652 条断言与 9 个样本 SHA-256；正式签名制品另需各平台 U01–U09 验收 |

[macOS 实跑日志](evidence/macos-arm64.log) · [Linux 容器实跑日志](evidence/linux-arm64.log) · [旧写者拒写实证](evidence/legacy-write-fence.json) · [U10 独立复判](evidence/frozen-regression.log) · [134 条原样本报告](../../research/memory-upgrade-technical/tests/release-007-regression-2026-11-27.md)

本机测试包含首次在并行高负载下触发的 CLI 测试看门狗。相同二进制以较低并发和 120 秒测试等待上限重跑，保留重试结论。未使用生产配置或密钥。

## 制品与复核

| 制品 | SHA-256 |
|---|---|
| 冻结 macOS 0.2.0 | `d0e36329b8de767013e0dd3a58c220df67e1d634aa04d9efb6689509226f02b1` |
| macOS 0.3.0 签名测试渠道客户端 | `d214761184f4dafd78a86fb33f9224bc4a218e9285b986cc6bb3d48be1c13438` |
| macOS 0.3.1 签名测试渠道目标 | `fe75d495dcab07b30174149baa32cdddcd69eb8bb32cc516507768a41f08bc4d` |
| 006 冻结样本驱动的 0.3.0 | `ffe1b94036e2ad74467987617ba00de1a6d512bbc4df26ec214df12b93277895` |

Claude Code 以 qwen4-max 独立复核合成升级路径，覆盖进程锁与持久标记、SQLite 恢复点、旧写者以及真实服务健康验证。编码复核确认了 Windows 回退同路径冲突，并改为独立恢复助手。执行证据来自产品程序、实际 SQLite 文件、签名元数据及宿主回环。[实施台账](tasks.md) · [复跑指南](quickstart.md)

## 0.3.0 正式发布准备

已准备六目标候选构建、原生制品登记和稳定目录校验脚本。生产提升门槛要求获准 TUF 根一致、最终字节匹配、原生签名及关联验收文件。[构建工作流](../../.github/workflows/release-candidate.yml) · [发布检查脚本](../../scripts/release/candidate.py)

Apple Silicon 的 `cargo build --locked --offline --release` 成功，生成 18 MiB 本地制品及构建清单。该制品无获准 TUF 根，隔离接管实际拒绝更新，旧程序、服务和已受理声明均可核对。最终制品 SHA-256 为 `f2a926b6618829b02e4e6b7a20b299430808e234c8782fd3b0d958694c09359a`。[制品登记](evidence/local-release-record.json) · [缺信任根的拒绝实测](evidence/local-release-smoke.json)

本机完成 Apple Silicon、Linux ARM64 隔离验证。正式发布仍需补齐 Windows 占用验收、剩余目标的生产构建与签名、正式公开 TUF 根和稳定渠道配置。现有 macOS 启动器识别按实际登记作只读核对，真实启停与 Linux/systemd、Windows 计划任务仍是原生验收项。

Linux 环境为普通用户 UID 501 的断网 Bookworm 容器，glibc 2.36。隔离构建优化了调试期 SHA-256，发布流程的最低 ABI 仍需实际签名包验证。Rust 单元测试在 macOS 与 Linux 各通过 40 条。macOS 的服务集成通过 37 条，单独启用实际本地模型后通过默认跳过的 1 条，另有 6 条真实双宿主回归通过。

首轮候选将升级恢复点保存在用户私有目录，用于事务提交前的回退。终态清理的后续实现及验收见补齐轮记录。35 秒停机等待可被较长的在途提炼调用超过，此时保持升级记录及原库，需待作业结束后恢复。
