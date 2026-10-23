"""Safety notifications in the household's language (notify_i18n).

Freeze, intrusion and lockdown alerts are filled from fixed templates rather
than by the model, so they have to work in every language Nova ships, fall
back to English cleanly, and never raise while an alert is being sent.
"""
from __future__ import annotations

import re

import pytest

LANGUAGES = {"en", "fr", "de", "es", "it", "nl", "pt"}
PLACEHOLDER = re.compile(r"\{(\w+)\}")
LEAD_IN = "{honorific}, "

SEVERE_FREEZE_EN = (
    "Outdoor temperature has dropped to 18.0°F. Pipe freeze risk is severe. "
    "I recommend opening cabinet doors near exterior walls and confirming heat "
    "is set to at least 55°F.")


@pytest.fixture
def i18n(load):
    return load("notify_i18n")


def _both_tables(module):
    return (("MESSAGES", module.MESSAGES), ("TITLES", module.TITLES))


# ── filled messages ──────────────────────────────────────────────────────

def test_english_severe_freeze_reads_as_it_always_has(i18n):
    text = i18n.message("freeze_critical", "en", honorific="Sir",
                        reading="18.0°F", set_to="55°F")
    assert text.startswith("Sir, outdoor temperature has dropped to 18.0°F")
    assert "55°F" in text
    assert text == "Sir, o" + SEVERE_FREEZE_EN[1:]


def test_french_severe_freeze_is_translated_and_filled(i18n):
    text = i18n.message("freeze_critical", "fr", honorific="Monsieur",
                        reading="-10,0°C", set_to="13°C")
    for fragment in ("température extérieure", "-10,0°C", "13°C"):
        assert fragment in text
    assert text.startswith("Monsieur, ")


@pytest.mark.parametrize("lang, needle", [
    ("de-DE", "Außentemperatur"),      # region suffix dropped
    ("FR-ca", "température extérieure"),  # and case ignored
])
def test_language_tags_are_reduced_to_their_base(i18n, lang, needle):
    assert needle in i18n.message("freeze_warning", lang, honorific="Sir", reading="2°C")


@pytest.mark.parametrize("lang", ["xx", None, ""])
def test_unknown_or_missing_language_gives_english(i18n, lang):
    text = i18n.message("freeze_warning", lang, honorific="Sir", reading="2°C")
    assert text.startswith("Sir, outdoor temperature")
    assert text == "Sir, outdoor temperature is 2°C. I'm monitoring for pipe freeze risk."


def test_unknown_message_key_gives_an_empty_string(i18n):
    assert i18n.message("nope", "fr", honorific="X") == ""


def test_missing_parameters_fall_back_to_the_raw_english_template(i18n):
    # Neither the French nor the English template can be filled without
    # `reading`; message() must still return something and not raise.
    assert i18n.message("freeze_critical", "fr") == i18n.MESSAGES["freeze_critical"]["en"]


@pytest.mark.parametrize("key, lang, params, expected", [
    ("intrusion_ctx_open", "fr", {"name": "Front Door"}, " (Front Door ouvert)"),
    ("intrusion_ctx_open", "fr", {"name": "Front Door", "honorific": ""},
     " (Front Door ouvert)"),
    ("intrusion_ctx_armed", "es", {}, " (alarma armada)"),
    ("intrusion_ctx_armed", "en", {}, " (alarm armed)"),
])
def test_intrusion_fragments_ignore_the_honorific(i18n, key, lang, params, expected):
    # Fragments have no lead-in, so an honorific (or none) changes nothing.
    assert i18n.message(key, lang, **params) == expected


@pytest.mark.parametrize("honorific", ["", "   "])
def test_no_one_to_address_gives_a_capitalised_sentence(i18n, honorific):
    # honorific.py passes "" when nobody in particular is home: no stray
    # leading ", ", but the sentence still opens with a capital.
    text = i18n.message("freeze_critical", "en", honorific=honorific,
                        reading="18.0°F", set_to="55°F")
    assert text == SEVERE_FREEZE_EN
    assert not text.startswith(",")


def test_honorific_defaults_to_sir(i18n):
    text = i18n.message("freeze_warning", "en", reading="2°C")
    assert text.startswith("Sir, outdoor temperature")


# ── titles ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, lang, expected", [
    ("intrusion_confirmed", "fr", "Nova — INTRUSION"),
    ("lockdown", "de", "Nova — Haus gesichert"),
    ("lockdown", "DE-at", "Nova — Haus gesichert"),
    ("lockdown", "xx", "Nova — House Secured"),     # English fallback
    ("lockdown", None, "Nova — House Secured"),
    ("unknown_type", "fr", "Nova"),                 # last-resort default
])
def test_titles(i18n, key, lang, expected):
    assert i18n.title(key, lang) == expected


# ── the tables themselves ────────────────────────────────────────────────

def test_every_entry_exists_in_all_seven_languages(i18n):
    for table_name, table in _both_tables(i18n):
        for key, by_lang in table.items():
            assert set(by_lang) == LANGUAGES, f"{table_name}[{key!r}] missing languages"


def test_translations_keep_the_english_placeholders(i18n):
    # A dropped or renamed {placeholder} would print literally in a safety
    # alert, or lose the device name (8.15.0).
    for table_name, table in _both_tables(i18n):
        for key, by_lang in table.items():
            wanted = sorted(PLACEHOLDER.findall(by_lang["en"]))
            for lang, text in by_lang.items():
                assert sorted(PLACEHOLDER.findall(text)) == wanted, f"{table_name}[{key!r}][{lang!r}]"


def test_the_lead_in_is_used_the_same_way_in_every_language(i18n):
    # message() strips "{honorific}, " only at the very start. A translation
    # that put it elsewhere, or left it out where English has it, would show
    # a raw placeholder or address nobody.
    for key, by_lang in i18n.MESSAGES.items():
        english_addresses = by_lang["en"].startswith(LEAD_IN)
        for lang, text in by_lang.items():
            assert text.startswith(LEAD_IN) is english_addresses, (key, lang)
            assert "{honorific}" not in text[len(LEAD_IN):], (key, lang)
