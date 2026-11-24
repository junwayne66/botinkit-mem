"""Unit tests for the T044-A gate runner (no real gates are ever run here).

Required coverage (task T044-A requirement 6):
  * any skip counts as failure (full-evidence skipped==0; baseline expected
    skip-set equality),
  * per-file browser empty runs are not evidence (browser_smoke.cjs is the
    only entry; zero-collection unittest output is not evidence),
  * missing BMEM_V2_TEST_REAL_MODEL fails with named variables,
  * worktree HEAD and binary SHA-256 fingerprints land in every record.

Everything is deterministic: fake ops, temp dirs, no cargo, no network, no
browser, no writes into the repository, no skips. Python 3.9, stdlib only.
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_V2 = os.path.dirname(_HERE)
if _V2 not in sys.path:
    sys.path.insert(0, _V2)

import run_gates  # noqa: E402
from gates import evidence, judge, steps as S  # noqa: E402

FAKE_HEAD = "5c44edc" + "0" * 33
MODEL = S.PINNED_MODEL


def make_cfg(root, **kw):
    defaults = dict(profile="assembly", jobs=2, target_dir="target-gate")
    defaults.update(kw)
    return S.Config(root, **defaults)


def fake_collection(ast_total=40, native=5, guards=None):
    return {"ast_total": ast_total, "methods": {"test_native_hosts": ["test_native_hosts.NativeHosts.test_%d" % i for i in range(native)]},
            "guards": guards or {}, "anomalies": [], "native_hosts_count": native,
            "sources": {}}


def unittest_output(ran, skipped_ids=(), status="OK"):
    lines = ["test_x (test_integration.BmemIntegration) ... ok"]
    for tid in skipped_ids:
        method = tid.split(".")[-1]
        cls = ".".join(tid.split(".")[:-1])
        lines.append("%s (%s) ... skipped 'opt-in'" % (method, cls))
    lines.append("Ran %d tests in 1.000s" % ran)
    if skipped_ids and status == "OK":
        lines.append("OK (skipped=%d)" % len(skipped_ids))
    else:
        lines.append(status)
    return "\n".join(lines) + "\n"


class FakeOps(run_gates.Ops):
    """Scripted side-effect surface; records every call."""

    def __init__(self, root, responder=None):
        super(FakeOps, self).__init__(root)
        self.calls = []
        self.responder = responder or (lambda argv, env: {"exit": 0, "stdout": "", "stderr": ""})
        self.files = {}
        self.exists_set = set()
        self.head_blob_text = None
        self.conflicts = {"HEAD": 64, None: 64}
        self.tags = {"ok": True, "data": {"models": [{"name": MODEL}]}}
        self.changed = []

    def run(self, argv, env=None, timeout=S.SHORT_TIMEOUT, cwd=None):
        self.calls.append(list(argv))
        base = self.responder(argv, env)
        out = {"exit": 0, "stdout": "", "stderr": "", "duration_ms": 5, "timed_out": False}
        out.update(base)
        return out

    def git_head(self):
        return FAKE_HEAD

    def git_branch(self):
        return "fix/test"

    def changed_files(self):
        return list(self.changed)

    def head_blob(self, path):
        return self.head_blob_text

    def conflict_count(self, ref=None):
        return self.conflicts.get(ref, 0)

    def http_get_json(self, url, timeout=10):
        return self.tags

    def which(self, name):
        return "/fake/bin/" + name

    def loadavg(self):
        return 1.0

    def read_text(self, path):
        return self.files.get(path)

    def exists(self, path):
        return path in self.exists_set or os.path.exists(path)

    def mtime(self, path):
        return 123.0 if self.exists(path) else None

    def glob(self, pattern):
        return []


def host_responder(argv, env):
    if argv and argv[-1] == "--version":
        if "claude" in argv[0]:
            return {"stdout": "2.1.308 (Claude Code)\n"}
        if "codex" in argv[0]:
            return {"stdout": "codex-cli 0.171.0\n"}
    if "unittest" in argv:
        return {"stdout": unittest_output(40), "exit": 0}
    return {}


def make_runner(tmp, responder=None, base_env=None, profile="assembly"):
    cfg = make_cfg(tmp, profile=profile)
    ops = FakeOps(tmp, responder)
    env = {"BMEM_V2_TEST_REAL_MODEL": MODEL, "PATH": "/usr/bin"}
    if base_env is not None:
        env = base_env
    runner = run_gates.Runner(cfg, ops, base_env=env)
    runner.head = FAKE_HEAD
    runner.branch = "fix/test"
    runner.changed = []
    runner.web_changed = False
    runner.jobs_info = S.resolve_jobs(2, 1.0, 8.0)
    runner.collection = fake_collection()
    ops.exists_set.add(cfg.binary_path)
    ops.exists_set.update({"/fake/bin/claude", "/fake/bin/codex"})
    return runner, ops, cfg


class TestSkipFails(unittest.TestCase):
    """Requirement: any skip counts as failure."""

    def test_full_evidence_any_skip_fails(self):
        parsed = judge.parse_unittest_output(unittest_output(40, ["test_integration.BmemIntegration.test_real_local_model_processes_fixture_page"]))
        res = judge.judge_unittest(parsed, 0, fake_collection(), {}, "posix", strict_zero_skips=True)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("skipped==0", res["reason"])

    def test_full_evidence_zero_skips_passes(self):
        parsed = judge.parse_unittest_output(unittest_output(40))
        res = judge.judge_unittest(parsed, 0, fake_collection(), {}, "posix", strict_zero_skips=True)
        self.assertEqual(res["status"], judge.PASS)

    def test_baseline_unexpected_skip_fails(self):
        parsed = judge.parse_unittest_output(unittest_output(40, ["test_workbench.WorkbenchTest.test_sneaky"]))
        res = judge.judge_unittest(parsed, 0, fake_collection(), {}, "posix", strict_zero_skips=False)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("unexpected", res["reason"])

    def test_baseline_expected_skip_set_passes(self):
        tid = "test_integration.BmemIntegration.test_real_local_model_processes_fixture_page"
        guards = {tid: [{"kind": "skipUnless", "cond": 'os.environ.get("BMEM_V2_TEST_REAL_MODEL")'}]}
        collection = fake_collection(guards=guards)
        parsed = judge.parse_unittest_output(unittest_output(40, [tid]))
        res = judge.judge_unittest(parsed, 0, collection, {}, "posix", strict_zero_skips=False)
        self.assertEqual(res["status"], judge.PASS)

    def test_baseline_missing_expected_skip_fails(self):
        tid = "test_integration.BmemIntegration.test_real_local_model_processes_fixture_page"
        guards = {tid: [{"kind": "skipUnless", "cond": 'os.environ.get("BMEM_V2_TEST_REAL_MODEL")'}]}
        collection = fake_collection(guards=guards)
        parsed = judge.parse_unittest_output(unittest_output(40))
        res = judge.judge_unittest(parsed, 0, collection, {}, "posix", strict_zero_skips=False)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("missing", res["reason"])

    def test_platform_guard_expected_skip(self):
        tid = "test_data_console.DataConsoleTest.test_posix_only"
        guards = {tid: [{"kind": "skipUnless", "cond": 'os.name == "posix"'}]}
        collection = fake_collection(guards=guards)
        expected, unknown = judge.expected_skip_ids(collection, {}, "nt")
        self.assertEqual(expected, {tid})
        self.assertEqual(unknown, [])
        expected_posix, _ = judge.expected_skip_ids(collection, {}, "posix")
        self.assertEqual(expected_posix, set())

    def test_cargo_ignored_counts_as_failure(self):
        parsed = judge.parse_cargo_test("test result: ok. 10 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n")
        res = judge.judge_cargo_test(parsed, 0)
        self.assertEqual(res["status"], judge.FAIL)

    def test_runner_full_discover_skip_fails(self):
        def responder(argv, env):
            if "unittest" in argv:
                return {"stdout": unittest_output(40, ["test_native_hosts.NativeHosts.test_mcp"]), "exit": 0}
            return host_responder(argv, env)
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp, responder)
            step = S.step_by_id(S.build_steps(cfg), "6")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.FAIL)
            self.assertIn("skipped==0", rec["reason"])


class TestPerFileEmptyRunNotEvidence(unittest.TestCase):
    """Requirement: per-file browser empty runs and zero collection are not evidence."""

    def test_browser_entry_command_accepted(self):
        self.assertTrue(judge.browser_command_is_entry(["node", "tests/v2/browser_smoke.cjs"]))

    def test_browser_per_file_commands_rejected(self):
        for name in S.BROWSER_SUBMODULES:
            argv = ["node", "tests/v2/" + name]
            self.assertFalse(judge.browser_command_is_entry(argv))
            res = judge.judge_browser_run(argv, 0, "")
            self.assertEqual(res["status"], judge.FAIL)
            self.assertIn("not evidence", res["reason"])

    def test_browser_empty_output_rejected(self):
        res = judge.judge_browser_run(["node", "tests/v2/browser_smoke.cjs"], 0, "\n")
        self.assertEqual(res["status"], judge.FAIL)

    def test_browser_step_uses_only_smoke_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            step = S.step_by_id(S.build_steps(cfg), "7")
            self.assertEqual(len(step.commands), 1)
            self.assertTrue(judge.browser_command_is_entry(step.commands[0]["argv"]))

    def test_zero_collection_unittest_fails(self):
        parsed = judge.parse_unittest_output("Ran 0 tests in 0.000s\n\nOK\n")
        res = judge.judge_unittest(parsed, 0, fake_collection(ast_total=0, native=0), {}, "posix", strict_zero_skips=True)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("zero tests", res["reason"])

    def test_count_mismatch_fails(self):
        parsed = judge.parse_unittest_output(unittest_output(12))
        res = judge.judge_unittest(parsed, 0, fake_collection(ast_total=40), {}, "posix", strict_zero_skips=True)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("AST", res["reason"])

    def test_native_hosts_must_be_collected(self):
        parsed = judge.parse_unittest_output(unittest_output(40))
        res = judge.judge_unittest(parsed, 0, fake_collection(native=0), {}, "posix", strict_zero_skips=True)
        self.assertEqual(res["status"], judge.FAIL)
        self.assertIn("test_native_hosts", res["reason"])

    def test_runner_browser_success(self):
        def responder(argv, env):
            if argv and argv[0].endswith("node") and "-e" in argv:
                return {"stdout": "browser-probe-ok\n"}
            if argv and argv[-1].endswith("browser_smoke.cjs"):
                return {"stdout": "Browser verified settings and declarations against the native CLI.\n"}
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp, responder)
            step = S.step_by_id(S.build_steps(cfg), "7")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.PASS)
            self.assertEqual(rec["browser_mode"], "full")


class TestMissingRealModelFails(unittest.TestCase):
    """Requirement: missing real-model env fails with named variables."""

    def test_preflight_missing_env_fails_with_variables(self):
        res = judge.judge_full_preflight({}, True, [MODEL], {"claude": "2.1.308", "codex-cli": "0.171.0"})
        self.assertEqual(res["status"], judge.FAIL)
        self.assertEqual(res["reason_code"], "missing_evidence")
        self.assertIn("BMEM_V2_TEST_REAL_MODEL", res["reason"])
        self.assertIn("BMEM_V2_TEST_NATIVE_HOSTS", res["reason"])

    def test_runner_missing_env_fails_without_running_discover(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp, host_responder, base_env={"PATH": "/usr/bin"})
            step = S.step_by_id(S.build_steps(cfg), "6")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.FAIL)
            self.assertIn("BMEM_V2_TEST_REAL_MODEL", rec["reason"])
            self.assertFalse(any("unittest" in call for call in ops.calls))

    def test_generic_profile_demotes_to_warn_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp, host_responder, base_env={"PATH": "/usr/bin"}, profile="generic")
            step = S.step_by_id(S.build_steps(cfg), "6")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.WARN)
            self.assertIn("BMEM_V2_TEST_REAL_MODEL", rec["reason"])

    def test_model_missing_blocked(self):
        res = judge.judge_full_preflight({"BMEM_V2_TEST_REAL_MODEL": MODEL}, True, ["other-model"],
                                         {"claude": "2.1.308", "codex-cli": "0.171.0"})
        self.assertEqual(res["status"], judge.BLOCKED)

    def test_host_version_mismatch_blocked(self):
        res = judge.judge_full_preflight({"BMEM_V2_TEST_REAL_MODEL": MODEL}, True, [MODEL],
                                         {"claude": "2.1.308", "codex-cli": "0.167.0"})
        self.assertEqual(res["status"], judge.BLOCKED)
        self.assertIn("0.167.0", res["reason"])

    def test_preflight_pass(self):
        res = judge.judge_full_preflight({"BMEM_V2_TEST_REAL_MODEL": MODEL}, True, [MODEL],
                                         {"claude": "2.1.308", "codex-cli": "0.171.0"})
        self.assertEqual(res["status"], judge.PASS)


class TestFingerprintsRecorded(unittest.TestCase):
    """Requirement: HEAD and binary SHA-256 land in the evidence records."""

    def test_build_step_records_head_and_binary_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp, lambda argv, env: {})
            binary = cfg.binary_path
            os.makedirs(os.path.dirname(binary), exist_ok=True)
            payload = b"fake bmem binary bytes"
            with open(binary, "wb") as handle:
                handle.write(payload)
            step = S.step_by_id(S.build_steps(cfg), "3")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.PASS)
            self.assertEqual(rec["head"], FAKE_HEAD)
            self.assertEqual(rec["binary_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(runner.binaries["bmem"]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(rec["binary"]["path"], binary)

    def test_stale_binary_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            def responder(argv, env):
                if argv and argv[0] == "find":
                    return {"stdout": "src/v2/web/app.js\n"}
                return {}
            runner, ops, cfg = make_runner(tmp, responder)
            binary = cfg.binary_path
            os.makedirs(os.path.dirname(binary), exist_ok=True)
            with open(binary, "wb") as handle:
                handle.write(b"x")
            step = S.step_by_id(S.build_steps(cfg), "3")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.FAIL)
            self.assertIn("newer", rec["reason"])

    def test_document_carries_head_and_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            step = S.build_steps(cfg)[1]
            rec = evidence.step_record(step, [["cargo", "fmt"]], 0, 3, "out", "",
                                       judge.PASS, "", FAKE_HEAD, "a" * 64)
            doc = evidence.build_document("rid12345", evidence.timestamp(), cfg, FAKE_HEAD,
                                          "fix/test", S.resolve_jobs(2, 1.0, 8.0),
                                          {"bmem": {"path": "/x", "sha256": "a" * 64}},
                                          [rec], True, False, {})
            self.assertEqual(doc["head"], FAKE_HEAD)
            self.assertEqual(doc["binaries"]["bmem"]["sha256"], "a" * 64)
            self.assertEqual(doc["steps"][0]["head"], FAKE_HEAD)
            self.assertEqual(doc["steps"][0]["binary_sha256"], "a" * 64)
            schema = json.loads(open(os.path.join(_HERE, "schemas", "gates.schema.json"), encoding="utf-8").read())
            for key in schema["required"]:
                self.assertIn(key, doc)
            for key in schema["properties"]["steps"]["items"]["required"]:
                self.assertIn(key, doc["steps"][0])

    def test_run_writes_evidence_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tests", "v2"), exist_ok=True)
            cfg = make_cfg(tmp)
            ops = FakeOps(tmp, lambda argv, env: {})
            runner = run_gates.Runner(cfg, ops, base_env={})
            with redirect_stdout(io.StringIO()):
                code = runner.run(only="fmt")
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(os.path.join(cfg.artifacts_dir, "gates.json")))
            self.assertTrue(os.path.exists(os.path.join(cfg.artifacts_dir, "validation_numbers.json")))
            doc = json.loads(open(os.path.join(cfg.artifacts_dir, "gates.json"), encoding="utf-8").read())
            self.assertEqual(doc["head"], FAKE_HEAD)
            self.assertIn("binary_sha256", doc["steps"][0])
            self.assertTrue(doc["all_pass"])
            self.assertEqual(len(os.listdir(cfg.evidence_dir)), 1)

    def test_failed_step_exit_code_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tests", "v2"), exist_ok=True)
            cfg = make_cfg(tmp)
            ops = FakeOps(tmp, lambda argv, env: {"exit": 1, "stderr": "error: lint"})
            runner = run_gates.Runner(cfg, ops, base_env={})
            with redirect_stdout(io.StringIO()):
                code = runner.run(only="clippy")
            self.assertEqual(code, 1)


class TestStepTableAndCommands(unittest.TestCase):
    def test_step_table_is_plan_0_to_13(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = S.build_steps(make_cfg(tmp))
            self.assertEqual([st.sid for st in table], list(range(14)))
            self.assertEqual([st.name for st in table], [
                "cleanup-precheck", "fmt", "clippy", "build", "cargo-test",
                "unittest-baseline", "unittest-full", "browser", "node-check",
                "git-diff-check", "doc-baseline", "executors", "boundaries",
                "cleanup-apply"])

    def test_cold_run_cleans_only_bmem_crate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            step = S.step_by_id(S.build_steps(cfg), "2")
            clean = step.commands[0]["argv"]
            self.assertIn("clean", clean)
            self.assertEqual(clean[clean.index("-p") + 1], "bmem")
            self.assertNotIn("--jobs", clean)  # cargo clean does not compile
            clippy = step.commands[1]["argv"]
            for flag in ("--locked", "--all-targets", "--no-deps", "-D"):
                self.assertIn(flag, clippy)
            self.assertEqual(clippy[clippy.index("--jobs") + 1], "2")
            self.assertIn("warnings", clippy)

    def test_full_clean_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, full_clean=True)
            clean = S.step_by_id(S.build_steps(cfg), "2").commands[0]["argv"]
            self.assertNotIn("-p", clean)

    def test_build_before_cargo_test_and_manifest_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            table = S.build_steps(cfg)
            self.assertLess(table[3].sid, table[4].sid)
            verbs = ("clean", "clippy", "build", "test")
            for st in table:
                for cmd in st.commands:
                    if cmd["argv"] and cmd["argv"][0] == cfg.cargo and "fmt" not in cmd["argv"]:
                        self.assertIn("--manifest-path", cmd["argv"])
                        self.assertEqual(cmd["argv"][cmd["argv"].index("--manifest-path") + 1],
                                         "src/v2/bmem/Cargo.toml")
                        # cargo only accepts options after the subcommand: an
                        # argv starting cargo --manifest-path <verb> aborts with
                        # "unexpected argument '--manifest-path'" in ~12ms, which
                        # is exactly the failure a real assembly run hit once.
                        idx = cmd["argv"].index("--manifest-path")
                        self.assertGreater(idx, 1)
                        self.assertIn(cmd["argv"][1], verbs)
                        self.assertNotIn("--manifest-path", cmd["argv"][1:idx])

    def test_cargo_env_sets_target_dir_and_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            env = cfg.cargo_env({"PATH": "/usr/bin"})
            self.assertEqual(env["CARGO_TARGET_DIR"], cfg.target_dir)
            self.assertEqual(env["CARGO_BUILD_JOBS"], "2")

    def test_jobs_demotion_above_threshold(self):
        info = S.resolve_jobs(2, 9.5, 8.0)
        self.assertEqual(info["effective"], 1)
        self.assertTrue(info["demoted"])
        info = S.resolve_jobs(2, 3.0, 8.0)
        self.assertEqual(info["effective"], 2)
        self.assertFalse(info["demoted"])

    def test_discover_env_baseline_strips_optins(self):
        base = {"BMEM_V2_TEST_REAL_MODEL": MODEL, "BMEM_V2_TEST_NATIVE_HOSTS": "1"}
        env = S.discover_env(base, "/bin/bmem", "baseline")
        self.assertNotIn("BMEM_V2_TEST_REAL_MODEL", env)
        self.assertNotIn("BMEM_V2_TEST_NATIVE_HOSTS", env)
        self.assertEqual(env["BMEM_V2_TEST_BIN"], "/bin/bmem")
        full = S.discover_env(base, "/bin/bmem", "full", MODEL)
        self.assertEqual(full["BMEM_V2_TEST_REAL_MODEL"], MODEL)
        self.assertEqual(full["BMEM_V2_TEST_NATIVE_HOSTS"], "1")

    def test_node_check_covers_web_js_and_cjs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = S.node_check_commands(make_cfg(tmp), ["src/v2/web/app.js"], ["tests/v2/browser_smoke.cjs"])
            self.assertEqual(len(cmds), 2)
            for cmd in cmds:
                self.assertEqual(cmd["argv"][:2], ["node", "--check"])


class TestParsersAndJudges(unittest.TestCase):
    def test_parse_unittest_3_11_id_format(self):
        text = ("test_a (test_x.Cls.test_a) ... skipped 'why'\n"
                "Ran 1 tests in 0.0s\nOK (skipped=1)\n")
        parsed = judge.parse_unittest_output(text)
        self.assertEqual(parsed["skipped_ids"], ["test_x.Cls.test_a"])
        self.assertEqual(parsed["skipped"], 1)

    def test_parse_cargo_test_sums_targets(self):
        text = ("test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
                "test result: ok. 7 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n")
        parsed = judge.parse_cargo_test(text)
        self.assertEqual(parsed["passed"], 12)
        self.assertEqual(parsed["failed"], 1)
        self.assertEqual(parsed["targets"], 2)
        self.assertEqual(judge.judge_cargo_test(parsed, 0)["status"], judge.FAIL)

    def test_clippy_requires_checking_bmem(self):
        self.assertEqual(judge.judge_clippy(0, "Checking bmem v0.2.0\n")["status"], judge.PASS)
        self.assertEqual(judge.judge_clippy(0, "Finished dev\n")["status"], judge.FAIL)
        self.assertEqual(judge.judge_clippy(101, "Checking bmem\n")["status"], judge.FAIL)

    def test_doc_drift_guard(self):
        self.assertEqual(judge.judge_doc_drift(True, False)["status"], judge.FAIL)
        self.assertEqual(judge.judge_doc_drift(True, True)["status"], judge.PASS)
        self.assertEqual(judge.judge_doc_drift(False, False)["status"], judge.PASS)

    def test_boundary_reconcile(self):
        recon = judge.reconcile_boundaries(
            ["src/v2/bmem/src/server.rs", "rogue.txt", "tests/v2/artifacts/gates.json"],
            {"src/v2/bmem/src/server.rs"})
        self.assertEqual(recon["offenders"], ["rogue.txt"])
        self.assertEqual(recon["excluded_evidence"], ["tests/v2/artifacts/gates.json"])
        self.assertEqual(judge.judge_boundaries(recon, 64, 64)["status"], judge.FAIL)
        recon_ok = judge.reconcile_boundaries(["src/v2/bmem/src/server.rs"], {"src/v2/bmem/src/server.rs"})
        self.assertEqual(judge.judge_boundaries(recon_ok, 64, 64)["status"], judge.PASS)
        self.assertEqual(judge.judge_boundaries(recon_ok, 64, 65)["status"], judge.FAIL)
        self.assertEqual(judge.judge_boundaries(recon_ok, 64, None)["status"], judge.FAIL)
        self.assertEqual(judge.judge_boundaries(recon_ok, None, 64)["status"], judge.FAIL)

    def test_conflict_count_argv_order(self):
        # git grep dies if --untracked comes after the pattern (verified);
        # the real Ops.conflict_count must put options before the pattern.
        class GreppingOps(FakeOps):
            def conflict_count(self, ref=None):
                return run_gates.Ops.conflict_count(self, ref)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            ops = GreppingOps(tmp, lambda argv, env: {"exit": 1, "stdout": ""})
            runner = run_gates.Runner(cfg, ops, base_env={})
            runner.head = FAKE_HEAD
            runner.changed = []
            runner.collection = fake_collection()
            step = S.step_by_id(S.build_steps(cfg), "12")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.PASS)
            grep_calls = [c for c in ops.calls if c[:2] == ["git", "grep"]]
            self.assertEqual(len(grep_calls), 2)
            for call in grep_calls:
                # -e <pattern> first, then revision/--untracked, then --
                self.assertEqual(call[call.index("-e") + 1], "Error::conflict(")
                marker = "--untracked" if "--untracked" in call else "HEAD"
                self.assertLess(call.index("Error::conflict("), call.index(marker))
                self.assertLess(call.index(marker), call.index("--"))

    def test_declared_files_from_patch_shapes(self):
        data = {"file_path": "a.rs", "patches": [{"file_path": "b.rs"}, {"files": ["c.rs"]}]}
        self.assertEqual(judge.declared_files_from_patch(data), {"a.rs", "b.rs", "c.rs"})

    def test_executors_judgment(self):
        good = {"executors": [
            {"name": "claude", "version": "2.1.308", "status": "available"},
            {"name": "codex", "version": "0.171.0", "status": "available"},
            {"name": "dsh", "version": "0.2.2-rc.2", "status": "available"}]}
        self.assertEqual(judge.judge_executors(good)["status"], judge.PASS)
        bad = {"executors": [{"name": "claude", "version": "2.1.308", "status": "available"}]}
        self.assertEqual(judge.judge_executors(bad)["status"], judge.FAIL)

    def test_executors_verdict_format_from_probe_script(self):
        # scripts/probe_agents.py records tiers as "verdict", not "status".
        probe = {"executors": [
            {"name": "claude", "version": "2.1.308 (Claude Code)", "verdict": "can_write_code"},
            {"name": "codex", "version": "codex-cli 0.171.0", "verdict": "review_only"},
            {"name": "dsh", "version": "0.2.2-rc.2", "verdict": "review_only"},
            {"name": "agy", "version": "1.5.15", "verdict": "unavailable"}]}
        res = judge.judge_executors(probe)
        self.assertEqual(res["status"], judge.PASS)
        self.assertEqual(res["executors"]["codex"]["status"], "review_only")

    def test_runner_executors_step_reads_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp)
            record = {"executors": [
                {"name": "claude", "version": "2.1.308", "verdict": "can_write_code"},
                {"name": "codex", "version": "0.171.0", "verdict": "review_only"},
                {"name": "dsh", "version": "0.2.2-rc.2", "verdict": "review_only"}]}
            ops.files[os.path.join(cfg.artifacts_dir, "executors.json")] = json.dumps(record)
            step = S.step_by_id(S.build_steps(cfg), "11")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.PASS)
            self.assertEqual(rec["source"], "tests/v2/artifacts/executors.json")

    def test_runner_executors_missing_blocked_in_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, ops, cfg = make_runner(tmp)
            step = S.step_by_id(S.build_steps(cfg), "11")
            rec = runner.execute(step)
            self.assertEqual(rec["status"], judge.BLOCKED)
            self.assertIn("executors.json", rec["reason"])

    def test_all_pass_rules(self):
        results = {6: {"status": judge.PASS}, 7: {"status": judge.WARN}}
        self.assertTrue(judge.compute_all_pass(results, "assembly", False))
        self.assertFalse(judge.compute_all_pass(results, "assembly", True))
        results[7]["status"] = judge.PASS
        self.assertTrue(judge.compute_all_pass(results, "assembly", True))
        results[6]["status"] = judge.BLOCKED
        self.assertFalse(judge.compute_all_pass(results, "assembly", False))
        self.assertTrue(judge.compute_all_pass({5: {"status": judge.WARN}}, "generic", False))

    def test_p0_evidence_complete(self):
        self.assertFalse(judge.p0_evidence_complete(judge.PASS, None))
        self.assertFalse(judge.p0_evidence_complete(judge.FAIL, {}))
        receipts = {"hosts": {
            "claude-code": {"provider_mode": "real_local_model", "loaded_state": "observed",
                            "used_evidence_kind": "model_output_assertion",
                            "outcome_verifier": "codex", "attempt_subject": "claude-code"},
            "codex": {"provider_mode": "real_local_model", "loaded_state": "observed"}}}
        self.assertTrue(judge.p0_evidence_complete(judge.PASS, receipts))
        receipts["hosts"]["codex"]["loaded_state"] = "unknown"
        self.assertFalse(judge.p0_evidence_complete(judge.PASS, receipts))

    def test_validation_numbers_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            table = S.build_steps(cfg)
            recs = [
                evidence.step_record(table[4], [["cargo", "test"]], 0, 10, "", "", judge.PASS, "", FAKE_HEAD, None,
                                     {"passed": 12, "failed": 0, "ignored": 0, "targets": 2}),
                evidence.step_record(table[5], [["python3", "-m", "unittest"]], 0, 10, "", "", judge.PASS, "", FAKE_HEAD, None,
                                     {"tests_run": 40, "skipped": 1, "ast_total": 40, "native_hosts_count": 5}),
            ]
            doc = evidence.build_document("rid12345", evidence.timestamp(), cfg, FAKE_HEAD, "b",
                                          S.resolve_jobs(2, 1.0, 8.0), {}, recs, True, False, {})
            numbers = evidence.validation_numbers(doc)
            self.assertEqual(numbers["rust"]["passed"], 12)
            self.assertEqual(numbers["python_baseline"]["tests_run"], 40)
            self.assertEqual(numbers["native_hosts_collected"], 5)
            self.assertEqual(numbers["assembly_commit"], FAKE_HEAD)
            self.assertIn("hand-filling forbidden", numbers["source"])


class TestCli(unittest.TestCase):
    def test_list_and_dry_run_exit_zero(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_gates.main(["--list"]), 0)
        text = out.getvalue()
        self.assertIn("browser_smoke.cjs", text)
        self.assertIn("clean -p bmem", text.replace("  ", " "))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_gates.main(["--dry-run"]), 0)
        self.assertIn("0 commands executed", out.getvalue())

    def test_dry_run_only_filters_to_selected_step(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_gates.main(["--only", "2", "--dry-run"]), 0)
        text = out.getvalue()
        self.assertIn("step  2", text)
        self.assertNotIn("step  13", text)
        self.assertIn("clean -p bmem", text)

    def test_argument_errors_exit_two(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                run_gates.main(["--only", "no-such-step", "--dry-run"])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(SystemExit) as ctx:
                run_gates.main(["--jobs", "0"])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(SystemExit) as ctx:
                run_gates.main(["--profile", "bogus"])
            self.assertEqual(ctx.exception.code, 2)

    def test_only_selects_single_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = S.build_steps(make_cfg(tmp))
            self.assertEqual(S.step_by_id(table, "clippy").sid, 2)
            self.assertEqual(S.step_by_id(table, "6").sid, 6)
            self.assertEqual(S.step_by_id(table, "unittest-full").sid, 6)
            self.assertIsNone(S.step_by_id(table, "99"))


if __name__ == "__main__":
    unittest.main()
