# bmem V2 技术设计独立评审

**2026-11-19 · Claude Code 对 R3 给出 PASS，可进入实施。** 本记录对应 [技术方案](../../technical/README.md) 的 21 个文件（17 份 Markdown、4 份 JSON）。三轮使用本机 Claude Code 2.1.308 的同一独立只读会话 `23db0828-f1d8-411a-a942-89ad07096fd9`。产品、UI 及冻结研究作为参照。评审员出具结论，作者修改并运行静态校验。

| 轮次 | Claude Code 结论 | 技术文件清单 SHA-256 | 原文及逐文件指纹 |
|---|---|---|---|
| R1 | REVISE | `47164c828a2db80e90ea46589631a1fbe798de06a6962e883da9cc577bcb6fcd` | [评审原文](round-1.md) · [清单](round-1.sha256) |
| R2 | PASS，有两条可选建议 | `baf048c3e3f6e3f3eeff28d951f23f1ffca4381100d1a5b2c69d89cb178f7ce7` | [评审原文](round-2.md) · [清单](round-2.sha256) |
| R3 | PASS，无剩余必须项或实质建议 | `601ab7ccddb27f4b28d62e5be3a6b3b33cfbb35aff6599c9d22b7ef248427350` | [评审原文](round-3.md) · [清单](round-3.sha256) |

> **清单语义（2026-11-20 补记）**：三份 `round-N.sha256` 是各轮评审**时点的不可变证据**，其自身 SHA-256 即上表封印值，记录评审员当时实际读到的文件版本，不随后续修改更新，直接用它们校验当前文件出现 FAILED 属预期。R3 通过后实施期对 `cli-and-platform.md`、`interfaces/config-patch.md`、`interfaces/config-patch.example.json` 三个文件做了细化（不改变 R3 已冻结的权威写入、授权与交付语义）。当前工作区的可校验基线是 [current.sha256](current.sha256)（同 21 个文件），技术文件变更后须重算此清单。

## 意见与处理

| 首轮编号 | 作者处理 | 修订位置与复核方式 |
|---|---|---|
| R1-T01，必须修订 | 采纳。在业务根外登记当前用户、本机的绝对数据根及实例 ID；CLI、Web、启动器、hook、MCP、升级和卸载共用解析规则。路径或身份不符时停止读写。迁移目标由恢复预览绑定 | [实例定位与三平台验收](../../technical/cli-and-platform.md) · [重启与隔离恢复](../../technical/operations.md) |
| R1-O02，服务停机 | 采纳。普通写命令先拉起已登记的实例，成功握手后再提交。浏览器断线保留草稿，用原幂等键重试；诊断可保留停机现场 | [CLI/Web 提交约定](../../technical/command-catalog.md) |
| R1-O03，事实标识 | 采纳。声明回执中区分维度键和事实 ID，CLI 与网页都按服务签发的 ID 查历史。增加同维度、不同范围的验收 | [声明回执样例](../../technical/contracts.md) |
| R1-O04，请求与回执的对应 | 采纳配对验证。只验证一个响应无法得知请求改了哪些字段，新增成对 Schema。请求改了模型、绑定或连接器，就必须收到对应的结果，测试另核对动作、对象 ID、绑定值和修订 | [成对 Schema](../../technical/interfaces/config-patch-exchange.schema.json) · [离线复验命令](../../technical/interfaces/config-patch.md) |
| R1-O05，重投同一页 | 采纳。同一页键、同一内容返回原回执；同键异文整页返回 409。独立验收核对事件数、待办数和游标 | [来源页重投](../../technical/domains/ingestion.md) |
| R1-O06，离线校验 | 接受备注。测试预绑 JSON Schema 元规范及本地引用，缺少引用直接失败。已在禁用 DNS 和 socket 连接的进程中执行文内复验命令 | [扩展契约](../../technical/extension-contract.md) |
| R2-S1，分卷诊断 | 采纳。诊断分列定位文件卷和数据卷，测试各自失败时的原因 | [平台诊断](../../technical/cli-and-platform.md) |
| R2-S2，停机时查状态 | 采纳。命令表标明在线读服务，停机读本机状态；单纯查看状态不会启动服务 | [命令目录](../../technical/command-catalog.md) |

## 核验范围

作者从仓库根运行以下命令核对最终稿和冻结研究。另用项目开发环境校验了动作描述符、配置正反例、18 个模块及接力夹具，并禁用 DNS 和 socket 连接运行了文内的[请求/响应成对检查](../../technical/interfaces/config-patch.md)。三轮报告均取自同一 Claude Code 会话成功返回的最终文本。首轮报告文件数笔误已在第二轮由评审员更正。

```sh
# 当前技术文件基线（活清单，技术文件变更后重算）
shasum -a 256 -c docs/v2/reviews/technical/current.sha256
# 冻结研究输入（不可变，必须全部 OK）
shasum -a 256 -c docs/v2/research.sha256
# 评审时点证据（不可变历史；与当前文件出现 FAILED 属预期，仅用于复现评审现场）
shasum -a 256 -c docs/v2/reviews/technical/round-3.sha256 || true
```

本次通过的范围是技术设计。三平台制品、真实 CLI/Web 修改、Claude Code 与 Codex 跨宿主接力、真实提炼准确率和压缩性能仍按[实施验收](../../technical/module-integration.md)执行。
