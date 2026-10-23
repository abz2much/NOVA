"""The lockdown announcement in languages other than English.

The sentence is put together from translated verb phrases, a translated list
join and a wrapper per language, so a French home hears all of it in French,
device list included. Device names themselves are never translated.
"""
import pytest

FR_PROMISE = "Je vous alerterai si quelque chose ne se sécurise pas."


@pytest.fixture
def build(load):
    return load("cognitive_core").build_lockdown_message


@pytest.fixture
def i18n(load):
    return load("notify_i18n")


# ── "and" in each language ──────────────────────────────────────────────────

@pytest.mark.parametrize("lang,two,three", [
    ("fr", "a et b", "a, b et c"),       # no comma before the last name
    ("de", "a und b", "a, b und c"),
    ("es", "a y b", "a, b y c"),
    ("it", "a e b", "a, b e c"),
    ("nl", "a en b", "a, b en c"),
    ("pt", "a e b", "a, b e c"),
    ("en", "a and b", "a, b, and c"),    # English keeps its serial comma
    ("en-US", "a and b", "a, b, and c"),
    ("fr-CA", "a et b", "a, b et c"),
])
def test_the_join_word_per_language(i18n, lang, two, three):
    assert i18n.join_names(["a", "b"], lang) == two
    assert i18n.join_names(["a", "b", "c"], lang) == three


# ── French sentences ────────────────────────────────────────────────────────

FRENCH = {
    "lock and close": (
        (["Porte d'entrée"], ["Garage"], []),
        "Monsieur, confinement activé. J'ai envoyé les commandes pour "
        "verrouiller Porte d'entrée et fermer Garage. " + FR_PROMISE),
    "nothing to do": (
        ([], [], []),
        "Monsieur, confinement activé — la maison était déjà entièrement sécurisée."),
    "three locks": (
        (["A", "B", "C"], [], []),
        "Monsieur, confinement activé. J'ai envoyé les commandes pour "
        "verrouiller A, B et C. " + FR_PROMISE),
    "one window only": (
        ([], [], ["f1"]),
        "Monsieur, confinement activé. Tout était déjà sécurisé, mais f1 : "
        "impossible à verrouiller à distance, à fermer manuellement."),
}


@pytest.mark.parametrize("lists,expected", list(FRENCH.values()), ids=list(FRENCH))
def test_french_word_for_word(build, lists, expected):
    assert build("monsieur", *lists, "fr") == expected


def test_french_lock_only(build):
    text = build("monsieur", ["Serrure avant"], [], [], "fr")
    assert "verrouiller Serrure avant" in text
    assert FR_PROMISE in text


def test_french_gap_does_not_agree_with_the_device(build):
    # The wording avoids an adjective or pronoun that would need to match the
    # grammatical gender of the device's name.
    text = build("monsieur", [], ["Garage"], ["Fenêtre 1"], "fr")
    assert "fermer Garage" in text
    assert "Fenêtre 1 : impossible à verrouiller à distance" in text
    assert "mais" in text
    assert FR_PROMISE in text


def test_french_many_open_are_counted(build):
    text = build("monsieur", [], [], ["f1", "f2", "f3", "f4"], "fr")
    assert "4 ouvertures ne peuvent pas être verrouillées à distance" in text


def test_french_failed_lock_and_unreadable_window(build):
    assert build("monsieur", [], [], [], "fr", failed=["Serrure"]) == (
        "Monsieur, confinement activé, mais je n'ai pas pu sécuriser Serrure "
        "— à vérifier manuellement.")
    assert build("monsieur", [], [], [], "fr", unreadable=["Fenêtre"]) == (
        "Monsieur, confinement activé. Tout était déjà sécurisé, mais je ne "
        "sais pas si Fenêtre est fermé.")


def test_a_regional_code_uses_the_base_language(build):
    assert build("monsieur", [], [], [], "fr-CA") == build("monsieur", [], [], [], "fr")


def test_german_wording(build):
    assert build("sir", ["Front Lock"], [], [], "de") == (
        "Sir, Sicherung aktiviert. Ich habe Befehle gesendet, um Front Lock zu "
        "verriegeln. Ich werde Sie benachrichtigen, falls etwas nicht sichert.")


# ── English is unchanged ────────────────────────────────────────────────────

def test_english_pending_wording(build):
    assert build("sir", ["Front Lock"], ["the Garage Door"], []) == (
        "Sir, lockdown engaged. I sent commands to lock Front Lock and close the "
        "Garage Door. I will alert you if anything does not secure.")


# ── message() directly ──────────────────────────────────────────────────────

def test_the_night_wrapper_in_french(i18n):
    said = i18n.message("lockdown_nighttime", "fr", honorific="Monsieur",
                        body="verrouillé Garage")
    assert said == "Monsieur, confinement nocturne : verrouillé Garage. La maison est sécurisée."


@pytest.mark.parametrize("lang", ["xx", None, ""])
def test_an_unknown_language_speaks_english(i18n, lang):
    said = i18n.message("lockdown_did_pending", lang, honorific="Sir", did="lock X")
    assert said == ("Sir, lockdown engaged. I sent commands to lock X. "
                    "I will alert you if anything does not secure.")


def test_an_unknown_key_is_empty_not_an_error(i18n):
    assert i18n.message("no_such_message", "fr") == ""
