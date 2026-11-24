"""Discover-counted TestCase for the real Claude Code and Codex host checks.

T044-E: the check logic that used to live in the native_hosts.py script is
migrated here as six real `test_*` methods on `NativeHosts(unittest.TestCase)`,
so `unittest discover` counts them instead of silently collecting zero.
native_hosts.py remains the single implementation layer (host fixtures,
run_host, prove_loaded, the measured HOST_CALL_TIMEOUT budget); this module
owns the orchestration and the assertions.  There is no parallel copy.

Opt-in guard mirrors tests/v2/test_integration.py:692-697.  It sits on the
CLASS so that, when BMEM_V2_TEST_NATIVE_HOSTS is unset, unittest skips all six
methods *without* running setUpClass (no service is started, no binary is
needed) and run_gates.py can predict the expected skip set from the AST guard.
The full-evidence profile sets BMEM_V2_TEST_NATIVE_HOSTS=1 and requires zero
skips.

The six cases share one vault/service built in setUpClass, but each runs its
own real host calls -- none of them merely inspects a previous step's text.
They are sequential by fixture dependency (the correction must supersede the
initial declaration before MCP/cross-host observe it), so load_tests fixes the
order rather than relying on alphabetical method names.

Attribution: when an installed host CLI outlives its measured per-call budget,
native_hosts.run_host raises HostTimeout carrying a BMEM_HOST_TIMEOUT marker.
_fail_host_timeout puts that marker in the FIRST segment of the failure message
and writes it to tests/v2/artifacts/native_hosts_output.txt; gates/judge.py maps
it to reason_code=external-host-cli-timeout.  The step still FAILs -- attribution
is not leniency.
"""

import contextlib
import json
import os
from pathlib import Path
from queue import Queue
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import native_hosts as nh  # noqa: E402  (single implementation layer)

REPO = HERE.parent.parent
ARTIFACTS = HERE / "artifacts"
DATA = HERE / "data"
OUTPUT_FILE = ARTIFACTS / "native_hosts_output.txt"

# Kept as a literal (in sync with native_hosts.py and gates/judge.py; the gate
# unit tests assert the three copies agree) so this module stays collectable
# even if the implementation module's fixtures move.
HOST_TIMEOUT_MARKER = "BMEM_HOST_TIMEOUT"

# Fixture dependency order: initial declarations -> correction supersedes them
# -> MCP/hook/redaction/cross-host observe the corrected revision.
_ORDER = (
    "test_initial_declarations_load_current_revisions",
    "test_corrected_declaration_supersedes_obsolete",
    "test_mcp_bound_client_search_and_denied_expand",
    "test_hook_replay_dedup_and_binding",
    "test_native_redaction_scrubs_nested_credentials",
    "test_cross_host_task_package_grants",
)


def load_tests(loader, tests, pattern):
    # Honour the sequential fixture dependency regardless of method name sort.
    return unittest.TestSuite(NativeHosts(name) for name in _ORDER)


@unittest.skipUnless(
    os.environ.get("BMEM_V2_TEST_NATIVE_HOSTS") == "1",
    "set BMEM_V2_TEST_NATIVE_HOSTS to opt in to the installed Claude Code and Codex hosts",
)
class NativeHosts(unittest.TestCase):
    # -- class-level fixture: one vault/service for all six cases -----------

    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("BMEM_V2_TEST_BIN")
        if not binary or not Path(binary).is_file():
            raise RuntimeError(
                "BMEM_V2_TEST_BIN must point at a built bmem binary for the host check")
        cls.binary = Path(binary).resolve()
        # Module-level fixture data is loaded here, once, not at import time.
        cls.provider = json.loads((DATA / "native_provider.json").read_text(encoding="utf-8"))
        cls.declarations = json.loads((DATA / "declarations.json").read_text(encoding="utf-8"))
        cls.native_hooks = json.loads((DATA / "host_hooks.json").read_text(encoding="utf-8"))
        cls._timeouts = []
        cls.corrected = None
        cls._stack = contextlib.ExitStack()
        try:
            tmp = cls._stack.enter_context(
                tempfile.TemporaryDirectory(prefix="bmem-v2-native-hosts-"))
            cls.endpoint, cls.captured = cls._stack.enter_context(
                nh.fixture_server(cls.provider))
            cls.vault = nh.VaultFixture(cls.binary, Path(tmp).resolve())
            cls._stack.enter_context(cls.vault.running())
            cls.pool = cls._stack.enter_context(ThreadPoolExecutor(max_workers=2))
            cls.registrations = {host: cls.vault.register(host) for host in nh.HOSTS}
            cls.task = cls.vault.cli(
                "task", "create", "fixture-parse/import@3", "--project", "fixture-parse")["task_id"]
            for client, _ in cls.registrations.values():
                cls.vault.grant("set", "--client", client, "--task", cls.task, "--allow", "true")
            initial = cls.vault.cli(
                "declare", cls.declarations["initial"], "--share", "authorized_assistants")
            by_key = {item["key"]: item["revision_id"] for item in initial["dimensions"]}
            cls.language = by_key[cls.declarations["language_key"]]
            cls.opening = by_key[cls.declarations["opening_key"]]
            cls.obsolete = None
        except BaseException:
            cls._stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    # -- attribution helpers ------------------------------------------------

    def _artifact(self, detail):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        markers = "\n".join(self._timeouts)
        code = "external-host-cli-timeout" if self._timeouts else "test-failure"
        OUTPUT_FILE.write_text(
            "# binary=%s\n# reason_code=%s\n%s\n%s\n" % (self.binary, code, markers, detail),
            encoding="utf-8")

    def _fail_host_timeout(self, exc, host, phase):
        marker = str(exc)
        self._timeouts.append(marker)
        detail = ("external host CLI exceeded its measured budget "
                  "(reason_code=external-host-cli-timeout); host=%s phase=%s; full output: %s"
                  % (host, phase, OUTPUT_FILE))
        self._artifact(detail)
        # The marker leads the message so tail truncation cannot hide it and
        # gates/judge.py can record reason_code=external-host-cli-timeout.
        self.fail("%s\n%s" % (marker, detail))

    def _declaration_phase(self, phase, expected, obsolete):
        """Run both hosts for one declaration phase and prove what loaded."""
        offsets = {host: len(self.captured[host]) for host in nh.HOSTS}
        prompts = {host: "%s %s %s" % (self.provider["prompt"], phase, host) for host in nh.HOSTS}
        runs = {
            host: self.pool.submit(
                nh.run_host, host,
                self.vault.command(host, *self.registrations[host], self.task),
                prompts[host], self.endpoint, self.vault.work, self.vault.env,
                self.provider, nh.PHASE_LABELS[phase])
            for host in nh.HOSTS
        }
        for host in nh.HOSTS:
            with self.subTest(host=host, phase=nh.PHASE_LABELS[phase]):
                try:
                    runs[host].result()
                except nh.HostTimeout as exc:
                    self._fail_host_timeout(exc, host, nh.PHASE_LABELS[phase])
                received = nh.prove_loaded(
                    host, self.captured[host][offsets[host]:], prompts[host],
                    expected, obsolete, self.provider)
                if any("bmem authorized task evidence." in text for text in nh.text_blocks(received)):
                    self.fail("%s received evidence before granting its source" % host)

    # -- the six counted cases ----------------------------------------------

    def test_initial_declarations_load_current_revisions(self):
        expected = {
            (self.declarations["language_key"], self.language, self.declarations["language"]),
            (self.declarations["opening_key"], self.opening, self.declarations["initial_opening"]),
        }
        self._declaration_phase("initial", expected, None)

    def test_corrected_declaration_supersedes_obsolete(self):
        response = self.vault.cli(
            "declare", self.declarations["correction"], "--share", "authorized_assistants")
        new_opening = response["dimensions"][0]["revision_id"]
        obsolete = self.opening
        expected = {
            (self.declarations["language_key"], self.language, self.declarations["language"]),
            (self.declarations["opening_key"], new_opening, self.declarations["corrected_opening"]),
        }
        # Publish the corrected state for the later MCP / cross-host cases.
        type(self).opening = new_opening
        type(self).obsolete = obsolete
        type(self).corrected = {"expected": expected, "obsolete": obsolete, "opening": new_opening}
        self._declaration_phase("corrected", expected, obsolete)
        jobs = self.vault.cli("job", "list")["jobs"]
        self.assertGreaterEqual(
            len(jobs), 8, "only %d durable jobs for 4 native prompt/tool pairs" % len(jobs))
        deliveries = self.vault.cli("delivery", "list")["deliveries"]
        self.assertEqual(
            {row["client_id"] for row in deliveries},
            {record[0] for record in self.registrations.values()},
            "missing durable delivery for a registered host")
        for row in deliveries:
            self.assertEqual(row["loaded_state"], "unknown",
                             "native hooks must not report evidence that they cannot observe")
            self.assertEqual(row["used_state"], "unknown",
                             "native hooks must not report evidence that they cannot observe")

    def test_mcp_bound_client_search_and_denied_expand(self):
        vault = self.vault
        client = self.registrations["codex"][0]
        opening = self.opening
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
                self.assertEqual(process.wait(5), 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                reader.join(5)
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "bmem",
                         "MCP server did not initialize")
        self.assertEqual([tool["name"] for tool in replies[2]["result"]["tools"]], ["search"],
                         "MCP discovery advertised ungranted evidence access")
        search_reply = replies[3]["result"]["structuredContent"]
        self.assertIn(opening, set(nh.text_blocks(search_reply)),
                      "MCP query omitted the corrected shared statement")
        self.assertTrue(replies[4]["result"]["isError"],
                        "MCP returned success for an ungranted evidence operation")

    def test_hook_replay_dedup_and_binding(self):
        vault = self.vault
        registrations = self.registrations
        task = self.task
        for host, (client, source) in registrations.items():
            with self.subTest(host=host):
                command = shlex.split(vault.command(host, client, source, task))
                hook = self.native_hooks[host]["user_prompt"]
                start = len(vault.cli("job", "list")["jobs"])
                for _ in range(2):
                    replay = subprocess.run(
                        command, input=json.dumps(hook), capture_output=True,
                        text=True, env=vault.env, cwd=vault.work, timeout=10)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                divergent = subprocess.run(
                    command, input=json.dumps({**hook, "prompt": "different text with the same native ID"}),
                    capture_output=True, text=True, env=vault.env, cwd=vault.work, timeout=10)
                self.assertNotEqual(divergent.returncode, 0,
                                    "%s accepted different text for the same hook ID" % host)
                self.assertEqual(json.loads(divergent.stderr)["code"], "key_conflict",
                                 "%s accepted different text for the same hook ID" % host)
                other = next(name for name in registrations if name != host)
                other_source = registrations[other][1]
                wrong_host = subprocess.run(
                    shlex.split(vault.command(other, client, source, task)),
                    input=json.dumps(self.native_hooks[other]["user_prompt"]), capture_output=True,
                    text=True, env=vault.env, cwd=vault.work, timeout=10)
                self.assertNotEqual(wrong_host.returncode, 0,
                                    "%s escaped its registered hook type" % host)
                self.assertEqual(json.loads(wrong_host.stderr)["code"], "action_denied",
                                 "%s escaped its registered hook type" % host)
                wrong_source = subprocess.run(
                    shlex.split(vault.command(host, client, other_source, task)),
                    input=json.dumps(hook), capture_output=True, text=True,
                    env=vault.env, cwd=vault.work, timeout=10)
                self.assertEqual(wrong_source.returncode, 0,
                                 "%s failed to block the prompt for an unbound capture source" % host)
                self.assertEqual(json.loads(wrong_source.stdout).get("decision"), "block",
                                 "%s failed to block the prompt for an unbound capture source" % host)
                self.assertEqual(len(vault.cli("job", "list")["jobs"]), start + 1,
                                 "%s did not keep exactly one job on hook replay" % host)

    def test_native_redaction_scrubs_nested_credentials(self):
        vault = self.vault
        for host, (client, source) in self.registrations.items():
            with self.subTest(host=host):
                hook = self.native_hooks[host]["tool_result"]
                sentinel = "visible structured receipt for %s" % host
                event = {
                    **hook,
                    "tool_response": {
                        "stdout": sentinel,
                        "nested": {"password": "SYNTHETIC-REDACTION-TEST", "check": "present"},
                    },
                }
                command = shlex.split(vault.command(host, client, source, self.task))
                result = subprocess.run(
                    command, input=json.dumps(event), capture_output=True,
                    text=True, env=vault.env, cwd=vault.work, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                hits = vault.cli("search", sentinel, "--task", self.task)["results"]
                self.assertEqual(len(hits), 1,
                                 "%s did not retain a searchable typed tool result" % host)
                self.assertEqual(hits[0]["actor_kind"], "tool_result",
                                 "%s did not retain a searchable typed tool result" % host)
                evidence = vault.cli("evidence", "expand", hits[0]["handle"])
                checks = json.loads(evidence["text"])
                sanitized = checks["output"]["result"]
                self.assertEqual(sanitized["nested"], {"check": "present"},
                                 "%s did not scrub a nested credential before persisting evidence" % host)
                self.assertEqual(sanitized["stdout"], sentinel,
                                 "%s did not scrub a nested credential before persisting evidence" % host)

    def test_cross_host_task_package_grants(self):
        vault = self.vault
        registrations = self.registrations
        task = self.task
        self.assertIsNotNone(
            self.corrected, "corrected declaration phase must run before the cross-host handoff")
        expected = self.corrected["expected"]
        obsolete = self.corrected["obsolete"]
        previous = {}
        for host in nh.HOSTS:
            other = next(candidate for candidate in nh.HOSTS if candidate != host)
            client = registrations[host][0]
            vault.grant("source", registrations[other][1], "--purpose", "task_delivery",
                        "--client", client, "--allow", "true")
            previous[host] = other
        prompts = {host: "%s authorized handoff %s" % (self.provider["prompt"], host) for host in nh.HOSTS}
        offsets = {host: len(self.captured[host]) for host in nh.HOSTS}
        runs = {
            host: self.pool.submit(
                nh.run_host, host, vault.command(host, *registrations[host], task),
                prompts[host], self.endpoint, vault.work, vault.env,
                self.provider, nh.PHASE_LABELS["cross-host"])
            for host in nh.HOSTS
        }
        marker = "bmem authorized task evidence."
        for host in nh.HOSTS:
            with self.subTest(host=host, phase=nh.PHASE_LABELS["cross-host"]):
                try:
                    runs[host].result()
                except nh.HostTimeout as exc:
                    self._fail_host_timeout(exc, host, nh.PHASE_LABELS["cross-host"])
                observed = nh.prove_loaded(
                    host, self.captured[host][offsets[host]:], prompts[host],
                    expected, obsolete, self.provider)
                packages = [
                    json.JSONDecoder().raw_decode(text.split(marker, 1)[1].split("\n", 1)[1])[0]
                    for text in nh.text_blocks(observed) if marker in text
                ]
                handed_off = [item for package in packages for item in package["evidence"]]
                user_text = {item["text"] for item in handed_off if item["actor_kind"] == "user_message"}
                allowed_prompt = "%s corrected %s" % (self.provider["prompt"], previous[host])
                denied_prompt = "%s corrected %s" % (self.provider["prompt"], host)
                self.assertIn(allowed_prompt, user_text,
                              "%s did not apply the opposite-source task grant" % host)
                self.assertNotIn(denied_prompt, user_text,
                                 "%s did not apply the opposite-source task grant" % host)
                self.assertTrue(
                    any(item["task_id"] == task and item["actor_kind"] == "tool_result"
                        and self.provider["receipt"] in item["text"] for item in handed_off),
                    "%s task context has no authorized source tool result" % host)


if __name__ == "__main__":
    unittest.main()
