# 本地构建与复验

从仓库根构建一份原生开发程序。程序内嵌 `src/v2/web`。

```sh
cargo build --locked --manifest-path src/v2/bmem/Cargo.toml
```

macOS 或 Linux 可先建立隔离的数据根和定位目录。以下命令仅使用该临时实例。

```sh
work=$(mktemp -d)
export BMEM_LOCATOR_DIR="$work/locator"
export BMEM_DEFAULT_HOME="$work/default"
bmem_bin="$PWD/src/v2/bmem/target/debug/bmem"
"$bmem_bin" --home "$work/vault" init --non-interactive
"$bmem_bin" declare '所有助手的报告用中文，先列行动项' --share authorized_assistants
"$bmem_bin" config set profile balanced
"$bmem_bin" web
"$bmem_bin" service stop
```

工作台随 V2 可执行文件内嵌分发。替换或重建程序后，如 `web` 提示服务的工作台版本不同，请用同一个 V2 可执行文件执行 `service stop`，等 `status` 显示 `stopped` 后再执行 `web`。重启会建立新的浏览器会话，并优先沿用该数据根上次监听的回环端口。

工作台状态读取最近一次提交的 SQLite 数据。总览汇总随业务写入更新。作业列表默认返回最新 40 条元数据，后续页使用 `next_cursor`，完整输入可通过 `bmem job show JOB_ID` 查看。飞书凭据在后台核对，数据来源页显示 SQLite 中最近一次核对的结果。

在「数据来源 → 添加集成」选择 Codex、Claude Code 或 DSH。页面在助手的本机配置中登记 V2 MCP 与采集 Hook 或插件。可按需勾选新会话采集和共享声明读取。Codex 的新 Hook 仍需在 Codex 中审阅信任。飞书使用官方 `lark-cli` 发起浏览器授权，完成后自动登记该账号为来源。[Codex Hook 信任说明](https://developers.openai.com/codex/hooks) · [飞书官方 CLI](https://github.com/larksuite/cli)

对已开启采集的来源，点击「同步历史」或执行 `bmem connector sync SOURCE_ID` 启动持续同步。用 `bmem connector sync SOURCE_ID --pause` 暂停。从来源卡片或「数据管理」点击「导入历史」，勾选所需类别并创建任务。飞书可按账号选全部私聊、全部群聊和全部可见文档，Codex、Claude Code 和 DeepSeek Harness 可导入本机保留的全部历史会话。数据管理显示进度和分项错误，可取消或重试，服务重启后从检查点继续。

在「数据管理」可预览并提交带游标的 V2 JSON 批次，也可按来源和日期下载 V2 事件快照。每次快照最多 1000 条或 4 MiB，可缩短日期范围分批导出。导出记录保留计数和校验值。原 `#/imports` 入口可继续访问。

CLI 用 `bmem connector history-import-all SOURCE_ID --kind sessions` 导入一个 Harness 来源的全部历史。飞书可组合 `--kind dm --kind group --kind docs`。需要指定对象时，可用 `history-options` 列出，再用 `history-preview` 和 `history-import` 指定对象与 RFC3339 半开时间段。`bmem connector history-jobs` 查看任务，`bmem connector data-export SOURCE_ID --from START --to END --json` 输出导出文档及回执。

`bmem job list --status done` 分页查看已完成作业，`bmem job candidates JOB_ID` 展开提炼候选和原文引用。工作台「后台作业」中的相同候选也可按需展开。

在工作台「设置 → 模型服务」中填写 Provider、BASE_URL、model_name 和 API_KEY，即可直接登记模型。API_KEY 在密码框输入，本地 Ollama 可留空。编辑时留空保留现有密钥，输入新值替换，勾选清除可删除。工作区用独立的 `data/settings.sqlite3` 保存模型设置和密钥。现有记忆备份只覆盖记忆库数据。

选择本机 Ollama 后，模型表单会列出已安装的模型。向量检索开启时，列表优先显示标记为 Embedding 的模型。缺少 Embedding 模型时，可复制页面给出的 `ollama pull bge-m3` 命令，下载后重新检测。[Ollama BGE-M3 模型页](https://ollama.com/library/bge-m3)

通过 `model add`、`model test`、`model use` 登记模型、运行探针并绑定用途。后台提炼支持通过用途测试的服务器 API 和本地 Ollama。两种用途分别按来源授权。例如对已绑定的生成模型，先用 `grant source <source_id> --purpose extract --allow true --preview` 核对接收端和预览号，再以 `--confirm <preview_id>` 确认。更换提炼接收端后需为该来源重新确认授权。向量默认关闭，启用时绑定通过测试的本机回环嵌入模型，再确认该来源的 `embed` 授权，最后用 `search --mode vector <query>` 执行相似检索。

## 其他本机 Agent 通过 MCP 查询记忆

支持 stdio MCP 的 Agent 可以在这台机器上启动 `bmem mcp serve`。工作台的回环 HTTP 地址供浏览器和 REST API 使用。以 Cursor 为例，在新终端为它登记独立客户端，`--destination` 填便于识别接收端的名称。以下使用默认数据根，若工作台使用自定义数据根，请替换 `bmem_root`。

```sh
bmem_root="$HOME/.bmem"
bmem --home "$bmem_root" --json client register cursor --destination cursor-agent
```

将返回的 `client_id` 替换到下面两条命令。先运行第一条核对授权范围，再把返回的 `preview_id` 填入第二条确认。

```sh
bmem --home "$bmem_root" grant set --client CLIENT_ID --share-statements true --preview
bmem --home "$bmem_root" grant set --client CLIENT_ID --share-statements true --confirm PREVIEW_ID
```

在 Cursor 的 `~/.cursor/mcp.json` 中将以下条目并入 `mcpServers`。将 `command` 改为本机 `command -v bmem` 输出的绝对路径，`--home` 后填写上述数据根的绝对路径，并替换 `CLIENT_ID`。其他 stdio 宿主同样填写这里的命令与参数。[Cursor MCP 配置文档](https://cursor.com/docs/mcp)

```json
{
  "mcpServers": {
    "bmem-v2": {
      "type": "stdio",
      "command": "/absolute/path/to/bmem",
      "args": ["--home", "/absolute/path/to/vault", "mcp", "serve", "--client", "CLIENT_ID"]
    }
  }
}
```

重新加载宿主的 MCP 配置后，`bmem-v2` 应列出 `search` 工具。它查询当前有效、标记为 `authorized_assistants` 的全局声明。凭据由本机桥接程序读取。新会话采集是数据来源页的独立授权。

## 集成检查

```sh
BMEM_V2_TEST_BIN="$bmem_bin" python3 -m unittest discover -s tests/v2 -v
BMEM_V2_TEST_BIN="$bmem_bin" python3 tests/v2/native_hosts.py
BMEM_V2_TEST_BIN="$bmem_bin" node tests/v2/browser_smoke.cjs
```

首次运行 Chrome 检查时将 `BMEM_V2_PLAYWRIGHT_MODULE` 指向本机 Playwright 模块，并准备 Chrome。双宿主检查需要本机 Claude Code、Codex 和 POSIX shell，使用隔离脚本与回环协议夹具。真实 Ollama 提炼用例可用环境变量 `BMEM_V2_TEST_REAL_MODEL` 指定 `127.0.0.1:11434` 上已有的模型。所有夹具保留在 `tests/v2/data`，实际发布验收边界见[验证记录](validation.md)。
