#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T044-B worktree 清理器（plan.md §三.1 / §五 步0与步13 / §八.4）。

协议要点（specs/005-agent-mcp-interface/worktree-protocol.md 是可执行一页）：
  * worktree 一律放 <repo>/.worktrees/T<NNN>-<slug>/（已 gitignore）；
    创建时写 .worktrees/<pkg>.meta（pkg_id/created_at/executor/base_commit）。
  * 陈旧判定：无 meta、meta 无效、超保留期（默认 7 天）、对应包已合流、
    构建产物（target/ 与 target-*/）超 5 GB。
  * 硬闸门：先归档不可再生内容（git diff --binary 补丁、未跟踪文件、
    唯一被忽略文件如日志/本地配置、status 快照、sha256 清单）到
    ~/archive/bmem-worktrees-<日期>/ 并打 tar 回读验证；归档或验证不通过
    一律拒删。教训：上一轮父目录包日志与 prompt 未归档即被删，不可恢复。
    另据共享记忆 pitfall（mem-codex-pitfall-74e8e8831d）：git worktree remove
    连被忽略文件一起删，所以唯一忽略文件必须一并归档。
  * 验证通过后才 git worktree remove --force，收尾 git worktree prune。

模式与退出码：
  --list      只列 worktree 与 meta（JSON stdout），exit 0
  --dry-run   默认。陈旧判定报告（JSON stdout），不写盘不删除，exit 0
  --apply     归档->验证->删除，写 tests/v2/artifacts/worktree_cleanup.json；
              全部处置成功 exit 0；任何拒删/失败 exit 1
  --selftest  临时目录＋stub git 的单测（含「未归档即拒删」），exit 0/1
  用法错误 exit 2（argparse）。

只读安全：--list/--dry-run/--selftest 不运行任何 git 写命令；--selftest 不
触碰真实仓库。禁止 cargo（本脚本从不运行 cargo）。Python 3.9，仅标准库。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 7          # plan §三.1：超 7 天即陈旧
BUILD_CAP_GB = 5.0          # plan §三.1：每 worktree 构建产物上限 5 GB
REGEN_DIR_NAMES = {"node_modules", "__pycache__", ".venv", "venv", ".git"}
REGEN_SUFFIXES = {".pyc", ".pyo", ".pdb"}
LEDGER_REL = "specs/005-agent-mcp-interface/tasks.md"
ARTIFACT_REL = "tests/v2/artifacts/worktree_cleanup.json"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """兼容 Z 后缀（3.9 的 fromisoformat 不认 Z）。naive 一律按 UTC。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty timestamp")
    t = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_target_dir(name):
    return name == "target" or name.startswith("target-")


def is_regenerable(rel_path):
    parts = Path(rel_path).parts
    for p in parts[:-1]:
        if p in REGEN_DIR_NAMES or is_target_dir(p):
            return True
    return Path(rel_path).suffix in REGEN_SUFFIXES


def dir_size_bytes(path):
    total = 0
    for root, dirs, files in os.walk(str(path)):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def sha256_file(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class GitRunner:
    """真实 git 封装。除 remove/prune 外全部只读。selftest 用子类整体替换。"""

    def __init__(self, timeout=120):
        self.timeout = timeout

    def _run(self, args, cwd):
        return subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=self.timeout)

    def worktrees(self, repo):
        p = self._run(["worktree", "list", "--porcelain"], repo)
        if p.returncode != 0:
            raise RuntimeError("git worktree list failed: " + p.stderr.strip())
        out, cur = [], {}
        for line in p.stdout.splitlines():
            if not line.strip():
                if cur:
                    out.append(cur)
                    cur = {}
                continue
            if line.startswith("worktree "):
                cur["path"] = line[len("worktree "):]
            elif line.startswith("HEAD "):
                cur["head"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                cur["branch"] = line[len("branch "):]
            elif line.strip() == "bare":
                cur["bare"] = True
            elif line.strip() == "detached":
                cur["detached"] = True
            elif line.startswith("prunable"):
                cur["prunable"] = True
        if cur:
            out.append(cur)
        return out

    def head(self, repo):
        p = self._run(["rev-parse", "HEAD"], repo)
        return p.stdout.strip() if p.returncode == 0 else ""

    def status(self, wt):
        p = self._run(["status", "--porcelain=v1", "-uall"], wt)
        if p.returncode != 0:
            raise RuntimeError("git status failed: " + p.stderr.strip())
        return p.stdout

    def diff_binary(self, wt):
        p = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=str(wt),
            capture_output=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError("git diff failed: " + p.stderr.decode("utf-8", "replace").strip())
        return p.stdout

    def others(self, wt, ignored):
        args = ["ls-files", "--others", "--exclude-standard"]
        if ignored:
            args.append("--ignored")
        p = self._run(args, wt)
        if p.returncode != 0:
            raise RuntimeError("git ls-files failed: " + p.stderr.strip())
        return [l for l in p.stdout.splitlines() if l.strip()]

    def is_ancestor(self, repo, a, b):
        p = self._run(["merge-base", "--is-ancestor", a, b], repo)
        return p.returncode == 0

    def remove(self, repo, wt):
        p = self._run(["worktree", "remove", "--force", str(wt)], repo)
        return p.returncode, p.stderr.strip()

    def prune(self, repo):
        p = self._run(["worktree", "prune"], repo)
        return p.returncode, p.stderr.strip()


def read_meta(repo, pkg_name):
    path = Path(repo) / ".worktrees" / (pkg_name + ".meta")
    if not path.exists():
        return None, None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return None, "meta unreadable: %s" % e
    required = ("pkg_id", "created_at", "executor", "base_commit")
    missing = [k for k in required if not meta.get(k)]
    if missing:
        return meta, "meta missing fields: %s" % ",".join(missing)
    try:
        parse_iso(meta["created_at"])
    except ValueError as e:
        return meta, "meta created_at invalid: %s" % e
    return meta, None


def read_ledger_checked(repo):
    """ASM 台账勾选＝包已合流的机械信号（只读 tasks.md，不改）。"""
    path = Path(repo) / LEDGER_REL
    checked = set()
    if not path.exists():
        return checked
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return checked
    for m in re.finditer(r"^- \[([xX])\] \*\*(T\d{3})", text, re.MULTILINE):
        checked.add(m.group(2))
    return checked


def build_artifact_bytes(wt_path):
    total = 0
    try:
        entries = os.listdir(str(wt_path))
    except OSError:
        return 0
    for name in entries:
        if is_target_dir(name):
            total += dir_size_bytes(Path(wt_path) / name)
    return total


def classify(wt, repo, git, now, retention_days, build_cap_bytes, ledger_checked):
    """单个 worktree 的协议对账与陈旧判定。主 worktree 永不陈旧。"""
    path = Path(wt["path"])
    pkg = path.name
    rep = {
        "path": str(path), "pkg": pkg, "head": wt.get("head", ""),
        "branch": wt.get("branch", ""), "is_main": False,
        "meta": None, "meta_error": None, "reasons": [], "stale": False,
        "build_bytes": 0, "action": "none",
    }
    if path.resolve() == Path(repo).resolve():
        rep["is_main"] = True
        return rep
    if wt.get("bare"):
        rep["reasons"].append("bare_worktree_skipped")
        return rep
    if not path.exists():
        rep["reasons"].append("missing_dir")
        rep["stale"] = True
        rep["action"] = "prune_only"
        return rep

    meta, meta_error = read_meta(repo, pkg)
    rep["meta"], rep["meta_error"] = meta, meta_error
    if meta is None and meta_error is None:
        rep["reasons"].append("no_meta")
        if (Path(repo) / ".worktrees").resolve() not in path.resolve().parents:
            rep["reasons"].append("outside_protocol_dir")
    elif meta_error:
        rep["reasons"].append("meta_invalid")

    if meta and not meta_error:
        age = now - parse_iso(meta["created_at"])
        rep["age_days"] = round(age.total_seconds() / 86400.0, 3)
        if age > timedelta(days=retention_days):
            rep["reasons"].append("retention_expired")

    # 合流判定（防误杀新建 worktree）：
    #   a) ASM 台账已勾选该包；或
    #   b) 分支 tip 已不同于 base_commit、是 repo HEAD 祖先、且工作区干净。
    pkg_id = (meta or {}).get("pkg_id") or ""
    m = re.match(r"(T\d{3})", pkg or "")
    if not pkg_id and m:
        pkg_id = m.group(1)
    merged_signal = None
    if pkg_id and pkg_id in ledger_checked:
        merged_signal = "ledger_checked"
    elif meta and not meta_error:
        tip = wt.get("head", "")
        base = meta.get("base_commit", "")
        if tip and base and tip != base:
            try:
                clean = git.status(path) == ""
            except RuntimeError:
                clean = False
            if clean and git.is_ancestor(repo, tip, git.head(repo)):
                merged_signal = "branch_ancestor_of_head"
    if merged_signal:
        rep["reasons"].append("merged:" + merged_signal)

    rep["build_bytes"] = build_artifact_bytes(path)
    if rep["build_bytes"] > build_cap_bytes:
        rep["reasons"].append("build_cap_exceeded")

    rep["stale"] = bool(rep["reasons"])
    if rep["stale"]:
        rep["action"] = "archive_and_remove"
    return rep


def build_report(repo, git, now, retention_days, build_cap_bytes):
    repo = Path(repo).resolve()
    ledger = read_ledger_checked(repo)
    wts = git.worktrees(repo)
    rows = [classify(w, repo, git, now, retention_days, build_cap_bytes, ledger)
            for w in wts]
    stale = [r for r in rows if r["stale"]]
    return {
        "schema_version": 1,
        "generated_at": iso(now),
        "repo": str(repo),
        "retention_days": retention_days,
        "build_cap_bytes": build_cap_bytes,
        "worktrees": rows,
        "summary": {
            "total": len(rows),
            "stale": len(stale),
            "stale_pkgs": [r["pkg"] for r in stale],
        },
    }


def archive_worktree(rep, git, archive_root):
    """归档不可再生内容。任何异常都视为归档失败（硬闸门拒删）。"""
    result = {
        "archive_dir": None, "tar_path": None, "verified": False,
        "files_archived": 0, "excluded_regenerable": {}, "error": None,
    }
    wt = Path(rep["path"])
    name = rep["pkg"]
    try:
        archive_root = Path(archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / name
        if dest.exists():  # append-only：不覆盖既有归档
            dest = archive_root / (name + "-" + utcnow().strftime("%H%M%S"))
        dest.mkdir(parents=True)
        result["archive_dir"] = str(dest)
        files = []  # (arcname_rel, category, src_path or None, bytes or None)

        # 1) status 快照
        status_txt = git.status(wt)
        header = ("worktree: %s\nhead: %s\nbranch: %s\ncaptured_at: %s\n"
                  "--- git status --porcelain=v1 -uall ---\n"
                  % (wt, rep.get("head", ""), rep.get("branch", ""), iso(utcnow())))
        (dest / "status.txt").write_text(header + status_txt, encoding="utf-8")
        files.append(("status.txt", "status", None, None))

        # 2) 已跟踪改动补丁（--binary 保证可还原）
        patch = git.diff_binary(wt)
        (dest / "tracked.patch").write_bytes(patch)
        files.append(("tracked.patch", "diff", None, None))

        # 3) meta 原件
        meta_src = Path(rep["repo_for_meta"]) / ".worktrees" / (name + ".meta") \
            if rep.get("repo_for_meta") else None
        if meta_src and meta_src.exists():
            shutil.copy2(str(meta_src), str(dest / "meta.json"))
            files.append(("meta.json", "meta", None, None))

        # 4) 未跟踪文件（非忽略）
        for rel in git.others(wt, ignored=False):
            src = wt / rel
            if not src.is_file():
                raise RuntimeError("untracked entry vanished: %s" % rel)
            dst = dest / "untracked" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            files.append((str(Path("untracked") / rel), "untracked", None, None))

        # 5) 被忽略但唯一的文件（日志/本地配置等）；可再生构建产物只盘点不复制
        excluded = {}
        for rel in git.others(wt, ignored=True):
            if is_regenerable(rel):
                top = Path(rel).parts[0]
                try:
                    size = (wt / rel).stat().st_size
                except OSError:
                    size = 0
                agg = excluded.setdefault(top, {"files": 0, "bytes": 0})
                agg["files"] += 1
                agg["bytes"] += size
                continue
            src = wt / rel
            if not src.is_file():
                raise RuntimeError("ignored entry vanished: %s" % rel)
            dst = dest / "ignored" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            files.append((str(Path("ignored") / rel), "ignored_unique", None, None))
        result["excluded_regenerable"] = excluded

        # 6) 清单（逐文件 sha256）
        manifest_files = []
        for rel, category, _s, _b in files:
            fp = dest / rel
            manifest_files.append({
                "path": rel, "category": category,
                "size": fp.stat().st_size, "sha256": sha256_file(fp),
            })
        manifest = {
            "pkg": name, "worktree_path": str(wt),
            "head": rep.get("head", ""), "branch": rep.get("branch", ""),
            "meta": rep.get("meta"), "reasons": rep.get("reasons", []),
            "archived_at": iso(utcnow()),
            "files": manifest_files,
            "excluded_regenerable": excluded,
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result["files_archived"] = len(manifest_files) + 1

        # 7) 打 tar
        tar_path = archive_root / (dest.name + ".tar.gz")
        with tarfile.open(str(tar_path), "w:gz") as tf:
            tf.add(str(dest), arcname=dest.name)
        result["tar_path"] = str(tar_path)

        # 8) 回读验证：逐成员 sha256 与清单一致才算备份成立
        ok, detail = verify_tar(tar_path, dest.name, manifest_files)
        result["verified"] = ok
        if not ok:
            result["error"] = "tar verification failed: " + detail
    except Exception as e:  # 归档任何一步失败＝硬闸门拒删
        result["verified"] = False
        result["error"] = "%s: %s" % (type(e).__name__, e)
    return result


def verify_tar(tar_path, root_name, manifest_files):
    expected = {root_name + "/" + f["path"]: f for f in manifest_files}
    expected[root_name + "/manifest.json"] = None
    try:
        with tarfile.open(str(tar_path), "r:gz") as tf:
            members = {m.name: m for m in tf.getmembers() if m.isfile()}
            for arcname, f in expected.items():
                if arcname not in members:
                    return False, "missing member " + arcname
                data = tf.extractfile(members[arcname]).read()
                if f is not None:
                    if len(data) != f["size"]:
                        return False, "size mismatch " + arcname
                    if hashlib.sha256(data).hexdigest() != f["sha256"]:
                        return False, "sha256 mismatch " + arcname
    except (tarfile.TarError, OSError, EOFError) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return True, "ok"


def apply_cleanup(report, git, archive_root, artifact_path):
    """硬闸门执行：归档验证通过才 remove；任何拒删/失败 exit 1。"""
    repo = Path(report["repo"])
    removed, refused, failed, pruned, excluded = [], [], [], [], []
    for rep in report["worktrees"]:
        if not rep["stale"] or rep["is_main"]:
            continue
        if rep.get("excluded"):
            excluded.append(rep["pkg"])
            rep["apply"] = {"removed": False, "skipped": "excluded"}
            continue
        rep["apply"] = {"removed": False, "archive": None, "error": None}
        if rep["action"] == "prune_only":
            pruned.append(rep["pkg"])  # 目录已不存在，prune 收尾即可
            rep["apply"]["removed"] = True
            continue
        rep["repo_for_meta"] = str(repo)
        arch = archive_worktree(rep, git, archive_root)
        rep["apply"]["archive"] = arch
        if not arch["verified"]:
            rep["apply"]["error"] = "REFUSED: archive unverified (%s)" % arch["error"]
            refused.append(rep["pkg"])
            continue
        code, err = git.remove(repo, rep["path"])
        if code != 0:
            rep["apply"]["error"] = "git worktree remove failed: " + err
            failed.append(rep["pkg"])
            continue
        rep["apply"]["removed"] = True
        removed.append(rep["pkg"])
        meta_path = repo / ".worktrees" / (rep["pkg"] + ".meta")
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError as e:
                rep["apply"]["error"] = "meta unlink failed: %s" % e
    code, err = git.prune(repo)
    report["prune"] = {"exit": code, "stderr": err}
    report["mode"] = "apply"
    report["archive_root"] = str(archive_root)
    report["summary"].update({
        "removed": removed, "refused": refused, "failed": failed,
        "pruned": pruned, "excluded": excluded,
        "all_clean": not refused and not failed and code == 0,
    })
    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["artifact_path"] = str(artifact_path)
    return report


def default_archive_root(now):
    return Path.home() / "archive" / ("bmem-worktrees-" + now.strftime("%Y%m%d"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T044-B worktree 清理：先证明已备份，再删（硬闸门）。")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="只列 worktree 与 meta")
    mode.add_argument("--dry-run", action="store_true", help="陈旧判定报告（默认）")
    mode.add_argument("--apply", action="store_true", help="归档->验证->删除")
    mode.add_argument("--selftest", action="store_true", help="临时目录单测")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--retention-days", type=float, default=RETENTION_DAYS)
    ap.add_argument("--build-cap-gb", type=float, default=BUILD_CAP_GB)
    ap.add_argument("--archive-root", default=None,
                    help="默认 ~/archive/bmem-worktrees-<YYYYMMDD>")
    ap.add_argument("--report", default=None,
                    help="apply 工件路径，默认 <repo>/" + ARTIFACT_REL)
    ap.add_argument("--exclude", action="append", default=[], metavar="PKG",
                    help="显式保护在途包（可重复）：仍报告陈旧原因，但 apply 跳过处置")
    args = ap.parse_args(argv)

    if args.selftest:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        run = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if run.wasSuccessful() else 1

    repo = Path(args.repo).resolve()
    git = GitRunner()
    now = utcnow()
    cap = int(args.build_cap_gb * (1 << 30))
    try:
        report = build_report(repo, git, now, args.retention_days, cap)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    for rep in report["worktrees"]:
        if rep["pkg"] in args.exclude:
            rep["excluded"] = True
            if rep["stale"]:
                rep["action"] = "skip_excluded"

    if args.apply:
        root = Path(args.archive_root) if args.archive_root else default_archive_root(now)
        artifact = Path(args.report) if args.report else repo / ARTIFACT_REL
        report = apply_cleanup(report, git, root, artifact)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["summary"]["all_clean"] else 1

    report["mode"] = "list" if args.list else "dry_run"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------
# selftest：全部在临时目录＋stub git 上跑，不触碰真实仓库、不跑真实 git 写命令。
# --------------------------------------------------------------------------

class FakeGit(GitRunner):
    """脚本化 git。remove 会真的删掉临时 fixture 目录以模拟删除效果。"""

    def __init__(self, worktrees, head="c" * 40, ancestor=True,
                 diff=b"diff --git a/f.txt b/f.txt\n", status="",
                 others=(), ignored=(), remove_code=0):
        super().__init__()
        self._wts = worktrees
        self._head = head
        self._ancestor = ancestor
        self._diff = diff
        self._status = status
        self._others = list(others)
        self._ignored = list(ignored)
        self._remove_code = remove_code
        self.remove_calls = []
        self.prune_calls = 0

    def worktrees(self, repo):
        return self._wts

    def head(self, repo):
        return self._head

    def status(self, wt):
        return self._status

    def diff_binary(self, wt):
        return self._diff

    def others(self, wt, ignored):
        return self._ignored if ignored else self._others

    def is_ancestor(self, repo, a, b):
        return self._ancestor

    def remove(self, repo, wt):
        self.remove_calls.append(str(wt))
        if self._remove_code == 0 and Path(wt).exists():
            shutil.rmtree(str(wt))
        return self._remove_code, "" if self._remove_code == 0 else "stub failure"

    def prune(self, repo):
        self.prune_calls += 1
        return 0, ""


class CleanupSelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wt-selftest-"))
        self.repo = self.tmp / "repo"
        (self.repo / ".worktrees").mkdir(parents=True)
        self.now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
        self.cap = 5 * (1 << 30)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def make_wt(self, name="T099-demo", with_meta=True, age_days=1.0,
                base="a" * 40, head="b" * 40):
        wt = self.repo / ".worktrees" / name
        wt.mkdir(parents=True)
        (wt / "f.txt").write_text("tracked\n", encoding="utf-8")
        (wt / "new.txt").write_text("untracked payload\n", encoding="utf-8")
        (wt / "logs").mkdir()
        (wt / "logs" / "run.log").write_text("precious log\n", encoding="utf-8")
        (wt / ".env.local").write_text("LOCAL=1\n", encoding="utf-8")
        (wt / "target-gate").mkdir()
        (wt / "target-gate" / "big.o").write_bytes(b"\0" * 2048)
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        if with_meta:
            created = self.now - timedelta(days=age_days)
            meta = {"pkg_id": name.split("-")[0], "created_at": iso(created),
                    "executor": "CC", "base_commit": base}
            (self.repo / ".worktrees" / (name + ".meta")).write_text(
                json.dumps(meta), encoding="utf-8")
        return wt

    def wt_entry(self, wt, head=None, branch="refs/heads/pkg/demo"):
        return {"path": str(wt), "head": head or "b" * 40, "branch": branch}

    def test_fresh_worktree_not_stale(self):
        wt = self.make_wt(head="a" * 40)  # tip == base：新建，未合流
        git = FakeGit([self.wt_entry(wt, head="a" * 40)], head="a" * 40)
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertEqual(rep["summary"]["stale"], 0)
        self.assertFalse(rep["worktrees"][0]["stale"])

    def test_no_meta_is_stale(self):
        wt = self.make_wt(with_meta=False)
        git = FakeGit([self.wt_entry(wt)])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        row = rep["worktrees"][0]
        self.assertIn("no_meta", row["reasons"])
        self.assertTrue(row["stale"])

    def test_retention_expired_is_stale(self):
        wt = self.make_wt(age_days=8.0, head="a" * 40)
        git = FakeGit([self.wt_entry(wt, head="a" * 40)], head="a" * 40)
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertIn("retention_expired", rep["worktrees"][0]["reasons"])

    def test_merged_by_ancestry_is_stale(self):
        wt = self.make_wt()  # head b != base a，status 干净，ancestor=True
        git = FakeGit([self.wt_entry(wt)], head="c" * 40, ancestor=True, status="")
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertIn("merged:branch_ancestor_of_head", rep["worktrees"][0]["reasons"])

    def test_dirty_worktree_not_merged(self):
        wt = self.make_wt()
        git = FakeGit([self.wt_entry(wt)], head="c" * 40, ancestor=True,
                      status=" M f.txt\n")
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertFalse(rep["worktrees"][0]["stale"])

    def test_merged_by_ledger_is_stale(self):
        wt = self.make_wt(head="a" * 40)
        ledger = self.repo / LEDGER_REL
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("- [x] **T099 demo**\n", encoding="utf-8")
        git = FakeGit([self.wt_entry(wt, head="a" * 40)], head="a" * 40)
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertIn("merged:ledger_checked", rep["worktrees"][0]["reasons"])

    def test_build_cap_exceeded(self):
        wt = self.make_wt(head="a" * 40)
        git = FakeGit([self.wt_entry(wt, head="a" * 40)], head="a" * 40)
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, 1024)
        row = rep["worktrees"][0]
        self.assertIn("build_cap_exceeded", row["reasons"])
        self.assertGreaterEqual(row["build_bytes"], 2048)

    def test_main_worktree_never_stale(self):
        git = FakeGit([{"path": str(self.repo), "head": "a" * 40,
                        "branch": "refs/heads/main"}])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        self.assertTrue(rep["worktrees"][0]["is_main"])
        self.assertFalse(rep["worktrees"][0]["stale"])

    def test_apply_archives_then_removes(self):
        wt = self.make_wt(with_meta=False)  # no_meta -> stale
        git = FakeGit([self.wt_entry(wt)], others=["new.txt"],
                      ignored=["logs/run.log", ".env.local",
                               "target-gate/big.o", "node_modules/pkg/index.js"])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        root = self.tmp / "archive"
        artifact = self.tmp / "artifacts" / "worktree_cleanup.json"
        rep = apply_cleanup(rep, git, root, artifact)
        self.assertTrue(rep["summary"]["all_clean"])
        self.assertEqual(git.remove_calls, [str(wt)])
        self.assertEqual(git.prune_calls, 1)
        self.assertFalse(wt.exists())
        self.assertTrue(artifact.exists())
        arch = rep["worktrees"][0]["apply"]["archive"]
        self.assertTrue(arch["verified"])
        ad = Path(arch["archive_dir"])
        self.assertTrue((ad / "tracked.patch").exists())
        self.assertTrue((ad / "status.txt").exists())
        self.assertEqual((ad / "untracked" / "new.txt").read_text(), "untracked payload\n")
        self.assertEqual((ad / "ignored" / "logs" / "run.log").read_text(), "precious log\n")
        self.assertTrue((ad / "ignored" / ".env.local").exists())
        # 可再生构建产物不复制，只盘点
        self.assertFalse((ad / "ignored" / "target-gate").exists())
        self.assertIn("target-gate", arch["excluded_regenerable"])
        self.assertIn("node_modules", arch["excluded_regenerable"])
        self.assertTrue(Path(arch["tar_path"]).exists())
        self.assertFalse((self.repo / ".worktrees" / "T099-demo.meta").exists())

    def test_hard_gate_archive_failure_refuses_delete(self):
        wt = self.make_wt(with_meta=False)
        git = FakeGit([self.wt_entry(wt)], others=["new.txt"])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        # 归档根不可写 -> 归档失败 -> 必须拒删
        root = self.tmp / "readonly-archive"
        root.mkdir()
        root.chmod(0o500)
        try:
            artifact = self.tmp / "artifacts" / "worktree_cleanup.json"
            rep = apply_cleanup(rep, git, root, artifact)
        finally:
            root.chmod(0o700)
        self.assertFalse(rep["summary"]["all_clean"])
        self.assertEqual(rep["summary"]["refused"], ["T099-demo"])
        self.assertEqual(git.remove_calls, [])
        self.assertTrue(wt.exists())
        self.assertTrue((wt / "new.txt").exists())
        self.assertIn("REFUSED", rep["worktrees"][0]["apply"]["error"])

    def test_hard_gate_diff_failure_refuses_delete(self):
        wt = self.make_wt(with_meta=False)

        class BrokenDiff(FakeGit):
            def diff_binary(self, wt):
                raise RuntimeError("git diff exploded")

        git = BrokenDiff([self.wt_entry(wt)], others=["new.txt"])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        rep = apply_cleanup(rep, git, self.tmp / "archive",
                            self.tmp / "a" / "worktree_cleanup.json")
        self.assertEqual(git.remove_calls, [])
        self.assertTrue(wt.exists())
        self.assertEqual(rep["summary"]["refused"], ["T099-demo"])

    def test_hard_gate_tar_verify_failure_refuses_delete(self):
        wt = self.make_wt(with_meta=False)
        git = FakeGit([self.wt_entry(wt)], others=["new.txt"])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        mod = sys.modules[__name__]  # 篡改本模块验证函数，模拟 tar 损坏
        orig = mod.verify_tar
        mod.verify_tar = lambda *a, **k: (False, "simulated corruption")
        try:
            rep = apply_cleanup(rep, git, self.tmp / "archive",
                                self.tmp / "a" / "worktree_cleanup.json")
        finally:
            mod.verify_tar = orig
        self.assertEqual(git.remove_calls, [])
        self.assertTrue(wt.exists())
        self.assertEqual(rep["summary"]["refused"], ["T099-demo"])

    def test_remove_failure_recorded_not_silent(self):
        wt = self.make_wt(with_meta=False)
        git = FakeGit([self.wt_entry(wt)], others=["new.txt"], remove_code=1)
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        rep = apply_cleanup(rep, git, self.tmp / "archive",
                            self.tmp / "a" / "worktree_cleanup.json")
        self.assertFalse(rep["summary"]["all_clean"])
        self.assertEqual(rep["summary"]["failed"], ["T099-demo"])
        self.assertTrue(wt.exists())

    def test_exclude_protects_inflight_worktree(self):
        wt = self.make_wt(with_meta=False)  # no_meta -> stale
        git = FakeGit([self.wt_entry(wt)], others=["new.txt"])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        rep["worktrees"][0]["excluded"] = True
        rep["worktrees"][0]["action"] = "skip_excluded"
        rep = apply_cleanup(rep, git, self.tmp / "archive",
                            self.tmp / "a" / "worktree_cleanup.json")
        self.assertEqual(git.remove_calls, [])
        self.assertTrue(wt.exists())
        self.assertEqual(rep["summary"]["excluded"], ["T099-demo"])
        self.assertTrue(rep["summary"]["all_clean"])

    def test_missing_dir_prune_only(self):
        ghost = self.repo / ".worktrees" / "T098-gone"
        git = FakeGit([{"path": str(ghost), "head": "a" * 40, "prunable": True}])
        rep = build_report(self.repo, git, self.now, RETENTION_DAYS, self.cap)
        rep = apply_cleanup(rep, git, self.tmp / "archive",
                            self.tmp / "a" / "worktree_cleanup.json")
        self.assertEqual(rep["summary"]["pruned"], ["T098-gone"])
        self.assertEqual(git.remove_calls, [])
        self.assertEqual(git.prune_calls, 1)
        self.assertTrue(rep["summary"]["all_clean"])


if __name__ == "__main__":
    sys.exit(main())
