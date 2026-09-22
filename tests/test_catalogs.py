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

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = {"source": {"commit": "a" * 40}, "client_version": "1234"}


class CatalogTests(unittest.TestCase):
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
        self.assertEqual(indexed["upgrade_vampire", "AbilityDuration"]["value"], 0)
        self.assertEqual(
            indexed["upgrade_vampire", "TierValues"]["value_json"], '"1 2 3"'
        )
        self.assertIsNone(indexed["upgrade_vampire", "MissingValue"]["value"])

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
        self.assertEqual(self.metadata["schema_version"], 5)
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
            payload = json.loads((self.output / f"{name}.json").read_text())
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

    def test_new_source_fields_and_properties_are_preserved_automatically(self):
        self.vdata["abilities.vdata"] = (
            b'{ability_future = {_class="citadel_ability_base" m_FutureField = {nested=[1,true,"abc"]} m_mapAbilityProperties={FutureStat={m_strValue="42" m_UnknownFlag=true}}}}'
        )
        catalogs.build_catalogs(
            self.vdata, self.localization, SOURCE, self.output, parquet=True
        )
        payload = json.loads((self.output / "abilities.json").read_text())
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
        payload = json.loads((self.output / "misc.json").read_text())
        self.assertEqual(payload["catalog"], "misc")
        self.assertEqual(payload["schema_version"], 1)
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
        payload = json.loads((self.output / "modifiers.json").read_text())
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
