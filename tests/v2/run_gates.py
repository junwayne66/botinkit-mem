#!/usr/bin/env python3
"""T044-A gate runner for bmem v2 (plan.md section 5, steps 0-13).

Executable entry:
    python3 tests/v2/run_gates.py --profile assembly
    python3 tests/v2/run_gates.py --list | --dry-run | --selftest
    python3 tests/v2/run_gates.py --only clippy --jobs 2 --target-dir target-gate

Spec source (do not invent requirements beyond it):
    specs/005-agent-mcp-interface/plan.md sections 3, 5, 8
    specs/005-agent-mcp-interface/tasks.md T044

Hard rules implemented here:
  * Step order is the plan's: cleanup dry-run -> fmt -> clean -p bmem + clippy
    -> build -> cargo test -> baseline discover -> full-evidence discover ->
    browser (browser_smoke.cjs ONLY entry) -> node --check -> git diff --check
    -> committed doc baseline -> executor probe record -> boundary reconcile
    -> cleanup apply.
  * Any unexpected unittest skip fails; the full-evidence discover requires
    skipped == 0. Missing BMEM_V2_TEST_REAL_MODEL fails with "missing
    evidence" and names the variables instead of passing silently.
  * Cold run cleans only the bmem crate (cargo clean -p bmem); full clean is
    opt-in (--full-clean) for the three plan-sanctioned cases.
  * Every cargo invocation passes --jobs explicitly (default 2, advisory
    loadavg>8 demotion to 1) and CARGO_TARGET_DIR=<target-dir>.
  * Each step records command, exit, key output lines, duration, binary
    SHA-256 and worktree HEAD into evidence/gates/<timestamp>.json (gitignored
    local evidence) plus tests/v2/artifacts/gates.json + validation_numbers.json.
  * Exit codes: 0 all green, 1 any FAIL/BLOCKED, 2 argument error.

This file is intentionally NOT named test*.py so unittest discover does not
collect it (plan section 5). Python 3.9 compatible, standard library only.
No git write operations are ever performed by this runner.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from gates import evidence, judge, steps as S  # noqa: E402


class Ops(object):
    """Real side-effect surface. Unit tests inject fakes in place of this."""

    def __init__(self, repo_root):
        self.repo_root = repo_root

    def run(self, argv, env=None, timeout=S.SHORT_TIMEOUT, cwd=None):
        # type: (List[str], Optional[Dict], int, Optional[str]) -> Dict
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=cwd or self.repo_root, env=env,
                capture_output=True, text=True,
                # The gate itself runs under nohup/CI, where the inherited stdin
                # is an open pipe that never closes.  No step command consumes
                # stdin (cargo/git/shasum/find, node --check, unittest, the host
                # --version probes), so hand every child EOF instead of the
                # parent's stdin: this is the defect class that made codex block
                # on "Reading additional input from stdin..." and masquerade as
                # an external host CLI tail on 2026-11-24.
                stdin=subprocess.DEVNULL, timeout=timeout)
            return {"exit": proc.returncode, "stdout": proc.stdout or "",
                    "stderr": proc.stderr or "",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "timed_out": False}
        except subprocess.TimeoutExpired as exc:
            def _text(value):
                if isinstance(value, bytes):
                    return value.decode("utf-8", "replace")
                return value or ""
            return {"exit": None, "stdout": _text(exc.output),
                    "stderr": _text(exc.stderr) + "\nTIMEOUT after %ss" % timeout,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "timed_out": True}
        except FileNotFoundError:
            return {"exit": 127, "stdout": "", "stderr": "binary not found: %s" % argv[0],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "timed_out": False}

    def git_head(self):
        # type: () -> Optional[str]
        res = self.run(["git", "rev-parse", "HEAD"], timeout=30)
        return res["stdout"].strip() if res["exit"] == 0 else None

    def git_branch(self):
        # type: () -> Optional[str]
        res = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=30)
        return res["stdout"].strip() if res["exit"] == 0 else None

    def changed_files(self):
        # type: () -> List[str]
        out = []  # type: List[str]
        res = self.run(["git", "diff", "--name-only", "HEAD"], timeout=60)
        if res["exit"] == 0:
            out += [ln for ln in res["stdout"].splitlines() if ln.strip()]
        res = self.run(["git", "ls-files", "--others", "--exclude-standard"], timeout=60)
        if res["exit"] == 0:
            out += [ln for ln in res["stdout"].splitlines() if ln.strip()]
        return sorted(set(out))

    def head_blob(self, path):
        # type: (str) -> Optional[str]
        res = self.run(["git", "show", "HEAD:%s" % path], timeout=30)
        return res["stdout"] if res["exit"] == 0 else None

    def conflict_count(self, ref=None):
        # type: (Optional[str]) -> Optional[int]
        # All options (incl. -e <pattern>) must precede any revision: git
        # grep treats the first non-option as pattern/rev start and then
        # misreads later flags as revisions (both failure modes verified).
        argv = ["git", "grep", "-c", "-F", "-e", "Error::conflict("]
        if ref:
            argv.append(ref)
        else:
            argv.append("--untracked")
        argv += ["--", "src/v2/bmem/src"]
        res = self.run(argv, timeout=60)
        if res["exit"] not in (0, 1):  # 1 = no matches, a legitimate zero
            return None
        total = 0
        for line in res["stdout"].splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) == 2 and parts[1].isdigit():
                total += int(parts[1])
        return total

    def http_get_json(self, url, timeout=10):
        # type: (str, int) -> Dict
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return {"ok": True, "data": json.loads(resp.read().decode("utf-8"))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def which(self, name):
        # type: (str) -> Optional[str]
        return shutil.which(name)

    def loadavg(self):
        # type: () -> Optional[float]
        try:
            return os.getloadavg()[0]
        except (OSError, AttributeError):
            return None

    def read_text(self, path):
        # type: (str) -> Optional[str]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def exists(self, path):
        # type: (str) -> bool
        return os.path.exists(path)

    def mtime(self, path):
        # type: (str) -> Optional[float]
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def glob(self, pattern):
        # type: (str) -> List[str]
        return sorted(glob.glob(os.path.join(self.repo_root, pattern)))


class Runner(object):
    def __init__(self, cfg, ops, base_env=None):
        # type: (S.Config, Ops, Optional[Dict]) -> None
        self.cfg = cfg
        self.ops = ops
        self.base_env = dict(base_env if base_env is not None else os.environ)
        self.results = {}  # type: Dict[int, Dict]
        self.records = []  # type: List[Dict]
        self.binaries = {}  # type: Dict[str, Dict]
        self.head = None  # type: Optional[str]
        self.branch = None  # type: Optional[str]
        self.web_changed = False
        self.collection = None  # type: Optional[Dict]
        self.jobs_info = {}  # type: Dict
        self.run_id = evidence.new_run_id()
        self.started_at = evidence.timestamp()

    # -- preparation -------------------------------------------------------

    def prepare(self):
        # type: () -> None
        self.head = self.ops.git_head()
        self.branch = self.ops.git_branch()
        self.jobs_info = S.resolve_jobs(self.cfg.jobs, self.ops.loadavg(), self.cfg.max_loadavg)
        self.cfg.jobs_effective = self.jobs_info["effective"]
        changed = self.ops.changed_files()
        self.web_changed = any(p.startswith("src/v2/web/") for p in changed)
        self.changed = changed
        for tool in ("cargo", "node", "python", "git", "shasum"):
            name = getattr(self.cfg, tool)
            path = self.ops.which(name) or (name if os.path.isabs(name) else None)
            self.binaries[tool] = {"path": path, "sha256": evidence.sha256_file(path) if path else None}
        self.collection = judge.collect_ast_tests(os.path.join(self.cfg.repo_root, "tests/v2"))

    def _binary_sha(self):
        # type: () -> Optional[str]
        entry = self.binaries.get("bmem") or {}
        return entry.get("sha256")

    def _fingerprint_bmem(self):
        # type: () -> Dict
        path = self.cfg.binary_path
        entry = {"path": path if self.ops.exists(path) else None,
                 "sha256": evidence.sha256_file(path),
                 "mtime": self.ops.mtime(path)}
        self.binaries["bmem"] = entry
        return entry

    # -- step execution ----------------------------------------------------

    def _run_commands(self, step, env_kind):
        # type: (S.Step, str) -> Dict
        env = dict(self.base_env)
        if env_kind == "cargo":
            env = self.cfg.cargo_env(env)
        elif env_kind == "baseline":
            env = S.discover_env(env, self.cfg.binary_path, "baseline")
        elif env_kind == "full":
            env = S.discover_env(env, self.cfg.binary_path, "full",
                                 self.base_env.get("BMEM_V2_TEST_REAL_MODEL"))
        elif env_kind == "browser":
            env = S.browser_env(env, self.cfg.binary_path)
        argvs = []  # type: List[List[str]]
        stdout = stderr = ""
        exit_code = 0  # type: Optional[int]
        duration = 0
        timed_out = False
        for cmd in step.commands:
            argvs.append(cmd["argv"])
            res = self.ops.run(cmd["argv"], env=env, timeout=step.timeout)
            stdout += res["stdout"]
            stderr += res["stderr"]
            duration += res["duration_ms"]
            exit_code = res["exit"]
            timed_out = timed_out or res["timed_out"]
            if res["exit"] != 0:
                break
        return {"argvs": argvs, "stdout": stdout, "stderr": stderr,
                "exit": exit_code, "duration_ms": duration, "timed_out": timed_out,
                "env": env}

    def _record(self, step, run_info, status, reason, extra=None):
        # type: (S.Step, Dict, str, str, Optional[Dict]) -> Dict
        rec = evidence.step_record(
            step, run_info.get("argvs", []), run_info.get("exit"),
            run_info.get("duration_ms", 0), run_info.get("stdout", ""),
            run_info.get("stderr", ""), status, reason,
            self.ops.git_head(), self._binary_sha(), extra)
        self.results[step.sid] = rec
        self.records.append(rec)
        return rec

    def _missing_infra(self, step, what):
        # type: (S.Step, str) -> Dict
        if self.cfg.profile == "assembly":
            return self._record(step, {}, judge.BLOCKED,
                                "missing evidence: %s (T044 sibling deliverable)" % what)
        return self._record(step, {}, judge.WARN,
                            "missing (generic profile): %s" % what)

    def execute(self, step):
        # type: (S.Step) -> Dict
        handler = getattr(self, "_step_" + step.kind)
        return handler(step)

    def _step_cleanup_dry(self, step):
        return self._cleanup(step, "--dry-run")

    def _step_cleanup_apply(self, step):
        return self._cleanup(step, "--apply")

    def _cleanup(self, step, flag):
        script = os.path.join(self.cfg.repo_root, "scripts/cleanup_worktrees.py")
        if not self.ops.exists(script):
            return self._missing_infra(step, "scripts/cleanup_worktrees.py")
        info = self._run_commands(step, "plain")
        status = judge.PASS if info["exit"] == 0 else judge.FAIL
        return self._record(step, info, status,
                            "" if status == judge.PASS else "cleanup_worktrees exit %s" % info["exit"])

    def _step_fmt(self, step):
        info = self._run_commands(step, "plain")
        status = judge.PASS if info["exit"] == 0 else judge.FAIL
        return self._record(step, info, status,
                            "" if status == judge.PASS else "cargo fmt --check exit %s" % info["exit"])

    def _step_clippy(self, step):
        info = self._run_commands(step, "cargo")
        if info["exit"] != 0:
            return self._record(step, info, judge.FAIL, "clean/clippy exit %s" % info["exit"])
        res = judge.judge_clippy(info["exit"], info["stdout"] + info["stderr"])
        return self._record(step, info, res["status"], res["reason"])

    def _step_build(self, step):
        info = self._run_commands(step, "cargo")
        if info["exit"] != 0:
            return self._record(step, info, judge.FAIL, "cargo build exit %s" % info["exit"])
        entry = self._fingerprint_bmem()
        if not entry["sha256"]:
            return self._record(step, info, judge.FAIL,
                                "binary missing after build: %s" % self.cfg.binary_path)
        newer = self.ops.run(["find", "src/v2/bmem/src", "src/v2/web", "-type", "f",
                              "-newer", self.cfg.binary_path], timeout=S.SHORT_TIMEOUT)
        files = [ln for ln in newer["stdout"].splitlines() if ln.strip()]
        res = judge.judge_newer_files(files)
        return self._record(step, info, res["status"], res["reason"],
                            {"binary": entry, "newer_files": files})

    def _step_cargo_test(self, step):
        info = self._run_commands(step, "cargo")
        parsed = judge.parse_cargo_test(info["stdout"] + info["stderr"])
        res = judge.judge_cargo_test(parsed, info["exit"])
        extra = {k: parsed[k] for k in ("passed", "failed", "ignored", "targets")}
        return self._record(step, info, res["status"], res["reason"], extra)

    def _discover(self, step, mode):
        if not self.ops.exists(self.cfg.binary_path):
            return self._record(step, {}, judge.BLOCKED,
                                "missing evidence: %s (run step 3 build first)" % self.cfg.binary_path)
        strict = mode == "full"
        if strict:
            pre = self._full_preflight(step)
            if pre is not None:
                return pre
        info = self._run_commands(step, mode)
        parsed = judge.parse_unittest_output(info["stdout"] + info["stderr"])
        env = info["env"]
        res = judge.judge_unittest(parsed, info["exit"], self.collection, env,
                                   os.name, strict_zero_skips=strict)
        extra = {"tests_run": parsed["tests_run"], "skipped": parsed["skipped"],
                 "skipped_ids": parsed["skipped_ids"],
                 "ast_total": self.collection["ast_total"],
                 "native_hosts_count": self.collection["native_hosts_count"],
                 "mode": mode}
        if strict:
            extra["host_versions"] = getattr(self, "_host_versions", None)
            extra["model_id"] = self.base_env.get("BMEM_V2_TEST_REAL_MODEL")
        # Failures carry a cause code so a reader can tell an external host CLI
        # tail from a bmem defect without re-reading the log.  The verdict does
        # not change: both stay FAIL.
        if res.get("reason_code"):
            extra["reason_code"] = res["reason_code"]
        return self._record(step, info, res["status"], res["reason"], extra)

    def _step_discover_baseline(self, step):
        return self._discover(step, "baseline")

    def _step_discover_full(self, step):
        return self._discover(step, "full")

    def _full_preflight(self, step):
        # type: (S.Step) -> Optional[Dict]
        tags = self.ops.http_get_json(S.OLLAMA_TAGS_URL, timeout=10)
        models = []
        if tags["ok"]:
            models = [m.get("name", "") for m in (tags.get("data") or {}).get("models", [])]
        host_versions = {}  # type: Dict[str, Optional[str]]
        # (binary name, pinned-matrix key, fallback path) — matrix keys follow
        # plan section 5 step 6: claude 2.1.308 / codex-cli 0.171.0.
        for binary_name, matrix_key, fallback in (
                ("claude", "claude", "~/.local/bin/claude"),
                ("codex", "codex-cli", "~/.hermes/node/bin/codex")):
            path = self.ops.which(binary_name) or os.path.expanduser(fallback)
            version = None
            if self.ops.exists(path):
                res = self.ops.run([path, "--version"], timeout=30)
                match = re.search(r"(\d+\.\d+\.\d+)", res["stdout"] + res["stderr"])
                version = match.group(1) if match else None
                self.binaries["host_" + binary_name] = {
                    "path": path, "sha256": evidence.sha256_file(path), "version": version}
            host_versions[matrix_key] = version
        self._host_versions = host_versions
        res = judge.judge_full_preflight(self.base_env, tags["ok"], models, host_versions)
        if res["status"] == judge.PASS:
            return None
        if self.cfg.profile == "generic" and res["status"] == judge.FAIL:
            # CONTRIBUTING.md:55 opt-in policy applies only to generic local
            # runs; assembly keeps the strict missing-evidence FAIL.
            return self._record(step, {}, judge.WARN,
                                "generic opt-in policy: " + res["reason"],
                                {"required_env": res.get("required_env")})
        return self._record(step, {}, res["status"], res["reason"],
                            {"required_env": res.get("required_env"),
                             "host_versions": host_versions,
                             "ollama_models": models})

    def _step_browser(self, step):
        probe = ("const mod = process.env.BMEM_V2_PLAYWRIGHT_MODULE ?? 'playwright';"
                 "const { chromium } = require(mod);"
                 "chromium.launch({ channel: 'chrome', headless: true })"
                 ".then(b => b.close()).then(() => console.log('browser-probe-ok'))"
                 ".catch(e => { console.error(String(e && e.message || e)); process.exit(1); });")
        env = S.browser_env(dict(self.base_env), self.cfg.binary_path)
        pres = self.ops.run([self.cfg.node, "-e", probe], env=env, timeout=60)
        if pres["exit"] != 0 or "browser-probe-ok" not in pres["stdout"]:
            reason = "playwright/chromium unavailable: %s" % (pres["stderr"].strip()[:300] or "probe failed")
            if self.cfg.profile == "generic":
                return self._record(step, {"argvs": [[self.cfg.node, "-e", "<browser probe>"]],
                                           "exit": pres["exit"], "duration_ms": pres["duration_ms"],
                                           "stdout": pres["stdout"], "stderr": pres["stderr"]},
                                    judge.WARN, reason + "; browser_mode=syntax_only (node --check is step 8)",
                                    {"browser_mode": "syntax_only"})
            status = judge.BLOCKED if self.web_changed else judge.WARN
            return self._record(step, {"argvs": [[self.cfg.node, "-e", "<browser probe>"]],
                                       "exit": pres["exit"], "duration_ms": pres["duration_ms"],
                                       "stdout": pres["stdout"], "stderr": pres["stderr"]},
                                status, reason, {"browser_mode": "syntax_only"})
        if not self.ops.exists(self.cfg.binary_path):
            return self._record(step, {}, judge.BLOCKED,
                                "missing evidence: %s (run step 3 build first)" % self.cfg.binary_path)
        info = self._run_commands(step, "browser")
        argv = step.commands[0]["argv"]
        res = judge.judge_browser_run(argv, info["exit"], info["stdout"])
        return self._record(step, info, res["status"], res["reason"],
                            {"browser_mode": res.get("browser_mode", "full")})

    def _step_node_check(self, step):
        web_js = [os.path.relpath(p, self.cfg.repo_root) for p in self.ops.glob("src/v2/web/*.js")]
        cjs = [os.path.relpath(p, self.cfg.repo_root) for p in self.ops.glob("tests/v2/*.cjs")]
        step.commands = S.node_check_commands(self.cfg, web_js, cjs)
        if not step.commands:
            return self._record(step, {}, judge.FAIL, "no js/cjs files found for node --check")
        # Check EVERY file even after a failure so the evidence lists all
        # syntax errors, not just the first (plan step 8 covers all files).
        argvs, stdout, stderr = [], "", ""
        exit_code = 0  # type: Optional[int]
        duration = 0
        failed_files = []  # type: List[str]
        for cmd in step.commands:
            argvs.append(cmd["argv"])
            res = self.ops.run(cmd["argv"], env=dict(self.base_env), timeout=step.timeout)
            stdout += res["stdout"]
            stderr += res["stderr"]
            duration += res["duration_ms"]
            if res["exit"] != 0:
                exit_code = res["exit"] if exit_code == 0 else exit_code
                failed_files.append(cmd["argv"][-1])
        info = {"argvs": argvs, "stdout": stdout, "stderr": stderr,
                "exit": exit_code, "duration_ms": duration, "timed_out": False}
        status = judge.PASS if exit_code == 0 else judge.FAIL
        return self._record(step, info, status,
                            "" if status == judge.PASS else "node --check failed: %s" % failed_files,
                            {"files_checked": len(step.commands), "failed_files": failed_files})

    def _step_git_diff_check(self, step):
        info = self._run_commands(step, "plain")
        status = judge.PASS if info["exit"] == 0 else judge.FAIL
        return self._record(step, info, status,
                            "" if status == judge.PASS else "git diff --check exit %s" % info["exit"])

    def _step_doc_baseline(self, step):
        path = os.path.join(self.cfg.repo_root, S.DOC_BASELINE)
        working = self.ops.read_text(path)
        if working is None:
            return self._record(step, {}, judge.FAIL, "missing doc baseline %s" % S.DOC_BASELINE)
        committed = self.ops.head_blob(S.DOC_BASELINE)
        baseline_changed = committed is not None and working != committed
        tech_changed = any(p.startswith("docs/v2/technical/") for p in getattr(self, "changed", []))
        drift = judge.judge_doc_drift(baseline_changed, tech_changed)
        if drift["status"] != judge.PASS:
            return self._record(step, {}, drift["status"], drift["reason"])
        info = self._run_commands(step, "plain")
        status = judge.PASS if info["exit"] == 0 else judge.FAIL
        return self._record(step, info, status,
                            "" if status == judge.PASS else "shasum -c exit %s (committed-baseline drift)" % info["exit"],
                            {"baseline_changed_in_diff": baseline_changed,
                             "entries": len([ln for ln in working.splitlines() if ln.strip()])})

    def _step_executors(self, step):
        # Plan-canonical path first; scripts/probe_agents.py (T044 sibling)
        # defaults to evidence/agents/<YYYYMMDD>.json, accepted as fallback.
        path = os.path.join(self.cfg.artifacts_dir, "executors.json")
        text = self.ops.read_text(path)
        if text is None:
            candidates = self.ops.glob("evidence/agents/*.json")
            if candidates:
                path = candidates[-1]
                text = self.ops.read_text(path)
        if text is None:
            return self._missing_infra(
                step, "tests/v2/artifacts/executors.json (or evidence/agents/<date>.json)")
        try:
            data = json.loads(text)
        except ValueError:
            return self._record(step, {}, judge.FAIL, "executors evidence is not valid JSON: %s" % path)
        res = judge.judge_executors(data)
        return self._record(step, {}, res["status"], res["reason"],
                            {"executors": res.get("executors"),
                             "source": os.path.relpath(path, self.cfg.repo_root)})

    def _step_boundaries(self, step):
        declared = set()  # type: set
        sources = []  # type: List[str]
        for patch in self.ops.glob("patches/*.json"):
            text = self.ops.read_text(patch)
            if text is None:
                continue
            try:
                declared |= judge.declared_files_from_patch(json.loads(text))
                sources.append(os.path.relpath(patch, self.cfg.repo_root))
            except ValueError:
                return self._record(step, {}, judge.FAIL, "invalid patch JSON: %s" % patch)
        if self.cfg.boundaries:
            text = self.ops.read_text(self.cfg.boundaries)
            if text is None:
                return self._record(step, {}, judge.FAIL,
                                    "boundaries file not found: %s" % self.cfg.boundaries)
            try:
                declared |= judge.declared_files_from_patch(json.loads(text))
            except ValueError:
                return self._record(step, {}, judge.FAIL,
                                    "invalid boundaries JSON: %s" % self.cfg.boundaries)
            sources.append(self.cfg.boundaries)
        changed = getattr(self, "changed", None)
        if changed is None:
            changed = self.ops.changed_files()
        recon = judge.reconcile_boundaries(changed, declared)
        head_count = self.ops.conflict_count("HEAD")
        work_count = self.ops.conflict_count()
        res = judge.judge_boundaries(recon, head_count, work_count)
        return self._record(step, {}, res["status"], res["reason"],
                            {"reconcile": recon, "declaration_sources": sources,
                             "head_conflicts": head_count, "work_conflicts": work_count})

    # -- orchestration -----------------------------------------------------

    def run(self, only=None):
        # type: (Optional[str]) -> int
        self.prepare()
        all_steps = S.build_steps(self.cfg)
        selected = all_steps
        if only is not None:
            step = S.step_by_id(all_steps, only)
            if step is None:
                raise KeyError(only)
            selected = [step]
        for step in selected:
            self.execute(step)
            rec = self.results[step.sid]
            print("[%2d/13] %-18s %-7s exit=%s %sms%s" % (
                step.sid, step.name, rec["status"], rec["exit"],
                rec["duration_ms"], ("  " + rec["reason"]) if rec["reason"] else ""))
        all_pass = judge.compute_all_pass(self.results, self.cfg.profile, self.web_changed)
        receipts = None
        receipts_path = os.path.join(self.cfg.artifacts_dir, "native_receipts.json")
        text = self.ops.read_text(receipts_path)
        if text:
            try:
                receipts = json.loads(text)
            except ValueError:
                receipts = None
        step6 = self.results.get(6, {}).get("status", judge.FAIL)
        p0 = judge.p0_evidence_complete(step6, receipts)
        summary = {
            "steps_executed": len(selected),
            "pass": sum(1 for r in self.records if r["status"] == judge.PASS),
            "fail": sum(1 for r in self.records if r["status"] == judge.FAIL),
            "blocked": sum(1 for r in self.records if r["status"] == judge.BLOCKED),
            "warn": sum(1 for r in self.records if r["status"] == judge.WARN),
            "web_changed": self.web_changed,
            "changed_files": len(getattr(self, "changed", [])),
        }
        document = evidence.build_document(
            self.run_id, self.started_at, self.cfg, self.head, self.branch,
            self.jobs_info, self.binaries, self.records, all_pass, p0, summary)
        numbers = evidence.validation_numbers(document)
        paths = evidence.write_evidence(document, numbers, self.cfg.evidence_dir, self.cfg.artifacts_dir)
        print("all_pass=%s profile=%s steps=%d fail=%d blocked=%d warn=%d head=%s binary_sha256=%s" % (
            str(all_pass).lower(), self.cfg.profile, summary["steps_executed"],
            summary["fail"], summary["blocked"], summary["warn"],
            (self.head or "unknown")[:12], (self._binary_sha() or "none")[:12]))
        print("evidence=%s gates=%s validation_numbers=%s" % (
            paths["evidence"], paths["gates"], paths["validation_numbers"]))
        return 0 if all_pass else 1


# --------------------------------------------------------------------------
# CLI modes that never run the heavy gates
# --------------------------------------------------------------------------

def _filled_steps(cfg, ops):
    # type: (S.Config, Ops) -> List[S.Step]
    table = S.build_steps(cfg)
    for step in table:
        if step.kind == "node_check":
            web_js = [os.path.relpath(p, cfg.repo_root) for p in ops.glob("src/v2/web/*.js")]
            cjs = [os.path.relpath(p, cfg.repo_root) for p in ops.glob("tests/v2/*.cjs")]
            step.commands = S.node_check_commands(cfg, web_js, cjs)
    return table


def print_plan(cfg, ops, dry_run, only=None):
    # type: (S.Config, Ops, bool, Optional[str]) -> int
    table = _filled_steps(cfg, ops)
    if only is not None:
        selected = S.step_by_id(table, only)
        table = [selected] if selected is not None else table
    mode = "DRY-RUN (no commands will be executed)" if dry_run else "LIST"
    print("run_gates.py %s profile=%s jobs=%d target-dir=%s manifest=%s full_clean=%s" % (
        mode, cfg.profile, cfg.jobs, cfg.target_dir, cfg.manifest_path, cfg.full_clean))
    for step in table:
        print("step %2d %-18s timeout=%ss  # %s" % (step.sid, step.name, step.timeout, step.note))
        for cmd in step.commands:
            print("    [%s] %s" % (cmd["env"], " ".join(cmd["argv"])))
        if not step.commands:
            print("    (in-process check; see step note)")
    if dry_run:
        print("dry-run: 0 commands executed, no evidence written")
    return 0


def selftest(cfg):
    # type: (S.Config) -> int
    import unittest
    ok = True
    table = S.build_steps(cfg)
    ids = [st.sid for st in table]
    if ids != list(range(14)):
        print("selftest FAIL: step table must be plan steps 0-13, got %s" % ids)
        ok = False
    schema_path = os.path.join(_HERE, "gates", "schemas", "gates.schema.json")
    try:
        with open(schema_path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        required = schema["required"]
        sample = evidence.build_document(
            "selftest", evidence.timestamp(), cfg, "0" * 40, "selftest",
            S.resolve_jobs(2, 1.0, 8.0), {}, [], False, False, {})
        missing = [key for key in required if key not in sample]
        if missing:
            print("selftest FAIL: document missing schema-required keys %s" % missing)
            ok = False
        step_required = schema["properties"]["steps"]["items"]["required"]
        rec = evidence.step_record(table[1], [["cargo", "fmt"]], 0, 1, "", "",
                                   judge.PASS, "", "0" * 40, None)
        missing = [key for key in step_required if key not in rec]
        if missing:
            print("selftest FAIL: step record missing schema-required keys %s" % missing)
            ok = False
    except (OSError, ValueError, KeyError) as exc:
        print("selftest FAIL: schema check error: %s" % exc)
        ok = False
    suite = unittest.TestLoader().loadTestsFromName("gates.test_run_gates")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        ok = False
    print("selftest %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_gates.py",
        description="bmem v2 gate runner (T044-A). Implements plan.md section 5 "
                    "steps 0-13. Exit codes: 0 all green, 1 any FAIL/BLOCKED, "
                    "2 argument error.",
        epilog="Cold run cleans only the bmem crate (cargo clean -p bmem). "
               "--full-clean is restricted by plan to R1/R2 exit gates, "
               "Cargo.toml/Cargo.lock changes, or run_gates.py changes.")
    parser.add_argument("--profile", choices=["assembly", "generic"], default="assembly",
                        help="assembly (default): strict gate, step 6 mandatory, any skip fails. "
                             "generic: local dev, CONTRIBUTING.md:55 opt-in demotions.")
    parser.add_argument("--list", dest="list_steps", action="store_true",
                        help="print steps and the exact commands; execute nothing")
    parser.add_argument("--only", metavar="STEP",
                        help="run a single step by number (0-13), name, or kind")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without executing any command or writing evidence")
    parser.add_argument("--selftest", action="store_true",
                        help="run the runner's own unit tests and structural checks (no gates)")
    parser.add_argument("--jobs", type=int, default=S.DEFAULT_JOBS,
                        help="explicit cargo --jobs (default 2; loadavg>threshold demotes to 1)")
    parser.add_argument("--target-dir", default=S.DEFAULT_TARGET_DIR,
                        help="assembly-only CARGO_TARGET_DIR (default target-gate, gitignored)")
    parser.add_argument("--full-clean", action="store_true",
                        help="full cargo clean instead of clean -p bmem (plan-sanctioned cases only)")
    parser.add_argument("--artifacts-dir", default="tests/v2/artifacts",
                        help="plan-canonical artifacts dir (gates.json, validation_numbers.json)")
    parser.add_argument("--evidence-dir", default="evidence/gates",
                        help="timestamped local evidence dir (gitignored /evidence/)")
    parser.add_argument("--boundaries", metavar="JSON",
                        help="extra file-boundary declaration file for step 12")
    parser.add_argument("--max-loadavg", type=float, default=S.DEFAULT_MAX_LOADAVG,
                        help="advisory loadavg threshold for jobs demotion (plan section 8.4)")
    parser.add_argument("--repo-root", default=None,
                        help="repository root (default: parent of tests/v2)")
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    repo_root = args.repo_root or os.path.dirname(os.path.dirname(_HERE))
    cfg = S.Config(
        repo_root, profile=args.profile, jobs=args.jobs, target_dir=args.target_dir,
        full_clean=args.full_clean, artifacts_dir=args.artifacts_dir,
        evidence_dir=args.evidence_dir, boundaries=args.boundaries,
        max_loadavg=args.max_loadavg,
        cargo=shutil.which("cargo") or "cargo",
        node=shutil.which("node") or "node",
        python=sys.executable or "python3",
        git=shutil.which("git") or "git",
        shasum=shutil.which("shasum") or "shasum")
    ops = Ops(cfg.repo_root)
    if args.selftest:
        return selftest(cfg)
    if args.only is not None and S.step_by_id(S.build_steps(cfg), args.only) is None:
        parser.error("unknown step: %s (use 0-13 or a step name from --list)" % args.only)
    if args.list_steps:
        return print_plan(cfg, ops, dry_run=False, only=args.only)
    if args.dry_run:
        return print_plan(cfg, ops, dry_run=True, only=args.only)
    runner = Runner(cfg, ops)
    try:
        return runner.run(only=args.only)
    except KeyError:
        parser.error("unknown step: %s" % args.only)


if __name__ == "__main__":
    sys.exit(main())
