"""Spec Kit 007 acceptance scenarios using real bmem executables.

Opt in with BMEM_UPDATE_TEST_BIN, BMEM_UPDATE_LEGACY_BIN and OpenSSL 3.
Use BMEM_UPDATE_NEXT_BIN for a separate, genuinely newer compiled candidate.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

import native_hosts as native
from test_integration import BrowserSession, fixture
from update_fixtures import LEGACY_SHA256, Repository, isolated_environment, sha256

REPO_ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.environ.get("BMEM_UPDATE_TEST_BIN"), "set BMEM_UPDATE_TEST_BIN to run release acceptance")
class UpdateAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_binary = Path(os.environ["BMEM_UPDATE_TEST_BIN"]).resolve(strict=True)
        cls.legacy = Path(os.environ["BMEM_UPDATE_LEGACY_BIN"]).resolve(strict=True)
        expected = os.environ.get("BMEM_UPDATE_LEGACY_SHA256", LEGACY_SHA256)
        if sha256(cls.legacy) != expected:
            raise AssertionError("legacy is not the frozen 0.2.0 executable")
        cls.next_binary = os.environ.get("BMEM_UPDATE_NEXT_BIN")
        if cls.next_binary:
            cls.next_binary = Path(cls.next_binary).resolve(strict=True)
        cls.openssl = os.environ.get("BMEM_UPDATE_TEST_OPENSSL", shutil.which("openssl"))
        if not cls.openssl:
            raise RuntimeError("OpenSSL 3 is needed to generate ephemeral TUF metadata")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bmem-007-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.root = self.work / "original data"
        self.env = isolated_environment(self.work)
        self.suffix = ".exe" if os.name == "nt" else ""
        self.canonical = self.work / "installed" / ("bmem" + self.suffix)
        self.canonical.parent.mkdir()
        shell_bin = self.work / "shell bin"
        shell_bin.mkdir()
        self.launcher = shell_bin / ("bmem" + self.suffix) if os.name != "nt" else self.canonical
        self.env["PATH"] = str(shell_bin if self.launcher != self.canonical else self.canonical.parent) + os.pathsep + self.env["PATH"]
        self.installer = self.work / ("bmem-installer" + self.suffix)
        shutil.copy2(self.test_binary, self.installer)
        self.repo = Repository(self.work / "signed-channel", self.openssl)
        self.addCleanup(self.repo.close)
        self.env["BMEM_UPDATE_TEST_REPOSITORY"] = str(self.repo.path)
        self.info = self.receipt(self.call("build-info", binary=self.test_binary))
        self.initial_asset = self.repo.asset(self.test_binary, self.info)
        self.initial_release = self.repo.release(self.initial_asset, self.info, ["0.0.0", "0.2.0"], [0, 1])
        self.repo.publish([self.initial_release])
        self.addCleanup(self.stop)

    def argv(self, *args, binary=None, root=True):
        return [str(binary or self.launcher), "--json",
                *(["--home", str(self.root)] if root else []), *args]

    def call(self, *args, binary=None, root=True, extra_env=None, timeout=120):
        return subprocess.run(
            self.argv(*args, binary=binary, root=root), cwd=self.work,
            env={**self.env, **(extra_env or {})}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )

    def status(self):
        return self.receipt(self.call("status"))

    def receipt(self, result, codes=(0,)):
        self.assertIn(result.returncode, codes, (result.stdout, result.stderr))
        return json.loads(result.stdout or result.stderr)

    def make_launcher(self):
        if self.launcher != self.canonical:
            self.launcher.symlink_to(self.canonical)

    def install(self, adopt=False, binary=None, extra_env=None):
        args = ["--adopt"] if adopt else []
        return self.call(*args, "--binary", str(self.launcher if adopt else self.canonical),
                         binary=binary or self.installer, extra_env=extra_env)

    def completed(self, result, status):
        if result.returncode == 10:
            return self.wait_for_windows_update(result, status)
        receipt = self.receipt(result)
        self.assertEqual(receipt["status"], status, receipt)
        return receipt

    def legacy_installation(self):
        shutil.copy2(self.legacy, self.canonical)
        self.make_launcher()
        self.assertEqual(self.status()["state"], "uninitialized")
        return self.receipt(self.call("init", "--non-interactive"))["instance_id"]

    def legacy_adopted(self, service_running=True):
        instance_id = self.legacy_installation()
        if service_running:
            self.receipt(self.call("service", "start"))
        self.completed(self.install(adopt=True), "installed")
        self.assertEqual(self.status()["instance_id"], instance_id)
        return instance_id

    def stop(self):
        if self.root.exists():
            for binary in (self.canonical, self.test_binary):
                if binary.is_file():
                    result = self.call("service", "stop", binary=binary, timeout=15)
                    if result.returncode == 0:
                        break
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                current = self.call("status", binary=self.test_binary, timeout=5)
                if current.returncode == 0 and json.loads(current.stdout)["state"] != "running":
                    break
                time.sleep(0.05)

    def owner(self):
        url = self.receipt(self.call("web", "--no-open"))["url"]
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "http")
        self.assertIn(parsed.hostname, ("127.0.0.1", "::1"))
        return BrowserSession(f"{parsed.scheme}://{parsed.netloc}", url)

    def post(self, owner, path, payload, key):
        status, result = owner.request(path, "POST", payload, key=key)
        self.assertIn(status, (200, 201, 202), result)
        return result

    def operate(self, owner, command, key):
        preview = self.post(owner, "/v2/operations/preview", command, "preview-" + key)
        return self.post(owner, "/v2/operations", {**command, "preview_id": preview["preview_id"]}, key)

    def seed_page(self, owner, suffix):
        sample = fixture("source_page.json")
        source = self.post(owner, "/v2/sources",
                           {**sample["source"], "account": "007-" + suffix},
                           "source-" + suffix)
        source_id = source["source_id"]
        self.operate(owner, {"kind": "resume_capture", "source_id": source_id}, "capture-" + suffix)
        page = {**sample["page"], "source_id": source_id}
        receipt = self.post(owner, "/v2/events", page, "event-" + suffix)
        return source_id, page, receipt

    def rows(self, statement, parameters=(), database="bmem.sqlite3", stopped=False):
        path = self.root / "data" / database
        uri = path.as_uri() + ("?mode=ro&immutable=1" if stopped else "?mode=ro")
        db = sqlite3.connect(uri, uri=True)
        try:
            return db.execute(statement, parameters).fetchall()
        finally:
            db.close()

    def add_next_release(self):
        if not self.next_binary:
            self.skipTest("set BMEM_UPDATE_NEXT_BIN to a separately compiled newer test version")
        info = self.receipt(self.call("build-info", binary=self.next_binary))
        self.assertGreater(tuple(map(int, info["version"].split("."))),
                           tuple(map(int, self.info["version"].split("."))))
        self.assertNotEqual(info["build_id"], self.info["build_id"])
        asset = self.repo.asset(self.next_binary, info)
        release = self.repo.release(asset, info, [self.info["version"]], [self.info["authority_epoch"]])
        self.repo.publish([release], version=2)
        return info, asset

    def active_health(self, instance, info):
        owner = self.owner()
        status, health = owner.request("/v2/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["instance_id"], instance)
        self.assertEqual(health["build_id"], info["build_id"])
        self.assertEqual(health["web_build_id"], info["web_build_id"])
        return owner

    @contextmanager
    def mcp(self, client):
        process = subprocess.Popen(
            self.argv("mcp", "serve", "--client", client),
            cwd=self.work, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        received = Queue()

        def read_replies():
            for line in process.stdout:
                received.put(json.loads(line))

        reader = threading.Thread(target=read_replies, daemon=True)
        reader.start()

        def send(method, ident=None, params=None):
            message = {"jsonrpc": "2.0", "method": method}
            if ident is not None:
                message["id"] = ident
            if params is not None:
                message["params"] = params
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if ident is not None:
                result = received.get(timeout=12)
                self.assertEqual(result["id"], ident)
                return result["result"]

        try:
            initialized = send("initialize", 1, {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "bmem-007-acceptance", "version": "1"},
            })
            send("notifications/initialized")
            yield process, initialized["serverInfo"], send
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_u01_signed_fresh_install_check_and_own_instance(self):
        self.completed(self.install(), "installed")
        self.make_launcher()
        self.assertEqual(shutil.which("bmem", path=self.env["PATH"]), str(self.launcher))
        self.assertEqual(sha256(self.canonical), self.initial_release["sha256"])
        instance = self.receipt(self.call("init", "--non-interactive"))["instance_id"]
        self.assertEqual(self.status()["state"], "stopped")
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "up_to_date")
        self.assertEqual(self.status()["state"], "stopped")
        self.active_health(instance, self.info)

    def test_u02_adoption_reconnects_hosts_and_preserves_authority(self):
        instance_id = self.legacy_installation()
        owner = self.owner()
        declarations = fixture("declarations.json")
        declared = self.receipt(self.call("declare", declarations["initial"], "--share",
                                         "authorized_assistants", "--idempotency-key", "original"))
        self.receipt(self.call("config", "set", "profile", "high"))
        kept, page, original_receipt = self.seed_page(owner, "kept")
        removed, _, _ = self.seed_page(owner, "removed")
        self.operate(owner, {"kind": "forget", "target": {"source_id": removed}}, "withdrawn")
        vault = native.VaultFixture(self.launcher, self.work)
        vault.root = self.root
        vault.env = {**self.env, "BMEM_TEST_PROVIDER_TOKEN": native.FIXTURE_TOKEN}
        clients = {host: vault.register(host) for host in native.HOSTS}
        task_id = self.receipt(self.call("task", "create", "007 real host", "--project", "release-acceptance"))["task_id"]
        for client, _ in clients.values():
            vault.grant("set", "--client", client, "--task", task_id, "--allow", "true")
        for client, _ in clients.values():
            with self.mcp(client) as (_, version, send):
                self.assertEqual(version["version"], "0.2.0")
                self.assertEqual([item["name"] for item in send("tools/list", 2)["tools"]], ["search"])

        result = self.completed(self.install(adopt=True), "installed")
        self.assertTrue(result.get("operation_id"))
        self.assertEqual(sha256(self.canonical), self.initial_release["sha256"])
        if os.name != "nt":
            self.assertEqual(self.launcher.resolve(), self.canonical)
        self.assertEqual(self.status()["state"], "running")
        owner = self.active_health(instance_id, self.info)
        self.assertEqual(owner.request("/v2/config")[1]["profile"], "high")
        same = self.post(owner, "/v2/events", page, "event-kept")
        self.assertEqual(same, original_receipt)
        sources = owner.request("/v2/sources")[1]["sources"]
        self.assertEqual(next(source for source in sources if source["source_id"] == kept)["cursor"],
                         page["next_cursor"])
        hits = self.post(owner, "/v2/search", {"query": "复验解析"}, "check-post-adopt")["results"]
        self.assertTrue(hits)
        self.assertEqual({hit["event_id"] for hit in hits},
                         {item["event_id"] for item in original_receipt["events"] if item["native_object"] == "run-plan"})
        jobs = owner.request("/v2/jobs")[1]["jobs"]
        self.assertTrue(any(job["status"] == "pending" for job in jobs))
        for client, _ in clients.values():
            with self.mcp(client) as (_, version, send):
                self.assertEqual(version["version"], self.info["version"])
                result = send("tools/call", 2, {"name": "search", "arguments": {"query": "report.opening"}})
                self.assertFalse(result.get("isError", False))
                opening = next(item["revision_id"] for item in declared["dimensions"]
                               if item["key"] == declarations["opening_key"])
                self.assertIn(opening, set(native.text_blocks(result)))

        if os.environ.get("BMEM_UPDATE_NATIVE_HOSTS") == "1":
            by_key = {item["key"]: item["revision_id"] for item in declared["dimensions"]}
            expected = {
                (declarations["language_key"], by_key[declarations["language_key"]], declarations["language"]),
                (declarations["opening_key"], by_key[declarations["opening_key"]], declarations["initial_opening"]),
            }
            provider = fixture("native_provider.json")
            with native.fixture_server(provider) as (endpoint, captured), ThreadPoolExecutor(max_workers=2) as pool:
                calls = {host: pool.submit(native.run_host, host, vault.command(host, *clients[host], task_id),
                                           provider["prompt"] + " update " + host, endpoint, self.work,
                                           vault.env, provider, "U02-after-adoption")
                         for host in native.HOSTS}
                for host, call in calls.items():
                    call.result()
                    native.prove_loaded(host, captured[host], provider["prompt"] + " update " + host,
                                        expected, None, provider)

    def test_u03_real_continuous_update_and_repeated_noop_while_stopped(self):
        instance_id = self.legacy_adopted(service_running=False)
        self.assertEqual(self.status()["state"], "stopped")
        duplicate = self.work / "other installation" / ("bmem" + self.suffix)
        duplicate.parent.mkdir()
        shutil.copy2(self.legacy, duplicate)
        self.env["PATH"] = self.env["PATH"] + os.pathsep + str(duplicate.parent)
        next_info, next_asset = self.add_next_release()
        checked = self.receipt(self.call("update", "--check"))
        self.assertEqual(checked["status"], "update_available")
        self.assertEqual(self.status()["state"], "stopped")
        changed = self.completed(self.call("update"), "updated")
        self.assertEqual(changed["installed_build"], next_info["build_id"])
        self.assertEqual(sha256(self.canonical), sha256(next_asset))
        self.assertEqual(self.status()["state"], "stopped")
        self.assertEqual(self.receipt(self.call("update"))["status"], "up_to_date")
        self.assertEqual(self.status()["state"], "stopped")
        self.assertEqual(sha256(duplicate), sha256(self.legacy))
        self.active_health(instance_id, next_info)

    def test_u04_expired_tampered_unavailable_and_old_metadata(self):
        instance_id = self.legacy_adopted()
        _, next_asset = self.add_next_release()
        manifest = json.loads((self.repo.path / "targets" / "release-catalog.json").read_text())
        next_release = manifest["releases"][0]
        before_hash = sha256(self.canonical)
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "update_available")

        faults = {
            "old_metadata": "update_metadata_rollback",
            "expired": "update_metadata_expired",
            "bad_signature": "update_signature_invalid",
            "wrong_target": "update_target_unavailable",
            "no_space": "update_no_space",
            "wrong_digest": "update_asset_digest_mismatch",
            "wrong_build": "candidate_invalid",
            "truncated_target": "update_asset_digest_mismatch",
            "missing_target": "internal_error",
        }
        for revision, (label, diagnostic) in enumerate(faults.items(), start=2):
            with self.subTest(label=label):
                if label == "old_metadata":
                    self.repo.publish([self.initial_release], version=1)
                elif label == "expired":
                    self.repo.publish([next_release], version=revision, expired=True)
                elif label == "bad_signature":
                    self.repo.publish([next_release], version=revision, bad_signature=True)
                elif label == "wrong_target":
                    self.repo.publish([{**next_release, "target": "unavailable-test-target"}], version=revision)
                elif label == "no_space":
                    self.repo.publish([next_release], version=revision)
                elif label == "wrong_digest":
                    self.repo.publish([{**next_release, "sha256": "1" * 64}], version=revision)
                elif label == "wrong_build":
                    self.repo.publish([{**next_release, "build_id": "test-claimed-build"}], version=revision)
                elif label == "truncated_target":
                    self.repo.publish([next_release], version=revision)
                    with next_asset.open("r+b") as binary:
                        binary.truncate(next_release["len"] // 2)
                else:
                    self.repo.publish([next_release], version=revision)
                    next_asset.unlink()
                test_environment = {"BMEM_UPDATE_TEST_FAIL_AT": "no_space"} if label == "no_space" else {}
                result = self.receipt(self.call("update", extra_env=test_environment), (20, 21))
                self.assertEqual(result["code"], diagnostic)
                self.assertEqual(sha256(self.canonical), before_hash)
                self.assertEqual(self.status()["state"], "running")
                self.active_health(instance_id, self.info)

    def test_u05_concurrent_updaters_and_frozen_old_writer(self):
        instance_id = self.legacy_adopted()
        expected, asset = self.add_next_release()
        owner = self.owner()
        source_id, page, accepted = self.seed_page(owner, "concurrent")
        with ThreadPoolExecutor(max_workers=3) as pool:
            updates = [pool.submit(self.call, "update") for _ in range(2)]
            cli_probe = pool.submit(self.call, "status")
            results = [operation.result() for operation in updates]
            self.assertEqual(cli_probe.result().returncode, 0)
        self.assertTrue(any(result.returncode in (0, 10) for result in results))
        self.assertTrue(all(result.returncode in (0, 10, 20) for result in results))
        if os.name == "nt":
            handoffs = [result for result in results if result.returncode == 10]
            self.assertEqual(len(handoffs), 1)
            for result in results:
                if result.returncode == 20:
                    self.assertEqual(self.receipt(result, (20,))["code"], "update_in_progress")
                elif result.returncode == 0:
                    self.assertEqual(self.receipt(result)["status"], "up_to_date")
            self.wait_for_windows_update(handoffs[0], "updated")
        self.assertEqual(sha256(self.canonical), sha256(asset))
        owner = self.active_health(instance_id, expected)
        self.assertEqual(self.post(owner, "/v2/events", page, "event-concurrent"), accepted)
        self.assertEqual(next(row for row in owner.request("/v2/sources")[1]["sources"]
                              if row["source_id"] == source_id)["cursor"], page["next_cursor"])

        self.stop()
        before = self.rows("PRAGMA user_version", stopped=True)
        sequence = self.rows("SELECT seq FROM authority_changes WHERE id=1", stopped=True)
        rejected = self.call("service", "run", binary=self.legacy, timeout=15)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("bmem_writer_epoch", rejected.stderr)
        self.assertEqual(self.rows("PRAGMA user_version", stopped=True), before)
        self.assertEqual(self.rows("SELECT seq FROM authority_changes WHERE id=1", stopped=True), sequence)
        self.assertEqual(self.status()["state"], "stopped")
        self.active_health(instance_id, expected)

    def test_u06_fault_before_commit_rolls_back_the_actual_binary_and_both_databases(self):
        instance_id = self.legacy_adopted()
        self.add_next_release()
        before_hash = sha256(self.canonical)
        before_epoch = self.rows("PRAGMA user_version")
        before_setting = self.receipt(self.call("config", "set", "profile", "high"))["profile"]
        for phase in ("snapshotted", "swapped", "migrated", "probed"):
            with self.subTest(phase=phase):
                failure = self.call("update", extra_env={"BMEM_UPDATE_TEST_FAIL_AT": phase})
                if os.name == "nt":
                    receipt = self.wait_for_windows_update(failure, "rolled_back")
                else:
                    receipt = self.receipt(failure, (22,))
                self.assertEqual(receipt["error_code"], "isolated_update_fault")
                self.assertEqual(sha256(self.canonical), before_hash)
                self.assertEqual(self.rows("PRAGMA user_version"), before_epoch)
                owner = self.active_health(instance_id, self.info)
                self.assertEqual(owner.request("/v2/config")[1]["profile"], before_setting)
                self.assertEqual(self.rows("PRAGMA quick_check", database="settings.sqlite3"), [("ok",)])

    def test_u06_interrupted_process_can_resume_with_same_transaction(self):
        instance_id = self.legacy_adopted()
        next_info, next_asset = self.add_next_release()
        stages = ("preflight", "drained", "snapshotted", "swapping", "swapped",
                  "migrating", "migrated", "probed", "committed")
        for phase in stages:
            with self.subTest(phase=phase):
                interrupted = self.call("update", extra_env={"BMEM_UPDATE_TEST_CRASH_AT": phase})
                if os.name == "nt" and phase != "preflight":
                    before = self.wait_for_windows_update(interrupted, "in_progress", phase=phase)
                else:
                    self.assertNotEqual(interrupted.returncode, 0)
                    before = self.receipt(self.call("update", "status", binary=self.test_binary))
                self.assertEqual(before["phase"], phase)
                self.assertTrue(before["operation_id"])
                recovery = self.root / "run/update-recovery" / before["operation_id"]
                self.assertEqual(before["recovery_cleanup"], "retained")
                if phase not in ("preflight", "drained"):
                    self.assertTrue((recovery / "snapshot.json").is_file())
                committed = phase == "committed"
                expectation = "updated" if committed else "rolled_back"
                if os.name == "nt":
                    outcome = self.resume_interrupted_windows(before["operation_id"], expectation, self.test_binary)
                else:
                    resumed = self.call("update", "resume", binary=self.test_binary)
                    outcome = self.receipt(resumed, (0, 22))
                self.assertEqual(outcome["operation_id"], before["operation_id"])
                self.assertEqual(outcome["recovery_cleanup"], "complete")
                self.assertFalse(recovery.exists())
                self.assertEqual(outcome["status"], expectation)
                self.assertEqual(sha256(self.canonical), sha256(next_asset if committed else self.test_binary))
                self.active_health(instance_id, next_info if committed else self.info)

    @unittest.skipIf(os.name == "nt", "symbolic link substitution requires test-user permission")
    def test_u06_completed_snapshot_cleanup_preserves_unowned_paths_and_retries(self):
        instance_id = self.legacy_adopted()
        next_info, next_asset = self.add_next_release()
        crash = self.call("update", extra_env={"BMEM_UPDATE_TEST_CRASH_AT": "published"})
        self.assertNotEqual(crash.returncode, 0)
        status = self.receipt(self.call("update", "status"))
        self.assertEqual((status["phase"], status["recovery_cleanup"]), ("published", "pending"))
        recovery = self.root / "run/update-recovery"
        current = recovery / status["operation_id"]
        same_instance = recovery / "cf1e62b0-1a04-4fe3-b65f-269b9157b942"
        unrelated = recovery / "bf57fdbe-5b39-478d-8aa2-5dcf4376641e"
        shutil.copytree(current, same_instance)
        shutil.copytree(current, unrelated)
        foreign = unrelated / "snapshot.json"
        details = json.loads(foreign.read_text(encoding="utf-8"))
        details["instance_id"] = "another-instance"
        foreign.write_text(json.dumps(details), encoding="utf-8")
        parked = recovery / "parked"
        current.rename(parked)
        current.symlink_to(unrelated, target_is_directory=True)
        blocked = self.receipt(self.call("update", "resume"))
        self.assertEqual(blocked["recovery_cleanup"], "pending")
        self.assertEqual(blocked["next_command"], "bmem update resume")
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "up_to_date")
        self.assertEqual(self.receipt(self.call("update"), (20,))["code"], "update_cleanup_pending")
        self.assertTrue(foreign.is_file())
        self.assertEqual(sha256(self.canonical), sha256(next_asset))
        self.active_health(instance_id, next_info)

        current.unlink()
        parked.rename(current)
        repaired = self.receipt(self.call("update", "resume"))
        self.assertEqual(repaired["operation_id"], status["operation_id"])
        self.assertEqual(repaired["recovery_cleanup"], "complete")
        self.assertFalse(current.exists())
        self.assertTrue((same_instance / "snapshot.json").is_file())
        self.assertTrue(foreign.is_file())
        self.assertEqual(self.receipt(self.call("update", "resume")), repaired)

    def test_u07_no_legacy_database_restore_after_opening_writes(self):
        instance_id = self.legacy_adopted()
        owner = self.active_health(instance_id, self.info)
        withdrawn, _, _ = self.seed_page(owner, "after-cutover")
        self.receipt(self.call("declare", "以后报告先写结论", "--idempotency-key", "after-cutover"))
        self.operate(owner, {"kind": "forget", "target": {"source_id": withdrawn}}, "after-cutover")
        installation = sha256(self.canonical)
        result = self.call("update", "rollback")
        self.assertIn(result.returncode, (21, 23), (result.stdout, result.stderr))
        self.assertEqual(sha256(self.canonical), installation)
        owner = self.active_health(instance_id, self.info)
        self.assertTrue(owner.request("/v2/statements")[1]["statements"])
        self.assertEqual(self.post(owner, "/v2/search", {"query": "复验解析"}, "after-rollback")["results"], [])

    def test_u07_compatible_program_rollback_preserves_later_writes_and_highest_version(self):
        instance_id = self.legacy_adopted()
        next_info, next_asset = self.add_next_release()
        self.completed(self.call("update"), "updated")
        owner = self.active_health(instance_id, next_info)
        withdrawn, _, _ = self.seed_page(owner, "latest")
        self.operate(owner, {"kind": "forget", "target": {"source_id": withdrawn}}, "latest")
        response = self.receipt(self.call("declare", "以后报告先写结论", "--idempotency-key", "latest"))
        new_revision = response["dimensions"][0]["revision_id"]
        self.completed(self.call("update", "rollback"), "rolled_back")
        self.assertEqual(sha256(self.canonical), sha256(self.test_binary))
        owner = self.active_health(instance_id, self.info)
        self.assertIn(new_revision, {row["revision_id"] for row in owner.request("/v2/statements")[1]["statements"]})
        self.assertEqual(self.post(owner, "/v2/search", {"query": "复验解析"}, "after-rollback")["results"], [])
        registry = json.loads((self.root / "run/bmem-install.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["highest_version"], next_info["version"])
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "update_available")
        self.completed(self.call("update"), "updated")
        self.assertEqual(sha256(self.canonical), sha256(next_asset))

    @unittest.skipUnless(os.name == "nt", "requires Windows EXE handles and native updater")
    def test_u08_native_stdio_holds_executable_until_it_closes(self):
        self.assert_windows_native_images(self.legacy, self.test_binary)
        instance_id = self.legacy_installation()
        self.receipt(self.call("service", "start"))
        declaration = self.receipt(self.call("declare", "以后报告先写结论", "--idempotency-key", "u08-old"))
        revision = declaration["dimensions"][0]["revision_id"]
        vault = native.VaultFixture(self.launcher, self.work)
        vault.root = self.root
        vault.env = self.env
        client, _ = vault.register("codex")
        with self.mcp(client) as (process, server, send):
            self.assertEqual(server["version"], "0.2.0")
            self.assertTrue(send("tools/list", 2)["tools"])
            receipt = self.wait_for_windows_update(self.install(adopt=True), "rolled_back")
            self.assertEqual(receipt["error_code"], "executable_in_use", receipt)
            self.assertIsNone(process.poll())
            self.assertEqual(sha256(self.canonical), sha256(self.legacy))
            self.assertEqual(self.status()["state"], "running")
            self.assertEqual(self.status()["instance_id"], instance_id)
            statements = self.owner().request("/v2/statements")[1]["statements"]
            self.assertIn(revision, {row["revision_id"] for row in statements})
        self.wait_for_windows_update(self.install(adopt=True), "installed")
        self.assertEqual(sha256(self.canonical), sha256(self.test_binary))
        self.active_health(instance_id, self.info)
        with self.mcp(client) as (_, server, send):
            self.assertEqual(server["version"], self.info["version"])
            self.assertTrue(send("tools/list", 2)["tools"])

    @unittest.skipUnless(os.name == "nt", "requires Windows EXE handles and native updater")
    def test_u08_managed_update_sharing_violation_and_interrupted_swap_recovery(self):
        # Keep a real Win32 read handle that omits FILE_SHARE_DELETE. Managed
        # MCP sessions drain themselves, so they cannot offer this guarantee.
        import ctypes
        from ctypes import wintypes

        instance_id = self.legacy_installation()
        self.receipt(self.call("service", "start"))
        self.wait_for_windows_update(self.install(adopt=True), "installed")
        next_info, next_asset = self.add_next_release()
        self.assert_windows_native_images(self.legacy, self.test_binary, next_asset)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        open_file.restype = wintypes.HANDLE
        close_file = kernel.CloseHandle
        close_file.argtypes = (wintypes.HANDLE,)
        close_file.restype = wintypes.BOOL
        generic_read, share_read, share_write, open_existing = 0x80000000, 1, 2, 3
        handle = open_file(str(self.canonical), generic_read, share_read | share_write, None,
                           open_existing, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            receipt = self.wait_for_windows_update(self.call("update"), "rolled_back")
            self.assertEqual(receipt["error_code"], "executable_in_use", receipt)
            self.assertEqual(sha256(self.canonical), sha256(self.test_binary))
            self.assertEqual(self.status()["state"], "running")
            self.active_health(instance_id, self.info)
        finally:
            self.assertTrue(close_file(handle))

        crashed = self.call("update", extra_env={"BMEM_UPDATE_TEST_CRASH_AT": "swapped"})
        handoff = self.receipt(crashed, (10,))
        self.wait_for_windows_update(crashed, "in_progress", phase="swapped")
        self.assertEqual(sha256(self.canonical), sha256(next_asset))
        barrier = self.root / "run" / "update-barrier.json"
        self.assertTrue(barrier.is_file())
        self.resume_interrupted_windows(handoff["operation_id"], "rolled_back", self.canonical, expect_handoff=True)
        self.assertEqual(sha256(self.canonical), sha256(self.test_binary))
        self.assertFalse(barrier.exists())
        self.assertEqual(self.status()["state"], "running")
        self.active_health(instance_id, self.info)

        self.wait_for_windows_update(self.call("update"), "updated")
        self.active_health(instance_id, next_info)

    def assert_windows_native_images(self, *images):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        architecture = kernel.IsWow64Process2
        architecture.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT))
        architecture.restype = wintypes.BOOL
        process, machine = wintypes.USHORT(), wintypes.USHORT()
        if not architecture(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(machine)):
            raise ctypes.WinError(ctypes.get_last_error())
        target = {0x8664: "x86_64-pc-windows-msvc", 0xAA64: "aarch64-pc-windows-msvc"}
        self.assertEqual(self.info["target"], target.get(machine.value))
        for image in images:
            with image.open("rb") as binary:
                self.assertEqual(binary.read(2), b"MZ", image.name)
                binary.seek(0x3C)
                binary.seek(int.from_bytes(binary.read(4), "little"))
                self.assertEqual(binary.read(4), b"PE\0\0", image.name)
                self.assertEqual(int.from_bytes(binary.read(2), "little"), machine.value, image.name)

    def wait_for_windows_update(self, started, expected, phase=None):
        handoff = self.receipt(started, (10,))
        self.assertEqual(handoff["status"], "handed_off")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            receipt = self.receipt(self.call("update", "status", binary=self.test_binary, timeout=10), (0, 22))
            self.assertEqual(handoff["operation_id"], receipt["operation_id"])
            matched = receipt["status"] == expected and (phase is None or receipt["phase"] == phase)
            if matched and receipt.get("recovery_cleanup") != "pending":
                return receipt
            if not matched and receipt["phase"] in ("published", "rolled_back"):
                break
            time.sleep(0.1)
        self.fail(f"Windows helper did not reach {expected}/{phase}: {receipt}")

    def resume_interrupted_windows(self, operation, expected, binary, *, expect_handoff=False):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            resumed = self.call("update", "resume", binary=binary, timeout=30)
            if resumed.returncode == 20:
                self.assertEqual(self.receipt(resumed, (20,))["code"], "update_in_progress")
                time.sleep(0.1)
                continue
            if expect_handoff:
                self.assertEqual(resumed.returncode, 10, (resumed.stdout, resumed.stderr))
            receipt = (self.wait_for_windows_update(resumed, expected) if resumed.returncode == 10
                       else self.receipt(resumed, (0, 22)))
            self.assertEqual(receipt["operation_id"], operation)
            self.assertEqual(receipt["status"], expected)
            return receipt
        self.fail(f"Windows operation still holds its update lock: {operation}")

    def test_u09_incomplete_and_corrupt_draft_cannot_move_signed_channel(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "release"))
        try:
            from candidate import assemble_catalogue
        finally:
            sys.path.pop(0)
        self.legacy_adopted()
        if not self.next_binary:
            self.skipTest("set BMEM_UPDATE_NEXT_BIN to a separately compiled newer test version")
        info = self.receipt(self.call("build-info", binary=self.next_binary))
        target = info["target"]
        version = info["version"]
        draft = self.work / "draft"
        draft.mkdir()
        record = draft / f"{target}.record.json"
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/release/candidate.py"), "record", "--binary", str(self.next_binary),
             "--target", target, "--min-os", "0.0", "--from-version", self.info["version"],
             "--from-epoch", str(self.info["authority_epoch"]), "--output", str(record)],
            check=True, stdin=subprocess.DEVNULL, env=self.env, cwd=self.work, timeout=20,
        )
        official_asset = draft / json.loads(record.read_text(encoding="utf-8"))["asset"]
        timestamp = self.repo.path / "metadata" / "timestamp.json"
        stable_before = timestamp.read_bytes()
        with self.assertRaisesRegex(ValueError, "digest"):
            assemble_catalogue(draft, [target], version, info["commit"])
        official_asset.write_bytes(b"not the tested executable")
        with self.assertRaisesRegex(ValueError, "digest"):
            assemble_catalogue(draft, [target], version, info["commit"])
        self.assertEqual(timestamp.read_bytes(), stable_before)
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "up_to_date")

        shutil.copy2(self.next_binary, official_asset)
        asset = self.repo.asset(official_asset, info)
        verified = assemble_catalogue(draft, [target], version, info["commit"])["releases"][0]
        self.assertEqual(sha256(asset), verified["sha256"])
        self.repo.publish([{**verified, "url": asset.as_uri()}], version=2)
        self.assertNotEqual(timestamp.read_bytes(), stable_before)
        self.assertEqual(self.receipt(self.call("update", "--check"))["status"], "update_available")
        self.completed(self.call("update"), "updated")
        self.assertEqual(sha256(self.canonical), verified["sha256"])


class FrozenReleaseRegression(unittest.TestCase):
    def test_u10_verify_recorded_original_fixture_run(self):
        raw = os.environ.get("BMEM_UPDATE_REGRESSION_JSON")
        binary = os.environ.get("BMEM_UPDATE_EVALUATION_BIN")
        if not raw or not binary:
            self.skipTest("set BMEM_UPDATE_REGRESSION_JSON and BMEM_UPDATE_EVALUATION_BIN after rerun")
        test_root = REPO_ROOT / "research/memory-upgrade-technical/tests"
        baseline = json.loads((test_root / "baseline-2026-11-27.json").read_text(encoding="utf-8"))
        for name, expected in baseline["suite_sha256"].items():
            self.assertEqual(sha256(test_root / (name + ".json")), expected, name)
        actual = json.loads(Path(raw).read_text(encoding="utf-8"))
        self.assertEqual(actual["binary_sha256"], sha256(binary))
        self.assertEqual(len(actual["complete_observations"]), 134)
        self.assertEqual(set(actual["execution"].values()), {"PASS"})
        with tempfile.TemporaryDirectory(prefix="bmem-007-rescore-") as work:
            observations = Path(work) / "observations.json"
            observations.write_text(json.dumps(actual["complete_observations"], ensure_ascii=False), encoding="utf-8")
            score = subprocess.run(
                [sys.executable, str(test_root / "score.py"), "--observations", str(observations)],
                check=True, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=20,
            )
            self.assertIn("PASS=134 FAIL=0 NOT_RUN=0", score.stdout)


if __name__ == "__main__":
    unittest.main()
