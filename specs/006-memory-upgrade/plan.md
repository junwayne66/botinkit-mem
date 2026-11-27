# Implementation Plan: Botinkit Memory 升级

## Summary

先处理可直接复现的声明、长任务交接和无答案。按故事完成剩余契约与原样本验收。

## Technical Context

现有 Rust 2024、SQLite WAL/FTS5、可选向量、axum、原生宿主 hook 和原生 Web。测试使用临时库和回环模型。具名时区实施时验证 chrono-tz。

## Constitution Check

SQLite 负责权威事务。提炼和向量网络调用置于写事务外。授权先于截断，撤回重验旧句柄。Codex 负责集成和验收，Claude Code 在隔离摘录上负责解析切片与审查。

## Project Structure

| 范围 | 代码位置与依赖 |
|---|---|
| 规格与验收 | 本目录的 spec、plan、tasks 以及 research/memory-upgrade-technical/tests |
| 声明 | `authority.rs` 确定性裁定分句、项目时区、时效与覆盖。`clock.rs` 为隔离评测固定收件时点。CLI 和 Web 使用同一权威查询 |
| 接手 | `retrieval.rs` 保留必要证据。`context.rs` 实现全量、差量、留存摘要与回执，`main.rs` 和 `host.rs` 绑定真实宿主 |
| 导入 | `history_tasks.rs`、`ingestion.rs` 联接页、已提交版本、去向与来源关系。归档页与持续采集使用独立检查点 |
| 检索 | `retrieval.rs` 融合精确、词法和语义候选，并按任务、来源、运行 ID 和调用 ID 补读。`server.rs` 处理模型路由和查询超时 |
| 治理 | `retrieval.rs` 执行根级权限和冷热操作，`archive.rs` 校验备份，`derived.rs` 对联合必要与替代支持作失效检查和显式重算 |
| 方法与评测 | `methods.rs` 管理工件版本、前提、独立验证与反例。`optimization.rs` 管理用途、冻结数据、实际观测、策略发布与回切 |
| 扩展和兼容 | `community.rs` 提供采集游标对账、分段、图路径和稳定邻居修订。`vector.rs` 按模型指纹管理投影与切换水位。`storage_epoch.rs` 管理升级库写入能力 |

## Validation

每项保留原 case ID 和实际观测。构建隔离候选二进制，使用冻结样本与旧版比较。报告写入指定 tests 目录。
