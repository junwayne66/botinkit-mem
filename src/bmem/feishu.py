"""Feishu/Lark connection manager: managed official CLI install, app setup,
authorization, read-scope checks and throttled history sync orchestration.

Credentials stay with the official CLI. This module persists only safe
selection/config/sync metadata in ``store.home/connections/feishu.json``;
authorization URL/expiry, device code and QR PNG stay in memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

BRANDS = ("feishu", "lark")
SOURCES = ("private_chats", "group_chats", "documents")
SENSITIVITIES = ("private", "normal")
CLI_PACKAGE = "@larksuite/cli@1.0.100"
SYNC_INTERVAL = 300.0
APP_TTL = 600.0
URL_HOSTS = ("feishu.cn", "larksuite.com")
CLR_AUTODETECT = (
    "OPENCLAW_CLI", "OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH",
    "OPENCLAW_SERVICE_MARKER", "OPENCLAW_SERVICE_VERSION", "OPENCLAW_GATEWAY_PORT",
    "OPENCLAW_SHELL", "HERMES_HOME", "HERMES_QUIET", "HERMES_EXEC_ASK",
    "HERMES_GATEWAY_TOKEN", "HERMES_SESSION_KEY", "LARK_CHANNEL",
)
URL_RE = re.compile(r"https?://[^\s'\"<>`]+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ConnectorError(ValueError):
    """Safe, actionable error: code plus bounded generic message and public details."""

    def __init__(self, code: str, message: str, *, missing: list[str] | None = None, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.message = message
        self.missing = list(missing or [])
        self.retryable = retryable

    def to_dto(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.missing:
            d["missing"] = self.missing
        return d


def normalize_brand(brand: str) -> str:
    value = str(brand or "").strip().lower()
    if value not in BRANDS:
        raise ConnectorError("invalid_brand", "brand must be feishu or lark")
    return value


def validate_sources(sources: Any) -> list[str]:
    if not isinstance(sources, (list, tuple)):
        raise ConnectorError("invalid_sources", "sources must be a list")
    out: list[str] = []
    for s in sources:
        if not isinstance(s, str) or s not in SOURCES:
            raise ConnectorError("invalid_sources", "invalid history source")
        if s not in out:
            out.append(s)
    return [s for s in SOURCES if s in out]


from .feishu_history import import_history, required_scopes


def _parse_json(text: str) -> Any:
    value = (text or "").strip()
    if not value:
        raise ValueError("empty")
    try:
        return json.loads(value)
    except ValueError:
        start = min((i for i in (value.find("{"), value.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        return json.loads(value[start:])


def _fingerprint(brand: str, app_id: str, open_id: str) -> str:
    raw = json.dumps([brand, app_id, open_id], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_url(text: str) -> str | None:
    """Return an untouched https URL only for Feishu/Lark hosts without userinfo."""
    m = URL_RE.search(_ANSI.sub("", text or ""))
    if not m:
        return None
    url = m.group(0)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or "@" in parts.netloc or not host:
        return None
    if not any(host == d or host.endswith("." + d) for d in URL_HOSTS):
        return None
    return url


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in CLR_AUTODETECT:
        env.pop(key, None)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def _valid_ver(text: str) -> str | None:
    for line in (text or "").splitlines():
        m = re.search(r"\d+\.\d+\.\d*", line)
        if m:
            return m.group(0)
    return None


def _candidate_bins() -> list[Path]:
    out: list[Path] = []
    wh = shutil.which("lark-cli")
    if wh:
        out.append(Path(wh))
    for rel in (".local/bin", ".npm-global/bin", ".hermes/node/bin"):
        out.append(Path.home() / rel / "lark-cli")
    out.extend(Path(p) / "lark-cli" for p in ("/opt/homebrew/bin", "/usr/local/bin"))
    return out


class ProcessOwner:
    """Owns every child process group started for one connector operation."""

    def __init__(self):
        self.cancelled = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def _signal(self, sig: int) -> None:
        with self._lock:
            processes = list(self._procs)
        for process in processes:
            try:
                os.killpg(process.pid, sig)
            except OSError:
                pass

    def terminate(self) -> None:
        self.cancelled.set()
        self._signal(signal.SIGTERM)


@dataclass
class CLIContext:
    """Mutable per-operation CLI session (binary, profile, identity)."""

    brand: str
    env: dict[str, str]
    owner: ProcessOwner
    exe: Path | None = None
    version: str | None = None
    profile: str = ""
    app_id: str = ""
    account: dict[str, Any] | None = None
    seen_account_id: str | None = None
    needs_config: bool = False

    def argv(self, args: Any) -> list[str]:
        if self.exe is None:
            raise ConnectorError("not_configured", "official CLI is not installed")
        return [str(self.exe), *args]

    def open(self) -> "CLIProcessRunner":
        return CLIProcessRunner(self.env, self.owner)


class CLIProcessRunner:
    def __init__(self, env: dict[str, str], owner: ProcessOwner, on_stderr: Callable[[str], None] | None = None):
        self.env, self.owner, self.on_stderr = env, owner, on_stderr

    def run(self, argv: list[str], *, timeout: float = 120.0, cwd: str | None = None) -> dict[str, Any]:
        """Run argv without a shell and collect complete stdout/stderr."""
        if self.owner.cancelled.is_set():
            raise ConnectorError("cancelled", "operation cancelled", retryable=True)
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env, cwd=cwd, start_new_session=True,
        )
        with self.owner._lock:
            cancelled = self.owner.cancelled.is_set()
            if not cancelled:
                self.owner._procs.add(process)
        if cancelled:
            process.kill()
        chunks = {"out": [], "err": []}
        received: queue.Queue = queue.Queue()

        def read_stream(name, stream):
            try:
                for line in stream:
                    received.put((name, line))
            finally:
                stream.close()
                received.put((name, None))

        readers = [threading.Thread(target=read_stream, args=(name, stream), daemon=True)
                   for name, stream in (("out", process.stdout), ("err", process.stderr))]
        for reader in readers:
            reader.start()
        remaining = len(readers)
        deadline = time.monotonic() + timeout
        try:
            while remaining:
                if self.owner.cancelled.is_set() or time.monotonic() > deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    cancelled = True
                try:
                    name, line = received.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    remaining -= 1
                else:
                    chunks[name].append(line)
                    if name == "err" and self.on_stderr is not None:
                        self.on_stderr(line)
            return_code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            for reader in readers:
                reader.join(timeout=1)
            with self.owner._lock:
                self.owner._procs.discard(process)
        return {"rc": return_code, "out": "".join(chunks["out"]), "err": "".join(chunks["err"]),
                "cancelled": cancelled or return_code < 0 or self.owner.cancelled.is_set()}


# ---------------------------------------------------------------- connector
class FeishuConnector:
    def __init__(self, store):
        self.store = store
        self._state_path = Path(store.home) / "connections" / "feishu.json"
        self._lock = threading.RLock()
        self._ops: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._last_tick: dict[str, float] = {}
        self._closed = False
        self._state = self._load_state()

    # ---- persistence (safe metadata only)
    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            raw = {}
        state: dict[str, Any] = {}
        if isinstance(raw, dict):
            for brand, stored in raw.items():
                if brand in BRANDS and isinstance(stored, dict):
                    state[brand] = self._sanitize(stored)
        return state

    def _sanitize(self, item: dict[str, Any]) -> dict[str, Any]:
        sources = [s for s in SOURCES if s in (item.get("sources") or [])]
        sens = item.get("sensitivity") if item.get("sensitivity") in SENSITIVITIES else "private"
        sync = item.get("sync") if isinstance(item.get("sync"), dict) else {}
        last = sync.get("last_success_at") if isinstance(sync.get("last_success_at"), str) else None
        return {
            "profile": str(item.get("profile") or ""),
            "app_id": str(item.get("app_id") or ""),
            "confirmed": bool(item.get("confirmed")),
            "sources": sources,
            "enabled": bool(item.get("enabled")),
            "sensitivity": sens,
            "account": None,
            "_info": {},
            "sync": {
                "imported": int(sync.get("imported") or 0),
                "skipped": int(sync.get("skipped") or 0),
                "errors": int(sync.get("errors") or 0),
                "last_success_at": last,
            },
        }

    def _empty_item(self) -> dict[str, Any]:
        return self._sanitize({})

    def _item(self, brand: str) -> dict[str, Any]:
        item = self._state.get(brand)
        if item is None:
            item = self._empty_item()
            self._state[brand] = item
        return item

    def _save(self) -> None:
        with self._lock:
            payload = json.dumps({brand: {key: value for key, value in item.items()
                                         if not key.startswith("_") and key != "account"}
                                  for brand, item in self._state.items()}, indent=2, sort_keys=True)
            path = self._state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(payload)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ---- DTO / public reads
    def _dto(self, brand: str) -> dict[str, Any]:
        with self._lock:
            item = self._item(brand)
            op = self._ops.get(brand)
            busy = op is not None
            sync = dict(item["sync"])
            if busy:
                sync.update(stage=(op or {}).get("stage") or "checking", imported=int((op or {}).get("imported") or 0),
                            skipped=int((op or {}).get("skipped") or 0))
            sync.setdefault("stage", "idle")
            auth = None
            if op is not None and op.get("url"):
                deadline = (op.get("url_at") or 0) + (op.get("ttl") or APP_TTL)
                if time.time() < deadline:
                    auth = {"url": op["url"], "expires_at": _iso(deadline)}
            info = item.get("_info") or {}
            return {
                "brand": brand,
                "phase": str(info.get("phase") or "unconfigured"),
                "installed": bool(info.get("installed")),
                "cli_version": info.get("cli_version"),
                "authenticated": bool(info.get("authenticated")),
                "account": dict(item["account"]) if item.get("account") else None,
                "history": {"confirmed": item["confirmed"], "sources": list(item["sources"]),
                            "enabled": item["enabled"], "sensitivity": item["sensitivity"]},
                "authorization": auth,
                "sync": {
                    "running": bool(busy and op.get("syncing")),
                    "stage": sync["stage"],
                    "imported": sync["imported"],
                    "skipped": sync["skipped"],
                    "errors": int(op.get("errors", sync["errors"])) if op else sync["errors"],
                    "last_success_at": sync["last_success_at"],
                },
                "error": dict(info["error"]) if info.get("error") else None,
                "busy": busy,
            }

    def _guard_brand(self, brand: str) -> str:
        return normalize_brand(brand)

    def status(self, brand: str) -> dict[str, Any]:
        brand = self._guard_brand(brand)
        with self._lock:
            item = self._item(brand)
            if not self._closed and item["profile"] and not item["_info"] and brand not in self._ops:
                operation = {"owner": ProcessOwner()}
                self._ops[brand] = operation
                self._start(brand, operation, {"kind": "inspect"})
            return self._dto(brand)

    def qrcode(self, brand: str) -> bytes | None:
        brand = self._guard_brand(brand)
        with self._lock:
            op = self._ops.get(brand)
            if op is None or not op.get("qr") or not op.get("url"):
                return None
            if time.time() >= (op.get("url_at") or 0) + (op.get("ttl") or APP_TTL):
                return None
            return bytes(op["qr"])

    # ---- state helpers (called with the lock held)
    def _set(self, brand: str, **changes: Any) -> None:
        self._item(brand).update(changes)

    def _info(self, brand: str, **changes: Any) -> None:
        with self._lock:
            self._item(brand).setdefault("_info", {}).update(changes)
            if brand in self._ops and "stage" in changes:
                self._ops[brand]["stage"] = changes["stage"]

    def _fail(self, brand: str, exc: ConnectorError) -> None:
        with self._lock:
            info = {"error": exc.to_dto(), "stage": "idle"}
            if exc.code in ("auth_failed", "expired", "not_authenticated", "app_setup_failed", "brand_mismatch", "missing_scopes"):
                info["authenticated"] = False
                self._item(brand)["account"] = None
            self._info(brand, **info)
            if exc.code not in ("sync_unavailable",):
                self._info(brand, phase="error")

    # ---- tool discovery and installation
    def _find_cli(self, context: CLIContext, allow_install: bool = True) -> None:
        managed = Path(self.store.home) / "runtime" / "lark-cli" / "node_modules" / ".bin" / "lark-cli"
        for path in [*_candidate_bins(), managed]:
            if not path.is_file() or not os.access(path, os.X_OK):
                continue
            previous_path = context.env.get("PATH", "")
            context.env["PATH"] = str(path.parent) + os.pathsep + previous_path
            probe = context.open().run([str(path), "--version"], timeout=30)
            ver = _valid_ver((probe["out"] or "") + "\n" + (probe["err"] or ""))
            if probe["rc"] == 0 and ver and not probe["cancelled"]:
                context.exe, context.version = path, ver
                self._info(context.brand, installed=True, cli_version=ver, stage="checking")
                return
            context.env["PATH"] = previous_path
        if allow_install:
            self._install_cli(context)
        else:
            raise ConnectorError("not_installed", "official CLI is unavailable; run connect to install it")

    def _install_cli(self, context: CLIContext) -> None:
        npm = shutil.which("npm")
        if not npm:
            raise ConnectorError("npm_missing", "Node.js/npm not found. Install Node.js (includes npm), then retry connect "
                                 "to install @larksuite/cli.")
        prefix = Path(self.store.home) / "runtime" / "lark-cli"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        self._info(context.brand, stage="installing", phase="installing", error=None)
        result = context.open().run(
            [npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund", CLI_PACKAGE], timeout=900)
        exe = prefix / "node_modules" / ".bin" / "lark-cli"
        if result["cancelled"]:
            raise ConnectorError("cancelled", "operation cancelled", retryable=True)
        if result["rc"] != 0 or not exe.is_file():
            raise ConnectorError("install_failed", "official CLI installation failed; check network and npm and retry",
                                 retryable=True)
        probe = context.open().run([str(exe), "--version"], timeout=30)
        ver = _valid_ver((probe["out"] or "") + "\n" + (probe["err"] or ""))
        if probe["rc"] != 0 or not ver:
            raise ConnectorError("install_failed", "installed CLI did not report a valid version")
        context.exe, context.version = exe, ver
        self._info(context.brand, installed=True, cli_version=ver, stage="checking")

    # ---- profile discovery / app setup
    def _profile_rows(self, context: CLIContext) -> list[dict[str, Any]]:
        result = context.open().run(context.argv(["profile", "list"]), timeout=60)
        try:
            rows = _parse_json(result["out"])
        except ValueError:
            rows = None
        if not isinstance(rows, list):
            raise ConnectorError("cli_error", "could not read CLI profiles; run `lark-cli auth login` manually and retry")
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]]

    def _discover(self, context: CLIContext) -> None:
        with self._lock:
            item = self._item(context.brand)
            saved_profile, saved_app = item["profile"], item["app_id"]
        rows = self._profile_rows(context)
        matching = [r for r in rows if str(r.get("brand") or "") == context.brand]
        chosen = next((r for r in matching if r["name"] == saved_profile
                       and str(r.get("appId") or "") == saved_app and saved_app), None)
        chosen = chosen or next((r for r in matching if r.get("active") or r.get("current")), None)
        chosen = chosen or (matching[0] if matching else None)
        if chosen is None:
            context.profile, context.app_id = "", ""
            context.needs_config = True
            with self._lock:
                self._set(context.brand, profile="", app_id="")
            return
        context.profile = chosen["name"]
        context.app_id = str(chosen.get("appId") or "")
        context.needs_config = False
        with self._lock:
            if (context.profile, context.app_id) != (saved_profile, saved_app):
                self._set(context.brand, profile=context.profile, app_id=context.app_id)
                self._save()

    def _setup_app(self, context: CLIContext) -> None:
        rows = self._profile_rows(context)
        suffix = 0
        while f"bmem-{context.brand}{'' if suffix == 0 else f'-{suffix}'}" in {r["name"] for r in rows}:
            suffix += 1
        name = f"bmem-{context.brand}{'' if suffix == 0 else f'-{suffix}'}"
        def on_stderr(line: str) -> None:
            url = _safe_url(line)
            if not url:
                return
            self._info(context.brand, phase="configuring", stage="configuring", error=None, authenticated=False)
            self._expose(context, url, APP_TTL)

        self._info(context.brand, phase="configuring", stage="configuring", error=None, authenticated=False)
        spawner = CLIProcessRunner(context.env, context.owner, on_stderr)
        result = spawner.run(context.argv(["config", "init", "--new", "--brand", context.brand, "--name", name]),
                             timeout=APP_TTL + 600)
        if result["cancelled"]:
            raise ConnectorError("cancelled", "app setup cancelled; retry connect", retryable=True)
        try:
            data = _parse_json(result["out"])
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("appId"):
            raise ConnectorError("app_setup_failed", "app creation did not complete; finish the official browser flow and retry connect")
        actual = str(data.get("brand") or context.brand)
        if actual != context.brand:
            raise ConnectorError("brand_mismatch", "created app belongs to a different brand; retry with the matching brand")
        with self._lock:
            self._set(context.brand, profile=name, app_id=str(data["appId"]))
            self._save()
        context.profile, context.app_id, context.needs_config = name, str(data["appId"]), False

    def _refresh_qr(self, context: CLIContext, url: str) -> None:
        tmp = tempfile.mkdtemp(prefix="bmem-feishu-qr-")
        try:
            spawner = CLIProcessRunner(context.env, context.owner)
            spawner.run(context.argv(["auth", "qrcode", url, "--output", "code.png"]), timeout=60, cwd=tmp)
            path = Path(tmp) / "code.png"
            data = path.read_bytes() if path.is_file() else None
        except (OSError, ConnectorError):
            data = None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if data:
            with self._lock:
                op = self._ops.get(context.brand)
                if op is not None and op.get("url") == url and op.get("qr") is None:
                    op["qr"] = data

    def _expose(self, context: CLIContext, url: str, ttl: float) -> None:
        url = _safe_url(url)
        if url is None:
            raise ConnectorError("auth_failed", "authorization returned an unsupported verification URL")
        with self._lock:
            operation = self._ops.get(context.brand)
            if operation is None or context.owner.cancelled.is_set():
                return
            if operation.get("url") == url:
                return
            operation.update(url=url, url_at=time.time(), ttl=ttl, qr=None)
        self._refresh_qr(context, url)

    # ---- authentication
    def _auth_status(self, context: CLIContext) -> bool:
        """Inspect current user identity; returns True when authenticated."""
        result = context.open().run(context.argv(["auth", "status", "--json",
                                                   "--profile", context.profile]), timeout=60)
        try:
            data = _parse_json(result["out"])
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return False
        if str(data.get("brand") or "") != context.brand:
            raise ConnectorError("brand_mismatch", "CLI profile is bound to a different brand; reconfigure this brand")
        app_id = str(data.get("appId") or context.app_id)
        identities = data.get("identities") if isinstance(data.get("identities"), dict) else {}
        user = identities.get("user") if isinstance(identities.get("user"), dict) else {}
        if not (app_id and user.get("available") is True and user.get("status") in ("ready", "needs_refresh")
                and str(user.get("openId") or "")):
            context.account = None
            return False
        open_id = str(user["openId"])
        account = {"id": _fingerprint(context.brand, app_id, open_id), "name": str(user.get("userName") or "")}
        if context.seen_account_id is not None and context.seen_account_id != account["id"]:
            with self._lock:
                self._item(context.brand)["sync"] = self._empty_item()["sync"]
                self._save()
        context.seen_account_id = account["id"]
        context.account, context.app_id = account, app_id
        with self._lock:
            self._set(context.brand, account=account)
            if context.app_id and self._item(context.brand)["app_id"] != context.app_id:
                self._set(context.brand, app_id=context.app_id)
                self._save()
        return True

    def _scope_problem(self, context: CLIContext, sources: list[str] | None = None) -> list[str]:
        with self._lock:
            item = self._item(context.brand)
            if sources is None:
                sources = list(item["sources"]) if item["confirmed"] else []
        scopes = " ".join(required_scopes(sources))
        result = context.open().run(context.argv(["auth", "check", "--scope", scopes, "--json",
                                                   "--profile", context.profile]), timeout=60)
        try:
            data = _parse_json(result["out"])
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True:
            return []
        missing = []
        if isinstance(data, dict) and isinstance(data.get("missing"), list):
            missing = sorted(str(m) for m in data["missing"] if isinstance(m, str) and m)
        return missing or ["offline_access"]

    def _auth_login(self, context: CLIContext) -> None:
        scopes = " ".join(required_scopes(self._confirmed_sources(context.brand)))
        result = context.open().run(context.argv(["auth", "login", "--scope", scopes, "--no-wait", "--json",
                                                   "--profile", context.profile]), timeout=120)
        try:
            data = _parse_json(result["out"])
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("verification_url") or not data.get("device_code"):
            raise ConnectorError("auth_failed", "could not start authorization; retry connect", retryable=True)
        try:
            ttl = max(1.0, float(data.get("expires_in") or APP_TTL))
        except (TypeError, ValueError):
            ttl = APP_TTL
        device_code = str(data["device_code"])
        with self._lock:
            self._info(context.brand, phase="authorizing", stage="authorizing", authenticated=False, error=None)
        self._expose(context, str(data["verification_url"]), ttl)
        poll = context.open().run(context.argv(["auth", "login", "--device-code", device_code, "--json",
                                                 "--profile", context.profile]), timeout=ttl + 60)
        with self._lock:
            operation = self._ops.get(context.brand)
            if operation is not None:
                operation.update(url=None, url_at=None, qr=None)
        if poll["cancelled"]:
            raise ConnectorError("cancelled", "authorization cancelled", retryable=True)
        try:
            body = _parse_json(poll["out"])
        except ValueError:
            body = None
        if poll["rc"] != 0 or not isinstance(body, dict):
            message = ("authorization request expired or was denied; retry connect to get a new QR code"
                       if poll["rc"] == 2 or (isinstance(body, dict) and not body.get("ok"))
                       else "authorization did not complete; retry connect")
            raise ConnectorError("expired", message, retryable=True)
        self._info(context.brand, stage="checking")
    # ---- operations body
    def _ensure_ready(self, context: CLIContext, authorize: bool) -> None:
        self._find_cli(context, allow_install=authorize)
        self._info(context.brand, stage="checking")
        self._discover(context)
        if context.needs_config:
            if not authorize:
                raise ConnectorError("not_configured", "official application is missing; run connect")
            self._setup_app(context)
        if not self._auth_status(context):
            if not authorize:
                raise ConnectorError("not_authenticated", "user authorization is required; run connect")
            self._auth_login(context)
            if not self._auth_status(context):
                raise ConnectorError("auth_failed", "authorization did not produce a usable user account; retry connect",
                                     retryable=True)
        missing = self._scope_problem(context)
        if missing:
            if not authorize:
                raise ConnectorError("missing_scopes", "read permissions are missing; run connect", missing=missing)
            self._auth_login(context)
            if not self._auth_status(context):
                raise ConnectorError("not_authenticated", "authorization did not produce a usable user account; retry connect",
                                     retryable=True)
            missing = self._scope_problem(context)
            if missing:
                raise ConnectorError("missing_scopes", "authorization is missing read permissions; retry connect",
                                     missing=missing, retryable=True)
        with self._lock:
            item = self._item(context.brand)
            self._info(context.brand, phase="connected", authenticated=True, stage="idle", error=None, account=dict(context.account))
            if context.account and item["account"] != context.account:
                self._set(context.brand, account=dict(context.account))
                self._save()
        self._last_tick[context.brand] = time.monotonic()

    def _confirmed_sources(self, brand: str) -> list[str]:
        with self._lock:
            item = self._item(brand)
            return list(item["sources"]) if item["confirmed"] else []

    def _run_cli(self, context: CLIContext, args: list[str]) -> Any:
        result = context.open().run(context.argv([*args, "--profile", context.profile]), timeout=300)
        try:
            data = _parse_json(result["out"])
        except ValueError:
            data = None
        if result["cancelled"]:
            raise ConnectorError("cancelled", "operation cancelled", retryable=True)
        if result["rc"] != 0 or data is None:
            raise ConnectorError("cli_error", "official CLI command failed; retry sync")
        return data

    def _sync_history(self, context: CLIContext, operation: dict[str, Any]) -> None:
        with self._lock:
            item = json.loads(json.dumps(self._item(context.brand)))
        if not (item["confirmed"] and item["enabled"] and item["sources"] and context.account):
            raise ConnectorError("sync_unavailable", "confirm and enable history sources before sync")
        with self._lock:
            operation.update(stage="running", syncing=True, imported=0, skipped=0, errors=0)
            self._info(context.brand, phase="syncing")
        sources, sensitivity, account_id = item["sources"], item["sensitivity"], context.account["id"]

        def progress(update: dict[str, Any]) -> None:
            with self._lock:
                operation.update(stage=str(update.get("stage") or "importing"),
                                 imported=int(update.get("imported") or 0),
                                 skipped=int(update.get("skipped") or 0),
                                 errors=int(update.get("errors") or 0))
        try:
            result = import_history(self.store, lambda args: self._run_cli(context, list(args)),
                                    context.brand, account_id, sources, progress=progress, sensitivity=sensitivity)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError("import_failed", f"history import failed: {type(exc).__name__}", retryable=True) from None
        imported, skipped = int(result.get("imported") or 0), int(result.get("skipped") or 0)
        errors = int(result.get("errors") or 0)
        with self._lock:
            operation.update(stage="idle", syncing=False, imported=imported, skipped=skipped, errors=errors)
            sync = self._item(context.brand)["sync"]
            sync["imported"] += imported
            sync["skipped"] += skipped
            sync["errors"] += errors
            if errors == 0:
                sync["last_success_at"] = _iso(time.time())
                self._save()
                self._info(context.brand, phase="connected", authenticated=True, error=None)
            else:
                self._save()
                self._info(context.brand, phase="connected",
                           error={"code": "import_errors", "message": "some history items failed; sync is retryable",
                                  "retryable": True})

    def _job(self, brand: str, operation: dict[str, Any], request: dict[str, Any]) -> None:
        owner: ProcessOwner = operation["owner"]
        context = CLIContext(brand, _child_env(), owner)
        with self._lock:
            previous_account = self._item(brand).get("account")
            context.seen_account_id = previous_account.get("id") if previous_account else None
        try:
            self._ensure_ready(context, authorize=request["kind"] == "connect")
            if request["kind"] == "sync" or request.get("sync"):
                self._sync_history(context, operation)
        except ConnectorError as exc:
            if not owner.cancelled.is_set():
                self._fail(brand, exc)
                if exc.code == "sync_unavailable":
                    with self._lock:
                        self._info(brand, authenticated=bool(context.account))
        except Exception as exc:  # never leak raw output
            if not owner.cancelled.is_set():
                self._fail(brand, ConnectorError("internal_error", f"operation failed: {type(exc).__name__}"))
        finally:
            with self._lock:
                self._ops.pop(brand, None)
                self._threads.pop(brand, None)

    # ---- public API
    def connect(self, brand: str, sources: Any = None, sensitivity: Any = None) -> dict[str, Any]:
        brand = self._guard_brand(brand)
        chosen = validate_sources(sources) if sources is not None else None
        if sensitivity is not None and sensitivity not in SENSITIVITIES:
            raise ConnectorError("invalid_sensitivity", "sensitivity must be private or normal")
        with self._lock:
            if self._closed:
                raise ConnectorError("closed", "connector is closed")
            if brand in self._ops:
                return self._dto(brand)
            item = self._item(brand)
            if chosen is not None or sensitivity is not None:
                if chosen is not None:
                    self._set(brand, sources=chosen, confirmed=True, enabled=bool(chosen))
                if sensitivity is not None:
                    self._set(brand, sensitivity=sensitivity)
                self._save()
            owner = ProcessOwner()
            operation = {"owner": owner}
            self._ops[brand] = operation
        request = {"kind": "connect", "sync": bool(item["confirmed"] and item["enabled"] and item["sources"])}
        self._start(brand, operation, request)
        return self._dto(brand)

    def configure(self, brand: str, sources: Any, enabled: bool = True, sensitivity: str = "private") -> dict[str, Any]:
        brand = self._guard_brand(brand)
        chosen = validate_sources(sources)
        if not isinstance(enabled, bool):
            raise ConnectorError("invalid_enabled", "enabled must be a boolean")
        if sensitivity not in SENSITIVITIES:
            raise ConnectorError("invalid_sensitivity", "sensitivity must be private or normal")
        with self._lock:
            if self._closed:
                raise ConnectorError("closed", "connector is closed")
            self._set(brand, sources=chosen, confirmed=True, enabled=enabled, sensitivity=sensitivity)
            self._save()
            return self._dto(brand)

    def sync(self, brand: str) -> dict[str, Any]:
        return self._start_sync(self._guard_brand(brand), require_selection=True)

    def _start_sync(self, brand: str, require_selection: bool) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ConnectorError("closed", "connector is closed")
            if brand in self._ops:
                return self._dto(brand)
            item = self._item(brand)
            if require_selection and not (item["enabled"] and item["confirmed"] and item["sources"]):
                raise ConnectorError("sync_unavailable", "confirm and enable history sources (connect or configure) before sync")
            owner = ProcessOwner()
            operation = {"owner": owner}
            self._ops[brand] = operation
        self._start(brand, operation, {"kind": "sync"})
        return self._dto(brand)

    def _start(self, brand: str, operation: dict[str, Any], request: dict[str, Any]) -> None:
        thread = threading.Thread(
            target=lambda: self._job(brand, operation, request), name=f"feishu-{brand}-{request['kind']}", daemon=True)
        with self._lock:
            self._threads[brand] = thread
        thread.start()

    def tick(self) -> list[str]:
        started: list[str] = []
        for brand in BRANDS:
            with self._lock:
                if self._closed or brand in self._ops:
                    continue
                item = self._item(brand)
                if not (item["confirmed"] and item["enabled"] and item["sources"]):
                    continue
                if item["profile"] and not item["_info"]:
                    self.status(brand)
                    continue
                if not (item.get("account") and (item.get("_info") or {}).get("authenticated")):
                    continue
                if time.monotonic() - self._last_tick.get(brand, 0.0) < SYNC_INTERVAL:
                    continue
                self._last_tick[brand] = time.monotonic()
                owner = ProcessOwner()
                operation = {"owner": owner}
                self._ops[brand] = operation
            try:
                self._start(brand, operation, {"kind": "sync"})
            except Exception:
                with self._lock:
                    self._ops.pop(brand, None)
                raise
            started.append(brand)
        return started

    def close(self) -> None:
        with self._lock:
            self._closed = True
            owners = [operation["owner"] for operation in self._ops.values()]
        for owner in owners:
            owner.terminate()
        with self._lock:
            threads = [thread for thread in self._threads.values() if thread is not threading.current_thread()]
        deadline = time.monotonic() + 3.0
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for owner in owners:
            owner._signal(signal.SIGKILL)
        with self._lock:
            self._ops.clear()
            self._threads.clear()
