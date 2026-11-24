# 装配补丁格式与 apply_patch.py 使用规程（T044-C）

2026-11-24。依据 [plan.md §三.3](plan.md)（热点文件走装配补丁，形式定义固定）与 [tasks.md T044](tasks.md)。
本文是 T044 交付的** interim 格式定义**：`patches/schema.json` 由 T045 冻结（冻结条文 (10)），冻结后本工具的结构校验须与其对齐；本文与其冲突时以 T045 冻结版为准并修订本文。

工具：`scripts/apply_patch.py`（Python 3.9+，仅标准库，不做任何 git 写操作、不跑 cargo）。
自测：`tests/v2/gates/test_apply_patch.py`（纯文本夹具）＋内置 `--selftest`（临时目录夹具）。
样例：`examples/patch/T051a.json`（声明）＋`examples/patch/T050.json`（reconciler），**只供 --list/--dry-run 演示，禁止对主线 apply**（handler 为假想名）。

## 一、目录与文件约定

| 路径 | 内容 |
|---|---|
| `patches/<pkg>.json` | 每包一份装配补丁（pkg 如 `T051a`）；ASM 合流只运行 `apply_patch.py --wave <N>`，不手工编辑热点 |
| `patches/schema.json` | T045 冻结的 JSON Schema（本轮尚未交付；工具现用内置结构校验） |
| `tests/v2/artifacts/patches_applied.json` | 证据（plan §三.3 的 artifacts 口径；`--evidence` 可改指他处），append-only 运行记录 |
| `specs/005-agent-mcp-interface/examples/patch/` | 格式样例，不在默认 `patches/` 内，不会被合流误拾 |

热点文件（plan §三.3 点名，按 basename 匹配、整文件从严适用 reconciler 规则）：
`server.rs`（路由/dispatch）、`main.rs`（子命令枚举）、`index.html`（导航）、`intake.js`。
门禁文件（`test_native_hosts.py`、`test_integration.py` 台账段、`browser_smoke.cjs` 等）的归属由 run_gates 步 12 文件边界对账执行，不走本工具的热点规则。

## 二、补丁文件结构

### 顶层（`patches/<pkg>.json`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `pkg` | 否 | 包号；缺省取文件名主干 |
| `wave` | 是 | 波次整数 ≥0；`--wave N` 按此过滤 |
| `order` | 否 | 同波次内的应用序（默认 0，整数）；排序键 `(wave, order, pkg)`。**reconciler 包必须排序最后**，由 ASM 分配 order 保证（样例用 90） |
| `sample` | 否 | true＝样例，仅演示 |
| `notes` | 否 | 自由说明 |
| `patches` | 是 | 条目数组（见下） |

### 条目（`patches[]` 元素）

| 字段 | 必填 | 说明 |
|---|---|---|
| `file_path` | 是 | 仓库相对路径；禁止绝对路径、`..`、反斜杠；目标必须已存在（不创建新文件） |
| `function_scope` | 否 | 函数名标识符；锚点搜索与插入点被限制在该函数段内（见 §四） |
| `notes` | 否 | 自由说明 |
| `route_additions` | 否 | **声明**：`[{method, path, handler, capability_bits[]}]`；method 大写动词，path 以 `/` 开头 |
| `middleware_stack` | 否 | **声明**：`[{layer, order}]`（plan 固定字段） |
| `enum_variants` | 否 | **声明**：`[{name, payload?, doc?}]` |
| `match_arms` | 否 | **写入操作**数组（见下）；热点文件上只有波次最后包允许携带 |

条目必须至少含一条声明或一个写入操作。所有层级**拒绝未知字段**（防拼写错误；T045 冻结 schema 后如需扩字段，同步改工具白名单）。

### 写入操作（`match_arms[]` 元素）

```json
{
  "arm": "reconcile_routes_wave2",
  "anchor": {
    "lines": ["        (\"GET\", [\"v2\", \"facts\", id, \"history\"]) => {", "            identity.owner()?;", "            (200, authority::history(&read_snapshot(app)?, id)?)", "        }"],
    "position": "after"
  },
  "insert": ["        // 逐字面插入的行，自带缩进"]
}
```

- `arm`：操作键，**同一次运行内同文件唯一**；两包同键＝`ARM_CONFLICT`（两条补丁改同一 match arm）。
- `anchor.lines`：连续若干行**逐字符精确**文本（含缩进；不做空白归一、不支持正则/模糊/第 N 次命中选择器）。在目标文件（或函数段）内**命中唯一**才允许；0 命中＝缺失，≥2 命中＝歧义，均 FAIL。
- `anchor.position`：`after`（默认，插锚点块之后）或 `before`（插之前）。
- `insert` 与 `generate` **恰好二选一**：
  - `insert`：字面行数组（非空；每行不含换行符；缩进由作者自理）。
  - `generate`：`{"from": "route_additions"|"enum_variants"|"middleware_stack", "template": "..."}`，消费**同波次同文件**的全部该 kind 声明（含 reconciler 包自己的），按包序逐条渲染。模板用 `string.Template` 的 `${name}` 占位符（Rust 花括号无需转义）：
    - `route_additions`：`${method}` `${path}` `${path_segments}` `${handler}` `${capability_bits}`；`path_segments` 把 `/v2/agent/statements` 渲染为 `"v2", "agent", "statements"`（与真实 server.rs 的段数组写法同构），`capability_bits` 逗号连接。
    - `enum_variants`：`${name}` `${payload}` `${doc}`（缺省空串）。
    - `middleware_stack`：`${layer}` `${order}`。
    - 未知占位符在加载期即 FAIL；声明集为空＝`GENERATE_EMPTY`，禁止静默空操作。

## 三、执行流程与判定

1. 读 `patches/*.json` → 结构校验（失败＝`PATCH_JSON_INVALID`）。
2. 按 `file_path` 分组；同文件按 `(wave, order, pkg, 条目序, 操作序)` 排序。
3. 静态检查：arm 键唯一、声明去重（route 按 method+path、enum 按 name、middleware 按 layer，同波次同文件）、热点 reconciler 规则、声明必须被同波次同文件的 generate 消费（否则 `DANGLING_DECLARATION`）。
4. 逐文件内存 dry-run（plan 所谓「git apply --check 形态」的等价语义：JSON 补丁不是 unified diff，故不实际调用 git apply，而是全量模拟、全有或全无）：逐操作解析函数段→找锚点→查与本次已插入区域的重叠→插入并平移后续落点。**任何冲突即 FAIL，不写任何文件**。
5. 全部通过才逐文件原子写（同目录临时文件＋os.replace，保留原权限位），随后向证据追加一条运行记录。写失败尽力恢复已写文件。

### 冲突码与建议动作（FAIL 时 stdout JSON 的 conflicts[] 逐条给出 code/pkg/file/line/detail/suggestion）

| code | 含义 | 建议动作 |
|---|---|---|
| `ANCHOR_MISSING` | 锚点上下文不存在（原始文件也没有） | 只读打开目标逐字符核对（含缩进）；源码已漂移则重写补丁 |
| `ANCHOR_MOVED` | 锚点在原始文件唯一，但被前序补丁移动/破坏 | 改用未被触及的稳定锚点，或合并进 reconciler |
| `ANCHOR_AMBIGUOUS` | 锚点命中 ≥2 处（报告全部行号） | 加长锚点上下文直至唯一；禁止任选一处 |
| `OVERLAP_CONFLICT` | 锚点落在本次运行新插入的行上（链式锚定） | 锚定原始文件上下文；不支持链式，必要时合并进 reconciler |
| `ARM_CONFLICT` | 两包写同一 arm 键 | 合并进 reconciler 或改键 |
| `HOTSPOT_RECONCILER` | 热点文件上非最后包直接写 | 改为声明条目，由最后包 reconciler 生成；或经 ASM 调整 order |
| `ROUTE_DUPLICATE`/`ENUM_DUPLICATE`/`LAYER_DUPLICATE` | 同波次同文件重复声明 | 协调归属，只留一个声明者 |
| `DANGLING_DECLARATION` | 声明没有 reconciler 消费 | 由波次最后包交付 generate 补丁 |
| `GENERATE_EMPTY` | generate 消费空集/渲染为空 | 补声明或删空 reconciler |
| `SCOPE_MISSING`/`SCOPE_AMBIGUOUS` | 函数名找不到/同名多定义 | 核对函数名；改用更长锚点并去掉 scope |
| `SCOPE_ESCAPE` | 插入点越出函数段 | 锚点移进函数体内 |
| `FILE_MISSING`/`FILE_UNREADABLE`/`CRLF_UNSUPPORTED` | 目标不存在/不可读/含 CR | 核对路径；统一 UTF-8 LF |
| `WRITE_FAILED`/`EVIDENCE_WRITE_FAILED` | 落盘/证据 IO 失败 | 查磁盘权限；已写文件已尽力恢复 |
| `ROLLBACK_DRIFT` | 回滚时当前内容 sha ≠ apply 后 sha | **人工接管**：对照证据 before_content 手工合并，工具拒绝盲目覆盖 |
| `ROLLBACK_ORDER`/`ROLLBACK_NOTHING`/`ROLLBACK_UNKNOWN_RUN`/`ROLLBACK_ALREADY` | 回滚顺序/对象错误 | 按 LIFO 逐运行回滚；核对 run_id |

## 四、函数作用域（文本启发式，非语法解析）

`function_scope` 给定函数名时：定位含 `fn <name>` 的行，用**朴素花括号计数**找到配平的闭括号行，得到函数段；锚点必须在段内唯一命中，插入块必须完整落在段内（边界外＝`SCOPE_ESCAPE`）。
局限：字符串/注释里的花括号会干扰计数；同名多定义直接 FAIL。失败形态一律是锚点/作用域错误 → 整批不写 → 人工接手，不会静默插错位置。

## 五、CLI 与退出码

```
python3 scripts/apply_patch.py --list                     # 只输出装配计划（不读目标文件）
python3 scripts/apply_patch.py --wave 1 --dry-run         # 全量模拟，不写任何文件
python3 scripts/apply_patch.py --wave 1                   # ASM 合流入口：通过才写＋留证据
python3 scripts/apply_patch.py --rollback [--run-id ID] [--dry-run]
python3 scripts/apply_patch.py --selftest                 # 内置临时夹具自测，不碰仓库源码
python3 scripts/apply_patch.py --dry-run --wave 2 \
    --patches-dir specs/005-agent-mcp-interface/examples/patch   # 样例演示（对真实仓库只读）
```

可选：`--root`（默认脚本上级目录）、`--patches-dir`（默认 `<root>/patches`）、`--evidence`（默认 `<root>/tests/v2/artifacts/patches_applied.json`）。相对路径按 root 解析。
stdout 恒为单个 JSON 报告（机器可判定），stderr 为人读摘要。
**退出码：0＝PASS/成功；1＝FAIL（冲突/校验/回滚漂移）；2＝用法/环境错误**（目录缺失、参数互斥等）。

## 六、证据与回滚

证据 `patches_applied.json`：`{schema_version:1, runs:[...]}` append-only。每条 run：
`run_id`、`mode`、`wave`、`applied_at`、`commit_before`/`commit_after`（apply 时刻的 `git rev-parse HEAD` 只读快照；**工具不做提交**，两者相同，ASM 的合流提交号由台账/manifest 登记）、`rolled_back`、`files[]`、`declarations`（键 `wave|file|kind`）。
`files[]` 每项：`file_path`、`packages`、`before_sha256`/`after_sha256`（整文件 SHA-256）、`before_content`（全文，供回滚）、`ops[]`（`pkg`/`wave`/`arm`/`function_scope`/`anchor{lines,position,line}`/`line_range`（最终文件 1-based 闭区间）/`insert_sha256`）。

回滚：默认取最近一次未回滚 run（`--run-id` 可指定）；**必须 LIFO**（更晚 run 未回滚即 FAIL）；逐文件校验当前 sha==after_sha256，漂移即 FAIL 交人工；通过才按 before_content 原子恢复并标记 rolled_back。`--rollback --dry-run` 只校验不写。

## 七、能力边界（不能表达什么，失败时人怎么接手）

**只做插入，不做其他**：
- 不能删除/替换/移动既有行，不能创建新文件，不能跨文件重命名/重构；此类变更由归属包在 worktree 内直接实现（函数级互斥），不走装配补丁。
- 不做语法解析与语义校验：不验证 Rust 可编译、不去重字面插入的重复路由（声明路径有去重；字面 insert 的重复由 cargo 门禁兜底）、不理解 match 语义。
- 锚点是逐字符精确行：源码漂移（重构、fmt 改缩进）即 FAIL，不做模糊匹配。
- 不支持链式锚定（锚在本次新插入的行上）；多包对同一锚点 stacked 插入按包序确定性排布。
- 仅 UTF-8 LF；CRLF 拒绝。
- 函数段定位是花括号计数启发式，可被字符串/注释中的花括号骗过——失败形态是 FAIL 而非错插。

**人工接手规程**：读 stdout 冲突 JSON（code/pkg/file/line/suggestion）→ 只读打开目标文件核对 → 修正补丁 JSON（重锚/合并进 reconciler/改声明归属；order 调整须 ASM 同意）→ 重跑 `--dry-run` → 通过后 apply。回滚遇 `ROLLBACK_DRIFT` 时禁止盲目覆盖：对照证据 `before_content` 与当前文件手工合并。外层安全网：工具全程零 git 写操作，ASM 按波次提交，`git diff`/`git checkout` 是最终恢复手段。

## 八、样例（examples/patch/）

- `T051a.json`（wave 2, order 10）：只在 server.rs 上**声明** `GET /v2/agent/statements`（假想 handler `agent_statements_shared`，capability_bits `["statement_list"]`）。
- `T050.json`（wave 2, order 90，最后包＝reconciler）：锚点四行**逐字符取自真实 [server.rs:538-541](../../src/v2/bmem/src/server.rs#L538-L541)**（`GET /v2/facts/:id/history` arm，全文件唯一命中，已只读核对），`generate` 消费本波次全部 route_additions，渲染与真实路由表同构的段数组 match arm：

```rust
        ("GET", ["v2", "agent", "statements"]) => {
            identity.bound()?;
            (200, agent_statements_shared(&read_snapshot(app)?)?)
        }
```

对真实仓库 `--dry-run --wave 2` 实测 PASS：锚点解析行 538、生成 arm 落点 542-545、`dispatch` 作用域内、server.rs SHA 不变。main.rs 枚举（`enum Command`，[main.rs:49](../../src/v2/bmem/src/main.rs#L49)）与 index.html 导航（`data-view` 按钮，[index.html:90-125](../../src/v2/web/index.html#L90-L125)）同理：声明 `enum_variants`/字面 insert，锚点取真实上下文，reconciler 最后写。

## 九、T044 最小可用设计补白（plan 留白处，T045 冻结时可改）

1. 顶层 `order` 字段＋pkg 名 tiebreak 定义「波次内最后包」；plan 只说「最后一个包」，未定义排序依据。
2. 锚点 schema `{lines, position}`、唯一命中强制、无选择器；plan 只说「精确锚点上下文」。
3. `match_arms` 写入操作 schema `{arm, anchor, insert|generate}` 与 arm 键唯一性。
4. `generate` 消费机制（string.Template、`path_segments`/`capability_bits` 派生占位符）；plan 只说 reconciler「消费所有 route_additions」，未定义消费接口。
5. 声明去重键、`DANGLING_DECLARATION` 强制消费、`GENERATE_EMPTY` 禁空操作。
6. 热点集合按 basename 整文件从严（plan 热点是文件内片段；从严避免片段归属争议）。
7. 证据 append-only＋LIFO 回滚＋after-sha 漂移守卫；plan 只要求记录 pkg/file/line_range/commit_before/commit_after，回滚语义为任务指令新增。
8. `commit_before/commit_after`＝apply 时刻 HEAD 只读快照（工具禁提交）。
9. 函数段花括号计数启发式与 `SCOPE_ESCAPE` 边界拒绝。
10. 未知字段拒绝、UTF-8 LF 限制、退出码 0/1/2、stdout 单 JSON 报告。
