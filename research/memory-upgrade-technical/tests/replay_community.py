"""Exercise versioned community adapter boundaries with original X01 inputs."""

import sqlite3

from baseline_probe import events, new_bound, post, source


COMMUNITY_CASES = {"F04-03", "F04-04", "F04-05", "X01-02", "X01-03", "X01-06", "X01-07", "X01-08", "X01-14"}


def replay_community(case, service, owner):
    name, data = case["id"], case["fixture"]

    def invoke(action, payload, key):
        return post(owner, "/v2/community/" + action, payload, key)

    if name.startswith("F04-"):
        runs, view = {}, {}
        for step in case["steps"]:
            args = step["args"]
            selected = args.get("enabled_channels", args.get("traverse", []))
            channels = {key: data["channel_fixture"][key] for key in selected if key in data["channel_fixture"]}
            response = invoke("ranked-replay", {"channels": channels, "candidate_limit": args["candidate_limit"],
                                                 "k": args.get("k", 60)}, step["id"])
            runs[step["id"]] = response
            view[step["id"]] = {"refs": [row["handle"] for row in response["results"]],
                                 "ranks": {row["handle"]: row["ranks"] for row in response["results"]}}
        return runs, view

    if name == "X01-02":
        sid = source(owner, "capture-bridge")
        boundary = data["old_checkpoint"]
        initial = set(map(tuple, data["initial_records"]))
        pages = data["pages"]

        def version(row):
            return {"native_object": row["id"], "native_version": str(row["version"]),
                    "text": row["text"], "actor_kind": "user_message"}

        overlap = [version(row) for page in pages for row in page["items"] if (row["id"], row["version"]) in initial]
        prefix = post(owner, "/v2/events", {"source_id": sid, "next_cursor": boundary["opaque"],
                                            "events": overlap}, "legacy-page")
        bridge = invoke("capture-bridge", {"source_id": sid, **boundary, "overlap": overlap,
                                          "candidate_contract": "native-page-" + data["new_adapter"]}, "bridge")
        cursor = bridge["expected_cursor"]
        receipts = []
        for page in pages:
            if receipts and page["after"] != cursor:
                raise RuntimeError("replacement adapter lost the native checkpoint")
            receipts.append(post(owner, "/v2/events", {"source_id": sid, "cursor": cursor,
                          "next_cursor": page["end"], "events": [version(row) for row in page["items"]]}, page["end"]))
            cursor = page["end"]
        db_uri = (service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as db:
            current, = db.execute("SELECT cursor FROM ing_sources WHERE source_id=?", (sid,)).fetchone()
            objects = [row[0] for row in db.execute("SELECT native_object FROM ing_event_versions WHERE source_id=? AND active=1 ORDER BY native_object", (sid,))]
            jobs, = db.execute("SELECT count(*) FROM ing_jobs j JOIN ing_event_versions e ON e.event_id=j.event_id WHERE j.source_id=? AND e.native_object=?", (sid, data["initial_records"][0][0])).fetchone()
        view = {"native_checkpoint": current, "native_ids": objects, "active_version_count": len(objects),
                "extraction_jobs_for_obj20": jobs}
        return {"switch": {"legacy": prefix, "bridge": bridge, "pages": receipts, "readback": view}}, {"switch": view}

    if name == "X01-14":
        runs = [invoke("root-replay", {"roots": data["sequence"]}, f"replay-{n}")
                for n in range(data["rerun_count"])]
        return {"run": runs}, {"run": {"first": runs[0]["roots"], "second": runs[1]["roots"],
                                       "compared_with_author_code": False}}

    if name == "X01-08":
        task = post(owner, "/v2/tasks", {"anchor": "graph-fixture"}, "graph-task")["task_id"]
        aliases, sources = events(owner, [{"native_object": edge["root"], "source": edge["root"],
                                            "task": "graph", "text": f"{edge['from']} → {edge['to']}"}
                                           for edge in data["edges"]], {"graph": task})
        browser, auth, reader = new_bound(service, owner, task, data["handoff_reader"])
        for label in data["initial_grants"]:
            service.operation(owner, {"kind": "change_grant", "source_id": sources[label], "purpose": "task_delivery",
                                      "client_id": reader["client_id"], "allow": True}, "graph-grant-" + label)
        handles = {alias: "event_" + event for event, alias in aliases.items()}
        edges = [{**edge, "evidence_handle": handles[edge["root"]]} for edge in data["edges"]]
        graph = {"graph": "fixture-graph"}
        seeded = invoke("replace-graph", {**graph, "edges": edges}, "graph-seed")

        def query():
            status, result = browser.request("/v2/community/graph", "POST", {**graph, "start": edges[0]["from"]}, authentication=auth)
            if status != 200:
                raise RuntimeError(f"bound graph status {status}")
            return result

        built = query()
        revoked = next(edge for edge in edges if edge["id"] == "g2")
        service.operation(owner, {"kind": "change_grant", "source_id": sources[revoked["root"]], "purpose": "task_delivery",
                                  "client_id": reader["client_id"], "allow": False}, "graph-revoke")
        stale = query()
        rebuild = invoke("replace-graph", {**graph, "edges": [edge for edge in edges if edge != revoked]}, "graph-rebuild")
        after = query()
        return {"build": {"write": seeded, "read": built}, "revoke": {"stale": stale, "write": rebuild, "read": after}}, {
            "build": built, "revoke": {"reachable_before_rebuild": stale["reachable"],
                                       "reachable_after_rebuild": after["reachable"], "expanded_refs": after["expanded_refs"]},
        }

    if name == "X01-03":
        aliases, _ = events(owner, [{"native_object": data["root"], "native_version": data["version"],
                                     "text": data["original"]}])
        handle = "event_" + next(iter(aliases))
        modes = {"句号分段": "sentence", "逐子句分段": "clause"}
        slices = [invoke("slice", {"handle": handle, "splitter": modes[mode]}, modes[mode])
                  for mode in data["splitters"]]
        source_bytes = data["original"].encode()
        parts = [part for output in slices for part in output["parts"]]
        agrees = all(source_bytes[part["byte_start"]:part["byte_end"]].decode() == part["quote"] for part in parts)
        return {"slice": slices}, {"slice": {"all_offsets_match_original": agrees,
            "unique_source_versions": sorted({output["source_version"] for output in slices}),
            "quotes_joined": "\n".join(part["quote"] for part in parts)}}

    contents = ({alias: data["notes"][alias]["context"] for alias in data["insertion_order"]}
                if name == "X01-06" else {"B": data["versions"][0]["text"]})
    evidence, _ = events(owner, [{"native_object": alias, "text": value} for alias, value in contents.items()])
    handles = {alias: "event_" + identifier for identifier, alias in evidence.items()}

    def apply(note_id, revision, text, expected=None):
        return invoke("apply-note", {"id": note_id, "revision": revision, "context": text,
                      "expected_revision": expected, "evidence_handle": handles[note_id]}, revision)

    if name == "X01-06":
        for alias in data["insertion_order"]:
            apply(alias, data["notes"][alias]["revision"], contents[alias])
        proposal = invoke("neighbor-proposal", {"retrieved": data["retrieved"], "reply": data["model_reply"]}, "proposal")
        applied = apply(proposal["target_id"], "updated", proposal["context"], proposal["expected_revision"])
        notes = {alias: invoke("note", {"id": alias}, "read-" + alias) for alias in data["insertion_order"]}
        return {"propose": proposal, "apply": applied, "notes": notes}, {"propose": proposal, "apply": {
            "A": notes["A"]["note"], "B": notes["B"]["note"],
            "preserved_revision_ids": notes[proposal["target_id"]]["preserved_revision_ids"],
        }}

    target = data["late_patch"]["target"]
    first, newer = data["versions"]
    apply(target, first["id"], first["text"])
    apply(target, newer["id"], newer["text"], newer["corrects"])
    stale = data["late_patch"]
    status, conflict = owner.request("/v2/community/apply-note", "POST", {
        "id": target, "revision": "late-worker-proposal", "context": stale["context"],
        "expected_revision": stale["expected_revision"], "evidence_handle": handles[target],
    }, "late-proposal")
    readback = invoke("note", {"id": target}, "readback")
    return {"race": {"late_patch_status": status, "late_patch_receipt": conflict, "readback": readback}}, {"race": {
        "current_revision": readback["note"]["revision"], "current_text": readback["note"]["context"],
        "old_patch_applied": 200 <= status < 300,
    }}
