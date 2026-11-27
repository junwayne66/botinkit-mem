"""Exercise task selection and evidence-based handoff cards against the real service."""

from http.server import ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlencode

from baseline_probe import BrowserSession, isolated, load_bound, post, source
from test_task_delivery import FixtureModel


TASK_LOOKUP_CASES = {"F02-11", "F02-12"}


def replay_task_lookup(case):
    data = case["fixture"]
    with isolated() as (service, owner):
        titles = data.get("task_titles", {})
        tasks = {}
        for row in data["tasks"]:
            payload = {key: row[key] for key in ("project", "anchor")}
            alias = row["id"]
            if alias in titles:
                payload["title"] = titles[alias]
            tasks[alias] = post(owner, "/v2/tasks", payload, alias)["task_id"]
        record = post(owner, "/v2/clients", {"host": "codex", "destination": "task-lookup-fixture",
                                            "capabilities": {"load_context": True}}, "codex")
        client = record["client_id"]
        client_headers = {"Authorization": "Bearer " + record["token"], "X-Bmem-Instance": service.instance_id}
        browser = BrowserSession(service.endpoint)
        for task in data.get("task_access", {}).get("Codex", tasks):
            service.operation(owner, {"kind": "change_grant", "client_id": client, "task_id": tasks[task],
                                      "allow": True}, "task-" + task)
        service.operation(owner, {"kind": "change_grant", "client_id": client,
                                  "share_statements": True}, "shared-statements")
        sources = {label: source(owner, label) for label in data["sources"]}
        for label, recipients in data["grants"].items():
            if "Codex" in recipients:
                service.operation(owner, {"kind": "change_grant", "source_id": sources[label],
                                          "purpose": "task_delivery", "client_id": client, "allow": True}, "source-" + label)
        seed = {"task_aliases": tasks, "sources": sources}
        if case["id"] == "F02-11":
            return replay_selection(case, owner, browser, record, client_headers, seed)
        return replay_plan_card(case, service, owner, browser, record, client_headers, seed)


def replay_selection(case, owner, browser, client, headers, seed):
    name = case["steps"][1]["args"]["name"]
    before = owner.request("/v2/deliveries")[1]["deliveries"]
    status, response = browser.request("/v2/tasks?" + urlencode({"name": name}), authentication=headers)
    after = owner.request("/v2/deliveries")[1]["deliveries"]
    raw = {"seed": seed, "select": {"status": status, "body": response, "delivery_count_before": len(before),
                                   "delivery_count_after": len(after)}}
    if status != 200:
        return raw, {}
    aliases = {identifier: alias for alias, identifier in seed["task_aliases"].items()}
    items = response["tasks"]
    selection = {
        "candidate_ids": [aliases.get(item["task_id"], item["task_id"]) for item in items],
        "rendered_text": "\n".join(row["title"] + " · " + row["anchor"] for row in items),
        "automatic_delivery_count": len(after) - len(before),
    }
    chosen = next((row for row in items if row["anchor"] == case["steps"][2]["args"]["choice"]), None)
    if chosen is None:
        return raw, {"seed": seed, "select": selection}
    status, attempt = browser.request(f"/v2/tasks/{chosen['task_id']}/attempts", "POST", {
        "route_ref": client["route_ref"], "session_id": "explicit-selection", "context_epoch": "1",
    }, "select-user-choice", authentication=headers)
    raw["choose"] = {"status": status, "body": attempt}
    return raw, {"seed": seed, "select": selection, "choose": {
        "bound_task": aliases.get(attempt.get("task_id")),
    }}


def replay_plan_card(case, service, owner, browser, client, headers, seed):
    configure_extractor(service)
    data = case["fixture"]
    captured = []
    for evidence in data["events"]:
        sid = seed["sources"][evidence["source"]]
        service.operation(owner, {"kind": "change_grant", "source_id": sid,
                                  "purpose": "extract", "allow": True}, "plan-extraction")
        fields = {key: evidence[key] for key in ("native_object", "native_version", "actor_kind", "at", "text")}
        receipt = post(owner, "/v2/events", {"source_id": sid, "next_cursor": "1",
                       "events": [{**fields, "task_id": seed["task_aliases"][evidence["task"]]}]}, "seed-plan")
        captured.extend(receipt["events"])
    raw = {"seed": {**seed, "events": captured}}
    jobs = completed_extractions(owner, [event["event_id"] for event in captured])
    if not jobs:
        return raw, {}
    raw["seed"]["completed_jobs"] = jobs
    args = case["steps"][1]["args"]
    task = seed["task_aliases"][args["task"]]
    status, package = load_bound(browser, headers, client, args["session_id"], task,
                                 args["budget"]["evidence_tokens"])
    raw["load"] = {"status": status, "body": package}
    if status != 200:
        return raw, {}
    status, card = browser.request(f"/v2/deliveries/{package['delivery_id']}/handoff-summary",
                                   authentication=headers)
    timeline_status, timeline = browser.request(f"/v2/tasks/{task}/handoff", authentication=headers)
    raw["audit"] = {"status": status, "body": card, "timeline_status": timeline_status, "timeline": timeline}
    if status != 200 or timeline_status != 200:
        return raw, {}
    return raw, {"seed": raw["seed"], "load": package, "audit": card}


def configure_extractor(service):
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    worker = Thread(target=model_server.serve_forever)
    worker.start()

    def stop_model():
        model_server.shutdown()
        worker.join()
        model_server.server_close()

    service.addCleanup(stop_model)
    endpoint = f"http://127.0.0.1:{model_server.server_port}"
    service.cli("model", "add", "replay-fixture", "--protocol", "ollama", "--endpoint", endpoint,
                "--model-id", "replay-fixture")
    service.cli("model", "test", "replay-fixture", "--for", "extract")
    service.cli("model", "use", "replay-fixture", "--for", "extract")


def completed_extractions(owner, event_ids):
    event_ids = set(event_ids)
    deadline = monotonic() + 10
    while monotonic() < deadline:
        jobs = [row for row in owner.request("/v2/jobs")[1]["jobs"] if row["event_id"] in event_ids]
        if len(jobs) == len(event_ids) and all(row["status"] == "done" for row in jobs):
            return jobs
        sleep(0.05)
    return []
