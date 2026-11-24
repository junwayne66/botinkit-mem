# T045 签字摘要（一页）· spec.md C01–C17 ＋ schemas/{matrix,patch}.schema.json

基线 HEAD 41e7dae；合并版已按四份评审逐条亲核 file:line 修订；89 项 python3 -I 校验全 PASS（spec §14.4）。签字前波 1 不开。

## 一、这份契约冻结了什么（每条附可判定检查点）

1. **本机边界不变**（C01）：回环 bind＋Host 精确匹配＋X-Bmem-Instance；R1 即有负对照 `authority_harness.rs::wrong_host_header_403/instance_header_mismatch_409/non_loopback_connect_refused`（T046），不再依赖条件包 T056。
2. **动作四元表＋release_round**（C02）：R1 八工具、R2 治理/bootstrap；agent 声明投影独立 command_id `agent.statement.list`（owner 的 statement.list 不变）；检查点 `tools_list_matches_capabilities_same_identity`（集合 diff 空集）。
3. **错误→MCP isError 映射＋脱敏**（C03）：429 带 retry_after_seconds 且与 HTTP Retry-After 同值；403/404  body 不含调用者输入；检查点 `route_authority.rs` 四用例＋`rate_limited_mcp_envelope_retry_after`。
4. **三档身份＋14 能力位穷尽表**（C04）：新注册缺省 agent_reader 仅 search；statement.list/search 同受 shared_enabled 门；检查点 `test_capability_reconcile.py`（位集==capability_bits.json）＋`bound_statement_list_without_share_403`。
5. **撤销/轮换立即生效＝下一请求，期望码统一 401**（C05）：bind 增 revoked_at 过滤；轮换走「暂存→SQLite 提交→发布」分段协议；T046 同包交付 user_version 0→1 守卫（旧二进制拒启新库）；检查点 `revoked_client_next_request_403`（断言 401）等＋失败注入三用例。
6. **actor_kind 服务端铸造＋change_kind 限制**（C06）：candidate.submit 信封/事件禁 actor_kind（任何值 403），按 checks/text 形状推导；agent 禁 revoked（防 withdraw_root 擦证据）；POST /v2/events 仅 owner/host_adapter；检查点 `agent_intake.rs` 十用例，变异点名 T053 推导函数。
7. **候选写入语义如实冻结**（C07）：目标限 kind='agent-intake' 自动注册来源；同步写集全披露（含 recall 观察索引）＋task_id 门；默认 extract inactive，远端目的地须 owner confirm 携带 remote_destination_ack（§八.5b 硬校验，新增交付）；检查点 `candidate_foreign_task_id_403_page_rollback`、`intake_extract_remote_requires_ack`。
8. **治理白名单＋preview 主体绑定**（C08）：grant.change 四重前置（含 task_id∈自建任务，补齐 tasks T054 已有前置）；stop/resume 限自身 intake 来源且 owner 已停者禁 resume；agent confirm 四 kind 均比对足迹＋主体；检查点治理段四负一正＋四新增负对照。
9. **history.import 请求单可实施化**（C09）：新表＋状态机 pending_owner→confirmed|rejected、owner preview 绑定 request_id、jobs 增 request_id 列、agent 自读回路由；检查点 `agent_history_request_pending_owner_zero_jobs`、`owner_confirm_creates_job_binding_request_id`。
10. **限流唯一算法**（C10）：60s 锚定窗、60 事件/1MiB；单页超配额→413 不可重试（消活锁）；配额判定与自增同事务；重放先复核当前授权、不计费；检查点表驱动＋`oversized_page_413_not_retryable`、`concurrent_submissions_window_not_exceeded`。
11. **token 文件交付 0600 atomic**（C11）：stdout 仅 client_id＋启动行；0600 是测试断言非代码事实；回灌脱敏明示 best-effort；检查点 `browser_integrations.cjs::token_file_delivery`。
12. **幂等键按工具类分立**（C12）：非 confirm 写工具调用方键＞桥 UUID；confirm 恒 grant-confirm-{preview_id}、裸键 422；T048 存量单据回填 owner 主体；检查点 `governance_confirm_bare_key_422_zero_rows`、回滚演练真跑。
13. **legacy-era 姿态＋升级判据修正**（C15）：rmcp LATEST 已是 2026-07-28，legacy 口径＝LATEST_WITH_INITIALIZE=2025-11-25；升级须 rmcp 新 era server 处理能力（非仅常量）＋Tier-1 SDK 枚举（待用户给出）。
14. **bootstrap 选项 A**（C16）：T054 同包 task.create/attempt.start；检查点 `agent_task_create_then_grant_change_succeeds`。
15. **验收纪律**（C17）：未纳入门禁不算完成；脚本检查点包装为 discover 用例；p0_evidence_complete 硬门增本次运行出处校验（run_id/commit/binary sha）；matrix schema 机械执行证据分级（synthetic 上限 partial 等），跨行判定归构建期；patch schema 与工具 51 组交叉变异对齐（7 组登记收紧）。

## 二、相对 plan/tasks 的新增或收紧（ASM 合流时登记台账）

- 新增交付：C06.6 change_kind 限制、C07.0/1a intake 来源限制＋task_id 门、C09 存储/读回/绑定、C05 分段轮换＋T046 user_version 守卫、C08 preview 主体列（T048 同窗）＋四 kind 足迹、§7.3 loopback 硬校验、429 交付链（T053＋error.rs/client.rs/host.rs）、C01 R1 负对照（T046）、judge 出处校验（T052）。
- 收紧/纠偏：statement.list→agent.statement.list；撤销期望码 401（用例名沿用 tasks 原名）；单页超配额 413；confirm 键恒命名空间；/v2/agent/candidates 为桥传输路由**无条件交付**（plan §八.7「条件」仅指公开直连入口，需签字确认该读法）；T063 聚合包集＝manifest 机械推导（含 T046/T047/T049-core）。
- 文件清单扩充（tasks.md 禁区，ASM 修订）：T054＋server.rs 补丁/history_tasks.rs；T053＋error.rs/client.rs/host.rs；T048＋governance_previews.client_id；T046＋版本守卫。

## 三、剩余风险与未决

- host_adapter 凭据持有即 user_message 信任边界（HTTP 无法区分 hook 出处）；独立采集凭据硬化未立项（§15.12，需拍板）。
- recall_grants 按 (source,client) 存储、跨该客户端全部任务可读；本轮三重前置收窄，逐任务许可未立项（§15.13）。
- apply_patch 写失败尽力恢复/证据失败保留改动（非严格原子）；硬化需 scripts 改造（§15.15，需拍板）；未立项前 FAIL 后先 rollback/核对哈希再继续。
- 「四级 Tier-1 SDK」未枚举，升级判据暂不可机械判定（§15.14，需拍板）。
- Windows ACL、三平台真机、签名/飞书＝外部材料门槛（blocked_external）；完整元 schema 离线校验归 T047/T063；外部复核「独立」口径：codex 嵌套二审失败自述只读复核、dsh D12 截断作废——reviews/ 落盘与闸门认定归 ASM/用户。

## 四、签字需确认（缺一不开波 1）

1. plan §八.5 三项隐私语义（含请求单机制与 loopback 硬校验为新增交付）。
2. plan §八.6 首发口径（macOS 无签名真验证＋fixture partial＋外部材料单列）。
3. plan §八.7：T056 默认不开工；**确认 /v2/agent/candidates 桥传输路由无条件交付读法**；公开 HTTP 直连入口默认否。
4. plan §八.10 幂等键按工具类分立读法（confirm 恒命名空间键）。
5. plan §八.11 bootstrap 选项 A。
6. C01–C17 全文＋两份 schema；§15.12/14/15 三项未决的默认姿态（不硬化/延后枚举/现状规程）。

## 五、签字后波 1 立刻可开工的包与前置

- **T046**（身份分级/迁移/撤销轮换＋user_version 守卫＋分段轮换＋authority_harness.rs C01 负对照）：前置＝本签字；文件 context.rs 判权段＋新 clients.rs＋装配补丁（server.rs/main.rs/integrations.rs 调用点）。
- **T047**（能力注册表＋描述符＋test_capability_reconcile.py＋capability_bits.json＋离线资源表）：前置＝本签字；与 T046 并行（文件互斥），消费冻结 schemas/ 路径。
- T049-core 待 T046 合流后开（context.rs 串行）；波 2（T050→T051a/T052/T053）与 T057-lite 按 plan §二依赖图；ASM 合流时同步 tasks.md 路径别名/文件清单/用例口径修订（§15.4/16）。
