"""Catalog semantics and Parquet round trips using small source fixtures."""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import catalogs
from test_engine_metadata import engine_sources

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE: dict = {"source": {"commit": "a" * 40}, "client_version": "1234"}


class CatalogTests(unittest.TestCase):
    def test_engine_metadata_does_not_change_vdata_definitions(self):
        output = self.output / "engine"
        output.mkdir()
        catalogs.build_catalogs(
            self.vdata, {**self.localization, **engine_sources()}, SOURCE, output
        )
        for name in ("abilities", "heroes", "modifiers", "misc"):
            before = self.json_catalog(name)
            after = json.loads((output / f"{name}.json").read_text())
            self.assertEqual(before["records"], after["records"])
        abilities = json.loads((output / "abilities.json").read_text())
        self.assertEqual(
            abilities["enum_definitions"]["EStatsType"]["values"]["ETechPower"], 59
        )
        self.assertIn("CScaleFunctionFutureVData", abilities["scaling_class_defaults"])
        self.assertEqual(
            abilities["modifier_value_types"],
            {"918": "MODIFIER_VALUE_TECH_RANGE_PERCENT"},
        )
        modifiers = json.loads((output / "modifiers.json").read_text())
        self.assertIn(
            "1243903559", modifiers["engine_modifier_names"]["indexes"]["by_id"]
        )
        self.assertEqual(
            modifiers["modifier_states"], {"19": "MODIFIER_STATE_SPRINTING"}
        )

    def test_explicit_bindings_identify_modifiers_without_class_name_prefix(self):
        self.vdata["abilities.vdata"] = b"""{
            generic_data_type = "CCitadelAbilityVData"
            ability_test = {
                m_mapAbilityProperties = {
                    Bonus = { m_strValue = "7" m_eProvidedPropertyType = "MODIFIER_VALUE_TECH_POWER" }
                }
                m_Aura = subclass:{
                    _class = "modifier_base_aura"
                    _my_subclass_name = "aura"
                    m_modifierProvidedByAura = subclass:{
                        _class = "unusual_effect_class"
                        _my_subclass_name = "friendly"
                        m_vecAutoRegisterModifierValueFromAbilityPropertyName = ["Bonus"]
                    }
                }
            }
        }"""
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        modifiers = self.json_catalog("modifiers")
        identifier = catalogs.string_token("ability_test/aura/friendly")
        (index,) = modifiers["indexes"]["by_qualified_id"][str(identifier)]
        record = modifiers["records"][index]
        self.assertEqual(record["definition"]["_class"], "unusual_effect_class")
        (effect,) = record["stat_changes"]
        self.assertEqual(
            (effect["stat"], effect["value"]), ("MODIFIER_VALUE_TECH_POWER", 7)
        )
        ability = self.json_catalog("abilities")["records"][0]
        self.assertEqual(
            ability["properties"]["Bonus"]["modifier_keys"], [record["record_key"]]
        )

    def test_misc_preserves_generic_item_prices(self):
        from keyvalues import parse

        misc = self.json_catalog("misc")
        self.assertEqual(
            misc["generic_data"],
            parse(self.vdata["generic_data.vdata"].decode()),
        )
        self.assertEqual(misc["generic_data"]["m_nItemPricePerTier"][2], 1550)
        self.assertIn("generic_data.vdata", self.metadata["vdata_metadata"])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.vdata = {
            name: (FIXTURES / name).read_bytes() for name in catalogs.VDATA_FILES
        }
        self.localization = {"english.txt": (FIXTURES / "english.txt").read_bytes()}
        self.metadata = catalogs.build_catalogs(
            self.vdata, self.localization, SOURCE, self.output, parquet=True
        )
        self.abilities = pl.read_parquet(self.output / "abilities.parquet")
        self.properties = pl.read_parquet(self.output / "ability_properties.parquet")
        self.heroes = pl.read_parquet(self.output / "heroes.parquet")
        self.modifiers = pl.read_parquet(self.output / "modifiers.parquet")
        self.misc = pl.read_parquet(self.output / "misc.parquet")

    def test_default_export_contains_only_json_with_unchanged_definitions(self):
        output = self.output / "json-only"
        output.mkdir()
        with (
            patch.object(pl.DataFrame, "write_parquet", side_effect=AssertionError),
            patch.object(
                catalogs, "ability_property_table", side_effect=AssertionError
            ),
        ):
            metadata = catalogs.build_catalogs(
                self.vdata, self.localization, SOURCE, output
            )
        self.assertEqual(metadata["tables"], {})
        self.assertEqual(metadata["json_catalogs"], self.metadata["json_catalogs"])
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {"abilities.json", "heroes.json", "modifiers.json", "misc.json"},
        )
        for path in output.iterdir():
            self.assertEqual(path.read_bytes(), (self.output / path.name).read_bytes())

    def test_ethereal_fire_rate_binds_only_its_buff_and_keeps_source_values(self):
        self.vdata["abilities.vdata"] = b"""{
            generic_data_type = "CCitadelAbilityVData"
            ability_test = {
                m_mapAbilityProperties = {
                    BonusFireRate = { m_strValue = "37" m_eProvidedPropertyType = "MODIFIER_VALUE_FIRE_RATE" }
                }
                m_Watcher = subclass:{
                    _class = "modifier_ethereal_bullets_watcher"
                    _my_subclass_name = "watcher"
                    m_BuffModifier = subclass:{ _class = "modifier_ethereal_bullets_buff" _my_subclass_name = "buff" }
                    m_BulletDamageBuffModifier = subclass:{ _class = "modifier_ethereal_bullets_bullet_buff" _my_subclass_name = "bullet_buff" }
                }
            }
        }"""
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        ability = self.json_catalog("abilities")["records"][0]
        modifiers = self.json_catalog("modifiers")["records"]
        buff = next(
            m
            for m in modifiers
            if m["definition"]["_class"] == "modifier_ethereal_bullets_buff"
        )
        (effect,) = buff["stat_changes"]
        self.assertEqual(effect["kind"], "inferred_property")
        self.assertEqual(effect["binding_source"], "curated")
        self.assertEqual(effect["value"], 37)
        self.assertEqual(
            ability["properties"]["BonusFireRate"]["modifier_keys"],
            [buff["record_key"]],
        )
        self.assertEqual(
            ability["stat_changes"][0]["modifier_keys"], [buff["record_key"]]
        )
        self.assertTrue(
            all(
                not m["stat_changes"]
                for m in modifiers
                if m is not buff and m["source_file"] == "abilities.vdata"
            )
        )
        self.assertNotIn(
            "m_vecAutoRegisterModifierValueFromAbilityPropertyName", buff["definition"]
        )
        # An explicit registration supersedes the curated link; a different engine
        # class must not inherit it. Exercise both with the same source fixture.
        for replacement, expected_kind in (
            (
                b'm_vecAutoRegisterModifierValueFromAbilityPropertyName = ["BonusFireRate"]',
                "bound_property",
            ),
            (b"", None),
        ):
            source = self.vdata["abilities.vdata"]
            if replacement:
                source = source.replace(
                    b'_class = "modifier_ethereal_bullets_buff"',
                    b'_class = "modifier_ethereal_bullets_buff" ' + replacement,
                )
            else:
                source = source.replace(
                    b"modifier_ethereal_bullets_buff", b"modifier_unrelated"
                )
            catalogs.build_catalogs(
                {**self.vdata, "abilities.vdata": source},
                self.localization,
                SOURCE,
                self.output,
            )
            effects = [
                e
                for m in self.json_catalog("modifiers")["records"]
                if m["source_file"] == "abilities.vdata"
                for e in m["stat_changes"]
            ]
            self.assertEqual(
                [e["kind"] for e in effects], [expected_kind] if expected_kind else []
            )

    def test_curated_counter_binding_preserves_source_values(self):
        payloads = {
            name: self.json_catalog(name)
            for name in ("abilities", "heroes", "modifiers", "misc")
        }
        ability = payloads["abilities"]["records"][0]
        ability["definition"]["_class"] = "upgrade_trophy_collector"
        ability["definition"].setdefault("m_mapAbilityProperties", {})[
            "StackingBonusSprintSpeed"
        ] = {"m_strValue": "0.27m"}
        modifier: dict = {
            "source_file": "abilities.vdata",
            "definition_path": ability["definition_path"] + "/m_GoldModifier",
            "definition": {"_class": "modifier_trophy_collector"},
            "modifier_name": "test_counter",
            "modifier_id": 123,
            "qualified_modifier_name": "test/counter",
            "qualified_modifier_id": 456,
        }
        payloads["modifiers"]["records"].append(modifier)
        original = json.dumps(ability["definition"], sort_keys=True)
        catalogs.add_lookups(payloads)
        effect = next(
            row for row in modifier["stat_changes"] if row["kind"] == "runtime_property"
        )
        self.assertEqual(effect["raw_value"], "0.27m")
        self.assertEqual(effect["runtime_count"], "m_iTrophyCount")
        self.assertEqual(effect["stat"], "MODIFIER_VALUE_SPRINT_SPEED_BONUS")
        self.assertEqual(effect["binding_source"], "curated")
        self.assertEqual(effect["modifier_keys"], [modifier["record_key"]])
        self.assertIn(effect, ability["stat_changes"])
        self.assertEqual(json.dumps(ability["definition"], sort_keys=True), original)
        # A changed engine class must not inherit an unrelated counter rule.
        modifier["definition"]["_class"] = "different_modifier"
        catalogs.add_lookups(payloads)
        self.assertFalse(
            any(row["kind"] == "runtime_property" for row in modifier["stat_changes"])
        )

    def test_bloodscent_counters_are_owner_only_and_preserve_catalog_amounts(self):
        payloads = {
            name: self.json_catalog(name)
            for name in ("abilities", "heroes", "modifiers", "misc")
        }
        ability = payloads["abilities"]["records"][0]
        ability["definition"]["_class"] = "ability_drifter_hunger"
        ability["definition"]["m_mapAbilityProperties"] = {
            "WeaponDmgPerIsolationKill": {
                "m_strValue": "7.5",
                "m_eProvidedPropertyType": "MODIFIER_VALUE_WEAPON_DAMAGE_INCREASE",
                "m_eStatsUsageFlags": "ConditionallyApplied",
            },
            "IsolationAssistPercentValue": {"m_strValue": "40"},
        }
        original = json.dumps(ability["definition"], sort_keys=True)
        catalogs.add_lookups(payloads)
        effect = next(
            row
            for row in ability["stat_changes"]
            if row.get("property_name") == "WeaponDmgPerIsolationKill"
        )
        self.assertEqual(effect["value"], 7.5)
        self.assertEqual(effect["binding_source"], "curated")
        self.assertEqual(effect["runtime_counts"][0], {"field": "m_nKillsEarned"})
        self.assertEqual(effect["runtime_counts"][1]["field"], "m_nAssistsEarned")
        self.assertEqual(effect["runtime_counts"][1]["percent"]["value"], 40)
        self.assertEqual(effect["modifier_keys"], [])
        self.assertFalse(
            any(
                "runtime_counts" in row
                for modifier in payloads["modifiers"]["records"]
                for row in modifier["stat_changes"]
            )
        )
        self.assertEqual(json.dumps(ability["definition"], sort_keys=True), original)
        ability["definition"]["_class"] = "unrelated_ability"
        catalogs.add_lookups(payloads)
        self.assertFalse(
            any("runtime_counts" in row for row in ability["stat_changes"])
        )

    def ability(self, name):
        return self.abilities.filter(pl.col("ability_name") == name).row(0, named=True)

    def test_ids_match_boon_and_identity_is_not_a_display_name(self):
        # Existing Boon name-table IDs; cover each Murmur tail length, too.
        for name, expected in {
            "upgrade_vampire": 499683006,
            "upgrade_surging_power": 1055679805,
            "modifier_intrinsic_base": 2312238751,
            "": 3050872623,
            "a": 516911585,
            "ab": 3892251786,
            "abc": 3603893526,
            "abcd": 3022530028,
        }.items():
            self.assertEqual(catalogs.string_token(name), expected)
        row = self.ability("upgrade_surging_power")
        self.assertEqual(row["ability_id"], 1055679805)
        self.assertEqual(row["display_name"], "Vampiric Burst")
        self.assertTrue(row["is_item"])  # Specialized class, not citadel_item.
        self.assertEqual(self.abilities.schema["ability_id"], pl.UInt32)
        self.assertTrue(row["disabled"])
        self.assertFalse(self.ability("upgrade_vampire")["disabled"])
        self.assertIsNone(self.ability("ability_unlocalized")["display_name"])
        self.assertIsNone(self.ability("ability_unlocalized")["disabled"])
        self.assertTrue(self.ability("item_base")["is_template"])

    def test_modifier_localization_only_fills_missing_display_names(self):
        self.localization = {
            "english.txt": b'"lang" { "Tokens" {'
            b'"modifier_glitch_debuff" "Cursed!" '
            b'"spirit_snare" "Spirit Snare" '
            b'"modifier_health_nova" "Healing Nova" '
            b'"modifier_empty" "" } }'
        }
        expected = {
            "modifier_glitch_debuff": "Cursed!",
            "modifier_healing_Nova_active": "Healing Nova",
            "modifier_unknown_token": None,
            "modifier_no_token": None,
            "modifier_empty": "",
        }
        for name, token in (
            ("modifier_glitch_debuff", "spirit_snare"),
            ("modifier_healing_Nova_active", "modifier_health_nova"),
            ("modifier_unknown_token", "missing"),
            ("modifier_no_token", None),
            ("modifier_empty", "spirit_snare"),
        ):
            with self.subTest(name=name):
                field = (
                    f"m_sLocalizationName = {json.dumps(token)}"
                    if token is not None
                    else ""
                )
                self.vdata["modifiers.vdata"] = (
                    '{ generic_data_type = "CModifierVData" modifier_test = {'
                    '_class = "modifier_test" m_Child = subclass:{'
                    '_class = "modifier_base" '
                    f'_my_subclass_name = "{name}" {field}'
                    "} } }"
                ).encode()
                catalogs.build_catalogs(
                    self.vdata, self.localization, SOURCE, self.output, parquet=True
                )
                row = next(
                    r
                    for r in self.json_catalog("modifiers")["records"]
                    if r["modifier_name"] == name
                )
                self.assertEqual(row["display_name"], expected[name])
                self.assertEqual(row["modifier_id"], catalogs.string_token(name))
                self.assertEqual(row["definition"].get("m_sLocalizationName"), token)
                parquet = pl.read_parquet(self.output / "modifiers.parquet")
                self.assertEqual(
                    parquet.filter(pl.col("modifier_name") == name)
                    .get_column("display_name")
                    .item(),
                    expected[name],
                )

    def test_all_properties_survive_without_conflating_absence_and_zero(self):
        row = self.ability("upgrade_vampire")
        self.assertEqual(row["stat_AbilityDuration"], 0.0)
        self.assertEqual(row["stat_BulletLifestealPercent"], 13.0)
        for key in (
            "ActiveBonusLifesteal",
            "TierValues",
            "Expression",
            "NotANumber",
            "MissingValue",
        ):
            self.assertIsNone(row[f"stat_{key}"])
        properties = {p["name"]: p for p in row["properties"]}
        self.assertEqual(properties["TierValues"]["value_json"], '"1 2 3"')
        self.assertIsNone(properties["MissingValue"]["value_json"])
        definition = json.loads(properties["BulletLifestealPercent"]["definition_json"])
        self.assertEqual(
            definition["m_subclassScaleFunction"]["$value"]["m_eSpecificStatScaleType"],
            "EHealingOutput",
        )
        active = self.ability("upgrade_surging_power")
        self.assertEqual(active["stat_ActiveBonusLifesteal"], 70)
        self.assertEqual(active["stat_BulletLifestealPercent"], 13)
        self.assertEqual(
            json.loads(active["upgrades_json"])[0]["m_vecPropertyUpgrades"][0][
                "m_strBonus"
            ],
            "16",
        )
        self.assertEqual(row["source_commit"], SOURCE["source"]["commit"])

    def test_property_table_has_one_row_per_defined_property(self):
        self.assertEqual(self.properties.height, 9)
        self.assertEqual(
            self.properties.select("ability_id", "property_name").n_unique(), 9
        )
        self.assertEqual(self.properties.schema["ability_id"], pl.UInt32)
        self.assertNotIn("ability_unlocalized", self.properties["ability_name"])
        self.assertNotIn("UnknownProperty", self.properties["property_name"])
        indexed = {
            (row["ability_name"], row["property_name"]): row
            for row in self.properties.to_dicts()
        }
        for ability in self.abilities.to_dicts():
            for prop in ability["properties"]:
                row = indexed[ability["ability_name"], prop["name"]]
                for key in (
                    "ability_id",
                    "display_name",
                    "is_item",
                    "is_template",
                    "disabled",
                    "source_commit",
                    "client_version",
                ):
                    self.assertEqual(row[key], ability[key])
                for key, value in prop.items():
                    self.assertEqual(
                        row["property_name" if key == "name" else key], value
                    )

    def test_property_scaling_preserves_explicit_zero_and_multiple_inputs(self):
        indexed = {
            (row["ability_name"], row["property_name"]): row
            for row in self.properties.to_dicts()
        }
        lifesteal = indexed["upgrade_vampire", "BulletLifestealPercent"]
        self.assertEqual(
            lifesteal["provided_property"], "MODIFIER_VALUE_BULLET_LIFESTEAL"
        )
        self.assertEqual(lifesteal["scale_function"], "scale_function_single_stat")
        self.assertEqual(lifesteal["scaling_stat"], "EHealingOutput")
        self.assertEqual(lifesteal["scaling_coefficient"], 0)
        self.assertEqual(lifesteal["street_brawl_scaling_coefficient"], 0.25)
        self.assertFalse(lifesteal["scaling_disabled"])
        self.assertIsNone(lifesteal["scaling_stats"])
        scale = json.loads(lifesteal["scale_function_json"])
        self.assertEqual(scale["$type"], "subclass")
        self.assertEqual(scale["$value"]["m_sFutureScaleField"], "preserve me")
        duration = indexed["upgrade_surging_power", "AbilityDuration"]
        self.assertEqual(
            duration["scaling_stats"], ["EChannelDuration", "ETechDuration"]
        )
        self.assertEqual(duration["usage_flags"], "ConditionallyApplied")
        self.assertEqual(duration["display_units"], "EDisplayUnit_Seconds")
        for key in ("scaling_stat", "scaling_coefficient", "scaling_disabled"):
            self.assertIsNone(duration[key])
        missing = indexed["upgrade_vampire", "AbilityDuration"]
        for key in (
            "scale_function",
            "scaling_stat",
            "scaling_stats",
            "scaling_coefficient",
            "scaling_disabled",
            "scale_function_json",
        ):
            self.assertIsNone(missing[key])

    def test_property_modifier_bindings_keep_the_owning_ability(self):
        modifiers = {
            (row["source_file"], row["definition_path"]): row
            for row in self.modifiers.to_dicts()
        }
        bound_rows = [
            row for row in self.properties.to_dicts() if row["modifier_bindings"]
        ]
        self.assertEqual(len(bound_rows), 2)
        for row in bound_rows:
            self.assertEqual(len(row["modifier_bindings"]), 1)
            binding = row["modifier_bindings"][0]
            modifier = modifiers[binding["source_file"], binding["definition_path"]]
            self.assertEqual(modifier["ability_id"], row["ability_id"])
            self.assertEqual(modifier["modifier_name"], binding["modifier_name"])
            self.assertEqual(modifier["modifier_id"], binding["modifier_id"])
            self.assertIn(
                row["property_name"], [p["name"] for p in modifier["bound_properties"]]
            )

    def test_empty_property_catalog_still_has_a_typed_schema(self):
        self.vdata["abilities.vdata"] = (
            b'{ability_empty = {_class="citadel_ability_base"}}'
        )
        metadata = catalogs.build_catalogs(
            self.vdata, self.localization, SOURCE, self.output, parquet=True
        )
        empty = pl.read_parquet(self.output / "ability_properties.parquet")
        self.assertEqual(empty.height, 0)
        self.assertEqual(empty.schema, self.properties.schema)
        self.assertEqual(metadata["tables"]["ability_properties.parquet"]["rows"], 0)

    def test_heroes_use_source_ids_and_keep_growth_and_scaling(self):
        row = self.heroes.row(0, named=True)
        self.assertEqual((row["hero_id"], row["display_name"]), (11, "Dynamo"))
        self.assertEqual(row["base_EMaxHealth"], 830)
        self.assertEqual(row["base_ETechArmorDamageReduction"], 0)
        self.assertEqual(row["level_MODIFIER_VALUE_BULLET_ARMOR_DAMAGE_RESIST"], 0.625)
        self.assertEqual(
            json.loads(row["bound_abilities_json"])["ESlot_Signature_1"],
            "ability_unlocalized",
        )
        self.assertEqual(
            json.loads(row["scaling_stats_json"])["ETechArmorDamageReduction"][
                "flScale"
            ],
            0.1,
        )

    def test_modifier_contexts_keep_repeated_ids_and_bound_values(self):
        instances = self.modifiers.filter(
            pl.col("modifier_name") == "modifier_intrinsic_base"
        )
        self.assertEqual(instances.height, 2)
        self.assertEqual(instances["modifier_id"].n_unique(), 1)
        self.assertEqual(instances["definition_path"].n_unique(), 2)
        by_ability = {row["ability_name"]: row for row in instances.to_dicts()}
        self.assertEqual(
            by_ability["upgrade_vampire"]["bound_properties"][0]["value"], 13
        )
        active = by_ability["upgrade_surging_power"]
        properties = {p["name"]: p for p in active["bound_properties"]}
        self.assertEqual(properties["ActiveBonusLifesteal"]["value"], 70)
        self.assertIsNone(properties["UnknownProperty"]["definition_json"])
        self.assertEqual(active["stat_m_flDuration"], 5)
        self.assertFalse(active["is_hidden"])
        shared = self.modifiers.filter(pl.col("source_file") == "modifiers.vdata")
        self.assertEqual(shared.height, 2)
        self.assertTrue(shared["ability_id"].is_null().all())
        self.assertEqual(
            self.modifiers.select("source_file", "definition_path").n_unique(),
            self.modifiers.height,
        )

    def test_manifest_describes_the_written_schema_and_retains_root_metadata(self):
        for name, metadata in self.metadata["tables"].items():
            table = pl.read_parquet(self.output / name)
            self.assertEqual(metadata["rows"], table.height)
            self.assertEqual(
                metadata["columns"],
                {name: str(dtype) for name, dtype in table.schema.items()},
            )
        includes = json.loads(
            self.metadata["vdata_metadata"]["abilities.vdata"]["_include"]
        )
        self.assertEqual(includes[0]["$type"], "resource_name")

    def test_json_catalogs_preserve_full_definitions_and_context(self):
        for name, table, identity in (
            ("abilities", self.abilities, ("ability_id",)),
            ("heroes", self.heroes, ("hero_id",)),
            ("misc", self.misc, ("misc_id",)),
            ("modifiers", self.modifiers, ("source_file", "definition_path")),
        ):
            payload = self.json_catalog(name)
            self.assertEqual(payload["source_commit"], SOURCE["source"]["commit"])
            indexed = {
                tuple(row[key] for key in identity): row for row in table.to_dicts()
            }
            self.assertEqual(len(payload["records"]), table.height)
            for record in payload["records"]:
                row = indexed[tuple(record[key] for key in identity)]
                self.assertEqual(
                    record["definition"], json.loads(row["definition_json"])
                )

    def json_catalog(self, name):
        return json.loads((self.output / f"{name}.json").read_text())

    def test_lookup_indexes_round_trip_every_record_and_reference(self):
        payloads = {
            name: self.json_catalog(name)
            for name in ("abilities", "heroes", "modifiers", "misc")
        }
        records = {
            r["record_key"]: r
            for payload in payloads.values()
            for r in payload["records"]
        }
        prefixes = {
            "abilities": "ability",
            "heroes": "hero",
            "modifiers": "modifier",
            "misc": "misc",
        }
        for name, payload in payloads.items():
            fields = {
                "by_id": f"{prefixes[name]}_id",
                "by_name": f"{prefixes[name]}_name",
            }
            if name == "modifiers":
                fields.update(
                    by_qualified_id="qualified_modifier_id",
                    by_qualified_name="qualified_modifier_name",
                )
            self.assertEqual(len(payload["indexes"]["by_key"]), len(payload["records"]))
            for index, record in enumerate(payload["records"]):
                self.assertEqual(
                    payload["indexes"]["by_key"][record["record_key"]], index
                )
                for lookup, field in fields.items():
                    self.assertIn(index, payload["indexes"][lookup][str(record[field])])
                for key in record["modifier_keys"]:
                    self.assertIn(key, records)
                for change in record["stat_changes"]:
                    source = records[change["source_record_key"]]
                    self.assertTrue(
                        change["definition_path"].startswith(
                            source["definition_path"] + "/"
                        )
                    )

    def test_json_property_bindings_preserve_values_scaling_and_unknowns(self):
        abilities = self.json_catalog("abilities")
        ability = abilities["records"][abilities["indexes"]["by_id"]["499683006"][0]]
        prop = ability["properties"]["BulletLifestealPercent"]
        self.assertEqual(prop["value"], 13)
        self.assertEqual(prop["raw_value"], "13")
        self.assertEqual(prop["stat"], "MODIFIER_VALUE_BULLET_LIFESTEAL")
        self.assertEqual(prop["scaling"]["$value"]["m_flStatScale"], 0)
        self.assertEqual(prop["scaling"]["$value"]["m_bFunctionDisabled"], "false")
        modifiers = self.json_catalog("modifiers")
        modifier = modifiers["records"][
            modifiers["indexes"]["by_key"][prop["modifier_keys"][0]]
        ]
        binding = modifier["property_bindings"][0]
        self.assertEqual(binding["status"], "resolved")
        self.assertEqual(binding["source_record_key"], ability["record_key"])
        change = modifier["stat_changes"][0]
        self.assertEqual(change["kind"], "bound_property")
        self.assertEqual(change["value"], 13)
        self.assertEqual(change["scaling"], prop["scaling"])
        self.assertEqual(
            [c["property_name"] for c in ability["stat_changes"]],
            ["BulletLifestealPercent"],
        )
        for name, raw in [
            ("TierValues", "1 2 3"),
            ("Expression", "damage * 0.5"),
            ("MissingValue", None),
        ]:
            self.assertIsNone(ability["properties"][name]["value"])
            self.assertEqual(ability["properties"][name]["raw_value"], raw)
        self.assertEqual(ability["properties"]["AbilityDuration"]["value"], 0)
        active = next(
            r
            for r in modifiers["records"]
            if r["ability_name"] == "upgrade_surging_power"
        )
        unresolved = next(
            b
            for b in active["property_bindings"]
            if b["property_name"] == "UnknownProperty"
        )
        self.assertEqual(unresolved["status"], "unresolved")
        self.assertIsNone(unresolved["property"])
        # A binding can exist without a declared MODIFIER_VALUE mapping.
        self.assertEqual(active["property_bindings"][0]["status"], "resolved")
        self.assertEqual(active["stat_changes"], [])

    def test_modifier_indexes_keep_shared_names_and_duplicate_qualified_ids(self):
        self.vdata["abilities.vdata"] = self.vdata["abilities.vdata"].replace(
            b"m_AutoIntrinsicModifiers = [subclass:{",
            b'm_OtherModifier = subclass:{ _class="modifier_intrinsic_base" _my_subclass_name="modifier_intrinsic_base" } m_AutoIntrinsicModifiers = [subclass:{',
            1,
        )
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        payload = self.json_catalog("modifiers")
        indexes = payload["indexes"]
        qualified = "upgrade_vampire/modifier_intrinsic_base"
        candidates = indexes["by_qualified_id"][str(catalogs.string_token(qualified))]
        self.assertEqual(candidates, indexes["by_qualified_name"][qualified])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            len({payload["records"][i]["record_key"] for i in candidates}), 2
        )
        shared = indexes["by_id"][str(catalogs.string_token("modifier_intrinsic_base"))]
        self.assertEqual(len(shared), 3)
        self.assertEqual(
            {payload["records"][i]["ability_name"] for i in shared},
            {"upgrade_vampire", "upgrade_surging_power"},
        )

    def test_declared_powerup_values_keep_ranges_and_permanent_values_separate(self):
        payload = self.json_catalog("modifiers")
        gun = next(
            r for r in payload["records"] if r["misc_name"] == "gun_powerup_pickup"
        )
        changes = {c["stat"]: c for c in gun["stat_changes"]}
        fire_rate = changes["MODIFIER_VALUE_FIRE_RATE"]
        self.assertEqual((fire_rate["value_min"], fire_rate["value_max"]), (12, 35))
        self.assertIsNone(fire_rate["value"])
        permanent = next(
            r
            for r in payload["records"]
            if r["misc_name"] == "ammo_permanent_pickup_lv2"
        )
        self.assertEqual(permanent["stat_changes"][0]["value"], 5)
        self.assertEqual(
            permanent["stat_changes"][0]["stat"],
            "MODIFIER_VALUE_AMMO_CLIP_SIZE_PERCENT",
        )
        self.assertIsNone(permanent["stat_changes"][0]["value_min"])
        self.assertEqual(permanent["stat_changes"][0]["definition"]["m_value"], 5)

    def test_record_paths_escape_names_without_changing_hash_inputs(self):
        self.vdata["abilities.vdata"] = self.vdata["abilities.vdata"].replace(
            b"upgrade_vampire", b'"ability/with~name"'
        )
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        payload = self.json_catalog("abilities")
        record = payload["records"][
            payload["indexes"]["by_name"]["ability/with~name"][0]
        ]
        self.assertEqual(record["record_key"], "abilities.vdata#/ability~1with~0name")
        self.assertEqual(
            record["ability_id"], catalogs.string_token("ability/with~name")
        )
        self.assertTrue(
            record["modifier_keys"][0].startswith(record["record_key"] + "/")
        )

    def test_new_source_fields_and_properties_are_preserved_automatically(self):
        self.vdata["abilities.vdata"] = (
            b'{ability_future = {_class="citadel_ability_base" m_FutureField = {nested=[1,true,"abc"]} m_mapAbilityProperties={FutureStat={m_strValue="42" m_UnknownFlag=true}}}}'
        )
        catalogs.build_catalogs(
            self.vdata, self.localization, SOURCE, self.output, parquet=True
        )
        payload = self.json_catalog("abilities")
        definition = payload["records"][0]["definition"]
        self.assertEqual(definition["m_FutureField"], {"nested": [1, True, "abc"]})
        self.assertTrue(
            definition["m_mapAbilityProperties"]["FutureStat"]["m_UnknownFlag"]
        )
        self.assertEqual(
            pl.read_parquet(self.output / "abilities.parquet")[
                "stat_FutureStat"
            ].item(),
            42,
        )
        props = pl.read_parquet(self.output / "ability_properties.parquet")
        self.assertEqual(props["property_name"].to_list(), ["FutureStat"])
        self.assertEqual(props["value"].item(), 42)

    def test_conflicting_localization_and_id_collisions_fail(self):
        conflict = b'"lang" {"Tokens" {"upgrade_vampire" "Conflicting name"}}'
        with self.assertRaisesRegex(ValueError, "conflicting English localization"):
            catalogs.localization_tokens(
                {**self.localization, "conflict.txt": conflict}
            )
        with (
            patch.object(catalogs, "string_token", return_value=1),
            self.assertRaisesRegex(ValueError, "ID collision"),
        ):
            catalogs.build_catalogs(
                self.vdata, self.localization, SOURCE, self.output, parquet=True
            )

    def test_missing_hero_id_is_not_silently_replaced_with_a_hash(self):
        self.vdata["heroes.vdata"] = b'{hero_unknown = {_class="CitadelHeroData_t"}}'
        with self.assertRaisesRegex(ValueError, "missing ID"):
            catalogs.build_catalogs(
                self.vdata, self.localization, SOURCE, self.output, parquet=True
            )

    def test_numeric_literals_and_flags_have_explicit_conversion(self):
        for value in (
            True,
            False,
            "",
            "NaN",
            "Infinity",
            "1 2 3",
            "5%",
            "health * 2",
            math.inf,
            math.nan,
            None,
            [1, 2],
            {},
        ):
            with self.subTest(value=value):
                self.assertIsNone(catalogs.number(value))
        self.assertEqual(catalogs.number("  -1.2e2  "), -120)
        self.assertEqual(catalogs.number("0"), 0)
        self.assertFalse(catalogs.boolean("false"))
        self.assertTrue(catalogs.boolean("1"))
        with self.assertRaisesRegex(ValueError, "unsupported boolean"):
            catalogs.boolean("maybe")

    def test_misc_preserves_temporary_and_permanent_pickup_definitions(self):
        payload = self.json_catalog("misc")
        self.assertEqual(payload["catalog"], "misc")
        self.assertEqual(payload["client_version"], "1234")
        records = {r["misc_name"]: r for r in payload["records"]}
        gun = records["gun_powerup_pickup"]
        self.assertEqual((gun["misc_id"], gun["display_name"]), (201785745, "Gun"))
        definition = gun["definition"]
        self.assertEqual(definition["_base"], "citadel_punchable_powerup_base")
        self.assertEqual(definition["m_sModifer"]["$type"], "subclass")
        modifier = definition["m_sModifer"]["$value"]
        self.assertEqual(modifier["m_flDuration"], 160)
        self.assertEqual((modifier["m_flTimeMin"], modifier["m_flTimeMax"]), (5, 40))
        self.assertEqual(
            modifier["m_vecModifierValues"],
            [
                {
                    "m_eModifierValue": "MODIFIER_VALUE_FIRE_RATE",
                    "m_valueMin": 12.0,
                    "m_valueMax": 35.0,
                },
                {
                    "m_eModifierValue": "MODIFIER_VALUE_AMMO_CLIP_SIZE_PERCENT",
                    "m_valueMin": 35.0,
                    "m_valueMax": 70.0,
                },
            ],
        )
        ammo = records["ammo_permanent_pickup_lv2"]
        self.assertEqual(ammo["display_name"], "+5% Max Ammo")
        self.assertNotIn("m_flDuration", ammo["definition"]["m_sModifer"]["$value"])
        world = records["world_spawner"]
        self.assertIsNone(world["display_name"])
        self.assertNotIn("_class", world["definition"])
        self.assertEqual(
            world["definition"]["m_FutureField"]["nested"], [0, False, "preserve me"]
        )
        self.assertEqual(world["definition"]["m_flRespawnTime"], -1)
        self.assertEqual(world["definition"]["m_Particle"]["$type"], "resource_name")
        self.assertIn("misc.vdata", self.metadata["vdata_metadata"])

    def test_misc_modifiers_keep_pickup_context_and_recorded_qualified_ids(self):
        payload = self.json_catalog("modifiers")
        rows = {
            r["misc_name"]: r
            for r in payload["records"]
            if r["source_file"] == "misc.vdata"
        }
        for owner, identifier in {
            "gun_powerup_pickup": 2161948557,
            "ammo_permanent_pickup": 2889034835,
            "ammo_permanent_pickup_lv2": 2592582493,
        }.items():
            row = rows[owner]
            self.assertEqual(row["qualified_modifier_id"], identifier)
            self.assertEqual(
                row["qualified_modifier_name"], f"{owner}/{row['modifier_name']}"
            )
            self.assertEqual(row["misc_id"], catalogs.string_token(owner))
            self.assertEqual(row["definition_path"], f"/{owner}/m_sModifer")
            self.assertIsNone(row["ability_id"])
            self.assertIsNone(row["hero_id"])
        self.assertEqual(
            rows["ammo_permanent_pickup"]["modifier_id"],
            rows["ammo_permanent_pickup_lv2"]["modifier_id"],
        )
        self.assertNotEqual(
            rows["ammo_permanent_pickup"]["qualified_modifier_id"],
            rows["ammo_permanent_pickup_lv2"]["qualified_modifier_id"],
        )

    def test_npc_aura_lookup_keeps_source_context_and_declared_resistances(self):
        misc = self.json_catalog("misc")
        owner = misc["records"][misc["indexes"]["by_name"]["npc_boss_tier2_weak"][0]]
        self.assertEqual(owner["source_file"], "npc_units.vdata")
        self.assertEqual(owner["record_key"], "npc_units.vdata#/npc_boss_tier2_weak")
        modifiers = self.json_catalog("modifiers")
        row = modifiers["records"][
            modifiers["indexes"]["by_qualified_id"]["1633171260"][0]
        ]
        self.assertEqual(
            row["qualified_modifier_name"],
            "npc_boss_tier2_weak/friendly_aura/target_near_walker",
        )
        self.assertEqual(row["misc_id"], owner["misc_id"])
        self.assertIn(row["record_key"], owner["modifier_keys"])
        self.assertIsNone(row["ability_id"])
        self.assertEqual(
            {c["stat"]: c["value"] for c in row["stat_changes"]},
            {
                "MODIFIER_VALUE_TECH_RESIST": 15,
                "MODIFIER_VALUE_BULLET_ARMOR_DAMAGE_RESIST": 15,
            },
        )
        for effect in row["stat_changes"]:
            self.assertEqual(effect["source_record_key"], row["record_key"])
            self.assertTrue(
                effect["definition_path"].startswith(
                    row["definition_path"] + "/m_vecScriptValues/"
                )
            )
        aura = owner["definition"]["m_FriendlyAuraModifier"]["$value"]
        self.assertEqual(aura["m_iAuraSearchType"], "CITADEL_UNIT_TARGET_HERO_FRIENDLY")
        self.assertEqual(aura["m_flAuraRadius"], 1102.36)
        self.assertIn("npc_units.vdata", self.metadata["vdata_metadata"])

        self.vdata["npc_units.vdata"] = self.vdata["npc_units.vdata"].replace(
            b"m_value = 15", b"m_value = 23"
        )
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        changed = self.json_catalog("modifiers")
        row = changed["records"][changed["indexes"]["by_qualified_id"]["1633171260"][0]]
        self.assertEqual([c["value"] for c in row["stat_changes"]], [23, 23])

    def test_citadel_modifier_prefix_keeps_nested_property_bindings(self):
        self.vdata["abilities.vdata"] = b"""{
            test_tether = {
                m_mapAbilityProperties = {
                    BonusFireRate = {
                        m_strValue="10"
                        m_eProvidedPropertyType="MODIFIER_VALUE_FIRE_RATE"
                        m_eStatsUsageFlags="ConditionallyApplied"
                    }
                }
                m_TetherModifier = subclass:{
                    _class="citadel_modifier_test_tether"
                    _my_subclass_name="tether"
                    m_BuffModifier = subclass:{
                        _class="citadel_modifier_test_receiver"
                        _my_subclass_name="receiver"
                        m_vecAutoRegisterModifierValueFromAbilityPropertyName=["BonusFireRate"]
                    }
                }
            }
        }"""
        catalogs.build_catalogs(self.vdata, self.localization, SOURCE, self.output)
        modifiers = self.json_catalog("modifiers")
        row = modifiers["records"][
            modifiers["indexes"]["by_qualified_name"]["test_tether/tether/receiver"][0]
        ]
        ability = self.json_catalog("abilities")["records"][0]
        self.assertEqual(row["ability_id"], ability["ability_id"])
        self.assertEqual(
            row["qualified_modifier_id"],
            catalogs.string_token("test_tether/tether/receiver"),
        )
        self.assertEqual(row["stat_changes"][0]["stat"], "MODIFIER_VALUE_FIRE_RATE")
        self.assertEqual(row["stat_changes"][0]["value"], 10)
        self.assertEqual(
            ability["properties"]["BonusFireRate"]["modifier_keys"], [row["record_key"]]
        )

    def test_nested_modifier_hashes_include_all_subclass_ancestors(self):
        self.vdata["misc.vdata"] = b"""{
            citadel_koth_cashin = {
                m_AuraModifier = subclass:{
                    _class="modifier_base_aura_cylinder"
                    _my_subclass_name="modifier_aura_idol_cashin"
                    m_modifierProvidedByAura = subclass:{
                        _class="modifier_idol_cashin_timer"
                        _my_subclass_name="target_near_idol_cashin"
                    }
                }
                m_OtherAura = subclass:{
                    _class="modifier_base_aura_cylinder"
                    _my_subclass_name="other_aura"
                    m_modifierProvidedByAura = subclass:{
                        _class="modifier_idol_cashin_timer"
                        _my_subclass_name="target_near_idol_cashin"
                    }
                }
            }
        }"""
        catalogs.build_catalogs(
            self.vdata, self.localization, SOURCE, self.output, parquet=True
        )
        payload = self.json_catalog("modifiers")
        row = payload["records"][payload["indexes"]["by_qualified_id"]["3930103894"][0]]
        self.assertEqual(
            row["qualified_modifier_name"],
            "citadel_koth_cashin/modifier_aura_idol_cashin/target_near_idol_cashin",
        )
        self.assertEqual(
            row["definition_path"],
            "/citadel_koth_cashin/m_AuraModifier/m_modifierProvidedByAura",
        )
        shared = payload["indexes"]["by_id"][
            str(catalogs.string_token("target_near_idol_cashin"))
        ]
        self.assertEqual(len(shared), 2)
        self.assertEqual(
            len({payload["records"][i]["qualified_modifier_id"] for i in shared}), 2
        )
        self.assertNotIn(
            str(catalogs.string_token("citadel_koth_cashin/target_near_idol_cashin")),
            payload["indexes"]["by_qualified_id"],
        )
        table = pl.read_parquet(self.output / "modifiers.parquet")
        self.assertEqual(
            table.filter(pl.col("qualified_modifier_id") == 3930103894)[
                "qualified_modifier_name"
            ].item(),
            row["qualified_modifier_name"],
        )


class PropertyMetadataTests(unittest.TestCase):
    def test_property_filter_is_retained(self):
        from catalogs import json_properties

        record = {
            "record_key": "abilities.vdata#/invented",
            "definition_path": "/invented",
            "definition": {
                "m_mapAbilityProperties": {
                    "Range": {
                        "m_strValue": "17",
                        "m_eProvidedPropertyType": "MODIFIER_VALUE_TECH_RANGE_PERCENT",
                        "m_eApplyFilter": "EApplyFilter_OnlyIfImbued",
                    },
                    "Cooldown": {
                        "m_strValue": "9",
                        "m_eApplyFilter": "EApplyFilter_OnlyIfHasCharges",
                    },
                }
            },
        }
        result = json_properties(record)
        self.assertEqual(result["Range"]["apply_filter"], "EApplyFilter_OnlyIfImbued")
        self.assertEqual(
            result["Cooldown"]["apply_filter"], "EApplyFilter_OnlyIfHasCharges"
        )

    def test_modifier_enums_preserve_ordinals_and_reject_ambiguity(self):
        cases = (
            (
                catalogs.modifier_value_types,
                "MODIFIER_VALUE",
                {"MODIFIER_VALUE_A": 918, "MODIFIER_VALUE_B": 0xABC},
                "",
            ),
            (
                catalogs.modifier_states,
                "MODIFIER_STATE",
                {"MODIFIER_STATE_A": 7, "MODIFIER_STATE_B": 0x22},
                "MODIFIER_STATE_COUNT = 35, MODIFIER_STATE_INVALID = 65535,",
            ),
        )
        for convert, prefix, values, sentinels in cases:
            with self.subTest(prefix=prefix):
                members = ",".join(
                    f"{name} = {value:#x}" for name, value in values.items()
                )
                self.assertEqual(
                    convert(f"{members},{sentinels}".encode()),
                    {str(value): name for name, value in values.items()},
                )
                self.assertEqual(convert(b""), {})
                for invalid in (
                    b"unexpected schema",
                    f"{prefix}_A = 5, {prefix}_B = 5,".encode(),
                ):
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        convert(invalid)
