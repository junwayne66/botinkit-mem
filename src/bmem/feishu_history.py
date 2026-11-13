"""Read-only Feishu/Lark history import through the official lark-cli.

The caller owns ``run_cli`` (fixed binary/profile, stderr and exit handling).
This module only builds read-only command lines and interprets the official
business envelope ``{ok: true, identity: "user", data}``.

Cursor design (rows in ``import_cursors``, ``source = brand:account``):

* ``list:chats``  ``{"page_token": t}`` while listing; removed once complete.
* ``chat:<chat>`` ``{"watermark": s, "threads": [...], "pending": {"start", "end", "page_token"}}``.
  A fixed window is opened each run; the watermark only advances to ``end``
  after every page of the window reached the store. A failed page keeps
  ``pending`` so the next run continues from that token in that window.
* ``thread:<thread>`` ``{"threads": [...], "known": [...]}`` and
  ``{"page_token": t}`` while an enumeration is incomplete.
* ``list:docs`` ``{"page_token": t}`` while a search page failed.
* ``doc:<canonical>`` ``{"revision": r, "hash": h}`` for unchanged-skip.
* ``message:<chat>:<message>`` last known ``update_time`` / ``deleted``.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from bmem.adapters import _markdown_chunks
from bmem.models import _MAX_BODY

PARSER_VERSION = "feishu-history/1"

_SOURCE_ORDER = ("private_chats", "group_chats", "documents")
_SOURCES = frozenset(_SOURCE_ORDER)
_MESSAGE_SCOPES = {"im:chat:read", "im:message:readonly"}
_CHAT_PAGE_SIZE = 100
_MESSAGE_PAGE_SIZE = 50
_DOCUMENT_PAGE_SIZE = 20
_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_sources(sources) -> list[str]:
    selected = [str(value) for value in list(sources or [])]
    for value in selected:
        if value not in _SOURCES:
            raise ValueError(f"unknown feishu history source: {value!r}")
    return [value for value in _SOURCE_ORDER if value in selected]


def required_scopes(sources) -> set[str]:
    """Return the minimal read-only OAuth scopes for the selected sources."""

    selected = _normalize_sources(sources)
    scopes = {"offline_access"}
    if "private_chats" in selected or "group_chats" in selected:
        scopes |= _MESSAGE_SCOPES
    if "private_chats" in selected:
        scopes.add("im:message.p2p_msg:get_as_user")
    if "group_chats" in selected:
        scopes.add("im:message.group_msg:get_as_user")
    if "documents" in selected:
        scopes |= {"search:docs:read", "docx:document:readonly"}
    return scopes


# ---------------------------------------------------------------------------
# Official CLI protocol
# ---------------------------------------------------------------------------


def _data(value):
    if not isinstance(value, dict) or value.get("ok") is not True:
        detail = value.get("error") if isinstance(value, dict) else value
        raise ValueError(f"lark-cli call failed: {json.dumps(detail, ensure_ascii=False)}")
    if value.get("identity") != "user":
        raise ValueError(f"lark-cli call did not run as user: {value.get('identity')!r}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("lark-cli call returned no data object")
    return data


def _page(value, key: str) -> tuple[list, str]:
    """Return ``(items, next_page_token)``; token is non-empty only when ``has_more``."""

    data = _data(value)
    items = data.get(key)
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError(f"lark-cli call returned no {key} list")
    if not data.get("has_more"):
        return items, ""
    token = data.get("page_token") or ""
    if not token:
        raise ValueError(f"lark-cli reported has_more without page_token for {key}")
    return items, str(token)


def _iso(value, millis: bool = False) -> str | None:
    try:
        seconds = int(str(value))
    except (TypeError, ValueError):
        return None
    if millis:
        seconds //= 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _object(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _rich_text(content: dict) -> str:
    """Flatten nested post content while keeping readable text."""

    lines = []
    title = content.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip())
    for row in content.get("content") or []:
        parts = []
        for node in row if isinstance(row, list) else []:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text":
                parts.append(str(node.get("text") or ""))
            elif tag in ("at", "mention"):
                parts.append(f"@{node.get('user_id') or node.get('user_name') or 'user'}")
            elif tag == "a":
                parts.append(f"{node.get('text') or ''} ({node.get('href') or ''})".strip())
            elif tag in ("img", "media"):
                parts.append(f"[{tag}:{node.get('image_key') or node.get('file_key') or '?'}]")
            elif tag == "emotion":
                parts.append(f"[emotion:{node.get('emoji_type') or '?'}]")
            elif node.get("text"):
                parts.append(str(node.get("text")))
        text = "".join(parts).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def message_text(message: dict) -> str:
    """Readable text of one message; other resources keep a typed placeholder."""

    kind = str(message.get("msg_type") or "").lower()
    content = _object(_object(message.get("body")).get("content"))
    if kind == "text":
        text = content.get("text")
        return text.strip() if isinstance(text, str) else ""
    if kind == "post":
        language = content.get("content")
        return _rich_text(language) if isinstance(language, dict) else _rich_text(content)
    if kind == "interactive":
        return "[card] " + _TAG_RE.sub("", str(content.get("title") or "")).strip()
    if kind in ("image", "audio", "media", "file", "sticker"):
        key = content.get("image_key") or content.get("file_key") or content.get("file_name")
        return f"[{kind}:{key or '?'}]"
    key = content.get("image_key") or content.get("file_key")
    return f"[{kind or 'message'}:{key or '?'}]"


def _reactions(message: dict) -> str:
    names = []
    for reaction in message.get("reactions") or []:
        if isinstance(reaction, dict):
            for item in reaction.get("reaction_list") or []:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
    return f" [reactions:{','.join(names)}]" if names else ""


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class _Importer:
    def __init__(self, store, run_cli, brand, account_id, sensitivity, progress):

        self.store = store
        self.run_cli = run_cli
        self.brand = brand
        self.sensitivity = sensitivity
        self.progress = progress or (lambda _update: None)
        fingerprint = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
        self.namespace = f"{brand}:{fingerprint}"
        self.counts = {"imported": 0, "skipped": 0, "errors": 0}
        self.stats: dict[str, dict] = {}

    # -- bookkeeping -------------------------------------------------------

    def emit(self, stage: str) -> None:
        self.progress({**self.counts, "stage": stage})

    def error(self, stage: str, exc: Exception) -> None:
        self.counts["errors"] += 1
        self.emit(f"error {stage}: {exc}")

    def cursor(self, path: str) -> dict:
        row = self.store.get_import_cursor(self.namespace, path)
        raw = row.get("last_cursor") if row else None
        if isinstance(raw, str) and raw:
            try:
                value = json.loads(raw)
            except ValueError:
                value = {}
            if isinstance(value, dict):
                return value
        return {}

    def record(self, path: str, state: dict, error: None | str = None) -> None:
        stats = self.stats.setdefault(path, {"discovered": 0, "imported": 0, "skipped": 0})
        self.store.record_import(self.namespace, {
            "path": path,
            "parser_version": PARSER_VERSION,
            "discovered": stats["discovered"],
            "imported": stats["imported"],
            "skipped": stats["skipped"],
            "last_cursor": state,
            "error": error,
        })

    def event(self, *, root: str, version: str, chunk: int, body: str, session: str,
              occurred_at: str | None, metadata: dict) -> dict:
        event_id = f"{self.namespace}:{root}:{version}:{chunk}"
        return {
            "body": body,
            "harness": self.brand,
            "device_id": "local",
            "native_event_id": event_id,
            "operation_id": event_id,
            "source_type": "external",
            "occurred_at": occurred_at,
            "session_id": session,
            "sensitivity": self.sensitivity,
            "metadata": {"origin": "feishu-remote", "history_import": True,
                         "root_source_id": f"{self.namespace}:{root}", **metadata},
        }

    # -- messages ----------------------------------------------------------

    def add_message(self, message: dict, chat_id: str, *, thread: bool, chat: dict | None) -> None:
        """Store one message version; chat and thread deliveries share its ID."""

        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise ValueError("feishu message without message_id")
        owner = str(message.get("chat_id") or chat_id)
        path = f"message:{owner}:{message_id}"
        state = self.cursor(path)
        discovered = {"discovered": 1, "imported": 0, "skipped": 0}
        updated = str(message.get("update_time") or "")
        deleted = bool(message.get("deleted"))
        create_iso = _iso(message.get("create_time"), millis=True)
        if (not deleted and state.get("updated") == updated and state.get("create_time") == create_iso
                and state.get("deleted") == deleted):
            self.stats[path] = discovered
            self.record(path, state)
            self.counts["skipped"] += 1
            self.stats[path]["skipped"] = 1
            return
        text = message_text(message)
        body = f"(撤回) {text}" if deleted else text + _reactions(message)
        if not body.strip():
            body = f"[{message.get('msg_type') or 'message'}]"
        flags = []
        if deleted:
            flags.append("deleted")
        if message.get("updated"):
            flags.append("edited")
        version = f"{updated or create_iso or '-'}:{','.join(flags) or 'v'}"
        changed = self.store.ingest(self.event(
            root=f"message:{message_id}", version=version, chunk=0, body=body,
            session=owner, occurred_at=create_iso,
            metadata={
                "source_url": message.get("message_app_link") or (chat or {}).get("chat_app_link") or "",
                "chat_id": owner,
                "chat_mode": (chat or {}).get("chat_mode"),
                "chat_name": (chat or {}).get("name"),
                "message_id": message_id,
                "thread_id": str(message.get("thread_id") or ""),
                "found_in": "thread" if thread else "chat",
                "msg_type": message.get("msg_type"),
                "sender": message.get("sender"),
                "status": "deleted" if deleted else "active",
            },
        ), reference_episode=True)
        kind = "skipped" if changed.get("duplicate") else "imported"
        self.counts[kind] += 1
        self.stats[path] = {"discovered": 1, "imported": int(kind == "imported"),
                            "skipped": int(kind == "skipped")}
        self.record(path, {"updated": updated, "create_time": create_iso, "deleted": deleted,
                           "chat_id": owner, "thread_id": str(message.get("thread_id") or "")})

    def add_message_page(self, items: list, chat_id: str, *, thread: bool, chat: dict | None) -> list[str]:
        founded = []
        for message in items:
            if not isinstance(message, dict):
                raise ValueError("feishu message page holds a non-object item")
            self.add_message(message, chat_id, thread=thread, chat=chat)
            thread_id = str(message.get("thread_id") or "")
            if thread_id and thread_id not in founded:
                founded.append(thread_id)
        return founded

    # -- chats -------------------------------------------------------------

    def list_chats(self, types: str) -> dict[str, dict]:
        path = "list:chats"
        cached = self.cursor(path)
        if cached.get("types") != types:
            cached = {}

        def command(page_token):
            args = ["im", "+chat-list", "--types", types, "--page-size", str(_CHAT_PAGE_SIZE)]
            if page_token:
                args += ["--page-token", page_token]
            return args + ["--as", "user", "--format", "json"]

        chats = {
            str(chat["chat_id"]): chat
            for chat in cached.get("chats") or []
            if isinstance(chat, dict) and chat.get("chat_id")
        }
        token = cached.get("page_token", "")
        while True:
            try:
                items, next_token = _page(self.run_cli(command(token)), "chats")
            except Exception as exc:
                self.error("chat list", exc)
                self.record(path, {"types": types, "page_token": token, "chats": list(chats.values())}, str(exc))
                return chats
            self.stats.setdefault(path, {"discovered": 0, "imported": 0, "skipped": 0})
            self.stats[path]["discovered"] += len(items)
            wanted = set(types.split(","))
            for item in items:
                if not isinstance(item, dict) or not item.get("chat_id"):
                    continue
                mode = str(item.get("chat_mode") or ("p2p" if "p2p" in wanted and "group" not in wanted else "group"))
                if mode == "p2p" and "p2p" not in wanted:
                    continue
                if mode != "p2p" and "group" not in wanted:
                    continue
                chats[str(item["chat_id"])] = item
            self.record(path, {"types": types, "page_token": next_token, "chats": list(chats.values())})
            self.emit("chats")
            if not next_token:
                self.record(path, {})
                return chats
            token = next_token

    def scan_chat(self, chat_id: str, chat: dict) -> list[str]:
        path = f"chat:{chat_id}"
        state = self.cursor(path)
        threads = [str(value) for value in state.get("threads") or []]
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
        end = int(datetime.now(timezone.utc).timestamp())
        start = int(state.get("watermark") or 0)
        window = (int(pending["start"]), int(pending["end"])) if pending else (
            max(0, min(start - 60, end) if start else 0), end)

        def command(page_token):
            value = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "sort_type": "ByCreateTimeAsc",
                "page_size": _MESSAGE_PAGE_SIZE,
                "start_time": str(window[0]),
                "end_time": str(window[1]),
            }
            if page_token:
                value["page_token"] = page_token
            return ["api", "GET", "/open-apis/im/v1/messages", "--params", json.dumps(value), "--as", "user", "--format", "json"]

        def save(page_token: str, error: str | None) -> None:
            done = not error and not page_token
            next_state = {"threads": threads}
            if done:
                next_state["watermark"] = window[1]
            else:
                next_state["pending"] = {"start": window[0], "end": window[1], "page_token": page_token}
                next_state["watermark"] = state.get("watermark", 0)
            self.record(path, next_state, error)

        token = str((pending or {}).get("page_token") or "")
        self.emit(f"chat:{chat_id}")
        self.record(path, {**self.cursor(path), "threads": threads})
        while True:
            try:
                items, next_token = _page(self.run_cli(command(token)), "items")
                self.stats.setdefault(path, {"discovered": 0, "imported": 0, "skipped": 0})
                self.stats[path]["discovered"] += len(items)
                for thread_id in self.add_message_page(items, chat_id, thread=False, chat=chat):
                    if thread_id not in threads:
                        threads.append(thread_id)
            except Exception as exc:
                self.error(f"chat {chat_id}", exc)
                save(token, str(exc))
                return threads
            token = next_token
            if token:
                save(token, None)
                continue
            save("", None)
            return threads

    def scan_thread(self, thread_id: str, chat: dict | None) -> bool:
        path = f"thread:{thread_id}"

        def command(page_token):
            value = {
                "container_id_type": "thread",
                "container_id": thread_id,
                "sort_type": "ByCreateTimeAsc",
                "page_size": _MESSAGE_PAGE_SIZE,
            }
            if page_token:
                value["page_token"] = page_token
            return ["api", "GET", "/open-apis/im/v1/messages", "--params", json.dumps(value), "--as", "user", "--format", "json"]

        token = str(self.cursor(path).get("page_token") or "")
        self.emit(f"thread:{thread_id}")
        while True:
            try:
                items, next_token = _page(self.run_cli(command(token)), "items")
                self.stats.setdefault(path, {"discovered": 0, "imported": 0, "skipped": 0})
                self.stats[path]["discovered"] += len(items)
                self.add_message_page(items, thread_id, thread=True, chat=chat)
            except Exception as exc:
                self.error(f"thread {thread_id}", exc)
                self.record(path, {"page_token": token}, str(exc))
                return False
            token = next_token
            if not token:
                self.record(path, {})
                return True
            self.record(path, {"page_token": token})

    # -- documents ---------------------------------------------------------

    def list_documents(self) -> dict[str, dict]:
        path = "list:docs"
        candidates = {
            str(item["id"]): item
            for item in self.cursor(path).get("docs") or []
            if isinstance(item, dict) and item.get("id")
        }

        def command(page_token):
            args = ["drive", "+search", "--query", "", "--doc-types", "docx,wiki",
                    "--page-size", str(_DOCUMENT_PAGE_SIZE)]
            if page_token:
                args += ["--page-token", page_token]
            return args + ["--as", "user", "--format", "json"]

        token = str(self.cursor(path).get("page_token") or "")
        while True:
            try:
                results, next_token = _page(self.run_cli(command(token)), "results")
            except Exception as exc:
                self.error("document list", exc)
                self.record(path, {"page_token": token, "docs": list(candidates.values())}, str(exc))
                return candidates
            for result in results:
                if not isinstance(result, dict):
                    raise TypeError("feishu search page holds a non-object item")
                doc_id = _doc_id(result)
                if not doc_id:
                    raise ValueError("feishu search result without document token")
                meta = _object(result.get("result_meta"))
                seen = {
                    "id": doc_id,
                    "url": meta.get("url") or "",
                    "title": _doc_title(result),
                    "update_time_iso": _text(meta.get("update_time_iso")),
                    "create_time_iso": _text(meta.get("create_time_iso")),
                }
                current = candidates.get(doc_id)
                if isinstance(current, dict):
                    # Every fresh search sighting refreshes title/url/time.
                    for key, value in seen.items():
                        if value or key not in current:
                            current[key] = value
                else:
                    candidates[doc_id] = dict(seen)
            self.record(path, {"page_token": next_token, "docs": list(candidates.values()),
                               "complete": not next_token})
            if not next_token:
                return candidates
            token = next_token

    def fetch_document(self, candidate: dict) -> str:
        """Fetch one document and store only new revisions/content hashes."""

        result = _data(self.run_cli(
            ["docs", "+fetch", "--doc", candidate.get("url") or candidate["id"],
             "--doc-format", "markdown", "--as", "user", "--format", "json"]))
        document = result.get("document")
        if not isinstance(document, dict):
            raise ValueError("feishu fetch returned no document object")
        canonical = str(document.get("document_id") or candidate["id"])
        content = document.get("content")
        if not isinstance(content, str):
            raise ValueError("feishu fetch returned no markdown content")
        path = f"doc:{canonical}"
        revision = str(document.get("revision_id") or "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        title = str(document.get("title") or "") or candidate.get("title") or ""
        state = self.cursor(path)
        self.stats.setdefault(path, {"discovered": 0, "imported": 0, "skipped": 0})
        self.stats[path]["discovered"] = 1
        if state.get("revision") == revision and state.get("hash") == digest:
            self.counts["skipped"] += 1
            self.stats[path]["skipped"] = 1
            self.record(path, state)
            return canonical
        text = content
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        if title and first_line.lstrip("# ").strip() != title.strip():
            text = f"# {title}\n\n{content}"
        chunks = _chunks(text)
        occurred_at = (_iso_text(document.get("update_time_iso"))
                       or _iso_text(candidate.get("update_time_iso"))
                       or _iso_text(candidate.get("create_time_iso"))
                       or _iso_text(candidate.get("occurred_at"))
                       or _iso(document.get("update_time"))
                       or _iso(document.get("update_time_ms"), millis=True))
        if occurred_at:
            candidate["occurred_at"] = occurred_at
        imported = skipped = 0
        for index, body in enumerate(chunks):
            changed = self.store.ingest(self.event(
                root=f"doc:{canonical}", version=f"{revision}:{digest[:16]}", chunk=index,
                body=body, session=f"doc:{canonical}", occurred_at=occurred_at,
                metadata={
                    "source_url": str(document.get("url") or candidate.get("url") or ""),
                    "document_id": canonical,
                    "alias_token": candidate["id"],
                    "wiki_node": candidate["id"] != canonical,
                    "title": title,
                    "revision_id": revision,
                    "content_hash": digest,
                    "doc_update_time": document.get("update_time"),
                },
            ), reference_episode=True)
            if not isinstance(changed, dict):
                raise ValueError("Store.ingest returned no result object")
            if changed.get("duplicate"):
                skipped += 1
                self.counts["skipped"] += 1
            else:
                imported += 1
                self.counts["imported"] += 1
        self.stats[path].update({"imported": imported, "skipped": skipped})
        self.record(path, {"revision": revision, "hash": digest, "title": title,
                           "url": str(document.get("url") or "")})
        return canonical


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _iso_text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _doc_id(result: dict) -> str | None:
    meta = _object(result.get("result_meta"))
    for key in ("token", "doc_token"):
        if meta.get(key):
            return str(meta[key])
    match = re.search(r"/(?:docx|wiki|docs)/([A-Za-z0-9]+)", str(meta.get("url") or ""))
    return match.group(1) if match else None


def _doc_title(result: dict) -> str:
    for key in ("title", "title_highlighted", "name"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return _TAG_RE.sub("", value).strip()
    return ""


def _split_long(chunk: str) -> list[str]:
    if len(chunk) <= _MAX_BODY:
        return [chunk]
    return [chunk[start:start + _MAX_BODY] for start in range(0, len(chunk), _MAX_BODY)]


def _chunks(text: str) -> list[str]:
    helpers = [chunk for _index, _heading, chunk in _markdown_chunks(text, limit=_MAX_BODY)]
    chunks = [piece for chunk in helpers for piece in _split_long(chunk)]
    return chunks or ["[empty document]"]


def _scan_document(importer: _Importer, candidate: dict) -> None:
    alias_path = f"doc:{candidate['id']}"
    try:
        canonical = importer.fetch_document(candidate)
        if canonical != candidate["id"]:
            importer.record(alias_path, {**importer.cursor(alias_path), "canonical_id": canonical})
    except Exception as exc:
        importer.error(f"document {candidate['id']}", exc)
        importer.record(alias_path, importer.cursor(alias_path), str(exc))


def import_history(store, run_cli, brand, account_id, sources,
                   progress=None, sensitivity="private") -> dict:
    """Import the selected read-only history; returns imported/skipped/errors."""

    if brand not in ("feishu", "lark"):
        raise ValueError(f"unknown brand: {brand!r}")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must be a non-empty safe account fingerprint")
    if sensitivity not in ("private", "normal"):
        raise ValueError(f"unknown sensitivity: {sensitivity!r}")
    selected = _normalize_sources(sources)
    importer = _Importer(store, run_cli, brand, account_id, sensitivity, progress)
    importer.emit("start")
    if not selected:
        importer.emit("done")
        return {**importer.counts, "coverage": _coverage(selected)}

    chat_sources = [value for value in selected if value in ("private_chats", "group_chats")]
    if chat_sources:
        types = ",".join("p2p" if value == "private_chats" else "group" for value in chat_sources)
        chats = importer.list_chats(types)
        for chat_id, chat in chats.items():
            for thread_id in importer.scan_chat(chat_id, chat):
                importer.scan_thread(thread_id, {**chat, "chat_id": chat_id})
    if "documents" in selected:
        importer.emit("documents")
        for candidate in importer.list_documents().values():
            importer.emit(f"doc:{candidate['id']}")
            _scan_document(importer, candidate)
    importer.emit("done")
    return {**importer.counts, "coverage": _coverage(selected)}


def _coverage(selected: list[str]) -> dict:
    return {
        "sources": list(selected),
        "messages": "create_time watermark; each run covers a fixed pending window",
        "threads": "all listed chats; every discovered thread paged to the end",
        "late": "edits and recalls of old messages visible only on a full rescan",
        "documents": "docx and wiki revisions via content hash",
    }
