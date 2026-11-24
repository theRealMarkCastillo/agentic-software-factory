#!/usr/bin/env bash
# Copy the canonical shared-skills/ into every plugin bundle.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for plugin in claude-code codex antigravity; do
  dest="$root/plugins/$plugin/skills"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$root/shared-skills/." "$dest/"
  find "$dest" -name .DS_Store -delete
done
echo "Synced shared-skills into plugins/{claude-code,codex,antigravity}/skills"
