"""Pipeline tests use a small synthetic upstream, without network access."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline

FIXTURES = Path(__file__).parent / "fixtures"

SHA = "a" * 40
SOURCE = {
    "schema_version": 2,
    "source_key": "1234-" + SHA[:12],
    "client_version": "1234",
    "server_version": "1234",
    "source_revision": "9876543",
    "version_date": "Sep 18 2026",
    "version_time": "12:00:00",
    "source": {
        "repository": pipeline.SOURCE_REPO,
        "commit": SHA,
        "committed_at": "2026-09-18T12:00:00Z",
        "path": pipeline.SOURCE_PATH,
    },
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "dist"
        self.contents = {
            name: (FIXTURES / name).read_bytes() for name in pipeline.REQUIRED_FILES
        }
        self.localization = {
            name: (FIXTURES / "english.txt").read_bytes()
            for name in pipeline.LOCALIZATION_FILES
        }
        self.contents["new_data.vdata"] = b"{ value = 0.25 }\n"
        self.entries = [
            {
                "name": name,
                "type": "file",
                "size": len(data),
                "sha": hashlib.sha1(
                    b"blob " + str(len(data)).encode() + b"\0" + data
                ).hexdigest(),
            }
            for name, data in self.contents.items()
        ]
        self.urls = []
        self.mock = patch.object(pipeline, "fetch", side_effect=self.fetch)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def fetch(self, url):
        self.urls.append(url)
        if "/commits/" in url:
            return json.dumps(
                {
                    "sha": SHA,
                    "commit": {"committer": {"date": SOURCE["source"]["committed_at"]}},
                }
            ).encode()
        if url.endswith("/steam.inf"):
            return b"ClientVersion=1234\nServerVersion=1234\nSourceRevision=9876543\nVersionDate=Sep 18 2026\nVersionTime=12:00:00\n"
        for name, data in self.localization.items():
            if url == f"{pipeline.API_URL}/contents/{name}?ref={SHA}":
                return json.dumps(
                    {
                        "type": "file",
                        "size": len(data),
                        "sha": hashlib.sha1(
                            b"blob " + str(len(data)).encode() + b"\0" + data
                        ).hexdigest(),
                    }
                ).encode()
            if url == f"{pipeline.RAW_URL}/{SHA}/{name}":
                return data
        if "/contents/" in url:
            return json.dumps(
                self.entries + [{"name": "notes.txt", "type": "file"}]
            ).encode()
        return self.contents[url.rsplit("/", 1)[-1]]

    def test_resolves_ref_once_and_reads_version_at_commit(self):
        self.assertEqual(pipeline.resolve_source("feature/historical"), SOURCE)
        self.assertTrue(self.urls[0].endswith("feature%2Fhistorical"))
        self.assertIn(SHA, self.urls[1])

    def test_packages_only_json_and_records_input_hashes(self):
        directory = pipeline.build(SOURCE, self.output)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(directory.name, "1234")
        self.assertEqual(manifest["release_key"], "1234")
        self.assertEqual(set(manifest["files"]), pipeline.REQUIRED_FILES)
        self.assertEqual(manifest["source"]["commit"], SHA)
        for name in pipeline.REQUIRED_FILES:
            self.assertEqual(
                manifest["files"][name], pipeline.fingerprint(self.contents[name])
            )
        self.assertEqual(
            set(manifest["artifacts"]),
            {"abilities.json", "heroes.json", "modifiers.json"},
        )
        for name, checksum in manifest["artifacts"].items():
            self.assertEqual(
                checksum, pipeline.fingerprint((directory / name).read_bytes())
            )
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            set(manifest["artifacts"]) | {"manifest.json"},
        )
        self.assertEqual(manifest["catalogs"]["tables"], {})
        self.assertEqual(
            manifest["catalogs"]["json_catalogs"]["abilities.json"]["records"], 4
        )
        self.assertEqual(
            manifest["localization_files"],
            {
                name: pipeline.fingerprint(data)
                for name, data in self.localization.items()
            },
        )
        self.assertTrue(all(SHA in url for url in self.urls))
        self.assertFalse(any("new_data.vdata" in url for url in self.urls))

    def test_unused_local_inputs_do_not_change_catalogs_or_fingerprints(self):
        directory = pipeline.build(SOURCE, self.output)
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        self.contents["unrelated.vdata"] = b"not a catalog"
        self.localization["unused_english.txt"] = b"not a localization catalog"
        rebuilt = pipeline.build(
            SOURCE, self.output, inputs=(self.contents, self.localization)
        )
        self.assertEqual(directory, rebuilt)
        self.assertEqual(original, {p.name: p.read_bytes() for p in rebuilt.iterdir()})

    def test_build_is_reproducible_and_does_not_overwrite(self):
        directory = pipeline.build(SOURCE, self.output)
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        pipeline.build(SOURCE, self.output)
        self.assertEqual(
            original, {p.name: p.read_bytes() for p in directory.iterdir()}
        )
        changed = copy.deepcopy(SOURCE)
        changed["source_revision"] = "different"
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            pipeline.build(changed, self.output)
        self.assertEqual(
            original, {p.name: p.read_bytes() for p in directory.iterdir()}
        )

    def test_missing_required_file_does_not_publish_partial_bundle(self):
        self.entries = [
            entry for entry in self.entries if entry["name"] != "heroes.vdata"
        ]
        with self.assertRaisesRegex(ValueError, "missing required"):
            pipeline.build(SOURCE, self.output)
        self.assertFalse(self.output.exists())

    def test_download_must_match_git_blob(self):
        self.contents["abilities.vdata"] = b"wrong content"
        with self.assertRaisesRegex(ValueError, "source integrity"):
            pipeline.build(SOURCE, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_catalog_does_not_publish_partial_bundle(self):
        with (
            patch.object(
                pipeline, "build_catalogs", side_effect=ValueError("invalid KV3")
            ),
            self.assertRaisesRegex(ValueError, "invalid KV3"),
        ):
            pipeline.build(SOURCE, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_localization_integrity_is_checked(self):
        original_fetch = self.fetch

        def corrupt(url):
            if url.startswith(pipeline.RAW_URL) and url.endswith("_english.txt"):
                return b"corrupt localization"
            return original_fetch(url)

        with (
            patch.object(pipeline, "fetch", side_effect=corrupt),
            self.assertRaisesRegex(ValueError, "source integrity"),
        ):
            pipeline.build(SOURCE, self.output)
        self.assertFalse(self.output.exists())

    def test_required_catalog_must_be_a_file(self):
        self.entries[0]["type"] = "dir"
        with self.assertRaisesRegex(ValueError, "unexpected VData entry"):
            pipeline.build(SOURCE, self.output)

    def test_different_commits_with_same_client_version_have_different_source_keys(
        self,
    ):
        first = pipeline.resolve_source("master")
        with patch.object(
            pipeline,
            "fetch",
            side_effect=[
                json.dumps(
                    {
                        "sha": "b" * 40,
                        "commit": {
                            "committer": {"date": SOURCE["source"]["committed_at"]}
                        },
                    }
                ).encode(),
                b"ClientVersion=1234\n",
            ],
        ):
            second = pipeline.resolve_source("master")
        self.assertEqual(first["client_version"], second["client_version"])
        self.assertNotEqual(first["source_key"], second["source_key"])


if __name__ == "__main__":
    unittest.main()
