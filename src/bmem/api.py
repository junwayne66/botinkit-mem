"""HTTP API, MCP Streamable HTTP endpoint, Worker lifecycle and static UI for bmem.

Implements the HTTP table and MCP tools from
specs/001-personal-memory/contracts/interfaces.md. The service always binds to
loopback; browser mutations are rejected unless their Origin is a localhost
origin. Store, Runtime and Worker are constructed once per app and imported
directly (all modules are implemented); their synchronous work is executed off
the event loop (sync ``def`` endpoints run in Starlette's threadpool; async
handlers use ``run_in_threadpool``). MCP tools default to the cloud target;
private content is only returned to an explicit local target.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from mcp.server.lowlevel import Server as McpServer
from mcp.server.streamable_http import TransportSecuritySettings
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.staticfiles import StaticFiles

import bmem as _bmem_pkg
from bmem import adapters
from bmem.models import (
    DeclarationInput,
    DomainError,
    EventInput,
    RecallInput,
    now_iso,
    redact,
)
from bmem.runtime import Runtime, Worker
from bmem.store import Store

SERVICE_NAME = "bmem"
DEFAULT_PORT = 8790
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
KINDS = {"episode", "fact", "decision", "method", "preference"}
STATUSES = {"active", "proposed", "quarantined", "superseded", "disputed", "retracted", "deleted"}
TARGETS = {"local", "cloud"}


class ConnectorSelection(BaseModel):
    sources: list[Literal["private_chats", "group_chats", "documents"]] | None = None
    sensitivity: Literal["normal", "private"] | None = None


class ConnectorConfig(BaseModel):
    sources: list[Literal["private_chats", "group_chats", "documents"]]
    enabled: bool = True
    sensitivity: Literal["normal", "private"] = "private"
RECORD_ENDPOINTS = {
    "arbitration": "arbitration",
    "injections": "injections",
    "imports": "import_cursors",
    "jobs": "jobs",
}
MAX_ERR_CHARS = 300
# Metadata allowed in a private save/declare acknowledgement for the cloud
# target: identifiers and lifecycle status only, never body or provenance.
_SAFE_MEMORY_KEYS = ("memory_id", "kind", "status", "sensitivity", "trust_tier")

_NONBLANK = {"minLength": 1, "pattern": "\\S"}
_TARGET_DESC = "Retrieval target. local includes private data, cloud excludes it."
_WRITE_TARGET_DESC = (
    "Response projection target. cloud (default) never echoes private content "
    "back; use local to get the full memory detail."
)


def default_home() -> Path:
    env = os.environ.get("BMEM_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".botinkit-mem"


def default_port() -> int:
    env = os.environ.get("BMEM_PORT", "").strip()
    try:
        port = int(env) if env else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _safe_message(exc: BaseException) -> str:
    """Short, sanitized error text. Never embed request bodies."""
    msg = str(exc).strip() or type(exc).__name__
    return redact(msg)[:MAX_ERR_CHARS]


def _host_of(host_header: str | None) -> str:
    """Normalize a Host header value to a lowercase hostname without port."""
    h = (host_header or "").strip()
    if h.startswith("["):  # bracketed IPv6 literal
        end = h.find("]")
        return h[1:end].lower() if end != -1 else ""
    if h.count(":") == 1:  # host:port
        h = h.rsplit(":", 1)[0]
    return h.lower()


def _loopback_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in LOOPBACK_HOSTS


class LoopbackGuardMiddleware:
    """Pure-ASGI guard: Host must be loopback and browser mutation Origin must be localhost.

    Implemented as raw ASGI middleware so long-lived SSE streams on /mcp are not
    wrapped by BaseHTTPMiddleware.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = _host_of(headers.get("host"))
        if host not in LOOPBACK_HOSTS:
            await JSONResponse({"detail": f"host not allowed: {host or '<missing>'}"},
                               status_code=400)(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if method in MUTATING_METHODS:
            origin = headers.get("origin")
            if origin and not _loopback_origin(origin.strip()):
                await JSONResponse({"detail": "origin not allowed for mutations"},
                                   status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _persisted_sensitivity(result: Any, fallback: str) -> str:
    """Sensitivity Store actually persisted for the resulting memory.

    An input declared ``normal`` can be stored as ``private`` when it updates a
    private memory or cites private ``derived_from`` evidence, so the cloud
    projection is decided from the persisted result. Falls back to ``fallback``
    (fail closed) when the result carries no usable sensitivity.
    """
    memory = result.get("memory") if isinstance(result, dict) else None
    if isinstance(memory, dict):
        value = memory.get("sensitivity")
        if isinstance(value, str) and value:
            return value
    return fallback


def _private_cloud_ack(result: dict) -> dict:
    """Project a private save/declare result for the default cloud target.

    Keeps only id/status metadata; drops body, conditions, revisions, sources
    and decisions so no private content is echoed.
    """
    memory = result.get("memory") or {}
    ack: dict[str, Any] = {
        "memory": {k: memory[k] for k in _SAFE_MEMORY_KEYS if k in memory},
        "event_id": result.get("event_id"),
        "duplicate": result.get("duplicate"),
        "target": "cloud",
        "note": "private memory stored locally; retrieve with target=local",
    }
    return ack


def _check_target(target: str) -> str:
    if target not in TARGETS:
        raise HTTPException(status_code=400, detail=f"invalid target: {target!r}")
    return target


def _check_choice(value: str | None, allowed: set[str], name: str) -> str | None:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {value!r}")
    return value


def _tool_schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def create_app(home: Path | None = None, start_worker: bool = True) -> FastAPI:
    """Build the FastAPI service for one data home.

    ``home`` defaults to $BMEM_HOME or ~/.botinkit-mem. ``start_worker`` controls
    the single background Worker loop started in the app lifespan.
    """
    if home is None:
        home = default_home()
    home = Path(home).expanduser()

    # Constructed once, up front: the fixed Store/Runtime/Worker contract.
    # Constructors do at most local directory setup; every blocking call on
    # these objects (run_once, status, install, embed) runs in the threadpool.
    store = Store(home)
    runtime = Runtime(home)
    worker = Worker(store, runtime)
    from bmem.feishu import ConnectorError, FeishuConnector

    feishu = FeishuConnector(store)

    # ------------------------------------------------------------------ worker
    loop_stats: dict[str, Any] = {
        "running": False, "cycles": 0, "errors": 0, "last_error": None,
        "last_success_at": None,
    }

    def _interval() -> float:
        raw = os.environ.get("BMEM_WORKER_INTERVAL", "0.5")
        try:
            return min(60.0, max(0.05, float(raw)))
        except ValueError:
            return 0.5

    async def _pause(stop_event: asyncio.Event, seconds: float) -> bool:
        """Sleep up to ``seconds``; True if stop was requested."""
        if seconds <= 0:
            return stop_event.is_set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return stop_event.is_set()

    async def _worker_loop(stop_event: asyncio.Event) -> None:
        loop_stats["running"] = True
        consecutive_errors = 0
        try:
            while not stop_event.is_set():
                try:
                    result = await run_in_threadpool(worker.run_once)
                    loop_stats["cycles"] += 1
                    loop_stats["last_success_at"] = now_iso()
                    loop_stats["last_error"] = None
                    consecutive_errors = 0
                    # A brief yield lets interactive writes acquire SQLite
                    # while the imported backlog drains continuously.
                    delay = 0.01 if result["claimed"] else _interval()
                    if await _pause(stop_event, delay):
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # claim/model/db failures retry with backoff
                    loop_stats["errors"] += 1
                    consecutive_errors += 1
                    loop_stats["last_error"] = _safe_message(exc)
                    backoff = min(30.0, _interval() * (2 ** min(consecutive_errors, 6)))
                    if await _pause(stop_event, backoff):
                        break
        finally:
            loop_stats["running"] = False

    # ---------------------------------------------------- persistent history sync
    # Opt-in background scan of harness histories. Enabled by BMEM_HISTORY_SYNC=1
    # and only started alongside the Worker (start_worker=True), so direct
    # services opened by tests never scan host histories.
    history_stats: dict[str, Any] = {
        "enabled": os.environ.get("BMEM_HISTORY_SYNC", "").strip().lower()
        in {"1", "true", "yes", "on"},
        "running": False,
        "scans": 0,
        "last_run_at": None,
        "last_success_at": None,
        "last_duration_ms": None,
        "last_summary": None,
        "errors": 0,
        "last_error": None,
        "consecutive_errors": 0,
    }

    def _history_interval() -> float:
        raw = os.environ.get("BMEM_HISTORY_INTERVAL", "300")
        try:
            return max(5.0, float(raw))
        except ValueError:
            return 300.0

    def _history_scan() -> dict[str, Any]:
        """Blocking scan in the threadpool; returns summary counts only."""
        sync = getattr(adapters, "sync_history", None)
        if not callable(sync):
            raise RuntimeError("history sync unavailable: adapters.sync_history missing")
        result = sync(store)
        if not isinstance(result, dict):
            return {}
        summary = {
            key: result[key]
            for key in ("harness", "discovered", "imported", "skipped", "errors",
                        "history_protocol")
            if key in result
        }
        summary["files"] = len(result.get("sources") or result.get("files") or [])
        return summary

    async def _history_loop(stop_event: asyncio.Event) -> None:
        history_stats["running"] = True
        try:
            await _pause(stop_event, _history_interval())
            while not stop_event.is_set():
                started = time.monotonic()
                history_stats["last_run_at"] = now_iso()
                try:
                    summary = await run_in_threadpool(_history_scan)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    history_stats["errors"] += 1
                    history_stats["consecutive_errors"] += 1
                    history_stats["last_error"] = _safe_message(exc)
                    if await _pause(stop_event, _history_interval()):
                        break
                    continue
                history_stats["scans"] += 1
                history_stats["last_success_at"] = now_iso()
                history_stats["last_duration_ms"] = int(
                    (time.monotonic() - started) * 1000)
                history_stats["last_summary"] = summary
                history_stats["last_error"] = None
                history_stats["consecutive_errors"] = 0
                history_stats["errors"] += int(summary.get("errors") or 0)
                if await _pause(stop_event, _history_interval()):
                    break
        finally:
            history_stats["running"] = False

    async def _warm_embedding(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await run_in_threadpool(runtime.embed, ["memory"], timeout=30)
                return
            except Exception:
                if await _pause(stop_event, 5):
                    return

    # ------------------------------------------------------------- MCP (1.30)
    mcp_server = McpServer(name=SERVICE_NAME, version=_bmem_pkg.__version__)

    tool_specs: list[Tool] = [
        Tool(
            name="memory_recall",
            description="Budgeted recall pack for the current turn. Returns injected "
                        "items, packed text with a provenance marker and token count.",
            inputSchema=_tool_schema({
                "query": {"type": "string", "default": ""},
                "harness": {"type": "string", "default": "mcp"},
                "session_id": {"type": "string", "default": "manual"},
                "turn_id": {"type": "string", "default": ""},
                "task": {"type": "string", "default": ""},
                "context_epoch": {"type": "string", "default": ""},
                "budget": {"type": "integer", "minimum": 0, "maximum": 10000,
                           "default": 2000},
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "cloud",
                           "description": _TARGET_DESC},
                "conditions": {"type": "object", "default": {}},
                "project": {"type": "string", "default": ""},
            }, []),
        ),
        Tool(
            name="memory_search",
            description="Keyword and (when enabled) semantic search over current "
                        "memories. Returns items with score and matched_by.",
            inputSchema=_tool_schema({
                "query": {"type": "string", **_NONBLANK},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "cloud",
                           "description": _TARGET_DESC},
                "conditions": {"type": "object", "default": {}},
                "project": {"type": "string", "default": "",
                            "description": "Rank hint only; never restricts the library."},
            }, ["query"]),
        ),
        Tool(
            name="memory_expand",
            description="Source expansion: one memory with revisions, sources and "
                        "arbitration decisions. Private memories require the explicit "
                        "local target; the default cloud target never returns them.",
            inputSchema=_tool_schema({
                "memory_id": {"type": "string", **_NONBLANK},
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "cloud",
                           "description": _TARGET_DESC},
            }, ["memory_id"]),
        ),
        Tool(
            name="memory_save",
            description="Explicit T1 save of a fact as an MCP declaration "
                        "(harness=mcp). Private saves on the default cloud target "
                        "acknowledge with id/status metadata only.",
            inputSchema=_tool_schema({
                "body": {"type": "string", "minLength": 1, "maxLength": 100000,
                         "pattern": "\\S"},
                "fact_key": {"type": "string", "minLength": 1},
                "effective_at": {"type": "string", "format": "date-time"},
                "occurred_at": {"type": "string", "format": "date-time"},
                "conditions": {"type": "object", "default": {}},
                "sensitivity": {"type": "string", "enum": ["normal", "private"],
                                "default": "normal"},
                "project": {"type": "string", "default": ""},
                "session_id": {"type": "string", "default": "manual"},
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "cloud",
                           "description": _WRITE_TARGET_DESC},
            }, ["body"]),
        ),
        Tool(
            name="memory_declare",
            description="Structured T1 declaration with declaration DTO fields; "
                        "corrections require memory_id and expected_revision "
                        "(harness=mcp). Private declarations on the default cloud "
                        "target acknowledge with id/status metadata only.",
            inputSchema=_tool_schema({
                "body": {"type": "string", "minLength": 1, "maxLength": 100000,
                         "pattern": "\\S"},
                "kind": {"type": "string", "enum": sorted(KINDS), "default": "fact"},
                "fact_key": {"type": "string", "minLength": 1},
                "effective_at": {"type": "string", "format": "date-time"},
                "occurred_at": {"type": "string", "format": "date-time"},
                "memory_id": {"type": "string", **_NONBLANK},
                "expected_revision": {"type": "integer", "minimum": 1},
                "conditions": {"type": "object", "default": {}},
                "derived_from": {"type": "array", "items": {"type": "string"},
                                 "default": []},
                "sensitivity": {"type": "string", "enum": ["normal", "private"],
                                "default": "normal"},
                "project": {"type": "string", "default": ""},
                "session_id": {"type": "string", "default": "manual"},
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "cloud",
                           "description": _WRITE_TARGET_DESC},
            }, ["body"]),
        ),
        Tool(
            name="memory_forget",
            description="Privacy forget: tombstone the memory and recursively clean "
                        "derived records.",
            inputSchema=_tool_schema(
                {"memory_id": {"type": "string", **_NONBLANK}}, ["memory_id"]),
        ),
        Tool(
            name="memory_status",
            description="Service, worker, settings and store counts for diagnostics.",
            inputSchema=_tool_schema({}, []),
        ),
    ]

    @mcp_server.list_tools()
    async def _mcp_list_tools() -> list[Tool]:
        return tool_specs

    def _mcp_dispatch(name: str, args: dict) -> Any:
        if name == "memory_recall":
            data = {
                "query": args.get("query", ""),
                "harness": args.get("harness", "mcp"),
                "session_id": args.get("session_id", "manual"),
                "turn_id": args.get("turn_id", ""),
                "task": args.get("task", ""),
                "context_epoch": args.get("context_epoch", ""),
                "budget": args.get("budget", 2000),
                "target": args.get("target", "cloud"),
                "conditions": args.get("conditions", {}),
                "project": args.get("project", ""),
            }
            RecallInput.model_validate(data)
            return worker.recall(data)
        if name == "memory_search":
            return worker.search(
                args["query"], limit=args.get("limit", 20),
                target=args.get("target", "cloud"),
                conditions=args.get("conditions"),
                project=args.get("project", ""))
        if name == "memory_expand":
            # Default cloud: private memories are hidden (Store raises 404).
            return store.get_memory(args["memory_id"],
                                    target=args.get("target", "cloud"))
        if name in {"memory_save", "memory_declare"}:
            payload: dict[str, Any] = {
                "body": args["body"],
                "harness": "mcp",
                "source_type": "user_declaration",
                "kind": args.get("kind", "fact") if name == "memory_declare" else "fact",
                "sensitivity": args.get("sensitivity", "normal"),
                "conditions": args.get("conditions", {}),
                "project": args.get("project", ""),
                "session_id": args.get("session_id", "manual"),
            }
            target = args.get("target", "cloud")
            if args.get("fact_key"):
                payload["fact_key"] = args["fact_key"]
            for field in ("effective_at", "occurred_at"):
                if args.get(field):
                    payload[field] = args[field]
            if name == "memory_declare":
                if args.get("derived_from"):
                    payload["derived_from"] = args["derived_from"]
                if args.get("memory_id"):
                    payload["memory_id"] = args["memory_id"]
                    if args.get("expected_revision") is None:
                        raise ValueError(
                            "expected_revision is required with memory_id")
                if args.get("expected_revision") is not None:
                    payload["expected_revision"] = args["expected_revision"]
            declaration = DeclarationInput.model_validate(payload)
            result = store.declare(declaration)
            # Decide the cloud projection from what was actually persisted: an
            # UPDATE of a private memory, or private derived_from evidence, can
            # store this result as private even when the payload said normal.
            # target=local always keeps the full detail; normal results are
            # returned unchanged.
            persisted = _persisted_sensitivity(result, str(declaration.sensitivity))
            if persisted != "normal" and target != "local":
                return _private_cloud_ack(result)
            return result
        if name == "memory_forget":
            return store.forget(args["memory_id"])
        if name == "memory_status":
            try:
                rt_status: dict[str, Any] = runtime.status()
            except Exception as exc:
                rt_status = {"available": False, "error": _safe_message(exc)}
            return {
                "service": SERVICE_NAME,
                "version": _bmem_pkg.__version__,
                "schema_version": store.schema_version,
                "worker": dict(loop_stats),
                "history_sync": dict(history_stats),
                "runtime": rt_status,
                "settings": store.get_settings(),
                "stats": store.stats(),
            }
        raise ValueError(f"unknown tool: {name}")

    @mcp_server.call_tool()
    async def _mcp_call_tool(name: str, args: dict | None) -> list[TextContent]:
        if name not in {t.name for t in tool_specs}:
            raise ValueError(f"unknown tool: {name}")
        try:
            # The SDK has already validated args against the tool inputSchema.
            result = await run_in_threadpool(_mcp_dispatch, name, args or {})
        except Exception as exc:
            # The SDK converts handler exceptions to isError CallToolResults.
            raise RuntimeError(_safe_message(exc)) from exc
        return [TextContent(type="text",
                            text=json.dumps(result, ensure_ascii=False, default=str))]

    def _make_session_manager() -> StreamableHTTPSessionManager:
        return StreamableHTTPSessionManager(
            app=mcp_server,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[*LOOPBACK_HOSTS, "127.0.0.1:*", "localhost:*",
                               "[::1]:*"],
                allowed_origins=[
                    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                    "https://127.0.0.1:*", "https://localhost:*", "https://[::1]:*",
                    "http://127.0.0.1", "http://localhost", "http://[::1]",
                    "https://127.0.0.1", "https://localhost", "https://[::1]",
                ],
            ),
        )

    # .run() is single-use per manager instance, so a fresh manager is built on
    # every lifespan start and requests dispatch through the current one.
    mcp_box: dict[str, Any] = {"manager": _make_session_manager()}

    async def _mcp_asgi(scope: dict, receive: Any, send: Any) -> None:
        await mcp_box["manager"].handle_request(scope, receive, send)

    connector_stats: dict[str, Any] = {"errors": 0, "last_error": None}

    async def _connector_loop(stop_event: asyncio.Event) -> None:
        while not await _pause(stop_event, 30):
            try:
                await run_in_threadpool(feishu.tick)
                connector_stats["last_error"] = None
            except Exception as exc:
                connector_stats["errors"] += 1
                connector_stats["last_error"] = _safe_message(exc)

    # ---------------------------------------------------------------- lifespan
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Fresh stop event per lifespan so repeated starts never share state or
        # bind waiters to a stale event loop.
        stop_event = asyncio.Event()
        worker_task: asyncio.Task | None = None
        history_task: asyncio.Task | None = None
        warmup_task: asyncio.Task | None = None
        connector_task: asyncio.Task | None = None
        if start_worker:
            # Store.__init__ no longer requeues running jobs, so opening a Store
            # (doctor, imports, tests) preserves live Worker ownership. This
            # process owns the worker loop, so recover abandoned claims here —
            # once, before launching it. With start_worker=False nothing is
            # touched and existing claims stay as they are.
            await run_in_threadpool(store.recover_jobs)
            settings = await run_in_threadpool(store.get_settings)
            if settings.get("semantic_enabled"):
                warmup_task = asyncio.create_task(_warm_embedding(stop_event),
                                                  name="bmem-embedding-warmup")
            worker_task = asyncio.create_task(_worker_loop(stop_event),
                                              name="bmem-worker")
            connector_task = asyncio.create_task(_connector_loop(stop_event),
                                                 name="bmem-connectors")
            if history_stats["enabled"]:
                history_task = asyncio.create_task(_history_loop(stop_event),
                                                   name="bmem-history-sync")
        app.state.worker_task = worker_task
        app.state.history_task = history_task
        mcp_box["manager"] = _make_session_manager()
        try:
            async with mcp_box["manager"].run():
                yield
        finally:
            stop_event.set()
            owned = [task for task in (worker_task, history_task, warmup_task, connector_task,
                                       app.state.install_task)
                     if task is not None]
            for task in owned:
                if not task.done():
                    task.cancel()
            for task in owned:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            app.state.install_task = None
            await run_in_threadpool(feishu.close)

    # -------------------------------------------------------------------- app
    app = FastAPI(title=SERVICE_NAME, version=_bmem_pkg.__version__,
                  lifespan=_lifespan)
    app.add_middleware(LoopbackGuardMiddleware)
    app.state.home = home
    app.state.store = store
    app.state.runtime = runtime
    app.state.worker = worker
    app.state.feishu = feishu
    app.state.mcp_box = mcp_box
    app.state.worker_stats = loop_stats
    app.state.history_stats = history_stats
    app.state.install_task = None
    app.state.install_error = None

    @app.exception_handler(DomainError)
    async def _domain_error(_req: Request, exc: DomainError) -> JSONResponse:
        status = int(exc.status_code)
        if status not in {400, 404, 409, 422}:
            status = 400
        return JSONResponse({"detail": _safe_message(exc)}, status_code=status)

    @app.exception_handler(ConnectorError)
    async def _connector_error(_req: Request, exc: ConnectorError) -> JSONResponse:
        return JSONResponse({"detail": redact(exc.message)},
                            status_code=409 if exc.code == "busy" else 422)

    @app.exception_handler(Exception)
    async def _internal_error(_req: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": _safe_message(exc)}, status_code=500)

    def _parse_dto(payload: Any, dto: type) -> Any:
        try:
            return dto.model_validate(payload)
        except ValidationError as exc:
            errors = [{"loc": list(item.get("loc", [])), "msg": item.get("msg"),
                       "type": item.get("type")} for item in exc.errors()[:20]]
            raise HTTPException(status_code=422,
                                detail={"message": "invalid input",
                                        "errors": errors}) from exc

    # ----------------------------------------------------------------- routes
    @app.get("/api/health")
    def health() -> dict:
        return {
            "service": SERVICE_NAME,
            "version": _bmem_pkg.__version__,
            "schema_version": store.schema_version,
            "worker": dict(loop_stats),
            "history_sync": dict(history_stats),
            "connectors": dict(connector_stats),
        }

    @app.post("/api/events")
    def post_events(payload: dict = Body(...)) -> dict:
        event = _parse_dto(payload, EventInput)
        return store.ingest(event)

    @app.post("/api/declarations")
    def post_declarations(payload: dict = Body(...)) -> dict:
        declaration = _parse_dto(payload, DeclarationInput)
        return store.declare(declaration)

    @app.get("/api/memories")
    def get_memories(q: str = Query(""), kind: str | None = Query(None),
                     status: str | None = Query(None),
                     harness: str | None = Query(None),
                     target: str = Query("local"),
                     limit: int = Query(100, ge=1, le=1000),
                     offset: int = Query(0, ge=0)) -> dict:
        return {"items": store.list_memories(
            query=q, kind=_check_choice(kind, KINDS, "kind"),
            status=_check_choice(status, STATUSES, "status"),
            harness=harness, target=_check_target(target),
            limit=limit, offset=offset)}

    @app.get("/api/memories/{memory_id}")
    def get_memory(memory_id: str, target: str = Query("local")) -> dict:
        return store.get_memory(memory_id, target=_check_target(target))

    @app.post("/api/memories/{memory_id}/forget")
    def forget_memory(memory_id: str) -> dict:
        return store.forget(memory_id)

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=1), target: str = Query("cloud"),
               limit: int = Query(20, ge=1, le=200)) -> dict:
        _check_target(target)
        return {"items": worker.search(q, limit=limit, target=target)}

    @app.post("/api/recall")
    def recall(payload: dict = Body(...)) -> dict:
        data = _parse_dto(payload, RecallInput).model_dump()
        return worker.recall(data)

    @app.get("/api/stats")
    def stats() -> dict:
        return store.stats()

    def _make_ledger(record: str):
        """Closure factory: the ledger name is captured, never a query param."""

        def _ledger() -> dict:
            return {"items": store.records(record)}

        _ledger.__name__ = f"records_{record}"
        return _ledger

    for _route, _record in RECORD_ENDPOINTS.items():
        app.get(f"/api/{_route}", name=f"records_{_record}")(_make_ledger(_record))

    @app.post("/api/imports")
    def post_imports(payload: dict = Body(...)) -> dict:
        harness = payload.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise HTTPException(status_code=400, detail="harness is required")
        paths = payload.get("paths")
        if paths is not None and (not isinstance(paths, list)
                                  or not all(isinstance(p, str) for p in paths)):
            raise HTTPException(status_code=400, detail="paths must be a list of strings")
        return adapters.import_history(store, harness=harness, paths=paths)

    @app.get("/api/graph")
    def graph(target: Literal["local", "cloud"] = "local") -> dict:
        return store.graph(target=target)

    @app.get("/api/events/{event_id}")
    def event_detail(event_id: str, target: Literal["local", "cloud"] = "local") -> dict:
        return store.get_event(event_id, target=target)

    @app.get("/api/sources")
    def sources() -> dict:
        return {"items": adapters.doctor()}

    @app.get("/api/connectors/{brand}")
    def connector_status(brand: Literal["feishu", "lark"]) -> dict:
        return feishu.status(brand)

    @app.post("/api/connectors/{brand}/connect")
    def connector_connect(brand: Literal["feishu", "lark"],
                          payload: ConnectorSelection = Body(default=ConnectorSelection())) -> dict:
        if payload.sensitivity is not None and payload.sources is not None:
            feishu.configure(brand, payload.sources, enabled=bool(payload.sources),
                             sensitivity=payload.sensitivity)
        return feishu.connect(brand, sources=payload.sources)

    @app.put("/api/connectors/{brand}/config")
    def connector_config(brand: Literal["feishu", "lark"], payload: ConnectorConfig) -> dict:
        return feishu.configure(brand, payload.sources, enabled=payload.enabled,
                                 sensitivity=payload.sensitivity)

    @app.post("/api/connectors/{brand}/sync")
    def connector_sync(brand: Literal["feishu", "lark"]) -> dict:
        return feishu.sync(brand)

    @app.get("/api/connectors/{brand}/qrcode")
    def connector_qrcode(brand: Literal["feishu", "lark"]) -> Response:
        content = feishu.qrcode(brand)
        if not content:
            raise HTTPException(status_code=404, detail="No active authorization")
        return Response(content=content, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/settings/semantic")
    def get_semantic_settings() -> dict:
        return store.get_settings()

    @app.put("/api/settings/semantic")
    def put_semantic_settings(payload: dict = Body(...)) -> dict:
        if "enabled" not in payload or not isinstance(payload["enabled"], bool):
            raise HTTPException(status_code=400, detail="enabled (bool) is required")
        return store.set_settings({"semantic_enabled": payload["enabled"]})

    @app.get("/api/runtime")
    def runtime_status() -> dict:
        try:
            return runtime.status()
        except Exception as exc:
            raise HTTPException(status_code=500,
                                detail=_safe_message(exc)) from exc

    @app.post("/api/runtime/embedding/install")
    async def install_embedding() -> dict:
        task = app.state.install_task
        if task is not None and not task.done():
            raise HTTPException(status_code=409,
                                detail="installation already running")

        async def _run() -> None:
            try:
                await run_in_threadpool(runtime.install, embedding_only=True,
                                        update=False)
                app.state.install_error = None
            except Exception as exc:
                app.state.install_error = _safe_message(exc)

        app.state.install_task = asyncio.create_task(_run(), name="bmem-install")
        return {"started": True}

    @app.post("/api/backup")
    def backup(payload: dict | None = Body(default=None)) -> dict:
        path = (payload or {}).get("path")
        if path is not None and not isinstance(path, str):
            raise HTTPException(status_code=400, detail="path must be a string")
        destination = Path(path).expanduser() if path else None
        return {"path": store.backup(destination)}

    @app.post("/api/restore")
    def restore(payload: dict = Body(...)) -> dict:
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(status_code=400, detail="path is required")
        result = store.restore(Path(path).expanduser())
        return {"restored": True, **result}

    @app.post("/api/worker/run")
    def worker_run() -> dict:
        return worker.run_once()

    @app.post("/api/indexes/rebuild")
    def indexes_rebuild() -> dict:
        return store.rebuild_indexes()

    # ----------------------------------------------------------------- static
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def ui_index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    # -------------------------------------------------------------------- mcp
    app.mount("/mcp", _mcp_asgi)

    return app


def serve(home: Path | None = None, port: int | None = None,
          start_worker: bool = True) -> None:
    """Run the loopback daemon in the foreground (used by ``bmem start``/``bmemd``)."""
    import uvicorn

    app = create_app(Path(home).expanduser() if home else None,
                     start_worker=start_worker)
    uvicorn.run(app, host="127.0.0.1", port=int(port) if port else default_port(),
                log_level="info")
