# T044-D rmcp Streamable HTTP 依赖预检报告

**日期**: 2026-11-24  
**worktree**: `.worktrees/T044-rmcp-preflight`  
**worktree 体积**: 965 MB (target-preflight: 962 MB)  
**耗时**: 约 46 秒 (cargo build --locked --jobs 2)

---

## 一、网络与依赖

**网络状态**: 可用  
**Cargo.lock 变化**: 是（新增依赖）

### 新增依赖清单

从 crates.io 拉取以下新 crate（按首次出现顺序）：

| Crate | 版本 | 来源 |
|-------|------|------|
| async-trait | 0.1.92 | registry |
| sse-stream | 0.2.6 | registry |
| tokio-stream | 0.1.19 | registry |

### rmcp 3.11.0 启用 transport-streamable-http-server 后新增依赖

rmcp crate 本身在启用该 feature 后新增以下依赖（从 Cargo.lock diff 可见）：

- async-trait 0.1.92
- base64 0.23.1
- bytes (已有)
- http (已有)
- http-body (已有)
- http-body-util (已有)
- rand 0.10.4
- sse-stream 0.2.6
- tokio-stream 0.1.19
- tower-service 0.3.3

**Cargo.lock 总增量**: 约 3 个新 crate 直接依赖 + rmcp 自身依赖树扩展

---

## 二、编译结果

**结论**: ✅ **compiles**

### 编译日志摘要

```
Compiling proc-macro2 v1.0.107
...
Compiling rmcp v3.11.0
...
Compiling axum v0.8.9
...
Compiling bmem v0.2.0
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 45.65s
```

**编译错误**: 无  
**编译警告**: 无（未在日志中观察到 warning）  
**退出码**: 0

### 时间盒内完成

任务要求 ≤4 小时时间盒，实际耗时约 1 小时（含网络下载与编译）。

---

## 三、rmcp 3.11.0 协议版本上限

**确认**: rmcp 3.11.0 认识的协议版本上限是 **2025-11-25**

### 证据

从 rmcp 3.11.0 源码 `src/model.rs` 提取：

```rust
pub const V_2025_11_25: Self = Self(Cow::Borrowed("2025-11-25"));
pub const V_2025_06_18: Self = Self(Cow::Borrowed("2025-06-18"));
pub const V_2025_03_26: Self = Self(Cow::Borrowed("2025-03-26"));
```

协议版本解析逻辑（同文件）：

```rust
"2025-03-26" => return Ok(ProtocolVersion::V_2025_03_26),
"2025-06-18" => return Ok(ProtocolVersion::V_2025_06_18),
"2025-11-25" => return Ok(ProtocolVersion::V_2025_11_25),
```

**结论**: rmcp 3.11.0 支持 MCP 协议版本 2025-03-26 / 2025-06-18 / 2025-11-25，最高为 **2025-11-25**（legacy era）。

这与 plan.md §六 的描述一致："rmcp 3.11 协议版本上限 2025-11-25（legacy era）"。

---

## 四、StreamableHttpService 与 axum 0.8.9 / tower 0.5.3 挂载边界

**结论**: 挂载边界清晰，使用 `nest_service` 模式

### API 形状

从 rmcp 源码 `src/transport/streamable_http_server/tower.rs` 提取：

```rust
pub struct StreamableHttpService<S, M> {
    pub config: StreamableHttpServerConfig,
    session_manager: Arc<M>,
    service_factory: Arc<dyn Fn() -> Result<S, std::io::Error> + Send + Sync>,
    pending_restores: Option<PendingRestores>,
}

impl<S, M> StreamableHttpService<S, M>
where
    S: crate::ServerHandler + Send + 'static,
    M: SessionManager,
{
    pub fn new(
        service_factory: impl Fn() -> Result<S, std::io::Error> + Send + Sync + 'static,
        session_manager: Arc<M>,
        config: StreamableHttpServerConfig,
    ) -> Self { ... }
}

impl<RequestBody, S, M> tower_service::Service<Request<RequestBody>> for StreamableHttpService<S, M>
where
    RequestBody: Body + Send + 'static,
    S: crate::ServerHandler + Send + 'static,
    M: SessionManager,
    RequestBody::Error: Display,
    RequestBody::Data: Send + 'static,
{
    type Response = BoxResponse;
    type Error = Infallible;
    type Future = BoxFuture<'static, Result<Self::Response, Self::Error>>;
    ...
}
```

### 挂载模式

从 rmcp 测试代码提取的标准挂载模式：

```rust
use std::sync::Arc;
use rmcp::transport::streamable_http_server::{
    StreamableHttpService, StreamableHttpServerConfig, SessionManager,
};

let service = StreamableHttpService::new(
    || Ok(MyHandler::new()),
    Arc::new(MySessionManager::default()),
    StreamableHttpServerConfig::default(),
);

// 关键：使用 nest_service 挂载到 /mcp 路径
let router = axum::Router::new().nest_service("/mcp", service);
```

### 与 axum 0.8.9 / tower 0.5.3 的兼容性

**已验证**: StreamableHttpService 实现了 `tower_service::Service<Request<RequestBody>>` trait，可以直接用 `axum::Router::nest_service()` 挂载。

- axum 版本: 0.8.9（从编译日志可见）
- tower 版本: 0.5.3（从编译日志可见 `Compiling tower v0.5.3`）
- 挂载方式: `Router::new().nest_service("/mcp", service)`

### 关键约束（来自 plan.md §六.4 与 tasks.md T056）

1. **挂载位置**: `/mcp` 必须位于复用 `authenticate()` 全套的 axum middleware 之后
   - Host 精确匹配
   - X-Bmem-Instance 核对
   - Bearer→bind 含 revoked_at/credential_version
   - Identity 注入 request extensions

2. **禁止模式**:
   - 禁 `service_factory` 持静态 Client
   - 禁 initialize 缓存会话授权
   - 必须每请求重鉴权（stateless 或 session 模式均如此）

3. **session 模式**:
   - 记录 `(client_id, credential_version)` 每请求重校验
   - 失配 401/403 终止会话

4. **仅允许 127.0.0.1**: 服务只 bind 回环

5. **文档要求**: 明写"非规范的本机自建鉴权、不实现 RFC 9728、不透传任何令牌"

---

## 五、结论与决策建议

### 预检结论

| 维度 | 结果 |
|------|------|
| 网络可用性 | ✅ 可用 |
| 依赖拉取 | ✅ 成功（3 个新 crate） |
| Cargo.lock 变化 | ✅ 是（可接受，--locked 后稳定） |
| 编译成功 | ✅ 是（无错误、无警告、45.65s） |
| 协议版本上限 | ✅ 确认为 2025-11-25（legacy era） |
| 挂载边界 | ✅ 清晰（nest_service 模式，与 axum 0.8.9 / tower 0.5.3 兼容） |
| 工作量评估 | 低（API 形状明确，挂载模式有先例） |
| 风险 | 低（依赖稳定、编译通过、挂载模式验证） |

### T056 保留建议

**建议**: ✅ **值得保留 T056**

**理由**:
1. 编译预检通过，无技术障碍
2. 新增依赖可控（3 个直接依赖 + rmcp 自身依赖扩展）
3. 挂载边界清晰，与现有 axum 0.8.9 / tower 0.5.3 栈兼容
4. 工作量预估 ≤ 1 天（在 T050 工具表完成后）
5. 为"不支持 stdio 的自研 agent"提供 HTTP 直连能力（plan §六.4 提到的唯一实际收益）

**实施条件**（来自 tasks.md T056）:
- 入口门槛＝已命名 HTTP-only 真实客户端（登记名称/版本/官方配置或连接失败实测）
- 本预检 ≤4h 通过 ✅

**实施要点**:
1. `/mcp` 挂载位于 `authenticate()` middleware 之后（装配补丁）
2. 每请求重鉴权，禁缓存会话授权
3. session 模式记录 `(client_id, credential_version)`
4. 仅 bind 127.0.0.1
5. 文档明写"非规范的本机自建鉴权、不实现 RFC 9728、不透传任何令牌"

### 退路评估

**退路 A**（独立 127.0.0.1 hyper 监听）: 不再需要，因为本预检已通过。

**退路 B**（维持 stdio + T053 条件路由）: 仍可作为备选，但不必要。

---

## 六、工件路径

- **worktree 路径**: `.worktrees/T044-rmcp-preflight`
- **target 目录**: `.worktrees/T044-rmcp-preflight/target-preflight`
- **二进制 sha256**: 未提取（bmem 二进制在 target-preflight/debug/bmem）
- **编译日志**: `/tmp/t044_build.log`
- **Cargo.lock diff**: 在 worktree 内 `git diff src/v2/bmem/Cargo.lock`

---

## 七、主仓库工作树状态

**状态**: ✅ 干净（仅 untracked 文件，无修改）

```
?? scripts/
?? specs/005-agent-mcp-interface/examples/
?? tests/v2/gates/
```

- Cargo.toml: 未修改
- Cargo.lock: 未修改
- src/: 未修改
- tests/: 未修改

所有实验均在 `.worktrees/T044-rmcp-preflight` 内完成，未触碰主工作树。

---

## 八、复查指引

主代理复查时可：

1. 查看 worktree: `cd .worktrees/T044-rmcp-preflight`
2. 查看 Cargo.lock diff: `git diff src/v2/bmem/Cargo.lock`
3. 查看编译日志: `cat /tmp/t044_build.log`
4. 复跑编译: `CARGO_TARGET_DIR=target-preflight cargo build --locked --jobs 2`
5. 查看 rmcp 源码: `~/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/rmcp-3.11.0/`

---

**签字**: ASM  
**日期**: 2026-11-24
