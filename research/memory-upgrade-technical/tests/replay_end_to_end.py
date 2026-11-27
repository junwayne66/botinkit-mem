"""Exercise a cross-source replay and a new native attempt in one isolated vault."""

import hashlib
import json
from pathlib import Path
import re
import shlex
import tempfile

from baseline_probe import BrowserSession, fixture, native, post, source
from replay_authority_hosts import proposal_endpoint
from replay_native_handoff import incoming, make_host


def replay_import_handoff(case, binary):
    data = case["fixture"]
    pages, continuation = data["pages"], data["new_attempt"]
    terminal = next(page for page in pages if page["role"] == "tool")
    known_handle = re.search(r"retry_handle: ([A-Za-z0-9-]+)", terminal["text"])[1]
    digest = hashlib.sha256(known_handle.encode()).hexdigest()
    defaults = fixture("native_provider.json")
    chosen = []

    def level(host, request):
        command = None
        if not native.tool_receipts(host, request):
            evidence = [item["text"] for package in incoming(request) for item in package["evidence"]
                        if item["actor_kind"] == "tool_result"]
            for text in evidence:
                handle = re.search(r"retry_handle: ([A-Za-z0-9-]+)", text)
                if handle and "write: TIMEOUT" in text and "parse: PASS" in text:
                    command = "python3 retry_write.py " + shlex.quote(handle[1])
                    chosen.append({"stage": "write", "handle": handle[1]})
                    break
        return {**defaults, "command": command}

    with tempfile.TemporaryDirectory(prefix="bmem-e2e-import-") as path:
        work = Path(path).resolve()
        artifact = work / "recovered.json"
        (work / "retry_write.py").write_text("\n".join([
            "import hashlib,json,pathlib,sys",
            "handle=sys.argv[1]",
            f"if hashlib.sha256(handle.encode()).hexdigest() != {digest!r}: sys.exit(75)",
            f"data=dict(attempt={continuation['id']!r},task={data['work_task']!r},stage='write',handle=handle,result='recovered')",
            "pathlib.Path('recovered.json').write_text(json.dumps(data))",
            "print('write: recovered')",
        ]), encoding="utf-8")
        vault = native.VaultFixture(binary, work)
        with vault.running(), native.fixture_server(level) as (endpoint, captured), proposal_endpoint() as (extract, _):
            web = vault.cli("web", "--no-open")["url"]
            url = web.split("/session", 1)[0]
            owner = BrowserSession(url, web)
            browser = BrowserSession(url)
            vault.cli("model", "add", "e2e-extraction", "--protocol", "ollama", "--endpoint", extract, "--model-id", "fixture")
            vault.cli("model", "test", "e2e-extraction", "--for", "extract")
            vault.cli("model", "use", "e2e-extraction", "--for", "extract")
            task = post(owner, "/v2/tasks", {"project": data["project"], "anchor": data["work_task"]}, "e2e-task")["task_id"]
            host = continuation["host"]
            client, native_source = make_host(vault, owner, host, task)
            declaration = post(owner, "/v2/statements", {"original": data["declaration"]["raw"],
                "scope": {"kind": "project", "project": data["project"]}, "share": "authorized_assistants"}, "e2e-declaration")
            arrivals = {}
            for page in pages:
                sid = source(owner, page["source"])
                vault.grant("source", sid, "--purpose", "extract", "--allow", "true")
                vault.grant("source", sid, "--purpose", "task_delivery", "--client", client["client_id"], "--allow", "true")
                row = {"native_object": page["object"], "native_version": str(page["version"]), "task_id": task,
                       "actor_kind": {"assistant": "assistant_message", "tool": "tool_result"}[page["role"]]}
                if page["role"] == "tool":
                    row["checks"] = {"output": page["text"], "native_run": page["run"]}
                else:
                    row["text"] = page["text"]
                intake = {"source_id": sid, "next_cursor": "page-1", "events": [row]}
                response = post(owner, "/v2/events", intake, page["object"])
                arrivals[page["object"]] = response
                if page["role"] == "tool":
                    arrivals["repeat"] = post(owner, "/v2/events", intake, page["object"])
            if artifact.exists():
                raise ValueError("independent artifact must start empty")
            native.run_host(host, vault.command(host, client["client_id"], native_source, task), continuation["prompt"],
                            endpoint, work, vault.env, defaults, case["id"], writable=True)
            request = captured[host][0]
            package = incoming(request)[-1]
            blocks = list(native.text_blocks(request))
            ref_value = None
            for block in blocks:
                if "bmem reference data." in block:
                    wire = block.split("bmem reference data.", 1)[1].split("\n", 1)[1]
                    declarations = json.JSONDecoder().raw_decode(wire)[0]
                    ref_value = next((row["value"] for row in declarations if row["key"] == "code_example.language"), ref_value)
            deliver = next(row for row in vault.cli("delivery", "list")["deliveries"] if row["package_id"] == package["package_id"])
            out = [row for req in captured[host] for row in native.tool_receipts(host, req)]
            new_artifact = json.loads(artifact.read_text()) if artifact.exists() else None
            prior = terminal["text"]
            passed = (chosen == [{"stage": "write", "handle": known_handle}] and new_artifact is not None
                      and new_artifact["handle"] == known_handle and any("write: recovered" in text for text in native.text_blocks(out)))
            verifier = post(owner, "/v2/clients", {"host": "independent-verifier", "destination": "isolated-artifacts",
                "capabilities": {"verify_outcomes": True}}, "e2e-verifier")
            vault.grant("set", "--client", verifier["client_id"], "--task", task, "--allow", "true")
            authorization = {"Authorization": "Bearer " + verifier["token"], "X-Bmem-Instance": vault.cli("status")["instance_id"]}
            response_status, outcome = browser.request(f"/v2/attempts/{deliver['attempt_id']}/outcomes", "POST", {
                "event_id": "e2e-artifact-check", "status": "passed" if passed else "failed", "artifact_ref": "recovered.json",
                "validator_ref": "isolated-e2e-artifact-check", "evidence_ref": "tool stdout and recovered.json",
                "delivery_ids": [deliver["delivery_id"]],
            }, authentication=authorization)
            if response_status != 200:
                raise ValueError(f"independent verifier returned {response_status}")
            all_events = {item["event_id"] for receipt in arrivals.values() for item in receipt["events"]}
            actual_context = "\n".join(block for block in blocks if "bmem authorized task evidence." in block)
            return {
                "import_job": {"declaration": declaration, "page_receipts": arrivals},
                "resume": {"note": "captured real Codex loopback requests", "evidence": package, "tools": chosen},
                "verify": {"old_tool_result": prior, "tool_stdout": out, "file": new_artifact, "outcome": outcome},
            }, {
                "import_job": {"distinct_native_versions": len(all_events)},
                "resume": {"package_mode": package["package_mode"], "example_language": ref_value.capitalize() if ref_value else None,
                           "actual_input_text": actual_context},
                "verify": {"parse": "passed" if "parse: PASS" in prior else "unknown",
                           "write": "failed" if "write: TIMEOUT" in prior else "unknown", "new_tool_stages": [op["stage"] for op in chosen],
                           "new_artifact": new_artifact, "new_independent_outcome": outcome["status"]},
            }
