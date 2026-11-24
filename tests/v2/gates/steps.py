"""Step table and command construction for run_gates.py (T044-A).

Authoritative source: specs/005-agent-mcp-interface/plan.md section 5, steps
0-13.  The order below is the plan's order (build BEFORE cargo test, doc
baseline / executor probe / boundary reconcile / cleanup apply included).
Nothing in this module executes commands; it only describes them, so --list
and --dry-run work without cargo, network, or a built binary.

Python 3.9 compatible, standard library only.
"""

import os
from typing import Dict, List, Optional

# plan.md section 5: fixed manifest path and assembly-only target dir.
MANIFEST_PATH = "src/v2/bmem/Cargo.toml"
DEFAULT_TARGET_DIR = "target-gate"
DEFAULT_JOBS = 2
# plan.md section 3.2 / section 8.4: loadavg > 8 is an advisory threshold,
# ASM may adjust it (--max-loadavg); above it jobs drop to 1.
DEFAULT_MAX_LOADAVG = 8.0
# plan.md section 5 step 6: pinned executor matrix (2026-11-23 baseline).
PINNED_HOST_VERSIONS = {"claude": "2.1.308", "codex-cli": "0.171.0"}
PINNED_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
# Documented python test baseline at the T044 freeze (commit 5c44edc,
# 2026-11-24): AST sum of def test_* over the six top-level tests/v2/test_*.py
# files = 39.  specs/004-bmem-v2/validation.md "13 items" is the stale
# 2026-11-19 value that run_gates must replace via validation_numbers.json.
BASELINE_MIN_PY_TESTS = 39
BASELINE_PROVENANCE = "AST count at T044 freeze commit 5c44edc (2026-11-24)"
# plan.md section 5 step 6: full-evidence discover timeout.
FULL_DISCOVER_TIMEOUT = 900
BASELINE_DISCOVER_TIMEOUT = 900
BROWSER_TIMEOUT = 600
CARGO_TIMEOUT = 3600
SHORT_TIMEOUT = 120
CLEANUP_TIMEOUT = 300
# plan.md section 5 step 10: committed doc baseline, 21 entries at freeze.
DOC_BASELINE = "docs/v2/reviews/technical/current.sha256"
# Gate evidence directories are local-only (plan T044 .gitignore entries;
# /evidence/ is already ignored, /tests/v2/artifacts/ lands with the T044
# baseline-commit package).  They are excluded from boundary reconciliation.
EVIDENCE_PREFIXES = ("tests/v2/artifacts/", "evidence/")
BROWSER_ENTRY = "tests/v2/browser_smoke.cjs"
# The six export-only submodules must never be run directly (plan section 5
# step 7; they have no main entry and were the previous false-green source).
BROWSER_SUBMODULES = (
    "browser_dashboard.cjs",
    "browser_data_console.cjs",
    "browser_intake.cjs",
    "browser_integrations.cjs",
    "browser_search.cjs",
    "browser_settings.cjs",
)


class Config(object):
    """Resolved runner configuration (no side effects beyond path resolution)."""

    def __init__(self, repo_root, profile="assembly", jobs=DEFAULT_JOBS,
                 target_dir=DEFAULT_TARGET_DIR, full_clean=False,
                 artifacts_dir="tests/v2/artifacts", evidence_dir="evidence/gates",
                 boundaries=None, max_loadavg=DEFAULT_MAX_LOADAVG,
                 cargo="cargo", node="node", python=None, git="git",
                 shasum="shasum"):
        # type: (str, str, int, str, bool, str, str, Optional[str], float, str, str, Optional[str], str, str) -> None
        self.repo_root = os.path.abspath(repo_root)
        self.profile = profile
        self.jobs = jobs
        self.jobs_effective = jobs
        self.target_dir = target_dir if os.path.isabs(target_dir) else os.path.join(self.repo_root, target_dir)
        self.full_clean = full_clean
        self.artifacts_dir = artifacts_dir if os.path.isabs(artifacts_dir) else os.path.join(self.repo_root, artifacts_dir)
        self.evidence_dir = evidence_dir if os.path.isabs(evidence_dir) else os.path.join(self.repo_root, evidence_dir)
        self.boundaries = boundaries
        self.max_loadavg = max_loadavg
        self.cargo = cargo
        self.node = node
        self.python = python or "python3"
        self.git = git
        self.shasum = shasum
        self.manifest_path = MANIFEST_PATH

    @property
    def binary_path(self):
        # type: () -> str
        # CONTRIBUTING.md:49 and plan step 3 use the debug profile binary.
        return os.path.join(self.target_dir, "debug", "bmem")

    def cargo_base(self):
        # type: () -> List[str]
        return [self.cargo, "--manifest-path", self.manifest_path]

    def cargo_env(self, base_env):
        # type: (Dict[str, str]) -> Dict[str, str]
        env = dict(base_env)
        env["CARGO_TARGET_DIR"] = self.target_dir
        env["CARGO_BUILD_JOBS"] = str(self.jobs_effective)
        return env


class Step(object):
    def __init__(self, sid, name, kind, commands, timeout, note=""):
        # type: (int, str, str, List[Dict], int, str) -> None
        self.sid = sid
        self.name = name
        self.kind = kind
        self.commands = commands  # each: {"argv": [...], "env": "cargo"|"baseline"|"full"|"browser"|"plain"}
        self.timeout = timeout
        self.note = note

    def required(self, profile, web_changed):
        # type: (str, bool) -> bool
        """plan section 5 summary: step 6 is mandatory in assembly; step 7 is
        mandatory in assembly when Web files changed."""
        if self.sid == 6:
            return profile == "assembly"
        if self.sid == 7:
            return profile == "assembly" and web_changed
        return False


def _cmd(argv, env="plain"):
    # type: (List[str], str) -> Dict
    return {"argv": argv, "env": env}


def build_steps(cfg):
    # type: (Config) -> List[Step]
    """The plan section 5 step table, 0-13, in plan order."""
    j = str(cfg.jobs_effective)
    td = ["--target-dir", cfg.target_dir]
    cargo = cfg.cargo_base()
    steps = [
        Step(0, "cleanup-precheck", "cleanup_dry",
             [_cmd([cfg.python, "scripts/cleanup_worktrees.py", "--dry-run"])],
             CLEANUP_TIMEOUT,
             "plan step 0: report stale worktrees (dry-run); apply is step 13"),
        Step(1, "fmt", "fmt",
             [_cmd([cfg.cargo, "fmt", "--check", "--manifest-path", cfg.manifest_path])],
             SHORT_TIMEOUT, "plan step 1: exit 0"),
        Step(2, "clippy", "clippy",
             [_cmd((cargo + ["clean"] + (["-p", "bmem"] if not cfg.full_clean else []) + td), "cargo"),
              _cmd(cargo + ["clippy", "--locked", "--all-targets", "--no-deps"] + td + ["--jobs", j, "--", "-D", "warnings"], "cargo")],
             CARGO_TIMEOUT,
             "plan step 2: clean -p bmem only (full clean only via --full-clean per plan cold-run policy); output must contain 'Checking bmem'"),
        Step(3, "build", "build",
             [_cmd(cargo + ["build", "--locked"] + td + ["--jobs", j], "cargo")],
             CARGO_TIMEOUT,
             "plan step 3: after clean, before test; record binary path+sha256+mtime; find -newer must be empty"),
        Step(4, "cargo-test", "cargo_test",
             [_cmd(cargo + ["test", "--locked"] + td + ["--jobs", j], "cargo")],
             CARGO_TIMEOUT,
             "plan step 4: parse 'N passed; 0 failed; 0 ignored' across all test targets"),
        Step(5, "unittest-baseline", "discover_baseline",
             [_cmd([cfg.python, "-m", "unittest", "discover", "-s", "tests/v2", "-v"], "baseline")],
             BASELINE_DISCOVER_TIMEOUT,
             "plan step 5: testsRun == AST sum; skip set == expected platform/env set; must collect test_native_hosts > 0"),
        Step(6, "unittest-full", "discover_full",
             [_cmd([cfg.python, "-m", "unittest", "discover", "-s", "tests/v2", "-v"], "full")],
             FULL_DISCOVER_TIMEOUT,
             "plan step 6 (assembly mandatory): ollama tags + pinned host versions preflight, then discover with BMEM_V2_TEST_REAL_MODEL + BMEM_V2_TEST_NATIVE_HOSTS=1, skipped == 0"),
        Step(7, "browser", "browser",
             [_cmd([cfg.node, BROWSER_ENTRY], "browser")],
             BROWSER_TIMEOUT,
             "plan step 7: browser_smoke.cjs is the ONLY entry; per-file direct runs are not evidence"),
        Step(8, "node-check", "node_check",
             [],  # filled dynamically from glob at execution/list time
             SHORT_TIMEOUT,
             "plan step 8: node --check every src/v2/web/*.js and tests/v2/*.cjs"),
        Step(9, "git-diff-check", "git_diff_check",
             [_cmd([cfg.git, "diff", "--check"])],
             SHORT_TIMEOUT, "plan step 9: exit 0"),
        Step(10, "doc-baseline", "doc_baseline",
             [_cmd([cfg.shasum, "-a", "256", "-c", DOC_BASELINE])],
             SHORT_TIMEOUT,
             "plan step 10: verify against the COMMITTED baseline; never recompute at gate time"),
        Step(11, "executors", "executors",
             [],
             SHORT_TIMEOUT,
             "plan step 11: artifacts/executors.json probe record must exist (T044 probe deliverable)"),
        Step(12, "boundaries", "boundaries",
             [],
             SHORT_TIMEOUT,
             "plan step 12: declared file sets vs git diff + untracked; Error::conflict( count must not increase"),
        Step(13, "cleanup-apply", "cleanup_apply",
             [_cmd([cfg.python, "scripts/cleanup_worktrees.py", "--apply"])],
             CLEANUP_TIMEOUT,
             "plan step 13: artifacts/worktree_cleanup.json"),
    ]
    return steps


def node_check_commands(cfg, web_js, browser_cjs):
    # type: (Config, List[str], List[str]) -> List[Dict]
    """plan step 8 covers src/v2/web/*.js and tests/v2/*.cjs."""
    return [_cmd([cfg.node, "--check", p]) for p in sorted(web_js) + sorted(browser_cjs)]


def discover_env(base_env, binary, mode, real_model=None):
    # type: (Dict[str, str], str, str, Optional[str]) -> Dict[str, str]
    """Env for the two discover runs.

    baseline: BMEM_V2_TEST_BIN only; real-model / native-host opt-ins are
    stripped so the expected skip set is deterministic (plan step 5).
    full: BMEM_V2_TEST_BIN + operator-provided BMEM_V2_TEST_REAL_MODEL +
    BMEM_V2_TEST_NATIVE_HOSTS=1 (plan step 6).
    """
    env = dict(base_env)
    env.pop("BMEM_V2_TEST_REAL_MODEL", None)
    env.pop("BMEM_V2_TEST_NATIVE_HOSTS", None)
    env["BMEM_V2_TEST_BIN"] = binary
    if mode == "full":
        if real_model:
            env["BMEM_V2_TEST_REAL_MODEL"] = real_model
        env["BMEM_V2_TEST_NATIVE_HOSTS"] = "1"
    return env


def browser_env(base_env, binary):
    # type: (Dict[str, str], str) -> Dict[str, str]
    env = dict(base_env)
    env["BMEM_V2_TEST_BIN"] = binary
    return env


def step_by_id(steps, key):
    # type: (List[Step], str) -> Optional[Step]
    for st in steps:
        if str(st.sid) == str(key) or st.name == key or st.kind == key:
            return st
    return None


def resolve_jobs(requested, loadavg, max_loadavg):
    # type: (int, float, float) -> Dict
    """plan section 3.2 / 8.4: advisory loadavg threshold drops jobs to 1."""
    effective = requested
    demoted = False
    if loadavg is not None and loadavg > max_loadavg and requested > 1:
        effective = 1
        demoted = True
    return {"requested": requested, "effective": effective,
            "loadavg": loadavg, "threshold": max_loadavg, "demoted": demoted}
