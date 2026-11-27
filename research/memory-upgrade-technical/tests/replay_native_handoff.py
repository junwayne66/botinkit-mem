"""Replay handoffs with real host hooks, real shell calls and an independent verifier."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from baseline_probe import BrowserSession, fixture, native, post


def incoming(request):
    packages = []
    for text in native.text_blocks(request):
        marker = "bmem authorized task evidence."
        if marker in text:
            wire = text.split(marker, 1)[1].split("\n", 1)[1]
            packages.append(json.JSONDecoder().raw_decode(wire)[0])
    return packages


def actual_checks(value):
    checks = {}
    if isinstance(value, dict):
        candidate = value.get("checks")
        if isinstance(candidate, dict) and all(isinstance(v, str) for v in candidate.values()):
            checks.update(candidate)
        for item in value.values():
            checks.update(actual_checks(item))
    elif isinstance(value, list):
        for item in value:
            checks.update(actual_checks(item))
    elif isinstance(value, str):
        for line in value.splitlines():
            line = line.lstrip()
            if not line.startswith("{"):
                continue
            try:
                row, _ = json.JSONDecoder().raw_decode(line)
            except ValueError:
                continue
            checks.update(actual_checks(row))
    return checks


def make_host(vault, owner, name, task):
    record = post(owner, "/v2/clients", {
        "host": name, "destination": "observed-loopback-" + name,
        "capabilities": {"load_context": True, "observe_loaded": True},
    }, name)
    client = record["client_id"]
    path = vault.root / "run/clients" / (client + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        json.dump({**record, "host": name}, file)
    vault.grant("set", "--client", client, "--task", task, "--allow", "true")
    vault.grant("set", "--client", client, "--share-statements", "true")
    source = vault.cli("connector", "add", name, "--account", "synthetic-" + name, "--client", client)["source_id"]
    vault.cli("connector", "resume", source)
    return record, source


def package_receipt(browser, auth, delivery, package, db):
    entries = {}
    for item in package["evidence"]:
        handle = item["handle"]
        observations = [row for row in package["observations"] if row["handle"] == handle]
        serialized = json.dumps([item, observations], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        entries[handle] = hashlib.sha256(serialized.encode()).hexdigest()
    expected, = db.execute("SELECT entries FROM context_evidence_manifests WHERE delivery_id=?",
                           (delivery["delivery_id"],)).fetchone()
    if json.loads(expected) != entries:
        raise RuntimeError("captured request did not preserve the actual evidence manifest")
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return browser.request(f"/v2/deliveries/{delivery['delivery_id']}/receipts", "POST", {
        "event_id": "captured-request-1", "kind": "loaded", "model_call_ref": "loopback-request-1",
        "route_ref": delivery["route_ref"], "context_epoch": delivery["context_epoch"],
        "snapshot_id": delivery["snapshot_id"], "package_id": delivery["package_id"],
        "revision_ids": [], "loaded_evidence_refs": sorted(entries), "loaded_manifest_digest": "sha256:" + digest,
    }, authentication=auth)


def replay_native_handoff(case, binary):
    data = case["fixture"]
    host_names = {"Codex": "codex", "Claude Code": "claude-code"}
    first, second = (host_names[name] for name in data["hosts"])
    script = data["tool_workspace"]
    selections = []
    provider_defaults = fixture("native_provider.json")

    def provider(host, body):
        command = None
        if not native.tool_receipts(host, body):
            if host == first and any(data["first_prompt"] in fragment for fragment in native.text_blocks(body)):
                command = script["first_command"]
            elif host == second:
                texts = list(native.text_blocks(body))
                packages = incoming(body)
                checks = actual_checks([item["text"] for package in packages for item in package["evidence"]
                                        if item["actor_kind"] == "tool_result"])
                if (any(data["handoff_prompt"] in fragment for fragment in texts) and checks.get("parse") == "passed"
                        and checks.get("write") == "timeout" and any(
                            script["rerun_write"] in item["text"] for package in packages for item in package["evidence"]
                            if item["actor_kind"] == "user_message")):
                    command = script["rerun_write"]
        if command is not None:
            selections.append({"host": host, "command": command})
        return {**provider_defaults, "command": command}

    with tempfile.TemporaryDirectory(prefix="bmem-native-repair-") as work:
        path = Path(work).resolve()
        for name, content in script["files"].items():
            (path / name).write_text(content, encoding="utf-8")
        vault = native.VaultFixture(binary, path)
        with vault.running(), native.fixture_server(provider) as (endpoint, captured):
            url = vault.cli("web", "--no-open")["url"]
            service_url = url.split("/session", 1)[0]
            owner = BrowserSession(service_url, url)
            browser = BrowserSession(service_url)
            task_row = data["tasks"][0]
            task = post(owner, "/v2/tasks", task_row, "fixture-task")["task_id"]
            hosts = {name: make_host(vault, owner, name, task) for name in (first, second)}
            for target, _ in hosts.values():
                for _, sid in hosts.values():
                    vault.grant("source", sid, "--purpose", "task_delivery", "--client", target["client_id"],
                                "--allow", "true")
            actor, source = hosts[first]
            native.run_host(first, vault.command(first, actor["client_id"], source, task), data["first_prompt"],
                            endpoint, path, vault.env, provider_defaults, case["id"] + "-source",
                            tool_commands=(script["first_command"],))
            source_calls = [row for call in captured[first] for row in native.tool_receipts(first, call)]
            status, source_package = owner.request(f"/v2/tasks/{task}/handoff")
            probe_events = [item for item in source_package["evidence"] if item["actor_kind"] == "tool_result"]
            ingested = actual_checks([item["text"] for item in probe_events])
            if status != 200 or actual_checks(source_calls) != ingested:
                raise RuntimeError(f"source probe stdout {actual_checks(source_calls)} and ingested receipt {ingested} disagree")
            if actual_checks(source_calls).get("write") != "timeout" or (path / "actions.log").exists():
                raise RuntimeError("first attempt did not execute only the original probe "
                                   f"(calls={len(source_calls)}, selected={len(selections)}, "
                                   f"checks={actual_checks(source_calls)}, wrote={(path / 'actions.log').exists()})")
            (path / "ready.flag").touch(exist_ok=False)
            actor, source = hosts[second]
            native.run_host(second, vault.command(second, actor["client_id"], source, task), data["handoff_prompt"],
                            endpoint, path, vault.env, provider_defaults, case["id"] + "-resume",
                            tool_commands=(script["rerun_write"],), writable=True)
            first_request = captured[second][0]
            package = incoming(first_request)[-1]
            delivery = next(row for row in vault.cli("delivery", "list")["deliveries"]
                            if row["package_id"] == package["package_id"] and row["client_id"] == actor["client_id"])
            instance = vault.cli("status")["instance_id"]
            headers = lambda record: {"Authorization": "Bearer " + record["token"], "X-Bmem-Instance": instance}
            db_uri = (vault.root / "data/bmem.sqlite3").as_uri() + "?mode=ro"
            with sqlite3.connect(db_uri, uri=True) as db:
                receipt_status, receipt = package_receipt(browser, headers(actor), delivery, package, db)
            if receipt_status != 200 or not receipt["evidence_complete"]:
                raise RuntimeError(f"captured context receipt is incomplete ({receipt_status})")
            result_text = (path / "result.txt").read_text() if (path / "result.txt").exists() else None
            actions = (path / "actions.log").read_text().splitlines() if (path / "actions.log").exists() else []
            verified = result_text == script["files"]["rows.txt"] and actions == ["write"]
            verifier = post(owner, "/v2/clients", {"host": "independent-verifier", "destination": "isolated-artifacts",
                                                    "capabilities": {"verify_outcomes": True}}, "verifier")
            vault.grant("set", "--client", verifier["client_id"], "--task", task, "--allow", "true")
            status, outcome = browser.request(f"/v2/attempts/{delivery['attempt_id']}/outcomes", "POST", {
                "event_id": "independent-file-check", "status": "passed" if verified else "failed",
                "validator_ref": "fixture-independent-file-check", "artifact_ref": "result.txt",
                "evidence_ref": "fixture actions.log and result.txt", "delivery_ids": [delivery["delivery_id"]],
            }, authentication=headers(verifier))
            if status != 200:
                raise RuntimeError(f"independent outcome was rejected ({status})")
            requests = {host: len(records) for host, records in captured.items()}
            raw = {"source_tool_responses": source_calls, "source_event_ids": [row["event_id"] for row in probe_events],
                   "captured_evidence": package, "request_counts": requests, "host_tools_requested": selections,
                   "receipts": receipt, "outcome": outcome, "artifacts": {"text": result_text, "actions": actions}}
            return raw, {
                "seed": {"task_id": task, "registered_hosts": list(hosts)},
                "source": {"tool_result_count": len(probe_events)}, "ready": {"created": True},
                "resume": {"attempt_id": delivery["attempt_id"], "loaded": receipt["evidence_complete"]},
                "observe": {"package_mode": package["package_mode"], "model_input_checks": actual_checks(
                    [item["text"] for item in package["evidence"] if item["actor_kind"] == "tool_result"])},
                "judge": {"result_text": result_text, "actions": actions, "extra_background_turns": 0,
                          "outcome": outcome["status"]},
            }
