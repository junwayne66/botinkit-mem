# bmem 0.3.0 内部测试版验收

2026-11-27 · Asia/Shanghai

## 验收范围

使用独立的本地 TUF 渠道。候选由 `internal-release` 构建，嵌入受信任的公开根。正式渠道继续使用原有验收台账。0.3.1 由同一源码生成，只用于验证连续升级和回退。Windows x64、ARM64 本轮暂缓构建和环境验收。 [007 台账](tasks.md)

| 项目 | 记录 |
|---|---|
| 0.3.0 与验收后继源码提交 | `2e07cf33c06aed3a8a554156c726240ae3cce89a`，从本轮 Rust 与 Web 源码分别固定，保存在本地隔离快照 |
| 真实 0.2.0 来源 | 审核提交 `12ba926685005eb236045b0aaea5072ffe4c6cb5`。对应平台的构建摘要见各验收回执 |
| 内部 TUF 公开根 SHA-256 | `ccc2dc73785afee8cac864f86058cb69465cad3076bddc3b259b5f71f065b87e` |
| 角色与版本 | Ed25519 `root`、`targets`、`snapshot`、`timestamp` 各有独立本机私钥。首装和后继渠道的 `targets`、`snapshot`、`timestamp` 版本分别为 1、2 |
| 核验工具 | 程序使用 Rust Tough 验证渠道和目标。独立 Python TUF 7.0.1 按已知公开根核对签名、版本并回读每个制品的完整字节 |

每组验收在普通用户的隔离目录运行实际程序。TUF 目录和制品只在授权测试位置暂存。macOS 运行使用本机构建制品和自管 TUF，证据范围为该配置下的真实执行。正式 GitHub Release 与公开渠道推广留给发布验收。 [发行手册](../../scripts/release/README.md#030-内部测试)

## 场景与判定

| 场景 | 通过条件 |
|---|---|
| U01 首装 | 空安装目录部署已签目录中的 0.3.0，确认版本、构建、实例和实际程序摘要。修改渠道附带的公开根文件也无法替换编译时信任根 |
| U02 接管 | 用真实 0.2.0 生成测试数据和 MCP 登记，经受管安装器替换原路径。确认原实例、声明、撤回、源授权、游标与直接 MCP 连接 |
| U03 升级 | `bmem update` 将 0.3.0 替换为 0.3.1，复查显示已最新。随后重放旧签名目录，拒绝更新并保留已安装文件 |
| U04 / U06 失败与恢复 | 拒绝坏时间戳签名、被篡改的已签目录和错误制品摘要。在同一程序真正完成替换后阻断服务端点发布，自动恢复上一程序与两库，清除障碍后重试成功 |
| U07 兼容回切 | 开放新版写入后回切兼容程序，保留新的写入、撤回和升级高水位，再次升级正常 |

Claude Code（qwen4-max）复核了编译时信任根、签名目录与制品绑定以及重放风险。按复核建议增加了在实际 `bmem update` 上的旧一代目录重放和同长度目录篡改验收。独立签名工具 4 条、正式制品核验回归 7 条、Rust 既有单元测试 40 条均通过。另实测拒绝在内测构建启用故障注入或冻结时钟。

## 制品与运行证据

**四个目标共 20 / 20 组通过。** 解包后再次核验两代目录的 TUF 签名与全部资产字节，并用解包的 macOS arm64 文件再跑五组，结果全部通过。

| 目标 | 实跑环境 | 五组结果 |
|---|---|---|
| macOS arm64 | Apple Silicon 原生，macOS 26.3 | [5 / 5](evidence/internal-mac-arm-final.log) · [回执](evidence/internal-mac-arm-final.json) |
| macOS x64 | Apple Silicon 的 Rosetta，macOS 26.3 | [5 / 5](evidence/internal-mac-x64-final.log) · [回执](evidence/internal-mac-x64-final.json) |
| Linux arm64 | Debian Bookworm，glibc 2.36，ARM64 Docker 普通用户，断网 | [5 / 5](evidence/internal-linux-arm64-final.log) · [回执](evidence/internal-linux-arm64-final.json) |
| Linux x64 | x86_64 WSL，glibc 2.35，普通用户，独立回环网络 | [5 / 5](evidence/internal-linux-x64-final.log) · [回执](evidence/internal-linux-x64-final.json) |
| Windows x64、ARM64 | 本轮暂缓 | 未构建、未运行 |

Linux x64 使用同一 TUF 根签出的单目标镜像。两代 x64 程序的摘要均与四目标归档一致，WSL 临时目录已清理。目录篡改由 Tough 拦截，当前呈现 `update_transport_failed`。签名时间戳篡改呈现 `update_signature_invalid`，制品篡改呈现 `update_asset_digest_mismatch`，旧签名目录重放呈现 `update_metadata_rollback`。

| 归档项目 | 记录 |
|---|---|
| 内测文件 | `bmem-0.3.0-internal-2026-11-27.tar.gz`，维护者离线归档 · SHA-256 `d86071b62bc943fdbeb6013cb9e4e6180bf53c1c86e8d88c046ad9a05f50126a` |
| 解包后独立复核 | [0.3.0 四目标](evidence/internal-archive-release.json) · [两代目录八制品](evidence/internal-archive-acceptance-successor.json) · [macOS arm64 解包实跑](evidence/internal-archive-mac-arm.json) |
| 当前首装目录有效期 | TUF 时间戳有效至 2026-12-04 13:55:13（Asia/Shanghai） |

内测归档含两套已验签目录、公开根、二进制和安装说明。四把签名私钥由本机用户专用目录保管。归档仅落本地。正式签名与公开渠道验收仍依照 T013–T015。 [正式发布状态](closure-validation-2026-11-27.md#正式发布状态)

## Mac arm64 大库接管修订

受限探针在完成 SQLite 完整性、撤回及全文检索检查后才开始监听。修订版将此阶段的等待预算独立设为 5 分钟，更新锁和常规服务启动沿用 12 秒。原恢复点和提交前自动回退流程继续适用。

修订版以新私有目录和自管 TUF 第 3 代元数据分发，使用独立的构建标识，仅用于 Mac arm64 上的空目录安装或 0.2.0 一次性接管。已独立验签并确认交付二进制不接受测试故障注入。验收使用用户授权的同一份多 GB 备份，原始证据存本机。

| 场景 | 实测结果 |
| --- | --- |
| 13 秒受限探针 | 原 12 秒对照构建自动回退，修订构建完成接管 |
| 受限探针达到新等待上限 | 310 秒故障注入触发回退，运行中的 0.2.0 恢复，两份 SQLite 完整性及数据水位核对通过 |
| 签名制品用同一份备份接管 | 迁移完成到提交约 55 秒，服务恢复，`bmem update --check` 返回 `up_to_date` |
| 签名制品用同一份备份回退 | 使受限探针无法登记监听地址，安装器退出 22，恢复原程序、两库格式与数据水位，解除写入屏障 |
| 接管后隔离环境宿主 | Claude Code 与 Codex 原生 MCP 检索和 `SessionStart` 回执均通过 |

第 3 代制品首次接管实际 LaunchAgent 时，`bootout` 成功返回后任务仍处于 `SIGTERMed`，安装器在迁移前停止。核实另一旧客户端自启了同实例服务后，用原程序停掉该服务，再以签名安装器 `update resume` 恢复原 LaunchAgent 和 0.2.0。恢复点未进入迁移阶段。

第 4 代将 launchd 注销纳入限时等待，并对仍存在的同实例服务执行认证排空。用隔离原生 LaunchAgent 人为延长 `SIGTERMed` 状态，旧构建复现失败，修订构建成功。按需启动和 KeepAlive 的两组既有原生用例也通过。实机完成停写和两库快照后，迁移预演的临时 locator 仍读取了真实默认数据根，初始化被原有绑定检查拒绝。安装器退出 22，恢复原 LaunchAgent 和 0.2.0，已核对独立保存的两份停写快照。

第 5 代同时隔离预演的默认数据根和 locator。复验用已登记默认数据根的布局，覆盖真实机器触发的绑定保护。

| 第 5 代验收项 | 结果 |
| --- | --- |
| 默认数据根已登记的接管回归 | 第 4 代在迁移前回退，第 5 代成功接管，按原实例定位 |
| 最终签名制品和同一份大库备份 | 迁移完成至发布约 27 秒，`update --check` 为 `up_to_date`，两款原生宿主的 MCP 检索和 `SessionStart` 投递回执通过 |
| 最终签名制品的探针故障 | 另用小库阻断端点发布，退出 22，两份 SQLite 完整性复核通过，原程序及写入屏障恢复到预期状态 |
| Rust 单元测试 | 40 / 40 通过 |

最终签名归档为 `bmem-0.3.0-mac-arm64-health-r3.tar.gz`，SHA-256 `4fdf7c026b3e2b79b64ca95bbc18073b419ed08e0be776c569950162a8373faa`，仅存本机。

### 本机实际接管

第 5 代自动接管原 LaunchAgent 的实机尝试分别因服务锁等待与 launchd 注销确认回退，原 0.2.0 和原启动项恢复。实际接管改为显式卸载并等待原 LaunchAgent 完全注销，确认服务锁空闲，以相同签名制品完成受管接管，再加载原 plist。

| 验收项 | 实测结果 |
| --- | --- |
| 程序、实例与启动项 | 0.3.0 以原 LaunchAgent 和原实例运行，程序摘要、构建标识与签名制品一致，宿主登记核对通过 |
| 数据库与恢复点 | 两库停写恢复点独立保存、摘要均通过，权威库达到写入世代 2，写入屏障解除 |
| 更新检查 | 实机 `bmem update --check` 读取私有 TUF 第 5 代目录并返回 `up_to_date` |
| 原生宿主 | Claude Code、Codex 各通过 MCP 检索和真实 `SessionStart` 投递，回执在 SQLite 中核对 |

本轮为 Mac arm64 本机内测接管。Windows 验收继续暂缓，本轮未公开发布。原始回执和配置备份保存在本机私有验收目录。

### macOS LaunchAgent 自动交接复验

使用独立 UUID 标识的原生 LaunchAgent 和同一份 3.9 GB 历史备份。现有 macOS 0.3.0 安装留在原运行环境。测试候选使用仅供隔离验收的受管入口和本地 Ed25519 签名目录。Claude Code 复核了启动项状态识别、同实例认证排空和预检回退边界。

| 原生回归 | 修复前 | 修复后 |
| --- | --- | --- |
| 原 PID 保持 `SIGTERMed` 36 秒 | 旧 35 秒注销限时导致 `revision_changed` 并回退 | 独立注销窗口内自动接管，原启动项启动新 PID |
| 旧独立服务取得锁并发布新端点 | 只排空一次，最终 `update_in_progress` 并回退 | 等锁期间向当前同实例端点重试认证排空，再完成接管 |
| 实际超过 90 秒仍未注销 | 旧代码使用 35 秒通用等待 | 返回 `launchd_retirement_timeout`，未进入快照或换程序，恢复原 0.2.0 与同一启动项 |

KeepAlive 与按需启动的两项原生回归均通过。Rust 单元测试 40 / 40 通过。仅在取得独占服务锁后进入快照、替换、迁移或恢复数据库。

| 大库验收项 | 实测结果 |
| --- | --- |
| 自动停写与原位接管 | 自动取得服务锁并拒绝写入，保留双库恢复点。约 308 秒后发布 0.3.0，原标签的新 PID 持有监听端口和服务锁，`update --check` 返回 `up_to_date` |
| 迁移后自动回退 | 阻断候选探针的端点发布，退出 22 并返回 `candidate_health_failed`。约 487 秒后恢复原 0.2.0、相同实例和双库水位，原标签的新 PID 持有监听端口和服务锁 |
| 恢复与清理核对 | 两条路径的 plist 字节一致，两库完整性检查通过，写入屏障解除，恢复点和隔离启动项均清理。历史备份的两份摘要复核一致 |

现有 Mac 0.3.0 的程序、plist、进程 PID 和运行状态与本轮验收前一致。Windows 继续暂缓，未公开发布。

后续采集与提炼连续性改动及 Claude Code 实跑见[独立验收报告](continuity-validation-2026-11-27.md)。
