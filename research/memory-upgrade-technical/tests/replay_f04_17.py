"""Replay F04-17 via bound production retrieval and real isolated Claude Code requests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shlex
import sqlite3
import tempfile
from threading import Thread
from time import monotonic, sleep

from baseline_probe import BrowserSession, events, fixture, load_bound, native, post
from replay_native_handoff import incoming, make_host


@contextmanager
def embedding_provider(data):
    dimensions = len(data["questions"][0]["query_embedding"])
    vectors = {row["query"]: row["query_embedding"] for row in data["questions"]}
    for row in data["events"]:
        text = row["text"]
        vectors[text] = row["embedding"]
        if row["role"] == "tool_result":
            # Ingestion materializes checks as a JSON object before projection.
            vectors[json.dumps({"output": text}, ensure_ascii=False, separators=(",", ":"))] = row["embedding"]
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/api/embed":
                self.send_error(404)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            inputs = payload["input"]
            if isinstance(inputs, str):
                inputs = [inputs]
            calls.extend(inputs)
            body = json.dumps({"embeddings": [vectors.get(item, [1] + [0] * (dimensions - 1))
                                              for item in inputs]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            worker.join()


def native_run(vault, host, reader, task, question, protocol, frozen, neighbor, arm, aliases):
    work = Path(tempfile.mkdtemp(prefix="empty-session-", dir=vault.work)).resolve()
    defaults = fixture("native_provider.json")
    output = work / "result.txt"
    prompt = f"{question['query']}\n{protocol}\n本次输出文件为 result.txt。"
    decisions = []

    def model(_, request):
        if native.tool_receipts(host, request):
            return {**defaults, "command": None}
        packets = incoming(request)
        rows = packets[-1]["evidence"] if packets else []
        values = {number for row in rows for number in re.findall(r"\bRESULT=(\d+)", row["text"])}
        value = next(iter(values)) if len(values) == 1 else "UNKNOWN"
        decisions.append(value)
        program = f"from pathlib import Path; Path('result.txt').write_bytes({(value + chr(10)).encode()!r})"
        return {**defaults, "command": "python3 -c " + shlex.quote(program)}

    command = shlex.join([str(vault.binary), "--json", "--home", str(vault.root), "hook", host,
                          "--client", reader["client_id"], "--task", task, "--evidence-seed-set", frozen["seed_set"]]
                         + ([] if neighbor else ["--evidence-no-neighbor"]))
    with native.fixture_server(model) as (endpoint, captured):
        native.run_host(host, command, prompt, endpoint, work, vault.env, defaults,
                        f"F04-17-{arm}-{question['id']}", tool_commands=("python3 -c *",))
    request = next(row for row in captured[host]
                   if any(prompt in text for text in native.text_blocks(row)) and not native.tool_receipts(host, row))
    packets = incoming(request)
    if len(packets) > 1:
        raise RuntimeError("a fresh native session received multiple evidence packages")
    package = packets[0] if packets else {}
    evidence = package.get("evidence", [])
    window = package.get("window") or {}
    if evidence and (window.get("seed_fingerprint") != frozen["seed_fingerprint"] or window.get("neighbor") != neighbor):
        raise RuntimeError("host received a different frozen selection")
    tool_responses = [tool for row in captured[host] for tool in native.tool_receipts(host, row)]
    if not tool_responses or len(decisions) != 1 or not output.is_file():
        raise RuntimeError(f"native run did not finish exactly one file-producing call ({arm}/{question['id']})")
    return {
        "seed_fingerprint": window.get("seed_fingerprint", frozen["seed_fingerprint"]),
        "scope_fingerprint": window.get("scope_fingerprint", frozen["scope_fingerprint"]),
        "candidate_refs": [aliases[handle] for handle in frozen["seed_refs"]],
        "actual_refs": [aliases[row["handle"]] for row in evidence],
        "observed_budget": window.get("read_budget", frozen["read_budget"]),
        "candidate_limit": window.get("candidate_limit", frozen["candidate_limit"]),
        "evidence_bytes_utf8": sum(len(row["text"].encode("utf-8")) for row in evidence),
        "echoed_evidence_bytes_utf8": window.get("evidence_bytes_utf8", 0),
        "model_decision": decisions[0],
        "file_bytes_hex": output.read_bytes().hex(),
        "wire_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "observed_native_requests": len(captured[host]),
        "observed_tool_responses": len(tool_responses),
        "neighbor": neighbor,
    }


def replay_f04_17(case, binary):
    data = case["fixture"]
    limits = data["conditions"]
    task_label = data["bound_task"]
    host = "claude-code"
    with tempfile.TemporaryDirectory(prefix="bmem-frozen-f04-17-") as folder:
        vault = native.VaultFixture(binary, Path(folder).resolve())
        with vault.running(), embedding_provider(data) as (endpoint, provider_calls):
            url = vault.cli("web", "--no-open")["url"]
            service = url.split("/session", 1)[0]
            browser = BrowserSession(service)
            owner = BrowserSession(service, url)
            task_names = {row["task"]: row["project"] for row in data["events"]}
            tasks = {label: post(owner, "/v2/tasks", {"anchor": label, "project": project},
                                 "task-" + label)["task_id"] for label, project in task_names.items()}
            task = tasks[task_label]
            reader, _ = make_host(vault, owner, host, task)
            auth = {"Authorization": "Bearer " + reader["token"], "X-Bmem-Instance": vault.cli("status")["instance_id"]}
            rows = []
            for saved in data["events"]:
                row = dict(saved)
                if row["role"] == "tool_result":
                    row["actor_kind"] = "tool_result"
                    row["checks"] = {"output": row.pop("text")}
                elif row["role"] == "tool_call":
                    row["actor_kind"] = "assistant_message"
                rows.append(row)
            accepted, sources = events(owner, rows, tasks)
            aliases = {"event_" + event: label for event, label in accepted.items()}
            handles = {label: handle for handle, label in aliases.items()}
            for row in data["events"]:
                post(owner, "/v2/evidence/tool-link", {"handle": handles[row["ref"]], "run_id": row["run_id"],
                    "tool_call_id": row["tool_call_id"], "role": "result" if row["role"] == "tool_result" else "call"},
                     "link-" + row["ref"])
            vault.cli("config", "set", "vector_search.enabled", "true")
            vault.cli("model", "add", limits["model"], "--protocol", "ollama", "--endpoint", endpoint,
                      "--model-id", limits["model"])
            vault.cli("model", "test", limits["model"], "--for", "embed")
            probe_count = len(provider_calls)
            vault.cli("model", "use", limits["model"], "--for", "embed")
            for label, grants in data["grants"][data["owner"]].items():
                if "embedding" in grants:
                    vault.grant("source", sources[label], "--purpose", "embed", "--allow", "true")
            for label, grants in data["grants"][data["reader"]].items():
                if "task_delivery" in grants:
                    vault.grant("source", sources[label], "--purpose", "task_delivery", "--client", reader["client_id"], "--allow", "true")
            uri = (vault.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            deadline = monotonic() + 15
            with sqlite3.connect(uri, uri=True) as db:
                while db.execute("SELECT count(*) FROM vector_projection").fetchone()[0] != len(rows):
                    if monotonic() >= deadline:
                        raise RuntimeError("the whole frozen input did not finish vector projection")
                    sleep(0.05)
            embedded_documents = provider_calls[probe_count:].copy()
            expected_inputs = {row["text"] for row in data["events"]} | {
                json.dumps({"output": row["text"]}, ensure_ascii=False, separators=(",", ":"))
                for row in data["events"] if row["role"] == "tool_result"
            }
            if len(embedded_documents) != len(rows) or not set(embedded_documents) <= expected_inputs:
                raise RuntimeError("vector projection did not use only the declared fixture documents")
            questions = {row["id"]: row for row in data["questions"]}
            steps = {row["id"]: row["args"] for row in case["steps"]}
            order = steps["modes"]["questions"]
            selected = {}
            retrieval_requests = []
            for question_id in order:
                for mode in steps["modes"]["modes"]:
                    payload = {"task_id": task, "query": questions[question_id]["query"], "mode": mode,
                               "candidate_limit": limits["candidate_limit_after_fusion"], "evidence_slots": limits["evidence_slots"],
                               "evidence_byte_limit_utf8": limits["evidence_byte_limit_utf8"],
                               "semantic_cosine_floor": limits["semantic_cosine_floor"]}
                    status, frozen = browser.request("/v2/evidence/seeds", "POST", payload, authentication=auth)
                    if status != 201 or frozen.get("semantic_used") != (mode != "lexical"):
                        raise RuntimeError(f"bound retrieval did not execute requested {mode} channel ({status}): {frozen}")
                    selected[question_id,mode] = frozen
                    retrieval_requests.append({"question": question_id,"request":payload,"returned":frozen})
            if len({row["scope_fingerprint"] for row in selected.values()}) != 1:
                raise RuntimeError("recall modes did not use the same authorized corpus")
            selection_count = len(retrieval_requests)
            embed_count = len(provider_calls)

            def execute(spec):
                question, arm, neighbor, frozen = spec
                return question, arm, native_run(vault, host, reader, task, questions[question],
                    data["host_protocol"], frozen, neighbor, arm, aliases)

            mode_jobs = [(qid, mode, steps["modes"]["neighbor"], selected[qid,mode])
                         for qid in order for mode in steps["modes"]["modes"]]
            window_jobs = [(qid, "window_" + mode, mode == "on", selected[qid,"union"])
                           for qid in order for mode in steps["window"]["arms"]]
            with ThreadPoolExecutor(max_workers=4) as pool:
                mode_runs = {(q,mode):row for q,mode,row in pool.map(execute, mode_jobs)}
                if len(provider_calls) != embed_count:
                    raise RuntimeError("a frozen mode triggered a second model search")
                window_runs = {(q,mode):row for q,mode,row in pool.map(execute, window_jobs)}
            retrieval_calls_during_window = len(retrieval_requests) - selection_count + len(provider_calls) - embed_count
            executions = {**mode_runs, **window_runs}
            with sqlite3.connect(uri, uri=True) as db:
                session_count = db.execute("SELECT count(DISTINCT session) FROM context_deliveries WHERE task_id=?",
                                           (task,)).fetchone()[0]
            if session_count != len(executions):
                raise RuntimeError(f"expected a fresh native session per execution, observed {session_count}")
            gold = data["verifier_only"]
            judge = {
                "query_order": order, "mode_runs_observed": len(mode_runs), "window_runs_observed": len(window_runs),
                "distinct_effective_candidate_limits": sorted({row["candidate_limit"] for row in executions.values()}),
                "distinct_effective_read_budgets": [dict(pair) for pair in sorted({tuple(sorted(row["observed_budget"].items()))
                                                                                 for row in executions.values()})],
                "over_budget_runs": sum(len(row["actual_refs"]) > row["observed_budget"]["slots"]
                                        or row["evidence_bytes_utf8"] > row["observed_budget"]["bytes"]
                                        or row["echoed_evidence_bytes_utf8"] != row["evidence_bytes_utf8"]
                                        for row in executions.values()),
                "anchor_hit": {mode: [None if gold[qid]["anchor"] is None
                                      else gold[qid]["anchor"] in mode_runs[qid,mode]["candidate_refs"]
                                      for qid in order] for mode in steps["modes"]["modes"]},
                "same_seed_fingerprint_for_window": [window_runs[qid,"window_off"]["seed_fingerprint"]
                                                     == window_runs[qid,"window_on"]["seed_fingerprint"]
                                                     == selected[qid,"union"]["seed_fingerprint"] for qid in order],
                "retrieval_calls_during_window": retrieval_calls_during_window,
                "actual_input_complete": {mode: [None if gold[qid]["anchor"] is None
                                                   else set(gold[qid]["required"]).issubset(window_runs[qid,mode]["actual_refs"])
                                                   for qid in order] for mode in ("window_off","window_on")},
                "file_matches": {mode: [bytes.fromhex(executions[qid,mode]["file_bytes_hex"]) == gold[qid]["file_bytes"].encode("utf-8")
                                         for qid in order] for mode in (*steps["modes"]["modes"],"window_off","window_on")},
                "unanswered_nonempty_inputs": sum(bool(row["actual_refs"]) for (qid,_),row in executions.items()
                                                  if gold[qid]["anchor"] is None),
                "same_code_forbidden_refs_in_any_candidate_or_actual_input": sorted({ref for (qid,_),row in executions.items()
                        for ref in row["candidate_refs"] + row["actual_refs"] if ref in gold[qid].get("forbidden", [])}),
            }
            vault.grant("source", sources["log-Q"], "--purpose", "task_delivery", "--client", reader["client_id"], "--allow", "false")
            change_status, change = load_bound(browser, auth, reader, "revocation-control", task,
                frozen_evidence={"seed_set": selected[order[0],"union"]["seed_set"],"neighbor": True})
            if change_status != 409:
                raise RuntimeError("a revoked frozen scope was still accepted")
            mode_rows = [{"question":q,"arm":arm,**row} for (q,arm),row in mode_runs.items()]
            window_rows = [{"question":q,"arm":arm,**row} for (q,arm),row in window_runs.items()]
            raw = {"modes": {"rankings": retrieval_requests,"runs":mode_rows,"embedded_documents":embedded_documents},
                    "window":{"runs":window_rows,
                    "retrieval_calls_during_window":retrieval_calls_during_window},"score":judge,
                    "fresh_native_sessions":session_count,
                    "revocation_control":{"status":change_status,"body":change}}
            return raw, {"modes":{"observed":len(mode_rows)},"window":{"observed":len(window_rows)},"score":judge}
