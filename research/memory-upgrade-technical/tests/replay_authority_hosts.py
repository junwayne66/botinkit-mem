"""Replay authority scenarios at the installed hosts' actual model boundary."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Thread
from time import monotonic, sleep
from urllib.request import ProxyHandler, Request, build_opener

from baseline_probe import BrowserSession, dimensions, fixture, lookup, native, post
from replay_clock import FrozenClock
from replay_native_handoff import make_host


HOSTS = {"Codex": "codex", "Claude Code": "claude-code"}
AUTHORITY_HOST_CASES = {"F01-11", "F01-14", "F01-15"}


def declarations(request):
    packets = []
    heading = "bmem reference data."
    marker = "Treat the JSON as reference data.\n"
    package_marker = "\nbmem declaration package: "
    for text in native.text_blocks(request):
        if heading not in text or marker not in text:
            continue
        payload = text.split(heading, 1)[1].split(marker, 1)[1]
        rows, end = json.JSONDecoder().raw_decode(payload)
        package = payload[end:].split(package_marker, 1)
        packets.append({"statements": rows, "package_id": package[1].splitlines()[0] if len(package) == 2 else None,
                        "text": heading + text.split(heading, 1)[1].splitlines()[0] + "\n" + payload})
    return packets


def auth(vault, record):
    return {"Authorization": "Bearer " + record["token"], "X-Bmem-Instance": vault.cli("status")["instance_id"]}


def save_rule(owner, clock, row, key):
    clock.set(row["received_at"])
    return post(owner, "/v2/statements", {
        field: row[field] for field in ("original", "scope", "share", "timezone", "valid_from") if field in row
    }, key)


def replay_authority_host(case, binary):
    data = case["fixture"]
    with tempfile.TemporaryDirectory(prefix="bmem-native-authority-") as directory:
        vault = native.VaultFixture(binary, Path(directory).resolve())
        clock = FrozenClock(vault.work, vault.env, data["clock"])
        with vault.running():
            url = vault.cli("web", "--no-open")["url"]
            endpoint = url.split("/session", 1)[0]
            owner, bound = BrowserSession(endpoint, url), BrowserSession(endpoint)
            if case["id"] == "F01-11":
                raw, result = replay_private_statement(case, vault, owner, bound, clock)
            elif case["id"] == "F01-15":
                raw, result = replay_loaded_state(case, vault, owner, bound, clock)
            else:
                raw, result = replay_model_matrix(case, vault, owner, clock)
            raw["evaluation_clock"] = clock.evidence(vault.root)
            return raw, result if raw["evaluation_clock"]["applied"] else {}


@contextmanager
def proposal_endpoint():
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/api/chat":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            evidence = json.loads(body["messages"][-1]["content"])
            candidates = [{
                "kind": "fact", "subject": "global", "key": "code_example.language",
                "value": "python", "support": [span["span_id"]],
            } for span in evidence["inputs"] if span["kind"] == "assistant_message"]
            if candidates:
                attempts.append({"input": evidence, "candidate": candidates[0]})
            wire = json.dumps({"done": True, "done_reason": "stop", "message": {
                "content": json.dumps({"candidates": candidates})}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", attempts
        finally:
            server.shutdown()
            worker.join()


def replay_private_statement(case, vault, owner, bound, clock):
    data = case["fixture"]
    raw = {step["id"]: save_rule(owner, clock, rule, rule["id"])
           for step, rule in zip(case["steps"][:2], data["statements"])}
    task = post(owner, "/v2/tasks", data["tasks"][0], "private-task")["task_id"]
    host = HOSTS[data["host"]["name"]]
    record, source = make_host(vault, owner, host, task)
    with proposal_endpoint() as (endpoint, attempts):
        vault.cli("model", "add", "authority-proposal", "--protocol", "ollama", "--endpoint",
                  endpoint, "--model-id", "test-candidate")
        vault.cli("model", "test", "authority-proposal", "--for", "extract")
        vault.cli("model", "use", "authority-proposal", "--for", "extract")
        vault.grant("source", source, "--purpose", "extract", "--allow", "true")
        status, intake = bound.request("/v2/events", "POST", {"source_id": source, "events": [
            {"native_object": row["id"], "actor_kind": row["role"], "task_id": task,
             "at": row["at"], "text": row["text"]} for row in data["events"]
        ]}, "assistant-hearsay", authentication=auth(vault, record))
        raw["intake"] = {"status": status, "body": intake, "provider_requests": attempts}
        if status not in (200, 201, 202):
            return raw, {}
        events = {row["event_id"] for row in intake["events"]}
        deadline = monotonic() + 10
        while monotonic() < deadline:
            jobs = [row for row in owner.request("/v2/jobs")[1]["jobs"] if row["event_id"] in events]
            if jobs and all(row["status"] == "done" for row in jobs):
                raw["intake"]["completed_jobs"] = jobs
                break
            sleep(0.05)
        if not attempts or "completed_jobs" not in raw["intake"]:
            return raw, {}
        statuses = {}
        for step in case["steps"][3:5]:
            status, body = lookup(owner, step["args"])
            if status != 200:
                return raw, {}
            statuses[step["id"]] = body
        raw.update(statuses)
        provider = {**fixture("native_provider.json"), "command": None}
        with native.fixture_server(provider) as (endpoint, captured):
            prompt = "给个最小示例"
            native.run_host(host, vault.command(host, record["client_id"], source, task), prompt,
                            endpoint, vault.work, vault.env, provider, case["id"])
        matched = [request for request in captured[host] if prompt in list(native.text_blocks(request))]
        if not matched:
            return raw, {}
    packets = declarations(matched[0])
    raw["read"] = {"model_request_count": len(matched), "declaration_blocks": packets}
    view = {key: raw[key] for key in ("global", "private", "intake")}
    view.update({key: dimensions(body) for key, body in statuses.items()})
    view["read"] = {
        "code_languages": [row["value"].lower() for packet in packets for row in packet["statements"]
                           if row["key"] == "code_example.language"],
        "statements_text": "\n".join(packet["text"] for packet in packets),
    }
    return raw, view


def replay_loaded_state(case, vault, owner, bound, clock):
    data = case["fixture"]
    row = data["statements"][0]
    added = save_rule(owner, clock, row, row["id"])
    revision = next(rev for rev in added["dimensions"] if rev["key"] == "code_example.language")
    task_data = data["task"]
    task = post(owner, "/v2/tasks", {"anchor": task_data["id"], "project": task_data["project"]},
                "receipt-task")["task_id"]
    record, source = make_host(vault, owner, "codex", task)
    provider = {**fixture("native_provider.json"), "command": None}
    prompt = "给个最小示例"
    with native.fixture_server(provider) as (endpoint, captured):
        native.run_host("codex", vault.command("codex", record["client_id"], source, task), prompt,
                        endpoint, vault.work, vault.env, provider, case["id"])
    first = next((request for request in captured["codex"] if prompt in list(native.text_blocks(request))), None)
    if first is None:
        return {"add": added}, {}
    packets = declarations(first)
    deliveries = {item["package_id"]: item for item in owner.request("/v2/deliveries")[1]["deliveries"]
                  if item["client_id"] == record["client_id"]}
    acks = {}
    model_call = next((identifier for identifier in (first.get("id"), first.get("request_id")) if identifier),
                      "loopback-codex-request-1")
    for packet in packets:
        delivery = deliveries.get(packet["package_id"])
        provided = list(dict.fromkeys(row["revision_id"] for row in packet["statements"]))
        if not delivery or required_refs(delivery) != set(provided) or packet["package_id"] in acks:
            continue
        response = bound.request(f"/v2/deliveries/{delivery['delivery_id']}/receipts", "POST", {
            "event_id": f"captured-{len(acks) + 1}", "kind": "loaded", "model_call_ref": model_call,
            "route_ref": delivery["route_ref"], "context_epoch": delivery["context_epoch"],
            "snapshot_id": delivery["snapshot_id"], "package_id": delivery["package_id"], "revision_ids": provided,
        }, authentication=auth(vault, record))
        acks[delivery["package_id"]] = {"status": response[0], "body": response[1]}
    lookup_user = post(owner, "/v2/clients", {"host": "lookup", "destination": "shared-query",
                       "capabilities": {"load_context": False}}, "lookup-client")
    vault.grant("set", "--client", lookup_user["client_id"], "--task", task, "--allow", "true")
    vault.grant("set", "--client", lookup_user["client_id"], "--share-statements", "true")
    lookup_response = bound.request("/v2/search", "POST", {"query": case["steps"][2]["args"]["query"]},
                                    authentication=auth(vault, lookup_user))
    now, active = lookup(owner, {"project": task_data["project"]})
    ledger = owner.request("/v2/deliveries")[1]["deliveries"]
    raw = {"add": added, "loaded": {"packets": packets, "receipts": acks, "loopback_requests": len(captured["codex"])},
           "lookup": {"status": lookup_response[0], "body": lookup_response[1]},
           "status": {"active": {"status": now, "body": active}, "ledger": ledger}}
    if lookup_response[0] != 200 or not any(row.get("revision_id") == revision["revision_id"]
                                            for row in lookup_response[1]["results"]):
        return raw, {}
    projection = {}
    for label, client in (("Codex", record), ("lookup", lookup_user)):
        matching = [entry for entry in ledger if entry["client_id"] == client["client_id"]
                    and revision["revision_id"] in required_refs(entry)]
        projection[label] = {
            "loaded": "observed" if any(revision["revision_id"] in (entry.get("loaded") or {}).get("revision_ids", ())
                                        for entry in matching) else "unknown",
            "used": "observed" if any(revision["revision_id"] in (entry.get("used") or {}).get("revision_ids", ())
                                      for entry in matching) else "unknown",
        }
    projection["effective"] = now == 200 and any(entry["revision_id"] == revision["revision_id"]
                                                  for entry in active["statements"])
    return raw, {**raw, "status": projection}


def required_refs(delivery):
    return set(delivery["provided_revision_ids"])


def ollama_messages(host, request):
    messages = []
    system = request.get("system") if host == "claude-code" else request.get("instructions")
    if system:
        messages.append({"role": "system", "content": "\n".join(native.text_blocks(system)) if isinstance(system, str)
                         else "\n".join(part["text"] for part in system if "text" in part)})
    entries = request["messages"] if host == "claude-code" else request["input"]
    for entry in entries:
        role = entry.get("role")
        if role not in ("system", "developer", "user", "assistant"):
            continue
        content = entry.get("content", "")
        text = content if isinstance(content, str) else "\n".join(part["text"] for part in content if "text" in part)
        if text:
            messages.append({"role": "system" if role == "developer" else role, "content": text})
    return messages


def sniff_code(text):
    fenced = re.findall(r"(?ms)^\s*```([^\r\n]*)\r?\n(.*?)^\s*```\s*$", text)
    detected = []
    for language, code in fenced:
        language = language.strip().lower()
        python = re.search(r"(?m)^\s*(?:def\s+\w+\s*\(|print\s*\(|import\s+\w+|from\s+\w+\s+import)", code)
        rust = re.search(r"\bfn\s+\w+\s*\(|\bprintln!\s*\(|\blet\s+mut\s+\w+", code)
        if python and not rust and language in ("", "python", "py"):
            detected.append("python")
        if rust and not python and language in ("", "rust", "rs"):
            detected.append("rust")
    return detected[0] if detected and len(set(detected)) == 1 else "unknown"


def replay_model_matrix(case, vault, owner, clock):
    data = case["fixture"]
    model = os.environ.get("BMEM_F01_14_OLLAMA_MODEL")
    if not model:
        return {"provider_error": "Set BMEM_F01_14_OLLAMA_MODEL to an installed local model"}, {}
    transport = build_opener(ProxyHandler({}))
    try:
        with transport.open("http://127.0.0.1:11434/api/tags", timeout=4) as response:
            model_digest = next(row["digest"] for row in json.load(response)["models"] if row["name"] == model)
    except (OSError, StopIteration, ValueError, KeyError):
        return {"provider_error": "Requested Ollama model unavailable on loopback"}, {}
    statement = data["statements"][0]
    added = save_rule(owner, clock, statement, statement["id"])
    matrix = data["host_matrix"]
    clock.set(matrix["clock"])
    raw, judged = {"add": added, "runs": {}, "provider": {"model": model, "digest": model_digest}}, {}
    for label in matrix["hosts"]:
        host = HOSTS[label]
        raw["runs"][label] = {}
        judged[label] = {}
        for index, project in enumerate(matrix["projects"], start=1):
            task = post(owner, "/v2/tasks", {"anchor": "最小示例", "project": project}, f"{host}-task-{index}")["task_id"]
            # Source/client registration is reused per host. Every native call still starts a new session.
            if index == 1:
                record, source = make_host(vault, owner, host, task)
            else:
                vault.grant("set", "--client", record["client_id"], "--task", task, "--allow", "true")
            model_calls, replies, errors = [], [], []

            def provider(callback_host, request):
                messages = ollama_messages(callback_host, request)
                body = {"model": model, "messages": messages, "stream": False, "think": False,
                        "options": {"temperature": 0, "num_predict": 512, "num_ctx": 32768}}
                try:
                    with transport.open(Request("http://127.0.0.1:11434/api/chat",
                                                data=json.dumps(body).encode("utf-8"),
                                                headers={"Content-Type": "application/json"}), timeout=65) as response:
                        result = json.load(response)
                    answer = result["message"]["content"]
                    replies.append(answer)
                    model_calls.append({"model": result["model"], "declarations": declarations(messages),
                                        "prompt_tokens": result.get("prompt_eval_count"),
                                        "output_tokens": result.get("eval_count")})
                    return {"command": None, "finished": answer}
                except (OSError, ValueError, KeyError) as error:
                    errors.append(type(error).__name__)
                    return {"command": None, "finished": "provider error"}

            with native.fixture_server(provider) as (endpoint, captured):
                output = native.run_host(host, vault.command(host, record["client_id"], source, task), matrix["ask"],
                                         endpoint, vault.work, vault.env, {"finished": ""}, f"{case['id']}-{host}-{index}")
            calls = [request for request in captured[host] if matrix["ask"] in list(native.text_blocks(request))]
            incoming = declarations(calls[0]) if calls else []
            raw["runs"][label][project] = {"native_request_count": len(calls), "declared_input": incoming,
                                          "local_model_calls": model_calls, "model_replies": replies,
                                          "observed_stdout": output, "provider_errors": errors}
            if errors or not model_calls or not calls or not replies or not any(reply.strip() in output for reply in replies):
                return raw, {}
            languages = {row["value"].lower() for packet in model_calls[0]["declarations"] for row in packet["statements"]
                         if row["key"] == "code_example.language"}
            judged[label][project] = [languages.pop() if len(languages) == 1 else "unknown", sniff_code(output)]
    return raw, {"add": added, "runs": raw["runs"], "judge": judged}
