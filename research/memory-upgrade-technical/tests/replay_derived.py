"""Exercise derived summaries with the exact F05 support groups and root withdrawals."""

import sqlite3


CASES = frozenset({"F05-09", "F05-10"})


def replay(case, service, owner):
    from baseline_probe import events, new_bound, post

    fixture = case["fixture"]
    steps = {step["id"]: step["args"] for step in case["steps"]}
    task = post(owner, "/v2/tasks", {"anchor": fixture["task"], "project": "青禾"}, "derived-task")["task_id"]
    evidence, sources = events(owner, [{
        "native_object": row["id"], "native_version": row["version"], "ref": row["version"],
        "text": row["text"], "at": fixture["now"], "task": fixture["task"],
    } for row in fixture["roots"]], {fixture["task"]: task})
    handles = {alias: "event_" + event for event, alias in evidence.items()}
    reader, auth, registration = new_bound(service, owner, task, "derived-reader")
    for source in sources.values():
        service.operation(owner, {"kind": "change_grant", "source_id": source, "purpose": "task_delivery",
                                  "client_id": registration["client_id"], "allow": True}, "allow-fixture-sources")
    raw, projected, seeds = {}, {}, {}
    for derived in fixture["derived"]:
        result = post(owner, "/v2/derived/put", {
            "task_id": task, "text": derived["text"],
            "support_groups": [[handles[alias] for alias in group] for group in derived["support_groups"]],
        }, "derived-" + derived["id"])
        handles[derived["id"]] = result["revision_id"]
        seeds[derived["id"]] = result

    aliases = {handle: name for name, handle in handles.items()}

    def handoff(query):
        status, result = reader.request(f"/v2/tasks/{task}/handoff", authentication=auth)
        if status != 200:
            raise ValueError((status, result))
        received = result.get("derived", [])
        view = {"derived_ids": [aliases.get(row["revision_id"], row["revision_id"]) for row in received],
                "root_versions": [evidence[row["event_id"]] for row in result["evidence"]]}
        if received:
            view.update(evidence=[aliases[item] for item in received[0]["evidence"]],
                        text=received[0]["text"], published_ids=view["derived_ids"])
        return {"query": query, "status": status, "response": result}, view

    target = steps["pv"]["command"]["root"]
    withdrawn = next(row for row in fixture["roots"] if row["id"] == target)
    event_id = handles[withdrawn["version"]].removeprefix("event_")
    db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        root = db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", (event_id,)).fetchone()[0]
    command = {"kind": "forget", "target": {"root_id": root}}
    for step in case["steps"]:
        name, args = step["id"], step["args"]
        if name in ("before", "after", "rebuilt"):
            raw[name], projected[name] = handoff(args["query"])
        elif name == "pv":
            raw[name] = projected[name] = post(owner, "/v2/operations/preview", command, "derived-forget-preview")
        elif name == "ok":
            raw[name] = projected[name] = post(owner, "/v2/operations",
                {**command, "preview_id": raw["pv"]["preview_id"]}, "derived-forget-confirm")
        elif name == "old":
            status, result = reader.request("/v2/derived/get", "POST",
                {"task_id": task, "revision_id": handles[args.get("id", args.get("revision"))]}, authentication=auth)
            raw[name] = {"status": status, "response": result}
            projected[name] = result
        elif name == "drain":
            raw[name] = projected[name] = post(owner, "/v2/derived/rebuild", {"task_id": task}, "rebuild-candidates")
    return {**raw, "seed": seeds}, projected
