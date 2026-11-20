# PASS

R2 增量复审结论：唯一阻塞项与全部可选项均已闭环，未引入新的实质性问题。技术设计可进入实施（仅指技术方案本身；三平台制品、真实宿主联调等仍是实施后验收，本轮维持 R1 的分期判断）。

## 逐项判定

**T01 闭环（原必须项）。** `docs/v2/technical/cli-and-platform.md` §工作目录与配置所有权现给出完整闭环：根外定位文件（三平台具体路径 + `<machine>` 散列解决共享主目录多机）、解析优先级"显式 `--home` → 已登记根 → 首次默认根"、已绑定实例与 `--home` 不符即退出并报两条路径（fail-closed，直接封死 R1 的双脑场景）、`instance.json`/定位/`run/` 服务三方身份握手、仅 `fresh` 态可建根、定位丢失走核验修复而非重建、launchd/计划任务/systemd 与 hook/MCP 登记携带绝对根参数与 `instance_id`、升级/卸载沿用同一解析器。迁移由 `restore preview --target-home` + 维护锁 + 定位原子替换 + 启动项重登记构成，并与 `contracts.md` 治理表（预览绑定实例/目标/水位，改变须重预览）、`operations.md` §迁移本机数据卷、`command-catalog.md` 行 106 三处一致；验收表新增三行覆盖网络盘主目录、损坏定位、冲突根与第二个库出现与否。旧矛盾句（"把同一绝对路径传给…适配器和"、"服务停机时命令行直接检查…"泛化表述）已移除。R1 修订要求的四点（持久位置、启动器绑定、fail-closed、验收行）全部可核。

**O02 闭环。** 停机写行为三处一致：`command-catalog.md:9`、`cli-and-platform.md:41`、`operations.md:5`——业务写先定位已核验实例再拉起/复用、握手通过才提交、失败不受理、断线沿原幂等键、Web 保留草稿经 `bmem web` 重连、两端不离线写库、`doctor/status` 保留本机直读。与新解析规则无冲突。

**O03 闭环。** `contracts.md:60–66` 声明响应改发不透明 `f_72` + 可读 `key`，并新增"全局与项目同键不同 ID、按 ID 查历史互不串"的验收口径；`command-catalog.md:73,87` 同步。残留 `report.opening` 均为维度键或夹具标签语义，与新约定一致，不再与 `fact_id` 路由混用。

**O04 闭环（含对原建议的合理驳回）。** 作者指出单一 response Schema 看不到请求，成立；新 `interfaces/config-patch-exchange.schema.json` 成对约束我逐条核过：三条 if/then/else 在 `request.patch` 必存在前提下语义正确（含"未涉及的组不得多带结果"的 else 排除），与 `config-patch.md` §校验一次成功交换的六个正文示例及混合补丁示例相容；数据值核对（op、ID 复用、`expected_revision+1`、幂等重放配对）由文内脚本承接。本评审环境无执行工具，脚本按静态阅读 API 用法正确（jsonschema≥4.18 的 registry 接口），作者声明已断网跑通——接受。

**O05 闭环。** `domains/ingestion.md:28,34`：同页键规范内容比较（排除传输噪声）、同键异文整页 409 且事件/待办/游标不变、真实编辑须新原生版本新页键，正反验收成对补齐。

**O06 闭环。** `extension-contract.md:18` 规定离线资源表注册元 Schema、按 `$id` 登记、缺失即契约错误；`config-patch.md` 的可运行块即为该模式的示范。

**清单计数。** 接受勘正：R1 为 20 个技术文件（17 Markdown + 3 JSON），我首轮"21"系笔误，不影响首轮逐项覆盖结论；R2 现 21 个（`review-input/r2.sha256` 逐行核对 = 17+4）。

## 剩余必须改项

无。

## 剩余建议（均不阻塞）

- S1：macOS/Linux 定位文件仍位于账户主目录（网络卷场景下依赖"单文件 rename 通常可用"的前提）；文档已声明不可安全替换时诊断报告，可接受，实施中建议在 doctor 里显式区分"定位所在卷"与"业务根所在卷"。
- S2：`command-catalog.md:22` 的 `status → health.read GET /health` 未复述停机时改走本机直读的分支（`cli-and-platform.md:41` 已定义），可在该行加一句引用以免实现者只读目录表。
- 覆盖限制照旧：SHA 复算、脚本执行与外部链接在本只读环境未验证，依赖作者记录与后续 CI。
