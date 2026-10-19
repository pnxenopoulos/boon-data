#!/usr/bin/env bash
# Install the pinned Linux x64 CLI into the ignored work directory.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
output="$root/.work/source2viewer/20.1"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT

curl -fsSL --retry 3 \
  https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/20.1/cli-linux-x64.zip \
  -o "$archive"
printf '%s  %s\n' \
  29d25a6d6b62377fc5386c3e5145b0a2180a401a50041afe9494b3f503deaef5 "$archive" \
  | sha256sum --check --status
mkdir -p "$output"
unzip -oq "$archive" -d "$output"
chmod +x "$output/Source2Viewer-CLI"
printf 'Installed Source2Viewer-CLI 20.1 in %s\n' "$output"
