# bmem 架构设计

## 升级功能设计

[Botinkit Memory 升级功能设计](PRD-memory-upgrade.md)按日常使用场景定义新特性、首版范围和验收条件。设计依据见[长期记忆系统再调研](landscape-2026-11-26/README.md)。

## V2 架构与研究分册

[产品架构总纲](reassessment-2026-11/architecture.md)定义重建范围、模块职责、数据流转和一期集成顺序。

下列分册保留冻结研究原文。文中引用的 Python 源码、测试和 001–003 规格属于 V1，可从仓库根用 `git show ae2c06ba7cd8921846ffd85ffac6554488026517:<仓库相对路径>` 查阅。当前实现入口见 [V2 文档](../docs/v2/README.md)。

| 分册 | 内容 |
|---|---|
| [模块与开源接入](reassessment-2026-11/modules.md) | 18 个模块的输入输出、组件复用与替换验证 |
| [记忆知识图与检索](reassessment-2026-11/knowledge-graph.md) | 实体、事实、关系、社区与检索路径 |
| [记忆生命周期](reassessment-2026-11/memory-lifecycle.md) | 自动提炼、冲突替代、全局声明与跨助手加载 |
| [反馈、数据集与模型改进](reassessment-2026-11/feedback-learning.md) | 反馈归因、训练与评测数据、发布和回滚 |
| [评测方案](reassessment-2026-11/evaluation.md) · [本地验证与复现](reassessment-2026-11/local-evidence.md) | 设计判据、检索基线、撤回与备份复验。实验脚本和结果随复现说明保留 |
