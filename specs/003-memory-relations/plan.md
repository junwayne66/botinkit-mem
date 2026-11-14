# Implementation Plan · 持续更新的事实与真实记忆星图

**Branch** `main` | **Date** 2026-11-14 | **Spec** [spec.md](spec.md)

## Summary

先交付真实关系投影与详情这一独立切片，交独立 review 后再实施事实时间策略。沿用单机权威库、单 Worker、原生界面和现有模型，关系视图从已持久化记录重建。

## Technical Context

- Python 3.12、FastAPI、SQLite WAL、原生 JavaScript/SVG。
- 测试复用 pytest、真实 Store 和隔离 HTTP 入口。浏览器验证使用隔离样本。
- 以原有设计令牌和抽屉呈现节点、关系和事件证据。
- 调度、频率和并发保持用户指定的测量配置。独立采样由指定观察方负责。

## Constitution Check

五项原则均满足。来源与修订继续作为权威记录，事件和任务保持事务一致。私密过滤覆盖图谱端点及事件详情。小步测试后独立 review，再本地提交和部署。

## Project Structure

- `src/bmem/store.py` 关系投影、依赖闭包、事实更新策略。
- `src/bmem/models.py`、`schema.sql`、`runtime.py` 事实时间表达及兼容迁移。
- `src/bmem/api.py` 本机只读关系和事件入口。
- `src/bmem/static/app.js`、`app.css`、`index.html` 原界面的交互扩展。
- `tests/test_graph.py`、`tests/test_recompute.py`、后续时间回归文件。

## Delivery Slices

1. 真实派生、共享来源节点、最近版本替代及证据详情。筛选后优先选取关联端点，重新进入重新读取。
2. 声明和模型候选的独立事实时间、同范围的新旧决策、旧数据和备份兼容。
3. 运行分类报告、必要回归、独立评审与本地发布。吞吐部分本轮仅评估。

## Complexity Tracking

新增代码复用既有存储和界面，不增加服务、模型或关系权威库。共享来源节点只投影真实共同来源，历史关系标明失效状态。
