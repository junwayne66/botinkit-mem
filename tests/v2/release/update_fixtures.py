"""Temporary fixtures for the native updater. No product or release keys are used."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone


LEGACY_SHA256 = "d0e36329b8de767013e0dd3a58c220df67e1d634aa04d9efb6689509226f02b1"


def sha256(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def file_details(data):
    return {"length": len(data), "hashes": {"sha256": hashlib.sha256(data).hexdigest()}}


def metadata_details(data, version):
    return {"version": version, **file_details(data)}


class Repository:
    def __init__(self, path, openssl):
        self.path = path.resolve()
        self.openssl = openssl
        (self.path / "metadata").mkdir(parents=True)
        (self.path / "targets").mkdir()
        self.key_home = tempfile.TemporaryDirectory(prefix="bmem-update-test-key-")
        self.private_key = Path(self.key_home.name) / "private.pem"
        self.command("genpkey", "-algorithm", "ED25519", "-out", str(self.private_key))
        public = self.command("pkey", "-in", str(self.private_key), "-pubout", "-outform", "DER")
        assert public.startswith(bytes.fromhex("302a300506032b6570032100")) and len(public) == 44
        self.key = {"keytype": "ed25519", "scheme": "ed25519", "keyval": {"public": public[-32:].hex()}}
        self.keyid = hashlib.sha256(canonical(self.key)).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        root = self.roles("root", 1, expires, consistent_snapshot=False, keys={self.keyid: self.key},
                          roles={name: {"keyids": [self.keyid], "threshold": 1}
                                 for name in ("root", "targets", "snapshot", "timestamp")})
        (self.path / "root.json").write_bytes(self.sign(root))

    def close(self):
        self.key_home.cleanup()

    def command(self, *args):
        return subprocess.run([self.openssl, *args], stdin=subprocess.DEVNULL,
                              capture_output=True, check=True, timeout=15).stdout

    @staticmethod
    def roles(role, version, expires, **fields):
        return {"_type": role, "spec_version": "1.0.31", "version": version,
                "expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}

    def sign(self, signed, corrupt=False):
        message = Path(self.key_home.name) / "data.json"
        message.write_bytes(canonical(signed))
        signature = self.command("pkeyutl", "-sign", "-rawin", "-inkey", str(self.private_key),
                                 "-in", str(message))
        if corrupt:
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        return canonical({"signed": signed, "signatures": [{"keyid": self.keyid, "sig": signature.hex()}]})

    def asset(self, binary, info):
        executable = Path(binary)
        extension = ".exe" if executable.suffix.lower() == ".exe" else ""
        asset = self.path / "targets" / f"bmem-{info['version']}-{info['target']}{extension}"
        shutil.copy2(executable, asset)
        return asset

    def release(self, asset, info, versions, epochs, **override):
        return {"version": info["version"], "build_id": info["build_id"], "commit": info["commit"],
                "sha256": sha256(asset), "len": asset.stat().st_size, "url": asset.as_uri(),
                "target": info["target"], "min_os": "0.0",
                "authority_epoch": info.get("authority_epoch", 2), "settings_epoch": info.get("settings_epoch", 0),
                "from_versions": versions, "from_authority_epochs": epochs, "min_updater": 1, **override}

    def publish(self, releases, version=1, expired=False, bad_signature=False):
        expires = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 14)
        catalogue = canonical({"schema_version": 1, "channel": "stable", "releases": releases})
        targets = self.sign(self.roles("targets", version, expires,
                                       targets={"release-catalog.json": file_details(catalogue)}))
        snapshot = self.sign(self.roles("snapshot", version, expires,
                                        meta={"targets.json": metadata_details(targets, version)}))
        timestamp = self.sign(self.roles("timestamp", version, expires,
                                         meta={"snapshot.json": metadata_details(snapshot, version)}),
                              corrupt=bad_signature)
        (self.path / "targets" / "release-catalog.json").write_bytes(catalogue)
        (self.path / "metadata" / "targets.json").write_bytes(targets)
        (self.path / "metadata" / "snapshot.json").write_bytes(snapshot)
        (self.path / "metadata" / "timestamp.json").write_bytes(timestamp)


def isolated_environment(work):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("BMEM_", "ANTHROPIC_", "OPENAI_"))}
    env.update(
        HOME=str(work / "account"),
        USERPROFILE=str(work / "account"),
        XDG_CONFIG_HOME=str(work / "account" / "config"),
        XDG_DATA_HOME=str(work / "account" / "app"),
        XDG_STATE_HOME=str(work / "account" / "state"),
        APPDATA=str(work / "account" / "app"),
        LOCALAPPDATA=str(work / "account" / "local"),
        BMEM_LOCATOR_DIR=str(work / "locator"),
        BMEM_DEFAULT_HOME=str(work / "unused-default"),
    )
    Path(env["HOME"]).mkdir(parents=True)
    return env
