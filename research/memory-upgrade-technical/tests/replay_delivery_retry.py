"""Verify that old handles and late receipts preserve delivery boundaries."""

import hashlib
import json
from urllib.request import ProxyHandler, Request, build_opener

from baseline_probe import evidence_refs, events, fixture, isolated, native, new_bound, post
from replay_task_lookup import completed_extractions, configure_extractor


DELIVERY_RETRY_CASES = {"F02-13", "F02-14"}


def replay_delivery_retry(case):
    data = case["fixture"]
    args = {step["id"]: step["args"] for step in case["steps"]}
    with isolated() as (service, owner):
        task = post(owner, "/v2/tasks", data["tasks"][0], "task")["task_id"]
        browser, headers, client = new_bound(service, owner, task)
        aliases, sources = events(owner, data["events"], {"T1": task})
        configure_extractor(service)
        for label, source in sources.items():
            for purpose, actor in (("task_delivery", {"client_id": client["client_id"]}), ("extract", {})):
                service.operation(owner, {"kind": "change_grant", "source_id": source, "allow": True,
                                          "purpose": purpose, **actor}, f"{label}-{purpose}")
        seed = completed_extractions(owner, aliases)
        raw = {"seed": {"aliases": aliases.copy(), "extractions": seed}}
        if not seed:
            return raw, {}
        projected = {"seed": raw["seed"]}

        def load(step, retained=None):
            call = args[step]
            session, epoch = call["session_id"], call["context_epoch"]
            request = {"session_id": session, "context_epoch": epoch, "route_ref": client["route_ref"],
                       "task_id": task, "budget": call["budget"]}
            if "append_only" in call:
                request["append_only"] = call["append_only"]
            status, attempt = browser.request(f"/v2/tasks/{task}/attempts", "POST", request,
                                              f"attempt:{session}:{epoch}", authentication=headers)
            if status != 201:
                raise ValueError(attempt)
            request["attempt_id"] = attempt["attempt_id"]
            if retained:
                request.update(retained)
            status, result = browser.request("/v2/context/load", "POST", request,
                                             call.get("idempotency_key", step), authentication=headers)
            raw[step] = {"status": status, "body": result}
            projected[step] = result
            return result if status == 200 else None

        def ledger():
            status, result = owner.request("/v2/deliveries")
            if status != 200:
                raise ValueError(result)
            return {row["delivery_id"]: row for row in result["deliveries"] if row["client_id"] == client["client_id"]}

        with native.fixture_server(fixture("native_provider.json")) as (endpoint, captured):
            def observe(label, package, acknowledge=False):
                wire = {name: package[name] for name in ("evidence", "observations", "statements")}
                encoded = json.dumps({"model": "fixture", "tools": [], "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": json.dumps(wire, ensure_ascii=False)}]}
                ]}).encode("utf-8")
                req = Request(endpoint + "/v1/responses", data=encoded, headers={"Content-Type": "application/json"})
                with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
                    response.read()
                actual_text = captured["codex"][-1]["input"][0]["content"][0]["text"]
                actual = json.loads(actual_text)
                raw[label] = {"model_input": actual}
                observation = {"refs": evidence_refs(actual["evidence"], aliases), "text": actual_text}
                if acknowledge:
                    digest, handles = evidence_manifest(actual)
                    status, receipt = browser.request(f"/v2/deliveries/{package['delivery_id']}/receipts", "POST", {
                        **{name: package[name] for name in ("snapshot_id", "package_id")},
                        "event_id": label, "kind": "loaded", "route_ref": client["route_ref"],
                        "context_epoch": args["first"]["context_epoch"], "model_call_ref": f"loopback-{label}",
                        "revision_ids": [row["revision_id"] for row in actual["statements"]],
                        "loaded_evidence_refs": handles, "loaded_manifest_digest": digest,
                    }, authentication=headers)
                    raw[label]["receipt"] = {"status": status, "body": receipt}
                    observation.update(actual_manifest_digest=digest, receipt_state=receipt.get("evidence_complete"))
                projected[label] = observation
                return observation

            first = load("first")
            if first is None:
                return raw, projected
            if case["id"] == "F02-13":
                ack = observe("ack", first, True)
                source = sources[args["revoke"]["source"]]
                raw["revoke"] = projected["revoke"] = service.operation(owner, {
                    "kind": "change_grant", "source_id": source, "client_id": client["client_id"],
                    "purpose": "task_delivery", "allow": False,
                }, "revoke-fixture")
                removed_alias = args["old"]["handle"].split(":")[-1]
                removed = next(row["handle"] for row in first["evidence"] if aliases[row["event_id"]] == removed_alias)
                status, result = browser.request("/v2/evidence/expand", "POST", {"handle": removed},
                                                 authentication=headers)
                raw["old"] = projected["old"] = {"status": status, "body": result}
                load("reuse", {"retained_delivery_id": first["delivery_id"],
                               "retained_manifest_digest": ack["actual_manifest_digest"]})
                fresh = load("fresh")
                if fresh:
                    observe("input", fresh)
            else:
                raw["drop"] = projected["drop"] = {"model_requests": len(captured["codex"]), "ledger": ledger()}
                again = load("again")
                if again is None:
                    return raw, projected
                records = ledger()
                raw["same"] = records
                projected["same"] = {"same_delivery": first["delivery_id"] == again["delivery_id"],
                                     "delivery_count": len(records),
                                     "loaded_count": sum(row["loaded"] is not None for row in records.values())}
                next_page = [{**{name: event[name] for name in ("native_object", "native_version",
                              "actor_kind", "at", "checks")}, "task_id": task} for event in data["later_events"]]
                receipt = post(owner, "/v2/events", {"source_id": sources["main"], "cursor": "c1",
                               "next_cursor": "c2", "events": next_page}, "later-page")
                aliases.update((row["event_id"], row["native_object"]) for row in receipt["events"])
                extracted = completed_extractions(owner, (row["event_id"] for row in receipt["events"]))
                raw["more"] = {"receipt": receipt, "extractions": extracted}
                if not extracted:
                    return raw, projected
                projected["more"] = raw["more"]
                later = load("later")
                if later is None:
                    return raw, projected
                observe("late_ack", again, True)
                records = ledger()
                raw["ledger"] = records
                projected["ledger"] = {label: {
                    "loaded": records[package["delivery_id"]]["loaded_state"],
                    "used": records[package["delivery_id"]]["used_state"],
                } for label, package in (("older", again), ("newer", later))}
    return raw, projected


def evidence_manifest(actual):
    def digest(value):
        normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    members = {row["handle"]: digest([row, [item for item in actual["observations"]
                                             if item["handle"] == row["handle"]]]) for row in actual["evidence"]}
    return "sha256:" + digest(members), list(members)
