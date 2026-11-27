"""Task packages through the real service with a deterministic loopback model fixture."""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from uuid import uuid4
from threading import Thread
import time
import unittest

import test_integration as support


class FixtureModel(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/api/chat", "/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers["Content-Length"])
        prompt = json.loads(self.rfile.read(length))
        evidence = json.loads(prompt["messages"][-1]["content"])
        candidates = [
            {
                "kind": "plan" if span["kind"] == "user_message" else "fact",
                "subject": "fixture-import",
                "key": span["field"],
                "value": "passed",
                "support": [span["span_id"]],
            }
            for span in evidence["inputs"]
        ]
        message = {"content": json.dumps({"candidates": candidates})}
        if self.path == "/chat/completions":
            choice = {"finish_reason": "stop", "message": message}
            if not prompt.get("max_completion_tokens"):
                choice = {"finish_reason": "length", "message": {"content": None}}
            response = {"choices": [choice]}
        else:
            response = {"done": True, "done_reason": "stop", "message": message}
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class TaskDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()

    def configure_model(self):
        model = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()

        def stop_model():
            model.shutdown()
            thread.join(timeout=5)
            model.server_close()

        self.addCleanup(stop_model)
        endpoint = f"http://127.0.0.1:{model.server_port}"
        self.service.cli(
            "model", "add", "task-fixture", "--protocol", "ollama",
            "--endpoint", endpoint, "--model-id", "task-fixture",
        )
        self.service.cli("model", "test", "task-fixture")
        self.service.cli("model", "use", "task-fixture", "--for", "extract")

    def test_model_api_key_uses_database_without_echo_and_survives_restart(self):
        service = self.service
        settings_db = service.root / "data/settings.sqlite3"
        legacy = service.root / "config.toml"
        settings_db.unlink()
        legacy.write_text('config_revision = 3\nprofile = "high"\n', encoding="utf-8")
        key = "fixture-" + uuid4().hex
        replacement = "fixture-" + uuid4().hex
        received = []

        class KeyedModel(FixtureModel):
            def do_POST(self):
                received.append(self.headers.get("Authorization"))
                super().do_POST()

        model = ThreadingHTTPServer(("127.0.0.1", 0), KeyedModel)
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(model.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(model.shutdown)
        endpoint = f"http://127.0.0.1:{model.server_port}"

        service.start()
        owner, _ = service.web()
        self.assertEqual(service.config(owner)["profile"], "high")

        def patch(models, revision, nonce):
            return owner.request("/v2/config", "PATCH", {
                "expected_revision": revision,
                "patch": {"models": models},
            }, key=nonce)

        add = {"op": "add", "name": "key-fixture", "protocol": "ollama",
               "endpoint": endpoint, "model_id": "fixture", "api_key": key}
        code, result = patch(add, 3, "model-add")
        self.assertEqual(code, 200, result)
        self.assertNotIn(key, json.dumps(result))
        self.assertTrue(result["model_result"]["credential_configured"])
        identifier = result["model_result"]["id"]

        service.stop()
        service.start()
        owner, _ = service.web()
        code, replay = patch(add, 3, "model-add")
        self.assertEqual((code, replay), (200, result))
        self.assertEqual(patch({**add, "api_key": replacement}, 3, "model-add")[0], 409)
        path = f"/v2/models/{identifier}/test"
        self.assertTrue(service.post(owner, path, {"purpose": "extract"})["tested"])
        self.assertEqual(received, ["Bearer " + key])
        self.assertNotIn(key, json.dumps(service.config(owner)))

        def update(value, rev):
            return patch({"op": "update", "id": identifier, "changes": {"api_key": value}},
                         rev, "key-" + str(rev))

        self.assertEqual(update(replacement, 4)[0], 200)
        self.assertEqual(service.config(owner)["models"][0]["tested"], [])
        self.assertTrue(service.post(owner, path, {"purpose": "extract"})["tested"])
        self.assertEqual(received[-1], "Bearer " + replacement)
        with sqlite3.connect(settings_db) as db:
            self.assertEqual(db.execute("SELECT api_key FROM model_api_keys").fetchall(),
                             [(replacement,)])
            snapshot = db.execute("SELECT snapshot FROM user_settings").fetchone()[0]
            self.assertNotIn(key, snapshot)
            self.assertNotIn(replacement, snapshot)

        code, cleared = update(None, 5)
        self.assertEqual(code, 200, cleared)
        self.assertFalse(cleared["model_result"]["credential_configured"])
        with sqlite3.connect(settings_db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM model_api_keys").fetchone()[0], 0)
        self.assertNotIn(replacement, settings_db.read_bytes().decode("latin1"))

    def test_server_extraction_requires_a_grant_for_the_current_endpoint(self):
        service = self.service
        service.start()
        owner, _ = service.web()
        self.configure_model()
        [model] = service.config(owner)["models"]
        case = support.fixture("source_page.json")
        source = service.post(owner, "/v2/sources", case["source"], "model-grant-source")
        source_id = source["source_id"]
        grant = {"kind": "change_grant", "source_id": source_id, "purpose": "extract", "allow": True}
        service.operation(owner, {"kind": "resume_capture", "source_id": source_id}, "capture")
        service.operation(owner, grant, "local-grant")
        old_preview = service.post(owner, "/v2/operations/preview", grant, "old-model-preview")
        self.assertTrue(old_preview["model_destination"]["local_endpoint"])

        # This hostname exercises the server endpoint path without an external dependency.
        endpoint = model["endpoint"].replace("127.0.0.1", "localhost")
        service.cli("model", "configure", model["name"], "--endpoint", endpoint, "--protocol", "openai_chat")
        self.assertEqual(service.config(owner)["activity"]["extract"], "pending_test")
        self.assertTrue(service.cli("model", "test", model["name"])["tested"])
        self.assertEqual(service.config(owner)["activity"]["extract"], "ready")
        [registered] = service.cli("connector", "list")["sources"]
        self.assertTrue(registered["extract_reauthorization_required"])
        status, error = owner.request("/v2/operations", "POST",
                                      {**grant, "preview_id": old_preview["preview_id"]}, key="stale")
        self.assertEqual((status, error["code"]), (409, "revision_changed"))
        page = {**case["page"], "source_id": source_id}
        service.post(owner, "/v2/events", page, "server-page")
        time.sleep(2.5)
        _, jobs = owner.request("/v2/jobs")
        self.assertEqual(len(jobs["jobs"]), len(page["events"]))
        self.assertTrue(all(job["status"] == "pending" and job["attempts"] == 0 for job in jobs["jobs"]))

        preview = service.post(owner, "/v2/operations/preview", grant, "server-preview")
        self.assertEqual(preview["model_destination"], {
            "name": model["name"], "endpoint": endpoint, "model_id": model["model_id"],
            "local_endpoint": False,
        })
        receipt = service.post(owner, "/v2/operations", {
            **grant, "preview_id": preview["preview_id"],
        }, "server-grant")
        self.assertTrue(receipt["effective"])
        self.wait_for_observations(owner, len(page["events"]))
        [registered] = service.cli("connector", "list")["sources"]
        self.assertFalse(registered["extract_reauthorization_required"])

    def wait_for_observations(self, owner, count):
        deadline = time.monotonic() + 10
        while True:
            status, output = owner.request("/v2/jobs")
            self.assertEqual(status, 200, output)
            jobs = output["jobs"]
            if len(jobs) == count and all(job["status"] == "done" for job in jobs):
                return
            self.assertLess(time.monotonic(), deadline, output)
            time.sleep(0.05)

    def test_task_source_permissions_budget_observations_and_append_only_resume(self):
        service = self.service
        service.start()
        owner, _ = service.web()
        caller = support.BrowserSession(service.endpoint)
        self.configure_model()
        case = support.fixture("source_page.json")
        registration = service.post(owner, "/v2/clients", {
            "host": "fixture-task-reader", "destination": "fixture-local",
            "capabilities": {"load_context": True, "observe_loaded": True},
        }, "register-reader")
        auth = {
            "Authorization": "Bearer " + registration["token"],
            "X-Bmem-Instance": service.instance_id,
        }
        client = registration["client_id"]
        route = registration["route_ref"]
        service.cli(
            "declare", support.FIXTURE["initial"], "--scope", "global",
            "--share", "authorized_assistants",
        )
        sharing = {"kind": "change_grant", "client_id": client, "share_statements": True}
        service.operation(owner, sharing, "share")
        task = service.post(
            owner, "/v2/tasks", {"anchor": "fixture@3", "project": support.FIXTURE["project_id"]},
            "task",
        )["task_id"]
        scope = {"session_id": "fixture-session", "context_epoch": "1", "route_ref": route}
        attempts_endpoint = f"/v2/tasks/{task}/attempts"
        status, denied = caller.request(
            attempts_endpoint, "POST", scope, "without-task-access", authentication=auth,
        )
        self.assertEqual((status, denied["code"]), (404, "not_available"))
        service.operation(owner, {
            "kind": "change_grant", "client_id": client, "task_id": task, "allow": True,
        }, "grant-task")
        attempt = service.post(caller, attempts_endpoint, scope, "attempt", auth)["attempt_id"]
        source = service.post(owner, "/v2/sources", case["source"], "source")["source_id"]
        service.operation(owner, {"kind": "resume_capture", "source_id": source}, "capture")
        service.operation(owner, {
            "kind": "change_grant", "source_id": source, "purpose": "extract", "allow": True,
        }, "extract")
        page = dict(case["page"], source_id=source)
        page["events"] = [dict(event, task_id=task) for event in page["events"]]
        service.post(owner, "/v2/events", page, "events")
        self.wait_for_observations(owner, len(page["events"]))

        code, jobs = owner.request("/v2/jobs")
        self.assertEqual(code, 200)
        tool_job = next(job for job in jobs["jobs"] if job["actor_kind"] == "tool_result")
        code, extracted = owner.request("/v2/jobs?has_candidates=true&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(extracted["jobs"]), 1)
        self.assertGreater(extracted["jobs"][0]["candidates_recorded"], 0)
        self.assertEqual(extracted["jobs"][0]["status"], "done")
        code, detail = owner.request(f'/v2/jobs/{tool_job["job_id"]}')
        self.assertEqual(code, 200)
        self.assertEqual(detail["input"]["actor_kind"], "tool_result")
        candidates_path = f'/v2/jobs/{tool_job["job_id"]}/candidates'
        code, candidates = owner.request(candidates_path)
        self.assertEqual(code, 200)
        self.assertEqual(candidates["source_id"], source)
        self.assertEqual(candidates["event_id"], tool_job["event_id"])
        self.assertEqual(datetime.fromisoformat(candidates["occurred_at"]),
                         datetime.fromisoformat(page["events"][1]["at"]))
        self.assertEqual({span["field"]: span["value"] for span in candidates["evidence"]}, {
            "/checks/parse": "passed", "/checks/write": "timeout",
        })
        self.assertEqual({span["span_id"] for span in candidates["evidence"]}, {
            span_id for candidate in candidates["candidates"] for span_id in candidate["support"]
        })
        status, denied = caller.request(candidates_path, authentication=auth)
        self.assertEqual((status, denied["code"]), (403, "action_denied"))

        endpoint = "/v2/context/load"
        request = {
            **scope, "task_id": task, "attempt_id": attempt, "requested_profile": "high",
            "budget": {"statement_tokens": 4096, "evidence_tokens": 4096},
        }
        status, denied = caller.request(
            endpoint, "POST", {**request, "route_ref": "another-route"}, authentication=auth,
        )
        self.assertEqual((status, denied["code"]), (403, "action_denied"))
        ungranted = service.post(caller, endpoint, request, "before-source-grant", auth)
        self.assertEqual(ungranted["package_state"], "evidence_unavailable")
        self.assertEqual(ungranted["evidence"], [])
        self.assertEqual(ungranted["observations"], [])
        grant = {
            "kind": "change_grant", "client_id": client, "source_id": source,
            "purpose": "task_delivery", "allow": True,
        }
        service.operation(owner, grant, "grant-source")
        package = service.post(caller, endpoint, request, "with-grant", auth)
        self.assertEqual(package["package_state"], "provided")
        self.assertEqual(package["requested_profile"], "high")
        self.assertEqual(package["effective_profile"], "data")
        self.assertEqual(package["fallback_reason"], "high_profile_quality_unverified")
        self.assertEqual(
            [row["actor_kind"] for row in package["evidence"]], ["user_message", "tool_result"],
        )
        plan, result = package["evidence"]
        self.assertEqual(plan["text"], page["events"][0]["text"])
        self.assertEqual(json.loads(result["text"]), page["events"][1]["checks"])
        self.assertEqual({
            (row["handle"], row["actor_kind"], row["field"], row["value"])
            for row in package["observations"]
        }, {
            (plan["handle"], "plan", "/text", plan["text"]),
            (result["handle"], "tool_result", "/checks/parse", "passed"),
            (result["handle"], "tool_result", "/checks/write", "timeout"),
        })
        self.assertEqual(package, service.post(caller, endpoint, request, "with-grant", auth))

        tiny = {**request, "budget": {"statement_tokens": 4096, "evidence_tokens": 1}}
        bounded = service.post(caller, endpoint, tiny, "bounded", auth)
        self.assertEqual(bounded["package_state"], "budget_truncated")
        self.assertEqual(bounded["evidence"], [])
        self.assertEqual(bounded["observations"], [])
        self.assertEqual(bounded["statements"], package["statements"])
        expand = {"handle": result["handle"], "route_ref": route}
        expanded = service.post(caller, "/v2/evidence/expand", expand, authentication=auth)
        self.assertEqual(expanded["text"], result["text"])

        service.operation(owner, {**grant, "allow": False}, "revoke-source")
        status, stale = caller.request(
            endpoint, "POST", request, "with-grant", authentication=auth,
        )
        self.assertEqual((status, stale["code"]), (409, "revision_changed"))
        after = service.post(caller, endpoint, request, "after-source-revoke", auth)
        self.assertEqual(after["package_state"], "evidence_unavailable")
        self.assertEqual(after["evidence"], [])
        self.assertEqual(after["observations"], [])
        status, denied = caller.request("/v2/evidence/expand", "POST", expand, authentication=auth)
        self.assertEqual((status, denied["code"]), (404, "not_available"))

        service.operation(owner, {**sharing, "share_statements": False}, "revoke-share")
        status, stale = caller.request(
            endpoint, "POST", {**request, "append_only": True}, "resume-session", authentication=auth,
        )
        self.assertEqual((status, stale["code"]), (409, "fresh_session_required"))
        new = service.post(caller, endpoint, {
            "session_id": "fresh-fixture-session", "context_epoch": "1", "route_ref": route,
            "budget": {"statement_tokens": 4096, "evidence_tokens": 0}, "append_only": True,
        }, "fresh-session", auth)
        self.assertEqual(new["snapshot_mode"], "replace")
        self.assertEqual(new["statements"], [])
        service.operation(owner, {"kind": "forget", "target": {"source_id": source}}, "forget-source")
        status, missing = owner.request(candidates_path)
        self.assertEqual((status, missing["code"]), (404, "not_available"))


if __name__ == "__main__":
    unittest.main()
