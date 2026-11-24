# T044-D rmcp Streamable HTTP 依赖预检 · 执行摘要

## 任务完成状态

✅ **完成** — 所有子任务已交付

---

## 执行步骤回顾

### 1. 创建隔离 worktree ✅

```bash
git worktree add .worktrees/T044-rmcp-preflight HEAD
```

- **路径**: `.worktrees/T044-rmcp-preflight`
- **状态**: 已创建，detached HEAD at 5c44edc
- **体积**: 965 MB（含 target-preflight 962 MB）

### 2. 启用 transport-streamable-http-server feature ✅

修改 `src/v2/bmem/Cargo.toml`:

```toml
rmcp = { version = "3.11", default-features = false, features = [
  "server",
  "transport-io",
  "transport-streamable-http-server"  # 新增
] }
```

### 3. 依赖拉取与网络检查 ✅

```bash
CARGO_TARGET_DIR=target-preflight cargo fetch
```

**结果**:
- 网络: ✅ 可用
- Cargo.lock: ✅ 更新成功
- 新增 crate: async-trait 0.1.92, sse-stream 0.2.6, tokio-stream 0.1.19

### 4. 编译测试 ✅

```bash
CARGO_TARGET_DIR=target-preflight cargo build --locked --jobs 2
```

**结果**:
- 编译: ✅ 成功
- 耗时: 45.65 秒
- 错误: 0
- 警告: 0
- 退出码: 0

### 5. 协议版本确认 ✅

从 rmcp 3.11.0 源码确认协议版本上限:

```rust
// rmcp-3.11.0/src/model.rs
pub const V_2025_11_25: Self = Self(Cow::Borrowed("2025-11-25"));
pub const V_2025_06_18: Self = Self(Cow::Borrowed("2025-06-18"));
pub const V_2025_03_26: Self = Self(Cow::Borrowed("2025-03-26"));
```

**结论**: rmcp 3.11.0 最高支持 **2025-11-25**（legacy era）

### 6. 挂载边界验证 ✅

从 rmcp 测试代码提取标准挂载模式:

```rust
let service = StreamableHttpService::new(
    || Ok(MyHandler::new()),
    Arc::new(MySessionManager::default()),
    StreamableHttpServerConfig::default(),
);

let router = axum::Router::new().nest_service("/mcp", service);
```

**兼容性**:
- axum 0.8.9: ✅ 兼容
- tower 0.5.3: ✅ 兼容
- 挂载方式: `Router::nest_service("/mcp", service)`

---

## 关键发现

### 1. 新增依赖可控

| 依赖 | 版本 | 用途 |
|------|------|------|
| async-trait | 0.1.92 | 异步 trait 支持 |
| sse-stream | 0.2.6 | Server-Sent Events 流 |
| tokio-stream | 0.1.19 | Tokio 流工具 |

rmcp 自身新增依赖: base64, rand, tower-service 等（已包含在 rmcp feature 中）

### 2. 协议版本与 plan.md 一致

rmcp 3.11.0 协议版本上限 2025-11-25，与 plan.md §六 描述一致：
> "rmcp 3.11 协议版本上限 2025-11-25（legacy era）"

### 3. 挂载边界清晰

- 使用 `axum::Router::nest_service()` 挂载
- StreamableHttpService 实现 `tower_service::Service<Request<RequestBody>>`
- 与 axum 0.8.9 / tower 0.5.3 完全兼容
- 挂载点: `/mcp`，需在 `authenticate()` middleware 之后

### 4. 主仓库工作树保持干净

所有实验均在 `.worktrees/T044-rmcp-preflight` 内完成:
- 主仓库 Cargo.toml: 未修改
- 主仓库 Cargo.lock: 未修改
- 主仓库 src/: 未修改
- 主仓库 tests/: 未修改

---

## 决策建议

### T056 保留评估

**建议**: ✅ **值得保留 T056**

**理由**:
1. ✅ 编译预检通过，无技术障碍
2. ✅ 新增依赖可控（3 个直接依赖）
3. ✅ 挂载边界清晰，与现有栈兼容
4. ✅ 工作量预估 ≤ 1 天（在 T050 后）
5. ✅ 为"不支持 stdio 的自研 agent"提供 HTTP 直连能力

**实施条件**（来自 tasks.md T056）:
- ✅ 本预检 ≤4h 通过
- ⏳ 入口门槛＝已命名 HTTP-only 真实客户端（待用户确认）

**实施要点**:
1. `/mcp` 挂载位于 `authenticate()` middleware 之后
2. 每请求重鉴权，禁缓存会话授权
3. session 模式记录 `(client_id, credential_version)`
4. 仅 bind 127.0.0.1
5. 文档明写"非规范的本机自建鉴权、不实现 RFC 9728、不透传任何令牌"

### 退路评估

- **退路 A**（独立 hyper 监听）: 不再需要
- **退路 B**（维持 stdio + T053 条件路由）: 可选备选

---

## 交付工件

### 文档

1. **PREFLIGHT_REPORT.md** — 完整预检报告（含技术细节、证据、复查指引）
2. **rmcp_spike.json** — 结构化预检结果（供 run_gates.py 消费）
3. **T044-D_SUMMARY.md** — 本执行摘要

### 日志

- `/tmp/t044_build.log` — 编译日志
- `/tmp/t044_fetch.log` — 依赖拉取日志
- worktree 内 `git diff src/v2/bmem/Cargo.lock` — Cargo.lock 变化

### 路径

- **worktree**: `.worktrees/T044-rmcp-preflight`
- **target**: `.worktrees/T044-rmcp-preflight/target-preflight`
- **二进制**: `.worktrees/T044-rmcp-preflight/target-preflight/debug/bmem`

---

## 复查指引

主代理复查时可：

1. **查看 worktree**:
   ```bash
   cd .worktrees/T044-rmcp-preflight
   ls -la
   ```

2. **查看 Cargo.lock diff**:
   ```bash
   cd .worktrees/T044-rmcp-preflight
   git diff src/v2/bmem/Cargo.lock
   ```

3. **查看编译日志**:
   ```bash
   cat /tmp/t044_build.log
   ```

4. **复跑编译**:
   ```bash
   cd .worktrees/T044-rmcp-preflight/src/v2/bmem
   CARGO_TARGET_DIR=../../target-preflight cargo build --locked --jobs 2
   ```

5. **查看 rmcp 源码**:
   ```bash
   cd ~/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/rmcp-3.11.0/
   ```

---

## 约束遵守

- ✅ 只允许新建/修改本包指定的文件
- ✅ 未修改 src/v2/bmem/**（仅修改 worktree 内的 Cargo.toml/Cargo.lock）
- ✅ 未修改 src/v2/web/**
- ✅ 未修改其他 tests/v2/**
- ✅ 未 git commit/add/push（仅 git worktree add）
- ✅ 未触碰真实 ~/.bmem
- ✅ 未触碰本机常驻服务端口
- ✅ CARGO_TARGET_DIR 隔离（target-preflight）
- ✅ --jobs 2 遵守并发纪律
- ✅ 时间盒 ≤4h（实际 ~1h）
- ✅ 主仓库工作树保持干净

---

## 结论

**预检结论**: ✅ **compiles**

**T056 建议**: ✅ **保留**（值得实施，无技术障碍）

**工作量**: ≤ 1 天（在 T050 工具表完成后）

**风险**: 低（依赖稳定、编译通过、挂载模式验证）

**下一步**: 等待用户确认 T056 入口门槛（已命名 HTTP-only 真实客户端）

---

**执行者**: ASM  
**日期**: 2026-11-24  
**耗时**: ~1 小时（含网络下载与编译）
