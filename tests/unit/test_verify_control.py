"""Verify-after-act (v6.38.0) and the honest status words around it.

After a control action Nova looks again. Lights, switches and fans get a short
synchronous poll (entity_verify); locks, covers and other domains get the
background checker (_verify_control), which retries once and records an
activity entry when the device still has not got there. Toggles and
brightness are never retried automatically. Bulk control and execute_plan
report "accepted", never "verified" or "completed", because Home Assistant
only accepted the calls.
"""
from __future__ import annotations

import json

import pytest


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def checker_waits(agent, monkeypatch):
    """Replace the background checker's sleep. ``on_wait`` (if set) is called
    with the running count, so a test can move a device mid-check."""
    waits = {"count": 0, "on_wait": None}

    async def instant(_seconds):
        waits["count"] += 1
        if waits["on_wait"] is not None:
            waits["on_wait"](waits["count"])

    monkeypatch.setattr(agent, "_VERIFY_SLEEP", instant)
    return waits


@pytest.fixture
def poll_waits(load, monkeypatch):
    """Replace the fast light/switch/fan poll's own sleep and count its ticks.
    It is separate from the background checker's sleep and must be stubbed
    on its own, or every unconfirmed poll really waits about a second."""
    ticks = {"count": 0}

    async def instant(_seconds):
        ticks["count"] += 1

    monkeypatch.setattr(load("entity_verify"), "_SLEEP", instant)
    return ticks


@pytest.fixture
def activity(load, monkeypatch):
    entries: list[dict] = []
    monkeypatch.setattr(load("database"), "save_activity",
                        lambda **entry: entries.append(entry))
    return entries


@pytest.fixture
def no_aliases(agent, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})


# ── helpers ─────────────────────────────────────────────────────────────────

def _device_reaches(hass, entity_id, state, **attributes):
    """A services.async_call that records the call and then moves the device."""
    async def call(domain, service, data=None, blocking=False, **kwargs):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set(entity_id, state, **attributes)
    return call


def _failing_for(hass, *offline):
    """A services.async_call that raises for the listed entities only."""
    async def call(domain, service, data=None, blocking=False, **kwargs):
        if (data or {}).get("entity_id") in offline:
            raise RuntimeError(f"{data['entity_id']} offline")
        hass.service_calls.append((domain, service, dict(data or {})))
    return call


def _sent(hass, domain, service) -> int:
    return sum(1 for call in hass.service_calls if call[:2] == (domain, service))


async def _control(agent, hass, entity_id, action, value=None) -> dict:
    request = {"entity_id": entity_id, "action": action}
    if value is not None:
        request["value"] = value
    return json.loads(await agent._exec_control_device(hass, request))


async def _bulk(agent, hass, domain, action) -> dict:
    return json.loads(await agent._exec_bulk_control(hass, {"domain": domain, "action": action}))


async def _plan(agent, hass, *entity_ids) -> dict:
    steps = [{"domain": "light", "service": "turn_on", "entity_id": eid} for eid in entity_ids]
    out = await agent._exec_execute_plan(hass, {"goal": "evening routine", "steps": steps})
    return json.loads(out)


# ── the background checker ──────────────────────────────────────────────────

async def test_stuck_cover_is_retried_once_then_reported(agent, fake_hass, checker_waits,
                                                         activity):
    fake_hass.states.set("cover.garage_door", "open")       # never reaches closed
    result = await _control(agent, fake_hass, "cover.garage_door", "close")
    assert result["success"] is True
    await fake_hass.drain()
    assert _sent(fake_hass, "cover", "close_cover") == 2    # the act plus one retry
    assert len(activity) == 1
    entry = activity[0]
    assert entry["urgency"] == "medium"
    assert "did not respond" in entry["message"]
    assert "state: open" in entry["message"]
    assert entry["source"] == "agent"


async def test_checker_attributes_to_agent_by_default(agent, fake_hass, checker_waits,
                                                      activity):
    fake_hass.states.set("lock.front", "unlocked")          # never reaches locked
    await agent._verify_control(
        fake_hass, "lock.front", "lock", "lock", "lock", {"entity_id": "lock.front"})
    await fake_hass.drain()
    assert [entry["source"] for entry in activity] == ["agent"]


async def test_checker_keeps_caller_source_when_retry_works(agent, fake_hass, checker_waits,
                                                            activity):
    fake_hass.states.set("lock.front", "unlocked")
    # The lock settles during the wait after the retry (the second wait).
    checker_waits["on_wait"] = lambda n: (
        fake_hass.states.set("lock.front", "locked") if n >= 2 else None)
    await agent._verify_control(
        fake_hass, "lock.front", "lock", "lock", "lock",
        {"entity_id": "lock.front"}, source="local_engine")
    await fake_hass.drain()
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"
    assert "succeeded on retry" in activity[0]["message"]


async def test_lock_recovering_on_retry_is_logged_low(agent, fake_hass, checker_waits,
                                                      activity):
    fake_hass.states.set("lock.front", "unlocked")
    checker_waits["on_wait"] = lambda n: (
        fake_hass.states.set("lock.front", "locked") if n >= 2 else None)
    await _control(agent, fake_hass, "lock.front", "lock")
    await fake_hass.drain()
    assert _sent(fake_hass, "lock", "lock") == 2
    assert len(activity) == 1 and activity[0]["urgency"] == "low"
    assert "retry" in activity[0]["message"]


async def test_moving_cover_gets_a_second_look_not_a_retry(agent, fake_hass, checker_waits,
                                                           activity):
    fake_hass.states.set("cover.garage_door", "open")
    fake_hass.services.async_call = _device_reaches(fake_hass, "cover.garage_door", "closing")
    checker_waits["on_wait"] = lambda n: (
        fake_hass.states.set("cover.garage_door", "closed") if n >= 2 else None)
    await _control(agent, fake_hass, "cover.garage_door", "close")
    await fake_hass.drain()
    assert _sent(fake_hass, "cover", "close_cover") == 1
    assert activity == []


async def test_cover_still_moving_after_both_looks_is_retried(agent, fake_hass,
                                                              checker_waits, activity):
    fake_hass.states.set("cover.garage_door", "closing")    # stuck mid-travel
    await agent._verify_control(
        fake_hass, "cover.garage_door", "close", "cover", "close_cover",
        {"entity_id": "cover.garage_door"})
    assert checker_waits["count"] == 3                      # two looks, one after retry
    assert _sent(fake_hass, "cover", "close_cover") == 1
    assert len(activity) == 1 and activity[0]["urgency"] == "medium"


@pytest.mark.parametrize("action", ["set_temperature", "volume_set", "media_pause"])
async def test_checker_ignores_actions_without_an_end_state(agent, fake_hass,
                                                            checker_waits, activity, action):
    fake_hass.states.set("climate.den", "heat")
    await agent._verify_control(fake_hass, "climate.den", action, "climate", action,
                                {"entity_id": "climate.den"})
    assert checker_waits["count"] == 0
    assert fake_hass.service_calls == [] and activity == []


# ── control_device: what each action reports ────────────────────────────────

async def test_light_that_turns_on_is_silent_and_not_retried(agent, fake_hass, checker_waits,
                                                             activity):
    fake_hass.states.set("light.den", "off")
    fake_hass.services.async_call = _device_reaches(fake_hass, "light.den", "on")
    await _control(agent, fake_hass, "light.den", "turn_on")
    await fake_hass.drain()
    assert _sent(fake_hass, "light", "turn_on") == 1
    assert activity == []


@pytest.mark.parametrize("entity_id, action, value, sent", [
    ("climate.den", "set_temperature", 70, ("climate", "set_temperature")),
    ("media_player.den", "volume_set", 40, ("media_player", "volume_set")),
], ids=["temperature", "volume"])
async def test_temperature_and_volume_skip_the_background_checker(
        agent, fake_hass, checker_waits, entity_id, action, value, sent):
    # Neither has a state to check against; neither may reach the retrying checker.
    fake_hass.states.set(entity_id, "on")
    result = await _control(agent, fake_hass, entity_id, action, value)
    assert result["status"] == "accepted"
    await fake_hass.drain()
    assert checker_waits["count"] == 0
    assert _sent(fake_hass, *sent) == 1


async def test_missing_temperature_is_refused_without_a_call(agent, fake_hass):
    fake_hass.states.set("climate.den", "heat")
    result = await _control(agent, fake_hass, "climate.den", "set_temperature")
    assert result["status"] == "error" and result["success"] is False
    assert fake_hass.service_calls == []


async def test_brightness_zero_is_sent_as_zero(agent, fake_hass, poll_waits):
    fake_hass.states.set("light.den", "on", brightness=200)
    fake_hass.services.async_call = _device_reaches(fake_hass, "light.den", "off")
    await _control(agent, fake_hass, "light.den", "set_brightness", 0)
    assert fake_hass.service_calls[0] == (
        "light", "turn_on", {"entity_id": "light.den", "brightness_pct": 0})
    fake_hass.close_pending()


@pytest.mark.parametrize("entity_id, action, start, end", [
    ("light.den", "turn_on", "off", "on"),
    ("fan.loft", "turn_off", "on", "off"),
    ("light.den", "toggle", "off", "on"),
])
async def test_fast_domain_confirmed_before_any_wait(agent, fake_hass, poll_waits,
                                                     entity_id, action, start, end):
    fake_hass.states.set(entity_id, start)
    fake_hass.services.async_call = _device_reaches(fake_hass, entity_id, end)
    result = await _control(agent, fake_hass, entity_id, action)
    assert result["status"] == "verified"
    assert result["new_state"] == end
    assert poll_waits["count"] == 0


async def test_unconfirmed_switch_hands_over_to_the_checker(agent, fake_hass, poll_waits,
                                                            checker_waits, activity):
    fake_hass.states.set("switch.pump", "off")              # never flips
    result = await _control(agent, fake_hass, "switch.pump", "turn_on")
    assert result["status"] == "unverified"
    assert poll_waits["count"] == 4                         # 4 x 0.25s = the 1s bound
    await fake_hass.drain()
    assert _sent(fake_hass, "switch", "turn_on") == 2       # original plus one retry
    assert len(activity) == 1
    assert activity[0]["urgency"] == "medium"


async def test_other_domains_skip_the_fast_poll(agent, fake_hass, poll_waits, checker_waits):
    fake_hass.states.set("media_player.den", "off")
    result = await _control(agent, fake_hass, "media_player.den", "turn_on")
    assert result["status"] == "accepted"
    assert poll_waits["count"] == 0
    await fake_hass.drain()
    assert checker_waits["count"] >= 1                      # the background path ran


async def test_unconfirmed_toggle_is_recorded_and_never_repeated(
        agent, fake_hass, poll_waits, checker_waits, activity):
    fake_hass.states.set("light.den", "off")                # never flips
    result = await _control(agent, fake_hass, "light.den", "toggle")
    assert result["status"] == "unverified"
    assert poll_waits["count"] == 4
    await fake_hass.drain()
    assert _sent(fake_hass, "light", "toggle") == 1
    assert checker_waits["count"] == 0
    assert len(activity) == 1
    note = activity[0]["message"]
    assert "toggle" in note
    assert "reverse a delayed successful action" in note


async def test_toggle_from_unknown_state_is_only_accepted(agent, fake_hass, poll_waits):
    # The entity exists but its state gives no direction to expect.
    fake_hass.states.set("light.den", "unknown")
    result = await _control(agent, fake_hass, "light.den", "toggle")
    assert result["status"] == "accepted"
    assert poll_waits["count"] == 0


async def test_brightness_within_tolerance_is_verified(agent, fake_hass, poll_waits):
    fake_hass.states.set("light.den", "off")
    fake_hass.services.async_call = _device_reaches(
        fake_hass, "light.den", "on", brightness=128)       # about 50.2%
    result = await _control(agent, fake_hass, "light.den", "set_brightness", 50)
    assert result["status"] == "verified"
    assert poll_waits["count"] == 0


async def test_brightness_off_target_is_recorded_and_never_repeated(
        agent, fake_hass, poll_waits, checker_waits, activity):
    fake_hass.states.set("light.den", "on", brightness=25)  # nowhere near 80%
    result = await _control(agent, fake_hass, "light.den", "set_brightness", 80)
    assert result["status"] == "unverified"
    assert poll_waits["count"] == 4
    await fake_hass.drain()
    assert _sent(fake_hass, "light", "turn_on") == 1
    assert checker_waits["count"] == 0
    assert len(activity) == 1
    note = activity[0]["message"]
    assert "brightness" in note
    assert "cannot validate the requested brightness level" in note


async def test_lock_reports_accepted_and_skips_the_fast_poll(agent, fake_hass, poll_waits):
    fake_hass.states.set("lock.front", "unlocked")
    raw = await agent._exec_control_device(
        fake_hass, {"entity_id": "lock.front", "action": "lock"})
    assert '"status": "accepted"' in raw
    assert '"status": "verified"' not in raw
    assert poll_waits["count"] == 0
    fake_hass.close_pending()


async def test_service_exception_is_an_error(agent, fake_hass):
    async def offline(*args, **kwargs):
        raise RuntimeError("device offline")

    fake_hass.services.async_call = offline
    fake_hass.states.set("light.den", "off")
    result = await _control(agent, fake_hass, "light.den", "turn_on")
    assert result["status"] == "error"
    assert result["success"] is False


@pytest.mark.parametrize("entity_id, action", [
    ("light.missing", "turn_on"),          # no such entity
    ("light.den", "levitate"),             # no such action
])
async def test_bad_request_is_an_error_with_nothing_sent(agent, fake_hass, entity_id, action):
    fake_hass.states.set("light.den", "off")
    result = await _control(agent, fake_hass, entity_id, action)
    assert result["status"] == "error"
    assert fake_hass.service_calls == []


# ── bulk control ────────────────────────────────────────────────────────────

async def test_bulk_lists_each_failure(agent, fake_hass, no_aliases):
    for eid in ("light.a", "light.b"):
        fake_hass.states.set(eid, "off")
    fake_hass.services.async_call = _failing_for(fake_hass, "light.b")
    result = await _bulk(agent, fake_hass, "light", "turn_on")
    assert result["count"] == 1
    assert result["failed"] == [{
        "entity_id": "light.b",
        "error": "RuntimeError (details are in the Home Assistant log)",
    }]
    fake_hass.close_pending()


async def test_bulk_successes_are_checked_in_the_background(agent, fake_hass, no_aliases,
                                                           checker_waits, activity):
    fake_hass.states.set("light.a", "off")
    fake_hass.services.async_call = _failing_for(fake_hass)  # records, never flips
    raw = await agent._exec_bulk_control(fake_hass, {"domain": "light", "action": "turn_on"})
    assert '"status": "accepted"' in raw
    await fake_hass.drain()
    assert _sent(fake_hass, "light", "turn_on") == 2
    assert len(activity) == 1


async def test_bulk_replies_before_any_wait(agent, fake_hass, no_aliases, checker_waits):
    fake_hass.states.set("light.a", "off")
    await _bulk(agent, fake_hass, "light", "turn_on")
    assert checker_waits["count"] == 0                      # scheduled, not awaited
    fake_hass.close_pending()


async def test_bulk_with_every_call_failing_is_an_error(agent, fake_hass, no_aliases):
    for eid in ("light.a", "light.b"):
        fake_hass.states.set(eid, "off")
    fake_hass.services.async_call = _failing_for(fake_hass, "light.a", "light.b")
    result = await _bulk(agent, fake_hass, "light", "turn_on")
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["count"] == 0
    assert len(result["failed"]) == 2


async def test_bulk_unlock_waits_for_confirmation(agent, fake_hass, load, no_aliases,
                                                  monkeypatch):
    for eid in ("lock.front", "lock.back"):
        fake_hass.states.set(eid, "unlocked")
    monkeypatch.setattr(load("policy"), "requires_confirmation", lambda *a, **k: True)
    result = await _bulk(agent, fake_hass, "lock", "unlock")
    assert result["success"] is False
    assert result["status"] == "awaiting_confirmation"
    assert result["blocked"] == 2
    assert result["count"] == 0
    assert fake_hass.service_calls == []


async def test_bulk_partial_failure_is_accepted_with_counts(agent, fake_hass, no_aliases):
    for eid in ("light.a", "light.b"):
        fake_hass.states.set(eid, "off")
    fake_hass.services.async_call = _failing_for(fake_hass, "light.b")
    result = await _bulk(agent, fake_hass, "light", "turn_on")
    assert (result["success"], result["status"], result["count"]) == (True, "accepted", 1)
    assert "1 failed" in result["message"]
    fake_hass.close_pending()


async def test_bulk_full_success_mentions_no_failures(agent, fake_hass, no_aliases):
    for eid in ("light.a", "light.b"):
        fake_hass.states.set(eid, "off")
    result = await _bulk(agent, fake_hass, "light", "turn_on")
    assert (result["success"], result["status"], result["count"]) == (True, "accepted", 2)
    for word in ("failed", "blocked"):
        assert word not in result["message"]
    fake_hass.close_pending()


# ── scenes and plans ────────────────────────────────────────────────────────

async def test_scene_reports_accepted(agent, load, fake_hass, monkeypatch):
    async def allow(*args, **kwargs):
        return True, None, "not_required"

    monkeypatch.setattr(load("policy"), "confirm_gate", allow)
    fake_hass.states.set("scene.movie_night", "off")
    raw = await agent._exec_run_scene_script(fake_hass, {"entity_id": "scene.movie_night"})
    assert '"status": "accepted"' in raw
    assert '"status": "verified"' not in raw


async def test_plan_step_reports_accepted(agent, fake_hass, no_aliases):
    fake_hass.states.set("light.den", "off")
    result = await _plan(agent, fake_hass, "light.den")
    assert result["results"][0]["status"] == "accepted"
    assert result["status"] == "accepted"


async def test_plan_step_exception_is_an_error(agent, fake_hass, no_aliases):
    fake_hass.states.set("light.den", "off")
    fake_hass.services.async_call = _failing_for(fake_hass, "light.den")
    result = await _plan(agent, fake_hass, "light.den")
    assert result["results"][0]["status"] == "error"
    assert result["status"] == "error"


async def test_plan_summary_says_sent_not_completed(agent, fake_hass, no_aliases):
    # Home Assistant accepted the calls; nothing confirmed they finished.
    for eid in ("light.den", "light.hall"):
        fake_hass.states.set(eid, "off")
    result = await _plan(agent, fake_hass, "light.den", "light.hall")
    assert result["status"] == "accepted"
    assert "completed" not in result["message"].lower()
    assert result["message"] == "I've sent 2 of 2 step(s) for evening routine."


# ── follow-up tools that share this file ────────────────────────────────────

def test_followup_tools_are_registered(agent):
    names = {tool["function"]["name"] for tool in agent.NOVA_TOOLS}
    assert {"schedule_followup", "manage_followups"} <= names
    for name in ("schedule_followup", "manage_followups"):
        assert name in agent._TOOL_MAP


async def test_followup_schedule_list_cancel(agent, load, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(load("followups"), "DB_PATH", str(tmp_path / "followups.db"))
    scheduled = await agent._exec_schedule_followup(
        fake_hass, {"instruction": "check the oven", "delay_minutes": 10})
    assert "Follow-up #1 scheduled" in scheduled
    listed = await agent._exec_manage_followups(fake_hass, {"action": "list"})
    assert "check the oven" in listed
    cancelled = await agent._exec_manage_followups(
        fake_hass, {"action": "cancel", "followup_id": 1})
    assert "#1 cancelled" in cancelled
