"""Opt-in vector retrieval through the actual CLI, local HTTP provider and SQLite."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import struct
from threading import Lock, Thread
import time
import unittest

import test_integration as support

QUERY = "查找语义相似的结果"
PRIVATE_TEXT = "未授准发送的来源材料"


class EmbeddingFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/embed":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request.get("truncate") is not False or len(request["input"]) != 1:
            self.send_error(422)
            return
        text = request["input"][0]
        with self.server.lock:
            self.server.inputs.append(text)
        if text == "offline-query":
            self.send_error(503)
            return
        if text == "unrelated-400":
            self.send_error(400)
            return
        if request["model"] == "small-context" and len(text.encode()) > 8000:
            body = json.dumps({"error": "the input length exceeds the context length"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if text == "☠" or (request["model"] == "small-context" and len(text.encode()) > 2400):
            self.send_error(413)
            return
        with self.server.lock:
            self.server.accepted.append(text)
        vector = [0.0, 2.0] if text == QUERY or '"timeout"' in text or "尾段关键词" in text else [1.0, 0.0]
        if request["model"] == "fixture-1024":
            vector += [0.0] * 1022
        body = json.dumps({"embeddings": [vector]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class VectorTest(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingFixture)
        self.model.lock = Lock()
        self.model.inputs = []
        self.model.accepted = []
        thread = Thread(target=self.model.serve_forever, daemon=True)
        thread.start()

        def stop_model():
            self.model.shutdown()
            thread.join(timeout=5)
            self.model.server_close()

        self.addCleanup(stop_model)

    def recorded(self):
        with self.model.lock:
            return list(self.model.inputs)

    def capture(self, owner, case, account, events):
        service = self.service
        source = service.post(owner, "/v2/sources", {**case["source"], "account": account}, f"source-{account}")
        source_id = source["source_id"]
        service.operation(owner, {"kind": "resume_capture", "source_id": source_id}, f"capture-{account}")
        page = {**case["page"], "source_id": source_id, "events": events}
        service.post(owner, "/v2/events", page, f"page-{account}")
        return source_id

    def test_exact_ranking_beyond_4096_candidates(self):
        service = self.service
        service.start()
        owner, _ = service.web()
        case = support.fixture("source_page.json")
        event = case["page"]["events"][0]
        source = self.capture(owner, case, "large-index", [event])
        cursor = case["page"]["next_cursor"]
        for start in range(0, 4100, 100):
            page = [{**event, "native_object": f"note-{n}"} for n in range(start, start + 100)]
            next_cursor = f"batch-{start}"
            service.post(owner, "/v2/events", {
                "source_id": source, "cursor": cursor, "next_cursor": next_cursor, "events": page,
            }, next_cursor)
            cursor = next_cursor
        service.cli("model", "add", "fixture", "--protocol", "ollama", "--endpoint",
                    f"http://127.0.0.1:{self.model.server_port}", "--model-id", "fixture-1024")
        service.cli("config", "set", "vector_search.enabled", "true")
        service.cli("model", "test", "fixture", "--for", "embed")
        service.cli("model", "use", "fixture", "--for", "embed")
        service.operation(owner, {"kind": "change_grant", "source_id": source,
                                  "purpose": "embed", "allow": True}, "allow-large-index")
        # Load the fixture projection in bulk and make its last ID the only exact match.
        with sqlite3.connect(service.root / "data" / "bmem.sqlite3") as db:
            winner, = db.execute("SELECT max(event_id) FROM recall_events").fetchone()
            padding = bytes(4 * 1022)
            db.execute("""
                INSERT OR REPLACE INTO vector_projection(event_id,source_id,route,dimensions,vector,norm)
                SELECT r.event_id,r.source_id,g.fingerprint,1024,
                       CASE WHEN r.event_id=? THEN ? ELSE ? END,1
                FROM recall_events r JOIN ing_vector_routes g ON r.source_id=g.source_id
            """, (winner, struct.pack("<2f", 0, 1) + padding, struct.pack("<2f", 1, 0) + padding))
        result = service.cli("search", QUERY, "--mode", "vector")
        self.assertEqual(result["retrieval"], "vector")
        self.assertEqual(len(result["results"]), 20)
        self.assertEqual(result["results"][0]["event_id"], winner)
        self.assertEqual(result["results"][0]["score"], 1.0)

        # Keep stale and excluded high scores in the projection. Neither may
        # take a top-k slot from the remaining evidence.
        with sqlite3.connect(service.root / "data" / "bmem.sqlite3") as db:
            db.execute("UPDATE ing_event_versions SET active=0 WHERE event_id=?", (winner,))
            promoted = db.execute("SELECT event_id FROM recall_events ORDER BY event_id LIMIT 25").fetchall()
            db.executemany("UPDATE recall_events SET actor_kind='tool_result' WHERE event_id=?", promoted)
            db.executemany("UPDATE vector_projection SET vector=? WHERE event_id=?",
                           [(struct.pack("<2f", 0, 1) + padding, event_id) for event_id, in promoted])
        active = service.cli("search", QUERY, "--mode", "vector")["results"]
        self.assertEqual(len(active), 20)
        self.assertNotIn(winner, [item["event_id"] for item in active])
        filtered = service.post(owner, "/v2/search", {"mode": "vector", "query": QUERY,
                                                      "exclude": ["tool_result"]})
        self.assertEqual(filtered["retrieval"], "vector")
        self.assertEqual(len(filtered["results"]), 20)
        self.assertTrue(all(item["actor_kind"] == "user_message" and item["score"] == 0
                            for item in filtered["results"]))

    def test_long_events_are_complete_and_a_rejected_event_cannot_block_the_queue(self):
        service = self.service
        service.start()
        owner, _ = service.web()
        case = support.fixture("source_page.json")
        event = case["page"]["events"][0]
        long_text = "中文🍳" * 1800 + "尾段关键词"
        short_text = "背景检索继续"
        source = self.capture(owner, case, "backlog", [
            {**event, "native_object": "poison", "text": "☠", "at": "2026-05-01T00:00:00Z"},
            {**event, "native_object": "oversize", "text": long_text, "at": "2026-05-02T00:00:00Z"},
            {**event, "native_object": "later", "text": short_text, "at": "2026-05-03T00:00:00Z"},
        ])
        service.cli("model", "add", "small", "--protocol", "ollama", "--endpoint",
                    f"http://127.0.0.1:{self.model.server_port}", "--model-id", "small-context")
        service.cli("config", "set", "vector_search.enabled", "true")
        service.cli("model", "test", "small", "--for", "embed")
        service.cli("model", "use", "small", "--for", "embed")
        grant = {"kind": "change_grant", "source_id": source, "purpose": "embed", "allow": True}
        service.operation(owner, grant, "enable-backfill")
        deadline = time.monotonic() + 12
        with sqlite3.connect(service.root / "data" / "bmem.sqlite3") as db:
            while db.execute("SELECT count(*) FROM vector_projection").fetchone()[0] != 2:
                self.assertLess(time.monotonic(), deadline, "long and subsequent events must both be indexed")
                time.sleep(0.05)
            self.assertEqual(db.execute("SELECT attempts,last_error FROM vector_work").fetchall(),
                             [(1, "embedding_input_too_long")])
            with self.model.lock:
                accepted = [text for text in self.model.accepted if text not in (
                    "A small bmem embedding probe.", short_text)]
            self.assertEqual("".join(accepted), long_text)
            self.assertEqual(self.recorded().count("☠"), 1)
            blob, = db.execute("""SELECT v.vector FROM vector_projection v
                JOIN ing_event_versions e USING(event_id) WHERE e.native_object='oversize'""").fetchone()
            self.assertTrue(all(component > 0 for component in struct.unpack("<2f", blob)))
        # Recreate the previous on-disk schema to exercise the production
        # migration with both a finished projection and outstanding work.
        service.cli("service", "stop")
        service.daemon.wait(timeout=10)
        service.stop()
        with sqlite3.connect(service.root / "data" / "bmem.sqlite3") as db:
            db.executescript("DROP TRIGGER enqueue_vector_evidence; DROP TRIGGER complete_vector_work; DROP TABLE vector_work;")
        service.start()
        owner, _ = service.web()
        with sqlite3.connect(service.root / "data" / "bmem.sqlite3") as db:
            deadline = time.monotonic() + 8
            while db.execute("SELECT attempts FROM vector_work").fetchone() != (1,):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.assertEqual(db.execute("SELECT count(*) FROM vector_projection").fetchone()[0], 2)
            backup = service.cli("backup", "create")
            db.execute("UPDATE vector_work SET available_at=0")
            db.commit()
            while db.execute("SELECT attempts FROM vector_work").fetchone() != (2,):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.assertEqual(service.cli("restore", "preview", backup["backup_id"])["state"], "verified")
            service.cli("config", "set", "vector_search.enabled", "false")
            db.execute("UPDATE vector_work SET available_at=unixepoch()-1")
            db.commit()
            offset = len(self.recorded())
            service.post(owner, "/v2/events", {"source_id": source, "cursor": case["page"]["next_cursor"],
                         "next_cursor": "later-event", "events": [{**event, "native_object": "fresh"}]}, "fresh")
            service.cli("config", "set", "vector_search.enabled", "true")
            deadline = time.monotonic() + 8
            while db.execute("SELECT count(*) FROM vector_projection").fetchone()[0] != 3:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.assertEqual(self.recorded()[offset:offset+2], ["☠", event["text"]])
            service.operation(owner, {**grant, "allow": False}, "revoke-backfill")
            self.assertEqual(db.execute("SELECT count(*) FROM vector_work").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM vector_projection").fetchone()[0], 0)

    def test_opt_in_projection_ranking_and_lexical_fallback(self):
        service = self.service
        service.start()
        owner, _ = service.web()
        case = support.fixture("source_page.json")
        events = case["page"]["events"]
        allowed_source = self.capture(owner, case, "vector-allowed", events)
        self.capture(owner, case, "vector-private", [{
            **events[0], "native_object": "private-note", "text": PRIVATE_TEXT,
        }])
        settings = service.config(owner)
        self.assertFalse(settings["vector_search"]["enabled"])
        service.cli(
            "model", "add", "vector-fixture", "--protocol", "ollama",
            "--endpoint", f"http://127.0.0.1:{self.model.server_port}",
            "--model-id", "vector-fixture",
        )
        self.assertEqual(service.cli("search", "复验")["retrieval"], "lexical")
        offline = service.cli("search", QUERY, "--mode", "vector")
        self.assertEqual(offline["retrieval"], "lexical")
        self.assertEqual(offline["fallback_reason"], "vector_not_ready")
        blocked = service.cli("model", "test", "vector-fixture", "--for", "embed", ok=False)
        self.assertEqual(blocked["code"], "revision_changed")
        self.assertEqual(self.recorded(), [])

        service.cli("config", "set", "vector_search.enabled", "true")
        probed = service.cli("model", "test", "vector-fixture", "--for", "embed")
        self.assertEqual(probed["dimension"], 2)
        self.assertEqual(len(self.recorded()), 1)
        service.cli("model", "use", "vector-fixture", "--for", "embed")
        self.assertEqual(service.cli("search", "复验")["retrieval"], "lexical")
        self.assertEqual(len(self.recorded()), 1)
        grant = {"kind": "change_grant", "source_id": allowed_source, "purpose": "embed", "allow": True}
        service.operation(owner, grant, "grant-vector-source")

        deadline = time.monotonic() + 10
        while True:
            found = service.cli("search", QUERY, "--mode", "vector")
            if found["retrieval"] == "vector" and len(found["results"]) == 2:
                break
            self.assertLess(time.monotonic(), deadline, found)
            time.sleep(0.05)
        closest, other = found["results"]
        self.assertEqual((closest["actor_kind"], closest["score"]), ("tool_result", 1.0))
        self.assertEqual((other["actor_kind"], other["score"]), ("user_message", 0.0))
        query = {"query": QUERY, "mode": "vector", "exclude": ["tool_result"]}
        filtered = service.post(owner, "/v2/search", query)
        self.assertEqual((filtered["retrieval"], filtered["results"]), ("vector", [other]))
        none = service.post(owner, "/v2/search", {**query, "exclude": ["user_message", "tool_result"]})
        self.assertEqual(none, {"retrieval": "vector", "results": []})
        scoped = service.post(owner, "/v2/search", {"query": QUERY, "mode": "vector", "task_id": "no-events"})
        self.assertEqual(scoped, none)
        expanded = service.post(owner, "/v2/evidence/expand", {"handle": closest["handle"]})
        self.assertEqual(json.loads(expanded["text"]), events[1]["checks"])
        texts = self.recorded()
        self.assertIn(events[0]["text"], texts)
        self.assertTrue(any('"timeout"' in value for value in texts))
        self.assertNotIn(PRIVATE_TEXT, texts)
        unavailable = service.cli("search", "offline-query", "--mode", "vector")
        self.assertEqual(unavailable["retrieval"], "lexical")
        self.assertEqual(unavailable["fallback_reason"], "vector_provider_unavailable")
        other_error = service.cli("search", "unrelated-400", "--mode", "vector")
        self.assertEqual(other_error["fallback_reason"], "vector_provider_unavailable")

        service.operation(owner, {**grant, "allow": False}, "revoke-vector-source")
        revoked = service.cli("search", QUERY, "--mode", "vector")
        self.assertEqual(revoked["retrieval"], "lexical")
        self.assertEqual(revoked["results"], [])
        service.cli("config", "set", "vector_search.enabled", "false")
        before = self.recorded()
        disabled = service.cli("search", QUERY, "--mode", "vector")
        self.assertEqual(disabled["retrieval"], "lexical")
        self.assertEqual(disabled["results"], [])
        self.assertEqual(self.recorded(), before)

        service.operation(owner, {
            "kind": "forget", "target": {"source_id": allowed_source},
        }, "forget-vector-source")
        status, gone = owner.request("/v2/evidence/expand", "POST", {"handle": closest["handle"]})
        self.assertEqual((status, gone["code"]), (404, "not_available"))
        self.assertEqual(self.recorded(), before)


if __name__ == "__main__":
    unittest.main()
