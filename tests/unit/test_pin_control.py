"""Pin what the agent's device control tools do today (8.7.19, tests only).

agent_runtime/capabilities/control.py is every actuation the conversational
agent performs: control_device, bulk_control, execute_plan and
run_scene_or_script, and the background verifier that retries. These tests
run the real policy gate; only the confirmation transport, the satellite
lookup and the action log are faked, and no time passes. Tests named
test_current_behaviour_* pin behaviour that looks wrong; each says why. The
value and verifier findings from 8.7.19 were fixed in 8.7.20.
"""
from __future__ import annotations

import json
import types

import pytest

from fakes import FakeHass

SATELLITE = "device-kitchen-satellite"


@pytest.fixture
def ctl(load, monkeypatch):
    mod = load("agent_runtime.capabilities.control")

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(mod, "_VERIFY_SLEEP", no_sleep)
    monkeypatch.setattr(load("entity_verify"), "_SLEEP", no_sleep)
    return mod


@pytest.fixture
def activity(load, monkeypatch):
    rows = []
    monkeypatch.setattr(load("database"), "save_activity", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def audit(load, monkeypatch):
    al = load("action_log")
    rows = {"start": [], "many": [], "execution": [], "approval": []}

    def start(request_id, action, source, **kw):
        rows["start"].append({"action": action, "source": source, **kw})
        return len(rows["start"])

    def start_many(request_id, action, source, targets, **kw):
        rows["many"].append({"action": action, "source": source, "targets": targets})
        return {t["key"]: 100 + i for i, t in enumerate(targets)}

    monkeypatch.setattr(al, "new_request_id", lambda: "req-1")
    monkeypatch.setattr(al, "start", start)
    monkeypatch.setattr(al, "start_many", start_many)
    monkeypatch.setattr(al, "set_execution",
                        lambda aid, res, **kw: rows["execution"].append((aid, res, kw.get("reason_code"))))
    monkeypatch.setattr(al, "set_approval", lambda aid, res, **kw: rows["approval"].append((aid, res)))
    monkeypatch.setattr(al, "mark_awaiting_approval", lambda aid, **kw: None)
    return rows


@pytest.fixture
def confirm(load, monkeypatch):
    """voice_confirm behind the real policy gate. `enabled` is the
    voice_confirm_enabled setting (off by default, as shipped)."""
    vc = load("voice_confirm")
    state = {"enabled": False, "answer": "approved", "phone": "approved", "asked": []}

    async def confirm_typed(hass, question, *, entity_id="", timeout=0):
        state["asked"].append("speaker")
        return state["answer"]

    async def phone_only(hass, question, *, timeout=0):
        state["asked"].append("phone")
        return state["phone"]

    monkeypatch.setattr(vc, "is_enabled", lambda hass: state["enabled"])
    monkeypatch.setattr(vc, "confirm_typed", confirm_typed)
    monkeypatch.setattr(vc, "confirm_via_phone_only_typed", phone_only)
    monkeypatch.setattr(vc, "is_voice_satellite_device",
                        lambda hass, device_id, strict=False: device_id == SATELLITE)
    return state


@pytest.fixture
def hass():
    h = FakeHass()
    h.states.set("light.desk", "off", friendly_name="Desk")
    h.states.set("media_player.lounge", "playing", friendly_name="Lounge")
    h.states.set("climate.hall", "heat", friendly_name="Hall")
    h.states.set("lock.front_door", "locked", friendly_name="Front Door")
    h.states.set("lock.back_door", "locked", friendly_name="Back Door")
    h.states.set("alarm_control_panel.home", "armed_away", friendly_name="Alarm")
    h.states.set("script.bedtime", "off", friendly_name="Bedtime")
    return h


def _calls(hass, domain=None):
    return [c for c in hass.service_calls if domain is None or c[0] == domain]


# ── values (fixed in 8.7.20: 0 is a real value, a missing target is refused) ─

async def test_brightness_zero_stays_zero(ctl, hass, audit, confirm, activity):
    # 8.7.19 pinned 0% becoming 50% (the light switched ON at half).
    await ctl._exec_control_device(hass, {"entity_id": "light.desk", "action": "set_brightness",
                                          "value": 0})
    assert _calls(hass) == [("light", "turn_on", {"entity_id": "light.desk", "brightness_pct": 0})]


async def test_volume_zero_stays_zero(ctl, hass, audit, confirm):
    # 8.7.19 pinned "volume to 0" becoming 50%.
    await ctl._exec_control_device(hass, {"entity_id": "media_player.lounge",
                                          "action": "volume_set", "value": 0})
    assert _calls(hass) == [("media_player", "volume_set",
                             {"entity_id": "media_player.lounge", "volume_level": 0.0})]


async def test_plain_brightness_and_volume_still_work(ctl, hass, audit, confirm, activity):
    await ctl._exec_control_device(hass, {"entity_id": "light.desk", "action": "set_brightness",
                                          "value": 40})
    await ctl._exec_control_device(hass, {"entity_id": "media_player.lounge",
                                          "action": "volume_set", "value": 30})
    await ctl._exec_control_device(hass, {"entity_id": "light.desk", "action": "set_brightness"})
    assert _calls(hass) == [
        ("light", "turn_on", {"entity_id": "light.desk", "brightness_pct": 40}),
        ("media_player", "volume_set", {"entity_id": "media_player.lounge", "volume_level": 0.3}),
        ("light", "turn_on", {"entity_id": "light.desk", "brightness_pct": 50}),  # no value: 50
    ]


@pytest.mark.parametrize("args", [{}, {"value": None}, {"value": ""}])
async def test_a_missing_temperature_is_refused_and_nothing_changes(ctl, hass, audit, confirm, args):
    # 8.7.19 pinned a missing value becoming 72, even in a Celsius home.
    hass.config.units = types.SimpleNamespace(temperature_unit="°C")
    res = json.loads(await ctl._exec_control_device(
        hass, {"entity_id": "climate.hall", "action": "set_temperature", **args}))
    assert res["success"] is False and res["status"] == "error"
    assert "No target temperature was given for Hall" in res["error"]
    assert hass.service_calls == []
    assert audit["execution"] == [(1, "failed", "missing_value")]


async def test_a_given_temperature_is_set_as_given(ctl, hass, audit, confirm):
    res = json.loads(await ctl._exec_control_device(
        hass, {"entity_id": "climate.hall", "action": "set_temperature", "value": 21.5}))
    assert res["success"] is True
    assert _calls(hass) == [("climate", "set_temperature",
                             {"entity_id": "climate.hall", "temperature": 21.5})]


# ── control_device: the gate and what reaches Home Assistant ────────────────

async def test_an_unknown_action_or_entity_moves_nothing(ctl, hass, audit, confirm):
    res = json.loads(await ctl._exec_control_device(hass, {"entity_id": "lock.front_door",
                                                           "action": "unbolt"}))
    assert res == {"error": "Unknown action: unbolt", "status": "error"}
    assert audit["execution"] == [(1, "failed", "unknown_action")]
    res = json.loads(await ctl._exec_control_device(hass, {"entity_id": "lock.garden",
                                                           "action": "unlock"}))
    assert res["status"] == "error" and "not found" in res["error"]
    assert hass.service_calls == []


async def test_a_service_failure_is_an_error_not_a_success(ctl, hass, audit, confirm):
    async def boom(*a, **k):
        raise RuntimeError("lock offline")
    hass.services.async_call = boom
    res = json.loads(await ctl._exec_control_device(hass, {"entity_id": "lock.front_door",
                                                           "action": "lock"}))
    assert res["success"] is False and res["status"] == "error"
    assert audit["execution"] == [(1, "failed", "service_call_failed")]


async def test_current_behaviour_unlock_from_chat_needs_no_confirmation_by_default(
        ctl, hass, audit, confirm, activity):
    # Looks wrong for a safety default: with voice_confirm_enabled off (the
    # shipped default) an unlock typed in chat or the app runs with no
    # confirmation. policy.classify rates it high, but that rating is only
    # used when the confirmation module itself fails.
    res = json.loads(await ctl._exec_control_device(hass, {"entity_id": "lock.front_door",
                                                           "action": "unlock"}))
    assert res["success"] is True and res["status"] == "accepted"
    assert _calls(hass) == [("lock", "unlock", {"entity_id": "lock.front_door"})]
    assert confirm["asked"] == [] and audit["approval"] == [(1, "not_required")]
    hass.close_pending()


async def test_unlock_with_confirmation_on_and_a_no_does_not_unlock(ctl, hass, audit, confirm):
    confirm.update(enabled=True, answer="rejected")
    res = json.loads(await ctl._exec_control_device(hass, {"entity_id": "lock.front_door",
                                                           "action": "unlock"}))
    assert res["status"] == "awaiting_confirmation"
    assert hass.service_calls == [] and audit["execution"] == [(1, "blocked",
                                                                "confirmation_not_approved")]


async def test_unlock_by_voice_needs_a_phone_tap_even_with_confirmation_off(ctl, hass, audit, confirm):
    confirm["phone"] = "expired"
    res = json.loads(await ctl._exec_control_device(
        hass, {"entity_id": "lock.front_door", "action": "unlock"}, device_id=SATELLITE))
    assert res["status"] == "awaiting_confirmation" and confirm["asked"] == ["phone"]
    assert hass.service_calls == []


# ── the background verifier ─────────────────────────────────────────────────

@pytest.mark.parametrize("action,service,entity", [
    ("unlock", "unlock", "lock.front_door"), ("open", "open_cover", "cover.garage_door")])
async def test_the_verifier_never_sends_an_unlock_or_open_a_second_time(
        ctl, hass, audit, confirm, activity, action, service, entity):
    # 8.7.19 pinned the verifier sending a second, unconfirmed unlock when the
    # lock still read locked (someone may have locked it again on purpose).
    hass.states.set("cover.garage_door", "closed", friendly_name="Garage")
    confirm.update(enabled=True, answer="approved")
    await ctl._exec_control_device(hass, {"entity_id": entity, "action": action})
    assert confirm["asked"] == ["speaker"]
    await hass.drain()                                 # it never reports unlocked or open
    assert _calls(hass) == [(entity.split(".")[0], service, {"entity_id": entity})]   # once
    assert audit["execution"][-1] == (1, "unverified", "not_retried_could_undo_a_lock")
    assert "Not retried automatically" in activity[-1]["message"]
    assert activity[-1]["urgency"] == "medium"


async def test_the_verifier_still_retries_a_lock_once(ctl, hass, audit, confirm, activity):
    hass.states.set("lock.back_door", "unlocked")       # never reports locked
    await ctl._verify_control(hass, "lock.back_door", "lock", "lock", "lock",
                              {"entity_id": "lock.back_door"}, action_id=9)
    assert _calls(hass) == [("lock", "lock", {"entity_id": "lock.back_door"})]   # one retry
    assert audit["execution"] == [(9, "unverified", "no_response_after_retry")]


async def test_the_verifier_is_silent_when_the_device_got_there(ctl, hass, audit, confirm, activity):
    await ctl._verify_control(hass, "lock.front_door", "lock", "lock", "lock",
                              {"entity_id": "lock.front_door"}, action_id=7)
    assert hass.service_calls == [] and activity == []
    assert audit["execution"] == [(7, "verified", None)]


async def test_the_verifier_waits_out_a_moving_device_before_retrying(ctl, hass, audit, confirm, activity):
    hass.states.set("lock.back_door", "locking")
    await ctl._verify_control(hass, "lock.back_door", "lock", "lock", "lock",
                              {"entity_id": "lock.back_door"}, action_id=8)
    # still "locking" after the grace wait → one retry, then reported
    assert _calls(hass) == [("lock", "lock", {"entity_id": "lock.back_door"})]
    assert audit["execution"] == [(8, "unverified", "no_response_after_retry")]


# ── bulk_control ────────────────────────────────────────────────────────────

async def test_current_behaviour_bulk_unlock_from_chat_unlocks_every_lock(ctl, hass, audit, confirm, activity):
    # Looks wrong: "unlock all the doors" typed in chat sends an unlock to every
    # lock in the house in one go with no confirmation, while voice
    # confirmation is off (the default). Bulk control cannot ask per device,
    # but it only skips a device that requires_confirmation() says is
    # protected, and with the setting off nothing is.
    res = json.loads(await ctl._exec_bulk_control(hass, {"domain": "lock", "action": "unlock"}))
    assert res["count"] == 2 and res["status"] == "accepted"
    assert sorted(c[2]["entity_id"] for c in _calls(hass, "lock")) == [
        "lock.back_door", "lock.front_door"]
    hass.close_pending()


async def test_bulk_unlock_by_voice_skips_every_lock(ctl, hass, audit, confirm):
    res = json.loads(await ctl._exec_bulk_control(
        hass, {"domain": "lock", "action": "unlock"}, device_id=SATELLITE))
    assert res["success"] is False and res["status"] == "awaiting_confirmation"
    assert res["blocked"] == 2 and hass.service_calls == []
    assert {t["reason_code"] for t in audit["many"][0]["targets"]} == {
        "confirmation_unavailable_in_bulk"}


async def test_bulk_lock_only_touches_unlocked_locks_and_counts_failures(ctl, hass, audit, confirm):
    hass.states.set("lock.back_door", "unlocked")
    hass.states.set("lock.shed", "unlocked")
    real = hass.services.async_call

    async def call(domain, service, data=None, blocking=False):
        if data["entity_id"] == "lock.shed":
            raise RuntimeError("out of range")
        await real(domain, service, data, blocking)
    hass.services.async_call = call

    res = json.loads(await ctl._exec_bulk_control(hass, {"domain": "lock", "action": "lock"}))
    assert _calls(hass) == [("lock", "lock", {"entity_id": "lock.back_door"})]
    assert res["count"] == 1 and res["total"] == 2
    assert res["failed"] == [{"entity_id": "lock.shed", "error": "out of range"}]
    assert "1 failed" in res["message"]
    hass.close_pending()


async def test_bulk_with_nothing_succeeding_is_not_reported_as_success(ctl, hass, audit, confirm):
    hass.states.set("lock.back_door", "unlocked")

    async def boom(*a, **k):
        raise RuntimeError("hub down")
    hass.services.async_call = boom
    res = json.loads(await ctl._exec_bulk_control(hass, {"domain": "lock", "action": "lock"}))
    assert res["success"] is False and res["status"] == "error" and res["count"] == 0


# ── execute_plan and run_scene_or_script ────────────────────────────────────

async def test_current_behaviour_a_plan_can_disarm_the_alarm_unconfirmed_by_default(
        ctl, hass, audit, confirm):
    # Looks wrong for a safety default: alarm_control_panel is on the plan
    # allow list, and with voice confirmation off a model-written plan step
    # alarm_disarm runs from chat with no confirmation. policy.classify rates
    # it critical, but only uses that rating when the confirmation module fails.
    res = json.loads(await ctl._exec_execute_plan(hass, {"goal": "let the cleaner in", "steps": [
        {"domain": "alarm_control_panel", "service": "alarm_disarm",
         "entity_id": "alarm_control_panel.home"}]}))
    assert res["succeeded"] == 1
    assert _calls(hass) == [("alarm_control_panel", "alarm_disarm",
                             {"entity_id": "alarm_control_panel.home"})]


async def test_a_plan_step_is_gated_per_step_and_a_refusal_skips_only_that_step(ctl, hass, audit, confirm):
    confirm.update(enabled=True, answer="rejected")
    res = json.loads(await ctl._exec_execute_plan(hass, {"steps": [
        {"domain": "alarm_control_panel", "service": "alarm_disarm",
         "entity_id": "alarm_control_panel.home"},
        {"domain": "light", "service": "turn_on", "entity_id": "light.desk"},
        {"domain": "homeassistant", "service": "restart", "entity_id": "light.desk"},
    ]}))
    assert [r["ok"] for r in res["results"]] == [False, True, False]
    assert _calls(hass) == [("light", "turn_on", {"entity_id": "light.desk"})]
    assert "not allowed" in res["results"][2]["error"]


async def test_a_script_is_gated_and_a_refusal_runs_nothing(ctl, hass, audit, confirm):
    confirm.update(enabled=True, answer="rejected")
    res = json.loads(await ctl._exec_run_scene_script(hass, {"entity_id": "script.bedtime"}))
    assert res["status"] == "awaiting_confirmation" and hass.service_calls == []
    res = json.loads(await ctl._exec_run_scene_script(hass, {"entity_id": "lock.front_door"}))
    assert "Not a scene/script/automation" in res["error"] and hass.service_calls == []
