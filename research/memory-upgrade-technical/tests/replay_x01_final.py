"""Run the isolated PostgreSQL handoff and full Mem0 comparison for the frozen catalogue."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

from baseline_probe import isolated, post, source
from replay_optimization import bound, load, task
from score import catalogue, grade
from test_integration import BmemIntegration


SUPPORTED = {"X01-10", "X01-12"}
POSTGRES = "postgres@sha256:1018733457011bf88d81db98cf43aac6d5150afc18787dbe204771fedf80ff95"
SIDECAR = Path(__file__).with_name("mem0_replay_port.py")


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sql_json(value):
    # psql receives data as hex, never as executable SQL or shell text.
    return "convert_from(decode('" + json.dumps(value, ensure_ascii=False).encode().hex() + "','hex'),'UTF8')::jsonb"


@contextmanager
def postgres_sandbox():
    name = "bmem-x01-10-" + uuid4().hex[:12]
    docker = ["docker", "--context", "default"]
    started = subprocess.run(docker + ["run", "--pull", "never", "--detach", "--rm", "--name", name,
                              "--network", "none", "--tmpfs", "/var/lib/postgresql:rw,size=128m",
                              "--env", "POSTGRES_HOST_AUTH_METHOD=trust", POSTGRES],
                             capture_output=True, text=True, timeout=25)
    if started.returncode:
        raise RuntimeError("could not start the pinned isolated PostgreSQL image: " + started.stderr.strip())

    def query(sql, wait=False):
        # TCP readiness excludes the different bootstrap daemon started by the image.
        command = docker + ["exec", "-i", name, "psql", "-X", "-Aqt", "-h", "127.0.0.1",
                            "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
        deadline = monotonic() + (25 if wait else 0)
        while True:
            result = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=15)
            if not result.returncode:
                return result.stdout.strip()
            if monotonic() >= deadline:
                raise RuntimeError("isolated PostgreSQL query failed: " + result.stderr.strip())
            sleep(0.15)

    try:
        version = query("SELECT version();", wait=True)
        query(Path(__file__).with_name("authority_handoff.pg.sql").read_text(encoding="utf-8"))
        yield query, version
    finally:
        subprocess.run(docker + ["stop", "-t", "3", name], capture_output=True, text=True, timeout=12)


def read_sqlite(path, source_id, grants, base_intents, revisions=(), stopped=False):
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if stopped else "")
    with sqlite3.connect(uri, uri=True) as db:
        checkpoint, = db.execute("SELECT cursor FROM ing_sources WHERE source_id=?", [source_id]).fetchone()
        events = [{"native_id": native, "event_id": event, "root_id": root, "content": json.loads(content), "active": bool(active)}
                  for native, event, root, content, active in db.execute(
                      "SELECT native_object,event_id,root_id,content,active FROM ing_event_versions "
                      "WHERE source_id=? ORDER BY native_object", [source_id])]
        origins = []
        for alias, client in grants.items():
            row = db.execute("SELECT source_id,client_id FROM recall_grants WHERE source_id=? AND client_id=?",
                             [source_id, client]).fetchone()
            if row:
                origins.append({"kind": "grant", "key": alias, "body": {"source_id": row[0], "client_id": row[1]}})
        for key in base_intents:
            row = db.execute("SELECT fingerprint,receipt,cursor_before,cursor_after FROM ing_ingests "
                             "WHERE source_id=? AND idem_key=?", [source_id, key]).fetchone()
            if row:
                origins.append({"kind": "intake_intent", "key": key, "body": {
                    "fingerprint": row[0], "receipt": json.loads(row[1]), "cursor_before": row[2], "cursor_after": row[3]}})
        tombstones = [row[0] for row in db.execute("SELECT v.root_id FROM ing_root_revocations v JOIN ing_roots r USING(root_id) "
                                                  "WHERE r.source_id=? ORDER BY v.root_id", [source_id])]
        values = [row for key in revisions for row in db.execute("SELECT key,result FROM authority_intents WHERE actor='owner' AND key=?", [key])]
        results = {key: json.loads(value) for key, value in values}
        drained, = db.execute("SELECT count(*) FROM ing_jobs WHERE status='running' AND source_id=?", [source_id]).fetchone()
    return {"checkpoint": checkpoint, "events": events, "origins": sorted(origins, key=lambda row: (row["kind"], row["key"])),
            "tombstones": tombstones, "visible_events": [row["native_id"] for row in events if row["active"]],
            "intents": results, "running_leases": drained}


def current_language(owner):
    status, result = owner.request("/v2/statements")
    if status != 200:
        raise RuntimeError(f"statement query returned {status}")
    return next((row for row in result["statements"] if row["key"] == "code_example.language"), None)


def statement(language):
    # Use the stable declaration field; the source wording remains unchanged in the intake event.
    return {"scope": {"kind": "global"}, "original": "所有代码示例使用 " + language + "。",
            "share": "authorized_assistants"}


def migration_case(case):
    seed = case["fixture"]["source"]
    if case["fixture"]["migration_mode"] != "single_writer":
        raise ValueError("handoff requires one writer")
    later = []
    for row in case["fixture"]["post_cutover"]:
        if row["op"] == "correct":
            match = re.fullmatch(r"有效值改为\s+([A-Za-z][A-Za-z0-9_+#-]*)", row["text"])
            if not match:
                raise ValueError("unsupported native correction envelope")
            later.append({**row, "value": match[1].lower()})
        else:
            later.append(dict(row))
    with isolated() as (service, owner), postgres_sandbox() as (sql, version):
        sid = source(owner, "authority-transfer-source")
        task_id = task(owner, "authority transfer", "handoff")
        grants = {label: bound(service, owner, "transfer-reader-" + label, task_id, [sid], label)[2]["client_id"]
                  for label in seed["grants"]}
        initial = post(owner, "/v2/events", {"source_id": sid, "next_cursor": seed["checkpoint"],
            "events": [{"native_object": alias, "native_version": "1", "text": alias, "task_id": task_id,
                        "actor_kind": "user_message"} for alias in seed["events"]]}, seed["intents"][0])
        process = service.daemon
        service.stop()
        if process.returncode != 0 or service.cli("status")["state"] != "stopped":
            raise RuntimeError("source writer did not stop gracefully")
        sqlite_path = service.root / "data/bmem.sqlite3"
        snapshot = read_sqlite(sqlite_path, sid, grants, seed["intents"], stopped=True)
        if snapshot["running_leases"]:
            raise RuntimeError("authority has an in-flight worker after stop")
        sql(f"SELECT handoff_seed({sql_json(snapshot)});")
        staged = json.loads(sql("SELECT handoff_state();"))
        if any(staged[key] != snapshot[key] for key in ("checkpoint", "tombstones", "visible_events", "origins")):
            raise RuntimeError("initial authority metadata differs across storage engines")
        target_events = json.loads(sql("SELECT jsonb_agg(to_jsonb(e) ORDER BY native_id) FROM handoff_events e;"))
        if target_events != snapshot["events"]:
            raise RuntimeError("initial event identities or versions differ across storage engines")
        sql("UPDATE handoff_control SET writer='postgres' WHERE writer='none';")
        started = json.loads(sql("SELECT handoff_state();"))
        source_off = service.cli("status")["state"] == "stopped"
        applied = [json.loads(sql(f"SELECT handoff_apply({sql_json(row)});")) for row in later]
        duplicate = json.loads(sql(f"SELECT handoff_apply({sql_json(later[0])});"))
        if duplicate != applied[0]:
            raise RuntimeError("PostgreSQL intent retry did not return the original receipt")
        paused = json.loads(sql("SELECT handoff_quiesce();"))
        stale = json.loads(sql("SELECT handoff_cut_back();"))
        if stale["state"] == "activated":
            raise RuntimeError("a stale source was activated before replay")
        fenced = json.loads(sql(f"SELECT handoff_apply({sql_json(later[0])});"))
        if fenced["state"] != "writer_fenced":
            raise RuntimeError("the sealed PostgreSQL writer still accepted a request")
        journal = json.loads(sql("SELECT jsonb_agg(to_jsonb(c) ORDER BY sequence) FROM handoff_changes c;"))

        # Replay while both advertised writers are fenced, using an unpublished SQLite recovery copy.
        shadow = BmemIntegration()
        shadow.setUp()
        try:
            shadow_path = shadow.root / "data/bmem.sqlite3"
            with sqlite3.connect(sqlite_path.as_uri() + "?mode=ro&immutable=1", uri=True) as original, sqlite3.connect(shadow_path) as replica:
                original.backup(replica)
            shadow.start()
            recovered_owner, _ = shadow.web()
            repayments = []
            for entry in journal:
                change = entry["request"]
                if change["op"] == "correct":
                    receipt = post(recovered_owner, "/v2/statements", statement(change["value"]), change["intent"])
                else:
                    root = next(row["root_id"] for row in snapshot["events"] if row["native_id"] == change["id"])
                    payload = {"kind": "forget", "target": {"root_id": root}}
                    preview = post(recovered_owner, "/v2/operations/preview", payload, "handoff-preview-" + change["intent"])
                    receipt = post(recovered_owner, "/v2/operations", {**payload, "preview_id": preview["preview_id"]}, change["intent"])
                repayments.append({"sequence": entry["sequence"], "intent": change["intent"], "receipt": receipt})
            current = current_language(recovered_owner)
            correction = next(row for row in later if row["op"] == "correct")
            replica_snapshot = read_sqlite(shadow_path, sid, grants, seed["intents"], [correction["intent"]])
            revision = replica_snapshot["intents"][correction["intent"]]["dimensions"][0]["revision_id"]
            if current is None or current["revision_id"] != revision:
                raise RuntimeError("the replica's live authority disagrees with its durable receipt")
            recovery = {key: replica_snapshot[key] for key in ("checkpoint", "origins", "visible_events", "tombstones")}
            recovery.update(value=current["value"], through=repayments[-1]["sequence"])
            shadow.stop()
            # SQLite backup is used only for the isolated physical recovery copy, after logical reconciliation.
            with sqlite3.connect(shadow_path.as_uri() + "?mode=ro&immutable=1", uri=True) as replica, sqlite3.connect(sqlite_path) as original:
                replica.backup(original)
        finally:
            shadow.doCleanups()
        acknowledged = json.loads(sql(f"SELECT handoff_acknowledge({sql_json(recovery)});"))
        activated = json.loads(sql("SELECT handoff_cut_back();"))
        if acknowledged["state"] != "replica_verified" or activated["state"] != "activated":
            raise RuntimeError("SQLite was not authorized for cutback")
        service.start()
        owner, _ = service.web()
        correction_payload = statement(correction["value"])
        count_correction = """SELECT count(*) FROM authority_statements s
            JOIN authority_revisions r ON r.statement_id=s.id WHERE s.original=? AND r.value=?"""
        correction_match = [correction_payload["original"], correction["value"]]
        with sqlite3.connect(sqlite_path.as_uri() + "?mode=ro", uri=True) as db:
            effects_before, = db.execute(count_correction, correction_match).fetchone()
        retry = post(owner, "/v2/statements", correction_payload, correction["intent"])
        live = current_language(owner)
        readback = read_sqlite(sqlite_path, sid, grants, seed["intents"], [correction["intent"]])
        with sqlite3.connect(sqlite_path.as_uri() + "?mode=ro", uri=True) as db:
            effects, = db.execute(count_correction, correction_match).fetchone()
        pg_effects = int(sql("SELECT count(*) FROM handoff_revisions;"))
        if pg_effects != 1 or effects_before != 1 or effects != 1 or retry != readback["intents"][correction["intent"]] or live["revision_id"] != revision:
            raise RuntimeError("the cross-backend correction did not retain one authority effect")
        raw = {"move": {"image": POSTGRES, "postgres_version": version, "source_intake": initial,
                        "source_snapshot_sha256": json_digest(snapshot), "verified_target": staged,
                        "source_stopped": source_off, "target_writer": started["writer"],
                        "applied": applied, "idempotent_target_retry": duplicate},
               "rollback": {"sealed_target": paused, "stale_attempt": stale, "fenced_target_write": fenced,
                            "journal": journal, "replica_receipts": repayments, "vs_target": acknowledged,
                            "active_writer": activated, "reissued_sqlite_receipt": retry,
                            "readback": readback, "postgres_effects": pg_effects,
                            "sqlite_effects_before_retry": effects_before, "sqlite_effects": effects}}
        return raw, {"move": {"write_primaries": int(started["writer"] == "postgres") + int(not source_off),
                              "checkpoint": staged["checkpoint"]},
                     "rollback": {"stale_cutback_allowed": stale["state"] == "activated",
                                  "visible_value_after_sync": live["value"].capitalize(),
                                  "visible_events": readback["visible_events"], "i3_effect_count": effects}}


@contextmanager
def upstream_process():
    python = Path(os.environ.get("BMEM_X01_MEM0_PYTHON", sys.executable)).expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("set BMEM_X01_MEM0_PYTHON to the isolated interpreter with the pinned Mem0 release")
    with tempfile.TemporaryDirectory(prefix="bmem-x01-12-mem0-") as folder:
        base = Path(folder).resolve()

        def run(payload):
            work = Path(tempfile.mkdtemp(dir=base)).resolve()
            temporary = work / "temp"
            temporary.mkdir()
            environment = {"PATH": os.defpath, "TMPDIR": str(temporary), "XDG_CACHE_HOME": str(work / "cache"),
                           "MEM0_TELEMETRY": "false", "MEM0_DIR": str(work / "mem0"), "NO_PROXY": "*",
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
            request = {"endpoint": "http://127.0.0.1:11434", "model": "qwen3:4b-instruct-2507-q4_K_M",
                       "embed_model": "bge-m3:latest", **payload}
            result = subprocess.run([str(python), "-B", str(SIDECAR)], cwd=work, env=environment,
                                    input=json.dumps(request, ensure_ascii=False), encoding="utf-8",
                                    capture_output=True, timeout=240)
            if result.returncode:
                detail = result.stderr.strip().splitlines() or [f"exit status {result.returncode}"]
                raise RuntimeError("the actual isolated Mem0 arm did not finish: " + detail[-1])
            # Earlier SDK lines can contain human-oriented initialization notices.
            return json.loads(result.stdout.splitlines()[-1])

        yield run


def language_from_event(row):
    match = re.fullmatch(r"(?:默认|更正)\s+([A-Za-z][A-Za-z0-9_+#-]*)", row["text"])
    if not match:
        raise ValueError("unsupported native language observation")
    return match[1]


def bmem_arm(timeline, merger=None):
    with isolated() as (service, owner):
        writes = []
        # Every observed endpoint for this arm must be from the just-started isolated daemon.
        status = service.cli("status")
        observed = status["instance_id"]
        if observed != service.instance_id or urlsplit(owner.base_url).hostname not in ("127.0.0.1", "::1"):
            raise RuntimeError("bmem arm has no scoped local write boundary")
        if not service.root.resolve().is_relative_to(service.work.resolve()):
            raise RuntimeError("bmem arm vault is outside its temporary workspace")

        def audit_requests(session):
            request = session.request

            def scoped(path, method="GET", *args, **kwargs):
                if method in ("POST", "PATCH", "PUT", "DELETE"):
                    scope_ok = session.base_url == status["endpoint"] and path.startswith("/v2/")
                    if not scope_ok:
                        raise RuntimeError("refused a comparison write outside the isolated daemon")
                    code, body = request(path, method, *args, **kwargs)
                    writes.append({"method": method, "path": path, "status": code,
                                   "isolated": scope_ok, "instance_id": observed})
                    return code, body
                return request(path, method, *args, **kwargs)

            session.request = scoped

        audit_requests(owner)
        sid = source(owner, "frozen-language")
        task_id = task(owner, "language comparison", "frozen-x01-12")
        reader = bound(service, owner, "freeze-comparison-reader", task_id, [sid], "compare-reader")
        audit_requests(reader[0])
        trace = []
        upstream_calls = []
        event_ids = {}
        cursor = None
        initial = current_language(owner)
        if initial is not None:
            raise RuntimeError("bmem comparison arm was not empty")
        for item in timeline:
            when = item["time"]
            if item["kind"] == "event":
                body = {"source_id": sid, "next_cursor": str(when), "events": [{
                    "native_object": item["id"], "native_version": "1", "text": item["text"],
                    "task_id": task_id, "actor_kind": "user_message"}]}
                if cursor is not None:
                    body["cursor"] = cursor
                receipt = post(owner, "/v2/events", body, "intake-" + item["id"])
                event = receipt["events"][0]
                event_ids[event["event_id"]] = item["id"]
                cursor = receipt["cursor"]
                language = language_from_event(item)
                current = current_language(owner)
                apply = True
                record = {"time": when, "native_id": item["id"], "intake_event_id": event["event_id"]}
                if merger is not None:
                    decision = merger({"mode": "merge", "event": item,
                                       "before": [] if current is None else [{"id": "0", "text": "默认 " + current["value"]}]})
                    upstream_calls.append(decision)
                    decisions = decision["result"]["proposal"]
                    # The merger proposes changes. Authority may only commit a supported change
                    # for this dimension and matching known input.
                    apply = any(row.get("event") in ("ADD", "UPDATE")
                                and re.search(r"\b" + re.escape(language) + r"\b", str(row.get("text", "")), re.I)
                                and (current is None or row.get("event") == "UPDATE" and str(row.get("id")) == "0")
                                for row in decisions)
                    record["merger_accepted"] = apply
                if apply:
                    payload = statement(language)
                    if current is not None:
                        payload["corrects_revision_id"] = current["revision_id"]
                    changed = post(owner, "/v2/statements", payload, "authority-" + item["id"])
                    if not changed["dimensions"]:
                        raise RuntimeError(f"{item['id']} did not produce a declaration revision: {changed}")
                    record["applied_revision"] = changed["dimensions"][0]["revision_id"]
                trace.append(record)
            else:
                delivery = load(reader, "query-" + str(when), task_id)
                active = next((row for row in delivery["statements"] if row["key"] == "code_example.language"), None)
                evidence = [event_ids[row["event_id"]] for row in delivery["evidence"] if row["event_id"] in event_ids]
                trace.append({"time": when, "query": item["q"], "response": {"answer_text": active["value"] if active else ""},
                              "input_watermark": list(event_ids.values()), "visible_refs": evidence,
                              "delivery_id": delivery["delivery_id"]})
        checked = service.cli("status")
        if checked["instance_id"] != observed or checked["endpoint"] != owner.base_url:
            raise RuntimeError("the bmem write target changed during the replay")
        return {"initial_memory_count": 0, "timeline": trace, "upstream_component_calls": upstream_calls,
                "isolation": {"instance_id": observed, "vault_is_temporary": True, "observed_write_actions": writes,
                              "nonisolated_writes": sum(not row["isolated"] and 200 <= row["status"] < 300 for row in writes)}}


def compare_systems(case, binary):
    data = case["fixture"]
    events = [{**row, "kind": "event"} for row in data["ordered_events"]]
    queries = [{**row, "kind": "query"} for row in data["queries"]]
    timeline = sorted(events + queries, key=lambda row: row["time"])
    if len({row["time"] for row in timeline}) != len(timeline):
        raise ValueError("ambiguous evaluation timeline")
    with upstream_process() as provider:
        current = bmem_arm(timeline)
        complete = provider({"mode": "full", "timeline": timeline})
        merger = bmem_arm(timeline, merger=provider)
    if current["isolation"]["instance_id"] == merger["isolation"]["instance_id"]:
        raise RuntimeError("both bmem strategy arms used the same authority database")
    results = dict(zip(data["arms"], [current, complete, merger]))
    tracked = [current["timeline"], complete["result"]["timeline"], merger["timeline"]]
    for trace in tracked:
        if [row["time"] for row in trace] != [row["time"] for row in timeline]:
            raise RuntimeError("an arm did not observe the complete interleaved timeline")
        for expected, measured in zip(timeline, trace):
            if expected["kind"] != "query":
                continue
            watermark = [row["id"] for row in events if row["time"] < expected["time"]]
            if measured["input_watermark"] != watermark:
                raise RuntimeError("query saw a write before its fixture time")
    boundary = [row["response"] for row in complete["result"]["timeline"] if "response" in row]
    blackbox = data["blackbox_contract"]
    lineage = "unknown" if blackbox["no_source_refs"] and all(set(row) == set(blackbox["output"]) for row in boundary) else "observable"
    observed_boundaries = list(results.values()) + merger["upstream_component_calls"]
    production_writes = sum(arm["isolation"]["nonisolated_writes"] for arm in observed_boundaries)
    first = next(row for row in current["timeline"] if "response" in row)
    return {"compare": {"arms": results, "blackbox_lineage_verdict": lineage,
                        "production_writes": production_writes, "candidate_binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}}, {
        "compare": {"evaluated_arms": len(results), "first_query_visible_refs": first["visible_refs"],
                    "blackbox_lineage_verdict": lineage, "production_writes": production_writes},
    }


def replay_x01_final(case, binary):
    if case["id"] == "X01-10":
        return migration_case(case)
    if case["id"] == "X01-12":
        return compare_systems(case, binary)
    return {}, {}


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(f"usage: {sys.argv[0]} BMEM_BINARY CASE [RAW_EVIDENCE]")
    target = Path(sys.argv[1]).resolve(strict=True)
    os.environ["BMEM_V2_TEST_BIN"] = str(target)
    sample = catalogue()[sys.argv[2]]
    actual, normalized = replay_x01_final(sample, target)
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(json.dumps({"actual": actual, "normalized": normalized}, ensure_ascii=False, indent=2)+"\n")
    verdict, failures = grade(sample, normalized)
    print(json.dumps({"case": sample["id"], "status": verdict, "failed_assertions": failures}, ensure_ascii=False))
    raise SystemExit(verdict != "PASS")
