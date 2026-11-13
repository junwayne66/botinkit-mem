"""Harness adapters: connect, hook callbacks, doctor and history import.

The module deliberately depends only on public bmem DTOs and the Store public
contract.  Harness formats were probed against the locally installed CLIs:

* Claude Code 2.1.308: JSON settings hooks under ``$CLAUDE_CONFIG_DIR``
  (default ``~/.claude``) and user-scoped MCP in ``~/.claude.json``, which
  becomes ``$CLAUDE_CONFIG_DIR/.claude.json`` when the env override is set.
  Both paths were verified with ``claude mcp add --scope user`` and
  ``claude --version`` against isolated temporary HOME roots only.
* Codex CLI 0.167.0: ``$CODEX_HOME/config.toml`` hooks (verified through the
  app-server ``hooks/list`` protocol) and ``[mcp_servers.<name>]``.
* DeepSeek Harness 0.2.2-rc.1: Cordis patch rows, a native Cordis plugin with
  absolute file-URL inserts, and the ``dsh-mcp-client`` Streamable HTTP bridge.

No adapter guesses a capability: unsupported or trust-gated states are reported
as ``partial`` with a concrete reason.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, TYPE_CHECKING

import httpx
import tomlkit
import zstandard as zstd

from .models import DomainError, redact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .store import Store

PARSER_VERSION = "bmem-adapters-1"
MAX_BODY = 100_000
DEFAULT_PORT = 8790

_HARNESSES = ("claude-code", "codex", "dsh")
_HARNESS_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "codex": "codex",
    "codex-cli": "codex",
    "dsh": "dsh",
    "deepseek-harness": "dsh",
    "deepseek": "dsh",
    "all": "all",
}

_MARKER_RE = re.compile(r"\[bmem-injection:([A-Za-z0-9._:\-]+)\]")
_REMEMBER_RE = re.compile(
    r"(?:"
    r"^\s*(?:请|务必)?记住"
    r"|^\s*remember(?:\s+that)?\b"
    r"|^\s*(?:please\s+)?remember\s*:"
    r"|请记住"
    r")",
    re.IGNORECASE,
)

_JSONL_SUFFIXES = (".jsonl", ".jsonl.zstd")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _normalize_harness(value: str) -> str:
    normalized = str(value or "").strip().lower()
    harness = _HARNESS_ALIASES.get(normalized)
    if harness is None:
        raise DomainError(
            422,
            f"unsupported harness {value!r}; expected claude-code, codex, dsh or all",
        )
    return harness


def _config_root(config_root: Path | None) -> Path:
    if config_root is not None:
        return Path(config_root).expanduser()
    env_root = os.environ.get("BMEM_CONFIG_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    return Path.home()


def _uses_isolated_root(root: Path) -> bool:
    if os.environ.get("BMEM_CONFIG_ROOT"):
        return True
    try:
        return str(root.expanduser()) != str(Path.home())
    except RuntimeError:  # pragma: no cover - no home available
        return True


def _harness_home(root: Path, harness: str) -> Path:
    """Return the effective harness-owned configuration directory.

    An explicitly passed or BMEM_CONFIG_ROOT root is authoritative, so tests can
    never escape into the real user configuration.  With the default home and no
    isolation root, the harness-native env override is honored.
    """

    if not _uses_isolated_root(root):
        env_name = {
            "claude-code": "CLAUDE_CONFIG_DIR",
            "codex": "CODEX_HOME",
            "dsh": "DSH_HOME",
        }[harness]
        env_value = os.environ.get(env_name)
        if env_value:
            return Path(env_value).expanduser()
    return root / {
        "claude-code": ".claude",
        "codex": ".codex",
        "dsh": ".dsh",
    }[harness]


def _claude_mcp_path(root: Path, claude_home: Path) -> Path:
    """Return where Claude Code keeps user-scoped ``mcpServers``.

    Verified against the installed CLI (2.1.308) with temporary roots only:
    ``claude mcp add --scope user`` writes ``$HOME/.claude.json`` by default
    and ``$CLAUDE_CONFIG_DIR/.claude.json`` when the override is set.  It is
    *not* ``$HOME/.claude/.claude.json``.  An isolated BMEM/config root stays
    authoritative (``<root>/.claude.json``) so tests cannot escape to the real
    user configuration.
    """

    if not _uses_isolated_root(root) and os.environ.get("CLAUDE_CONFIG_DIR"):
        return claude_home / ".claude.json"
    return root / ".claude.json"


def _api_base(url: str | None) -> str:
    raw = (url or os.environ.get("BMEM_URL") or
           f"http://127.0.0.1:{os.environ.get('BMEM_PORT', DEFAULT_PORT)}")
    raw = str(raw).strip()
    if "://" not in raw:
        raw = "http://" + raw
    raw = raw.rstrip("/")
    for suffix in ("/mcp", "/api"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
    return raw


def _mcp_url(api_base: str) -> str:
    return api_base.rstrip("/") + "/mcp"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _iso_time(value: Any, fallback: datetime | None = None) -> str:
    """Normalize common transcript timestamps without ever raising."""

    fallback = fallback or datetime.now(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric > 10_000_000_000:  # milliseconds
            numeric /= 1000.0
        try:
            dt = datetime.fromtimestamp(numeric, UTC)
            return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
        except ValueError:
            pass
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=UTC)
    return fallback.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _file_mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return datetime.now(UTC)


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, limit: int | None = None) -> str | None:
    """Streaming file digest; returns ``None`` above ``limit`` instead of reading it."""

    try:
        if limit is not None and path.stat().st_size > limit:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _path_key(path: Path) -> str:
    try:
        absolute = path.resolve()
    except OSError:
        absolute = path.absolute()
    return _sha256(str(absolute))[:24]


def _safe_error(exc: BaseException) -> str:
    """A bounded, redacted error string that never carries a raw body."""

    if isinstance(exc, DomainError):
        return redact(exc.message)[:400]
    return f"{type(exc).__name__}: {redact(str(exc))[:240]}"


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.bmem-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    elif path.exists():
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
    os.replace(tmp, path)


def _backup(path: Path) -> str | None:
    """Copy an existing file to a timestamped sibling before modification."""

    if not path.exists() or not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for ordinal in range(1000):
        suffix = stamp if ordinal == 0 else f"{stamp}-{ordinal}"
        destination = path.with_name(f"{path.name}.bmem-backup-{suffix}")
        if not destination.exists():
            shutil.copy2(path, destination)
            return str(destination)
    raise DomainError(409, f"cannot allocate a backup name next to {path}")


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DomainError(409, f"cannot read {path.name}: {_safe_error(exc)}") from exc
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainError(409, f"{path.name} is not valid JSON; refusing to overwrite it") from exc
    if not isinstance(value, dict):
        raise DomainError(409, f"{path.name} must contain a JSON object")
    return value


def _read_toml_document(path: Path) -> Any:
    if not path.exists():
        return tomlkit.document()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainError(409, f"cannot read {path.name}: {_safe_error(exc)}") from exc
    if not raw.strip():
        return tomlkit.document()
    try:
        return tomlkit.parse(raw)
    except Exception as exc:  # tomlkit has several parse exception types
        raise DomainError(409, f"{path.name} is not valid TOML; refusing to overwrite it") from exc


def _probe_version(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    if not output:
        return None
    return redact(output.splitlines()[0])[:120]


def _hook_command(harness: str, event: str) -> str:
    """Build a shell-safe command that runs the installed bmem entry point."""

    executable = shutil.which("bmem")
    if executable:
        parts = [executable, "hook", harness, event]
    else:
        parts = [sys.executable, "-m", "bmem.cli", "hook", harness, event]
    return " ".join(shlex.quote(part) for part in parts)


def _managed_command(command: Any, harness: str, event: str | None = None) -> bool:
    text = str(command or "")
    if "bmem" not in text or "hook" not in text or harness not in text:
        return False
    return event is None or event in text


# ---------------------------------------------------------------------------
# Request / event helpers shared by hooks and importers
# ---------------------------------------------------------------------------


def _marker_id(text: str) -> str | None:
    match = _MARKER_RE.search(text)
    return match.group(1) if match else None


def _explicit_remember(text: str) -> bool:
    return bool(_REMEMBER_RE.search(text or ""))


def _make_candidate(
    *,
    body: str,
    source_type: str,
    native_event_id: str,
    operation_id: str | None = None,
    occurred_at: str | None = None,
    session_id: str = "manual",
    project: str = "",
    harness: str = "manual",
    metadata: dict[str, Any] | None = None,
    mode: str = "event",
    kind: str = "fact",
    fact_key: str | None = None,
    conditions: dict[str, Any] | None = None,
    sensitivity: str = "normal",
) -> dict[str, Any] | None:
    """Build a validated-by-store event/declaration candidate or skip it."""

    if not isinstance(body, str):
        return None
    body = body.strip()
    if not body or len(body) > MAX_BODY:
        return None
    evidence_hash = _sha256(body)
    marker_id = _marker_id(body)
    metadata = dict(metadata or {})
    metadata.setdefault("parser_version", PARSER_VERSION)
    metadata.setdefault("evidence_hash", evidence_hash)
    if marker_id is not None:
        source_type = "injection_reference"
        metadata["injection_id"] = marker_id
        metadata["injection_reference"] = True
        # A reference is not a declaration; keep it as an event.
        mode = "event"
    if mode == "event" and source_type == "user_dialogue" and _explicit_remember(body):
        mode = "declare"
        source_type = "user_declaration"
        metadata.setdefault("explicit_remember", True)
    candidate: dict[str, Any] = {
        "body": body,
        "harness": harness,
        "device_id": "local",
        "native_event_id": native_event_id or str(uuid.uuid4()),
        "operation_id": operation_id or native_event_id or str(uuid.uuid4()),
        "source_type": source_type,
        "occurred_at": occurred_at or _now_iso(),
        "session_id": session_id or "manual",
        "project": project or "",
        # Native private labels (e.g. Markdown ``sensitivity: private``) are
        # carried through verbatim; everything else stays normal.
        "sensitivity": "private" if sensitivity == "private" else "normal",
        "metadata": metadata,
        "mode": mode,
    }
    if mode == "declare":
        candidate.update({
            "source_type": "user_declaration",
            "kind": kind,
            "conditions": dict(conditions or {}),
            "derived_from": [],
        })
        if fact_key:
            candidate["fact_key"] = fact_key
    return candidate


def _candidate_for_text_block(
    *,
    text: str,
    role: str,
    kind: str,
    source_type: str,
    native_event_id: str,
    occurred_at: Any,
    session_id: str,
    project: str,
    harness: str,
    metadata: dict[str, Any],
    mode: str = "event",
    fact_key: str | None = None,
) -> dict[str, Any] | None:
    return _make_candidate(
        body=text,
        source_type=source_type,
        native_event_id=native_event_id,
        occurred_at=_iso_time(occurred_at),
        session_id=session_id,
        project=project,
        harness=harness,
        metadata={**metadata, "role": role, "block_kind": kind},
        mode=mode,
        fact_key=fact_key,
    )


def _content_parts(content: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(text, kind)`` for durable message content blocks."""

    if isinstance(content, str):
        if content.strip():
            yield content, "text"
        return
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                yield text, "text"
        elif block_type in {"tool_result", "tool-output", "tool_output"}:
            nested = block.get("content", block.get("output"))
            if isinstance(nested, str) and nested.strip():
                yield nested, "tool_result"
            else:
                for text, _kind in _content_parts(nested):
                    yield text, "tool_result"
        elif block_type in {"image", "file", "audio", "thinking", "reasoning"}:
            continue
        elif "text" in block and isinstance(block.get("text"), str):
            # Codex app-server UserInput text blocks have ``type: 'text'``; a
            # harness may add another text carrier, which remains conservative.
            text = str(block["text"])
            if text.strip():
                yield text, block_type or "text"


def _tool_output_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), str):
            return value["content"]
        if isinstance(value.get("output"), str):
            return value["output"]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _tool_output_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    return None


# ---------------------------------------------------------------------------
# Claude Code connect / doctor
# ---------------------------------------------------------------------------


def _connect_claude(root: Path, api_base: str, changes: list[str], backups: list[str]) -> dict[str, Any]:
    claude_home = _harness_home(root, "claude-code")
    settings_path = claude_home / "settings.json"
    mcp_path = _claude_mcp_path(root, claude_home)
    version = _probe_version("claude")

    settings = _read_json_object(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise DomainError(409, f"{settings_path} has a non-object hooks value")
    managed_events = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
    for event_name in managed_events:
        existing = hooks.get(event_name)
        if existing is None:
            existing = []
        if not isinstance(existing, list):
            raise DomainError(409, f"{settings_path} hooks.{event_name} must be an array")
        kept = [
            group for group in existing
            if not _claude_group_is_managed(group)
        ]
        hook_event = {
            "SessionStart": "session-start",
            "UserPromptSubmit": "prompt",
            "Stop": "stop",
            "SessionEnd": "session-end",
        }[event_name]
        kept.append({"hooks": [{
            "type": "command",
            "command": _hook_command("claude-code", hook_event),
        }]})
        hooks[event_name] = kept

    candidate_settings = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    existing_settings = _read_json_object(settings_path) if settings_path.exists() else {}
    if existing_settings != settings:
        backup = _backup(settings_path)
        if backup:
            backups.append(backup)
        _atomic_write(settings_path, candidate_settings, mode=0o600)
        changes.append(str(settings_path))

    mcp_config = _read_json_object(mcp_path)
    servers = mcp_config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise DomainError(409, f"{mcp_path} has a non-object mcpServers value")
    desired_mcp = {"type": "http", "url": _mcp_url(api_base)}
    if servers.get("bmem") != desired_mcp:
        servers["bmem"] = desired_mcp
        candidate_mcp = json.dumps(mcp_config, indent=2, ensure_ascii=False) + "\n"
        existing_mcp = _read_json_object(mcp_path) if mcp_path.exists() else {}
        if existing_mcp != mcp_config:
            backup = _backup(mcp_path)
            if backup:
                backups.append(backup)
            _atomic_write(mcp_path, candidate_mcp, mode=0o600)
            changes.append(str(mcp_path))

    return {
        "harness": "claude-code",
        "status": "connected" if version else "partial",
        "version": version,
        "config_root": str(root),
        "config_home": str(claude_home),
        "paths": {"settings": str(settings_path), "mcp": str(mcp_path)},
        "changed": changes,
        "backups": backups,
        "capabilities": {
            "native_protocol": "claude-json-hooks",
            "hooks": True,
            "mcp": True,
            "hook_events": list(managed_events),
            "format_source": (
                "installed claude 2.1.308; settings hooks under "
                "$CLAUDE_CONFIG_DIR (default ~/.claude) and user-scoped "
                "$HOME/.claude.json (or $CLAUDE_CONFIG_DIR/.claude.json), "
                "probed with claude mcp add on temporary roots"
            ),
        },
        "notes": [] if version else ["claude CLI was not found; configuration was still written"],
    }


def _claude_group_is_managed(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []) or []:
        if isinstance(hook, dict) and _managed_command(
            hook.get("command"), "claude-code"
        ):
            return True
    return False


def _doctor_claude(root: Path, url: str | None = None) -> dict[str, Any]:
    claude_home = _harness_home(root, "claude-code")
    api_base = _api_base(url)
    version = _probe_version("claude")
    settings_path = claude_home / "settings.json"
    mcp_path = _claude_mcp_path(root, claude_home)
    managed_hooks = False
    managed_mcp = False
    configured_mcp_url: str | None = None
    errors: list[str] = []
    try:
        settings = _read_json_object(settings_path)
        hooks = settings.get("hooks")
        managed_hooks = isinstance(hooks, dict) and any(
            _claude_group_is_managed(group)
            for groups in hooks.values() if isinstance(groups, list)
            for group in groups
        )
    except DomainError as exc:
        errors.append(exc.message)
    try:
        mcp_config = _read_json_object(mcp_path)
        servers = mcp_config.get("mcpServers")
        configured_mcp_url = (
            str(servers["bmem"].get("url"))
            if isinstance(servers, dict) and isinstance(servers.get("bmem"), dict)
            else None
        )
        managed_mcp = bool(configured_mcp_url) and configured_mcp_url == _mcp_url(api_base)
    except DomainError as exc:
        errors.append(exc.message)
    status = "ready" if version and managed_hooks and managed_mcp and not errors else "partial"
    notes: list[str] = [] if version else ["claude CLI not found on PATH"]
    if not managed_mcp and configured_mcp_url:
        notes.append(
            f"configured bmem MCP url {configured_mcp_url} does not match this URL's {_mcp_url(api_base)}"
        )
    return {
        "harness": "claude-code",
        "status": status,
        "installed": bool(version),
        "version": version,
        "config_root": str(root),
        "api_base": api_base,
        "mcp_url": _mcp_url(api_base),
        "paths": {"settings": str(settings_path), "mcp": str(mcp_path)},
        "managed": {"hooks": managed_hooks, "mcp": managed_mcp},
        "capabilities": {
            "native_protocol": "claude-json-hooks",
            "hooks": True,
            "mcp": True,
            "hook_events": ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"],
        },
        "errors": errors,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Codex connect / doctor
# ---------------------------------------------------------------------------


def _codex_group_is_managed(group: Any) -> bool:
    if not hasattr(group, "get"):
        return False
    handlers = group.get("hooks", []) or []
    for hook in handlers:
        if hasattr(hook, "get") and _managed_command(hook.get("command"), "codex"):
            return True
    return False


def _codex_new_aot(event_name: str, command: str) -> Any:
    """Build a real ``[[hooks.<Event>]]`` array-of-tables via tomlkit.

    An inline array emitted as a multiline inline table is rejected by Codex's
    TOML parser (verified with app-server hooks/list), so managed groups are
    always emitted as array-of-tables records.
    """

    snippet = (
        f"[[hooks.{event_name}]]\n"
        'matcher = ""\n'
        f"[[hooks.{event_name}.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command)}\n"
        "async = false\n"
        "timeout = 10\n"
    )
    return tomlkit.parse(snippet)["hooks"][event_name]


def _codex_convert_legacy_groups(event_name: str, groups: Any) -> Any:
    """Convert an inline/list hooks array to a valid AoT without dropping hooks."""

    aot = tomlkit.parse(f'[[hooks.{event_name}]]\nmatcher = ""\n')["hooks"][event_name]
    aot.remove(aot[0])
    for group in groups:
        if not hasattr(group, "get"):
            continue
        table = tomlkit.table()
        matcher = group.get("matcher")
        if matcher is not None:
            table["matcher"] = matcher
        handlers = group.get("hooks", []) or []
        handler_array = tomlkit.array()
        for handler in handlers:
            if not hasattr(handler, "get"):
                continue
            inline = tomlkit.inline_table()
            for key, value in handler.items():
                inline[key] = value
            handler_array.append(inline)
        table["hooks"] = handler_array
        aot.append(table)
    return aot


def _codex_merge_hooks(event_name: str, existing: Any, command: str) -> Any:
    if existing is None:
        return _codex_new_aot(event_name, command)
    if existing.__class__.__name__ == "AoT":
        for group in list(existing):
            if _codex_group_is_managed(group):
                existing.remove(group)
        for group in _codex_new_aot(event_name, command):
            existing.append(group)
        return existing
    if isinstance(existing, (list, tuple)) or existing.__class__.__name__ == "Array":
        groups = list(existing)
        managed = [group for group in groups if _codex_group_is_managed(group)]
        kept = [group for group in groups if not _codex_group_is_managed(group)]
        converted = _codex_convert_legacy_groups(event_name, kept)
        for group in _codex_new_aot(event_name, command):
            converted.append(group)
        return converted
    raise DomainError(409, f"hooks.{event_name} must be an array")


def _connect_codex(root: Path, api_base: str, changes: list[str], backups: list[str]) -> dict[str, Any]:
    codex_home = _harness_home(root, "codex")
    config_path = codex_home / "config.toml"
    version = _probe_version("codex")

    document = _read_toml_document(config_path)

    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        document["mcp_servers"] = servers
    if not hasattr(servers, "get"):
        raise DomainError(409, f"{config_path} has a non-table mcp_servers value")
    servers["bmem"] = {"url": _mcp_url(api_base)}

    hooks = document.get("hooks")
    if hooks is None:
        hooks = tomlkit.table()
        document["hooks"] = hooks
    if not hasattr(hooks, "get"):
        raise DomainError(409, f"{config_path} has a non-table hooks value")

    for event_name, hook_event in (
        ("UserPromptSubmit", "user-prompt-submit"),
        ("Stop", "stop"),
    ):
        hooks[event_name] = _codex_merge_hooks(
            event_name,
            hooks.get(event_name),
            _hook_command("codex", hook_event),
        )

    candidate = document.as_string()
    if candidate and not candidate.endswith("\n"):
        candidate += "\n"
    current = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    semantically_same = False
    if current:
        try:
            semantically_same = tomlkit.parse(current).unwrap() == document.unwrap()
        except Exception:
            semantically_same = False
    if candidate != current and not semantically_same:
        backup = _backup(config_path)
        if backup:
            backups.append(backup)
        _atomic_write(config_path, candidate, mode=0o600)
        changes.append(str(config_path))

    return {
        "harness": "codex",
        "status": "partial",
        "version": version,
        "config_root": str(root),
        "config_home": str(codex_home),
        "paths": {"config": str(config_path)},
        "changed": changes,
        "backups": backups,
        "capabilities": {
            "native_protocol": "codex-hooks-json",
            "hooks": True,
            "mcp": True,
            "hook_events": ["UserPromptSubmit", "Stop"],
            "app_server_protocol": "codex app-server hooks/list verified",
            "hook_trust_required": True,
            "format_source": "installed codex 0.167.0 config.toml + app-server hooks/list",
        },
        "notes": [
            "Codex requires a user trust approval for new/changed command hooks; "
            "hooks remain untrusted until approved. Use `codex app-server` "
            "hooks/list to inspect trustStatus.",
            "MCP uses the native [mcp_servers.bmem] URL table.",
        ],
        "partial_reasons": ["hook trust approval is a Codex-owned gate"],
    }


def _doctor_codex_hooks_list(root: Path, codex_home: Path) -> dict[str, Any]:
    """Read the installed hooks once via ``codex app-server hooks/list``.

    Read-only: one start, ``initialize`` + ``hooks/list``, one ``finally``
    shutdown.  Only enabled/trust metadata of bmem's own command hooks is
    reported; raw commands and RPC payloads never leave this function.
    """

    events = {"userPromptSubmit": "UserPromptSubmit", "stop": "Stop"}
    result: dict[str, Any] = {
        "listed": False,
        "error": None,
        "events": {
            name: {"present": False, "enabled": None, "trust_status": None}
            for name in events.values()
        },
    }
    executable = shutil.which("codex")
    if executable is None:
        result["error"] = "codex-cli-not-found"
        return result
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        _rpc_send(process, {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "bmem-doctor", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        init = _rpc_read(process, 1)
        if not isinstance(init.get("result"), dict):
            raise RuntimeError("initialize failed")
        _rpc_send(process, {"method": "initialized", "params": {}})
        _rpc_send(process, {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}})
        response = _rpc_read(process, 2)
        payload = response.get("result")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("hooks/list failed")
        for entry in data:
            if not isinstance(entry, dict):
                continue
            errors = entry.get("errors")
            if isinstance(errors, list) and errors:
                result["error"] = "hooks/list reported cwd errors"
            hooks = entry.get("hooks")
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                if not isinstance(hook, dict):
                    continue
                if not _managed_command(hook.get("command"), "codex"):
                    continue
                event = events.get(str(hook.get("eventName") or ""))
                if event is None:
                    continue
                slot = result["events"][event]
                slot["present"] = True
                enabled = hook.get("enabled")
                trust = hook.get("trustStatus")
                slot["enabled"] = True if enabled is not False else False
                slot["trust_status"] = str(trust) if isinstance(trust, str) else "unknown"
        result["listed"] = True
    except Exception as exc:  # noqa: BLE001 - the report is the error itself
        result["error"] = _safe_error(exc)
    finally:
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
    return result


def _doctor_codex(root: Path, url: str | None = None) -> dict[str, Any]:
    codex_home = _harness_home(root, "codex")
    config_path = codex_home / "config.toml"
    api_base = _api_base(url)
    executable = shutil.which("codex")
    version = _probe_version("codex")
    managed_hooks = False
    managed_mcp = False
    configured_mcp_url: str | None = None
    errors: list[str] = []
    try:
        document = _read_toml_document(config_path)
        hooks = document.get("hooks")
        if hasattr(hooks, "get"):
            managed_hooks = any(
                _codex_group_is_managed(group)
                for event in ("UserPromptSubmit", "Stop", "SessionStart")
                for group in (hooks.get(event) or [])
            )
        servers = document.get("mcp_servers")
        configured_mcp_url = (
            str(servers["bmem"].get("url"))
            if hasattr(servers, "get") and hasattr(servers.get("bmem"), "get")
            else None
        )
        managed_mcp = bool(configured_mcp_url) and configured_mcp_url == _mcp_url(api_base)
    except DomainError as exc:
        errors.append(exc.message)
    if not managed_hooks and configured_mcp_url is None and not errors and executable is None:
        return {
            "harness": "codex",
            "status": "unavailable",
            "installed": False,
            "version": version,
            "config_root": str(root),
            "api_base": api_base,
            "mcp_url": _mcp_url(api_base),
            "paths": {"config": str(config_path)},
            "managed": {"hooks": False, "mcp": False},
            "capabilities": {
                "native_protocol": "codex app-server hooks/list",
                "hooks": managed_hooks,
                "mcp": managed_mcp,
                "hook_events": ["UserPromptSubmit", "Stop"],
                "app_server_protocol": True,
                "hook_trust_required": True,
            },
            "errors": errors,
            "notes": ["codex CLI not found and no bmem hooks/MCP configured"],
        }

    hooks_list = _doctor_codex_hooks_list(root, codex_home)
    events = hooks_list["events"]
    problems: list[str] = []
    for event in ("UserPromptSubmit", "Stop"):
        slot = events[event]
        if not slot["present"]:
            problems.append(f"{event} bmem command hook is not listed by codex")
        elif slot["enabled"] is not True:
            problems.append(f"{event} bmem command hook is disabled")
        elif slot["trust_status"] not in ("trusted", "managed"):
            problems.append(
                f"{event} bmem command hook is not trusted (trustStatus={slot['trust_status']})"
            )
    if hooks_list["error"] and not problems:
        problems.append(f"codex hooks/list failed: {hooks_list['error']}")
    if not executable:
        problems.append("codex CLI not found")
    if not managed_mcp:
        problems.append(
            "bmem MCP config is missing or stale"
            if configured_mcp_url else "bmem MCP config is not configured"
        )
    if errors:
        problems.append("; ".join(errors))
    connected = not problems
    notes: list[str] = []
    if connected:
        notes.append("bmem UserPromptSubmit and Stop hooks are enabled and trusted by codex")
    else:
        notes.extend(problems)
    if not managed_hooks:
        notes.append("no managed bmem hooks are found in codex config.toml")
    if not managed_mcp and configured_mcp_url:
        notes.append("configured bmem MCP url does not match this URL's endpoint")
    return {
        "harness": "codex",
        "status": "connected" if connected else "partial",
        "installed": bool(version or executable),
        "version": version,
        "config_root": str(root),
        "api_base": api_base,
        "mcp_url": _mcp_url(api_base),
        "paths": {"config": str(config_path)},
        "managed": {"hooks": managed_hooks, "mcp": managed_mcp},
        "hook_trust": {
            "listed": hooks_list["listed"],
            "error": hooks_list["error"],
            "events": events,
        },
        "capabilities": {
            "native_protocol": "codex app-server hooks/list",
            "hooks": hooks_list["listed"],
            "mcp": managed_mcp,
            "hook_events": ["UserPromptSubmit", "Stop"],
            "app_server_protocol": True,
            "hook_trust_required": True,
        },
        "errors": errors,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# DSH connect / doctor
# ---------------------------------------------------------------------------


_DSH_BLOCK_BEGIN = "# BEGIN BMEM MANAGED (do not edit; idempotent block)"
_DSH_BLOCK_END = "# END BMEM MANAGED"
_DSH_PATCH_ROW_RE = re.compile(r"^\s*-\s+id:\s*(?P<id>[A-Za-z0-9._:-]+)\s*$")
_DSH_PATCH_URL_RE = re.compile(r"""^\s+url:\s*(?P<url>"[^"]*"|'[^']*'|[^\s#]+)\s*$""")


def _dsh_managed_block(api_base: str) -> str:
    plugin_path = Path(__file__).with_name("dsh_plugin.mjs")
    try:
        plugin_uri = plugin_path.resolve().as_uri()
    except OSError:
        plugin_uri = "file://" + str(plugin_path.absolute())
    plugin_name = json.dumps(plugin_uri, ensure_ascii=False)
    api_name = json.dumps(api_base, ensure_ascii=False)
    mcp_name = json.dumps(_mcp_url(api_base), ensure_ascii=False)
    return (
        f"{_DSH_BLOCK_BEGIN}\n"
        "- insert:\n"
        "    - id: bmem-dsh-native\n"
        f"      name: {plugin_name}\n"
        "      config:\n"
        f"        url: {api_name}\n"
        "        budget: 2000\n"
        "        target: cloud\n"
        "    - id: bmem-dsh-mcp\n"
        "      name: \"@deepseek-ai/dsh-mcp-client\"\n"
        "      config:\n"
        "        serverName: bmem\n"
        "        transport: streamable-http\n"
        f"        url: {mcp_name}\n"
        "        failOnStartupError: false\n"
        f"{_DSH_BLOCK_END}\n"
    )


def _connect_dsh(root: Path, api_base: str, changes: list[str], backups: list[str]) -> dict[str, Any]:
    dsh_home = _harness_home(root, "dsh")
    patch_path = dsh_home / "cordis.patch.yml"
    version = _probe_version("dsh")
    profiles_dir = dsh_home / "profiles"
    profiles = sorted(
        child.name for child in profiles_dir.iterdir()
        if child.is_dir()
    ) if profiles_dir.is_dir() else []

    current = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    block = _dsh_managed_block(api_base)
    if current.find(_DSH_BLOCK_BEGIN) >= 0:
        start = current.find(_DSH_BLOCK_BEGIN)
        end = current.find(_DSH_BLOCK_END, start)
        if end < 0:
            raise DomainError(409, f"{patch_path} has an unterminated bmem managed block")
        end += len(_DSH_BLOCK_END)
        if end < len(current) and current[end:end + 1] == "\n":
            end += 1
        candidate = current[:start] + block + current[end:]
    else:
        stripped = current.strip()
        if stripped and not stripped.startswith(("-", "#", "[")):
            raise DomainError(
                409,
                f"{patch_path} is not a YAML patch list; refusing to append a managed list",
            )
        if current and not current.endswith("\n"):
            current += "\n"
        if current.strip():
            current += "\n"
        candidate = current + block

    if candidate != current:
        backup = _backup(patch_path)
        if backup:
            backups.append(backup)
        _atomic_write(patch_path, candidate)
        changes.append(str(patch_path))

    plugin_file = Path(__file__).with_name("dsh_plugin.mjs")
    status = "connected" if version and profiles and plugin_file.is_file() else "partial"
    notes: list[str] = []
    if not version:
        notes.append("dsh CLI was not found; the native plugin row was still written")
    if not profiles:
        notes.append(
            "no DSH profile exists under $DSH_HOME/profiles; the home-level patch "
            "is valid but DSH needs a profile to boot"
        )
    if not plugin_file.is_file():
        notes.append("bmem/dsh_plugin.mjs is missing from this installation")
    return {
        "harness": "dsh",
        "status": status,
        "version": version,
        "config_root": str(root),
        "config_home": str(dsh_home),
        "paths": {"patch": str(patch_path), "plugin": str(plugin_file)},
        "changed": changes,
        "backups": backups,
        "profiles": profiles,
        "capabilities": {
            "native_protocol": "cordis-agent-pre-step + session/event",
            "native_plugin": plugin_file.is_file(),
            "hooks": True,
            "mcp": True,
            "mcp_client": "@deepseek-ai/dsh-mcp-client",
            "format_source": "installed dsh 0.2.2-rc.1 Cordis absolute-file-URL insert",
        },
        "notes": notes,
        "partial_reasons": [
            reason for reason in (
                None if version else "dsh CLI unavailable",
                None if profiles else "profile-required",
                None if plugin_file.is_file() else "plugin-file-missing",
            ) if reason
        ],
    }


def _dsh_patch_urls(text: str) -> dict[str, str]:
    """Map each managed Cordis row id to the URL that row configured.

    Used by ``doctor`` so a supplied ``--url`` is evaluated against what the
    plugin row will actually talk to, instead of only against the default.
    """

    found: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        row = _DSH_PATCH_ROW_RE.match(line)
        if row:
            current = row.group("id")
            continue
        if current is None:
            continue
        match = _DSH_PATCH_URL_RE.match(line)
        if match:
            value = match.group("url").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
                value = value[1:-1]
            found.setdefault(current, value)
    return found


def _doctor_dsh(root: Path, url: str | None = None) -> dict[str, Any]:
    dsh_home = _harness_home(root, "dsh")
    patch_path = dsh_home / "cordis.patch.yml"
    plugin_file = Path(__file__).with_name("dsh_plugin.mjs")
    api_base = _api_base(url)
    version = _probe_version("dsh")
    profiles_dir = dsh_home / "profiles"
    profiles = sorted(
        child.name for child in profiles_dir.iterdir()
        if child.is_dir()
    ) if profiles_dir.is_dir() else []
    managed = False
    patch_urls: dict[str, str] = {}
    errors: list[str] = []
    try:
        if patch_path.exists():
            patch_text = patch_path.read_text(encoding="utf-8")
            managed = _DSH_BLOCK_BEGIN in patch_text
            patch_urls = _dsh_patch_urls(patch_text)
    except OSError as exc:
        errors.append(_safe_error(exc))
    plugin_url = patch_urls.get("bmem-dsh-native")
    configured_mcp_url = patch_urls.get("bmem-dsh-mcp")
    url_matches = (
        plugin_url == api_base and configured_mcp_url == _mcp_url(api_base)
    ) if managed else False
    status = "connected" if version and managed and profiles and plugin_file.is_file() else "partial"
    if status == "connected" and url is not None and not url_matches:
        # The rows exist but point somewhere else than the URL under review.
        status = "partial"
    notes: list[str] = [] if profiles else [
        "no DSH profile exists; a home-level patch alone cannot boot DSH"
    ]
    if managed and not url_matches:
        if plugin_url is None:
            notes.append(
                f"the managed patch has no plugin url; the plugin falls back to $BMEM_URL/"
                f"{api_base} at DSH runtime"
            )
        elif plugin_url != api_base:
            notes.append(
                f"plugin url {plugin_url} does not match this URL's {api_base}; re-run "
                f"`bmem connect dsh --url {api_base}`"
            )
        if configured_mcp_url and configured_mcp_url != _mcp_url(api_base):
            notes.append(
                f"MCP row url {configured_mcp_url} does not match this URL's {_mcp_url(api_base)}"
            )
    return {
        "harness": "dsh",
        "status": status,
        "installed": bool(version),
        "version": version,
        "config_root": str(root),
        "api_base": api_base,
        "mcp_url": _mcp_url(api_base),
        "paths": {"patch": str(patch_path), "plugin": str(plugin_file)},
        "profiles": profiles,
        "managed": {
            "patch": managed,
            "plugin_file": plugin_file.is_file(),
            "url_matches": url_matches,
        },
        "configured": {"plugin_url": plugin_url, "mcp_url": configured_mcp_url},
        "capabilities": {
            "native_protocol": "cordis systemPrompt context registry + session/event capture",
            "native_plugin": plugin_file.is_file(),
            "runtime_context_registry": True,
            "hooks": True,
            "mcp": True,
            "mcp_client": "@deepseek-ai/dsh-mcp-client",
        },
        "errors": errors,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Public connect / doctor
# ---------------------------------------------------------------------------


def connect(harness: str, config_root: Path | None = None, url: str | None = None) -> dict:
    """Install idempotent native hooks/MCP configuration for one harness."""

    normalized = _normalize_harness(harness)
    root = _config_root(config_root)
    api_base = _api_base(url)
    if normalized == "all":
        items = [
            connect(item, config_root=root, url=api_base)
            for item in _HARNESSES
        ]
        statuses = {item["status"] for item in items}
        return {
            "harness": "all",
            "status": "partial" if "partial" in statuses else "connected",
            "config_root": str(root),
            "items": items,
            "changed": [path for item in items for path in item.get("changed", [])],
            "backups": [path for item in items for path in item.get("backups", [])],
        }
    changes: list[str] = []
    backups: list[str] = []
    try:
        if normalized == "claude-code":
            return _connect_claude(root, api_base, changes, backups)
        if normalized == "codex":
            return _connect_codex(root, api_base, changes, backups)
        return _connect_dsh(root, api_base, changes, backups)
    except DomainError:
        raise
    except Exception as exc:  # defensive conversion for CLI/API callers
        raise DomainError(500, f"{normalized} connect failed: {_safe_error(exc)}") from exc


def doctor(config_root: Path | None = None, url: str | None = None) -> list[dict]:
    """Report real installed capability state for the three supported harnesses.

    ``url`` is the same value the CLI/HTTP caller advertises (``--url``/``$BMEM_URL``
    or ``http://127.0.0.1:$BMEM_PORT``).  Every adapter resolves it through
    ``_api_base`` exactly like ``connect`` and like the native plugin's own
    ``apiBase()``, so a doctor run against a non-default port evaluates the URLs
    that row would really use.  ``config_root`` stays the *Harness* configuration
    root; it is never the bmem database home (``BMEM_HOME``).
    """

    root = _config_root(config_root)
    return [
        _doctor_claude(root, url),
        _doctor_codex(root, url),
        _doctor_dsh(root, url),
    ]


# ---------------------------------------------------------------------------
# Hook path (HTTP recall/capture)
# ---------------------------------------------------------------------------

_HOOK_EVENTS = {
    "user-prompt-submit": "UserPromptSubmit",
    "userpromptsubmit": "UserPromptSubmit",
    "user_prompt_submit": "UserPromptSubmit",
    "prompt": "UserPromptSubmit",
    "session-start": "SessionStart",
    "sessionstart": "SessionStart",
    "session_start": "SessionStart",
    "stop": "Stop",
    "session-end": "SessionEnd",
    "sessionend": "SessionEnd",
    "session_end": "SessionEnd",
    "post-tool-use": "PostToolUse",
    "posttooluse": "PostToolUse",
    "post_tool_use": "PostToolUse",
    "pre-tool-use": "PreToolUse",
    "precompact": "PreCompact",
    "pre-compact": "PreCompact",
    "postcompact": "PostCompact",
    "post-compact": "PostCompact",
}


def _normalize_event(event: str) -> str:
    key = str(event or "").strip().replace("_", "-").lower()
    native = _HOOK_EVENTS.get(key)
    if native is None:
        native = _HOOK_EVENTS.get(key.replace("-", ""), "")
    return native or str(event or "").strip()


def _payload_text(payload: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [str(item) for item in value if isinstance(item, str) and item.strip()]
            if parts:
                return "\n".join(parts)
    return ""


def _hook_session_id(payload: dict[str, Any]) -> str:
    for key in ("session_id", "sessionId", "thread_id", "threadId", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "hook"


def _hook_turn_id(payload: dict[str, Any]) -> str:
    for key in ("turn_id", "turnId", "prompt_id", "promptId", "message_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _hook_project(payload: dict[str, Any]) -> str:
    for key in ("cwd", "project", "working_directory", "workdir"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _hook_occurred_at(payload: dict[str, Any]) -> str:
    for key in ("timestamp", "occurred_at", "time"):
        value = payload.get(key)
        if value is not None:
            return _iso_time(value)
    return _now_iso()


def _hook_identity(harness: str, payload: dict[str, Any], body: str, event: str) -> str:
    session_id = _hook_session_id(payload)
    turn_id = _hook_turn_id(payload)
    transcript = payload.get("transcript_path")
    size = ""
    if isinstance(transcript, str):
        try:
            size = str(Path(transcript).stat().st_size)
        except OSError:
            size = ""
    token = "|".join([
        harness,
        session_id,
        turn_id,
        size,
        event,
        _sha256(body)[:24],
    ])
    return f"{harness}:hook:{_sha256(token)[:28]}"


def _post_event(api_base: str, candidate: dict[str, Any], timeout: float = 1.5) -> bool:
    mode = candidate.get("mode", "event")
    endpoint = "/api/declarations" if mode == "declare" else "/api/events"
    payload = {key: value for key, value in candidate.items() if key != "mode"}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(0.5, timeout)),
            follow_redirects=False,
        ) as client:
            response = client.post(api_base + endpoint, json=payload)
        return 200 <= response.status_code < 300
    except Exception:
        return False


def _recall_via_http(
    api_base: str,
    *,
    query: str,
    harness: str,
    session_id: str,
    turn_id: str,
    context_epoch: str,
    project: str,
    budget: int,
    target: str,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    payload = {
        "query": query,
        "harness": harness,
        "session_id": session_id,
        "turn_id": turn_id,
        "task": "",
        "context_epoch": context_epoch,
        "budget": budget,
        "target": target,
        "conditions": {},
        "project": project,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(0.5, timeout)),
            follow_redirects=False,
        ) as client:
            response = client.post(api_base + "/api/recall", json=payload)
        if response.status_code >= 300:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _native_hook_output(native_event: str, text: str) -> dict[str, Any]:
    if not text:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": native_event,
            "additionalContext": text,
        }
    }


def hook(harness: str, event: str, payload: dict, url: str | None = None) -> dict:
    """Handle one native hook callback and return its native JSON response.

    The HTTP endpoint is called with a deliberately short timeout.  A missing or
    slow service degrades to no additional context instead of breaking a harness
    turn.
    """

    normalized = _normalize_harness(harness)
    if normalized == "all":
        raise DomainError(422, "hook requires one concrete harness")
    if not isinstance(payload, dict):
        raise DomainError(422, "hook payload must be an object")
    native_event = _normalize_event(event)
    api_base = _api_base(url)
    occurred_at = _hook_occurred_at(payload)
    session_id = _hook_session_id(payload)
    turn_id = _hook_turn_id(payload)
    project = _hook_project(payload)

    try:
        if native_event == "UserPromptSubmit":
            prompt = _payload_text(
                payload,
                ("prompt", "user_prompt", "userPrompt", "text", "message"),
            )
            if prompt:
                candidate = _make_candidate(
                    body=prompt,
                    source_type="user_dialogue",
                    native_event_id=_hook_identity(normalized, payload, prompt, "user"),
                    occurred_at=occurred_at,
                    session_id=session_id,
                    project=project,
                    harness=normalized,
                    metadata={
                        "hook_event": native_event,
                        "origin": "hook-user-dialogue",
                        "turn_id": turn_id,
                    },
                )
                if candidate is not None:
                    _post_event(api_base, candidate)
                recall = _recall_via_http(
                    api_base,
                    query=prompt,
                    harness=normalized,
                    session_id=session_id,
                    turn_id=turn_id,
                    context_epoch=f"session:{session_id}",
                    project=project,
                    budget=2000,
                    target="cloud",
                )
                text = str((recall or {}).get("text") or "")
                return _native_hook_output(native_event, text)

        if native_event == "SessionStart":
            # No user text exists yet at session start, so recall assembles
            # the current-context pack server-side.  The SessionStart output
            # is only injected when the server actually delivered memories.
            recall = _recall_via_http(
                api_base,
                query="",
                harness=normalized,
                session_id=session_id,
                turn_id=turn_id,
                context_epoch=f"session:{session_id}",
                project=project,
                budget=2000,
                target="cloud",
            )
            if (recall or {}).get("items"):
                text = str(recall.get("text") or "")
                return _native_hook_output(native_event, text)
            return {}

        if native_event == "SessionEnd":
            # Final delta capture so a session that ends without a trailing
            # Stop (or whose last Stop raced the transcript flush) is complete.
            _capture_transcript_delta(normalized, payload, api_base)
            return {}

        if native_event == "Stop":
            captured, consumed = _capture_transcript_delta(normalized, payload, api_base)
            if not consumed:
                last = _payload_text(
                    payload,
                    ("last_assistant_message", "lastAssistantMessage", "assistant_message"),
                )
                if last:
                    candidate = _make_candidate(
                        body=last,
                        source_type="agent_inference",
                        native_event_id=_hook_identity(
                            normalized, payload, last, "stop-assistant"
                        ),
                        occurred_at=occurred_at,
                        session_id=session_id,
                        project=project,
                        harness=normalized,
                        metadata={
                            "hook_event": native_event,
                            "origin": "hook-stop-fallback",
                            "turn_id": turn_id,
                        },
                    )
                    if candidate is not None:
                        _post_event(api_base, candidate)
            return {}

        if native_event == "PostToolUse":
            tool_text = _tool_output_text(
                payload.get("tool_response", payload.get("toolResponse"))
            )
            if tool_text:
                candidate = _make_candidate(
                    body=tool_text,
                    source_type="external",
                    native_event_id=_hook_identity(normalized, payload, tool_text, "tool"),
                    occurred_at=occurred_at,
                    session_id=session_id,
                    project=project,
                    harness=normalized,
                    metadata={
                        "hook_event": native_event,
                        "origin": "hook-tool-output",
                        "tool_name": str(payload.get("tool_name") or ""),
                    },
                )
                if candidate is not None:
                    _post_event(api_base, candidate)
            return {}

        # PreCompact/Notification/other lifecycle events have no handled
        # context in this build.
        return {}
    except DomainError:
        raise
    except Exception:
        # Hook failures must never break the host turn; no raw body is logged.
        return {}


# ---------------------------------------------------------------------------
# Transcript delta capture
# ---------------------------------------------------------------------------


def _hook_cursor_path(path: Path) -> Path | None:
    home = Path(os.environ.get("BMEM_HOME") or (Path.home() / ".botinkit-mem"))
    try:
        cursor_dir = home / "hook-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        return cursor_dir / f"{_path_key(path)}.json"
    except OSError:
        return None


def _load_cursor_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_cursor_file(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        _atomic_write(path, json.dumps(value, sort_keys=True) + "\n")
    except OSError:
        pass


def _read_transcript_lines(path: Path) -> Iterator[tuple[int, Any, str | None]]:
    """Yield ``(line_no, object, error)`` from Claude/Codex/DSH JSONL."""

    name = path.name.lower()
    try:
        if name.endswith(".zstd"):
            with path.open("rb") as raw:
                reader = zstd.ZstdDecompressor().stream_reader(raw)
                data = reader.read().decode("utf-8", errors="replace")
        else:
            data = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, zstd.ZstdError) as exc:
        yield 0, None, f"{type(exc).__name__}: {_safe_error(exc)}"
        return
    for line_no, line in enumerate(data.splitlines()):
        if not line.strip():
            continue
        try:
            yield line_no, json.loads(line), None
        except json.JSONDecodeError:
            yield line_no, None, "json_decode"


def _transcript_hints(
    harness: str,
    record: dict[str, Any],
    session_hint: str | None,
    project_hint: str | None,
) -> tuple[str | None, str | None]:
    """Carry session/project identity across a delta read after the header line."""

    record_type = record.get("type")
    if harness == "claude-code":
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            session_hint = sid
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            project_hint = cwd
    elif harness == "codex":
        payload = record.get("payload")
        if isinstance(payload, dict):
            if record_type == "session_meta":
                sid = payload.get("id") or payload.get("session_id")
                if isinstance(sid, str) and sid.strip():
                    session_hint = sid
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    project_hint = cwd
            elif record_type == "turn_context":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    project_hint = cwd
    elif harness == "dsh":
        if record_type == "session":
            sid = record.get("id")
            if isinstance(sid, str) and sid.strip():
                session_hint = sid
            cwd = record.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                project_hint = cwd
    return session_hint, project_hint


def _capture_transcript_delta(
    harness: str, payload: dict[str, Any], api_base: str
) -> tuple[int, bool]:
    """Capture new transcript lines.

    Returns ``(posted, consumed)``.  ``consumed`` means a transcript was found
    and read to its end (even when it had no new lines), which prevents the
    Stop hook from re-posting ``last_assistant_message`` on every stop.
    """

    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript.strip():
        return 0, False
    path = Path(transcript).expanduser()
    if not path.is_file():
        return 0, False
    try:
        stat = path.stat()
    except OSError:
        return 0, False
    cursor_path = _hook_cursor_path(path)
    cursor = _load_cursor_file(cursor_path)
    zstd_mode = path.name.lower().endswith(".zstd")
    offset = 0 if zstd_mode else int(cursor.get("byte_offset") or 0)
    line_no = int(cursor.get("line_no") or 0)
    if not zstd_mode and stat.st_size < offset:
        offset = 0
        line_no = 0

    file_key = _path_key(path)
    project_default = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    session_hint = cursor.get("session_id") if isinstance(cursor.get("session_id"), str) else None
    project_hint = cursor.get("project") if isinstance(cursor.get("project"), str) else None
    posted = 0
    processed_through = offset
    consumed = False

    if not zstd_mode:
        try:
            with path.open("rb") as handle:
                consumed = True
                handle.seek(offset)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        text_line = line.decode("utf-8", errors="replace")
                    except Exception:
                        line_no += 1
                        processed_through = handle.tell()
                        continue
                    if text_line.strip():
                        try:
                            record = json.loads(text_line)
                        except json.JSONDecodeError:
                            line_no += 1
                            processed_through = handle.tell()
                            continue
                        if isinstance(record, dict):
                            session_hint, project_hint = _transcript_hints(
                                harness, record, session_hint, project_hint
                            )
                            candidates = list(_candidates_for_record(
                                harness,
                                record,
                                file_key=file_key,
                                line_no=line_no,
                                fallback_time=_file_mtime(path),
                                project_default=project_default,
                                source_path=str(path),
                                session_hint=session_hint,
                                project_hint=project_hint,
                            ))
                            ok = True
                            for candidate in candidates:
                                if candidate is None:
                                    continue
                                ok = _post_event(api_base, candidate) and ok
                                if ok:
                                    posted += 1
                                else:
                                    break
                            if not ok:
                                break
                    line_no += 1
                    processed_through = handle.tell()
        except OSError:
            return posted, consumed
    else:
        consumed = True
        # Compressed DSH transcripts cannot be byte-seeked safely; use a
        # decompressed line counter so a new tail is still captured.
        all_lines: list[tuple[int, Any, str | None]] = list(_read_transcript_lines(path))
        for current_line_no, record, error in all_lines:
            if current_line_no < line_no:
                continue
            if error or not isinstance(record, dict):
                line_no = current_line_no + 1
                continue
            session_hint, project_hint = _transcript_hints(
                harness, record, session_hint, project_hint
            )
            candidates = list(_candidates_for_record(
                harness,
                record,
                file_key=file_key,
                line_no=current_line_no,
                fallback_time=_file_mtime(path),
                project_default=project_default,
                source_path=str(path),
                session_hint=session_hint,
                project_hint=project_hint,
            ))
            ok = True
            for candidate in candidates:
                if candidate is None:
                    continue
                ok = _post_event(api_base, candidate) and ok
                if ok:
                    posted += 1
                else:
                    break
            if not ok:
                break
            line_no = current_line_no + 1
        processed_through = stat.st_size

    _save_cursor_file(cursor_path, {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "byte_offset": processed_through if not zstd_mode else stat.st_size,
        "line_no": line_no,
        "session_id": session_hint,
        "project": project_hint,
        "updated_at": _now_iso(),
    })
    return posted, consumed


# ---------------------------------------------------------------------------
# History import: discovery
# ---------------------------------------------------------------------------


def _glob(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(path for path in root.glob(pattern) if path.is_file())
    except OSError:
        return []


_CLAUDE_PROJECT_MEMORY_GLOB = "projects/**/memory/**/*.md"


def _discover_markdown(root: Path, harness: str) -> list[Path]:
    """Enumerate existing native Markdown memory files under ``root``.

    Claude Code keeps per-project memory under
    ``projects/<encoded-project>/memory/**`` and may carry a project-local
    ``CLAUDE.md`` inside the encoded project directory; both are discovered in
    addition to the user-level files.  DSH native memory directories are
    ``~/.dsh/memory``/``memories`` plus ``~/.dsh/projects/*/memory``.  Nothing is
    written here and no ACL is globalized: only files that already exist are
    enumerated.
    """

    claude_home = _harness_home(root, "claude-code")
    codex_home = _harness_home(root, "codex")
    dsh_home = _harness_home(root, "dsh")
    files: list[Path] = []
    if harness in ("all", "claude-code"):
        files.extend(_glob(root, "CLAUDE.md"))
        files.extend(_glob(claude_home, "CLAUDE.md"))
        files.extend(_glob(claude_home, "memory/**/*.md"))
        files.extend(_glob(claude_home, "memories/**/*.md"))
        files.extend(_glob(claude_home, _CLAUDE_PROJECT_MEMORY_GLOB))
        files.extend(_glob(claude_home, "projects/**/CLAUDE.md"))
    if harness in ("all", "codex"):
        files.extend(_glob(root, "AGENTS.md"))
        files.extend(_glob(codex_home, "AGENTS.md"))
        files.extend(_glob(codex_home, "memory/**/*.md"))
        files.extend(_glob(codex_home, "memories/**/*.md"))
    if harness in ("all", "dsh"):
        files.extend(_glob(dsh_home, "AGENTS.md"))
        files.extend(_glob(dsh_home, "memory/**/*.md"))
        files.extend(_glob(dsh_home, "memories/**/*.md"))
        files.extend(_glob(dsh_home, "projects/*/memory/**/*.md"))
        files.extend(_glob(dsh_home, "session-memory/**/*.md"))
    return files


def _discover_for_harness(root: Path, harness: str) -> list[Path]:
    files: list[Path] = []
    if harness in ("all", "claude-code"):
        claude_home = _harness_home(root, "claude-code")
        files.extend(_glob(claude_home, "projects/**/*.jsonl"))
        files.extend(_glob(claude_home, "projects/**/*.jsonl.zstd"))
    if harness in ("all", "codex"):
        codex_home = _harness_home(root, "codex")
        files.extend(_glob(codex_home, "sessions/**/*.jsonl"))
        files.extend(_glob(codex_home, "archived_sessions/**/*.jsonl"))
        files.extend(_glob(codex_home, "rollouts/**/*.jsonl"))
    if harness in ("all", "dsh"):
        dsh_home = _harness_home(root, "dsh")
        files.extend(_glob(dsh_home, "**/*.jsonl"))
        files.extend(_glob(dsh_home, "**/*.jsonl.zstd"))
    files.extend(_discover_markdown(root, harness))
    return files


def _detect_harness(path: Path) -> str | None:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if ".dsh" in parts or name.endswith(".jsonl.zstd"):
        return "dsh"
    if ".codex" in parts:
        return "codex"
    if ".claude" in parts:
        return "claude-code"
    if name == "claude.md":
        return "claude-code"
    if name == "agents.md":
        return "codex"
    if path.suffix.lower() == ".md":
        return "manual"
    first: dict[str, Any] | None = None
    try:
        if name.endswith(".zstd"):
            with path.open("rb") as raw:
                reader = zstd.ZstdDecompressor().stream_reader(raw)
                first_line = reader.read(4096).decode("utf-8", errors="replace").splitlines()
        else:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                first_line = []
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    if line.strip():
                        first_line = [line]
                        break
        if first_line:
            first = json.loads(first_line[0])
    except (OSError, ValueError, zstd.ZstdError):
        first = None
    if isinstance(first, dict):
        if first.get("type") == "session":
            return "dsh"
        if first.get("type") in {"session_meta", "response_item", "event_msg", "turn_context"}:
            return "codex"
        if any(key in first for key in ("sessionId", "uuid", "parentUuid")):
            return "claude-code"
    return None


def _expand_paths(root: Path, paths: list[str], harness: str) -> tuple[list[tuple[Path, str]], list[dict[str, Any]]]:
    selected: list[tuple[Path, str]] = []
    errors: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            errors.append({"path": str(path), "error": "path not found"})
            continue
        if path.is_dir():
            candidates = [
                child for child in sorted(path.rglob("*"))
                if child.is_file() and child.suffix.lower() in (".jsonl", ".zstd", ".md")
            ]
            for child in candidates:
                detected = harness if harness != "all" else (_detect_harness(child) or "manual")
                if detected == "all":
                    detected = _detect_harness(child) or "manual"
                selected.append((child, detected))
            continue
        detected = harness if harness != "all" else (_detect_harness(path) or "manual")
        selected.append((path, detected))
    return selected, errors


# ---------------------------------------------------------------------------
# History import: parsers
# ---------------------------------------------------------------------------


def _decode_line(raw: str | bytes) -> tuple[Any, str | None]:
    """Parse one physical JSONL line (``str`` or ``bytes``) into an object."""

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return None, "decode_error"
    if not raw.strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "json_decode"


class _JsonlScan:
    """A bounded-memory, re-openable JSONL/Zstd record scan.

    ``iter_records`` streams the file one physical line at a time, yielding
    ``(line_no, object, error)`` with 0-based physical line numbers (blank
    lines keep their slot) and skipping malformed lines as recorded errors.
    ``count`` and ``eof_line_no`` give the physical line count without
    retaining any record.  The scan never holds more than one line, so the
    caller decides whether to pass the file once, or re-open it for a second
    full-file pass.

    Iteration failures (permission loss, truncated Zstd stream) are yielded as a
    structural ``(line_no, None, "read_error: ...")`` record instead of an
    opaque object, so callers can count and report them.
    """

    def __init__(self, path: Path):
        self.path = path
        self.compressed = path.name.lower().endswith(".zstd")
        self.read_error: str | None = None
        self.eof_line_no: int = -1

    def __iter__(self) -> Iterator[tuple[int, Any, str | None]]:
        return self.iter_records()

    def iter_records(self) -> Iterator[tuple[int, Any, str | None]]:
        line_no = -1
        try:
            raw = self.path.open("rb")
        except OSError as exc:
            self.read_error = _safe_error(exc)
            yield -1, None, f"read_error: {self.read_error}"
            return
        try:
            # TextIOBase.readline() yields one physical line per record; the
            # zstd stream reader already implements it over the decompressed
            # stream, and the wrapper decodes a single next chunk at a time.
            binary = zstd.ZstdDecompressor().stream_reader(raw) if self.compressed else raw
            with io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline="") as lines:
                for line in lines:
                    line_no += 1
                    self.eof_line_no = line_no
                    record, error = _decode_line(line)
                    if record is None and error is None:
                        continue
                    yield line_no, record, error
        except (OSError, zstd.ZstdError) as exc:
            self.read_error = _safe_error(exc)
            yield line_no + 1, None, f"read_error: {self.read_error}"
        finally:
            raw.close()

    def count(self) -> int:
        """Physical line count of a completed scan, without retaining records."""

        if self.eof_line_no >= 0:
            return self.eof_line_no + 1
        for _line_no, _record, _error in self.iter_records():
            pass
        return self.eof_line_no + 1


def _read_jsonl_records(path: Path, *, stream: bool = False) -> Any:
    """Small-file reader kept for compatibility; history import uses ``_JsonlScan``.

    Default (``stream=False``) returns a full list of ``(line_no, object,
    error)`` plus a ``str`` file-or-skip error.  With ``stream=True`` it returns
    the re-openable, one-line-at-a-time ``_JsonlScan`` instead.  A missing file yields ``([] , error)`` / an empty scan
    so callers can report the path instead of raising.
    """

    scan = _JsonlScan(path)
    if stream:
        return scan
    records: list[tuple[int, Any, str | None]] = []
    for line_no, record, error in scan.iter_records():
        if error is not None and error.startswith("read_error:"):
            return [], scan.read_error or error
        records.append((line_no, record, error))
    return records, None


def _claude_candidates(
    records: list[tuple[int, Any, str | None]],
    *,
    file_key: str,
    source_path: str,
    fallback_time: datetime,
    project_default: str = "",
    session_hint: str | None = None,
    project_hint: str | None = None,
) -> Iterator[tuple[dict[str, Any] | None, str | None]]:
    session_id = session_hint or Path(source_path).stem
    project = project_hint or project_default
    for line_no, record, error in records:
        if error:
            # Streaming read faults stay structural; malformed lines are skips.
            yield None, error if error.startswith("read_error:") else "json_decode"
            continue
        if not isinstance(record, dict):
            yield None, "non_object"
            continue
        record_type = str(record.get("type") or "")
        if record_type in {"summary", "system", "file-history-snapshot"}:
            continue
        if record.get("isMeta") is True:
            continue
        sid = record.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            session_id = sid
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            project = cwd
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or record_type or "").lower()
        if role not in {"user", "assistant"}:
            continue
        uuid_value = record.get("uuid")
        record_id = str(uuid_value) if isinstance(uuid_value, str) and uuid_value else f"{file_key}:{line_no}"
        for index, (text, block_kind) in enumerate(_content_parts(message.get("content"))):
            if block_kind == "tool_result":
                source_type = "external"
                template = "tool_result"
            elif role == "user":
                source_type = "user_dialogue"
                template = "user"
            else:
                source_type = "agent_inference"
                template = "assistant"
            native = f"claude:{session_id}:{record_id}:{index}"
            candidate = _candidate_for_text_block(
                text=text,
                role=role,
                kind=block_kind,
                source_type=source_type,
                native_event_id=native,
                occurred_at=record.get("timestamp") or fallback_time,
                session_id=session_id,
                project=project,
                harness="claude-code",
                metadata={
                    "source_path": source_path,
                    "record_type": record_type,
                    "origin": "claude-jsonl",
                    "template": template,
                },
            )
            yield candidate, None if candidate else "empty_or_oversize"


def _codex_candidates(
    records: Any,
    *,
    file_key: str,
    source_path: str,
    fallback_time: datetime,
    project_default: str = "",
    session_hint: str | None = None,
    project_hint: str | None = None,
) -> Iterator[tuple[dict[str, Any] | None, str | None]]:
    """Codex rollout parser over a bounded-memory record source.

    ``records`` may be a ``_JsonlScan``, another single-use iterable, or the
    legacy list used by hook deltas.  A scan is read twice: a whole-file
    ``state_of`` pass decides whether the canonical ``response_item`` layer
    exists anywhere in the file, then a fresh emit pass reports the records in
    physical line order.  Both passes stream one line at a time, so no header
    window, record count or byte threshold is involved and an ``event_msg``
    before the first ``response_item`` is skipped exactly like one after it.
    """

    session_id = session_hint or Path(source_path).stem
    project = project_hint or project_default

    def state_of(source: Iterable[tuple[int, Any, str | None]]) -> tuple[bool, str, str]:
        """Prepass: canonical layer present plus header session/project."""

        sid, proj, canonical = session_id, project, False
        for _line_no, record, error in source:
            if error is not None and error.startswith("read_error:"):
                raise RuntimeError(error)
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            payload = record.get("payload")
            if record_type == "session_meta" and isinstance(payload, dict):
                value = payload.get("id") or payload.get("session_id")
                if isinstance(value, str) and value.strip():
                    sid = value
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    proj = cwd
            elif record_type == "turn_context" and isinstance(payload, dict):
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    proj = cwd
            elif record_type == "response_item" and isinstance(payload, dict):
                if payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                    canonical = True
        return canonical, sid, proj

    def emit(source: Iterable[tuple[int, Any, str | None]], canonical: bool, sid: str, proj: str):
        """Emit records in file order with the canonical event_msg decision."""

        for line_no, record, error in source:
            if error is not None and error.startswith("read_error:"):
                raise RuntimeError(error)
            if error is not None:
                yield None, "json_decode"
                continue
            if not isinstance(record, dict):
                yield None, "non_object"
                continue
            record_type = str(record.get("type") or "")
            payload = record.get("payload")
            if record_type in {"session_meta", "turn_context"}:
                yield None, "metadata"
                continue
            if record_type == "compacted":
                yield None, "unsupported:compacted"
                continue
            if record_type in {"response_item", "event_msg"} and isinstance(payload, dict):
                if record_type == "event_msg" and canonical:
                    # Old parser counted exactly one discovery for this skip.
                    yield None, "skip:noncanonical_event_msg"
                    continue
                yield from _codex_message_candidates(
                    line_no,
                    record,
                    file_key=file_key,
                    source_path=source_path,
                    fallback_time=fallback_time,
                    session_id=sid,
                    project=proj,
                    has_response_items=canonical,
                )
                continue
            yield None, f"skip:record_type:{record_type or 'unknown'}"

    scan = records if isinstance(records, _JsonlScan) else None
    iterable: Iterable[tuple[int, Any, str | None]] = (
        scan.iter_records() if scan is not None else records
    )
    if scan is not None:
        canonical, session_id, project = state_of(iterable)
        # The full-file prepass consumed the (possibly Zstd) stream; emit from a
        # fresh one, so a rollout costs two streaming passes, both bounded.
        yield from emit(scan.iter_records(), canonical, session_id, project)
    elif isinstance(records, list):
        canonical, session_id, project = state_of(records)
        yield from emit(records, canonical, session_id, project)
    else:
        # Single-use iterable (hook single-record path): the window is the
        # iterable itself, so one pass is both prepass and emission.
        buffered = list(iterable)
        canonical, session_id, project = state_of(buffered)
        yield from emit(buffered, canonical, session_id, project)


def _codex_message_candidates(
    line_no: int,
    record: dict[str, Any],
    *,
    file_key: str,
    source_path: str,
    fallback_time: datetime,
    session_id: str,
    project: str,
    has_response_items: bool,
) -> Iterator[tuple[dict[str, Any] | None, str | None]]:
    """Emit the message/tool-output candidates of one canonical rollout record."""

    record_type = str(record.get("type") or "")
    payload = record.get("payload")
    timestamp = record.get("timestamp") or record.get("ts") or fallback_time
    if record_type == "response_item" and isinstance(payload, dict):
        item_type = str(payload.get("type") or "")
        if item_type == "message" and payload.get("role") in {"user", "assistant"}:
            role = str(payload["role"])
            item_id = payload.get("id")
            record_id = str(item_id) if isinstance(item_id, str) and item_id else f"{file_key}:{line_no}"
            source_type = "user_dialogue" if role == "user" else "agent_inference"
            template = role
            for index, (text, block_kind) in enumerate(_content_parts(payload.get("content"))):
                native = f"codex:{session_id}:{record_id}:{index}"
                candidate = _candidate_for_text_block(
                    text=text,
                    role=role,
                    kind=block_kind,
                    source_type=source_type,
                    native_event_id=native,
                    occurred_at=timestamp,
                    session_id=session_id,
                    project=project,
                    harness="codex",
                    metadata={
                        "source_path": source_path,
                        "record_type": record_type,
                        "origin": "codex-rollout",
                        "template": template,
                        "payload_type": item_type,
                    },
                )
                yield candidate, None if candidate else "empty_or_oversize"
            return
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            output = _tool_output_text(payload.get("output"))
            if output:
                record_id = payload.get("call_id") or payload.get("id") or f"{file_key}:{line_no}"
                native = f"codex:{session_id}:{record_id}:tool-output"
                candidate = _candidate_for_text_block(
                    text=output,
                    role="tool",
                    kind="tool_result",
                    source_type="external",
                    native_event_id=native,
                    occurred_at=timestamp,
                    session_id=session_id,
                    project=project,
                    harness="codex",
                    metadata={
                        "source_path": source_path,
                        "record_type": record_type,
                        "origin": "codex-rollout",
                        "template": "tool_result",
                        "payload_type": item_type,
                    },
                )
                yield candidate, None if candidate else "empty_or_oversize"
            else:
                yield None, "unsupported:empty_tool_output"
            return
    if record_type == "event_msg" and isinstance(payload, dict):
        if has_response_items:
            return
        event_type = str(payload.get("type") or "")
        role = ""
        if event_type == "user_message":
            role = "user"
        elif event_type == "agent_message":
            role = "assistant"
        if not role:
            return
        text = _payload_text(payload, ("message", "text", "content"))
        if not text:
            yield None, "unsupported:empty_message"
            return
        record_id = payload.get("id") or f"{file_key}:{line_no}"
        native = f"codex:{session_id}:{record_id}:event-msg"
        candidate = _candidate_for_text_block(
            text=text,
            role=role,
            kind="text",
            source_type="user_dialogue" if role == "user" else "agent_inference",
            native_event_id=native,
            occurred_at=timestamp,
            session_id=session_id,
            project=project,
            harness="codex",
            metadata={
                "source_path": source_path,
                "record_type": record_type,
                "origin": "codex-rollout-event-msg",
                "template": role,
                "payload_type": event_type,
            },
        )
        yield candidate, None if candidate else "empty_or_oversize"


_DSH_TRUNCATION_SUFFIX = "\n[bmem truncated]"
_DSH_USER_SOURCE_KINDS = {"user", "user-rpc", ""}


def _dsh_seq_token(seq: Any) -> str:
    """Canonical seq segment of the shared DSH identity (plugin and importer)."""

    if isinstance(seq, bool):
        return "-"
    if isinstance(seq, int):
        return str(seq)
    if isinstance(seq, float) and seq.is_integer() and abs(seq) < 2 ** 53:
        return str(int(seq))
    if isinstance(seq, str) and seq.strip():
        return seq.strip()
    return "-"


def _dsh_message_token(message_id: Any, body: str) -> str:
    """Message segment: the durable message id, else a body digest."""

    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    return f"body:{_sha256(body)[:16]}"


def _dsh_native_id(session_id: str, seq: Any, message_id: Any, body: str) -> str:
    return f"dsh:{session_id}:{_dsh_seq_token(seq)}:{_dsh_message_token(message_id, body)}"


def _dsh_message_body(content: Any) -> tuple[str, str]:
    """Whole-message body and block kind, identical to the native plugin.

    The plugin aggregates a message's durable text blocks into one event; the
    importer must do the same, otherwise a re-import writes per-block rows whose
    ids never match the live rows and every live event is duplicated.
    """

    parts = list(_content_parts(content))
    if not parts:
        return "", "text"
    kind = "tool_result" if any(block_kind == "tool_result" for _text, block_kind in parts) else "text"
    body = "\n".join(text for text, _block_kind in parts).strip()
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - len(_DSH_TRUNCATION_SUFFIX)] + _DSH_TRUNCATION_SUFFIX
    return body, kind


def _dsh_candidates(
    records: list[tuple[int, Any, str | None]],
    *,
    file_key: str,
    source_path: str,
    fallback_time: datetime,
    project_default: str = "",
    session_hint: str | None = None,
    project_hint: str | None = None,
) -> Iterator[tuple[dict[str, Any] | None, str | None]]:
    header_id = session_hint or Path(source_path).stem
    project = project_hint or project_default
    header: dict[str, Any] | None = None
    for line_no, record, error in records:
        if error:
            # Streaming read faults stay structural; malformed lines are skips.
            yield None, error if error.startswith("read_error:") else "json_decode"
            continue
        if not isinstance(record, dict):
            yield None, "non_object"
            continue
        if header is None and record.get("type") == "session":
            header = record
            sid = record.get("id")
            if isinstance(sid, str) and sid.strip():
                header_id = sid
            cwd = record.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                project = cwd
            yield None, "metadata"
            continue
        event_type = str(record.get("type") or "")
        data = record.get("data")
        if not isinstance(data, dict):
            if event_type == "session":
                continue
            yield None, "skip:no_data"
            continue
        occurred = record.get("time") or record.get("timestamp") or fallback_time
        seq = record.get("seq")
        message: dict[str, Any] | None = None
        if event_type == "user/message":
            message = data
            role = "user"
            source_kind = str((data.get("source") or {}).get("kind") or "") if isinstance(data.get("source"), dict) else ""
            if source_kind not in _DSH_USER_SOURCE_KINDS:
                yield None, "skip:non_user_source"
                continue
        elif event_type == "assistant/message":
            message = data.get("message")
            role = "assistant"
        elif event_type == "tool/result":
            message = data.get("message")
            role = "tool"
        else:
            continue
        if not isinstance(message, dict):
            yield None, "skip:no_message"
            continue
        message_id = message.get("id")
        source_kind = str((message.get("source") or {}).get("kind") or "") if isinstance(message.get("source"), dict) else ""
        if role == "user" and source_kind not in _DSH_USER_SOURCE_KINDS:
            yield None, "skip:non_user_source"
            continue
        if role == "assistant" and source_kind not in {"model", ""}:
            yield None, "skip:non_model_source"
            continue
        if role == "tool" and source_kind not in {"tool", ""}:
            yield None, "skip:non_tool_source"
            continue
        if role == "tool":
            source_type = "external"
            template = "tool_result"
        elif role == "user":
            source_type = "user_dialogue"
            template = "user"
        else:
            source_type = "agent_inference"
            template = "assistant"
        # One whole-message row per durable session event, with the same
        # canonical identity the native plugin posts live.
        body, block_kind = _dsh_message_body(message.get("content"))
        if not body:
            yield None, "empty_or_oversize"
            continue
        native = _dsh_native_id(header_id, seq, message_id, body)
        candidate = _make_candidate(
            body=body,
            source_type=source_type,
            native_event_id=native,
            operation_id=native,
            occurred_at=_iso_time(occurred, fallback=fallback_time),
            session_id=header_id,
            project=project,
            harness="dsh",
            metadata={
                "source_path": source_path,
                "record_type": event_type,
                "origin": "dsh-jsonl",
                "template": template,
                "role": role,
                "block_kind": block_kind,
                "session_id": header_id,
                "turn_id": str(data.get("turn") or ""),
                "seq": _dsh_seq_token(seq),
                "message_id": _dsh_message_token(message_id, body),
                "truncated": body.endswith("[bmem truncated]"),
            },
        )
        yield candidate, None if candidate else "empty_or_oversize"



def _candidates_for_record(
    harness: str,
    record: dict[str, Any],
    *,
    file_key: str,
    line_no: int,
    fallback_time: datetime,
    project_default: str = "",
    source_path: str,
    session_hint: str | None = None,
    project_hint: str | None = None,
) -> Iterator[dict[str, Any] | None]:
    """Adapter used by hook transcript deltas for one already-parsed record."""

    records = [(line_no, record, None)]
    if harness == "claude-code":
        generator: Iterable[tuple[dict[str, Any] | None, str | None]] = _claude_candidates(
            records,
            file_key=file_key,
            source_path=source_path,
            fallback_time=fallback_time,
            project_default=project_default,
            session_hint=session_hint,
            project_hint=project_hint,
        )
    elif harness == "codex":
        generator = _codex_candidates(
            records,
            file_key=file_key,
            source_path=source_path,
            fallback_time=fallback_time,
            project_default=project_default,
            session_hint=session_hint,
            project_hint=project_hint,
        )
    elif harness == "dsh":
        generator = _dsh_candidates(
            records,
            file_key=file_key,
            source_path=source_path,
            fallback_time=fallback_time,
            project_default=project_default,
            session_hint=session_hint,
            project_hint=project_hint,
        )
    else:
        return iter(())
    return (candidate for candidate, _error in generator)


# ---------------------------------------------------------------------------
# History import: markdown
# ---------------------------------------------------------------------------


def _markdown_chunks(text: str, limit: int = MAX_BODY) -> Iterator[tuple[int, str, str]]:
    """Yield ``(index, heading, body)`` chunks bounded by the event body limit."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match and body and "\n".join(body).strip():
            sections.append((heading, "\n".join(body).strip()))
            heading = match.group(2).strip()
            body = [line]
        else:
            if match and not heading:
                heading = match.group(2).strip()
            body.append(line)
    if body and "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))
    if not sections:
        sections = [("", text.strip())]

    index = 0
    for section_heading, section_body in sections:
        if len(section_body) <= limit:
            if section_body:
                yield index, section_heading, section_body
                index += 1
            continue
        lines = section_body.splitlines()
        current: list[str] = []
        size = 0
        part = 0
        for line in lines:
            if current and size + len(line) + 1 > limit:
                yield index, f"{section_heading} (part {part + 1})", "\n".join(current)
                index += 1
                part += 1
                current = []
                size = 0
            current.append(line)
            size += len(line) + 1
        if current:
            yield index, f"{section_heading} (part {part + 1})" if part else section_heading, "\n".join(current)
            index += 1


_FRONT_MATTER_RE = re.compile(r"^(sensitivity|status)\s*[:=]\s*([^\s#]+)", re.IGNORECASE)


def _markdown_front_matter(text: str) -> tuple[str, str, str, str]:
    """Split a Markdown document into front matter, sensitivity, status and body.

    Only a leading ``---`` YAML block is treated as front matter.  The
    ``sensitivity: private`` value is preserved explicitly and the front matter
    is excluded from the memory body; anything else defaults to ``normal``.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    if not normalized.startswith("---\n"):
        return "", "normal", "active", normalized
    end = normalized.find("\n---", 4)
    if end < 0:
        return "", "normal", "active", normalized
    closing = normalized.find("\n", end + 1)
    block = normalized[4:end]
    body = normalized[closing + 1:] if closing >= 0 else ""
    sensitivity = "normal"
    status = "active"
    for line in block.splitlines():
        match = _FRONT_MATTER_RE.match(line.strip())
        if match:
            value = match.group(2).strip().strip("\"'").lower()
            if match.group(1).lower() == "status":
                status = value
            else:
                sensitivity = "private" if value == "private" else "normal"
    return block, sensitivity, status, body


def _markdown_candidates(
    text: str,
    *,
    harness: str,
    source_path: str,
    file_key: str,
    fallback_time: datetime,
    sensitivity: str = "normal",
) -> Iterator[tuple[dict[str, Any] | None, str | None]]:
    for index, heading, body in _markdown_chunks(text):
        if not body:
            continue
        body_hash = _sha256(body)[:16]
        fact_key = f"import:{harness}:{file_key}:{index}"
        native = f"md:{harness}:{file_key}:{index}:{body_hash}"
        candidate = _make_candidate(
            body=body,
            source_type="user_declaration",
            native_event_id=native,
            operation_id=native,
            occurred_at=_iso_time(fallback_time),
            session_id=f"import:{harness}",
            project="",
            harness=harness,
            metadata={
                "source_path": source_path,
                "origin": "markdown-memory",
                "section_index": index,
                "heading": heading,
                "file_key": file_key,
                "template": "markdown_memory",
                "sensitivity": "private" if sensitivity == "private" else "normal",
            },
            mode="declare",
            kind="fact",
            fact_key=fact_key,
            sensitivity=sensitivity,
        )
        yield candidate, None if candidate else "empty_or_oversize"


# ---------------------------------------------------------------------------
# History import: execution
# ---------------------------------------------------------------------------


def _record_counts(stats: dict[str, Any], reason: str | None, imported: bool) -> None:
    if imported:
        stats["imported"] += 1
    else:
        stats["skipped"] += 1
        if reason:
            stats["skipped_reasons"][reason] = stats["skipped_reasons"].get(reason, 0) + 1


def _import_candidate(store: "Store", candidate: dict[str, Any], stats: dict[str, Any]) -> bool:
    mode = candidate.get("mode", "event")
    data = {key: value for key, value in candidate.items() if key != "mode"}
    data["metadata"] = {**(data.get("metadata") or {}), "history_import": True}
    try:
        result = store.declare(data) if mode == "declare" else store.ingest(data)
    except Exception as exc:
        stats["errors"] += 1
        stats["skipped"] += 1
        reason = f"store_error:{type(exc).__name__}"
        stats["skipped_reasons"][reason] = stats["skipped_reasons"].get(reason, 0) + 1
        stats["error_details"].append(_safe_error(exc))
        return False
    duplicate = bool(result.get("duplicate")) if isinstance(result, dict) else False
    if duplicate:
        stats["skipped"] += 1
        stats["skipped_reasons"]["duplicate"] = stats["skipped_reasons"].get("duplicate", 0) + 1
        return False
    stats["imported"] += 1
    return True


def _cursor_for_path(store: "Store", source: str, path: Path) -> dict[str, Any]:
    """Return the stored cursor for one import source.

    The row-level ``error`` and ``parser_version`` travel with it so a caller can
    tell a genuinely unchanged file apart from one whose last attempt failed.
    ``Store.get_import_cursor(source, path)`` is an exact lookup, so a large
    import set never loses a cursor to a truncated ``records`` page.
    """

    absolute = str(path)
    row: Any = None
    getter = getattr(store, "get_import_cursor", None)
    if callable(getter):
        try:
            row = getter(source, absolute)
        except Exception:
            row = None
    if not isinstance(row, dict):
        # Compatibility fallback for stores without the exact lookup.
        try:
            page = store.records("import_cursors", limit=1000)
        except Exception:
            return {}
        if isinstance(page, dict):
            page = page.get("items") or page.get("records") or []
        if not isinstance(page, list):
            return {}
        exact_source = [
            item for item in page
            if isinstance(item, dict) and item.get("source") == source and item.get("path") == absolute
        ]
        same_path = [
            item for item in page
            if isinstance(item, dict) and item.get("path") == absolute
        ]
        row = (exact_source or same_path or [None])[0]

    if not isinstance(row, dict):
        return {}
    cursor = row.get("last_cursor")
    if isinstance(cursor, str):
        try:
            cursor = json.loads(cursor)
        except json.JSONDecodeError:
            cursor = None
    if not isinstance(cursor, dict):
        return {}
    merged = dict(cursor)
    # The stored error lives on the cursor row, not inside last_cursor.
    if row.get("error"):
        merged.setdefault("error", str(row.get("error")))
    if row.get("parser_version"):
        merged.setdefault("parser_version", str(row.get("parser_version")))
    return merged


def _persist_cursor(
    store: "Store",
    *,
    source: str,
    path: Path,
    stats: dict[str, Any],
    cursor: dict[str, Any],
    error: str | None,
) -> None:
    values = {
        "path": str(path),
        "parser_version": PARSER_VERSION,
        "discovered": stats["discovered"],
        "imported": stats["imported"],
        "skipped": stats["skipped"],
        "last_cursor": json.dumps(cursor, sort_keys=True),
        "error": error,
        "updated_at": _now_iso(),
        "skipped_reasons": stats["skipped_reasons"],
    }
    try:
        store.record_import(source, values)
    except Exception as exc:
        stats["errors"] += 1
        stats["error_details"].append(f"cursor:{_safe_error(exc)}")


def _empty_stats() -> dict[str, Any]:
    return {
        "discovered": 0,
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "skipped_reasons": {},
        "error_details": [],
    }


def _import_file(store: "Store", path: Path, harness: str) -> dict[str, Any]:
    source = f"{harness}:{path}"
    file_key = _path_key(path)
    stats = _empty_stats()
    result: dict[str, Any] = {
        "path": str(path),
        "harness": harness,
        "source": source,
        "parser_version": PARSER_VERSION,
        "discovered": 0,
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "skipped_reasons": {},
        "unchanged": False,
        "error": None,
    }
    try:
        stat = path.stat()
    except OSError as exc:
        result["error"] = _safe_error(exc)
        result["errors"] = 1
        _persist_cursor(store, source=source, path=path, stats=stats, cursor={}, error=result["error"])
        return result

    cursor = _cursor_for_path(store, source, path)
    if (
        cursor.get("size") == stat.st_size
        and cursor.get("mtime_ns") == stat.st_mtime_ns
        and cursor.get("parser_version") == PARSER_VERSION
        and not cursor.get("error")
    ):
        # Nothing changed *and* the last attempt finished cleanly.  A stored
        # error (for example a file that was unreadable at its recorded size and
        # mtime, then only had its permissions repaired) or a cursor written by
        # an older parser must re-read the file instead of fast-pathing.
        result["unchanged"] = True
        result["cursor"] = cursor
        return result

    suffix = path.suffix.lower()
    line_count = 0
    scan: _JsonlScan | None = None
    try:
        if suffix == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            _front_matter, sensitivity, status, body_text = _markdown_front_matter(text)
            iterator: Iterable[tuple[dict[str, Any] | None, str | None]] = _markdown_candidates(
                body_text,
                harness=harness,
                source_path=str(path),
                file_key=file_key,
                fallback_time=_file_mtime(path),
                sensitivity=sensitivity,
            )
            if status in {"superseded", "expired"}:
                iterator = [(None, "inactive_memory")]
            # Body lines exclude front matter; the declared sensitivity is
            # preserved on every emitted declaration.
            line_count = len(body_text.splitlines())
        else:
            # Stream the transcript: records are parsed one line at a time and
            # only counts survive, so multi-hundred-MB rollouts import in
            # bounded memory.  A decompression/iteration failure surfaces here
            # through the scan's read_error record and lands in the cursor.
            scan = _read_jsonl_records(path, stream=True)
            if harness not in _HARNESSES:
                raise RuntimeError(f"no parser for harness {harness}")
            parser = {
                "claude-code": _claude_candidates,
                "codex": _codex_candidates,
                "dsh": _dsh_candidates,
            }[harness]
            iterator = parser(
                scan,
                file_key=file_key,
                source_path=str(path),
                fallback_time=_file_mtime(path),
            )
    except Exception as exc:
        result["error"] = _safe_error(exc)
        result["errors"] = 1
        result["skipped_reasons"]["file_error"] = 1
        cursor = {
            "version": 1,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "updated_at": _now_iso(),
            "error": result["error"],
        }
        _persist_cursor(store, source=source, path=path, stats=stats, cursor=cursor, error=result["error"])
        result["cursor"] = cursor
        return result

    try:
        for candidate, reason in iterator:
            if reason == "metadata":
                continue
            if candidate is None and reason and reason.startswith("read_error:"):
                # Permission loss / truncated stream: a file failure, not a
                # skipped record.  The cursor keeps the error so the unchanged
                # fast path does not mask a later repaired file.
                stats["errors"] += 1
                stats["error_details"].append(reason)
                continue
            stats["discovered"] += 1
            if candidate is None:
                if reason:
                    stats["skipped"] += 1
                    stats["skipped_reasons"][reason] = stats["skipped_reasons"].get(reason, 0) + 1
                continue
            _import_candidate(store, candidate, stats)
    except Exception as exc:
        # A decompression/iteration fault mid-stream is reported like a file
        # error: counters so far are kept and the cursor records the failure.
        result["error"] = _safe_error(exc)
        result["errors"] += 1
        cursor = {
            "version": 1,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "updated_at": _now_iso(),
            "error": result["error"],
        }
        _persist_cursor(store, source=source, path=path, stats=stats, cursor=cursor, error=result["error"])
        result["cursor"] = cursor
        return result

    if scan is not None:
        # The scan records the last physical line it read, so the count needs no
        # extra replay and no records are retained.  The digest is streamed in
        # chunks and only for small files.
        try:
            line_count = scan.count()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = _safe_error(exc)
        chunk_sha = _sha256_file(path, limit=64 * 1024 * 1024)
    else:
        chunk_sha = None
    cursor = {
        "version": 2,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": chunk_sha,
        "line_count": line_count,
        "byte_offset": stat.st_size,
        "parser_version": PARSER_VERSION,
        "updated_at": _now_iso(),
    }
    error_detail = "; ".join(stats["error_details"][:8]) or None
    _persist_cursor(store, source=source, path=path, stats=stats, cursor=cursor, error=error_detail)
    result.update({
        "discovered": stats["discovered"],
        "imported": stats["imported"],
        "skipped": stats["skipped"],
        "errors": stats["errors"],
        "skipped_reasons": stats["skipped_reasons"],
        "cursor": cursor,
        "error": error_detail,
    })
    return result


def import_history(
    store: "Store",
    harness: str = "all",
    paths: list[str] | None = None,
    config_root: Path | None = None,
) -> dict:
    """Import Claude/Codex/DSH history and Markdown memory under config_root.

    Returns real discovered/imported/skipped counts plus per-file cursors and
    errors.  A damaged file is reported independently and never aborts the rest.
    """

    normalized = _normalize_harness(harness)
    root = _config_root(config_root)
    app_server: dict[str, Any] | None = None

    if paths:
        selected, path_errors = _expand_paths(root, paths, normalized)
    else:
        selected = []
        path_errors = []
        if normalized in ("all", "codex"):
            app_server = _codex_app_server_import(store, root)
        for item in _discover_for_harness(root, normalized):
            detected = normalized if normalized != "all" else (_detect_harness(item) or "manual")
            selected.append((item, detected))

    # The app-server is the preferred Codex history protocol when usable.
    if app_server and app_server.get("usable") and app_server.get("threads", 0) > 0:
        selected = [
            (path, item_harness)
            for path, item_harness in selected
            if not (
                item_harness == "codex"
                and path.name.lower().endswith(".jsonl")
            )
        ]

    seen: set[str] = set()
    files: list[dict[str, Any]] = list(path_errors)
    for path, item_harness in selected:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if item_harness not in _HARNESSES and item_harness != "manual":
            files.append({
                "path": str(path),
                "harness": item_harness,
                "error": "unsupported harness for this path",
                "discovered": 0,
                "imported": 0,
                "skipped": 0,
            })
            continue
        files.append(_import_file(store, path, item_harness))

    source_rows: list[dict[str, Any]] = list(files)
    app_row: dict[str, Any] | None = None
    if app_server is not None:
        details = [str(item) for item in (app_server.get("error_details") or [])]
        app_row = {
            "path": str(app_server.get("path") or "codex:app-server"),
            "harness": "codex",
            "source": str(app_server.get("source") or "codex:app-server"),
            "protocol": "codex-app-server",
            "usable": bool(app_server.get("usable")),
            "threads": int(app_server.get("threads") or 0),
            "discovered": int(app_server.get("discovered") or 0),
            "imported": int(app_server.get("imported") or 0),
            "skipped": int(app_server.get("skipped") or 0),
            "errors": int(app_server.get("errors") or 0),
            "skipped_reasons": app_server.get("skipped_reasons") or {},
            "error": app_server.get("reason") or ("; ".join(details[:8]) or None),
            "cursor": app_server.get("cursor") or {},
            "unchanged": False,
        }
        source_rows.append(app_row)

    totals = {
        key: sum(int(item.get(key) or 0) for item in source_rows)
        for key in ("discovered", "imported", "skipped", "errors")
    }
    result: dict[str, Any] = {
        "harness": normalized,
        "config_root": str(root),
        "parser_version": PARSER_VERSION,
        "files": files,
        "sources": source_rows,
        **totals,
    }
    if app_server is not None:
        result["codex_app_server"] = app_server
        result["history_protocol"] = (
            "codex-app-server"
            if app_server.get("usable") and app_server.get("threads", 0) > 0
            else "jsonl-fallback"
        )
    else:
        result["history_protocol"] = "jsonl"
    if path_errors:
        result["errors"] += 0
    return result


def _excluded_path(path: Path, excluded: Iterable[str]) -> bool:
    """True when ``path`` equals or lives under an explicitly excluded path."""

    candidates: list[str] = []
    for entry in excluded:
        raw = str(entry or "").strip()
        if not raw:
            continue
        for variant in (raw, str(Path(raw).expanduser())):
            try:
                candidates.append(os.path.realpath(variant))
            except OSError:  # pragma: no cover - defensive
                candidates.append(variant)
    if not candidates:
        return False
    try:
        target = os.path.realpath(str(path))
    except OSError:  # pragma: no cover - defensive
        target = str(path)
    for excluded_path in candidates:
        if target == excluded_path or target.startswith(excluded_path.rstrip(os.sep) + os.sep):
            return True
    return False


def sync_history(
    store: "Store",
    config_root: Path | None = None,
    *,
    exclude_paths: list[str] | None = None,
) -> dict:
    """Periodic history top-up over already discovered native files.

    Discovery first enumerates the Claude/Codex/DSH transcripts and Markdown
    memory that exist on disk and then calls :func:`import_history` with those
    explicit paths.  Explicit paths mean:

    * no Codex app-server is launched, so a periodic scan cannot spawn CLIs or
      re-read every remote thread;
    * unchanged files are skipped by the cursor fast path, so a rescan only
      reads what actually changed.

    ``exclude_paths`` is for known isolated test/sandbox directories only; user
    histories are never suppressed broadly.  When ``<store.home>/history-sync.json``
    exists its ``exclude_paths`` and ``additional_paths`` lists are merged in:
    excluded paths are added to the explicit ``exclude_paths`` argument (which
    is never overridden), and additional files/directories are expanded next to
    the discovered set, deduplicated, then all of them pass the same exclusion
    check.  With nothing discovered the result reports honest zero counts
    instead of a fabricated import.
    """

    root = _config_root(config_root)
    discovered = _discover_for_harness(root, "all")
    excluded = list(exclude_paths or [])
    additional: list[str] = []
    config_path = Path(store.home) / "history-sync.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            config = None
            config_error = _safe_error(exc)
        else:
            config_error = None
            config = config if isinstance(config, dict) else {}
            for name, target in (("exclude_paths", excluded), ("additional_paths", additional)):
                value = config.get(name)
                if isinstance(value, list):
                    target.extend(str(item) for item in value if isinstance(item, (str, Path)))
    else:
        config_error = None

    if additional:
        extra_selected, extra_errors = _expand_paths(root, additional, "all")
    else:
        extra_selected, extra_errors = [], []
    candidates = [
        (path, _detect_harness(path) or "manual")
        for path in discovered
    ] + extra_selected
    wanted: list[str] = []
    selected: list[tuple[Path, str]] = []
    skipped_excluded: list[str] = []
    seen: set[str] = set()
    for path, harness in candidates:
        if _excluded_path(path, excluded):
            skipped_excluded.append(str(path))
            continue
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        selected.append((path, harness))
        wanted.append(str(path))

    if not selected:
        empty = _empty_stats()
        return {
            "harness": "all",
            "config_root": str(root),
            "parser_version": PARSER_VERSION,
            "files": list(extra_errors),
            "sources": [],
            "history_protocol": "discovered-paths",
            "discovered_paths": 0,
            "excluded_paths": skipped_excluded,
            "unchanged_files": 0,
            "errors": len(extra_errors),
            "sync_config_error": config_error,
            **empty,
        }

    result = import_history(store, harness="all", paths=wanted, config_root=root)
    files = [item for item in (result.get("files") or []) if isinstance(item, dict)]
    result["files"] = files
    result["excluded_paths"] = skipped_excluded
    result["discovered_paths"] = len(files)
    result["unchanged_files"] = sum(1 for item in files if item.get("unchanged"))
    result["sync_config_error"] = config_error
    result["history_protocol"] = "discovered-paths"
    # import_history computed totals over its own rows; recompute so excluded
    # paths and any non-dict rows cannot inflate the reported coverage.
    for key in ("discovered", "imported", "skipped", "errors"):
        result[key] = sum(int(item.get(key) or 0) for item in files)
    result["sources"] = list(result.get("sources") or [])
    result.pop("codex_app_server", None)
    return result


# ---------------------------------------------------------------------------
# Codex app-server history protocol
# ---------------------------------------------------------------------------


def _rpc_send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
    process.stdin.flush()  # type: ignore[union-attr]


def _rpc_read(process: subprocess.Popen[str], request_id: int, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"codex app-server did not answer request {request_id}")
        readable, _, _ = select.select([process.stdout], [], [], remaining)  # type: ignore[arg-type]
        if not readable:
            raise TimeoutError(f"codex app-server did not answer request {request_id}")
        line = process.stdout.readline()  # type: ignore[union-attr]
        if not line:
            raise RuntimeError("codex app-server closed stdout")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") == request_id:
            return message


def _codex_app_server_import(store: "Store", root: Path) -> dict[str, Any]:
    """Try Codex's real app-server history protocol, falling back to JSONL."""

    if os.environ.get("BMEM_CODEX_APP_SERVER", "1").lower() in {"0", "false", "no"}:
        return {"usable": False, "reason": "disabled-by-BMEM_CODEX_APP_SERVER"}
    executable = shutil.which("codex")
    if executable is None:
        return {"usable": False, "reason": "codex-cli-not-found"}
    codex_home = _harness_home(root, "codex")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    # Cursor identity for this protocol source.  It is a key into import_cursors,
    # not a claimed on-disk transcript: config_root/codex home semantics stay
    # owned by the JSONL discovery pass.
    app_source = "codex:app-server"
    app_cursor_path = codex_home / "app-server"
    process: subprocess.Popen[str] | None = None
    threads: list[dict[str, Any]] = []
    errors: list[str] = []
    # One stats object owns every app-server counter: the same object is handed
    # to _import_candidate/_record_counts, folded into the overall import totals
    # and persisted as this source's cursor row.
    stats = _empty_stats()
    usable = False
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        _rpc_send(process, {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "bmem-import", "title": "bmem import", "version": "0.1.0"},
                "capabilities": None,
            },
        })
        init = _rpc_read(process, 1)
        if "result" not in init:
            raise RuntimeError("initialize failed")
        usable = True
        cursor: str | None = None
        request_id = 2
        for _page in range(10):
            params: dict[str, Any] = {"limit": 100, "sortKey": "updated_at", "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            _rpc_send(process, {"id": request_id, "method": "thread/list", "params": params})
            response = _rpc_read(process, request_id)
            request_id += 1
            result = response.get("result") if isinstance(response, dict) else None
            if not isinstance(result, dict):
                raise RuntimeError("thread/list failed")
            data = result.get("data")
            if isinstance(data, list):
                threads.extend(item for item in data if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        # Non-None means thread/list paging stopped at the page cap.
        list_cursor = cursor
        for thread in threads:
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            _rpc_send(process, {
                "id": request_id,
                "method": "thread/read",
                "params": {"threadId": thread_id, "includeTurns": True},
            })
            response = _rpc_read(process, request_id)
            request_id += 1
            if "result" not in response:
                errors.append(f"thread/read rejected for {thread_id}")
                continue
            loaded = response["result"].get("thread") if isinstance(response.get("result"), dict) else None
            if not isinstance(loaded, dict):
                errors.append(f"thread/read returned no thread for {thread_id}")
                continue
            project = str(loaded.get("cwd") or "")
            session_id = str(loaded.get("sessionId") or thread_id)
            turns = loaded.get("turns")
            if not isinstance(turns, list):
                turns = []
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                items = turn.get("items")
                if not isinstance(items, list):
                    continue
                turn_time = turn.get("startedAt")
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "")
                    item_id = str(item.get("id") or "")
                    if item_type == "userMessage":
                        for index, (text, block_kind) in enumerate(_content_parts(item.get("content"))):
                            if block_kind == "tool_result":
                                source_type = "external"
                                template = "tool_result"
                            else:
                                source_type = "user_dialogue"
                                template = "user"
                            native = f"codex-app:{session_id}:{item_id}:{index}"
                            candidate = _candidate_for_text_block(
                                text=text,
                                role="user",
                                kind=block_kind,
                                source_type=source_type,
                                native_event_id=native,
                                occurred_at=turn_time or loaded.get("updatedAt"),
                                session_id=session_id,
                                project=project,
                                harness="codex",
                                metadata={
                                    "source_path": "codex-app-server",
                                    "thread_id": thread_id,
                                    "turn_id": str(turn.get("id") or ""),
                                    "origin": "codex-app-server",
                                    "template": template,
                                },
                            )
                            stats["discovered"] += 1
                            if candidate is None:
                                _record_counts(stats, "empty_or_oversize", False)
                                continue
                            _import_candidate(store, candidate, stats)
                    elif item_type == "agentMessage":
                        text = item.get("text")
                        if not isinstance(text, str) or not text.strip():
                            continue
                        native = f"codex-app:{session_id}:{item_id}:assistant"
                        candidate = _candidate_for_text_block(
                            text=text,
                            role="assistant",
                            kind="text",
                            source_type="agent_inference",
                            native_event_id=native,
                            occurred_at=turn_time or loaded.get("updatedAt"),
                            session_id=session_id,
                            project=project,
                            harness="codex",
                            metadata={
                                "source_path": "codex-app-server",
                                "thread_id": thread_id,
                                "turn_id": str(turn.get("id") or ""),
                                "origin": "codex-app-server",
                                "template": "assistant",
                            },
                        )
                        stats["discovered"] += 1
                        if candidate is None:
                            _record_counts(stats, "empty_or_oversize", False)
                            continue
                        _import_candidate(store, candidate, stats)
                    elif item_type in {"functionCallOutput", "customToolCallOutput"}:
                        output = _tool_output_text(item.get("output"))
                        if not output:
                            continue
                        native = f"codex-app:{session_id}:{item_id}:tool-output"
                        candidate = _candidate_for_text_block(
                            text=output,
                            role="tool",
                            kind="tool_result",
                            source_type="external",
                            native_event_id=native,
                            occurred_at=turn_time or loaded.get("updatedAt"),
                            session_id=session_id,
                            project=project,
                            harness="codex",
                            metadata={
                                "source_path": "codex-app-server",
                                "thread_id": thread_id,
                                "turn_id": str(turn.get("id") or ""),
                                "origin": "codex-app-server",
                                "template": "tool_result",
                            },
                        )
                        stats["discovered"] += 1
                        if candidate is None:
                            _record_counts(stats, "empty_or_oversize", False)
                            continue
                        _import_candidate(store, candidate, stats)
        error_detail = "; ".join(errors[:8]) or None
        for detail in errors:
            stats["error_details"].append(detail)
        app_cursor = {
            "version": 1,
            "protocol": "codex-app-server",
            "threads": len(threads),
            "list_cursor": list_cursor,
            "parser_version": PARSER_VERSION,
            "updated_at": _now_iso(),
        }
        _persist_cursor(
            store,
            source=app_source,
            path=app_cursor_path,
            stats=stats,
            cursor=app_cursor,
            error=error_detail,
        )
        return {
            "usable": usable,
            "threads": len(threads),
            "source": app_source,
            "path": str(app_cursor_path),
            "discovered": stats["discovered"],
            "imported": stats["imported"],
            "skipped": stats["skipped"],
            "errors": len(errors),
            "error_details": errors[:8],
            "skipped_reasons": stats["skipped_reasons"],
            "cursor": app_cursor,
            "fallback": "jsonl",
            "protocol": "codex app-server thread/list + thread/read(includeTurns)",
        }
    except Exception as exc:
        errors.append(_safe_error(exc))
        stats["error_details"].append(_safe_error(exc))
        return {
            "usable": False,
            "reason": _safe_error(exc),
            "threads": len(threads),
            "source": app_source,
            "path": str(app_cursor_path),
            "discovered": stats["discovered"],
            "imported": stats["imported"],
            "skipped": stats["skipped"],
            "errors": len(errors),
            "error_details": errors[:8],
            "skipped_reasons": stats["skipped_reasons"],
            "fallback": "jsonl",
        }
    finally:
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
