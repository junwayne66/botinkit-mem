# 智能体与 MCP 接入

先完成[安装与初始化](quick-start.md)。把 bmem 可执行文件放在长期使用的位置，再配置宿主。工作台与所有宿主应指向同一数据根。

## Codex、Claude Code、DeepSeek Harness

1. 运行 `bmem web`，进入「数据来源 → 添加集成」，选择宿主。
2. 按引导选定读取范围。如果只需让助手查询已共享的声明，配置 MCP 即可。会话采集、历史同步和模型提炼按需分别启用。
3. 重新加载宿主配置或开启新会话。Codex 按宿主提示审阅、信任 bmem 的 Hook。先检查宿主是否显示 `bmem-v2` 及 `search` 工具，再用一条非敏感声明验证查询。使用 Hook 的宿主可在 bmem 工作台核对来源状态和投递回执。
4. 如需自动提炼，在工作台的模型设置中测试并绑定提炼模型。核对模型提供方和接收范围后，再在该来源上开启对应授权。

声明默认可通过本机工作台管理。向获授权的助手共享一条测试声明时，使用 `bmem declare '所有助手的报告用中文' --share authorized_assistants`。宿主还需有读取共享声明的权限。

## Cursor 与通用 stdio MCP

使用 `bmem --json doctor` 核对 `data_volume.path`，然后用 `bmem --json client list` 检查该宿主与接收端的登记。新客户端可按下列方式登记并预览授权。

```sh
bmem --json client register cursor --destination cursor-agent
bmem grant set --client CLIENT_ID --share-statements true --preview
bmem grant set --client CLIENT_ID --share-statements true --confirm PREVIEW_ID
```

替换 `CLIENT_ID`。核对预览的接收端和权限后，再填写其 `preview_id` 确认。对于其他 MCP 宿主，把命令中的宿主和接收端换成对应名称。已有匹配记录时使用已有的客户标识。

下面以 Cursor 的 `~/.cursor/mcp.json` 为例。合并 `bmem-v2` 条目，保留该文件中的其他配置。Windows 路径在 JSON 中使用双反斜杠，程序路径指向实际的 `bmem.exe`。

```json
{
  "mcpServers": {
    "bmem-v2": {
      "type": "stdio",
      "command": "/absolute/path/to/bmem",
      "args": [
        "--home", "/absolute/path/to/vault",
        "mcp", "serve", "--client", "CLIENT_ID"
      ]
    }
  }
}
```

重载后核对 `tools/list` 含 `search`。授权范围内没有匹配记录时，空结果也属于成功调用。凭据由本机桥接从 bmem 读取，并存储于本机。

## 需要定位问题时

| 现象 | 检查什么 |
|---|---|
| 宿主找不到程序 | 核对配置里的可执行文件绝对路径及执行权限，重载宿主配置 |
| 能连上，搜不到已共享声明 | 核对声明的共享范围、该客户端的共享声明读取权限及数据根 |
| 历史未出现在工作台 | 在来源卡检查采集授权及同步进度，或通过 `bmem connector list` 查看来源 |
| 新记录入库，暂时没有提炼结果 | 用 `bmem job list` 查看积压、提炼暂停与探测时间，核对模型绑定及来源提炼授权 |
| 希望暂停收集或取消共享 | 在来源卡及授权界面分别操作，核对暂停或撤回预览的影响范围 |

MCP、Hook 与 Harness 的完整契约见 [005 智能体接口规格](../specs/005-agent-mcp-interface/spec.md)。
