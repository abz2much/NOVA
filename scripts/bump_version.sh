#!/usr/bin/env bash
# Bump the Nova version everywhere it appears (HACS integration layout).
# Usage: ./scripts/bump_version.sh 6.3.3
set -euo pipefail

NEW="${1:-}"
if [[ -z "$NEW" ]]; then
  echo "Usage: $0 <new-version>   e.g. $0 6.3.3" >&2
  exit 1
fi
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Version must look like X.Y.Z (got '$NEW')" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMP="$ROOT/custom_components/nova"
PANEL="$COMP/frontend/nova-panel.js"

# Current version from the integration manifest (source of truth).
OLD="$(python3 -c "import json;print(json.load(open('$COMP/manifest.json'))['version'])")"
if [[ "$OLD" == "$NEW" ]]; then
  echo "Already at $NEW — nothing to do."
  exit 0
fi
echo "Bumping $OLD → $NEW"

# Plain X.Y.Z in the manifest, and vX.Y.Z in the dashboard footer/masthead.
# Done via python3 (already a hard dependency here) rather than `sed -i`,
# whose in-place flag and \b word-boundary support both differ between BSD
# sed (macOS) and GNU sed (Linux, what CI runs) — this way works on both.
python3 - "$OLD" "$NEW" "$COMP/manifest.json" "$PANEL" <<'PYEOF'
import re, sys
old, new, manifest_path, panel_path = sys.argv[1:5]

def replace(path, pattern, replacement):
    text = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(pattern.sub(replacement, text))

replace(manifest_path, re.compile(r"\b" + re.escape(old) + r"\b"), new)
replace(panel_path, re.compile(r"v" + re.escape(old) + r"\b"), f"v{new}")
PYEOF

echo "Updated:"
echo "  manifest.json   -> $(python3 -c "import json;print(json.load(open('$COMP/manifest.json'))['version'])")"
echo "  nova-panel.js -> $(grep -om1 "v${NEW}" "$PANEL" | head -1 || echo '(check manually)')"
echo
echo "Next: review CHANGELOG.md, then  git commit -am 'Release v$NEW' && git tag v$NEW && git push --tags"
echo "(HACS publishes from the git tag/release — no add-on build.)"
