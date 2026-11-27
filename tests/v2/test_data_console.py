"""The data console's owner endpoints, CLI and SQLite share one isolated vault."""

from pathlib import Path
import json
import os
import time
import unittest

import test_integration as support


class DataConsoleTest(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()
        self.config_root = self.service.work / "connector-config"
        self.service.environment["BMEM_CONFIG_ROOT"] = str(self.config_root)

    def wait_for_job(self, browser, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, value = browser.request("/v2/history/jobs")
            self.assertEqual(status, 200, value)
            job = next(item for item in value["jobs"] if item["id"] == job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail(f"History job did not finish: {job_id}")

    def test_manual_history_is_filtered_durable_and_deduplicated(self):
        service = self.service
        sessions = self.config_root / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        fixture = Path(__file__).parent / "data" / "history_codex.jsonl"
        (sessions / "rollout-fixture.jsonl").write_bytes(fixture.read_bytes())
        service.start()
        browser, _ = service.web()
        source = service.cli("connector", "add", "codex", "--account", "local")["source_id"]
        service.cli("connector", "resume", source)
        options = service.cli("connector", "history-options", source, "--kind", "sessions")
        self.assertEqual(len(options["targets"]), 1, options)
        target = options["targets"][0]["id"]
        self.assertNotIn(str(sessions), target)
        body = {"source_id": source, "kind": "sessions", "targets": [target],
                "from": "2026-11-19T13:00:00Z", "to": "2026-11-19T13:00:03Z",
                "content": "messages"}
        service.post(browser, "/v2/history/preview", body)
        self.assertEqual(service.cli("connector", "history-jobs")["jobs"], [])
        first = service.post(browser, "/v2/history/jobs", body, "history-first")
        replay = service.post(browser, "/v2/history/jobs", body, "history-first")
        self.assertEqual(first["id"], replay["id"])
        completed = self.wait_for_job(browser, first["id"])
        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(completed["accepted"], 1, completed)
        duplicate = service.post(browser, "/v2/history/jobs", body, "history-second")
        repeated = self.wait_for_job(browser, duplicate["id"])
        self.assertEqual((repeated["accepted"], repeated["duplicates"]), (0, 1), repeated)
        service.stop()
        service.start()
        fresh, _ = service.web()
        self.assertEqual(self.wait_for_job(fresh, first["id"])["accepted"], 1)
        source_row = next(row for row in service.cli("connector", "list")["sources"]
                          if row["source_id"] == source)
        self.assertFalse(source_row["history"]["enabled"])
        service.cli("connector", "sync", source)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            source_row = next(row for row in service.cli("connector", "list")["sources"]
                              if row["source_id"] == source)
            if source_row["history"]["events"]:
                break
            time.sleep(0.05)
        self.assertEqual(source_row["history"]["events"], 1, source_row)
        exported = service.post(fresh, "/v2/data/export", {
            "source_id": source, "from": "2026-11-19T13:00:00Z", "to": "2026-11-19T13:01:00Z",
        })
        self.assertEqual([event["actor_kind"] for event in exported["document"]["events"]],
                         ["user_message", "tool_result"])

    def test_data_export_and_import_receipts_come_from_the_same_vault(self):
        service = self.service
        service.start()
        browser, _ = service.web()
        fixture = support.fixture("source_page.json")
        source = service.post(browser, "/v2/sources", fixture["source"])["source_id"]
        service.cli("connector", "resume", source)
        batch = {**fixture["page"], "source_id": source}
        imported = service.post(browser, "/v2/events?import=true", batch, "data-import")
        self.assertEqual(imported, service.post(browser, "/v2/events?import=true", batch, "data-import"))
        exported = service.cli("connector", "data-export", source,
                               "--from", "2026-11-19T02:00:00Z", "--to", "2026-11-19T02:02:00Z")
        self.assertTrue(exported["filename"].endswith(".json"))
        document = str(exported["document"])
        self.assertIn("run-plan", document)
        self.assertNotIn("run-result", document)
        records = service.cli("connector", "data-records")["records"]
        self.assertEqual([record["kind"] for record in records].count("import"), 1, records)
        self.assertEqual([record["kind"] for record in records].count("export"), 1, records)
        guest = support.BrowserSession(service.endpoint)
        self.assertEqual(guest.request("/v2/data/records")[0], 401)
        self.assertEqual(guest.request("/v2/history/jobs")[0], 401)

    def test_all_local_transcripts_include_archives_and_leave_capture_off(self):
        service = self.service
        fixture = Path(__file__).parent / "data" / "history_codex.jsonl"
        codex = self.config_root / ".codex"
        for folder in ("sessions", "archived_sessions"):
            target = codex / folder
            target.mkdir(parents=True)
            (target / f"rollout-{folder}.jsonl").write_text(
                fixture.read_text().replace("history-fixture", folder), encoding="utf-8",
            )
        claude = self.config_root / ".claude" / "projects" / "first"
        claude.mkdir(parents=True)
        (claude / "session.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": "2026-11-20T00:10:00Z", "sessionId": "c",
            "uuid": "first", "message": {"role": "user", "content": "Claude historical fixture"},
        }) + "\n")
        dsh = self.config_root / ".dsh" / "sessions"
        dsh.mkdir(parents=True)
        (dsh / "session.jsonl").write_text("\n".join(map(json.dumps, [
            {"type": "session", "id": "deepseek-history"},
            {"type": "user/message", "time": "2026-11-20T00:10:00Z", "seq": 1,
             "data": {"id": "first", "source": {"kind": "user-rpc"}, "content": "DSH historical fixture"}},
        ])) + "\n")

        service.start()
        browser, _ = service.web()
        for host, count, accepted in [("codex", 2, 4), ("claude-code", 1, 1), ("dsh", 1, 1)]:
            with self.subTest(host=host):
                source = service.cli("connector", "add", host, "--account", "local")["source_id"]
                batch = service.cli("connector", "history-import-all", source, "--kind", "sessions",
                                    "--idempotency-key", "bulk-history")
                job, = batch["jobs"]
                done = self.wait_for_job(browser, job["id"])
                self.assertEqual((done["state"], done["scope"], done["accepted"]),
                                 ("completed", "all", accepted), done)
                self.assertEqual((done["target_count"], done["completed_targets"]), (count, count))
                self.assertNotIn(str(self.config_root), json.dumps(done))
                row = next(s for s in service.cli("connector", "list")["sources"] if s["source_id"] == source)
                self.assertFalse(row["capture_enabled"])
                self.assertFalse(row["history"]["enabled"])

    @unittest.skipUnless(os.name == "posix", "isolated shell CLI fixtures require POSIX")
    def test_source_settings_reloaded_from_sqlite_after_service_restart(self):
        service = self.service
        tools = service.work / "tools"
        tools.mkdir()
        service.environment["PATH"] = str(tools) + os.pathsep + service.environment["PATH"]
        codex = tools / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o700)
        lark = tools / "lark-cli"
        fixture = Path(__file__).parent / "data" / "feishu_console_cli.sh"
        lark.write_bytes(fixture.read_bytes())
        lark.chmod(0o700)
        blocked_page = service.work / "block-group-discovery"
        service.environment["BMEM_TEST_FEISHU_DISCOVERY_FAILURE_FILE"] = str(blocked_page)
        service.start()
        browser, _ = service.web()
        installed = service.post(browser, "/v2/integrations/codex/install", {
            "capture": True, "share_statements": True,
        })
        source = installed["source_id"]
        service.cli("connector", "pause", source)
        service.post(browser, "/v2/integrations/feishu/start", {})
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            _, view = browser.request("/v2/integrations")
            if view["feishu"]["authorization"] == "connected":
                break
            time.sleep(0.05)
        else:
            self.fail(f"Fixture Feishu authorization did not finish: {view['feishu']}")
        registered = view["feishu"]["accounts"][0]["source_id"]
        before = next(host for host in view["hosts"] if host["host"] == "codex")
        self.assertFalse(before["capture_enabled"], before)
        self.assertTrue(before["shared_statements"], before)
        service.stop()
        (service.root / "run/integrations/codex.json").unlink(missing_ok=True)
        service.environment["BMEM_TEST_FEISHU_STATUS"] = "needs_refresh"
        service.start()
        fresh, _ = service.web()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, view = fresh.request("/v2/integrations")
            self.assertEqual(status, 200, view)
            if view["feishu"]["accounts"][0]["credential_state"] == "needs_refresh":
                break
            time.sleep(0.05)
        else:
            self.fail("The refreshable Feishu credential was not published")
        self.assertEqual(view["feishu"]["authorization"], "connected")
        self.assertEqual(view["feishu"]["accounts"][0]["source_id"], registered)
        restored = next(host for host in view["hosts"] if host["host"] == "codex")
        self.assertTrue(restored["configured"], restored)
        self.assertFalse(restored["capture_enabled"], restored)
        self.assertTrue(restored["shared_statements"], restored)
        service.cli("connector", "resume", registered)
        private = service.cli("connector", "history-options", registered, "--kind", "dm")
        group = service.cli("connector", "history-options", registered, "--kind", "group")
        docs = service.cli("connector", "history-options", registered, "--kind", "docs")
        self.assertEqual([item["id"] for item in private["targets"]], ["oc_private"])
        self.assertEqual([item["id"] for item in group["targets"]], ["oc_group"])
        self.assertEqual(docs["targets"][0]["label"], "Fixture document")
        for kind, target in [("group", "oc_group"), ("docs", docs["targets"][0]["id"])]:
            selection = {"source_id": registered, "kind": kind, "targets": [target]}
            if kind == "group":
                selection.update({"from": "2025-11-01T00:00:00Z", "to": "2025-12-01T00:00:00Z"})
            task = service.post(fresh, "/v2/history/jobs", selection, f"feishu-fixture-{kind}")
            finished = self.wait_for_job(fresh, task["id"])
            self.assertEqual((finished["state"], finished["accepted"]), ("completed", 1), finished)
        service.cli("connector", "pause", registered)
        body = {"source_id": registered, "kinds": ["group", "dm", "docs"]}
        created = service.post(fresh, "/v2/history/jobs", body, "all-feishu")
        replay = service.post(fresh, "/v2/history/jobs", {**body, "kinds": ["docs", "dm", "group"]},
                              "all-feishu")
        self.assertEqual([j["id"] for j in created["jobs"]], [j["id"] for j in replay["jobs"]])
        self.assertEqual(fresh.request("/v2/history/jobs", "POST",
                                       {**body, "kinds": ["dm"]}, key="all-feishu")[0], 409)
        self.assertEqual(fresh.request("/v2/history/jobs", "POST",
                                       {**body, "kinds": []}, key="empty-bulk")[0], 422)
        expected = {"dm": (2, 2, 0), "group": (2, 3, 1), "docs": (3, 2, 1)}
        for job in created["jobs"]:
            done = self.wait_for_job(fresh, job["id"])
            self.assertEqual(done["state"], "completed", done)
            self.assertTrue(done["discovered"])
            targets, accepted, duplicates = expected[job["kind"]]
            self.assertEqual((done["target_count"], done["completed_targets"]), (targets, targets))
            self.assertEqual((done["accepted"], done["duplicates"]), (accepted, duplicates), done)
        # The fixture contains two DMs, four group messages (including a thread)
        # and three documents. All contain "fixture", so categories select by provenance.
        for exclude, count in [(["group"], 5), (["dm", "docs"], 4), (["feishu"], 0)]:
            found = service.post(fresh, "/v2/search", {"query": "fixture", "exclude": exclude})
            self.assertEqual(len(found["results"]), count, (exclude, found))
        row = next(s for s in service.cli("connector", "list")["sources"] if s["source_id"] == registered)
        self.assertFalse(row["capture_enabled"])

        blocked_page.touch()
        group_job, = service.post(fresh, "/v2/history/jobs", {"source_id": registered, "kinds": ["group"]},
                                  "broken-page")["jobs"]
        partial = self.wait_for_job(fresh, group_job["id"])
        self.assertEqual((partial["state"], partial["target_count"]), ("partial", 1), partial)
        self.assertFalse(partial["discovered"])
        self.assertEqual(partial["errors"][0]["target"], "@discover")
        service.stop()
        blocked_page.unlink()
        service.start()
        fresh, _ = service.web()
        service.post(fresh, f'/v2/history/jobs/{group_job["id"]}/retry', {})
        recovered = self.wait_for_job(fresh, group_job["id"])
        self.assertEqual((recovered["state"], recovered["target_count"], recovered["duplicates"]),
                         ("completed", 2, 4), recovered)
        service.stop()
        service.environment["BMEM_TEST_FEISHU_STATUS"] = "expired"
        service.start()
        fresh, _ = service.web()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, view = fresh.request("/v2/integrations")
            self.assertEqual(status, 200, view)
            if view["feishu"]["authorization"] == "reauthorization_required":
                break
            time.sleep(0.05)
        else:
            self.fail("The expired Feishu credential was not published")
