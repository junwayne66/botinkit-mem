"""Drive the frozen archive fixtures through the native task, page, and extraction APIs."""

from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Event, Thread
from time import monotonic, sleep
from urllib.parse import urlencode


CASE_IDS = frozenset("F03-%02d" % number for number in range(1, 16) if number != 4)


def extractor(service, configuration):
    calls = []
    entered = Event()
    release = Event()
    responded = Event()

    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/api/chat":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            evidence = json.loads(request["messages"][-1]["content"])
            probe = evidence.get("event_id") == "probe"
            status = 200
            result = {"candidates": []}
            if not probe:
                calls.append(evidence)
                entered.set()
                if configuration.get("on_call") == "block_until_release":
                    if not release.wait(25):
                        status = 503
                    else:
                        source = configuration["candidate_inputs"][0]
                        result = {"candidates": [{"kind": "fact", "subject": "archive", "key": "keep",
                                                 "value": source["proposal"],
                                                 "support": [evidence["inputs"][0]["span_id"]]}]}
                elif len(calls) == 1:
                    status = configuration.get("first_call_http", 200)
                    result = configuration.get("response", result)
                    if configuration.get("candidate_inputs"):
                        result = {"candidates": [{
                            "kind": "fact", "subject": "archive", "key": "source_observation",
                            "value": item["proposal"], "support": [next((span["span_id"] for span in evidence["inputs"]
                                if span["value"] == item["quote"]), "unknown/" + item["span"])],
                        } for item in configuration["candidate_inputs"]]}
                else:
                    status = configuration.get("second_call_http", 200)
                    result = configuration.get("second_body", result)
            body = json.dumps({"done": True, "done_reason": "stop",
                               "message": {"content": json.dumps(result)}}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            finally:
                if not probe:
                    responded.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        release.set()
        server.shutdown()
        thread.join()
        server.server_close()

    service.addCleanup(stop)
    route = f"http://127.0.0.1:{server.server_port}"
    service.cli("model", "add", "archive-test", "--protocol", "ollama",
                "--endpoint", route, "--model-id", "archive-test")
    service.cli("model", "test", "archive-test", "--for", "extract")
    service.cli("model", "use", "archive-test", "--for", "extract")
    return calls, entered, release, responded


def replay(case, service, owner):
    from baseline_probe import post

    identifier = case["id"]
    fixture = case["fixture"]
    steps = {step["id"]: step["args"] for step in case["steps"]}
    raw, projected = {}, {}
    source = post(owner, "/v2/sources", {
        "type": "archive", "account": fixture["default_source"],
        "categories": ["user_message", "assistant_message", "tool_result"],
    }, "register-archive")["source_id"]
    service.operation(owner, {"kind": "resume_capture", "source_id": source}, "enable-archive")
    db_path = service.root / "data/bmem.sqlite3"
    read_uri = db_path.as_uri() + "?mode=ro"
    source_events = fixture["source_events"]
    tasks = {name: post(owner, "/v2/tasks", {"anchor": name, "project": name}, f"archive-task-{index}")["task_id"]
             for index,name in enumerate(set(fixture.get("scope", {}).values()))}

    def start(targets, interval=None):
        request = {"source_id": source, "kind": "archive", "targets": targets, "content": "all"}
        if interval:
            start, through = interval
            request["from"] = f"{start}T00:00:00Z"
            request["to"] = f"{date.fromisoformat(through) + timedelta(days=1)}T00:00:00Z"
        preview = post(owner, "/v2/history/preview", request, "dry-preview")
        with sqlite3.connect(read_uri, uri=True) as db:
            committed = db.execute("SELECT count(*) FROM history_job_pages").fetchone()[0]
            versions = db.execute("SELECT count(*) FROM ing_event_versions WHERE source_id=?", (source,)).fetchone()[0]
        preview.update(committed_page_count=committed, ingested_version_count=versions)
        job = post(owner, "/v2/history/jobs", request, "archive-confirm")
        return preview, job

    def event(row):
        output = {"native_object": row["native_object"], "native_version": row["version_key"],
                  "at": row.get("day", "2026-11-20") + "T00:00:00Z"}
        if row.get("role") == "tool":
            output.update(actor_kind="tool_result", checks={"run_id":row["run_id"],
                          "tool_call_id":row["tool_call_id"], "content":row["text"]})
        else:
            output.update(actor_kind="assistant_message" if row.get("role") == "assistant" else "user_message",
                          text=row["text"])
        if name := fixture.get("scope", {}).get(row["native_object"]):
            output["task_id"] = tasks[name]
        return output

    def send(job, target, rows, cursor, next_cursor, key, *, complete=False, failure=None):
        request = {"target": target, "cursor": cursor, "next_cursor": next_cursor,
                   "events": [event(row) for row in rows], "complete": complete}
        relays = [{"native_object": row["native_object"], "native_version": row["version_key"],
                   "cites_source_id": source, "cites_object": row["quotes"]["object"],
                   "cites_version": row["quotes"]["version"], "cites_field": "/text"}
                  for row in rows if "quotes" in row]
        if relays:
            request["relays"] = relays
        if failure:
            request["source_error"] = failure
        return owner.request(f"/v2/history/jobs/{job['id']}/pages", "POST", request, key)

    def accepted(*args, **options):
        status, response = send(*args, **options)
        if status != 200:
            raise ValueError((status, response))
        return response

    def results(job, **query):
        route = f"/v2/history/jobs/{job['id']}/results"
        if query:
            route += "?" + urlencode(query)
        status, response = owner.request(route)
        if status != 200:
            raise ValueError((status, response))
        return response

    def counts(job):
        with sqlite3.connect(read_uri, uri=True) as db:
            linked = db.execute("SELECT count(*) FROM history_job_pages WHERE task_id=?", (job["id"],)).fetchone()[0]
            versions = db.execute("SELECT count(*) FROM ing_event_versions WHERE source_id=?", (source,)).fetchone()[0]
            queue = db.execute("SELECT count(*) FROM ing_jobs WHERE source_id=?", (source,)).fetchone()[0]
        return {"linked_page_count": linked, "distinct_versions": versions, "extract_job_count": queue}

    def listed(response):
        return [row["native_object"] for row in response["rows"]]

    def drain(job, state, every=False):
        end = monotonic() + 15
        actual = None
        while monotonic() < end:
            actual = results(job)
            checked = actual["rows"] if every else actual["rows"][:1]
            if checked and all(row["extract"]["state"] == state for row in checked):
                return actual
            sleep(0.04)
        raise RuntimeError((identifier, "extraction did not reach " + state, actual))

    if identifier == "F03-01":
        args = steps["preview"]
        preview, job = start(args["targets"], (args["from_day"], args["through_day"]))
        commits = {target: accepted(job, target, [row for row in source_events if row["source"] == target],
                                    "start", "selected", "page-" + target, complete=True)
                   for target in args["targets"]}
        outcome = results(job)
        raw = {"preview": preview, "confirm": job, "pages": commits, "results": outcome}
        projected = {"preview": preview, "confirm": {**counts(job),
                     "target_count": len(outcome["job"]["items"]), "native_objects": listed(outcome)}}
    elif identifier == "F03-02":
        targets = steps["import"]["target_sources"]
        preview, job = start(targets)
        commits = {target: accepted(job, target, [row for row in source_events if row["source"] == target],
                                    "start", "terminal", "page-" + target, complete=True,
                                    failure="archive read failure" if fixture["connector"][target] == "fail_before_first_page" else None)
                   for target in targets}
        outcome = results(job)
        raw = {"preview": preview, "import": job, "pages": commits, "results": outcome}
        projected = {"import": job, "audit": {**counts(job), "target_count": len(outcome["job"]["items"]),
                     "targets": {item["target"]: {"state": item["state"], "captured_versions": item["accepted"]}
                                 for item in outcome["job"]["items"]}}}
    elif identifier == "F03-03":
        page = fixture["page"]
        preview, job = start([page["target"]])
        rows = [item for item in source_events if item["native_object"] in page["members"]]
        receipts = [accepted(job, page["target"], rows, "start", page["source_cursor"],
                             "stable-archive-page", complete=True)
                    for _ in range(steps["first"]["send_count"])]
        if any(receipt != receipts[0] for receipt in receipts[1:]):
            raise ValueError("same archive page key did not replay the original receipt")
        outcome = results(job)
        raw = {"preview": preview, "job": job, "receipts": receipts, "audit": outcome}
        projected = {"first": receipts[0], "audit": {**counts(job), "page_item_count": len(outcome["rows"])}}
    elif identifier == "F03-05":
        target = str(fixture["pages"][0]["target"])
        preview, job = start([target])
        pages = {page["page_seq"]: [row for row in source_events if row["native_object"] in page["members"]]
                 for page in fixture["pages"]}
        p1 = accepted(job, target, pages[1], "start", "first", "page-1")
        first = results(job, limit=steps["p1"]["limit"])
        p2 = accepted(job, target, pages[2], "first", "latest", "page-2", complete=True)
        second = results(job, cursor=first["next_cursor"], limit=steps["p2"]["limit"])
        fresh = results(job, limit=steps["fresh"]["limit"])
        raw = {"preview": preview, "job": job, "p1": {"page":p1,"results":first},
               "p2": {"page":p2,"results":second}, "fresh": fresh}
        projected = {key: {"native_objects": listed(result), "has_next": bool(result["next_cursor"])}
                     for key, result in (("p1", first), ("p2", second), ("fresh", fresh))}
    elif identifier == "F03-06":
        page = fixture["page"]
        target = "recovery"
        preview, job = start([target])
        rows = [item for item in source_events if item["native_object"] in page["members"]]
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TRIGGER fixture_abort_archive_checkpoint BEFORE UPDATE OF cursor ON history_task_items "
                       "BEGIN SELECT RAISE(ABORT, 'fixture: after intake, before checkpoint'); END")
        status, failure = send(job, target, rows, page["input_cursor"], page["next_cursor"],
                               "archive-crash-replay", complete=True)
        if status != 500 or "fixture: after intake, before checkpoint" not in failure["message"]:
            raise ValueError((status, failure))
        service.stop()
        with sqlite3.connect(db_path) as db:
            db.execute("DROP TRIGGER fixture_abort_archive_checkpoint")
            checkpoint = db.execute("SELECT cursor FROM history_task_items WHERE task_id=? AND target=?",
                                    (job["id"], target)).fetchone()[0]
        service.start()
        owner, _ = service.web()
        crash = {**counts(job), "checkpoint": json.loads(checkpoint) if checkpoint is not None else "start"}
        retry = accepted(job, target, rows, page["input_cursor"], page["next_cursor"], "archive-crash-replay",
                         complete=True)
        raw = {"preview": preview, "job": job, "crash": {"status": status, "body": failure, "observed": crash},
               "retry": retry, "results": results(job)}
        projected = {"crash": crash, "retry": {**counts(job), "checkpoint": retry["checkpoint"]}}
    elif identifier == "F03-07":
        calls, _, _, _ = extractor(service, {})
        preview, job = start(["journal"])
        control = f"/v2/history/jobs/{job['id']}/extract-controls"
        paused = post(owner, control, {"paused": True}, "extract-paused")
        intake = accepted(job, "journal", source_events, "start", "loaded", "ingest-with-worker-paused", complete=True)
        service.operation(owner, {"kind":"change_grant", "source_id":source,
                                  "purpose":"extract", "allow":True}, "extraction-approved")
        imported = results(job)
        named = {row["native_object"]: row["event_id"] for row in imported["rows"]}
        closed = named[steps["restrict"]["root"]]
        with sqlite3.connect(read_uri, uri=True) as db:
            root = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (closed,)).fetchone()[0]
        verdict = post(owner, control, {"root_id":root, "deny":True}, "exclude-one-extract-root")
        limited = results(job)
        resumed = post(owner, control, {"paused":False}, "resume-extract")
        completed = drain(job,"done")
        counts_by_root = {name:sum(call["event_id"] == event_id for call in calls) for name,event_id in named.items()}
        raw = {"preview":preview,"import":intake,"paused":paused,"restriction":verdict,"restrict":limited,
               "resume":resumed,"audit":completed,"model_calls":calls}
        projected = {"import":intake,"restrict":{"by_object":{row["native_object"]:row for row in limited["rows"]}},
                     "drain":resumed,"audit":{"by_object":{row["native_object"]:row for row in completed["rows"]},
                                              "model_calls_by_root":counts_by_root}}
    elif identifier in ("F03-10", "F03-11", "F03-12", "F03-13"):
        calls, _, _, _ = extractor(service, fixture.get("test_extractor", {}))
        targets = list(dict.fromkeys(row.get("source", "journal") for row in source_events))
        preview, job = start(targets)
        intake = {target:accepted(job,target,[row for row in source_events if row.get("source","journal") == target],
                                  "start","loaded","native-page-"+target,complete=True) for target in targets}
        service.operation(owner, {"kind":"change_grant", "source_id":source,
                                  "purpose":"extract", "allow":True}, "process-archive-candidates")
        completed = drain(job,"done",every=True)
        raw = {"preview":preview,"import":intake,"audit":completed,"model_calls":calls}
        projected = {"import":intake,"drain":{"job":completed["job"]}}
        if identifier in ("F03-10", "F03-11"):
            audit = {**counts(job), "rows":[{**row, "decision_kinds":[decision["kind"] for decision in row["decisions"]],
                     "all_decisions_have_reasons":all(bool(decision["reason"]) for decision in row["decisions"])}
                     for row in completed["rows"]],
                     "unique_verified_observations":sum(row["candidate_counts"]["verified"] for row in completed["rows"])}
            projected["audit"] = audit
        else:
            provenance = {row["native_object"]:row["provenance"] for row in completed["rows"]}
            if identifier == "F03-12":
                projected["trace"] = {"relay_root_map":{name:lineage["origin_object"] for name,lineage in provenance.items()
                                      if lineage["relation"] == "reported"}, "independent_declaration_roots":
                                      sorted({lineage["origin_object"] for lineage in provenance.values() if lineage["relation"] == "source"}),
                                      "independent_tool_runs":sorted({lineage["run_id"] for lineage in provenance.values()
                                                                       if lineage["relation"] == "tool_run"})}
            else:
                beta = provenance[steps["trace"]["pair"][1]]
                projected["trace"] = {**counts(job), "relation":beta["relation"],
                                      "extra_independent_support_from_beta":int(beta["relation"] == "source")}
    elif identifier in ("F03-08", "F03-09", "F03-15"):
        configuration = fixture["test_extractor"]
        calls, entered, release, responded = extractor(service, configuration)
        preview, job = start(["journal"])
        is_revocation = identifier == "F03-15"
        intake = accepted(job, "journal", source_events[:1] if is_revocation else source_events,
                          "start", "first", "archive-intake", complete=not is_revocation)
        service.operation(owner, {"kind": "change_grant", "source_id": source,
                                  "purpose": "extract", "allow": True}, "start-extract")
        if identifier == "F03-09":
            failed = drain(job, "failed")
            with sqlite3.connect(read_uri, uri=True) as db:
                queue_id = db.execute("SELECT job_id FROM ing_jobs WHERE source_id=?", (source,)).fetchone()[0]
            receipt = post(owner, f"/v2/jobs/{queue_id}/retry", {}, "retry-once")
            completed = drain(job, "done")
            raw = {"preview": preview, "import": intake, "fail": failed, "retry_receipt": receipt, "retry": completed}
            projected = {"import": intake, "fail": failed,
                         "retry": {**completed, **counts(job), "model_call_count": len(calls)}}
        elif identifier == "F03-08":
            completed = drain(job, "done")
            handle = "event_" + completed["rows"][0]["event_id"]
            evidence = post(owner, "/v2/evidence/expand", {"handle": handle}, "reopen-original")
            raw = {"preview": preview, "import": intake, "audit": completed, "open": evidence}
            projected = {"import": intake, "drain": completed["rows"][0]["extract"], "audit": completed,
                         "open": evidence}
        else:
            if not entered.wait(10):
                raise ValueError("extraction never started for the original root")
            stored = results(job)
            event_id = stored["rows"][0]["event_id"]
            with sqlite3.connect(read_uri, uri=True) as db:
                root = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (event_id,)).fetchone()[0]
            forgotten = service.operation(owner, {"kind": "forget", "target": {"root_id": root}}, "forget-root")
            late = accepted(job, "journal", source_events[1:], "first", "late", "delayed-page", complete=True)
            release.set()
            if not responded.wait(10):
                raise ValueError("in-flight extractor did not return")
            # A graceful service shutdown joins the extraction worker before
            # inspecting the durable ledger, so absence cannot be a timing gap.
            daemon = service.daemon
            service.stop()
            if daemon.returncode != 0:
                raise ValueError("archive service did not drain the in-flight worker")
            service.start()
            owner, _ = service.web()
            outcome = drain(job, "cancelled")
            status, evidence = owner.request("/v2/evidence/expand", "POST", {"handle": "event_" + event_id})
            with sqlite3.connect(read_uri, uri=True) as db:
                active = db.execute("SELECT count(*) FROM ing_event_versions WHERE root_id=? AND active=1", (root,)).fetchone()[0]
                delivered = db.execute("SELECT count(*) FROM ing_candidates c JOIN ing_jobs j USING(job_id) "
                                       "JOIN ing_event_versions e ON e.event_id=j.event_id WHERE e.root_id=?", (root,)).fetchone()[0]
                tombstone = bool(db.execute("SELECT EXISTS(SELECT 1 FROM ing_root_revocations WHERE root_id=?)",
                                           (root,)).fetchone()[0])
            raw = {"preview": preview, "begin": intake, "forget": forgotten, "late": late, "finish": outcome,
                   "expand": {"status": status, "body": evidence}}
            projected = {"begin": intake, "forget": {"active_versions_for_root": active},
                         "finish": {"delivered_candidate_count": delivered, "old_handle_readable": status == 200,
                                    "tombstone_present": tombstone}}
        raw["model_calls"] = calls
    elif identifier == "F03-14":
        preview, job = start(["journal"])
        first = accepted(job, "journal", source_events, "start", "loaded", "initial-versions")
        failed = accepted(job, "journal", [], "loaded", "after-failure", "offline-report",
                          failure="remote source unavailable")
        outcome = results(job)
        evidence = {row["native_version"]: post(owner, "/v2/evidence/expand",
                    {"handle": "event_" + row["event_id"]}, "expand-" + row["native_version"])
                    for row in outcome["rows"]}
        raw = {"preview": preview, "import": first, "offline": failed, "results": outcome, "evidence": evidence}
        projected = {"import": first, "offline": {"local_text_by_version": {
            version: value["text"] for version, value in evidence.items()},
            "remote_head_verified": outcome["job"]["state"] == "completed"}}
    else:
        raise ValueError("unsupported archive replay " + identifier)
    return raw, projected
