# Hindsight 两路检索融合实跑

同一条证据在向量路排第二、词法路排第一时，上游 RRF 和按公式独立计算的分数都是 **0.032522475**，高于单路首位的 **0.016393443**。另一组样本中，两条记录完全同分，上游先给首次遇见的记录。若阅读预算只剩一个位置，调整两路的遍历顺序就可能换掉交付内容。bmem 拟用稳定的证据引用打破同分。[固定版融合函数](https://github.com/vectorize-io/hindsight/blob/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/fusion.py#L26-L98) · [本地技术设计](README.md)

## 样本与实际结果

每一行独立执行。输入按相关性从高到低排列，A、Z 等为合成片段句柄。使用上游默认 k=60，名次从 1 开始。表中的提议顺序由下方独立的 Python 公式实现给出。

| 情形 | 向量路 / 词法路 | Hindsight 输出 | bmem 提议顺序 |
|---|---|---|---|
| 两路命中相同片段 | S, X / X, L | X, S, L | X, S, L |
| 向量路为空 | 空 / L, M | L, M | L, M |
| 两个首位恰好同分 | Z / A | Z, A | A, Z |
| 交换上述两个句柄的通道 | A / Z | A, Z | A, Z |

**适配边界。** Hindsight 用各通道的位置标记 semantic 或 bm25，保留第一份命中对象，并分别记录源排名。本例 X 的两个排名为 2 和 1。bmem 应以自己的通道名传递排名，用事件版本与片段的稳定引用合并，同一引用在同一路重复时只保留最早出现的一项，重新连续编号后再算分。精确错误码的预留名额、读者权限和回到原文取证在融合函数外处理。[候选与展开契约](contracts.md) · [组件选型](reuse.md)

## 一键重放

在任意目录粘贴下方命令。需要 Python 3.10+；命令用 python3.11，可换为符合版本要求的 python3。仅向 GitHub 下载两个定版源文件，逐个核对 SHA-256，导入临时包中的原版函数与数据类。已经备好这两个原文件时，可把环境变量 HINDSIGHT_RRF_DIR 指向它们所在的目录，整段可断网运行。[融合源码](https://raw.githubusercontent.com/vectorize-io/hindsight/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/fusion.py) · [数据类源码](https://raw.githubusercontent.com/vectorize-io/hindsight/0aca9f80ec87237756c445ac27bf9e5affdc67fe/hindsight-api-slim/hindsight_api/engine/search/types.py)

~~~sh
python3.11 -I - <<'PY'
import hashlib, math, os, sys, tempfile
from fractions import Fraction
from pathlib import Path
from urllib.request import urlopen

rev = "0aca9f80ec87237756c445ac27bf9e5affdc67fe"
base = f"https://raw.githubusercontent.com/vectorize-io/hindsight/{rev}/hindsight-api-slim/hindsight_api/engine/search/"
digests = {
    "fusion.py": "19ccdfd73e5c9da7c10561c92e4c86af2d6c7b6fed870f41a848fbce1d25dfa4",
    "types.py":  "2e84c9ec598c9f7c473df7f1ac14d5e261498641153bf1015810495aadf99162",
}

def reference(arms):
    scores = {}
    for arm in arms:
        for rank, key in enumerate(dict.fromkeys(arm), 1):
            scores[key] = scores.get(key, 0) + Fraction(1, 60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key)), scores

cases = [
    ("overlap", ["S", "X"], ["X", "L"], "XSL", "XSL"),
    ("empty",   [],         ["L", "M"], "LM",  "LM"),
    ("tie",     ["Z"],      ["A"],      "ZA",  "AZ"),
    ("swapped", ["A"],      ["Z"],      "AZ",  "AZ"),
]
with tempfile.TemporaryDirectory() as root:
    package = Path(root, "rrf_fixture")
    package.mkdir()
    (package / "__init__.py").touch()
    source = os.getenv("HINDSIGHT_RRF_DIR")
    for name, digest in digests.items():
        if source:
            data = (Path(source) / name).read_bytes()
        else:
            with urlopen(base + name, timeout=20) as response:
                data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f"SHA-256 mismatch: {name}")
        (package / name).write_bytes(data)
    sys.path.insert(0, root)
    from rrf_fixture.fusion import reciprocal_rank_fusion
    from rrf_fixture.types import RetrievalResult

    for name, semantic, lexical, want_up, want_local in cases:
        arms = [semantic, lexical]
        rows = reciprocal_rank_fusion([[RetrievalResult(key, "", "fact") for key in arm] for arm in arms])
        upstream = [row.id for row in rows]
        local, scores = reference(arms)
        assert upstream == list(want_up) and local == list(want_local)
        for row in rows:
            assert math.isclose(row.rrf_score, scores[row.id], rel_tol=0, abs_tol=1e-15)
        if name == "overlap":
            assert rows[0].source_ranks == {"semantic_rank": 2, "bm25_rank": 1}
        values = " ".join(f"{row.id}={row.rrf_score:.9f}" for row in rows)
        print(f"{name:7} upstream={''.join(upstream)} local={''.join(local)}  {values}")
PY
~~~

实跑环境为 CPython 3.11.15。四组顺序、逐条分数及 X 的通道排名断言均通过。

~~~text
overlap upstream=XSL local=XSL  X=0.032522475 S=0.016393443 L=0.016129032
empty   upstream=LM local=LM  L=0.016393443 M=0.016129032
tie     upstream=ZA local=AZ  Z=0.016393443 A=0.016393443
swapped upstream=AZ local=AZ  A=0.016393443 Z=0.016393443
~~~

本次演练只验证候选融合的分数、通道排名和同分顺序。真实任务能否在固定阅读预算内找全必要证据，以及最终装入是否遵守授权，仍需通过 Rust 服务与实际宿主验证。
