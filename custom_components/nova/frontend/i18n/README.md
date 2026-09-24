# Nova panel UI translations

One JSON file per language (e.g. `fr.json`), keyed by the **exact English string** shown
in the panel, mapping to the translation. The panel picks the file matching your Home
Assistant language automatically; untranslated strings simply stay English.

To add or extend a language: copy an existing file, translate the values, keep the keys
identical. The panel follows Home Assistant's language unless Settings → General →
Language overrides it; regional tags fall back to their base language when needed.
Technical and live values (entity IDs, model names, numbers, logs) are never translated,
and a missing key remains readable English.
