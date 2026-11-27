"""Release read-back and actual signed offline catalogue verification."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from update_fixtures import Repository

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts/release"))
from candidate import RELEASE_URL, TARGETS, assemble_catalogue, read_approved_root, write_json
from verify_release import verify_channel, verify_github, verify_stream


VERSION = "0.3.0"
COMMIT = "1" * 40
TAG_OBJECT = "2" * 40
BODY = b"synthetic artifact bytes for release transport checks"


def catalogue():
    releases = []
    for target in TARGETS:
        suffix = ".exe" if target.endswith("windows-msvc") else ""
        releases.append({
            "target": target, "len": len(BODY), "sha256": sha256(BODY).hexdigest(),
            "url": f"{RELEASE_URL}v{VERSION}/bmem-{VERSION}-{target}{suffix}",
        })
    return {"schema_version": 1, "channel": "stable", "releases": releases}


class GitHubReadback(unittest.TestCase):
    def setUp(self):
        self.expected = catalogue()
        self.release = {
            "id": 81, "tag_name": f"v{VERSION}", "immutable": True, "draft": False,
            "prerelease": False, "published_at": "2026-11-27T01:00:00Z",
            "assets": [
                {"name": target["url"].rsplit("/", 1)[1], "state": "uploaded", "size": target["len"],
                 "browser_download_url": target["url"], "digest": "sha256:" + target["sha256"]}
                for target in self.expected["releases"]
            ],
        }
        self.commit = COMMIT
        self.body = BODY
        self.downloads = []

    def get(self, suffix):
        return {
            f"/releases/tags/v{VERSION}": self.release,
            f"/git/ref/tags/v{VERSION}": {"object": {"type": "tag", "sha": TAG_OBJECT}},
            f"/git/tags/{TAG_OBJECT}": {"object": {"type": "commit", "sha": self.commit}},
        }[suffix]

    def read(self, descriptor):
        self.downloads.append(descriptor["target"])
        verify_stream(BytesIO(self.body), descriptor)

    def verify(self):
        return verify_github(self.expected, VERSION, COMMIT, self.get, self.read)

    def test_reads_entire_bodies_for_all_six_targets(self):
        receipt = self.verify()
        self.assertEqual(receipt["release_id"], self.release["id"])
        self.assertEqual(self.downloads, list(TARGETS))

    def test_requires_immutable_release_and_exact_tag_commit(self):
        self.release["immutable"] = False
        with self.assertRaisesRegex(ValueError, "stable and immutable"):
            self.verify()
        self.release["immutable"] = True
        self.commit = "3" * 40
        with self.assertRaisesRegex(ValueError, "verified commit"):
            self.verify()
        self.assertEqual(self.downloads, [])

    def test_requires_exact_asset_entries_and_body_content(self):
        asset = self.release["assets"].pop()
        with self.assertRaisesRegex(ValueError, "asset metadata does not match"):
            self.verify()
        self.release["assets"].append(asset)
        for mutated in (BODY[:-1], BODY + b"!", b"x" + BODY[1:]):
            self.body = mutated
            with self.subTest(mutated=mutated), self.assertRaisesRegex(ValueError, "asset body"):
                self.verify()


class ProductionCatalogue(unittest.TestCase):
    def test_accepts_platform_receipts_before_the_public_u09_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root_hash = "a" * 64
            expected = catalogue()
            for descriptor in expected["releases"]:
                target = descriptor["target"]
                name = descriptor["url"].rsplit("/", 1)[1]
                (directory / name).write_bytes(BODY)
                signature = {"darwin": "developer-id-notarized", "gnu": "tuf-target",
                             "msvc": "authenticode-timestamp"}[target.rsplit("-", 1)[1]]
                write_json(directory / f"{target}.record.json", {
                    "asset": name, "descriptor": {**descriptor, "version": VERSION, "commit": COMMIT},
                    "native_signature": signature, "test_build": False, "official_update_root_sha256": root_hash,
                })
                receipt = {
                    "asset_sha256": descriptor["sha256"],
                    "scenarios": {f"U{i:02d}": "PASS" for i in [*range(1, 9), 10]},
                    "regression": {"pass": 134, "fail": 0, "not_run": 0},
                }
                if not name.endswith(".exe"):
                    receipt["scenarios"]["U08"] = "NOT_APPLICABLE"
                write_json(directory / f"{target}.acceptance.json", receipt)

            assembled = assemble_catalogue(directory, TARGETS, VERSION, COMMIT, root_hash)
            self.assertEqual([item["target"] for item in assembled["releases"]], list(TARGETS))
            missing_target = TARGETS[-1]
            (directory / f"bmem-{VERSION}-{missing_target}.exe").unlink()
            with self.assertRaisesRegex(ValueError, "asset does not match"):
                assemble_catalogue(directory, TARGETS, VERSION, COMMIT, root_hash)


class SignedStaging(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bmem-release-stage-test-")
        self.addCleanup(temporary.cleanup)
        self.stage = Path(temporary.name).resolve() / "signed stage"
        openssl = os.environ.get("BMEM_UPDATE_TEST_OPENSSL") or shutil.which("openssl")
        self.repo = Repository(self.stage, openssl)
        self.addCleanup(self.repo.close)
        self.catalogue = catalogue()
        self.root = self.stage / "root.json"
        self.repo.publish(self.catalogue["releases"])

    def verify(self):
        return verify_channel(read_approved_root(self.root), self.catalogue, self.stage)

    def test_validates_actual_local_signatures_and_six_target_catalogue(self):
        # Production uses versioned snapshot/targets metadata and hash-prefixed target filenames.
        signed_root = json.loads(self.root.read_bytes())["signed"]
        signed_root["consistent_snapshot"] = True
        self.root.write_bytes(self.repo.sign(signed_root))
        for role in ("targets", "snapshot"):
            source = self.stage / "metadata" / (role + ".json")
            source.rename(source.with_name("1." + source.name))
        target = self.stage / "targets/release-catalog.json"
        target_hash = sha256(target.read_bytes()).hexdigest()
        target.rename(target.with_name(target_hash + "." + target.name))
        receipt = self.verify()
        self.assertEqual(receipt["metadata_versions"],
                         {"root": 1, "targets": 1, "snapshot": 1, "timestamp": 1})
        self.assertEqual(receipt["catalogue_sha256"], target_hash)

    def test_rejects_tampered_catalogue_and_wrong_signed_catalogue(self):
        target = self.stage / "targets/release-catalog.json"
        target.write_bytes(target.read_bytes().replace(b'"aarch64-apple', b'"mismatched-apple'))
        with self.assertRaisesRegex(ValueError, "TUF channel verification failed"):
            self.verify()
        unexpected = deepcopy(self.catalogue["releases"])
        unexpected[0]["sha256"] = "a" * 64
        self.repo.publish(unexpected, version=2)
        with self.assertRaisesRegex(ValueError, "signed catalogue differs"):
            self.verify()

        unexpected = deepcopy(self.catalogue["releases"])
        unexpected[0]["len"] = float(unexpected[0]["len"])
        self.repo.publish(unexpected, version=3)
        with self.assertRaisesRegex(ValueError, "signed catalogue differs"):
            self.verify()

    def test_rejects_bad_signatures_and_expiry(self):
        for condition in ({"expired": True}, {"bad_signature": True}):
            self.repo.publish(self.catalogue["releases"], version=2, **condition)
            with self.subTest(condition=condition), self.assertRaisesRegex(ValueError, "TUF channel verification failed"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
