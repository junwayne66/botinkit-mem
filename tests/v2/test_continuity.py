"""Run continuity cases against the native service and a synthetic loopback LLM."""
from contextlib import closing
import http.server
import json
import sqlite3
import threading
import time
import unittest

import test_integration as support


class ExtractionContinuity(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()
        self.service.start()
        self.owner, _ = self.service.web()
        self.mode = "healthy"
        self.requests = []
        self.cursor = None
        fixture = self

        class Model(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.loads(request["messages"][-1]["content"])
                event = data["event_id"]
                fixture.requests.append(event)
                if fixture.mode == "disconnect":
                    self.close_connection = True
                    return
                status = {
                    "outage": 503, "rate_limit": 429, "unauthorized": 401,
                }.get(fixture.mode, 200)
                if fixture.mode == "shared_failure" and event == "probe":
                    status = 503
                invalid = fixture.mode == "shared_failure" and event != "probe"
                invalid = invalid or (fixture.mode == "bad_item" and "poison" in request["messages"][-1]["content"])
                body = json.dumps({"done": True, "done_reason": "stop", "message": {
                    "content": "invalid fixture candidate" if invalid else '{"candidates":[]}',
                }}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        endpoint = f"http://127.0.0.1:{provider.server_port}"
        self.service.cli("model", "add", "continuity", "--protocol", "ollama", "--endpoint", endpoint,
                         "--model-id", "fixture")
        self.service.cli("model", "test", "continuity")
        self.service.cli("model", "use", "continuity", "--for", "extract")
        self.source = self.service.post(self.owner, "/v2/sources", {"type":"test", "account":"continuity", "actors":["owner"]}, "source")["source_id"]
        self.service.operation(self.owner, {"kind":"resume_capture", "source_id":self.source}, "capture")
        self.service.operation(self.owner, {"kind":"change_grant", "source_id":self.source, "purpose":"extract", "allow":True}, "extract")
        self.requests.clear()

    def jobs(self):
        status, response = self.owner.request("/v2/jobs")
        self.assertEqual(status, 200, response)
        return response

    def until(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            view = self.jobs()
            if predicate(view):
                return view
            time.sleep(.08)
        self.fail(view)

    def add(self, key, *ids):
        receipt = self.service.post(self.owner, "/v2/events", {"source_id":self.source,
            "cursor":self.cursor, "next_cursor":key,
            "events":[{"native_object":name,"native_version":"1","actor_kind":"user_message",
                       "text":f"synthetic continuity sample {name}"} for name in ids]}, key)
        self.assertEqual(receipt["jobs_created"], len(ids))
        self.cursor = receipt["cursor"]

    def due(self, sql, arguments=()):
        # Advance only the disposable fixture's deadlines without sleeping through each backoff.
        with closing(sqlite3.connect(self.service.root / "data/bmem.sqlite3")) as db, db:
            db.execute(sql, arguments)

    def test_u13_outage_survives_restart_and_resumes_under_same_route(self):
        self.mode = "outage"
        self.add("during-503-1", "first")
        state = self.until(lambda view: view["extraction"]["state"] == "waiting_model")
        self.assertEqual(state["extraction"]["reason"], "model_http_503")
        self.assertTrue(state["jobs"][0]["waiting_model"])
        self.add("during-503-2", "second")
        self.down_and_back_up()
        state = self.jobs()
        self.assertEqual(state["extraction"]["queued"], 2)
        self.assertEqual(sum(job["attempts"] for job in state["jobs"]), 1)
        self.assertTrue(all(job["waiting_model"] and job["retry_count"] == 0 for job in state["jobs"]))
        self.mode = "healthy"
        self.due("UPDATE ing_extract_health SET next_probe_at=0")
        state = self.until(lambda view: view["extraction"]["state"] == "ready" and
                           all(job["status"] == "done" for job in view["jobs"]))
        self.assertEqual(state["extraction"]["queued"], 0)
        self.assertIn("probe", self.requests)

    def down_and_back_up(self):
        self.service.stop()
        self.service.start()
        self.owner, _ = self.service.web()

    def test_u13_disconnect_rate_limit_and_credentials_report_actionable_state(self):
        for mode, reason in (("disconnect", "model_unreachable"), ("rate_limit", "model_http_429"),
                             ("unauthorized", "model_http_401")):
            with self.subTest(mode=mode):
                self.mode = mode
                self.add(mode, mode)
                state = self.until(lambda view: view["extraction"]["state"] == "waiting_model")
                status = state["extraction"]
                self.assertEqual(status["reason"], reason)
                self.assertEqual(status["configuration_attention"], mode == "unauthorized")
                self.assertEqual(status["queued"], 1)
                self.assertIsNotNone(status["oldest_queued_at"])
                self.assertIsNotNone(status["next_probe_at"])
                self.assertEqual(self.service.cli("job", "list")["extraction"], status)
                self.mode = "healthy"
                self.due("UPDATE ing_extract_health SET next_probe_at=0")
                self.until(lambda view: view["extraction"]["state"] == "ready" and
                           all(job["status"] == "done" for job in view["jobs"]))

    def test_u14_retry_ceiling_and_granted_manual_requeue(self):
        self.mode = "bad_item"
        self.add("single-bad-input", "poison", "good")
        self.until(lambda view: any(job["status"] == "done" for job in view["jobs"]))
        for attempt in range(1, 5):
            state = self.until(lambda view: any(job["attempts"] == attempt and
                               job["status"] in ("retrying", "failed") for job in view["jobs"]))
            self.assertEqual(state["extraction"]["state"], "ready")
            [bad] = [job for job in state["jobs"] if job["status"] != "done"]
            self.assertEqual(bad["status"], "failed" if attempt == 4 else "retrying")
            if attempt != 4:
                self.due("UPDATE ing_jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE status='retrying'")
        self.assertEqual(bad["retry_count"], 3)
        self.mode = "healthy"
        path = "/v2/jobs/" + bad["job_id"]
        status, reply = self.owner.request(path + "/retry", "POST", {})
        self.assertEqual(status, 202, reply)
        self.assertEqual(reply["job_id"] if "job_id" in reply else reply["id"], bad["job_id"])
        self.until(lambda view: all(job["status"] == "done" for job in view["jobs"]))
        _, detail = self.owner.request(path)
        self.assertEqual(detail["attempts"], 5)
        self.assertEqual(len(detail["attempt_history"]), 5)
        self.assertEqual(self.owner.request(path + "/retry", "POST", {})[0], 409)

    def test_u15_shared_failure_pauses_before_fourth_job_and_recovers(self):
        self.mode = "shared_failure"
        self.add("four-samples", "one", "two", "three", "four")
        state = self.until(lambda view: view["extraction"]["state"] == "waiting_model" and
                           view["extraction"]["reason"] == "model_http_503")
        self.assertEqual(sum(job["attempts"] for job in state["jobs"]), 3)
        self.assertEqual(self.requests.count("probe"), 1)
        self.add("still-captures", "five")
        self.assertEqual(self.jobs()["extraction"]["queued"], 5)
        self.mode = "healthy"
        self.due("UPDATE ing_extract_health SET next_probe_at=0")
        self.due("UPDATE ing_jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE status='retrying'")
        self.until(lambda view: view["extraction"]["state"] == "ready" and
                   all(job["status"] == "done" for job in view["jobs"]))


if __name__ == "__main__":
    unittest.main()
