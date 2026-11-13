# 一期 v0.1 验收记录

2026-11-13，覆盖单机 M1–M4。公开规格、实现计划与任务分别见 [spec.md](spec.md)、[plan.md](plan.md) 和 [tasks.md](tasks.md)。

## 自动化验证

在 Python 3.12 与 macOS 上执行。依赖版本由根目录 `uv.lock` 固定。

```bash
uv sync --locked --extra dev
uv run pytest tests -q
uv run python -m compileall -q src/bmem
node --check src/bmem/dsh_plugin.mjs
node --check src/bmem/static/app.js
uv build
```

目录整理后完整回归为 92 passed，耗时 22.05 秒，附带一条上游 Starlette/TestClient 的弃用提示。Python 编译、两份 JS 语法检查通过。

| 测试文件 | 主要覆盖 |
|---|---|
| `tests/test_acceptance.py` | 事件幂等、CJK 检索、条件与预算、CAS 纠正、派生传播、有效来源、独立证据、观察期、降权、遗忘与备份恢复 |
| `tests/test_interfaces.py` | 真实 daemon/CLI 子进程、Streamable HTTP MCP 七工具、私密响应、配置备份、三类历史格式、增量补扫与模块入口 |
| `tests/test_runtime.py` | Ollama 运行时、模型 tag/digest、固定向量空间、候选与修订快照绑定、模型降级与有界重试 |

自动化用例使用临时数据和配置目录。模型 HTTP 协议回归使用受控端点，真实本地推理另行验证。

## 已完成的集成验收

| 入口 | 实测范围 |
|---|---|
| 跨 Harness | Claude Code 声明、DSH 自动读取、DSH 纠正、Claude Code 自动读取新值，保存来源与修订记录 |
| Codex | 原生 hooks 经宿主持久信任后自动召回与采集，CLI 和 Desktop 内置入口验证通过 |
| MCP | 七工具可用，默认 cloud 目标隐藏 private 正文，继承私密级的纠正响应保持隐藏 |
| 历史导入 | JSONL、Zstd 与 Markdown，重复导入幂等，流式处理大文件，游标、失败原因及跳过原因可查 |
| Markdown 元数据 | private 标签继承，superseded/expired 条目跳过，active、默认及未知状态保留兼容 |
| 本地推理 | bge-m3 返回 1024 维向量，固定 `qwen3:4b-instruct-2507-q4_K_M` 完成候选整合与每日整理 |
| 模型安装 | 官方 manifest/blob 校验、断点续传、运行中缺失文件修复与安装状态查询 |
| macOS 服务 | 用户级 launchd 注册、登录启动、bmemd 退出恢复，以及重启后增量补扫 |
| 管理台 | 七页真实 API、375px 布局、声明和版本详情、键盘焦点、减少动效、语义开关与索引回填 |

本地模型下载、三工具原生接入与浏览器验收由单独集成步骤完成。日常 pytest 执行覆盖公开边界与已定位回归。

## 检索评测口径

五组中文改写在同一组八条测试记忆上验证。关键词和混合检索按正文及直接来源的 Top-3 命中均为 5/5。仅按原 episode ID 计算时，关键词为 3/5，混合为 4/5。部分查询返回具有相同正文、条件和直接来源的每日整理记录，原 episode 排名较后。

以上为小样本验收。性能随硬件、模型热态及数据规模变化，完整真实历史的语义准确率仍需持续使用评估。

## 审查与执行边界

DSH 执行开发，Claude Code 通过既有第三方模型进行独立静态审查，Codex 核对意见并执行功能测试。覆盖存储、DTO、服务、适配器、运行时、CLI、管理台与最终修复。审查意见依据实际实现和回归结果采纳。

macOS 已进行真实用户级部署。Linux systemd 用户服务有实现及路径生成检查，Linux 实机执行待验证。多电脑共享属于第二期设计范围。

个人历史、部署数据、原生配置、模型文件、执行器日志及测试数据库保存在本地数据或临时目录。公开仓库保留源码、规格、测试与本验收摘要。

## 仓库结构与分发验收

源码采用 `src/bmem`，测试采用 `tests`，包内 schema、DSH 插件和三份管理台资源随 wheel 分发。迁移时原有业务源码与测试逐字节保持一致，打包入口和测试发现路径已同步更新。

wheel 已在项目目录外的全新 Python 环境安装。命令入口、模块入口、隔离初始化、声明与检索、备份恢复后检索通过，五份包内资源与源码逐字节一致。源码包检查未包含缓存、本机记忆数据或第三方参考截图。隔离验收数据在检查后清理。

Claude Code 使用第三方模型完成一轮工具关闭的结构及 README 审查。根据审查澄清工具安装与源码运行入口、两个 init 开关的独立行为，补充本地 wheel 安装命令。目录路径、相对链接和发布文件另经本地检查。

公开版验收记录保留可复现命令及验证边界，个人部署证据留在本地。第三方 UI 调研保留原公开来源链接，参考图片原文件留在本机。
