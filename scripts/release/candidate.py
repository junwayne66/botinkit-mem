"""Record and validate bmem release assets before signing the stable TUF target."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


TARGETS = (
    "aarch64-apple-darwin", "x86_64-apple-darwin",
    "aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu",
    "aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc",
)
PLATFORM_SCENARIOS = {f"U{i:02d}" for i in range(1, 9)} | {"U10"}
RELEASE_URL = "https://github.com/junwayne66/botinkit-mem/releases/download/"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def identity(binary):
    command = subprocess.run([str(binary.resolve()), "--json", "build-info"], stdin=subprocess.DEVNULL,
                             capture_output=True, check=True, text=True, encoding="utf-8", timeout=15)
    return json.loads(command.stdout)


def native_signature(binary, target, signer, signtool):
    if target.endswith("apple-darwin"):
        if not re.fullmatch(r"[A-Z0-9]{10}", signer):
            raise ValueError("use the approved ten-character Apple Developer Team ID")
        requirement = ("anchor apple generic and "
                       "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and "
                       f'certificate leaf[subject.OU] = "{signer}"')
        subprocess.run(["codesign", "--verify", "--all-architectures", "--strict",
                        "--check-notarization", "-R=" + requirement, str(binary)], check=True)
        return "developer-id-notarized"
    if target.endswith("windows-msvc"):
        subprocess.run([signtool, "verify", "/pa", "/all", "/tw", str(binary)], check=True)
        # Parse the publisher certificate directly. Value is the configured SHA256 certificate thumbprint.
        script = (
            "$signature = Get-AuthenticodeSignature -LiteralPath $env:BMEM_RELEASE_FILE; "
            "if ($signature.Status -ne 'Valid' -or !$signature.TimeStamperCertificate) { exit 1 }; "
            "$signature.SignerCertificate.GetCertHashString([System.Security.Cryptography.HashAlgorithmName]::SHA256)"
        )
        result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                                check=True, capture_output=True, text=True,
                                env={**os.environ, "BMEM_RELEASE_FILE": str(binary.resolve())})
        if result.stdout.strip().upper() != signer.upper():
            raise ValueError("Windows Authenticode certificate does not match the expected signer")
        return "authenticode-timestamp"
    if signer != "tuf-target":
        raise ValueError("Linux release records use --signer tuf-target")
    return "tuf-target"


def cli_record(options):
    binary = options.binary
    signer = "pending"
    if options.signer:
        if not options.target:
            raise ValueError("pin --target before verifying and running a signed candidate")
        signer = native_signature(binary, options.target, options.signer, options.signtool)
    info = identity(binary)
    target, version, commit = (info[key] for key in ("target", "version", "commit"))
    if target not in TARGETS or (options.target and target != options.target):
        raise ValueError("the candidate did not run with the expected native target")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("candidate lacks a stable version and a full Git commit")
    extension = ".exe" if target.endswith("windows-msvc") else ""
    asset_name = f"bmem-{version}-{target}{extension}"
    if options.signer and info["test_build"] is not False:
        raise ValueError("a test build cannot be accepted as a production asset")
    descriptor = {
        **{key: info[key] for key in ("version", "build_id", "commit", "target", "authority_epoch", "settings_epoch")},
        "sha256": digest(binary), "len": binary.stat().st_size,
        "url": RELEASE_URL + f"v{version}/{asset_name}",
        "min_os": options.min_os, "min_updater": 1,
        "from_versions": options.from_version, "from_authority_epochs": options.from_epoch,
    }
    write_json(options.output, {
        "descriptor": descriptor, "asset": asset_name, "web_build_id": info["web_build_id"],
        "native_signature": signer, "test_build": info["test_build"],
        "official_update_root_sha256": info.get("official_update_root_sha256"),
    })


def assemble_catalogue(release_dir, targets, version, commit, approved_root=None):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("use the stable version and full Git commit from the candidate records")
    files = set(release_dir.glob("*.record.json"))
    expected = {release_dir / f"{target}.record.json" for target in targets}
    if files != expected:
        raise ValueError("the set of build records is incomplete or contains unexpected targets")
    releases = []
    for target in targets:
        report = json.loads((release_dir / f"{target}.record.json").read_text(encoding="utf-8"))
        descriptor = report["descriptor"]
        if (descriptor["target"], descriptor["version"], descriptor["commit"]) != (target, version, commit):
            raise ValueError(f"{target} record does not match the requested commit and version")
        extension = ".exe" if target.endswith("windows-msvc") else ""
        name = f"bmem-{version}-{target}{extension}"
        path = release_dir / name
        if report["asset"] != name or descriptor["url"] != RELEASE_URL + f"v{version}/{name}":
            raise ValueError(f"{target} asset name and immutable URL disagree")
        if path.is_symlink() or not path.is_file() or path.stat().st_size != descriptor["len"] or digest(path) != descriptor["sha256"]:
            raise ValueError(f"{target} asset does not match its verified length and digest")
        if approved_root is not None:
            gate = json.loads((release_dir / f"{target}.acceptance.json").read_text(encoding="utf-8"))
            signature = {
                "apple-darwin": "developer-id-notarized",
                "windows-msvc": "authenticode-timestamp",
                "linux-gnu": "tuf-target",
            }["-".join(target.split("-")[-2:])]
            scenarios = {name: ("NOT_APPLICABLE" if name == "U08" and not extension else "PASS")
                         for name in PLATFORM_SCENARIOS}
            fingerprint = report.get("official_update_root_sha256")
            if fingerprint != approved_root:
                raise ValueError(f"{target} was not built with the approved update root")
            if (report["native_signature"] != signature or report["test_build"] is not False or
                    gate["asset_sha256"] != descriptor["sha256"] or
                    gate["scenarios"] != scenarios or
                    gate["regression"] != {"pass": 134, "fail": 0, "not_run": 0}):
                raise ValueError(f"{target} lacks bound production signature and acceptance evidence")
        releases.append(descriptor)
    return {"schema_version": 1, "channel": "stable", "releases": releases}


def read_approved_root(path):
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("approved TUF root exceeds the update metadata budget")
    root = json.loads(data)
    if root["signed"]["_type"] != "root" or not root["signatures"]:
        raise ValueError("approved root must be signed TUF root metadata")
    return data


def cli_catalogue(options):
    fingerprint = hashlib.sha256(read_approved_root(options.root)).hexdigest()
    catalogue = assemble_catalogue(options.assets, TARGETS, options.version, options.commit,
                                   fingerprint)
    # The catalogue is only an input to the operator's TUF signing step.
    write_json(options.output, catalogue)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    record = commands.add_parser("record")
    record.add_argument("--binary", required=True, type=Path)
    record.add_argument("--target", choices=TARGETS)
    record.add_argument("--min-os", required=True)
    record.add_argument("--from-version", action="append", required=True)
    record.add_argument("--from-epoch", action="append", required=True, type=int)
    record.add_argument("--output", required=True, type=Path)
    record.add_argument("--signer", help="expected Developer Team ID or Authenticode SHA256 thumbprint")
    record.add_argument("--signtool", default="signtool.exe")
    record.set_defaults(run=cli_record)
    catalogue = commands.add_parser("catalogue")
    catalogue.add_argument("--assets", required=True, type=Path)
    catalogue.add_argument("--version", required=True)
    catalogue.add_argument("--commit", required=True)
    catalogue.add_argument("--root", required=True, type=Path, help="exact approved TUF root bytes used at build time")
    catalogue.add_argument("--output", required=True, type=Path)
    catalogue.set_defaults(run=cli_catalogue)
    arguments = parser.parse_args()
    try:
        arguments.run(arguments)
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
