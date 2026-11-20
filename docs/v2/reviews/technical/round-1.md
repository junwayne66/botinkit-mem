# REVISE

**总体结论：** bmem V2 技术设计在权威事务、授权/撤回、三态反馈、三档压缩与验收判据上已达可实施深度，六域 18 模块、双入口契约与可执行 Schema 经逐项交叉核对基本自洽。但存在 1 项必须解决的规格空洞（自定义数据根的持久解析未定义，与本文档自身的"所有入口指向同一份数据"不变量直接冲突）。修复为局部段落级修订，不需要重开架构。

---

## 必须解决

### R1-T01 · 严重度：中 · `--home` 数据根没有定义持久化与统一解析次序，存在"双脑数据根"失败模式

- **位置：** `docs/v2/technical/cli-and-platform.md` §工作目录与配置所有权（第 43–54 行）；同文 §安装、启动与升级的系统差异（LaunchAgent / 计划任务 / systemd 用户 unit 登记）；`docs/v2/technical/operations.md` §一（第 5 行 `bmem init` 默认根描述）。
- **依据与矛盾点：** 同一节宣称"程序用系统用户目录 API 解析绝对路径；后台进程、MCP 和 CLI 从不同工作路径启动也指向同一份数据"，但服务发现文件位于数据根内的 `run/`。对网络盘主目录，文档只说"由用户用显式的全局参数 `--home <本机目录>` 选择本机卷，并把同一绝对路径传给该实例的服务和适配器"——全文没有任何一处定义该覆盖被持久记在哪里、其余入口按什么次序恢复它（已用 `--home`、服务发现、`init/update/doctor/web`、MCP stdio、hook、卸载器逐一检索确认仅此一处提及）。
- **实际失败场景：** 用户主目录在网络盘，`bmem init --home ~/local/.bmem` 成功并配好模型与两宿主。次日新终端运行 `bmem declare ...`（不带 `--home`）：CLI 按默认解析落到网络盘 `~/.bmem`，找不到实例，要么报错、要么在未通过 WAL 校验的路径上静默建立第二个权威库；登录自启动的三个平台启动器同样不知道该拉起哪个根，可能出现两个服务并行写入，声明、幂等键与撤回水位跨根不可见——恰好击穿本设计最核心的"单权威存储 + 双入口同对象"承诺，且用户很难自行发现。
- **可验证修订要求：** 在 `cli-and-platform.md` 增补：(1) `--home` 决议结果的持久位置与格式（例如默认根或平台配置目录下的不依赖 WAL 的轻量指针文件），并写明各入口的统一解析优先级：显式 `--home` 参数 > 持久指针 > 默认根；(2) `launchd` plist、Windows 计划任务、systemd 用户 unit 登记命令必须携带解析后的绝对路径，卸载与 `--unbind-client` 一并清理；(3) 失败关闭规则：检测到指针与实际路径不一致、或目标路径 WAL 条件不成立时，显式报错并要求用户选择，禁止静默初始化第二个根；(4) 在 §三平台交付验收表增加一行：网络盘主目录 + `--home` 场景下，CLI/Web/hook/MCP 与重启后的服务全部命中同一实例，重复 `init` 不产生第二数据根。

---

## 可选意见（不阻塞实施）

- **R1-O02 · 低 · 服务停机时 CLI 写命令行为未规定。** `docs/v2/technical/command-catalog.md` §同一次操作怎样从两个入口执行 与 `cli-and-platform.md:41` 只规定 `doctor/status` 可本机直读；`config set`、`declare` 等在实例未运行时是自动拉起登记的服务还是返回明确错误未定义。建议一句话定死（推荐：自动拉起指针登记的实例，失败给可操作错误），并双端一致。
- **R1-O03 · 低 · 事实键与不透明 fact_id 混用。** `command-catalog.md:72,85` 示例 `bmem statement history report.opening` 传的是"主体.谓词"键，而路由是 `GET /v2/facts/{fact_id}/history`，且 `module-integration.md:41` 规定所有 `*_id` 由所有者签发。建议注明 CLI 接受键别名并由服务端解析，或把示例改为 fact_id。
- **R1-O04 · 低 · config.patch 输出 Schema 未做条件约束。** `interfaces/config-patch.md:52` 说"涉及模型目录、绑定或连接参数的请求分别附带 `model_result` / `bindings` / `connector_result`"，但 `interfaces/config-patch.example.json` 的 output_schema 中三者均可选；两个入口可能各自通过契约测试却返回不同形状。建议为 input→output 增加 if/then 必填关联。
- **R1-O05 · 低 · 来源页"同页键、不同内容"未定义。** `domains/ingestion.md` §M02 重投与乱序 只规定相同页键**和内容**复用回执；异文同键是否 `409 key_conflict`（`contracts.md` §调用约定已为业务写入定义该语义）未说明，建议补入并纳入 M02 独立验收。
- **R1-O06 · 备注。** `payloadSchema` 以远程 `$ref` 引用 2020-12 元 Schema，离线契约测试需预绑该元 Schema（`jsonschema` 库内置，作者自验记录亦采用；仅提示 CI 固定行为）。

---

## 已核对并判定自洽的关键点

- **Schema 可执行性（手工逐项）：** `command-descriptor.schema.json` 的 required/allOf 六条对 `config-patch.example.json` 逐分支验证通过；owner 写动作强制双入口、workflow 禁第二 HTTP 写路径、action 方法枚举等均与 `extension-contract.md` 文字一致。`config-patch.md` 六个正例与四个反例均被 input/output Schema 正确判定；endpoint 模式排除 userinfo/查询串、credential 双后端、`bindings` idOrNull、"最多一个模型和一个来源"由 singular oneOf 结构天然保证。
- **原子性与重试：** 单写者短事务 + 同事务 outbox；页附件先持久化后提交引用、无引用对象回收避让在途写与快照引用；`credential.store → config.patch` 分步补偿；配置回执 64 窗口 + `expected_revision` 单调防超窗重复应用；作业唯一键（种类/目标/输入版本/策略版本）、领取过期恢复、提交前重查授权与源版本；治理单据逐位置重试不重做已成功位置；恢复预览水位变化 409。
- **撤回/披露：** 404 无正文统一拒读语义；依赖组失效先停供、独立证据重算；墓碑防重导、明确恢复事件要求；已发外部单列且"旧包不得作为当前授权"；共享声明按字段独立裁剪、跨路由句柄不能补无权原文；仅追加宿主的干净重建路径；物理块多引用下的撤权分离展示与重打包清理单。
- **三态反馈：** `provided/loaded/used` + `passed/failed/unknown` + 归因未知，各层判定条件、跨 epoch 拒绝、部分装入明细与"整包成功不上传单条采用"均可机检。
- **并行接缝：** D1–D6 端口与提交边界表完整；18 模块与 `research/reassessment-2026-11/modules.md` 逐行逐域匹配；每模块均有可用临时库/录制端点独立执行的验收；`handoff-fixture.json` 实测计数与 `module-integration.md:56` 声明一致（5 输入、3 路由、7 时点、8 组断言），各域分册引用的标签全部存在。
- **冻结要求与引用数字：** 438/369/203、撤回后空目录复活命中 1 等引用与 `local-evidence.md` 逐项一致，锚点有效；三平台 6 制品、双模型通路、向量默认零探测、三档双预算与预注册单侧 95% 下界 ≥0 的准确率门槛覆盖全部用户确认要求；`research.sha256` 十项与文档声明吻合。

## 审读覆盖与限制

- 已通读技术清单全部 21 个文件（含六域分册、四份 interfaces、三份总览/运行/压缩章节），并按相对路径核对 `docs/v2/baseline.md`、product 四册、`ui/README.md`、`delivery-plan.md`、`research/…/modules.md`、`local-evidence.md` 与被引协议文档 `docs/llm-providers.md`。
- 未做（声明为限制）：只读环境下无法执行 JSON Schema 校验器与 `shasum -c` 复算（均为作者已声明、可进 CI 的机械项）；外部 URL 一律未验证；`ui/prototype.html`、`preview.png` 等原型资产不在本快照与技术清单内，未审。
