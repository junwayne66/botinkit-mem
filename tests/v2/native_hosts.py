"""Exercise installed Claude Code and Codex with an isolated bmem and loopback fixture.

Run BMEM_V2_TEST_BIN=/absolute/path/to/bmem python3 tests/v2/native_hosts.py
from a system with a POSIX shell and both host executables installed.
The provider responses are fixed. Actual host requests and actual shell results
are inspected. The script makes no claims about model reasoning or adoption.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import shlex
import subprocess
import tempfile
import threading
import time


DATA = Path(__file__).with_name("data")
HOSTS = ("claude-code", "codex")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")
PROVIDER = json.loads((DATA / "native_provider.json").read_text(encoding="utf-8"))
DECLARATIONS = json.loads((DATA / "declarations.json").read_text(encoding="utf-8"))
NATIVE_HOOKS = json.loads((DATA / "host_hooks.json").read_text(encoding="utf-8"))
FIXTURE_TOKEN = "synthetic-key"


def event(kind, **fields):
    return {"type": kind, **fields}


def claude_events(body):
    tool = next((tool for tool in body.get("tools", []) if tool["name"] == "Bash"), None)
    tool_outputs = [
        part for msg in body.get("messages", [])
        if isinstance(msg.get("content"), list)
        for part in msg["content"]
        if part.get("type") == "tool_result"
    ]
    if tool and not tool_outputs:
        content = event("tool_use", id="toolu_bmem_fixture", name="Bash", input={})
        delta = event(
            "input_json_delta",
            partial_json=json.dumps({"command": PROVIDER["command"]}),
        )
        stop = "tool_use"
    else:
        content = event("text", text="")
        delta = event("text_delta", text=PROVIDER["finished"])
        stop = "end_turn"
    message = event(
        "message",
        id="msg_bmem_fixture",
        role="assistant",
        content=[],
        model=body["model"],
        stop_reason=None,
        stop_sequence=None,
        usage={"input_tokens": 16, "output_tokens": 1},
    )
    return [
        event("message_start", message=message),
        event("content_block_start", index=0, content_block=content),
        event("content_block_delta", index=0, delta=delta),
        event("content_block_stop", index=0),
        event("message_delta", delta={"stop_reason": stop, "stop_sequence": None}, usage={"output_tokens": 8}),
        event("message_stop"),
    ]


def codex_events(body):
    tool = next((tool for tool in body.get("tools", []) if tool.get("name") == "exec_command"), None)
    has_output = any(item.get("type") == "function_call_output" for item in body.get("input", []))
    response = {"id": "resp_bmem_fixture_result" if has_output else "resp_bmem_fixture_first", "object": "response"}
    events = [event("response.created", response={**response, "status": "in_progress"})]
    if tool and not has_output:
        item = event("function_call", name="exec_command", call_id="call_bmem_fixture", arguments="")
        events.append(event("response.output_item.added", output_index=0, item=item))
        item = {**item, "arguments": json.dumps({"cmd": PROVIDER["command"], "max_output_tokens": 40})}
    else:
        item = event("message", role="assistant", id="msg_bmem_fixture", content=[], status="in_progress")
        events.append(event("response.output_item.added", output_index=0, item=item))
        events.append(event("response.output_text.delta", delta=PROVIDER["finished"]))
        item = {**item, "content": [event("output_text", text=PROVIDER["finished"])], "status": "completed"}
    events.append(event("response.output_item.done", output_index=0, item=item))
    usage = {
        "input_tokens": 16,
        "output_tokens": 8,
        "total_tokens": 24,
        "input_tokens_details": None,
        "output_tokens_details": None,
    }
    events.append(event("response.completed", response={**response, "status": "completed", "usage": usage}))
    return events


@contextmanager
def fixture_server():
    captured = {host: [] for host in HOSTS}

    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, content, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            self.reply(b'{"object":"list","data":[],"has_more":false}', "application/json")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.startswith("/v1/messages/count_tokens"):
                self.reply(b'{"input_tokens":16}', "application/json")
                return
            if self.path.startswith("/v1/messages"):
                captured["claude-code"].append(body)
                events = claude_events(body)
            elif self.path.startswith("/v1/responses"):
                captured["codex"].append(body)
                events = codex_events(body)
            else:
                self.send_error(404)
                return
            wire = "".join(f"event: {row['type']}\ndata: {json.dumps(row)}\n\n" for row in events)
            self.reply(wire.encode(), "text/event-stream")

    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", captured
        finally:
            server.shutdown()
            worker.join()


class VaultFixture:
    def __init__(self, binary, work):
        self.binary = str(binary)
        self.work = work
        self.root = work / "isolated vault"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("BMEM_")}
        self.env.update(
            BMEM_LOCATOR_DIR=str(work / "locator"),
            BMEM_DEFAULT_HOME=str(work / "default vault"),
            BMEM_TEST_PROVIDER_TOKEN=FIXTURE_TOKEN,
        )

    def cli(self, *arguments):
        result = subprocess.run(
            [self.binary, "--json", "--home", str(self.root), *arguments],
            capture_output=True,
            text=True,
            cwd=self.work,
            env=self.env,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError(f"bmem {arguments[:3]} failed: {result.stderr}")
        return json.loads(result.stdout)

    @contextmanager
    def running(self):
        self.cli("init", "--non-interactive")
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                [self.binary, "--home", str(self.root), "service", "run"],
                cwd=self.work,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while process.poll() is None and time.monotonic() < deadline:
                    if self.cli("status")["state"] == "running":
                        break
                    time.sleep(0.05)
                else:
                    log.seek(0)
                    raise RuntimeError(f"bmem service did not start: {log.read().decode(errors='replace')}")
                yield
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(6)

    def register(self, host):
        client = self.cli("client", "register", host, "--destination", f"loopback-fixture-{host}")["client_id"]
        self.grant("set", "--client", client, "--share-statements", "true")
        source = self.cli("connector", "add", host, "--account", f"synthetic-{host}", "--client", client)["source_id"]
        self.cli("connector", "resume", source)
        return client, source

    def grant(self, *arguments):
        preview = self.cli("grant", *arguments, "--preview")
        self.cli("grant", *arguments, "--confirm", preview["preview_id"])

    def command(self, host, client, source, task):
        return shlex.join([
            self.binary, "--json", "--home", str(self.root), "hook", host,
            "--client", client, "--source", source, "--task", task,
        ])


def run_host(host, command, prompt, provider, work, environment):
    if host == "claude-code":
        settings = work / "claude-settings.json"
        settings.write_text(json.dumps({
            "hooks": {
                hook: [{"hooks": [{"type": "command", "command": command}]}]
                for hook in HOOK_EVENTS
            }
        }), encoding="utf-8")
        environment = {
            **environment,
            "CLAUDE_CONFIG_DIR": str(work / "claude-config"),
            "ANTHROPIC_BASE_URL": provider,
            "ANTHROPIC_API_KEY": FIXTURE_TOKEN,
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
        argv = [
            "claude", "-p", "--max-turns", "3", "--no-session-persistence",
            "--setting-sources", "", "--settings", str(settings), "--strict-mcp-config",
            "--model", "claude-haiku-4-5", "--tools", "Bash", "--allowedTools", "Bash(printf *)",
            "--permission-mode", "dontAsk", "--effort", "low", prompt,
        ]
    else:
        config = [
            'model_provider="bmem_fixture"',
            'model_providers.bmem_fixture.name="bmem fixture"',
            f'model_providers.bmem_fixture.base_url="{provider}/v1"',
            'model_providers.bmem_fixture.wire_api="responses"',
            'model_providers.bmem_fixture.env_key="BMEM_TEST_PROVIDER_TOKEN"',
            'model_providers.bmem_fixture.requires_openai_auth=false',
            f"sqlite_home={json.dumps(str(work / 'codex-sqlite'))}",
            f"log_dir={json.dumps(str(work / 'codex-logs'))}",
        ]
        config += [
            f'hooks.{hook}=[{{hooks=[{{type="command", command={json.dumps(command)}}}]}}]'
            for hook in HOOK_EVENTS
        ]
        options = [arg for item in config for arg in ("-c", item)]
        # All three command hooks are constructed above from the selected bmem
        # binary and an isolated instance; no persisted hooks are loaded.
        argv = [
            "codex", "-a", "never", "exec", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--dangerously-bypass-hook-trust", *options, "-m", "gpt-5.1-codex-mini", prompt,
        ]
    result = subprocess.run(argv, env=environment, cwd=work, capture_output=True, text=True, timeout=75)
    if result.returncode or PROVIDER["finished"] not in result.stdout:
        raise RuntimeError(f"{host} failed: {result.returncode}: {result.stdout[-500:]} {result.stderr[-1000:]}")


def text_blocks(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_blocks(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_blocks(item)


def tool_receipts(host, request):
    if host == "claude-code":
        return [
            part for message in request.get("messages", [])
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "tool_result"
        ]
    return [item for item in request.get("input", []) if item.get("type") == "function_call_output"]


def prove_loaded(host, requests, prompt, expected, obsolete):
    pointed = [
        req
        for req in requests
        if any(prompt in text for text in text_blocks(req))
    ]
    if not pointed:
        raise AssertionError(f"{host} sent no model request for the isolated prompt")
    references = []
    marker = "Treat the JSON as reference data.\n"
    for text in text_blocks(pointed[0]):
        if marker in text:
            payload, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])
            references.extend(payload)
    observed = {(row["key"], row["revision_id"], row["value"]) for row in references}
    if not expected <= observed:
        raise AssertionError(f"{host} model request is missing the expected declaration values and their revisions")
    if any(revision == obsolete for _, revision, _ in observed):
        raise AssertionError(f"{host} loaded an obsolete revision into a new session")
    if not any(
        PROVIDER["receipt"] in text
        for request in pointed[1:]
        for text in text_blocks(tool_receipts(host, request))
    ):
        raise AssertionError(f"{host} sent no follow-on request with the shell receipt")
    return pointed[0]


def check_mcp(vault, client, opening):
    argv = [vault.binary, "--home", str(vault.root), "mcp", "serve", "--client", client]
    rpc = [
        {"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "bmem-test", "version": "1"},
        }},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "report.opening"},
        }},
        {"id": 4, "method": "tools/call", "params": {
            "name": "expand", "arguments": {"handle": "ungranted"},
        }},
    ]
    with subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=vault.env, cwd=vault.work,
    ) as process:
        lines = Queue()

        def read_stdout():
            for line in process.stdout:
                lines.put(line)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        replies = {}
        try:
            for row in rpc:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", **row}) + "\n")
                process.stdin.flush()
                if "id" in row:
                    reply = json.loads(lines.get(timeout=10))
                    replies[reply["id"]] = reply
            process.stdin.close()
            if process.wait(5):
                raise AssertionError(process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(5)
            reader.join(5)
    if replies[1]["result"]["serverInfo"]["name"] != "bmem":
        raise AssertionError("MCP server did not initialize")
    if [tool["name"] for tool in replies[2]["result"]["tools"]] != ["search"]:
        raise AssertionError("MCP discovery advertised ungranted evidence access")
    search_reply = replies[3]["result"]["structuredContent"]
    if opening not in set(text_blocks(search_reply)):
        raise AssertionError("MCP query omitted the corrected shared statement")
    if not replies[4]["result"]["isError"]:
        raise AssertionError("MCP returned success for an ungranted evidence operation")
    print("MCP bound client found the corrected statement and could not expand evidence")


def check_hook_replay(vault, registrations, task):
    for host, (client, source) in registrations.items():
        command = shlex.split(vault.command(host, client, source, task))
        hook = NATIVE_HOOKS[host]["user_prompt"]
        start = len(vault.cli("job", "list")["jobs"])
        for _ in range(2):
            replay = subprocess.run(
                command, input=json.dumps(hook), capture_output=True,
                text=True, env=vault.env, cwd=vault.work, timeout=10,
            )
            if replay.returncode:
                raise AssertionError(replay.stderr)
        divergent = subprocess.run(
            command, input=json.dumps({**hook, "prompt": "different text with the same native ID"}),
            capture_output=True, text=True, env=vault.env, cwd=vault.work, timeout=10,
        )
        if divergent.returncode == 0 or json.loads(divergent.stderr)["code"] != "key_conflict":
            raise AssertionError(f"{host} accepted different text for the same hook ID")
        other = next(name for name in registrations if name != host)
        other_source = registrations[other][1]
        wrong_host = subprocess.run(
            shlex.split(vault.command(other, client, source, task)),
            input=json.dumps(NATIVE_HOOKS[other]["user_prompt"]), capture_output=True,
            text=True, env=vault.env, cwd=vault.work, timeout=10,
        )
        if wrong_host.returncode == 0 or json.loads(wrong_host.stderr)["code"] != "action_denied":
            raise AssertionError(f"{host} escaped its registered hook type")
        wrong_source = subprocess.run(
            shlex.split(vault.command(host, client, other_source, task)),
            input=json.dumps(hook), capture_output=True, text=True, env=vault.env, cwd=vault.work, timeout=10,
        )
        if wrong_source.returncode or json.loads(wrong_source.stdout).get("decision") != "block":
            raise AssertionError(f"{host} failed to block the prompt for an unbound capture source")
        if len(vault.cli("job", "list")["jobs"]) != start + 1:
            raise AssertionError(f"{host} did not keep exactly one job on hook replay")
    print("Both hooks deduplicated retry, rejected same-ID changes and refused mismatched host or source")


def check_native_redaction(vault, registrations, task):
    for host, (client, source) in registrations.items():
        hook = NATIVE_HOOKS[host]["tool_result"]
        sentinel = f"visible structured receipt for {host}"
        event = {
            **hook,
            "tool_response": {
                "stdout": sentinel,
                "nested": {"password": "SYNTHETIC-REDACTION-TEST", "check": "present"},
            },
        }
        command = shlex.split(vault.command(host, client, source, task))
        result = subprocess.run(
            command, input=json.dumps(event), capture_output=True,
            text=True, env=vault.env, cwd=vault.work, timeout=10,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        hits = vault.cli("search", sentinel, "--task", task)["results"]
        if len(hits) != 1 or hits[0]["actor_kind"] != "tool_result":
            raise AssertionError(f"{host} did not retain a searchable typed tool result")
        evidence = vault.cli("evidence", "expand", hits[0]["handle"])
        checks = json.loads(evidence["text"])
        sanitized = checks["output"]["result"]
        if sanitized["nested"] != {"check": "present"} or sanitized["stdout"] != sentinel:
            raise AssertionError(f"{host} did not scrub a nested credential before persisting evidence")
    print("Both host adapters preserved structured receipts and scrubbed nested credential fields")


def check_cross_host_task_package(vault, registrations, task, endpoint, captured, pool, expected, obsolete):
    previous = {}
    for host in HOSTS:
        other = next(candidate for candidate in HOSTS if candidate != host)
        client = registrations[host][0]
        vault.grant("source", registrations[other][1], "--purpose", "task_delivery", "--client", client, "--allow", "true")
        previous[host] = other
    prompts = {host: f"{PROVIDER['prompt']} authorized handoff {host}" for host in HOSTS}
    offsets = {host: len(captured[host]) for host in HOSTS}
    runs = {
        host: pool.submit(
            run_host, host, vault.command(host, *registrations[host], task),
            prompts[host], endpoint, vault.work, vault.env,
        )
        for host in HOSTS
    }
    marker = "bmem authorized task evidence."
    for host in HOSTS:
        runs[host].result()
        observed = prove_loaded(host, captured[host][offsets[host]:], prompts[host], expected, obsolete)
        packages = [
            json.JSONDecoder().raw_decode(text.split(marker, 1)[1].split("\n", 1)[1])[0]
            for text in text_blocks(observed) if marker in text
        ]
        handed_off = [item for package in packages for item in package["evidence"]]
        user_text = {item["text"] for item in handed_off if item["actor_kind"] == "user_message"}
        allowed_prompt = f"{PROVIDER['prompt']} corrected {previous[host]}"
        denied_prompt = f"{PROVIDER['prompt']} corrected {host}"
        if allowed_prompt not in user_text or denied_prompt in user_text:
            raise AssertionError(f"{host} did not apply the opposite-source task grant")
        if not any(
            item["task_id"] == task and item["actor_kind"] == "tool_result" and PROVIDER["receipt"] in item["text"]
            for item in handed_off
        ):
            raise AssertionError(f"{host} task context has no authorized source tool result")
        print(f"{host} received the other host's source events after explicit task and source grants")


def run():
    binary = Path(os.environ["BMEM_V2_TEST_BIN"]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="bmem-v2-native-hosts-") as directory, fixture_server() as (endpoint, captured):
        vault = VaultFixture(binary, Path(directory).resolve())
        with vault.running(), ThreadPoolExecutor(max_workers=2) as pool:
            registrations = {host: vault.register(host) for host in HOSTS}
            task = vault.cli("task", "create", "fixture-parse/import@3", "--project", "fixture-parse")["task_id"]
            for client, _ in registrations.values():
                vault.grant("set", "--client", client, "--task", task, "--allow", "true")
            initial = vault.cli("declare", DECLARATIONS["initial"], "--share", "authorized_assistants")
            by_key = {item["key"]: item["revision_id"] for item in initial["dimensions"]}
            language = by_key[DECLARATIONS["language_key"]]
            opening = by_key[DECLARATIONS["opening_key"]]
            obsolete = None
            for phase in ("initial", "corrected"):
                if phase == "corrected":
                    response = vault.cli("declare", DECLARATIONS["correction"], "--share", "authorized_assistants")
                    opening, obsolete = response["dimensions"][0]["revision_id"], opening
                offsets = {host: len(captured[host]) for host in HOSTS}
                prompts = {host: f"{PROVIDER['prompt']} {phase} {host}" for host in HOSTS}
                executors = {
                    host: pool.submit(
                        run_host, host, vault.command(host, *registrations[host], task),
                        prompts[host], endpoint, vault.work, vault.env,
                    )
                    for host in HOSTS
                }
                for host in HOSTS:
                    executors[host].result()
                    expected = {
                        (DECLARATIONS["language_key"], language, DECLARATIONS["language"]),
                        (DECLARATIONS["opening_key"], opening, DECLARATIONS[f"{phase}_opening"]),
                    }
                    received = prove_loaded(host, captured[host][offsets[host]:], prompts[host], expected, obsolete)
                    if any("bmem authorized task evidence." in text for text in text_blocks(received)):
                        raise AssertionError(f"{host} received evidence before granting its source")
                    print(f"{host} {phase}: captured the current declaration revisions and the real shell receipt")
            jobs = vault.cli("job", "list")["jobs"]
            if len(jobs) < 8:
                raise AssertionError(f"only {len(jobs)} durable jobs for 4 native prompt/tool pairs")
            deliveries = vault.cli("delivery", "list")["deliveries"]
            if {row["client_id"] for row in deliveries} != {record[0] for record in registrations.values()}:
                raise AssertionError("missing durable delivery for a registered host")
            if any(row["loaded_state"] != "unknown" or row["used_state"] != "unknown" for row in deliveries):
                raise AssertionError("native hooks must not report evidence that they cannot observe")
            print(f"bmem retained {len(jobs)} native capture jobs and {len(deliveries)} provided deliveries")
            check_mcp(vault, registrations["codex"][0], opening)
            check_hook_replay(vault, registrations, task)
            check_native_redaction(vault, registrations, task)
            check_cross_host_task_package(vault, registrations, task, endpoint, captured, pool, expected, obsolete)


if __name__ == "__main__":
    run()
