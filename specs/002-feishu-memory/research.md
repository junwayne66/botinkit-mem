# 官方协议研究

核对日期为 2026-11-13，官方源码提交 `05e9c6ef74290f78601892ed9eb8a6389d8fc8ca`。

- 工具正式命令为 `lark-cli`，npm 包 `@larksuite/cli`，同时支持飞书与 Lark。复用可用安装，缺失时在用户数据目录托管安装固定 1.0.100。安装依赖 Node/npm，官方 postinstall 下载并校验平台二进制。失败给出修复入口。[官方安装源码](https://github.com/larksuite/cli/blob/05e9c6ef74290f78601892ed9eb8a6389d8fc8ca/scripts/install.js)
- 复用官方 local workspace 与 Keychain，通过 `profile list` 选择匹配品牌应用，每次显式 `--profile`，不修改当前默认 profile。无匹配应用时 `config init --new --brand BRAND --name bmem-BRAND`，从 stderr 提取官方配置 URL，成功后由 profile list 重查。[应用配置](https://github.com/larksuite/cli/blob/05e9c6ef74290f78601892ed9eb8a6389d8fc8ca/cmd/config/init_interactive.go)
- 认证采用 `auth login --scope ... --no-wait --json`，原样展示 URL 与二维码，内部持有 device_code，再由受管理子进程执行 `auth login --device-code ... --json`。设备授权输出为裸 JSON，业务 API 输出为 `{ok,identity,data,meta}`，分别处理。[认证源码](https://github.com/larksuite/cli/blob/05e9c6ef74290f78601892ed9eb8a6389d8fc8ca/cmd/auth/login.go)
- 用户可用状态为 identities.user.available，status 为 ready 或 needs_refresh，且 auth check 的所需 scope 均已授予。顶层 identity=bot 不代表用户已登录。空历史只申请 offline_access。
- 消息读取采用官方通用只读 API，按私聊和群聊分别申请权限，避免 shortcut 自动要求 reactions。聊天列表分页及消息分页显式完成，不使用默认仅十页的 page-all 截断。
- 事件走现有 Store.ingest，外部信任、private、history_import 标签，来源身份包括品牌、应用与用户。原始工具历史的归档跳过策略不适用于飞书业务内容。
- OAuth 受用户和组织权限约束，应用创建或授权需要管理员批准时展示官方状态。没有额外的 bmem 应用密钥输入步骤。

补充核验，v1.0.100命名profile追加不改变已有默认。Wiki URL由官方docs_ai读取路径解析token，当前无新增Wiki scope依据。官方搜索支持空query加doc/wiki筛选器，文档覆盖范围为该搜索可见的docx/wiki结果。
