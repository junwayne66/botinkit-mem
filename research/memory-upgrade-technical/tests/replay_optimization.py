"""Run the frozen F07 scenarios through the upgrade candidate."""

import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path

from baseline_probe import events, isolated, post
from score import catalogue, grade
from test_integration import BrowserSession, fixture
import native_hosts as native

SUPPORTED = {f"F07-{index:02d}" for index in range(1, 18)} | {"X01-13", "E2E-03"}

HOST_EVALUATION_HOOK = """\
import json
import sys
from pathlib import Path

incoming = json.load(sys.stdin)
if incoming.get("hook_event_name") == "UserPromptSubmit":
    directory = Path(__file__).parent
    reply = json.loads((directory / "provider-input.json").read_text(encoding="utf-8"))
    (directory / "observed-hook.json").write_text(
        json.dumps({"session_id": incoming["session_id"], "prompt": incoming["prompt"]}), encoding="utf-8"
    )
    print(json.dumps(reply, ensure_ascii=False))
"""


def api(owner, action, body=None, key=None):
    return post(owner, "/v2/optimization/" + action, body or {}, key or action)


def task(owner, anchor, project, key="task"):
    return post(owner, "/v2/tasks", {"anchor": anchor, "project": project}, key)["task_id"]


def capture(service, owner, rows, task_id=None):
    admitted = [
        {
            "native_object": row.get("root", row["id"]),
            "native_version": row["id"],
            "ref": row["id"],
            "text": row["text"],
            "task": "task",
            "source": row.get("source", "optimization"),
        }
        for row in rows
    ]
    aliases, sources = events(owner, admitted, {"task": task_id} if task_id else None)
    evidence = {alias: "event_" + event_id for event_id, alias in aliases.items()}
    with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        roots = {
            alias: db.execute("SELECT root_id FROM ing_event_versions WHERE event_id=?", [event_id]).fetchone()[0]
            for event_id, alias in aliases.items()
        }
    return sources, roots, evidence


def bound(service, owner, host, task_id, sources=(), label="client", verify=False):
    registered = post(owner, "/v2/clients", {
        "host": host, "destination": "loopback-" + label,
        "capabilities": {"load_context": True, "observe_loaded": True, "observe_used": True,
                         "verify_outcomes": verify},
    }, label)
    client = registered["client_id"]
    service.operation(owner, {"kind": "change_grant", "client_id": client,
                              "share_statements": True}, label + "-declare")
    service.operation(owner, {"kind": "change_grant", "client_id": client,
                              "task_id": task_id, "allow": True}, label + "-task")
    for sid in sources:
        service.operation(owner, {"kind": "change_grant", "client_id": client,
                                  "source_id": sid, "purpose": "task_delivery", "allow": True}, label + "-" + sid)
    token = {"Authorization": "Bearer " + registered["token"], "X-Bmem-Instance": service.instance_id}
    return BrowserSession(service.endpoint), token, registered


def load(client, label, task_id, **extra):
    browser, auth, registered = client
    request = {"session_id": label, "context_epoch": "1", "route_ref": registered["route_ref"],
               "budget": {"statement_tokens": 4096, "evidence_tokens": 4096}}
    status, attempt = browser.request(f"/v2/tasks/{task_id}/attempts", "POST", request,
                                      "attempt-" + label, authentication=auth)
    if status != 201:
        raise ValueError((status, attempt))
    status, body = browser.request("/v2/context/load", "POST", {
        **request, **extra, "task_id": task_id, "attempt_id": attempt["attempt_id"],
    }, "load-" + label, authentication=auth)
    if status != 200:
        raise ValueError((status, body))
    return {**body, "attempt_id": attempt["attempt_id"]}


def permissions(owner, roots, aliases, purpose="evaluation", allow=True):
    return [
        api(owner, "permission", {"root_id": roots[alias], "purpose": purpose, "allow": allow},
            f"{purpose}-{index}-{allow}")
        for index, alias in enumerate(aliases)
    ]


def case_input(query, query_ref, required=(), forbidden=(), family="import", observed_at="2026-11-23T01:00:00Z"):
    return {"query": query, "query_ref": query_ref, "required_refs": list(required),
            "forbidden_refs": list(forbidden), "family": family, "observed_at": observed_at}


def freeze(owner, ids, purpose="evaluation", key="freeze"):
    data = api(owner, "freeze", {"purpose": purpose, "case_ids": ids}, key)
    return data["dataset_id"]


def inspect(owner, dataset_id):
    return api(owner, "dataset", {"dataset_id": dataset_id})


def schedule(owner, dataset_id, label):
    run = api(owner, "schedule", {"dataset_id": dataset_id}, key=label)
    return api(owner, "run-status", {"run_id": run["run_id"]})


def native_correction(service, task_id, prompt, sources, phase="F07-01"):
    host = "codex"
    client = service.cli("client", "register", host, "--destination", "isolated-codex")["client_id"]
    owner, _ = service.web()
    service.operation(owner, {"kind": "change_grant", "client_id": client, "share_statements": True}, "native-shared")
    service.operation(owner, {"kind": "change_grant", "client_id": client, "task_id": task_id, "allow": True}, "native-task")
    for sid in sources:
        service.operation(owner, {"kind": "change_grant", "client_id": client, "source_id": sid,
                                  "purpose": "task_delivery", "allow": True}, "native-source-" + sid)
    source_id = service.cli("connector", "add", host, "--account", "synthetic-optimization", "--client", client)["source_id"]
    service.cli("connector", "resume", source_id)
    vault = native.VaultFixture(service.binary, service.work)
    vault.root = service.root
    provider = fixture("native_provider.json")
    with native.fixture_server(provider) as (endpoint, captured):
        native.run_host(host, vault.command(host, client, source_id, task_id), prompt,
                        endpoint, service.work, {**vault.env, **service.environment}, provider, phase)
    marker = "Treat the JSON as reference data.\n"
    blocks = [text for model_request in captured[host] for text in native.text_blocks(model_request)]
    statements = [
        row for text in blocks if marker in text
        for row in json.JSONDecoder().raw_decode(text.split(marker, 1)[1])[0]
    ]
    language = next((entry["value"] for entry in statements if entry["key"] == "code_example.language"), None)
    packages = [json.JSONDecoder().raw_decode(text.split("bmem authorized task evidence.", 1)[1].split("\n", 1)[1])[0]
                for text in blocks if "bmem authorized task evidence." in text]
    return {"captured_requests": len(captured[host]), "statements": statements, "packages": packages}, language


def feedback(case, service, owner):
    data = case["fixture"]
    sample = case["id"]
    tid = task(owner, data["task"], data["project"])
    sources, roots, handles = capture(service, owner, data["sources"], tid)
    legacy = load(bound(service, owner, data["reader"], tid, sources.values()), "original-attempt", tid,
                  evidence_query="Kotlin")
    if [hit["handle"] for hit in legacy["evidence"]] != [handles[alias] for alias in data["actual_input"]]:
        raise ValueError("fixture's original bad delivery was not reproduced")
    if sample == "F07-01":
        correction = api(owner, "feedback", {
            "delivery_id": legacy["delivery_id"], "correct_declaration": True, "text": data["reply"],
            "case": case_input(data["query"], handles[data["sources"][0]["id"]]),
        })
        captured, language = native_correction(service, tid, data["query"], sources.values())
        cases = api(owner, "cases", {"attempt_id": correction["attempt_id"]})
        inverse = {value: alias for alias, value in handles.items()}
        refs = sorted({inverse[item["handle"]] for package in captured["packages"] for item in package["evidence"]
                       if item["handle"] in inverse})
        return {"correct": correction, "old_delivery": legacy, "open": captured, "cases": cases}, {
            "correct": correction, "open": {"example_language": language.title() if language else None,
                                            "evidence_refs": refs}, "cases": cases,
        }
    permissions(owner, roots, handles)
    args = case["steps"][0]["args"]
    saved = api(owner, "feedback", {"delivery_id": legacy["delivery_id"], "text": data["feedback"],
        "case": case_input(data["query"], handles[args["required"]], [handles[args["required"]]],
                           [handles[args["forbidden"]]])})
    detail = api(owner, "cases", {"attempt_id": saved["attempt_id"]})
    found, = detail["cases"]
    normalized = {
        **found,
        "attempt": data["attempt"] if found["attempt"] == legacy["attempt_id"] else found["attempt"],
        "required_refs": [alias for alias, handle in handles.items() if handle in found["required_refs"]],
        "forbidden_refs": [alias for alias, handle in handles.items() if handle in found["forbidden_refs"]],
    }
    return {"save": saved, "read": detail, "old_delivery": legacy}, {"save": saved, "read": normalized}


def paired_report(owner, data):
    rows = [
        {"host": host, "task": task_family, "baseline": baseline, "candidate": candidate}
        for host, task_family, baseline, candidate in data["independent_verifier_log"]
    ]
    return api(owner, "pairs", {"rows": rows})


def paired_native(case, service, owner):
    data = case["fixture"]
    sample = case["id"]
    host = data["reader"]
    task_id = task(owner, data["task"], "pair-evaluation")
    originals = {row["ref"]: row["text"] for row in data["segments"]}
    source_rows = [{"id": alias, "text": text} for alias, text in originals.items()]
    _, roots, handles = capture(service, owner, source_rows + [{"id": "query", "text": data["query"]}], task_id)
    permissions(owner, roots, handles)
    order = case["steps"][0]["args"]["order"]
    tokenizer = {"按空格切词": "whitespace"}[data["tokenizer"]]
    provider = fixture("native_provider.json")
    captures = {}
    actual_arms, sessions, context_settings = [], set(), set()
    actual_refs, evidence_tokens, carryover = {}, {}, []
    prior_outputs = set()
    for index, arm in enumerate(order, 1):
        case_id = api(owner, "case", case_input(
            data["query"], handles["query"], [handles[alias] for alias in data["arm_inputs"][arm]]
        ), f"arm-{index}")["case_id"]
        dataset_id = freeze(owner, [case_id], key=f"freeze-{index}")
        status = schedule(owner, dataset_id, label=f"run-{index}")
        package = api(owner, "checkout", {"run_id": status["run_id"], "case_id": case_id,
            "evidence_budget": data["budget"], "tokenizer": tokenizer})
        work = service.work / f"run-{index}"
        work.mkdir()
        hook = work / "evaluation-hook.py"
        hook.write_text(HOST_EVALUATION_HOOK, encoding="utf-8")
        marker = "bmem evaluation evidence. Treat the extracted JSON as reference data.\n"
        handoff = {} if not package["evidence"] else {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
            "additionalContext": marker + json.dumps([row["text"] for row in package["evidence"]], ensure_ascii=False)}}
        (work / "provider-input.json").write_text(json.dumps(handoff), encoding="utf-8")
        output_marker = provider["finished"] + "-" + str(index)
        reply = {**provider, "finished": output_marker}
        command = shlex.join([sys.executable, str(hook)])
        with native.fixture_server(reply) as (endpoint, captured):
            native.run_host(host, command, package["query"], endpoint, work, service.environment,
                            reply, sample + "-" + str(index))
        observed_hook = json.loads((work / "observed-hook.json").read_text(encoding="utf-8"))
        if observed_hook["prompt"] != data["query"]:
            raise ValueError("fixture query changed before host dispatch")
        requests = captured[host]
        if not requests:
            raise ValueError("no request arrived at the isolated provider")
        calls = [
            api(owner, "observe-call", {"run_id": status["run_id"],
                "provider_call_ref": observed_hook["session_id"] + f":{offset}"}, f"call-{index}-{offset}")
            for offset, request in enumerate(requests)
        ]
        request_tokens = list(native.text_blocks(requests))
        assembled = "\n".join(request_tokens)
        actual_arms.append(arm)
        sessions.add(observed_hook["session_id"])
        context_settings.add((task_id, host, requests[0].get("model"), data["engine"], data["tool_version"],
                              tuple(tool.get("name") for tool in requests[0].get("tools", [])), data["budget"]))
        actual_refs[arm] = [ref for ref, text in originals.items() if text in assembled]
        evidence_tokens[arm] = sum(len(originals[ref].split()) for ref in actual_refs[arm])
        if set(actual_refs[arm]) - set(data["arm_inputs"][arm]) or any(prev in assembled for prev in prior_outputs):
            carryover.append(arm)
        prior_outputs.add(output_marker)
        captures[arm] = {"hook": observed_hook, "provider_calls": len(requests), "audit": calls[-1],
                         "provider_model": requests[0]["model"], "source_package": package,
                         "evidence_blocks": sorted({block for block in request_tokens if marker in block})}
    return {"run": captures}, {"run": {"arms": actual_arms, "unique_task_host_engine_tool_budget": len(context_settings),
        "distinct_context_count": len(sessions), "evidence_tokens": evidence_tokens, "actual_input_refs": actual_refs,
        "arm_inputs_with_prior_messages": carryover}}


def one_case_per_root(owner, rows, handles, **input_data):
    return {
        row["id"]: api(owner, "case", case_input(
            row["text"], handles[row["id"]], [handles[row["id"]]],
            **input_data.get(row["id"], {})
        ), "case-" + str(index))["case_id"]
        for index, row in enumerate(rows)
    }


def dataset_replay(case, service, owner):
    sample = case["id"]
    data = case["fixture"]
    rows = data.get("roots", data.get("examples", data.get("sources")))
    _, roots, handles = capture(service, owner, rows)
    permissions(owner, roots, data.get("granted_evaluation", data.get("evaluation_granted", handles))
                if not isinstance(data.get("evaluation_granted"), bool) else handles)
    if sample == "F07-03":
        ids = [api(owner, "case", case_input(
            data["case_query"], handles[selection], [handles[selection]]
        ), f"selection-{index}")["case_id"] for index, selection in enumerate(data["selection"])]
        profiles = {"evaluate": "evaluation", "train": "training"}
        raw = {step: inspect(owner, freeze(owner, ids, purpose, key=step)) for step, purpose in profiles.items()}
        inverse = {handle: key for key, handle in handles.items()}
        projected = {step: {"included_refs": [inverse[ref] for row in dataset["members"]
                                               for ref in row["required_refs"]]}
                     for step, dataset in raw.items()}
        projected["train"]["training_examples_written"] = len(raw["train"]["members"])
        return raw, projected
    if sample == "F07-05":
        meta = {row["id"]: {"family": row["family"], "observed_at": row["time"] + "T00:00:00Z"} for row in rows}
        ids = one_case_per_root(owner, rows, handles, **meta)
        dataset = freeze(owner, list(ids.values()))
        result = api(owner, "split", {"dataset_id": dataset, "cutoff": data["cutoff"],
            "dev_ids": [ids[alias] for alias in case["steps"][0]["args"]["dev_ids"]]})
        inverse = {value: key for key, value in ids.items()}
        return {"split": result}, {"split": {key: [inverse[actual] for actual in values]
                                            for key, values in result.items()}}
    ids = one_case_per_root(owner, rows, handles)
    if sample == "F07-04":
        frozen = {label: freeze(owner, [ids[alias] for alias in members], key=label)
                  for label, members in data["datasets"].items()}
        withdrawn = next(alias for alias in data["granted_evaluation"] if alias not in data["datasets"]["control"])
        revoked = permissions(owner, roots, [withdrawn], allow=False)
        readback = {label: schedule(owner, dataset, label) for label, dataset in frozen.items()}
        return {"freeze": frozen, "revoke": revoked, "inspect": readback}, {
            "freeze": frozen, "revoke": {"count": len(revoked)},
            "inspect": readback,
        }
    frozen = {alias: freeze(owner, [value], key=alias) for alias, value in ids.items()}
    affected = data["query_built_from"]
    forgotten = service.operation(owner, {"kind": "forget", "target": {"root_id": roots[affected]}}, "owner-forget")
    readback = {alias: inspect(owner, dataset) for alias, dataset in frozen.items()}
    runs = {alias: schedule(owner, dataset, alias) for alias, dataset in frozen.items()}
    with sqlite3.connect((service.root / "data/bmem.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT query FROM optimization_cases WHERE id=?", [ids[affected]]).fetchone()
    assert row == (None,), "forgotten query content survived in authority snapshot"
    return {"build": frozen, "forget": forgotten, "audit": readback,
            "runs": runs, "query_erased": True}, {
        "build": frozen, "forget": forgotten, "audit": {
            "exported_text": "\n".join(member["query"] for view in readback.values() for member in view["members"]),
            "affected_queries_run": runs[affected]["model_calls"],
            "usable_roots": [alias for alias, view in readback.items() if view["usable"]],
        },
    }


def register_policy(owner, name, parent="stable", roots=(), evidence_cap=4096):
    return api(owner, "policy", {"version": name, "parent": parent, "evidence_token_cap": evidence_cap,
                                "training_roots": list(roots)}, name)


def gate(owner, candidate, scope, thresholds, rows, key):
    plan = api(owner, "trial", {"scope": scope, "candidate": candidate, "thresholds": thresholds,
        "holdout_families": [row["task"] for row in rows], "authorized": True}, "trial-" + key)
    result = api(owner, "assess", {"trial_id": plan["trial_id"], "attempts": rows}, "assess-" + key)
    return {"pre_registered": plan, "result": result}


PASS_ROW = {"task": "independent-support", "baseline": "failed", "candidate": "passed",
            "cost_units": 1, "forbidden_refs": []}
UPGRADE_GATE = {"min_independently_scored_pairs": 1, "min_net_gained": 1,
                "max_candidate_cost_units": 2, "max_forbidden_hits": 0}


def observe_policy(service, owner, host, project, label, anchor="strategy verification", task_id=None):
    tid = task_id or task(owner, anchor, project, "task-" + label)
    return load(bound(service, owner, host, tid, label="strategy-"+label), "verify-strategy-" + label, tid)


def optimize(case, service, owner, over_budget=False):
    sample = case["id"]
    data = case["fixture"]
    scope = data.get("scope", {"host": "codex", "project": "青禾"})
    label = data.get("candidate", case["steps"][0]["args"].get("candidate"))
    if sample in ("F07-13", "F07-14"):
        rows = [
            {"task": row["task"], "baseline": row["baseline"], "candidate": row["candidate"],
             "cost_units": row["cost_units"], "forbidden_refs": row.get("forbidden_refs", row.get("forbidden_hits"))}
            for row in data["attempts"]
        ]
    else:
        control = data["over_budget_control"]
        rows = [
            {"task": family, "baseline": before, "candidate": after,
             "cost_units": cost + (control["additional_cost_units"]
                                   if over_budget and family == control["task_family"] else 0),
             "forbidden_refs": [f"hit:{index}" for index in range(hits)]}
            for family, before, after, cost, hits in data["paired_holdout_log"]
        ]
    register_policy(owner, label)
    assessment = gate(owner, label, scope, data["thresholds"], rows, "budget" if over_budget else "normal")
    return assessment


def version_replay(case, service, owner):
    sample = case["id"]
    data = case["fixture"]
    scope = {"host": "codex", "project": "青禾"}
    if sample == "F07-12":
        scope["project"] = next(iter(data["manifests"].values()))["scope"]
        rows = [{"id": key, "text": value} for key, value in data["originals"].items()]
        _, roots, _ = capture(service, owner, rows)
        for purpose in ("evaluation", "training"):
            permissions(owner, roots, [alias for alias, grants in data["grants"].items() if purpose in grants], purpose)
        registered = []
        for policy, manifest in data["manifests"].items():
            register_policy(owner, policy, roots=[roots[alias] for alias in manifest["training_roots"]])
            registered.append(gate(owner, policy, scope, UPGRADE_GATE, [PASS_ROW], policy))
        withdrawn = next(alias for alias, grants in data["grants"].items() if grants == ["training"])
        permissions(owner, roots, [withdrawn], "training", allow=False)
        policies = {policy: api(owner, "policy-status", {"version": policy}) for policy in data["manifests"]}
        served = observe_policy(service, owner, scope["host"], scope["project"], "withdraw")
        return {"withdraw": {"assessments": registered, "policies": policies, "served": served}}, {
            "withdraw": {**policies, "serving_policies": [served["strategy_version"]]},
        }
    previous, active = data["policy"]["previous"], data["policy"]["active"]
    for label in (previous, active):
        register_policy(owner, label)
        gate(owner, label, scope, UPGRADE_GATE, [PASS_ROW], label)
    tid = task(owner, "rollback remains local to policy", scope["project"])
    client = bound(service, owner, scope["host"], tid, label="before")
    before = load(client, "before-rollback", tid)
    lookup = {row["id"]: row["text"] for row in data["events"]}
    body = {"scope": {"kind": "project", "project": scope["project"]}, "share": "authorized_assistants"}
    declared = post(owner, "/v2/statements", {**body, "original": lookup["base"]}, "base")
    old = declared["dimensions"][0]["revision_id"]
    replacement = post(owner, "/v2/statements", {**body, "original": "本项目代码示例用 " + lookup["replace"].split()[-1],
                    "corrects_revision_id": old, "corrects_attempt_id": before["attempt_id"]}, "replace")
    sources, roots, _ = capture(service, owner, [{"id": "ev-99", "text": lookup["ev-99"]}], tid)
    for sid in sources.values():
        service.operation(owner, {"kind": "change_grant", "client_id": client[2]["client_id"],
                                  "source_id": sid, "purpose": "task_delivery", "allow": True}, "rollback-side-" + sid)
    seeded = load(client, "with-old-evidence", tid)
    ids = [row["event_id"] for row in seeded["evidence"]]
    if len(ids) != 1:
        raise ValueError("expected to observe the evidence before forgetting")
    forgotten = service.operation(owner, {"kind": "forget", "target": {"root_id": roots["ev-99"]}},
                                  "rollback-forget")
    reverted = api(owner, "revert", {"scope": scope})
    observed = load(client, "after-rollback", tid)
    return {"apply": {"before": before, "replacement": replacement, "forgotten": forgotten, "revert": reverted},
            "read": observed}, {
        "apply": reverted, "read": {"active_policy": observed["strategy_version"],
            "current_language": next((row["value"].title() for row in observed["statements"]
                                      if row["key"] == "code_example.language"), None),
            "visible_refs": ["ev-99" if row["event_id"] in ids else row["event_id"]
                             for row in observed["evidence"]],
            "current_revisions": ["base" if row["revision_id"] == old else row["revision_id"]
                                  for row in observed["statements"]]},
    }


def ledger_replay(case, service, owner):
    data = case["fixture"]
    tid = task(owner, "verify-evidence-ledger", "青禾")
    rows = [{"id": ref, "text": "Evidence " + ref} for ref in data["provided"]]
    sources, _, handles = capture(service, owner, rows, tid)
    client = bound(service, owner, data["executor"], tid, sources.values(), verify=True)
    delivery = load(client, "record-usage", tid)
    browser, authentication, adapter = client
    capturing = data["model_call_capture"]
    available = {row["handle"] for row in delivery["evidence"]}
    included = [handles[ref] for ref in capturing["input_refs"] if handles[ref] in available]
    linked = [handles[ref] for ref in capturing["action_refs"] if handles[ref] in available]
    endpoint = f"/v2/deliveries/{delivery['delivery_id']}/receipts"
    shared = {"context_epoch": "1", "snapshot_id": delivery["snapshot_id"], "package_id": delivery["package_id"],
              "route_ref": adapter["route_ref"], "model_call_ref": capturing["call_id"], "revision_ids": []}
    captured = browser.request(endpoint, "POST", {**shared, "event_id": "loaded",
        "kind": "loaded", "loaded_evidence_refs": included, "missing_reason": "fixture_capture"},
        authentication=authentication)
    used = browser.request(endpoint, "POST", {**shared, "event_id": "used",
        "kind": "used", "used_evidence_refs": linked}, authentication=authentication)
    if captured[0] != 200 or used[0] != 200:
        raise ValueError((captured, used))
    claim = browser.request(f"/v2/attempts/{delivery['attempt_id']}/outcomes", "POST", {
        "event_id": "executor-says-passed", "status": case["steps"][0]["args"]["executor_claim"],
        "evidence_ref": linked[0], "artifact_ref": "runner-claim", "validator_ref": data["executor"],
        "delivery_ids": [delivery["delivery_id"]]}, authentication=authentication)
    audit = api(owner, "ledger", {"delivery_id": delivery["delivery_id"]})
    inverse = {handle: ref for ref, handle in handles.items()}
    return {"ledger": audit, "delivery": delivery, "loaded": captured, "used": used, "self_claim": claim}, {
        "ledger": {**audit, **{field: [inverse[handle] for handle in audit[field]]
                               for field in ("loaded_refs", "used_refs")}},
    }


def shadow_replay(case, service, owner):
    data = case["fixture"]
    scope = {"host": "codex", "project": "shadow-evaluation"}
    rows = [{**row, "text": json.dumps({key: value for key, value in row.items() if key != "allow"},
                                      ensure_ascii=False)} for row in data["observation_stream"]]
    _, roots, handles = capture(service, owner, rows)
    permissions(owner, roots, [row["id"] for row in rows if "evaluation" in row["allow"]])
    register_policy(owner, data["active"])
    activated = gate(owner, data["active"], scope, UPGRADE_GATE, [PASS_ROW], "shadow-baseline")
    proposed = register_policy(owner, data["candidate"], data["active"])
    raw = {"activation": activated, "candidate": proposed, "checked_out": {}, "excluded": {}}
    included = []
    candidate_ids = []
    for index, row in enumerate(rows):
        label = row["id"]
        status, response = owner.request("/v2/optimization/case", "POST", case_input(
            data["proposal"], handles[label], [handles[label]], family=label
        ), f"shadow-{index}")
        if status == 403:
            raw["excluded"][label] = response
            continue
        if status != 200:
            raise ValueError((status, response))
        candidate_ids.append(response["case_id"])
        dataset_id = freeze(owner, [response["case_id"]], key=f"shadow-freeze-{index}")
        allowed = schedule(owner, dataset_id, label=f"shadow-run-{index}")
        package = api(owner, "checkout", {"run_id": allowed["run_id"], "case_id": response["case_id"],
            "evidence_budget": 4096, "tokenizer": "utf8_bytes"})
        raw["checked_out"][label] = package
        if any(item["handle"] == handles[label] for item in package["evidence"]):
            included.append(label)
    heldout = freeze(owner, candidate_ids, key="shadow-heldout")
    shadow = api(owner, "trial", {"scope": scope, "candidate": data["candidate"], "thresholds": UPGRADE_GATE,
        "dataset_id": heldout, "authorized": False}, "shadow-trial")
    training_id = freeze(owner, candidate_ids, "training", key="training-negative-control")
    training = inspect(owner, training_id)
    served = observe_policy(service, owner, scope["host"], scope["project"], "shadow-after")
    return {"shadow": {**raw, "trial": shadow, "training_view": training, "served": served}}, {
        "shadow": {"evaluated_refs": included, "active_policy": served["strategy_version"],
            "candidate_versions": [shadow["candidate"]], "training_examples_exported": len(training["members"])},
    }


def durable_correction(case, service, owner):
    data = case["fixture"]
    scope = {"host": "codex", "project": data["project"]}
    for policy in (data["last_strategy"], data["active_strategy"]):
        register_policy(owner, policy)
        gate(owner, policy, scope, UPGRADE_GATE, [PASS_ROW], policy)
    task_id = task(owner, "corrected report defaults", data["project"])
    statement_scope = {"scope": {"kind": data["scope"]},
        "share": "authorized_assistants" if data["grants"]["share"] else "owner"}
    initial = {row["id"]: post(owner, "/v2/statements",
        {**statement_scope, "original": row["text"]}, row["id"]) for row in data["initial"]}
    initial_revisions = {row["id"]: next(entry for entry in initial[row["id"]]["dimensions"]
        if entry["key"] == row["dimension"]) for row in data["initial"]}
    correction = data["correction"]
    appended = post(owner, "/v2/statements", {**statement_scope, "original": correction["text"],
        "corrects_revision_id": initial_revisions[correction["supersedes"]]["revision_id"]}, correction["id"])
    revision = next(item for item in appended["dimensions"] if item["key"] == "report.language")
    before = observe_policy(service, owner, scope["host"], scope["project"], "durability-before", task_id=task_id)
    sources, _, _ = capture(service, owner, [{"id": "reindex-control", "text": data["initial"][0]["text"]}], task_id)
    service.stop()
    service.start()
    owner, _ = service.web()
    reverted = api(owner, "revert", {"scope": scope})
    rebuilt = post(owner, "/v2/search/rebuild", {}, "rebuild")
    if not rebuilt["rebuilt"]:
        raise ValueError("derived index was not rebuilt")
    after = observe_policy(service, owner, scope["host"], scope["project"], "durability-after", task_id=task_id)
    cli = service.cli("statement", "list", "--project", data["project"])
    native_trace, _ = native_correction(service, task_id, "请按本项目约定写一份报告。",
        sources.values(), phase=case["id"])
    refs = {item["revision_id"]: alias for alias, item in initial_revisions.items()}
    refs[revision["revision_id"]] = correction["id"]
    authoritative = {}
    for label, item in [("L2", revision), ("O1", initial_revisions["O1"])]:
        status, history = owner.request(f"/v2/facts/{item['fact_id']}/history")
        if status != 200:
            raise ValueError((status, history))
        authoritative[label] = next(entry for entry in history["revisions"] if entry["revision_id"] == item["revision_id"])
    cases = api(owner, "cases")
    current = next(entry for entry in cli["statements"] if entry["key"] == "report.language")
    author_language = {"zh": "中文", "en": "英文"}[current["value"]]
    projected = {
        "append": appended,
        "restart": {"effective_language": author_language, "model_input_current_refs": sorted({
            refs.get(entry["revision_id"], entry["revision_id"]) for entry in native_trace["statements"]})},
        "history": {"L2": {"supersedes": refs.get(authoritative["L2"]["supersedes"])},
            "O1": {"text": authoritative["O1"]["original"]}, "eval_cases_created": cases["case_count"]},
    }
    return {"append": appended, "restart": {"before": before, "revert": reverted, "rebuild": rebuilt,
        "after": after, "cli": cli, "native": native_trace}, "history": {"facts": authoritative, "cases": cases}}, projected


def replay_optimization(case, binary):
    sample = case["id"]
    data = case["fixture"]
    step = case["steps"][0]["id"]
    if sample not in SUPPORTED:
        return {}, {}
    with isolated() as (service, owner):
        if sample in ("F07-01", "F07-02"):
            return feedback(case, service, owner)
        if sample in ("F07-03", "F07-04", "F07-05", "F07-11"):
            return dataset_replay(case, service, owner)
        if sample == "F07-06":
            return paired_native(case, service, owner)
        if sample == "F07-08":
            return ledger_replay(case, service, owner)
        if sample in ("F07-10", "F07-12"):
            return version_replay(case, service, owner)
        if sample == "X01-13":
            return shadow_replay(case, service, owner)
        if sample == "E2E-03":
            return durable_correction(case, service, owner)
        if sample in ("F07-13", "F07-14", "F07-17"):
            assessment = optimize(case, service, owner)
            projection = {"assess": assessment["result"]}
            if sample == "F07-17":
                served = {}
                for label in case["steps"][1]["args"]["targets"]:
                    host, project = label.split("/", 1)
                    served[label] = observe_policy(service, owner, host, project, label.replace("/", "-").encode().hex())
                projection["sample"] = {"served_policies": {
                    label: request["strategy_version"] for label, request in served.items()}}
                with isolated() as (control_service, control_owner):
                    control = optimize(case, control_service, control_owner, over_budget=True)
                projection["cost_control"] = control["result"]
                return {"assess": assessment, "sample": served, "cost_control": control}, projection
            return {"assess": assessment}, projection
        if sample == "F07-07":
            response = paired_report(owner, data)
        elif sample == "F07-09":
            response = api(owner, "score", {**data["case_label"],
                "unanswerable": data["case_label"]["answerability"] == "unanswerable",
                "observed_calls": data["observed_calls"]})
        elif sample == "F07-15":
            rows = [{"id": key, "expected": {"value": expected_value, "scope": expected_scope, "until": expected_end},
                     "actual": {"value": actual_value, "scope": actual_scope, "until": actual_end}}
                    for key, expected_value, expected_scope, expected_end, actual_value, actual_scope, actual_end
                    in data["required_declarations"]]
            response = api(owner, "usage", {**data, "required_declarations": rows})
        elif sample == "F07-16":
            response = api(owner, "telemetry", data)
        else:
            raise ValueError("unhandled " + sample)
        return {step: response}, {step: response}


if __name__ == "__main__":
    binary = Path(sys.argv[1]).resolve()
    os.environ["BMEM_V2_TEST_BIN"] = str(binary)
    selected = sys.argv[2:] or sorted(SUPPORTED)
    catalogue_data = catalogue()
    for key in selected:
        try:
            raw, projected = replay_optimization(catalogue_data[key], binary)
            status, failures = grade(catalogue_data[key], projected)
            print(key, status, failures, flush=True)
        except (ValueError, RuntimeError, AssertionError, native.HostTimeout) as error:
            print(key, "PROBE_ERROR", str(error), flush=True)
