"""Source2Viewer image tests use a synthetic install and mocked tool output."""

import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import images as image_assets

SOURCE: dict = {
    "client_version": "1234",
    "source_revision": "5678",
    "source": {"commit": "a" * 40},
}
MAP = image_assets.MAP_IMAGES[0]
ABILITY = "panorama/images/hud/abilities/test_psd.vtex_c"
ITEM = "panorama/images/upgrades/test_psd.vtex_c"
SHOP = "panorama/images/items/test_psd.vtex_c"
HERO = "panorama/images/heroes/test_mm_psd.vtex_c"
STAT = "panorama/images/icons/properties/fire_rate.vsvg_c"


def png() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (3, 2), (10, 20, 30, 128)).save(output, "PNG")
    return output.getvalue()


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.game = self.directory / "game-install"
        (self.game / image_assets.STEAM_INF).parent.mkdir(parents=True)
        (self.game / image_assets.STEAM_INF).write_text(
            "ClientVersion=1234\nSourceRevision=5678\n"
        )
        (self.game / image_assets.PAK).write_bytes(b"synthetic VPK index")
        self.tool = self.directory / "Source2Viewer-CLI"
        self.tool.write_bytes(b"synthetic extractor")
        self.catalog = {
            "client_version": SOURCE["client_version"],
            "source_commit": SOURCE["source"]["commit"],
            "records": [
                {
                    "hero_id": 7,
                    "hero_name": "hero_test",
                    "display_name": "Test Hero",
                    "definition": {
                        "m_strMinimapImage": {
                            "$type": "panorama",
                            "$value": "file://{images}/heroes/test_mm.psd",
                        },
                        "m_strIconImageSmall": "file://{images}/heroes/test_mm.psd",
                    },
                },
                {
                    "hero_id": 8,
                    "hero_name": "hero_missing",
                    "display_name": None,
                    "definition": {},
                },
            ],
        }
        self.abilities: dict = {
            "client_version": SOURCE["client_version"],
            "source_commit": SOURCE["source"]["commit"],
            "records": [
                {
                    "ability_id": 11,
                    "ability_name": "arbitrary_item",
                    "display_name": "Test Item",
                    "definition": {
                        "m_eAbilityType": "EAbilityType_Item",
                        "m_strAbilityImage": ITEM,
                        "m_strShopIconLarge": SHOP,
                    },
                },
                {
                    "ability_id": 12,
                    "ability_name": "upgrade_test_ability",
                    "display_name": "Test Ability",
                    "definition": {
                        "m_eAbilityType": "EAbilityType_Signature",
                        "m_strAbilityImage": ABILITY,
                    },
                },
            ],
        }
        self.data = png()
        self.resources = {*image_assets.MAP_IMAGES, HERO, ABILITY, ITEM, SHOP, STAT}
        self.omit = set()
        self.mock = patch.object(image_assets, "run", side_effect=self.run_tool)
        self.commands = self.mock.start()
        self.addCleanup(self.mock.stop)

    def run_tool(self, tool, *args):
        if args == ("--version",):
            return "Version: test-extractor\nOS: test\n"
        if "--vpk_list" in args:
            return "\n".join(
                f"{path} CRC:0012345678 size:123" for path in sorted(self.resources)
            )
        selected = args[args.index("-f") + 1].split(",")
        output = Path(args[args.index("-o") + 1])
        for resource in selected:
            if resource not in self.omit:
                path = (
                    output
                    if len(selected) == 1
                    else output / image_assets.image_path(resource)
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 35 29"/>'
                    if resource.endswith(".vsvg_c")
                    else self.data
                )
        return "extracted\n"

    def build(self, source=None):
        (self.directory / "heroes.json").write_text(json.dumps(self.catalog))
        (self.directory / "abilities.json").write_text(json.dumps(self.abilities))
        image_assets.build(
            source or SOURCE,
            self.directory,
            game_dir=self.game,
            source2viewer=self.tool,
        )
        return json.loads((self.directory / "images.json").read_text())

    def test_wrong_client_version_is_rejected_before_tool_execution(self):
        source = copy.deepcopy(SOURCE)
        source["client_version"] = "1235"
        with self.assertRaisesRegex(ValueError, "install ClientVersion is 1234"):
            self.build(source)
        self.commands.assert_not_called()

    def test_wrong_source_revision_is_rejected(self):
        source = copy.deepcopy(SOURCE)
        source["source_revision"] = "5679"
        with self.assertRaisesRegex(ValueError, "install SourceRevision is 5678"):
            self.build(source)

    def test_hero_index_uses_catalog_ids(self):
        self.assertEqual(set(self.build()["heroes"]), {"7", "8"})

    def test_undeclared_hero_images_remain_null(self):
        self.assertEqual(
            self.build()["heroes"]["8"]["images"],
            {"minimap": None, "icon": None, "card": None},
        )

    def test_shared_texture_references_are_extracted_once(self):
        images = self.build()["heroes"]["7"]["images"]
        self.assertEqual(images["minimap"]["path"], images["icon"]["path"])
        extraction = self.commands.call_args.args
        self.assertEqual(
            extraction[extraction.index("-f") + 1].split(",").count(HERO), 1
        )

    def test_index_describes_the_archived_png_bytes(self):
        image = self.build()["heroes"]["7"]["images"]["minimap"]
        with zipfile.ZipFile(self.directory / "images.zip") as archive:
            data = archive.read(image["path"])
        self.assertEqual(data, self.data)
        self.assertEqual(image["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(image["bytes"], len(data))
        self.assertEqual((image["width"], image["height"]), (3, 2))

    def test_rebuilds_are_byte_identical(self):
        self.build()
        before = {
            name: (self.directory / name).read_bytes()
            for name in image_assets.ARTIFACTS
        }
        self.build()
        self.assertEqual(
            before,
            {
                name: (self.directory / name).read_bytes()
                for name in image_assets.ARTIFACTS
            },
        )

    def test_invalid_png_fails_before_writing_artifacts(self):
        self.data = b"not a PNG"
        with self.assertRaises(OSError):
            self.build()
        self.assertFalse((self.directory / "images.zip").exists())

    def test_missing_optional_texture_is_reported(self):
        self.resources.remove(HERO)
        index = self.build()
        self.assertIsNone(index["heroes"]["7"]["images"]["minimap"])
        self.assertEqual(index["missing_images"], [HERO])

    def test_missing_map_textures_are_an_error(self):
        self.resources.difference_update(image_assets.MAP_IMAGES)
        with self.assertRaisesRegex(ValueError, "no supported map image resources"):
            self.build()

    def test_success_without_expected_output_is_an_error(self):
        self.omit.add(HERO)
        with self.assertRaisesRegex(ValueError, "did not extract"):
            self.build()

    def test_single_texture_uses_a_file_output_path(self):
        self.resources = {MAP}
        self.assertIsNotNone(self.build()["map"]["images"][MAP])

    def test_tool_failure_does_not_become_missing_image(self):
        self.commands.side_effect = subprocess.CalledProcessError(1, [str(self.tool)])
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()

    def test_only_the_three_selected_minimaps_are_included(self):
        self.resources.update(
            {
                "panorama/images/minimap/base/minimap_midtown_mid_psd_01234567.vtex_c",
                "panorama/images/overviews/test_psd.vtex_c",
                "panorama/images/minimap/base/minimap_frame_psd.vtex_c",
            }
        )
        images = self.build()["map"]["images"]
        self.assertEqual(set(images), set(image_assets.MAP_IMAGES))
        with zipfile.ZipFile(self.directory / "images.zip") as archive:
            maps = [name for name in archive.namelist() if name.startswith("maps/")]
        self.assertEqual(len(maps), 3)

    def test_missing_selected_minimap_is_reported(self):
        self.resources.remove(MAP)
        index = self.build()
        self.assertIsNone(index["map"]["images"][MAP])
        self.assertEqual(index["missing_images"], [MAP])

    def test_item_classification_uses_the_catalog_type(self):
        index = self.build()
        self.assertEqual(set(index["items"]), {"11"})
        self.assertEqual(set(index["abilities"]), {"12"})

    def test_items_include_only_the_shop_image(self):
        images = self.build()["items"]["11"]["images"]
        self.assertEqual(set(images), {"shop"})
        self.assertEqual(images["shop"]["resource"], SHOP)
        extraction = self.commands.call_args.args
        self.assertNotIn(ITEM, extraction[extraction.index("-f") + 1].split(","))

    def test_ability_icon_uses_its_catalog_reference(self):
        ability = self.build()["abilities"]["12"]
        self.assertEqual(ability["ability_name"], "upgrade_test_ability")
        self.assertEqual(ability["images"]["icon"]["resource"], ABILITY)

    def test_alternate_ability_icon_is_preserved(self):
        self.abilities["records"][1]["definition"]["m_strSingleDoorAbilityImage"] = HERO
        images = self.build()["abilities"]["12"]["images"]
        self.assertEqual(images["single_door"]["resource"], HERO)

    def test_undeclared_shop_image_has_no_hud_icon_fallback(self):
        del self.abilities["records"][0]["definition"]["m_strShopIconLarge"]
        self.assertIsNone(self.build()["items"]["11"]["images"]["shop"])

    def test_shared_item_and_ability_texture_is_extracted_once(self):
        self.abilities["records"][0]["definition"]["m_strShopIconLarge"] = ABILITY
        index = self.build()
        self.assertEqual(
            index["items"]["11"]["images"]["shop"],
            index["abilities"]["12"]["images"]["icon"],
        )
        extraction = self.commands.call_args.args
        self.assertEqual(
            extraction[extraction.index("-f") + 1].split(",").count(ABILITY), 1
        )

    def test_stat_icons_use_game_filenames_as_keys(self):
        image = self.build()["stats"]["fire_rate"]
        self.assertEqual(image["resource"], STAT)
        self.assertEqual(image["path"], "stats/fire_rate.svg")
        with zipfile.ZipFile(self.directory / "images.zip") as archive:
            self.assertTrue(archive.read(image["path"]).startswith(b"<svg"))

    def test_stat_icons_are_discovered_without_a_name_map(self):
        resource = "panorama/images/icons/properties/new_stat.vsvg_c"
        self.resources.add(resource)
        self.assertEqual(self.build()["stats"]["new_stat"]["resource"], resource)

    def test_stat_icons_exclude_other_icon_directories(self):
        self.resources.add("panorama/images/icons/unrelated.vsvg_c")
        self.assertEqual(set(self.build()["stats"]), {"fire_rate"})

    def test_missing_stat_icon_directory_produces_an_empty_index(self):
        self.resources.remove(STAT)
        self.assertEqual(self.build()["stats"], {})

    def test_ambiguous_stat_icon_names_are_rejected(self):
        self.resources.add("panorama/images/icons/properties/fire_rate.vtex_c")
        with self.assertRaisesRegex(ValueError, "ambiguous stat icon name"):
            self.build()

    def test_svg_icons_keep_their_vector_format(self):
        resource = "panorama/images/icons/test.vsvg_c"
        self.resources.add(resource)
        self.abilities["records"][1]["definition"]["m_strAbilityImage"] = (
            "file://{images}/icons/test.svg"
        )
        image = self.build()["abilities"]["12"]["images"]["icon"]
        self.assertTrue(image["path"].endswith(".svg"))
        self.assertEqual((image["width"], image["height"]), (35, 29))
        with zipfile.ZipFile(self.directory / "images.zip") as archive:
            self.assertTrue(archive.read(image["path"]).startswith(b"<svg"))

    def test_invalid_svg_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid SVG"):
            image_assets.image_size(b"<html/>", "panorama/images/icons/test.vsvg_c")

    def test_mismatched_ability_catalog_is_rejected(self):
        self.abilities["source_commit"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "abilities catalog does not match"):
            self.build()

    def test_ambiguous_ability_id_is_rejected(self):
        self.abilities["records"][1]["ability_id"] = 11
        with self.assertRaisesRegex(ValueError, "ambiguous ability image ID"):
            self.build()

    def test_resource_paths_cannot_escape_the_image_directory(self):
        for reference in [
            "file://{images}/../secret.png",
            "s2r://panorama/images/test,other.png",
        ]:
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                image_assets.texture_path(reference)

    def test_panorama_reference_formats_resolve_to_compiled_textures(self):
        for reference in [
            "file://{images}/heroes/test_mm.psd",
            "s2r://panorama/images/heroes/test_mm_psd.vtex",
            HERO,
        ]:
            with self.subTest(reference=reference):
                self.assertEqual(image_assets.texture_path(reference), HERO)

    def test_changing_install_is_rejected_before_writing_artifacts(self):
        def update_install(tool, *args):
            output = self.run_tool(tool, *args)
            if "-o" in args:
                (self.game / image_assets.STEAM_INF).write_text("ClientVersion=1235\n")
            return output

        self.commands.side_effect = update_install
        with self.assertRaisesRegex(ValueError, "installation changed"):
            self.build()
        self.assertFalse((self.directory / "images.zip").exists())

    def test_provenance_records_tool_and_vpk_hashes(self):
        index = self.build()
        self.assertEqual(index["tool"]["version"], "Version: test-extractor")
        self.assertEqual(
            index["tool"]["sha256"], hashlib.sha256(self.tool.read_bytes()).hexdigest()
        )
        self.assertEqual(
            index["install"]["vpk"]["sha256"],
            hashlib.sha256((self.game / image_assets.PAK).read_bytes()).hexdigest(),
        )
