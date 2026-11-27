"""Create and verify private, local TUF channels for bmem 0.3.0 testing."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from candidate import TARGETS, digest, identity, read_approved_root, write_json
from verify_release import CATALOGUE, TUF_VERSION, verify_channel, verify_stream


ROLES = ("root", "targets", "snapshot", "timestamp")
MIN_OS = ("15.0", "15.0", "2.35", "2.35", "10.0.26100", "10.0.20348")
VERSIONS = ("0.3.0", "0.3.1")
MAX_BINARY = 256 * 1024 * 1024
DESCRIPTOR_KEYS = ("version", "build_id", "commit", "target", "authority_epoch", "settings_epoch")


def asset_name(version, target):
    extension = ".exe" if target.endswith("windows-msvc") else ""
    return f"bmem-{version}-{target}{extension}"


def signing_paths(keys, public):
    if os.name != "posix":
        raise ValueError("prepare the keys on a POSIX signing host with owner-only permissions")
    keys = keys.resolve()
    workspace = Path(__file__).resolve().parents[2]
    if keys.is_relative_to(workspace):
        raise ValueError("keep the signing key directory outside the source worktree")
    for location in public:
        location = location.resolve()
        if location.is_relative_to(keys) or keys.is_relative_to(location):
            raise ValueError("keep the signing key directory separate from channels, roots and assets")
    return keys


def require_signing_dependencies():
    import tuf

    if tuf.__version__ != TUF_VERSION:
        raise ValueError(f"the internal signer uses tuf=={TUF_VERSION}")


def initialize(keys, root_path):
    require_signing_dependencies()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, Role, Root

    keys = signing_paths(keys, (root_path,))
    if os.path.lexists(root_path):
        raise ValueError("the public root output must be a new file")
    keys.mkdir(mode=0o700)
    signers = {}
    for role in ROLES:
        key = Ed25519PrivateKey.generate()
        descriptor = os.open(keys / f"{role}.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        signers[role] = CryptoSigner(key)

    expires = datetime.now(timezone.utc) + timedelta(days=365)
    trusted = Metadata(Root(expires=expires, consistent_snapshot=True,
                            keys={item.public_key.keyid: item.public_key for item in signers.values()},
                            roles={role: Role([item.public_key.keyid], 1) for role, item in signers.items()}))
    trusted.sign(signers["root"])
    data = trusted.to_bytes()
    with root_path.open("xb") as handle:
        handle.write(data)
    return {"scope": "internal", "root_sha256": hashlib.sha256(data).hexdigest(),
            "role_count": len(ROLES), "root_expires": trusted.signed.expires.isoformat()}


def record(binary, root_path, target, output, min_os=None):
    min_os = MIN_OS[TARGETS.index(target)] if min_os is None else min_os
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", min_os):
        raise ValueError("use a numeric OS or ABI version")
    root_sha = hashlib.sha256(read_approved_root(root_path)).hexdigest()
    info = identity(binary)
    if (info["target"] != target or info["version"] not in VERSIONS or info["test_build"] is not True
            or info.get("internal_update_root_sha256") != root_sha
            or info.get("official_update_root_sha256") is not None
            or not re.fullmatch(r"[0-9a-f]{40}", info["commit"])
            or not re.fullmatch(r"[0-9a-f]{64}", info["build_id"])):
        raise ValueError("binary identity does not match the expected internal target and root")
    descriptor = {key: info[key] for key in DESCRIPTOR_KEYS}
    descriptor.update(sha256=digest(binary), len=binary.stat().st_size, min_os=min_os, min_updater=1)
    report = {"scope": "internal", "root_sha256": root_sha, "descriptor": descriptor}
    write_json(output, report)
    return report


def check_asset(parent, descriptor):
    name = asset_name(descriptor["version"], descriptor["target"])
    path = parent / name
    if (path.is_symlink() or not path.is_file()
            or type(descriptor["len"]) is not int or not 0 < descriptor["len"] <= MAX_BINARY):
        raise ValueError(f"{name} is missing or exceeds the binary size budget")
    with path.open("rb") as stream:
        verify_stream(stream, descriptor)
    return name


def read_verified_channel(root_path, channel, requested_targets=None):
    root = read_approved_root(root_path)
    for name in ("assets", "metadata", "targets"):
        path = channel / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"the channel {name} path must be a real directory")
    channel_root = channel / "root.json"
    if (channel_root.is_symlink() or not channel_root.is_file() or channel_root.stat().st_size != len(root)
            or channel_root.read_bytes() != root):
        raise ValueError("channel root bytes differ from the independently supplied public root")
    locations = list((channel / "targets").glob(f"*.{CATALOGUE}"))
    if len(locations) != 1 or locations[0].is_symlink() or locations[0].stat().st_size > 1024 * 1024:
        raise ValueError("the channel must contain exactly one local catalogue within the size budget")
    catalogue = json.loads(locations[0].read_bytes())
    signature_proof = verify_channel(root, catalogue, channel)
    if (catalogue["schema_version"] != 1 or catalogue["channel"] != "internal"
            or locations[0].name != signature_proof["catalogue_sha256"] + f".{CATALOGUE}"):
        raise ValueError("the authenticated catalogue is not for this internal channel")

    targets = set(TARGETS if requested_targets is None else requested_targets)
    by_version = {}
    checked_assets = []
    for item in catalogue["releases"]:
        version, target = item["version"], item["target"]
        if version not in VERSIONS or target not in TARGETS:
            raise ValueError("unsupported version or target in the internal catalogue")
        enrolled = by_version.setdefault(version, set())
        if target in enrolled:
            raise ValueError("duplicate version and target in the internal catalogue")
        enrolled.add(target)
        name = asset_name(version, target)
        if item["url"] != f"assets/{name}":
            raise ValueError(f"{name} has an unexpected file location")
        try:
            check_asset(channel / "assets", item)
        except ValueError as error:
            raise ValueError(f"{name} failed the authenticated length or SHA-256 check") from error
        checked_assets.append({key: item[key] for key in (*DESCRIPTOR_KEYS, "sha256", "len")})
    if ("0.3.0" not in by_version or not targets.issubset(by_version["0.3.0"])
            or any(enrolled != by_version["0.3.0"] for enrolled in by_version.values())):
        raise ValueError("the authenticated channel is missing one or more requested targets")
    if {item.name for item in (channel / "assets").iterdir()} != {
            asset_name(item["version"], item["target"]) for item in checked_assets}:
        raise ValueError("the channel contains an asset outside its authenticated catalogue")
    return catalogue, {"scope": "internal", "tuf": signature_proof, "assets": checked_assets}


def sign_generation(staging, keys, root, releases, generation):
    require_signing_dependencies()
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, MetaFile, Snapshot, TargetFile, Targets, Timestamp

    keys_stat = keys.stat()
    if keys_stat.st_uid != os.geteuid() or stat.S_IMODE(keys_stat.st_mode) & 0o077:
        raise ValueError("signing keys require an owner-only directory")
    trusted = Metadata.from_bytes(root)
    trusted.verify_delegate("root", trusted)
    now = datetime.now(timezone.utc)
    catalogue = json.dumps({"schema_version": 1, "channel": "internal", "releases": releases},
                           separators=(",", ":"), sort_keys=True).encode("utf-8")
    catalogue_entry = TargetFile.from_data(CATALOGUE, catalogue, ["sha256"])
    (staging / "targets" / (catalogue_entry.hashes["sha256"] + f".{CATALOGUE}")).write_bytes(catalogue)
    (staging / "root.json").write_bytes(root)
    metadata = staging / "metadata"
    (metadata / f"{trusted.signed.version}.root.json").write_bytes(root)

    def signed_bytes(role, payload):
        private = keys / f"{role}.pem"
        file_info = private.lstat()
        if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid()
                or stat.S_IMODE(file_info.st_mode) & 0o077):
            raise ValueError(f"the {role} signing key must be an owner-only regular file")
        current = Metadata(payload)
        current.sign(CryptoSigner(load_pem_private_key(private.read_bytes(), password=None)))
        trusted.verify_delegate(role, current)
        return current.to_bytes()

    expiry = now + timedelta(days=30)
    targets = signed_bytes("targets", Targets(version=generation, expires=expiry,
                                               targets={CATALOGUE: catalogue_entry}))
    snapshot = signed_bytes("snapshot", Snapshot(version=generation, expires=expiry,
                            meta={"targets.json": MetaFile.from_data(generation, targets, ["sha256"])}))
    timestamp = signed_bytes("timestamp", Timestamp(version=generation, expires=now + timedelta(days=7),
                             snapshot_meta=MetaFile.from_data(generation, snapshot, ["sha256"])))
    (metadata / f"{generation}.targets.json").write_bytes(targets)
    (metadata / f"{generation}.snapshot.json").write_bytes(snapshot)
    (metadata / "timestamp.json").write_bytes(timestamp)


def publish(keys, root_path, channel, assets, version, commit, requested_targets=None, previous=None, generation=None):
    keys = signing_paths(keys, (root_path, channel, assets, *([previous] if previous else [])))
    if os.path.lexists(channel):
        raise ValueError("publish to a new channel directory")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or version not in VERSIONS:
        raise ValueError("use the candidate version and its full recorded Git commit")
    root = read_approved_root(root_path)
    root_sha = hashlib.sha256(root).hexdigest()
    targets = tuple(TARGETS if requested_targets is None else requested_targets)
    if len(set(targets)) != len(targets) or not targets or not set(targets).issubset(TARGETS):
        raise ValueError("choose a distinct supported target set")
    expected = {assets / f"{target}.record.json" for target in targets}
    if set(assets.glob("*.record.json")) != expected:
        raise ValueError("the build record set does not match the requested target set")

    old_releases = []
    minimum_generation = 1
    if version == "0.3.1":
        if previous is None:
            raise ValueError("the 0.3.1 acceptance stage needs the original 0.3.0 channel")
        prior, prior_receipt = read_verified_channel(root_path, previous, targets)
        old_releases = prior["releases"]
        if ({(item["version"], item["target"]) for item in old_releases}
                != {("0.3.0", target) for target in targets}):
            raise ValueError("the previous channel must contain exactly these 0.3.0 targets")
        minimum_generation = max(prior_receipt["tuf"]["metadata_versions"].values()) + 1
    elif previous is not None:
        raise ValueError("the first 0.3.0 channel has no predecessor")
    if generation is None:
        generation = minimum_generation
    if generation < minimum_generation:
        raise ValueError("channel metadata generation must increase")

    releases = []
    for target in targets:
        report = json.loads((assets / f"{target}.record.json").read_bytes())
        item = report["descriptor"]
        if (report["scope"] != "internal" or report["root_sha256"] != root_sha
                or (item["target"], item["version"], item["commit"]) != (target, version, commit)):
            raise ValueError(f"{target} recording does not match this internal root, version and commit")
        if previous:
            before = next(item for item in old_releases if item["target"] == target)
            compatible_versions, compatible_epochs = [before["version"]], [before["authority_epoch"]]
        else:
            compatible_versions, compatible_epochs = ["0.0.0", "0.2.0"], [0, 1]
        name = check_asset(assets, item)
        releases.append({**item, "url": f"assets/{name}", "from_versions": compatible_versions,
                         "from_authority_epochs": compatible_epochs})

    with tempfile.TemporaryDirectory(prefix=".bmem-tuf-", dir=channel.parent) as work:
        staging = Path(work) / "channel"
        for name in ("assets", "metadata", "targets"):
            (staging / name).mkdir(parents=True)
        sources = [(assets, releases)]
        if previous is not None:
            sources.append((previous / "assets", old_releases))
        for directory, entries in sources:
            for item in entries:
                name = asset_name(item["version"], item["target"])
                shutil.copy2(directory / name, staging / "assets" / name)
        sign_generation(staging, keys, root, old_releases + releases, generation)
        _, receipt = read_verified_channel(root_path, staging, targets)
        if os.path.lexists(channel):
            raise ValueError("the channel destination already exists")
        staging.rename(channel)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="create a private signing key directory and its public root")
    rec = commands.add_parser("record", help="execute the candidate's build-info and record its exact bytes")
    pub = commands.add_parser("publish", help="sign and assemble a new local channel directory")
    verify = commands.add_parser("verify", help="verify a local channel without using the signing keys")
    for command in (init, rec, pub, verify):
        command.add_argument("--root", required=True, type=Path)
    for command in (init, pub):
        command.add_argument("--keys", required=True, type=Path)
    for command in (pub, verify):
        command.add_argument("--channel", required=True, type=Path)
        command.add_argument("--target", choices=TARGETS, action="append", help="explicit subset for local acceptance")
    rec.add_argument("--binary", required=True, type=Path)
    rec.add_argument("--target", required=True, choices=TARGETS)
    rec.add_argument("--min-os", help="record the OS or ABI floor established for this internal build")
    rec.add_argument("--output", required=True, type=Path)
    pub.add_argument("--assets", required=True, type=Path)
    pub.add_argument("--version", required=True, choices=VERSIONS)
    pub.add_argument("--commit", required=True)
    pub.add_argument("--previous", type=Path)
    pub.add_argument("--generation", type=int)
    verify.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize(args.keys, args.root)
        elif args.action == "record":
            result = record(args.binary, args.root, args.target, args.output, args.min_os)
        elif args.action == "publish":
            result = publish(args.keys, args.root, args.channel, args.assets, args.version, args.commit,
                             args.target, args.previous, args.generation)
        else:
            _, result = read_verified_channel(args.root, args.channel, args.target)
            if args.output:
                if os.path.lexists(args.output):
                    raise ValueError("write the verification receipt to a new path")
                write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ImportError, KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
