"""Governance preview footprint binding through the real service.

Previews bind only the target's own state (grant rows, approved model
destination, forget target watermark/revision). Background sync, unrelated
governance traffic, task/client registrations and declaration commits must
not invalidate a pending preview; genuine target changes must still return
409 revision_changed, and a successful confirm consumes its preview while
same-key retries replay the stored receipt.

Set BMEM_V2_TEST_BIN to the compiled bmem binary and run
python3 -m unittest discover -s tests/v2 -p test_governance_preview.py -v.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Thread
import unittest

try:
    import test_integration as support
except ImportError:  # invoked as tests.v2.test_governance_preview from the repo root
    from tests.v2 import test_integration as support


class FixtureModel(BaseHTTPRequestHandler):
    """Loopback ollama chat + embeddings fixture; no external dependency."""

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.rfile.read(length)
        if self.path == "/api/embed":
            payload = {"embeddings": [[1.0, 0.0]]}
        elif self.path == "/api/chat":
            message = {"content": json.dumps({"candidates": []})}
            payload = {"done": True, "done_reason": "stop", "message": message}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class GovernancePreviewTest(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()
        self.service.start()
        self.owner, _ = self.service.web()
        self._key_serial = 0

    def key(self, label):
        self._key_serial += 1
        return f"{label}-{self._key_serial}"

    def start_fixture_model(self):
        model = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()

        def stop_model():
            model.shutdown()
            thread.join(timeout=5)
            model.server_close()

        self.addCleanup(stop_model)
        return f"http://127.0.0.1:{model.server_port}"

    def bind_extract_model(self, name="grant-fixture"):
        endpoint = self.start_fixture_model()
        self.service.cli(
            "model", "add", name, "--protocol", "ollama",
            "--endpoint", endpoint, "--model-id", name,
        )
        self.service.cli("model", "test", name)
        self.service.cli("model", "use", name, "--for", "extract")
        return endpoint

    def register_source(self, account, capture=True):
        source = self.service.post(self.owner, "/v2/sources", {
            "type": "fixture", "account": account,
            "categories": ["user_message", "tool_result"],
        }, self.key("source"))["source_id"]
        if capture:
            # stop/resume confirm without a preview: semantics unchanged.
            self.service.post(self.owner, "/v2/operations", {
                "kind": "resume_capture", "source_id": source,
            }, self.key("capture"))
        return source

    def import_events(self, source_id, cursor, next_cursor, objects, tag="背景同步事件"):
        page = {
            "source_id": source_id, "cursor": cursor, "next_cursor": next_cursor,
            "events": [
                {
                    "native_object": obj, "native_version": "1",
                    "actor_kind": "user_message", "at": "2026-11-20T10:00:00+08:00",
                    "text": f"{tag} {obj}",
                }
                for obj in objects
            ],
        }
        return self.service.post(self.owner, "/v2/events", page, self.key("page"))

    def preview(self, payload):
        return self.service.post(
            self.owner, "/v2/operations/preview", payload, self.key("preview")
        )["preview_id"]

    def confirm(self, payload, preview_id, key=None):
        return self.owner.request(
            "/v2/operations", "POST", {**payload, "preview_id": preview_id},
            key=key or self.key("confirm"),
        )

    def extract_grant(self, source_id, allow=True):
        return {
            "kind": "change_grant", "source_id": source_id,
            "purpose": "extract", "allow": allow,
        }

    def declare(self, original, scope=None, share="owner"):
        return self.service.post(self.owner, "/v2/statements", {
            "original": original, "scope": scope or {"kind": "global"}, "share": share,
        }, self.key("declare"))

    def assert_conflict(self, status, payload, code="revision_changed"):
        self.assertEqual((status, payload.get("code")), (409, code), payload)

    def governance_db(self):
        for path in sorted(self.service.root.rglob("*.sqlite3")):
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            if "governance_previews" in tables:
                return path
        self.fail("governance database not found under the isolated root")

    # (a) The reported bug: a background-style import advances the SAME
    # source's cursor between preview and confirm of an extract grant.
    def test_extract_grant_confirm_survives_same_source_cursor_advance(self):
        self.bind_extract_model()
        source = self.register_source("active-collect")
        grant = self.extract_grant(source)
        preview_id = self.preview(grant)
        receipt = self.import_events(source, None, "sync-1", ["msg-1", "msg-2"])
        self.assertEqual(receipt["cursor"], "sync-1")
        status, confirmed = self.confirm(grant, preview_id)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["effective"], True)
        [registered] = self.service.cli("connector", "list")["sources"]
        self.assertTrue(registered["extract_enabled"])
        self.assertEqual(registered["cursor"], "sync-1")
        # Collection continues after the grant under the moved cursor.
        self.import_events(source, "sync-1", "sync-2", ["msg-3"])

    # (b) Unrelated activity must not invalidate a pending grant preview.
    def test_unrelated_activity_does_not_invalidate_grant_preview(self):
        self.bind_extract_model()
        target = self.register_source("grant-target")
        other = self.register_source("grant-other")
        grant = self.extract_grant(target)
        preview_id = self.preview(grant)

        # Unrelated source grant change (also grows governance_operations).
        self.service.operation(self.owner, self.extract_grant(other), self.key("other-grant"))
        # Unrelated cursor advance on the other source.
        self.import_events(other, None, "other-1", ["other-msg"])
        # Task creation and client registration.
        self.service.post(self.owner, "/v2/tasks", {
            "anchor": "unrelated@1", "project": "unrelated",
        }, self.key("task"))
        self.service.post(self.owner, "/v2/clients", {
            "host": "fixture-unrelated", "destination": "fixture-local",
            "capabilities": {"load_context": True},
        }, self.key("client"))
        # Declaration commit advances the global change_seq.
        self.declare(support.FIXTURE["initial"], share="authorized_assistants")

        status, confirmed = self.confirm(grant, preview_id)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["effective"], True)

    # (c) A concurrent change of the SAME target grant still conflicts.
    def test_concurrent_same_target_grant_change_requires_fresh_preview(self):
        self.bind_extract_model()
        source = self.register_source("contended")
        grant = self.extract_grant(source)
        stale = self.preview(grant)
        # Another entry grants the same source first.
        self.service.operation(self.owner, grant, self.key("winner"))
        self.assert_conflict(*self.confirm(grant, stale))
        fresh = self.preview(grant)
        status, confirmed = self.confirm(grant, fresh)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["effective"], True)

        # Same contention in the revoke direction.
        revoke = self.extract_grant(source, allow=False)
        stale = self.preview(revoke)
        self.service.operation(self.owner, revoke, self.key("revoke-winner"))
        self.assert_conflict(*self.confirm(revoke, stale))
        fresh = self.preview(revoke)
        status, confirmed = self.confirm(revoke, fresh)
        self.assertEqual(status, 202, confirmed)
        [registered] = self.service.cli("connector", "list")["sources"]
        self.assertFalse(registered["extract_enabled"])

    # (c) A model rebind between preview and confirm must still 409: the
    # destination being approved is part of the bound footprint.
    def test_model_rebind_between_preview_and_confirm_conflicts(self):
        self.bind_extract_model("rebind-fixture")
        source = self.register_source("rebind-target")
        grant = self.extract_grant(source)
        stale = self.preview(grant)
        second = self.start_fixture_model()
        self.service.cli(
            "model", "configure", "rebind-fixture", "--endpoint", second,
        )
        self.service.cli("model", "test", "rebind-fixture")
        self.assert_conflict(*self.confirm(grant, stale))
        fresh_preview = self.preview(grant)
        status, confirmed = self.confirm(grant, fresh_preview)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["effective"], True)
        [registered] = self.service.cli("connector", "list")["sources"]
        self.assertFalse(registered["extract_reauthorization_required"])

    # (d) Forget statement binds the target's own revision state only.
    def test_forget_statement_binds_target_revision_only(self):
        case = support.FIXTURE
        first = self.declare(case["initial"], share="authorized_assistants")
        target = first["statement_id"]
        forget = {"kind": "forget", "target": {"statement_id": target}}
        preview_id = self.preview(forget)
        # Unrelated declaration: advances change_seq, touches no target fact.
        unrelated = self.declare("这是一条与报告格式无关的声明")
        self.assertEqual(unrelated["dimensions"], [])
        status, confirmed = self.confirm(forget, preview_id)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["effective"], True)
        self.assertEqual(self.service.cli("statement", "list")["statements"], [])

        # A superseding correction of the target's fact is a genuine target
        # revision change: re-preview required.
        second = self.declare(case["initial"], share="authorized_assistants")
        target = second["statement_id"]
        forget = {"kind": "forget", "target": {"statement_id": target}}
        stale = self.preview(forget)
        self.declare(case["correction"], share="authorized_assistants")
        self.assert_conflict(*self.confirm(forget, stale))
        fresh = self.preview(forget)
        status, confirmed = self.confirm(forget, fresh)
        self.assertEqual(status, 202, confirmed)
        current = {
            item["key"]: item["value"]
            for item in self.service.cli("statement", "list")["statements"]
        }
        self.assertEqual(current, {case["opening_key"]: case["corrected_opening"]})

        # Revocation by another entry invalidates the pending preview.
        third = self.declare(case["initial"], share="authorized_assistants")
        target = third["statement_id"]
        forget = {"kind": "forget", "target": {"statement_id": target}}
        stale = self.preview(forget)
        self.service.operation(self.owner, forget, self.key("other-forget"))
        self.assert_conflict(*self.confirm(forget, stale))

    # (e) Forget source binds the target's OWN watermark, not other sources.
    def test_forget_source_binds_own_watermark_only(self):
        target = self.register_source("forget-target")
        other = self.register_source("forget-other")
        tag = "目标来源专属资料"
        self.import_events(target, None, "t-1", ["keep-1", "keep-2"], tag=tag)
        self.import_events(other, None, "o-1", ["other-1"])
        forget = {"kind": "forget", "target": {"source_id": target}}
        preview_id = self.preview(forget)
        # Unrelated source events and a declaration commit do not conflict.
        self.import_events(other, "o-1", "o-2", ["other-2"])
        self.declare("所有助手的报告用中文，先列行动项")
        status, confirmed = self.confirm(forget, preview_id)
        self.assertEqual(status, 202, confirmed)
        self.assertEqual(confirmed["result"]["withdrawn_events"], 2)

        # New objects on the SAME source change the cleanup scope: 409.
        self.import_events(target, "t-1", "t-2", ["late-1"], tag=tag)
        stale = self.preview(forget)
        self.import_events(target, "t-2", "t-3", ["late-2"], tag=tag)
        self.assert_conflict(*self.confirm(forget, stale))
        fresh = self.preview(forget)
        status, confirmed = self.confirm(forget, fresh)
        self.assertEqual(status, 202, confirmed)
        self.assertEqual(confirmed["result"]["withdrawn_events"], 2)
        hits = self.service.post(self.owner, "/v2/search", {"query": tag})
        self.assertEqual(hits["results"], [])
        # The unrelated source keeps its own evidence.
        other_hits = self.service.post(self.owner, "/v2/search", {"query": "背景同步事件"})
        self.assertEqual(len(other_hits["results"]), 2)

    # (e, continued) A committed page may add objects WITHOUT moving the
    # cursor (next_cursor == cursor is a valid acceptance). The accepted-page
    # watermark must still invalidate the pending forget preview.
    def test_forget_source_same_cursor_page_requires_fresh_preview(self):
        source = self.register_source("same-cursor")
        tag = "同游标来源专属资料"
        self.import_events(source, None, "w-1", ["obj-a"], tag=tag)
        forget = {"kind": "forget", "target": {"source_id": source}}
        stale = self.preview(forget)
        receipt = self.import_events(source, "w-1", "w-1", ["obj-b"], tag=tag)
        self.assertEqual(receipt["cursor"], "w-1")
        self.assertEqual(receipt["events"][0]["disposition"], "accepted")
        self.assert_conflict(*self.confirm(forget, stale))
        # Even a duplicate-only committed page advances the watermark: the
        # preview binds the accepted-page scope, not just new objects.
        duplicate_preview = self.preview(forget)
        duplicate = self.import_events(source, "w-1", "w-1", ["obj-b"], tag=tag)
        self.assertEqual(duplicate["events"][0]["disposition"], "duplicate")
        self.assert_conflict(*self.confirm(forget, duplicate_preview))
        # An idempotent replay commits nothing and must NOT conflict.
        replay_key = self.key("replay-page")
        page = {
            "source_id": source, "cursor": "w-1", "next_cursor": "w-1",
            "events": [{
                "native_object": "obj-b", "native_version": "1",
                "actor_kind": "user_message", "at": "2026-11-20T10:00:00+08:00",
                "text": f"{tag} obj-b",
            }],
        }
        first = self.service.post(self.owner, "/v2/events", page, replay_key)
        replay_preview = self.preview(forget)
        again = self.service.post(self.owner, "/v2/events", page, replay_key)
        self.assertEqual(again, first)
        status, confirmed = self.confirm(forget, replay_preview)
        self.assertEqual(status, 202, confirmed)
        self.assertEqual(confirmed["result"]["withdrawn_events"], 2)
        hits = self.service.post(self.owner, "/v2/search", {"query": tag})
        self.assertEqual(hits["results"], [])

    # (f) Readiness is honest at PREVIEW time for extract/embed allow.
    def test_allow_preview_without_ready_model_is_model_not_ready(self):
        source = self.register_source("no-model")
        status, payload = self.owner.request(
            "/v2/operations/preview", "POST", self.extract_grant(source),
            key=self.key("preview"),
        )
        self.assertEqual((status, payload["code"]), (409, "model_not_ready"), payload)
        embed = {
            "kind": "change_grant", "source_id": source, "purpose": "embed", "allow": True,
        }
        status, payload = self.owner.request(
            "/v2/operations/preview", "POST", embed, key=self.key("preview"),
        )
        self.assertEqual((status, payload["code"]), (409, "model_not_ready"), payload)
        # Revocation and non-model purposes never require a model.
        self.preview(self.extract_grant(source, allow=False))
        client = self.service.post(self.owner, "/v2/clients", {
            "host": "fixture-no-model", "destination": "fixture-local",
        }, self.key("client"))["client_id"]
        self.preview({
            "kind": "change_grant", "source_id": source,
            "purpose": "task_delivery", "client_id": client, "allow": True,
        })

    def test_embed_allow_preview_requires_enabled_vector_search(self):
        endpoint = self.start_fixture_model()
        self.service.cli(
            "model", "add", "embed-fixture", "--protocol", "ollama",
            "--endpoint", endpoint, "--model-id", "embed-fixture",
        )
        self.service.cli("config", "set", "vector_search.enabled", "true")
        self.service.cli("model", "test", "embed-fixture", "--for", "embed")
        self.service.cli("model", "use", "embed-fixture", "--for", "embed")
        source = self.register_source("embed-target")
        embed = {
            "kind": "change_grant", "source_id": source, "purpose": "embed", "allow": True,
        }
        preview_id = self.preview(embed)
        # Disabling vector search unbinds the destination: the stale preview
        # conflicts and a fresh preview reports model_not_ready honestly.
        self.service.cli("config", "set", "vector_search.enabled", "false")
        self.assert_conflict(*self.confirm(embed, preview_id))
        status, payload = self.owner.request(
            "/v2/operations/preview", "POST", embed, key=self.key("preview"),
        )
        self.assertEqual((status, payload["code"]), (409, "model_not_ready"), payload)
        self.service.cli("config", "set", "vector_search.enabled", "true")
        fresh = self.preview(embed)
        status, confirmed = self.confirm(embed, fresh)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["result"]["embed_enabled"], True)

    # (g) Successful confirm consumes the preview; same-key retries replay
    # the stored receipt, and the CLI derives its confirm key from preview_id.
    def test_confirm_replay_returns_stored_receipt_and_consumes_preview(self):
        self.bind_extract_model()
        source = self.register_source("replay")
        grant = self.extract_grant(source)
        preview_id = self.preview(grant)
        key = self.key("replay-confirm")
        status, receipt = self.confirm(grant, preview_id, key=key)
        self.assertEqual(status, 202, receipt)
        self.assertIs(receipt["effective"], True)
        # The consumed preview row is gone, yet the replay still works
        # because it reads governance_operations.
        with sqlite3.connect(f"file:{self.governance_db()}?mode=ro", uri=True) as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM governance_previews WHERE id=?", (preview_id,)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        status, replayed = self.confirm(grant, preview_id, key=key)
        self.assertEqual((status, replayed), (202, receipt))
        # Same key with a different body remains a key conflict.
        self.assert_conflict(
            *self.confirm(self.extract_grant(source, allow=False), preview_id, key=key),
            code="key_conflict",
        )
        # CLI retry across separate invocations replays the original ticket.
        cli_preview = self.service.cli(
            "grant", "source", source, "--purpose", "extract",
            "--allow", "true", "--preview",
        )
        args = (
            "grant", "source", source, "--purpose", "extract",
            "--allow", "true", "--confirm", cli_preview["preview_id"],
        )
        first = self.service.cli(*args)
        self.assertIs(first["effective"], True)
        self.assertEqual(self.service.cli(*args), first)

    # Client-side grants bind the named client/task rows, not global lists.
    def test_client_grant_previews_bind_target_rows(self):
        def register_client(host):
            return self.service.post(self.owner, "/v2/clients", {
                "host": host, "destination": "fixture-local",
                "capabilities": {"load_context": True},
            }, self.key("client"))["client_id"]

        client = register_client("fixture-target-client")
        task = self.service.post(self.owner, "/v2/tasks", {
            "anchor": "grant@1", "project": "grant-project",
        }, self.key("task"))["task_id"]

        sharing = {"kind": "change_grant", "client_id": client, "share_statements": True}
        preview_id = self.preview(sharing)
        register_client("fixture-unrelated-client")
        self.service.post(self.owner, "/v2/tasks", {
            "anchor": "grant@2", "project": "grant-project",
        }, self.key("task"))
        self.declare("所有助手的报告用中文，先列行动项")
        status, confirmed = self.confirm(sharing, preview_id)
        self.assertEqual(status, 202, confirmed)

        # Concurrent flip of the SAME client's shared flag conflicts.
        revoke = {**sharing, "share_statements": False}
        stale = self.preview(revoke)
        self.service.operation(self.owner, revoke, self.key("share-winner"))
        self.assert_conflict(*self.confirm(revoke, stale))
        status, confirmed = self.confirm(revoke, self.preview(revoke))
        self.assertEqual(status, 202, confirmed)

        # Task access binds the named (task, client) row only.
        access = {
            "kind": "change_grant", "client_id": client, "task_id": task, "allow": True,
        }
        preview_id = self.preview(access)
        self.service.post(self.owner, "/v2/tasks", {
            "anchor": "grant@3", "project": "grant-project",
        }, self.key("task"))
        status, confirmed = self.confirm(access, preview_id)
        self.assertEqual(status, 202, confirmed)
        stale = self.preview(access)
        self.service.operation(self.owner, {**access, "allow": False}, self.key("access-winner"))
        self.assert_conflict(*self.confirm(access, stale))
        status, confirmed = self.confirm(access, self.preview(access))
        self.assertEqual(status, 202, confirmed)

    def test_task_delivery_preview_binds_recall_grant_row(self):
        source = self.register_source("delivery-target")
        client = self.service.post(self.owner, "/v2/clients", {
            "host": "fixture-delivery", "destination": "fixture-local",
            "capabilities": {"load_context": True},
        }, self.key("client"))["client_id"]
        grant = {
            "kind": "change_grant", "source_id": source,
            "purpose": "task_delivery", "client_id": client, "allow": True,
        }
        preview_id = self.preview(grant)
        # The source's own cursor moving does not change a delivery grant.
        self.import_events(source, None, "d-1", ["delivery-msg"])
        self.service.operation(
            self.owner, self.extract_grant(source, allow=False), self.key("unrelated")
        )
        status, confirmed = self.confirm(grant, preview_id)
        self.assertEqual(status, 202, confirmed)
        self.assertIs(confirmed["result"]["task_delivery"], True)
        # Concurrent revoke of the SAME (source, client) grant conflicts.
        stale = self.preview(grant)
        self.service.operation(self.owner, {**grant, "allow": False}, self.key("delivery-winner"))
        self.assert_conflict(*self.confirm(grant, stale))
        status, confirmed = self.confirm(grant, self.preview(grant))
        self.assertEqual(status, 202, confirmed)

    # stop/resume keep their preview-free confirm path (no new 409s).
    def test_stop_resume_capture_confirm_without_preview_unchanged(self):
        source = self.register_source("control", capture=False)
        for kind in ("resume_capture", "stop_capture", "resume_capture"):
            status, confirmed = self.owner.request(
                "/v2/operations", "POST", {"kind": kind, "source_id": source},
                key=self.key(kind),
            )
            self.assertEqual(status, 202, confirmed)
            self.assertIs(confirmed["effective"], True)
        self.import_events(source, None, "c-1", ["control-msg"])
        # The preview+confirm path still works for control operations.
        self.service.operation(
            self.owner, {"kind": "stop_capture", "source_id": source}, self.key("stop")
        )
        status, rejected = self.owner.request("/v2/events", "POST", {
            "source_id": source, "cursor": "c-1", "next_cursor": "c-2",
            "events": [{
                "native_object": "blocked", "native_version": "1",
                "actor_kind": "user_message", "at": "2026-11-20T10:00:00+08:00",
                "text": "停采后不得入库",
            }],
        }, key=self.key("blocked-page"))
        self.assertIn(status, (403, 409), rejected)


if __name__ == "__main__":
    unittest.main()
