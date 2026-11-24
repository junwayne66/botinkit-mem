"""Evidence record construction for the T044-A gate runner.

plan section 5: every step writes {step, command, exit, duration_ms,
stdout_tail}; the run document additionally carries the worktree HEAD and
SHA-256 fingerprints of the binaries used (task requirement 4), all_pass and
p0_evidence_complete.  Documents are written to evidence/gates/<timestamp>.json
(gitignored local evidence) and to the plan-canonical artifacts dir
(tests/v2/artifacts/gates.json + validation_numbers.json).

Python 3.9 compatible, standard library only.
"""

import hashlib
import json
import os
import time
import uuid
from typing import Dict, List, Optional

TAIL_LINES = 60
TAIL_LINE_CHARS = 500
KEY_LINE_LIMIT = 80
KEY_MARKERS = (
    "test result:", "Ran ", "OK", "FAILED", "Checking bmem", "error[", "error:",
    "warning:", "skipped", "Browser verified", "Browser retained", "FAIL:",
    "ERROR:", "missing evidence", "BLOCKED",
)


def sha256_file(path):
    # type: (str) -> Optional[str]
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def tail_lines(text, limit=TAIL_LINES):
    # type: (str, int) -> List[str]
    lines = (text or "").splitlines()
    return [ln[:TAIL_LINE_CHARS] for ln in lines[-limit:]]


def key_lines(text, limit=KEY_LINE_LIMIT):
    # type: (str, int) -> List[str]
    out = []  # type: List[str]
    for ln in (text or "").splitlines():
        if any(marker in ln for marker in KEY_MARKERS):
            out.append(ln.strip()[:TAIL_LINE_CHARS])
        if len(out) >= limit:
            break
    return out


def new_run_id():
    # type: () -> str
    return uuid.uuid4().hex


def timestamp(now=None):
    # type: (Optional[float]) -> str
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))


def step_record(step, commands, exit_code, duration_ms, stdout, stderr,
                  status, reason, head, binary_sha256, extra=None):
    # type: (object, List[List[str]], Optional[int], int, str, str, str, str, Optional[str], Optional[str], Optional[Dict]) -> Dict
    """One plan-shaped step record plus HEAD and binary fingerprint."""
    combined = (stdout or "") + "\n" + (stderr or "")
    record = {
        "step": step.sid,
        "name": step.name,
        "command": [" ".join(c) for c in commands],
        "argv": commands,
        "exit": exit_code,
        "duration_ms": duration_ms,
        "stdout_tail": tail_lines(stdout),
        "stderr_tail": tail_lines(stderr),
        "key_lines": key_lines(combined),
        "status": status,
        "reason": reason,
        "head": head,
        "binary_sha256": binary_sha256,
    }
    if extra:
        record.update(extra)
    return record


def build_document(run_id, started_at, cfg, head, branch, jobs_info,
                   binaries, step_records, all_pass, p0_complete,
                   summary, dry_run=False):
    # type: (str, str, object, str, str, Dict, Dict, List[Dict], bool, bool, Dict, bool) -> Dict
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": timestamp(),
        "generator": "tests/v2/run_gates.py (T044-A)",
        "spec": "specs/005-agent-mcp-interface/plan.md section 5 steps 0-13",
        "dry_run": dry_run,
        "profile": cfg.profile,
        "repo_root": cfg.repo_root,
        "head": head,
        "branch": branch,
        "manifest_path": cfg.manifest_path,
        "target_dir": cfg.target_dir,
        "jobs": jobs_info,
        "full_clean": cfg.full_clean,
        "binaries": binaries,
        "steps": step_records,
        "all_pass": all_pass,
        "p0_evidence_complete": p0_complete,
        "summary": summary,
    }


def validation_numbers(document):
    # type: (Dict) -> Dict
    """Mechanical numbers for validation.md injection (plan section 5:
    'no hand-filled values'). T044: replaces the stale 004 validation.md
    '0 items / 13 items' figures."""
    by_step = {rec["step"]: rec for rec in document["steps"]}
    out = {
        "run_id": document["run_id"],
        "generated_at": document["finished_at"],
        "source": "run_gates.py mechanical injection; hand-filling forbidden",
        "assembly_commit": document["head"],
        "profile": document["profile"],
        "all_pass": document["all_pass"],
        "binary_sha256": (document["binaries"].get("bmem") or {}).get("sha256"),
        "rust": None,
        "python_baseline": None,
        "python_full": None,
        "native_hosts_collected": None,
        "browser_mode": None,
        "host_versions": None,
        "model_id": None,
    }
    if 4 in by_step:
        out["rust"] = {k: by_step[4].get(k) for k in ("passed", "failed", "ignored", "targets")}
    for sid, key in ((5, "python_baseline"), (6, "python_full")):
        if sid in by_step:
            rec = by_step[sid]
            out[key] = {k: rec.get(k) for k in ("tests_run", "skipped", "status")}
            out[key]["ast_total"] = rec.get("ast_total")
    if 5 in by_step:
        out["native_hosts_collected"] = by_step[5].get("native_hosts_count")
    if 7 in by_step:
        out["browser_mode"] = by_step[7].get("browser_mode")
    if 6 in by_step:
        out["host_versions"] = by_step[6].get("host_versions")
        out["model_id"] = by_step[6].get("model_id")
    return out


def write_json(path, data):
    # type: (str, Dict) -> str
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def write_evidence(document, numbers, evidence_dir, artifacts_dir):
    # type: (Dict, Dict, str, str) -> Dict[str, str]
    """Timestamped local evidence (gitignored /evidence/) plus the
    plan-canonical artifacts consumed by T052/T063."""
    stamp = document["started_at"].replace(":", "")
    run_short = document["run_id"][:8]
    paths = {}
    paths["evidence"] = write_json(
        os.path.join(evidence_dir, "%s-%s.json" % (stamp, run_short)), document)
    paths["gates"] = write_json(os.path.join(artifacts_dir, "gates.json"), document)
    paths["validation_numbers"] = write_json(
        os.path.join(artifacts_dir, "validation_numbers.json"), numbers)
    return paths
