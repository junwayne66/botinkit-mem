# Implementation Plan — 一期个人记忆中心

**Date** 2026-11-12 | **Spec** [spec.md](spec.md)

## Summary

Python 本地服务、SQLite 权威库、原生 JS 管理台。先冻结公开契约，再以三个互不重叠的 DSH 工作包并行实现存储、服务入口、接入适配器。下一波实现模型运行时与真实 UI。Codex 通过公开 API、CLI、MCP、Harness 回调和浏览器功能验收，Claude 独立审查后收敛。

## Technical Context

- Python 3.12，uv 管理隔离环境与锁定依赖。
- FastAPI、uvicorn、Pydantic、httpx、MCP Python SDK 1.x、sqlite-vec 固定版本、zstandard。
- SQLite WAL 与短写事务，单 Worker。模型调用使用 Ollama HTTP API，蒸馏固定为 qwen3:4b-instruct-2507-q4_K_M，bge-m3 1024 维。
- 普通用户对话和 Agent 推断先用同一本地 4B 模型判断是否存在可保存陈述，再整合候选。Agent 判断还读取该会话在消息发生前的最近用户输入与实际注入版本，借此区分复述与独立观察。分类限制 64 个输出 tokens，候选限制 800 个。声明、结构化验证、外部输入、每日整理与定向重算直接执行候选整合。
- pytest 功能用例，真实服务进程、MCP client 与本地 Harness 回调，Chrome 浏览器检查。
- macOS 为本轮实测平台，Linux systemd 代码静态审查。个人电脑单用户，localhost:8790。
- 关键词召回记录 P95，起步参考 200ms。语义实测单列耗时，避免在模型未加载时承诺固定延迟。

## Constitution Check

设计前与设计后均检查通过。统一个人记忆、来源可追溯、模型事务外、T4 隔离、private 限制本地、入口脱敏、显式版本和遗忘、可重建索引均映射至契约。开发阶段接入与服务注册在隔离配置根实测。2026-11-13 用户进一步授权清理本项目测试记忆、导入本机三个 Harness 历史，并在真实用户配置中启用自动读取和沉淀。部署阶段保留配置备份，原生历史留存，真实数据与脱敏统计保存在用户数据目录。

## Project Structure

```text
src/bmem/
  __init__.py
  models.py           # 入口 DTO 与领域错误
  store.py            # 权威事务、生命周期、FTS、注入、备份
  schema.sql
  api.py              # HTTP、MCP、静态文件、Worker 生命周期
  cli.py              # bmem 命令
  adapters.py         # 配置接入、hook、历史扫描
  runtime.py          # Ollama 安装/健康/模型/向量/Worker
  static/             # 从现有概念稿与 CSS 适配真实 UI
pyproject.toml
uv.lock
tests/test_acceptance.py
tests/test_interfaces.py
tests/test_runtime.py
specs/001-personal-memory/
  spec.md
  plan.md
  research.md
  data-model.md
  contracts/interfaces.md
  quickstart.md
  tasks.md
  validation.md
```

单 Python 包减少打包和入口复杂度。现有 docs 保留设计来源，运行文件随 wheel 分发。配置由 BMEM_HOME 指定数据根、BMEM_CONFIG_ROOT 指定隔离 Harness 根。默认分别是 ~/.botinkit-mem 和用户 home。

## Milestones and Acceptance

| 里程碑 | 交付 | 原黄金用例 |
|---|---|---|
| M1 | 存储、bmemd、CLI/MCP、两个自动接入、init 与 provisioning | 1–4、16 |
| M2 | 三种历史清单、幂等补扫、注入台账和回灌识别 | 6–9 |
| M3 | 仲裁、独立证据、CAS、派生传播、遗忘及恢复 | 10–14 |
| M4 | 真实 bge-m3/sqlite-vec、语义开关、七页 UI 与运行时进度 | 5、15、UI 1–10 |

## Execution Ownership

Codex 固定契约与验收。DSH 工作包 A 拥有 models/store/schema，B 拥有 api/cli，C 拥有 adapters，D 拥有 runtime，E 拥有 static。每包只读必要规格并仅写其文件，不重复全库阅读。A 的模型选 deepseek-v4.1-flash-economy，B/C/D/E 起步同型号或 qwen3.8-flash，按实测可用性选择。Claude 用 deepseek-v4.1-flash medium 只读分组审查。执行日志放本机临时目录，项目只保留脱敏结果与模型用量。

## Complexity Tracking

数据量和并发以个人电脑实际负载为界。复用标准 SQLite backup 与官方 MCP SDK，不增加通用插件框架。明确纠正目标和事实键用于离线即时一致性，自由文本由后台本地模型补充语义关联。
