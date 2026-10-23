"""The English "lockdown engaged" announcement (build_lockdown_message).

Three things are reported apart: locks a command went to, closeable openings
(garage doors) a command went to, and openings Nova cannot shut from afar
(bare window contacts). An opening left open is named as something to close,
never shrugged off. The engage message goes out before the background checks
have run, so it may only say commands were sent, never that a door is locked
or the home is secure.
"""
import pytest

PROMISE = "I will alert you if anything does not secure."
ALREADY = "Sir, lockdown engaged — the home was already fully secured."


@pytest.fixture
def build(load):
    return load("cognitive_core").build_lockdown_message


@pytest.fixture
def join(load):
    return load("notify_i18n").join_names


# ── the list join ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("given,joined", [
    ([], ""),
    (["a"], "a"),
    (["a", "b"], "a and b"),
    (["a", "b", "c"], "a, b, and c"),
    (["a", "", "b"], "a and b"),     # blank names are dropped
    (None, ""),
])
def test_english_join(join, given, joined):
    assert join(given) == joined


# ── whole sentences, word for word ──────────────────────────────────────────

WHOLE = {
    "nothing to do": (
        ("sir", [], [], []), ALREADY),
    "locks and a garage": (
        ("sir", ["Front Door"], ["Garage Door"], []),
        "Sir, lockdown engaged. I sent commands to lock Front Door and close "
        "Garage Door. " + PROMISE),
    "locks, a garage and a window": (
        ("sir", ["Front Door"], ["Garage Door"], ["the Window"]),
        "Sir, lockdown engaged. I sent commands to lock Front Door and close "
        "Garage Door, but the Window is open and I can't secure it remotely "
        "— you'll want to close it. " + PROMISE),
    "only a window": (
        ("sir", [], [], ["Bedroom Window"]),
        "Sir, lockdown engaged. Everything was already secured, but Bedroom "
        "Window is open and I can't secure it remotely — you'll want to close it."),
    "three windows by name": (
        ("sir", [], [], ["A", "B", "C"]),
        "Sir, lockdown engaged. Everything was already secured, but A, B, and C "
        "are open and I can't secure them remotely — you'll want to close them."),
    "four windows by count": (
        ("sir", [], [], ["A", "B", "C", "D"]),
        "Sir, lockdown engaged. Everything was already secured, but 4 openings "
        "are open and I can't secure them remotely — you'll want to close them."),
    "nobody to address": (
        ("", ["Front Lock"], [], []),
        "Lockdown engaged. I sent commands to lock Front Lock. " + PROMISE),
}


@pytest.mark.parametrize("args,expected", list(WHOLE.values()), ids=list(WHOLE))
def test_the_whole_sentence(build, args, expected):
    assert build(*args) == expected


# ── what each part says ─────────────────────────────────────────────────────

def test_locks_only_are_reported_as_sent(build):
    text = build("sir", ["Front Lock", "Back Lock"], [], [])
    assert "I sent commands to lock Front Lock and Back Lock" in text
    assert PROMISE in text


def test_a_garage_only_is_reported_as_sent(build):
    text = build("sir", [], ["the Garage Door"], [])
    assert "I sent commands to close the Garage Door" in text
    assert PROMISE in text


def test_locks_and_a_garage_share_one_sentence(build):
    text = build("sir", ["Front Lock"], ["the Garage Door"], [])
    assert "I sent commands to lock Front Lock and close the Garage Door" in text


def test_a_garage_sent_and_a_window_left(build):
    text = build("sir", [], ["the Garage Door"], ["Bedroom Window"])
    assert "I sent commands to close the Garage Door" in text
    assert "Bedroom Window is open" in text and "close it" in text
    assert PROMISE in text


def test_one_open_window_is_named_as_a_job_to_do(build):
    text = build("sir", [], [], ["Bedroom Window"])
    assert "Bedroom Window is open" in text and "close it" in text
    assert "already secured" in text
    # The old wording that hid the gap must never come back.
    assert "left as-is" not in text and "1 opening already open" not in text


def test_two_open_are_named_together(build):
    text = build("sir", [], [], ["the garage", "a window"])
    assert "the garage and a window are open" in text and "close them" in text


def test_many_open_are_counted(build):
    text = build("sir", [], [], ["d1", "d2", "d3", "d4", "d5"])
    assert "5 openings are open" in text and "close them" in text


@pytest.mark.parametrize("args", [
    ("sir", ["Front Lock", "Back Lock"], [], []),
    ("sir", [], ["the Garage Door"], []),
    ("sir", ["Front Door"], ["Garage Door"], []),
    ("sir", ["Front Door"], ["Garage Door"], ["the Window"]),
])
def test_sent_commands_never_claim_the_result(build, args):
    lowered = build(*args).lower()
    for claim in ("is locked", "is closed", "the home is secure", "is secure"):
        assert claim not in lowered


def test_with_nothing_open_no_lock_or_close_word_appears(build):
    lowered = build("sir", ["Front Door"], ["Garage Door"], []).lower()
    assert "locked" not in lowered and "closed" not in lowered


# ── failures and unreadable openings ────────────────────────────────────────

def test_a_failed_lock_alone_is_never_called_secure(build):
    text = build("sir", [], [], [], failed=["Back Lock"])
    assert text == ("Sir, lockdown engaged, but I couldn't secure Back Lock — "
                    "please check by hand.")
    assert "already" not in text


def test_an_unreadable_window_is_never_taken_as_shut(build):
    text = build("sir", [], [], [], unreadable=["Hall Window"])
    assert text == ("Sir, lockdown engaged. Everything was already secured, but "
                    "I can't tell if Hall Window is closed.")
    assert text != ALREADY


def test_every_problem_is_listed_after_what_was_sent(build):
    text = build("sir", ["Front Lock"], [], [], failed=["Back Lock"],
                 unreadable=["Hall Window"])
    assert text == ("Sir, lockdown engaged. I sent commands to lock Front Lock, but "
                    "I couldn't secure Back Lock — please check by hand; I can't tell "
                    "if Hall Window is closed. " + PROMISE)


def test_a_failed_lock_comes_before_an_open_window(build):
    text = build("sir", [], [], ["Bedroom Window"], failed=["Back Lock"])
    assert text.index("Back Lock") < text.index("Bedroom Window")
    assert text.startswith("Sir, lockdown engaged, but I couldn't secure Back Lock")


# ── who is addressed ────────────────────────────────────────────────────────

@pytest.mark.parametrize("honorific,opening", [
    ("sir", "Sir, lockdown engaged"),
    ("madam", "Madam, lockdown engaged"),
    # Nobody in particular at home: the sentence is capitalised, not "sir".
    ("", "Lockdown engaged"),
])
def test_the_opening_follows_the_honorific(build, honorific, opening):
    assert build(honorific, [], [], []).startswith(opening)
