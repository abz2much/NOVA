# Contributing to Nova

Thanks for helping with Nova. This guide covers how the repository is laid
out, the checks a change must pass, the rules for panel text, and how a
release is made.

## Layout

```
nova/                                   repository root (a HACS integration)
├── hacs.json                           HACS metadata
├── README.md  CHANGELOG.md  LICENSE
├── .github/                            CI workflows, issue forms, funding
├── docs/                               user guides and the settings reference
├── mypy.ini                            type checking settings
├── pytest.ini                          test settings
├── scripts/
│   ├── audit.py                        static checks: compile, imports, names, architecture
│   ├── build_panel.py                  builds the panel from frontend/src/
│   ├── bump_version.sh                 sets a new version everywhere it appears
│   ├── coverage_floors.py              fails when a module's coverage drops below its floor
│   ├── smoke_panel.js                  loads the built panel under jsdom and clicks through it
│   ├── panel_strings.js                the list of English panel strings for translation
│   ├── late_text_check.js              checks text drawn after the first render is translated
│   ├── live_gemini_probe.py            optional live check of the Gemini adapter, with your key
│   └── live_vision_probe.py            optional live check of the model tests, with your keys
├── frontend/src/                       panel source, in ordered parts
├── tests/
│   ├── unit/                           fast tests with stand ins for Home Assistant
│   ├── integration/                    tests against a real Home Assistant (PHACC)
│   ├── evaluation/                     scenario runs of the reasoning loop (run.py)
│   └── fixtures/                       pinned contracts and sample data
└── custom_components/nova/             the integration (domain nova)
    ├── manifest.json
    ├── translations/                   setup dialog text, en.json is the source
    └── frontend/
        ├── nova-panel.js               the built panel (generated, do not edit)
        └── i18n/                       panel languages and string lists
```

HACS installs `custom_components/nova/` into Home Assistant.

## Local checks

These are the checks CI runs (`.github/workflows/validate.yml`). Run them from
the repository root before you open a pull request.

Unit tests, coverage, audit and types:

```bash
pip install pytest pytest-asyncio pyflakes pyyaml mypy==2.3.1 pytest-cov==7.1.0 voluptuous wyoming==1.10.2
python scripts/audit.py
python -m mypy
python -m pytest tests/ -q --cov=custom_components/nova --cov-report=json:coverage.json
python scripts/coverage_floors.py coverage.json
python tests/evaluation/run.py
```

Integration tests need the real `homeassistant` package, which clashes with the
stand ins the unit tests use, so run them in their own virtual environment.
Install the packages listed in the `integration` job of `validate.yml`, then:

```bash
python -m pytest tests/integration/ -q
```

The panel. Edit `frontend/src/`, never the built file, then:

```bash
python3 scripts/build_panel.py
python3 scripts/build_panel.py --check
node --check custom_components/nova/frontend/nova-panel.js
npm install --prefix /tmp/jsdomtest --no-save jsdom@29.1.1
export NODE_PATH=/tmp/jsdomtest/node_modules
node scripts/smoke_panel.js
node scripts/panel_strings.js --check
node scripts/late_text_check.js
```

## Panel text and translations

The panel's languages live in `custom_components/nova/frontend/i18n/<lang>.json`,
keyed by the exact English text. CI fails when a language is missing a key, so
when you add or change panel text:

1. Write fixed text as one whole string. When a value sits inside the text, use
   `this._t("{count} RECENT", { count })` for plain text, or `this._tHtml(...)`
   in markup. Use one template for each plural form.
2. Text set after the first render goes through `this._setHtml(el, html)`,
   `this._setText(el, text)`, or `this._tx(text)` for a title or a dialog
   message. `scripts/late_text_check.js` and `tests/unit/test_panel_late_text.py`
   fail on a direct `innerHTML`, `textContent`, `title` or `confirm()`. Where one
   is really needed, give the reason on the same line with `// i18n-ok: <reason>`.
3. Run `node scripts/panel_strings.js --write` (with jsdom on `NODE_PATH`) to
   update `frontend/i18n/panel_strings.json`.
4. Add each new string, with its translation, to every language file. Keep each
   `{placeholder}` and symbol (→, ·, %, icons) exactly as in the English.
5. Remove the keys for text you took out. `python -m pytest tests/unit/test_panel_i18n.py`
   lists any that are left.

Never add to `frontend/i18n/baseline.json`. It lists the strings that had no
translation when the checks began, and it may only get shorter. A brand or
technical word that stays in English (LLM, TTS) goes in the "panel" part of
`frontend/i18n/keep_english.json`.

The setup dialog uses `custom_components/nova/translations/`, with `en.json` as
the English source. There is no `strings.json`. A new English string there needs
a translation in every file, with the same placeholders, symbols and line
breaks; `tests/unit/test_setup_dialog_i18n.py` checks them. Use Home
Assistant's own word for each menu and feature, and its form of "you".

## Releasing

Set the new version with one command:

```bash
bash scripts/bump_version.sh 8.69.0
```

It updates `manifest.json` and the panel version in `frontend/src/panel/core.js`,
then rebuilds `nova-panel.js`. Add a section for the version at the top of
`CHANGELOG.md`, headed `## [8.69.0] Short title`.

HACS offers Nova's GitHub releases. When a change to the version in
`manifest.json` reaches `main`, `.github/workflows/release.yml` creates the tag
`v8.69.0` and its release, with that version's CHANGELOG section as the notes.
It does nothing when that release already exists, and it can also be run by
hand from the Actions tab.

After updating a live system, hard refresh the browser (`Ctrl+Shift+R`) so the
cached panel reloads.

## Pull requests

Keep each change focused, say why it is needed, and note any limits. Add a test
that fails without the change. If it touches the reasoning loop, cameras or
safety alerts, say how you checked it.
