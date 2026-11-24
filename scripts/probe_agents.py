#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T044-B 本机执行体探针（plan.md §四；tasks.md T044 执行体探针项）。

逐个探测本机执行体，输出 JSON 证据到 evidence/agents/<YYYYMMDD>.json
（/evidence/ 已 gitignore，不入库）。探测只读、不改任何配置：
  * 版本：claude/codex/dsh/agy/gemini --version（gemini 为 -v/--version）。
  * 非交互 PONG：claude -p（Sonnet 档失败回落默认路由并留档）、
    codex exec --sandbox read-only --skip-git-repo-check、
    dsh headless --permission-mode read-only -（stdin；dsh 无 --sandbox 参数）、
    agy -p、gemini -p（撞目录信任闸门时加 --skip-trust 复探，help 实测参数）。
    全部 CLI 在空临时目录里跑，提示词只有
    "Reply with exactly PONG and nothing else."——不把仓库内容、本机路径、
    内网信息写进任何外部请求（plan §四 隐私红线）。
  * 模型后端：GET http://127.0.0.1:11434/api/tags（只读），核对
    qwen3:4b-instruct-2507-q4_K_M 与 bge-m3 在位。ollama 是模型后端不是
    执行体，单独记录可用性，不参与三档结论。
  * 每个 CLI 执行体三档结论：can_write_code（可写代码）/ review_only
    （仅可评审）/ unavailable（不可用＋原因）。codex/dsh 本探针只证只读档，
    按 plan §三.7/§四 写档须另过 workspace-write 探针，故上限 review_only。
  * 可能挂起的 CLI 一律 subprocess timeout（超时杀进程组），失败原文入档。

模式与退出码：
  --list      探针名册（JSON stdout），exit 0
  --dry-run   探针计划（命令/超时/输出路径），不执行任何子进程与网络，exit 0
  （默认）    实跑全部探针并写证据 JSON；写盘成功 exit 0（执行体不可用是
              数据不是脚本失败），写盘失败 exit 1
  --selftest  单测（stub runner＋真实超时路径＋ollama 解析），exit 0/1
  用法错误 exit 2。Python 3.9，仅标准库。禁止 cargo（本脚本从不运行 cargo）。
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PONG_PROMPT = "Reply with exactly PONG and nothing else."
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
EXPECTED_MODELS = ("qwen3:4b-instruct-2507-q4_K_M", "bge-m3")
VERSION_TIMEOUT = 20
PONG_TIMEOUT = 180
SHORT_TIMEOUT = 90
HTTP_TIMEOUT = 5
MAX_CAPTURE = 65536
# plan §五 步6 钉住矩阵＋本轮基线（tasks.md 执行体代号段）
PINNED = {"claude": "2.1.308", "codex": "0.171.0", "dsh": "0.2.2-rc.2",
          "agy": "1.5.15", "gemini": None}


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(args, timeout, cwd, stdin_text=None):
    """只读子进程探针。超时杀整个进程组，绝不无限等。失败原文保留。"""
    ev = {"command": list(args), "timeout_s": timeout, "cwd": str(cwd),
          "exit": None, "duration_ms": None, "stdout": "", "stderr": "",
          "timed_out": False, "error": None, "truncated": False}
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(args), cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
    except OSError as e:
        ev["error"] = "spawn failed: %s" % e
        ev["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return ev
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        ev["exit"] = proc.returncode
        ev["stdout"], ev["stderr"] = out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        out, err = proc.communicate()
        ev["timed_out"] = True
        ev["exit"] = proc.returncode
        ev["stdout"], ev["stderr"] = out or "", err or ""
        ev["error"] = "timeout after %ss (process group killed)" % timeout
    ev["duration_ms"] = int((time.monotonic() - t0) * 1000)
    for key in ("stdout", "stderr"):
        if len(ev[key]) > MAX_CAPTURE:
            ev[key] = ev[key][:MAX_CAPTURE]
            ev["truncated"] = True
    return ev


def pong_ok(ev):
    return ev.get("exit") == 0 and "PONG" in (ev.get("stdout") or "")


def executor_specs():
    """命令形态全部来自本机 --help 实测与 plan §四 启动模板，不臆造参数。"""
    return [
        {"name": "claude", "tier_on_success": "can_write_code",
         "version_args": ["--version"],
         "attempts": [
             {"args": ["-p", "--model", "sonnet", "--output-format", "json", PONG_PROMPT],
              "label": "sonnet 档（plan §四 探针确认路由）"},
             {"args": ["-p", "--output-format", "json", PONG_PROMPT],
              "label": "默认路由回落（Sonnet 档失败时）"}],
         "note": "CC 实现执行体；写权限按派工 permission-mode，探针只证非交互可用。"},
        {"name": "codex", "tier_on_success": "review_only",
         "version_args": ["--version"],
         "attempts": [
             {"args": ["exec", "--sandbox", "read-only", "--skip-git-repo-check", PONG_PROMPT],
              "label": "read-only 评审档"}],
         "note": "只读档已证即 review_only；workspace-write 须另探针（plan §三.7），read-only codex 不得承担装配写。"},
        {"name": "dsh", "tier_on_success": "review_only",
         "version_args": ["--version"],
         "attempts": [
             {"args": ["headless", "--permission-mode", "read-only", "-"],
              "stdin": PONG_PROMPT, "label": "headless read-only 档"}],
         "note": "dsh headless 无 --sandbox 参数（实测 help 确认）；写预置 workspace-write 须另探针，失败由 CC 代写。"},
        {"name": "agy", "tier_on_success": "can_write_code",
         "version_args": ["--version"],
         "attempts": [{"args": ["-p", PONG_PROMPT], "label": "print 非交互档"}],
         "note": "基线：账号地区资格被拒，等待账号恢复本轮不派包。"},
        {"name": "gemini", "tier_on_success": "can_write_code",
         "version_args": ["--version"],
         "attempts": [
             {"args": ["-p", PONG_PROMPT], "label": "headless 档（默认信任检查）"},
             {"args": ["--skip-trust", "-p", PONG_PROMPT],
              "label": "headless 档＋--skip-trust（help 实测参数，穿透目录信任闸门看真实服务状态；--skip-trust 须在 -p 前，否则 yargs 报缺参）"}],
         "note": "基线：CLI 已 EOL。空临时目录会先撞目录信任闸门（exit 55），第二跳用 --skip-trust 取真实失败原文。"},
    ]


def probe_executor(spec, runner, pong_timeout, work_root):
    """runner(args, timeout, cwd, stdin_text) -> 证据 dict。可注入 stub。"""
    name = spec["name"]
    binary = shutil.which(name)
    rec = {"name": name, "kind": "cli", "binary": binary,
           "version": None, "version_probe": None, "matches_pin": None,
           "attempts": [], "noninteractive_ok": False,
           "verdict": "unavailable", "verdict_reason": "", "note": spec["note"]}
    if not binary:
        rec["verdict_reason"] = "binary not found on PATH"
        return rec
    cwd = Path(work_root) / (name + "-probe")
    cwd.mkdir(parents=True, exist_ok=True)
    ver = runner([binary] + spec["version_args"], VERSION_TIMEOUT, cwd, None)
    rec["version_probe"] = ver
    if ver.get("exit") == 0:
        text = (ver.get("stdout") or ver.get("stderr") or "").strip()
        rec["version"] = text.splitlines()[0] if text else None
    pin = PINNED.get(name)
    if pin and rec["version"]:
        rec["matches_pin"] = pin in rec["version"]
    for attempt in spec["attempts"]:
        ev = runner([binary] + attempt["args"], pong_timeout, cwd,
                    attempt.get("stdin"))
        ev["label"] = attempt["label"]
        rec["attempts"].append(ev)
        if pong_ok(ev):
            rec["noninteractive_ok"] = True
            break  # 回落仅在失败时发生
    if rec["noninteractive_ok"]:
        rec["verdict"] = spec["tier_on_success"]
        rec["verdict_reason"] = ("PONG 非交互探针通过（%s）" %
                                 rec["attempts"][-1].get("label", ""))
        if name == "claude":
            try:
                data = json.loads(rec["attempts"][-1]["stdout"])
                rec["routing_evidence"] = {
                    "result": data.get("result"),
                    "model_usage_keys": sorted((data.get("modelUsage") or {}).keys()),
                }
            except (ValueError, TypeError):
                pass
    else:
        reasons = []
        for ev in rec["attempts"]:
            if ev.get("timed_out"):
                reasons.append(ev["error"])
            elif ev.get("error"):
                reasons.append(ev["error"])
            else:
                snippet = ((ev.get("stderr") or "") + "\n" + (ev.get("stdout") or "")).strip()
                reasons.append("exit=%s: %s" % (ev.get("exit"), snippet[:2000]))
        rec["verdict_reason"] = "PONG 探针失败原文 -> " + " | ".join(reasons)
    return rec


def parse_tags(data):
    models = [m.get("name", "") for m in (data.get("models") or [])]
    present = {}
    for want in EXPECTED_MODELS:
        present[want] = any(m == want or m.startswith(want + ":") for m in models)
    return models, present


def probe_ollama(url=OLLAMA_TAGS_URL, timeout=HTTP_TIMEOUT, opener=None):
    ev = {"name": "ollama", "kind": "model_backend", "endpoint": url,
          "timeout_s": timeout, "reachable": False, "models": [],
          "expected_present": {}, "status": "unavailable",
          "duration_ms": None, "error": None,
          "note": "模型后端，非执行体；供 real_local_model 门禁与宿主探针使用，不参与三档结论。"}
    t0 = time.monotonic()
    try:
        open_fn = opener or urllib.request.urlopen
        with open_fn(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ev["reachable"] = True
        ev["models"], ev["expected_present"] = parse_tags(data)
        if all(ev["expected_present"].values()):
            ev["status"] = "available"
        else:
            ev["status"] = "degraded_missing_expected_models"
    except (urllib.error.URLError, OSError, ValueError) as e:
        ev["error"] = "%s: %s" % (type(e).__name__, e)
    ev["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return ev


def build_plan(pong_timeout, out_path):
    plan = {"mode": "dry_run", "privacy": "PONG-only prompt; empty temp cwd; "
            "no repo content/paths/intranet info in any external request",
            "executors": [], "model_backend": {"endpoint": OLLAMA_TAGS_URL,
                                              "timeout_s": HTTP_TIMEOUT},
            "out_path": str(out_path)}
    for spec in executor_specs():
        binary = shutil.which(spec["name"])
        plan["executors"].append({
            "name": spec["name"], "binary": binary,
            "version_command": ([binary] + spec["version_args"]) if binary else None,
            "pong_commands": [([binary] + a["args"]) if binary else None
                              for a in spec["attempts"]],
            "pong_stdin": [a.get("stdin") for a in spec["attempts"]],
            "timeout_s": pong_timeout,
            "tier_on_success": spec["tier_on_success"]})
    return plan


def default_out_path(now=None):
    now = now or datetime.now(timezone.utc)
    repo = Path(__file__).resolve().parent.parent
    return repo / "evidence" / "agents" / (now.strftime("%Y%m%d") + ".json")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T044-B 本机执行体只读探针：版本/认证资格/非交互 PONG/ollama 模型清单。")
    ap.add_argument("--list", action="store_true", help="探针名册")
    ap.add_argument("--dry-run", action="store_true", help="探针计划，不执行")
    ap.add_argument("--selftest", action="store_true", help="单测")
    ap.add_argument("--only", default=None, metavar="NAMES",
                    help="逗号分隔，只探这些（含 ollama）")
    ap.add_argument("--timeout", type=float, default=PONG_TIMEOUT,
                    help="PONG 探针超时秒数（默认 %s）" % PONG_TIMEOUT)
    ap.add_argument("--out", default=None, help="证据 JSON 路径（默认 evidence/agents/<日期>.json）")
    args = ap.parse_args(argv)

    if args.selftest:
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        run = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if run.wasSuccessful() else 1

    out_path = Path(args.out) if args.out else default_out_path()
    only = set(args.only.split(",")) if args.only else None

    if args.list:
        roster = [{"name": s["name"], "kind": "cli",
                   "tier_on_success": s["tier_on_success"], "note": s["note"]}
                  for s in executor_specs()]
        roster.append({"name": "ollama", "kind": "model_backend",
                       "endpoint": OLLAMA_TAGS_URL})
        print(json.dumps({"executors": roster}, ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        print(json.dumps(build_plan(args.timeout, out_path), ensure_ascii=False, indent=2))
        return 0

    evidence = {"schema_version": 1, "generated_at": iso_now(),
                "probe_mode": "read-only",
                "privacy": "PONG-only prompt; empty temp cwd; no repo content/paths/intranet info in any external request",
                "host": {"system": platform.system(), "machine": platform.machine(),
                         "python": platform.python_version()},
                "pinned_matrix": PINNED,
                "executors": [], "model_backends": []}
    work_root = Path(tempfile.mkdtemp(prefix="agent-probe-"))
    try:
        for spec in executor_specs():
            if only and spec["name"] not in only:
                continue
            evidence["executors"].append(
                probe_executor(spec, run_cmd, args.timeout, work_root))
        if not only or "ollama" in only:
            evidence["model_backends"].append(probe_ollama())
    finally:
        shutil.rmtree(str(work_root), ignore_errors=True)

    evidence["summary"] = {
        "can_write_code": [e["name"] for e in evidence["executors"]
                           if e["verdict"] == "can_write_code"],
        "review_only": [e["name"] for e in evidence["executors"]
                        if e["verdict"] == "review_only"],
        "unavailable": [e["name"] for e in evidence["executors"]
                        if e["verdict"] == "unavailable"],
        "ollama_status": (evidence["model_backends"][0]["status"]
                          if evidence["model_backends"] else "not_probed"),
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    except OSError as e:
        print(json.dumps({"error": "evidence write failed: %s" % e}, ensure_ascii=False))
        return 1
    print(json.dumps({"evidence_path": str(out_path), "summary": evidence["summary"]},
                     ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------
# selftest：stub runner 验证判定逻辑；真实子进程只用于超时路径（python sleep）。
# 不触碰网络、不触碰真实仓库、不运行 cargo。
# --------------------------------------------------------------------------

def fake_ev(exit=0, stdout="", stderr="", timed_out=False, error=None):
    return {"command": [], "timeout_s": 1, "cwd": "/tmp", "exit": exit,
            "duration_ms": 1, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "error": error, "truncated": False}


class ProbeSelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-selftest-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def spec(self, name):
        for s in executor_specs():
            if s["name"] == name:
                return s
        raise KeyError(name)

    def test_claude_success_can_write_code(self):
        calls = []

        def runner(args, timeout, cwd, stdin):
            calls.append(args)
            if "--version" in args:
                return fake_ev(stdout="2.1.308 (Claude Code)\n")
            return fake_ev(stdout=json.dumps({"result": "PONG", "modelUsage": {"sonnet": {}}}))

        rec = probe_executor(self.spec("claude"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "can_write_code")
        self.assertTrue(rec["matches_pin"])
        self.assertEqual(rec["routing_evidence"]["result"], "PONG")
        self.assertEqual(len(rec["attempts"]), 1)  # 成功即止，不回落

    def test_claude_sonnet_fail_falls_back(self):
        def runner(args, timeout, cwd, stdin):
            if "--version" in args:
                return fake_ev(stdout="2.1.308 (Claude Code)\n")
            if "sonnet" in args:
                return fake_ev(exit=1, stderr="model not found")
            return fake_ev(stdout=json.dumps({"result": "PONG"}))

        rec = probe_executor(self.spec("claude"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "can_write_code")
        self.assertEqual(len(rec["attempts"]), 2)

    def test_codex_success_is_review_only(self):
        def runner(args, timeout, cwd, stdin):
            if "--version" in args:
                return fake_ev(stdout="codex-cli 0.171.0\n")
            self.assertIn("--sandbox", args)
            self.assertIn("read-only", args)
            self.assertIn("--skip-git-repo-check", args)
            return fake_ev(stdout="PONG\n")

        rec = probe_executor(self.spec("codex"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "review_only")

    def test_dsh_uses_permission_mode_not_sandbox(self):
        seen = {}

        def runner(args, timeout, cwd, stdin):
            if "--version" in args:
                return fake_ev(stdout="0.2.2-rc.2\n")
            seen["args"] = args
            seen["stdin"] = stdin
            return fake_ev(stdout="PONG\n")

        rec = probe_executor(self.spec("dsh"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "review_only")
        self.assertIn("--permission-mode", seen["args"])
        self.assertNotIn("--sandbox", seen["args"])
        self.assertEqual(seen["stdin"], PONG_PROMPT)

    def test_region_denial_unavailable_verbatim(self):
        def runner(args, timeout, cwd, stdin):
            if "--version" in args:
                return fake_ev(stdout="1.5.15\n")
            return fake_ev(exit=1, stderr="not currently available in your location")

        rec = probe_executor(self.spec("agy"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "unavailable")
        self.assertIn("not currently available in your location", rec["verdict_reason"])

    def test_timeout_recorded_not_hang(self):
        def runner(args, timeout, cwd, stdin):
            if "--version" in args:
                return fake_ev(stdout="0.70.0\n")
            return fake_ev(timed_out=True, error="timeout after 5s (process group killed)")

        rec = probe_executor(self.spec("gemini"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "unavailable")
        self.assertIn("timeout", rec["verdict_reason"])

    def test_real_subprocess_timeout_kills(self):
        ev = run_cmd([sys.executable, "-c", "import time; time.sleep(30)"],
                     1, self.tmp)
        self.assertTrue(ev["timed_out"])
        self.assertIsNotNone(ev["error"])
        self.assertLess(ev["duration_ms"], 8000)

    def test_missing_binary_unavailable(self):
        spec = dict(self.spec("claude"))
        spec["name"] = "definitely-not-a-real-binary-xyz"
        rec = probe_executor(spec, lambda *a: self.fail("must not run"), 5, self.tmp)
        self.assertEqual(rec["verdict"], "unavailable")
        self.assertIn("not found", rec["verdict_reason"])

    def test_parse_tags_prefix_match(self):
        data = {"models": [{"name": "qwen3:4b-instruct-2507-q4_K_M"},
                           {"name": "bge-m3:latest"}, {"name": "qwen3:4b"}]}
        models, present = parse_tags(data)
        self.assertEqual(len(models), 3)
        self.assertTrue(present["qwen3:4b-instruct-2507-q4_K_M"])
        self.assertTrue(present["bge-m3"])
        _, present2 = parse_tags({"models": [{"name": "bge-m3"}]})
        self.assertFalse(present2["qwen3:4b-instruct-2507-q4_K_M"])

    def test_ollama_unreachable_recorded(self):
        def opener(url, timeout):
            raise urllib.error.URLError("connection refused")

        ev = probe_ollama(opener=opener)
        self.assertFalse(ev["reachable"])
        self.assertEqual(ev["status"], "unavailable")
        self.assertIn("connection refused", ev["error"])

    def test_ollama_available(self):
        import io

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        payload = json.dumps({"models": [
            {"name": "qwen3:4b-instruct-2507-q4_K_M"}, {"name": "bge-m3:latest"}]}).encode()

        def opener(url, timeout):
            return Resp(payload)

        ev = probe_ollama(opener=opener)
        self.assertEqual(ev["status"], "available")
        self.assertTrue(all(ev["expected_present"].values()))

    def test_dry_run_plan_executes_nothing(self):
        plan = build_plan(5, self.tmp / "e.json")
        self.assertEqual(plan["mode"], "dry_run")
        names = [e["name"] for e in plan["executors"]]
        self.assertEqual(names, ["claude", "codex", "dsh", "agy", "gemini"])


if __name__ == "__main__":
    sys.exit(main())
