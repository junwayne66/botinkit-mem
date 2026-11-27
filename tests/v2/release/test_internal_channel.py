"""Sign local TUF test data and verify that actual byte changes are rejected."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "release"))
from internal_channel import TARGETS, asset_name, initialize, publish, read_verified_channel, write_json


@unittest.skipUnless(os.name == "posix", "internal signing host uses POSIX key permissions")
class InternalChannelTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="bmem-tuf-channel-test-")
        self.addCleanup(self.workspace.cleanup)
        self.home = Path(self.workspace.name)
        self.keys = self.home / "keys"
        self.root = self.home / "root.json"
        self.commit = "1" * 40
        self.base = self.home / "base"
        self.fingerprint = initialize(self.keys, self.root)["root_sha256"]

    def fixture_assets(self, version, targets):
        folder = self.home / f"fixtures-{version}"
        folder.mkdir()
        for target in targets:
            binary = folder / asset_name(version, target)
            sample = f"Synthetic internal packaging input {version} {target}\n".encode()
            binary.write_bytes(sample)
            checksum = hashlib.sha256(sample).hexdigest()
            write_json(folder / f"{target}.record.json", {
                "scope": "internal", "root_sha256": self.fingerprint,
                "descriptor": {"target": target, "version": version, "commit": self.commit, "build_id": checksum,
                               "authority_epoch": 2, "settings_epoch": 0, "sha256": checksum, "len": len(sample),
                               "min_os": "0.0", "min_updater": 1},
            })
        return folder

    def initial(self, targets=None):
        assets = self.fixture_assets("0.3.0", TARGETS if targets is None else targets)
        return publish(self.keys, self.root, self.base, assets, "0.3.0", self.commit, targets)

    def test_roles_are_separate_and_the_six_target_channel_can_move(self):
        signed = json.loads(self.root.read_bytes())["signed"]
        self.assertEqual(len({role["keyids"][0] for role in signed["roles"].values()}), 4)
        self.assertEqual(stat.S_IMODE(self.keys.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(key.stat().st_mode) == 0o600 for key in self.keys.glob("*.pem")))
        result = self.initial()
        moved = self.base.with_name("moved private channel")
        shutil.move(self.base, moved)
        _, receipt = read_verified_channel(self.root, moved)
        self.assertEqual(receipt, result)
        self.assertEqual({item["target"] for item in receipt["assets"]}, set(TARGETS))
        self.assertEqual(list(moved.rglob("*.pem")), [])

    def test_explicit_platform_subset_can_advance_to_successor(self):
        targets = [TARGETS[0]]
        initial = self.initial(targets)
        with self.assertRaisesRegex(ValueError, "missing one or more requested targets"):
            read_verified_channel(self.root, self.base)
        next_folder = self.home / "later"
        incoming = self.fixture_assets("0.3.1", targets)
        publication = publish(self.keys, self.root, next_folder, incoming, "0.3.1", self.commit,
                              targets, self.base)
        self.assertEqual(initial["tuf"]["metadata_versions"]["timestamp"], 1)
        self.assertEqual(publication["tuf"]["metadata_versions"]["timestamp"], 2)
        catalogue, _ = read_verified_channel(self.root, next_folder, targets)
        initial_descriptor, successor = catalogue["releases"]
        self.assertEqual(initial_descriptor["sha256"], initial["assets"][0]["sha256"])
        self.assertEqual(successor["from_versions"], ["0.3.0"])
        self.assertEqual(successor["from_authority_epochs"], [2])

    def test_each_asset_byte_is_authenticated(self):
        self.initial()
        asset = self.base / "assets" / asset_name("0.3.0", TARGETS[-1])
        body = asset.read_bytes()
        asset.write_bytes(b"X" + body[1:])
        with self.assertRaisesRegex(ValueError, "failed the authenticated length or SHA-256 check"):
            read_verified_channel(self.root, self.base)

    def test_signed_metadata_is_independently_checked(self):
        self.initial()
        stamp = self.base / "metadata" / "timestamp.json"
        data = json.loads(stamp.read_bytes())
        sig = data["signatures"][0]["sig"]
        data["signatures"][0]["sig"] = ("0" if sig[0] != "0" else "1") + sig[1:]
        write_json(stamp, data)
        with self.assertRaisesRegex(ValueError, "TUF channel verification failed"):
            read_verified_channel(self.root, self.base)


if __name__ == "__main__":
    unittest.main()
