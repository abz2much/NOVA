"""The policy gate in front of every state changing call (policy.py).

The guard that matters most: when the confirmation subsystem breaks, a
protected action (unlock, open a garage, disarm) is refused, never let
through. Only low risk convenience may carry on when the check itself
fails, and then only with nothing to warn about.
"""
from __future__ import annotations

import sys
import types

import pytest


class _Confirmer:
    """A stand-in voice_confirm module.

    ``protected`` is what action_is_protected reports and ``answer`` what a
    confirmation returns; either may be an exception to raise instead. Each
    question asked is kept in ``asked`` as (question, entity_id)."""

    def __init__(self, protected=True, answer="approved"):
        self.protected, self.answer, self.asked = protected, answer, []

    def _protected(self, hass, domain, service, entity_id=""):
        if isinstance(self.protected, BaseException):
            raise self.protected
        return self.protected

    async def _confirm_typed(self, hass, question, entity_id=""):
        self.asked.append((question, entity_id))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def _confirm(self, hass, question, entity_id=""):
        return await self._confirm_typed(hass, question, entity_id) == "approved"

    def module(self):
        mod = types.ModuleType("jc.voice_confirm")
        mod.action_is_protected = self._protected
        mod.confirm_typed = self._confirm_typed
        mod.confirm = self._confirm
        return mod


@pytest.fixture
def policy(load):
    return load("policy")


@pytest.fixture
def confirmer(monkeypatch):
    """install(**kw) puts a _Confirmer in place of voice_confirm."""
    def install(**kw):
        fake = _Confirmer(**kw)
        monkeypatch.setitem(sys.modules, "jc.voice_confirm", fake.module())
        return fake
    return install


BROKEN = RuntimeError("voice_confirm exploded")
UNLOCK = ("lock", "unlock", "lock.front_door")
LIGHT = ("light", "turn_on", "light.kitchen")

# Every rating above low, and some convenience, for the fail closed tables.
ABOVE_LOW = [
    ("lock", "unlock", "lock.front_door"),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.house"),
    ("cover", "open_cover", "cover.garage"),
    ("scene", "turn_on", "scene.evening"),
    ("switch", "turn_off", "switch.alarm_siren"),
]
CONVENIENCE = [LIGHT, ("climate", "set_temperature", "climate.hall"),
               ("media_player", "media_play", "media_player.lounge")]


# ── confirm_gate: what each answer means ───────────────────────────────────

@pytest.mark.parametrize("answer, allowed, note", [
    ("approved", True, ""),
    ("rejected", False,
     "asked for spoken confirmation before unlock on front door; not yet confirmed"),
    ("expired", False,
     "asked for spoken confirmation before unlock on front door; not yet confirmed"),
])
async def test_a_protected_action_follows_the_typed_answer(policy, confirmer, answer,
                                                          allowed, note):
    fake = confirmer(protected=True, answer=answer)
    assert await policy.confirm_gate(None, *UNLOCK) == (allowed, note, answer)
    assert fake.asked == [("unlock front door — are you sure?", "lock.front_door")]


async def test_a_confirmation_that_raises_denies_the_action(policy, confirmer):
    confirmer(protected=True, answer=BROKEN)
    ok, note, result = await policy.confirm_gate(None, *UNLOCK)
    assert ok is False and result == "error"
    assert "denied" in note.lower() and "unavailable" in note.lower()
    assert note == "confirmation unavailable — action denied for safety"


async def test_the_label_and_target_name_word_the_question_only(policy, confirmer):
    fake = confirmer(protected=True, answer="approved")
    result = await policy.confirm_gate(None, *UNLOCK, action_label="Unlock",
                                       target_name="Front Door")
    assert result == (True, "", "approved")
    # The decision and the entity passed on still use the entity id.
    assert fake.asked == [("Unlock Front Door — are you sure?", "lock.front_door")]


async def test_an_unprotected_action_runs_without_asking(policy, confirmer):
    fake = confirmer(protected=False)
    assert await policy.confirm_gate(None, *LIGHT) == (True, "", "not_required")
    assert fake.asked == []


async def test_a_live_warning_is_always_asked_and_opens_the_question(policy, confirmer):
    fake = confirmer(protected=False, answer="approved")
    result = await policy.confirm_gate(None, "lock", "lock", "lock.back_door",
                                       warning="The back door is open.")
    assert result == (True, "", "approved")
    assert fake.asked == [("The back door is open. lock back door anyway — are you sure?",
                           "lock.back_door")]


@pytest.mark.parametrize("domain, service", [("nova", "dismiss_intrusion"),
                                             ("nova", "ask_executive_assistant")])
async def test_stand_down_and_specialists_ask_even_when_not_listed(policy, confirmer,
                                                                  domain, service):
    fake = confirmer(protected=False, answer="rejected")
    ok, _, result = await policy.confirm_gate(None, domain, service)
    assert (ok, result) == (False, "rejected")
    assert len(fake.asked) == 1


# ── confirm_gate: a broken protection check ────────────────────────────────

@pytest.mark.parametrize("call", ABOVE_LOW, ids=lambda c: f"{c[0]}.{c[1]}")
async def test_a_broken_protection_check_denies_anything_above_low(policy, confirmer, call):
    fake = confirmer(protected=BROKEN)
    assert await policy.confirm_gate(None, *call) == (
        False, "confirmation check failed — action denied for safety", "error")
    assert fake.asked == []


@pytest.mark.parametrize("call", CONVENIENCE, ids=lambda c: f"{c[0]}.{c[1]}")
async def test_a_broken_protection_check_lets_convenience_through(policy, confirmer, call):
    confirmer(protected=BROKEN)
    assert await policy.confirm_gate(None, *call) == (True, "", "not_required")


@pytest.mark.parametrize("answer, allowed", [("approved", True), ("rejected", False),
                                             (BROKEN, False)])
async def test_a_warning_is_asked_about_even_when_the_check_is_broken(policy, confirmer,
                                                                     answer, allowed):
    # A live warning makes the action protected before the check is read, so
    # even low risk is never waved through on the broken-check path.
    fake = confirmer(protected=BROKEN, answer=answer)
    ok, _, result = await policy.confirm_gate(None, *LIGHT, warning="Zones are open.")
    assert ok is allowed
    assert result == (answer if isinstance(answer, str) else "error")
    assert len(fake.asked) == 1


# ── requires_confirmation (the sync check bulk control uses) ───────────────

@pytest.mark.parametrize("protected", [True, False])
def test_requires_confirmation_reports_what_voice_confirm_says(policy, confirmer, protected):
    confirmer(protected=protected)
    assert policy.requires_confirmation(None, *UNLOCK) is protected


@pytest.mark.parametrize("call", ABOVE_LOW, ids=lambda c: f"{c[0]}.{c[1]}")
def test_requires_confirmation_holds_anything_above_low_on_error(policy, confirmer, call):
    confirmer(protected=BROKEN)
    assert policy.requires_confirmation(None, *call) is True


@pytest.mark.parametrize("call", CONVENIENCE, ids=lambda c: f"{c[0]}.{c[1]}")
def test_requires_confirmation_lets_convenience_through_on_error(policy, confirmer, call):
    confirmer(protected=BROKEN)
    assert policy.requires_confirmation(None, *call) is False


def test_an_unlock_by_voice_is_held_whatever_voice_confirm_says(policy, confirmer):
    confirmer(protected=False)
    assert policy.requires_confirmation(None, *UNLOCK, voice=True) is True
    assert policy.requires_confirmation(None, *LIGHT, voice=True) is False


# ── the ratings the gate relies on ─────────────────────────────────────────

@pytest.mark.parametrize("domain, service, entity_id, risk", [
    ("lock", "unlock", "", "high"),
    ("alarm_control_panel", "alarm_disarm", "", "critical"),
    ("cover", "open_cover", "", "medium"),
    ("light", "turn_on", "", "low"),
    ("media_player", "media_play", "", "low"),
    ("climate", "set_temperature", "", "low"),
    ("switch", "turn_off", "switch.front_door_lock", "high"),
    ("switch", "turn_off", "switch.alarm_siren", "high"),
    ("switch", "turn_off", "switch.desk_lamp", "low"),
    # A scene, script or automation may unlock or open something inside
    # itself, and the gate cannot see in, so starting one is medium.
    ("scene", "turn_on", "", "medium"),
    ("script", "turn_on", "", "medium"),
    ("automation", "trigger", "", "medium"),
    # Every script also gets its own script.<object_id> service; the rule is
    # per domain so those are caught too.
    ("script", "my_unlock_front_door_script", "", "medium"),
    ("script", "some_random_custom_script_id", "", "medium"),
    # Reloading or stopping cannot set off what one contains.
    ("scene", "reload", "", "low"),
    ("script", "reload", "", "low"),
    ("script", "turn_off", "", "low"),
    ("automation", "turn_off", "", "low"),
    ("automation", "reload", "", "low"),
])
def test_ratings(policy, domain, service, entity_id, risk):
    assert policy.classify(domain, service, entity_id)[0] == risk
