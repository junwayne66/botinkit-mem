"""bmem command line interface and bmemd daemon entry point.

Implements the CLI surface from
specs/001-personal-memory/contracts/interfaces.md: global ``--home``/``--url``,
``init`` (default full provisioning, ``--skip-models``/``--skip-service``),
foreground ``start``/``bmemd``, service-backed operations with local Store
fallback for appropriate offline operations, harness ``hook`` over stdin/stdout,
and JSON output. Errors go to stderr with a nonzero exit; bodies are never
echoed back in error text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

HOOK_STDIN_LIMIT = 1_000_000  # generous; DTOs bound bodies far below this


class ApiUnavailable(RuntimeError):
    """Service is not reachable; caller may fall back to the local Store."""


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"HTTP {status}: {_detail_text(detail)}")
        self.status = status
        self.detail = detail


class CliError(RuntimeError):
    """User-facing failure message (already sanitized)."""


def _detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail[:300]
    try:
        return json.dumps(detail, ensure_ascii=False, default=str)[:300]
    except Exception:
        return str(detail)[:300]


def _safe_exc_message(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    try:  # reuse entry-level redaction when available
        from bmem.models import redact

        msg = redact(msg)
    except Exception:
        pass
    return msg[:300]


def _default_home() -> Path:
    env = os.environ.get("BMEM_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".botinkit-mem"


def _default_port() -> int:
    env = os.environ.get("BMEM_PORT", "").strip()
    try:
        port = int(env) if env else 8790
    except ValueError:
        port = 8790
    return port if 1 <= port <= 65535 else 8790


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home).expanduser() if getattr(args, "home", None) \
        else _default_home()


def _url(args: argparse.Namespace) -> str:
    raw = getattr(args, "url", None) or os.environ.get("BMEM_URL", "").strip()
    if not raw:
        raw = f"http://127.0.0.1:{_default_port()}"
    return raw.rstrip("/")


def _emit(obj: Any) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return 0


def _service_up(url: str) -> bool:
    """True only when a live bmem service answers /api/health on this URL."""
    import httpx

    try:
        r = httpx.get(f"{url}/api/health", timeout=1.5)
        if r.status_code != 200:
            return False
        body = r.json()
        return isinstance(body, dict) and body.get("service") == "bmem"
    except Exception:
        return False


def _with_service_fallback(args: argparse.Namespace, api_call: Callable[[str], Any],
                           local_call: Callable[[], Any], via: str = "api") -> Any:
    """Run api_call when the bmem service is live; otherwise the local Store.

    Domain/API errors (4xx/5xx from a verified bmem service) propagate so
    conflicts stay visible; transport failures fall through to local.
    """
    url = _url(args)
    if _service_up(url):
        try:
            result = api_call(url)
            if isinstance(result, dict):
                result = {**result, "via": via}
            return result
        except ApiUnavailable:
            pass  # service dropped between health check and call
    result = local_call()
    if isinstance(result, dict):
        result = {**result, "via": "local"}
    return result


def _api(url: str, method: str, path: str, *, json_body: Any = None,
         params: dict | None = None) -> Any:
    import httpx

    try:
        with httpx.Client(base_url=url, timeout=httpx.Timeout(60.0, connect=1.5)) as c:
            r = c.request(method, path, json=json_body, params=params)
    except (httpx.HTTPError, OSError) as exc:
        raise ApiUnavailable(f"service not reachable at {url} ({type(exc).__name__})") \
            from exc
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise ApiError(r.status_code, detail)
    if not r.content:
        return {}
    return r.json()


def _store(home: Path):
    from bmem.store import Store

    return Store(home)


def _runtime(home: Path):
    from bmem.runtime import Runtime

    return Runtime(home)


def _adapters():
    from bmem import adapters

    return adapters


def _result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("available") is False:
        return True
    if result.get("error"):
        return True
    state = str(result.get("status", "")).lower()
    return state in {"failed", "error"}


# ------------------------------------------------------------------ commands


def cmd_init(args: argparse.Namespace) -> int:
    home = _home(args)
    home.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"home": str(home)}
    failed = False

    _store(home)  # creates memory.db + evidence dir
    report["store"] = {"ok": True, "db": str(home / "memory.db")}

    rt = None
    if not (args.skip_models and args.skip_service):
        rt = _runtime(home)  # constructed once for both provisioning steps

    if args.skip_models:
        report["models"] = {"skipped": True}
    else:
        try:
            result = rt.install(embedding_only=False, update=False)
            report["models"] = result
            failed = failed or _result_failed(result)
        except Exception as exc:
            report["models"] = {"ok": False, "error": _safe_exc_message(exc)}
            failed = True

    if args.skip_service:
        report["service"] = {"skipped": True}
    else:
        try:
            result = rt.register_service()
            report["service"] = result
            failed = failed or _result_failed(result)
        except Exception as exc:
            report["service"] = {"ok": False, "error": _safe_exc_message(exc)}
            failed = True

    rc = _emit(report)
    return 1 if failed else rc


def cmd_start(args: argparse.Namespace) -> int:
    from bmem.api import serve

    serve(home=_home(args), port=args.port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    url = _url(args)
    home = _home(args)
    out: dict[str, Any] = {"home": str(home)}
    if _service_up(url):
        try:
            out["live"] = True
            out["url"] = url
            out["health"] = _api(url, "GET", "/api/health")
            out["stats"] = _api(url, "GET", "/api/stats")
            out["settings"] = _api(url, "GET", "/api/settings/semantic")
            out["runtime"] = _api(url, "GET", "/api/runtime")
            return _emit(out)
        except ApiUnavailable:
            out["note"] = "service dropped mid-request; showing local view"
    out["live"] = False
    try:
        store = _store(home)
        out["stats"] = store.stats()
        out["settings"] = store.get_settings()
    except Exception as exc:  # real store/runtime failures still get reported
        out["store"] = {"ok": False, "error": _safe_exc_message(exc)}
    rt_box: dict[str, Any] = {}
    try:
        rt_box = _runtime(home).status()
    except Exception as exc:
        rt_box = {"available": False, "error": _safe_exc_message(exc)}
    out["runtime"] = rt_box
    return _emit(out)


def cmd_doctor(args: argparse.Namespace) -> int:
    url = _url(args)
    home = _home(args)
    out: dict[str, Any] = {"home": str(home), "url": url}
    out["service_reachable"] = _service_up(url)

    adapters = _adapters()
    try:
        out["adapters"] = adapters.doctor()
    except Exception as exc:
        out["adapters"] = {"ok": False, "error": _safe_exc_message(exc)}

    try:
        store = _store(home)
        out["store"] = {"ok": True, "stats": store.stats()}
    except Exception as exc:
        out["store"] = {"ok": False, "error": _safe_exc_message(exc)}

    try:
        out["runtime"] = _runtime(home).status()
    except Exception as exc:
        out["runtime"] = {"available": False, "error": _safe_exc_message(exc)}

    ok = bool(out["service_reachable"]) and out["store"].get("ok", False) is True
    _emit(out)
    return 0 if ok else 1


def cmd_web(args: argparse.Namespace) -> int:
    import webbrowser

    url = _url(args)
    if not _service_up(url):
        raise CliError(f"service is not reachable at {url}; run `bmem start` first")
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    return _emit({"url": url, "opened": bool(opened),
                  "note": "loopback management console"})


def cmd_connect(args: argparse.Namespace) -> int:
    if args.harness in {"feishu", "lark"}:
        args.brand = args.harness
        args.action = "connect"
        return cmd_connector(args)
    if args.sources is not None or args.no_history or args.sensitivity is not None:
        raise CliError("历史范围选项用于飞书或 Lark 连接")
    adapters = _adapters()
    result = adapters.connect(args.harness, config_root=None, url=_url(args))
    return _emit(result)


def cmd_connector(args: argparse.Namespace) -> int:
    url = _url(args)
    path = f"/api/connectors/{args.brand}"
    if args.action == "status":
        return _emit(_api(url, "GET", path))
    state = _api(url, "GET", path)
    if args.action == "connect":
        sources = _connector_sources(args, state)
        payload = {}
        if sources is not None:
            payload["sources"] = sources
        if args.sensitivity is not None:
            if sources is None:
                payload["sources"] = state["history"]["sources"]
            payload["sensitivity"] = args.sensitivity
        state = _api(url, "POST", path + "/connect", json_body=payload)
    else:
        state = _api(url, "POST", path + "/sync", json_body={})
    should_wait = args.wait if args.wait is not None else sys.stdin.isatty()
    if should_wait:
        state = _wait_connector(url, path, state)
    _emit(state)
    return 1 if state.get("phase") == "error" else 0


def _connector_sources(args: argparse.Namespace, state: dict) -> list[str] | None:
    if args.no_history:
        return []
    if args.sources is not None:
        return args.sources
    if state.get("history", {}).get("confirmed"):
        return None
    if not sys.stdin.isatty():
        raise CliError("请选择历史范围，使用 --sources private_chats,group_chats,documents，"
                       "或使用 --no-history 暂不读取")
    print("首次连接可读取历史资料，可多选。", file=sys.stderr)
    print("1 私聊  2 群聊  3 飞书文档", file=sys.stderr)
    answer = input("输入编号并用逗号分隔，直接回车暂不读取\n").strip()
    choices = {"1": "private_chats", "2": "group_chats", "3": "documents"}
    selected = answer.replace(" ", ",").replace("，", ",").split(",")
    selected = [value for value in selected if value]
    if any(value not in choices for value in selected):
        raise CliError("范围编号为 1、2、3，可使用逗号多选")
    return list(dict.fromkeys(choices[value] for value in selected))


def _wait_connector(url: str, path: str, state: dict) -> dict:
    import webbrowser

    last_phase = None
    last_authorization = None
    deadline = time.monotonic() + 1200
    while True:
        phase = state.get("phase")
        if phase != last_phase:
            print(f"当前步骤 {phase}", file=sys.stderr, flush=True)
            last_phase = phase
        authorization = state.get("authorization") or {}
        authorization_url = authorization.get("url")
        if authorization_url and authorization_url != last_authorization:
            print(f"授权链接\n{authorization_url}", file=sys.stderr, flush=True)
            print(f"二维码\n{url}{path}/qrcode", file=sys.stderr, flush=True)
            webbrowser.open(authorization_url)
            last_authorization = authorization_url
        if not state.get("busy"):
            return state
        if time.monotonic() >= deadline:
            raise CliError("连接仍在后台进行，可通过 status 或 Web 来源页继续查看")
        time.sleep(1)
        state = _api(url, "GET", path)


def _parse_connector_sources(value: str) -> list[str]:
    sources = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    allowed = {"private_chats", "group_chats", "documents"}
    if not sources or not set(sources) <= allowed:
        raise argparse.ArgumentTypeError("范围为 private_chats、group_chats、documents，可用逗号多选")
    return sources


def _add_connector_options(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--sources", type=_parse_connector_sources,
                           help="private_chats,group_chats,documents，多选历史范围")
    selection.add_argument("--no-history", action="store_true", help="暂不读取历史")
    parser.add_argument("--sensitivity", choices=["normal", "private"], default=None,
                        help="private 仅本机，normal 允许云端助手召回")
    waiting = parser.add_mutually_exclusive_group()
    waiting.add_argument("--wait", dest="wait", action="store_true")
    waiting.add_argument("--no-wait", dest="wait", action="store_false")
    parser.set_defaults(wait=None)


def cmd_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read(HOOK_STDIN_LIMIT)
    if not raw.strip():
        raise CliError("hook expects a JSON payload on stdin")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid hook payload JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise CliError("hook payload must be a JSON object")
    adapters = _adapters()
    result = adapters.hook(args.harness, args.event, payload, url=_url(args))
    # Native protocol: result JSON (additionalContext) on stdout.
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    adapters = _adapters()
    store = _store(_home(args))
    result = adapters.import_history(store, harness=args.harness,
                                     paths=args.paths or None)
    return _emit(result)


def cmd_search(args: argparse.Namespace) -> int:
    params = {"q": args.query, "limit": args.limit, "target": args.target}
    if args.project:
        params["project"] = args.project

    def api(url: str) -> Any:
        return _api(url, "GET", "/api/search", params=params)

    def local() -> Any:
        store = _store(_home(args))
        return {"items": store.search(args.query, limit=args.limit,
                                      target=args.target, project=args.project)}

    return _emit(_with_service_fallback(args, api, local))


def cmd_recall(args: argparse.Namespace) -> int:
    data: dict[str, Any] = {
        "query": args.query,
        "budget": args.budget,
        "target": args.target,
        "conditions": _parse_conditions(args.conditions),
        "project": args.project,
    }
    for key, value in (("harness", args.harness), ("session_id", args.session),
                       ("turn_id", args.turn_id), ("task", args.task),
                       ("context_epoch", args.context_epoch)):
        if value:
            data[key] = value

    def api(url: str) -> Any:
        return _api(url, "POST", "/api/recall", json_body=data)

    def local() -> Any:
        return _store(_home(args)).recall(data)

    return _emit(_with_service_fallback(args, api, local))


def _parse_conditions(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid --conditions JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CliError("--conditions must be a JSON object")
    return value


def cmd_declare(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "body": args.body,
        "kind": args.kind,
        "sensitivity": args.sensitivity,
        "conditions": _parse_conditions(args.conditions),
        "project": args.project,
        "session_id": args.session,
        "harness": args.harness,
    }
    if args.fact_key:
        payload["fact_key"] = args.fact_key
    if args.memory_id:
        payload["memory_id"] = args.memory_id
        if args.expected_revision is None:
            raise CliError("--expected-revision is required with --memory-id")
    if args.expected_revision is not None:
        payload["expected_revision"] = args.expected_revision

    def api(url: str) -> Any:
        return _api(url, "POST", "/api/declarations", json_body=payload)

    def local() -> Any:
        return _store(_home(args)).declare(payload)

    return _emit(_with_service_fallback(args, api, local))


def cmd_forget(args: argparse.Namespace) -> int:
    def api(url: str) -> Any:
        return _api(url, "POST", f"/api/memories/{args.memory_id}/forget")

    def local() -> Any:
        return _store(_home(args)).forget(args.memory_id)

    return _emit(_with_service_fallback(args, api, local))


def cmd_backup(args: argparse.Namespace) -> int:
    body = {"path": args.destination} if args.destination else {}

    def api(url: str) -> Any:
        return _api(url, "POST", "/api/backup", json_body=body)

    def local() -> Any:
        dest = Path(args.destination).expanduser() if args.destination else None
        return {"path": _store(_home(args)).backup(dest)}

    return _emit(_with_service_fallback(args, api, local))


def cmd_restore(args: argparse.Namespace) -> int:
    archive = str(Path(args.archive).expanduser())

    def api(url: str) -> Any:
        return _api(url, "POST", "/api/restore", json_body={"path": archive})

    def local() -> Any:
        # Local restore is only appropriate while the service is stopped:
        # Store.restore closes live connections before swapping files.
        result = _store(_home(args)).restore(Path(archive))
        if isinstance(result, dict):
            return {"restored": True, **result}
        return {"restored": True, "result": result}

    return _emit(_with_service_fallback(args, api, local))


def cmd_runtime(args: argparse.Namespace) -> int:
    rt = _runtime(_home(args))
    if args.action == "status":
        return _emit(rt.status())
    if args.action == "install":
        result = rt.install(embedding_only=args.embedding_only, update=False)
        report = {"install": result, "status": rt.status()}
        rc = _emit(report)
        return 1 if _result_failed(result) else rc
    if args.action == "update":
        result = rt.install(embedding_only=False, update=True)
        report = {"update": result, "status": rt.status()}
        rc = _emit(report)
        return 1 if _result_failed(result) else rc
    raise CliError(f"unknown runtime action: {args.action}")


def cmd_uninstall(args: argparse.Namespace) -> int:
    rt = _runtime(_home(args))
    return _emit(rt.uninstall_runtime())


def cmd_index(args: argparse.Namespace) -> int:
    if args.action != "rebuild":
        raise CliError(f"unknown index action: {args.action}")

    def api(url: str) -> Any:
        return _api(url, "POST", "/api/indexes/rebuild")

    def local() -> Any:
        return _store(_home(args)).rebuild_indexes()

    return _emit(_with_service_fallback(args, api, local))


# -------------------------------------------------------------------- parser


def _add_global(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    extra: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--home", metavar="PATH",
                        help="data root (default $BMEM_HOME or ~/.botinkit-mem)",
                        **extra)
    parser.add_argument("--url", metavar="URL",
                        help="service URL (default http://127.0.0.1:$BMEM_PORT or :8790)",
                        **extra)


def build_parser(prog: str = "bmem") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="botinkit-mem personal memory service CLI")
    _add_global(parser)
    parser.add_argument("--version", action="store_true",
                        help="print package version and exit")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create the store, install models, register service")
    p.add_argument("--skip-models", action="store_true",
                   help="skip local runtime/model provisioning")
    p.add_argument("--skip-service", action="store_true",
                   help="skip launchd/systemd service registration")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("start", help="run the loopback daemon in the foreground")
    p.add_argument("--port", type=int, default=None,
                   help="listen port (default $BMEM_PORT or 8790, loopback only)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("doctor", help="check adapters, store, runtime and service")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="service status (API when running, local otherwise)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("web", help="open the management console in a browser")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("connect", help="wire one harness into bmem (honors BMEM_CONFIG_ROOT)")
    p.add_argument("harness", choices=["claude-code", "codex", "dsh", "feishu", "lark"])
    _add_connector_options(p)
    p.set_defaults(func=cmd_connect)

    for brand in ("feishu", "lark"):
        p = sub.add_parser(brand, help=f"{brand} 连接、授权与历史同步")
        p.add_argument("action", choices=["connect", "status", "sync"])
        _add_connector_options(p)
        p.set_defaults(func=cmd_connector, brand=brand)

    p = sub.add_parser("hook", help="harness hook bridge: JSON payload on stdin, "
                                    "additionalContext JSON on stdout")
    p.add_argument("harness", choices=["claude-code", "codex", "dsh"])
    p.add_argument("event", help="hook event name (e.g. UserPromptSubmit)")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("import", help="import harness history into the store")
    p.add_argument("--harness", default="all",
                   help="claude-code|codex|dsh|history kind|all (default all)")
    p.add_argument("paths", nargs="*", help="specific files or directories to import")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("search", help="keyword/semantic search over memories")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--target", choices=["local", "cloud"], default="cloud")
    p.add_argument("--project", default="")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recall", help="budgeted recall pack for a turn")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--harness", default="")
    p.add_argument("--session", default="")
    p.add_argument("--turn-id", dest="turn_id", default="")
    p.add_argument("--task", default="")
    p.add_argument("--context-epoch", dest="context_epoch", default="")
    p.add_argument("--budget", type=int, default=2000)
    p.add_argument("--target", choices=["local", "cloud"], default="cloud")
    p.add_argument("--conditions", default="")
    p.add_argument("--project", default="")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("declare", help="declare an immediate T1 fact/correction")
    p.add_argument("body")
    p.add_argument("--kind", choices=["episode", "fact", "decision", "method",
                                      "preference"], default="fact")
    p.add_argument("--fact-key", dest="fact_key", default="")
    p.add_argument("--memory-id", dest="memory_id", default="")
    p.add_argument("--expected-revision", dest="expected_revision", type=int,
                   default=None)
    p.add_argument("--sensitivity", choices=["normal", "private"], default="normal")
    p.add_argument("--conditions", default="")
    p.add_argument("--project", default="")
    p.add_argument("--session", default="manual")
    p.add_argument("--harness", default="cli")
    p.set_defaults(func=cmd_declare)

    p = sub.add_parser("forget", help="tombstone a memory and clean derived records")
    p.add_argument("memory_id")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("backup", help="create a consistent tar.gz backup")
    p.add_argument("destination", nargs="?", default=None)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore a backup archive")
    p.add_argument("archive")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("runtime", help="manage the local model runtime")
    p.add_argument("action", choices=["install", "status", "update"])
    p.add_argument("--embedding-only", dest="embedding_only", action="store_true",
                   help="install only the bge-m3 embedding model")
    p.set_defaults(func=cmd_runtime)

    p = sub.add_parser("uninstall", help="uninstall the managed runtime and service")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("index", help="manage derived indexes")
    p.add_argument("action", choices=["rebuild"])
    p.set_defaults(func=cmd_index)

    # Accept the global flags after the subcommand as well.
    for action in sub.choices.values():
        _add_global(action, suppress=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("bmem")
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        import bmem

        print(bmem.__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ApiError as exc:
        print(json.dumps({"error": _safe_exc_message(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except ApiUnavailable as exc:  # explicit non-fallback commands reach here
        print(json.dumps({"error": _safe_exc_message(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except Exception as exc:  # CliError, domain/store/http failures alike
        print(json.dumps({"error": _safe_exc_message(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bmemd", description="bmem foreground daemon (loopback only)")
    parser.add_argument("--home", metavar="PATH",
                        help="data root (default $BMEM_HOME or ~/.botinkit-mem)")
    parser.add_argument("--port", type=int, default=None,
                        help="listen port (default $BMEM_PORT or 8790)")
    args = parser.parse_args(argv)
    from bmem.api import serve

    serve(home=Path(args.home).expanduser() if args.home else None,
          port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
