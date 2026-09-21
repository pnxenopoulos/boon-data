# boon-data

Versioned Deadlock JSON stat catalogs for [Boon](https://github.com/pnxenopoulos/boon).
The pipeline downloads the `.vdata` files directly from
[SteamTracking/GameTracking-Deadlock](https://github.com/SteamTracking/GameTracking-Deadlock),
joins English names from the same commit, and publishes three JSON catalogs
and a JSON manifest as GitHub Release assets. `versions.json` lists available
Deadlock client versions, release timestamps, download URLs, and checksums.
It uses [uv](https://docs.astral.sh/uv/) to manage Python 3.11+ and dependencies.
No game install, Steam login, VPK extraction tools, or map images are needed.

Scripts and tests live on the default branch; the publisher maintains
`versions.json` on a separate `data-index` branch. Generated bundles go under
`.work/` and are ignored. Boon support for this JSON format and client-version
lookup will be implemented separately.

## Build a snapshot

```bash
uv sync --locked

# Resolve master to a commit and build its JSON catalogs and manifest.
uv run --locked python scripts/pipeline.py

# Inspect the source commit and game versions without building.
uv run --locked python scripts/pipeline.py --resolve

# Build a historical snapshot using an upstream commit or tag.
uv run --locked python scripts/pipeline.py --ref <upstream-commit>
```

An optional `GH_TOKEN` or `GITHUB_TOKEN` raises the GitHub API rate limit.
The token is sent only to the GitHub API; raw source downloads are public.

The pipeline:

1. Resolves the requested upstream ref to its full commit SHA.
2. Reads `game/citadel/steam.inf` at that commit.
3. Locates `abilities.vdata`, `heroes.vdata`, and `modifiers.vdata` in
   `game/citadel/pak01_dir/scripts/` at the same commit.
4. Downloads those three files and verifies their sizes and Git blob hashes
   against the directory listing. Missing files are an error.
5. Downloads and verifies the English hero, ability, item, and modifier
   localization files at the same commit.
6. Parses the three catalog VData files, joins names, and writes JSON catalogs.
7. Writes `manifest.json` with provenance, game identifiers, catalog schemas
   and record counts, and SHA-256 hashes for every input and output file.

Release tags include the source game version, upstream commit prefix, and a
dataset revision: `<ClientVersion>-<commit-prefix>-r<dataset-prefix>`. Full
SHA-256 fingerprints and the full upstream SHA are recorded in the manifest.
The dataset revision covers the shipped inputs, catalog schema version, and
generator code/writer revision. A catalog correction can therefore produce a
new immutable release for the same upstream commit.

The GitHub release title is **Deadlock 6698**, for example. Users select client
version `6698` through `versions.json`; the longer tag is an internal immutable
identifier. Both hash prefixes have 12 hexadecimal characters.

The publisher compares content fingerprints before building. A new game version
with identical inputs reuses an existing snapshot and only updates the version
index. The snapshot retains its original source provenance; the index records
the newer observation separately. Rebuilding an existing release checks that
its artifacts are identical, and never overwrites different artifacts.

```text
.work/dist/<release>/
├── manifest.json
├── abilities.json
├── heroes.json
└── modifiers.json
```

These four JSON files are the complete release. ZIP archives and Parquet files
are not built or uploaded by the publisher. The manifest pins the original
upstream commit and input hashes, so source files can be fetched again for audits.

## Stat catalogs

The catalogs describe exported game definitions. They do not calculate a player's
current resistance, lifesteal, or healing output. Values retain source
units and meanings: `13` is not automatically divided by 100, and a duration
sentinel such as `-1` is not converted to zero. Combining passive, active,
conditional, upgrade, and scaled values requires separate game logic.

Each JSON catalog contains `source_commit` and `client_version`, and each record
contains its identity and complete parsed `definition`.
KV3 annotations are represented as `{"$type": "subclass", "$value": {...}}`.
Missing localization is null; unspecified source fields remain absent from
`definition`. Disabled, unreleased, and template definitions are retained
for historical analysis.

### JSON and future stats

`abilities.json`, `heroes.json`, and `modifiers.json` expose complete parsed
source definitions as nested JSON objects, together with IDs and English names:

```json
{
  "schema_version": 1,
  "catalog": "abilities",
  "source_commit": "<full upstream SHA>",
  "client_version": "6698",
  "records": [
    {
      "ability_name": "upgrade_damage_recycler",
      "ability_id": 865846625,
      "display_name": "Leech",
      "definition": {
        "m_mapAbilityProperties": {
          "BulletLifestealPercent": {"m_strValue": "28"}
        }
      }
    }
  ]
}
```

This example is abbreviated; actual definitions retain every parsed field.
Modifier JSON records additionally carry their source file, definition path,
and owning ability/hero identity. KV3 type annotations are preserved using
`$type` and `$value`.

New properties and other fields are automatically retained in each nested
`definition`; no hardcoded stat list is needed. Property values, scaling functions,
provided properties, and modifier bindings remain at their source paths.
New source syntax or changed gameplay semantics may still require a parser or
interpretation update. Changes to the JSON envelope require schema versioning.

```python
import json
from pathlib import Path

snapshot = Path(".work/dist/<release-key>")
abilities = json.loads((snapshot / "abilities.json").read_text(encoding="utf-8"))
leech = next(r for r in abilities["records"] if r["ability_name"] == "upgrade_damage_recycler")
print(leech["definition"]["m_mapAbilityProperties"]["BulletLifestealPercent"])
```

## Optional local Parquet exports

For local analysis, the catalog builder can optionally export four Parquet
tables. Fetch the source files pinned by a manifest and write the exports to a
separate folder. Run this from the repository root:

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from catalogs import build_catalogs
from pipeline import download_inputs

snapshot = Path(".work/dist/<release-key>")
output = Path(".work/parquet/<release-key>")
output.mkdir(parents=True, exist_ok=True)
source = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
vdata, localization = download_inputs(source)
build_catalogs(vdata, localization, source, output, parquet=True)
```

This writes JSON copies alongside the local Parquet exports. Every table row
contains `source_commit`, `client_version`, and `definition_json`. Ability, hero,
and modifier rows also contain `class_name`, `is_template`, `disabled`, and
`base_names`. New `m_mapAbilityProperties` keys automatically become rows in
`ability_properties.parquet` and columns in `abilities.parquet`; other fields
remain in `definition_json`.

### abilities.parquet

One row per top-level definition in `abilities.vdata`, keyed by `ability_name`.
Items use the same identity columns as other abilities; filter `is_item` to
select definitions whose `m_eAbilityType` is `EAbilityType_Item`.

| Columns | Contents |
| --- | --- |
| `ability_name`, `ability_id` | Internal item/ability string and unsigned 32-bit Source 2 string token, matching Boon's IDs |
| `display_name` | English in-game name joined by exact localization token |
| `is_item`, `item_tier`, `item_slot_type`, `activation` | Source item category and activation metadata |
| `stat_<property>` | One nullable Float64 column per `m_mapAbilityProperties` key, with its original spelling |
| `properties` | List of property structs: `name`, numeric `value`, `value_json`, `provided_property`, `usage_flags`, `display_type`, `display_units`, `definition_json` |
| `upgrades_json` | Full `m_vecAbilityUpgrades`, including tier-specific property changes |

For example, the `6698-a1139b2e4533` snapshot contains:

| ability_name | ability_id | display_name | stat_BulletLifestealPercent | stat_ActiveBonusLifesteal | stat_AbilityDuration |
| --- | ---: | --- | ---: | ---: | ---: |
| upgrade_vampire | 499683006 | Bullet Lifesteal | 13 | null | 0 |
| upgrade_surging_power | 1055679805 | Vampiric Burst | 13 | 70 | 5 |

A numeric zero remains zero. Missing properties, expressions, lists of tier
values, and non-numeric values are null in scalar columns; inspect `properties`
for their actual definitions. Scalar columns are discovered from each snapshot,
so a property that does not exist anywhere in that version has no column.
Scaling functions and display units are preserved inside property definitions.
The passive and active lifesteal values above are separate source properties;
the catalog does not interpret their combination.

### ability_properties.parquet

One row per property defined in an ability's `m_mapAbilityProperties`, keyed by
`(ability_id, property_name)` within a snapshot. This table makes the nested
`abilities.properties` fields directly queryable. Abilities with no properties
produce no rows. Include `source_commit` in joins across snapshots.

| Columns | Contents |
| --- | --- |
| `ability_name`, `ability_id`, `display_name` | Owning ability/item's identity and English display name |
| `is_item`, `is_template`, `disabled` | Owning ability's classification and source flags |
| `property_name`, `value`, `value_json` | Original property name, nullable numeric value, and JSON-encoded source value |
| `provided_property`, `usage_flags`, `display_type`, `display_units` | The same property metadata preserved in `abilities.properties` |
| `scale_function` | Scaling subclass's `_class` |
| `scaling_stat`, `scaling_stats` | `m_eSpecificStatScaleType` and `m_vecScalingStats`, respectively |
| `scaling_coefficient`, `street_brawl_scaling_coefficient` | `m_flStatScale` and `m_flStreetBrawlStatScale`, without inferred defaults |
| `scaling_disabled` | Explicit `m_bFunctionDisabled` flag, or null |
| `scale_function_json` | Complete `m_subclassScaleFunction`, retaining KV3 type annotations |
| `modifier_bindings` | List of explicitly bound modifiers, each with `modifier_name`, `modifier_id`, `source_file`, and `definition_path` |
| `definition_json` | Complete property definition, including fields not promoted to columns |

Missing scaling metadata stays null; an explicit zero coefficient stays zero.
A scaling class can have behavior not expressed by these convenience columns;
they do not imply a universal formula or an additive/multiplicative stacking rule.
`modifier_bindings` reverses the owning ability's explicit auto-registration
bindings in `modifiers.parquet`. An empty list means no such binding was found;
other modifier application logic may be implemented in the game.

### heroes.parquet

One row per top-level definition in `heroes.vdata`, keyed by `hero_name`.
`hero_id` comes from `m_HeroID`, and `display_name` uses the `hero_name:n`
localization token. `player_selectable` preserves the source flag.

- `base_<stat>`: numeric values from `m_mapStartingStats`.
- `level_<stat>`: numeric values from `m_mapStandardLevelUpUpgrades`.
- `scaling_stats_json`: complete `m_mapScalingStats` definitions.
- `bound_abilities_json`: slot-to-ability mapping from `m_mapBoundAbilities`.

For example, this snapshot exports Lash's
`base_ETechArmorDamageReduction = 10` and Dynamo's
`level_MODIFIER_VALUE_BULLET_ARMOR_DAMAGE_RESIST = 0.625`.

### modifiers.parquet

One row per modifier definition **in its source context**. Includes all top-level
entries in `modifiers.vdata`, plus named nested subclasses whose `_class` starts
with `modifier_` in the ability, hero, and modifier files.

| Columns | Contents |
| --- | --- |
| `source_file`, `definition_path` | Composite row key; the path is a JSON Pointer within the parsed source |
| `modifier_name`, `modifier_id`, `display_name` | Subclass name, unsigned string token, and English name where available |
| `ability_name`, `ability_id`, `hero_name`, `hero_id` | Owning ability or hero, when defined in those files |
| `bound_properties` | Properties named by `m_vecAutoRegisterModifierValueFromAbilityPropertyName`, resolved against the owning ability's property map |
| `is_hidden`, `debuff_type` | Explicit source flags |
| `stat_<field>` | Direct numeric modifier fields, such as a duration or scale |

`modifier_id` is deliberately not a unique row key. Two items can use
`modifier_intrinsic_base` with different bindings and values. Shared modifiers
without an owning ability keep unresolved bindings with null definitions;
`definition_json` and the source path retain the surrounding evidence.

### Query the catalogs

```python
from pathlib import Path
import polars as pl

snapshot = Path(".work/parquet/<release-key>")
abilities = pl.scan_parquet(snapshot / "abilities.parquet")
items = abilities.filter(pl.col("is_item") & ~pl.col("is_template"))
print(items.select(
    "ability_name", "ability_id", "display_name", "disabled",
    "stat_BulletLifestealPercent", "stat_AbilityDuration",
).collect())

properties = pl.scan_parquet(snapshot / "ability_properties.parquet")
print(properties.filter(
    pl.col("provided_property") == "MODIFIER_VALUE_BULLET_LIFESTEAL"
).select(
    "ability_name", "property_name", "value", "scaling_stat",
    "scaling_coefficient", "modifier_bindings",
).collect())
```

The catalogs preserve the fields exported in the three source files. They do
not reimplement engine defaults, resolve `_base`/`_multibase` inheritance, follow
`_include` files, or extract modifiers from other VData categories. Base names
remain on each row, and root include metadata remains in the manifest.
The JSON definitions retain these source fields without resolving them.

## Manifest format

The JSON-only manifest has `schema_version: 2`:

| Field | Meaning |
| --- | --- |
| `source_key` | Observed source identity, `<ClientVersion>-<commit-prefix>` |
| `release_key` | Immutable release tag, `<source_key>-r<dataset-prefix>` |
| `source.repository` | `SteamTracking/GameTracking-Deadlock` |
| `source.commit` | Full upstream commit SHA |
| `source.committed_at` | Upstream committer timestamp, used to prevent older observations replacing newer ones |
| `source.path` | Source VData directory |
| `client_version`, `server_version` | Values from `steam.inf` |
| `source_revision` | Engine source revision from `steam.inf` |
| `version_date`, `version_time` | Build timestamp strings from `steam.inf` |
| `files` | Map of the three VData input filenames to `{ "sha256": "…", "bytes": N }` |
| `localization_files` | Map of upstream localization paths to SHA-256 and byte count |
| `artifacts` | Map of the three JSON catalogs to SHA-256 and byte count (the manifest itself is fingerprinted in the index) |
| `snapshot.content_sha256` | Fingerprint of the three VData and four English localization input names, hashes, and sizes |
| `snapshot.generator_sha256` | Fingerprint of the catalog/parser/packaging code, `pyproject.toml`, `uv.lock`, and installed Polars version |
| `snapshot.dataset_sha256` | Fingerprint combining content, generator revision, and catalog schema version |
| `catalogs.schema_version` | Catalog structure version; `4` publishes JSON catalogs and keeps Parquet exports local |
| `catalogs.polars_version` | Polars version used by the catalog builder |
| `catalogs.tables` | Empty in published manifests; row counts and column types when exporting local Parquet |
| `catalogs.json_catalogs` | JSON envelope schema version and record count for each JSON catalog |
| `catalogs.vdata_metadata` | JSON-encoded root metadata from the three parsed VData files |

Identifiers are strings; unavailable optional `steam.inf` fields are `null`.
There is no inferred Steam build ID or demo build mapping. Packaging timestamps
are omitted so rebuilding the same source with the same toolchain produces the
same artifacts. Dataset revision tags allow multiple generator revisions of a
source snapshot to coexist. Manifest schema `2` replaces the earlier raw-archive
contract. The publication timestamp lives in the version index, where it is
recorded after GitHub publishes the verified release. Published assets are
never replaced.

## Publishing and change detection

`.github/workflows/build-assets.yml` checks hourly, at minute 0 (UTC), and
supports manual runs with an upstream `ref`. A check publishes a new
release only when the dataset changes. It invokes `scripts/publish.py --publish`:

1. Read the version index and recover any complete release published before an
   interrupted index update.
2. Resolve and pin the requested upstream ref; download and verify its inputs.
3. Compare the content, generator, and schema fingerprint against indexed
   snapshots. Version numbers and commit timestamps do not create new content.
4. If the dataset is new, build **all four JSON assets**: `abilities.json`,
   `heroes.json`, `modifiers.json`, and `manifest.json`.
5. Upload to a draft, verify the complete asset list, sizes, and SHA-256 hashes,
   and only then publish the release. Interrupted draft uploads can resume.
6. Record the client version, asset URLs/checksums, and the release's actual
   GitHub `published_at` timestamp (normalized to UTC as `released_at`). Reuse an
   existing snapshot when inputs and generator revision are identical.
7. Commit the index atomically to `data-index/versions.json`, using the previous
   file SHA to reject conflicting updates. Runs against `master` update `latest`
   to the client version; explicit historical refs do not advance it. Older
   observations cannot replace a newer commit for the same client version.

The fingerprint includes the **three catalog VData files and four English
localization files**, including property additions and removals. Changes to
other upstream VData files do not trigger releases. A reversion to earlier content can
reuse that earlier snapshot. GitHub's latest release follows the snapshot used
by the latest indexed observation, even when that snapshot is reused.

The default workflow needs only its built-in `GITHUB_TOKEN` with contents-write
permission. The `data-index` branch is created on first publication. The index
is committed only after the snapshot is complete and verified; a failed index
commit is recoverable on a later run. Published assets are immutable, and a
missing/corrupt published asset fails verification rather than being replaced.

Preview without changing GitHub:

```bash
# Read the public index, prepare artifacts, and write a candidate plan/index locally.
uv run --locked python scripts/publish.py

# Use a local index (a missing file starts an empty preview index).
uv run --locked python scripts/publish.py --index .work/seed-versions.json --output .work/preview

# Preview another observation against the candidate index to exercise reuse.
uv run --locked python scripts/publish.py --index .work/preview/versions.json --output .work/preview
```

Previews write `plan.json` and a **candidate** `versions.json` under the output
root, plus a snapshot directory when a build is needed. Unpublished snapshots
have `released_at: null`; previews never invent a release timestamp. They do not
publish or commit anything. Actual publication requires `--publish`, a repository, and the
boon-data generator commit in `--target`/`GITHUB_SHA`. The workflow supplies
these through `GITHUB_REPOSITORY` and `GITHUB_SHA`.

To enable scheduled publication, commit and push this repository, including
`.github/workflows/`, to its default branch with GitHub Actions enabled. The
hourly schedule then runs automatically. GitHub may delay scheduled runs, so the
schedule is a polling interval rather than an exact publication deadline.

For the first release or any manual check, open **Actions → Build boon-data →
Run workflow**. Select the default branch for the workflow code and leave the
upstream `ref` input as `master`, or enter an upstream branch, tag, or commit to
build a specific snapshot. The same content checks apply to manual runs, so an
unchanged dataset reuses the existing release. Manual runs never overwrite
published assets. If inputs or the generator change, the publisher creates a
new immutable revision. Interrupted drafts resume by verifying existing assets
and uploading only missing files; mismatched assets fail instead of being replaced.

The equivalent GitHub CLI command is:

```bash
gh workflow run build-assets.yml --repo pnxenopoulos/boon-data -f ref=master
```

Both triggers use the same publisher and concurrency group, preventing scheduled
and manual publication from running simultaneously. The built-in workflow token
supplies repository write access; no additional release token is required.

Polling records the versions it observes; it does not yet scan all intermediate
upstream commits. Use an explicit upstream `ref` to backfill missing versions.

## Version index

After publication, the index is available at:

```text
https://raw.githubusercontent.com/pnxenopoulos/boon-data/data-index/versions.json
```

The index schema is `2`:

| Field | Meaning |
| --- | --- |
| `source_repository` | The upstream GameTracking repository |
| `latest` | Latest available client version observed on `master`, such as `"6698"`; null until one is observed |
| `versions` | Map keyed by Deadlock client version |
| `snapshots` | Internal history of immutable snapshots, keyed by release tag, used for content reuse and recovery |

Each `versions["6698"]` entry contains:

| Field | Meaning |
| --- | --- |
| `client_version` | `"6698"`, matching the map key |
| `released_at` | UTC timestamp of the associated boon-data GitHub release, such as `"2026-09-21T16:00:00Z"` |
| `artifacts` | All four JSON files, each with `url`, SHA-256 `sha256`, and byte count `bytes` |
| `snapshot` | Internal immutable release tag; users do not need to select it |
| `source` | Observed upstream repository, full commit SHA, commit timestamp, and source path |
| `server_version`, `source_revision`, `version_date`, `version_time` | Original game build metadata from `steam.inf` |

`released_at` is the **boon-data publication time**, not the upstream commit
or game build time. Versions that share an unchanged snapshot share its original
publication timestamp and download URLs; their observed source metadata remains
separate. JSON files and their manifest retain the snapshot's original client
version and source commit. Consumers should use the index to resolve the
requested client version, rather than require it to match the snapshot's origin.

There is one current entry per client version. A newer upstream commit for the
same version updates that entry. A catalog correction creates a new immutable
snapshot and updates the entry's URLs, checksums, and publication timestamp.
Earlier snapshots remain in `snapshots` and earlier mappings in the index's Git
history. Recovery selects the newer source observation, then the later publication
for corrections to the same observation, independent of the release listing order.

```python
import json
from urllib.request import urlopen

url = "https://raw.githubusercontent.com/pnxenopoulos/boon-data/data-index/versions.json"
with urlopen(url) as response:
    index = json.load(response)

for version, entry in sorted(index["versions"].items(), key=lambda item: int(item[0])):
    print(version, entry["released_at"])

entry = index["versions"]["6698"]  # or index["versions"][index["latest"]]
print(entry["artifacts"]["abilities.json"])  # url, sha256, bytes
```

The central index contains availability and release metadata. Installation status
belongs to the consuming client: compare the selected version's checksums with
its locally cached files to distinguish installed, missing, or outdated data.

## Planned Boon integration

Boon's current raw-VData downloader does not support these JSON-only releases.
Updating Boon is a separate task. The intended interface is:

```text
boon versions          # client version, boon-data release timestamp, local status
boon get 6698          # fetch the four JSON files for this client version
boon get               # use versions.json's latest client version
boon versions --local  # inspect the local cache without network access
```

The intended cache is `~/.boon/<client-version>/`, containing `abilities.json`,
`heroes.json`, `modifiers.json`, and `manifest.json`. The index provides the exact
URLs and checksums needed to verify those downloads and detect catalog corrections.
These commands and the new cache layout are not implemented by this repository.

**A release's `ClientVersion` is not `demo.build`.** Matching a replay's header
to the appropriate source snapshot remains separate work. The catalogs do not
calculate resistance or lifesteal percentages for a live player.

## Development

```bash
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

`pyproject.toml` declares dependencies and `uv.lock` pins the resolved versions.
`uv sync --locked` creates `.venv/` and checks the lockfile without changing it.
The optional `.python-version` selects Python 3.13 for local runs and releases.
`requires-python` in `pyproject.toml` declares compatibility with Python 3.11+;
CI overrides the default to test both 3.11 and 3.13 through `astral-sh/setup-uv`.
Both workflows pin uv to 0.11.28 and use `uv run --locked`.
The scripts are a uv virtual project and are not installed as a Python package.

The current catalog builder still uses Polars internally, including for JSON
output, so it remains a pinned dependency. Removing it requires a separate
catalog refactor.

Tests use synthetic source responses and do not access the network. They cover
KV3/KV1 parsing, identity and localization joins, nullable numeric values,
modifier context, JSON/Parquet preservation of new fields, checksums, reproducible
builds, content reuse, publication timestamps, client-version lookup, draft
recovery, and atomic index updates. CI runs on Linux and Windows with Python
3.11 and 3.13.

The build scripts are MIT licensed. The upstream game data remains Valve's;
this repository does not grant a license to Valve's game assets. Source
provenance is included in every bundle.
