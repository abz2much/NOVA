"""Phase 3 — local_engine's fast path uses the SAME status vocabulary
("verified"/"accepted"/"unverified"/"error") as agent.py, via the shared
entity_verify module. Covers _execute_action_verified directly (the
single-entity wrapper), _resp's status-aware wording, and the goodnight
shortcut's honest, per-entity-verified rewrite.
"""
import pytest

from fakes import FakeHass


@pytest.fixture
def le(load):
    return load("local_engine")


@pytest.fixture
def no_sleep(load, monkeypatch):
    """Both entity_verify's fast synchronous check and agent's retrying
    background _verify_control (reused here for lock/cover/turn_on/off
    fallback) have their own sleep seam -- mock both, or these tests take
    real wall-clock seconds."""
    ev = load("entity_verify")
    agent = load("agent")
    calls = {"entity_verify": 0, "agent": 0}
    async def _ev_sleep(_secs):
        calls["entity_verify"] += 1
    async def _agent_sleep(_secs):
        calls["agent"] += 1
    monkeypatch.setattr(ev, "_SLEEP", _ev_sleep)
    monkeypatch.setattr(agent, "_VERIFY_SLEEP", _agent_sleep)
    return calls


@pytest.fixture
def activity(load, monkeypatch):
    db = load("database")
    sink = []
    monkeypatch.setattr(db, "save_activity", lambda **kw: sink.append(kw))
    return sink


# ── turn_on / turn_off ───────────────────────────────────────────────────────

async def test_turn_on_light_verified_when_state_flips(le, no_sleep):
    hass = FakeHass()
    hass.states.set("light.den", "off")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set("light.den", "on")
    hass.services.async_call = call_and_flip

    status = await le._execute_action_verified(hass, "turn_on", "light.den", {}, "Den")
    assert status == "verified"
    assert no_sleep["entity_verify"] == 0
    hass.close_pending()


async def test_turn_on_light_unverified_schedules_background_retry(le, no_sleep, activity):
    hass = FakeHass()
    hass.states.set("light.den", "off")   # never flips

    status = await le._execute_action_verified(hass, "turn_on", "light.den", {}, "Den")
    assert status == "unverified"
    assert no_sleep["entity_verify"] == 4  # 0.25s x 4 = bounded 1.0s poll

    await hass.drain()
    calls = [c for c in hass.service_calls if c[:2] == ("light", "turn_on")]
    assert len(calls) == 2  # original + 1 background retry (turn_on is idempotent-safe)
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"


async def test_turn_off_switch_verified(le, no_sleep):
    hass = FakeHass()
    hass.states.set("switch.pump", "on")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set("switch.pump", "off")
    hass.services.async_call = call_and_flip

    status = await le._execute_action_verified(hass, "turn_off", "switch.pump", {}, "Pump")
    assert status == "verified"


async def test_turn_on_non_fast_domain_skips_sync_check_stays_accepted(le, no_sleep):
    """media_player is not in FAST_VERIFY_DOMAINS -- turn_on there must never
    reach the synchronous poll; it falls straight to the existing
    background-only accepted path."""
    hass = FakeHass()
    hass.states.set("media_player.den", "off")

    status = await le._execute_action_verified(hass, "turn_on", "media_player.den", {}, "Den")
    assert status == "accepted"
    assert no_sleep["entity_verify"] == 0
    await hass.drain()
    assert no_sleep["agent"] >= 1  # the existing background _verify_control DID run


# ── toggle ────────────────────────────────────────────────────────────────────

async def test_toggle_light_verified_from_derived_pre_state(le, no_sleep):
    hass = FakeHass()
    hass.states.set("light.den", "off")
    async def call_and_flip(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set("light.den", "on")
    hass.services.async_call = call_and_flip

    status = await le._execute_action_verified(hass, "toggle", "light.den", {}, "Den")
    assert status == "verified"


async def test_toggle_light_unverified_records_honestly_never_auto_retries(le, no_sleep, activity):
    hass = FakeHass()
    hass.states.set("light.den", "off")  # never flips

    status = await le._execute_action_verified(hass, "toggle", "light.den", {}, "Den")
    assert status == "unverified"
    assert no_sleep["entity_verify"] == 4

    await hass.drain()
    calls = [c for c in hass.service_calls if c[:2] == ("light", "toggle")]
    assert len(calls) == 1  # exactly the original call -- NO retry
    assert no_sleep["agent"] == 0  # _verify_control (which retries) never scheduled
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"
    assert "reverse a delayed successful action" in activity[0]["message"]


async def test_toggle_non_fast_domain_is_accepted_no_check_no_retry(le, no_sleep):
    hass = FakeHass()
    hass.states.set("media_player.den", "off")

    status = await le._execute_action_verified(hass, "toggle", "media_player.den", {}, "Den")
    assert status == "accepted"
    assert no_sleep["entity_verify"] == 0
    await hass.drain()
    assert no_sleep["agent"] == 0  # toggle is NEVER sent to _verify_control, any domain


# ── dim / brighten (brightness) ───────────────────────────────────────────────

async def test_dim_verified_within_tolerance(le, no_sleep):
    hass = FakeHass()
    hass.states.set("light.den", "off")
    async def call_and_set(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set("light.den", "on", brightness=128)  # ~50.2%
    hass.services.async_call = call_and_set

    status = await le._execute_action_verified(
        hass, "dim", "light.den", {"brightness_pct": 50}, "Den")
    assert status == "verified"


async def test_dim_unverified_records_honestly_never_auto_retries(le, no_sleep, activity):
    hass = FakeHass()
    hass.states.set("light.den", "on", brightness=25)  # far from requested 80%

    status = await le._execute_action_verified(
        hass, "dim", "light.den", {"brightness_pct": 80}, "Den")
    assert status == "unverified"

    await hass.drain()
    calls = [c for c in hass.service_calls if c[:2] == ("light", "turn_on")]
    assert len(calls) == 1  # no retry
    assert no_sleep["agent"] == 0  # never sent to the retrying _verify_control
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"
    assert "cannot validate the requested brightness level" in activity[0]["message"]


async def test_brighten_defaults_to_100_percent(le, no_sleep):
    hass = FakeHass()
    hass.states.set("light.den", "off")
    async def call_and_set(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        hass.states.set("light.den", "on", brightness=255)
    hass.services.async_call = call_and_set

    status = await le._execute_action_verified(hass, "brighten", "light.den", {}, "Den")
    assert status == "verified"


# ── lock / unlock / open / close (unchanged background path) ────────────────

async def test_lock_reports_accepted_and_schedules_background_verify(le, no_sleep, activity):
    hass = FakeHass()
    hass.states.set("lock.front", "unlocked")   # never reaches 'locked'

    status = await le._execute_action_verified(hass, "lock", "lock.front", {}, "Front")
    assert status == "accepted"
    assert no_sleep["entity_verify"] == 0  # locks never touch the new synchronous check
    await hass.drain()
    assert no_sleep["agent"] >= 1  # existing retrying verifier still runs in the background
    # Phase 3 correction: the retrying _verify_control's own activity record
    # must attribute to local_engine, not silently default to "agent".
    assert len(activity) == 1
    assert activity[0]["source"] == "local_engine"


async def test_close_cover_reports_accepted(le, no_sleep):
    hass = FakeHass()
    hass.states.set("cover.garage", "open")
    status = await le._execute_action_verified(hass, "close", "cover.garage", {}, "Garage")
    assert status == "accepted"
    hass.close_pending()


# ── errors ────────────────────────────────────────────────────────────────────

async def test_service_exception_reports_error_status(le, no_sleep):
    hass = FakeHass()
    hass.states.set("light.den", "off")
    async def boom(*a, **k):
        raise RuntimeError("offline")
    hass.services.async_call = boom
    status = await le._execute_action_verified(hass, "turn_on", "light.den", {}, "Den")
    assert status == "error"


# ── _resp wording ─────────────────────────────────────────────────────────────

def test_resp_unverified_wording_does_not_claim_completion(le):
    text = le._resp("turn_on", "Den", True, {}, "sir", status="unverified")
    assert "can't confirm" in text
    assert "is on" not in text


def test_resp_unverified_brightness_wording(le):
    text = le._resp("dim", "Den", True, {"brightness_pct": 80}, "sir", status="unverified")
    assert "80%" in text
    assert "brightness reached that level" in text


def test_resp_lock_accepted_does_not_claim_completion(le):
    text = le._resp("lock", "Front Door", True, {}, "sir", status="accepted")
    assert "is locked" not in text
    assert "Locking" in text


def test_resp_verified_turn_on_unchanged_wording(le):
    text = le._resp("turn_on", "Den", True, {}, "sir", status="verified")
    assert "is on" in text


# ── goodnight shortcut ────────────────────────────────────────────────────────

async def test_goodnight_reports_sent_not_confirmed_and_verifies_in_background(
    le, no_sleep, activity, monkeypatch,
):
    monkeypatch.setattr("os.path.exists", lambda path: False)  # no scene.goodnight found
    hass = FakeHass()
    hass.states.set("light.den", "on", friendly_name="Den")
    hass.states.set("light.hall", "on", friendly_name="Hall")
    hass.states.set("light.office", "off", friendly_name="Office")  # already off, skipped
    hass.states.set("lock.front", "unlocked", friendly_name="Front Door")

    result = await le.try_local(hass, "goodnight", "sir")
    assert result is not None and result.handled
    assert "I've sent the command to turn off 2 lights and lock 1 lock" in result.text
    assert "lights off" not in result.text  # must not claim confirmed completion
    assert "locks secured" not in result.text

    light_calls = [c for c in hass.service_calls if c[0] == "light" and c[1] == "turn_off"]
    lock_calls = [c for c in hass.service_calls if c[0] == "lock" and c[1] == "lock"]
    assert len(light_calls) == 2
    assert len(lock_calls) == 1
    assert all(c[2].get("entity_id") for c in light_calls)  # blocking, per-entity calls

    await hass.drain()  # background verification for each entity actually runs
    assert no_sleep["agent"] >= 1
    assert activity and all(a["source"] == "local_engine" for a in activity)
