"""Tests for verify-after-act (v6.38.0) — control actions confirm their outcome,
retry once, and report honestly. Stubs homeassistant.helpers.llm at import."""
import sys
import types

import pytest

if "homeassistant.helpers.llm" not in sys.modules:
    _llm = types.ModuleType("homeassistant.helpers.llm")
    _llm.async_get_api = lambda *a, **k: None
    sys.modules["homeassistant.helpers.llm"] = _llm


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def fast_sleep(agent, monkeypatch):
    calls = {"n": 0, "hook": None}
    async def _sleep(_secs):
        calls["n"] += 1
        if calls["hook"]:
            calls["hook"](calls["n"])
    monkeypatch.setattr(agent, "_VERIFY_SLEEP", _sleep)
    return calls


@pytest.fixture
def activity(load, monkeypatch):
    db = load("database")
    sink = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: sink.append(kw))
    return sink


@pytest.fixture
def fast_entity_verify_sleep(load, monkeypatch):
    """Phase 3's synchronous fast-domain check uses its OWN sleep seam
    (entity_verify._SLEEP), separate from agent._VERIFY_SLEEP (which only
    the existing retrying background _verify_control uses) -- must be
    mocked separately or set_brightness/toggle tests take a real ~1s."""
    ev = load("entity_verify")
    calls = {"n": 0}
    async def _sleep(_secs):
        calls["n"] += 1
    monkeypatch.setattr(ev, "_SLEEP", _sleep)
    return calls


def _retries(fake_hass, domain, service):
    return [c for c in fake_hass.service_calls if c[0] == domain and c[1] == service]


async def test_persistent_failure_retries_once_and_logs(agent, fake_hass, fast_sleep, activity):
    fake_hass.states.set("cover.garage_door", "open")   # never reaches 'closed'
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "cover.garage_door", "action": "close"})
    assert '"success": true' in out.lower()
    await fake_hass.drain()                            # run the verify task
    assert len(_retries(fake_hass, "cover", "close_cover")) == 2   # act + 1 retry
    assert len(activity) == 1
    assert activity[0]["urgency"] == "medium"
    assert "did not respond" in activity[0]["message"]
    assert activity[0]["source"] == "agent"


# ── verification provenance: source defaults to "agent", callers can override ──

async def test_verify_control_defaults_source_to_agent(agent, fake_hass, fast_sleep, activity):
    fake_hass.states.set("lock.front", "unlocked")   # never reaches 'locked'
    await agent._verify_control(
        fake_hass, "lock.front", "lock", "lock", "lock", {"entity_id": "lock.front"})
    await fake_hass.drain()
    assert len(activity) == 1
    assert activity[0]["source"] == "agent"


async def test_verify_control_honours_explicit_source_on_success_and_failure(
    agent, fake_hass, fast_sleep, activity,
):
    # retry succeeds -> the "needed a second attempt... succeeded" branch
    fake_hass.states.set("lock.front", "unlocked")
    fast_sleep["hook"] = (lambda n: fake_hass.states.set("lock.front", "locked")
                          if n >= 2 else None)
    await agent._verify_control(
        fake_hass, "lock.front", "lock", "lock", "lock",
        {"entity_id": "lock.front"}, source="local_engine")
    await fake_hass.drain()
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"
    assert "succeeded on retry" in activity[0]["message"]


async def test_success_first_try_is_silent(agent, fake_hass, fast_sleep, activity):
    fake_hass.states.set("light.den", "off")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        fake_hass.states.set("light.den", "on")
    fake_hass.services.async_call = call_and_flip
    await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "turn_on"})
    await fake_hass.drain()
    assert len(_retries(fake_hass, "light", "turn_on")) == 1       # no retry
    assert activity == []                                          # silent


async def test_recovery_on_retry_logs_low(agent, fake_hass, fast_sleep, activity):
    fake_hass.states.set("lock.front", "unlocked")
    # flip to locked during the post-retry sleep (2nd sleep on this path)
    fast_sleep["hook"] = (lambda n: fake_hass.states.set("lock.front", "locked")
                          if n >= 2 else None)
    await agent._exec_control_device(
        fake_hass, {"entity_id": "lock.front", "action": "lock"})
    await fake_hass.drain()
    assert len(_retries(fake_hass, "lock", "lock")) == 2
    assert len(activity) == 1 and activity[0]["urgency"] == "low"
    assert "retry" in activity[0]["message"]


async def test_transitional_state_gets_grace_then_passes(agent, fake_hass, fast_sleep, activity):
    fake_hass.states.set("cover.garage_door", "open")
    async def call_then_move(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        fake_hass.states.set("cover.garage_door", "closing")
    fake_hass.services.async_call = call_then_move
    fast_sleep["hook"] = (lambda n: fake_hass.states.set("cover.garage_door", "closed")
                          if n >= 2 else None)
    await agent._exec_control_device(
        fake_hass, {"entity_id": "cover.garage_door", "action": "close"})
    await fake_hass.drain()
    assert len(_retries(fake_hass, "cover", "close_cover")) == 1   # no retry needed
    assert activity == []


async def test_set_temperature_and_volume_skip_the_retrying_background_verifier(
    agent, fake_hass, fast_sleep,
):
    """set_temperature/volume_set are out of scope this phase -- they must
    never reach the retrying background _verify_control (agent._VERIFY_SLEEP
    stays uncalled), unlike set_brightness/toggle which now use the
    separate, non-retrying entity_verify fast check instead (see below)."""
    fake_hass.states.set("climate.den", "on")
    await agent._exec_control_device(
        fake_hass, {"entity_id": "climate.den", "action": "set_temperature", "value": 70})
    await fake_hass.drain()
    assert fast_sleep["n"] == 0

    fake_hass.states.set("media_player.den", "on")
    await agent._exec_control_device(
        fake_hass, {"entity_id": "media_player.den", "action": "volume_set", "value": 40})
    await fake_hass.drain()
    assert fast_sleep["n"] == 0


def test_new_tools_registered(agent):
    names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    assert {"schedule_followup", "manage_followups"} <= names
    assert "schedule_followup" in agent._TOOL_MAP
    assert "manage_followups" in agent._TOOL_MAP


async def test_schedule_and_manage_exec_roundtrip(agent, load, fake_hass, tmp_path, monkeypatch):
    fu = load("followups")
    monkeypatch.setattr(fu, "DB_PATH", str(tmp_path / "p.db"))
    out = await agent._exec_schedule_followup(
        fake_hass, {"instruction": "check the oven", "delay_minutes": 10})
    assert "Follow-up #1 scheduled" in out
    listing = await agent._exec_manage_followups(fake_hass, {"action": "list"})
    assert "check the oven" in listing
    cancelled = await agent._exec_manage_followups(
        fake_hass, {"action": "cancel", "followup_id": 1})
    assert "#1 cancelled" in cancelled


# ── Phase 3: fast-domain synchronous verification ────────────────────────────

def _calls(fake_hass, domain, service):
    return [c for c in fake_hass.service_calls if c[0] == domain and c[1] == service]


async def test_fast_domain_turn_on_verified_immediately_no_sleep(
    agent, fake_hass, fast_entity_verify_sleep,
):
    """State already correct the instant after the blocking call -> verified
    with zero sleeps (the immediate-check-before-any-sleep contract)."""
    fake_hass.states.set("light.den", "off")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        fake_hass.states.set("light.den", "on")
    fake_hass.services.async_call = call_and_flip

    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "turn_on"})
    assert '"status": "verified"' in out
    assert fast_entity_verify_sleep["n"] == 0


async def test_fast_domain_turn_on_unverified_after_bound_schedules_background(
    agent, fake_hass, fast_entity_verify_sleep, fast_sleep, activity,
):
    """State never flips within the 1.0s bound -> unverified, and the
    EXISTING retrying background _verify_control is scheduled (turn_on is
    idempotent, safe to retry)."""
    fake_hass.states.set("switch.pump", "off")  # never flips to "on"
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "switch.pump", "action": "turn_on"})
    assert '"status": "unverified"' in out
    assert fast_entity_verify_sleep["n"] == 4  # 0.25s x 4 = 1.0s bound, no more

    await fake_hass.drain()
    assert len(_calls(fake_hass, "switch", "turn_on")) == 2  # original + _verify_control's 1 retry
    assert len(activity) == 1
    assert activity[0]["urgency"] == "medium"


async def test_fast_domain_gating_excludes_other_domains(
    agent, fake_hass, fast_entity_verify_sleep, fast_sleep,
):
    """turn_on on a domain OTHER than light/switch/fan must never reach the
    new synchronous check -- falls through to the existing background-only
    path unchanged."""
    fake_hass.states.set("media_player.den", "off")
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "media_player.den", "action": "turn_on"})
    assert '"status": "accepted"' in out
    assert fast_entity_verify_sleep["n"] == 0
    await fake_hass.drain()  # let the background _verify_control (agent._VERIFY_SLEEP) run
    assert fast_sleep["n"] >= 1  # the OLD background path did run for this domain


async def test_toggle_verified_from_derived_pre_state(
    agent, fake_hass, fast_entity_verify_sleep,
):
    fake_hass.states.set("light.den", "off")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        fake_hass.states.set("light.den", "on")
    fake_hass.services.async_call = call_and_flip

    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "toggle"})
    assert '"status": "verified"' in out
    assert fast_entity_verify_sleep["n"] == 0


async def test_toggle_unverified_records_honestly_and_never_auto_retries(
    agent, fake_hass, fast_entity_verify_sleep, fast_sleep, activity,
):
    fake_hass.states.set("light.den", "off")  # never flips to "on"
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "toggle"})
    assert '"status": "unverified"' in out
    assert fast_entity_verify_sleep["n"] == 4

    await fake_hass.drain()
    assert len(_calls(fake_hass, "light", "toggle")) == 1  # exactly the original call, NO retry
    assert fast_sleep["n"] == 0  # _verify_control (which retries) was never scheduled
    assert len(activity) == 1
    assert "toggle" in activity[0]["message"]
    assert "reverse a delayed successful action" in activity[0]["message"]


async def test_toggle_with_unknown_pre_state_is_accepted_no_check(
    agent, fake_hass, fast_entity_verify_sleep,
):
    # entity_id with no prior state at all -> pre_state is None, can't derive
    # an expectation. _exec_control_device requires the entity to exist
    # (returns an error otherwise), so simulate an existing-but-unknown state.
    fake_hass.states.set("light.den", "unknown")
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "toggle"})
    assert '"status": "accepted"' in out
    assert fast_entity_verify_sleep["n"] == 0


async def test_set_brightness_verified_within_tolerance(
    agent, fake_hass, fast_entity_verify_sleep,
):
    fake_hass.states.set("light.den", "off")
    async def call_and_set(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        fake_hass.states.set("light.den", "on", brightness=128)  # ~50.2%
    fake_hass.services.async_call = call_and_set

    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "set_brightness", "value": 50})
    assert '"status": "verified"' in out
    assert fast_entity_verify_sleep["n"] == 0


async def test_set_brightness_unverified_records_honestly_never_auto_retries(
    agent, fake_hass, fast_entity_verify_sleep, fast_sleep, activity,
):
    fake_hass.states.set("light.den", "on", brightness=25)  # far from requested 80%
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "set_brightness", "value": 80})
    assert '"status": "unverified"' in out
    assert fast_entity_verify_sleep["n"] == 4

    await fake_hass.drain()
    assert len(_calls(fake_hass, "light", "turn_on")) == 1  # exactly the original call, no retry
    assert fast_sleep["n"] == 0  # never sent to the retrying _verify_control
    assert len(activity) == 1
    assert "brightness" in activity[0]["message"]
    assert "cannot validate the requested brightness level" in activity[0]["message"]


async def test_locks_and_covers_report_accepted_not_verified(agent, fake_hass, fast_entity_verify_sleep):
    fake_hass.states.set("lock.front", "unlocked")
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "lock.front", "action": "lock"})
    assert '"status": "accepted"' in out
    assert '"status": "verified"' not in out
    assert fast_entity_verify_sleep["n"] == 0  # locks never touch the new synchronous check
    fake_hass.close_pending()


async def test_error_status_on_service_exception(agent, fake_hass):
    async def boom(*a, **k):
        raise RuntimeError("device offline")
    fake_hass.services.async_call = boom
    fake_hass.states.set("light.den", "off")
    out = await agent._exec_control_device(
        fake_hass, {"entity_id": "light.den", "action": "turn_on"})
    assert '"status": "error"' in out
    assert '"success": false' in out.lower()


# ── Phase 3: bulk control ────────────────────────────────────────────────────

async def test_bulk_control_records_immediate_exceptions_not_swallowed(agent, fake_hass, monkeypatch):
    fake_hass.states.set("light.a", "off")
    fake_hass.states.set("light.b", "off")
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})

    calls = {"n": 0}
    async def flaky(domain, service, data=None, blocking=False, **kw):
        calls["n"] += 1
        if data.get("entity_id") == "light.b":
            raise RuntimeError("light.b offline")
        fake_hass.service_calls.append((domain, service, dict(data or {})))
    fake_hass.services.async_call = flaky

    out = await agent._exec_bulk_control(fake_hass, {"domain": "light", "action": "turn_on"})
    import json
    result = json.loads(out)
    assert result["count"] == 1
    assert result["failed"] == [{"entity_id": "light.b", "error": "light.b offline"}]
    fake_hass.close_pending()


async def test_bulk_control_schedules_background_verify_for_successes(
    agent, fake_hass, monkeypatch, fast_sleep, activity,
):
    fake_hass.states.set("light.a", "off")
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})

    async def flip(domain, service, data=None, blocking=False, **kw):
        fake_hass.service_calls.append((domain, service, dict(data or {})))
        # never actually flips -- forces the background verifier to retry
    fake_hass.services.async_call = flip

    out = await agent._exec_bulk_control(fake_hass, {"domain": "light", "action": "turn_on"})
    assert '"status": "accepted"' in out
    await fake_hass.drain()
    assert len(_calls(fake_hass, "light", "turn_on")) == 2  # original + 1 background retry
    assert len(activity) == 1


async def test_bulk_control_does_not_delay_the_response(agent, fake_hass, monkeypatch, fast_sleep):
    """Background verify scheduling must be fire-and-forget -- the response
    returns before any sleep happens (no drain needed for the response)."""
    fake_hass.states.set("light.a", "off")
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    await agent._exec_bulk_control(fake_hass, {"domain": "light", "action": "turn_on"})
    assert fast_sleep["n"] == 0  # nothing slept BEFORE the response was built
    fake_hass.close_pending()


# ── Phase 3: scene/script/automation/execute_plan wording ───────────────────

async def test_scene_script_reports_accepted_not_verified(agent, load, fake_hass, monkeypatch):
    policy = load("policy")
    async def _ok_gate(*a, **k):
        return True, None
    monkeypatch.setattr(policy, "confirm_gate", _ok_gate)
    fake_hass.states.set("scene.movie_night", "off")
    out = await agent._exec_run_scene_script(fake_hass, {"entity_id": "scene.movie_night"})
    assert '"status": "accepted"' in out
    assert '"status": "verified"' not in out


async def test_execute_plan_steps_report_accepted_not_verified(agent, fake_hass, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    fake_hass.states.set("light.den", "off")
    import json
    out = await agent._exec_execute_plan(fake_hass, {
        "goal": "evening routine",
        "steps": [{"domain": "light", "service": "turn_on", "entity_id": "light.den"}],
    })
    result = json.loads(out)
    assert result["results"][0]["status"] == "accepted"
    assert result["status"] == "accepted"


async def test_execute_plan_exception_step_reports_error(agent, fake_hass, monkeypatch):
    monkeypatch.setattr(agent, "_load_learned", lambda: {"alias": {}})
    fake_hass.states.set("light.den", "off")
    async def boom(*a, **k):
        raise RuntimeError("offline")
    fake_hass.services.async_call = boom
    import json
    out = await agent._exec_execute_plan(fake_hass, {
        "goal": "evening routine",
        "steps": [{"domain": "light", "service": "turn_on", "entity_id": "light.den"}],
    })
    result = json.loads(out)
    assert result["results"][0]["status"] == "error"
    assert result["status"] == "error"
