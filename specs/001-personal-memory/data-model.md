# Data Model

## Authority

- events 保存 event_id、device_id、harness、native_event_id、operation_id、source_type、body、occurred_at、ingested_at、session_id、project、sensitivity、evidence_hash、metadata、schema_version。原生身份三元组唯一，operation_id 去重。
- memories 保存 memory_id、fact_key、body、kind、status、revision_no、conditions、sensitivity、trust_tier、confidence、evidence_count、occurred_at、created_at、updated_at、supersedes。相同键与条件定位当前事实。
- revisions 保存不可变的 memory_id/revision_no 快照、事件和修订原因。隐私删除清除所有版本正文。
- sources 保存 memory_id、event_id、root_source_id、derived_from、source_type 及 invalidated_at。同一有效证据根仅计一次独立印证，失效关系保留审计来源。
- arbitration 保存 decision_id、memory_id、decision、reason、evidence_ids、trust_tier、confidence、model_id、occurred_at。
- injections 保存 injection_id、harness、session_id、turn_id、task、context_epoch、target、items(memory_id/revision_no)、token_count、去重数和时间。
- jobs 保存 job_id、kind、event_id、payload、status、attempts、available_at、last_error、created_at。pending/running/done/failed，失败可重试，启动恢复 running。
- import_cursors 保存 source/path、parser_version、discovered、imported、skipped、last_cursor、error、updated_at。
- tombstones 保存 memory_id、相关事件身份、删除时间和原因类型，保持无正文。删除日志随备份恢复重放。
- settings 保存语义开关与运行参数。

## Derived

FTS5 保存预处理文本及 memory_id。向量索引保存 memory_id、revision_no、space、1024维向量。状态、敏感级、条件和版本始终由权威表复核。

## Lifecycle

T1/T2 active。T3 proposed，两个独立来源或72h无冲突且confidence≥0.8后active。T4规则quarantined。纠正旧版本superseded。无法裁决disputed。撤销retracted。遗忘deleted并清除正文。每次转换生成决策台账。晚到事件保存历史，冲突并发返回409。

## Validation

公共入口用 DTO 校验 kind、source_type、sensitivity、文本长度、时间、预算、条件和 revision。客户端不设置 trust_tier，服务按入口与事件角色确定。模型候选在模型边界验证，只能获得源事件允许的信任与敏感级。
