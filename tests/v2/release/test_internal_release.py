"""Run Spec Kit 007 against verified assets from two offline internal generations."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import time
import unittest

import test_update
from update_fixtures import isolated_environment, sha256


RELEASE_TOOLS = Path(__file__).resolve().parents[3] / "scripts/release"
sys.path.insert(0, str(RELEASE_TOOLS))
from candidate import TARGETS, write_json
from internal_channel import read_verified_channel


def selected(catalogue, target, version):
    releases = [entry for entry in catalogue["releases"]
                if (entry["target"], entry["version"]) == (target, version)]
    if len(releases) != 1:
        raise ValueError(f"expected one signed {version} asset for {target}")
    return releases[0]


class InternalReleaseAcceptance(unittest.TestCase):
    options = None
    results = []

    def setUp(self):
        if self.options is None:
            self.skipTest("run this file with independently verified internal bundle inputs")
        self.fixture = fixture = test_update.UpdateAcceptance("runTest")
        temporary = tempfile.TemporaryDirectory(prefix="bmem-007-internal-", dir=Path.home())
        self.addCleanup(temporary.cleanup)
        fixture.work = Path(temporary.name).resolve()
        fixture.root = fixture.work / "instance data"
        fixture.env = isolated_environment(fixture.work)
        fixture.env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        fixture.env["XDG_RUNTIME_DIR"] = str(fixture.work / "runtime")
        (fixture.work / "runtime").mkdir(mode=0o700)
        fixture.legacy = self.options.legacy
        fixture.suffix = ".exe" if os.name == "nt" else ""
        fixture.canonical = fixture.work / "account/installed" / ("bmem" + fixture.suffix)
        fixture.canonical.parent.mkdir()
        fixture.launcher = fixture.canonical
        if os.name != "nt":
            fixture.launcher = fixture.work / "account/shell bin/bmem"
            fixture.launcher.parent.mkdir()
        fixture.env["PATH"] = str(fixture.launcher.parent) + os.pathsep + fixture.env["PATH"]
        self.channel = fixture.work / "channel"
        fixture.env["BMEM_INTERNAL_TUF_REPOSITORY"] = str(self.channel)
        fixture.initial_release = self.options.initial_release
        fixture.test_binary = fixture.initial_asset = self.stage(
            self.options.repository, fixture.initial_release,
        )
        fixture.installer = fixture.work / ("bmem-installer" + fixture.suffix)
        shutil.copy2(fixture.test_binary, fixture.installer)
        fixture.info = self.check_build(fixture.installer, fixture.initial_release)
        if os.name == "nt":
            fixture.assert_windows_native_images(fixture.legacy, fixture.installer)
        fixture.add_next_release = self.activate_successor
        self.addCleanup(fixture.stop)
        self.started = time.monotonic()

    def stage(self, source, release):
        self.channel.mkdir(exist_ok=True)
        shutil.copyfile(source / "root.json", self.channel / "root.json")
        for folder in ("metadata", "targets"):
            shutil.copytree(source / folder, self.channel / folder, dirs_exist_ok=True)
        asset = self.channel / release["url"]
        asset.parent.mkdir(exist_ok=True)
        shutil.copy2(source / release["url"], asset)
        self.assertEqual(sha256(asset), release["sha256"])
        return asset

    def check_build(self, binary, release):
        fixture = self.fixture
        info = fixture.receipt(fixture.call("build-info", binary=binary))
        for key in ("target", "version", "build_id", "commit", "authority_epoch", "settings_epoch"):
            self.assertEqual(info[key], release[key], key)
        self.assertTrue(info["test_build"])
        self.assertEqual(info["internal_update_root_sha256"], self.options.root_sha256)
        self.assertIsNone(info["official_update_root_sha256"])
        return info

    def activate_successor(self):
        fixture = self.fixture
        descriptor = self.options.successor_release
        asset = self.stage(self.options.successor_repository, descriptor)
        info = self.check_build(asset, descriptor)
        if os.name == "nt":
            fixture.assert_windows_native_images(asset)
        return info, asset

    def passed(self, **observed):
        fixture = self.fixture
        actual = fixture.receipt(fixture.call("build-info"))
        self.results.append({
            "case": self._testMethodName,
            "seconds": round(time.monotonic() - self.started, 3),
            "installed_version": actual["version"],
            "installed_build": actual["build_id"],
            "installed_sha256": sha256(fixture.canonical),
            **observed,
        })

    def test_u01_bootstrap(self):
        self.fixture.test_u01_signed_fresh_install_check_and_own_instance()
        published_root = self.channel / "root.json"
        original = published_root.read_bytes()
        try:
            published_root.write_text("untrusted root beside the signed repository", encoding="utf-8")
            check = self.fixture.receipt(self.fixture.call("update", "--check"))
            self.assertEqual(check["status"], "up_to_date")
        finally:
            published_root.write_bytes(original)
        self.passed(embedded_root_unchanged=True)

    def test_u02_adopt_0_2_0(self):
        self.fixture.test_u02_adoption_reconnects_hosts_and_preserves_authority()
        self.passed()

    def test_u03_update_and_repeat(self):
        self.fixture.test_u03_real_continuous_update_and_repeated_noop_while_stopped()
        replay = self.fixture.receipt(self.fixture.call(
            "update", extra_env={"BMEM_INTERNAL_TUF_REPOSITORY": str(self.options.repository)},
        ), (21,))
        self.assertEqual(replay["code"], "update_metadata_rollback")
        self.assertEqual(sha256(self.fixture.canonical), self.options.successor_release["sha256"])
        self.passed(stale_generation=replay["code"])

    def reject_metadata(self, file, replacement, expected):
        original = file.read_bytes()
        try:
            file.write_bytes(replacement)
            status, code = expected
            rejected = self.fixture.receipt(self.fixture.call("update"), (status,))
            self.assertEqual(rejected["code"], code)
            return rejected["code"]
        finally:
            file.write_bytes(original)

    def test_u04_u06_reject_tampering_and_restore_after_failed_probe(self):
        fixture = self.fixture
        instance = fixture.legacy_adopted()
        revision = fixture.receipt(fixture.call("declare", "以后报告先写结论",
                                              "--idempotency-key", "internal-before-failure"))["dimensions"][0]["revision_id"]
        fixture.receipt(fixture.call("config", "set", "profile", "high"))
        next_info, asset = self.activate_successor()
        installed = fixture.receipt(fixture.call("update", "status"))

        timestamp = self.channel / "metadata/timestamp.json"
        invalid = json.loads(timestamp.read_bytes())
        signature = invalid["signatures"][0]["sig"]
        invalid["signatures"][0]["sig"] = ("1" if signature[0] == "0" else "0") + signature[1:]
        bad_signature = self.reject_metadata(timestamp, json.dumps(invalid).encode("utf-8"),
                                             (21, "update_signature_invalid"))
        catalogue = self.channel / "targets" / f"{self.options.successor_catalogue_sha256}.release-catalog.json"
        catalogue_bytes = catalogue.read_bytes()
        changed = catalogue_bytes.replace(b'"schema_version":1', b'"schema_version":2', 1)
        self.assertNotEqual(changed, catalogue_bytes)
        # The TUF digest adapter wraps a hash mismatch in a transport error.
        bad_catalogue = self.reject_metadata(catalogue, changed, (20, "update_transport_failed"))

        try:
            with asset.open("r+b") as target:
                target.seek(-1, os.SEEK_END)
                byte = target.read(1)
                target.seek(-1, os.SEEK_END)
                target.write(bytes([byte[0] ^ 1]))
            bad_payload = fixture.receipt(fixture.call("update"), (21,))
            self.assertEqual(bad_payload["code"], "update_asset_digest_mismatch")
        finally:
            shutil.copy2(self.options.successor_repository / self.options.successor_release["url"], asset)
        initial_digest = fixture.initial_release["sha256"]
        self.assertEqual(sha256(fixture.canonical), initial_digest)
        self.assertEqual(fixture.receipt(fixture.call("update", "status")), installed)
        self.assertEqual(fixture.status()["state"], "running")
        fixture.active_health(instance, fixture.info)

        fixture.stop()
        self.assertEqual(fixture.status()["state"], "stopped")
        authority_epoch = fixture.rows("PRAGMA user_version")
        endpoint = fixture.root / "run/service.json"
        saved_endpoint = endpoint.with_suffix(".before-probe")
        if endpoint.exists():
            endpoint.replace(saved_endpoint)
        # This blocks only the successor's discovery publication, after swap and migration.
        endpoint.mkdir()
        try:
            result = fixture.call("update")
            rolled_back = (fixture.wait_for_windows_update(result, "rolled_back")
                           if os.name == "nt" else fixture.receipt(result, (22,)))
            self.assertEqual(rolled_back["status"], "rolled_back")
            self.assertEqual(rolled_back["error_code"], "candidate_health_failed")
            self.assertEqual(rolled_back["recovery_cleanup"], "complete")
            self.assertEqual(sha256(fixture.canonical), initial_digest)
            self.assertEqual(fixture.status()["state"], "stopped")
        finally:
            endpoint.rmdir()
            if saved_endpoint.exists():
                saved_endpoint.replace(endpoint)

        self.assertEqual(fixture.rows("PRAGMA user_version"), authority_epoch)
        for database in ("bmem.sqlite3", "settings.sqlite3"):
            self.assertEqual(fixture.rows("PRAGMA quick_check", database=database), [("ok",)])
        owner = fixture.active_health(instance, fixture.info)
        self.assertEqual(owner.request("/v2/config")[1]["profile"], "high")
        self.assertIn(revision, {row["revision_id"] for row in owner.request("/v2/statements")[1]["statements"]})
        fixture.completed(fixture.call("update"), "updated")
        fixture.active_health(instance, next_info)
        self.passed(rejections={"timestamp": bad_signature, "catalogue": bad_catalogue,
                                "binary": bad_payload["code"]},
                    rollback=rolled_back["error_code"], restored_sha256=initial_digest)

    def test_u07_compatible_program_rollback_keeps_new_writes(self):
        self.fixture.test_u07_compatible_program_rollback_preserves_later_writes_and_highest_version()
        self.passed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "repository", "successor-repository", "legacy", "receipt"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--root-sha256", required=True)
    parser.add_argument("--legacy-sha256", required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--execution-mode", choices=("native", "translated", "emulated"), required=True)
    options = parser.parse_args()
    if options.receipt.exists():
        parser.error("use a new path for the acceptance receipt")
    for name in ("root", "repository", "successor_repository", "legacy"):
        setattr(options, name, getattr(options, name).resolve(strict=True))
    for path, expected in ((options.root, options.root_sha256), (options.legacy, options.legacy_sha256)):
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(path) != expected:
            parser.error("the supplied root or reviewed 0.2.0 binary differs from its independently pinned digest")
    try:
        initial, initial_verification = read_verified_channel(options.root, options.repository, [options.target])
        successor, next_verification = read_verified_channel(options.root, options.successor_repository, [options.target])
        options.initial_release = selected(initial, options.target, "0.3.0")
        options.successor_release = selected(successor, options.target, "0.3.1")
        options.successor_catalogue_sha256 = next_verification["tuf"]["catalogue_sha256"]
        if selected(successor, options.target, "0.3.0") != options.initial_release:
            raise ValueError("the successor channel replaced the initial signed release record")
    except (OSError, ValueError) as error:
        parser.exit(1, f"internal bundle verification failed: {error}\n")
    InternalReleaseAcceptance.options = options
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InternalReleaseAcceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    observed = InternalReleaseAcceptance.results
    passed = result.wasSuccessful() and not result.skipped and len(observed) == result.testsRun == 5
    write_json(options.receipt, {
        "scope": "internal-tuf",
        "target": options.target,
        "execution_mode": options.execution_mode,
        "system": platform.system(),
        "system_release": platform.release(),
        "runtime_machine": platform.machine(),
        "status": "PASS" if passed else "INCOMPLETE",
        "root_sha256": options.root_sha256,
        "legacy_sha256": options.legacy_sha256,
        "initial_sha256": options.initial_release["sha256"],
        "successor_sha256": options.successor_release["sha256"],
        "metadata": [initial_verification, next_verification],
        "tests_run": result.testsRun,
        "failures": [test.id() for test, _ in result.failures + result.errors],
        "skipped": [test.id() for test, _ in result.skipped],
        "observations": observed,
    })
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
