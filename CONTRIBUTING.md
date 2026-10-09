# Contributing to Nova

Thanks for your interest in improving Nova. This document covers the repository
layout, the development workflow, and the release process.

## Repository layout

```
nova-aio/                         repo root (HACS integration repository)
├── hacs.json                        HACS metadata
├── README.md  CHANGELOG.md  LICENSE
├── icon.png  logo.png               branding (for home-assistant/brands)
├── .github/                         funding, issue templates, CI
├── scripts/bump_version.sh          one-command version bump
├── scripts/build_panel.py           builds the dashboard from frontend/src/
├── frontend/src/                    dashboard source (NOVA3D engine + panel parts)
├── frontend/dev/                    3D model viewer and renderer (not shipped)
└── custom_components/nova/        the integration (domain: nova)
    ├── manifest.json
    ├── __init__.py and modules
    └── frontend/nova-panel.js     the built dashboard (generated, do not edit)
```

HACS installs `custom_components/nova/` into Home Assistant. The integration
runs in-process; the config flow (or a migrated legacy config) sets it up.

## Code standard

This project holds to senior+ engineering output:

- **4-pass audit before every release:** (1) syntax, (2) integration references,
  (3) all modules parse, (4) version bump + package.
- **Simulate tests internally** before shipping — the codebase carries standalone
  test harnesses for the reasoning loop, the Local Mind decision matrix, the
  package state machine, cognition salience tiering, and the speech composer.
- **Honest caveats** documented with every change. Verification before speculation.
- No careless mistakes, and no apologies in place of fixes.

## Local checks

```bash
# Python syntax across all integration modules
cd custom_components/nova
for f in *.py; do python3 -c "import ast; ast.parse(open('$f').read())" || echo "FAIL $f"; done

# Architecture: cycles, package dependency direction, compatibility modules
python3 ../../scripts/audit.py

# Types (from the repo root; needs `pip install mypy`)
(cd ../.. && python3 -m mypy)

# Dashboard: edit frontend/src/, rebuild, then check the built file
python3 ../../scripts/build_panel.py
node --check frontend/nova-panel.js

# Add-on shell script
bash -n ../run.sh
```

## Panel text and translations

The panel's languages live in `custom_components/nova/frontend/i18n/<lang>.json`,
keyed by the exact English text. CI fails if a language is missing a key, so
when you add or change panel text:

1. Write fixed text as a whole string. If a value sits inside the text, use
   `this._t("{count} RECENT", { count })` for plain text, or `this._tHtml(...)`
   in markup. Use one template per plural form.
2. Run `NODE_PATH=node_modules node scripts/panel_strings.js --write` to update
   `frontend/i18n/panel_strings.json`.
3. Add each new string, with its translation, to every language file. Keep
   every `{placeholder}` and symbol (→, ·, %, icons) exactly as in the English.
4. Delete the keys for text you removed. `python -m pytest tests/unit/test_panel_i18n.py`
   shows any that are left.

Never add to `frontend/i18n/baseline.json`. It lists the strings that were
untranslated when the checks began, and it may only shrink. A brand or
technical word that stays in English (LLM, TTS) goes in
`frontend/i18n/keep_english.json`.

The setup dialog uses `custom_components/nova/translations/`, with `en.json` as
the English source. There is no `strings.json`.

## Releasing

Bump the version everywhere it appears with one command:

```bash
./scripts/bump_version.sh 6.3.3
```

This updates the integration `manifest.json` and the panel version in
`frontend/src/panel/core.js`, then rebuilds `nova-panel.js`. Then commit, tag
(`git tag v6.3.3`), and push the tag — the validation workflow runs on every push.

After updating on a live system, hard-refresh the browser (`Ctrl+Shift+R`) so the
cached dashboard JavaScript reloads.

## Pull requests

Keep changes focused, include a short rationale, and note any limitations. If a
change touches the reasoning pipeline, the camera/Nest paths, or cognition
salience, please describe how you verified it.
