# 第一切片验收 · 真实关系与详情

2026-11-14，本机隔离数据验收。事实生效时间策略的结果见第二切片。

## 实际执行

```sh
.venv/bin/python -m pytest tests/test_graph.py tests/test_recompute.py tests/test_acceptance.py::test_management_endpoints_are_real tests/test_acceptance.py::test_forget_backup_restore_and_no_resurrection -q --tb=short
node --check src/bmem/static/app.js
git diff --check
```

pytest指定独立 `--basetemp`。最终17 passed，2.76秒，1条上游Starlette弃用提示。Python语法解析通过。

新增图谱六项HTTP验收覆盖多父级真实来源行、共享来源事件、修订身份及替代、已失效关系标识、独立节点零虚构关系、cloud私密和遗忘边界。自定义来源根补入真实Worker多级重算回归。

## 浏览器

使用独立预览服务与合成数据，后台Worker关闭，无实际模型调用。

- 首页在30条最新独立记录之前优先显示真实关联端点，24节点中有9条实际关系。
- 完整图35条记忆、4个来源或历史节点、9条关系，计数与实际显示对应。
- Agent筛选后为2条记忆、1个来源节点、2条关系，全部边端点实际存在。
- 鼠标点击真实线段和键盘Enter都能打开关系证据。
- 历史节点正确定位第1、2版，更新至第3版后可查看全部3条修订记录。
- 刷新关系读取后台新增的修订，来源事件可以查看正文、发生与采集时间和证据可用性。
- 控制台错误为0。

## 审查与修复

独立审查方已报告后端/API/UI差异审阅和15项隔离测试通过。审查指出的抽屉晚响应错配，在4秒延迟的实际浏览器中先复现，再通过共享请求身份校验修复。

回归覆盖来源请求未返回时关闭并打开另一记忆或关系。延迟响应完成后，新正文与关系证据保持正确。新增来源、关系详情和关闭动作会更新请求身份，记忆和来源响应的成功与错误分支均按当前身份回写。

最后一轮独立差异复核通过后，本地提交ea8e701并部署。运行健康、实际代码哈希、全部关系端点及持久化证据已核对，原权威记录行标识缺失为0。本切片未涉及数据结构迁移。

## 边界

图谱展示最近上一版与当前版的替代，完整历史保留在记忆详情。现有单机存储、模型、业务调度和并发配置沿用当前运行配置。原始事件的时间直接来自已保存数据。

新增全库关系投影未执行大规模负载测试。30分钟运行采样由独立观察方收集。

# 第二切片验收 · 事实时间与可信度

2026-11-14，实现与独立复审完成，本地提交及部署校验通过。

## 实际执行

```sh
.venv/bin/python -m pytest tests -q --tb=short --basetemp <隔离临时目录>
node --check src/bmem/static/app.js
git diff --check
```

首轮全套171 passed，34.78秒，1条上游Starlette TestClient弃用提示。定向事实时间、依赖重算与验收91项通过，4.01秒。CLI子进程和实际MCP HTTP入口包含在全套测试中。

tests/test_fact_time.py首轮新增28项，覆盖同范围新旧事实、显式目标、迟到旧资料、可信确认水位、低可信推断、条件分支、缺省及非法日期、真实Worker/Runtime传递、旧结构与备份、等时争议、用户版本纠正、同值隐私升级及重算时间。

另使用当前已安装的第一切片代码创建真实schema 1隔离数据库及备份，再用新代码打开和恢复。两条路径均迁移至schema 2，旧事实时间回退到原事件时间，旧可空列保持空值，原schema_version与证据哈希保持一致。

## 浏览器

使用独立预览服务与合成数据，Worker关闭，无实际模型调用。

- 明确事实时间五月的新声明更新同一记忆至第2版，来源发生及采集时间保留十一月。
- 同值六月确认推进事实时间水位，保持第2版，证据增至3条。
- 四月的迟到相反声明保留older_fact审计，当前正文、六月水位、第2版和3条支持证据保持正确。
- 每次打开声明表单时，可选日期字段为空。空正文会给出校验反馈。
- 控制台错误为0，截图保存在本机独立验收目录。

## 复核修复

本会话内并行只读复核发现的三项问题均先通过真实实现回归复现，再修复并验证。

- 同时间相反正文原本可能覆盖，已复用争议保留流程。用户带目标与预期版本的显式纠正可消歧。
- 内部重算候选时间原本可能被首条证据时间覆盖，现采用候选明确时间，缺省保留目标水位。
- 等时争议处理曾把同值隐私升级拆成争议副本，现同值元数据修订沿用既有流程。回归核对同一ID、private、active与cloud 404。

并行复核仅做静态审阅，测试由当前会话执行。独立审查与后续修复结果见下节。

## 实现决定与边界

T1用户声明与T2验证结果按已确认默认值视为可信度相近。同值确认把当前记忆与当前修订的事实时间作为确认水位，原始日期保留在事件和审计中。数据库版本提升为2，仅添加缺失列。

真实模型提取日期的准确率尚未评估，Worker测试使用模型协议替身。旧资料按既有时间兼容读取，已有库的批量重算尚未执行。独立负载测量及业务调度决策由指定观察方和用户推进。

# 独立审查修复 · 已解决争议退出召回

独立审查方报告43项事实时间、重算与图谱回归通过，2.95秒，并在隔离Store复现主流程缺口。同键同条件的五月相反声明形成两条disputed，六月的新可信声明只更新其中一条，另一旧争议仍可通过search召回。

## 实际执行与修复

先新增真实Store及HTTP回归，10 failed、5 passed，复现新事实及同值确认后旧争议残留。随后补充状态恢复的索引验收，两个同值确认入口均先复现缺少embed任务，再接入既有索引出箱。

新增19项覆盖声明和候选的事实键/显式目标、可信同值确认、较旧或低可信输入、不同事实键与条件、迟到确认借用水位、未来争议、显式等时纠正、旧分支派生失效及共同来源根保护。

同事务收束仅接受已生效的T1/T2输入，按非空事实键及精确条件比较本次事实时间。符合条件的旧disputed记录转为retracted，决策以resolved_by_newer_fact关联本次事件和当前记忆，原正文、修订、来源与证据文件继续可展开。派生失效复用既有逻辑，并保护共同来源根下本次接受的当前记忆。可信较新同值确认恢复active并进入既有索引出箱。显式用户版本纠正可消解等时争议。

```sh
.venv/bin/python -m pytest tests/test_fact_time.py tests/test_recompute.py tests/test_graph.py -q --tb=short --basetemp <隔离临时目录>
.venv/bin/python -m pytest tests -q --tb=short --basetemp <隔离临时目录>
node --check src/bmem/static/app.js
git diff --check
```

定向62 passed，2.97秒。全套190 passed，35.59秒，1条上游Starlette弃用提示。Python AST、JavaScript语法和差异空白检查通过。

并行只读静态复核未发现本轮新增逻辑的具体问题，测试由当前会话执行。独立审查方报告事实时间及旧争议解决逻辑通过，另行执行定向62 passed，3.05秒，git diff --check与node --check通过。独立复审后本地提交65228bd并部署。

## 本地发布校验

2026-11-14北京时间15:22:31旧服务停止，15:22:33受审安装包安装完成，15:22:38新服务健康。14个包内文件与受审源码逐字节一致，35项运行依赖及应用版本一致性校验通过。

schema 1迁移至2，事件、记忆与修订各添加一个可空effective_at列。既有列定义、原事件发生与采集时间、事件schema版本及证据哈希校验通过。events、jobs、memories、sources、revisions、arbitration、tombstones中迁移前的记录标识缺失为0。数据库设置和启动配置的一致性校验通过。

15:23:33运行校验显示Worker已完成6个周期，错误0，在途任务自动恢复并完成。线上只读记忆详情验证旧资料事实时间回退及修订时间字段通过，旧事件的可空事实时间列维持空值。安装后的CLI帮助已呈现两项时间参数。

本轮静态收敛检查覆盖FR-001至FR-012、三个用户故事及现有任务，构建工作全部完成。

# 运行恢复补丁 · embedding输入上限

2026-11-14，按失败诊断修复Runtime.embed的HTTP413恢复路径，当前工作区等待独立审查。

## 实际执行

```sh
.venv/bin/python -m pytest tests/test_embedding_limits.py tests/test_runtime.py -q --tb=short --basetemp <隔离临时目录>
.venv/bin/python -m pytest tests -q --tb=short --basetemp <隔离临时目录>
git diff --check
```

新增9项真实HTTP测试，初轮4 failed、5 passed。修复后定向25 passed，8.73秒，全套199 passed，40.16秒，1条上游Starlette弃用提示。Python AST及差异空白检查通过。

测试覆盖首次完整输入、Unicode前缀减半、持续413的请求上限、单字符边界、混合批次顺序与完整短输入、400/500直接报错、共享恢复预算耗尽，以及真实Store/Worker向量写入、完整正文和关键词检索。

## 行为与审查边界

HTTP413触发批量逐条恢复，单输入最多额外3次前缀减半，各次请求使用剩余调用预算。其余错误通过既有失败路径传播。向量仍按每个输入返回1024维结果，成功完成整批生成后由Worker写入索引。

前缀缩短降低尾段语义召回能力，字符长度无法保证token长度安全。持续拒绝后任务继续记录failed并参与既有失败退避。测试采用合成服务，真实模型对原失败文本的恢复效果尚待受控验证。时限测试覆盖模拟预算耗尽，严格总墙钟上限尚未验证。

并行只读静态复核未发现阻塞性实现问题。独立审查方逐行复核通过，另行执行定向25 passed，8.76秒。运行失败类型、来源、时间、尝试次数及幂等性诊断保存在本机私有诊断目录。

## 本地发布与自然周期验证

2026-11-14，本地提交27c0e49。北京时间19:27:39旧服务停止，19:27:41受审安装包安装完成，19:27:47服务健康。14个包内文件与受审源码一致，独立只读核对确认源码、安装包和已安装文件三方SHA一致。schema 2、数据库设置、启动配置及运行依赖一致性校验通过。十类权威记录的既有行标识缺失为0。

一条长期HTTP413向量任务于19:39:55按自然周期完成，从第121次失败进入第122次成功，错误字段清空。对应记忆第1版已有canonical BGE向量行，完整正文哈希及关键词索引验证通过。重启前的在途蒸馏任务亦由既有恢复流程完成。

19:40:33校验显示Worker已运行162个周期，循环异常0。此次实测覆盖该条向量任务的自然恢复，其余蒸馏失败仍需按诊断分别处理。前缀语义损失和严格墙钟时限的验证边界见上节。

# 运行恢复补丁 · 同批候选版本衔接

2026-11-15，新增失败诊断后修复隔离确认的同批版本自冲突路径，当前工作区等待独立审查。

## 实际执行

```sh
.venv/bin/python -m pytest tests/test_runtime.py -k 'truncated_generation or distill_batch or distill_noop or untargeted_add' -q --tb=short --basetemp <隔离临时目录>
.venv/bin/python -m pytest tests/test_runtime.py tests/test_fact_time.py tests/test_recompute.py tests/test_embedding_limits.py -q --tb=short --basetemp <隔离临时目录>
.venv/bin/python -m pytest tests -q --tb=short --basetemp <隔离临时目录>
git diff --check
```

新增6项批次回归并扩展既有生成诊断测试。初轮4 failed、3 passed，修复后7 passed，0.82秒。定向87 passed，11.75秒，全套205 passed，42.47秒，1条上游Starlette弃用提示。Python AST与差异空白检查通过。

## 行为与审查边界

整批候选继续采用生成时的目标快照预检。一次作业内，成功通过CAS的UPDATE、NOOP和DISPUTE返回目标身份匹配的实际版本时，后续同目标定向候选承接该版本。版本映射只在该次调用内使用。测试验证外部修订介入时仍触发Store冲突，不定向ADD不会授予后续候选跨快照版本。

测试覆盖ADD后连续UPDATE、NOOP及DISPUTE，同值时间确认、NOOP保持版本、外部写入介入，以及不定向ADD的边界。JSON解析报错补充白名单done_reason和程序侧num_predict，合成HTTP测试核对结束、截断及未知结束原因，错误消息采用脱敏元信息。

本次修复不提供跨次任务的统一幂等保证。真实失败的原始第三候选未保存，隔离复现确认的是可触发的批内自冲突路径，真实任务的唯一成因仍待进一步证据。真实JSON失败记录缺少结束原因与输出预算，新增诊断用于后续区分。

并行只读静态复核未发现阻塞问题。独立审查方逐行复核通过，另行执行runtime、fact_time及acceptance共123 passed，6.50秒，1条上游弃用提示。新增失败ID、时间、来源及部分写入的脱敏证据保存在本机私有诊断目录。

## 本地发布与短时运行验证

2026-11-15，本地提交b571543。北京时间06:14:57旧服务停止，06:14:59受审安装包安装完成，06:15:04服务健康。14个包内文件与受审源码匹配，独立只读核对确认源码、安装包及已安装文件三方SHA一致。schema 2、列定义、数据库设置、启动配置和运行依赖一致性校验通过，十类权威记录的既有行标识缺失为0。

06:16:00短时校验显示Worker自然完成5个周期，done任务新增5，重启前在途任务经既有恢复流程完成。部署前失败记录的尝试次数及错误摘要一致性校验通过。

此次发布范围为隔离复现的批内同目标版本衔接及JSON脱敏诊断。真实历史冲突的唯一成因仍待证据，跨次任务幂等问题仍需单独处理。

# 运行恢复补丁 · 蒸馏输出约束与完成状态

2026-11-15，T018实现、隔离验证与独立审查完成，补丁处于未部署状态。

## 诊断与实现

只读诊断确认两条新失败均在3200输出预算下以length结束。两条事件各4024个ASCII字符，均低于已有6000字符输入裁剪阈值，关联记忆及修订计数均为0。原始生成响应未保存，具体膨胀字段及唯一成因仍无法确定。

既有最多3条、正文600字符的约束只写在提示中，schema未表达数量和长度。解析后只取前三条无法限制生成阶段的开销，还会漏掉后续独立候选。现将普通蒸馏和整合限制为最多3条，重算最多1条，正文最多600字符、事实键最多512字符，并按上下文约束来源引用数量。生成入口拒绝超长字段及去重后超量候选，整批校验完成后才返回Worker。

候选响应以length结束时，包含可解析JSON的响应同样进入原有800到3200的一次重试，预算耗尽后失败。顶层has_more=true表示尚有明确耐久信息未完整表达，整批失败并保留源事件和证据。重算Worker额外拒绝多个结果。旧包络及省略has_more的响应继续兼容，明确false的边界成功路径已有回归。

## 实际执行

```sh
.venv/bin/python -m pytest tests/test_generation_bounds.py -q --tb=no --basetemp <隔离临时目录>
.venv/bin/python -m pytest tests/test_generation_bounds.py tests/test_runtime.py tests/test_recompute.py tests/test_fact_time.py tests/test_embedding_limits.py -q --tb=short --basetemp <隔离临时目录>
nice -n 10 .venv/bin/python -m pytest tests -q --tb=short --basetemp <隔离临时目录>
uv build --wheel --out-dir <隔离构建目录>
git diff --check
```

首轮新增15项回归得到11 failed、4 passed，7.70秒。修复并补充边界用例后新增18 passed，9.21秒。定向105 passed，22.98秒。全套223 passed，69.15秒，1条上游Starlette弃用提示。wheel构建、Python AST和差异空白检查通过。仓库未配置独立类型检查器及lint工具，本轮复用现有验证工具。

新增回归覆盖三种生成入口的schema、闭合及不闭合JSON的length重试与耗尽、第四条独立候选、has_more、超长正文及事实键、3条/600字符/512字符成功边界、重复去重与旧包络、64 token耐久门控，以及真实Runtime和替代Runtime重算超量时正文与版本保留。失败路径核对实际job状态、完整源正文和证据文件可用性。

## 审查与验证边界

源线程独立逐文件人工审查覆盖runtime.py、生成约束测试、tasks及validation，未发现阻塞问题。独立执行如下命令得到49 passed，15.89秒。同会话并行只读静态复核也未发现阻塞问题。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_generation_bounds.py tests/test_runtime.py tests/test_recompute.py -q -p no:cacheprovider --tb=short --basetemp <独立隔离临时目录>
```

初轮缺少OCR入口，用户随后授权联网安装、配置和使用。已安装官方OCR CLI v1.13.10及open-code-review-delegate技能，并执行workspace模式的delegate preview和delegate rule。OCR默认筛选出runtime.py，按Python规则完成1/1文件委托审查，跳过0，覆盖率100%，未发现阻塞问题。默认排除的测试文件及两项Markdown文档均已补充逐文件人工复核。委托审查由OCR负责文件选择和规则解析，当前助手负责代码审阅。[官方技能说明](https://github.com/alibaba/open-code-review/blob/main/skills/open-code-review-delegate/SKILL.md)

全部模型测试使用本机合成HTTP端点，真实Store和Worker使用隔离数据。真实模型对原失败任务的恢复率尚未验证。has_more依赖模型如实报告，并为兼容旧响应允许缺省。conditions对象仍无新增长度限制，字符长度无法保证token上限。既有事件、上下文及证据输入裁剪继续存在，因此本轮无法给出全部事实语义覆盖保证。超出本轮表达上限的作业保留失败状态和证据，后续分批处理尚未实现。

# 普通distill批次事务补丁

2026-11-15，T019实现、隔离验证与独立复审完成，补丁处于未部署状态。

## 实现

Store.apply_distill_batch在一次SQLite事务内应用候选并标记job done，模型生成在事务外完成。公开apply_candidate与批次入口复用同一内部候选业务。候选业务及完成/失败标记的内部逻辑经AST比对与原实现一致，同批成功定向候选继续承接已确认版本。条件、CAS或完成标记失败时，本批记忆、修订、来源、审计、FTS与索引outbox一起回滚。Worker使用批次完成状态跳过第二次标记。

重试生成前按event关联的非policy修订及审计识别既有模型应用。命中时保留failed、原始last_error及既有数据，在现有job.payload记录待核对原因。即时T1声明的policy记录不触发该保护，自由文本调和可继续处理。静态复核发现的当前memory.model_id归属误判已先复现再修复，判定采用该event自身的历史记录。

## 实际执行

```sh
nice -n 15 .venv/bin/python -m pytest -c pyproject.toml <隔离基线复现脚本> -k 'later_conditions_failure or finish_failure' -q -p no:cacheprovider --tb=short --basetemp <隔离临时目录>
nice -n 15 .venv/bin/python -m pytest tests/test_distill_transactions.py tests/test_runtime.py tests/test_fact_time.py tests/test_recompute.py -q -p no:cacheprovider --tb=short --basetemp <隔离临时目录>
BMEM_OLLAMA_URL=http://127.0.0.1:1 nice -n 15 .venv/bin/python -m pytest tests -q -p no:cacheprovider --tb=short --basetemp <隔离临时目录>
nice -n 15 uv build --wheel --out-dir <隔离构建目录>
git diff --check
```

基线按目标断言得到2 failed，0.77秒，分别确认部分提交和完成标记窗口。T1跨事件模型更新豁免补充用例先得到1 failed、1 passed，0.15秒。修复后定向90 passed，7.19秒，全套235 passed，50.07秒，1条上游Starlette弃用提示。wheel构建、4文件AST与差异空白检查通过。

新增12项展开回归覆盖后续条件/CAS失败全回滚、完成标记失败全回滚、提交后模拟进程中断及恢复、旧无键ADD和低信任分支保护、仅审计的旧NOOP、连续定向更新、公开单候选、结构化T1与自由文本调和。两项原有runtime回归按整批原子语义调整，生成时的外部声明保留第2版。模型边界使用合成响应或CandidateStub，Store、Worker及数据库均为真实隔离实现。

## 审查与未测项

同会话只读静态复核已完成，T1归属误判修复后未发现新增具体问题。OCR委托完成2/2项应用源码审阅，跳过0，覆盖率100%。默认排除的两份测试和两份规格文档均已补充人工复核。

源线程独立复审已核对runtime/store核心差异与事务设计，未发现阻塞问题。独立低优先级执行tests/test_distill_transactions.py及tests/test_runtime.py，34 passed，4.29秒，采用隔离/tmp目录，未访问生产数据或模型。该结果单独记录，作者侧全套结果为235 passed，50.07秒。

本补丁基于c62c22a，事务正确性验证无法证明输出限制的真实模型质量。c62c22a仍缺完整真实配对，建议维持待部署状态。

历史候选未保存，保护仅能确认event已有模型应用，不能推断哪些剩余事实尚未完成，命中的旧重试进入待核对。重算采用原有独立入口，其正常回归已通过，本次未为该入口实施事务变更。提交后中断通过SystemExit模拟，未执行操作系统强杀、断电或生产故障注入。真实失败恢复及c62c22a输出约束的完整实模配对仍待验证。
