"""Pin what the local intent router does today (8.7.19, tests only).

LocalIntentRouter is reached in production through nova.process_intent: a
phrase and an area come in, and it closes covers, locks locks and switches
lights with no LLM and no policy gate. A nova.speak with expect_response opens
a ten second window in which a "yes" runs the pending action.

These tests drive the real router against FakeHass. The 8.7.19
test_current_behaviour_* tests here were fixed in 8.7.20: a question or a
negated phrase never acts, a refusal is never a yes, arming is refused
plainly, and "close it" closes a cover.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass

GARAGE = "garage"
HALL = "hall"
AREAS = {
    "cover.garage_door": GARAGE, "lock.garage_side": GARAGE, "light.garage": GARAGE,
    "cover.hall_blind": HALL, "lock.front_door": HALL, "light.hall": HALL,
    "media_player.hall_speaker": HALL, "alarm_control_panel.home": HALL,
}


@pytest.fixture
def ir(load):
    return load("intent.intent_router")


@pytest.fixture
def mutex_mod(load):
    return load("automation.mutex")


@pytest.fixture
def hass():
    h = FakeHass()
    h.states.set("cover.garage_door", "open", device_class="garage")
    h.states.set("lock.garage_side", "unlocked")
    h.states.set("light.garage", "on")
    h.states.set("cover.hall_blind", "open")
    h.states.set("lock.front_door", "unlocked")
    h.states.set("light.hall", "off")
    h.states.set("alarm_control_panel.home", "disarmed")
    return h


def _router(ir, hass, **kw):
    r = ir.LocalIntentRouter(hass, **kw)
    r._area_of = AREAS.get        # the area lookup normally goes to audio_routing
    return r


class _Ledger:
    """A write-ahead ledger that records the order of what it was told."""

    def __init__(self, events):
        self.events = events
        self.open: dict = {}

    def record_intent(self, eid, desired, action):
        txn = f"txn-{len(self.open)}"
        self.open[txn] = (eid, desired, action)
        self.events.append(("intent", eid, desired))
        return txn

    def mark_complete(self, txn):
        self.events.append(("complete", self.open.pop(txn)[0]))


# ── secure_area: what it moves, and only in the named area ──────────────────

async def test_secure_area_closes_covers_and_locks_locks_in_that_area_only(ir, hass):
    res = await _router(ir, hass).route("secure the garage", GARAGE)
    assert res == {"matched": True, "executed": True, "intent": "secure_area",
                   "entities": ["cover.garage_door", "lock.garage_side"]}
    assert hass.service_calls == [
        ("cover", "close_cover", {"entity_id": ["cover.garage_door"]}),
        ("lock", "lock", {"entity_id": ["lock.garage_side"]}),
    ]


async def test_secure_area_in_an_area_with_nothing_securable_does_nothing(ir, hass):
    res = await _router(ir, hass).route("secure it all", "bedroom")
    assert res["executed"] is False and res["entities"] == []
    assert hass.service_calls == []


async def test_lights_follow_the_area(ir, hass):
    r = _router(ir, hass)
    await r.route("turn off the lights", GARAGE)
    await r.route("lights on", HALL)
    assert hass.service_calls == [
        ("light", "turn_off", {"entity_id": ["light.garage"]}),
        ("light", "turn_on", {"entity_id": ["light.hall"]}),
    ]


async def test_an_unmatched_phrase_moves_nothing(ir, hass):
    assert await _router(ir, hass).route("what's the weather", GARAGE) == {
        "matched": False, "intent": None, "executed": False}
    assert hass.service_calls == []


async def test_a_failed_service_call_reports_not_executed(ir, hass):
    async def boom(*a, **k):
        raise RuntimeError("zigbee down")
    hass.services.async_call = boom
    res = await _router(ir, hass).route("lights off", GARAGE)
    assert res == {"matched": True, "executed": False, "intent": "lights_off", "entities": []}


# ── questions, negations and arming (fixed in 8.7.20) ──────────────────────

@pytest.mark.parametrize("phrase", [
    "is the garage secure?", "is the garage secure", "is it locked?",
    "are the lights off?", "can you secure the garage?",
])
async def test_a_question_moves_nothing(ir, hass, phrase):
    # 8.7.19 pinned "is the garage secure?" closing the garage door and
    # locking the lock. A question now never acts.
    res = await _router(ir, hass).route(phrase, GARAGE)
    assert res == {"matched": False, "intent": None, "executed": False}
    assert hass.service_calls == []


@pytest.mark.parametrize("phrase", [
    "don't secure the garage", "do not close the garage", "never lock up",
    "dont turn off the lights", "don’t secure the garage", "no, lights off",
])
async def test_a_negated_command_moves_nothing(ir, hass, phrase):
    # 8.7.19 pinned "don't secure the garage" securing it.
    res = await _router(ir, hass).route(phrase, GARAGE)
    assert res["matched"] is False and hass.service_calls == []


@pytest.mark.parametrize("phrase", [
    "secure the garage", "close the garage door", "lock up", "secure it",
    "turn off the lights", "kill the lights",
])
async def test_plain_commands_still_act(ir, hass, phrase):
    res = await _router(ir, hass).route(phrase, GARAGE)
    assert res["matched"] is True and res["executed"] is True and hass.service_calls


async def test_arm_the_alarm_is_refused_plainly_and_nothing_moves(ir, hass):
    # 8.7.19 pinned "arm the alarm" closing covers and locking locks, never
    # arming, and reporting executed=True. It is now refused, with a reason.
    res = await _router(ir, hass).route("arm the alarm", HALL)
    assert res == {"matched": True, "executed": False, "intent": "arm_alarm",
                   "reason": "arming the alarm is not available as a local voice "
                             "command; nothing was done"}
    assert hass.service_calls == []
    assert hass.states.get("alarm_control_panel.home").state == "disarmed"


async def test_close_it_closes_an_open_cover_and_never_turns_off_a_light(ir, hass):
    # 8.7.19 pinned "close it" turning a light off and never closing a cover.
    hass.states.set("light.hall", "on")
    res = await _router(ir, hass).route("close it", HALL)
    assert res == {"matched": True, "executed": True, "intent": "context_close",
                   "entity_id": "cover.hall_blind"}
    assert hass.service_calls == [("cover", "close_cover", {"entity_id": "cover.hall_blind"})]

    hass.service_calls.clear()
    hass.states.set("cover.hall_blind", "closed")
    res = await _router(ir, hass).route("close it", HALL)     # only a light is on now
    assert res["executed"] is False and hass.service_calls == []


async def test_pronoun_prefers_playing_media_in_the_area(ir, hass):
    hass.states.set("light.hall", "on")
    hass.states.set("media_player.hall_speaker", "playing")
    res = await _router(ir, hass).route("pause it", HALL)
    assert res["entity_id"] == "media_player.hall_speaker"
    assert hass.service_calls == [("media_player", "turn_off",
                                   {"entity_id": "media_player.hall_speaker"})]


# ── the confirmation window ─────────────────────────────────────────────────

@pytest.fixture
def timers(monkeypatch):
    """homeassistant.helpers.event.async_call_later, recording each timer."""
    made = []
    ev = types.ModuleType("homeassistant.helpers.event")

    def async_call_later(hass, delay, cb):
        made.append({"delay": delay, "cb": cb, "cancelled": False})
        return lambda: made[-1].update(cancelled=True)
    ev.async_call_later = async_call_later
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", ev)
    return made


async def test_a_yes_in_the_window_runs_the_pending_action_once(ir, hass, timers):
    r = _router(ir, hass)
    await r.open_feedback_window({"intent": "secure_area", "area": GARAGE})
    assert hass.bus.fired == [(ir.EVENT_FEEDBACK_WINDOW, {
        "timeout": ir.FEEDBACK_TIMEOUT_S, "action": {"intent": "secure_area", "area": GARAGE}})]
    assert timers[0]["delay"] == ir.FEEDBACK_TIMEOUT_S

    res = await r.handle_voice_response("yes please")
    assert res["handled"] is True and res["executed"] is True
    assert len(hass.service_calls) == 2
    assert timers[0]["cancelled"] is True
    # The window is spent: a second yes does nothing.
    assert await r.handle_voice_response("yes") == {"handled": False, "reason": "no open window"}
    assert len(hass.service_calls) == 2


async def test_the_window_expires_and_then_a_yes_does_nothing(ir, hass, timers):
    r = _router(ir, hass)
    await r.open_feedback_window({"intent": "secure_area", "area": GARAGE})
    timers[0]["cb"](None)                       # ten seconds pass
    assert await r.handle_voice_response("yes") == {"handled": False, "reason": "no open window"}
    assert hass.service_calls == []


async def test_a_plain_no_leaves_the_window_open_and_moves_nothing(ir, hass, timers):
    r = _router(ir, hass)
    await r.open_feedback_window({"intent": "secure_area", "area": GARAGE})
    assert await r.handle_voice_response("no") == {"handled": False, "affirmative": False}
    assert hass.service_calls == [] and r._pending_feedback is not None


@pytest.mark.parametrize("reply", [
    "no, don't do it", "that's not ok", "don't shut it", "do not", "stop", "cancel it",
    "never mind, no", "yes, no wait",
])
async def test_a_refusal_is_never_a_yes(ir, hass, timers, reply):
    # 8.7.19 pinned "no, don't do it", "that's not ok" and "don't shut it" all
    # confirming. A phrase with a negation is now never a yes, and the window
    # stays open for a real answer.
    r = _router(ir, hass)
    await r.open_feedback_window({"intent": "secure_area", "area": GARAGE})
    assert await r.handle_voice_response(reply) == {"handled": False, "affirmative": False}
    assert hass.service_calls == [] and r._pending_feedback is not None


@pytest.mark.parametrize("reply", ["yes", "yeah do it", "ok", "go ahead", "sure", "close it"])
async def test_a_plain_yes_still_confirms(ir, hass, timers, reply):
    r = _router(ir, hass)
    await r.open_feedback_window({"intent": "secure_area", "area": GARAGE})
    res = await r.handle_voice_response(reply)
    assert res["handled"] is True and res["executed"] is True


# ── concurrency lock and write-ahead ledger ─────────────────────────────────

async def test_an_entity_held_at_higher_priority_is_skipped(ir, mutex_mod, hass):
    reg = mutex_mod.EntityLockRegistry()
    held = reg.try_acquire("lock.garage_side", mutex_mod.Priority.SAFETY)
    res = await _router(ir, hass, mutex=reg).route("secure the garage", GARAGE)
    assert res["entities"] == ["cover.garage_door"]
    assert hass.service_calls == [("cover", "close_cover", {"entity_id": ["cover.garage_door"]})]
    # Nothing the router took is still held; the safety hold is untouched.
    assert reg.is_locked("cover.garage_door") is False
    assert held.valid and reg.held_priority("lock.garage_side") == mutex_mod.Priority.SAFETY


async def test_a_busy_pronoun_target_is_not_touched(ir, mutex_mod, hass):
    reg = mutex_mod.EntityLockRegistry()
    reg.try_acquire("light.garage", mutex_mod.Priority.SAFETY)
    res = await _router(ir, hass, mutex=reg).route("turn it off", GARAGE)
    assert res == {"matched": True, "executed": False, "intent": "context_off",
                   "entity_id": "light.garage",
                   "reason": "entity busy (higher-priority lock)"}
    assert hass.service_calls == []


async def test_high_stakes_intent_is_written_before_the_call_and_completed_after(ir, mutex_mod, hass):
    events = []
    ledger = _Ledger(events)
    real_call = hass.services.async_call

    async def call(domain, service, data=None, blocking=False):
        events.append(("call", domain, service))
        await real_call(domain, service, data, blocking)
    hass.services.async_call = call

    await _router(ir, hass, ledger=ledger, mutex=mutex_mod.EntityLockRegistry()).route(
        "secure the garage", GARAGE)
    assert events == [
        ("intent", "cover.garage_door", "closed"), ("call", "cover", "close_cover"),
        ("complete", "cover.garage_door"),
        ("intent", "lock.garage_side", "locked"), ("call", "lock", "lock"),
        ("complete", "lock.garage_side"),
    ]


async def test_a_failed_high_stakes_call_leaves_the_intent_open_for_boot_recovery(ir, mutex_mod, hass):
    events = []
    ledger = _Ledger(events)
    reg = mutex_mod.EntityLockRegistry()

    async def boom(domain, service, data=None, blocking=False):
        raise RuntimeError("cover jammed")
    hass.services.async_call = boom

    res = await _router(ir, hass, ledger=ledger, mutex=reg).route("secure the garage", GARAGE)
    assert res["executed"] is False
    assert sorted(eid for eid, _d, _a in ledger.open.values()) == [
        "cover.garage_door", "lock.garage_side"]
    assert not reg.is_locked("cover.garage_door") and not reg.is_locked("lock.garage_side")


async def test_lights_are_not_written_to_the_ledger(ir, hass):
    events = []
    await _router(ir, hass, ledger=_Ledger(events)).route("lights off", GARAGE)
    assert events == []
