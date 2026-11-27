"""Process-isolated reference arms for X01-12. Input is the frozen synthetic timeline."""

import hashlib
from importlib.metadata import distribution, version
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


PINNED_VERSION = "2.2.1"
WHEEL_SHA256 = "fe91bb91ac8926231993a4aa58df00a60c6c74c709e6a338fe399500776eef4d"
PINNED_CLIENTS = {"qdrant-client": "1.18.0", "ollama": "0.6.1"}
PINNED_MODULES = {
    "mem0/memory/main.py": "f329ca4a17af1fbefb32ac10d4cc7ae7ddbdb7435a87eb74c30915cff98b8b7c",
    "mem0/configs/prompts.py": "10bc8a34b3b5f0ce24560a2a3190c9112b979a891b981f48393bbd168d915a5c",
}


def isolate(work, endpoint):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError("the frozen arm requires an explicit loopback model server")
    rejected = []
    observed = set()
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def observe_path(name):
        if isinstance(name, int) or name == ":memory:":
            return
        path = Path(os.fsdecode(name)).resolve()
        if not path.is_relative_to(work):
            rejected.append(path.name)
            raise PermissionError("upstream arm cannot write outside its isolated state")
        observed.add(str(path.relative_to(work)))

    def inspect(event, args):
        if event == "open" and args[2] is not None and args[2] & write_flags:
            observe_path(args[0])
        elif event in ("sqlite3.connect", "os.mkdir", "os.remove", "os.rmdir"):
            observe_path(args[0])
        elif event == "os.rename":
            observe_path(args[0])
            observe_path(args[1])
        elif event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address[:2] != (parsed.hostname, parsed.port or 80):
                raise PermissionError("upstream arm cannot connect outside the loopback model endpoint")

    sys.addaudithook(inspect)
    return observed, rejected


def verify_package():
    package = distribution("mem0ai")
    if package.version != PINNED_VERSION:
        raise RuntimeError("installed Mem0 version differs from the replay version")
    modules = {name: hashlib.sha256(package.locate_file(name).read_bytes()).hexdigest()
               for name in PINNED_MODULES}
    if modules != PINNED_MODULES:
        raise RuntimeError("installed Mem0 engine differs from the pinned wheel")
    clients = {name: version(name) for name in PINNED_CLIENTS}
    if clients != PINNED_CLIENTS:
        raise RuntimeError("installed model and vector clients differ from the replay versions")
    return {"version": package.version, "reference_wheel_sha256": WHEEL_SHA256, "module_sha256": modules,
            "client_versions": clients}


def full_memory(value, model, endpoint, work):
    from mem0 import Memory
    from ollama import Client

    class ReplayModelClient(Client):
        def chat(self, *args, **kwargs):
            # Mem0's extraction prompt exceeds Ollama's default context window.
            kwargs["options"] = {**kwargs.get("options", {}), "num_ctx": 12288}
            return super().chat(*args, **kwargs)

    provider = ReplayModelClient(host=endpoint, timeout=100)
    advertised = {entry["model"]: entry["digest"] for entry in provider.list()["models"]}
    if model not in advertised or value["embed_model"] not in advertised:
        raise RuntimeError("both local model versions must already be installed")
    settings = {
        "version": "v1.1",
        "history_db_path": str(work / "history.sqlite3"),
        "llm": {"provider": "ollama", "config": {"model": model, "ollama_base_url": endpoint,
                                                "temperature": 0, "max_tokens": 180}},
        "embedder": {"provider": "ollama", "config": {"model": value["embed_model"], "embedding_dims": 1024,
                                                     "ollama_base_url": endpoint}},
        "vector_store": {"provider": "qdrant", "config": {"collection_name": "frozen_x01_12",
                          "path": str(work / "qdrant"), "embedding_model_dims": 1024}},
    }
    trace = []
    entered = []
    with Memory.from_config(settings) as memory:
        memory.llm.client = provider
        filters = {"user_id": "frozen-x01-12"}
        initial = memory.get_all(filters=filters)["results"]
        if initial:
            raise RuntimeError("upstream storage did not start empty")
        for operation in value["timeline"]:
            if operation["kind"] == "event":
                result = memory.add([{"role": "user", "content": operation["text"]}], user_id="frozen-x01-12",
                                    metadata={"source_event": operation["id"], "fixture_time": operation["time"]},
                                    infer=True, prompt="提取关于编程语言的已述事实，保留完整语言名。")
                entered.append(operation["id"])
                trace.append({"time": operation["time"], "event_id": operation["id"],
                              "persisted_memories": len(result["results"])})
            else:
                found = memory.search(operation["q"], filters=filters, limit=5,
                                      threshold=0)["results"]
                # Query responses are projected by the adapter to answer_text only.
                messages = [{"role": "system", "content": "根据记忆回答问题。以最后一次明确更正为准。只给简短答案。"},
                            {"role": "user", "content": json.dumps({"question": operation["q"],
                                 "memories": [{"text": row["memory"], "arrival": row.get("metadata", {}).get("fixture_time")}
                                              for row in found]}, ensure_ascii=False)}]
                answer = provider.chat(model=model, messages=messages, think=False,
                                       options={"temperature": 0, "num_predict": 50})["message"]["content"].strip()
                trace.append({"time": operation["time"], "input_watermark": list(entered),
                              "response": {"answer_text": answer}})
        history_written = (work / "history.sqlite3").stat().st_size > 0
        index_created = (work / "qdrant").is_dir()
    if not any(row.get("persisted_memories") for row in trace) or not history_written or not index_created:
        raise RuntimeError("upstream writes did not reach its independent history and vector store")
    return {"initial_memory_count": len(initial), "timeline": trace, "history_written": history_written,
            "index_created": index_created, "model_context_tokens": 12288,
            "local_model_digests": {k: advertised[k] for k in (model, value["embed_model"])}}


def merge_only(value, model, endpoint):
    from mem0.configs.prompts import get_update_memory_messages
    from ollama import Client

    provider = Client(host=endpoint, timeout=70)
    # Invoke this published upstream update component without creating an upstream store.
    before = [{"id": row["id"], "text": row["text"]} for row in value["before"]]
    question = get_update_memory_messages(before, [value["event"]["text"]])
    messages = [{"role":"system","content":"You consolidate a single programming-language preference. "
                                           "A correction ('更正') changes that preference and uses the old ID. "
                                           "Return valid JSON following the supplied format."},
                {"role":"user","content":question}]
    result = provider.chat(model=model, messages=messages, format="json", think=False,
                           options={"temperature":0,"num_predict":280})["message"]["content"]
    decoded = json.loads(result)
    changes = decoded.get("memory")
    if not isinstance(changes, list) or any(not isinstance(row, dict) for row in changes):
        raise RuntimeError("the actual upstream merger did not return its decision array")
    return {"component": "mem0.configs.prompts.get_update_memory_messages", "proposal": changes,
            "input_digest": hashlib.sha256(question.encode()).hexdigest()}


def main():
    value = json.load(sys.stdin)
    work = Path.cwd().resolve()
    endpoint = value["endpoint"]
    observed, rejected = isolate(work, endpoint)
    provenance = verify_package()
    if value["mode"] == "full":
        result = full_memory(value, value["model"], endpoint, work)
    elif value["mode"] == "merge":
        result = merge_only(value, value["model"], endpoint)
    else:
        raise ValueError("unrecognized upstream arm")
    print(json.dumps({"provenance": provenance, "result": result,
                      "isolation": {"local_write_targets": sorted(observed), "nonisolated_writes": len(rejected)}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
