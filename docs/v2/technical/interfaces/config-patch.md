# P0 设置修改契约

`config.patch` 接收所有者对 `PATCH /v2/config` 的一次修改。CLI 和 Web 先从 `GET /v2/config` 取得当前修订，再发送相同字段、`expected_revision` 及请求头 `Idempotency-Key`。服务在同一把配置写锁内校验引用、比较修订并原子替换配置。一次补丁全部成功只推进一次修订，任一字段失败都保留原版本。相同主体、动作、键和内容的重试取回原结果。描述符里的 `input_schema` 和 `output_schema` 可分别取出按 JSON Schema 2020-12 验证。[机器可读描述符](config-patch.example.json) · [命令和 Web 对照](../command-catalog.md)

| 接口字段 | CLI / Web | 写入与读回规则 |
|---|---|---|
| `profile`、`vector_search.enabled` | `bmem config set profile high`、`bmem config set vector_search.enabled true` / 设置页档位和向量开关 | 枚举或布尔。开启可先返回待配模型。关闭停止新嵌入作业，保留原绑定。档位生效和历史对象重打包各有进度 |
| `models` | `bmem model add/configure/remove <name>` / 模型表单 | `add` 要求唯一非空名称、协议、端点及提供方模型标识 `model_id`，服务生成稳定配置 ID。`update` 以该 ID 修改至少一个字段，未提交字段沿用原值。显示名可改，绑定仍引用同一 ID。`remove` 返回被移除 ID，保留用户已有的模型权重 |
| `bindings.extract`、`bindings.embed` | `bmem model use <name> --for extract`、`bmem model unbind --for embed` / 设为提炼或嵌入模型、解除用途 | CLI 与 Web 均将唯一名称解析为配置 ID。非空 ID 必须存在，并已通过本模型、端点、用途组合的测试。空值 `null` 解除该用途，缺省保留。设置新嵌入绑定时向量开关须已开启或在本补丁开启 |
| `connectors` | `bmem connector add <type>` 或 `bmem connector configure <source_id>` 的连接参数步骤 / U4 连接参数 | 必须引用已由 `source.register` 登记的来源 ID。可改显示名、凭据引用和声明支持分批读取的来源的 `page_size`。后者为正整数且符合该连接器的能力范围，传 `null` 恢复连接器默认值。新增/更换账号走来源流程。授权范围、游标、停采和删除行为各由对应领域处理 |

**凭据和路由。** 所有者可在模型新增参数或编辑的 `changes` 中提交 `api_key`。字符串写入新密钥，`null` 清除，省略时保留既有凭据。模型的 `api_key` 和 `credential_ref` 互斥，CLI 使用的环境变量引用格式仍为 `{"backend":"env","name":"BMEM_MODEL_API_KEY"}`。配置快照、密钥变更及重试回执在工作区 `data/settings.sqlite3` 同一事务内提交。旧 `config.toml` 首次启动时导入。原始密钥只存于 `model_api_keys` 表，运行时按引用读取。配置读取、回执和诊断只返回 `credential_configured`。协议、端点、提供方模型或凭据变更会使旧连接测试失效。改本地显示名称可保留测试状态。

### 两个入口提交的相同输入

在 `config_revision=7`，CLI 使用 `bmem config set profile high --expected-revision 7`，Web 在同一修订选择「高压缩／保存」。两者的请求体相同。

```json
{"expected_revision":7,"patch":{"profile":"high"}}
```

成功返回 HTTP 200。配置已生效，`activity` 按本次涉及的能力返回最近观测。本例有历史对象稍后处理。

```json
{"config_revision":8,"profile":"high","vector_search":{"enabled":false},"activity":{"repack":"pending"}}
```

CLI 执行 `bmem model add extractor` 并在表单选择已有 API，或在 Web 点「模型／新增」，会提交下列合成设置。得到新 ID 后可直接从另一入口调用 `bmem model test extractor --for extract` 或对应测试控件。

```json
{"expected_revision":8,"patch":{"models":{"op":"add","name":"extractor","protocol":"openai_chat","endpoint":"https://models.example.invalid/v1","model_id":"example-gen","credential_ref":{"backend":"system","id":"cred_example"}}}}
```

下例中的 `m_s1` 代表服务生成的 ID。只有实际测试通过后才绑定供提炼使用。

```json
{"config_revision":9,"profile":"high","vector_search":{"enabled":false},"model_result":{"op":"add","id":"m_s1","name":"extractor","protocol":"openai_chat","endpoint":"https://models.example.invalid/v1","model_id":"example-gen","credential_configured":true}}
```

`bmem model use extractor --for extract` 与 Web「设为提炼模型」共享下列请求。两端收到的 `bindings` 均为完整的两用途当前值，如 `{"extract":"m_s1","embed":null}`。

```json
{"expected_revision":9,"patch":{"bindings":{"extract":"m_s1"}}}
```

在另一份初始库中，`src_docs` 已登记，来源描述符允许每批 20 条。`bmem connector configure src_docs` 与 Web U4 的两个控件分别映射为显示名、每批数量。账号、授权和已提交游标沿用其原有记录。

```json
{"expected_revision":3,"patch":{"connectors":{"source_id":"src_docs","settings":{"display_name":"开发资料","page_size":20}}}}
```

响应始终包含 `config_revision`、`profile` 和 `vector_search`。涉及模型目录、绑定或连接参数的请求分别附带 `model_result`、两项 `bindings` 或 `connector_result`。读取整个设置用 `GET /v2/config`。单次补丁最多改一个模型和一个来源，绑定可以同次修改。服务先建立整份目标配置再校验引用，例如一次解除两个绑定并移除原模型可成功。新增所分配的 ID 由增设响应取得，随后通过测试再绑定。

### 校验一次成功交换

CLI 和网页的契约测试分别捕获实际请求及 HTTP 200 响应，组成 `{"request": …, "response": …}` 后送入[成对 Schema](config-patch-exchange.schema.json)。其中两个 URN 引用[现有描述符](config-patch.example.json)里的输入、输出 Schema。请求中每出现 `models`、`bindings`、`connectors`，回执就必须出现其对应结果；未涉及的组不返回该结果。混合补丁同时应用全部条件。这个包装只用于核对同一次 HTTP 交换。

通过结构验证后，再核对数据值。成功提交修订等于所带预期修订加一。请求指定的档位、向量开关和用途绑定均须与回执中的已生效值相符。模型动作 `op` 必须相同，更新、移除复用请求中的模型 ID，新增返回服务分配的 ID 并可从 `GET /v2/config` 核对。连接器回执必须指向请求中的 `source_id`。幂等重放与原请求、原提交结果配对。4xx 错误使用公共错误契约。

以下命令可从项目根在已有开发环境中执行。它同时校验描述符和成功交换，覆盖组合提交、遗漏结果、多带结果，以及合法形状却回错模型 ID 的情况。

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator as Validator
from jsonschema_specifications import REGISTRY
from referencing import Resource

folder = Path("docs/v2/technical/interfaces")
def load(name):
    return json.loads((folder / name).read_text(encoding="utf-8"))

descriptor = load("config-patch.example.json")
schemas = [descriptor["input_schema"], descriptor["output_schema"]]
registry = REGISTRY.with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
Validator(load("command-descriptor.schema.json"), registry=registry).validate(descriptor)
exchange = Validator(load("config-patch-exchange.schema.json"), registry=registry)

patch = {"models": {"op": "remove", "id": "m1"}, "bindings": {"extract": None},
         "connectors": {"source_id": "s1", "settings": {"display_name": "Docs"}}}
request = {"expected_revision": 7, "patch": patch}
response = {"config_revision": 8, "profile": "balanced", "vector_search": {"enabled": False},
            "model_result": {"op": "remove", "id": "m1"}, "bindings": {"extract": None, "embed": None},
            "connector_result": {"source_id": "s1", "display_name": "Docs", "credential_configured": False}}

def matches(req, receipt):
    if not exchange.is_valid({"request": req, "response": receipt}):
        return False
    changes = req["patch"]
    model = changes.get("models")
    if model:
        result = receipt["model_result"]
        if result["op"] != model["op"] or ("id" in model and result["id"] != model["id"]):
            return False
    if "connectors" in changes and receipt["connector_result"]["source_id"] != changes["connectors"]["source_id"]:
        return False
    return (receipt["config_revision"] == req["expected_revision"] + 1
            and all(receipt[key] == changes[key] for key in ("profile", "vector_search") if key in changes)
            and all(receipt["bindings"][key] == value for key, value in changes.get("bindings", {}).items()))

assert matches(request, response)
for field in ("model_result", "bindings", "connector_result"):
    assert not matches(request, {k: v for k, v in response.items() if k != field})
assert not matches({"expected_revision": 7, "patch": {"profile": "balanced"}}, response)
assert not matches(request, {**response, "model_result": {"op": "remove", "id": "other"}})
print("config.patch exchange OK")
PY
```

### 拒绝或重试

| 输入或时序 | 两端可核对的结果 |
|---|---|
| `{"expected_revision":7,"patch":{"vector_search":{"enabled":"false"}}}` | HTTP 422 `invalid_input`，布尔值不能用字符串。未知键、空模型改动、传入未声明字段 `api_key`、尝试写授权或游标同样失败 |
| `{"expected_revision":9,"patch":{"connectors":{"source_id":"src_docs","settings":{"grant":"all"}}}}` | HTTP 422。设置处理器返回字段错误，授权版本不变。改用途时转向预览与确认操作 |
| 输入结构正确，但给不支持翻页的宿主来源设置 `page_size`、绑定未通过该用途测试的模型、移除目标配置中仍被绑定的模型 | HTTP 422，指出对应字段和可执行修复。先修改所需绑定或配置，重新提交后核对修订。仅改显示名保留稳定 ID |
| 两端都从修订 7 打开，第一项成功后另一项仍发 `expected_revision=7` | HTTP 409 `revision_changed`。读当前快照，保留用户未提交编辑，按新状态决定是否重新保存。不同正文复用旧幂等键返回 `key_conflict` |

独立联调时在两个隔离数据根分别运行 CLI、Web 的相同正例，按业务对象对应关系比较修订、绑定、凭据状态和实际排队次数。在同一数据根用同一键跨入口重提交，确认同一生成 ID 和结果。结构反例由 Schema 校验，引用有效性、权限、原子性和幂等由配置服务验收。[公共失败语义](../contracts.md)
