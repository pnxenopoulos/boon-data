"""Extract game images from a matching Deadlock install with Source2Viewer."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from PIL import Image

ARTIFACTS = {"images.json", "images.zip"}
PAK = Path("game/citadel/pak01_dir.vpk")
STEAM_INF = Path("game/citadel/steam.inf")
MAP_IMAGES = tuple(
    f"panorama/images/minimap/base/{name}.vtex_c"
    for name in (
        "minimap_midtown_mid_psd",
        "minimap_midtown_mid_tunnels_psd",
        "minimap_midtown_rat_tunnels_psd",
    )
)
HERO_IMAGES = {
    "minimap": "m_strMinimapImage",
    "icon": "m_strIconImageSmall",
    "card": "m_strIconHeroCard",
}
ABILITY_IMAGES = {
    "icon": "m_strAbilityImage",
    "single_door": "m_strSingleDoorAbilityImage",
}
ITEM_IMAGES = {"shop": "m_strShopIconLarge"}
STAT_ICON_PREFIX = "panorama/images/icons/properties/"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--images",
        action="store_true",
        help="Include minimap, hero, item, ability, and stat images.",
    )
    parser.add_argument(
        "--game-dir",
        type=Path,
        default=os.environ.get("DEADLOCK_DIR") or None,
        help="Deadlock install root (or set DEADLOCK_DIR).",
    )
    parser.add_argument(
        "--source2viewer",
        type=Path,
        default=os.environ.get("SOURCE2VIEWER_CLI") or None,
        help="Source2Viewer-CLI executable (or set SOURCE2VIEWER_CLI).",
    )


def fingerprint(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def texture_path(value) -> str | None:
    """Convert Panorama references to their compiled texture resource paths."""
    while isinstance(value, dict) and "$value" in value:
        value = value["$value"]
    if not value:
        return None
    if not isinstance(value, str):
        raise TypeError("invalid image reference")
    path = value.replace("file://{images}/", "panorama/images/", 1).removeprefix(
        "s2r://"
    )
    parts = PurePosixPath(path)
    if (
        not path.startswith("panorama/images/")
        or any(part in {".", ".."} for part in path.split("/"))
        or any(character in path for character in ("\\", ",", "\n", "\r"))
    ):
        raise ValueError(f"invalid image resource path: {value}")
    if parts.suffix in {".vtex_c", ".vsvg_c"}:
        return path
    if parts.suffix in {".vtex", ".vsvg"}:
        return path + "_c"
    if parts.suffix == ".svg":
        return str(parts.with_suffix(".vsvg_c"))
    if parts.suffix not in {".psd", ".png", ".tga", ".jpg"}:
        raise ValueError(f"unsupported image resource: {value}")
    return str(parts.with_suffix("")) + "_" + parts.suffix[1:] + ".vtex_c"


def run(tool: Path, *arguments: str) -> str:
    return subprocess.run(
        [str(tool), *arguments], check=True, capture_output=True, text=True
    ).stdout


def image_path(resource: str) -> PurePosixPath:
    suffix = ".svg" if resource.endswith(".vsvg_c") else ".png"
    return PurePosixPath(resource).with_suffix(suffix)


def image_size(data: bytes, resource: str) -> tuple[float, float]:
    if resource.endswith(".vsvg_c"):
        try:
            svg = ElementTree.fromstring(data)
            if svg.tag != "{http://www.w3.org/2000/svg}svg":
                raise ValueError("missing SVG root")
            bounds = svg.get("viewBox", "").split()
            dimensions = (
                bounds[2:]
                if len(bounds) == 4
                else [svg.attrib["width"], svg.attrib["height"]]
            )
            return float(dimensions[0]), float(dimensions[1])
        except (ElementTree.ParseError, KeyError, ValueError) as error:
            raise ValueError(f"invalid SVG image: {resource}") from error
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError(f"expected a PNG image: {resource}")
        size = image.size
        image.verify()
        return size


def catalog_records(source: dict, directory: Path, name: str) -> list[dict]:
    catalog = json.loads((directory / f"{name}.json").read_bytes())
    if (
        catalog["client_version"] != source["client_version"]
        or catalog["source_commit"] != source["source"]["commit"]
    ):
        raise ValueError(f"{name} catalog does not match the requested snapshot")
    return catalog["records"]


def build(
    source: dict,
    directory: Path,
    *,
    game_dir: Path | None = None,
    source2viewer: Path | None = None,
) -> None:
    """Extract selected minimaps and catalog image references; preserve unknowns."""
    if game_dir is None:
        raise ValueError("images require --game-dir or DEADLOCK_DIR")
    game_dir = game_dir.expanduser().resolve()
    steam = (game_dir / STEAM_INF).read_bytes()
    info = dict(
        line.strip().split("=", 1)
        for line in steam.decode("utf-8").splitlines()
        if "=" in line
    )
    for field, key in (
        ("ClientVersion", "client_version"),
        ("SourceRevision", "source_revision"),
    ):
        if source.get(key) is not None and info.get(field) != source[key]:
            raise ValueError(
                f"install {field} is {info.get(field)}, but the catalog requires {source[key]}"
            )
    vpk = game_dir / PAK
    vpk_hash = fingerprint(vpk.read_bytes())
    if source2viewer is None:
        local = (
            Path(__file__).resolve().parents[1]
            / ".work/source2viewer/20.1/Source2Viewer-CLI"
        )
        source2viewer = (
            local if local.is_file() else Path(shutil.which("Source2Viewer-CLI") or "")
        )
    tool = source2viewer.expanduser().resolve()
    if not tool.is_file():
        raise ValueError(
            "Source2Viewer-CLI is missing; run scripts/install-source2viewer.sh or set --source2viewer"
        )
    version = run(tool, "--version").splitlines()[0]
    listing = run(tool, "-i", str(vpk), "--vpk_list", "-f", "panorama/images/")
    resources = {
        match[1]
        for line in listing.splitlines()
        if (
            match := re.fullmatch(
                r"(panorama/images/[^,\r\n]+\.v(?:tex|svg)_c) CRC:[0-9a-fA-F]+ size:[0-9]+",
                line,
            )
        )
    }
    if not resources:
        raise ValueError("Source2Viewer returned no image resources")
    if any(".." in PurePosixPath(path).parts or "\\" in path for path in resources):
        raise ValueError("invalid VPK image resource path")
    maps: dict = {path: path for path in MAP_IMAGES}
    if not resources.intersection(maps):
        raise ValueError("the install has no supported map image resources")
    heroes = {}
    for hero in catalog_records(source, directory, "heroes"):
        hero_id = hero["hero_id"]
        if hero_id is None:
            continue
        heroes[str(hero_id)] = {
            "hero_name": hero["hero_name"],
            "display_name": hero["display_name"],
            "images": {
                role: texture_path(hero["definition"].get(field))
                for role, field in HERO_IMAGES.items()
            },
        }
    abilities = {}
    items = {}
    for ability in catalog_records(source, directory, "abilities"):
        ability_id = ability["ability_id"]
        if ability_id is None:
            continue
        definition = ability["definition"]
        is_item = definition.get("m_eAbilityType") == "EAbilityType_Item"
        records = items if is_item else abilities
        fields = ITEM_IMAGES if is_item else ABILITY_IMAGES
        key = str(ability_id)
        if key in items or key in abilities:
            raise ValueError(f"ambiguous ability image ID: {ability_id}")
        records[key] = {
            "ability_name": ability["ability_name"],
            "display_name": ability["display_name"],
            "images": {
                role: texture_path(definition.get(field))
                for role, field in fields.items()
            },
        }
    stats: dict = {}
    for resource in sorted(resources):
        if resource.startswith(STAT_ICON_PREFIX):
            key = str(
                PurePosixPath(resource).relative_to(STAT_ICON_PREFIX).with_suffix("")
            )
            if key in stats:
                raise ValueError(f"ambiguous stat icon name: {key}")
            stats[key] = resource
    groups: list[tuple[str, dict]] = [("maps", maps), ("stats", stats)] + [
        (f"{category}/{key}", record["images"])
        for category, records in (
            ("heroes", heroes),
            ("items", items),
            ("abilities", abilities),
        )
        for key, record in sorted(records.items(), key=lambda item: int(item[0]))
    ]
    paths = {}
    missing = set()
    for folder, images in groups:
        for role, resource in sorted(images.items()):
            if resource is None:
                continue
            if resource not in resources:
                missing.add(resource)
                continue
            # Map keys retain their source resource paths.
            name = (
                "maps/" + str(image_path(resource).relative_to("panorama/images"))
                if folder == "maps"
                else f"{folder}/{role}{image_path(resource).suffix}"
            )
            paths.setdefault(resource, name)
    results: dict[str, tuple[bytes, dict]] = {}
    with tempfile.TemporaryDirectory(prefix=".images-", dir=directory) as temporary:
        extracted = Path(temporary)
        selected = sorted(paths)
        output = (
            extracted if len(selected) != 1 else extracted / image_path(selected[0])
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        run(tool, "-i", str(vpk), "-f", ",".join(selected), "-o", str(output), "-d")
        for resource, name in paths.items():
            image = extracted / image_path(resource)
            if not image.is_file():
                raise ValueError(f"Source2Viewer did not extract {resource}")
            data = image.read_bytes()
            width, height = image_size(data, resource)
            results[resource] = (
                data,
                {
                    "path": name,
                    "resource": resource,
                    **fingerprint(data),
                    "width": width,
                    "height": height,
                },
            )
    if (game_dir / STEAM_INF).read_bytes() != steam or fingerprint(
        vpk.read_bytes()
    ) != vpk_hash:
        raise ValueError("the game installation changed during image extraction")
    for _, images in groups:
        for role, resource in images.items():
            result = results.get(resource)
            images[role] = result[1] if result else None
    index = {
        "client_version": source["client_version"],
        "source_commit": source["source"]["commit"],
        "provider": "Source2Viewer",
        "tool": {"version": version, **fingerprint(tool.read_bytes())},
        "install": {
            "version": info,
            "steam_inf": fingerprint(steam),
            "vpk": {"path": PAK.as_posix(), **vpk_hash},
        },
        "map": {"radius": None, "images": maps},
        "heroes": heroes,
        "items": items,
        "abilities": abilities,
        "stats": stats,
        "missing_images": sorted(missing),
    }
    (directory / "images.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with zipfile.ZipFile(directory / "images.zip", "w") as archive:
        for data, details in sorted(
            results.values(), key=lambda result: result[1]["path"]
        ):
            entry = zipfile.ZipInfo(details["path"])
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
