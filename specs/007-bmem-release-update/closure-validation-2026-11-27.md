# 007 补齐轮验收记录 · 0.3.0

2026-11-27 · Asia/Shanghai

| 项目 | 实跑结果 | 使用范围 |
|---|---|---|
| 升级恢复点清理 | 通过 | 终态与运行状态写入后，仅清理本实例账本记录的专用恢复目录。预提交保留恢复点，清理失败可重试。符号链接、无账本目录及兼容回切所需的旧程序得到保留 |
| macOS 启动器 | 2 条通过 | Apple Silicon 原生 launchd。KeepAlive 与按需任务均更换 PID、恢复到原实例并保留 plist |
| Linux 启动器 | 1 条通过 | Linux arm64 隔离容器，systemd 252.39 普通用户服务使用测试签名完成接管。PID 从 750 切到 814，实例与单元配置保持一致 |
| Windows 启动器、U08 | 6 条原生用例通过 | x64、ARM64 各通过两条 U08 和一条 Task Scheduler 接管。旧 stdio 与 Win32 占用、同一事务中断恢复、任务运行与停止状态均有实跑证据 |
| 六平台发布核验 | 7 条隔离验证通过，正式制品待验收 | 核验六份资产的大小、完整字节和摘要，拒绝可变发行、错误提交和无效签名。macOS 裸 CLI 使用 Developer ID Application 和在线公证核验，错误签名团队被拒绝 |
| TUF 稳定渠道 | 隔离签名暂存通过，公开稳定渠道待验收 | Python TUF 独立复核真实临时 Ed25519 元数据并拒绝过期、坏签名和修改的目标目录。公开稳定时间戳在本轮只读检查中返回 404 |
| 冻结样本 | 134 / 134 通过 | 652 条原断言与 9 个样本哈希核对一致。F01-14 在同一候选上补跑通过 |

[恢复点与旧程序回切](evidence/recovery-cleanup.log) · [macOS 启动器](evidence/launchd-native.log) · [Linux 启动器](evidence/systemd-native.log) · [Windows 静态验证](evidence/windows-cross-check.log) · [TUF 与制品核验](evidence/release-channel-verifier.log) · [完整 134 条回归](../../research/memory-upgrade-technical/tests/closure-007-regression-2026-11-27.md)

Claude Code（qwen4-max）独立复核了终态与删除顺序、目录归属和 Windows 助手交接。实现按持久账本限定清理范围，拒绝重解析目录入口，保留清理失败的重试状态。用同一实例下的符号链接替换夹具验证了删除边界。[Rust 目录删除行为](https://doc.rust-lang.org/std/fs/fn.remove_dir_all.html) · [Windows 文件替换语义](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)

## Windows 原生验收

本轮用 GitHub 托管原生 Windows 和一次性 TUF 测试签名。候选提交 `e6034563c8a4869ad79930383c619a440207367b`。从公开主线导出旧版所需的 Rust 和 Web 源码，两棵 Git 树与原审核的 0.2.0 一致。四份回执的日志、三份程序摘要和架构均已校验。[本轮 Actions 实跑](https://github.com/junwayne66/botinkit-mem/actions/runs/45934049256)

| 原生环境 | U08 | Task Scheduler |
|---|---|---|
| Windows Server 2022 x64 `10.0.20348.0` | [2 / 2](evidence/windows-native-u08-x64.log) · [回执](evidence/windows-native-u08-x64.json) | [1 / 1](evidence/windows-native-scheduler-x64.log) · [回执](evidence/windows-native-scheduler-x64.json) |
| Windows 11 ARM64 `10.0.26200.0` | [2 / 2](evidence/windows-native-u08-arm64.log) · [回执](evidence/windows-native-u08-arm64.json) | [1 / 1](evidence/windows-native-scheduler-arm64.log) · [回执](evidence/windows-native-scheduler-arm64.json) |

原生运行发现快照刷盘需要可写句柄，且仅有旧 stdio 进程时，`ReplaceFileW` 可能仍允许替换。修复后，接管等待该安装路径的旧进程退出，文件共享冲突仍走原回退路径。计划任务夹具用同一交互账户初始化 0.2.0，使用实际任务直接启动程序，验收原实例、任务配置、重启和初始停止状态。macOS 的中断恢复定向回归 2 / 2 通过。[Windows 刷盘访问要求](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers)

## 正式发布状态

T005、T009 已关闭。T013、T014、T015 的正式发布验收仍开放。六目标需要统一的正式构建提交、macOS 和 Windows 签名身份、获准 TUF 根和最终制品验收回执。公开稳定时间戳返回 404。发布执行人由项目所有者指定，首次启用时须回读不可变 Release，再验证公开稳定渠道。0.3.0 待签名、待发布。[007 台账](tasks.md) · [发行手册及权限清单](../../scripts/release/README.md)

0.3.0 内部测试版另以本地独立 TUF 根验收。macOS 与 Linux 四目标均通过。Windows 内测本轮暂缓。[内部测试版验收与归档](internal-validation-2026-11-27.md)
