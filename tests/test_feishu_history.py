"""Real Store regressions against synthetic official Lark CLI envelopes."""

import itertools
import json
import sqlite3

import pytest

from bmem.feishu_history import import_history, required_scopes
from bmem.store import Store


P2P = "chat-private"
GROUP = "chat-group"
SOURCES = ("private_chats", "group_chats", "documents")
COMBINATIONS = [list(items) for count in range(4) for items in itertools.combinations(SOURCES, count)]
SCOPES = {
    "private_chats": {"im:chat:read", "im:message:readonly", "im:message.p2p_msg:get_as_user"},
    "group_chats": {"im:chat:read", "im:message:readonly", "im:message.group_msg:get_as_user"},
    "documents": {"search:docs:read", "docx:document:readonly"},
}


def message(identifier, chat=P2P, text="私聊记录山楂糕", thread=None):
    item = {
        "message_id": identifier, "chat_id": chat, "msg_type": "text",
        "create_time": "1762900000000", "update_time": "1762900000000",
        "sender": {"id": "ou_synthetic", "sender_type": "user"},
        "body": {"content": json.dumps({"text": text})},
    }
    if thread:
        item["thread_id"] = thread
    return item


def document_result(identifier):
    return {"title": "合成文档", "result_meta": {
        "token": identifier, "url": f"https://example.feishu.cn/docx/{identifier}",
        "doc_types": ["DOCX"],
    }}


class Protocol:
    """Canned remote pages and failures, without importing any business logic."""

    def __init__(self):
        self.chat_pages = [[{"chat_id": P2P, "chat_mode": "p2p"},
                            {"chat_id": GROUP, "chat_mode": "group"}]]
        self.messages = {("chat", P2P): [[message("private-1")]],
                         ("chat", GROUP): [[message("group-1", GROUP, "群聊记录桂圆冻")]]}
        self.doc_pages = [[document_result("doc1")]]
        self.documents = {"doc1": {"document_id": "doc1", "revision_id": 1,
                                   "content": "文档正文紫藤花"}}
        self.calls = []
        self.failures = {}
        self.final_token = False

    def __call__(self, args):
        self.calls.append(list(args))
        assert args[args.index("--as") + 1] == "user"
        assert args[args.index("--format") + 1] == "json"
        assert "--page-all" not in args
        token = args[args.index("--page-token") + 1] if "--page-token" in args else ""
        if args[:2] == ["im", "+chat-list"]:
            key = ("chats", "", token)
            pages, field = self.chat_pages, "chats"
        elif args[:3] == ["api", "GET", "/open-apis/im/v1/messages"]:
            params = json.loads(args[args.index("--params") + 1])
            kind, identifier = params["container_id_type"], params["container_id"]
            token = params.get("page_token", "")
            key = (kind, identifier, token)
            assert params["sort_type"] == "ByCreateTimeAsc"
            assert params["page_size"] == 50
            if kind == "thread":
                assert "start_time" not in params and "end_time" not in params
            else:
                assert "start_time" in params and "end_time" in params
            pages, field = self.messages.get((kind, identifier), [[]]), "items"
        elif args[:2] == ["drive", "+search"]:
            key = ("documents", "", token)
            pages, field = self.doc_pages, "results"
        elif args[:2] == ["docs", "+fetch"]:
            identifier = args[args.index("--doc") + 1].rsplit("/", 1)[-1]
            self.fail_once(("fetch", identifier, ""))
            return self.ok({"document": self.documents[identifier]})
        else:
            raise AssertionError(f"unexpected command {args}")
        self.fail_once(key)
        index = int(token.removeprefix("page-")) if token else 0
        more = index + 1 < len(pages)
        next_token = f"page-{index + 1}" if more else ("stale" if self.final_token else "")
        return self.ok({field: pages[index], "has_more": more, "page_token": next_token})

    def fail_once(self, key):
        if self.failures.get(key, 0):
            self.failures[key] -= 1
            raise RuntimeError("synthetic CLI read failed")

    @staticmethod
    def ok(data):
        return {"ok": True, "identity": "user", "data": data}

    def api_params(self):
        return [json.loads(args[args.index("--params") + 1]) for args in self.calls if args[0] == "api"]


def events(store):
    with sqlite3.connect(store.db_path) as connection:
        identifiers = [row[0] for row in connection.execute("SELECT event_id FROM events ORDER BY rowid")]
    return [store.get_event(identifier) for identifier in identifiers]


def run(store, protocol, sources, **kwargs):
    return import_history(store, protocol, kwargs.pop("brand", "feishu"),
                          kwargs.pop("account_id", "account-a"), sources, **kwargs)


def test_document_heading_does_not_create_an_empty_reference(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.documents["doc1"]["title"] = "合成文档"
    protocol.documents["doc1"]["content"] = "# 合成文档\n\n文档正文紫藤花"
    result = run(store, protocol, ["documents"])
    assert result["imported"] == 1
    assert len(events(store)) == 1
    assert "紫藤花" in events(store)[0]["body"]


def test_empty_history_accepts_absent_or_null_optional_lists(tmp_path):
    def empty_response(args):
        data = {"has_more": False}
        if args[:2] == ["drive", "+search"]:
            data["results"] = None
        return {"ok": True, "identity": "user", "data": data}

    result = import_history(Store(tmp_path), empty_response, "feishu", "empty-account", list(SOURCES))
    assert result["errors"] == 0
    assert result["imported"] == 0


@pytest.mark.parametrize("sources", COMBINATIONS)
def test_selected_sources_and_exact_read_scopes(tmp_path, sources):
    store, protocol = Store(tmp_path), Protocol()
    expected = {"offline_access"}
    for source in sources:
        expected.update(SCOPES[source])
    assert required_scopes(sources) == expected
    result = run(store, protocol, sources)
    assert result["errors"] == 0
    assert result["imported"] == len(sources)
    rows = events(store)
    assert len(rows) == len(sources)
    if not sources:
        assert protocol.calls == []
    for row in rows:
        assert row["harness"] == "feishu" and row["source_type"] == "external"
        assert row["sensitivity"] == "private"
        assert row["metadata"]["origin"] == "feishu-remote"
        assert row["metadata"]["history_import"] is True
    api_ids = {params["container_id"] for params in protocol.api_params()}
    assert (P2P in api_ids) == ("private_chats" in sources)
    assert (GROUP in api_ids) == ("group_chats" in sources)
    assert any(args[:2] == ["drive", "+search"] for args in protocol.calls) == ("documents" in sources)
    assert all(item["kind"] == "episode" and item["trust_tier"] == "T4"
               for item in store.list_memories())
    if "private_chats" in sources:
        assert store.search("山楂糕", target="local")
        assert store.search("山楂糕", target="cloud") == []


def test_replay_namespace_and_explicit_cloud_permission(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    assert run(store, protocol, ["private_chats"])["imported"] == 1
    assert run(store, protocol, ["private_chats"])["imported"] == 0
    run(store, protocol, ["private_chats"], brand="lark")
    run(store, protocol, ["private_chats"], account_id="account-b", sensitivity="normal")
    rows = events(store)
    assert len(rows) == 3
    assert len({row["operation_id"] for row in rows}) == 3
    assert len({row["metadata"]["root_source_id"] for row in rows}) == 3
    assert len(store.search("山楂糕", target="cloud")) == 1


def test_all_pages_and_has_more_terminate_with_stale_final_token(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.chat_pages = [[{"chat_id": P2P, "chat_mode": "p2p"}],
                           [{"chat_id": GROUP, "chat_mode": "group"}]]
    protocol.messages[("chat", P2P)] = [[message(f"page-message-{index}")] for index in range(11)]
    protocol.final_token = True
    result = run(store, protocol, ["private_chats"])
    assert result["errors"] == 0
    assert len(events(store)) == 11
    assert len(protocol.api_params()) == 11
    assert len([args for args in protocol.calls if args[:2] == ["im", "+chat-list"]]) == 2


def test_failed_message_page_resumes_fixed_window_then_advances(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.messages[("chat", P2P)] = [[message("first")], [message("second")]]
    protocol.failures[("chat", P2P, "page-1")] = 1
    assert run(store, protocol, ["private_chats"])["errors"] == 1
    first = protocol.api_params()[0]
    assert len(events(store)) == 1
    protocol.calls.clear()
    assert run(store, protocol, ["private_chats"])["errors"] == 0
    resumed = protocol.api_params()[0]
    assert resumed["page_token"] == "page-1"
    assert (resumed["start_time"], resumed["end_time"]) == (first["start_time"], first["end_time"])
    assert len(events(store)) == 2
    protocol.calls.clear()
    run(store, protocol, ["private_chats"])
    following = protocol.api_params()[0]
    assert "page_token" not in following
    assert 0 < int(following["start_time"]) <= int(first["end_time"])
    assert len(events(store)) == 2


def test_known_threads_paginate_and_rescan_without_duplicate_root(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    root = message("root", thread="thread-1")
    protocol.messages[("chat", P2P)] = [[root]]
    pages = [[root]] + [[message(f"reply-{index}", thread="thread-1")] for index in range(10)]
    protocol.messages[("thread", "thread-1")] = pages
    assert run(store, protocol, ["private_chats"])["errors"] == 0
    assert len(events(store)) == 11
    protocol.messages[("chat", P2P)] = [[]]
    pages.append([message("new-reply", thread="thread-1")])
    assert run(store, protocol, ["private_chats"])["errors"] == 0
    assert len(events(store)) == 12


def test_reducing_sources_stops_cached_group_and_thread_reads(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.messages[("chat", GROUP)] = [[message("group-root", GROUP, thread="group-thread")]]
    protocol.messages[("thread", "group-thread")] = [[message("group-reply", GROUP)]]
    run(store, protocol, ["private_chats", "group_chats"])
    protocol.calls.clear()
    assert run(store, protocol, ["private_chats"])["errors"] == 0
    assert {params["container_id"] for params in protocol.api_params()} == {P2P}


def test_document_paging_retry_revisions_hashes_and_shared_chunk_root(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.doc_pages = [[document_result("doc1")], [document_result("doc2")]]
    protocol.documents["doc2"] = {"document_id": "doc2", "revision_id": 1, "content": "另一文档桂花糕"}
    protocol.failures[("fetch", "doc2", "")] = 1
    assert run(store, protocol, ["documents"])["errors"] == 1
    assert len(events(store)) == 1
    assert run(store, protocol, ["documents"])["imported"] == 1
    assert len(events(store)) == 2
    assert run(store, protocol, ["documents"])["imported"] == 0
    protocol.documents["doc1"].update(revision_id=2, content="修订后的文档紫藤花")
    assert run(store, protocol, ["documents"])["imported"] == 1
    protocol.documents["doc1"]["content"] += "正文再次更改"
    assert run(store, protocol, ["documents"])["imported"] == 1
    protocol.documents["doc1"]["content"] = ("长段内容" * 200 + "\n") * 150
    before = len(events(store))
    assert run(store, protocol, ["documents"])["errors"] == 0
    rows = [row for row in events(store) if row["metadata"].get("document_id") == "doc1"]
    assert len(events(store)) - before > 1
    assert all(len(row["body"]) <= 100_000 for row in rows)
    assert len({row["metadata"]["root_source_id"] for row in rows}) == 1


def test_document_replay_after_event_commit_before_cursor_save_counts_duplicates(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    assert run(store, protocol, ["documents"])["imported"] == 1
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DELETE FROM import_cursors WHERE path='doc:doc1'")
    result = run(store, protocol, ["documents"])
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert len(events(store)) == 1


def test_document_preserves_official_time_and_complete_long_paragraph(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    protocol.doc_pages[0][0]["result_meta"]["update_time_iso"] = "2025-11-11T22:26:40Z"
    assert run(store, protocol, ["documents"])["errors"] == 0
    assert events(store)[0]["occurred_at"].startswith("2025-11-11T22:26:40")
    content = "longparagraph" * 9000
    protocol.documents["doc1"]["content"] = content
    result = run(store, protocol, ["documents"])
    assert result["errors"] == 0
    rows = events(store)
    assert all(len(row["body"]) <= 100_000 for row in rows)
    assert content in "".join(row["body"] for row in rows)
    assert all(row["occurred_at"].startswith("2025-11-11T22:26:40") for row in rows)


def test_wiki_alias_error_clears_on_recovery_and_tracks_current_failures(tmp_path):
    store, protocol = Store(tmp_path), Protocol()
    alias = document_result("wiki1")
    alias["result_meta"]["url"] = "https://example.feishu.cn/wiki/wiki1"
    protocol.doc_pages = [[alias]]
    protocol.documents["wiki1"] = protocol.documents.pop("doc1")

    def alias_cursor():
        return next(row for row in store.records("import_cursors") if row["path"] == "doc:wiki1")

    protocol.failures[("fetch", "wiki1", "")] = 1
    assert run(store, protocol, ["documents"])["errors"] == 1
    failed = alias_cursor()
    assert failed["error"]

    recovered = run(store, protocol, ["documents"])
    assert recovered["errors"] == 0 and recovered["imported"] == 1
    cursor = alias_cursor()
    assert cursor["source"] == failed["source"]
    assert cursor["error"] is None
    assert json.loads(cursor["last_cursor"])["canonical_id"] == "doc1"

    protocol.failures[("fetch", "wiki1", "")] = 1
    assert run(store, protocol, ["documents"])["errors"] == 1
    assert alias_cursor()["error"]
    assert json.loads(alias_cursor()["last_cursor"])["canonical_id"] == "doc1"
    replayed = run(store, protocol, ["documents"])
    assert replayed["errors"] == 0 and replayed["imported"] == 0
    assert alias_cursor()["error"] is None
    assert len(events(store)) == 1
