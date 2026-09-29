"""Engine metadata must preserve evidence without inventing gameplay effects."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catalogs import string_token
from engine_metadata import (
    SCHEMA_DIRECTORY,
    SCHEMA_FILES,
    STRING_FILES,
    engine_modifier_names,
    enum_definitions,
    scaling_class_defaults,
)

FIXTURES = Path(__file__).parent / "fixtures" / "engine"
SCALING_FILE = f"{SCHEMA_DIRECTORY}/CScaleFunctionFutureVData.h"


def engine_sources() -> dict[str, bytes]:
    return {
        path: (FIXTURES / Path(path).name).read_bytes()
        for path in (*SCHEMA_FILES, *STRING_FILES, SCALING_FILE)
    }


class EngineMetadataTests(unittest.TestCase):
    def test_enum_aliases_sentinels_and_flags_remain_explicit(self):
        enums = enum_definitions(engine_sources())
        stats = enums["EStatsType"]
        self.assertEqual(
            stats["names_by_value"]["98"], ["EStatsCount", "EStatsInvalid"]
        )
        self.assertEqual(stats["values"]["ETechPower"], 59)
        self.assertEqual(stats["underlying_type"], "uint32_t")
        self.assertEqual(
            enums["StatsUsageFlags_t"]["values"]["ConditionallyApplied"], 4
        )

    def test_unsupported_enum_expressions_do_not_silently_drop_members(self):
        path = f"{SCHEMA_DIRECTORY}/EStatsType.h"
        for entry in ("EImplicit", "EExpression = 1 << 2", "EValue = 1, EValue = 2"):
            with (
                self.subTest(entry=entry),
                self.assertRaisesRegex(ValueError, "enum member"),
            ):
                enum_definitions({path: f"enum EStatsType {{{entry},}};".encode()})

    def test_scaling_defaults_preserve_nested_data_and_class_identity(self):
        record = scaling_class_defaults(engine_sources())["CScaleFunctionFutureVData"]
        self.assertEqual(record["source_file"], SCALING_FILE)
        self.assertEqual(record["base_class"], "CScaleFunctionVData")
        self.assertEqual(record["defaults"]["m_flStatScale"], 0.75)
        self.assertEqual(record["defaults"]["m_statCurve"], {"m_spline": [0.0, 1.5]})
        self.assertIs(record["defaults"]["m_bFunctionDisabled"], False)

    def test_missing_defaults_remain_absent_and_bad_defaults_fail(self):
        source = engine_sources()[SCALING_FILE]
        self.assertEqual(
            scaling_class_defaults({SCALING_FILE: source[source.index(b"class ") :]}),
            {},
        )
        for changed in (
            source.replace(
                b'"_class": "CScaleFunctionFutureVData"', b'"_class": "WrongClass"'
            ),
            source.replace(b"0.75,", b"bad_json,"),
        ):
            with self.subTest(source=changed), self.assertRaises(ValueError):
                scaling_class_defaults({SCALING_FILE: changed})

    def test_modifier_names_have_exact_matches_locations_and_no_effects(self):
        names = engine_modifier_names(engine_sources(), [], string_token)
        self.assertEqual(len(names["records"]), 3)
        record = names["records"][
            names["indexes"]["by_name"]["modifier_citadel_pre_match_wait"]
        ]
        self.assertEqual(record["modifier_id"], 1243903559)
        self.assertEqual(record["status"], "name_only")
        self.assertEqual(record["catalog_keys"], [])
        self.assertEqual(
            record["sources"],
            [{"source_file": path, "line": 1} for path in STRING_FILES],
        )
        self.assertNotIn("stat_changes", record)
        self.assertNotIn("definition", record)
        self.assertEqual(names["id_collisions"], {})

    def test_name_collisions_keep_every_candidate_including_vdata_names(self):
        modifiers = [
            {
                "modifier_name": "modifier_catalog_only",
                "modifier_id": 7,
                "qualified_modifier_name": "ability/modifier_catalog_only",
                "qualified_modifier_id": 8,
                "record_key": "abilities.vdata#/ability/modifier",
            }
        ]
        sources = {
            STRING_FILES[0]: b"modifier_first\nmodifier_second\nmodifier_catalog_only\n"
        }
        names = engine_modifier_names(sources, modifiers, lambda _: 7)
        self.assertEqual(len(names["indexes"]["by_id"]["7"]), 3)
        self.assertEqual(
            names["id_collisions"]["7"],
            ["modifier_catalog_only", "modifier_first", "modifier_second"],
        )
        record = names["records"][names["indexes"]["by_name"]["modifier_catalog_only"]]
        self.assertEqual(record["catalog_keys"], [modifiers[0]["record_key"]])
