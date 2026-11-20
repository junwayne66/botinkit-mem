# 本地实证：从“搜到会话”到“读到证据”

2026-11-19。我们用两种材料检验了不同的问题。作者公开的 LongMemEval_S cleaned 适合测长历史里的定位能力。六条自造的中文记录适合解释当前产品入口在特定措辞上的行为。以下均为本次运行结果。没有调用生成或嵌入模型。

## 1. 英文长历史，四种检索对照

**数据。** 作者清洗版本共 500 题。本实验测有答案时的来源定位，排除 30 道 ID 以 `_abs` 结尾的拒答题，留下 470 道，金标来源会话均存在。每题只在自己的历史中搜索，候选会话数中位 47，范围 38–62。逐题合计 22,419 个会话实例，跨题可能有重复。470 题中有 3 题各需要 6 个来源，前 5 个全覆盖的理论上限是 467/470。数据卡标注英语、MIT。使用作者 2025 年 9 月公告所指清洗版。[数据卡与版本](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/tree/98d7416c24c778c2fee6e6f3006e7a073259d48f) · [作者字段说明与更新记录](https://github.com/xiaowu0162/LongMemEval)

**协议。** 四组使用相同历史和同一道原问题。“最近”直接取最后五个会话。三组词法均使用同一 SQLite FTS5 tokenizer、项目查询扩词和 BM25。同分取后录入的行。①“原始会话”索引按顺序拼合的用户及助手正文，保留重复词。②“项目切词·会话”先调用实际 `_fts_tokens` 扩展、去重并截断会话词集。③“项目切词·消息”对每条消息分别切词、索引，按消息得分从高到低取前五个不同的会话，每个会话保留首次命中消息。问题、答案、金标 ID 和 `has_answer` 均未进入历史索引。查询只使用问题正文。每题使用独立内存库。[完整运行脚本](replay_longmemeval.py) · [项目切词实现](../../src/bmem/store.py)

| 方法 | 第一项是金标来源 | 前五项含至少一个金标来源 | 前五项包含全部金标来源 |
|---|---:|---:|---:|
| 最近会话 | 21/470（4.5%） | 112/470（23.8%） | 27/470（5.7%） |
| 原始会话正文 + BM25 | 408/470（86.8%） | 458/470（97.4%） | **392/470（83.4%）** |
| 项目切词 · 整段会话 | 395/470（84.0%） | 447/470（95.1%） | 374/470（79.6%） |
| 项目切词 · 单条消息，按会话去重 | 395/470（84.0%） | 444/470（94.5%） | 377/470（80.2%） |

**先看分歧。** 按会话索引改为按消息索引，找全金标会话只净增 3 题：18 题由错变对，15 题由对变错，359 题两者都找全，78 题都未找全。按题型，单条消息方案在跨会话题找全 74/121，在偏好题找全 20/30。原始会话方案分别是 77/121 和 26/30。两种粒度同时改变了词频、文档长度和切词上限的影响，无法据此将差异归因给某一因素。[逐切片及配对计数](longmemeval-results.json)

**真正影响架构的是证据粒度。** 另取每个金标会话都有显式 `has_answer=true` 消息的 **438 题**。按消息搜索时，369/438（84.2%）在前五个候选中找全金标会话。但如果要求每个金标会话送出的那条最高分消息本身也有金标，只剩 **203/438（46.3%）**。这里每个金标会话只须命中一条标注消息，未要求覆盖同一会话内全部标注。至少有一条最高分消息带金标的是 317/438。未被作者标注的邻句也可能有用，所以不能把其余题一概判成答错。这个落差支持把“找到出处”和“从出处补出充分证据”设为两个独立步骤、两项独立指标。

**可得出的结论。** 原始会话的词法基线值得保留。只调候选召回率，容易错过定位到会话后仍只送出无关消息的问题。下一项有辨别力的实验是固定候选和阅读 token，对比只发首个命中、补相邻动作、允许再展开一次。在英文对话、中文开发日志、不同阅读模型上分别评分。

**适用范围。** 本实验是自定义的会话定位试验。作者检索脚本的普通设置索引用户消息，且有证据轮筛选。这里索引双方消息，按会话做金标匹配。各组上限均为五个不同会话，词法组仅返回实际匹配项。不同方法对应的文本预算不同。实验调用项目切词函数，跳过了业务提炼、授权、向量、提示构造和最终回答，因此这些数字既非官方 QA 分，也不能当作已部署 bmem 的端到端质量。[作者检索脚本](https://github.com/xiaowu0162/LongMemEval/blob/main/src/retrieval/run_retrieval.py)

### 复现

在仓库根目录使用项目现有 Python 环境。数据保存在临时目录，脚本会在运行前校验 SHA-256。输出不含任何数据集对话原文。

```sh
dataset_dir=$(mktemp -d)
curl -fL --retry 2 -o "$dataset_dir/longmemeval_s_cleaned.json" \
  'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json'
.venv/bin/python research/reassessment-2026-11/replay_longmemeval.py \
  "$dataset_dir/longmemeval_s_cleaned.json" > "$dataset_dir/result.json"
diff -u research/reassessment-2026-11/longmemeval-results.json "$dataset_dir/result.json"
```

运行环境为 Python 3.12.13、SQLite 3.53.1，项目 HEAD 为 `ae2c06b`，读取的是当时的工作树。`src/bmem/store.py` 的 SHA-256 为 `aa455e39821b7055c4da91a0763d76b7b9dba6942391ca11b2a895f808663c03`。数据为 277,383,467 字节，SHA-256 为 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。两次完整独立进程运行的 JSON 逐字节一致，结果 SHA-256 为 `185ed9526de84d83c64e8bb6611c5ee7d20e12a948be8743cf4805d08b69d285`。

## 2. 中文工作词：现有 Store 的十次查询

在临时空目录实例化真实 `Store`，依次用 `declare` 写入下列正文，未指定事实键。逐个调用 `search(query, limit=5)`，不提供语义候选。A–F 只在报告里标记结果。该样本刻意放入共享单字、改写和无答案查询，供诊断机制。

| 标签 | 写入的完整正文 |
|---|---|
| A | 流量激增时采用令牌桶限速。 |
| B | 证书轮换采用双签名过渡。 |
| C | 异步归档由松柏服务负责。 |
| D | 缓存异常先检查雪松索引。 |
| E | 蓝鸟流水线遇到 E4721 就回退构建。 |
| F | 付款凭证归到白鹭账册。 |

| 查询 | 实际前五（从左到右） | 说明 |
|---|---|---|
| 令牌桶 / E4721 / 白鹭（分别查询） | A / E / F | 三个精确词均只返回预期项 |
| 松柏 / 雪松（分别查询） | C、D / D、C | 两字实体排在首位，共享单字“松”又带入另一项 |
| 访问压力突然变大该如何节流 | A、E | 找到限速经历，也命中不相关流水线 |
| 蜂拥请求怎样削峰 | 空 | 技术语义相关，缺少共同字词时词法未能补回 A |
| 凭据换新怎样平滑切换 | F、B | 首位是付款凭证，相关的证书轮换排第二 |
| 令牌桶报销细则 | A | 词法相似，原记录没有任何报销依据 |
| 岩浆黏度 | 空 | 无关问题正确无结果 |

**产品含义。** 中文二元切分与标识符拆分已经存在。两字专名命中、共享单字误召、相似经历漏召和“检到了却答不了”应分别计量。增加向量可能改善第三类。第四类还需要检查证据能否支持所问谓词，必要时不注入。十个手造问句只展示可能发生的行为，不能估算发生率或证明哪种 embedding 胜出。

## 3. 撤回与备份恢复

使用仓库现有虚拟环境和临时空目录，每组仅一条人工生成的唯一词，不调用模型、宿主服务或飞书。Python 3.12.13，SQLite 3.53.1，仓库 HEAD 为 `ae2c06b`。实际工作树的 `store.py` Git blob 前缀为 `ded61748`，`feishu_history.py` 为 `7621f9fe`。结果说明这条路径的行为，不能据此估计线上发生率、撤回可达率或磁盘残留。

| 实验 | 观察 | 决策含义 |
|---|---|---|
| 备份、主动遗忘与重建 | 初始精确词命中 **1**。forget 后 **0**。同一旧包恢复到留有墓碑的原库 **0**。同一旧包恢复到空目录 **1**。删除后制作的备份恢复到另一空目录 **0**。 | 旧机仍在时的恢复和整机丢失后的恢复有不同前提。若产品要在整机丢失后仍执行历史删除，需要有晚于旧包且能恢复的删除记录，或明确旧快照的恢复边界。SQLite 在线备份本身给出时间点快照。 [存储与恢复实现](../../src/bmem/store.py) · [现有原库恢复用例](../../tests/test_acceptance.py) · [SQLite Backup](https://www.sqlite.org/backup.html) |
| 已收到上游撤回记录后的本机可见性 | 首轮导入普通消息命中 **1**。次轮同 ID、空正文、`deleted=true`、较晚更新时间，两次各导入 **1**。事件同时保存 `active` 和 `deleted`，旧正文关键词仍命中 **1 个 active episode**。 | 当前路径会记录收到的撤回版本，普通本地检索仍返回旧正文。方案需明确外部资料是历史留档还是随源撤回。若选择随源退场，应按根来源覆盖旧版本、索引和后续回填，并为允许保留的审计视图单独授权。 [导入器](../../src/bmem/feishu_history.py) · [合成 API 夹具](../../tests/test_feishu_history.py) |

复验命令从仓库根运行，外部协议只使用现有合成夹具。返回值是本机检索命中数。

```bash
PYTHONPATH=src:tests .venv/bin/python - <<'PY'
import tempfile
from pathlib import Path
from bmem.store import Store
from test_feishu_history import Protocol, P2P, message, run

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    live = Store(root / "live")
    word = "BTMEMPROBETESTQY"
    mid = live.declare({"body": word, "fact_key": "restore_probe"})["memory"]["memory_id"]
    old = Path(live.backup(root / "before.tgz"))
    counts = [len(live.search(word, target="local"))]
    live.forget(mid)
    counts.append(len(live.search(word, target="local")))
    new = Path(live.backup(root / "after.tgz"))
    for store, archive in [(live, old), (Store(root / "empty1"), old),
                           (Store(root / "empty2"), new)]:
        store.restore(archive)
        counts.append(len(store.search(word, target="local")))
    print("备份恢复", counts)

    source = Store(root / "source")
    protocol = Protocol()
    marker = "SYNTHETICFILERECALLQ"
    row = message("id1", text=marker)
    archived = dict(message("id1", text=""), deleted=True, update_time="1762900001000")
    for payload in [row, archived]:
        protocol.messages[("chat", P2P)] = [[payload]]
        stats = run(source, protocol, ["private_chats"])
        hits = source.search(marker, target="local")
        print("导入数/原词命中状态", stats["imported"], [x["status"] for x in hits])
PY
```

[相关回归测试与覆盖边界](evaluation.md#对当前工作树的实测)
