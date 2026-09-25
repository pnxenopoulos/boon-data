"""Convert a pinned VData/localization snapshot into definition catalogs."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path

import polars as pl
from keyvalues import parse, to_json, unwrap

CATALOG_SCHEMA_VERSION = 6
JSON_SCHEMA_VERSION = 2
VDATA_FILES = ("abilities.vdata", "heroes.vdata", "modifiers.vdata", "misc.vdata")
LOCALIZATION_FILES = tuple(
    f"game/citadel/resource/localization/{name}/{name}_english.txt"
    for name in (
        "citadel_heroes",
        "citadel_gc_mod_names",
        "citadel_gc_hero_names",
        "citadel_mods",
        "citadel_main",
    )
)
PROPERTY_SCHEMA = {
    "name": pl.String,
    "value": pl.Float64,
    "value_json": pl.String,
    "provided_property": pl.String,
    "usage_flags": pl.String,
    "display_type": pl.String,
    "display_units": pl.String,
    "definition_json": pl.String,
}
PROPERTY_TYPE = pl.List(pl.Struct(PROPERTY_SCHEMA))

PROVENANCE = {"source_commit": pl.String, "client_version": pl.String}
DEFINITION = {
    "class_name": pl.String,
    "is_template": pl.Boolean,
    "disabled": pl.Boolean,
    "base_names": pl.List(pl.String),
    "definition_json": pl.String,
}


def string_token(name: str) -> int:
    """Source 2 CUtlStringToken: MurmurHash2, seed 0x31415926."""
    data = name.encode("utf-8")
    multiplier = 0x5BD1E995
    mask = 0xFFFFFFFF
    result = 0x31415926 ^ len(data)
    end = len(data) - len(data) % 4
    for start in range(0, end, 4):
        block = int.from_bytes(data[start : start + 4], "little")
        block = block * multiplier & mask
        block ^= block >> 24
        block = block * multiplier & mask
        result = (result * multiplier & mask) ^ block
    if end != len(data):
        result ^= int.from_bytes(data[end:], "little")
        result = result * multiplier & mask
    result ^= result >> 13
    result = result * multiplier & mask
    return result ^ (result >> 15)


def number(value) -> float | None:
    """Only finite, single numeric literals are scalar stats; zero stays zero."""
    value = unwrap(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and not re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()
    ):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def boolean(value) -> bool | None:
    """VData exports flags as booleans, numeric bits, or quoted equivalents."""
    if value is None:
        return None
    if value in (True, 1, "true", "1"):
        return True
    if value in (False, 0, "false", "0"):
        return False
    raise ValueError(f"unsupported boolean value: {value!r}")


def property_rows(properties: dict, names=None) -> list[dict]:
    rows = []
    for name in sorted(properties if names is None else names):
        definition = unwrap(properties.get(name))
        fields = definition if isinstance(definition, dict) else {}
        rows.append(
            {
                "name": name,
                "value": number(fields.get("m_strValue")),
                "value_json": to_json(fields["m_strValue"])
                if "m_strValue" in fields
                else None,
                "provided_property": fields.get("m_eProvidedPropertyType"),
                "usage_flags": fields.get("m_eStatsUsageFlags"),
                "display_type": fields.get("m_eDisplayType"),
                "display_units": fields.get("m_eDisplayUnits"),
                "definition_json": to_json(definition)
                if definition is not None
                else None,
            }
        )
    return rows


def localization_tokens(contents: dict[str, bytes]) -> dict[str, str]:
    tokens = {}
    for path, data in sorted(contents.items()):
        document = parse(data.decode("utf-8-sig"), kv1=True)
        for key, value in document["lang"]["Tokens"].items():
            if not isinstance(value, str):
                raise TypeError(f"non-string localization token {key!r} in {path}")
            if key in tokens and tokens[key] != value:
                raise ValueError(f"conflicting English localization token {key!r}")
            tokens[key] = value
    return tokens


def definition_fields(definition: dict) -> dict:
    bases = definition.get("_multibase", [])
    if "_base" in definition:
        bases = [definition["_base"], *bases]
    return {
        "class_name": definition.get("_class"),
        "is_template": bool(definition.get("_not_pickable", False)),
        "disabled": boolean(definition.get("m_bDisabled")),
        "base_names": bases,
        "definition_json": to_json(definition),
    }


def definitions(document: dict) -> dict[str, dict]:
    result = {}
    for name, value in document.items():
        if name.startswith("_") or name == "generic_data_type":
            continue
        value = unwrap(value)
        if not isinstance(value, dict):
            raise TypeError(f"expected an object definition for {name!r}")
        result[name] = value
    if not result:
        raise ValueError("VData has no definitions")
    return dict(sorted(result.items()))


def frame(rows: list[dict], schema: dict, stat_prefixes=()) -> pl.DataFrame:
    stat_columns = sorted(
        {
            key
            for row in rows
            for key in row
            if key.startswith(stat_prefixes) and key not in schema
        }
    )
    return pl.DataFrame(
        rows, schema={**schema, **dict.fromkeys(stat_columns, pl.Float64)}
    )


def check_ids(table: pl.DataFrame, prefix: str) -> None:
    names = {}
    for name, identifier in table.select(f"{prefix}_name", f"{prefix}_id").iter_rows():
        if identifier is None:
            raise ValueError(f"missing ID for {name}")
        if identifier in names and names[identifier] != name:
            raise ValueError(f"ID collision: {names[identifier]} and {name}")
        names[identifier] = name


def ability_property_table(
    abilities: list[dict], definitions: dict, modifiers: list[dict]
) -> pl.DataFrame:
    """Keep one row per property, with explicit scaling and modifier bindings."""
    identity = {
        "ability_name": pl.String,
        "ability_id": pl.UInt32,
        "display_name": pl.String,
        "is_item": pl.Boolean,
        "is_template": pl.Boolean,
        "disabled": pl.Boolean,
        **PROVENANCE,
    }
    binding_schema = {
        "modifier_name": pl.String,
        "modifier_id": pl.UInt32,
        "source_file": pl.String,
        "definition_path": pl.String,
    }
    bindings = defaultdict(list)
    for modifier in modifiers:
        if modifier["ability_name"] is not None:
            for prop in modifier["bound_properties"]:
                bindings[modifier["ability_name"], prop["name"]].append(
                    {key: modifier[key] for key in binding_schema}
                )
    rows = []
    for ability in abilities:
        name = ability["ability_name"]
        for prop in ability["properties"]:
            definition = unwrap(
                definitions[name]["m_mapAbilityProperties"][prop["name"]]
            )
            scale = (
                definition.get("m_subclassScaleFunction")
                if isinstance(definition, dict)
                else None
            )
            fields = unwrap(scale)
            fields = fields if isinstance(fields, dict) else {}
            rows.append(
                {
                    **{key: ability[key] for key in identity},
                    **{
                        ("property_name" if key == "name" else key): value
                        for key, value in prop.items()
                    },
                    "scale_function": fields.get("_class"),
                    "scaling_stat": fields.get("m_eSpecificStatScaleType"),
                    "scaling_stats": fields.get("m_vecScalingStats"),
                    "scaling_coefficient": number(fields.get("m_flStatScale")),
                    "street_brawl_scaling_coefficient": number(
                        fields.get("m_flStreetBrawlStatScale")
                    ),
                    "scaling_disabled": boolean(fields.get("m_bFunctionDisabled")),
                    "scale_function_json": to_json(scale)
                    if scale is not None
                    else None,
                    "modifier_bindings": bindings.get((name, prop["name"]), []),
                }
            )
    return frame(
        rows,
        {
            **identity,
            **{
                ("property_name" if key == "name" else key): dtype
                for key, dtype in PROPERTY_SCHEMA.items()
            },
            "scale_function": pl.String,
            "scaling_stat": pl.String,
            "scaling_stats": pl.List(pl.String),
            "scaling_coefficient": pl.Float64,
            "street_brawl_scaling_coefficient": pl.Float64,
            "scaling_disabled": pl.Boolean,
            "scale_function_json": pl.String,
            "modifier_bindings": pl.List(pl.Struct(binding_schema)),
        },
    )


def pointer_segment(value: str) -> str:
    """Escape one logical VData path segment (typed wrappers are transparent)."""
    return value.replace("~", "~0").replace("/", "~1")


def json_properties(record: dict) -> dict:
    properties = record["definition"].get("m_mapAbilityProperties", {})
    result = {}
    for name, definition in sorted(properties.items()):
        definition = unwrap(definition)
        fields = definition if isinstance(definition, dict) else {}
        result[name] = {
            "value": number(fields.get("m_strValue")),
            "raw_value": fields.get("m_strValue"),
            "stat": fields.get("m_eProvidedPropertyType"),
            "usage_flags": fields.get("m_eStatsUsageFlags"),
            "display_units": fields.get("m_eDisplayUnits"),
            "scaling": fields.get("m_subclassScaleFunction"),
            "source_record_key": record["record_key"],
            "definition_path": (
                f"{record['definition_path']}/m_mapAbilityProperties/"
                f"{pointer_segment(name)}"
            ),
            "modifier_keys": [],
        }
    return result


def declared_values(record: dict) -> list[dict]:
    """Expose only explicit stat/value pairs; retain ranges and unknown fields."""
    changes = []
    for field in ("m_vecScriptValues", "m_vecModifierValues"):
        for index, definition in enumerate(record["definition"].get(field, [])):
            fields = unwrap(definition)
            changes.append(
                {
                    "kind": field,
                    "stat": fields.get("m_eModifierValue"),
                    "value": number(fields.get("m_value")),
                    "raw_value": fields.get("m_value"),
                    "value_min": number(fields.get("m_valueMin")),
                    "value_max": number(fields.get("m_valueMax")),
                    "source_record_key": record["record_key"],
                    "definition_path": f"{record['definition_path']}/{field}/{index}",
                    "definition": definition,
                }
            )
    return changes


def add_lookups(payloads: dict[str, dict]) -> None:
    """Index every candidate and link explicit property bindings without applying effects."""
    prefixes = {
        "abilities": "ability",
        "heroes": "hero",
        "misc": "misc",
        "modifiers": "modifier",
    }
    by_key = {}
    properties = {}
    for catalog, payload in payloads.items():
        prefix = prefixes[catalog]
        fields = {"by_id": f"{prefix}_id", "by_name": f"{prefix}_name"}
        if catalog == "modifiers":
            fields.update(
                by_qualified_id="qualified_modifier_id",
                by_qualified_name="qualified_modifier_name",
            )
        indexes = {name: {} for name in ("by_key", *fields)}
        for index, record in enumerate(payload["records"]):
            if catalog != "modifiers":
                record["source_file"] = f"{catalog}.vdata"
                record["definition_path"] = "/" + pointer_segment(
                    record[f"{prefix}_name"]
                )
            key = f"{record['source_file']}#{record['definition_path']}"
            if key in by_key:
                raise ValueError(f"duplicate record key: {key}")
            record["record_key"] = key
            by_key[key] = record
            indexes["by_key"][key] = index
            for name, field in fields.items():
                indexes[name].setdefault(str(record[field]), []).append(index)
            record["modifier_keys"] = []
            record["stat_changes"] = declared_values(record)
            properties[key] = json_properties(record)
            if catalog == "abilities" or properties[key]:
                record["properties"] = properties[key]
        payload["indexes"] = indexes

    for modifier in payloads["modifiers"]["records"]:
        root_path = "/" + modifier["definition_path"].split("/")[1]
        owner_key = f"{modifier['source_file']}#{root_path}"
        owner = by_key[owner_key]
        if owner is not modifier:
            owner["modifier_keys"].append(modifier["record_key"])
        bindings = []
        for name in modifier["definition"].get(
            "m_vecAutoRegisterModifierValueFromAbilityPropertyName", []
        ):
            prop = properties[owner_key].get(name)
            binding = {
                "property_name": name,
                "source_record_key": owner_key,
                "status": "resolved" if prop is not None else "unresolved",
                "property": None,
            }
            if prop is not None:
                if modifier["record_key"] not in prop["modifier_keys"]:
                    prop["modifier_keys"].append(modifier["record_key"])
                binding["property"] = {
                    k: v for k, v in prop.items() if k != "modifier_keys"
                }
                if prop["stat"]:
                    modifier["stat_changes"].append(
                        {
                            "kind": "bound_property",
                            "property_name": name,
                            **binding["property"],
                        }
                    )
            bindings.append(binding)
        modifier["property_bindings"] = bindings

    for record in payloads["abilities"]["records"]:
        for name, prop in record["properties"].items():
            if prop["stat"]:
                record["stat_changes"].append(
                    {
                        "kind": "ability_property",
                        "property_name": name,
                        **prop,
                    }
                )


def build_catalogs(
    vdata: dict[str, bytes],
    localization: dict[str, bytes],
    source: dict,
    output: Path,
    *,
    parquet: bool = False,
) -> dict:
    """Write JSON definitions, optionally adding local Parquet exports."""
    documents = {name: parse(vdata[name].decode("utf-8-sig")) for name in VDATA_FILES}
    roots = {name: definitions(document) for name, document in documents.items()}
    tokens = localization_tokens(localization)
    provenance = {
        "source_commit": source["source"]["commit"],
        "client_version": source["client_version"],
    }
    abilities = []
    for name, definition in roots["abilities.vdata"].items():
        properties = property_rows(definition.get("m_mapAbilityProperties", {}))
        abilities.append(
            {
                "ability_name": name,
                "ability_id": string_token(name),
                "display_name": tokens.get(name),
                "is_item": definition.get("m_eAbilityType") == "EAbilityType_Item",
                "item_tier": definition.get("m_iItemTier"),
                "item_slot_type": definition.get("m_eItemSlotType"),
                "activation": definition.get("m_eAbilityActivation"),
                "properties": properties,
                "upgrades_json": to_json(definition.get("m_vecAbilityUpgrades", [])),
                **definition_fields(definition),
                **provenance,
                **{f"stat_{p['name']}": p["value"] for p in properties},
            }
        )
    ability_table = frame(
        abilities,
        {
            "ability_name": pl.String,
            "ability_id": pl.UInt32,
            "display_name": pl.String,
            "is_item": pl.Boolean,
            "item_tier": pl.String,
            "item_slot_type": pl.String,
            "activation": pl.String,
            "properties": PROPERTY_TYPE,
            "upgrades_json": pl.String,
            **DEFINITION,
            **PROVENANCE,
        },
        ("stat_",),
    )

    heroes = []
    for name, definition in roots["heroes.vdata"].items():
        heroes.append(
            {
                "hero_name": name,
                "hero_id": definition.get("m_HeroID"),
                "display_name": tokens.get(f"{name}:n"),
                "player_selectable": boolean(definition.get("m_bPlayerSelectable")),
                "bound_abilities_json": to_json(
                    definition.get("m_mapBoundAbilities", {})
                ),
                "scaling_stats_json": to_json(definition.get("m_mapScalingStats", {})),
                **definition_fields(definition),
                **provenance,
                **{
                    f"base_{key}": number(value)
                    for key, value in definition.get("m_mapStartingStats", {}).items()
                },
                **{
                    f"level_{key}": number(value)
                    for key, value in definition.get(
                        "m_mapStandardLevelUpUpgrades", {}
                    ).items()
                },
            }
        )
    hero_table = frame(
        heroes,
        {
            "hero_name": pl.String,
            "hero_id": pl.UInt32,
            "display_name": pl.String,
            "player_selectable": pl.Boolean,
            "bound_abilities_json": pl.String,
            "scaling_stats_json": pl.String,
            **DEFINITION,
            **PROVENANCE,
        },
        ("base_", "level_"),
    )

    misc_table = frame(
        [
            {
                "misc_name": name,
                "misc_id": string_token(name),
                "display_name": tokens.get(definition.get("m_sNameLocString", name)),
                **definition_fields(definition),
                **provenance,
            }
            for name, definition in roots["misc.vdata"].items()
        ],
        {
            "misc_name": pl.String,
            "misc_id": pl.UInt32,
            "display_name": pl.String,
            **DEFINITION,
            **PROVENANCE,
        },
    )

    modifiers = []

    def walk(value, path: str, source_file: str, owner: str, root: dict):
        value = unwrap(value)
        if isinstance(value, dict):
            is_root = (
                path == "/" + pointer_segment(owner)
                and source_file == "modifiers.vdata"
            )
            if is_root or str(value.get("_class", "")).startswith("modifier_"):
                name = owner if is_root else value.get("_my_subclass_name")
                if name:
                    ability = owner if source_file == "abilities.vdata" else None
                    hero = owner if source_file == "heroes.vdata" else None
                    misc = owner if source_file == "misc.vdata" else None
                    qualified_name = name if is_root else f"{owner}/{name}"
                    bindings = value.get(
                        "m_vecAutoRegisterModifierValueFromAbilityPropertyName", []
                    )
                    modifiers.append(
                        {
                            "modifier_name": name,
                            "modifier_id": string_token(name),
                            "qualified_modifier_name": qualified_name,
                            "qualified_modifier_id": string_token(qualified_name),
                            "display_name": tokens.get(name),
                            "source_file": source_file,
                            "definition_path": path,
                            "ability_name": ability,
                            "ability_id": string_token(ability) if ability else None,
                            "hero_name": hero,
                            "hero_id": root.get("m_HeroID") if hero else None,
                            "misc_name": misc,
                            "misc_id": string_token(misc) if misc else None,
                            "is_hidden": boolean(value.get("m_bIsHidden")),
                            "debuff_type": value.get("m_eDebuffType"),
                            "bound_properties": property_rows(
                                root.get("m_mapAbilityProperties", {}), bindings
                            ),
                            **definition_fields(value),
                            **provenance,
                            **{
                                f"stat_{key}": number(item)
                                for key, item in value.items()
                                if not key.startswith("_") and number(item) is not None
                            },
                        }
                    )
            for key, child in sorted(value.items()):
                # JSON Pointer paths identify repeated subclasses without conflating them.
                segment = pointer_segment(key)
                walk(child, f"{path}/{segment}", source_file, owner, root)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}/{index}", source_file, owner, root)

    for source_file, entries in roots.items():
        for name, definition in entries.items():
            walk(definition, "/" + pointer_segment(name), source_file, name, definition)
    modifier_table = frame(
        modifiers,
        {
            "modifier_name": pl.String,
            "modifier_id": pl.UInt32,
            "qualified_modifier_name": pl.String,
            "qualified_modifier_id": pl.UInt32,
            "display_name": pl.String,
            "source_file": pl.String,
            "definition_path": pl.String,
            "ability_name": pl.String,
            "ability_id": pl.UInt32,
            "hero_name": pl.String,
            "hero_id": pl.UInt32,
            "misc_name": pl.String,
            "misc_id": pl.UInt32,
            "is_hidden": pl.Boolean,
            "debuff_type": pl.String,
            "bound_properties": PROPERTY_TYPE,
            **DEFINITION,
            **PROVENANCE,
        },
        ("stat_",),
    )
    check_ids(modifier_table, "qualified_modifier")
    tables = {
        "abilities": ability_table,
        "heroes": hero_table,
        "modifiers": modifier_table,
        "misc": misc_table,
    }
    if parquet:
        tables["ability_properties"] = ability_property_table(
            abilities, roots["abilities.vdata"], modifiers
        )
    metadata = {}
    json_metadata = {}
    payloads = {}
    for name, table in tables.items():
        check_ids(
            table,
            {
                "abilities": "ability",
                "ability_properties": "ability",
                "heroes": "hero",
                "modifiers": "modifier",
                "misc": "misc",
            }[name],
        )
        if parquet:
            table.write_parquet(output / f"{name}.parquet", compression="zstd")
            metadata[f"{name}.parquet"] = {
                "rows": table.height,
                "columns": {key: str(dtype) for key, dtype in table.schema.items()},
            }
        if name != "ability_properties":
            # JSON is a nested definition catalog, without thousands of sparse stat columns.
            identity = {
                "abilities": ("ability_name", "ability_id", "display_name"),
                "heroes": ("hero_name", "hero_id", "display_name"),
                "misc": ("misc_name", "misc_id", "display_name"),
                "modifiers": (
                    "modifier_name",
                    "modifier_id",
                    "qualified_modifier_name",
                    "qualified_modifier_id",
                    "display_name",
                    "source_file",
                    "definition_path",
                    "ability_name",
                    "ability_id",
                    "hero_name",
                    "hero_id",
                    "misc_name",
                    "misc_id",
                ),
            }[name]
            records = [
                {
                    **{key: row[key] for key in identity},
                    "definition": json.loads(row["definition_json"]),
                }
                for row in table.select(*identity, "definition_json").to_dicts()
            ]
            payloads[name] = {
                "schema_version": JSON_SCHEMA_VERSION,
                "catalog": name,
                **provenance,
                "records": records,
            }
            json_metadata[f"{name}.json"] = {
                "schema_version": JSON_SCHEMA_VERSION,
                "records": len(records),
            }
    add_lookups(payloads)
    for name, payload in payloads.items():
        (output / f"{name}.json").write_text(to_json(payload) + "\n", encoding="utf-8")
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generator": "boon-data",
        "polars_version": pl.__version__,
        "tables": metadata,
        "json_catalogs": json_metadata,
        "vdata_metadata": {
            name: {
                key: to_json(value)
                for key, value in document.items()
                if key.startswith("_") or key == "generic_data_type"
            }
            for name, document in documents.items()
        },
    }
