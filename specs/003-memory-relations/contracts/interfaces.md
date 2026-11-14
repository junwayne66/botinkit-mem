# Interface contracts

## Slice 1

- `GET /api/graph?target=local|cloud` 返回 `nodes` 和 `edges`。保留原有字段，增加节点类型、修订身份和边证据。
- 记忆节点的 `id` 与 `memory_id`一致，修订和来源节点有独立 `id`。客户端按 `id`区分图节点。
- `derived`引用来源行，`evidence`连接真实共享来源，`supersedes`引用历史和当前修订。
- `GET /api/events/{event_id}?target=local|cloud` 返回已有来源事件及证据可用性。私密事件在cloud目标返回404。
- 节点和边详情使用文字节点呈现证据，历史版本在已有记忆详情中定位。
- 星图进入与手动刷新重新读取，筛选先于节点上限，不新增后台轮询。

## Slice 2

声明、候选及对应CLI/MCP入口接受可选 `effective_at`。CLI支持 `--effective-at` 和 `--occurred-at`，MCP的memory_save与memory_declare支持对应字段，Web声明提供可选本地日期时间。输入日期规范化为UTC，缺省使用原事件发生时间。

记忆和修订响应增加 `effective_at`，事件响应保留可空的显式时间。可信度相近时按事实时间更新，等时相反正文保留争议，用户带memory_id与expected_revision的版本纠正可以消歧。既有调用可省略时间字段。细化行为遵循spec.md的FR-001至FR-007。
