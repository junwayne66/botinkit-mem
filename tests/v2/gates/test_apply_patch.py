#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T044-C apply_patch.py 门禁自测（纯文本夹具，不碰仓库真源码，不跑 cargo）。

运行: python3 -m unittest discover -s tests/v2/gates -v
覆盖: 唯一命中 / 锚点缺失 / 重复命中 / 同 arm 冲突 / 波次与包序排序 /
      热点 reconciler 规则 / 目标行被前一条移动 / 链式锚定 / --rollback 往返一致 /
      回滚漂移与 LIFO / dry-run 不落盘 / 函数作用域 / CLI --help --list --selftest。
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "apply_patch.py"

_spec = importlib.util.spec_from_file_location("apply_patch_t044c", str(SCRIPT))
ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap)

# 纯文本夹具：形似 server.rs 的 dispatch match 块，但不是仓库源码。
SERVER_FIXTURE = (
    "use crate::app::App;\n"
    "\n"
    "fn dispatch(app: &App, method: &str, parts: &[&str]) -> Result<u16> {\n"
    "    let identity = authenticate(app)?;\n"
    "    let outcome = match (method, parts) {\n"
    '        ("GET", ["v2", "ping"]) => {\n'
    "            identity.owner()?;\n"
    "            ok()\n"
    "        }\n"
    '        ("GET", ["v2", "facts", id, "history"]) => {\n'
    "            identity.owner()?;\n"
    "            history(id)\n"
    "        }\n"
    "        _ => missing(),\n"
    "    };\n"
    "    Ok(outcome)\n"
    "}\n"
    "\n"
    "fn helper() {\n"
    "            ok()\n"
    "}\n"
)

NOTES_FIXTURE = "line one\nline two\nline three\n"

PING_ANCHOR = [
    '        ("GET", ["v2", "ping"]) => {',
    "            identity.owner()?;",
    "            ok()",
    "        }",
]


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_op(arm, anchor_lines, insert=None, generate=None, position="after"):
    op = {"arm": arm, "anchor": {"lines": anchor_lines, "position": position}}
    if generate is not None:
        op["generate"] = generate
    else:
        op["insert"] = insert if insert is not None else ["            // inserted"]
    return op


def entry(file_path, ops=(), scope=None, **declarations):
    item = {"file_path": file_path, "match_arms": list(ops)}
    if scope:
        item["function_scope"] = scope
    item.update(declarations)
    return item


def package(pkg, wave, order, entries):
    return {"pkg": pkg, "wave": wave, "order": order, "patches": entries}


class ApplyPatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="t044c-apply-patch-")
        self.root = Path(self._tmp.name)
        self.patches = self.root / "patches"
        self.patches.mkdir()
        self.evidence = self.root / "artifacts" / "patches_applied.json"
        self.server = self.root / "src" / "demo" / "server.rs"
        self.server.parent.mkdir(parents=True)
        self.server.write_text(SERVER_FIXTURE, encoding="utf-8")
        self.notes = self.root / "src" / "demo" / "notes.rs"
        self.notes.write_text(NOTES_FIXTURE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, pkg_obj):
        name = "%s.json" % pkg_obj["pkg"]
        (self.patches / name).write_text(
            json.dumps(pkg_obj, ensure_ascii=False), encoding="utf-8")

    def run_apply(self, wave=None, dry=False):
        return ap.execute_apply(str(self.root), str(self.patches),
                                str(self.evidence), wave, dry)

    def run_rollback(self, run_id=None, dry=False):
        return ap.execute_rollback(str(self.root), str(self.evidence), run_id, dry)

    def codes(self, report):
        return [c["code"] for c in report.get("conflicts", [])]

    def read_server(self):
        return self.server.read_text(encoding="utf-8")

    # ---- 唯一命中与证据 ----

    def test_unique_anchor_apply_and_evidence(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("ping_extra", PING_ANCHOR,
                          insert=["            // extra a", "            // extra b"])],
            scope="dispatch")]))
        report, code = self.run_apply()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "PASS")
        content = self.read_server()
        anchor_block = "\n".join(PING_ANCHOR)
        self.assertIn(anchor_block + "\n            // extra a\n            // extra b\n", content)
        self.assertTrue(self.evidence.exists())
        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        run = evidence["runs"][-1]
        self.assertFalse(run["rolled_back"])
        self.assertIn("commit_before", run)
        self.assertIn("commit_after", run)
        record = run["files"][0]
        self.assertEqual(record["file_path"], "src/demo/server.rs")
        self.assertEqual(record["packages"], ["T050"])
        self.assertEqual(record["before_sha256"], sha(SERVER_FIXTURE))
        self.assertEqual(record["after_sha256"], sha(content))
        self.assertEqual(record["before_content"], SERVER_FIXTURE)
        op = record["ops"][0]
        self.assertEqual(op["pkg"], "T050")
        self.assertEqual(op["arm"], "ping_extra")
        self.assertEqual(op["anchor"]["lines"], PING_ANCHOR)
        start, end = op["line_range"]
        self.assertEqual(end - start + 1, 2)
        self.assertEqual(content.split("\n")[start - 1], "            // extra a")

    def test_insert_before_position(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs",
            ops=[write_op("before_two", ["line two"], insert=["inserted"], position="before")])]))
        report, code = self.run_apply()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.notes.read_text(encoding="utf-8"),
                         "line one\ninserted\nline two\nline three\n")

    # ---- 冲突：缺失 / 重复 / 同 arm ----

    def test_anchor_missing_fails_no_write(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("ghost", ["        this line does not exist"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("ANCHOR_MISSING", self.codes(report))
        self.assertEqual(self.read_server(), SERVER_FIXTURE)
        self.assertFalse(self.evidence.exists())

    def test_anchor_ambiguous_fails(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("dup", ["            ok()"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("ANCHOR_AMBIGUOUS", self.codes(report))
        item = next(c for c in report["conflicts"] if c["code"] == "ANCHOR_AMBIGUOUS")
        self.assertIn("2", item["detail"])
        self.assertEqual(self.read_server(), SERVER_FIXTURE)

    def test_same_arm_conflict(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("shared_arm", ["line one"])])]))
        self.put(package("T051a", 1, 10, [entry(
            "src/demo/notes.rs", ops=[write_op("shared_arm", ["line three"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("ARM_CONFLICT", self.codes(report))
        self.assertEqual(self.notes.read_text(encoding="utf-8"), NOTES_FIXTURE)

    def test_target_moved_by_previous_patch(self):
        self.put(package("T050", 1, 10, [entry(
            "src/demo/notes.rs",
            ops=[write_op("first", ["line one"], insert=["inserted"])])]))
        self.put(package("T051a", 1, 20, [entry(
            "src/demo/notes.rs",
            ops=[write_op("second", ["line one", "line two"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("ANCHOR_MOVED", self.codes(report))
        item = next(c for c in report["conflicts"] if c["code"] == "ANCHOR_MOVED")
        self.assertEqual(item["line"], 1)
        self.assertEqual(self.notes.read_text(encoding="utf-8"), NOTES_FIXTURE)

    def test_chained_anchor_on_inserted_lines_fails(self):
        self.put(package("T050", 1, 10, [entry(
            "src/demo/notes.rs",
            ops=[write_op("first", ["line one"], insert=["new line"])])]))
        self.put(package("T051a", 1, 20, [entry(
            "src/demo/notes.rs",
            ops=[write_op("second", ["new line"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("OVERLAP_CONFLICT", self.codes(report))

    # ---- 波次与包序 ----

    def test_order_within_wave_decides_layout(self):
        self.put(package("T050", 1, 20, [entry(
            "src/demo/notes.rs", ops=[write_op("a", ["line two"], insert=["A"])])]))
        self.put(package("T051a", 1, 10, [entry(
            "src/demo/notes.rs", ops=[write_op("b", ["line two"], insert=["B"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 0, report)
        # order 10 的 B 先插入，order 20 的 A 后插入同一锚点之后 → A 在 B 前。
        self.assertEqual(self.notes.read_text(encoding="utf-8"),
                         "line one\nline two\nA\nB\nline three\n")
        ops = report["files"][0]["ops"]
        self.assertEqual([o["pkg"] for o in ops], ["T051a", "T050"])

    def test_wave_filter_selects_packages(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("w1", ["line one"], insert=["W1"])])]))
        self.put(package("T051a", 2, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("w2", ["line three"], insert=["W2"])])]))
        report, code = self.run_apply(wave=1)
        self.assertEqual(code, 0, report)
        content = self.notes.read_text(encoding="utf-8")
        self.assertIn("W1", content)
        self.assertNotIn("W2", content)
        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(evidence["runs"][-1]["wave"], 1)

    # ---- 热点 reconciler 规则 ----

    def test_hotspot_non_last_writer_fails(self):
        self.put(package("T051a", 2, 10, [entry(
            "src/demo/server.rs", ops=[write_op("early_write", PING_ANCHOR)])]))
        self.put(package("T050", 2, 90, [entry(
            "src/demo/server.rs",
            ops=[write_op("reconcile", PING_ANCHOR, insert=["            // reconciled"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("HOTSPOT_RECONCILER", self.codes(report))
        item = next(c for c in report["conflicts"] if c["code"] == "HOTSPOT_RECONCILER")
        self.assertEqual(item["pkg"], "T051a")
        self.assertIn("T050", item["suggestion"])
        self.assertEqual(self.read_server(), SERVER_FIXTURE)

    def test_hotspot_reconciler_generate_passes(self):
        self.put(package("T051a", 2, 10, [entry(
            "src/demo/server.rs",
            route_additions=[{"method": "GET", "path": "/v2/agent/ping",
                              "handler": "agent_ping", "capability_bits": ["search"]}])]))
        self.put(package("T050", 2, 90, [entry(
            "src/demo/server.rs",
            ops=[write_op("reconcile_routes", PING_ANCHOR, generate={
                "from": "route_additions",
                "template": '        ("${method}", [${path_segments}]) => {\n'
                            "            ${handler}()\n        }"})],
            scope="dispatch")]))
        report, code = self.run_apply()
        self.assertEqual(code, 0, report)
        content = self.read_server()
        self.assertIn('        ("GET", ["v2", "agent", "ping"]) => {\n'
                      "            agent_ping()\n        }\n", content)
        self.assertIn("2|src/demo/server.rs|route_additions",
                      json.loads(self.evidence.read_text(encoding="utf-8"))["runs"][-1]["declarations"])

    def test_dangling_declaration_fails(self):
        self.put(package("T051a", 2, 10, [entry(
            "src/demo/server.rs",
            route_additions=[{"method": "GET", "path": "/v2/agent/ping",
                              "handler": "agent_ping", "capability_bits": []}])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("DANGLING_DECLARATION", self.codes(report))

    def test_generate_empty_fails(self):
        self.put(package("T050", 2, 90, [entry(
            "src/demo/server.rs",
            ops=[write_op("reconcile_routes", PING_ANCHOR, generate={
                "from": "route_additions", "template": "        // ${handler}"})])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("GENERATE_EMPTY", self.codes(report))

    def test_route_duplicate_declaration_fails(self):
        route = {"method": "GET", "path": "/v2/agent/ping",
                 "handler": "agent_ping", "capability_bits": []}
        self.put(package("T051a", 2, 10, [entry(
            "src/demo/server.rs", route_additions=[route])]))
        self.put(package("T052", 2, 20, [entry(
            "src/demo/server.rs", route_additions=[dict(route)])]))
        self.put(package("T050", 2, 90, [entry(
            "src/demo/server.rs",
            ops=[write_op("reconcile_routes", PING_ANCHOR, generate={
                "from": "route_additions", "template": "        // ${handler}"})])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("ROUTE_DUPLICATE", self.codes(report))

    # ---- 函数作用域 ----

    def test_function_scope_disambiguates_anchor(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("scoped", ["            ok()"], insert=["            // scoped"])],
            scope="dispatch")]))
        report, code = self.run_apply()
        self.assertEqual(code, 0, report)
        content = self.read_server()
        self.assertEqual(content.count("// scoped"), 1)
        self.assertLess(content.index("// scoped"), content.index("fn helper"))

    def test_scope_missing_fails(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("scoped", PING_ANCHOR)], scope="no_such_fn")]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_MISSING", self.codes(report))

    def test_scope_ambiguous_fails(self):
        self.server.write_text(SERVER_FIXTURE + "\nfn dispatch() {\n    twin()\n}\n",
                               encoding="utf-8")
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("scoped", PING_ANCHOR)], scope="dispatch")]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_AMBIGUOUS", self.codes(report))

    def test_scope_escape_rejected(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("escape", ["    Ok(outcome)", "}"], insert=["// outside"])],
            scope="dispatch")]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_ESCAPE", self.codes(report))

    # ---- dry-run / list ----

    def test_dry_run_writes_nothing(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs", ops=[write_op("ping_extra", PING_ANCHOR)])]))
        report, code = self.run_apply(dry=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(self.read_server(), SERVER_FIXTURE)
        self.assertFalse(self.evidence.exists())

    def test_list_mode_needs_no_target_files(self):
        self.server.unlink()
        self.notes.unlink()
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs", ops=[write_op("ping_extra", PING_ANCHOR)])]))
        report, code = ap.execute_list(str(self.patches), None)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["plan"][0]["pkg"], "T050")
        self.assertTrue(report["plan"][0]["entries"][0]["hotspot"])

    # ---- 回滚 ----

    def test_rollback_roundtrip_restores_bytes(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs",
            ops=[write_op("ping_extra", PING_ANCHOR,
                          insert=["            // extra a", "            // extra b"])])]))
        _, code = self.run_apply()
        self.assertEqual(code, 0)
        self.assertNotEqual(self.read_server(), SERVER_FIXTURE)
        report, code = self.run_rollback()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.read_server(), SERVER_FIXTURE)
        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertTrue(evidence["runs"][-1]["rolled_back"])
        report, code = self.run_rollback()
        self.assertEqual(code, 1)
        self.assertIn("ROLLBACK_NOTHING", self.codes(report))

    def test_rollback_drift_detected(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("n", ["line one"], insert=["X"])])]))
        _, code = self.run_apply()
        self.assertEqual(code, 0)
        self.notes.write_text(self.notes.read_text(encoding="utf-8") + "manual edit\n",
                              encoding="utf-8")
        drifted = self.notes.read_text(encoding="utf-8")
        report, code = self.run_rollback()
        self.assertEqual(code, 1)
        self.assertIn("ROLLBACK_DRIFT", self.codes(report))
        self.assertEqual(self.notes.read_text(encoding="utf-8"), drifted)

    def test_rollback_lifo_order(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("r1", ["line one"], insert=["R1"])])]))
        _, code = self.run_apply()
        self.assertEqual(code, 0)
        run1 = json.loads(self.evidence.read_text(encoding="utf-8"))["runs"][-1]["run_id"]
        self.put(package("T051a", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("r2", ["line three"], insert=["R2"])])]))
        (self.patches / "T050.json").unlink()
        _, code = self.run_apply()
        self.assertEqual(code, 0)
        report, code = self.run_rollback(run_id=run1)
        self.assertEqual(code, 1)
        self.assertIn("ROLLBACK_ORDER", self.codes(report))
        report, code = self.run_rollback()
        self.assertEqual(code, 0, report)
        report, code = self.run_rollback(run_id=run1)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.notes.read_text(encoding="utf-8"), NOTES_FIXTURE)

    def test_rollback_dry_run_verifies_only(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("n", ["line one"], insert=["X"])])]))
        _, code = self.run_apply()
        self.assertEqual(code, 0)
        applied = self.notes.read_text(encoding="utf-8")
        report, code = self.run_rollback(dry=True)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.notes.read_text(encoding="utf-8"), applied)

    # ---- 输入校验 ----

    def test_file_missing_fails(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/absent.rs", ops=[write_op("x", ["line one"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("FILE_MISSING", self.codes(report))

    def test_crlf_rejected(self):
        self.notes.write_bytes(NOTES_FIXTURE.replace("\n", "\r\n").encode("utf-8"))
        self.put(package("T050", 1, 0, [entry(
            "src/demo/notes.rs", ops=[write_op("x", ["line one"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("CRLF_UNSUPPORTED", self.codes(report))

    def test_unknown_field_rejected(self):
        bad = package("T050", 1, 0, [entry("src/demo/notes.rs")])
        bad["patches"][0]["match_arms"] = [{
            "arm": "x", "anchor": {"lines": ["line one"]}, "inserts": ["typo"]}]
        self.put(bad)
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("PATCH_JSON_INVALID", self.codes(report))

    def test_path_escape_rejected(self):
        self.put(package("T050", 1, 0, [entry(
            "../outside.rs", ops=[write_op("x", ["line one"])])]))
        report, code = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("PATCH_JSON_INVALID", self.codes(report))

    # ---- CLI  wiring ----

    def cli(self, *extra):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT)] + list(extra),
            capture_output=True, text=True, timeout=60)

    def test_cli_help(self):
        proc = self.cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--rollback", proc.stdout)
        self.assertIn("--selftest", proc.stdout)

    def test_cli_selftest(self):
        proc = self.cli("--selftest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "PASS")

    def test_cli_apply_dry_run_and_default_evidence(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs", ops=[write_op("ping_extra", PING_ANCHOR)])]))
        proc = self.cli("--root", str(self.root), "--wave", "1", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "PASS")
        self.assertEqual(self.read_server(), SERVER_FIXTURE)
        proc = self.cli("--root", str(self.root), "--wave", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        default_evidence = self.root / "tests" / "v2" / "artifacts" / "patches_applied.json"
        self.assertTrue(default_evidence.exists())
        proc = self.cli("--root", str(self.root), "--rollback")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_server(), SERVER_FIXTURE)

    def test_cli_conflict_exit_code(self):
        self.put(package("T050", 1, 0, [entry(
            "src/demo/server.rs", ops=[write_op("ghost", ["no such anchor line"])])]))
        proc = self.cli("--root", str(self.root), "--dry-run")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["status"], "FAIL")
        self.assertIn("ANCHOR_MISSING", proc.stderr)

    def test_cli_missing_patches_dir_exit_two(self):
        proc = self.cli("--root", str(self.root), "--patches-dir", "no-such-dir", "--list")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
