"""SafetyManager._nighttime_lockdown: the sleeping sweep that locks unlocked
locks and closes open covers (only with the automatic lockdown opt in).

Characterisation tests (8.7.15). The opt in itself, and the "turning it off
mid sweep" cancel, are covered in test_lockdown_resilience.py; the exempt lock
list on the formal lockdown sweep in test_lockdown_exempt_locks.py. These pin
what the sweep locks, what it skips, how one failure is handled and how the
audit rows end up. test_current_behaviour_* tests pin something odd that is
not being changed here (see "Found, not fixed" in the PR).
"""
import os
import tempfile

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture(autouse=True)
def audit_db(al, monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(al, "_DEFAULT_DB", path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
def safety(cc, fake_hass):
    s = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    s.sweep_verify_delay = 0     # 8.24.0: the sweep checks what it secured
    return s


@pytest.fixture(autouse=True)
def devices_obey(fake_hass):
    """8.24.0: the sweep rereads each lock and cover after acting, so the
    fake devices change state when commanded, as real ones do."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        if st is not None and new:
            fake_hass.states.set(eid, new, **dict(st.attributes))
    fake_hass.services.async_call = call


def _rows(al):
    pages = al.page_requests()["requests"]
    return [t for p in pages for t in p["targets"]]


def _fail_for(fake_hass, *entities):
    """Make the service call raise for the given entities (others still work)."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        if (data or {}).get("entity_id") in entities:
            raise RuntimeError("device unreachable")
        await real(domain, service, data, blocking=blocking, **kw)
    fake_hass.services.async_call = call


# ── what it locks and what it skips ─────────────────────────────────────────

async def test_only_locks_that_are_unlocked_get_a_lock_command(safety, fake_hass):
    for eid, state in (("lock.a", "unlocked"), ("lock.b", "locked"), ("lock.c", "jammed"),
                       ("lock.d", "unavailable"), ("lock.e", "locking"), ("lock.f", "unlocking"),
                       ("lock.g", "open")):
        fake_hass.states.set(eid, state)
    actions = await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "lock", "lock")] == ["lock.a"]
    assert len(actions) == 1


async def test_only_covers_that_are_open_get_a_close_command(safety, fake_hass):
    for eid, state in (("cover.a", "open"), ("cover.b", "closed"), ("cover.c", "opening"),
                       ("cover.d", "closing"), ("cover.e", "unavailable")):
        fake_hass.states.set(eid, state, device_class="garage")
    await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "cover", "close_cover")] == ["cover.a"]


async def test_current_behaviour_every_open_cover_is_closed_whatever_its_class(
        safety, fake_hass):
    """The formal lockdown only closes door, garage, window and gate covers.
    The sleeping sweep has no class filter, so blinds, curtains and awnings
    that are open at night are closed too."""
    for eid, dc in (("cover.living_blind", "blind"), ("cover.curtain", "curtain"),
                    ("cover.awning", "awning"), ("cover.shed_door", "door"),
                    ("cover.no_class", None)):
        attrs = {"device_class": dc} if dc else {}
        fake_hass.states.set(eid, "open", **attrs)
    await safety._nighttime_lockdown(0)
    closed = sorted(c[2]["entity_id"] for c in service_calls(fake_hass, "cover", "close_cover"))
    assert closed == ["cover.awning", "cover.curtain", "cover.living_blind",
                      "cover.no_class", "cover.shed_door"]


async def test_saved_exempt_thermostat_locks_are_never_touched(safety, cc, fake_hass):
    # 8.28.0: the list comes from the saved setting (a migrated install has
    # these two written there), never from a built in default.
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked")
    fake_hass.states.set("lock.upstairs_thermo_lock", "unlocked")
    fake_hass.states.set("lock.front", "unlocked")
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": [
        "lock.downstairs_thermo_lock", "lock.upstairs_thermo_lock"]})
    await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "lock", "lock")] == ["lock.front"]


async def test_with_nothing_saved_no_lock_is_exempt(safety, fake_hass):
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked")
    await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "lock", "lock")] == [
        "lock.downstairs_thermo_lock"]


async def test_the_exempt_list_comes_from_the_lockdown_manager_when_there_is_one(
        safety, cc, fake_hass):
    fake_hass.states.set("lock.thermo", "unlocked")
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked")
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": ["lock.thermo"]})
    await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "lock", "lock")] == [
        "lock.downstairs_thermo_lock"]                              # the configured list replaces the default


async def test_an_explicit_empty_exempt_list_means_exempt_nothing(safety, cc, fake_hass):
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked")
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": []})
    await safety._nighttime_lockdown(0)
    assert len(service_calls(fake_hass, "lock", "lock")) == 1


async def test_nothing_to_secure_means_no_service_calls_and_no_message(safety, fake_hass):
    fake_hass.states.set("lock.front", "locked")
    fake_hass.states.set("cover.garage", "closed", device_class="garage")
    assert await safety._nighttime_lockdown(0) == []
    assert fake_hass.service_calls == []


# ── the message ─────────────────────────────────────────────────────────────

async def test_the_message_names_what_was_locked_and_closed_and_is_low_urgency(
        safety, cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front Door")
    fake_hass.states.set("lock.back", "unlocked")                   # no friendly name: the id is used
    fake_hass.states.set("cover.garage", "open", friendly_name="Garage")
    (action,) = await safety._nighttime_lockdown(0)
    i18n = cc._notify_i18n()
    parts = [i18n.message("lockdown_locked", "en", names=i18n.join_names(["Front Door", "lock.back"], "en")),
             i18n.message("lockdown_closed", "en", names=i18n.join_names(["Garage"], "en"))]
    assert action == {
        "type": "lockdown", "urgency": "low", "auto_act": True,
        "message": i18n.message("lockdown_nighttime", "en", honorific="Sir",
                                body=i18n.join_names(parts, "en"))}


async def test_the_message_is_localised(safety, cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_hass_lang", lambda hass: "de")
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Haustür")
    (action,) = await safety._nighttime_lockdown(0)
    i18n = cc._notify_i18n()
    body = i18n.message("lockdown_locked", "de", names="Haustür")
    assert action["message"] == i18n.message("lockdown_nighttime", "de", honorific="Sir", body=body)
    assert "locked" not in action["message"].lower().split()        # not the English sentence


# ── one failure does not stop the rest ──────────────────────────────────────

async def test_one_lock_that_fails_does_not_stop_the_others_and_is_named_as_not_secured(
        safety, cc, fake_hass, al, caplog, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    _fail_for(fake_hass, "lock.back")
    with caplog.at_level("WARNING"):
        (action,) = await safety._nighttime_lockdown(0)
    assert [c[2]["entity_id"] for c in service_calls(fake_hass, "lock", "lock")] == [
        "lock.front", "lock.side"]
    i18n = cc._notify_i18n()
    locked = i18n.message("lockdown_locked", "en", names=i18n.join_names(["Front", "Side"], "en"))
    assert action["message"] == i18n.message(
        "lockdown_nighttime_partial", "en", honorific="Sir", body=locked,
        failed=i18n.message("lockdown_secure_failed", "en", names="Back"))
    assert "The house is secured." not in action["message"]
    assert action["urgency"] == "high"                              # a failure is raised so it reaches the phone
    assert "failed to lock lock.back" in caplog.text
    by_entity = {r["entity_id"]: r for r in _rows(al)}
    assert by_entity["lock.front"]["execution_result"] == "verified"   # checked (8.24.0)
    assert by_entity["lock.back"]["execution_result"] == "failed"
    assert by_entity["lock.back"]["reason_code"] == "service_call_failed"


async def test_a_failing_cover_does_not_stop_the_locks_and_is_named_as_not_secured(
        safety, fake_hass, al):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("cover.garage", "open", friendly_name="Garage")
    _fail_for(fake_hass, "cover.garage")
    (action,) = await safety._nighttime_lockdown(0)
    assert "Front" in action["message"] and "Garage" in action["message"]
    assert "not fully secured" in action["message"] and "The house is secured." not in action["message"]
    assert {r["entity_id"]: r["execution_result"] for r in _rows(al)} == {
        "lock.front": "verified", "cover.garage": "failed"}         # checked (8.24.0)


async def test_when_everything_fails_the_message_says_so_and_never_claims_success(
        safety, cc, fake_hass, monkeypatch):
    """Before 8.7.16 this returned nothing at all, so a night where every lock
    failed produced no message."""
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("cover.garage", "open", friendly_name="Garage")
    _fail_for(fake_hass, "lock.front", "cover.garage")
    (action,) = await safety._nighttime_lockdown(0)
    i18n = cc._notify_i18n()
    assert action["message"] == i18n.message(
        "lockdown_nighttime_failed_only", "en", honorific="Sir",
        failed=i18n.message("lockdown_secure_failed", "en", names=i18n.join_names(["Front", "Garage"], "en")))
    assert action["urgency"] == "high" and action["type"] == "lockdown"
    assert "The house is secured." not in action["message"]


# ── the audit trail ─────────────────────────────────────────────────────────

async def test_one_request_covers_every_target_of_one_sweep(safety, fake_hass, al):
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("cover.garage", "open")
    await safety._nighttime_lockdown(0)
    pages = al.page_requests()["requests"]
    assert len(pages) == 1
    assert pages[0]["action"] == "lockdown" and pages[0]["source"] == "safety_routine"
    assert sorted(t["entity_id"] for t in pages[0]["targets"]) == ["cover.garage", "lock.front"]
    assert {t["execution_result"] for t in pages[0]["targets"]} == {"verified"}  # checked (8.24.0)
    assert sorted((t["domain"], t["service"]) for t in pages[0]["targets"]) == [
        ("cover", "close_cover"), ("lock", "lock")]


async def test_an_exempt_lock_gets_no_audit_row(safety, cc, fake_hass, al):
    cc._CORE.lockdown_mgr = cc.LockdownManager(fake_hass, {"lockdown_exempt_locks": [
        "lock.downstairs_thermo_lock"]})
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked")
    await safety._nighttime_lockdown(0)
    assert _rows(al) == []


async def test_nothing_to_secure_writes_no_audit_rows(safety, fake_hass, al):
    fake_hass.states.set("lock.front", "locked")
    await safety._nighttime_lockdown(0)
    assert _rows(al) == []


# ── the opt in generation guard ─────────────────────────────────────────────

async def test_a_sweep_started_under_an_old_setting_does_nothing(safety, fake_hass):
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("cover.garage", "open")
    old = safety._automatic_generation
    safety.set_automatic_lockdown(True)                             # any change bumps the generation
    assert await safety._nighttime_lockdown(old) == []
    assert fake_hass.service_calls == []


async def test_a_sweep_is_cancelled_when_the_opt_in_is_switched_off(safety, fake_hass):
    fake_hass.states.set("lock.front", "unlocked")
    generation = safety._automatic_generation
    safety.set_automatic_lockdown(False)
    assert await safety._nighttime_lockdown(generation) == []
    assert fake_hass.service_calls == []


async def test_only_the_literal_true_re_enables_the_automatic_sweep(safety):
    safety.set_automatic_lockdown("yes")
    assert safety.config["lockdown_auto_on_arm"] is False
    safety.set_automatic_lockdown(True)
    assert safety.config["lockdown_auto_on_arm"] is True


async def test_current_behaviour_audit_rows_of_a_cancelled_sweep_stay_pending(
        safety, fake_hass, al):
    """The audit rows are written before the first guard check, so a sweep that
    is cancelled because the setting changed leaves its rows at pending."""
    fake_hass.states.set("lock.front", "unlocked")
    generation = safety._automatic_generation
    safety.set_automatic_lockdown(False)
    await safety._nighttime_lockdown(generation)
    assert [r["execution_result"] for r in _rows(al)] == ["pending"]


async def test_the_house_is_secured_is_only_said_when_nothing_failed(safety, cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    (ok,) = await safety._nighttime_lockdown(0)
    assert ok["message"].endswith("The house is secured.") and ok["urgency"] == "low"
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    _fail_for(fake_hass, "lock.back")
    (bad,) = await safety._nighttime_lockdown(0)
    assert "The house is secured." not in bad["message"] and "Back" in bad["message"]


async def test_an_open_window_sensor_is_still_not_mentioned_by_the_nighttime_sweep(safety, fake_hass):
    """Out of scope for 8.7.16: window sensors are not part of this message."""
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("binary_sensor.window", "on", device_class="window", friendly_name="Window")
    (action,) = await safety._nighttime_lockdown(0)
    assert "window" not in action["message"].lower() and action["message"].endswith("The house is secured.")


async def test_the_sleeping_tick_with_the_opt_in_off_does_not_start_a_sweep_at_all(
        cc, fake_hass, al, clock):
    """The sweep checks the setting again inside, so the visible result of a
    wrong gate in tick() would only be pending audit rows."""
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("lock.front", "unlocked")
    assert await safety.tick(sleeping=True, anyone_home=True) == []
    assert _rows(al) == [] and safety._last_lockdown_check == 0.0
