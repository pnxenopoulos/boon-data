"""Package a pinned GameTracking-Deadlock snapshot and its JSON definition catalogs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
from catalogs import LOCALIZATION_FILES, VDATA_FILES, build_catalogs
from keyvalues import to_json

SOURCE_REPO = "SteamTracking/GameTracking-Deadlock"
SOURCE_PATH = "game/citadel/pak01_dir/scripts"
API_URL = f"https://api.github.com/repos/{SOURCE_REPO}"
RAW_URL = f"https://raw.githubusercontent.com/{SOURCE_REPO}"
REQUIRED_FILES = set(VDATA_FILES)


def fetch(url: str) -> bytes:
    """Fetch public source files; use an optional token only for the GitHub API."""
    headers = {"User-Agent": "boon-data", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=60
    ) as response:
        return response.read()


def resolve_source(ref: str) -> dict:
    """Resolve a branch, tag, or commit once, then read version data at that SHA."""
    commit = json.loads(fetch(f"{API_URL}/commits/{urllib.parse.quote(ref, safe='')}"))
    sha = commit["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("upstream did not return a full commit SHA")
    info = {}
    for line in (
        fetch(f"{RAW_URL}/{sha}/game/citadel/steam.inf").decode("utf-8").splitlines()
    ):
        if "=" in line:
            key, value = line.split("=", 1)
            info[key.strip()] = value.strip()
    client_version = info.get("ClientVersion", "")
    if not re.fullmatch(r"[0-9]+", client_version):
        raise ValueError("steam.inf is missing a numeric ClientVersion")
    return {
        "source_key": f"{client_version}-{sha[:12]}",
        "source": {
            "repository": SOURCE_REPO,
            "commit": sha,
            "committed_at": commit["commit"]["committer"]["date"],
            "path": SOURCE_PATH,
        },
        "client_version": client_version,
        "server_version": info.get("ServerVersion"),
        "source_revision": info.get("SourceRevision"),
        "version_date": info.get("VersionDate"),
        "version_time": info.get("VersionTime"),
    }


def fingerprint(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def generator_fingerprint() -> str:
    """Revision of the code and writer used to build artifacts, independent of checkout."""
    root = Path(__file__).resolve().parents[1]
    files = (
        "scripts/pipeline.py",
        "scripts/catalogs.py",
        "scripts/keyvalues.py",
        "pyproject.toml",
        "uv.lock",
    )
    digests = {
        name: fingerprint((root / name).read_text(encoding="utf-8").encode())["sha256"]
        for name in files
    }
    # Include the actual writer too, in case a local environment bypasses the lockfile.
    return fingerprint(
        to_json({"files": digests, "polars_version": pl.__version__}).encode()
    )["sha256"]


def snapshot_metadata(source: dict, contents: dict, localization: dict) -> dict:
    files = {name: fingerprint(contents[name]) for name in sorted(REQUIRED_FILES)}
    localization_files = {
        name: fingerprint(localization[name]) for name in sorted(LOCALIZATION_FILES)
    }
    content_hash = fingerprint(
        to_json({"files": files, "localization_files": localization_files}).encode()
    )["sha256"]
    identity = {
        "content_sha256": content_hash,
        "generator_sha256": generator_fingerprint(),
    }
    dataset_hash = fingerprint(to_json(identity).encode())["sha256"]
    return {
        **source,
        "release_key": source["client_version"],
        "files": files,
        "localization_files": localization_files,
        "snapshot": {**identity, "dataset_sha256": dataset_hash},
    }


def download_inputs(source: dict) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Download and verify the catalog inputs at one pinned upstream commit."""
    sha = source["source"]["commit"]
    entries = json.loads(fetch(f"{API_URL}/contents/{SOURCE_PATH}?ref={sha}"))
    if not isinstance(entries, list) or len(entries) >= 1000:
        raise ValueError("could not obtain a complete VData directory listing")
    entries = [entry for entry in entries if entry["name"].endswith(".vdata")]
    entries = [entry for entry in entries if entry["name"] in REQUIRED_FILES]
    names = [entry["name"] for entry in entries]
    if len(set(names)) != len(names) or not REQUIRED_FILES.issubset(names):
        raise ValueError(
            "VData listing contains duplicate names or is missing required files"
        )
    for entry in entries:
        if entry["type"] != "file" or not re.fullmatch(
            r"[A-Za-z0-9_]+\.vdata", entry["name"]
        ):
            raise ValueError(f"unexpected VData entry: {entry['name']}")

    def download(path: str, entry: dict | None = None) -> bytes:
        if entry is None:
            entry = json.loads(fetch(f"{API_URL}/contents/{path}?ref={sha}"))
        if entry["type"] != "file":
            raise ValueError(f"expected a source file: {path}")
        data = fetch(f"{RAW_URL}/{sha}/{path}")
        # Compare with the Git blob listed at this commit, not a moving branch.
        blob = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        if len(data) != entry["size"] or hashlib.sha1(blob).hexdigest() != entry["sha"]:
            raise ValueError(f"source integrity check failed for {path}")
        return data

    def download_vdata(entry: dict) -> tuple[str, bytes]:
        name = entry["name"]
        return name, download(f"{SOURCE_PATH}/{name}", entry)

    def download_localization(path: str) -> tuple[str, bytes]:
        return path, download(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        contents = dict(
            pool.map(download_vdata, sorted(entries, key=lambda e: e["name"]))
        )
        localization = dict(pool.map(download_localization, sorted(LOCALIZATION_FILES)))
    return contents, localization


def build(
    source: dict, output: Path, *, inputs: tuple[dict, dict] | None = None
) -> Path:
    """Build deterministic artifacts and preserve any already-built snapshot."""
    contents, localization = download_inputs(source) if inputs is None else inputs
    contents = {name: contents[name] for name in sorted(REQUIRED_FILES)}
    localization = {name: localization[name] for name in sorted(LOCALIZATION_FILES)}
    source = snapshot_metadata(source, contents, localization)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / source["release_key"]
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        catalogs = build_catalogs(contents, localization, source, stage)
        artifacts = {
            path.name: fingerprint(path.read_bytes())
            for path in sorted(stage.iterdir())
        }
        manifest = {
            **source,
            "catalogs": catalogs,
            "artifacts": artifacts,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if destination.exists():
            for name in ("manifest.json", *artifacts):
                if (destination / name).read_bytes() != (stage / name).read_bytes():
                    raise ValueError(
                        f"refusing to overwrite different artifacts for {destination.name}"
                    )
        else:
            stage.rename(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref", default="master", help="Upstream branch, tag, or full commit SHA."
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".work/dist"), help="Bundle output root."
    )
    parser.add_argument(
        "--resolve", action="store_true", help="Print source metadata without building."
    )
    args = parser.parse_args()
    try:
        source = resolve_source(args.ref)
        if args.resolve:
            print(json.dumps(source, indent=2))
        else:
            print(build(source, args.output))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
