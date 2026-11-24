"""Implementation layer for the installed Claude Code / Codex host checks.

This module is imported by tests/v2/test_native_hosts.py, the TestCase module
that `unittest discover` counts.  It keeps only the reusable host fixtures and
the measured per-call budget; the check logic and orchestration live in the
test module, so there is exactly one implementation.  Run the checks with:

    BMEM_V2_TEST_NATIVE_HOSTS=1 BMEM_V2_TEST_BIN=/abs/path/to/bmem \
        python3 tests/v2/test_native_hosts.py

from a system with a POSIX shell and both host executables installed.
The provider responses are fixed. Actual host requests and actual shell results
are inspected. The fixtures make no claims about model reasoning or adoption.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time


HOSTS = ("claude-code", "codex")
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")
FIXTURE_TOKEN = "synthetic-key"

# Per-call budget for the *external* host CLIs, measured not guessed.
# 2026-11-24, N=9 full runs of this script (54 host calls; claude 2.1.308,
# codex-cli 0.171.0, loopback fixture provider, loadavg 3.0-4.0):
#   claude  p50/p95/max = 1.24 / 1.31 / 1.49 s
#   codex   p50/p95/max = 9.32 / 22.70 / 23.08 s
#     phase 1 (cold sqlite/log dirs) 21.94 / 23.08 / 23.08 s
#     phase 2 8.87 / 10.42 / 10.42 s, phase 3 9.12 / 9.49 / 9.49 s
#   round wall time p50/p95/max = 41.13 / 42.43 / 42.43 s, 9/9 passed,
#   no call above 75 s; the 414 bmem-side calls in the same runs maxed at 0.10 s.
# Rule: ceil(max(p95 * 3, max_observed * 1.5)), hard-capped at 300 s so one
# round can never grow without bound.  The fresh sample alone gives
# ceil(max(22.70*3, 23.08*1.5)) = 69 s -- *below* the 75 s budget that the
# 2026-11-24 assembly gate actually breached (codex, phase 1).  That breach is
# a right-censored observation of this same call site (true duration >= 75 s,
# unknown), so it enters the sample as max_observed = 75:
#   ceil(max(22.70*3, 75*1.5)) = ceil(112.5) = 113 s  <= 300 s cap.
# 113 s is therefore a floor of the rule, not a licence: it is ~5x the cold
# phase p95 and 1.5x the only observed breach.  Re-measure before raising it.
HOST_CALL_TIMEOUT = 113
HOST_CALL_TIMEOUT_CAP = 300
# Phase labels ride in the timeout marker so a gate reader can name the stage.
PHASE_LABELS = {"initial": "1-initial", "corrected": "2-corrected", "cross-host": "3-cross-host"}
# Machine-readable attribution line: the external CLI outlived its budget.
# tests/v2/test_native_hosts.py surfaces it in the failure message and
# gates/judge.py maps it to reason_code=external-host-cli-timeout (still FAIL).
HOST_TIMEOUT_MARKER = "BMEM_HOST_TIMEOUT"
HOST_TIMEOUT_EXIT = 3


class HostTimeout(Exception):
    """An installed host CLI exceeded HOST_CALL_TIMEOUT (external tail)."""


def event(kind, **fields):
    return {"type": kind, **fields}


def claude_events(body, provider):
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
            partial_json=json.dumps({"command": provider["command"]}),
        )
        stop = "tool_use"
    else:
        content = event("text", text="")
        delta = event("text_delta", text=provider["finished"])
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


def codex_events(body, provider):
    tool = next((tool for tool in body.get("tools", []) if tool.get("name") == "exec_command"), None)
    has_output = any(item.get("type") == "function_call_output" for item in body.get("input", []))
    response = {"id": "resp_bmem_fixture_result" if has_output else "resp_bmem_fixture_first", "object": "response"}
    events = [event("response.created", response={**response, "status": "in_progress"})]
    if tool and not has_output:
        item = event("function_call", name="exec_command", call_id="call_bmem_fixture", arguments="")
        events.append(event("response.output_item.added", output_index=0, item=item))
        item = {**item, "arguments": json.dumps({"cmd": provider["command"], "max_output_tokens": 40})}
    else:
        item = event("message", role="assistant", id="msg_bmem_fixture", content=[], status="in_progress")
        events.append(event("response.output_item.added", output_index=0, item=item))
        events.append(event("response.output_text.delta", delta=provider["finished"]))
        item = {**item, "content": [event("output_text", text=provider["finished"])], "status": "completed"}
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
def fixture_server(provider):
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
                events = claude_events(body, provider)
            elif self.path.startswith("/v1/responses"):
                captured["codex"].append(body)
                events = codex_events(body, provider)
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


def _tail(value, limit=800):
    """One-line tail of a possibly-bytes stream, for the timeout report."""
    if not value:
        return "(empty)"
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[-limit:].replace("\n", "\\n")


def run_host(host, command, prompt, endpoint, work, environment, fixture, phase="0-unlabelled"):
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
            "ANTHROPIC_BASE_URL": endpoint,
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
            f'model_providers.bmem_fixture.base_url="{endpoint}/v1"',
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
    started = time.monotonic()
    try:
        result = subprocess.run(argv, env=environment, cwd=work, capture_output=True, text=True,
                                timeout=HOST_CALL_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        # The external CLI outlived its measured budget.  Name it before any
        # traceback can bury it: the test module puts this line in the failure
        # message and judge.py classifies the step as external-host-cli-timeout.
        marker = "%s host=%s phase=%s elapsed=%.1f budget=%d exit=%d" % (
            HOST_TIMEOUT_MARKER, host, phase, elapsed, HOST_CALL_TIMEOUT, HOST_TIMEOUT_EXIT)
        print(marker, flush=True)
        print("  host argv head: %s" % (argv[:8],), flush=True)
        print("  host stdout tail: %s" % _tail(exc.stdout), flush=True)
        print("  host stderr tail: %s" % _tail(exc.stderr), flush=True)
        raise HostTimeout(marker)
    if result.returncode or fixture["finished"] not in result.stdout:
        raise RuntimeError(f"{host} failed after {time.monotonic() - started:.1f}s: "
                           f"{result.returncode}: {result.stdout[-500:]} {result.stderr[-1000:]}")


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


def prove_loaded(host, requests, prompt, expected, obsolete, fixture):
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
        fixture["receipt"] in text
        for request in pointed[1:]
        for text in text_blocks(tool_receipts(host, request))
    ):
        raise AssertionError(f"{host} sent no follow-on request with the shell receipt")
    return pointed[0]


if __name__ == "__main__":
    # Guard against the stale CONTRIBUTING entry silently passing: this file is
    # no longer a runnable script.  Exit non-zero and point at the real entry.
    sys.exit(
        "native_hosts.py is now an implementation module imported by "
        "test_native_hosts.py; run the host checks with:\n"
        "  BMEM_V2_TEST_NATIVE_HOSTS=1 BMEM_V2_TEST_BIN=/abs/path/to/bmem "
        "python3 tests/v2/test_native_hosts.py"
    )
