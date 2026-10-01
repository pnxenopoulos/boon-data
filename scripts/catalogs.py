"""Convert a pinned VData/localization snapshot into definition catalogs."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path

import polars as pl
from engine_metadata import (
    MODIFIER_ENUM_FILE,
    MODIFIER_STATE_ENUM_FILE,
    engine_modifier_names,
    enum_definitions,
    is_engine_source,
    scaling_class_defaults,
)
from keyvalues import parse, to_json, unwrap

VDATA_FILES = (
    "abilities.vdata",
    "heroes.vdata",
    "modifiers.vdata",
    "misc.vdata",
    "npc_units.vdata",
    "generic_data.vdata",
)
WORLD_FILES = ("misc.vdata", "npc_units.vdata")
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
                "apply_filter": fields.get("m_eApplyFilter"),
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
            "apply_filter": fields.get("m_eApplyFilter"),
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
                record.setdefault("source_file", f"{catalog}.vdata")
                record.setdefault(
                    "definition_path", "/" + pointer_segment(record[f"{prefix}_name"])
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

    # A standalone modifier can register properties supplied by an explicit
    # non-embedded reference. Retain each owner; shared names are not ownership.
    external_owners: dict[str, set[str]] = {}
    for modifier in payloads["modifiers"]["records"]:
        definition = modifier["definition"]
        name = definition.get("m_NonEmbeddedModifier")
        if definition.get("m_bUseNonEmbedded") is True and isinstance(name, str):
            owner_key = f"{modifier['source_file']}#/{modifier['definition_path'].split('/')[1]}"
            if by_key[owner_key].get("ability_id") is not None:
                external_owners.setdefault(name, set()).add(owner_key)

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
            owners = [owner_key]
            if modifier["source_file"] == "modifiers.vdata" and owner is modifier:
                owners.extend(
                    sorted(external_owners.get(modifier["modifier_name"], set()))
                )
            matches = [key for key in owners if name in properties[key]]
            if not matches:
                bindings.append(
                    {
                        "property_name": name,
                        "source_record_key": owner_key,
                        "status": "unresolved",
                        "property": None,
                    }
                )
            for key in matches:
                prop = properties[key][name]
                if modifier["record_key"] not in prop["modifier_keys"]:
                    prop["modifier_keys"].append(modifier["record_key"])
                bound = {k: v for k, v in prop.items() if k != "modifier_keys"}
                if key != owner_key:
                    bound["source_ability_id"] = by_key[key]["ability_id"]
                bindings.append(
                    {
                        "property_name": name,
                        "source_record_key": key,
                        "status": "resolved",
                        "property": bound,
                    }
                )
                if prop["stat"]:
                    modifier["stat_changes"].append(
                        {"kind": "bound_property", "property_name": name, **bound}
                    )
        modifier["property_bindings"] = bindings

    add_ethereal_fire_rate_binding(payloads, by_key)
    add_spirit_snatch_bindings(payloads, by_key)

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

    add_runtime_bindings(payloads, by_key)


def add_ethereal_fire_rate_binding(
    payloads: dict[str, dict], by_key: dict[str, dict]
) -> None:
    """Bind Mercurial Magnum's fire-rate property to its specific effect modifier."""
    for modifier in payloads["modifiers"]["records"]:
        if modifier["definition"].get("_class") != "modifier_ethereal_bullets_buff":
            continue
        parent_key, field = modifier["record_key"].rsplit("/", 1)
        parent = by_key.get(parent_key, {})
        if (
            field != "m_BuffModifier"
            or parent.get("definition", {}).get("_class")
            != "modifier_ethereal_bullets_watcher"
        ):
            continue
        owner_key = (
            f"{modifier['source_file']}#/{modifier['definition_path'].split('/')[1]}"
        )
        owner = by_key[owner_key]
        prop = owner.get("properties", {}).get("BonusFireRate")
        if (
            prop is None
            or prop["stat"] != "MODIFIER_VALUE_FIRE_RATE"
            or prop["modifier_keys"]
        ):
            continue
        # Curated link, not an explicit VData registration. At GameTracking revision
        # 8580b13d18d5d966430d2c41501f34bdb7c6243a the engine buff schema has
        # m_flEffectivecFireRatePercent; replay 108575009 records a matching float1
        # during this buff. Do not bind its separate bullet-damage buff or watcher.
        # Keep values/upgrades in VData and label the activation as inferred.
        prop["modifier_keys"].append(modifier["record_key"])
        bound = {k: v for k, v in prop.items() if k != "modifier_keys"}
        modifier["stat_changes"].append(
            {
                "kind": "inferred_property",
                "property_name": "BonusFireRate",
                "binding_source": "curated",
                **bound,
            }
        )
        modifier["property_bindings"].append(
            {
                "property_name": "BonusFireRate",
                "source_record_key": owner_key,
                "status": "resolved",
                "binding_source": "curated",
                "property": bound,
            }
        )


def add_spirit_snatch_bindings(
    payloads: dict[str, dict], by_key: dict[str, dict]
) -> None:
    """Link the separate steal effects; keep all amounts and upgrades in VData."""
    roles = {
        "m_BuffModifier": (
            "modifier_upgrade_spirit_snatch_buff",
            ("TechPowerGain", "TechArmorGain"),
        ),
        "m_DebuffModifier": (
            "modifier_upgrade_spirit_snatch_debuff",
            ("TechPowerReduction", "TechArmorDamageReduction"),
        ),
    }
    for modifier in payloads["modifiers"]["records"]:
        parent_key, field = modifier["record_key"].rsplit("/", 1)
        role = roles.get(field)
        if role is None or modifier["definition"].get("_class") != role[0]:
            continue
        parent = by_key.get(parent_key, {})
        if parent.get("definition", {}).get("_class") != "modifier_spirit_snatch":
            continue
        owner_key = (
            f"{modifier['source_file']}#/{modifier['definition_path'].split('/')[1]}"
        )
        owner = by_key[owner_key]
        properties = owner.get("properties", {})
        if "LightMeleeReduction" not in properties or any(
            properties.get(name, {}).get("stat") != "MODIFIER_VALUE_TECH_POWER"
            for name in ("TechPowerGain", "TechPowerReduction")
        ):
            # These count units are verified for the flat-steal/light-reduction
            # definition. Do not apply them to an older percentage-steal shape.
            continue
        for name in role[1]:
            prop = properties.get(name)
            if prop is None or prop["stat"] is None or prop["modifier_keys"]:
                continue
            # Curated activation and count units, not a VData registration. In
            # replay 108575009, light hits record 70 and heavy hits record 100;
            # the catalog's LightMeleeReduction is 30%. Counts accumulate and
            # decay. Resolve each recorded buff/debuff independently, including
            # removals on target death. Source: GameTracking revision
            # 8580b13d18d5d966430d2c41501f34bdb7c6243a.
            prop["modifier_keys"].append(modifier["record_key"])
            prop["runtime_count"] = {
                "source": "modifier",
                "field": "stack_count",
                "divisor": 100,
            }
            bound = {k: v for k, v in prop.items() if k != "modifier_keys"}
            modifier["stat_changes"].append(
                {
                    "kind": "inferred_property",
                    "property_name": name,
                    "binding_source": "curated",
                    **bound,
                }
            )
            modifier["property_bindings"].append(
                {
                    "property_name": name,
                    "source_record_key": owner_key,
                    "status": "resolved",
                    "binding_source": "curated",
                    "property": bound,
                }
            )


def add_bloodscent_counter(ability: dict) -> None:
    """Bind the owner's recorded rewards; never attach them to target markers."""
    if ability["definition"].get("_class") != "ability_drifter_hunger":
        return
    prop = ability["properties"].get("WeaponDmgPerIsolationKill")
    assist = ability["properties"].get("IsolationAssistPercentValue")
    if (
        prop is None
        or prop["stat"] != "MODIFIER_VALUE_WEAPON_DAMAGE_INCREASE"
        or prop["modifier_keys"]
        or assist is None
    ):
        return
    # Curated interpretation, not an explicit VData registration. The matching
    # server schema networks both earned counters; localization describes a
    # permanent reward on isolated hero death. Interpret the assist percentage
    # as its reward weight. Keep this assumption visible and all amounts in data.
    # Verified source: GameTracking-Deadlock 19022f397ce9ba65856752d0cbaa82e80a2da73f.
    binding = {
        "binding_source": "curated",
        "runtime_counts": [
            {"field": "m_nKillsEarned"},
            {
                "field": "m_nAssistsEarned",
                "percent": {"property_name": "IsolationAssistPercentValue", **assist},
            },
        ],
    }
    prop.update(binding)
    for effect in ability["stat_changes"]:
        if effect.get("property_name") == "WeaponDmgPerIsolationKill":
            effect.update(binding)


def add_runtime_bindings(payloads: dict[str, dict], by_key: dict[str, dict]) -> None:
    """Add curated counter semantics separately from the unchanged VData definition."""
    for ability in payloads["abilities"]["records"]:
        add_bloodscent_counter(ability)
        if ability["definition"].get("_class") != "upgrade_trophy_collector":
            continue
        modifier = by_key.get(f"{ability['record_key']}/m_GoldModifier")
        prop = ability["properties"].get("StackingBonusSprintSpeed")
        if (
            modifier is None
            or modifier["definition"].get("_class") != "modifier_trophy_collector"
            or prop is None
        ):
            continue
        # Curated engine relationship, not a VData registration: the ability's
        # replicated trophy count multiplies this property while its gold
        # modifier is active. Keep the amount and upgrades in the source data.
        prop.update(
            stat="MODIFIER_VALUE_SPRINT_SPEED_BONUS",
            runtime_count="m_iTrophyCount",
            binding_source="curated",
            modifier_keys=[modifier["record_key"]],
        )
        effect = {
            "kind": "runtime_property",
            "property_name": "StackingBonusSprintSpeed",
            **prop,
        }
        ability["stat_changes"].append(effect)
        modifier["stat_changes"].append(effect.copy())
        modifier["property_bindings"].append(
            {
                "property_name": "StackingBonusSprintSpeed",
                "source_record_key": ability["record_key"],
                "status": "resolved",
                "binding_source": "curated",
                "property": {k: v for k, v in prop.items() if k != "modifier_keys"},
            }
        )


def _modifier_enum(
    source: bytes, prefix: str, excluded: tuple[str, ...] = ()
) -> dict[str, str]:
    """Read explicit engine ordinals; never infer a missing enum value."""
    result = {}
    for name, value in re.findall(
        rf"\b({prefix}\w+)\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)\s*,?",
        source.decode("utf-8-sig"),
    ):
        if name in excluded:
            continue
        key = str(int(value, 0) if value.startswith("0x") else int(value))
        if key in result and result[key] != name:
            raise ValueError(f"ambiguous modifier enum: {key}")
        result[key] = name
    if source and not result:
        raise ValueError("modifier schema has no explicit enum values")
    return result


def modifier_value_types(source: bytes) -> dict[str, str]:
    """Map modifier value ordinals to their engine names."""
    return _modifier_enum(source, "MODIFIER_VALUE_")


def modifier_states(source: bytes) -> dict[str, str]:
    """Map state bit indices to engine names, excluding enum sentinels."""
    return _modifier_enum(
        source, "MODIFIER_STATE_", ("MODIFIER_STATE_COUNT", "MODIFIER_STATE_INVALID")
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
    roots = {
        name: definitions(document)
        for name, document in documents.items()
        if name != "generic_data.vdata"
    }
    tokens = localization_tokens(
        {k: v for k, v in localization.items() if not is_engine_source(k)}
    )
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
                "source_file": source_file,
                "definition_path": "/" + pointer_segment(name),
                "display_name": tokens.get(definition.get("m_sNameLocString", name)),
                **definition_fields(definition),
                **provenance,
            }
            for source_file in WORLD_FILES
            for name, definition in roots[source_file].items()
        ],
        {
            "misc_name": pl.String,
            "misc_id": pl.UInt32,
            "source_file": pl.String,
            "definition_path": pl.String,
            "display_name": pl.String,
            **DEFINITION,
            **PROVENANCE,
        },
    )

    modifiers = []

    def walk(value, path: str, source_file: str, owner: str, root: dict, scope: str):
        value = unwrap(value)
        if isinstance(value, dict):
            is_root = (
                path == "/" + pointer_segment(owner)
                and source_file == "modifiers.vdata"
            )
            if not is_root and (subclass_name := value.get("_my_subclass_name")):
                scope = f"{scope}/{subclass_name}"
            # This field belongs to CCitadelModifierVData. Some subclasses have
            # other class-name prefixes, but still declare explicit stat bindings.
            if (
                is_root
                or str(value.get("_class", "")).startswith(
                    ("modifier_", "citadel_modifier_")
                )
                or "m_vecAutoRegisterModifierValueFromAbilityPropertyName" in value
            ):
                name = owner if is_root else value.get("_my_subclass_name")
                if name:
                    ability = owner if source_file == "abilities.vdata" else None
                    hero = owner if source_file == "heroes.vdata" else None
                    misc = owner if source_file in WORLD_FILES else None
                    bindings = value.get(
                        "m_vecAutoRegisterModifierValueFromAbilityPropertyName", []
                    )
                    modifiers.append(
                        {
                            "modifier_name": name,
                            "modifier_id": string_token(name),
                            "qualified_modifier_name": scope,
                            "qualified_modifier_id": string_token(scope),
                            "display_name": tokens.get(
                                name, tokens.get(value.get("m_sLocalizationName"))
                            ),
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
                walk(child, f"{path}/{segment}", source_file, owner, root, scope)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}/{index}", source_file, owner, root, scope)

    for source_file, entries in roots.items():
        for name, definition in entries.items():
            walk(
                definition,
                "/" + pointer_segment(name),
                source_file,
                name,
                definition,
                name,
            )
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
    payloads: dict[str, dict] = {}
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
                "misc": (
                    "misc_name",
                    "misc_id",
                    "display_name",
                    "source_file",
                    "definition_path",
                ),
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
                "catalog": name,
                **provenance,
                "records": records,
            }
            json_metadata[f"{name}.json"] = {"records": len(records)}
    add_lookups(payloads)
    # Network enum ordinals change between builds. Preserve the names from the
    # same source revision rather than embedding ordinal tables in consumers.
    payloads["abilities"]["modifier_value_types"] = modifier_value_types(
        localization.get(MODIFIER_ENUM_FILE, b"")
    )
    payloads["abilities"]["enum_definitions"] = enum_definitions(localization)
    payloads["abilities"]["scaling_class_defaults"] = scaling_class_defaults(
        localization
    )
    payloads["modifiers"]["engine_modifier_names"] = engine_modifier_names(
        localization, payloads["modifiers"]["records"], string_token
    )
    payloads["misc"]["generic_data"] = documents["generic_data.vdata"]
    payloads["modifiers"]["modifier_states"] = modifier_states(
        localization.get(MODIFIER_STATE_ENUM_FILE, b"")
    )
    for name, payload in payloads.items():
        (output / f"{name}.json").write_text(to_json(payload) + "\n", encoding="utf-8")
    return {
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
