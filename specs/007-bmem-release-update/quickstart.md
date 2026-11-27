# 验证与发布演练

下面是实现后应执行的验收方案。使用临时安装目录、数据根、宿主配置和测试签名仓库。

| 编号 | 入口与操作 | 发布证据 |
|---|---|---|
| U01 | 各目标在普通用户空环境验证签名并首装，运行 `bmem --version`、`bmem init`、`bmem update --check` | shell 路径、构建、最低 OS/ABI、单实例及正确渠道 |
| U02 | 用真实旧 0.2.0 生成声明、源授权、撤回、队列、导入游标和已绑定 MCP。安装器接管后执行同样的查询与真实宿主请求 | 更新后 CLI、登录启动项、Web、Claude Code、Codex 使用同一构建与原实例，保留授权及游标 |
| U03 | 多次运行 `bmem update`，覆盖启动中、初始停止、重复任务、PATH 多副本及旧链接。另用一个测试稳定版本验证连续升级 | 一次切换、最新结果如实返回，系统级管理器安装及其他实例的内容可核对 |
| U04 | 下载中断、过期/回退元数据、签名和哈希不符、错误架构、磁盘不足 | 文件、进程和数据库水位对照；已有服务继续可用 |
| U05 | 并发 update、CLI、宿主 hook 与自动启动器；持久作业面临取消、恢复和重试 | 最多一个权威写者；同幂等键仅一次效果；游标及撤回保留 |
| U06 | 在每个持久阶段停止进程，另测更换后的构建错误、权限错误、迁移失败、健康发现端口冲突、终态清理中断及重试 | 原程序及数据摘要，恢复点完整性，账本对应目录的删除边界，断点前后运行状态 |
| U07 | 开放新写后新增更正与撤回，再制造启动错误；分别测试可兼容和不可兼容最低旧版本 | 兼容时只回切程序。不可兼容时前向修复后，同一实例仍可读更正、撤回继续有效 |
| U08 | Windows 真机用 stdio 进程保持旧 EXE 占用。保留旧版本继续服务，释放占用后重试 | 交接状态、占用原因和原生签名。x64 与 ARM64 分别验收 |
| U09 | release 草稿资产缺一项或某目标签名/验证失败。修复后再发布，核对域名上的稳定目标和下载内容 | 不完整时稳定目标保持原版本；完整后外部取回资产，与发布证据哈希相符 |
| U10 | 运行既有门禁及 006 原样本。对最终签名的生产包复做 U01–U09 中对应入口检查 | 134/134、原样本哈希、两款真实宿主和六目标生产包迁移记录。测试时钟构建与生产包分别记录 |
| U11 | 在升级成功和提交前回退两条路径中，分别保留已确认事件、未领取提炼作业、未完成的作业和断点游标。让宿主在停写期间尝试写入 | 维护窗口有明确反馈。放行后重启和领取租约均正确，已确认事件零遗漏，同输入只生成一次有效结果 |
| U12 | 停写期间分别生成可回读的宿主事件和无法回读也无法重投的事件。再模拟提交成功但确认回执丢失 | 自动补录有授权且可回读的事件，重投沿原键去重。不可恢复范围有明确缺口。撤回、关闭采集或无权限的记录不会被重新导入 |
| U13 | 让当前模型路线持续断连、超时、429 或 503。在此期间继续采集并重启服务；随后恢复模型，另测配置无效 | 明确提醒，等待和积压可持久查看。新事件仍入库，作业重试预算不被故障等待耗尽。模型恢复后自动续跑；需修复配置时有对应指引 |
| U14 | 在健康路线注入一条可重试的数据错误，另送正常项。让首次加三次重试均失败，再由所有者重新排队 | 确切四次尝试后停止自动领取。失败队列保留错误与历史。正常项继续完成，原作业可重新排队，已成功结果不会重复入库 |
| U15 | 同一路线在 10 分钟内连续三个不同作业出现同类错误，令探测失败；再恢复模型。另测单个坏作业反复失败和正常零候选 | 共同故障暂停受影响路线、提醒并保留积压。恢复后自动试跑和继续处理。采集入队及既有记忆查询继续；单条坏数据和零候选不会触发全局停止 |
| U16 | 用含旧版 `failed`、未到期租约和已撤回输入的队列升级，随后多次重启并重复执行重新排队 | 旧作业保留 ID、原始失败信息和已确认结果。有明确的次数与状态迁移规则；重复操作不并发重复处理，已撤回输入不可重放 |

大库接管补充 U06 验收。用只读在线备份生成同规模隔离副本，复跑最终签名制品的接管、`bmem update --check`、宿主检索与 Hook。在单独的失败场景验证提交前回退及清理，记录探针就绪时间、两库完整性、原程序摘要及数据水位。备份和宿主原始回执仅存本机。

在含 006 验收夹具的工作树可先运行以下现有检查。冻结时钟与真实宿主的完整环境沿用 [006 的复跑说明](../../research/memory-upgrade-technical/tests/README.md#最终对比复跑条件)。

```sh
python3 -B research/memory-upgrade-technical/tests/score.py
cargo test --locked --manifest-path src/v2/bmem/Cargo.toml
```

每次升级演练记录版本与构建 ID、旧/新程序哈希、实例、库格式、权威和撤回水位、宿主调用路径、结果状态和停写耗时。使用普通用户验证和有计划的受控故障，准备发版时再对最终制品复测。

## 在隔离环境复验本地候选

将 0.3.0 和较新的测试版本分别编译并保留两个文件。两次都启用 `update-test`，用 `BMEM_BUILD_COMMIT` 写入本次完整 Git 提交标识；仅第二次设 `BMEM_BUILD_VERSION=0.3.1`。旧版使用本平台已冻结、实际运行的 0.2.0，保存其 SHA-256。

```sh
PYTHONPATH=tests/v2 \
  BMEM_UPDATE_TEST_BIN=/absolute/path/to/test-0.3.0 \
  BMEM_UPDATE_LEGACY_BIN=/absolute/path/to/frozen-0.2.0 \
  BMEM_UPDATE_NEXT_BIN=/absolute/path/to/test-0.3.1 \
  BMEM_UPDATE_NATIVE_HOSTS=1 \
  python3 -B tests/v2/release/test_update.py -v
```

本地验收器使用 OpenSSL 3 创建一次性签名仓库，每例使用独立数据根和安装目录。U08 需要原生 Windows。U10 在实跑 134 条之后，设置 `BMEM_UPDATE_REGRESSION_JSON` 指向该次原始结果、`BMEM_UPDATE_EVALUATION_BIN` 指向同一候选，再单独执行验收器中的 `FrozenReleaseRegression`。

Windows 原生工作流在 x64 和 ARM64 各执行两条 U08 和一条计划任务接管，分别保存日志与回执。手动复测计划任务需要专用、已登录且未初始化 bmem 的测试账户。复用上述二进制和 OpenSSL 环境，设置 `BMEM_UPDATE_ISOLATED_TASK_SCHEDULER=1`，执行 `python -B tests/v2/release/test_windows_launcher.py -v`。测试使用唯一 UUID 的任务名，核对运行与停止状态、配置和原实例。[Windows 原生工作流](../../.github/workflows/windows-update-acceptance.yml)

## 验收 0.3.0 内部制品

先从签名维护者取得可信的公开根指纹和两个本地签名目录。测试程序逐个校验根、制品与签名目录，再使用平台对应的真实 0.2.0 启动。测试工作目录建立在当前用户家目录，独立保存安装位置和 locator。首次安装、原位接管、升级、篡改拒绝、故障回退和兼容回切共五组，JSON 保留精确制品与元数据校验值。第三项测试会将签名目录切回旧一代，核对 `bmem update` 拒绝重放。

```sh
PYTHONPATH=tests/v2 /path/to/tuf-venv/bin/python -B tests/v2/release/test_internal_release.py \
  --root /path/to/trusted-root.json --root-sha256 "$pinned_root_sha256" \
  --repository /path/to/internal-0.3.0 --successor-repository /path/to/acceptance-successor \
  --legacy /path/to/bmem-0.2.0 --legacy-sha256 "$legacy_sha256" \
  --target "$target" --execution-mode "$execution_mode" --receipt /path/to/new-receipt.json
```

`execution_mode` 按实际运行环境填写 `native`、`translated` 或 `emulated`。依赖版本与签发流程见[发行手册](../../scripts/release/README.md#030-内部测试)。

## 正式制品交接

六目标工作流生成候选构建证据。正式构建注入获准的公开 TUF 根，关闭 `update-test` 与 `evaluation-clock`。完成各平台签名和实机验收后，用 `scripts/release/candidate.py record` 对最终字节重新登记。发布目录含 `<target>.record.json`、`<target>.acceptance.json` 和对应制品。验收 JSON 含最终制品 `asset_sha256`、U01–U08 与 U10 的适用状态及 `regression` 三项计数。U09 分别记录草稿和离线预验收、不可变 Release 公开资产回读以及稳定 timestamp 启用后的验收。[候选构建](../../.github/workflows/release-candidate.yml) · [发布交接与核验命令](../../scripts/release/README.md)
