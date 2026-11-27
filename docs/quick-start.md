# 安装与 Quick Start

从 [GitHub Releases](https://github.com/junwayne66/botinkit-mem/releases) 下载对应平台的二进制包及同一版本的 `SHA256SUMS`。预览版在 Release 页面标为 **Pre-release**。选择附件列表中的 bmem 包。GitHub 自动附带的源码压缩包适用于自行编译。

| 系统 | 设备 | 包名后缀 |
|---|---|---|
| macOS | Apple Silicon | `aarch64-apple-darwin.tar.gz` |
| macOS | Intel | `x86_64-apple-darwin.tar.gz` |
| Windows | x64 | `x86_64-pc-windows-msvc.zip` |
| Windows | ARM64 | `aarch64-pc-windows-msvc.zip` |
| Linux（GNU/glibc） | x64 | `x86_64-unknown-linux-gnu.tar.gz` |
| Linux（GNU/glibc） | ARM64 | `aarch64-unknown-linux-gnu.tar.gz` |

macOS、Linux 可运行 `uname -m` 确认架构。`arm64` 或 `aarch64` 对应 ARM64。`x86_64` 对应 x64。Windows 从「设置 → 系统 → 系统信息」查看系统类型。兼容性以 Release 实测的操作系统和运行时为准。

## 校验并安装

对下载的单个压缩包计算 SHA-256，核对 `SHA256SUMS` 中同名文件的值。

```sh
shasum -a 256 ./bmem-v0.3.0-preview.1-aarch64-apple-darwin.tar.gz
# Linux 也可使用 sha256sum ./bmem-v0.3.0-preview.1-aarch64-apple-darwin.tar.gz
```

```powershell
Get-FileHash .\bmem-v0.3.0-preview.1-x86_64-pc-windows-msvc.zip -Algorithm SHA256
```

将压缩包解压到长期使用的目录，例如 macOS 或 Linux 的 `~/.local/bin` 下的专用目录，或 Windows 的 `%LOCALAPPDATA%\Programs\bmem`。可将二进制所在目录添加到用户 PATH，也可每次使用它的绝对路径。下面从解压目录运行。

```sh
./bmem --version
./bmem init
./bmem web
```

```powershell
.\bmem.exe --version
.\bmem.exe init
.\bmem.exe web
```

默认数据根是当前用户家目录下的 `.bmem`。用 `bmem doctor` 核对程序发现的数据根，用 `bmem status` 查看本机服务。若自定义数据根，从初始化到后续命令都使用同一个 `bmem --home <绝对路径> …`。

### 首次启动提示

- **macOS**：预览包没有 Developer ID 签名与 Apple 公证。确认下载来源和摘要后，如果系统提供「系统设置 → 隐私与安全性 → 仍要打开」，可以为该程序授权。[Apple 首次运行说明](https://support.apple.com/en-us/102445)
- **Windows**：预览版可能触发 SmartScreen。Smart App Control 或组织策略也可能阻止运行。受管设备由组织 IT 审批。[Microsoft 下载信誉说明](https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation)
- **Linux**：保留解压后的执行权限。如果系统报告 glibc 版本不足，查看对应 Release 附件的测试环境，或在目标系统从源码编译。

## 写入并检索第一条记忆

```sh
bmem declare '所有助手的报告用中文'
bmem statement list
bmem search '报告语言'
```

工作台的「记忆库」可查看声明及修订。词法检索无需模型。自动提炼须先在「设置 → 模型服务」测试并绑定模型，再为选定来源开启提炼授权。配置模型、采集历史和共享声明各有独立权限。[智能体接入与授权](agent-integration.md)

## 备份、预览版升级和退出

- `bmem backup create` 创建本地备份。用 `bmem restore preview BACKUP_ID` 查看恢复条件。
- 公开预览通过 Release 手动下载。已有重要数据时先备份，在隔离环境验证新版本能打开原库和正常产生宿主回执，再切换生产实例。正式受管安装和 TUF 自动升级见 [007 验收说明](../specs/007-bmem-release-update/quickstart.md)。
- 停止本机服务用 `bmem service stop`。移除程序前，可在各宿主撤销或移除相应 MCP 和 Hook 登记。保留数据时将程序目录与数据根分别管理。

从源码安装可在仓库根运行 `cargo install --locked --path src/v2/bmem`。开发依赖见[贡献指南](../CONTRIBUTING.md)。
