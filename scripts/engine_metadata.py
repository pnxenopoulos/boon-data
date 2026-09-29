"""Read engine declarations without inferring gameplay rules."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import PurePosixPath

SCHEMA_DIRECTORY = "DumpSource2/schemas/client"
MODIFIER_ENUM_FILE = f"{SCHEMA_DIRECTORY}/EModifierValue.h"
MODIFIER_STATE_ENUM_FILE = f"{SCHEMA_DIRECTORY}/EModifierState.h"
SCHEMA_FILES = (
    MODIFIER_ENUM_FILE,
    MODIFIER_STATE_ENUM_FILE,
    f"{SCHEMA_DIRECTORY}/EStatsType.h",
    f"{SCHEMA_DIRECTORY}/PropertyValueApplyFilter_t.h",
    f"{SCHEMA_DIRECTORY}/StatsUsageFlags_t.h",
)
STRING_FILES = (
    "game/citadel/bin/win64/client_strings.txt",
    "game/citadel/bin/win64/server_strings.txt",
)


def is_scaling_schema(path: str) -> bool:
    source = PurePosixPath(path)
    return (
        str(source.parent) == SCHEMA_DIRECTORY
        and source.name.startswith("CScaleFunction")
        and source.suffix == ".h"
    )


def is_schema(path: str) -> bool:
    return path in SCHEMA_FILES or is_scaling_schema(path)


def is_engine_source(path: str) -> bool:
    return is_schema(path) or path in STRING_FILES


def enum_definitions(sources: dict[str, bytes]) -> dict:
    """Preserve explicit ordinals, aliases, flags, and sentinels by enum name."""
    result = {}
    for path in SCHEMA_FILES:
        if path not in sources:
            continue
        text = re.sub(
            r"//[^\n]*|/\*.*?\*/",
            "",
            sources[path].decode("utf-8-sig"),
            flags=re.DOTALL,
        )
        match = re.fullmatch(
            r"\s*enum\s+(\w+)(?:\s*:\s*(\w+))?\s*\{(.*?)\}\s*;\s*",
            text,
            flags=re.DOTALL,
        )
        if match is None or match[1] != PurePosixPath(path).stem:
            raise ValueError(f"invalid enum declaration: {path}")
        values = {}
        by_value = defaultdict(list)
        for entry in match[3].split(","):
            if not entry.strip():
                continue
            member = re.fullmatch(
                r"\s*(\w+)\s*=\s*(-?(?:0[xX][0-9a-fA-F]+|[0-9]+))\s*", entry
            )
            if member is None or member[1] in values:
                raise ValueError(
                    f"invalid explicit enum member in {path}: {entry.strip()}"
                )
            raw = member[2]
            value = int(raw, 16 if "x" in raw.lower() else 10)
            values[member[1]] = value
            by_value[str(value)].append(member[1])
        if not values:
            raise ValueError(f"empty enum declaration: {path}")
        result[match[1]] = {
            "source_file": path,
            "underlying_type": match[2],
            "values": values,
            "names_by_value": dict(by_value),
        }
    return result


def scaling_class_defaults(sources: dict[str, bytes]) -> dict:
    """Keep schema defaults separate from explicit VData and unknown equations."""
    result = {}
    for path, data in sorted(sources.items()):
        if not is_scaling_schema(path):
            continue
        text = data.decode("utf-8-sig")
        declaration = re.search(
            r"^class (\w+)(?: : public (\w+))?\s*\{", text, re.MULTILINE
        )
        if declaration is None or declaration[1] != PurePosixPath(path).stem:
            raise ValueError(f"invalid scaling class declaration: {path}")
        comments = "\n".join(
            line[2:] for line in text.splitlines() if line.startswith("//")
        )
        marker = "MGetKV3ClassDefaults ="
        if marker not in comments:
            continue
        defaults, _ = json.JSONDecoder().raw_decode(
            comments.split(marker, 1)[1].lstrip()
        )
        if not isinstance(defaults, dict) or defaults.get("_class") != declaration[1]:
            raise ValueError(f"scaling defaults do not match class: {path}")
        result[declaration[1]] = {
            "source_file": path,
            "base_class": declaration[2],
            "defaults": defaults,
        }
    return result


def engine_modifier_names(
    sources: dict[str, bytes], modifiers: list[dict], hash_name: Callable[[str], int]
) -> dict:
    """Index whole modifier-name strings without inventing definitions or effects."""
    locations = defaultdict(list)
    for path in STRING_FILES:
        if path not in sources:
            continue
        for line, text in enumerate(sources[path].decode("utf-8-sig").splitlines(), 1):
            name = text.strip()
            if re.fullmatch(r"(?:citadel_)?modifier_[A-Za-z0-9_]+", name):
                locations[name].append({"source_file": path, "line": line})
    catalog_keys = defaultdict(set)
    id_names = defaultdict(set)
    for modifier in modifiers:
        for prefix in ("modifier", "qualified_modifier"):
            name = modifier[f"{prefix}_name"]
            catalog_keys[name].add(modifier["record_key"])
            id_names[str(modifier[f"{prefix}_id"])].add(name)
    records = []
    by_id = defaultdict(list)
    by_name = {}
    for name, locations_for_name in sorted(locations.items()):
        identifier = hash_name(name)
        index = len(records)
        records.append(
            {
                "modifier_name": name,
                "modifier_id": identifier,
                "status": "name_only",
                "sources": locations_for_name,
                "catalog_keys": sorted(catalog_keys[name]),
            }
        )
        by_id[str(identifier)].append(index)
        by_name[name] = index
        id_names[str(identifier)].add(name)
    return {
        "records": records,
        "indexes": {"by_id": dict(by_id), "by_name": by_name},
        "id_collisions": {
            key: sorted(id_names[key]) for key in by_id if len(id_names[key]) > 1
        },
    }
