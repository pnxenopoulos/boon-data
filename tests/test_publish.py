"""Publication is tested against an in-memory GitHub, never a real repository."""

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import catalogs
import pipeline
import publish
from test_pipeline import SOURCE

FIXTURES = Path(__file__).parent / "fixtures"
TARGET = "d" * 40
PUBLISHED_AT = "2026-09-21T16:00:00Z"


def source_at(commit="b", version="1235", date="2026-09-19T12:00:00Z"):
    source = copy.deepcopy(SOURCE)
    source["source"]["commit"] = commit * 40
    source["source"]["committed_at"] = date
    source["client_version"] = version
    source["source_key"] = f"{version}-{source['source']['commit'][:12]}"
    return source


class FakeGitHub(publish.GitHub):
    def __init__(self):
        super().__init__(publish.DEFAULT_REPOSITORY)
        self.published_at: str | None = PUBLISHED_AT
        self.remote = {}
        self.tags = set()
        self.blobs = {}
        self.index = publish.empty_index()
        self.commands = []
        self.writes = []
        self.fail_upload = False
        self.corrupt_upload = False
        self.fail_index = False
        self.latest = None

    def release(self, tag):
        return copy.deepcopy(self.remote.get(tag))

    def releases(self):
        return copy.deepcopy(list(self.remote.values()))

    def asset_bytes(self, asset):
        return self.blobs[asset["id"]]

    def api(self, path, *, method="GET", data=None, missing_ok=False):
        if path.startswith("git/ref/tags/") and method == "GET":
            tag = path.removeprefix("git/ref/tags/")
            return {"ref": f"refs/tags/{tag}"} if tag in self.tags else None
        if path == "releases" and method == "POST":
            assert data is not None
            tag = data["tag_name"]
            self.commands.append(("create", tag))
            self.remote[tag] = {
                "id": len(self.remote) + 1,
                "tag_name": tag,
                "draft": data["draft"],
                "prerelease": False,
                "assets": [],
                "published_at": None,
                "name": data["name"],
                "html_url": "https://github.com/owner/repo/releases/tag/untagged-123",
            }
            return copy.deepcopy(self.remote[tag])
        release = next(r for r in self.remote.values() if path == f"releases/{r['id']}")
        if method == "PATCH":
            assert data is not None
            self.commands.append(("edit", release["tag_name"]))
            if data.get("draft") is False:
                release["draft"] = False
                release["published_at"] = self.published_at
                self.tags.add(release["tag_name"])
            if data.get("make_latest") == "true":
                self.latest = release["tag_name"]
        else:
            assert method == "GET"
        return copy.deepcopy(release)

    def upload_asset(self, release_id, path):
        release = next(r for r in self.remote.values() if r["id"] == release_id)
        assert release["draft"]
        self.commands.append(("upload", release["tag_name"], str(path)))
        assets = release["assets"]
        if any(a["name"] == path.name for a in assets):
            raise FileExistsError("release asset already exists")
        data = path.read_bytes()
        if self.corrupt_upload:
            data += b"corrupt"
        asset_id = len(self.blobs) + 1
        self.blobs[asset_id] = data
        digest = pipeline.fingerprint(data)
        assets.append(
            {
                "name": path.name,
                "id": asset_id,
                "state": "uploaded",
                "size": digest["bytes"],
                "digest": "sha256:" + digest["sha256"],
            }
        )
        if self.fail_upload:
            raise OSError("upload interrupted")

    def write_index(self, index, previous_sha, target):
        publish.validate_index(index, published=True)
        if self.fail_index:
            raise OSError("index update conflict")
        self.writes.append((copy.deepcopy(index), previous_sha, target))
        self.index = copy.deepcopy(index)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.inputs = (
            {name: (FIXTURES / name).read_bytes() for name in pipeline.REQUIRED_FILES},
            {
                name: (FIXTURES / "english.txt").read_bytes()
                for name in catalogs.LOCALIZATION_FILES
            },
        )
        self.initial = publish.empty_index()
        self.plan = publish.make_plan(
            SOURCE, self.inputs, self.initial, self.output, latest=True
        )
        self.github = FakeGitHub()

    def test_misc_is_required_by_manifests_and_index_records(self):
        manifest = json.loads(
            (Path(self.plan["directory"]) / "manifest.json").read_bytes()
        )
        del manifest["artifacts"]["misc.json"]
        with self.assertRaisesRegex(ValueError, "complete release"):
            publish.snapshot_record(publish.to_json(manifest).encode())
        index = copy.deepcopy(self.plan["index"])
        del index["snapshots"][self.plan["snapshot"]]["artifacts"]["misc.json"]
        with self.assertRaisesRegex(ValueError, "invalid artifact set"):
            publish.validate_index(index)

    def test_unchanged_inputs_reuse_snapshot_and_record_new_game_version(self):
        source = source_at()
        with patch.object(
            pipeline, "build", side_effect=AssertionError("must not rebuild")
        ):
            plan = publish.make_plan(
                source, self.inputs, self.plan["index"], self.output, latest=True
            )
        self.assertEqual(plan["action"], "reuse")
        self.assertEqual(plan["snapshot"], self.plan["snapshot"])
        self.assertEqual(len(plan["index"]["snapshots"]), 1)
        self.assertEqual(len(plan["index"]["versions"]), 2)
        observation = plan["index"]["versions"][source["client_version"]]
        self.assertEqual(observation["client_version"], "1235")
        self.assertEqual(
            plan["index"]["snapshots"][plan["snapshot"]]["client_version"], "1234"
        )
        self.assertEqual(plan["index"]["latest"], source["client_version"])

    def test_catalog_and_localization_changes_create_new_snapshots(self):
        for kind in (
            "vdata",
            "misc",
            "localization",
            "property_addition",
            "property_removal",
        ):
            with self.subTest(kind=kind):
                inputs = copy.deepcopy(self.inputs)
                if kind == "vdata":
                    inputs[0]["abilities.vdata"] = inputs[0]["abilities.vdata"].replace(
                        b'm_strValue = "13"', b'm_strValue = "14"'
                    )
                elif kind == "misc":
                    inputs[0]["misc.vdata"] = inputs[0]["misc.vdata"].replace(
                        b"m_valueMax = 70.0", b"m_valueMax = 71.0"
                    )
                elif kind == "localization":
                    path = next(iter(inputs[1]))
                    changed = inputs[1][path].replace(
                        b'"Bullet Lifesteal"', b'"Changed name"'
                    )
                    inputs[1].update(dict.fromkeys(inputs[1], changed))
                elif kind == "property_addition":
                    inputs[0]["abilities.vdata"] = inputs[0]["abilities.vdata"].replace(
                        b"m_mapAbilityProperties = {",
                        b'm_mapAbilityProperties = { FutureStat = {m_strValue = "7"}',
                        1,
                    )
                else:
                    inputs[0]["abilities.vdata"] = inputs[0]["abilities.vdata"].replace(
                        b'AbilityDuration = { m_strValue = "0" }', b""
                    )
                plan = publish.make_plan(
                    source_at(),
                    inputs,
                    self.plan["index"],
                    self.output / kind,
                    latest=True,
                )
                self.assertEqual(plan["action"], "publish")
                self.assertNotEqual(plan["snapshot"], self.plan["snapshot"])

    def test_vdata_gate_ignores_localization_and_generator_changes(self):
        inputs = copy.deepcopy(self.inputs)
        path = next(iter(inputs[1]))
        inputs[1][path] += b"\n"
        for source in (SOURCE, source_at()):
            with (
                self.subTest(version=source["client_version"]),
                patch.object(pipeline, "generator_fingerprint", return_value="f" * 64),
                patch.object(
                    pipeline, "build", side_effect=AssertionError("must not rebuild")
                ),
            ):
                plan = publish.make_plan(
                    source,
                    inputs,
                    self.plan["index"],
                    self.output,
                    latest=True,
                    vdata_only=True,
                )
            self.assertEqual(plan["action"], "reuse")
            self.assertEqual(plan["snapshot"], self.plan["snapshot"])
            self.assertEqual(
                plan["index"]["snapshots"], self.plan["index"]["snapshots"]
            )
            self.assertEqual(plan["index"]["latest"], source["client_version"])

    def test_cli_vdata_gate_previews_reuse_after_a_generator_update(self):
        index_path = self.output / "versions.json"
        index_path.write_text(publish.to_json(self.plan["index"]))
        output = self.output / "preview"
        with (
            patch.object(
                sys,
                "argv",
                [
                    "publish.py",
                    "--vdata-only",
                    "--index",
                    str(index_path),
                    "--output",
                    str(output),
                ],
            ),
            patch.object(pipeline, "resolve_source", return_value=source_at()),
            patch.object(pipeline, "download_inputs", return_value=self.inputs),
            patch.object(pipeline, "generator_fingerprint", return_value="f" * 64),
            patch.object(
                pipeline, "build", side_effect=AssertionError("must not rebuild")
            ),
            patch("builtins.print"),
        ):
            publish.main()
        plan = json.loads((output / "plan.json").read_text())
        index = json.loads((output / "versions.json").read_text())
        self.assertEqual(plan["action"], "reuse")
        self.assertEqual(index["versions"]["1235"]["snapshot"], "1234")
        self.assertFalse((output / "1235").exists())

    def test_vdata_gate_publishes_changes_to_each_required_file(self):
        for name in sorted(pipeline.REQUIRED_FILES):
            with self.subTest(file=name):
                inputs = copy.deepcopy(self.inputs)
                inputs[0][name] += b"\n"
                plan = publish.make_plan(
                    source_at(),
                    inputs,
                    self.plan["index"],
                    self.output / name,
                    latest=True,
                    vdata_only=True,
                )
                self.assertEqual(plan["action"], "publish")
                self.assertNotEqual(plan["snapshot"], self.plan["snapshot"])

    def test_vdata_gate_bootstraps_an_empty_index(self):
        plan = publish.make_plan(
            SOURCE, self.inputs, self.initial, self.output, latest=True, vdata_only=True
        )
        self.assertEqual(plan["action"], "publish")

    def test_vdata_gate_uses_a_backfill_when_no_latest_version_exists(self):
        index = copy.deepcopy(self.plan["index"])
        index["latest"] = None
        with patch.object(pipeline, "generator_fingerprint", return_value="f" * 64):
            plan = publish.make_plan(
                source_at(),
                self.inputs,
                index,
                self.output,
                latest=True,
                vdata_only=True,
            )
        self.assertEqual(plan["action"], "reuse")
        self.assertEqual(plan["index"]["latest"], "1235")

    def test_vdata_gate_still_refuses_to_overwrite_a_changed_client_version(self):
        inputs = copy.deepcopy(self.inputs)
        inputs[0]["abilities.vdata"] += b"\n"
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            publish.make_plan(
                SOURCE,
                inputs,
                self.plan["index"],
                self.output,
                latest=True,
                vdata_only=True,
            )

    def test_vdata_gate_records_new_versions_without_uploading_assets(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        uploads = [c for c in self.github.commands if c[0] == "upload"]
        inputs = copy.deepcopy(self.inputs)
        inputs[1].update({name: data + b"\n" for name, data in inputs[1].items()})
        with patch.object(pipeline, "generator_fingerprint", return_value="f" * 64):
            plan = publish.make_plan(
                source_at(),
                inputs,
                self.github.index,
                self.output,
                latest=True,
                vdata_only=True,
            )
        publish.publish_plan(self.github, plan, self.github.index, "sha", TARGET)
        self.assertEqual(self.github.index["latest"], "1235")
        self.assertEqual(self.github.index["versions"]["1235"]["snapshot"], "1234")
        self.assertEqual(self.github.latest, "1234")
        self.assertEqual([c for c in self.github.commands if c[0] == "upload"], uploads)

    def test_unrelated_vdata_does_not_trigger_release(self):
        inputs = copy.deepcopy(self.inputs)
        inputs[0]["extra.vdata"] = b"{extra={}}"
        with patch.object(
            pipeline, "build", side_effect=AssertionError("must not rebuild")
        ):
            plan = publish.make_plan(
                source_at(), inputs, self.plan["index"], self.output, latest=True
            )
        self.assertEqual(plan["action"], "reuse")

    def test_input_order_and_observation_metadata_do_not_change_content_identity(self):
        reversed_inputs = tuple(
            dict(reversed(list(data.items()))) for data in self.inputs
        )
        first = pipeline.snapshot_metadata(SOURCE, *self.inputs)
        second = pipeline.snapshot_metadata(source_at(), *reversed_inputs)
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertNotEqual(first["release_key"], second["release_key"])

    def test_backfill_reuses_published_content_after_a_generator_change(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        original = copy.deepcopy(self.github.index)
        remote = copy.deepcopy(self.github.remote)
        self.github.commands.clear()
        with (
            patch.object(pipeline, "generator_fingerprint", return_value="e" * 64),
            patch.object(
                pipeline, "build", side_effect=AssertionError("must not rebuild")
            ),
        ):
            plan = publish.make_plan(
                SOURCE, self.inputs, original, self.output, latest=False
            )
        self.assertEqual(plan["action"], "reuse")
        self.assertEqual(plan["snapshot"], "1234")
        self.assertIsNone(plan["directory"])
        self.assertEqual(plan["index"], original)
        publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(self.github.index, original)
        self.assertEqual(self.github.remote, remote)
        self.assertFalse(
            any(c[0] in ("create", "upload") for c in self.github.commands)
        )
        self.assertEqual(len(self.github.writes), 1)

        self.github.remote["1234"]["assets"].pop()
        with self.assertRaisesRegex(ValueError, "complete artifact set"):
            publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(len(self.github.writes), 1)

    def test_generator_change_reuses_a_versions_shared_snapshot(self):
        shared = publish.make_plan(
            source_at(), self.inputs, self.plan["index"], self.output, latest=True
        )
        self.assertEqual(shared["index"]["versions"]["1235"]["snapshot"], "1234")
        with (
            patch.object(pipeline, "generator_fingerprint", return_value="e" * 64),
            patch.object(
                pipeline, "build", side_effect=AssertionError("must not rebuild")
            ),
        ):
            plan = publish.make_plan(
                source_at(), self.inputs, shared["index"], self.output, latest=False
            )
        self.assertEqual(plan["action"], "reuse")
        self.assertEqual(plan["snapshot"], "1234")
        self.assertEqual(plan["index"], shared["index"])

    def test_new_version_uses_the_current_generator(self):
        with patch.object(pipeline, "generator_fingerprint", return_value="e" * 64):
            plan = publish.make_plan(
                source_at(), self.inputs, self.plan["index"], self.output, latest=False
            )
        self.assertEqual(plan["action"], "publish")
        self.assertEqual(plan["snapshot"], "1235")
        self.assertEqual(
            plan["index"]["snapshots"]["1235"]["identity"]["generator_sha256"], "e" * 64
        )

    def test_manual_backfill_does_not_advance_latest(self):
        plan = publish.make_plan(
            source_at(), self.inputs, self.plan["index"], self.output, latest=False
        )
        self.assertEqual(plan["index"]["latest"], SOURCE["client_version"])
        plan = publish.make_plan(
            source_at(date="2020-01-01T00:00:00Z"),
            self.inputs,
            self.plan["index"],
            self.output,
            latest=True,
        )
        self.assertEqual(plan["index"]["latest"], SOURCE["client_version"])

    def test_backfill_publishes_older_data_without_changing_latest_and_can_repeat(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        original = copy.deepcopy(self.github.index)
        source = source_at(version="1233", date="2026-09-17T12:00:00Z")
        inputs = copy.deepcopy(self.inputs)
        inputs[0]["abilities.vdata"] = inputs[0]["abilities.vdata"].replace(
            b'm_strValue = "13"', b'm_strValue = "12"'
        )
        plan = publish.make_plan(source, inputs, original, self.output, latest=False)
        self.assertEqual(plan["action"], "publish")
        publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(self.github.index["latest"], "1234")
        self.assertEqual(self.github.latest, "1234")
        self.assertEqual(
            self.github.index["versions"]["1234"], original["versions"]["1234"]
        )
        self.assertEqual(
            self.github.index["versions"]["1233"]["source"], source["source"]
        )
        self.assertEqual(len(self.github.release("1233")["assets"]), 5)
        self.assertFalse(self.github.release("1233")["draft"])

        index = copy.deepcopy(self.github.index)
        uploads = [c for c in self.github.commands if c[0] == "upload"]
        plan = publish.make_plan(source, inputs, index, self.output, latest=False)
        self.assertEqual(plan["action"], "reuse")
        publish.publish_plan(self.github, plan, index, "sha", TARGET)
        self.assertEqual(self.github.index, index)
        self.assertEqual(self.github.latest, "1234")
        self.assertEqual([c for c in self.github.commands if c[0] == "upload"], uploads)

    def test_backfill_succeeds_when_the_indexed_latest_release_was_deleted(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        del self.github.remote["1234"]
        self.github.latest = None
        source = source_at(version="1233", date="2026-09-17T12:00:00Z")
        inputs = copy.deepcopy(self.inputs)
        inputs[0]["abilities.vdata"] += b"\n// historical input"

        for action in ("publish", "reuse"):
            with self.subTest(action=action):
                original = copy.deepcopy(self.github.index)
                index = publish.recover_snapshots(self.github, original)
                plan = publish.make_plan(
                    source, inputs, index, self.output, latest=False
                )
                self.assertEqual(plan["action"], action)
                self.github.commands.clear()
                with patch.object(
                    self.github, "release", wraps=self.github.release
                ) as lookup:
                    publish.publish_plan(self.github, plan, original, "sha", TARGET)
                lookup.assert_called_once_with("1233")
                self.assertEqual(self.github.index["latest"], "1234")
                self.assertEqual(
                    self.github.index["versions"]["1234"], original["versions"]["1234"]
                )
                self.assertEqual(
                    self.github.index["versions"]["1233"]["source"], source["source"]
                )
                self.assertIsNone(self.github.latest)
                self.assertFalse(self.github.remote["1233"]["draft"])
                self.assertEqual(len(self.github.remote["1233"]["assets"]), 5)
                if action == "reuse":
                    self.assertEqual(self.github.commands, [])
        self.assertEqual(len(self.github.writes), 2)

    def test_cli_backfill_rebuilds_a_deleted_release_and_preserves_other_versions(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        inputs = copy.deepcopy(self.inputs)
        inputs[0]["abilities.vdata"] += b"\n// newer source"
        newer = publish.make_plan(
            source_at(), inputs, self.github.index, self.output, latest=True
        )
        publish.publish_plan(self.github, newer, self.github.index, "sha1", TARGET)
        original = copy.deepcopy(self.github.index)
        self.github.remote.clear()
        self.github.tags.clear()
        self.github.latest = None
        self.github.commands.clear()
        output = self.output / "backfill"
        with (
            patch.object(
                sys,
                "argv",
                [
                    "publish.py",
                    "--ref",
                    SOURCE["source"]["commit"],
                    "--publish",
                    "--target",
                    TARGET,
                    "--output",
                    str(output),
                ],
            ),
            patch.object(publish, "GitHub", return_value=self.github),
            patch.object(self.github, "read_index", return_value=(original, "sha2")),
            patch.object(pipeline, "resolve_source", return_value=SOURCE),
            patch.object(pipeline, "download_inputs", return_value=self.inputs),
            patch.object(pipeline, "generator_fingerprint", return_value="e" * 64),
            patch("builtins.print"),
        ):
            publish.main()
        self.assertEqual(
            json.loads((output / "plan.json").read_text())["action"], "publish"
        )
        self.assertEqual(set(self.github.remote), {"1234"})
        self.assertEqual(len(self.github.remote["1234"]["assets"]), 5)
        self.assertFalse(self.github.remote["1234"]["draft"])
        self.assertEqual(self.github.index["latest"], "1235")
        self.assertEqual(
            self.github.index["versions"]["1235"], original["versions"]["1235"]
        )
        self.assertEqual(
            self.github.index["snapshots"]["1235"], original["snapshots"]["1235"]
        )
        self.assertNotEqual(
            self.github.index["versions"]["1234"]["artifacts"],
            original["versions"]["1234"]["artifacts"],
        )
        self.assertEqual(
            self.github.index["versions"]["1234"]["source"], SOURCE["source"]
        )
        self.assertEqual(self.github.writes[-1][1], "sha2")
        self.assertIsNone(self.github.latest)

    def test_missing_release_with_existing_tag_is_not_recreated(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        del self.github.remote["1234"]
        original = copy.deepcopy(self.github.index)
        plan = publish.make_plan(
            SOURCE,
            self.inputs,
            original,
            self.output,
            latest=False,
            published_tags=set(),
        )
        self.github.commands.clear()
        with self.assertRaisesRegex(
            ValueError, "release 1234 is missing.*Git tag still exists"
        ):
            publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(self.github.tags, {"1234"})
        self.assertEqual(self.github.remote, {})
        self.assertEqual(self.github.index, original)
        self.assertEqual(self.github.commands, [])
        self.assertEqual(len(self.github.writes), 1)

    def test_existing_tag_blocks_a_new_release_without_an_index_entry(self):
        self.github.tags.add("1234")
        with self.assertRaisesRegex(
            ValueError, "release 1234 is missing.*Git tag still exists"
        ):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(self.github.tags, {"1234"})
        self.assertEqual(self.github.remote, {})
        self.assertEqual(self.github.commands, [])
        self.assertEqual(self.github.writes, [])

    def test_missing_shared_snapshot_is_not_reused_by_a_new_version(self):
        for vdata_only in (False, True):
            with self.subTest(vdata_only=vdata_only):
                plan = publish.make_plan(
                    source_at(),
                    self.inputs,
                    self.plan["index"],
                    self.output / str(vdata_only),
                    latest=vdata_only,
                    vdata_only=vdata_only,
                    published_tags=set(),
                )
                self.assertEqual(plan["action"], "publish")
                self.assertEqual(plan["snapshot"], "1235")
                self.assertEqual(
                    plan["index"]["versions"]["1234"],
                    self.plan["index"]["versions"]["1234"],
                )
                self.assertEqual(
                    plan["index"]["snapshots"]["1234"],
                    self.plan["index"]["snapshots"]["1234"],
                )

    def test_rebuilt_release_refreshes_checksums_for_its_shared_versions(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        shared = publish.make_plan(
            source_at(), self.inputs, self.github.index, self.output, latest=True
        )
        publish.publish_plan(self.github, shared, self.github.index, "sha1", TARGET)
        original = copy.deepcopy(self.github.index)
        self.github.remote.clear()
        self.github.tags.clear()
        self.github.published_at = "2026-09-25T19:00:00Z"
        with patch.object(pipeline, "generator_fingerprint", return_value="e" * 64):
            plan = publish.make_plan(
                SOURCE,
                self.inputs,
                original,
                self.output / "rebuilt",
                latest=False,
                published_tags=set(),
            )
        self.assertEqual(plan["action"], "publish")
        publish.publish_plan(self.github, plan, original, "sha2", TARGET)
        index = self.github.index
        for version in ("1234", "1235"):
            self.assertEqual(
                index["versions"][version]["source"],
                original["versions"][version]["source"],
            )
            self.assertEqual(
                index["versions"][version]["artifacts"],
                index["snapshots"]["1234"]["artifacts"],
            )
            self.assertEqual(
                index["versions"][version]["released_at"], self.github.published_at
            )
        self.assertEqual(index["latest"], "1235")
        self.assertNotEqual(
            index["versions"]["1235"]["artifacts"],
            original["versions"]["1235"]["artifacts"],
        )
        publish.validate_index(index, published=True)

    def test_rebuilt_release_recovers_after_an_interrupted_index_update(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        shared = publish.make_plan(
            source_at(), self.inputs, self.github.index, self.output, latest=True
        )
        publish.publish_plan(self.github, shared, self.github.index, "sha1", TARGET)
        original = copy.deepcopy(self.github.index)
        self.github.remote.clear()
        self.github.tags.clear()
        self.github.published_at = "2026-09-25T19:00:00Z"
        with patch.object(pipeline, "generator_fingerprint", return_value="e" * 64):
            plan = publish.make_plan(
                SOURCE,
                self.inputs,
                original,
                self.output / "rebuilt",
                latest=False,
                published_tags=set(),
            )
        self.github.fail_index = True
        with self.assertRaisesRegex(OSError, "index update conflict"):
            publish.publish_plan(self.github, plan, original, "sha2", TARGET)
        self.assertEqual(self.github.index, original)
        recovered = publish.recover_snapshots(self.github, original)
        self.assertEqual(recovered, plan["index"])
        self.github.fail_index = False
        uploads = [c for c in self.github.commands if c[0] == "upload"]
        plan = publish.make_plan(
            SOURCE,
            self.inputs,
            recovered,
            self.output,
            latest=False,
            published_tags={"1234"},
        )
        self.assertEqual(plan["action"], "reuse")
        publish.publish_plan(self.github, plan, original, "sha2", TARGET)
        self.assertEqual(self.github.index, recovered)
        self.assertEqual([c for c in self.github.commands if c[0] == "upload"], uploads)

    def test_indexed_draft_is_rebuilt_and_resumed_without_overwriting_assets(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        release = self.github.remote["1234"]
        release["draft"] = True
        removed = release["assets"].pop()
        preserved = copy.deepcopy(release["assets"])
        self.github.commands.clear()
        plan = publish.make_plan(
            SOURCE,
            self.inputs,
            self.github.index,
            self.output,
            latest=False,
            published_tags=set(),
        )
        self.assertEqual(plan["action"], "publish")
        publish.publish_plan(self.github, plan, self.github.index, "sha", TARGET)
        self.assertFalse(release["draft"])
        self.assertEqual(release["assets"][:-1], preserved)
        self.assertEqual(release["assets"][-1]["name"], removed["name"])
        self.assertEqual(len([c for c in self.github.commands if c[0] == "upload"]), 1)
        self.assertFalse(any(c[0] == "create" for c in self.github.commands))

    def test_prerelease_is_not_overwritten_or_indexed(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.github.remote["1234"]["prerelease"] = True
        remote = copy.deepcopy(self.github.remote)
        original = copy.deepcopy(self.github.index)
        self.github.commands.clear()
        plan = publish.make_plan(
            SOURCE,
            self.inputs,
            original,
            self.output,
            latest=False,
            published_tags=set(),
        )
        with self.assertRaisesRegex(
            ValueError, "snapshot release is not published: 1234"
        ):
            publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(self.github.remote, remote)
        self.assertEqual(self.github.index, original)
        self.assertEqual(self.github.commands, [])

    def test_reuse_still_rejects_a_missing_selected_release(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        del self.github.remote["1234"]
        original = copy.deepcopy(self.github.index)
        plan = publish.make_plan(
            source_at(version="1233", date="2026-09-17T12:00:00Z"),
            self.inputs,
            original,
            self.output,
            latest=False,
        )
        self.assertEqual(plan["action"], "reuse")
        with self.assertRaisesRegex(ValueError, "snapshot release is missing: 1234"):
            publish.publish_plan(self.github, plan, original, "sha", TARGET)
        self.assertEqual(self.github.index, original)
        self.assertEqual(len(self.github.writes), 1)

    def test_complete_release_is_verified_before_index_commit(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        release = self.github.release(self.plan["snapshot"])
        self.assertFalse(release["draft"])
        self.assertEqual(
            {a["name"] for a in release["assets"]},
            {
                "abilities.json",
                "heroes.json",
                "modifiers.json",
                "misc.json",
                "manifest.json",
            },
        )
        self.assertEqual(self.github.index, self.plan["index"])
        self.assertEqual(self.github.latest, self.plan["snapshot"])

    def test_new_draft_is_published_by_id_when_tag_lookup_cannot_find_it(self):
        # The tag lookup returns no release; creation returns an untagged draft URL.
        # Any further lookup by tag would reproduce the failed Actions run.
        with patch.object(self.github, "release", side_effect=[None]) as lookup:
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        lookup.assert_called_once_with(self.plan["snapshot"])
        release = self.github.remote[self.plan["snapshot"]]
        self.assertFalse(release["draft"])
        self.assertEqual(len(release["assets"]), 5)
        self.assertEqual(self.github.latest, self.plan["snapshot"])

    def test_release_published_between_lookup_and_refresh_is_not_uploaded(self):
        self.github.fail_upload = True
        with self.assertRaises(OSError):
            publish.publish_snapshot(self.github, self.plan, TARGET)
        self.github.fail_upload = False
        api = self.github.api

        def publish_before_refresh(path, **kwargs):
            self.github.remote[self.plan["snapshot"]]["draft"] = False
            return api(path, **kwargs)

        self.github.commands.clear()
        with (
            patch.object(self.github, "api", side_effect=publish_before_refresh),
            self.assertRaisesRegex(ValueError, "non-draft release"),
        ):
            publish.publish_snapshot(self.github, self.plan, TARGET)
        self.assertEqual(self.github.commands, [])
        self.assertEqual(self.github.writes, [])

    def test_versions_include_publication_time_and_download_metadata(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(self.github.index["latest"], "1234")
        self.assertEqual(set(self.github.index["versions"]), {"1234"})
        entry = self.github.index["versions"]["1234"]
        self.assertEqual(entry["released_at"], PUBLISHED_AT)
        self.assertNotEqual(entry["released_at"], entry["source"]["committed_at"])
        release = self.github.release(entry["snapshot"])
        self.assertEqual(release["name"], "boon-data-1234")
        for name, asset in entry["artifacts"].items():
            self.assertEqual(
                asset["url"],
                f"https://github.com/{self.github.repository}/releases/download/{entry['snapshot']}/{name}",
            )
            local = Path(self.plan["directory"]) / name
            self.assertEqual(
                {key: asset[key] for key in ("sha256", "bytes")},
                pipeline.fingerprint(local.read_bytes()),
            )

    def test_preview_does_not_invent_a_publication_timestamp(self):
        self.assertIsNone(self.plan["index"]["versions"]["1234"]["released_at"])
        with self.assertRaisesRegex(TypeError, "publication timestamp"):
            publish.validate_index(self.plan["index"], published=True)
        self.github.published_at = None
        with self.assertRaisesRegex(TypeError, "publication timestamp"):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(self.github.writes, [])
        self.github.remote[self.plan["snapshot"]]["published_at"] = PUBLISHED_AT
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(
            self.github.index["versions"]["1234"]["released_at"], PUBLISHED_AT
        )

    def test_publication_timestamps_are_utc_and_timezone_is_required(self):
        self.assertEqual(
            publish.published_time("2026-09-21T12:00:00-04:00"), PUBLISHED_AT
        )
        for value in (None, "", "2026-09-21T16:00:00"):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                publish.published_time(value)

    def test_shared_snapshot_keeps_original_release_time_and_asset_urls(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        plan = publish.make_plan(
            source_at(), self.inputs, self.github.index, self.output, latest=True
        )
        self.github.published_at = "2026-09-22T16:00:00Z"
        publish.publish_plan(self.github, plan, self.github.index, "sha", TARGET)
        older, newer = (self.github.index["versions"][v] for v in ("1234", "1235"))
        self.assertEqual(newer["released_at"], older["released_at"])
        self.assertEqual(newer["artifacts"], older["artifacts"])
        self.assertNotEqual(newer["source"]["commit"], older["source"]["commit"])
        self.assertEqual(self.github.index["latest"], "1235")

    def test_automatic_observations_cannot_revert_a_newer_source_commit(self):
        newer = source_at(version="1234")
        plan = publish.make_plan(
            newer, self.inputs, self.plan["index"], self.output, latest=True
        )
        self.assertEqual(set(plan["index"]["versions"]), {"1234"})
        self.assertEqual(plan["index"]["versions"]["1234"]["source"], newer["source"])
        older = publish.make_plan(
            SOURCE, self.inputs, plan["index"], self.output, latest=True
        )
        self.assertEqual(older["index"], plan["index"])

    def test_backfill_replaces_only_the_selected_versions_observation(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        expected = copy.deepcopy(self.github.index["versions"]["1234"])
        newer = source_at(version="1234")
        newer.update(
            source_revision="newer-revision",
            version_date="Sep 19 2026",
            version_time="12:00:00",
        )
        plan = publish.make_plan(
            newer, self.inputs, self.github.index, self.output, latest=True
        )
        publish.publish_plan(self.github, plan, self.github.index, "sha1", TARGET)
        plan = publish.make_plan(
            source_at(commit="c", date="2026-09-20T12:00:00Z"),
            self.inputs,
            self.github.index,
            self.output,
            latest=True,
        )
        publish.publish_plan(self.github, plan, self.github.index, "sha2", TARGET)
        original = copy.deepcopy(self.github.index)
        remote = copy.deepcopy(self.github.remote)
        self.github.commands.clear()

        backfill = publish.make_plan(
            SOURCE, self.inputs, original, self.output, latest=False
        )
        self.assertEqual(backfill["action"], "reuse")
        publish.publish_plan(self.github, backfill, original, "sha3", TARGET)
        self.assertEqual(self.github.index["versions"]["1234"], expected)
        self.assertEqual(
            self.github.index["versions"]["1235"], original["versions"]["1235"]
        )
        self.assertEqual(self.github.index["snapshots"], original["snapshots"])
        self.assertEqual(self.github.index["latest"], "1235")
        self.assertEqual(self.github.remote, remote)
        self.assertEqual(len(self.github.writes), 4)
        self.assertEqual(self.github.writes[-1][1], "sha3")
        self.assertFalse(
            any(c[0] in ("create", "upload") for c in self.github.commands)
        )
        self.assertEqual(original["versions"]["1234"]["source"], newer["source"])

    def test_changed_inputs_cannot_overwrite_same_client_version(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        original = copy.deepcopy(self.github.index)
        remote = copy.deepcopy(self.github.remote)
        for group, name in ((0, "abilities.vdata"), (1, next(iter(self.inputs[1])))):
            changed = copy.deepcopy(self.inputs)
            changed[group][name] += b"\n// changed source input"
            with (
                self.subTest(file=name),
                self.assertRaisesRegex(
                    ValueError,
                    "client version 1234.*different source content.*refusing",
                ),
            ):
                publish.make_plan(
                    source_at(version="1234"),
                    changed,
                    original,
                    self.output,
                    latest=True,
                )
            self.assertEqual(self.github.index, original)
            self.assertEqual(self.github.remote, remote)

    def test_manifest_tag_must_equal_the_client_version(self):
        manifest = json.loads(
            (Path(self.plan["directory"]) / "manifest.json").read_bytes()
        )
        manifest["release_key"] = "5678"
        with self.assertRaisesRegex(ValueError, "release tag does not match"):
            publish.snapshot_record(json.dumps(manifest).encode())

    def test_index_rejects_version_keys_and_metadata_that_disagree(self):
        for kind in ("version", "timestamp", "artifacts"):
            index = copy.deepcopy(self.plan["index"])
            entry = index["versions"]["1234"]
            if kind == "version":
                entry["client_version"] = "5678"
            elif kind == "timestamp":
                entry["released_at"] = PUBLISHED_AT
            else:
                entry["artifacts"] = {}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                publish.validate_index(index)

    def test_download_urls_follow_the_destination_repository(self):
        plan = publish.make_plan(
            SOURCE,
            self.inputs,
            self.initial,
            self.output,
            latest=True,
            repository="owner/catalog-data",
        )
        for asset in plan["index"]["versions"]["1234"]["artifacts"].values():
            self.assertTrue(
                asset["url"].startswith("https://github.com/owner/catalog-data/")
            )

    def test_interrupted_upload_keeps_draft_and_index_unchanged_then_resumes(self):
        self.github.fail_upload = True
        with self.assertRaisesRegex(OSError, "upload interrupted"):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertTrue(self.github.release(self.plan["snapshot"])["draft"])
        self.assertEqual(self.github.index, self.initial)
        uploaded = copy.deepcopy(self.github.remote[self.plan["snapshot"]]["assets"])
        self.github.fail_upload = False
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(sum(c[0] == "create" for c in self.github.commands), 1)
        self.assertEqual(self.github.index, self.plan["index"])
        resumed = {
            a["name"]: a for a in self.github.remote[self.plan["snapshot"]]["assets"]
        }
        for asset in uploaded:
            self.assertEqual(resumed[asset["name"]], asset)

    def test_draft_retry_rejects_mismatched_or_unexpected_assets_without_overwriting(
        self,
    ):
        self.github.fail_upload = True
        with self.assertRaises(OSError):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        draft = self.github.remote[self.plan["snapshot"]]
        original = copy.deepcopy(draft["assets"])
        self.github.fail_upload = False
        for kind in ("checksum", "unexpected"):
            draft["assets"] = copy.deepcopy(original)
            if kind == "checksum":
                draft["assets"][0]["digest"] = "sha256:" + "0" * 64
            else:
                draft["assets"][0]["name"] = "unexpected.json"
            before = copy.deepcopy(draft)
            self.github.commands.clear()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
            self.assertEqual(self.github.commands, [])
            self.assertEqual(draft, before)
            self.assertEqual(self.github.writes, [])

    def test_incomplete_or_corrupt_upload_is_not_published(self):
        self.github.corrupt_upload = True
        with self.assertRaisesRegex(ValueError, "incomplete release asset"):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertTrue(self.github.release(self.plan["snapshot"])["draft"])
        self.assertEqual(self.github.writes, [])

    def test_index_conflict_is_recovered_even_after_upstream_moves(self):
        self.github.fail_index = True
        with self.assertRaisesRegex(OSError, "index update conflict"):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertFalse(self.github.release(self.plan["snapshot"])["draft"])
        recovered = publish.recover_snapshots(self.github, self.github.index)
        plan = publish.make_plan(
            source_at(), self.inputs, recovered, self.output, latest=True
        )
        self.assertEqual(plan["action"], "reuse")
        self.github.fail_index = False
        publish.publish_plan(self.github, plan, self.initial, None, TARGET)
        self.assertEqual(len(self.github.remote), 1)
        self.assertEqual(len(self.github.index["versions"]), 2)

    def test_published_assets_are_not_reuploaded_on_retry(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.github.commands.clear()
        publish.publish_plan(
            self.github, self.plan, self.plan["index"], "old-sha", TARGET
        )
        self.assertFalse(
            any(c[0] in ("create", "upload") for c in self.github.commands)
        )
        self.assertEqual(len(self.github.writes), 1)

    def test_incomplete_published_release_is_never_overwritten_or_indexed(self):
        publish.publish_snapshot(self.github, self.plan, TARGET)
        self.github.remote[self.plan["snapshot"]]["assets"].pop()
        self.github.commands.clear()
        with self.assertRaisesRegex(ValueError, "complete artifact set"):
            publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        self.assertEqual(self.github.commands, [])
        self.assertEqual(self.github.index, self.initial)

    def test_digest_fallback_and_checksum_validation(self):
        publish.publish_snapshot(self.github, self.plan, TARGET)
        record = self.plan["index"]["snapshots"][self.plan["snapshot"]]
        for asset in self.github.remote[self.plan["snapshot"]]["assets"]:
            asset["digest"] = None
        publish.verify_release(
            self.github,
            self.github.release(self.plan["snapshot"]),
            record,
            published=True,
        )
        asset = self.github.remote[self.plan["snapshot"]]["assets"][0]
        asset["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            publish.verify_release(
                self.github,
                self.github.release(self.plan["snapshot"]),
                record,
                published=True,
            )

    def test_reverted_inputs_reuse_earlier_snapshot_and_update_latest_alias(self):
        publish.publish_plan(self.github, self.plan, self.initial, None, TARGET)
        changed = copy.deepcopy(self.inputs)
        changed[0]["abilities.vdata"] += b"\n// changed catalog input"
        second = publish.make_plan(
            source_at(), changed, self.github.index, self.output, latest=True
        )
        publish.publish_plan(self.github, second, self.github.index, "sha1", TARGET)
        third = publish.make_plan(
            source_at(commit="c", version="1236", date="2026-09-20T12:00:00Z"),
            self.inputs,
            self.github.index,
            self.output,
            latest=True,
        )
        self.assertEqual(third["action"], "reuse")
        publish.publish_plan(self.github, third, self.github.index, "sha2", TARGET)
        self.assertEqual(self.github.latest, self.plan["snapshot"])
        self.assertEqual(len(self.github.remote), 2)
        self.assertEqual(len(self.github.index["versions"]), 3)

    def test_manifest_requires_exact_json_assets_and_valid_fingerprints(self):
        path = Path(self.plan["directory"]) / "manifest.json"
        for kind in ("artifact", "parquet", "zip", "content", "dataset"):
            manifest = json.loads(path.read_text())
            if kind == "artifact":
                manifest["artifacts"].pop("heroes.json")
            elif kind == "parquet":
                manifest["artifacts"]["abilities.parquet"] = pipeline.fingerprint(b"")
            elif kind == "zip":
                manifest["artifacts"]["vdata.zip"] = pipeline.fingerprint(b"")
            else:
                manifest["snapshot"][f"{kind}_sha256"] = "0" * 64
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                publish.snapshot_record(json.dumps(manifest).encode())


class ReleaseApiTests(unittest.TestCase):
    def test_existing_draft_is_found_when_tag_endpoint_returns_not_found(self):
        github = publish.GitHub("owner/repo")
        draft = {"id": 123, "tag_name": "snapshot", "draft": True}
        github.api = Mock(side_effect=[None, [draft]])
        self.assertEqual(github.release("snapshot"), draft)
        self.assertEqual(
            [call.args[0] for call in github.api.call_args_list],
            ["releases/tags/snapshot", "releases?per_page=100&page=1"],
        )


class IndexApiTests(unittest.TestCase):
    def test_missing_index_and_bootstrap_use_a_separate_branch(self):
        github = publish.GitHub("owner/repo")
        github.api = Mock(return_value=None)
        self.assertEqual(github.read_index(), (publish.empty_index(), None))
        github.write_index(publish.empty_index(), None, TARGET)
        calls = github.api.call_args_list
        self.assertTrue(
            any(
                c.args[0] == "git/refs"
                and c.kwargs["data"] == {"ref": "refs/heads/data-index", "sha": TARGET}
                for c in calls
            )
        )
        final = calls[-1]
        self.assertEqual(final.args[0], "contents/versions.json")
        self.assertEqual(final.kwargs["data"]["branch"], "data-index")
        self.assertNotIn("sha", final.kwargs["data"])

    def test_index_update_passes_the_read_sha_for_atomic_compare_and_swap(self):
        github = publish.GitHub("owner/repo")
        blob = {
            "sha": "old-sha",
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps(publish.empty_index()).encode()
            ).decode(),
        }
        github.api = Mock(return_value=blob)
        index, sha = github.read_index()
        github.write_index(index, sha, TARGET)
        self.assertEqual(github.api.call_args.kwargs["data"]["sha"], "old-sha")
        self.assertEqual(
            json.loads(
                base64.b64decode(github.api.call_args.kwargs["data"]["content"])
            ),
            index,
        )

    def test_large_index_uses_git_blob_and_broken_references_fail(self):
        github = publish.GitHub("owner/repo")
        data = base64.b64encode(json.dumps(publish.empty_index()).encode()).decode()
        github.api = Mock(
            side_effect=[{"sha": "blob-sha", "encoding": "none"}, {"content": data}]
        )
        self.assertEqual(github.read_index()[1], "blob-sha")
        self.assertEqual(github.api.call_args.args[0], "git/blobs/blob-sha")
        index = publish.empty_index()
        index["latest"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "latest reference"):
            publish.validate_index(index)
