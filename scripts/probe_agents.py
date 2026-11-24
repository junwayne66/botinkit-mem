#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T044-B/F 本机执行体探针（plan.md §四；tasks.md T044 执行体探针项）。

逐个探测本机执行体，输出 JSON 证据到 evidence/agents/<YYYYMMDD>.json
（/evidence/ 已 gitignore，不入库）。探测分两类：

只读探针（不改任何配置）：
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

写入探针（T044-F；plan §三/§四；只在临时目录内写，绝不写仓库/真实配置）：
  * codex workspace-write：codex exec --sandbox workspace-write
    --skip-git-repo-check --ephemeral -C <tmp>，提示词要求在 <tmp> 内创建
    test.txt 并回读；验证文件存在且内容匹配即判 can_write_code。
  * dsh workspace-write：dsh headless --permission-mode workspace-write
    --add-dir <tmp-resolved>，stdin 提示词要求在 <tmp-resolved> 内创建
    test.txt 并回读（macOS 须 realpath 解析 /tmp → /private/tmp，
    dsh 拒 symlinked grants）。验证同上。
  * claude 最小写实验（2026-11-24 实测修正）：裸 claude -p 在非交互下
    写文件被权限闸门拦截（permission_denials 记录 Write/Bash 拒绝，exit=0
    但文件未创建）。实测确认的安全授权方式：--permission-mode acceptEdits
    ＋--tools Write,Read ＋--allowedTools Write,Read ＋--add-dir <tmp>，
    并把整个会话隔离进临时 CLAUDE_CONFIG_DIR（不加载用户 settings/hooks，
    因而不写 ~/.bmem；--no-session-persistence ＋--setting-sources ""
    ＋--strict-mcp-config 保证不写 ~/.claude、不启 MCP）。路由经继承的
    ANTHROPIC_* 环境变量，与 PONG 探针同源。严禁 --dangerously-skip-permissions
    与 bypassPermissions。变长参数（--tools/--allowedTools/--add-dir）后必须
    紧跟另一个 flag，否则会吞掉位置 prompt（实测 "Input must be provided" 错误）。
    验证文件存在且内容精确匹配即判 can_write_code；exit=0 但文件未验证判
    write_unverified（可写但本机非交互档未证实），不再伪报 unavailable。

模型档位探针（plan §三.7；区分"CLI 可用但模型档不可用"）：
  * codex -m <model>：逐个探测 gpt-5.5 / gpt-5.4-mini / fable / terra 等
    台账点名档位。CLI 返回 exit!=0 且 stderr/stdout 含 "not supported" /
    "not exist" / "Model not" 即判 model_unavailable 并保留错误原文，
    避免派工计划继续引用不存在的模型。

每个 CLI 执行体三档结论：can_write_code / review_only / unavailable。
可能挂起的 CLI 一律 subprocess timeout（超时杀进程组），失败原文入档。

结论字段名统一（方便 plan.md §三.7/§四 引用）：executors / write_probes /
model_tiers 三类记录一律用 verdict ＋ verdict_reason 承载结论。写入探针另留
write_kind，模型档位另留 model；旧 write_verdict/model_verdict 已并入 verdict。
写入探针 verdict 取值：can_write_code / write_unverified / unavailable。
模型档位 verdict 取值：available / model_unavailable / cli_unavailable。

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
WRITE_PROBE_CONTENT = "WRITE_PROBE_OK"
WRITE_PROBE_FILENAME = "test.txt"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
EXPECTED_MODELS = ("qwen3:4b-instruct-2507-q4_K_M", "bge-m3")
VERSION_TIMEOUT = 20
PONG_TIMEOUT = 180
WRITE_TIMEOUT = 240  # T044-F 写入探针超时 ≤240s/次
SHORT_TIMEOUT = 90
HTTP_TIMEOUT = 5
MAX_CAPTURE = 65536
# plan §五 步6 钉住矩阵＋本轮基线（tasks.md 执行体代号段）
PINNED = {"claude": "2.1.308", "codex": "0.171.0", "dsh": "0.2.2-rc.2",
          "agy": "1.5.16", "gemini": None}  # agy 2026-11-24 实测 1.5.16（地区资格仍被拒）
# T044-F 模型档位探针：台账点名但实测可能不可用的 codex 模型档
CODEX_MODEL_TIERS = ("gpt-5.5", "gpt-5.4-mini", "fable", "terra")


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cmd(args, timeout, cwd, stdin_text=None, env=None):
    """只读子进程探针。超时杀整个进程组，绝不无限等。失败原文保留。

    env 为可选覆盖项，与 os.environ 合并（保留 PATH/ANTHROPIC_* 路由），
    仅用于把 claude 写入实验隔离进临时 CLAUDE_CONFIG_DIR。env 内容绝不入档
    （可能含密钥）；证据只记录 command/cwd，不记录环境。"""
    ev = {"command": list(args), "timeout_s": timeout, "cwd": str(cwd),
          "exit": None, "duration_ms": None, "stdout": "", "stderr": "",
          "timed_out": False, "error": None, "truncated": False}
    proc_env = dict(os.environ)
    proc_env.update(env or {})
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(args), cwd=str(cwd), env=proc_env,
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


def resolve_symlink(path):
    """macOS /tmp -> /private/tmp 解析；dsh 拒 symlinked grants 必须 realpath。"""
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def write_probe_prompt(target_dir):
    """写入探针提示词：在指定目录创建 test.txt 并回读验证。

    提示词明确要求只在 target_dir 内写，不写其他位置；回读内容必须精确匹配
    WRITE_PROBE_CONTENT。提示词纯英文避免中文路由干扰执行体工具选择。
    """
    return (
        "Create a file named {fname} in the directory {target} with the exact "
        "content '{content}' (no trailing newline), then read it back to confirm "
        "the write succeeded. Do not write anywhere else."
    ).format(fname=WRITE_PROBE_FILENAME, target=target_dir, content=WRITE_PROBE_CONTENT)


def verify_write_probe(target_dir):
    """验证写入探针结果：文件存在且内容精确匹配。返回 (ok, detail)。"""
    target = Path(target_dir) / WRITE_PROBE_FILENAME
    if not target.exists():
        return False, "file %s not found in %s" % (WRITE_PROBE_FILENAME, target_dir)
    try:
        actual = target.read_text(encoding="utf-8")
    except OSError as e:
        return False, "read failed: %s" % e
    if actual == WRITE_PROBE_CONTENT:
        return True, "content matches exactly (%d bytes)" % len(actual)
    return False, "content mismatch: got %r (len=%d), want %r" % (
        actual[:200], len(actual), WRITE_PROBE_CONTENT)


def write_probe_specs():
    """写入探针规格。每条 spec 描述一个执行体的写入实验。

    write_kind:
      - codex_workspace_write: codex exec --sandbox workspace-write
      - dsh_workspace_write: dsh headless --permission-mode workspace-write
      - claude_minimal_write: claude -p 最小写实验（已派工 can_write_code 的实证）
    """
    return [
        {"name": "claude", "write_kind": "claude_minimal_write",
         "note": "claude 最小写实验：acceptEdits＋Write,Read＋--add-dir <tmp>，"
                 "隔离 CLAUDE_CONFIG_DIR 进 tmp（不写仓库/~/.bmem/~/.claude）；"
                 "实测授权写成功才判 can_write_code，否则 write_unverified。"},
        {"name": "codex", "write_kind": "codex_workspace_write",
         "note": "codex workspace-write 探针（plan §三.7 派工依据）；在 tmp 内写 test.txt 并回读。"},
        {"name": "dsh", "write_kind": "dsh_workspace_write",
         "note": "dsh workspace-write 探针；macOS 须 realpath 解析 /tmp → /private/tmp，"
                 "dsh 拒 symlinked grants。"},
    ]


def probe_executor_write(spec, runner, write_timeout, work_root):
    """写入探针执行。runner(args, timeout, cwd, stdin_text) -> 证据 dict。

    写入实验只在临时目录内进行，绝不写仓库/真实配置。返回 rec 字段：
      write_probe: {command, exit, duration_ms, stdout, stderr, timed_out, error,
                    target_dir, file_verified, verify_detail}
      verdict: can_write_code / write_unverified / unavailable（统一结论字段）
      verdict_reason: 失败原文或验证成功说明
    """
    name = spec["name"]
    binary = shutil.which(name)
    rec = {"name": name, "write_kind": spec["write_kind"], "binary": binary,
           "write_probe": None, "verdict": "unavailable",
           "verdict_reason": "", "note": spec["note"]}
    if not binary:
        rec["verdict_reason"] = "binary not found on PATH"
        return rec
    # 写入实验目录：每轮新建，用完即弃
    write_dir = Path(work_root) / (name + "-write")
    write_dir.mkdir(parents=True, exist_ok=True)
    # macOS dsh 必须 realpath，否则 symlinked grant 被拒
    write_dir_resolved = resolve_symlink(str(write_dir))
    prompt = write_probe_prompt(write_dir_resolved)
    env = None

    if spec["write_kind"] == "codex_workspace_write":
        args = [binary, "exec", "--sandbox", "workspace-write",
                "--skip-git-repo-check", "--ephemeral", "-C", write_dir_resolved, prompt]
        ev = runner(args, write_timeout, write_dir, None)
    elif spec["write_kind"] == "dsh_workspace_write":
        args = [binary, "headless", "--permission-mode", "workspace-write",
                "--add-dir", write_dir_resolved, "-"]
        ev = runner(args, write_timeout, write_dir, prompt)
    elif spec["write_kind"] == "claude_minimal_write":
        # 实测安全授权（2026-11-24）：acceptEdits 自动批准 cwd/add-dir 内的
        # Write/Edit；--tools Write,Read 去掉 Bash，模型无法越界写。整个会话
        # 隔离进临时 CLAUDE_CONFIG_DIR（不加载用户 settings/hooks → 不写
        # ~/.bmem；--no-session-persistence → 不写 ~/.claude/projects）。
        # 路由经继承的 ANTHROPIC_* 环境变量。变长 flag 后紧跟另一 flag，
        # prompt 放最后，避免被 --allowedTools/--add-dir 吞掉。
        config_dir = Path(work_root) / (name + "-write-config")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_resolved = resolve_symlink(str(config_dir))
        args = [binary, "-p",
                "--tools", "Write,Read",
                "--allowedTools", "Write,Read",
                "--add-dir", write_dir_resolved,
                "--permission-mode", "acceptEdits",
                "--permission-prompts", "none",
                "--no-session-persistence",
                "--setting-sources", "",
                "--strict-mcp-config",
                "--output-format", "json",
                prompt]
        env = {"CLAUDE_CONFIG_DIR": config_resolved,
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        ev = runner(args, write_timeout, write_dir_resolved, None, env)
    else:
        rec["verdict_reason"] = "unknown write_kind: %s" % spec["write_kind"]
        return rec

    ev["target_dir"] = write_dir_resolved
    # 验证写入结果
    ok, detail = verify_write_probe(write_dir_resolved)
    ev["file_verified"] = ok
    ev["verify_detail"] = detail
    rec["write_probe"] = ev

    # 判定优先级：timeout > error > (exit==0 且 file_verified) > (exit==0 但 file 未验证) > 其他
    if ev.get("timed_out"):
        rec["verdict_reason"] = "timeout: %s" % ev.get("error", "")
    elif ev.get("error"):
        rec["verdict_reason"] = "error: %s" % ev["error"]
    elif ev.get("exit") == 0 and ok:
        rec["verdict"] = "can_write_code"
        rec["verdict_reason"] = "write probe passed: %s" % detail
    elif ev.get("exit") == 0 and not ok:
        # exit==0 但文件未创建/内容不匹配：可写但本机非交互档未证实，不伪报成功
        rec["verdict"] = "write_unverified"
        rec["verdict_reason"] = "exit=0 but file verification failed: %s" % detail
    else:
        snippet = ((ev.get("stderr") or "") + "\n" + (ev.get("stdout") or "")).strip()
        rec["verdict_reason"] = "exit=%s: %s" % (ev.get("exit"), snippet[:2000])
    return rec


def model_tier_specs():
    """模型档位探针规格。目前只覆盖 codex（台账点名的 gpt-5.5 / fable 等）。"""
    return [
        {"executor": "codex", "models": list(CODEX_MODEL_TIERS),
         "note": "codex 模型档位探针（plan §三.7）；区分 CLI 可用但模型档不可用。"},
    ]


def probe_model_tier(executor, model, runner, timeout, work_root):
    """探测单个模型档位。返回 rec 字段：
      model, command, exit, duration_ms, stdout, stderr, timed_out, error,
      verdict: available / model_unavailable / cli_unavailable（统一结论字段）
      verdict_reason: 错误原文或成功说明
    """
    binary = shutil.which(executor)
    rec = {"executor": executor, "model": model, "binary": binary,
           "verdict": "cli_unavailable", "verdict_reason": "",
           "command": None, "exit": None, "duration_ms": None,
           "stdout": "", "stderr": "", "timed_out": False, "error": None}
    if not binary:
        rec["verdict_reason"] = "binary %s not found on PATH" % executor
        return rec
    cwd = Path(work_root) / (executor + "-model-" + model.replace("/", "-"))
    cwd.mkdir(parents=True, exist_ok=True)
    args = [binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
            "--ephemeral", "-m", model, PONG_PROMPT]
    rec["command"] = list(args)
    ev = runner(args, timeout, cwd, None)
    rec.update({"exit": ev.get("exit"), "duration_ms": ev.get("duration_ms"),
                "stdout": ev.get("stdout", ""), "stderr": ev.get("stderr", ""),
                "timed_out": ev.get("timed_out", False), "error": ev.get("error")})
    # 判定：exit==0 且 stdout 含 PONG 即 available；否则看错误原文判 model_unavailable
    if ev.get("exit") == 0 and "PONG" in (ev.get("stdout") or ""):
        rec["verdict"] = "available"
        rec["verdict_reason"] = "PONG success"
    elif ev.get("timed_out"):
        rec["verdict"] = "model_unavailable"  # 超时也归类为模型不可用
        rec["verdict_reason"] = "timeout: %s" % ev.get("error", "")
    else:
        combined = ((ev.get("stderr") or "") + "\n" + (ev.get("stdout") or "")).lower()
        # 常见模型不可用错误模式（实测 codex 返回 "not supported" / "not exist"）
        unavailable_markers = ("not supported", "not exist", "model not",
                               "unknown model", "invalid model")
        if any(m in combined for m in unavailable_markers):
            rec["verdict"] = "model_unavailable"
            snippet = ((ev.get("stderr") or "") + "\n" + (ev.get("stdout") or "")).strip()
            rec["verdict_reason"] = "model unavailable: %s" % snippet[:2000]
        else:
            rec["verdict"] = "model_unavailable"  # 保守归类
            snippet = ((ev.get("stderr") or "") + "\n" + (ev.get("stdout") or "")).strip()
            rec["verdict_reason"] = "exit=%s: %s" % (ev.get("exit"), snippet[:2000])
    return rec


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
            "executors": [], "write_probes": [], "model_tiers": [],
            "model_backend": {"endpoint": OLLAMA_TAGS_URL, "timeout_s": HTTP_TIMEOUT},
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
    # T044-F 写入探针计划
    for wspec in write_probe_specs():
        binary = shutil.which(wspec["name"])
        plan["write_probes"].append({
            "name": wspec["name"], "write_kind": wspec["write_kind"],
            "binary": binary, "timeout_s": WRITE_TIMEOUT,
            "note": wspec["note"]})
    # 模型档位探针计划
    for mspec in model_tier_specs():
        plan["model_tiers"].append({
            "executor": mspec["executor"], "models": mspec["models"],
            "timeout_s": pong_timeout, "note": mspec["note"]})
    return plan


def default_out_path(now=None):
    now = now or datetime.now(timezone.utc)
    repo = Path(__file__).resolve().parent.parent
    return repo / "evidence" / "agents" / (now.strftime("%Y%m%d") + ".json")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="T044-B/F 本机执行体探针：只读 PONG/写入实验/模型档位/ollama。")
    ap.add_argument("--list", action="store_true", help="探针名册")
    ap.add_argument("--dry-run", action="store_true", help="探针计划，不执行")
    ap.add_argument("--selftest", action="store_true", help="单测")
    ap.add_argument("--only", default=None, metavar="NAMES",
                    help="逗号分隔，只探这些（含 ollama）")
    ap.add_argument("--timeout", type=float, default=PONG_TIMEOUT,
                    help="PONG/模型档位探针超时秒数（默认 %s）" % PONG_TIMEOUT)
    ap.add_argument("--write-timeout", type=float, default=WRITE_TIMEOUT,
                    help="写入探针超时秒数（默认 %s）" % WRITE_TIMEOUT)
    ap.add_argument("--skip-write", action="store_true",
                    help="跳过写入探针（只跑只读 PONG）")
    ap.add_argument("--skip-model-tiers", action="store_true",
                    help="跳过模型档位探针")
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
        write_roster = [{"name": s["name"], "write_kind": s["write_kind"],
                         "note": s["note"]} for s in write_probe_specs()]
        model_roster = [{"executor": s["executor"], "models": s["models"],
                         "note": s["note"]} for s in model_tier_specs()]
        print(json.dumps({"executors": roster, "write_probes": write_roster,
                          "model_tiers": model_roster}, ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        print(json.dumps(build_plan(args.timeout, out_path), ensure_ascii=False, indent=2))
        return 0

    evidence = {"schema_version": 2, "generated_at": iso_now(),
                "probe_mode": "read-only+write+model-tiers",
                "privacy": "PONG/write prompts in empty temp cwd; no repo content/paths/intranet info in any external request; write experiments only in tempfile.mkdtemp, never in repo or real config",
                "host": {"system": platform.system(), "machine": platform.machine(),
                         "python": platform.python_version()},
                "pinned_matrix": PINNED,
                "executors": [], "write_probes": [], "model_tiers": [],
                "model_backends": []}
    work_root = Path(tempfile.mkdtemp(prefix="agent-probe-"))
    try:
        # 只读 PONG 探针
        for spec in executor_specs():
            if only and spec["name"] not in only:
                continue
            evidence["executors"].append(
                probe_executor(spec, run_cmd, args.timeout, work_root))
        # T044-F 写入探针
        if not args.skip_write:
            for wspec in write_probe_specs():
                if only and wspec["name"] not in only:
                    continue
                evidence["write_probes"].append(
                    probe_executor_write(wspec, run_cmd, args.write_timeout, work_root))
        # 模型档位探针
        if not args.skip_model_tiers:
            for mspec in model_tier_specs():
                if only and mspec["executor"] not in only:
                    continue
                for model in mspec["models"]:
                    evidence["model_tiers"].append(
                        probe_model_tier(mspec["executor"], model, run_cmd,
                                         args.timeout, work_root))
        if not only or "ollama" in only:
            evidence["model_backends"].append(probe_ollama())
    finally:
        shutil.rmtree(str(work_root), ignore_errors=True)

    # 综合判定：只读 PONG  verdict + 写入 verdict 取较严格的
    # can_write_code 需 PONG ok 且 write_probes.verdict==can_write_code
    # review_only 需 PONG ok 但 write 未通过或未探
    # unavailable 即 PONG 失败
    def final_verdict(exec_name):
        pong_rec = next((e for e in evidence["executors"] if e["name"] == exec_name), None)
        write_rec = next((w for w in evidence["write_probes"] if w["name"] == exec_name), None)
        if not pong_rec or pong_rec["verdict"] == "unavailable":
            return "unavailable"
        if write_rec and write_rec["verdict"] == "can_write_code":
            return "can_write_code"
        # PONG 成功但写入未通过/未探：保留原只读档结论
        return pong_rec["verdict"]

    evidence["summary"] = {
        "can_write_code": [e["name"] for e in evidence["executors"]
                           if final_verdict(e["name"]) == "can_write_code"],
        "review_only": [e["name"] for e in evidence["executors"]
                        if final_verdict(e["name"]) == "review_only"],
        "unavailable": [e["name"] for e in evidence["executors"]
                        if final_verdict(e["name"]) == "unavailable"],
        "write_verified": [w["name"] for w in evidence["write_probes"]
                           if w["verdict"] == "can_write_code"],
        "write_failed": [w["name"] for w in evidence["write_probes"]
                         if w["verdict"] != "can_write_code"],
        "write_unverified": [w["name"] for w in evidence["write_probes"]
                             if w["verdict"] == "write_unverified"],
        "model_tiers_available": [m["model"] for m in evidence["model_tiers"]
                                  if m["verdict"] == "available"],
        "model_tiers_unavailable": [m["model"] for m in evidence["model_tiers"]
                                    if m["verdict"] == "model_unavailable"],
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
                return fake_ev(stdout="1.5.16\n")
            return fake_ev(exit=1, stderr="not currently available in your location")

        rec = probe_executor(self.spec("agy"), runner, 5, self.tmp)
        self.assertEqual(rec["verdict"], "unavailable")
        self.assertIn("not currently available in your location", rec["verdict_reason"])
        # agy 钉住版本按 2026-11-24 实测更正为 1.5.16，不再永久假差异
        self.assertTrue(rec["matches_pin"])
        self.assertEqual(PINNED["agy"], "1.5.16")

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
        # T044-F: dry-run 也包含写入探针与模型档位
        self.assertIn("write_probes", plan)
        self.assertIn("model_tiers", plan)
        write_names = [w["name"] for w in plan["write_probes"]]
        self.assertIn("codex", write_names)
        self.assertIn("dsh", write_names)

    # ------------------------------------------------------------------
    # T044-F 写入探针单测
    # ------------------------------------------------------------------

    def wspec(self, name):
        for s in write_probe_specs():
            if s["name"] == name:
                return s
        raise KeyError(name)

    def test_write_probe_codex_success(self):
        """codex workspace-write 成功：exit==0 且文件验证通过。"""
        def runner(args, timeout, cwd, stdin):
            # 模拟 codex 写入成功
            # 在实际 runner 中，codex 会创建文件；这里模拟文件已存在
            target_dir = None
            for i, arg in enumerate(args):
                if arg == "-C" and i + 1 < len(args):
                    target_dir = args[i + 1]
                    break
            if target_dir:
                (Path(target_dir) / WRITE_PROBE_FILENAME).write_text(
                    WRITE_PROBE_CONTENT, encoding="utf-8")
            return fake_ev(exit=0, stdout="write confirmed\n")

        rec = probe_executor_write(self.wspec("codex"), runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "can_write_code")
        self.assertTrue(rec["write_probe"]["file_verified"])
        self.assertIn("matches", rec["verdict_reason"])

    def test_write_probe_dsh_success_with_realpath(self):
        """dsh workspace-write 成功：--add-dir 用 realpath 解析后的路径。"""
        seen_args = []

        def runner(args, timeout, cwd, stdin):
            seen_args.extend(args)
            # 模拟 dsh 写入成功
            target_dir = None
            for i, arg in enumerate(args):
                if arg == "--add-dir" and i + 1 < len(args):
                    target_dir = args[i + 1]
                    break
            if target_dir:
                (Path(target_dir) / WRITE_PROBE_FILENAME).write_text(
                    WRITE_PROBE_CONTENT, encoding="utf-8")
            return fake_ev(exit=0, stdout="write confirmed\n")

        rec = probe_executor_write(self.wspec("dsh"), runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "can_write_code")
        self.assertIn("--permission-mode", seen_args)
        self.assertIn("workspace-write", seen_args)
        self.assertIn("--add-dir", seen_args)
        self.assertNotIn("--sandbox", seen_args)

    def test_write_probe_claude_success(self):
        """claude 最小写实验成功：实测安全授权＋tmp 隔离，统一 verdict 字段。"""
        seen = {}

        def runner(args, timeout, cwd, stdin, env=None):
            seen["args"] = args
            seen["env"] = env
            seen["cwd"] = cwd
            if cwd:
                (Path(cwd) / WRITE_PROBE_FILENAME).write_text(
                    WRITE_PROBE_CONTENT, encoding="utf-8")
            return fake_ev(exit=0, stdout=json.dumps({"result": "done", "permission_denials": []}))

        rec = probe_executor_write(self.wspec("claude"), runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "can_write_code")
        self.assertTrue(rec["write_probe"]["file_verified"])
        args = seen["args"]
        # 实测确认的授权方式（严禁 dangerously-skip / bypassPermissions）
        self.assertIn("--permission-mode", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--no-session-persistence", args)
        self.assertIn("--setting-sources", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--add-dir", args)
        self.assertEqual(args[args.index("--tools") + 1], "Write,Read")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("bypassPermissions", args)
        # 变长 flag 后必须紧跟另一 flag，prompt 在最后（否则吞 prompt）
        self.assertEqual(args[-1], write_probe_prompt(rec["write_probe"]["target_dir"]))
        for variadic in ("--tools", "--allowedTools", "--add-dir"):
            nxt = args[args.index(variadic) + 2]
            self.assertTrue(nxt.startswith("--"), "%s 后须紧跟 flag" % variadic)
        # 会话隔离进临时 CLAUDE_CONFIG_DIR（不写 ~/.claude/~/.bmem）
        self.assertIn("CLAUDE_CONFIG_DIR", seen["env"])
        self.assertTrue(seen["env"]["CLAUDE_CONFIG_DIR"].startswith(str(self.tmp.resolve())))

    def test_write_probe_failure_file_not_created(self):
        """写入失败：exit==0 但文件未创建。"""
        def runner(args, timeout, cwd, stdin):
            return fake_ev(exit=0, stdout="I couldn't write\n")

        rec = probe_executor_write(self.wspec("codex"), runner, 10, self.tmp)
        # exit=0 但文件未验证：诚实判 write_unverified（可写但非交互档未证实）
        self.assertEqual(rec["verdict"], "write_unverified")
        self.assertFalse(rec["write_probe"]["file_verified"])
        self.assertIn("file verification failed", rec["verdict_reason"])

    def test_write_probe_timeout(self):
        """写入超时：timed_out==True 记录原文。"""
        def runner(args, timeout, cwd, stdin):
            return fake_ev(timed_out=True, error="timeout after 10s")

        rec = probe_executor_write(self.wspec("codex"), runner, 10, self.tmp)
        self.assertNotEqual(rec["verdict"], "can_write_code")
        self.assertIn("timeout", rec["verdict_reason"])

    def test_write_probe_content_mismatch(self):
        """写入内容不匹配：文件存在但内容错误。"""
        def runner(args, timeout, cwd, stdin):
            target_dir = None
            for i, arg in enumerate(args):
                if arg == "-C" and i + 1 < len(args):
                    target_dir = args[i + 1]
                    break
            if target_dir:
                (Path(target_dir) / WRITE_PROBE_FILENAME).write_text(
                    "WRONG_CONTENT", encoding="utf-8")
            return fake_ev(exit=0, stdout="done\n")

        rec = probe_executor_write(self.wspec("codex"), runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "write_unverified")
        self.assertFalse(rec["write_probe"]["file_verified"])
        self.assertIn("file verification failed", rec["verdict_reason"])
        self.assertIn("mismatch", rec["write_probe"]["verify_detail"])

    # ------------------------------------------------------------------
    # T044-F 模型档位探针单测
    # ------------------------------------------------------------------

    def test_model_tier_available(self):
        """模型档可用：exit==0 且 stdout 含 PONG。"""
        rec = probe_model_tier("codex", "gpt-5.6-sol",
                               lambda *a: fake_ev(exit=0, stdout="PONG\n"),
                               10, self.tmp)
        self.assertEqual(rec["verdict"], "available")
        self.assertEqual(rec["model"], "gpt-5.6-sol")

    def test_model_tier_unavailable_not_supported(self):
        """模型档不可用：stderr 含 'not supported'。"""
        def runner(args, timeout, cwd, stdin):
            return fake_ev(exit=1, stderr="The 'gpt-5.5' model is not supported")
        rec = probe_model_tier("codex", "gpt-5.5", runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "model_unavailable")
        self.assertIn("not supported", rec["verdict_reason"])

    def test_model_tier_unavailable_not_exist(self):
        """模型档不可用：stderr 含 'not exist'。"""
        def runner(args, timeout, cwd, stdin):
            return fake_ev(exit=1, stderr="Model does not exist")
        rec = probe_model_tier("codex", "fable", runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "model_unavailable")
        self.assertIn("not exist", rec["verdict_reason"].lower())

    def test_model_tier_cli_unavailable(self):
        """CLI 不可用：binary not found。"""
        rec = probe_model_tier("definitely-not-a-binary", "gpt-5.5",
                               lambda *a: self.fail("must not run"), 10, self.tmp)
        self.assertEqual(rec["verdict"], "cli_unavailable")
        self.assertIn("not found", rec["verdict_reason"])

    def test_model_tier_timeout(self):
        """模型档位超时。"""
        def runner(args, timeout, cwd, stdin):
            return fake_ev(timed_out=True, error="timeout after 10s")
        rec = probe_model_tier("codex", "gpt-5.5", runner, 10, self.tmp)
        self.assertEqual(rec["verdict"], "model_unavailable")
        self.assertIn("timeout", rec["verdict_reason"])

    # ------------------------------------------------------------------
    # 结论字段名统一 ＋ 环境隔离（2026-11-24 修正）
    # ------------------------------------------------------------------

    def test_unified_verdict_field_across_families(self):
        """executors/write_probes/model_tiers 一律用 verdict＋verdict_reason。"""
        w = probe_executor_write(self.wspec("codex"),
                                 lambda *a, **k: fake_ev(exit=0, stdout="x"), 10, self.tmp)
        m = probe_model_tier("codex", "gpt-5.5",
                             lambda *a: fake_ev(exit=1, stderr="not supported"), 10, self.tmp)
        for rec in (w, m):
            self.assertIn("verdict", rec)
            self.assertIn("verdict_reason", rec)
            self.assertNotIn("write_verdict", rec)
            self.assertNotIn("model_verdict", rec)

    def test_run_cmd_env_merge_preserves_routing_and_is_not_recorded(self):
        """env 覆盖与 os.environ 合并（保留路由），密钥值绝不入档。"""
        ev = run_cmd([sys.executable, "-c",
                      "import os; print('ISO=' + os.environ.get('PROBE_TMP_ISOLATION', 'MISSING'))"],
                     10, self.tmp, None,
                     {"PROBE_TMP_ISOLATION": "tmp-config",
                      "PROBE_SECRET_TOKEN": "super-secret-value"})
        self.assertEqual(ev["exit"], 0)
        # 覆盖项确实合并进子进程环境（PATH 继承使 python 可启动）
        self.assertIn("ISO=tmp-config", ev["stdout"])
        # 证据不含 env 字段，且子进程未打印的密钥值不出现在任何证据字段
        self.assertNotIn("env", ev)
        self.assertNotIn("super-secret-value", json.dumps(ev))


if __name__ == "__main__":
    sys.exit(main())
