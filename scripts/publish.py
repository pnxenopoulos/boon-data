"""Plan or publish complete snapshots and atomically update the version index."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pipeline
from keyvalues import to_json

DEFAULT_REPOSITORY = "pnxenopoulos/boon-data"
INDEX_BRANCH = "data-index"
INDEX_FILE = "versions.json"
RELEASE_TAG = re.compile(r"[0-9]+-[0-9a-f]{12}-r[0-9a-f]{12}")
ARTIFACTS = {
    "abilities.json",
    "heroes.json",
    "modifiers.json",
}


def empty_index() -> dict:
    return {
        "schema_version": 2,
        "source_repository": pipeline.SOURCE_REPO,
        "latest": None,
        "versions": {},
        "snapshots": {},
    }


def published_time(value: str) -> str:
    """Normalize a GitHub release timestamp; previews never invent one."""
    if not isinstance(value, str):
        raise TypeError("release is missing its publication timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("publication timestamp must include a timezone")
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_index(index: dict, *, published: bool = False) -> dict:
    if (
        index["schema_version"] != 2
        or index["source_repository"] != pipeline.SOURCE_REPO
    ):
        raise ValueError("unsupported version index")
    if not isinstance(index["versions"], dict) or not isinstance(
        index["snapshots"], dict
    ):
        raise TypeError("invalid version index mappings")
    for record in index["snapshots"].values():
        if published or record["released_at"] is not None:
            published_time(record["released_at"])
        if set(record["artifacts"]) != ARTIFACTS | {"manifest.json"}:
            raise ValueError("snapshot index has an invalid artifact set")
    for version, entry in index["versions"].items():
        if (
            not re.fullmatch(r"[0-9]+", version)
            or version != entry["client_version"]
            or entry["snapshot"] not in index["snapshots"]
        ):
            raise ValueError("version index has an invalid snapshot reference")
        record = index["snapshots"][entry["snapshot"]]
        if any(entry[key] != record[key] for key in ("released_at", "artifacts")):
            raise ValueError("version index has inconsistent release metadata")
    if index["latest"] is not None and index["latest"] not in index["versions"]:
        raise ValueError("version index has an invalid latest reference")
    return index


def snapshot_record(
    data: bytes, repository: str = DEFAULT_REPOSITORY
) -> tuple[str, dict]:
    """Validate a manifest before registering its complete asset set in the index."""
    manifest = json.loads(data)
    tag, identity = manifest["release_key"], manifest["snapshot"]
    if not RELEASE_TAG.fullmatch(tag) or manifest["schema_version"] != 2:
        raise ValueError("unsupported snapshot manifest")
    if manifest["source"]["repository"] != pipeline.SOURCE_REPO:
        raise ValueError("unexpected snapshot source repository")
    if set(manifest["artifacts"]) != ARTIFACTS:
        raise ValueError("snapshot manifest does not describe a complete release")
    inputs = {key: manifest[key] for key in ("files", "localization_files")}
    if (
        pipeline.fingerprint(to_json(inputs).encode())["sha256"]
        != identity["content_sha256"]
    ):
        raise ValueError("invalid source content fingerprint")
    revision = {
        key: identity[key]
        for key in ("content_sha256", "generator_sha256", "catalog_schema_version")
    }
    dataset = pipeline.fingerprint(to_json(revision).encode())["sha256"]
    if (
        dataset != identity["dataset_sha256"]
        or manifest["catalogs"]["schema_version"] != identity["catalog_schema_version"]
    ):
        raise ValueError("invalid dataset fingerprint")
    expected_tag = f"{manifest['client_version']}-{manifest['source']['commit'][:12]}-r{dataset[:12]}"
    if tag != expected_tag:
        raise ValueError("release tag does not match the snapshot identity")
    artifacts = {**manifest["artifacts"], "manifest.json": pipeline.fingerprint(data)}
    for item in artifacts.values():
        if (
            type(item["bytes"]) is not int
            or item["bytes"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("invalid artifact fingerprint")
    for name, item in artifacts.items():
        item["url"] = f"https://github.com/{repository}/releases/download/{tag}/{name}"
    return tag, {
        "released_at": None,
        "identity": identity,
        "client_version": manifest["client_version"],
        "source": manifest["source"],
        "artifacts": artifacts,
    }


def observe(index: dict, source: dict, tag: str, *, latest: bool) -> None:
    """Record an observed version separately from the snapshot's original provenance."""
    version = source["client_version"]
    existing = index["versions"].get(version)
    if existing is not None and datetime.fromisoformat(
        source["source"]["committed_at"]
    ) < datetime.fromisoformat(existing["source"]["committed_at"]):
        return
    entry = {
        key: source.get(key)
        for key in (
            "client_version",
            "server_version",
            "source_revision",
            "version_date",
            "version_time",
            "source",
        )
    }
    entry["snapshot"] = tag
    record = index["snapshots"][tag]
    entry.update({key: record[key] for key in ("released_at", "artifacts")})
    previous = index["latest"]
    previous_date = (
        index["versions"][previous]["source"]["committed_at"] if previous else None
    )
    index["versions"][version] = entry
    if (
        latest
        and (previous is None or int(version) >= int(previous))
        and (
            previous_date is None
            or datetime.fromisoformat(entry["source"]["committed_at"])
            >= datetime.fromisoformat(previous_date)
        )
    ):
        index["latest"] = version


def make_plan(
    source: dict,
    inputs: tuple[dict, dict],
    index: dict,
    output: Path,
    *,
    latest: bool,
    repository: str = DEFAULT_REPOSITORY,
) -> dict:
    metadata = pipeline.snapshot_metadata(source, *inputs)
    index = copy.deepcopy(index)
    dataset = metadata["snapshot"]["dataset_sha256"]
    tag = next(
        (
            tag
            for tag, record in sorted(index["snapshots"].items())
            if record["identity"]["dataset_sha256"] == dataset
        ),
        None,
    )
    directory = None
    if tag is None:
        directory = pipeline.build(source, output, inputs=inputs)
        tag, record = snapshot_record(
            (directory / "manifest.json").read_bytes(), repository
        )
        index["snapshots"][tag] = record
    observe(index, source, tag, latest=latest)
    return {
        "action": "publish" if directory else "reuse",
        "client_version": source["client_version"],
        "snapshot": tag,
        "directory": str(directory) if directory else None,
        "index": validate_index(index),
    }


class GitHub:
    def __init__(self, repository: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("expected a GitHub owner/repository")
        self.repository = repository
        self.endpoint = f"repos/{repository}"

    def api(self, path: str, *, method: str = "GET", data=None, missing_ok=False):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "boon-data",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"https://api.github.com/{self.endpoint}/{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise

    def releases(self) -> list[dict]:
        result = []
        page = 1
        while True:
            entries = self.api(f"releases?per_page=100&page={page}")
            result.extend(entries)
            if len(entries) < 100:
                return result
            page += 1

    def release(self, tag: str) -> dict | None:
        result = self.api(f"releases/tags/{tag}", missing_ok=True)
        if result is None:
            # Include drafts even when the tag endpoint only exposes published releases.
            return next(
                (item for item in self.releases() if item["tag_name"] == tag), None
            )
        return result

    def upload_asset(self, release_id: int, path: Path) -> None:
        name = urllib.parse.quote(path.name, safe="")
        subprocess.run(
            [
                "gh",
                "api",
                f"https://uploads.github.com/{self.endpoint}/releases/{release_id}/assets?name={name}",
                "--method",
                "POST",
                "--header",
                "Content-Type: application/json",
                "--input",
                str(path),
                "--silent",
            ],
            check=True,
        )

    def asset_bytes(self, asset: dict) -> bytes:
        return subprocess.run(
            [
                "gh",
                "api",
                f"{self.endpoint}/releases/assets/{asset['id']}",
                "-H",
                "Accept: application/octet-stream",
            ],
            check=True,
            capture_output=True,
        ).stdout

    def read_index(self) -> tuple[dict, str | None]:
        entry = self.api(f"contents/{INDEX_FILE}?ref={INDEX_BRANCH}", missing_ok=True)
        if entry is None:
            return empty_index(), None
        blob = (
            entry
            if entry.get("encoding") == "base64"
            else self.api(f"git/blobs/{entry['sha']}")
        )
        return validate_index(
            json.loads(base64.b64decode(blob["content"])), published=True
        ), entry["sha"]

    def write_index(self, index: dict, previous_sha: str | None, target: str) -> None:
        validate_index(index, published=True)
        if self.api(f"git/ref/heads/{INDEX_BRANCH}", missing_ok=True) is None:
            self.api(
                "git/refs",
                method="POST",
                data={"ref": f"refs/heads/{INDEX_BRANCH}", "sha": target},
            )
        payload = {
            "message": "Update Deadlock version index",
            "branch": INDEX_BRANCH,
            "content": base64.b64encode((to_json(index) + "\n").encode()).decode(),
        }
        if previous_sha is not None:
            payload["sha"] = (
                previous_sha  # GitHub rejects a stale update instead of losing observations.
            )
        self.api(f"contents/{INDEX_FILE}", method="PUT", data=payload)


def verify_release(
    github: GitHub, release: dict | None, record: dict, *, published: bool
) -> None:
    if release is None or (published and (release["draft"] or release["prerelease"])):
        raise ValueError("snapshot release is not published")
    assets = {asset["name"]: asset for asset in release["assets"]}
    if len(assets) != len(release["assets"]) or set(assets) != set(record["artifacts"]):
        raise ValueError("release does not contain the complete artifact set")
    for name, expected in record["artifacts"].items():
        asset = assets[name]
        if asset["state"] != "uploaded" or asset["size"] != expected["bytes"]:
            raise ValueError(f"incomplete release asset: {name}")
        digest = asset.get("digest")
        if digest is None:
            actual = pipeline.fingerprint(github.asset_bytes(asset))["sha256"]
        else:
            actual = digest.removeprefix("sha256:")
        if actual != expected["sha256"]:
            raise ValueError(f"release checksum mismatch: {name}")


def record_publication(index: dict, tag: str, release: dict) -> None:
    record = index["snapshots"][tag]
    record["released_at"] = published_time(release.get("published_at"))
    for entry in index["versions"].values():
        if entry["snapshot"] == tag:
            entry["released_at"] = record["released_at"]


def recover_snapshots(github: GitHub, index: dict) -> dict:
    """Recover a publication that succeeded before its index commit did."""
    index = copy.deepcopy(index)
    for release in github.releases():
        tag = release["tag_name"]
        if (
            release["draft"]
            or release["prerelease"]
            or not RELEASE_TAG.fullmatch(tag)
            or tag in index["snapshots"]
        ):
            continue
        asset = next(
            (a for a in release["assets"] if a["name"] == "manifest.json"), None
        )
        if asset is None:
            raise ValueError(f"published snapshot has no manifest: {tag}")
        data = github.asset_bytes(asset)
        manifest_tag, record = snapshot_record(data, github.repository)
        if manifest_tag != tag:
            raise ValueError("published manifest does not match its release tag")
        verify_release(github, release, record, published=True)
        index["snapshots"][tag] = record
        record_publication(index, tag, release)
        source = json.loads(data)
        existing = index["versions"].get(source["client_version"])
        if existing is None or (
            datetime.fromisoformat(source["source"]["committed_at"]),
            datetime.fromisoformat(record["released_at"]),
        ) > (
            datetime.fromisoformat(existing["source"]["committed_at"]),
            datetime.fromisoformat(existing["released_at"]),
        ):
            observe(index, source, tag, latest=False)
    return validate_index(index, published=True)


def publish_snapshot(github: GitHub, plan: dict, target: str) -> dict:
    tag = plan["snapshot"]
    record = plan["index"]["snapshots"][tag]
    release = github.release(tag)
    if plan["action"] == "reuse" or release is not None and not release["draft"]:
        verify_release(github, release, record, published=True)
        return release
    directory = Path(plan["directory"])
    for name, checksum in record["artifacts"].items():
        if pipeline.fingerprint((directory / name).read_bytes()) != {
            key: checksum[key] for key in ("sha256", "bytes")
        }:
            raise ValueError(f"local release artifact changed: {name}")
    if release is None:
        release = github.api(
            "releases",
            method="POST",
            data={
                "tag_name": tag,
                "target_commitish": target,
                "draft": True,
                "make_latest": "false",
                "name": f"Deadlock {record['client_version']}",
                "body": (
                    f"Deadlock ClientVersion {record['client_version']}.\n\n"
                    f"Source: https://github.com/{pipeline.SOURCE_REPO}/commit/{record['source']['commit']}\n\n"
                    f"Content SHA-256: {record['identity']['content_sha256']}\n"
                ),
            },
        )
    # Keep the release ID: a new draft may not yet be discoverable by tag.
    endpoint = f"releases/{release['id']}"
    release = github.api(endpoint)
    if not release["draft"]:
        raise ValueError("refusing to upload assets to a non-draft release")
    existing = {asset["name"] for asset in release["assets"]}
    if not existing <= record["artifacts"].keys():
        raise ValueError("draft release contains unexpected assets")
    verify_release(
        github,
        release,
        {"artifacts": {name: record["artifacts"][name] for name in existing}},
        published=False,
    )
    for name in sorted(record["artifacts"].keys() - existing):
        github.upload_asset(release["id"], directory / name)
    verify_release(github, github.api(endpoint), record, published=False)
    release = github.api(
        endpoint, method="PATCH", data={"draft": False, "make_latest": "false"}
    )
    verify_release(github, release, record, published=True)
    return release


def publish_plan(
    github: GitHub,
    plan: dict,
    original_index: dict,
    previous_sha: str | None,
    target: str,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("publishing requires the boon-data generator commit SHA")
    release = publish_snapshot(github, plan, target)
    record_publication(plan["index"], plan["snapshot"], release)
    validate_index(plan["index"], published=True)
    if plan["index"] != original_index:
        github.write_index(plan["index"], previous_sha, target)
    latest = plan["index"]["latest"]
    if latest is not None:
        tag = plan["index"]["versions"][latest]["snapshot"]
        latest_release = release if release["tag_name"] == tag else github.release(tag)
        if latest_release is None:
            raise ValueError(f"latest snapshot release is missing: {tag}")
        github.api(
            f"releases/{latest_release['id']}",
            method="PATCH",
            data={"make_latest": "true"},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="master")
    parser.add_argument("--output", type=Path, default=Path(".work/dist"))
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY),
    )
    parser.add_argument("--target", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument(
        "--index", type=Path, help="Use a local index for a preview; no GitHub writes."
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish verified releases and commit the version index.",
    )
    args = parser.parse_args()
    if args.publish and args.index:
        parser.error("--index is only available for local previews")
    try:
        github = GitHub(args.repository)
        if args.index:
            original = (
                validate_index(json.loads(args.index.read_text()))
                if args.index.exists()
                else empty_index()
            )
            previous_sha = None
            index = original
        else:
            original, previous_sha = github.read_index()
            index = recover_snapshots(github, original)
        source = pipeline.resolve_source(args.ref)
        plan = make_plan(
            source,
            pipeline.download_inputs(source),
            index,
            args.output,
            latest=args.ref == "master",
            repository=github.repository,
        )
        if args.publish:
            publish_plan(github, plan, original, previous_sha, args.target)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / INDEX_FILE).write_text(
            to_json(plan["index"]) + "\n", encoding="utf-8"
        )
        summary = {key: value for key, value in plan.items() if key != "index"}
        summary["index"] = str(args.output / INDEX_FILE)
        (args.output / "plan.json").write_text(
            to_json(summary) + "\n", encoding="utf-8"
        )
        print(json.dumps({**summary, "published": args.publish}, indent=2))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
