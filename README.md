# boon-data

Versioned Deadlock JSON stat catalogs for [Boon](https://github.com/pnxenopoulos/boon).
The pipeline downloads the `.vdata` files directly from
[SteamTracking/GameTracking-Deadlock](https://github.com/SteamTracking/GameTracking-Deadlock),
joins English names from the same commit, and publishes four JSON catalogs
and a JSON manifest as GitHub Release assets. `versions.json` lists available
Deadlock client versions, release timestamps, download URLs, and checksums.
It uses [uv](https://docs.astral.sh/uv/) to manage Python 3.11+ and dependencies.
No game install, Steam login, VPK extraction tools, or map images are needed.

Scripts and tests live on the default branch; the publisher maintains
`versions.json` on a separate `data-index` branch. Generated bundles go under
`.work/` and are ignored. This repository supplies the JSON catalogs and client-version index.

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
3. Locates `abilities.vdata`, `heroes.vdata`, `modifiers.vdata`, `misc.vdata`,
   `npc_units.vdata`, and `generic_data.vdata` in
   `game/citadel/pak01_dir/scripts/` at the same commit.
4. Downloads those six files and verifies their sizes and Git blob hashes
   against the directory listing. Missing files are an error.
5. Downloads and verifies the English hero, ability, item, modifier, and general UI
   localization files, selected engine schemas, and client/server string dumps
   at the same commit.
6. Parses the six VData files, joins names, and writes JSON catalogs.
7. Writes `manifest.json` with provenance, game identifiers, catalog record counts,
   and SHA-256 hashes for every input and output file.

Release tags are the Deadlock **ClientVersion**, for example `6698`, with the
GitHub release title **boon-data-6698**. The release URL is
`https://github.com/pnxenopoulos/boon-data/releases/tag/6698`.
Full SHA-256 fingerprints and the full upstream commit SHA remain in the
manifest for provenance and verification; they are not part of the tag.

A client-version tag cannot hold two different snapshots. Rerunning an existing
version with identical source inputs verifies and reuses its
published files, even if the generator has changed. Different source content for
an already released version fails without replacing its assets.

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
├── modifiers.json
└── misc.json
```

These five JSON files are the complete release. ZIP archives and Parquet files
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

`abilities.json`, `heroes.json`, `modifiers.json`, and `misc.json` expose complete parsed
source definitions as nested JSON objects, together with IDs and English names:

```json
{
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

This example is abbreviated; actual files also include lookup indexes and record links,
and definitions retain every parsed field.
Modifier JSON records additionally carry their source file, definition path,
and owning ability/hero/misc identity. `modifier_id` preserves the unqualified
name token. `qualified_modifier_name` and `qualified_modifier_id` also identify
nested modifiers as `owner_name/modifier_name`, matching observed replay IDs
for ability and pickup modifiers. Standalone modifiers use their original name. KV3 type annotations are preserved using
`$type` and `$value`.

New properties and other fields are automatically retained in each nested
`definition`; no hardcoded stat list is needed. Property values, scaling functions,
provided properties, and modifier bindings remain at their source paths.
New source syntax or changed gameplay semantics may still require a parser or
interpretation update.

```python
import json
from pathlib import Path

snapshot = Path(".work/dist/<release-key>")
abilities = json.loads((snapshot / "abilities.json").read_text(encoding="utf-8"))
leech = next(
    r for r in abilities["records"] if r["ability_name"] == "upgrade_damage_recycler"
)
print(leech["definition"]["m_mapAbilityProperties"]["BulletLifestealPercent"])
```

### Lookups and declared stat changes

Boon reads hero, ability/item, and modifier name maps from these catalogs.
Python and Rust use the newest verified local installation, downloading latest
when none is installed. Consumers can explicitly select a client version.
Name maps use the IDs already present in records. Hero labels use `display_name`
with `hero_name` as the fallback. Modifier lookups include both unqualified and
owner-qualified names. Duplicate labels can share a map entry; the record
indexes below retain every candidate definition for gameplay analysis.

Each catalog contains `records` and lookup `indexes`. Each record retains its raw
`definition` and has a `record_key`, `source_file`, and `definition_path`. A record key combines the
source file and logical definition path, for example
`abilities.vdata#/upgrade_vampire`. Path segments escape `~` as `~0` and `/` as
`~1`; KV3 `$type`/`$value` wrappers are transparent in these logical paths.
Record keys identify a definition location within a snapshot, not a live entity.

Every catalog has these `indexes`:

| Index | Key | Value |
| --- | --- | --- |
| `by_id` | Decimal ID string | List of positions in `records` |
| `by_name` | Internal name | List of positions in `records` |
| `by_key` | Unique `record_key` | One position in `records` |
| `by_qualified_id` (modifiers only) | Decimal owner-qualified ID string | List of positions in `records` |
| `by_qualified_name` (modifiers only) | Owner-qualified modifier name | List of positions in `records` |

ID and name indexes always return lists. Shared modifier names and even repeated
owner-qualified names can refer to different definition paths. Keep all candidates
and use the replay's owning ability/hero and context to distinguish them; do not
silently choose the first match. Missing keys mean no match in this snapshot.
Different names colliding on one hash fail the build. Never mix record positions
or references from different snapshots. Hero IDs still come from `m_HeroID`,
not from hashing the hero name.

Ability records expose `properties`, keyed by property name, and `modifier_keys`
linking to their embedded modifier records. Each property contains its numeric
`value` when it is a finite scalar literal, original `raw_value`, declared `stat`
(`m_eProvidedPropertyType`), usage flags, display units, original `scaling`
definition, source record/path, and the `modifier_keys` explicitly bound to it.
Zero stays zero; missing values, expressions, and lists of tier values are not
coerced to numbers. Other source fields remain in the original `definition`.

Modifier records expose `property_bindings`. Each binding identifies the source
record and property name, with `status: "resolved"` and a `property` object when
the owning definition declares it. Otherwise it has `status: "unresolved"` and
`property: null`. Resolution means the property was found; its `stat` or numeric
`value` can still be null. No inheritance or engine defaults are inferred.
Root hero, misc, and standalone modifier records also link to their embedded
modifiers through `modifier_keys`.

An explicit `m_NonEmbeddedModifier` reference with `m_bUseNonEmbedded: true`
also links an ability property to a standalone modifier. Its bound effect has
`source_ability_id`. A shared modifier can have bindings from several abilities.
Select the binding that matches the replay's source ability. If that identity is
missing, do not select the first binding or add all values. The original source
`definition` stays unchanged. These links use explicit references and registrations;
they do not infer an effect from a class name.


Records expose `stat_changes` for explicit declarations:

- `kind: "ability_property"`: an ability property with a declared stat mapping,
  including its scaling and bound modifier keys.
- `kind: "bound_property"`: a modifier's explicitly bound property with a declared
  stat mapping, including its value and scaling from the owning definition.
- `kind: "m_vecScriptValues"`: direct modifier stat/value entries.
- `kind: "m_vecModifierValues"`: direct modifier stat ranges, preserving separate
  `value_min` and `value_max`; no point within the range is chosen.

Each entry retains its source record and definition path. Direct entries also
retain their original `definition`. These are declarations, not calculated or
necessarily active bonuses. The ability property and its bound modifier describe
the same source effect: do not sum them together. A record's own `stat_changes`
does not include changes from its embedded modifiers; follow `modifier_keys`.
Values retain source units (for example, `13` percent stays `13`, not `0.13`).
Activation conditions, duration parameters, upgrade tiers, and any additional
fields remain in `definition`. Empty `stat_changes` does **not** establish that a
modifier has no effects: engine-defined effects and unresolved bindings are not
converted into invented stat mappings. Hero starting stats, level/purchase bonuses,
and scaling remain under the corresponding maps in the hero's `definition`.

```python
import json
from pathlib import Path

snapshot = Path(".work/lookup-preview/6698")
abilities = json.loads((snapshot / "abilities.json").read_text())
modifiers = json.loads((snapshot / "modifiers.json").read_text())

# An ability/item ID from the replay: Bullet Lifesteal.
for position in abilities["indexes"]["by_id"].get("499683006", []):
    ability = abilities["records"][position]
    for key in ability["modifier_keys"]:
        modifier = modifiers["records"][modifiers["indexes"]["by_key"][key]]
        print(modifier["qualified_modifier_name"], modifier["stat_changes"])

# An owner-qualified modifier ID from the replay; retain every candidate.
positions = modifiers["indexes"]["by_qualified_id"].get("1373598984", [])
candidates = [modifiers["records"][position] for position in positions]
```

### misc.json

The `generic_data` field preserves `generic_data.vdata`, including item prices
by tier (`m_nItemPricePerTier`). These prices support the hero purchase-bonus
tables. Rebuild a release to add this field to an existing catalog.

One record per top-level definition in `misc.vdata` and `npc_units.vdata`, keyed
by `misc_name`. `source_file` and `record_key` retain the original input file.
`misc_id` is its unsigned Source 2 string token. `display_name` uses the explicit
`m_sNameLocString` localization token when present, otherwise the definition name;
missing English names stay null. The entire parsed `definition` is preserved,
including temporary power-ups, permanent pickups, NPCs, spawners, neutral camps,
breakable props, and definitions without an explicit `_class`.

For client 6698, `gun_powerup_pickup` contains:

| Source field | Value |
| --- | --- |
| `m_sModifer.$value.m_flDuration` | `160` seconds |
| `m_sModifer.$value.m_flTimeMin`, `m_flTimeMax` | `5`, `40` |
| `MODIFIER_VALUE_FIRE_RATE` in `m_vecModifierValues` | `m_valueMin=12`, `m_valueMax=35` |
| `MODIFIER_VALUE_AMMO_CLIP_SIZE_PERCENT` in `m_vecModifierValues` | `m_valueMin=35`, `m_valueMax=70` |

The source spells `m_sModifer` this way; JSON retains that exact spelling.
The endpoints and time parameters remain source data, not a calculated live
bonus. Applying the engine's time-scaling rule and determining whether a pickup
is active belong in Boon's rulesets.

The same nested modifier appears in `modifiers.json`, with
`misc_name="gun_powerup_pickup"`, `misc_id=201785745`,
`qualified_modifier_name="gun_powerup_pickup/gun_powerup_pickup"`, and
`qualified_modifier_id=2161948557`, matching the buff observed on Victor.
Permanent pickups preserve their `m_vecScriptValues` separately; for example,
`ammo_permanent_pickup_lv2` supplies `MODIFIER_VALUE_AMMO_CLIP_SIZE_PERCENT=5`.
No duration is invented when the source omits it.

```python
misc = json.loads((snapshot / "misc.json").read_text(encoding="utf-8"))
gun = next(r for r in misc["records"] if r["misc_name"] == "gun_powerup_pickup")
print(gun["definition"]["m_sModifer"]["$value"]["m_vecModifierValues"])
```

NPC definitions share `misc.json` so the release still contains four catalogs.
Their nested modifiers appear in `modifiers.json`, with `misc_name` and `misc_id`
pointing to the NPC root. All effects keep their source paths and raw definitions.

For example, the friendly Walker aura in snapshot 6694 has the qualified name
`npc_boss_tier2_weak/friendly_aura/target_near_walker` and ID `1633171260`.
Its `stat_changes` contain `MODIFIER_VALUE_TECH_RESIST=15` and
`MODIFIER_VALUE_BULLET_ARMOR_DAMAGE_RESIST=15`, read from `m_vecScriptValues`.
The parent aura retains its friendly-target filter and radius. These values are
source bonuses; Boon must check aura state and apply the resistance rules.

## Optional local Parquet exports

For local analysis, the catalog builder can optionally export five Parquet
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
with `modifier_` or `citadel_modifier_` in the ability, hero, modifier, misc, and
NPC files. This includes nested Ivy tether modifiers and their explicit property
bindings.

| Columns | Contents |
| --- | --- |
| `qualified_modifier_name`, `qualified_modifier_id` | Owner-qualified modifier name and its string token; original name for standalone definitions |
| `source_file`, `definition_path` | Composite row key; the path is a JSON Pointer within the parsed source |
| `modifier_name`, `modifier_id`, `display_name` | Subclass name, unsigned string token, and English name where available |
| `ability_name`, `ability_id`, `hero_name`, `hero_id`, `misc_name`, `misc_id` | Owning ability, hero, or misc definition, when defined in those files |
| `bound_properties` | Properties named by `m_vecAutoRegisterModifierValueFromAbilityPropertyName`, resolved against the owning ability's property map |
| `is_hidden`, `debuff_type` | Explicit source flags |
| `stat_<field>` | Direct numeric modifier fields, such as a duration or scale |

In both JSON and Parquet, `display_name` first uses the English translation of
`modifier_name`. Only when that lookup is missing does it try the modifier's
`m_sLocalizationName` token. If neither resolves, the label is null. Existing
labels, including empty strings, remain unchanged: Spirit Snare's
`modifier_glitch_debuff` retains "Cursed!", while `modifier_healing_Nova_active`
can obtain "Healing Nova" through its fallback token. IDs and raw definitions
are unaffected.

Qualified modifier names include every containing subclass name. For example,
`citadel_koth_cashin/modifier_aura_idol_cashin/target_near_idol_cashin` hashes to
`3930103894`. Its JSON Pointer path still uses field names. The owner fields
continue to identify the root ability, hero, or world definition.

`modifier_id` is deliberately not a unique row key. Two items can use
`modifier_intrinsic_base` with different bindings and values. Shared modifiers
without an owning ability keep unresolved bindings with null definitions;
`definition_json` and the source path retain the surrounding evidence.

### misc.parquet

Optional local export of the misc definitions with `misc_name`, `misc_id`,
`display_name`, the shared definition/provenance columns, and `definition_json`.
Stats remain nested because world definitions have different structures.

### Query the catalogs

```python
from pathlib import Path
import polars as pl

snapshot = Path(".work/parquet/<release-key>")
abilities = pl.scan_parquet(snapshot / "abilities.parquet")
items = abilities.filter(pl.col("is_item") & ~pl.col("is_template"))
print(
    items.select(
        "ability_name",
        "ability_id",
        "display_name",
        "disabled",
        "stat_BulletLifestealPercent",
        "stat_AbilityDuration",
    ).collect()
)

properties = pl.scan_parquet(snapshot / "ability_properties.parquet")
print(
    properties.filter(pl.col("provided_property") == "MODIFIER_VALUE_BULLET_LIFESTEAL")
    .select(
        "ability_name",
        "property_name",
        "value",
        "scaling_stat",
        "scaling_coefficient",
        "modifier_bindings",
    )
    .collect()
)
```

The catalogs preserve the fields exported in the five source files. They do
not reimplement engine defaults, resolve `_base`/`_multibase` inheritance, follow
`_include` files, or extract modifiers from other VData categories. Base names
remain on each row, and root include metadata remains in the manifest.
The JSON definitions retain these source fields without resolving them.

## Manifest format

`manifest.json` records the source, generator, and output checksums:

| Field | Meaning |
| --- | --- |
| `source_key` | Observed source identity, `<ClientVersion>-<commit-prefix>` |
| `release_key` | Release tag, equal to `client_version` (for example, `6698`) |
| `source.repository` | `SteamTracking/GameTracking-Deadlock` |
| `source.commit` | Full upstream commit SHA |
| `source.committed_at` | Upstream committer timestamp; automatic runs keep the newest observation |
| `source.path` | Source VData directory |
| `client_version`, `server_version` | Values from `steam.inf` |
| `source_revision` | Engine source revision from `steam.inf` |
| `version_date`, `version_time` | Build timestamp strings from `steam.inf` |
| `files` | Map of the six VData input filenames to `{ "sha256": "…", "bytes": N }` |
| `localization_files` | Map of upstream localization paths to SHA-256 and byte count |
| `schema_files` | Map of enum and scaling-class schema paths to SHA-256 and byte count |
| `engine_string_files` | Map of client/server string-dump paths to SHA-256 and byte count |
| `artifacts` | Map of the four JSON catalogs to SHA-256 and byte count (the manifest itself is fingerprinted in the index) |
| `snapshot.content_sha256` | Fingerprint of the VData, localization, schema, and engine-string input names, hashes, and sizes |
| `snapshot.generator_sha256` | Fingerprint of the catalog/parser/packaging code, `pyproject.toml`, `uv.lock`, and installed Polars version |
| `snapshot.dataset_sha256` | Fingerprint combining content and generator revision |
| `catalogs.polars_version` | Polars version used by the catalog builder |
| `catalogs.tables` | Empty in published manifests; row counts and column types when exporting local Parquet |
| `catalogs.json_catalogs` | Record count for each JSON catalog |
| `catalogs.vdata_metadata` | JSON-encoded root metadata from the six parsed VData files |

Identifiers are strings; unavailable optional `steam.inf` fields are `null`.
There is no inferred Steam build ID or demo build mapping. Packaging timestamps
are omitted so rebuilding the same source with the same toolchain produces the
same artifacts. Each client-version tag identifies one immutable snapshot.
The publication timestamp lives in the version index, where it is recorded after
GitHub publishes the verified release. Published assets are never replaced.

## Publishing and change detection

`.github/workflows/build-assets.yml` checks hourly, at minute 0 (UTC), and
supports manual runs with an upstream `ref`. Both use
`scripts/publish.py --vdata-only --publish` to gate new releases on changes to
`abilities.vdata`, `heroes.vdata`, `modifiers.vdata`, `misc.vdata`,
`npc_units.vdata`, or `generic_data.vdata`:

1. Read the version index and GitHub's release list. Recover any complete release
   published before an interrupted index update. Only published releases can be
   reused; stale index entries for missing releases do not count.
2. Resolve and pin the requested upstream ref; download and verify its inputs.
3. Compare the six VData file hashes and sizes with the latest indexed snapshot.
   If unchanged, reuse that snapshot even if localization, generator code,
   or dependencies changed. If there is no `latest` yet, compare with the most
   recently observed backfilled version. If that release is missing or the index
   is empty, build the requested version.
4. When VData changes, use the full dataset fingerprint to reuse an identical
   earlier snapshot or build **all five JSON assets**: `abilities.json`,
   `heroes.json`, `modifiers.json`, `misc.json`, and `manifest.json`.
5. Upload to a draft, verify the complete asset list, sizes, and SHA-256 hashes,
   and only then publish the release. Interrupted draft uploads can resume.
6. Record the client version, asset URLs/checksums, and the release's actual
   GitHub `published_at` timestamp (normalized to UTC as `released_at`). Reuse an
   existing snapshot when the VData gate or full dataset comparison matches.
   A new client version with unchanged VData is still recorded against the reused
   snapshot, without creating a release or re-uploading assets.
7. Commit the index atomically to `data-index/versions.json`, using the previous
   file SHA to reject conflicting updates. Runs against `master` update `latest`
   to the client version; explicit historical refs do not advance it. Automatic
   runs retain the newest observation; backfills replace the selected version's
   observation with the requested commit's metadata.

The gate compares file contents, not upstream commit IDs. Localization-only,
schema-only, engine-string-only, metadata-only, generator, lockfile, and lint-tool updates do not trigger
new releases in **Build boon-data**. Their changes are incorporated when the next
VData change requires a build. Other upstream VData files do not trigger releases.
The full content and generator fingerprints remain in the manifest for
provenance and exact snapshot reuse. GitHub's latest release follows the snapshot
used by the latest indexed observation, even when that snapshot is reused.

Snapshot entries in the index retain the manifest's `files` hashes for the VData
comparison.

The default workflow needs only its built-in `GITHUB_TOKEN` with contents-write
permission. The `data-index` branch is created on first publication. The index
is committed only after the snapshot is complete and verified; a failed index
commit is recoverable on a later run. Published assets are immutable, and a
missing/corrupt published asset fails verification rather than being replaced.

Preview without changing GitHub:

```bash
# Preview the hourly workflow's VData gate against the public index.
uv run --locked python scripts/publish.py --vdata-only

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
check a specific snapshot. The same VData gate applies to manual runs of
**Build boon-data**. Use **Backfill boon-data** for an explicit historical build
with full dataset matching instead of the VData gate. Manual runs never overwrite
published assets. Any new snapshot requires an unused client-version tag. Interrupted drafts resume by verifying existing assets
and uploading only missing files; mismatched assets fail instead of being replaced.

The equivalent GitHub CLI command is:

```bash
gh workflow run build-assets.yml --repo pnxenopoulos/boon-data -f ref=master
```

All publication workflows use the same publisher and concurrency group, preventing
scheduled and manual publication from running simultaneously. The built-in workflow
token supplies repository write access; no additional release token is required.

### Backfill historical data

Open **Actions → Backfill boon-data → Run workflow**. Select the default branch
for the boon-data workflow code and enter the full 40-character commit SHA from
`SteamTracking/GameTracking-Deadlock` in `source_commit`. Choose the upstream
commit whose file tree represents the point in time you want to capture; the
input is not a boon-data commit, client version, or date.

```bash
gh workflow run backfill.yml --repo pnxenopoulos/boon-data \
  -f source_commit="<full-upstream-commit-sha>"
```

The backfill workflow deliberately omits `--vdata-only`: it compares all VData,
localization, schema, and engine-string inputs. An existing client version with matching inputs reuses its
published snapshot across generator updates. For a new version, reuse requires
matching both content and generator fingerprints. It reads
`steam.inf`, all six VData files, English localization, selected schemas, and
engine strings from that exact commit. It publishes the same five JSON assets and records the
source commit, client version, build date/time, and publication time in the index.
New releases are named `boon-data-<ClientVersion>` with tag `<ClientVersion>`.
Identical datasets reuse an existing snapshot; the historical client version is
added to or replaced in `versions.json`. A backfill replaces that client version's
entry with the selected commit, source revision, and build date/time, even when an
existing entry points to a newer commit. The updated file is written to the
`data-index` branch after release verification. Unrelated versions and snapshots,
and the `latest` pointer are preserved. An unchanged index needs no new commit.
Existing releases are never overwritten: rerunning the same
source content verifies and reuses the published files; it does not regenerate
them with newer code. Different source content for an already-published
client-version tag fails. Missing historical inputs also fail instead of using
current files.

If the requested release and its Git tag were deleted, backfilling the same source
content rebuilds it with the current generator and updates its index metadata after
verification. Before creating the release, the publisher checks the Git tag
separately. If a tag remains without a release, publication stops with an error;
remove that leftover tag explicitly before recreating the release. The publisher
never deletes or moves existing tags.
Versions sharing that rebuilt release receive its new checksums and publication
time while retaining their source observations. If the index write is interrupted,
the next run recovers the recreated release from its verified manifest. A matching draft can resume;
existing published assets are never overwritten. Local `--index` previews use only
the supplied index and do not check GitHub release availability.

A deleted latest release does not block publishing a different historical snapshot.
Backfills preserve unrelated index entries. To keep a release intentionally removed,
remove its snapshot and referencing versions from `versions.json` on the
`data-index` branch, and update `latest` to an available version or `null`.

Polling records only the versions it observes. Backfill runs process one selected
commit at a time; they do not scan all intermediate upstream commits.

## Version index

After publication, the index is available at:

```text
https://raw.githubusercontent.com/pnxenopoulos/boon-data/data-index/versions.json
```

`versions.json` contains:

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
| `artifacts` | All five JSON files, each with `url`, SHA-256 `sha256`, and byte count `bytes` |
| `snapshot` | Internal immutable release tag; users do not need to select it |
| `source` | Observed upstream repository, full commit SHA, commit timestamp, and source path |
| `server_version`, `source_revision`, `version_date`, `version_time` | Original game build metadata from `steam.inf` |

`released_at` is the **boon-data publication time**, not the upstream commit
or game build time. Versions that share an unchanged snapshot share its original
publication timestamp and download URLs; their observed source metadata remains
separate. JSON files and their manifest retain the snapshot's original client
version and source commit. Consumers should use the index to resolve the
requested client version, rather than require it to match the snapshot's origin.

There is one current entry per client version. An unchanged dataset can update
its source observation without changing release assets. A different dataset
cannot replace an existing client-version tag. Existing snapshots remain in
`snapshots`, and earlier mappings remain in the index's Git history.

```python
import json
from urllib.request import urlopen

url = (
    "https://raw.githubusercontent.com/pnxenopoulos/boon-data/data-index/versions.json"
)
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

## Boon integration

Boon provides `boon versions` and `boon get` for JSON releases, including
`misc.json`. NPC data uses the existing release files.
The CLI interface is:

```text
boon versions          # client version, source date/time, local status
boon get 6698          # fetch the release JSON files for this client version
boon get               # use versions.json's latest client version
boon versions --local  # inspect the local cache without network access
```

The intended cache is `~/.boon/<client-version>/`, containing `abilities.json`,
`heroes.json`, `modifiers.json`, `misc.json`, and `manifest.json`. The index provides the exact
URLs and checksums needed to verify those downloads.
Existing installations retain their published contents. These additions require
a newly built release; updating the generator does not replace release assets.

**A release's `ClientVersion` is not `demo.build`.** Matching a replay's header
to the appropriate source snapshot remains separate work. The catalogs do not
calculate resistance or lifesteal percentages for a live player.

## Development

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked python -m unittest discover -s tests -v
```

`pyproject.toml` declares dependencies and `uv.lock` pins the resolved versions.
`uv sync --locked` creates `.venv/` and checks the lockfile without changing it.
The optional `.python-version` selects Python 3.13 for local runs and releases.
CI uses the same Python 3.13 default on Ubuntu.
All workflows pin uv to 0.11.28 and use `uv run --locked`.
Ruff 0.16.9 and ty 0.0.84 are pinned as development dependencies, with the same
lint, formatting, and type checks in CI and both publication workflows. Ruff also
checks import ordering, modern syntax, common bug patterns, and simplifications.
The checks include the tests; ty resolves the standalone modules in `scripts/`
and treats warnings as failures. Ruff formatting also covers Python examples in
this README. Run `uv run --locked ruff format .` to apply formatting locally.
The scripts are a uv virtual project and are not installed as a Python package.

The current catalog builder still uses Polars internally, including for JSON
output, so it remains a pinned dependency. Removing it requires a separate
catalog refactor.

Tests use synthetic source responses and do not access the network. They cover
KV3/KV1 parsing, identity and localization joins, nullable numeric values,
modifier context, JSON/Parquet preservation of new fields, checksums, reproducible
builds, content reuse, publication timestamps, client-version lookup, draft
recovery, and atomic index updates. CI and releases each use one Ubuntu job
with Python 3.13.

The build scripts are MIT licensed. The upstream game data remains Valve's;
this repository does not grant a license to Valve's game assets. Source
provenance is included in every bundle.

### Curated runtime bindings

Mercurial Magnum links `BonusFireRate` to the watcher's
`modifier_ethereal_bullets_buff`. The separate bullet-damage buff and the watcher
do not grant this bonus. This link uses replay evidence and the engine schema;
it is not an explicit VData registration. The stat change has
`kind: "inferred_property"` and `binding_source: "curated"`. Apply it only while
that buff is active. The value and upgrades come from the catalog. An explicit
VData binding takes priority. Raw definitions remain unchanged.

Spirit Snatch links `TechPowerGain` and `TechArmorGain` to its caster buff.
It links `TechPowerReduction` and `TechArmorDamageReduction` to its victim debuff.
These effects use this count descriptor:

```json
{"source": "modifier", "field": "stack_count", "divisor": 100}
```

Multiply the upgraded catalog value by `stack_count / divisor`. Spirit power is
a flat change, not a percentage of the victim's spirit. Replay `108575009.dem`
records 70 for light hits and 100 for heavy hits; the source defines a 30% light-hit
reduction. Counts also accumulate and decay. Read each buff and debuff separately.
When a marked target dies, use the recorded removal or count change; other
victims can still supply a bonus. Do not infer a new hit from a timestamp refresh.
This link is curated and uses `kind: "inferred_property"`. Values and upgrades
stay in VData. Explicit registrations take priority. Raw definitions do not change.

A string `runtime_count` reads an ability entity field. An object names its
`source` (`ability` or `modifier`), `field`, and optional positive `divisor`.
The default divisor is 1. The current modifier-field reader supports `stack_count`.
Missing or invalid counts must leave the effect unresolved.

Trophy Collector links `StackingBonusSprintSpeed` to the ability entity's
`m_iTrophyCount` while `m_GoldModifier` is active. This engine relationship is
explicitly declared in `scripts/catalogs.py`; it is not a VData registration.
The exported property and stat change have `binding_source: "curated"` and
`runtime_count: "m_iTrophyCount"`. Consumers multiply the selected catalog's
property value, with its upgrades, by the recorded count. No balance value or
item ID is embedded in this binding. The raw `definition` remains unchanged.

Bloodscent uses an owner-only `runtime_counts` binding. Each term gives an ability
entity `field`; an optional `percent` contains a catalog property that weights
that count. The effective count is the sum of `count * percent / 100`, with
100% for an unweighted term. Multiply that count by the effect's property value.

The binding uses `m_nKillsEarned` and `m_nAssistsEarned`, with the latter weighted
by `IsolationAssistPercentValue`. The amount comes from
`WeaponDmgPerIsolationKill`. It applies while the player owns the ability; it
requires no nearby target marker. Neither the amount nor the assist weight is
fixed in the binding. Ability upgrades remain available in the catalog.

This is a curated interpretation: the
[matching server schema](https://github.com/SteamTracking/GameTracking-Deadlock/blob/19022f397ce9ba65856752d0cbaa82e80a2da73f/DumpSource2/schemas/server/CCitadel_Ability_Drifter_Hunger.h)
confirms both recorded counters, and localization describes the permanent reward.
It does not expose the engine equation. Raw definitions remain unchanged.


### Ability targeting and network stat types

Normalized properties and bound stat changes retain `apply_filter` from
`m_eApplyFilter`. Consumers can distinguish global bonuses from effects restricted
to an imbued ability or to abilities with charges. Unknown filters remain in the
data; a consumer must not assume that they mean a global effect.

`abilities.json` also has a `modifier_value_types` object. It maps decimal network
enum IDs to `MODIFIER_VALUE_*` names from
`DumpSource2/schemas/client/EModifierValue.h` at the same source commit. This lets
consumers decode recorded dynamic ability values without embedding version-specific
numeric maps. The source file is verified against its Git blob. `schema_files`
in the manifest records its SHA-256 and size, and the content fingerprint includes
it. Scheduled change detection still uses the tracked VData files.

The JSON files do not decide whether a next-cast effect is ready or active. That
requires replay state. Effect values, targeting filters, and engine enum names
are data; activation and stacking rules belong in the consumer.

### Player state names

`modifiers.json` includes `modifier_states`, a map from decimal bit indices to
`MODIFIER_STATE_*` names. The map comes from
`DumpSource2/schemas/client/EModifierState.h` at the snapshot's source commit.
It excludes `MODIFIER_STATE_COUNT` and `MODIFIER_STATE_INVALID`. The build verifies
the source Git blob and records its size and SHA-256 in `schema_files`. Changes
to this file affect the content fingerprint. Scheduled checks still use the
tracked VData files.

Consumers can use this map to decode a pawn's enabled, disabled, and predicted
state masks. The catalog does not decide which mask takes priority or supply
the magnitude of an effect. State names and bit indices are version-specific.

### Scaling-class defaults and enum definitions

`abilities.json` contains two additional objects:

| Field | Contents |
| --- | --- |
| `scaling_class_defaults` | Schema class name to `defaults`, `base_class`, and `source_file` |
| `enum_definitions` | Enum name to `values`, `names_by_value`, `underlying_type`, and `source_file` |

The build finds `CScaleFunction*.h` files in `DumpSource2/schemas/client` at
`source_commit`. It reads each explicit `MGetKV3ClassDefaults` object. Classes
without declared defaults have no entry. Nested curves and other fields remain
intact. Explicit VData values remain unchanged; defaults are stored separately.
These records do not supply equations or infer links between schema class names
and VData `_class` aliases.

Enum definitions include `EStatsType`, `PropertyValueApplyFilter_t`,
`StatsUsageFlags_t`, `EModifierValue`, and `EModifierState`. `values` maps each
symbol to its integer value. `names_by_value` maps each decimal value to a list
of symbols. Aliases, flags, and count/invalid sentinels remain in these full
records. The existing `modifier_value_types` and `modifier_states` maps keep
their current format and filtering.

```python
import json
from pathlib import Path

abilities = json.loads(Path("abilities.json").read_text())
filters = abilities["enum_definitions"]["PropertyValueApplyFilter_t"]["values"]
imbued_filter = filters["EApplyFilter_OnlyIfImbued"]
```

### Modifier names from engine strings

`modifiers.json.engine_modifier_names` contains `records`, `indexes.by_name`,
`indexes.by_id`, and `id_collisions`. The build reads whole lines that match
`modifier_*` or `citadel_modifier_*` identifiers from the client and server
string dumps at `source_commit`.

Each record has `modifier_name`, its hashed `modifier_id`, `status: "name_only"`,
and `sources` with file paths and line numbers. `catalog_keys` links exact names
to existing VData records, when available. Name-only records have no inferred
stat effects, activation conditions, or owning ability. A string's presence
also does not prove that the modifier is used in a match.

`indexes.by_name` maps a name to a record index. `indexes.by_id` maps a decimal
ID to all matching record indices. `id_collisions` lists IDs with different
names, including conflicts with VData modifier names. Consumers must not select
an arbitrary candidate when an ID is ambiguous.

For example, the source used for client version `6698` contains
`modifier_citadel_pre_match_wait`, whose hashed ID is `1243903559`. Its name
helps identify the modifier; it does not establish its gameplay effects.

All new source files have Git blob checks and manifest fingerprints. The release
still contains four catalogs and a manifest. Existing published files do not
change; a new build is required to include this metadata. Scheduled checks
continue to use the six VData files as their release gate.
