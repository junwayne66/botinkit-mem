# Data Model

## Graph projection

节点包含唯一 `id`、`node_type`，以及实际记忆ID、修订号或来源根/事件ID。记忆、修订、共享来源分别投影现有 `memories`、`revisions`、`sources`。

边包含 `source`、`target`、`relation` 和实际 `evidence`，证据保留来源行ID、事件ID、创建及失效时间。关系类型采用 `derived`、`evidence`、`supersedes`。删除和私密边界覆盖所有端点。

## Fact time

后续切片在声明和候选中接受可选 `effective_at`，当前记忆与修订保存对应时间。旧资料从已知发生时间兼容读取。原事件时间和入库时间分别保留。

事实身份继续采用带主体含义的 `fact_key` 与精确 `conditions`，当前正文更新复用版本和派生失效机制。
