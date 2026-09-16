"""Tests for the lockdown-engaged announcement (v6.31.0).

Reports three outcomes distinctly — locks locked, closeable openings closed
(garage doors), and openings that can't be secured remotely (windows). Open
openings are named and framed as the gap to close, never the old "left as-is"
shrug, and the message is never self-contradictory.
"""
import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def i18n(load):
    return load("notify_i18n")


@pytest.mark.parametrize("names,expected", [
    ([], ""),
    (["a"], "a"),
    (["a", "b"], "a and b"),
    (["a", "b", "c"], "a, b, and c"),
])
def test_join_names(i18n, names, expected):
    assert i18n.join_names(names) == expected


def test_already_secured(cc):
    assert cc.build_lockdown_message("sir", [], [], []) == \
        "Sir, lockdown engaged — the home was already fully secured."


def test_locked_only(cc):
    msg = cc.build_lockdown_message("sir", ["Front Lock", "Back Lock"], [], [])
    assert "I sent commands to lock Front Lock and Back Lock" in msg
    assert "I will alert you if anything does not secure." in msg
    # Phase 3: engage() hasn't observed the lock actually take yet -- must
    # not claim completion or overall home security.
    assert "is locked" not in msg
    assert "The home is secure" not in msg


def test_closed_a_garage(cc):
    msg = cc.build_lockdown_message("sir", [], ["the Garage Door"], [])
    assert "I sent commands to close the Garage Door" in msg
    assert "I will alert you if anything does not secure." in msg
    assert "is closed" not in msg
    assert "The home is secure" not in msg


def test_locked_and_closed(cc):
    msg = cc.build_lockdown_message("sir", ["Front Lock"], ["the Garage Door"], [])
    assert "I sent commands to lock Front Lock and close the Garage Door" in msg
    assert "The home is secure" not in msg


def test_open_window_named_and_actionable(cc):
    # Username's case: nothing closeable, locks already locked, one window open.
    msg = cc.build_lockdown_message("sir", [], [], ["Username's Window 1"])
    assert "Username's Window 1 is open" in msg
    assert "close it" in msg
    assert "already secured" in msg
    assert "left as-is" not in msg and "1 opening already open" not in msg


def test_closed_garage_but_window_open(cc):
    msg = cc.build_lockdown_message("sir", [], ["the Garage Door"], ["Username's Window 1"])
    assert "I sent commands to close the Garage Door" in msg
    assert "Username's Window 1 is open" in msg
    assert "close it" in msg
    assert "I will alert you if anything does not secure." in msg


def test_multiple_open_named(cc):
    msg = cc.build_lockdown_message("sir", [], [], ["the garage", "a window"])
    assert "the garage and a window are open" in msg and "close them" in msg


def test_many_open_summarised(cc):
    msg = cc.build_lockdown_message("sir", [], [], ["d1", "d2", "d3", "d4", "d5"])
    assert "5 openings are open" in msg and "close them" in msg


def test_honorific_applied(cc):
    assert cc.build_lockdown_message("madam", [], [], []).startswith("Madam, lockdown engaged")


def test_empty_honorific_capitalizes_instead_of_defaulting(cc):
    # Nobody specifically home to address (see honorific.py) -> no longer
    # silently coerced to "sir"; the sentence is just capitalized on its own.
    assert cc.build_lockdown_message("", [], [], []).startswith("Lockdown engaged")


# ── Phase 3: honest wording for actions awaiting background verification ────

def test_pending_actions_never_claim_locked_closed_or_secure(cc):
    """engage() schedules _verify_secured() in the background and responds
    before any of it resolves -- the immediate message must not claim a
    lock/cover reached its target state, nor that the home is secure."""
    msg = cc.build_lockdown_message(
        "sir", ["Front Door"], ["Garage Door"], [])
    assert "locked" not in msg.lower()
    assert "closed" not in msg.lower()
    assert "is secure" not in msg.lower()
    assert "is secured" not in msg.lower()
    assert msg == (
        "Sir, lockdown engaged. I sent commands to lock Front Door and close "
        "Garage Door. I will alert you if anything does not secure.")


def test_pending_actions_with_uncloseable_gap(cc):
    msg = cc.build_lockdown_message(
        "sir", ["Front Door"], ["Garage Door"], ["the Window"])
    assert msg == (
        "Sir, lockdown engaged. I sent commands to lock Front Door and close "
        "Garage Door, but the Window is open and I can't secure it remotely "
        "— you'll want to close it. I will alert you if anything does not "
        "secure.")


def test_already_secured_wording_is_unaffected_by_the_pending_fix(cc):
    # Nothing acted on -> everything was already observed secure -> no
    # pending-verification wording applies at all.
    assert cc.build_lockdown_message("sir", [], [], []) == \
        "Sir, lockdown engaged — the home was already fully secured."
