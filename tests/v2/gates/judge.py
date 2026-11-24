"""Pure judgment logic for the T044-A gate runner.

Every function here is deterministic and side-effect free so the strict
verdicts (any skip fails, per-file browser runs are not evidence, missing
real-model env fails with named variables, zero collection fails) can be
unit-tested without running the real gates.

Python 3.9 compatible, standard library only.
"""

import ast
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple

from . import steps as S

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
WARN = "WARN"


def verdict(status, reason="", **extra):
    # type: (str, str, ...) -> Dict
    out = {"status": status, "reason": reason}
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# unittest output parsing (python3 -m unittest discover -s tests/v2 -v)
# --------------------------------------------------------------------------

_UNIT_LINE = re.compile(r"^(\S+) \(([^)]+)\) \.\.\. (.*)$")
_RAN = re.compile(r"^Ran (\d+) tests? in ")
_OK_SKIP = re.compile(r"^OK \(skipped=(\d+)\)\s*$")
_FAILED_PART = re.compile(r"(failures|errors|skipped)=(\d+)")


def parse_unittest_output(text):
    # type: (str) -> Dict
    """Parse verbose unittest output (3.9 and 3.11+ id formats)."""
    tests_run = None
    skipped = 0
    failures = 0
    errors = 0
    skipped_ids = []  # type: List[str]
    failed_ids = []  # type: List[str]
    ok = False
    for line in text.splitlines():
        line = line.strip()
        m = _RAN.match(line)
        if m:
            tests_run = int(m.group(1))
            continue
        if line == "OK":
            ok = True
            continue
        m = _OK_SKIP.match(line)
        if m:
            ok = True
            skipped = int(m.group(1))
            continue
        if line.startswith("FAILED"):
            for key, val in _FAILED_PART.findall(line):
                if key == "failures":
                    failures = int(val)
                elif key == "errors":
                    errors = int(val)
                elif key == "skipped":
                    skipped = int(val)
            continue
        m = _UNIT_LINE.match(line)
        if m:
            method, paren, status = m.group(1), m.group(2), m.group(3)
            parts = paren.split(".")
            if len(parts) >= 3 and parts[-1] == method:
                paren = ".".join(parts[:-1])  # 3.11+ repeats the method
            tid = paren + "." + method
            if status.startswith("skipped"):
                skipped_ids.append(tid)
            elif status.startswith(("FAIL", "ERROR")):
                failed_ids.append(tid)
    return {"tests_run": tests_run, "skipped": skipped, "failures": failures,
            "errors": errors, "skipped_ids": skipped_ids,
            "failed_ids": failed_ids, "ok": ok}


# --------------------------------------------------------------------------
# AST collection: what discover MUST collect (anti silent-zero-collection)
# --------------------------------------------------------------------------

def _is_testcase(class_node):
    # type: (ast.ClassDef) -> bool
    for base in class_node.bases:
        if isinstance(base, ast.Name) and base.id.endswith("TestCase"):
            return True
        if isinstance(base, ast.Attribute) and base.attr.endswith("TestCase"):
            return True
    return False


def _module_id(tests_dir, path):
    # type: (str, str) -> str
    rel = os.path.relpath(path, tests_dir)
    parts = rel.split(os.sep)
    stem = os.path.splitext(parts[-1])[0]
    return ".".join(parts[:-1] + [stem])


def collect_ast_tests(tests_dir):
    # type: (str) -> Dict
    """Walk tests_dir exactly like unittest discover (3.9 recurses only into
    packages), count every def test_* (plan step 5 AST sum) and record
    TestCase method ids plus their skip guards."""
    total = 0
    methods = {}  # type: Dict[str, List[str]]
    guards = {}  # type: Dict[str, List[Dict]]
    anomalies = []  # type: List[str]
    sources = {}  # type: Dict[str, str]

    def walk(directory, prefix):
        nonlocal total
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if os.path.isdir(path):
                if os.path.isfile(os.path.join(path, "__init__.py")):
                    walk(path, prefix + [entry])
                continue
            if not (entry.startswith("test_") and entry.endswith(".py")):
                continue
            module = ".".join(prefix + [os.path.splitext(entry)[0]])
            source = open(path, "r", encoding="utf-8").read()
            sources[module] = source
            tree = ast.parse(source, filename=path)
            ids = []  # type: List[str]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    total += 1
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and _is_testcase(node):
                    class_guards = _skip_decorators(node, source)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                            tid = module + "." + node.name + "." + item.name
                            ids.append(tid)
                            guards[tid] = class_guards + _skip_decorators(item, source)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    anomalies.append(module + "." + node.name + " (module-level, discover will not collect)")
            methods[module] = ids

    walk(tests_dir, [])
    native = sum(len(ids) for mod, ids in methods.items() if mod.split(".")[-1] == "test_native_hosts")
    return {"ast_total": total, "methods": methods, "guards": guards,
            "anomalies": anomalies, "native_hosts_count": native,
            "sources": sources}


def _skip_decorators(node, source):
    # type: (ast.AST, str) -> List[Dict]
    out = []  # type: List[Dict]
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if name not in ("skip", "skipUnless", "skipIf"):
            continue
        cond_src = None
        if name in ("skipUnless", "skipIf") and dec.args:
            cond_src = ast.get_source_segment(source, dec.args[0])
        out.append({"kind": name, "cond": cond_src})
    return out


class _FakeOs(object):
    def __init__(self, name, environ):
        self.name = name
        self.environ = environ


def expected_skip_ids(collection, env, platform_name):
    # type: (Dict, Dict[str, str], str) -> Tuple[Set[str], List[str]]
    """Evaluate skip guards against the step env/platform (plan step 5:
    'skip set == expected set computed from platform/environment').
    Unevaluable guards are reported; if such a test skips at runtime the
    strict set-equality check fails it as unexpected."""
    expected = set()  # type: Set[str]
    unknown = []  # type: List[str]
    fake = _FakeOs(platform_name, env)
    for tid, guard_list in collection["guards"].items():
        skip = False
        for guard in guard_list:
            if guard["kind"] == "skip":
                skip = True
                break
            if guard["cond"] is None:
                unknown.append(tid)
                continue
            try:
                value = eval(guard["cond"], {"__builtins__": {}}, {"os": fake, "sys": sys})  # noqa: S307 - restricted guard eval
            except Exception:
                unknown.append(tid)
                continue
            if guard["kind"] == "skipUnless" and not value:
                skip = True
            if guard["kind"] == "skipIf" and value:
                skip = True
        if skip:
            expected.add(tid)
    return expected, sorted(set(unknown))


def judge_unittest(parsed, exit_code, collection, env, platform_name,
                   strict_zero_skips, baseline_min=S.BASELINE_MIN_PY_TESTS):
    # type: (Dict, Optional[int], Dict, Dict[str, str], str, bool, int) -> Dict
    """plan step 5 / step 6 verdicts. Any unexpected skip fails; step 6
    (full evidence) requires skipped == 0 outright."""
    reasons = []
    if exit_code != 0 or parsed["failures"] or parsed["errors"] or not parsed["ok"]:
        reasons.append("unittest reported failures/errors or non-zero exit")
    run = parsed["tests_run"]
    if run is None or run == 0:
        reasons.append("zero tests collected (silent zero-collection is not evidence)")
    else:
        if run != collection["ast_total"]:
            reasons.append("testsRun %s != AST def test_ sum %s" % (run, collection["ast_total"]))
        if run < baseline_min:
            reasons.append("testsRun %s below documented baseline %s (%s)" % (run, baseline_min, S.BASELINE_PROVENANCE))
    if collection["native_hosts_count"] <= 0:
        reasons.append("collection must include test_native_hosts with >0 cases")
    if collection["anomalies"]:
        reasons.append("uncollectable test defs: " + "; ".join(collection["anomalies"]))
    actual = set(parsed["skipped_ids"])
    if strict_zero_skips:
        if parsed["skipped"] or actual:
            reasons.append("full-evidence run must have skipped==0, got %s: %s"
                           % (parsed["skipped"], ", ".join(sorted(actual)) or "(count only)"))
    else:
        expected, unknown = expected_skip_ids(collection, env, platform_name)
        if actual != expected:
            unexpected = sorted(actual - expected)
            missing = sorted(expected - actual)
            reasons.append("skip set != expected platform/env set; unexpected=%s missing=%s"
                           % (unexpected, missing))
        if unknown and actual & set(unknown):
            reasons.append("skipped tests with unevaluable guards: %s" % sorted(actual & set(unknown)))
    if reasons:
        return verdict(FAIL, "; ".join(reasons), tests_run=run,
                       skipped=parsed["skipped"], skipped_ids=sorted(actual))
    return verdict(PASS, "", tests_run=run, skipped=parsed["skipped"],
                   skipped_ids=sorted(actual))


# --------------------------------------------------------------------------
# cargo output parsing
# --------------------------------------------------------------------------

_CARGO_RESULT = re.compile(
    r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored")


def parse_cargo_test(text):
    # type: (str) -> Dict
    passed = failed = ignored = 0
    targets = 0
    for m in _CARGO_RESULT.finditer(text):
        targets += 1
        passed += int(m.group(2))
        failed += int(m.group(3))
        ignored += int(m.group(4))
    return {"passed": passed, "failed": failed, "ignored": ignored,
            "targets": targets}


def judge_cargo_test(parsed, exit_code):
    # type: (Dict, Optional[int]) -> Dict
    reasons = []
    if exit_code != 0:
        reasons.append("cargo test exit %s" % exit_code)
    if parsed["targets"] == 0:
        reasons.append("no 'test result:' lines found (no evidence)")
    if parsed["failed"]:
        reasons.append("%s failed" % parsed["failed"])
    if parsed["ignored"]:
        reasons.append("%s ignored (any skip/ignore fails)" % parsed["ignored"])
    if reasons:
        return verdict(FAIL, "; ".join(reasons), **parsed)
    return verdict(PASS, "", **parsed)


def judge_clippy(exit_code, output):
    # type: (Optional[int], str) -> Dict
    if exit_code != 0:
        return verdict(FAIL, "clippy exit %s" % exit_code)
    if "Checking bmem" not in output:
        return verdict(FAIL, "clippy output lacks 'Checking bmem' (cold-run freshness not proven)")
    return verdict(PASS, "")


def judge_newer_files(newer_files):
    # type: (List[str]) -> Dict
    if newer_files:
        return verdict(FAIL, "sources newer than binary (stale embedded web/resources): %s" % newer_files[:20])
    return verdict(PASS, "")


# --------------------------------------------------------------------------
# browser: browser_smoke.cjs is the only valid entry
# --------------------------------------------------------------------------

def browser_command_is_entry(argv):
    # type: (List[str]) -> bool
    if len(argv) < 2:
        return False
    node = os.path.basename(argv[0])
    if node not in ("node", "node.exe"):
        return False
    args = [a.replace("\\", "/") for a in argv[1:]]
    if any(a.endswith(".cjs") and not a.endswith("browser_smoke.cjs") for a in args):
        return False
    return any(a == S.BROWSER_ENTRY or a.endswith("/" + S.BROWSER_ENTRY) for a in args)


def judge_browser_run(argv, exit_code, stdout):
    # type: (List[str], Optional[int], str) -> Dict
    if not browser_command_is_entry(argv):
        return verdict(FAIL, "per-file direct browser runs are not evidence; only %s is a valid entry" % S.BROWSER_ENTRY)
    if exit_code != 0:
        return verdict(FAIL, "browser_smoke exit %s" % exit_code)
    if not stdout.strip():
        return verdict(FAIL, "empty browser output is not evidence")
    return verdict(PASS, "", browser_mode="full")


# --------------------------------------------------------------------------
# step 6 preflight: real model + pinned hosts
# --------------------------------------------------------------------------

def judge_full_preflight(env, tags_ok, tags_models, host_versions,
                         pinned=None, pinned_model=S.PINNED_MODEL):
    # type: (Dict[str, str], bool, List[str], Dict[str, Optional[str]], Optional[Dict], str) -> Dict
    pinned = pinned or S.PINNED_HOST_VERSIONS
    real_model = env.get("BMEM_V2_TEST_REAL_MODEL")
    if not real_model:
        return verdict(
            FAIL,
            "missing evidence: real-model cases require BMEM_V2_TEST_REAL_MODEL. "
            "Set BMEM_V2_TEST_REAL_MODEL=%s (and the runner sets "
            "BMEM_V2_TEST_NATIVE_HOSTS=1 for the full discover). "
            "Refusing to pass silently." % pinned_model,
            reason_code="missing_evidence",
            required_env=["BMEM_V2_TEST_REAL_MODEL=%s" % pinned_model,
                          "BMEM_V2_TEST_NATIVE_HOSTS=1 (set by runner)"])
    if not tags_ok:
        return verdict(BLOCKED, "cannot reach %s to confirm model inventory" % S.OLLAMA_TAGS_URL,
                       reason_code="ollama_unreachable")
    if real_model not in tags_models:
        return verdict(BLOCKED, "model %s not present in ollama tags (pinned %s)" % (real_model, pinned_model),
                       reason_code="model_missing")
    mismatch = {}
    for host, want in pinned.items():
        got = host_versions.get(host)
        if got != want:
            mismatch[host] = {"pinned": want, "observed": got}
    if mismatch:
        return verdict(BLOCKED, "host versions differ from pinned matrix: %s (ASM must re-verify and update the matrix; no silent SKIP)" % mismatch,
                       reason_code="host_version_mismatch")
    return verdict(PASS, "", model_id=real_model, host_versions=dict(host_versions),
                   pinned_model_present=pinned_model in tags_models)


# --------------------------------------------------------------------------
# steps 10 / 12: committed doc baseline and file boundaries
# --------------------------------------------------------------------------

def judge_doc_drift(baseline_changed, tech_docs_changed):
    # type: (bool, bool) -> Dict
    if baseline_changed and not tech_docs_changed:
        return verdict(FAIL, "doc baseline drifted without technical-doc changes in the same diff")
    return verdict(PASS, "")


def reconcile_boundaries(changed, declared, evidence_prefixes=S.EVIDENCE_PREFIXES):
    # type: (List[str], Set[str], Tuple[str, ...]) -> Dict
    excluded = sorted(p for p in changed if p.startswith(evidence_prefixes))
    effective = sorted(set(changed) - set(excluded))
    offenders = sorted(p for p in effective if p not in declared)
    return {"changed": effective, "excluded_evidence": excluded,
            "offenders": offenders, "declared_count": len(declared)}


def judge_boundaries(recon, head_conflicts, work_conflicts):
    # type: (Dict, Optional[int], Optional[int]) -> Dict
    reasons = []
    if recon["offenders"]:
        reasons.append("undeclared file changes (out of bounds): %s" % recon["offenders"])
    if head_conflicts is None or work_conflicts is None:
        reasons.append("Error::conflict( count unavailable (git grep failed); refusing to guess")
    elif work_conflicts > head_conflicts:
        reasons.append("Error::conflict( count increased %s -> %s" % (head_conflicts, work_conflicts))
    if reasons:
        return verdict(FAIL, "; ".join(reasons), head_conflicts=head_conflicts,
                       work_conflicts=work_conflicts)
    return verdict(PASS, "", head_conflicts=head_conflicts, work_conflicts=work_conflicts)


def declared_files_from_patch(data):
    # type: (object) -> Set[str]
    """Extract file_path declarations from a patches/<pkg>.json document.
    The schema is frozen by T045; until then accept the plan section 3.3
    field names tolerantly (object, list, or nested 'patches'/'entries')."""
    out = set()  # type: Set[str]
    if isinstance(data, dict):
        fp = data.get("file_path")
        if isinstance(fp, str):
            out.add(fp)
        for key in ("patches", "entries", "files"):
            if key in data:
                out |= declared_files_from_patch(data[key])
        if isinstance(data.get("files"), list):
            out |= {f for f in data["files"] if isinstance(f, str)}
    elif isinstance(data, list):
        for item in data:
            out |= declared_files_from_patch(item)
    elif isinstance(data, str):
        out.add(data)
    return out


# --------------------------------------------------------------------------
# step 11: executor probe record
# --------------------------------------------------------------------------

def judge_executors(data):
    # type: (object) -> Dict
    entries = []  # type: List[Dict]
    if isinstance(data, dict):
        for key in ("executors", "probes", "results"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
        else:
            entries = [dict(v, name=k) for k, v in data.items() if isinstance(v, dict)]
    elif isinstance(data, list):
        entries = data
    by_name = {}
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("executor") or entry.get("id")
            if name:
                by_name[name] = entry
    missing = [n for n in ("claude", "codex", "dsh") if n not in by_name]
    # scripts/probe_agents.py (T044 sibling) records tiers as "verdict";
    # accept either field name as the status of record.
    incomplete = [n for n, e in by_name.items()
                  if not e.get("version") or not (e.get("status") or e.get("verdict"))]
    if missing or incomplete:
        return verdict(FAIL, "executors.json incomplete: missing=%s incomplete=%s" % (missing, incomplete))
    return verdict(PASS, "", executors={n: {"version": by_name[n].get("version"),
                                           "status": by_name[n].get("status") or by_name[n].get("verdict")}
                                       for n in by_name})


# --------------------------------------------------------------------------
# summary flags
# --------------------------------------------------------------------------

def compute_all_pass(results, profile, web_changed):
    # type: (Dict[int, Dict], str, bool) -> bool
    for res in results.values():
        if res["status"] in (FAIL, BLOCKED):
            return False
    if profile == "assembly":
        if 6 in results and results[6]["status"] != PASS:
            return False
        if web_changed and 7 in results and results[7]["status"] != PASS:
            return False
    return True


def p0_evidence_complete(step6_status, receipts):
    # type: (str, Optional[Dict]) -> bool
    """Informational flag for T052/T063 (plan section 5). T052 freezes the
    final native_receipts.json shape; this mechanical check requires both
    hosts real_local_model loaded observed, one model-output used assertion,
    and verifier != attempt subject. Missing file -> False, never a guess."""
    if step6_status != PASS or not isinstance(receipts, dict):
        return False
    hosts = receipts.get("hosts", receipts)
    if not isinstance(hosts, dict):
        return False
    needed = ("claude-code", "codex")
    loaded_ok = True
    used_assertion = False
    verifier_ok = False
    for host in needed:
        entry = hosts.get(host)
        if not isinstance(entry, dict):
            return False
        if entry.get("provider_mode") != "real_local_model" or entry.get("loaded_state") != "observed":
            loaded_ok = False
        if entry.get("used_evidence_kind") == "model_output_assertion":
            used_assertion = True
        if entry.get("outcome_verifier") and entry.get("attempt_subject") \
                and entry["outcome_verifier"] != entry["attempt_subject"]:
            verifier_ok = True
    return bool(loaded_ok and used_assertion and verifier_ok)
