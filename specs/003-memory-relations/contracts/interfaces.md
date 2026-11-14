# Interface contracts

## Slice 1

- `GET /api/graph?target=local|cloud` 返回 `nodes` 和 `edges`。保留原有字段，增加节点类型、修订身份和边证据。
- 记忆节点的 `id` 与 `memory_id`一致，修订和来源节点有独立 `id`。客户端按 `id`区分图节点。
- `derived`引用来源行，`evidence`连接真实共享来源，`supersedes`引用历史和当前修订。
- `GET /api/events/{event_id}?target=local|cloud` 返回已有来源事件及证据可用性。私密事件在cloud目标返回404。
- 节点和边详情使用文字节点呈现证据，历史版本在已有记忆详情中定位。
- 星图进入与手动刷新重新读取，筛选先于节点上限，不新增后台轮询。

## Slice 2

声明、候选及对应CLI/MCP入口接受可选事实生效时间。缺省使用事件发生时间，既有调用保持可用。细化行为遵循spec.md的FR-001至FR-007。
