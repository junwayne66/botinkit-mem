"""Run the original same-run and candidate-budget samples through the native adapter."""

from pathlib import Path
import tempfile

from baseline_probe import BrowserSession, events, fixture, native, post
from replay_native_handoff import incoming, make_host


WINDOW_CASES = {"F04-06", "F04-07", "F04-11", "F04-12", "F04-13", "F04-15"}


def replay_windows(case, binary):
    data = case["fixture"]
    host = "codex"
    defaults = fixture("native_provider.json")
    broken_output = case["id"] == "F04-15"
    if broken_output:
        defaults = {**defaults, "command": "printf '0\\n' > submitted.txt"}
    primary = next(iter(data["clients"].values()))
    raw, view = {}, {}
    with tempfile.TemporaryDirectory(prefix="bmem-native-window-") as folder:
        work = Path(folder).resolve()
        vault = native.VaultFixture(binary, work)
        with vault.running(), native.fixture_server(defaults) as (endpoint, captured):
            url = vault.cli("web", "--no-open")["url"]
            owner = BrowserSession(url.split("/session", 1)[0], url)
            task = post(owner, "/v2/tasks", {"anchor": primary["task"], "project": primary["project"]}, "window-task")["task_id"]
            reader, source = make_host(vault, owner, host, task)
            aliases = {}

            def ingest(rows, allowed):
                copy = []
                for item in rows:
                    row = {**item, "at": item.get("ts", "2026-11-23T01:00:00Z")}
                    if item["role"] == "tool_result":
                        row["actor_kind"] = "tool_result"
                        row["checks"] = {"output": row.pop("text")}
                    elif item["role"] in ("tool_call", "assistant"):
                        row["actor_kind"] = "assistant_message"
                    copy.append(row)
                indexed, sources = events(owner, copy, {primary["task"]: task})
                aliases.update({"event_" + event: label for event, label in indexed.items()})
                for name, ident in sources.items():
                    if name in allowed:
                        vault.grant("source", ident, "--purpose", "task_delivery", "--client", reader["client_id"], "--allow", "true")
                for record in rows:
                    if "run_id" in record:
                        handle = next(ref for ref, key in aliases.items() if key == record["ref"])
                        post(owner, "/v2/evidence/tool-link", {"handle": handle, "run_id": record["run_id"],
                                "tool_call_id": record.get("tool_call_id", ""),
                                "role": {"tool_result": "result", "assistant": "plan"}.get(record["role"], "call")}, "link-"+record["ref"])
                return {"events": indexed, "sources": sources}

            allowed = {name for name, grant in data.get("grants", {}).get("hA", {primary["source"]:"allow"}).items() if grant == "allow"}
            ingest(data["events"], allowed)

            def refs(handles):
                return [aliases[ref] for ref in handles if ref in aliases]

            for step in case["steps"]:
                name, args = step["id"], step["args"]
                if name == "hidden":
                    raw[name] = view[name] = ingest(data[args["fixture_field"]], set())
                elif name in ("window", "seed"):
                    request = {key: value for key, value in args.items() if key != "reader"}
                    result = post(owner, "/v2/search", {**request, "task_id": task}, name)
                    raw[name] = result
                    view[name] = {"expanded_refs": refs(result.get("evidence_window", {}).get("window", {}).get("expanded_refs", [])),
                                  "refs": refs([row.get("handle") for row in result["results"]])}
                elif name == "compare":
                    before, after = (view[args[k]] for k in ("first", "second"))
                    raw[name] = view[name] = {
                        "same_ordered_refs": before["model_input_evidence_refs"] == after["model_input_evidence_refs"],
                        "same_reason_list": before["recipient_metadata"]["seed_reasons"] == after["recipient_metadata"]["seed_reasons"],
                        "same_recipient_visible_counts": before["recipient_metadata"]["recipient_visible_count"] == after["recipient_metadata"]["recipient_visible_count"],
                    }
                elif name == "judge":
                    received = (work / args["actual_file"]).read_bytes()
                    expected = data["verifier"]["contents"].encode()
                    raw[name] = view[name] = {"match": received == expected, "observed_text": received.decode()}
                else:
                    offset = len(captured[host])
                    command = vault.command(host, reader["client_id"], source, task)
                    command += f" --evidence-slots {args['evidence_slots']} --candidate-limit {args.get('candidate_limit',1)}"
                    native.run_host(host, command, args["query"], endpoint, work, vault.env, defaults,
                                    case["id"]+"-"+name, writable=broken_output)
                    packages = incoming(captured[host][offset])
                    package = packages[-1] if packages else {}
                    info = package.get("window") or {}
                    raw[name] = {"captured_package": package, "request_count": len(captured[host])-offset}
                    view[name] = {"model_input_evidence_refs": refs([row["handle"] for row in package.get("evidence", [])]),
                                  "model_input_evidence_text": "\n".join(row["text"] for row in package.get("evidence", [])),
                                  "ranked_seed_refs": refs(info.get("ranked_seed_refs", [])), "evidence_gap": info.get("evidence_gap"),
                                  "result_status": info.get("result_status"), "recipient_metadata": info,
                                  "verified_result_refs": refs([row["handle"] for row in package.get("observations", []) if row["actor_kind"]=="tool_result"])}
    return raw, view
