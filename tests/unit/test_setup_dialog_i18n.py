"""Setup dialog translations (custom_components/nova/translations) (8.16.0).

Structure and placeholders are checked in tests/test_translations.py and
tests/unit/test_strings_placeholders.py. This file adds what those do not:

  * the expected languages exist, named as Home Assistant names them
    (pt-BR.json, which Home Assistant never reads as pt.json).
  * symbols and line breaks match the English.
  * a value copied from the English fails, unless keep_english.json allows it.
  * no placeholder sits inside single quotes, which hassfest rejects.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TRANSLATIONS = ROOT / "custom_components" / "nova" / "translations"
KEEP_ENGLISH = ROOT / "frontend" / "i18n" / "keep_english.json"

LANGUAGES = {
    "en", "de", "es", "fr", "it", "nl", "pt",
    "cs", "da", "fi", "nb", "pl", "pt-BR", "ro", "ru", "sk", "sv", "tr", "uk",
}
# "&" and "+" are left out: a translation may rightly say "and".
EXTRA_SYMBOLS = set("·%")
PROSE_SYMBOLS = set("+")


def _leaves(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, path + (k,))
    else:
        yield path, node


def _load(name: str) -> dict:
    return dict(_leaves(json.loads((TRANSLATIONS / f"{name}.json").read_text(encoding="utf-8"))))


def _others() -> list[str]:
    return sorted(p.stem for p in TRANSLATIONS.glob("*.json") if p.stem != "en")


def _symbols(text: str) -> collections.Counter:
    return collections.Counter(
        c for c in text
        if (unicodedata.category(c)[0] == "S" and c not in PROSE_SYMBOLS) or c in EXTRA_SYMBOLS)


def _keep_english(lang: str) -> set[str]:
    allow = json.loads(KEEP_ENGLISH.read_text(encoding="utf-8"))["setup dialog"]
    return set(allow["any language"]) | set(allow.get(lang, []))


def test_every_expected_language_exists():
    assert {p.stem for p in TRANSLATIONS.glob("*.json")} == LANGUAGES


@pytest.mark.parametrize("lang", _others())
def test_symbols_and_line_breaks_match_the_english(lang):
    en = _load("en")
    bad = [".".join(p) for p, v in _load(lang).items()
           if _symbols(en[p]) != _symbols(v) or en[p].count("\n") != v.count("\n")]
    assert not bad, f"{lang}.json symbols or line breaks differ from the English: {bad}"


@pytest.mark.parametrize("lang", _others())
def test_no_value_is_left_in_english(lang):
    en = _load("en")
    allowed = _keep_english(lang)
    copied = [".".join(p) for p, v in _load(lang).items() if v == en[p] and v not in allowed]
    assert not copied, (
        f"{lang}.json leaves these in English: {copied}. Translate them, or add a brand "
        "or technical value to the \"setup dialog\" part of frontend/i18n/keep_english.json.")


def test_keep_english_only_lists_real_values():
    allow = json.loads(KEEP_ENGLISH.read_text(encoding="utf-8"))["setup dialog"]
    en = _load("en")
    for lang, values in allow.items():
        assert values == sorted(set(values)), lang
        langs = _others() if lang == "any language" else [lang]
        kept = {v for name in langs for p, v in _load(name).items() if v == en[p]}
        unused = [v for v in values if v not in kept]
        assert not unused, f"keep_english.json setup dialog [{lang}] lists unused values: {unused}"


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_no_placeholder_in_single_quotes(lang):
    bad = [".".join(p) for p, v in _load(lang).items() if re.search(r"'\{\w+\}'", v)]
    assert not bad, f"{lang}.json: hassfest rejects '{{placeholder}}' in single quotes: {bad}"
