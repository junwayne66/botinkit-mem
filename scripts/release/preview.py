"""Package six verified public-preview build artifacts for GitHub Release."""

import argparse
import json
from pathlib import Path
import re
import tarfile
import zipfile

from candidate import TARGETS, assemble_catalogue, digest


ROOT = Path(__file__).resolve().parents[2]
QUICK_START = "https://github.com/junwayne66/botinkit-mem/blob/{tag}/docs/quick-start.md"


def pack(options):
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)-preview\.[1-9]\d*", options.tag)
    if match is None:
        raise ValueError("use a tag like v0.3.0-preview.1")
    catalogue = assemble_catalogue(options.assets, TARGETS, match[1], options.commit)
    for item in catalogue["releases"]:
        target = item["target"]
        report = json.loads((options.assets / f"{target}.record.json").read_text(encoding="utf-8"))
        if (report["native_signature"] != "pending" or report["test_build"] is not False
                or report["official_update_root_sha256"] is not None):
            raise ValueError(f"{target} was not built for the public preview")
    options.output.mkdir(parents=True, exist_ok=False)
    readme = options.output / "README.txt"
    readme.write_text(
        f"Botinkit Memory {options.tag}\n\n"
        "Public preview. Manual installation and updates.\n"
        "The macOS and Windows binaries do not carry a formal platform signature.\n\n"
        f"Quick Start: {QUICK_START.format(tag=options.tag)}\n",
        encoding="utf-8",
    )
    checksums = {}
    for item in catalogue["releases"]:
        target = item["target"]
        windows = target.endswith("windows-msvc")
        name = "bmem.exe" if windows else "bmem"
        source = options.assets / f"bmem-{match[1]}-{target}{'.exe' if windows else ''}"
        suffix = "zip" if windows else "tar.gz"
        asset = options.output / f"bmem-{options.tag}-{target}.{suffix}"
        members = ((source, name), (ROOT / "LICENSE", "LICENSE"), (readme, "README.txt"))
        if windows:
            with zipfile.ZipFile(asset, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, member in members:
                    archive.write(path, member)
        else:
            with tarfile.open(asset, "x:gz") as archive:
                for path, member in members:
                    entry = archive.gettarinfo(str(path), member)
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mode = 0o755 if member == name else 0o644
                    with path.open("rb") as stream:
                        archive.addfile(entry, stream)
        checksums[asset.name] = digest(asset)
    (options.output / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(checksums.items())),
        encoding="ascii",
    )
    readme.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True, help="flat directory of six binaries and build records")
    parser.add_argument("--output", type=Path, required=True, help="new directory for Release attachments")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    pack(parser.parse_args())
