"""Panel translations cannot drift (8.15.0).

frontend/i18n/panel_strings.json lists every fixed English string the panel
shows. scripts/panel_strings.js builds it and fails CI when it is out of date.
These tests hold each language file in custom_components/nova/frontend/i18n/
to that list:

  * a string with no key fails, unless it is in baseline.json (the strings
    missing when the checks began). The baseline can only shrink.
  * a key that matches nothing the panel shows fails.
  * placeholders and symbols must match the English.
  * a value copied from the English fails, unless keep_english.json allows it.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECKS = ROOT / "frontend" / "i18n"
LANG_DIR = ROOT / "custom_components" / "nova" / "frontend" / "i18n"
BACKEND = ROOT / "custom_components" / "nova"

# Each language's baseline may lose entries but never gain them. Lower a
# number when that language's strings are translated; never raise one for a
# string that already exists. History: one shared list of 895 (8.15.0),
# 931 (8.17.0, late drawn text), then per language from 8.18.0, when the
# generator stopped counting test data and code fragments: 913 each.
# Finished languages must have every key: their baseline is empty and stays so.
FINISHED = {"cs", "de", "es", "fr", "nl", "pl", "pt-br", "ru", "sv", "zh", "zh-hant"}
BASELINE_MAX = {lang: 918 for lang in ("da", "fi", "it", "nb", "pt", "ro", "sk", "tr", "uk")}  # 913 + 6 new in 8.19.0, less "Chinese" in 8.19.1
BASELINE_MAX.update({lang: 0 for lang in FINISHED})

PLACEHOLDER = re.compile(r"\{(\w+)\}")
# "&" and "+" are left out: a translation may rightly say "and".
EXTRA_SYMBOLS = set("·%")
PROSE_SYMBOLS = set("+")


def _json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _langs() -> dict[str, dict[str, str]]:
    return {p.stem: _json(p) for p in sorted(LANG_DIR.glob("*.json"))}


def _strings() -> list[str]:
    return _json(CHECKS / "panel_strings.json")


def _baseline() -> dict[str, list[str]]:
    """The strings each language is still allowed to miss. A language that
    is complete has an empty list."""
    return _json(CHECKS / "baseline.json")


def _backend_labels() -> list[str]:
    return _json(CHECKS / "backend_labels.json")["labels"]


def _keep_english(lang: str) -> set[str]:
    allow = _json(CHECKS / "keep_english.json")["panel"]
    return set(allow["any language"]) | set(allow.get(lang, []))


def _symbols(text: str) -> collections.Counter:
    return collections.Counter(
        c for c in text
        if (unicodedata.category(c)[0] == "S" and c not in PROSE_SYMBOLS) or c in EXTRA_SYMBOLS)


def _backend_literals() -> set[str]:
    """Every quoted string in the backend, with "_" read as a space."""
    out: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"([\"'])((?:(?!\1)[^\\\n])*)\1", text):
            out.add(m.group(2).replace("_", " "))
    return out


def _from_backend(label: str, literals: set[str]) -> bool:
    """Whether a backend value can still show as this exact label: as it is,
    or in capitals, which is how the panel shows many backend values."""
    return label in literals or (label == label.upper() and label in {s.upper() for s in literals})


def test_the_string_list_is_sorted_and_unique():
    strings = _strings()
    assert strings == sorted(set(strings))
    assert len(strings) > 500


def test_every_language_file_is_checked():
    assert set(_langs()) == {
        "cs", "da", "de", "es", "fi", "fr", "it", "nb", "nl", "pl",
        "pt", "pt-br", "ro", "ru", "sk", "sv", "tr", "uk", "zh", "zh-hant"}


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_every_panel_string_has_a_key_or_is_in_the_baseline(lang):
    keys = _langs()[lang]
    baseline = set(_baseline().get(lang, []))
    missing = [s for s in _strings() if s not in keys and s not in baseline]
    assert not missing, (
        f"{lang}.json has no key for {len(missing)} panel string(s): {missing[:10]}. "
        "Add a translation for each one to every language file.")


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_the_baseline_only_shrinks(lang):
    baseline = _baseline()
    assert set(baseline) == set(_langs()), "baseline.json needs one list per language file"
    mine = baseline[lang]
    strings = set(_strings())
    keys = _langs()[lang]
    assert mine == sorted(set(mine))
    assert len(mine) <= BASELINE_MAX[lang], f"{lang}: the baseline grew; translate new strings instead"
    gone = [s for s in mine if s not in strings]
    assert not gone, f"{lang}: baseline strings the panel no longer shows, remove them: {gone[:10]}"
    done = [s for s in mine if s in keys]
    assert not done, f"{lang}: now translated, remove them from its baseline: {done[:10]}"


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_no_stale_keys(lang):
    known = set(_strings()) | set(_backend_labels())
    stale = [k for k in _langs()[lang] if k not in known]
    assert not stale, f"{lang}.json has keys the panel never shows: {stale[:10]}"


def test_backend_labels_still_come_from_the_backend():
    literals = _backend_literals()
    langs = _langs()
    labels = _backend_labels()
    assert labels == sorted(set(labels))
    gone = [s for s in labels if not _from_backend(s, literals)]
    assert not gone, f"no backend value can show these any more, remove them: {gone}"
    unused = [s for s in labels if not any(s in keys for keys in langs.values())]
    assert not unused, f"no language file has these, remove them: {unused}"


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_placeholders_match_the_english(lang):
    bad = [k for k, v in _langs()[lang].items()
           if sorted(PLACEHOLDER.findall(k)) != sorted(PLACEHOLDER.findall(v))]
    assert not bad, f"{lang}.json placeholders differ from the English: {bad[:10]}"


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_symbols_match_the_english(lang):
    # A translation may drop "&" for a word, but never add one: _tHtml puts
    # some translations into markup.
    bad = [k for k, v in _langs()[lang].items()
           if _symbols(k) != _symbols(v) or ("&" in v and "&" not in k)]
    assert not bad, f"{lang}.json symbols differ from the English: {bad[:10]}"


@pytest.mark.parametrize("lang", sorted(_langs()))
def test_no_value_is_left_in_english(lang):
    allowed = _keep_english(lang)
    copied = [k for k, v in _langs()[lang].items() if v == k and k not in allowed]
    assert not copied, (
        f"{lang}.json leaves these in English: {copied[:10]}. Translate them, or add a "
        "brand or technical word to frontend/i18n/keep_english.json.")


def test_keep_english_only_lists_real_keys():
    allow = _json(CHECKS / "keep_english.json")["panel"]
    langs = _langs()
    for lang, words in allow.items():
        assert words == sorted(set(words)), lang
        targets = langs.values() if lang == "any language" else [langs[lang]]
        unused = [w for w in words if not any(t.get(w) == w for t in targets)]
        assert not unused, f"keep_english.json [{lang}] lists words nothing keeps in English: {unused}"


def _template_keys(src: str) -> set[str]:
    """Every double quoted string with words in the first argument of _t,
    _tHtml or _tx, the same rule scripts/panel_strings.js uses."""
    keys: set[str] = set()
    for m in re.finditer(r"\b_t(?:Html|x)?\(", src):
        depth, i = 0, m.end()
        while i < len(src):
            c = src[i]
            if c == '"':
                j = i + 1
                while j < len(src) and src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
                if not re.search(r"[=!]==?\s*$", src[max(0, i - 5):i]):
                    keys.add(json.loads(src[i:j + 1]))
                i = j
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            i += 1
    return {k for k in keys if re.search(r"[A-Za-z]{2}", k)}


def test_t_helper_keys_are_in_the_list():
    """Every _t or _tHtml template in the panel source is a panel string."""
    found: set[str] = set()
    for path in (ROOT / "frontend" / "src").rglob("*.js"):
        found |= _template_keys(path.read_text(encoding="utf-8"))
    assert len(found) > 50, "expected the _t templates in the panel source"
    missing = sorted(found - set(_strings()))
    assert not missing, f"run scripts/panel_strings.js --write: {missing[:10]}"
