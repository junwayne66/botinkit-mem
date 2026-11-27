"""Use the candidate's opt-in evaluation clock to replay temporal fixtures."""

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


FROZEN_CASES = {
    "F01-01", "F01-02", "F01-03", "F01-04", "F01-05", "F01-06", "F01-07",
    "F01-08", "F01-09", "F01-10", "F01-13", "F01-16", "F01-17", "F01-18",
    "F05-06",
}


class FrozenClock:
    def __init__(self, work, environment, initial):
        self.file = work / "evaluation-clock"
        self.history = []
        environment["BMEM_EVALUATION_CLOCK_FILE"] = str(self.file)
        self.set(initial)

    def set(self, timestamp):
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
        normalized = value.isoformat().replace("+00:00", "Z")
        if not self.history or self.history[-1] != normalized:
            staging = self.file.with_suffix(".next")
            staging.write_text(normalized + "\n", encoding="utf-8")
            staging.replace(self.file)
            self.history.append(normalized)

    def evidence(self, root):
        db_uri = (root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as db:
            received = {sid: known for sid, known in db.execute("SELECT id,known FROM authority_statements")}
        dates = {
            datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
            for value in self.history
        }
        return {"timeline_utc": self.history, "received_at_by_statement": received,
                "applied": bool(received) and set(received.values()) <= dates}


def read_web_snapshot(service, args):
    script = Path(__file__).with_name("replay_clock_web.cjs")
    parameters = {"url": service.cli("web", "--no-open")["url"],
                  "project": args["project"], "valid_at": args["valid_at"]}
    try:
        result = subprocess.run(["node", str(script)], input=json.dumps(parameters),
                                capture_output=True, text=True, encoding="utf-8", timeout=25)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"observation_error": "Chrome replay unavailable"}
    if result.returncode != 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"observation_error": "Chrome replay did not return statement fields"}
    return json.loads(result.stdout)


def replay_clock(case, integration, collect_statements, *, evidence_helpers=None):
    data = case["fixture"]
    service = integration()
    try:
        service.setUp()
        clock = FrozenClock(service.work, service.environment, data.get("clock", data.get("now")))
        service.start()
        owner, _ = service.web()
        zones = {}
        for project, zone in data.get("project_timezones", {}).items():
            status, body = owner.request("/v2/project-timezone", "PUT", {"project": project, "timezone": zone})
            zones[project] = {"status": status, "body": body}
            if status != 200:
                return {"configured_timezones": zones}, {}
        if case["id"] == "F05-06":
            raw, projected = replay_temporary_report(case, service, owner, clock, evidence_helpers)
        else:
            raw, projected = collect_statements(
                case, service, owner, freeze=clock.set,
                web_snapshot=lambda args: read_web_snapshot(service, args),
            )
        raw["evaluation_clock"] = {**clock.evidence(service.root), "configured_timezones": zones}
        return raw, projected if raw["evaluation_clock"]["applied"] else {}
    finally:
        service.doCleanups()


def replay_temporary_report(case, service, owner, clock, helpers):
    post, lookup, dimensions, events, new_bound, load_bound = helpers
    data = case["fixture"]
    task = post(owner, "/v2/tasks", {"anchor": data["task"], "project": data["task"]}, "report-task")["task_id"]
    _, sources = events(owner, [
        {"native_object": row["id"], "native_version": row["version"], "text": row["text"],
         "task": data["task"], "source": "discussion"}
        for row in data["roots"]
    ], {data["task"]: task})
    bound = new_bound(service, owner, task)
    for source in sources.values():
        service.operation(owner, {"kind": "change_grant", "source_id": source, "purpose": "task_delivery",
                                  "allow": True, "client_id": bound[2]["client_id"]}, "report-source")
    rule_ids = {}
    seeded = {}
    for row in data["rules"]:
        scope = {"kind": "global"} if row["scope"] == "global" else {"kind": "project", "project": row["scope"]}
        request = {"original": row["text"], "scope": scope, "share": "authorized_assistants",
                   "valid_from": row["start"]}
        if "end_exclusive" in row:
            request["valid_until"] = row["end_exclusive"]
        seeded[row["id"]] = post(owner, "/v2/statements", request, row["id"])
        for revision in seeded[row["id"]]["dimensions"]:
            rule_ids[revision["revision_id"]] = row["id"]
    raw, projected = {"seeded": seeded}, {}
    for step in case["steps"]:
        name, args = step["id"], step["args"]
        if name == "clock":
            clock.set(args["to"])
            raw[name] = projected[name] = {"advanced_to": args["to"]}
        elif name == "history":
            status, body = lookup(owner, {"project": data["task"], "valid_at": args["as_of"]})
            raw[name] = {"status": status, "body": body}
            if status == 200:
                selected = dimensions(body)["dimensions"].get("report.language", {})
                projected[name] = {"selected_rule_id": rule_ids.get(selected.get("revision_id")),
                                   "text": selected.get("evidence")}
        else:
            status, body = load_bound(*bound, name, task, evidence_query=args["query"])
            raw[name] = {"status": status, "body": body}
            if status == 200:
                projected[name] = {"active_rule_ids": [
                    rule_ids[revision["revision_id"]] for revision in body["statements"]
                ]}
    return raw, projected
