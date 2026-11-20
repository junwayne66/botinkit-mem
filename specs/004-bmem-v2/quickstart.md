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

在「数据来源 → 添加集成」选择 Codex、Claude Code 或 DSH。页面在助手的本机配置中登记 V2 MCP 与采集 Hook 或插件。可按需勾选新会话采集和共享声明读取。Codex 的新 Hook 仍需在 Codex 中审阅信任。飞书使用官方 `lark-cli` 发起浏览器授权，完成后自动登记该账号为来源。[Codex Hook 信任说明](https://developers.openai.com/codex/hooks) · [飞书官方 CLI](https://github.com/larksuite/cli)

对已开启采集的来源，点击「同步历史」或执行 `bmem connector sync SOURCE_ID`。本机宿主持续读取新写入的会话行，飞书按已授权账号分页读取会话和话题。重启时从持久进度继续。用 `bmem connector sync SOURCE_ID --pause` 暂停历史扫描。来源卡显示入库数量与读取错误，手工 JSON 批次仍可从「历史导入」提交。

`bmem job list --status done` 分页查看已完成作业，`bmem job candidates JOB_ID` 展开提炼候选和原文引用。工作台「后台作业」中的相同候选也可按需展开。

在工作台「设置 → 模型服务」中填写 Provider、BASE_URL、model_name 和 API_KEY，即可直接登记模型。API_KEY 在密码框输入，本地 Ollama 可留空。编辑时留空保留现有密钥，输入新值替换，勾选清除可删除。工作区用独立的 `data/settings.sqlite3` 保存模型设置和密钥。现有记忆备份只覆盖记忆库数据。

选择本机 Ollama 后，模型表单会列出已安装的模型。向量检索开启时，列表优先显示标记为 Embedding 的模型。缺少 Embedding 模型时，可复制页面给出的 `ollama pull bge-m3` 命令，下载后重新检测。[Ollama BGE-M3 模型页](https://ollama.com/library/bge-m3)

通过 `model add`、`model test`、`model use` 登记模型、运行探针并绑定用途。后台提炼支持通过用途测试的服务器 API 和本地 Ollama。两种用途分别按来源授权。例如对已绑定的生成模型，先用 `grant source <source_id> --purpose extract --allow true --preview` 核对接收端和预览号，再以 `--confirm <preview_id>` 确认。更换提炼接收端后需为该来源重新确认授权。向量默认关闭，启用时绑定通过测试的本机回环嵌入模型，再确认该来源的 `embed` 授权，最后用 `search --mode vector <query>` 执行相似检索。

## 集成检查

```sh
BMEM_V2_TEST_BIN="$bmem_bin" python3 -m unittest discover -s tests/v2 -v
BMEM_V2_TEST_BIN="$bmem_bin" python3 tests/v2/native_hosts.py
BMEM_V2_TEST_BIN="$bmem_bin" node tests/v2/browser_smoke.cjs
```

首次运行 Chrome 检查时将 `BMEM_V2_PLAYWRIGHT_MODULE` 指向本机 Playwright 模块，并准备 Chrome。双宿主检查需要本机 Claude Code、Codex 和 POSIX shell，使用隔离脚本与回环协议夹具。真实 Ollama 提炼用例可用环境变量 `BMEM_V2_TEST_REAL_MODEL` 指定 `127.0.0.1:11434` 上已有的模型。所有夹具保留在 `tests/v2/data`，实际发布验收边界见[验证记录](validation.md)。
