# Worktree 并发开发协议（T044-B 可执行一页）

2026-11-24。依据 [plan.md](plan.md) §三/§五/§八.4 与 [tasks.md](tasks.md) T044，整理为可执行规程；数字以 plan §八 锁版值为准。配套脚本：`scripts/cleanup_worktrees.py`（本页 §4）、`scripts/probe_agents.py`（执行体探针）、`scripts/apply_patch.py`（T044-A 交付，接缝见 §5）。

## 1. 命名与 meta

- worktree 一律放 **`<repo>/.worktrees/T<NNN>-<slug>/`**（`.gitignore` 已含 `.worktrees/`）。不放 `/private/tmp`（OS 会清理）。
- 创建时**立即**写 **`.worktrees/<pkg>.meta`**（`<pkg>`＝worktree 目录名），四个字段缺一不可：

```json
{
  "pkg_id": "T046",
  "created_at": "2026-11-24T12:00:00Z",
  "executor": "CC",
  "base_commit": "5c44edc76ef980eb674986f569c73f3968e461c2"
}
```

- 无 meta 的 worktree 会被清理器按陈旧处置（§4）。在途 spike 也不例外：先写 meta，或让 ASM 用 `--exclude` 显式保护。

## 2. 创建流程

```sh
cd <repo>
git worktree add .worktrees/T046-client-identity -b pkg/T046-client-identity <base_commit>
# 立即写 .worktrees/T046-client-identity.meta（§1 四字段，created_at 用 UTC ISO8601）
cd .worktrees/T046-client-identity
export CARGO_TARGET_DIR="$PWD/target-T046"   # <worktree>/target-<pkg>，gitignore **/target-*/
export CARGO_BUILD_JOBS=2
cargo build --locked --jobs 2              # 显式 --jobs，见 §3
```

- `~/.cargo` 只读共享，不在 worktree 里改全局 cargo 配置。
- 实例测试一律 mkdtemp 隔离根；不触碰真实 `~/.bmem`；不推送任何远端。

## 3. 并发与 jobs 数学（plan §八.4 锁版值）

| 项 | 生效值 |
|---|---|
| 在途实现 worktree | **≤2**（一实现一复核，或双实现交叉 codex 复核） |
| 并发 cargo 进程 | **≤2**（与在途 worktree 同口径） |
| 每进程 jobs | 显式 **`--jobs 2`**＋全局 `CARGO_BUILD_JOBS=2` → 2×2=4 线程，10 核留 6 核给系统/门禁 |
| loadavg>8 | 降 **`--jobs 1`**。建议阈值，ASM 按实测调整（允许波内调整） |
| run_gates 启动 cargo | 显式传 `--jobs 2` |
| 构建类任务 | 不与门禁同波次 |
| 每 worktree 构建产物 | `target/`＋`target-*/` 合计上限 **5 GB**，超出即 FAIL 并清理 |
| 角色 | DSH 只做固化模板填空与装配树复跑；codex 只读复核全程并行；ASM 单点串行合流 |

## 4. 销毁流程与硬闸门（cleanup_worktrees.py）

**陈旧判定（命中任一）**：无 meta／meta 无效；`created_at` 超 **7 天**；包已合流（ASM 台账 `[x]`，或分支 tip≠`base_commit` 且为 repo HEAD 祖先且工作区干净——新建未提交的 worktree 不会被误判）；构建产物超 5 GB；目录已不存在（仅 prune）。

**硬闸门顺序，任何一步失败即拒删（exit 1），默认行为是「先证明已备份，再删」**：

1. **归档**不可再生内容到 `~/archive/bmem-worktrees-<YYYYMMDD>/<pkg>/`（append-only，不覆盖既有归档）：
   - `tracked.patch`（`git diff --binary HEAD`）、`untracked/`（全部未跟踪文件）、`status.txt`（`git status --porcelain=v1 -uall` 快照）、`meta.json` 原件；
   - **唯一被忽略文件**（日志、本地配置等）一并归档——实测 `git worktree remove` 连被忽略文件一起删（共享记忆 pitfall `mem-codex-pitfall-74e8e8831d`）；
   - 可再生构建产物（`target-*/`、`node_modules/`、`__pycache__/`、`.venv/`、`*.pyc` 等）只盘点数量/字节，不复制；
   - `manifest.json`：逐文件 size＋sha256。
2. **打 tar 并回读验证**：`<pkg>.tar.gz` 逐成员 sha256 与清单一致才算备份成立。
3. 验证通过后才 **`git worktree remove --force`**，删除对应 `.meta`；收尾 **`git worktree prune`**。
4. 写工件 **`tests/v2/artifacts/worktree_cleanup.json`**（apply 模式）。

**命令与退出码**（`--help` 全量）：

```sh
python3 scripts/cleanup_worktrees.py --list       # 只列 worktree 与 meta
python3 scripts/cleanup_worktrees.py --dry-run    # 门禁步0：陈旧报告，不写盘不删除
python3 scripts/cleanup_worktrees.py --apply [--exclude <在途pkg>]...   # 门禁步13
python3 scripts/cleanup_worktrees.py --selftest   # 临时目录单测（含「未归档即拒删」）
```

exit 0＝成功／dry-run 报告完成；1＝拒删、失败或自测红；2＝用法错误。`--exclude` 只跳过处置、仍报告陈旧原因，供 ASM 保护在途包。主 worktree 永不处置。**每波合流前 ASM 确认 `.worktrees/` 只有当前波次的包。**

## 5. 越界检测与装配补丁接缝

- 包定义必须列**函数级**互斥文件集；run_gates 步 12 对账声明集 vs `git diff --name-only`＋untracked，交集非空即阻断；另断言 `Error::conflict(` 计数不增。
- 热点文件（`server.rs` 路由/dispatch、`main.rs` 子命令枚举、`index.html` 导航、`intake.js`、门禁文件）禁多包直改：每包交付 `patches/<pkg>.json`（schema 由 T045 冻结），ASM 合流**只运行** `scripts/apply_patch.py --wave <N>`（分组→排序→逐文件 dry-run→冲突即 FAIL→apply→`artifacts/patches_applied.json`）。
- **接缝**：`cleanup_worktrees.py` 只触碰 `.worktrees/`、归档目录与自己的工件，从不碰 `patches/` 与热点源码；`apply_patch.py` 只在装配树运行。worktree 销毁不影响已交付的补丁 JSON（补丁交在主仓库）。台账/validation 由 ASM 统一维护，worker 不改 `tasks.md`/`validation.md`（清理器只读台账勾选作合流信号）。

## 6. 本轮基线事实与教训（2026-11-24）

- 上一轮遗留 **6 个陈旧 worktree** `/private/tmp/bmem-data-implementation.ONIz66vc/{adapters,history,intake,ledger,status,transfer}`（os.walk 实测约 **4.5 GiB**）**已删除并 prune**。存证仅 `~/archive/bmem-worktrees-20261124/` 的 6 份 tar：adapters.tar.gz 15052 B、history.tar.gz 12545 B、intake.tar.gz 13597 B、ledger.tar.gz 10928 B、status.tar.gz 4954 B、transfer.tar.gz 7207 B。
- **教训（不可恢复事故，必须铭记）**：父目录里的**包日志与 prompt 未归档即被删除，已不可恢复**。自此删除任何包目录（含父目录）前必须先归档不可再生内容并回读验证；`cleanup_worktrees.py` 已把「归档→tar 验证→才删」做成硬闸门，归档或验证不通过一律拒删。
- 写本页时实测：主 worktree HEAD `5c44edc`（fix/web-ux-grant-preview），`git status` 干净；`.worktrees/T044-rmcp-preflight` 为 T044 rmcp 编译预检的在途独立 spike worktree（plan §六.5），**写本页时其 `.meta` 尚未补写**，dry-run 按协议报 `no_meta`（构建产物约 1.0 GB，未超 5 GB）。spike 属主应按 §1 补 meta；spike 结束前门禁步 13 用 `--exclude T044-rmcp-preflight` 保护。**本包未删除任何现存目录。**
- 执行体探针证据：`evidence/agents/20261124.json`（`/evidence/` 已 gitignore，不入库），三档结论与失败原文以该 JSON 为准；每波合流复探（`probe_agents.py` 实跑）。
