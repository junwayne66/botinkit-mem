# 四轮交叉评审对账清单

2026-11-23。本文件记录统一版 plan.md/tasks.md 相对 v1 的全部改动依据、拒绝采纳项、待用户拍板项与评审执行统计。

**输入材料**：v1 plan/tasks（被重写）；`/tmp/judge-full.txt`（第三轮裁判：25 条 UC＋17 条拒绝＋5 条待拍板＋完整性批评者 8 missing/3 agreements_wrong/3 final_risks）；`/tmp/supplement-full.txt`（第四轮 depgraph〔Claude sonnet，6 条〕与 scope〔Claude opus，9 条〕两车道）；`protocol-choice.md`（§5 五条规格影响已折叠进 plan §六，§6 执行体可用性）；004 tasks/validation（编号与历史证据）。

**重要缺口**：任务描述中第四轮合并者的 `delta_changes`/`wave_structure_final`/`schedule_final`/`rejected`/`needs_user_decision` 段落**在 /tmp/supplement-full.txt 中不存在**（本人 rg 复核零命中，文件仅两车道评审＋各自判定与待决）。统一版波次结构由本人按「depgraph 6 条全部落地＋scope MVP 结论＋任务指令硬性约束」推导重绘，非照抄 merge 结论；冲突裁决记录于本文件 B3。

**核验方法**：文档中所有 file:line 均经本人打开核对（python3 -I 脚本＋rg，只读）；标「已核」者为本轮复核，标「第 N 轮记录」者引自材料且无法本机复验（网络/执行体探针类）。

## A. 统一版相对 v1 改了什么

### A1 协议与规格代际（UC-1 被覆盖＋protocol-choice §5）
- v1 断链的 protocol-choice.md **现已存在**（本人 ls 复核，specs/005 共 4 文件），链接恢复指向它；UC-1「改指 research-brief、不新建 protocol-choice.md」的依据（目录仅 3 文件）已过时，被任务指令覆盖。
- 折叠 §5 五条：era 姿态声明进 T045（legacy-era ≤2025-11-25）；T054 无状态 preview→confirm 用服务端自铸 preview_id 句柄；T050 契约写两种 list_changed 路径；T056 降级待拍板；rmcp 编译预检进 T044（独立 worktree ≤4h spike，主线 Cargo.toml 不改）。依据：protocol-choice §四/五；rmcp features 现状 [Cargo.toml:28](../../src/v2/bmem/Cargo.toml#L28)（已核；材料引 :31 有漂移）。

### A2 轮次与波次（UC-2/3/25＋scope-1/6＋depgraph-5＋批评者#3）
- 单一发布 5 波 → **两轮出口**：R1＝MVP 11 项（T044/T045/T046/T047/T049-core/T050/T051a/T052/T053/T057-lite/T063-lite），R2＝治理面＋一期长尾。能力面仍全量冻结（四元表加 release_round 列＋available=false 登记，extension-contract.md:20 已核）。
- 关键路径改 **T044→T045→T046→T049-core→T050→T052→T063-lite**：批评者证实原表述错（W1 整体是 W2 闸门、T048 +50% 工期在链上）；T048/T054 移 R2 后治理迁移出 P0 闭合路径。T052「已采用/独立结果」以 real_local_model 输出断言为准、native_receipts.json 唯一证据源（UC-2 保留句）。
- T063 双模式＋manifest.json 机械推导依赖（scope-6）：手工清单反复漏包（原 tasks.md:89 只写 T052，UC-24 补到 7 包仍漏 T051/T053/T054/T056）；矩阵补 005 主线行（批评者 missing#6）；新增 specs/005/validation.md 指针＋分段，004 仍唯一总台账（CONTRIBUTING.md:125 已核）。
- T062 依赖改 T045/T050、与 T059 并行（depgraph-5）：retrieval.rs Zstd 段（:12/:88-92/:161-165/:229，recall_events 表）与治理段（:26-28/:573-706，governance 表）无共享函数/表，本人核对成立。UC-24/scope-5 的「T062 依赖 T059」被覆盖。
- 删 T064（UC-8§8.4）、T055 并入 T051（UC-18）、T060 并入 T058+60（UC-24）：18 包号保留。

### A3 并发纪律（UC-4＋depgraph-2/3/4）
- 装配补丁形式定义（depgraph-2）：patches/<pkg>.json（schema T045 冻结）＋scripts/apply_patch.py（T044）＋reconciler 函数（T050 reconcile_routes()）＋dry-run 冲突即 FAIL＋patches_applied.json。v1 只有「装配补丁」字样无定义（v1 plan.md:66）。
- worktree 命名与清理协议（depgraph-4）：`.worktrees/T<NNN>-<slug>`＋meta＋cleanup_worktrees.py（步 0 dry-run/步 13 apply）＋7 天/已合流即清。6 个陈旧 worktree 本人 git worktree list 复核在位，os.walk 实测 4,810,537,213 字节≈4.5 GiB。
- jobs 数学（depgraph-3）：CARGO_TARGET_DIR=<worktree>/target-<pkg>、并发 cargo ≤4、每进程 --jobs 2（4×2=8 线程留 2 核）、loadavg>8 降 1、5GB/worktree 上限；叠加 scope-1 在途实现 worktree ≤2。UC-7「全局 -j 上限 4」表述被 depgraph-3 的每进程数学取代。
- 函数级互斥清单＋run_gates 步 12 越界检测（UC-4）；门禁文件归属：test_native_hosts.py 归 T052 独占（:367 tools==[search]、:525 unknown 断言均已核）、test_integration.py:544-553 归 T052、browser_integrations.cjs 归 T057。

### A4 执行体（UC-5/9＋protocol-choice §6＋任务指令）
- 名册改真实可用：CC（claude 2.1.308）、CX（codex exec read-only）、DSH（headless，--permission-mode，无 --sandbox）、SUB-opus/SUB-sonnet、ASM。AGY/gemini/ocx 系全部不可用（证据见 D）。v1 指派 AGY/SUB-sol/terra/luna/mini 的包（T045/T047/T049/T054/T055/T057/T061/T064）全部重派。
- 隐私复核闸门改 **opus＋外部 codex＋dsh 三家交叉**（任务指令＋protocol-choice §6）；A≠B 跨族、同模型两实例不算独立复核；五列派工＋PONG 探针＋每波复探。
- 删除 v1「ocx-self fork 同族复核」（违反自设跨族原则）；read-only codex 不承担装配写（cx-1 counter）。

### A5 门禁（UC-6＋depgraph 对 UC-6 判定＋scope-8＋用户裁决）
- run_gates.py 步 0–13 工件化；**冷跑改 `cargo clean -p bmem --target-dir target-gate`**（用户裁决＋depgraph 判定），顺序 clean→clippy→build→test 保证二进制重链（消解 G1 counter 对 clean -p 的担忧）；全量 clean 仅限轮次出口/Cargo.lock 变更/runner 变更（scope-8）。
- 「任何 skip 即失败」（用户裁决）兑现为：assembly 步 6 skipped==0＋步 5 skip 集合==期望集（意外 skip 即 FAIL）；CONTRIBUTING:55 opt-in 政策只适用非 assembly 跑（已核）。
- 步 10 改对已提交基线校验、改文档包同 diff 携带基线更新（批评者#2 证实「先重算再校验」恒真）；current.sha256 21 项本人核对。
- 独立复跑：codex workspace-write 探针（限 target-gate/artifacts）通过则复跑门禁，失败则只读机械核验＋显式记录局限（scope-8③，批评者 missing#8）。
- 负对照必须先能红（UC-10 模板④）：变异点名单点名实码（context.rs:249、ingestion.rs:500-512、retrieval.rs:249-252 等均已核）＋红绿 run id 成对。

### A6 包级改写（UC-10–24＋depgraph-1/6＋scope-2/3/4/5/7/9＋批评者 missing）
- **T044**（UC-11＋depgraph-4＋protocol-choice §5.5）：38 条/-uall 41 文件（本人实测；UC-11 的 40 已过时）；native_hosts 原子改名＋TestCase 化（现 535 行无 TestCase 已核）；rmcp 编译 spike 保留在 T044（scope-3 主张删，被任务指令「依赖预检进 T044」覆盖；spike 放独立 worktree 使 scope-3 的「占关键路径/无决策价值」担忧消解——结果供 T056 拍板）。
- **T045**（UC-12＋scope-1/7＋protocol-choice §5.1）：补角色白名单/治理白名单/limits/撤销语义/幂等键来源/era 姿态/release_round/matrix_schema/补丁 schema/task bootstrap 二选一；文件清单补 authority.md 与 cli-and-platform.md；三家交叉复核 JSON 落盘。
- **T046**（UC-13＋批评者#1）：Browser 限制收窄为裸 POST /v2/clients（server.rs:542-543 owner-only 已核）并豁免 integrations install（server.rs:704-705 owner()、Browser 满足 :351，install 即铸 token integrations.rs:218-224，均已核）——消解「Browser 不得签发凭据」与「保留 owner-console 全量」自相矛盾；存量迁移回填；main.rs:1054/integrations.rs:222 写死 load_context 改默认值函数。
- **T047**（UC-14＋scope-7）：脚本生成骨架、不写死数量（command-catalog 63 个管道行含表头，P0 三表 :16/:68/:94 已核）；available=false 登记；版本头发射移 T049-callers。
- **T048**（UC-15＋depgraph-6＋批评者 missing#4）：移 R2；WAL 三文件备份＋integrity_check＋user_version 版本门＋停服换文件回滚演练；不做双写。archive::backup 不得当回滚点（副本 unbind 全部客户端，见 B4 引文更正）。
- **T049 拆 core/callers**（depgraph-1，任务指令强制；scope-7 lite/full 命名被覆盖，实质约束吸收）：core＝context.rs 判权＋7 处迁移、error.rs ConflictKind 枚举＋From 脱敏、ingestion.rs 四处反射点删后缀保前缀（main.rs:1156 唯一 starts_with 消费者、app.js:186 按 code 显示，均已核）；callers＝57 处/10 文件机械迁移＋移除前缀映射＋显示层＋版本头，合流窗口在 R2 各归属包之后（消解 depgraph 待决#4 与 T050 同波冲突）。
- **T050**（UC-17＋批评者 missing#2＋scope-1）：补幂等键贯通（host.rs:122 key=None、client.rs:59-61 已核）；逐请求判权不变量＋撤销/旋转变异用例；reconciler。
- **T051**（UC-18＋scope-2）：T055 并入；T051b 降条件包（本机零个已命名 resources-only 客户端，三宿主 install 全 stdio 已核）；shared 投影条文保留（authority.rs:354-356、retrieval.rs:249-252、server.rs:518-519 均已核）。
- **T052**（UC-19）：provider_mode 双模＋tee 代理 wire 互证＋回执出自真实 hook＋verifier≠attempt＋红先；wire_api responses→chat（native_hosts.py:248 已核）；statement_tokens 512（main.rs:1166）与 deferred 空对象（host.rs:279-281）已核。
- **T053**（UC-20＋scope-3＋protocol-choice §5.4）：角色白名单服务端执行＋三表零增量＋限流表驱动四断言＋可注入 Clock；/v2/agent/candidates 条件路由**保留但触发点前移到 T045 签字**（scope-3 主张删；因 T056 改待拍板而非定死砍掉，条件可能触发，前移消解批评者 missing#5 时点悖论）。
- **T054**（UC-21＋批评者 missing#3＋protocol-choice §5.2）：白名单收窄（history 路由 owner-only server.rs:727-728、grant.change 能力极大 retrieval.rs:640-678 均已核）；同包交付 task.create/attempt.start bootstrap（server.rs:598、context.rs:370 已核）；无状态句柄形式。
- **T056**（UC-22＋scope-3＋protocol-choice §5.4）：降条件包待拍板；UC-22 middleware/逐请求鉴权条文保留为入口规格；nest_service 绕过 fallback Host 检查风险（server.rs:101/:145-152 已核）。
- **T057**（UC-23＋scope-9）：lite/full 两阶段；install 器仅三宿主＋generic；「最近调用」删除（无调用日志基础设施，rg 零命中已核）；静态卡片限定语保留（intake.js:82-86 已核）。
- **T058+60/T059/T061/T062**（UC-24＋scope-4/5＋depgraph-5/6）：并包消 archive.rs 同波交集（:168/:171/:258-263 已核）；T059 补 retrieval.rs:693；T061 macOS 真验证＋他平台 fixture partial＋签名/真机/飞书 blocked_external 材料清单；T062 可判定子集＋token 自 wire 解析＋providers.rs 不改（零 usage 解析已核）。
- **T063**（UC-24＋scope-6）：双模式＋manifest 机械依赖＋005 矩阵段＋19 字段全自工件注入。

### A7 风险与待确认（UC-7/8＋scope-3/4＋depgraph-6）
- 风险表重写：actor_kind「既有防线」纠正（ingestion.rs:77-78 注释只覆盖正文角色词、:1149-1152 结构化字段由正文给出，已核）；「本地已核」改「源码已核、编译未验证」；飞书租户归属移出 T057（004 validation.md:111 已核）；新增宿主版本漂移（004 validation.md:12 记录 Codex 0.167.0 vs 现 0.171.0）、runner 失效、指纹漂移、执行体失效四行；WAL 三文件备份与 T048 演练入表。
- §八改 9 项待拍板（见 C），v1 的 4 项全部吸收或重述。

## B. 拒绝采纳与覆盖记录

### B1 第三轮裁判拒绝的 17 项（沿袭，理由摘要）
| 项 | 拒绝理由 |
|---|---|
| dsh-6 两条事实指控 | T057「无测试」属段混淆（browser_integrations.cjs 测 integrationHosts 面板，connectorCatalog :82-86 才是静态卡片，已核）；T061「0 行未核实」不成立（platform.rs 612 行 rg 零命中，本人复核） |
| dsh-7 问题框架 | plan §五 Python 行不含 T053（在 Rust 行）；「待确认还在就先分配」不构成风险（签字闸门在） |
| exec-3 双写过渡期 | 被其自身 counter 拒绝：四重复杂度远超收益；改文件级备份＋事务内四步重建 |
| exec-5 / dsh-6 的 T047a/b 拆包 | 未经对抗复核；extension-contract.md:20 已有 available=false 语义（已核），单包生成器足够 |
| exec-1 原 blocking 定级 | 被自身 counter 修正：T055 属陈旧首选修复；真正严重的是 ocx-sol 复核 lane 无人接替 |
| G1 原步骤顺序与 skipped==1 基线 | clean -p 删二进制而 test 不重链（顺序缺陷）；三步跑三遍最贵证据；统一版以 clean→clippy→build→test 顺序＋期望 skip 集消解 |
| G2 静态 conclusion_first | 开头期望随阶段变化；引用应为 native_hosts.py:525（改名尚未发生，已核） |
| G3 blanket real_host_real_model | owner-only 行永远不可达，被迫造假即重演假绿；改 owner_native evidence_kind |
| cx-1 废除改名/保留独立入口 | 原子绑定 TestCase 化后静默零收集担忧消除，保留 __main__ 入口；其「read-only codex 承担装配」被自身 counter 拒绝 |
| cx-3 原提案 | 未要求 A≠B 与 DSH 写探针，采纳其 counter |
| dsh-1「任何 skip 都算失败」原语义 | 与 CONTRIBUTING:55 opt-in 冲突（已核）；**用户裁决恢复该口号**，统一版以 assembly 步 6 skipped==0＋步 5 期望集精确相等兑现，opt-in 只留非 assembly 档 |
| dsh-2「夹具 loaded 无法独立观测」 | 过头：prove_loaded（native_hosts.py:293 起，已核）读宿主→provider 出站捕获独立于 bmem 自报；真缺口在台账/receipt 侧，UC-19 双模已补 |
| ocx-self fork 同族复核 | 违反「评审必须不同模型」自设原则 |
| stdout 明文 token 默认 | 裸 uuid 不命中 authority.rs:59-71 脱敏（:70 仅 "token="，已核），经宿主转录→history.import 回灌；改文件交付 |
| T064 本轮开工 | 三前提全不成立（SUB-luna 不可用、DSH 占关键路径、reqwest 0.13 新 major，Cargo.toml:14=0.12 已核） |
| authz-5 T058→T060→T061 纯串行 | 并包更优（G4/exec-2 holds=true） |
| G8 把 :367 断言归 T050 | 与 T052 独占驱动文件冲突；归 T052 |

### B2 第四轮对 UC 的否定/修订（holds=False）与处置
- UC-4（函数级互斥难执行）→ depgraph-1 修复后保留机制：T049 拆包＋全调用点清单归属，步 12 可执行。
- UC-6（每波全量冷跑过贵）→ 改 clean -p bmem＋冷跑时机收窄（用户裁决＋scope-8）。
- UC-15（迁移回滚不可执行）→ depgraph-6 四件补齐（WAL 三文件/版本门/手工规程演练/单写者事实）。
- UC-16（T049 范围不足）→ depgraph-1 core/callers 拆分（任务指令强制）。
- UC-24（T059→T062 依赖无必要）→ depgraph-5 并行（本人核对两段不相交）。
- scope 对 UC-2/3/6/7/11/13/14/16/17/18/21/23/24/25 的修订与 UC-22 本轮反对：全部按 A 节所述吸收；UC-22 条文保留为 T056 入口规格。

### B3 被任务指令/第四轮覆盖的 UC（硬性约束 4 记录）
| UC | 覆盖内容 | 理由 |
|---|---|---|
| UC-1 | 链接恢复 protocol-choice.md，§5 折叠进计划 | 该文件已存在（本人 ls 核），任务指令明示折叠；UC-1 依据过时 |
| UC-2/3/25 | 关键路径与波次按两轮重绘 | scope-1＋批评者#3；任务指令采纳 MVP 子集 |
| UC-6 | 冷跑改 clean -p bmem；「任何 skip 即失败」恢复 | 用户裁决；depgraph 判定＋scope-8 时机收窄 |
| UC-11 | 保留 rmcp 预检并升级为编译 spike；-uall 数字 40→41 | protocol-choice §5.5 任务指令；protocol-choice.md 新增使计数变化（本人实测） |
| UC-14 | 版本头发射移 T049-callers | scope-7（406 全仓零发送方，本人 rg 核） |
| UC-15 | T048 移 R2＋depgraph-6 机制 | scope-1（移出 P0 关键路径）＋depgraph-6 |
| UC-16 | 命名采 T049-core/callers（非 scope-7 lite/full） | 任务指令硬性约束 3；scope-7 的前缀契约/显示层/版本头约束全部吸收 |
| UC-18 | T051b 降条件包默认不开工 | scope-2（零已命名受益客户端） |
| UC-20 | 条件路由保留、触发点前移 T045 | T056 改待拍板（protocol-choice §5.4）后条件可能触发；前移消解时点悖论 |
| UC-22 | T056 降条件包待拍板 | scope-3＋protocol-choice §5.4＋任务指令 |
| UC-23 | T057-lite/full 两阶段、删最近调用 | scope-9 |
| UC-24 | W4 整体移 R2；T062∥T059；T061/T062/T063 按 scope-4/5/6 重写 | scope-1/4/5/6＋depgraph-5 |

### B4 本人核对后发现的**材料新错误**（依据不成立或引文失真）
1. **第四轮 merge 段落缺失**：/tmp/supplement-full.txt 无 delta_changes/wave_structure_final/schedule_final/rejected/needs_user_decision（rg 零命中）。统一版结构为推导产物，已在前言声明。
2. **depgraph-1 与批评者 missing#1 的算术错**：「T049 三文件含 22 处、剩余 42 处/9 文件」不成立。实测 64 处/11 文件中，context.rs 7＋ingestion.rs 11＋error.rs 0＝**18 处**；除 context/ingestion 外剩 **46 处/9 文件**。按 core 只迁 context.rs 7 处的统一版口径，callers 实取 **57 处/10 文件**。分布列表本身正确（与任务给定一致，本人 rg 逐文件复核）。
3. **depgraph-1 callers 文件清单漏 settings.rs**（2 处）：其 proposed_change 列 9 文件不含 settings.rs，但分布列表含。统一版 callers 清单已补（10 文件）。
4. **depgraph-6 引文失真**：「archive.rs:51 实测 'DELETE FROM context_clients'」不成立——:51 实为 `let rebind_clients = unbind(&copy)?;`，unbind()（:325-335）执行的是 `UPDATE context_clients SET token_hash='unbound:' || id`＋VACUUM（:329，已核），全文件无该 DELETE。**结论仍成立**（备份副本使全部客户端凭据失效，恢复即静默撤销），统一版按真实机制表述。
5. **UC-11 计数过时**：-uall 展开 40→**41** 文件（protocol-choice.md 新增）；38 条（27M＋11??）仍正确（本人实测）。
6. **research-brief 行号漂移**：批评者 missing#2 引「research-brief:175 已明示幂等键贯通」，实际该结论在 **:172**（:175 是载荷尺寸条目的出处行）；UC-5 引「intake.js:117-125/243-25」中 243-25 为截断笔误，实为 :243-253（已核）。另 rmcp 依赖行实为 Cargo.toml:28（材料引 :31）。
7. **UC-1 依据过时**（见 B3）：specs/005 现 4 文件非 3 文件。

## C. 待用户拍板（四轮合并去重后 9 项）

| # | 事项 | 建议默认 | 影响面 | 来源 |
|---|---|---|---|---|
| 1 | 速率阈值默认数字 | 60 事件/分钟、1 MiB/分钟；「并发 4」重定义为 HTTP 每主体在途上限或删除；settings 可调不做新控件 | T045/T053 | 裁判待决 1＋scope 待决 5（无异议）合并 |
| 2 | generic token 交付 | 文件交付 run/clients/<id>.json（0600 atomic）；stdout 仅 token=<value> 单行＋回灌警告的坚持退路 | T057 | 裁判待决 2 |
| 3 | T064 本轮砍掉 | 砍；缺口留 004 validation.md:111 | 台账/下一轮 | 裁判待决 3 |
| 4 | 并发规模与 jobs 数学 | 在途实现 worktree ≤2、并发 cargo ≤4、每进程 --jobs 2、loadavg>8 降 1、允许波内动态调整 | 全波次排期 | 裁判待决 4＋depgraph 待决 2 合并 |
| 5 | 三项隐私语义 | (a) history.import 请求单＋所有者 preview→confirm；(b) agent intake 默认无 extract、仅 loopback 目的地；(c) Browser 读法 (a)，限制收窄裸 POST /v2/clients 并豁免 install | T045/T046/T054/T057 | 裁判待决 5（§8.6） |
| 6 | P0 首发宣布口径 | macOS 无签名本机真验证＋他平台代码就绪 fixture partial＋签名/真机/飞书 blocked_external 单列即可宣布；否则三平台真机＋签名硬门槛 | T061/T063-full/排期 | scope-4 新增 |
| 7 | 条件包 T051b/T056 与 HTTP 直连 | 默认不开工；T051b 门槛＝命名 resources-only 客户端或所有者签字；T056 是否保留拍板，门槛＝命名 HTTP-only 客户端＋≤4h spike；HTTP 直连则 T053 条件路由 T045 触发 | R2 范围/T053 | scope 待决 3＋protocol-choice §5.4 合并 |
| 8 | T062 推迟项 | 接受：p99/三系统/大语料/准确率 lab 为首发后缺口与紧凑模式启用门槛 | T062 | scope 待决 4 |
| 9 | T048 回滚手工规程 | 接受停服换文件＋演练；不前移 T058+60 restore 能力（会破坏 MVP 互斥）；窗口局限记台账 | T048/T058+60 | depgraph 待决 3 |

已吸收不再单列：**两轮拆分**（scope 待决 2，任务指令已采纳）；**装配补丁形式定义**（depgraph 待决 1，任务指令要求落地）；**T049-callers 与 T050 同波冲突**（depgraph 待决 4，callers 移 R2 后合流窗口＋apply_patch 串行技术性消解）。

## D. 四轮评审的执行体与实际产出统计

| 轮次 | 车道/执行体 | 实际产出 |
|---|---|---|
| 第一/二轮 | 见 research-brief.md（27 万字符，只按行抽核）与 judge 引用 | 协议核实、代码事实盘点（简报 1/2）；v1 plan/tasks |
| 第三轮（judge-full.txt） | authz-1..10、G1..G8、exec-1..6、cx-1..8、dsh-1..7 各 lane（source_reviews 标签统计）；合成者＝裁判会话 | 25 UC＋17 拒绝＋5 待拍板；完整性批评者 8 missing/3 agreements_wrong/3 final_risks。cx lane 由本机 codex exec 真跑（产物见下）；dsh lane 由 dsh headless 真跑；原 ocx-gpt-5.6 系指派作废后由可用执行体重跑 |
| 第四轮（supplement-full.txt） | depgraph＝Claude sonnet（会话内子代理）；scope＝Claude opus（会话内子代理） | depgraph 6 条（1 blocking/2 high/3 medium）＋对 25 UC 逐条判定；scope 9 条＋逐条判定＋5 待决。**合并者段落缺失**（B4.1） |
| 统一版（本轮） | 本会话（任务指令执行体） | 三文件重写；全部 file:line 本人复核 |

**不可用执行体的实测证据**：
- agy：/tmp/agy_err.txt（本人 cat 核验原文）"Eligibility check failed: … not currently available in your location"；protocol-choice §6 记录三次调用、两个模型均被拒。
- gemini CLI：protocol-choice §6 记录 "This client is no longer supported … migrate to the Antigravity suite"（EOL）；本机 /tmp 未找到留存产物，引委托方实测记录。
- opencodex：gpt-5.6-sol/terra/luna、gpt-5.5、gpt-5.4-mini 与 fable 档 Model not exist（protocol-choice §6＋judge UC-5 第三轮实测；luna 另见任务指令作废名单）。

**外部评审真跑证据（本人核验产物）**：
- codex：/tmp/cx_stdout.txt（19,929 字节，JSON verdict＋cx-1.. edits，头尾抽核）＋/tmp/cx_stderr.txt（661,051 字节运行日志）＋/tmp/cx_review_prompt.txt；`codex exec --sandbox read-only --skip-git-repo-check` 路线成立。
- dsh：/tmp/dsh_out.txt（16,121 字节，JSON verdict 开头的真实评审）＋/tmp/dsh_err.txt（real 182.02s）＋/tmp/dsh_review_prompt.txt；`dsh headless` read-only 完成一份真实评审成立。
- 会话内子代理：第四轮两车道标题自署 Claude sonnet/opus；本轮名册仅 opus、sonnet 两档可用（任务指令）。

**本人复核统计（只读）**：Error::conflict( 64 处/11 文件逐文件 rg 复核一致；git status 38 条/-uall 41 文件；6 个陈旧 worktree 在位（约 4.5 GiB）；platform.rs 612 行零适配器命中；current.sha256 21 项；test_governance_preview.py 13 个 def test_；tests/v2/*.rs 5 处 #[path] 内联先例；browser_smoke.cjs require 6 子模块；native_hosts.py 535 行无 TestCase。网络类事实（11434 模型在位、rmcp registry 无缓存、ollama /v1/messages 405）引第三轮合成者记录，未本机复验。

## E. R4-merge 对齐（补做，2026-11-23）

merge 输出（20 条 delta_changes、4 条 rejected、6 条 needs_user_decision、wave_structure_final、schedule_final）曾因主代理导出遗漏未写入 /tmp/supplement-full.txt、未参与首轮统一版撰写（前言「重要缺口」与 B4.1）；现已恢复为 /tmp/supplement-merge.txt，**本节为补做对齐**。本节所有「已核」均为本轮本人打开文件/python3 -I 只读脚本复核（脚本 /tmp/r4_align_check{,2,3}.py，输出 /tmp/r4_check_out{,2,3}.txt）；merge 与现文的行号/事实漂移按实测更正并记录于 E4。A–D 节为历史记录不改；A3 与 C.4 的「并发 cargo ≤4（4×2=8 线程留 2 核）」口径由本节 E5.1/2 更正，原文保留。

**处置计数：20 条 delta＝已在文中体现 14／需补写并已补写 4（#1/#6/#9/#15）／与已采纳结论冲突故不采纳 2（#4/#11）。**

### E1. 20 条 delta_changes 逐条处置

merge 的 target 节号是 UC 时代旧 plan 结构，映射：merge「plan §三 波次」→现 §二；「plan §四 并发纪律」→现 §三；「plan §六 门禁」→现 §五；「plan §二 关键路径」→现 §二「关键路径与排期」。

| # | delta 目标 | 处置 | 说明／现文位置 |
|---|---|---|---|
| 1 | plan 波次结构＋并发纪律 | 已补写 | 两轮结构/release_round/条件包/available=false 已体现（§一/§二）；并发数学「cargo≤4×-j2=8 线程留 2 核」与同条「在途 worktree≤2」自相矛盾，按 merge wave_structure_final 统一为 ≤2×-j2=4 线程留 6 核（E5.1/2）；loadavg>8 改建议阈值 ASM 按实测调整（merge rejected #4） |
| 2 | plan 装配补丁形式定义 | 已体现 | §三.3：patches schema/apply_patch.py 流程/patches_applied.json/reconciler（T050 reconcile_routes）/禁多包改同一 match 块全在；merge「--round」现「--wave」命名差异；字段枚举为示意、T045 冻结权威不变（与 rejected #2 一致） |
| 3 | plan worktree 命名清理协议 | 已体现 | §三.1：.worktrees 命名/meta/cleanup 步 0 dry-run 步 13 apply/7 天或已合流即清/5GB 上限全在；容量本轮复测 ≈4.48GiB，现文「约 4.5GiB」成立，merge「4GB」为粗口径（E4.10） |
| 4 | tasks T044 删 rmcp 依赖预检 | 不采纳 | 与任务指令「依赖预检进 T044」＋protocol-choice §5.5 冲突：T056 是待拍板而非定死砍掉，spike 结果是 T045 签字拍板输入；独立 worktree ≤4h 不占主线关键路径；「门槛未过前任何包不得改 Cargo.toml 启用该 feature」已在 §六.5/T056 入口门槛。B3 UC-11 已记录 |
| 5 | tasks T045 release_round＋幂等键冻结 | 已体现 | release_round 列/available=false＋reason＋stage（T047 描述符）/T045 条文(6)键来源/main.rs:1038 形态（已核）/T050 贯通＋no-422 正例全在；键来源的用户确认补为 §八.10（E3） |
| 6 | tasks T046 Browser 限制收窄 | 已补写 | 收窄裸 POST /v2/clients＋豁免 install 已体现（server.rs:542-543/:704-705/:351、integrations.rs:218-224 本轮已核）；「与 Owner 差异仅在不得创建新 owner 会话」现文遗漏，已补入 T046 条文（E5.3） |
| 7 | tasks T047 删版本头行 | 已体现 | T047「发射移 T049-callers（R2），本包不做」；T049-callers 承接＋406 可达用例；零发送方本轮 rg 复核（src 全仓 Bmem-Command-Version 0 命中） |
| 8 | tasks T049 拆两段 | 已体现 | core/callers 与 lite/full 同一拆分不同命名（任务指令，不改名）；前缀冻结契约/main.rs:1156 唯一 starts_with 消费者（本轮已核）/合流前 rg 复核/变异红/57 处 10 文件/显示层/版本头全在。差异：现 core 另含 context.rs 判权＋7 处迁移、共同可见集前移 R1（任务指令 depgraph-1 口径，merge lite 更窄）；无主文件现显式归 T049-callers（merge 称 T044 装配补丁认领），均有主。merge 沿袭 22/42 算术错见 E4.5 |
| 9 | tasks T050 幂等键贯通 | 已补写 | 贯通＋键来源＋no-422 正例已体现；「server.rs 写路由 key(headers)? 形态不改」与负例 explicit_key_conflict_409 已补（E5.4） |
| 10 | tasks T051 替换 UC-18 | 已体现 | T051a 五工具/授权条文（shared 投影/recall_grants×task_access/禁复用 owner-only statements/变异红）/T051b 双门槛/available=false reason/展示移 R2 T057-full 全在；merge 引 GET /v2/statements :525-526 正确，现 T051a 误引 :518-519（那是 POST），已更正（E5.6） |
| 11 | tasks T053 删条件路由 | 不采纳 | 与已采纳结论冲突：T056 待拍板（protocol-choice §5.4/§八.7），HTTP 直连选项仍在、条件路由可能触发；触发点前移 T045 签字已消解 missing#5 时点悖论。B3 UC-20 已记录 |
| 12 | tasks T054 task bootstrap | 已体现 | 同包 task.create/attempt.start（server.rs:598/context.rs:370 已核）＋T045 条文(11)二选一＋bootstrap→grant 正例全在；二选一的用户确认补为 §八.11（E3） |
| 13 | tasks T056 降条件包 | 已体现 | 默认不开工/命名 HTTP-only 客户端＋≤4h spike 双门槛/退路 A 时间盒/named gap/Cargo.toml R1 不改/UC-22 入口规格保留全在；「定死砍掉 vs 待拍板」口径差为 protocol-choice §5.4＋任务指令覆盖（B3 UC-22） |
| 14 | tasks T057-lite 收窄 | 已体现 | 三宿主＋generic/install 升级旧绑定（integrations.rs:215-216 已核）/静态卡片/最近调用删除（本轮 rg 零命中复核）/四步浏览器回归/飞书 blocked_external 全在；merge「撤销」现步骤名「unbind」同属 T046 撤销语义、old_token_403 断言一致；静态卡片措辞 nuance 见 E4.12 |
| 15 | tasks T048 整条替换 | 已补写 | 移 R2/WAL 三文件/integrity_check/停服换文件/回滚演练/test_governance_preview 13 项（本轮已核）/复合 UNIQUE/单事务全在；「迁移成功即 user_version+1」与验收「user_version 递增」断言已补（E5.5）；merge 沿袭 archive.rs:51 DELETE 引文失真见 E4.4 |
| 16 | tasks T061 替换 | 已体现 | macOS 真验证/他平台 fixture partial/命名真机门槛/签名清单 blocked_external owner=user/禁「待材料」散文/首发口径 §八.6 全在；飞书租户门槛现文归 T057-full＋§七材料清单，实质一致 |
| 17 | tasks T062 替换 | 已体现 | 可判定子集①②③/providers.rs 不改/token 自 wire 解析/无 usage verdict=gap/推迟项命名门槛/与 T059 并行全在；merge 粗行段字面重叠，现文精确锚点本轮复核互斥成立（E4.6） |
| 18 | tasks T063 替换 | 已体现 | 双模式/manifest 机械依赖/未登记强制 pending＋owner/005 矩阵段/19 字段全自工件注入/004 唯一总台账（CONTRIBUTING.md:125 已核）全在 |
| 19 | plan 门禁 UC-6 修订 | 已体现 | ②步 10 对已提交基线＋同 diff 携带更新＋禁现场重算、③workspace-write 探针＋降级只读机械核验＋显式局限记录、①全量冷跑限三情形全在（§五/§三.7）；①「非出口波次纯增量不 clean」与用户裁决冲突不采纳——每波 clean -p bmem 为用户裁决口径（B3 UC-6），只清 bmem 产物保留依赖缓存，scope-8 成本担忧已消解 |
| 20 | plan 关键路径替换 | 已体现 | R1 路径一致且含 T049-core 段与 T047 支线切换条件（较 merge 更精确）；T052 闭合口径/native_receipts 唯一证据源/manifest 机械依赖在；R2 瓶颈 T048 +50% 在排期与风险表；merge R2 路径为波次串行口径且自相矛盾（E4.8），现文依赖驱动并行保留；排期数字差异见 E6 |

### E2. rejected 4 条对齐

| merge rejected | 现文状态 | 对齐结论 |
|---|---|---|
| depgraph-1 的 core/callers 命名被拒（采 scope-7 lite/full） | 现文按任务指令硬性约束采 core/callers（B3 UC-16） | 实质方向一致（R1 脱敏保 hook、R2 全改造）；命名差异为任务指令覆盖，按指令不改名 |
| depgraph-2 schema 字段不在 delta 硬编码、由 T045 冻结 | §三.3 字段枚举明示「schema 由 T045 冻结」，T045 条文(10)承接 | 一致；枚举为示意（与 delta 2 文本同源），冻结权威不变 |
| scope-1「R1 10 包」计数口径（T049 同包跨轮两段、总 18） | R1 11 台账项＝10 包＋T063-lite；18 包号 19 行台账 | 一致；merge 自身 R2「8 包」清单漏 T057-full（其 delta 10/14 均引用该包），wave 标题「8 包＋T063-full」与 rejected 清单（含 T063-full）自相矛盾，现文计数自洽不改 |
| depgraph-3 loadavg>8 阈值不硬编码、ASM 按实测调整 | merge delta 1 自身又硬编码 >8，内部不一致；现 §八.4 本为待确认建议默认 | §三.2/§八.4 本轮改「建议阈值，ASM 按实测调整」（E5.1/2），与 rejected #4 精神一致 |

### E3. needs_user_decision 6 条覆盖核对

| merge 待决 | plan §八 覆盖 | 处置 |
|---|---|---|
| 1 P0 首发宣布口径 | §八.6 | 覆盖 |
| 2 两轮拆分（两个验收出口） | §一「主线顺序」冻结行＋§八末「已吸收不再单列」 | 覆盖（任务指令已采纳为冻结执行口径，非待决，不回退） |
| 3 T051b/T056 条件包默认不开工 | §八.7 | 覆盖 |
| 4 T062 推迟项 | §八.8 | 覆盖 |
| 5 T045 签字一并确认 (a)(b)(c)(d) | (a)(b)→§八.1；(c)(d) 原 9 项未覆盖 | (c) 幂等键来源**新增 §八.10**、(d) bootstrap 二选一**新增 §八.11**；T045 验收签字范围与 tasks 验收状态同步补（E5.8/9） |
| 6 三项隐私语义解释 | §八.5 | 覆盖 |

新增 2 项（编号接续 10、11），§八 标题「9 项」改「11 项」（E5.10/11）；C 节 9 项表为首轮历史记录，以本节为准。

### E4. 本轮复核发现的漂移与 nuance 清单（均本人打开文件/脚本核实）

1. **tasks T051a 引文漂移（已更正）**：GET /v2/statements 实在 server.rs:525-526，:518-519 是 POST /v2/statements；现 T051a 误引 518-519，merge delta 10 引文正确。已改 525-526（E5.6）。plan §一写入信任行引 POST :518-519 正确，保留。
2. **-uall 计数漂移（已更正）**：本轮实测 42 文件（原 41），原因是 specs/005 新增 REVIEW-DECISIONS.md（本对账文件，晚于 T044 计数）；38 条（27 modified＋11 untracked）不变。T044 快照已更正（E5.7），执行仍以脚本输出为准。
3. **merge delta 8 cli-and-platform.md:64→:63**：有界日志类别约束（「日志只保留操作 ID、能力、版本和错误类别」）在 :63，:64 是 backups/tmp 行。现文此处无引文，无需改，记录。
4. **merge delta 15 沿袭 B4.4 引文失真**：archive.rs:51 本轮复核为 `let rebind_clients = unbind(&copy)?;`，unbind()（:325-335）执行 UPDATE context_clients SET token_hash='unbound:'||id（:329）＋VACUUM（:330），全文件无 DELETE FROM context_clients。「备份副本使全部客户端凭据失效」结论仍成立，现文按真实机制表述，保留。
5. **merge summary#2/delta 8 沿袭 B4.2 算术错**：本轮脚本逐文件复核 Error::conflict( 64 处/11 文件分布一致（platform 21/ingestion 11/integrations 9/context 7/history_tasks 5/server 4/vector 2/settings 2/retrieval 1/main 1/client 1）；context 7＋ingestion 11＋error 0＝18 处（非 22），除 context/ingestion 外剩 46 处/9 文件（非 42），callers 实取 57 处/10 文件。
6. **merge delta 17/wave_structure retrieval.rs 粗行段重叠**：「Zstd 段 lines 12-229」与「governance 段 lines 23-706」字面重叠（23-229）。本轮复核精确锚点：encode 段 :88-99（level match、zstd::bulk::compress :95）、decode :161-168（event_body、decompress :164）、expand :228-232、recall_events 表 :10-17；治理段 :23-28（previews/operations 表）、:570-706（单键回放 :573、INSERT :695、cleanup_state :693）。函数级互斥成立，现 §三.4 锚点保留。
7. **merge rejected#3 R2 包数漏 T057-full**（见 E2 第 3 行）。
8. **merge wave W4 自相矛盾**：W4 列 T048∥T054∥T049-full 并行，但其自身 R2 关键路径 T048→T054、且 T054 grant.change 前置依赖 T048 复合 UNIQUE；现文 T054 依赖 T048/T050 串行执行更严谨，保留。
9. **merge delta 1 与 rejected#4 loadavg 内部不一致**（见 E2 第 4 行），现文统一为建议阈值＋ASM 按实测调整。
10. **worktree 容量复测**：本轮 os.walk 4,806,111,597 字节 ≈4.48 GiB，与原记录 4,810,537,213 ≈4.5 GiB 同量级（差异为临时文件波动）；merge/depgraph「4GB/4.0G」为 du 粗口径。现文「约 4.5 GiB」保留；6 个陈旧 worktree 本轮 git worktree list 复核在位。
11. **acceptance.md:31 引文成立（无需改）**：:31 为「P1 再按同一任务比较基本路径与增强方案…」，字面「配对」在 :50；merge 与现文引 :31 表「配对比较属 P1」语义成立。acceptance.md 共 50 行，:50 P0 首发集合引文正确。
12. **integrations.rs/web 措辞 nuance（结论均成立）**：①字面 "stdio" 仅 :388（install_claude），install_codex :394-432 写 [mcp_servers] command/args、install_dsh :434-458 写 cordis.patch.yml command/args，全文件无 HTTP 传输——「三分支全 stdio」成立，merge「dsh transport stdio」无字面 transport 字段；②atomic()（:319）不设 0600 权限位，§八.2/T057「0600 atomic」是本轮新增要求，:281 引文指 run/clients/<id>.json 文件交付形态（:281-282 store_client），T057 须自行实现权限位、不得假设沿用既有形态；③intake.js:82-86 connectorCatalog 现有仅 hermes/openclaw/mcp 三条目，agy/Cursor/Zed/Copilot 现 web 代码零命中（本轮 rg intake.js/index.html），「保留静态候选卡片」应理解为「本轮只登记静态卡片、不建 install 器」，新增卡片属 T057 范围。
13. **providers.rs「无 usage 解析」语义成立**：:37 max_completion_tokens 为请求参数非响应 usage 解析；token 证据须自 T052 wire 捕获的结论不变。
14. **merge delta 7 research-brief 引文漂移**：「406 零调用方」依据实在 research-brief.md:44/:198，merge 引 :199-201（该几行为 bound 客户端路由盘点与出处行）；零调用方事实本轮独立复核（src 全仓 Bmem-Command-Version 0 命中）。research-brief:202 引 Cargo.toml:31 实为 :28（B4.6 已录），merge delta 1/13 引 :28 正确。

### E5. 本轮 plan.md/tasks.md 实际改动清单（最小改动，未重排波次、未改编号体系、未推翻冻结决定）

plan.md：
1. §三.2：并发 cargo 上限 4→2（与在途 worktree≤2 同口径）、4×2=8 线程留 2 核→2×2=4 线程留 6 核；loadavg>8 加「（建议阈值，ASM 按实测调整）」。
2. §八.4：同步 ≤4→≤2，补 2×2=4 线程留 6 核口径与建议阈值措辞。
3. tasks T046 身份判定条文：补「与 Owner 差异仅在不得创建新 owner 会话」。
4. tasks T050：幂等键贯通条文补「server.rs 写路由 key(headers)? 形态不改，由桥保证必有值（plan §八.10）」；验收补负例 `explicit_key_conflict_409`。
5. tasks T048：版本门条文补「迁移成功即 user_version+1」、「遇新表即拒」改「遇版本不匹配即拒」；验收 migrate 用例补「user_version 递增」断言。
6. tasks T051a：GET /v2/statements 引文 518-519→525-526。
7. tasks T044：-uall 41→42 文件、specs/005 四份→五份（补 REVIEW-DECISIONS）。
8. tasks T045 验收签字范围：补 §八.10 幂等键来源、§八.11 task bootstrap 二选一。
9. tasks 验收状态：签字范围同步补幂等键来源、task bootstrap 二选一。
10. plan §八 标题：去重后 9 项→11 项。
11. plan §八 新增第 10（MCP 写工具幂等键来源）、11（task bootstrap 二选一）项，编号接续、各给建议默认。

注：第 3–9 条在 tasks.md，第 1/2/10/11 条在 plan.md。A3/C.4 的旧并发数学口径以本节 1/2 条为准。

### E6. wave_structure_final / schedule_final 对齐

- R1 W0–W3 与现文波 0–3 一致（W0 T044→T045 签字；W1 stageA{T046∥T047}→stageB{T049-core}；W2 T050 先行→{T051a∥T052∥T053}；W3 T057-lite→T063-lite）。现文 T057-lite 文件互斥可提前与波 2 并行（merge W3 串行）——排期精化非约束冲突，保留。
- R2 现文依赖驱动并行（T048→T054；T053→T058+60→T061；T048→T059；T049-callers 合流窗口在 R2 各归属包之后、T063-full 之前），merge W4/W5 波次串行口径与其自身关键路径自相矛盾（E4.8），现文保留，未重排波次。
- 并发纪律统一到 merge wave_structure_final 口径：在途实现 worktree≤2、CARGO_BUILD_JOBS=2、2×2=4 线程留 6 核、ASM 单点串行合流、codex 只读复核全程并行（E5.1/2）。
- 执行体派工一致：CC≈12 Rust 包、CX 只读跨族、DSH 固化模板填空＋装配树复跑、ASM 单点串行（T044/T063/合流/门禁）、SUB 契约起草 A≠B。
- 排期：现文 R1 约 6–8 天/R2 约 4–6 天，merge 均 5–7 天，合计均约 10–14 天；现文含 T052 M-L 预算与「单实现者名义并行不缩短日历时间、ASM 串行合流是真实瓶颈」说明，保留。
- 条件包 T051b/T056 默认不开工、T064 本轮砍掉，与 merge 一致。
