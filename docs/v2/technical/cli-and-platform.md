# bmem 的安装、命令行与本机配置

首发面向 macOS、原生 Windows 和 Linux。每台机器为当前系统用户运行一份本地记忆库。三种平台都使用 `bmem` 命令，Windows 文件名为 `bmem.exe`。SQLite、全文检索和管理网页随程序分发；运行 CLI 或打开网页均无需另装语言运行时、Web 服务或数据库。本文规定 V2 实现目标与测试方法。

## 从安装到第一条可用记忆

1. **安装程序。** 从正式发布渠道取得与系统和处理器匹配的已签名制品。macOS 与 Linux 解包后执行 `./bmem install`，Windows 在 PowerShell 执行 `.\bmem.exe install`。安装到用户可写目录，显示可执行文件的绝对路径，并在需要时提供加入用户 PATH 的选项。新终端可运行 `bmem --version`。安装器只部署程序，数据目录由初始化创建。
2. **运行 `bmem init`。** 检查工作目录、可写空间与现有配置，创建本机所有者身份、配置文件及空库，启动或复用本次用户会话的回环服务。登录自启动可在向导内选择，也可后续启用。向导给出默认平衡模式，允许选择 Claude Code、Codex 或资料连接器，再选择「配置已有生成模型 API」或「用 Ollama 下载并运行本地生成模型」，核对其接收范围。向量相似检索默认关闭。宿主探测只读取安装情况。展示将新增的宿主接入项、采集类别、回填范围和接收端，按所选授权绑定。用户可在稍后用 `bmem web` 继续。
3. **看就绪结果。** 分项显示「本地库可用」「宿主已登记／待授权」「模型已配置／已实测」「自动提炼就绪／待完成」及后续命令。选择模型后可用不含用户资料的合成输入验证结构化候选。仅网络可达时显示“连通”，通过结构化提取测试后再显示“提炼可用”。没有获准可用模型时，可先做声明、授权与词法查询，已获准的普通事件持久排队。完整首发验收还须用 Claude Code、Codex 的实际输入和回执走通自动提炼及接手。[宿主与提炼验收](../product/acceptance.md)
4. **再次运行。** 已初始化时列出当前值与待配项，只改本次明示的选择。中断后从已落盘的步骤继续，复用所有者、来源及游标；登录授权过期时只恢复对应连接器。无终端交互可用 `bmem init --non-interactive` 建立本地核心，随后用相同管理命令或 Web 完成授权。已有提供方、模型 ID 和密钥引用时可复用其配置；无交互且缺少选择时保留“待配置”，只在显式指定本地选项和允许下载时拉取模型。无输入终端、没有可选模型或无浏览器时均能返回机器可读状态。

典型的首次操作只有 `bmem init`。下面是用户决定稍后配置时的一条替代路径；`extractor` 是用户给模型起的示例名称，添加和绑定时由向导收集实际端点、模型标识及允许的用途。

```text
bmem init
bmem connector add claude-code
bmem connector add codex
bmem model add extractor
bmem model test extractor
bmem model use extractor --for extract
bmem doctor
bmem web
```

## 生成模型与向量检索分开配置

| 配置时点 | 命令行与 Web 的同一选择 | 执行与回执 |
|---|---|---|
| 首次配置自动提炼 | 选择「已有生成模型 API」。填写协议、端点、模型 ID 和凭据引用，选择可处理的来源范围 | 用合成输入测试结构化候选，成功后绑定 `extract` 用途。真实资料只有在用途和实际目的地均获准时才会送出 |
| 首次选择本机生成 | 选择「本地 Ollama」，使用已有服务与已有模型，或从兼容清单选一个生成模型下载 | 先检测本机 Ollama 及版本，再列出已安装模型并核对模型名/摘要；已存在且通过能力测试可直接复用。未安装运行时则提供对应系统的官方安装入口，安装后回到同一步。下载前展示模型标识、用途、已知大小和可用空间；用户确认后调用 pull，记录进度、错误与重试入口。网络中断后可重试同一模型，成功后按实际摘要核验，并用结构化提炼测试确认可用。[Ollama 模型列表](https://docs.ollama.com/api/tags) · [模型下载](https://docs.ollama.com/api/pull) · [结构化输出](https://docs.ollama.com/capabilities/structured-outputs) |
| 初始状态或关闭向量 | `bmem config get vector_search.enabled` 返回 `false`；Web 显示「向量相似检索 · 关闭」 | 词法查询、证据补读和生成提炼照常工作。初始化、查询、启动与常规诊断跳过 embedding 模型探测、下载、装载、调用及向量索引构建。此时允许读出已保存的本地配置，以便日后恢复 |
| 显式启用向量 | `bmem config set vector_search.enabled true` 或打开 Web 开关；接着选已有 embedding API，或用本地 Ollama 复用／下载带 embedding 能力的模型。CLI 可执行 `bmem model add vectors`、`bmem model test vectors --for embed`、`bmem model use vectors --for embed` | 配置请求可记录为“已启用／待配模型”。仅在已选端点、用途授权和受控测试就绪后建索引。记录模型 ID、模型版本、向量维度和可检索水位；未建完时按实际覆盖提示或回落词法。生成端点与向量端点各自可测试、切换和报告失败。[Ollama embedding API](https://docs.ollama.com/api/embed) |
| 关闭或改换模型 | `bmem config set vector_search.enabled false`，或 `bmem model use <name> --for extract` / `--for embed` | 关闭后停止新 embedding 请求及相关作业，停用向量读取路径，保留模型绑定和索引清单供再次启用。重启和重复 init 沿用已有选择。改换生成模型只改提炼绑定；改换 embedding 模型在新索引空间重建，通过就绪检查后切换。解绑、关闭及卸载 bmem 均不得删除用户已有的 Ollama 模型或终止其他应用共用的 Ollama 服务 |

向导对选定的本地运行时使用官方平台安装、健康检查与模型拉取能力。已装 Ollama 的模型存储归该运行时管理，bmem 登记模型引用及摘要。探针在所选推理后端支持相应能力时才显示“就绪”。每个 OS/CPU 的运行时、模型和内存需求随真实制品验证，单独列明可走本地推理的组合；本地推理不受支持时仍可选择已有生成 API。[macOS 安装](https://docs.ollama.com/macos) · [Windows 安装](https://docs.ollama.com/windows) · [Linux 安装](https://docs.ollama.com/linux)

## 日常命令及网页对应操作

输入 `bmem --help` 查看命令组，输入 `bmem capabilities list` 看本机已注册的能力、状态及 Web 对应入口。安装、服务、自启动、更新、模型和接入向导的完整拼写，与声明、查询、治理、备份恢复、作业及后续命令统一列在[子命令与 Web 操作契约](command-catalog.md)。该表约定每个用户操作如何映射到同一应用命令和回执。

CLI 管理命令的 `--json` 结果写标准输出，诊断写标准错误。服务停机时，`doctor` 和 `status` 直接报告本机目录与进程状态。`declare`、`config set` 等业务命令先解析已初始化的实例，服务未运行就按该根的进程锁启动或复用服务。身份握手通过后才提交原请求。启动失败显示原因及 `bmem doctor`，提交后断线沿用原幂等键重试。未初始化则提示 `bmem init`。网页断线时保留草稿，提示用 `bmem web` 拉起同一实例，重连后回读修订及在途结果。两端均不离线写库。生成模型待配时本地核心仍可用，向量关闭时跳过嵌入探测。

## 工作目录与配置所有权

macOS、Linux 默认使用当前系统用户的 `~/.bmem`，Windows 默认使用 `%USERPROFILE%\.bmem`。主目录位于网络卷时，可在首次初始化中指定 `bmem --home <本机卷绝对路径> init`。安装时在业务根外准备一个仅当前用户可写的定位文件。位置为 macOS 的 `~/Library/Application Support/bmem/<machine>/active.json`、Windows 本地应用数据目录的 `%LOCALAPPDATA%\bmem\<machine>\active.json`、Linux 的 `~/.config/bmem/<machine>/active.json`。`~` 取系统账户主目录，`<machine>` 是本机操作系统稳定标识的散列，以便共享主目录的两台机器分别定位。此小文件只需单文件原子替换，不依赖 SQLite/WAL。路径不可读或不能安全替换时，诊断给出文件位置和修复原因。

`bmem doctor --json` 和网页「运行诊断」分别列出 `locator_volume`（定位文件所在卷、原子替换能力）与 `data_volume`（实际业务根所在卷、SQLite WAL 条件）。每项返回可确定的路径、检查结果与原因；无法解析业务根时该项标为未知。验收时分别令定位文件不可替换、业务卷不支持 WAL，检查诊断能指出各自失败的卷。服务停机时 CLI 可独立报告这两项。

**定位与创建顺序。**

1. 安装器首次写入 `{"version":1,"state":"fresh"}`，升级与重装沿用已有登记。定位丢失但仍有安装登记、登录启动项、宿主登记或默认根内的实例标识时，安装器进入修复、列出可核验路径，不能重新标为 `fresh`。活动根失联时保留原登记。所有入口按**显式全局 `--home` → 已登记的根 → 首次使用的默认根**选候选路径。路径转成规范绝对路径，处理空格、非 ASCII、符号链接及 Windows 目录联接。已绑定的实例即使传了 `--home` 也须匹配当前登记；与活动定位不同即退出并报告两条路径。
2. 仅 `init` 可在明确的首次安装状态创建业务根。先验证目标为新建或空目录、可写且位于支持 WAL 的本地卷。在同一把本机初始化锁内登记 `initializing`、目标绝对路径和随机 `instance_id`，再写根标识及空库；验证后将定位文件原子替换为 `active`。示例为 `{"version":1,"state":"active","root":"/Volumes/LocalData/bmem","instance_id":"b0855638-0721-42e2-909c-b512a7a7cf54"}`。同一 `init` 中断后复用已登记的路径与 ID 完成剩余步骤；重复运行只打开同一库。
3. 已初始化时先核对定位记录、所选路径、根内 `instance.json` 的 `instance_id`、所有者及本地卷条件，再以只开既有库的方式打开数据，通过该根的 `run/` 记录与服务做实例身份握手。CLI、Web 启动器、hook、MCP、登录启动器及升级/卸载程序共用这个解析器。已登记根无法访问、根标识不符、定位文件损坏或缺失、运行服务不属于同一实例时，停止业务读写并报告冲突位置。诊断和 `init` 修复可以读取可核验的登记，不静默回退到默认目录建库。
4. 丢失定位文件时，所有者用 `bmem --home <原业务根> init`，比对根标识和已有安装或宿主登记的根路径及实例 ID。能证明确是原实例才重建定位文件。无法证明是原活动根时保持停服；可从具有独立撤回清单的备份，按新环境恢复流程核验。数据根迁到其他卷时，`--home` 仍标识原控制实例。所有者用 `bmem restore preview <bundle> --target-home <目标绝对路径>`，网页「恢复预览」可填写同一目标。`restore apply <preview_id>` 只接受已绑定来源、目标和水位的预览。新机器先由 `init` 登记本机实例，恢复应用保留本机 `instance_id`；备份携带的库世代用于数据校验。预览与孤本隔离查看均不改生产定位。[恢复核验](operations.md)

| 工作目录成员 | 内容与写入方 |
|---|---|
| `instance.json` | 首次初始化生成的本机实例 ID，供根外定位、服务握手及登记修复比对。它与归档中的库世代分开，恢复时不从备份覆盖 |
| `data/settings.sqlite3` | 本机设置，包括配置修订、压缩档、模型端点、用途绑定及连接器设置。网页提交的模型密钥单独存于 `model_api_keys` 表。首次打开旧数据根时从 `config.toml` 导入设置 |
| `data/bmem.sqlite3` | 唯一的权威业务库，含来源身份、用途授权与撤回、修订、游标、回执、待办和索引水位。WAL 及共享内存由 SQLite 管理 |
| `objects/`、`indexes/` | 受管的大型证据对象、可重建的派生索引。受管内容、版本引用及清理范围按数据格式记录 |
| `models/` | 识别出的本地模型清单、已验证的摘要与能力；启用的用途绑定写在配置文件。Ollama 权重默认位于所选运行时自己的模型存储，bmem 直接引用。使用已有 API 时此目录可为空 |
| `run/`、`logs/` | 用户私有的服务发现文件、进程锁和有界运行日志。日志只保留操作 ID、能力、版本和错误类别 |
| `backups/`、`tmp/` | 受管快照、临时导出及分步重建的暂存。需要跨灾难恢复时另存最新撤回清单。程序升级暂存于安装目录所在卷，避免跨卷替换可执行文件 |

**一次配置，两种入口。** CLI 和 Web 共用经过身份认证的本地配置服务。管理页取 `GET /v2/config`，使用 `PATCH /v2/config` 携带配置修订和需改的字段；CLI 走同一条命令。配置服务检查修订，在同一目录写临时 TOML、刷新并替换配置文件，成功后发布新的内存快照。`POST /v2/models/{id}/test` 只处理合成测试输入，并返回连通、认证、模型识别、结构化响应各环节的观测。两端同改过期修订返回 `409 revision_changed`，刷新展示差异。连接器接入、停用和收窄权限通过已有 `sources/clients/grants/operations` 契约写业务库。仅在 TOML 中添加端点或勾选采集类别不足以授予来源权限；实际许可和当前游标以业务库为准。[已有领域契约](contracts.md)

**凭据与宿主。** 常驻服务按当前 OS 用户访问系统凭据库（macOS Keychain、Windows Credential Manager、可用的 Linux Secret Service）；无桌面凭据库的 Linux 可采用服务进程环境变量名作为密钥引用，服务端取当前环境值，缺引用时明确报待配。CLI 通过掩码交互或标准输入接收密钥，Web 的密钥字段只接受写入与替换。列表、导出、命令参数及诊断不返回密钥值。Web 仅绑定回环地址，本机 CLI 通过一次性登录码建立所有者会话；浏览器写请求校验会话与同源。配置端点只对所有者开放，宿主使用各自的绑定凭据。MCP stdio 入口及宿主 hook 仅转送给本机服务；无装入回执的适配器按“已提供／装入未知”呈现。[现有装入协议](contracts.md) · [Apple Keychain](https://developer.apple.com/documentation/security/keychain-services) · [Microsoft 凭据写入接口](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew) · [Secret Service API](https://specifications.freedesktop.org/secret-service/latest/)

## 安装、启动与升级的系统差异

launchd 的参数数组、Windows 登录计划任务及 systemd 用户 unit 都保存可执行文件绝对路径及 `--home <已解析的绝对根> service run`。hook 和 MCP 配置同样把绝对路径作为独立参数，登记清单同时保存 `instance_id`。升级器、卸载器从本次登记传递相同根及预期实例 ID；开始动作前再与活动定位及根标识核对。位于同一程序目录、同一端口或持有旧宿主登记均不足以另选实例。

| 发布目标 | 用户安装与登录启动 | 平台验证点 |
|---|---|---|
| macOS arm64、x86_64 | 分发 Developer ID 签名并经公证的制品，用户安装命令复制到 `~/.local/bin/bmem`。选择登录启动后登记 `~/Library/LaunchAgents/` 的用户 LaunchAgent；用户退出后随登录会话结束 | 用干净账户核验 Gatekeeper、签名与公证、登录后服务、升级后重启。记录发布制品的最低系统版本。[Apple Developer ID](https://developer.apple.com/developer-id/) · [Apple 用户 LaunchAgent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) |
| Windows x64、arm64 | 分发签名原生 EXE，安装到 `%LOCALAPPDATA%\Programs\bmem\bmem.exe`。可选当前用户登录触发的计划任务，使用该用户交互令牌和普通权限。服务发现及数据根使用当前用户 ACL | 测 PowerShell 与 cmd、用户级 PATH、含空格路径、签名验证、登录任务及更新时 EXE 占用。独立测原生 Windows。[Microsoft 任务安全上下文](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks) · [SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) |
| Linux GNU x86_64、aarch64 | 分发按架构构建的归档及签名清单，安装到 `~/.local/bin/bmem`。可用 systemd 用户管理器时注册用户 unit；没有用户总线或 systemd 时 `bmem service run` 可由终端或既有用户进程管理器托管 | 在最低目标 glibc 环境执行实际制品并记录 ABI；分别验证桌面、无桌面、无用户服务管理器。后者显式显示可用的启动方式。[systemd 用户 unit 搜索路径](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html) |

**程序升级事务。** `bmem update` 按当前渠道、OS 和 CPU 选择制品，核对内置发布信任根签署的清单及制品摘要，暂存新版本。核对数据库格式和可用空间，再暂停接收新写、完成当前短事务并停止服务。保存上一可执行文件与必要的受管恢复点。Windows 将切换工作交给独立暂存副本，等正在运行的 CLI、服务和相关 stdio 适配器释放旧 EXE；超时列出仍占用的 bmem 实例和重试操作。其他平台同样在健康验证前保留上一程序。由暂存程序切换、启动，核查版本、库格式和词法读取后才开放写入。记录更新进度，下一次启动可从中断阶段继续或退回。[Microsoft 使用中文件的更新与重启机制](https://learn.microsoft.com/en-us/windows/win32/rstmgr/about-restart-manager)

签名或制品校验失败时当前版本继续服务。若新程序启动失败且尚未开放新写，恢复上一程序；如果更新包含不可逆的数据格式改动，先在隔离副本验证升级，失败时使用切换前一致性快照并复核撤回水位。开放新写后再发现错误时，须先确认旧程序能读取当前格式及全部新写入，或实施经过核验的前向修复。[备份与恢复](operations.md)

`bmem uninstall` 与网页「关于 → 卸载程序」都调用同一本机维护程序。卸载范围取自安装与宿主接入时的登记清单，先停止接收和启动项，再移除仍属于该实例的宿主配置片段、其启动参数和可执行文件。宿主配置被其他程序改写时，保留改写内容并报告待处理项。`connector configure <id> --unbind-client <client_id>` 只移除该客户端的托管 hook/MCP 绑定并停用其凭据。两种操作都保留数据根和定位文件。Windows 使用独立暂存程序待 EXE 释放后完成。维护程序按操作号写入保留的数据根，重装后先核对原实例，再用 `bmem operation show` 查原单据。数据治理仍由来源遗忘和备份操作负责。

## 三平台交付验收

| 场景 | 必须保存的运行证据 |
|---|---|
| 六个目标各取干净、普通用户环境 | 实际安装制品及摘要、OS/CPU/ABI、首次运行和重复 init 输出、工作目录、服务进程数；`--version` 和核心 CLI 在未安装 Python、Node、数据库客户端时通过 |
| 三平台分别把账户主目录设在网络卷，首次 `--home` 选择本地盘，重开终端并重新登录 | 用无 `--home` 的 CLI 修改声明，再由 Web、hook、MCP 和登录启动的服务读取。比对定位、各进程实际根、同一 `instance_id`、声明修订与撤回水位。重复 init、升级、解绑单个客户端及卸载重装均不得在默认目录出现第二个库 |
| 损坏或移走定位，换入另一个实例目录，或给旧启动项传入冲突根 | 普通命令退出并指向诊断，两个根均无新写。用可核验的原登记修复后再次读取原修订；恢复预览期间生产定位不变。尝试将业务根放在不支持 WAL 的卷时无活动库和服务 |
| 停服务后在 CLI 新增声明、Web 保留未提交草稿再重连 | 有效登记只拉起一个服务，原命令只生效一次，两端回读同一修订。启动失败时不离线写库；新用户直接执行写命令仅获得初始化指引 |
| 首次完全离线、无模型，随后配齐 | 声明/授权/词法可用；自动提炼显示待配；CLI 添加模型后 Web 读到同一修订，在 Web 修改后 CLI 回读，测试结果与实际授权可分别核对 |
| 本机已有 Ollama／模型，或新模型下载中断 | 复用前者的端点与模型摘要，验证二次 init 的下载量为零；下载中断后重试，观察最终摘要与结构化候选。切换或解绑后其他程序仍能访问原有模型 |
| 默认关闭、随后单独启停向量 | 捕获模型出站请求与任务队列；默认 embedding 探测、下载、请求及建索引次数均为零。显式启用时分别走 embedding API 与本地下载两条路径，再关闭，确认词法与生成提炼正常，后续 embedding 调用为零 |
| CLI 和浏览器编辑同一旧配置 | 一方成功、一方得到修订冲突，重启后读取一致；停采、恢复和撤回不会因重跑 init、修改 TOML 或切档而失效 |
| 平台登录、重启、无 GUI、端口已占用 | 三个用户级启动器分别验证；已有实例复用。占用端口时使用记录的可用回环地址或报告显式端口冲突。无浏览器时从 CLI 完成相同配置 |
| 正常升级、签名错误、Windows 文件占用、启动失败 | 记录更新前后版本、签名校验结果、是否成功恢复服务、权威库水位及撤回后禁读对照；确认结果可由 `bmem doctor` 回读 |
| 由 CLI 或 Web 卸载后重新安装 | 登录启动项和自身宿主绑定已解除，用户的其他配置和模型仍在。重装后可读原有授权、声明及该次卸载单据，重新登记宿主时显示实际接入状态 |
| 已授权普通对话及工具回执 | Claude Code 与 Codex 各自的真实系统/宿主版本、采集与装入能力、模型目的地、一次自然任务接手和独立结果。制品可安装与宿主提供某个 hook 的支持情况分别出报告 |

首发发布清单逐架构登记最低 OS 版本、Linux ABI、验证机器、安装制品及宿主能力。以上均是待实施验收，当前仓库内的设计原型和既有 Python 测试只覆盖其各自记录的范围。
