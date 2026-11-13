# Research and Decisions

## 现有资产

已审阅根 README、一期、二期、UI、治理正文及两份研究笔记，UI HTML/CSS、图形源、导出脚本与质量回执。资产审计覆盖全部初始文件类别。现有 UI 为 18 条示例数据的原生 JS 演示，接入真实数据需替换所有假安装、假声明与静态统计。

## 服务与协议

采用 FastAPI 与官方 MCP Python SDK 的 Streamable HTTP，在同一 ASGI 进程提供 /api 与 /mcp。SDK 主版本固定 1.x，并以安装后的签名验证挂载及 lifespan。现有原生 UI 适合本地管理台，直接由服务分发。[官方 MCP SDK](https://github.com/modelcontextprotocol/python-sdk)

## 检索与一致性

采用 SQLite FTS5，索引和查询均生成 CJK bigram，保留拉丁标识符。向量层采用固定版本 sqlite-vec，独立派生索引记录 memory_id/revision_no/embedding_space，返回前复核权威状态。备份使用 SQLite online backup，包含 evidence 与权威台账，恢复后重放现存 tombstones。[SQLite FTS5](https://www.sqlite.org/fts5.html) [SQLite backup](https://www.sqlite.org/backup.html) [sqlite-vec Python 示例](https://github.com/asg017/sqlite-vec/blob/main/examples/simple-python/demo.py)

## 模型与运行时

通过 Ollama /api/generate、/api/embed、/api/pull 接口实现本地蒸馏、1024维 bge-m3 和真实安装进度。按机器系统下载已锁定的官方发行物，复用已存在 Ollama 时检查版本和模型，安装失败保留可重试状态。运行时 lockfile 记录发行版及模型 digest。[Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)

功能验收时发现通用 `qwen3:4b` 标签的 manifest `359d7dd4bcda` 指向专用思考版，关闭思考会误判问题和有效经历。蒸馏固定为 `qwen3:4b-instruct-2507-q4_K_M`，4B、约 2.5GB，manifest 固定为 `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`。下载验证涵盖 manifest 和全部 blob 的大小及 SHA-256。[Ollama 标签](https://registry.ollama.com/library/qwen3/tags) [Qwen 官方指令版说明](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)

真实正反例暴露了单次候选生成的两类错误，提问被当作已有事实的印证，有效经历又可能被略过。普通对话和推断先做陈述分类，再结合已有记忆生成候选。助手判断还读取该会话在消息发生前的最近用户输入与实际注入的确切修订，正文仅用于本次本地模型调用，以便识别用户声明确认和改写复述。分类使用平衡示例及 64 个输出 tokens，候选使用 800 个，调用均在 SQLite 写事务之外。独立新观察的父来源为空，实际派生关系由事件中的推导依据确定。验证结果记录在 `validation.md`。

## 规格消歧

16 项黄金用例按实际依赖分布至 M1–M4。T2 仅指结构化验证事件，普通工具文本为 T4。T3 阈值采用治理正文。T1 以明确事实键/目标支持离线版本更新，其他自由声明先独立落盘再关联。遗忘清除正文和证据，保留无正文 tombstone。首次安装和 repair 复用锁定版本，update 显式更新。

## 本机工具检查

本机 Spec Kit 0.16.5。ai-project-init 未找到，使用已安装 spec-kit-project 的 ensure_spec_kit.sh 初始化现行模板。OCR 委托技能未找到，本轮按用户明确要求直接用 Claude 独立审查。第三方 provider 通过临时进程环境使用已有凭据，模型选择和用量进入验证记录，凭据不进入项目。

## 本机模型下载兼容性实测

Ollama 0.40.16 的官方运行时已在隔离目录完成 SHA-256 校验和启动。模型下载遇到系统代理 fake-IP DNS 与重定向校验冲突。通过现有系统 HTTPS 代理访问官方 registry 可行，采用官方仓库记录的受控恢复路径，逐层下载并校验 SHA-256、校验固定 manifest digest，再交由本地 Ollama 使用。该发现来自真实安装，相关实现与验收仍在进行。[Ollama issue 18716](https://github.com/ollama/ollama/issues/18716) [Ollama issue 19086](https://github.com/ollama/ollama/issues/19086)
