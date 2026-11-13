# CLI / HTTP / Python Contract

## HTTP

- `GET /api/connectors/{brand}` 获取单品牌安全状态。
- `POST /api/connectors/{brand}/connect` 可选body `{sources: []}`。缺省沿用配置，首次未确认由客户端展示选择。传数组即显式确认，空数组表示暂不读取。
- `PUT /api/connectors/{brand}/config` body `{sources: ["private_chats","group_chats","documents"], enabled: true, sensitivity: "private"}` 保存范围，返回状态。
- `POST /api/connectors/{brand}/sync` 启动所选范围读取，返回状态。
- `GET /api/connectors/{brand}/qrcode` 返回当前授权PNG或404。

brand仅feishu/lark。来源值非法422。接口保持既有Host/Origin检查。同步或连接已运行时返回同一当前操作。

## Public Status

```json
{
  "brand": "feishu",
  "phase": "unconfigured",
  "installed": false,
  "cli_version": null,
  "authenticated": false,
  "account": null,
  "history": {"confirmed": false, "sources": [], "enabled": false, "sensitivity": "private"},
  "authorization": null,
  "sync": {"running": false, "stage": "idle", "imported": 0, "skipped": 0, "errors": 0, "last_success_at": null},
  "error": null,
  "busy": false
}
```

error为null或安全对象{code,message,retryable,missing?}，界面展示message。phase为unconfigured/installing/configuring/authorizing/connected/syncing/error。account只含安全id/name。authorization含url/expires_at，二维码由固定HTTP端点读取。认证成功必须检验用户身份和当前所需只读scopes。

## Python

`FeishuConnector(store)` 单例，方法 `status(brand)`、`connect(brand, sources=None)`、`configure(brand, sources, enabled=True, sensitivity="private")`、`sync(brand)`、`qrcode(brand)`、`tick()`、`close()`。status/connect/configure/sync均返回上文DTO。长操作立即返回，管理器拥有后台线程/进程。

`feishu_history.required_scopes(sources)` 返回scope集合，空来源至少offline_access。

`feishu_history.import_history(store, run_cli, brand, account_id, sources, progress=None, sensitivity="private")` 返回imported/skipped/errors计数，run_cli(args)返回官方JSON；progress(dict)更新stage和计数。调用方固定CLI/profile，历史模块始终传--as user，仅只读命令。

## CLI

- `bmem connect feishu`、`bmem connect lark` 交互询问历史多选，确认后发起安装/认证，展示阶段、URL和二维码。
- 非交互支持可选 `--sensitivity private|normal`，默认private。非交互范围支持 `--sources private_chats,group_chats,documents` 或 `--no-history`，两者互斥。
- `bmem feishu status|connect|sync`、`bmem lark status|connect|sync` 复用服务状态，connect支持同一范围参数。
- 非TTY且范围尚未确认时不隐式导入，返回清晰的范围选项。已有选择可直接复用。
- API离线时提示先启动bmem，不自行另建长生命周期认证服务。

## Reference Memories

导入器调用 `Store.ingest(event, reference_episode=True)`。该关键字是可信内部调用参数，公共事件DTO不能设置。复用同一事务写事件、T4/episode引用快照与outbox，引用可检索，job reconcile=false跳过重复模型生成。原T4候选规则隔离策略保持有效。引用保留source_type external，默认private，允许云端时显式normal。正文按外部资料显示，事件来源和时间可追踪。
