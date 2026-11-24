"""Integration checks for the native bmem executable and its loopback API.

Set BMEM_V2_TEST_BIN to the compiled bmem binary and run
python3 -m unittest discover -s tests/v2 -p test_integration.py -v.
"""

import concurrent.futures
import http.cookiejar
from http.server import ThreadingHTTPServer
import json
import os
import signal
from pathlib import Path
from socketserver import BaseRequestHandler
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


def fixture(name):
    return json.loads((Path(__file__).parent / "data" / name).read_text(encoding="utf-8"))


FIXTURE = fixture("declarations.json")


class BrowserSession:
    def __init__(self, base_url, login_url=None):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )
        if login_url is not None:
            if urllib.parse.urlsplit(login_url).netloc != urllib.parse.urlsplit(base_url).netloc:
                raise ValueError("Unexpected Web login origin")
            with self.opener.open(login_url, timeout=5) as response:
                if response.status != 200 or not response.geturl().startswith(base_url + "/"):
                    raise ValueError("Failed to reach the local Web console")

    def request(self, path, method="GET", body=None, key=None, origin=None, authentication=None):
        headers = {"Accept": "application/json", **(authentication or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = origin or self.base_url
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers=headers,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)


class BmemIntegration(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("BMEM_V2_TEST_BIN")
        if not binary or not Path(binary).is_file():
            self.fail("Set BMEM_V2_TEST_BIN to the compiled native bmem executable")
        self.binary = str(Path(binary).resolve())
        self.temp = tempfile.TemporaryDirectory(prefix="bmem-v2-integration-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.root = self.work / "isolated vault"
        self.default = self.work / "default vault"
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("BMEM_")
        }
        self.environment.update(
            BMEM_LOCATOR_DIR=str(self.work / "locator"),
            BMEM_DEFAULT_HOME=str(self.default),
        )
        self.daemon = None
        self.daemon_log = None
        self.addCleanup(self.stop)
        self.cli("--home", str(self.root), "init", "--non-interactive")

    def cli(self, *arguments, ok=True, timeout=15):
        result = subprocess.run(
            [self.binary, "--json", *arguments],
            cwd=self.work,
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")
        return json.loads(result.stderr)

    def start(self):
        self.daemon_log = tempfile.TemporaryFile(mode="w+b")
        self.daemon = subprocess.Popen(
            [self.binary, "service", "run"],
            cwd=self.work,
            env=self.environment,
            stdout=self.daemon_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 12
        while self.daemon.poll() is None and time.monotonic() < deadline:
            status = self.cli("status")
            if status["state"] == "running":
                self.endpoint = status["endpoint"]
                self.instance_id = status["instance_id"]
                origin = urllib.parse.urlsplit(self.endpoint)
                self.assertEqual(origin.scheme, "http")
                self.assertIn(origin.hostname, ("127.0.0.1", "::1"))
                return
            time.sleep(0.05)
        self.daemon_log.seek(0)
        self.fail("bmem did not become ready: " + self.daemon_log.read().decode(errors="replace"))

    def stop(self):
        if self.daemon is not None:
            if self.daemon.poll() is None:
                self.daemon.terminate()
                try:
                    self.daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.daemon.kill()
                    self.daemon.wait(timeout=5)
            self.daemon = None
        if self.daemon_log is not None:
            self.daemon_log.close()
            self.daemon_log = None

    def web(self):
        login_url = self.cli("web", "--no-open")["url"]
        self.assertEqual(urllib.parse.urlsplit(login_url).path, "/session")
        return BrowserSession(self.endpoint, login_url), login_url

    def config(self, browser):
        status, settings = browser.request("/v2/config")
        self.assertEqual(status, 200, settings)
        return settings

    def post(self, browser, path, body, key=None, authentication=None):
        status, payload = browser.request(
            path, "POST", body, key=key, authentication=authentication
        )
        self.assertIn(status, (200, 201, 202), payload)
        return payload

    def operation(self, browser, payload, key):
        preview = self.post(browser, "/v2/operations/preview", payload, "preview-" + key)
        return self.post(
            browser, "/v2/operations", {**payload, "preview_id": preview["preview_id"]}, key
        )

    def test_explicit_root_is_registered_and_status_leaves_service_offline(self):
        offline = self.cli("status")
        self.assertEqual(offline["state"], "stopped")
        doctor = self.cli("doctor")
        self.assertIs(doctor["locator_volume"]["atomic_replace"], True)
        self.assertIs(doctor["data_volume"]["wal_ready"], True)
        self.assertEqual(self.cli("status")["state"], "stopped")
        if os.name == "posix":
            self.assertEqual(self.root.stat().st_mode & 0o077, 0)
            self.assertEqual((self.work / "locator").stat().st_mode & 0o022, 0)
        self.cli("init", "--non-interactive")
        self.assertEqual(self.cli("status")["instance_id"], offline["instance_id"])
        alternate = self.work / "other vault"
        self.cli("--home", str(alternate), "init", "--non-interactive", ok=False)
        self.assertFalse(alternate.exists())
        self.assertFalse(self.default.exists())
        (self.work / "locator" / "active.json").unlink()
        self.cli("init", "--non-interactive", ok=False)
        recovered = self.cli("--home", str(self.root), "init", "--non-interactive")
        self.assertEqual(recovered["instance_id"], offline["instance_id"])
        self.start()
        self.assertEqual(self.instance_id, offline["instance_id"])

    def test_restart_reuses_web_port_and_falls_back_when_occupied(self):
        self.start()
        previous = self.endpoint
        previous_browser, _ = self.web()
        self.cli("service", "stop")
        self.daemon.wait(timeout=10)
        self.stop()
        self.start()
        self.assertEqual(self.endpoint, previous)
        self.assertEqual(previous_browser.request("/v2/config")[0], 401)
        fresh_browser, _ = self.web()
        self.assertEqual(self.config(fresh_browser)["profile"], "balanced")

        self.cli("service", "stop")
        self.daemon.wait(timeout=10)
        self.stop()
        origin = urllib.parse.urlsplit(previous)
        # A second loopback listener now owns the previous socket.
        with ThreadingHTTPServer((origin.hostname, origin.port), BaseRequestHandler) as occupied:
            thread = threading.Thread(target=occupied.serve_forever)
            thread.start()
            try:
                self.start()
                self.assertNotEqual(self.endpoint, previous)
                current_browser, _ = self.web()
                self.assertEqual(self.config(current_browser)["profile"], "balanced")
            finally:
                occupied.shutdown()
                thread.join()

    @unittest.skipUnless(os.name == "posix", "POSIX terminal process groups")
    def test_web_service_survives_its_launchers_process_group(self):
        launcher = subprocess.Popen(
            [self.binary, "--json", "web", "--no-open"],
            cwd=self.work, env=self.environment, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = launcher.communicate(timeout=15)
            self.assertEqual(launcher.returncode, 0, stderr)
            # The browser opens its ticket after the launching command has exited.
            try:
                os.killpg(launcher.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            url = json.loads(stdout)["url"]
            endpoint = self.cli("status").get("endpoint")
            self.assertTrue(endpoint, "The service stopped with its launcher")
            browser = BrowserSession(endpoint, url)
            self.assertEqual(browser.request("/v2/health")[0], 200)
        finally:
            self.cli("service", "stop")

    def test_first_write_starts_the_registered_instance(self):
        stopped = self.cli("status")
        self.assertEqual(stopped["state"], "stopped")
        try:
            written = self.cli("config", "set", "profile", "high")
            self.assertEqual(written["profile"], "high")
            running = self.cli("status")
            self.assertEqual(running["state"], "running")
            self.assertEqual(running["instance_id"], stopped["instance_id"])
            self.assertFalse(self.default.exists())
        finally:
            self.cli("service", "stop")
            deadline = time.monotonic() + 5
            while self.cli("status")["state"] != "stopped":
                self.assertLess(time.monotonic(), deadline, "Daemon did not stop")
                time.sleep(0.05)

    def test_web_requires_owner_session_and_same_origin(self):
        self.start()
        anonymous = BrowserSession(self.endpoint)
        status, response = anonymous.request("/v2/config")
        self.assertEqual(status, 401, response)
        browser, one_time_url = self.web()
        self.assertTrue(any(
            cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")
            for cookie in browser.cookies
        ))
        settings = self.config(browser)
        request = {"expected_revision": settings["config_revision"], "patch": {"profile": "data"}}
        status, response = browser.request(
            "/v2/config", "PATCH", request, key="cross-origin", origin="https://untrusted.invalid"
        )
        self.assertEqual(status, 403, response)
        self.assertEqual(self.config(browser), settings)
        with self.assertRaises(urllib.error.HTTPError) as failed_login:
            anonymous.opener.open(one_time_url, timeout=5)
        with failed_login.exception as response:
            self.assertGreaterEqual(response.code, 400)
            self.assertLess(response.code, 500)
        status, response = anonymous.request("/v2/config")
        self.assertEqual(status, 401, response)

    def test_configuration_is_shared_versioned_and_replayed_after_restart(self):
        self.start()
        browser, _ = self.web()
        old = self.config(browser)
        self.assertEqual(old["profile"], "balanced")
        self.assertIs(old["vector_search"]["enabled"], False)
        revision = old["config_revision"]
        cli_update = self.cli(
            "config", "set", "profile", "high", "--expected-revision", str(revision),
            "--idempotency-key", "cli-first",
        )
        self.assertEqual(cli_update["config_revision"], revision + 1)
        self.assertEqual(self.config(browser)["profile"], "high")

        stale = {"expected_revision": revision, "patch": {"profile": "data"}}
        status, response = browser.request("/v2/config", "PATCH", stale, key="stale-tab")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["code"], "revision_changed")

        second = {"expected_revision": revision + 1, "patch": {"profile": "data"}}
        status, response = browser.request("/v2/config", "PATCH", second, key="web-second")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["config_revision"], revision + 2)
        self.assertEqual(self.cli("config", "get")["profile"], "data")

        self.stop()
        self.start()
        browser, _ = self.web()
        recovered = self.config(browser)
        self.assertEqual(recovered["profile"], "data")
        self.assertEqual(recovered["config_revision"], revision + 2)
        replayed = self.cli(
            "config", "set", "profile", "data", "--expected-revision", str(revision + 1),
            "--idempotency-key", "web-second",
        )
        self.assertEqual(replayed["config_revision"], revision + 2)
        status, collision = browser.request(
            "/v2/config", "PATCH", {**second, "patch": {"profile": "high"}}, key="web-second"
        )
        self.assertEqual(status, 409, collision)
        self.assertEqual(collision["code"], "key_conflict")
        self.assertEqual(self.config(browser), recovered)

    def test_concurrent_cli_and_web_save_one_settings_revision(self):
        self.start()
        browser, _ = self.web()
        revision = self.config(browser)["config_revision"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            cli = pool.submit(
                subprocess.run,
                [
                    self.binary, "--json", "config", "set", "profile", "high",
                    "--expected-revision", str(revision), "--idempotency-key", "parallel-cli",
                ],
                cwd=self.work, env=self.environment, capture_output=True, text=True,
                encoding="utf-8", timeout=10,
            )
            web = pool.submit(
                browser.request,
                "/v2/config", "PATCH",
                {"expected_revision": revision, "patch": {"profile": "data"}},
                "parallel-web",
            )
            cli_result = cli.result()
            status, response = web.result()
        settings = self.config(browser)
        self.assertEqual(settings["config_revision"], revision + 1)
        if cli_result.returncode == 0:
            self.assertEqual(status, 409, response)
            self.assertEqual(response["code"], "revision_changed")
            self.assertEqual(settings["profile"], "high")
        else:
            self.assertEqual(status, 200, response)
            self.assertEqual(json.loads(cli_result.stderr)["code"], "revision_changed")
            self.assertEqual(settings["profile"], "data")

    def test_statement_correction_preserves_other_facts_and_isolates_scope(self):
        self.start()
        browser, _ = self.web()
        case = FIXTURE
        first = self.cli(
            "declare", case["initial"], "--scope", "global", "--share", "authorized_assistants",
            "--idempotency-key", "original",
        )
        before = {item["key"]: item for item in first["dimensions"]}
        status, response = browser.request("/v2/statements")
        self.assertEqual(status, 200, response)
        initial = {item["key"]: item for item in response["statements"]}
        language = case["language_key"]
        opening = case["opening_key"]
        self.assertEqual(initial[language]["value"], case["language"])
        self.assertEqual(initial[opening]["value"], case["initial_opening"])

        status, correction = browser.request(
            "/v2/statements", "POST",
            {
                "original": case["correction"], "scope": {"kind": "global"},
                "share": "authorized_assistants",
            },
            key="correction",
        )
        self.assertEqual(status, 201, correction)
        self.assertEqual(len(correction["dimensions"]), 1)
        amended, = correction["dimensions"]
        self.assertEqual(amended["fact_id"], before[opening]["fact_id"])
        self.assertNotEqual(amended["fact_id"], opening)
        self.assertEqual(amended["supersedes"], before[opening]["revision_id"])
        current = {item["key"]: item for item in self.cli("statement", "list")["statements"]}
        self.assertEqual(current[language]["revision_id"], before[language]["revision_id"])
        self.assertEqual(current[opening]["value"], case["corrected_opening"])
        self.assertEqual(current[opening]["revision_id"], amended["revision_id"])

        status, project = browser.request(
            "/v2/statements", "POST",
            {
                "original": case["project_override"], "share": "authorized_assistants",
                "scope": {"kind": "project", "project": case["project_id"]},
            },
            key="project",
        )
        self.assertEqual(status, 201, project)
        project_fact, = project["dimensions"]
        self.assertEqual(project_fact["key"], opening)
        self.assertNotEqual(project_fact["fact_id"], amended["fact_id"])

        history = self.cli("statement", "history", amended["fact_id"])
        status, browser_history = browser.request("/v2/facts/" + amended["fact_id"] + "/history")
        self.assertEqual(status, 200, browser_history)
        self.assertEqual(history, browser_history)
        historical_revisions = {entry["revision_id"] for entry in history["revisions"]}
        self.assertIn(amended["revision_id"], historical_revisions)
        self.assertIn(before[opening]["revision_id"], historical_revisions)
        self.assertNotIn(project_fact["revision_id"], historical_revisions)

    def test_synthetic_bound_host_receipts_and_revocation(self):
        self.start()
        browser, _ = self.web()
        anonymous = BrowserSession(self.endpoint)
        host_a = self.post(browser, "/v2/clients", {
            "host": "fixture-claude",
            "destination": "test-local",
            "capabilities": {
                "load_context": True, "observe_loaded": True,
                "observe_used": True, "verify_outcomes": True,
            },
        }, "host-a")
        host_b = self.post(browser, "/v2/clients", {
            "host": "fixture-codex", "destination": "test-local",
        }, "host-b")
        verifier = self.post(browser, "/v2/clients", {
            "host": "fixture-verifier", "destination": "test-local",
            "capabilities": {"verify_outcomes": True},
        }, "verifier")

        def authentication(host):
            return {
                "Authorization": "Bearer " + host["token"],
                "X-Bmem-Instance": self.instance_id,
            }

        self.cli(
            "declare", FIXTURE["initial"], "--scope", "global",
            "--share", "authorized_assistants", "--idempotency-key", "shared",
        )
        task = self.post(
            browser, "/v2/tasks", {"project": FIXTURE["project_id"], "anchor": "import@3"}, "task"
        )
        grant = {
            "kind": "change_grant",
            "client_id": host_a["client_id"],
            "share_statements": True,
        }
        self.operation(browser, grant, "grant-statements")
        task_grant = {
            "kind": "change_grant", "client_id": host_a["client_id"],
            "task_id": task["task_id"], "allow": True,
        }
        self.operation(browser, task_grant, "grant-task")
        self.operation(browser, {**task_grant, "client_id": verifier["client_id"]}, "grant-verifier")

        session = {
            "session_id": "run-fixture-a",
            "context_epoch": "epoch-1",
            "route_ref": host_a["route_ref"],
        }
        attempt = self.post(
            anonymous, f"/v2/tasks/{task['task_id']}/attempts",
            session, "attempt", authentication(host_a),
        )
        context_request = {
            **session, "task_id": task["task_id"], "attempt_id": attempt["attempt_id"],
            "budget": {"statement_tokens": 4096, "evidence_tokens": 0},
        }
        denied, payload = anonymous.request(
            "/v2/context/load", "POST", context_request, authentication=authentication(host_b),
        )
        self.assertIn(denied, (403, 404), payload)
        provided = self.post(
            anonymous, "/v2/context/load", context_request, "first-load", authentication(host_a)
        )
        self.assertEqual(provided["delivery_state"], "provided")
        self.assertEqual(provided["snapshot_mode"], "replace")
        self.assertEqual({entry["key"] for entry in provided["statements"]}, {
            FIXTURE["language_key"], FIXTURE["opening_key"],
        })
        self.assertNotIn(FIXTURE["initial"], json.dumps(provided, ensure_ascii=False))
        known = {**context_request, "known_snapshot_id": provided["snapshot_id"]}
        repeat = self.post(anonymous, "/v2/context/load", known, "seen-only", authentication(host_a))
        self.assertEqual(repeat["snapshot_mode"], "replace")

        ack = {
            "event_id": "observed-input",
            "kind": "loaded",
            "revision_ids": [item["revision_id"] for item in provided["statements"]],
            "route_ref": host_a["route_ref"],
            "context_epoch": session["context_epoch"],
            "snapshot_id": provided["snapshot_id"],
            "package_id": provided["package_id"],
            "model_call_ref": "fixture-call",
        }
        receipt_path = f"/v2/deliveries/{provided['delivery_id']}/receipts"
        mismatch, result = anonymous.request(
            receipt_path, "POST", {**ack, "context_epoch": "epoch-2"},
            authentication=authentication(host_a),
        )
        self.assertEqual(mismatch, 409, result)
        used_first, result = anonymous.request(
            receipt_path, "POST", {**ack, "kind": "used", "evidence_ref": "fixture-output"},
            authentication=authentication(host_a),
        )
        self.assertEqual(used_first, 409, result)
        loaded = self.post(anonymous, receipt_path, ack, authentication=authentication(host_a))
        self.assertEqual(loaded["kind"], "loaded")
        self.assertIs(loaded["complete"], True)
        self.assertEqual(self.post(
            anonymous, receipt_path, ack, authentication=authentication(host_a)
        ), loaded)
        unchanged = self.post(
            anonymous, "/v2/context/load", known, "seen-loaded", authentication(host_a)
        )
        self.assertEqual(unchanged["snapshot_mode"], "unchanged")

        outcome = {
            "event_id": "external-check", "status": "passed", "validator_ref": "fixture-verifier",
            "artifact_ref": "import@3", "evidence_ref": "fixture-output",
            "delivery_ids": [provided["delivery_id"]],
        }
        path = f"/v2/attempts/{attempt['attempt_id']}/outcomes"
        self_verified, result = anonymous.request(
            path, "POST", outcome, authentication=authentication(host_a),
        )
        self.assertEqual(self_verified, 403, result)
        inspected = self.post(anonymous, path, outcome, authentication=authentication(verifier))
        self.assertEqual(inspected["attribution"], "unknown")
        status, report = browser.request("/v2/deliveries")
        self.assertEqual(status, 200, report)
        [observed] = [item for item in report["deliveries"] if item["delivery_id"] == provided["delivery_id"]]
        self.assertEqual(observed["outcome_state"], "passed")
        self.assertEqual(observed["used_state"], "unknown")
        status, ledger = browser.request(
            "/v2/deliveries/ledger?host=fixture-claude&loaded=observed&used=pending&page=1"
        )
        self.assertEqual(status, 200, ledger)
        self.assertEqual(ledger["total"], 1, ledger)
        self.assertEqual(ledger["deliveries"][0]["delivery_id"], provided["delivery_id"])
        self.assertEqual(ledger["deliveries"][0]["loaded_state"], "observed")
        self.assertEqual(ledger["deliveries"][0]["used_state"], "pending")
        self.assertEqual(ledger["summary"]["used"], 0, ledger)

        self.operation(browser, {**grant, "share_statements": False}, "revoke-share")
        revoked = self.post(
            anonymous, "/v2/context/load", known, "after-revoke", authentication(host_a)
        )
        self.assertEqual(revoked["snapshot_mode"], "replace")
        self.assertEqual(revoked["statements"], [])
        stale_ack, result = anonymous.request(
            receipt_path, "POST", {**ack, "event_id": "late-ack"},
            authentication=authentication(host_a),
        )
        self.assertIn(stale_ack, (403, 404, 409), result)

    def test_history_sync_preserves_offsets_and_pending_jobs_across_restart(self):
        self.environment["BMEM_CONFIG_ROOT"] = str(self.work / "host configuration")
        transcript = Path(self.environment["BMEM_CONFIG_ROOT"]) / ".codex/sessions/transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        payload = (Path(__file__).parent / "data/history_codex.jsonl").read_bytes()
        prefix, last_line = payload.rstrip(b"\n").rsplit(b"\n", 1)
        midpoint = len(last_line) // 2
        transcript.write_bytes(prefix + b"\n" + last_line[:midpoint])

        self.start()
        sid = self.cli("connector", "add", "codex", "--account", "local")["source_id"]
        self.cli("connector", "resume", sid)
        self.cli("connector", "sync", sid)

        def wait_for_events(count):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                [source] = self.cli("connector", "list")["sources"]
                if source["history"]["events"] == count:
                    return source
                time.sleep(.1)
            self.fail(f"Expected {count} historical events: {source['history']}")

        self.assertTrue(wait_for_events(1)["history"]["enabled"])
        self.cli("connector", "sync", sid, "--pause")
        self.stop()
        with transcript.open("ab") as file:
            file.write(last_line[midpoint:] + b"\n")
        self.start()
        self.assertFalse(wait_for_events(1)["history"]["enabled"])
        self.cli("connector", "sync", sid)
        self.assertTrue(wait_for_events(2)["history"]["enabled"])
        [user, tool] = self.cli("job", "list")["jobs"]
        self.assertEqual({user["actor_kind"], tool["actor_kind"]},
                         {"user_message", "tool_result"})
        self.assertTrue(all(job["status"] == "pending" for job in [user, tool]))

    def test_source_page_and_outbox_commit_once_without_a_model(self):
        self.start()
        owner, _ = self.web()
        case = fixture("source_page.json")
        source = self.post(owner, "/v2/sources", case["source"], "source-register")
        page = {**case["page"], "source_id": source["source_id"]}
        rejected, payload = owner.request("/v2/events", "POST", page, key="unauthorized-capture")
        self.assertIn(rejected, (403, 409), payload)
        self.operation(owner, {"kind": "resume_capture", "source_id": source["source_id"]}, "capture")

        receipt = self.post(owner, "/v2/events", page, "event-page")
        code, jobs = owner.request("/v2/jobs")
        self.assertEqual(code, 200, jobs)
        self.assertEqual(len(jobs["jobs"]), 2)
        self.assertEqual(self.post(owner, "/v2/events", page, "event-page"), receipt)
        changed = {**page, "next_cursor": "different-offset"}
        code, conflict = owner.request("/v2/events", "POST", changed, key="event-page")
        self.assertEqual(code, 409, conflict)
        self.assertEqual(conflict["code"], "key_conflict")
        self.assertEqual(owner.request("/v2/jobs"), (200, jobs))
        code, sources = owner.request("/v2/sources")
        self.assertEqual(code, 200, sources)
        [registered] = [s for s in sources["sources"] if s["source_id"] == source["source_id"]]
        self.assertEqual(registered["cursor"], page["next_cursor"])
        hits = self.post(owner, "/v2/search", {"query": "复验解析"})
        self.assertEqual(hits["retrieval"], "lexical")
        [plan] = [hit for hit in hits["results"] if hit.get("actor_kind") == "user_message"]
        handle = plan["handle"]
        original = self.post(owner, "/v2/evidence/expand", {"handle": handle})
        self.assertEqual(original["text"], page["events"][0]["text"])

        self.stop()
        self.start()
        owner, _ = self.web()
        self.assertEqual(owner.request("/v2/jobs"), (200, jobs))
        self.assertEqual(self.post(owner, "/v2/events", page, "event-page"), receipt)
        self.operation(owner, {"kind": "forget", "target": {"source_id": source["source_id"]}}, "forget-source")
        code, missing = owner.request("/v2/evidence/expand", "POST", {"handle": handle})
        self.assertEqual(code, 404, missing)
        self.assertEqual(missing["code"], "not_available")
        self.assertEqual(self.post(owner, "/v2/search", {"query": "复验解析"})["results"], [])

    def test_backup_preview_refuses_pre_forget_state(self):
        self.start()
        owner, _ = self.web()
        statement = self.cli("declare", FIXTURE["initial"], "--scope", "global")
        snapshot = self.cli("backup", "create")
        self.assertEqual(snapshot["state"], "complete")
        self.assertRegex(snapshot["sha256"], r"^[a-f0-9]{64}$")
        preview = self.cli("restore", "preview", snapshot["backup_id"])
        self.assertEqual(preview["state"], "verified")
        self.assertEqual(preview["integrity"], "verified")

        subject = {
            "kind": "forget", "target": {"statement_id": statement["statement_id"]},
        }
        erased = self.operation(owner, subject, "erase-before-restore")
        self.assertIs(erased["effective"], True)
        blocked = self.cli("restore", "preview", snapshot["backup_id"])
        self.assertEqual(blocked["state"], "blocked")
        self.assertIs(blocked["can_apply"], False)
        self.assertNotEqual(blocked["active_revocation_cut"], blocked["revocation_cut"])
        self.assertEqual(self.cli("statement", "list")["statements"], [])
        fact = statement["dimensions"][0]["fact_id"]
        status, history = owner.request("/v2/facts/" + fact + "/history")
        self.assertEqual(status, 404, history)
        self.assertEqual(history["code"], "not_available")

    def test_source_revocation_blocks_an_unseen_older_version(self):
        self.start()
        owner, _ = self.web()
        case = fixture("source_page.json")
        source = self.post(owner, "/v2/sources", case["source"], "late-version-source")
        source_id = source["source_id"]
        self.operation(owner, {"kind": "resume_capture", "source_id": source_id}, "accept-late")
        event = {**case["page"]["events"][0], "native_version": "3"}
        def submit(cursor, next_cursor, events, key):
            return self.post(owner, "/v2/events", {
                "source_id": source_id, "cursor": cursor, "next_cursor": next_cursor, "events": events,
            }, key)
        receipt = submit(None, "3", [event, event], "initial-3")
        self.assertEqual([row["disposition"] for row in receipt["events"]], ["accepted", "duplicate"])
        self.assertEqual(receipt["events"][0]["event_id"], receipt["events"][1]["event_id"])
        submit("3", "4", [{**event, "change_kind": "revoked"}], "withdraw-3")
        late = submit("4", "5", [{**event, "native_version": "2"}], "late-2")
        self.assertEqual(late["events"][0]["disposition"], "revoked_blocked")
        self.assertEqual(late["jobs_created"], 0)
        self.assertEqual(self.post(owner, "/v2/search", {"query": "复验解析"})["results"], [])

    @unittest.skipUnless(
        os.environ.get("BMEM_V2_TEST_REAL_MODEL"),
        "set BMEM_V2_TEST_REAL_MODEL to opt in to the installed local Ollama model",
    )
    def test_real_local_model_processes_fixture_page(self):
        self.start()
        owner, _ = self.web()
        case = fixture("source_page.json")
        self.cli(
            "model", "add", "replay", "--protocol", "ollama",
            "--endpoint", "http://127.0.0.1:11434",
            "--model-id", os.environ["BMEM_V2_TEST_REAL_MODEL"],
        )
        self.cli("model", "test", "replay", timeout=90)
        self.cli("model", "use", "replay", "--for", "extract")
        config = self.config(owner)
        self.assertIs(config["vector_search"]["enabled"], False)
        self.assertIsNotNone(config["bindings"]["extract"])

        task = self.post(owner, "/v2/tasks", {
            "project": FIXTURE["project_id"], "anchor": "import@3",
        }, "extraction-task")
        source = self.post(owner, "/v2/sources", case["source"], "real-source")
        self.operation(
            owner, {"kind": "resume_capture", "source_id": source["source_id"]}, "capture-real"
        )
        self.operation(owner, {
            "kind": "change_grant", "source_id": source["source_id"],
            "purpose": "extract", "allow": True,
        }, "extract-real")
        events = [{**event, "task_id": task["task_id"]} for event in case["page"]["events"]]
        page = {**case["page"], "source_id": source["source_id"], "events": events}
        self.post(owner, "/v2/events", page, "real-page")

        deadline = time.monotonic() + 90
        while True:
            code, response = owner.request("/v2/jobs")
            self.assertEqual(code, 200, response)
            jobs = response["jobs"]
            if len(jobs) == len(events) and all(job["status"] == "done" for job in jobs):
                break
            self.assertLess(time.monotonic(), deadline, f"Jobs did not complete: {response}")
            time.sleep(0.25)
        # Genuine model activity on ordinary event roles must stay distinct from a T1 declaration.
        self.assertEqual(self.cli("statement", "list")["statements"], [])
        for job in jobs:
            code, detail = owner.request("/v2/jobs/" + (job.get("job_id") or job["id"]))
            self.assertEqual(code, 200, detail)
            self.assertGreater(detail["candidates_recorded"], 0, detail)
        code, handoff = owner.request(f"/v2/tasks/{task['task_id']}/handoff")
        self.assertEqual(code, 200, handoff)
        [tool_event] = [
            item for item in handoff["evidence"] if item["actor_kind"] == "tool_result"
        ]
        [plan_event] = [
            item for item in handoff["evidence"] if item["actor_kind"] == "user_message"
        ]
        self.assertEqual(json.loads(tool_event["text"]), events[1]["checks"])
        self.assertEqual(plan_event["text"], events[0]["text"])
        actual = {
            (item["event_id"], item["actor_kind"], item["field"], item["value"])
            for item in handoff["observations"]
        }
        self.assertEqual(actual, {
            (tool_event["event_id"], "tool_result", "/checks/parse", "passed"),
            (tool_event["event_id"], "tool_result", "/checks/write", "timeout"),
            (plan_event["event_id"], "plan", "/text", events[0]["text"]),
        }, handoff)


if __name__ == "__main__":
    unittest.main()
