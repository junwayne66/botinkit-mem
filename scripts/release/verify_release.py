"""Read back the immutable GitHub release and verify the matching TUF channel."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, url2pathname

from candidate import TARGETS, assemble_catalogue, digest, read_approved_root, write_json


API = "https://api.github.com/repos/junwayne66/botinkit-mem"
CHANNEL = "https://junwayne66.github.io/botinkit-mem/update"
CATALOGUE = "release-catalog.json"
TUF_VERSION = "7.0.1"
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def allowed_https(url, hosts):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port is None
            and parsed.username is None and parsed.password is None and not parsed.fragment)


class AssetRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_https(newurl, ASSET_HOSTS - {"github.com"}):
            raise ValueError("GitHub asset redirected outside the release download hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_json(suffix):
    request = Request(API + suffix, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "botinkit-mem-release-verifier"})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("GitHub release response exceeded the metadata budget")
    return json.loads(data)


def resolve_tag(tag, get_json):
    ref = get_json(f"/git/ref/tags/{tag}")["object"]
    for _ in range(8):
        sha = ref["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("GitHub tag object has an invalid SHA")
        if ref["type"] == "commit":
            return sha
        if ref["type"] != "tag":
            break
        ref = get_json(f"/git/tags/{sha}")["object"]
    raise ValueError("GitHub release tag does not resolve to a commit")


def verify_asset_body(descriptor):
    url = descriptor["url"]
    if not allowed_https(url, {"github.com"}):
        raise ValueError("GitHub release asset URL is invalid")
    request = Request(url, headers={"User-Agent": "botinkit-mem-release-verifier"})
    with build_opener(AssetRedirect()).open(request, timeout=30) as response:
        verify_stream(response, descriptor)


def verify_stream(stream, descriptor):
    value = hashlib.sha256()
    remaining = descriptor["len"]
    for part in iter(lambda: stream.read(min(1024 * 1024, remaining + 1)), b""):
        remaining -= len(part)
        if remaining < 0:
            raise ValueError("published asset body exceeded the expected length")
        value.update(part)
    if remaining or value.hexdigest() != descriptor["sha256"]:
        raise ValueError("published asset body SHA-256 or length differs from verified local bytes")


def verify_github(catalogue, version, commit, get_json=github_json, read_asset=verify_asset_body):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("use the stable version and full Git commit from the candidate records")
    tag = f"v{version}"
    release = get_json(f"/releases/tags/{tag}")
    if (release["tag_name"] != tag or release["draft"] is not False
            or release["prerelease"] is not False or release.get("immutable") is not True
            or not release["published_at"]):
        raise ValueError("GitHub release must be published, stable and immutable")
    if resolve_tag(tag, get_json) != commit:
        raise ValueError("immutable GitHub release tag does not identify the verified commit")
    assets = {asset["name"]: asset for asset in release["assets"]}
    if len(assets) != len(release["assets"]):
        raise ValueError("duplicate GitHub release asset name")
    verified = []
    for descriptor in catalogue["releases"]:
        name = descriptor["url"].rsplit("/", 1)[1]
        asset = assets.get(name)
        if (not asset or asset["state"] != "uploaded" or asset["size"] != descriptor["len"]
                or asset["browser_download_url"] != descriptor["url"]):
            raise ValueError(f"published GitHub asset metadata does not match {name}")
        api_digest = asset.get("digest")
        if api_digest is not None and api_digest != "sha256:" + descriptor["sha256"]:
            raise ValueError(f"published GitHub asset metadata digest does not match {name}")
        read_asset(descriptor)
        verified.append({"target": descriptor["target"], "asset": name, "sha256": descriptor["sha256"],
                         "len": descriptor["len"]})
    return {"tag": tag, "commit": commit, "release_id": release["id"], "assets": verified}


def verify_channel(bootstrap, catalogue, stage=None):
    try:
        import tuf
        from tuf.api.exceptions import DownloadError, DownloadHTTPError, RepositoryError
        from tuf.ngclient import FetcherInterface, Updater, UpdaterConfig
    except ImportError as error:
        raise ValueError(f"install tuf=={TUF_VERSION} with its cryptography dependency in a release venv") from error
    if tuf.__version__ != TUF_VERSION:
        raise ValueError(f"the release verifier uses tuf=={TUF_VERSION}")

    stage = stage.resolve(strict=True) if stage else None
    base = stage.as_uri() if stage else CHANNEL

    class ChannelFetcher(FetcherInterface):
        def _fetch(self, url):
            parts = urlsplit(url)
            try:
                if stage:
                    source = Path(url2pathname(parts.path)).resolve()
                    if parts.scheme != "file" or parts.netloc or not source.is_relative_to(stage):
                        raise ValueError("TUF requested a file outside the signed staging tree")
                    stream = source.open("rb")
                else:
                    if (not allowed_https(url, {"junwayne66.github.io"}) or parts.query
                            or not url.startswith(CHANNEL + "/")):
                        raise ValueError("TUF requested content outside the official update channel")
                    stream = build_opener(NoRedirect()).open(url, timeout=20)
                with stream:
                    yield from iter(lambda: stream.read(65536), b"")
            except FileNotFoundError as error:
                raise DownloadHTTPError("TUF metadata file is missing", 404) from error
            except HTTPError as error:
                raise DownloadHTTPError("TUF channel HTTP response rejected", error.code) from error
            except URLError as error:
                raise DownloadError("TUF channel could not be reached") from error

    with tempfile.TemporaryDirectory(prefix="bmem-release-readback-") as temporary:
        cache = Path(temporary)
        target = cache / CATALOGUE
        budget = 1024 * 1024
        config = UpdaterConfig(root_max_length=budget, timestamp_max_length=budget,
                               snapshot_max_length=budget, targets_max_length=budget)
        try:
            updater = Updater(str(cache), base + "/metadata/", target_base_url=base + "/targets/",
                              fetcher=ChannelFetcher(), config=config, bootstrap=bootstrap)
            updater.refresh()
            info = updater.get_targetinfo(CATALOGUE)
            if info is None or info.length > budget:
                raise ValueError("TUF catalogue target is missing or exceeds the size budget")
            updater.download_target(info, str(target))
        except (RepositoryError, DownloadError) as error:
            raise ValueError(f"TUF channel verification failed ({type(error).__name__})") from error
        received = json.loads(target.read_bytes(), object_pairs_hook=unique_object)
        if json.dumps(received, sort_keys=True) != json.dumps(catalogue, sort_keys=True):
            raise ValueError("TUF signed catalogue differs from the six verified release assets")
        versions = {role: json.loads((cache / (role + ".json")).read_bytes())["signed"]["version"]
                    for role in ("root", "targets", "snapshot", "timestamp")}
        return {"catalogue_sha256": digest(target), "metadata_versions": versions,
                "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
                "verifier": "python-tuf " + TUF_VERSION}


def unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("TUF signed catalogue contains duplicate fields")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True, help="exact approved TUF root used to build the assets")
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--stage", type=Path, help="locally signed Pages tree with metadata/ and targets/")
    parser.add_argument("--offline", action="store_true", help="verify a local stage before publishing Release assets")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.offline and not options.stage:
        parser.error("--offline requires --stage")
    try:
        if options.output.exists():
            raise ValueError("use a new output path for this verification receipt")
        bootstrap = read_approved_root(options.root)
        fingerprint = hashlib.sha256(bootstrap).hexdigest()
        catalogue = assemble_catalogue(options.assets, TARGETS, options.version, options.commit, fingerprint)
        github = None if options.offline else verify_github(catalogue, options.version, options.commit)
        tuf = verify_channel(bootstrap, catalogue, options.stage)
        write_json(options.output, {"github": github, "tuf": tuf, "root_sha256": fingerprint,
                                    "channel": "staging" if options.stage else "public-stable",
                                    "commit": options.commit, "version": options.version})
    except (KeyError, TypeError, ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
