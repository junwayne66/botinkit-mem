"""Owner workbench projections through the native service and its real write paths."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
import datetime
import sqlite3
from threading import Event
import unittest

import test_integration as support


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.service = support.BmemIntegration()
        self.addCleanup(self.service.doCleanups)
        self.service.setUp()
        self.service.start()
        self.owner, _ = self.service.web()

    def get(self, path):
        status, response = self.owner.request(path)
        self.assertEqual(status, 200, response)
        return response

    def test_empty_projection_access_and_bounds(self):
        overview = self.get("/v2/overview?days=30")
        self.assertEqual(overview["days"], 30)
        end = datetime.datetime.fromisoformat(overview["as_of"]).date()
        self.assertEqual([row["date"] for row in overview["activity"]], [
            (end - datetime.timedelta(days=back)).isoformat() for back in reversed(range(30))
        ])
        self.assertTrue(all(not any(row[name] for name in ("statements", "events", "deliveries"))
                            for row in overview["activity"]))
        self.assertFalse(any(overview["counts"].values()))
        self.assertEqual(self.get("/v2/graph")["nodes"], [])
        self.assertEqual(self.owner.request("/v2/overview?days=180")[0], 422)
        self.assertEqual(self.owner.request("/v2/graph?limit=10000")[0], 422)
        self.assertEqual(self.owner.request("/v2/jobs?limit=101")[0], 422)
        self.assertEqual(self.owner.request("/v2/jobs?limit=1&before=bad")[0], 422)
        self.assertEqual(self.owner.request("/v2/jobs?limit=1&status=unknown")[0], 422)
        anon = support.BrowserSession(self.service.endpoint)
        client = self.service.post(self.owner, "/v2/clients", {
            "host": "fixture", "destination": "test-local",
        }, "client")
        for path in ("/v2/overview", "/v2/graph"):
            self.assertEqual(anon.request(path)[0], 401)
            self.assertEqual(anon.request(path, authentication={
                "Authorization": "Bearer " + client["token"], "X-Bmem-Instance": self.service.instance_id,
            })[0], 403)

    def test_projection_membership_and_withdrawal(self):
        service = self.service
        declarations = support.FIXTURE
        declared = service.cli("declare", declarations["initial"], "--scope", "project",
                               "--project", declarations["project_id"])
        case = support.fixture("source_page.json")
        source = service.post(self.owner, "/v2/sources", case["source"], "source")
        source_id = source["source_id"]
        service.operation(self.owner, {"kind": "resume_capture", "source_id": source_id}, "capture")
        receipt = service.post(self.owner, "/v2/events", {
            **case["page"], "source_id": source_id,
        }, "page")

        overview = self.get("/v2/overview")
        self.assertEqual(overview["counts"], {
            "sources": 1, "events": 2, "deliveries": 0, "statements": 1, "jobs": 2,
        })
        self.assertEqual(len(overview["activity"]), 7)
        self.assertEqual(sum(row["events"] for row in overview["activity"]), 2)
        self.assertEqual(sum(row["statements"] for row in overview["activity"]), 1)
        self.assertEqual({i["kind"]: i["count"] for i in overview["distribution"]}, {
            "user_message": 1, "assistant_message": 0, "tool_result": 1, "statement": 1,
        })
        self.assertEqual(sum(row["count"] for row in overview["jobs"]), 2)
        self.assertEqual({r["id"] for r in overview["recent"]},
                         {declared["statement_id"], *(r["event_id"] for r in receipt["events"])})

        first = self.get("/v2/jobs?limit=1&status=pending")
        second = self.get("/v2/jobs?limit=1&status=pending&before=" + first["next_cursor"])
        self.assertIsNone(second["next_cursor"])
        for row in (first["jobs"][0], second["jobs"][0]):
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(row["candidates_recorded"], 0)
            self.assertEqual(row["source_id"], source_id)
            self.assertNotIn("input", row)
        self.assertEqual(first["jobs"][0]["event_id"], receipt["events"][-1]["event_id"])
        self.assertEqual(second["jobs"][0]["event_id"], receipt["events"][0]["event_id"])
        self.assertEqual(self.get("/v2/jobs?limit=1&status=failed")["jobs"], [])
        self.assertEqual(self.get("/v2/jobs")["jobs"],
                         self.get("/v2/jobs?limit=40")["jobs"])

        graph = self.get("/v2/graph")
        self.assertEqual(graph["shown_records"], 3)
        nodes = {node["id"]: node for node in graph["nodes"]}
        groups = {group["id"]: group for group in graph["communities"]}
        statement = nodes[declared["statement_id"]]
        self.assertEqual(statement["label"], declarations["initial"])
        self.assertIn(statement["fact_id"], {r["fact_id"] for r in declared["dimensions"]})
        self.assertEqual(groups[statement["community"]]["label"], declarations["project_id"])
        self.assertCountEqual(
            [edge["kind"] for edge in graph["edges"] if edge["source"] == statement["id"]],
            ["topic", "topic", "scope"],
        )
        events = [nodes[r["event_id"]] for r in receipt["events"]]
        self.assertEqual(events[0]["label"], case["page"]["events"][0]["text"])
        self.assertEqual(groups[events[0]["community"]]["size"], 2)
        self.assertTrue(all(node["kind"] == "event" for node in events))
        for edge in graph["edges"]:
            self.assertIn(edge["source"], nodes)
            self.assertEqual(nodes[edge["target"]]["kind"], "community")
        sample = self.get("/v2/graph?limit=1")
        self.assertEqual((sample["shown_records"], sample["total_records"], sample["truncated"]),
                         (1, 3, True))
        for key, target in (("source_id", source_id), ("statement_id", declared["statement_id"])):
            service.operation(self.owner, {"kind": "forget", "target": {key: target}}, "forget-" + key)
        self.assertEqual(self.get("/v2/graph")["nodes"], [])
        after = self.get("/v2/overview")
        self.assertEqual(after["recent"], [])
        self.assertEqual(after["counts"]["events"], 0)
        self.assertEqual(after["counts"]["statements"], 0)
        self.assertEqual(after["jobs"], [{"status": "cancelled", "count": 2}])

    def test_status_reads_committed_state_while_writer_waits(self):
        service = self.service
        case = support.fixture("source_page.json")
        source = service.post(self.owner, "/v2/sources", case["source"], "source")
        source_id = source["source_id"]
        service.operation(self.owner, {"kind": "resume_capture", "source_id": source_id}, "capture")
        service.post(self.owner, "/v2/events", {
            **case["page"], "source_id": source_id,
        }, "page")
        job_id = self.get("/v2/jobs")["jobs"][0]["job_id"]
        started = Event()

        def write():
            started.set()
            return self.owner.request(f"/v2/jobs/{job_id}/retry", "POST", {})

        with closing(sqlite3.connect(service.root / "data/bmem.sqlite3")) as writer:
            with writer:
                writer.execute("UPDATE ing_jobs SET status='failed' WHERE job_id=?", (job_id,))
            writer.execute("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(max_workers=6) as executor:
                pending_write = executor.submit(write)
                try:
                    self.assertTrue(started.wait(1))
                    with self.assertRaises(TimeoutError):
                        pending_write.result(timeout=0.15)
                    paths = ("/v2/tasks", "/v2/sources", "/v2/overview", "/v2/jobs",
                             "/v2/deliveries/ledger")
                    pending_reads = {p: executor.submit(self.get, p) for p in paths}
                    results = {p: future.result(timeout=2) for p, future in pending_reads.items()}
                    self.assertEqual(results["/v2/tasks"]["tasks"], [])
                    self.assertEqual(results["/v2/overview"]["counts"]["sources"], 1)
                    self.assertEqual(results["/v2/sources"]["sources"][0]["source_id"], source_id)
                    self.assertEqual(results["/v2/jobs"]["jobs"][0]["status"], "failed")
                finally:
                    writer.rollback()
                status, job = pending_write.result(timeout=2)
                self.assertEqual(status, 202, job)
        self.assertEqual(self.get("/v2/jobs")["jobs"][0]["status"], "pending")

    def test_overview_backfills_existing_records_once(self):
        service = self.service
        case = support.fixture("source_page.json")
        source = service.post(self.owner, "/v2/sources", case["source"], "source")
        source_id = source["source_id"]
        service.operation(self.owner, {"kind": "resume_capture", "source_id": source_id}, "capture")
        service.post(self.owner, "/v2/events", {
            **case["page"], "source_id": source_id,
        }, "page")
        before = self.get("/v2/overview")
        self.assertEqual(before["counts"]["events"], 2)

        service.stop()
        with closing(sqlite3.connect(service.root / "data/bmem.sqlite3")) as old_db:
            with old_db:
                triggers = old_db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB 'dashboard_*'"
                ).fetchall()
                for (name,) in triggers:
                    old_db.execute("DROP TRIGGER " + name)
                old_db.execute("DROP TABLE dashboard_counts")

        for _ in range(2):
            service.start()
            self.owner, _ = service.web()
            after = self.get("/v2/overview")
            for field in ("counts", "distribution", "jobs"):
                self.assertEqual(after[field], before[field])
            service.stop()
