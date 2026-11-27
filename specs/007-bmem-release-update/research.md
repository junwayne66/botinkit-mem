# 调研与设计选择

| 选择 | 仓库现状与采用理由 | 可复用来源 |
|---|---|---|
| GitHub Releases 存不可变制品，GitHub Pages 发签名渠道元数据 | 2026-11-27 实查公开 Releases 与 tags 均为空，源码安装是 README 的当前入口。发布草稿集齐六份制品、签名和证据后公开，再更新稳定渠道。用户端无需 GitHub 登录 | [项目 Releases](https://github.com/junwayne66/botinkit-mem/releases) · [GitHub 不可变发布](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) · [GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) |
| 采用 TUF 与现有 Rust 库 `tough` | 下载、可信根轮替、元数据有效期、更新序号、目标长度和摘要复用既有实现。本项目只编排目标选择、实例切换与恢复。集成任务锁定版本并验证网络重定向和持久缓存语义 | [TUF 规范](https://theupdateframework.github.io/specification/latest/) · [tough 与 tuftool](https://github.com/awslabs/tough) |
| 沿用 Cargo 与分平台构建，Windows 使用同卷独立更新程序 | V2 已是一个 Rust 可执行文件，CLI、HTTP、worker 和 Web 同包交付。Windows 上长期运行的原 EXE 可能阻止替换。原生替换 API 与占用诊断可复用，业务顺序由 bmem 管理 | [Rust 目标平台](https://doc.rust-lang.org/rustc/platform-support.html) · [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew) · [Restart Manager](https://learn.microsoft.com/en-us/windows/win32/rstmgr/about-restart-manager) |
| 同实例恢复点复用 SQLite 备份接口 | 当前 `archive::backup` 的业务归档会解绑客户端凭据。升级需要保留原客户端、登录状态以外的持久设置、授权和待办。停写后复制两库及迁移将改动的受管文件 | [SQLite Backup API](https://sqlite.org/backup.html) · [业务归档实现](../../src/v2/bmem/src/archive.rs) |
| 保留已登记的命令路径 | 实查本机 shell 入口链接到数据根里的 `bin/bmem`，已登记的登录服务也直接使用目标路径。源码安装还可能位于 Cargo 目录。先解析真实目标及调用方，原位版本切换后才能证明所有入口一致 | [宿主配置实现](../../src/v2/bmem/src/integrations.rs) · [运行时启动](../../src/v2/bmem/src/client.rs) |

Apple 路径采用 Developer ID 签名和公证。给首次安装用户提供可装订公证票据的分发容器，升级制品内的实际执行文件也纳入公证与 Gatekeeper 实机检查。Windows 制品使用 Authenticode 和时间戳。两个平台均先完成原生签名，再计算更新目标摘要。[Apple 公证](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) · [Windows SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
