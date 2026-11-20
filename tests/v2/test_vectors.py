"""Opt-in vector retrieval through the actual CLI, local HTTP provider and SQLite."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
        vector = [0.0, 2.0] if text == QUERY or '"timeout"' in text else [1.0, 0.0]
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
        expanded = service.post(owner, "/v2/evidence/expand", {"handle": closest["handle"]})
        self.assertEqual(json.loads(expanded["text"]), events[1]["checks"])
        texts = self.recorded()
        self.assertIn(events[0]["text"], texts)
        self.assertTrue(any('"timeout"' in value for value in texts))
        self.assertNotIn(PRIVATE_TEXT, texts)

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
