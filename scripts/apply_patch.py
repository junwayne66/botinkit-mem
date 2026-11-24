#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T044-C 装配补丁工具链（plan.md §三.3 的实现）。

读取 patches/<pkg>.json 装配补丁，按 file_path 分组、同文件按 (wave, order, pkg)
排序，逐条在内存中 dry-run（精确锚点上下文、唯一命中才允许），任何冲突即 FAIL
且不落盘；全部通过才写目标文件并追加证据 tests/v2/artifacts/patches_applied.json。

能力边界：只做基于锚点行/函数作用域的文本插入，不做语法解析、不删改既有行。
详见 specs/005-agent-mcp-interface/patch-format.md。

退出码：0=PASS/成功；1=FAIL（冲突/校验/回滚漂移）；2=用法或环境错误。
仅标准库；Python 3.9 兼容。git 只读（rev-parse HEAD 记录基线），不做任何 git 写操作。
"""

import argparse
import hashlib
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# plan.md §三.3 点名的热点文件（按 basename 匹配，从严：整文件适用 reconciler 规则）。
HOTSPOT_BASENAMES = {"server.rs", "main.rs", "index.html", "intake.js"}

TOP_KEYS = {"pkg", "wave", "order", "sample", "notes", "patches"}
ENTRY_KEYS = {
    "file_path", "function_scope", "notes",
    "route_additions", "middleware_stack", "enum_variants", "match_arms",
}
ROUTE_KEYS = {"method", "path", "handler", "capability_bits"}
MIDDLEWARE_KEYS = {"layer", "order"}
ENUM_KEYS = {"name", "payload", "doc"}
ARM_KEYS = {"arm", "anchor", "insert", "generate"}
ANCHOR_KEYS = {"lines", "position"}
GENERATE_KEYS = {"from", "template"}

# generate 模板可用占位符（string.Template 的 ${name} 语法，Rust 花括号无需转义）。
GENERATE_FIELDS = {
    "route_additions": {"method", "path", "path_segments", "handler", "capability_bits"},
    "enum_variants": {"name", "payload", "doc"},
    "middleware_stack": {"layer", "order"},
}

DECL_KINDS = ("route_additions", "middleware_stack", "enum_variants")


def conflict(code, pkg=None, file_path=None, arm=None, line=None, detail="", suggestion=""):
    return {
        "code": code, "pkg": pkg, "file_path": file_path, "arm": arm,
        "line": line, "detail": detail, "suggestion": suggestion,
    }


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".apply_patch-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def git_head(root):
    """只读记录当前 HEAD；无 git 或失败时返回 None（工具不做任何 git 写操作）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(prefix):
    return "%s-%s-%s" % (
        prefix, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8],
    )


# ---------------------------------------------------------------------------
# 补丁加载与结构校验
# ---------------------------------------------------------------------------

def _require_str(value, label, errors, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        errors.append("%s 必须是非空字符串" % label)
        return False
    return True


def _require_list(value, label, errors):
    if not isinstance(value, list):
        errors.append("%s 必须是数组" % label)
        return False
    return True


def _check_keys(obj, allowed, label, errors):
    if not isinstance(obj, dict):
        errors.append("%s 必须是对象" % label)
        return False
    unknown = sorted(set(obj) - allowed)
    if unknown:
        errors.append("%s 含未知字段 %s（防拼写错误；schema 见 patch-format.md）" % (label, unknown))
        return False
    return True


def _check_lines(value, label, errors):
    if not _require_list(value, label, errors) or not value:
        errors.append("%s 必须是非空字符串数组" % label)
        return False
    for item in value:
        if not isinstance(item, str) or "\n" in item or "\r" in item:
            errors.append("%s 的每一项必须是单行字符串（不含换行符）" % label)
            return False
    return True


def load_patch_files(patches_dir):
    """返回 (packages, errors)。errors 非空时 packages 可能不完整。"""
    packages, errors = [], []
    if not os.path.isdir(patches_dir):
        return None, ["补丁目录不存在: %s" % patches_dir]
    names = sorted(n for n in os.listdir(patches_dir) if n.endswith(".json"))
    for name in names:
        path = os.path.join(patches_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            errors.append("%s: JSON 读取失败: %s" % (name, exc))
            continue
        label = name
        if not _check_keys(data, TOP_KEYS, label, errors):
            continue
        pkg = data.get("pkg", os.path.splitext(name)[0])
        local = []
        if not _require_str(pkg, label + ".pkg", local):
            pkg = os.path.splitext(name)[0]
        wave = data.get("wave")
        if not isinstance(wave, int) or isinstance(wave, bool) or wave < 0:
            local.append(label + ".wave 必须是 >=0 的整数")
        order = data.get("order", 0)
        if not isinstance(order, int) or isinstance(order, bool):
            local.append(label + ".order 必须是整数")
        entries = data.get("patches")
        if not _require_list(entries, label + ".patches", local):
            entries = []
        norm_entries = []
        for idx, entry in enumerate(entries):
            elabel = "%s.patches[%d]" % (label, idx)
            if not _check_keys(entry, ENTRY_KEYS, elabel, local):
                continue
            norm = _validate_entry(entry, elabel, local)
            if norm is not None:
                norm_entries.append(norm)
        if local:
            errors.extend(local)
            continue
        packages.append({
            "pkg": pkg, "wave": wave, "order": order,
            "sample": bool(data.get("sample", False)),
            "source_file": name, "entries": norm_entries,
        })
    return packages, errors


def _validate_entry(entry, label, errors):
    file_path = entry.get("file_path")
    if not _require_str(file_path, label + ".file_path", errors):
        return None
    if file_path.startswith("/") or ".." in file_path.split("/") or "\\" in file_path:
        errors.append("%s.file_path 必须是仓库相对路径，不含 .. 与反斜杠: %r" % (label, file_path))
        return None
    scope = entry.get("function_scope")
    if scope is not None and (not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", scope)):
        errors.append("%s.function_scope 必须是函数名标识符" % label)
        return None
    norm = {
        "file_path": file_path.replace("\\", "/"),
        "function_scope": scope,
        "notes": entry.get("notes", ""),
        "declarations": {}, "ops": [],
    }
    for kind in DECL_KINDS:
        items = entry.get(kind, [])
        if not _require_list(items, "%s.%s" % (label, kind), errors):
            return None
        norm_items = []
        for i, item in enumerate(items):
            ilabel = "%s.%s[%d]" % (label, kind, i)
            if kind == "route_additions":
                if not _check_keys(item, ROUTE_KEYS, ilabel, errors):
                    return None
                method = item.get("method")
                path = item.get("path")
                handler = item.get("handler")
                bits = item.get("capability_bits", [])
                if not _require_str(method, ilabel + ".method", errors):
                    return None
                if not re.fullmatch(r"[A-Z]+", method):
                    errors.append("%s.method 必须是大写 HTTP 动词" % ilabel)
                    return None
                if not _require_str(path, ilabel + ".path", errors) or not path.startswith("/"):
                    errors.append("%s.path 必须以 / 开头" % ilabel)
                    return None
                if not _require_str(handler, ilabel + ".handler", errors):
                    return None
                if not _require_list(bits, ilabel + ".capability_bits", errors) or \
                        any(not isinstance(b, str) or not b for b in bits):
                    errors.append("%s.capability_bits 必须是字符串数组" % ilabel)
                    return None
                norm_items.append({"method": method, "path": path, "handler": handler,
                                   "capability_bits": list(bits)})
            elif kind == "middleware_stack":
                if not _check_keys(item, MIDDLEWARE_KEYS, ilabel, errors):
                    return None
                layer = item.get("layer")
                order = item.get("order")
                if not _require_str(layer, ilabel + ".layer", errors):
                    return None
                if not isinstance(order, int) or isinstance(order, bool):
                    errors.append("%s.order 必须是整数" % ilabel)
                    return None
                norm_items.append({"layer": layer, "order": order})
            else:  # enum_variants
                if not _check_keys(item, ENUM_KEYS, ilabel, errors):
                    return None
                name = item.get("name")
                if not _require_str(name, ilabel + ".name", errors):
                    return None
                payload = item.get("payload", "")
                doc = item.get("doc", "")
                if not isinstance(payload, str) or not isinstance(doc, str):
                    errors.append("%s.payload/doc 必须是字符串" % ilabel)
                    return None
                norm_items.append({"name": name, "payload": payload, "doc": doc})
        norm["declarations"][kind] = norm_items
    arms = entry.get("match_arms", [])
    if not _require_list(arms, label + ".match_arms", errors):
        return None
    for i, arm in enumerate(arms):
        alabel = "%s.match_arms[%d]" % (label, i)
        if not _check_keys(arm, ARM_KEYS, alabel, errors):
            return None
        key = arm.get("arm")
        if not _require_str(key, alabel + ".arm", errors):
            return None
        anchor = arm.get("anchor")
        if not _check_keys(anchor, ANCHOR_KEYS, alabel + ".anchor", errors):
            return None
        anchor_lines = anchor.get("lines")
        if not _check_lines(anchor_lines, alabel + ".anchor.lines", errors):
            return None
        position = anchor.get("position", "after")
        if position not in ("after", "before"):
            errors.append("%s.anchor.position 只能是 after/before" % alabel)
            return None
        has_insert = "insert" in arm
        has_generate = "generate" in arm
        if has_insert == has_generate:
            errors.append("%s 必须恰好提供 insert 与 generate 之一" % alabel)
            return None
        insert = None
        generate = None
        if has_insert:
            if not _check_lines(arm.get("insert"), alabel + ".insert", errors):
                return None
            insert = list(arm["insert"])
        else:
            gen = arm.get("generate")
            if not _check_keys(gen, GENERATE_KEYS, alabel + ".generate", errors):
                return None
            source = gen.get("from")
            template = gen.get("template")
            if source not in GENERATE_FIELDS:
                errors.append("%s.generate.from 只能是 %s" % (alabel, sorted(GENERATE_FIELDS)))
                return None
            if not _require_str(template, alabel + ".generate.template", errors):
                return None
            allowed = GENERATE_FIELDS[source]
            used = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", template))
            bare = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", template))
            unknown = (used | bare) - allowed
            if unknown:
                errors.append("%s.generate.template 含未知占位符 %s（可用: %s）"
                              % (alabel, sorted(unknown), sorted(allowed)))
                return None
            generate = {"from": source, "template": template}
        norm["ops"].append({
            "arm": key, "anchor_lines": list(anchor_lines), "position": position,
            "insert": insert, "generate": generate,
        })
    if not norm["ops"] and not any(norm["declarations"][k] for k in DECL_KINDS):
        errors.append("%s 是空条目：既无声明也无写入操作" % label)
        return None
    return norm


# ---------------------------------------------------------------------------
# 计划构建：分组、排序、静态冲突检查
# ---------------------------------------------------------------------------

def build_plan(packages, wave_filter):
    """返回 (file_ops, declarations, static_conflicts)。

    file_ops: {file_path: [op, ...]}，op 按 (wave, order, pkg, entry_idx, op_idx) 排序。
    declarations: {(wave, file_path, kind): [{"pkg": ..., **item}, ...]}。
    """
    selected = [p for p in packages if wave_filter is None or p["wave"] == wave_filter]
    file_ops = {}
    declarations = {}
    conflicts = []
    pkg_wave_order = {}
    for package in selected:
        pkg, wave, order = package["pkg"], package["wave"], package["order"]
        pkg_wave_order[(wave, pkg)] = (order, pkg)
        for entry_idx, entry in enumerate(package["entries"]):
            file_path = entry["file_path"]
            for kind in DECL_KINDS:
                for item in entry["declarations"][kind]:
                    declarations.setdefault((wave, file_path, kind), []).append(
                        dict(item, pkg=pkg))
            for op_idx, op in enumerate(entry["ops"]):
                file_ops.setdefault(file_path, []).append(dict(
                    op, pkg=pkg, wave=wave, order=order, file_path=file_path,
                    function_scope=entry["function_scope"], notes=entry["notes"],
                    entry_idx=entry_idx, op_idx=op_idx,
                ))
    for file_path in file_ops:
        file_ops[file_path].sort(
            key=lambda o: (o["wave"], o["order"], o["pkg"], o["entry_idx"], o["op_idx"]))
    for key in declarations:
        declarations[key].sort(key=lambda d: pkg_wave_order.get((key[0], d["pkg"]), (0, d["pkg"])))

    # 静态检查 1：同文件同 arm 键唯一（两条补丁改同一 match arm）。
    seen_arm = {}
    for file_path, ops in sorted(file_ops.items()):
        for op in ops:
            key = (file_path, op["arm"])
            if key in seen_arm:
                other = seen_arm[key]
                conflicts.append(conflict(
                    "ARM_CONFLICT", op["pkg"], file_path, op["arm"],
                    detail="arm 键 %r 已由包 %s 声明" % (op["arm"], other["pkg"]),
                    suggestion="两包不得写同一 match arm；合并进 reconciler 包或改用不同 arm 键。"))
            else:
                seen_arm[key] = op

    # 静态检查 2：声明重复（同波次同文件）。
    route_seen, enum_seen, layer_seen = {}, {}, {}
    for (wave, file_path, kind), items in sorted(declarations.items()):
        for item in items:
            if kind == "route_additions":
                key = (wave, file_path, item["method"], item["path"])
                if key in route_seen:
                    conflicts.append(conflict(
                        "ROUTE_DUPLICATE", item["pkg"], file_path,
                        detail="%s %s 已由包 %s 声明" % (item["method"], item["path"], route_seen[key]),
                        suggestion="同一路由只允许一个包声明；与声明者协调归属。"))
                else:
                    route_seen[key] = item["pkg"]
            elif kind == "enum_variants":
                key = (wave, file_path, item["name"])
                if key in enum_seen:
                    conflicts.append(conflict(
                        "ENUM_DUPLICATE", item["pkg"], file_path,
                        detail="变体 %s 已由包 %s 声明" % (item["name"], enum_seen[key]),
                        suggestion="同一枚举变体只允许一个包声明。"))
                else:
                    enum_seen[key] = item["pkg"]
            else:
                key = (wave, file_path, item["layer"])
                if key in layer_seen:
                    conflicts.append(conflict(
                        "LAYER_DUPLICATE", item["pkg"], file_path,
                        detail="middleware 层 %s 已由包 %s 声明" % (item["layer"], layer_seen[key]),
                        suggestion="同一 middleware 层只允许一个包声明。"))
                else:
                    layer_seen[key] = item["pkg"]

    # 静态检查 3：热点文件 reconciler 规则——同波次只允许最后一个包写。
    waves_files = {}
    for package in selected:
        for entry in package["entries"]:
            waves_files.setdefault((package["wave"], entry["file_path"]), set()).add(package["pkg"])
    for (wave, file_path), pkgs in sorted(waves_files.items()):
        if os.path.basename(file_path) not in HOTSPOT_BASENAMES:
            continue
        last = max(pkgs, key=lambda p: pkg_wave_order[(wave, p)])
        for op in file_ops.get(file_path, []):
            if op["wave"] == wave and op["pkg"] != last:
                conflicts.append(conflict(
                    "HOTSPOT_RECONCILER", op["pkg"], file_path, op["arm"],
                    detail="热点文件波次 %d 的 reconciler 是最后包 %s，%s 不得直接写" % (wave, last, op["pkg"]),
                    suggestion="把写入改为声明条目（route_additions/enum_variants/middleware_stack），"
                               "由 %s 的 reconciler 补丁统一生成；或经 ASM 调整 order 使 reconciler 排序最后。" % last))

    # 静态检查 4：声明必须被同波次同文件的 generate 消费；generate 不得消费空集。
    for (wave, file_path, kind), items in sorted(declarations.items()):
        consumers = [o for o in file_ops.get(file_path, [])
                     if o["wave"] == wave and o["generate"] and o["generate"]["from"] == kind]
        if not consumers:
            conflicts.append(conflict(
                "DANGLING_DECLARATION", items[0]["pkg"], file_path,
                detail="波次 %d 的 %s 声明（%d 条）没有 reconciler generate 消费" % (wave, kind, len(items)),
                suggestion="由该波次最后一个包交付 generate 补丁消费全部声明（plan §三.3）。"))
    for file_path, ops in sorted(file_ops.items()):
        for op in ops:
            if op["generate"]:
                key = (op["wave"], file_path, op["generate"]["from"])
                if not declarations.get(key):
                    conflicts.append(conflict(
                        "GENERATE_EMPTY", op["pkg"], file_path, op["arm"],
                        detail="generate.from=%s 在波次 %d 没有可消费的声明" % (op["generate"]["from"], op["wave"]),
                        suggestion="删除空 reconciler 或补齐声明；禁止静默空操作。"))
    return file_ops, declarations, conflicts


# ---------------------------------------------------------------------------
# 锚点/作用域解析与内存模拟
# ---------------------------------------------------------------------------

def find_scope(lines, name):
    """朴素花括号计数定位函数段（文本启发式，非语法解析）。

    返回 (span, error_code)：span=(start, end) 含闭括号行；error_code 为
    SCOPE_MISSING / SCOPE_AMBIGUOUS / None。字符串/注释中的花括号会干扰计数，
    失败形态是锚点缺失/歧义 → FAIL 交人工（见 patch-format.md 能力边界）。
    """
    pattern = re.compile(r"\bfn\s+" + re.escape(name) + r"\b")
    starts = [i for i, line in enumerate(lines) if pattern.search(line)]
    if not starts:
        return None, "SCOPE_MISSING"
    if len(starts) > 1:
        return None, "SCOPE_AMBIGUOUS"
    start = starts[0]
    depth = 0
    opened = False
    for j in range(start, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if "{" in lines[j]:
            opened = True
        if opened and depth <= 0:
            return (start, j), None
    return None, "SCOPE_MISSING"


def find_windows(lines, anchor_lines, lo, hi):
    """在 [lo, hi) 行范围内找连续精确等于 anchor_lines 的窗口，返回全部起始下标。"""
    k = len(anchor_lines)
    hits = []
    for i in range(lo, hi - k + 1):
        if lines[i:i + k] == anchor_lines:
            hits.append(i)
    return hits


def render_generate(op, declarations):
    items = declarations.get((op["wave"], op["file_path"], op["generate"]["from"]), [])
    out = []
    template = string.Template(op["generate"]["template"])
    for item in items:
        mapping = {}
        for field in GENERATE_FIELDS[op["generate"]["from"]]:
            if field == "path_segments":
                segments = [s for s in item.get("path", "").strip("/").split("/") if s]
                mapping[field] = ", ".join('"%s"' % s for s in segments)
            elif field == "capability_bits":
                mapping[field] = ",".join(item.get("capability_bits", []))
            else:
                mapping[field] = str(item.get(field, ""))
        out.extend(template.substitute(mapping).split("\n"))
    return out


def simulate_file(file_path, original_text, ops, declarations):
    """对单文件按序模拟全部写入操作。返回 (conflicts, final_text, records)。"""
    conflicts = []
    lines = original_text.split("\n")
    orig_lines = list(lines)
    intervals = []   # 已插入区间 [start, end)，当前坐标
    records = []     # 每个 op 的落点记录（start 随后续插入平移）
    for op in ops:
        pkg, arm = op["pkg"], op["arm"]
        span = None
        if op["function_scope"]:
            span, scope_err = find_scope(lines, op["function_scope"])
            if scope_err:
                conflicts.append(conflict(
                    scope_err, pkg, file_path, arm,
                    detail="函数作用域 %r 无法唯一定位" % op["function_scope"],
                    suggestion="核对函数名；同名多定义时去掉 function_scope 改用更长锚点。"))
                continue
        lo, hi = (0, len(lines)) if span is None else (span[0], span[1] + 1)
        anchor = op["anchor_lines"]
        hits = find_windows(lines, anchor, lo, hi)
        if len(hits) != 1:
            orig_span = None
            if op["function_scope"]:
                orig_span, _ = find_scope(orig_lines, op["function_scope"])
            olo, ohi = (0, len(orig_lines)) if orig_span is None else (orig_span[0], orig_span[1] + 1)
            orig_hits = find_windows(orig_lines, anchor, olo, ohi)
            if not hits:
                if len(orig_hits) == 1:
                    prior = ["%s/%s" % (r["op"]["pkg"], r["op"]["arm"]) for r in records]
                    conflicts.append(conflict(
                        "ANCHOR_MOVED", pkg, file_path, arm, line=orig_hits[0] + 1,
                        detail="锚点在原始文件唯一（行 %d），但被前序补丁移动/破坏；前序写入: %s"
                               % (orig_hits[0] + 1, prior or "无"),
                        suggestion="改用未被前序补丁触及的稳定锚点，或把两条写入合并进 reconciler。"))
                else:
                    conflicts.append(conflict(
                        "ANCHOR_MISSING", pkg, file_path, arm,
                        detail="锚点上下文在目标文件中不存在（原始命中 %d 处）" % len(orig_hits),
                        suggestion="只读打开目标文件核对锚点逐字符一致（含缩进）；源码已漂移时重写补丁。"))
            else:
                conflicts.append(conflict(
                    "ANCHOR_AMBIGUOUS", pkg, file_path, arm, line=hits[0] + 1,
                    detail="锚点命中 %d 处（行 %s；原始命中 %d 处）"
                           % (len(hits), [h + 1 for h in hits], len(orig_hits)),
                    suggestion="加长锚点上下文直至唯一命中；禁止任选一处。"))
            continue
        hit = hits[0]
        if any(hit < e and s < hit + len(anchor) for s, e in intervals):
            conflicts.append(conflict(
                "OVERLAP_CONFLICT", pkg, file_path, arm, line=hit + 1,
                detail="锚点落在本次运行前序补丁新插入的行上（目标行由前一条产生/移动）",
                suggestion="锚定原始文件的稳定上下文；不支持链式锚定，必要时合并进 reconciler。"))
            continue
        insert_lines = op["insert"] if op["insert"] is not None else render_generate(op, declarations)
        if not insert_lines:
            conflicts.append(conflict(
                "GENERATE_EMPTY", pkg, file_path, arm,
                detail="generate 渲染结果为空",
                suggestion="补齐声明或删除空 reconciler。"))
            continue
        p = hit + len(anchor) if op["position"] == "after" else hit
        n = len(insert_lines)
        if span is not None and not (span[0] <= p and p + n <= span[1] + 1):
            conflicts.append(conflict(
                "SCOPE_ESCAPE", pkg, file_path, arm, line=p + 1,
                detail="插入点越出函数作用域 %r（行 %d-%d）" % (op["function_scope"], span[0] + 1, span[1] + 1),
                suggestion="把锚点移进函数体内；作用域边界外的插入一律拒绝。"))
            continue
        for interval in intervals:
            if interval[0] >= p:
                interval[0] += n
                interval[1] += n
        for record in records:
            if record["start"] >= p:
                record["start"] += n
        lines[p:p] = insert_lines
        intervals.append([p, p + n])
        records.append({
            "op": op, "anchor_line": hit + 1, "start": p, "count": n,
            "insert_sha256": sha256_text("\n".join(insert_lines)),
        })
    return conflicts, "\n".join(lines), records


def read_target(root, file_path):
    """返回 (text, conflict_or_none)。只接受 UTF-8 LF 文件。"""
    path = os.path.join(root, file_path)
    if not os.path.isfile(path):
        return None, conflict("FILE_MISSING", None, file_path,
                              detail="目标文件不存在: %s" % path,
                              suggestion="核对 file_path；补丁只插入既有文件，不创建新文件。")
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        return None, conflict("FILE_UNREADABLE", None, file_path,
                              detail=str(exc), suggestion="确认文件为 UTF-8 文本且可读。")
    if "\r" in text:
        return None, conflict("CRLF_UNSUPPORTED", None, file_path,
                              detail="目标文件含 CR（CRLF/旧 Mac 换行）",
                              suggestion="先统一为 LF 再装配；锚点按逐字符精确匹配。")
    return text, None


# ---------------------------------------------------------------------------
# 证据与回滚
# ---------------------------------------------------------------------------

def load_evidence(path):
    if not os.path.exists(path):
        return {"schema_version": 1, "runs": []}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError("证据文件格式非法: %s" % path)
    return data


def save_evidence(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def execute_apply(root, patches_dir, evidence_path, wave, dry_run):
    packages, errors = load_patch_files(patches_dir)
    if packages is None:
        return {"status": "ERROR", "mode": "dry-run" if dry_run else "apply",
                "errors": errors}, EXIT_USAGE
    if errors:
        return {"status": "FAIL", "mode": "dry-run" if dry_run else "apply",
                "conflicts": [conflict("PATCH_JSON_INVALID", detail=e,
                                       suggestion="按 patch-format.md 修正补丁结构。") for e in errors]}, EXIT_FAIL
    file_ops, declarations, conflicts = build_plan(packages, wave)
    report = {
        "status": "FAIL" if conflicts else "PASS",
        "mode": "dry-run" if dry_run else "apply",
        "wave": wave, "root": os.path.abspath(root),
        "patches_dir": os.path.abspath(patches_dir),
        "conflicts": conflicts, "files": [],
    }
    if conflicts:
        return report, EXIT_FAIL

    simulated = {}
    for file_path, ops in sorted(file_ops.items()):
        text, err = read_target(root, file_path)
        if err:
            conflicts.append(err)
            continue
        file_conflicts, final_text, records = simulate_file(file_path, text, ops, declarations)
        conflicts.extend(file_conflicts)
        if file_conflicts:
            continue
        simulated[file_path] = {
            "before": text, "after": final_text, "records": records,
            "packages": sorted({o["pkg"] for o in ops}),
        }
    if conflicts:
        report["status"] = "FAIL"
        report["conflicts"] = conflicts
        return report, EXIT_FAIL

    for file_path, result in sorted(simulated.items()):
        before, after = result["before"], result["after"]
        ops_report = []
        for record in result["records"]:
            op = record["op"]
            ops_report.append({
                "pkg": op["pkg"], "wave": op["wave"], "arm": op["arm"],
                "function_scope": op["function_scope"],
                "anchor": {"lines": op["anchor_lines"], "position": op["position"],
                           "line": record["anchor_line"]},
                "line_range": [record["start"] + 1, record["start"] + record["count"]],
                "insert_sha256": record["insert_sha256"],
            })
        report["files"].append({
            "file_path": file_path, "packages": result["packages"],
            "before_sha256": sha256_text(before), "after_sha256": sha256_text(after),
            "ops": ops_report,
        })
    report["declarations"] = {
        "%s|%s|%s" % key: items for key, items in sorted(declarations.items())
    }
    if dry_run:
        return report, EXIT_OK

    # 全部通过才落盘；写失败时尽力恢复已写文件。
    written = []
    head = git_head(root)
    try:
        for file_path, result in sorted(simulated.items()):
            atomic_write(os.path.join(root, file_path), result["after"])
            written.append(file_path)
    except OSError as exc:
        for file_path in written:
            try:
                atomic_write(os.path.join(root, file_path), simulated[file_path]["before"])
            except OSError:
                pass
        report["status"] = "FAIL"
        report["conflicts"] = [conflict("WRITE_FAILED", None, None, detail=str(exc),
                                        suggestion="检查磁盘/权限后重跑；已写文件已尽力恢复。")]
        return report, EXIT_FAIL

    run_id = new_run_id("apply")
    evidence_files = []
    for entry in report["files"]:
        record = dict(entry)
        record["before_content"] = simulated[entry["file_path"]]["before"]
        evidence_files.append(record)
    run = {
        "run_id": run_id, "mode": "apply", "wave": wave,
        "applied_at": now_iso(),
        "commit_before": head, "commit_after": head,
        "rolled_back": False,
        "files": evidence_files,
        "declarations": report["declarations"],
    }
    try:
        evidence = load_evidence(evidence_path)
        evidence["runs"].append(run)
        save_evidence(evidence_path, evidence)
    except (OSError, ValueError) as exc:
        report["status"] = "FAIL"
        report["conflicts"] = [conflict("EVIDENCE_WRITE_FAILED", None, None, detail=str(exc),
                                        suggestion="目标文件已写入；修复证据路径后用 --rollback 或手工恢复。")]
        report["run_id"] = run_id
        return report, EXIT_FAIL
    report["run_id"] = run_id
    report["evidence_path"] = os.path.abspath(evidence_path)
    return report, EXIT_OK


def execute_rollback(root, evidence_path, run_id, dry_run):
    mode = "rollback"
    try:
        evidence = load_evidence(evidence_path)
    except (OSError, ValueError) as exc:
        return {"status": "ERROR", "mode": mode, "errors": [str(exc)]}, EXIT_USAGE
    runs = evidence["runs"]
    pending = [i for i, r in enumerate(runs) if not r.get("rolled_back")]
    if not pending:
        return {"status": "FAIL", "mode": mode,
                "conflicts": [conflict("ROLLBACK_NOTHING", detail="没有可回滚的 apply 运行",
                                       suggestion="核对证据文件与 run_id。")]}, EXIT_FAIL
    if run_id is None:
        index = pending[-1]
    else:
        matches = [i for i, r in enumerate(runs) if r.get("run_id") == run_id]
        if not matches:
            return {"status": "FAIL", "mode": mode,
                    "conflicts": [conflict("ROLLBACK_UNKNOWN_RUN", detail="run_id 不存在: %s" % run_id,
                                           suggestion="用 --list 或读取证据文件核对 run_id。")]}, EXIT_FAIL
        index = matches[0]
        if runs[index].get("rolled_back"):
            return {"status": "FAIL", "mode": mode,
                    "conflicts": [conflict("ROLLBACK_ALREADY", detail="该运行已回滚",
                                           suggestion="无需重复回滚。")]}, EXIT_FAIL
    newer = [runs[i]["run_id"] for i in pending if i > index]
    if newer:
        return {"status": "FAIL", "mode": mode,
                "conflicts": [conflict("ROLLBACK_ORDER", detail="必须先回滚更晚的运行: %s" % newer,
                                       suggestion="按 LIFO 顺序逐运行回滚。")]}, EXIT_FAIL
    run = runs[index]
    conflicts = []
    for entry in reversed(run["files"]):
        file_path = entry["file_path"]
        path = os.path.join(root, file_path)
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                current = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            conflicts.append(conflict("ROLLBACK_DRIFT", None, file_path, detail=str(exc),
                                      suggestion="文件不可读；从证据 before_content 手工恢复。"))
            continue
        if sha256_text(current) != entry["after_sha256"]:
            conflicts.append(conflict(
                "ROLLBACK_DRIFT", None, file_path,
                detail="当前内容 sha256 与 apply 后不一致（文件在 apply 之后被改动）",
                suggestion="人工接管：对照证据 before_content/after_sha256 手工合并，禁止盲目覆盖。"))
    report = {"status": "FAIL" if conflicts else "PASS", "mode": mode,
              "run_id": run["run_id"], "dry_run": dry_run,
              "files": [e["file_path"] for e in run["files"]], "conflicts": conflicts}
    if conflicts:
        return report, EXIT_FAIL
    if dry_run:
        return report, EXIT_OK
    for entry in reversed(run["files"]):
        atomic_write(os.path.join(root, entry["file_path"]), entry["before_content"])
    run["rolled_back"] = True
    run["rolled_back_at"] = now_iso()
    save_evidence(evidence_path, evidence)
    report["evidence_path"] = os.path.abspath(evidence_path)
    return report, EXIT_OK


def execute_list(patches_dir, wave):
    packages, errors = load_patch_files(patches_dir)
    if packages is None:
        return {"status": "ERROR", "mode": "list", "errors": errors}, EXIT_USAGE
    if errors:
        return {"status": "FAIL", "mode": "list",
                "conflicts": [conflict("PATCH_JSON_INVALID", detail=e) for e in errors]}, EXIT_FAIL
    file_ops, declarations, conflicts = build_plan(packages, wave)
    plan = []
    for package in sorted(packages, key=lambda p: (p["wave"], p["order"], p["pkg"])):
        if wave is not None and package["wave"] != wave:
            continue
        plan.append({
            "pkg": package["pkg"], "wave": package["wave"], "order": package["order"],
            "sample": package["sample"], "source_file": package["source_file"],
            "entries": [{
                "file_path": e["file_path"],
                "hotspot": os.path.basename(e["file_path"]) in HOTSPOT_BASENAMES,
                "function_scope": e["function_scope"],
                "writes": [o["arm"] for o in e["ops"]],
                "declarations": {k: len(e["declarations"][k]) for k in DECL_KINDS
                                 if e["declarations"][k]},
            } for e in package["entries"]],
        })
    return {"status": "FAIL" if conflicts else "PASS", "mode": "list", "wave": wave,
            "plan": plan, "conflicts": conflicts,
            "files": sorted(file_ops),
            "declaration_sets": ["%s|%s|%s" % k for k in sorted(declarations)]}, \
        EXIT_FAIL if conflicts else EXIT_OK


# ---------------------------------------------------------------------------
# 内置自测（纯临时夹具，不碰仓库源码，不跑 cargo）
# ---------------------------------------------------------------------------

SELFTEST_SERVER = (
    "fn dispatch() {\n"
    "    let outcome = match (method, parts) {\n"
    '        ("GET", ["v2", "ping"]) => {\n'
    "            ok()\n"
    "        }\n"
    "        _ => err(),\n"
    "    };\n"
    "}\n"
    "\n"
    "fn helper() {\n"
    "            ok()\n"
    "}\n"
)


def execute_selftest():
    checks = []

    def record(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="apply-patch-selftest-") as root:
        src = os.path.join(root, "src", "demo")
        os.makedirs(src)
        target = os.path.join(src, "server.rs")
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(SELFTEST_SERVER)
        patches_dir = os.path.join(root, "patches")
        os.makedirs(patches_dir)
        evidence_path = os.path.join(root, "artifacts", "patches_applied.json")
        package = {
            "pkg": "T900", "wave": 1, "order": 0, "sample": True,
            "patches": [{
                "file_path": "src/demo/server.rs", "function_scope": "dispatch",
                "route_additions": [{"method": "GET", "path": "/v2/agent/ping",
                                     "handler": "agent_ping", "capability_bits": ["search"]}],
                "match_arms": [{
                    "arm": "reconcile_routes",
                    "anchor": {"lines": ['        ("GET", ["v2", "ping"]) => {',
                                         "            ok()", "        }"],
                               "position": "after"},
                    "generate": {"from": "route_additions",
                                 "template": '        ("${method}", [${path_segments}]) => {\n'
                                             "            ${handler}()\n        }"},
                }],
            }],
        }
        with open(os.path.join(patches_dir, "T900.json"), "w", encoding="utf-8") as handle:
            json.dump(package, handle, ensure_ascii=False)
        report, code = execute_apply(root, patches_dir, evidence_path, 1, False)
        with open(target, "r", encoding="utf-8", newline="") as handle:
            applied = handle.read()
        record("apply_unique_anchor", code == EXIT_OK and '("GET", ["v2", "agent", "ping"]) =>' in applied,
               report.get("conflicts", []))
        record("evidence_written", os.path.exists(evidence_path))
        report, code = execute_rollback(root, evidence_path, None, False)
        with open(target, "r", encoding="utf-8", newline="") as handle:
            restored = handle.read()
        record("rollback_roundtrip", code == EXIT_OK and restored == SELFTEST_SERVER)
        os.unlink(os.path.join(patches_dir, "T900.json"))
        bad = json.loads(json.dumps(package))
        bad["pkg"] = "T901"
        bad["patches"][0]["match_arms"][0]["arm"] = "other_arm"
        bad["patches"][0]["match_arms"][0]["anchor"]["lines"] = ["            ok()"]
        bad["patches"][0].pop("function_scope", None)
        bad["patches"][0]["route_additions"] = []
        bad["patches"][0]["match_arms"][0].pop("generate")
        bad["patches"][0]["match_arms"][0]["insert"] = ["            // dup"]
        with open(os.path.join(patches_dir, "T901.json"), "w", encoding="utf-8") as handle:
            json.dump(bad, handle, ensure_ascii=False)
        report, code = execute_apply(root, patches_dir, evidence_path, 1, True)
        codes = [c["code"] for c in report.get("conflicts", [])]
        record("ambiguous_anchor_fails", code == EXIT_FAIL and "ANCHOR_AMBIGUOUS" in codes, codes)
        with open(target, "r", encoding="utf-8", newline="") as handle:
            record("dry_run_no_write", handle.read() == SELFTEST_SERVER)
    ok = all(c["ok"] for c in checks)
    return {"status": "PASS" if ok else "FAIL", "mode": "selftest", "checks": checks}, \
        EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="apply_patch.py",
        description="T044-C 装配补丁工具：patches/*.json → 分组排序 → 锚点 dry-run → "
                    "冲突即 FAIL → 通过才写文件并留证据。只做文本插入，不做语法解析。",
        epilog="退出码: 0=PASS/成功, 1=FAIL(冲突/校验/回滚漂移), 2=用法/环境错误。"
               "格式与能力边界见 specs/005-agent-mcp-interface/patch-format.md。",
    )
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_root = os.path.dirname(script_dir)
    parser.add_argument("--root", default=default_root,
                        help="仓库根（默认: 脚本所在 scripts/ 的上级）")
    parser.add_argument("--patches-dir", default=None,
                        help="补丁目录（默认: <root>/patches；相对路径按 root 解析）")
    parser.add_argument("--evidence", default=None,
                        help="证据文件（默认: <root>/tests/v2/artifacts/patches_applied.json）")
    parser.add_argument("--wave", type=int, default=None, help="只处理该波次的包")
    parser.add_argument("--dry-run", action="store_true",
                        help="只模拟与校验，不写任何文件（apply/rollback 均可搭配）")
    parser.add_argument("--list", action="store_true", help="只输出装配计划，不读目标文件")
    parser.add_argument("--rollback", action="store_true",
                        help="凭证据 before_content 恢复（默认回滚最近一次未回滚运行）")
    parser.add_argument("--run-id", default=None, help="rollback 指定 run_id")
    parser.add_argument("--selftest", action="store_true",
                        help="内置临时夹具自测（不碰仓库源码，不需要 cargo）")
    args = parser.parse_args(argv)

    modes = [args.list, args.rollback, args.selftest]
    if sum(1 for m in modes if m) > 1:
        parser.error("--list/--rollback/--selftest 互斥")
    if args.dry_run and (args.list or args.selftest):
        parser.error("--dry-run 只能搭配默认 apply 模式或 --rollback")
    if args.run_id and not args.rollback:
        parser.error("--run-id 只能搭配 --rollback")
    if args.wave is not None and args.wave < 0:
        parser.error("--wave 必须是 >=0 的整数")

    root = os.path.abspath(args.root)

    def resolve(value, default_rel):
        if value is None:
            value = default_rel
        return value if os.path.isabs(value) else os.path.join(root, value)

    patches_dir = resolve(args.patches_dir, "patches")
    evidence_path = resolve(args.evidence, os.path.join("tests", "v2", "artifacts", "patches_applied.json"))

    if args.selftest:
        report, code = execute_selftest()
    elif args.list:
        report, code = execute_list(patches_dir, args.wave)
    elif args.rollback:
        report, code = execute_rollback(root, evidence_path, args.run_id, args.dry_run)
    else:
        report, code = execute_apply(root, patches_dir, evidence_path, args.wave, args.dry_run)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("status") == "PASS":
        print("[apply_patch] PASS mode=%s%s" % (
            report.get("mode"), " wave=%s" % args.wave if args.wave is not None else ""),
            file=sys.stderr)
    else:
        for item in report.get("conflicts", []) or [{"code": "ERROR", "detail": e}
                                                    for e in report.get("errors", [])]:
            print("[apply_patch] %s pkg=%s file=%s line=%s: %s | 建议: %s" % (
                item.get("code"), item.get("pkg"), item.get("file_path"),
                item.get("line"), item.get("detail"), item.get("suggestion", "")),
                file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
