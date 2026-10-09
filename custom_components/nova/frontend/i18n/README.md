# Nova panel UI translations

One JSON file per language (e.g. `fr.json`), keyed by the **exact English string** shown
in the panel, mapping to the translation. The panel picks the file matching your Home
Assistant language automatically; untranslated strings simply stay English.

To add or extend a language: copy an existing file, translate the values, keep the keys
identical. The panel follows Home Assistant's language unless Settings → General →
Language overrides it; regional tags fall back to their base language when needed.
Technical and live values (entity IDs, model names, numbers, logs) are never translated,
and a missing key remains readable English.

Checks (8.15.0): the tests in `tests/unit/test_panel_i18n.py` hold every file to
`frontend/i18n/panel_strings.json`, the list of fixed English strings the panel shows.
A missing key, a key the panel never shows, or a changed `{placeholder}` or symbol fails
CI. See "Panel text and translations" in `CONTRIBUTING.md` for the steps.
