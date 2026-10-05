"""LockdownManager and the module level lockdown wiring in cognitive_core.

Characterisation tests (8.7.15). Existing files cover the engage sweep, the
exempt locks, the message wording, the alarm sync and its indeterminate hold,
and restoring state in the legacy and manual cases (test_lockdown_engage.py,
test_lockdown_exempt_locks.py, test_lockdown_message.py, test_lockdown_i18n.py,
test_lockdown_resilience.py). These pin the remaining decisions: the secure
state model, disengage and the manual lift, every handle_state_change branch,
the background verify, tick, state persistence and what a restart mid lockdown
does. test_current_behaviour_* tests pin something odd that is not being
changed here (see "Found, not fixed" in the PR).
"""
import json
import types

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)
from fakes import FakeState

ALARM = "alarm_control_panel.home"


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture(autouse=True)
def audit_db(al, tmp_path, monkeypatch):
    monkeypatch.setattr(al, "_DEFAULT_DB", str(tmp_path / "audit.db"))


@pytest.fixture
def emitted(cc, monkeypatch):
    """Stand in for _emit_action: collect what would be announced."""
    seen = []

    async def fake_emit(hass, config, action, sleeping):
        seen.append((action, sleeping))
    monkeypatch.setattr(cc, "_emit_action", fake_emit)
    return seen


def _mgr(cc, hass, **cfg):
    cfg.setdefault("honorific", "sir")
    return cc.LockdownManager(hass, cfg)


def _saved(cc):
    with open(cc._lockdown_state_path()) as f:
        return json.load(f)


def _ev(old, new):
    return types.SimpleNamespace(data={"entity_id": new.entity_id, "old_state": old, "new_state": new})


def _st(eid, state, **attrs):
    return FakeState(eid, state, attrs)


# ── the secure state model ──────────────────────────────────────────────────

@pytest.mark.parametrize("dom,state,expected", [
    ("lock", "locked", True), ("lock", "unlocked", False), ("lock", "jammed", False),
    ("lock", "UNLOCKED", False), ("lock", "unavailable", False),
    ("cover", "closed", True), ("cover", "closing", True), ("cover", "open", False),
    ("cover", "opening", False),
    ("binary_sensor", "off", True), ("binary_sensor", "closed", True),
    ("binary_sensor", "false", True), ("binary_sensor", "on", False),
    ("light", "on", True),                                          # anything else is "secure"
])
def test_is_secure(cc, fake_hass, dom, state, expected):
    assert _mgr(cc, fake_hass)._is_secure(dom, state) is expected


@pytest.mark.parametrize("dom,dc,expected", [
    ("lock", None, True), ("cover", "garage", True), ("cover", "door", True),
    ("cover", "garage_door", True), ("cover", "window", True), ("cover", "gate", True),
    ("cover", "blind", False), ("cover", None, False),
    ("binary_sensor", "window", False), ("light", None, False)])
def test_can_secure_only_what_has_an_actuator(cc, fake_hass, dom, dc, expected):
    assert _mgr(cc, fake_hass)._can_secure(dom, dc) is expected


@pytest.mark.parametrize("dom,dc,eid,expected", [
    ("lock", None, "lock.front", True),
    ("lock", None, "lock.downstairs_thermo_lock", False),           # exempt by default
    ("cover", "garage", "cover.g", True), ("cover", "blind", "cover.b", False),
    ("binary_sensor", "door", "binary_sensor.d", True),
    ("binary_sensor", "window", "binary_sensor.w", True),
    ("binary_sensor", "garage_door", "binary_sensor.g", True),
    ("binary_sensor", "opening", "binary_sensor.o", True),
    ("binary_sensor", "motion", "binary_sensor.m", False),
    ("light", None, "light.l", False)])
def test_is_relevant(cc, fake_hass, dom, dc, eid, expected):
    assert _mgr(cc, fake_hass)._is_relevant(dom, dc, eid) is expected


def test_open_openings_lists_every_open_door_window_and_cover(cc, fake_hass):
    fake_hass.states.set("binary_sensor.d", "on", device_class="door")
    fake_hass.states.set("binary_sensor.w", "on", device_class="window")
    fake_hass.states.set("binary_sensor.shut", "off", device_class="door")
    fake_hass.states.set("binary_sensor.m", "on", device_class="motion")
    fake_hass.states.set("cover.g", "open", device_class="garage")
    fake_hass.states.set("cover.o", "opening", device_class="gate")
    fake_hass.states.set("cover.blind", "open", device_class="blind")
    assert _mgr(cc, fake_hass)._open_openings() == {
        "binary_sensor.d", "binary_sensor.w", "cover.g", "cover.o"}


@pytest.mark.parametrize("state,expected", [
    ("home", True), ("not_home", False)])
def test_anyone_home_by_person_or_tracker(cc, fake_hass, state, expected):
    mgr = _mgr(cc, fake_hass)
    fake_hass.states.set("person.a", state)
    assert mgr._anyone_home() is expected
    fake_hass.states.remove("person.a")
    fake_hass.states.set("device_tracker.phone", state)
    assert mgr._anyone_home() is expected


@pytest.mark.parametrize("dc,state,expected", [
    ("occupancy", "on", True), ("motion", "detected", True), ("presence", "occupied", True),
    ("presence", "home", True), ("motion", "true", True), ("motion", "ON", True),
    ("motion", "off", False), ("door", "on", False), (None, "on", False)])
def test_anyone_home_also_counts_live_occupancy(cc, fake_hass, dc, state, expected):
    attrs = {"device_class": dc} if dc else {}
    fake_hass.states.set("binary_sensor.s", state, **attrs)
    assert _mgr(cc, fake_hass)._anyone_home() is expected


def test_nobody_anywhere_is_not_home(cc, fake_hass):
    assert _mgr(cc, fake_hass)._anyone_home() is False


async def test_secure_entity_sends_the_right_service_and_reports_failure(cc, fake_hass):
    mgr = _mgr(cc, fake_hass)
    assert await mgr._secure_entity("lock.a", "lock") is True
    assert await mgr._secure_entity("cover.a", "cover") is True
    assert fake_hass.service_calls == [("lock", "lock", {"entity_id": "lock.a"}),
                                       ("cover", "close_cover", {"entity_id": "cover.a"})]

    async def boom(*a, **k):
        raise RuntimeError("offline")
    fake_hass.services.async_call = boom
    assert await mgr._secure_entity("lock.a", "lock") is False


# ── _lock_all ───────────────────────────────────────────────────────────────

async def test_lock_all_logs_one_request_and_marks_each_outcome(cc, fake_hass, al):
    fake_hass.states.set("lock.a", "unlocked", friendly_name="A")
    fake_hass.states.set("lock.b", "unlocked", friendly_name="B")
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        if data["entity_id"] == "lock.b":
            raise RuntimeError("jammed")
        await real(domain, service, data, blocking=blocking, **kw)
    fake_hass.services.async_call = call
    mgr = _mgr(cc, fake_hass)
    assert await mgr._lock_all(request_id="req-1") == [("lock.a", "A")]
    (request,) = al.page_requests()["requests"]
    assert request["action"] == "lockdown_engage" and request["source"] == "safety"
    assert {t["entity_id"]: t["execution_result"] for t in request["targets"]} == {
        "lock.a": "accepted", "lock.b": "failed"}


async def test_lock_all_without_a_request_id_writes_no_audit_rows(cc, fake_hass, al):
    fake_hass.states.set("lock.a", "unlocked")
    assert await _mgr(cc, fake_hass)._lock_all() == [("lock.a", "lock.a")]
    assert al.page_requests()["requests"] == []


async def test_lock_all_stops_at_once_when_the_automatic_setting_changes(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    fake_hass.states.set("lock.b", "unlocked")
    mgr = _mgr(cc, fake_hass, lockdown_auto_on_arm=True)
    stale = mgr._automatic_generation
    mgr.set_automatic_lockdown(True)
    assert await mgr._lock_all(automatic_generation=stale) == []
    assert fake_hass.service_calls == []


# ── engage ──────────────────────────────────────────────────────────────────

async def test_engage_records_what_started_it_and_persists_it(cc, fake_hass, clock):
    mgr = _mgr(cc, fake_hass)
    action = await mgr.engage("because", auto=False)
    assert (mgr.active, mgr.reason, mgr.auto, mgr.since) == (True, "because", False, clock["now"])
    assert action["type"] == "lockdown_engaged" and action["urgency"] == "high"
    assert action["auto_act"] is True
    assert _saved(cc) == {"active": True, "since": clock["now"], "reason": "because",
                          "auto": False, "exempt_windows": [], "auto_suppressed": False}


async def test_a_second_engage_while_active_changes_nothing(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("first")
    fake_hass.close_pending()
    fake_hass.service_calls.clear()
    assert await mgr.engage("second", auto=True) is None
    assert mgr.reason == "first" and mgr.auto is False
    assert fake_hass.service_calls == []


async def test_an_automatic_engage_needs_the_opt_in_and_does_nothing_without_it(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    mgr = _mgr(cc, fake_hass)                                       # lockdown_auto_on_arm not set
    assert await mgr.engage("alarm armed", auto=True) is None
    assert mgr.active is False and fake_hass.service_calls == []


async def test_a_manual_engage_does_not_need_the_opt_in(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    mgr = _mgr(cc, fake_hass)
    assert (await mgr.engage("button")) is not None
    fake_hass.close_pending()
    assert len(service_calls(fake_hass, "lock", "lock")) == 1


async def test_only_closed_covers_are_remembered_as_secured_by_nova_not_locks(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x")
    fake_hass.close_pending()
    assert mgr._secured_by_us == {"cover.garage"}


async def test_a_cover_that_cannot_be_closed_is_left_open_and_named_in_the_message(
        cc, fake_hass, al):
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        if domain == "cover":
            raise RuntimeError("obstruction")
        await real(domain, service, data, blocking=blocking, **kw)
    fake_hass.services.async_call = call
    mgr = _mgr(cc, fake_hass)
    action = await mgr.engage("x")
    fake_hass.close_pending()
    assert mgr.exempt_windows == {"cover.garage"}                   # adopted, so it is not nagged about again
    assert "Garage" in action["message"]
    assert "cover.garage" not in mgr._secured_by_us
    (request,) = al.page_requests()["requests"]
    assert request["targets"][0]["execution_result"] == "failed"


def _fail_locks(fake_hass, *entities):
    """Make the lock command raise for the given locks (others still work)."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        if domain == "lock" and (data or {}).get("entity_id") in entities:
            raise RuntimeError("lock offline")
        await real(domain, service, data, blocking=blocking, **kw)
    fake_hass.services.async_call = call


async def test_a_lock_that_fails_to_lock_is_named_and_never_called_already_secured(
        cc, fake_hass, monkeypatch):
    """Before 8.7.16 a failing lock command was only logged, so with nothing
    else to do the announcement said the home was already fully secured."""
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    _fail_locks(fake_hass, "lock.front")
    mgr = _mgr(cc, fake_hass)
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    action = await mgr.engage("x")
    fake_hass.close_pending()
    i18n = cc._notify_i18n()
    assert mgr.active is True
    assert action["message"] == i18n.message(
        "lockdown_failed_only", "en", honorific="Sir",
        gap=i18n.message("lockdown_secure_failed", "en", names="Front"))
    assert "already fully secured" not in action["message"]
    assert "Front" in action["message"] and action["urgency"] == "high"


async def test_a_failed_lock_is_reported_alongside_the_ones_that_were_sent(
        cc, fake_hass, monkeypatch):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    _fail_locks(fake_hass, "lock.back")
    mgr = _mgr(cc, fake_hass)
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    action = await mgr.engage("x")
    fake_hass.close_pending()
    i18n = cc._notify_i18n()
    did = i18n.join_names([i18n.message("lockdown_lock_pending", "en", names="Front"),
                           i18n.message("lockdown_close_pending", "en", names="Garage")], "en")
    assert action["message"] == i18n.message(
        "lockdown_did_gap_pending", "en", honorific="Sir", did=did,
        gap=i18n.message("lockdown_secure_failed", "en", names="Back"))


async def test_a_failed_lock_and_an_open_window_are_both_named(cc, fake_hass, monkeypatch):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("binary_sensor.window", "on", device_class="window", friendly_name="Window")
    _fail_locks(fake_hass, "lock.front")
    mgr = _mgr(cc, fake_hass)
    action = await mgr.engage("x")
    fake_hass.close_pending()
    assert "Front" in action["message"] and "Window" in action["message"]
    assert "already" not in action["message"].lower()


async def test_a_failed_lock_gets_the_same_background_check_as_the_ones_that_were_sent(
        cc, fake_hass, monkeypatch, slept, emitted):
    """If the lock is still unlocked after the check, it is raised as a
    critical alert, the way an unsecured cover already is."""
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    _fail_locks(fake_hass, "lock.front")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x")
    assert len(fake_hass._tasks) == 1
    await fake_hass.drain()
    assert [a["type"] for a, _ in emitted] == ["lockdown_breach"]
    assert emitted[0][0]["urgency"] == "critical" and "Front" in emitted[0][0]["message"]


async def test_already_fully_secured_is_only_said_when_nothing_failed_and_nothing_needed_doing(
        cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("lock.front", "locked")
    action = await _mgr(cc, fake_hass).engage("x")
    assert action["message"] == cc._notify_i18n().message(
        "lockdown_already_secured", "en", honorific="Sir")


def test_the_lockdown_message_builder_never_says_already_secured_with_a_failure(cc):
    for kwargs in ({}, {"closed": ["Garage"]}, {"locked": ["A"]}, {"open_names": ["Window"]}):
        msg = cc.build_lockdown_message(
            "sir", kwargs.get("locked", []), kwargs.get("closed", []), kwargs.get("open_names", []),
            failed=["Front"])
        assert "Front" in msg and "already" not in msg.lower()


@pytest.mark.parametrize("lang", ["en", "fr", "de", "es", "it", "nl", "pt"])
def test_every_language_has_the_failure_wording(cc, lang):
    msg = cc.build_lockdown_message("sir", ["A"], [], [], lang=lang, failed=["Front"])
    assert "Front" in msg and "{" not in msg
    only = cc.build_lockdown_message("sir", [], [], [], lang=lang, failed=["Front"])
    assert "Front" in only and "{" not in only
    assert only != cc.build_lockdown_message("sir", [], [], [], lang=lang)
    i18n = cc._notify_i18n()
    for key in ("lockdown_secure_failed", "lockdown_failed_only",
                "lockdown_nighttime_partial", "lockdown_nighttime_failed_only"):
        assert lang in i18n.MESSAGES[key]


async def test_silent_engage_still_acts_and_persists(cc, fake_hass):
    fake_hass.states.set("lock.a", "unlocked")
    mgr = _mgr(cc, fake_hass)
    assert await mgr.engage("startup", announce=False) is None
    fake_hass.close_pending()
    assert mgr.active is True and _saved(cc)["active"] is True
    assert len(service_calls(fake_hass, "lock", "lock")) == 1


async def test_the_state_file_keeps_the_openings_left_as_they_were(cc, fake_hass):
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x")
    assert _saved(cc)["exempt_windows"] == ["binary_sensor.window"]


# ── disengage ───────────────────────────────────────────────────────────────

async def test_disengage_when_not_active_is_a_no_op(cc, fake_hass):
    assert await _mgr(cc, fake_hass).disengage("x") is None


async def test_disengage_clears_everything_and_says_it_is_lifted(cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x", auto=False)
    action = await mgr.disengage("done")
    assert (mgr.active, mgr.reason, mgr.auto, mgr.exempt_windows) == (False, "", False, set())
    assert mgr._secured_by_us == set() and mgr._alerted == set()
    assert action == {"type": "lockdown_disengaged", "urgency": "low", "auto_act": True,
                      "message": cc._notify_i18n().message("lockdown_lifted", "en", honorific="Sir")}
    assert _saved(cc)["active"] is False
    assert fake_hass.service_calls == []                            # lifting unlocks nothing


@pytest.mark.parametrize("alarm,manual,suppressed", [
    ("armed_away", True, True),      # a manual lift while still armed: do not re-engage on the next tick
    ("armed_home", True, True),
    ("disarmed", True, False),
    ("armed_away", False, False),    # an automatic lift never sets it
])
async def test_a_manual_lift_while_the_alarm_is_armed_suppresses_re_engaging(
        cc, fake_hass, alarm, manual, suppressed):
    fake_hass.states.set(ALARM, alarm)
    mgr = _mgr(cc, fake_hass, security_alarm_entity=ALARM)
    await mgr.engage("x")
    await mgr.disengage("lift", manual=manual)
    assert mgr._auto_suppressed is suppressed
    assert _saved(cc)["auto_suppressed"] is suppressed


# ── tick: the alarm drives automatic lockdown ───────────────────────────────

def _auto(cc, hass, **cfg):
    cfg.update(lockdown_auto_on_arm=True, security_alarm_entity=ALARM)
    return _mgr(cc, hass, **cfg)


async def test_tick_engages_when_armed_and_lifts_when_disarmed(cc, fake_hass):
    mgr = _auto(cc, fake_hass)
    fake_hass.states.set(ALARM, "armed_night")
    (engaged,) = await mgr.tick()
    fake_hass.close_pending()
    assert engaged["type"] == "lockdown_engaged"
    assert (mgr.active, mgr.auto, mgr.reason) == (True, True, "alarm armed")
    assert await mgr.tick() == []                                   # armed and engaged: nothing more to do
    fake_hass.states.set(ALARM, "disarmed")
    (lifted,) = await mgr.tick()
    assert lifted["type"] == "lockdown_disengaged" and mgr.active is False


@pytest.mark.parametrize("alarm", [
    "armed_home", "armed_away", "armed_night", "armed_vacation", "armed_custom_bypass"])
async def test_every_armed_state_engages(cc, fake_hass, alarm):
    mgr = _auto(cc, fake_hass)
    fake_hass.states.set(ALARM, alarm)
    assert len(await mgr.tick()) == 1
    fake_hass.close_pending()


async def test_tick_without_the_opt_in_never_engages_but_still_lifts_what_the_alarm_owned(
        cc, fake_hass):
    fake_hass.states.set(ALARM, "armed_away")
    mgr = _mgr(cc, fake_hass, security_alarm_entity=ALARM)
    assert await mgr.tick() == []
    assert mgr.active is False
    mgr.active, mgr.auto = True, True                               # an alarm owned lockdown that exists anyway
    fake_hass.states.set(ALARM, "disarmed")
    assert [a["type"] for a in await mgr.tick()] == ["lockdown_disengaged"]


async def test_a_manual_lockdown_is_never_lifted_by_a_disarm(cc, fake_hass):
    mgr = _auto(cc, fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    await mgr.engage("button", auto=False)
    assert await mgr.tick() == []
    assert mgr.active is True


async def test_after_a_manual_lift_the_alarm_must_be_disarmed_before_it_can_engage_again(
        cc, fake_hass):
    mgr = _auto(cc, fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    await mgr.tick()
    fake_hass.close_pending()
    await mgr.disengage("user lifted it", manual=True)
    assert await mgr.tick() == [] and mgr.active is False           # still armed: stays lifted
    assert mgr._auto_suppressed is True
    fake_hass.states.set(ALARM, "unavailable")
    await mgr.tick()
    assert mgr._auto_suppressed is True                             # a dropout does not clear it
    fake_hass.states.set(ALARM, "disarmed")
    await mgr.tick()
    assert mgr._auto_suppressed is False and _saved(cc)["auto_suppressed"] is False
    fake_hass.states.set(ALARM, "armed_away")
    assert len(await mgr.tick()) == 1                               # a fresh arm engages again
    fake_hass.close_pending()


async def test_tick_holds_an_automatic_lockdown_while_the_alarm_is_unreadable(cc, fake_hass):
    mgr = _auto(cc, fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    await mgr.tick()
    fake_hass.close_pending()
    for state in ("unavailable", "unknown"):
        fake_hass.states.set(ALARM, state)
        assert await mgr.tick() == []
        assert mgr.active is True
    fake_hass.states.remove(ALARM)                                  # the panel disappears entirely
    assert await mgr.tick() == [] and mgr.active is True


# ── handle_state_change ─────────────────────────────────────────────────────

async def _active(cc, hass, **cfg):
    mgr = _mgr(cc, hass, **cfg)
    await mgr.engage("x")
    hass.close_pending()
    hass.service_calls.clear()
    return mgr


async def test_nothing_happens_when_lockdown_is_not_active(cc, fake_hass):
    mgr = _mgr(cc, fake_hass)
    new = _st("lock.front", "unlocked")
    assert await mgr.handle_state_change("lock.front", _st("lock.front", "locked"), new) is None
    assert fake_hass.service_calls == []


@pytest.mark.parametrize("eid,old,new,attrs", [
    ("light.l", "off", "on", {}),                                   # not a security entity
    ("lock.downstairs_thermo_lock", "locked", "unlocked", {}),      # exempt lock
    ("binary_sensor.m", "off", "on", {"device_class": "motion"}),
    ("cover.blind", "closed", "open", {"device_class": "blind"}),
    ("lock.front", "unlocked", "locked", {}),                       # became secure
    ("lock.front", "unlocked", "jammed", {}),                       # was already unsecure: not a fresh transition
    ("cover.garage", "open", "opening", {"device_class": "garage"}),
    ("cover.garage", "closed", "closing", {"device_class": "garage"}),   # secure to secure
])
async def test_these_changes_are_ignored(cc, fake_hass, eid, old, new, attrs):
    mgr = await _active(cc, fake_hass)
    assert await mgr.handle_state_change(eid, _st(eid, old, **attrs), _st(eid, new, **attrs)) is None
    assert fake_hass.service_calls == []


async def test_something_already_open_at_engage_is_left_alone(cc, fake_hass):
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x")
    new = _st("binary_sensor.window", "on", device_class="window")
    assert await mgr.handle_state_change("binary_sensor.window", _st("binary_sensor.window", "off"), new) is None
    assert fake_hass.service_calls == []


async def test_a_state_change_with_no_old_state_counts_as_a_fresh_transition(cc, fake_hass):
    mgr = await _active(cc, fake_hass)
    fake_hass.states.set("lock.front", "unlocked")
    assert await mgr.handle_state_change("lock.front", None, _st("lock.front", "unlocked")) is None
    fake_hass.close_pending()
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": "lock.front"})]


async def test_a_bare_contact_that_opens_is_assumed_intentional_and_remembered(cc, fake_hass):
    mgr = await _active(cc, fake_hass)
    eid = "binary_sensor.window"
    new = _st(eid, "on", device_class="window")
    assert await mgr.handle_state_change(eid, _st(eid, "off"), new) is None
    assert eid in mgr.exempt_windows and eid in _saved(cc)["exempt_windows"]
    assert fake_hass.service_calls == []                            # Nova cannot close a sensor
    assert await mgr.handle_state_change(eid, _st(eid, "off"), new) is None   # and it is never re-evaluated


async def test_an_unlocked_lock_is_locked_again_and_the_attempt_is_audited(
        cc, fake_hass, al):
    mgr = await _active(cc, fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    new = _st("lock.front", "unlocked", friendly_name="Front")
    assert await mgr.handle_state_change("lock.front", _st("lock.front", "locked"), new) is None
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": "lock.front"})]
    assert "lock.front" in mgr._secured_by_us
    assert len(fake_hass._tasks) == 1                               # the background verify was scheduled
    fake_hass.close_pending()
    (request,) = al.page_requests()["requests"]
    assert request["action"] == "lockdown_breach_resecure"
    assert request["targets"][0]["execution_result"] == "accepted"
    assert request["targets"][0]["service"] == "lock"


async def test_a_reopened_closeable_cover_is_closed_again(cc, fake_hass):
    mgr = await _active(cc, fake_hass)
    new = _st("cover.garage", "open", device_class="garage")
    assert await mgr.handle_state_change("cover.garage", _st("cover.garage", "closed"), new) is None
    assert service_calls(fake_hass, "cover", "close_cover") == [
        ("cover", "close_cover", {"entity_id": "cover.garage"})]
    fake_hass.close_pending()


async def test_a_breach_that_cannot_be_secured_raises_one_critical_alert(cc, fake_hass, al, monkeypatch):
    mgr = await _active(cc, fake_hass)
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")

    async def boom(*a, **k):
        raise RuntimeError("offline")
    fake_hass.services.async_call = boom
    new = _st("lock.front", "unlocked", friendly_name="Front")
    alert = await mgr.handle_state_change("lock.front", _st("lock.front", "locked"), new)
    assert alert == {"type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                     "message": "Sir, Front opened during lockdown and I couldn't secure it."}
    assert "lock.front" not in mgr._secured_by_us
    assert await mgr.handle_state_change("lock.front", _st("lock.front", "locked"), new) is None   # once only
    requests = al.page_requests()["requests"]
    assert len(requests) == 2                                       # but every attempt is audited
    assert {r["targets"][0]["execution_result"] for r in requests} == {"failed"}


async def test_a_cover_that_nova_closed_and_that_is_opened_again_is_adopted_once(cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    mgr = _mgr(cc, fake_hass)
    await mgr.engage("x")
    fake_hass.close_pending()
    fake_hass.service_calls.clear()
    new = _st("cover.garage", "open", device_class="garage", friendly_name="Garage")
    alert = await mgr.handle_state_change("cover.garage", _st("cover.garage", "closed"), new)
    assert alert == {"type": "lockdown_breach", "urgency": "high", "auto_act": True,
                     "message": "Sir, Garage reopened after I secured it — I'll leave it open."}
    assert "cover.garage" in mgr.exempt_windows and "cover.garage" in _saved(cc)["exempt_windows"]
    assert fake_hass.service_calls == []                            # not closed a second time
    assert await mgr.handle_state_change("cover.garage", _st("cover.garage", "closed"), new) is None


async def test_a_lock_unlocked_a_second_time_is_a_critical_alert_and_never_adopted(
        cc, fake_hass, monkeypatch):
    """Fixed in 8.7.20 (8.7.15 pinned the lock being adopted as "left open").
    The first unlock is locked again; the second is a critical alert, the lock
    is not locked a second time (Nova does not fight a person at the door) and
    it is never made exempt. Once it is locked again, a later unlock alerts
    again."""
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    mgr = await _active(cc, fake_hass)
    new = _st("lock.front", "unlocked", friendly_name="Front")
    old = _st("lock.front", "locked")
    assert await mgr.handle_state_change("lock.front", old, new) is None          # locked again
    fake_hass.close_pending()
    alert = await mgr.handle_state_change("lock.front", old, new)                  # second unlock
    assert alert == {"type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                     "message": "Sir, Front was unlocked again during lockdown after I "
                                "locked it. Please check it."}
    assert "lock.front" not in mgr.exempt_windows
    assert len(service_calls(fake_hass, "lock", "lock")) == 1                      # not locked twice
    assert await mgr.handle_state_change("lock.front", old, new) is None           # alerted once...
    await mgr.handle_state_change("lock.front", new, _st("lock.front", "locked"))  # ...until locked
    assert (await mgr.handle_state_change("lock.front", old, new))["urgency"] == "critical"


# ── _verify_secured (the background check) ──────────────────────────────────

@pytest.fixture
def slept(cc, monkeypatch):
    waited = []

    async def fake_sleep(secs):
        waited.append(secs)
    monkeypatch.setattr(cc.asyncio, "sleep", fake_sleep)
    return waited


async def test_verify_waits_for_slow_covers_then_alerts_if_still_open_and_only_once(
        cc, fake_hass, slept, emitted, monkeypatch):
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    mgr = _mgr(cc, fake_hass)
    mgr.active = True
    await mgr._verify_secured("cover.garage", "cover", "Garage")
    await mgr._verify_secured("cover.garage", "cover", "Garage")
    assert slept == [cc.LOCKDOWN_SECURE_VERIFY_DELAY] * 2 and cc.LOCKDOWN_SECURE_VERIFY_DELAY == 25
    assert emitted == [({"type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                         "message": "Sir, I tried to secure Garage during lockdown but it's still open."},
                        False)]


@pytest.mark.parametrize("state", ["closed", "closing"])
async def test_verify_is_silent_once_the_cover_is_secure(cc, fake_hass, slept, emitted, state):
    fake_hass.states.set("cover.garage", state, device_class="garage")
    mgr = _mgr(cc, fake_hass)
    mgr.active = True
    await mgr._verify_secured("cover.garage", "cover", "Garage")
    assert emitted == []


async def test_verify_is_silent_when_lockdown_ended_the_opening_was_adopted_or_vanished(
        cc, fake_hass, slept, emitted):
    fake_hass.states.set("lock.front", "unlocked")
    mgr = _mgr(cc, fake_hass)
    await mgr._verify_secured("lock.front", "lock", "Front")                       # not active
    mgr.active = True
    mgr.exempt_windows = {"lock.front"}
    await mgr._verify_secured("lock.front", "lock", "Front")                       # adopted as intentional
    mgr.exempt_windows = set()
    fake_hass.states.remove("lock.front")
    await mgr._verify_secured("lock.front", "lock", "Front")                       # entity gone
    assert emitted == []


async def test_verify_never_raises(cc, fake_hass, slept, monkeypatch):
    fake_hass.states.set("lock.front", "unlocked")
    mgr = _mgr(cc, fake_hass)
    mgr.active = True

    async def boom(*a, **k):
        raise RuntimeError("announce broke")
    monkeypatch.setattr(cc, "_emit_action", boom)
    await mgr._verify_secured("lock.front", "lock", "Front")


# ── the state file ──────────────────────────────────────────────────────────

def test_a_missing_state_file_means_a_clean_start(cc, fake_hass):
    mgr = _mgr(cc, fake_hass)
    assert (mgr.active, mgr.since, mgr.reason, mgr.exempt_windows, mgr._auto_suppressed) == (
        False, 0.0, "", set(), False)


@pytest.mark.parametrize("content", ["{not json", "[]", "null", ""])
def test_an_unreadable_state_file_means_a_clean_start_and_a_warning(cc, fake_hass, content, caplog):
    with open(cc._lockdown_state_path(), "w") as f:
        f.write(content)
    with caplog.at_level("WARNING"):
        mgr = _mgr(cc, fake_hass)
    assert (mgr.active, mgr.auto, mgr.exempt_windows, mgr._auto_suppressed) == (False, False, set(), False)
    assert "Lockdown state restore failed" in caplog.text


def test_current_behaviour_a_state_file_that_is_active_but_has_a_bad_field_restores_part_of_it(
        cc, fake_hass, caplog):
    """The fields are applied one by one, so a bad exempt_windows after
    "active": true leaves the lockdown active with the later fields at their
    defaults (it fails toward staying locked down)."""
    with open(cc._lockdown_state_path(), "w") as f:
        json.dump({"active": True, "since": 9.0, "reason": "button", "auto": False,
                   "exempt_windows": 5}, f)
    with caplog.at_level("WARNING"):
        mgr = _mgr(cc, fake_hass)
    assert (mgr.active, mgr.since, mgr.reason, mgr.exempt_windows) == (True, 9.0, "button", set())
    assert "Lockdown state restore failed" in caplog.text


def test_a_good_manual_state_file_restores_the_whole_lockdown(cc, fake_hass):
    with open(cc._lockdown_state_path(), "w") as f:
        json.dump({"active": True, "since": 55.0, "reason": "button", "auto": False,
                   "exempt_windows": ["binary_sensor.w", "cover.g"], "auto_suppressed": True}, f)
    mgr = _mgr(cc, fake_hass)
    assert (mgr.active, mgr.since, mgr.reason, mgr.auto) == (True, 55.0, "button", False)
    assert mgr.exempt_windows == {"binary_sensor.w", "cover.g"} and mgr._auto_suppressed is True


def test_a_state_file_with_missing_keys_gets_sensible_defaults(cc, fake_hass, clock):
    with open(cc._lockdown_state_path(), "w") as f:
        json.dump({"active": True}, f)
    mgr = _mgr(cc, fake_hass)
    assert (mgr.active, mgr.reason, mgr.auto, mgr.exempt_windows) == (True, "restored", False, set())
    assert mgr.since == clock["now"]


def test_an_inactive_file_still_restores_the_manual_lift_suppression(cc, fake_hass):
    with open(cc._lockdown_state_path(), "w") as f:
        json.dump({"active": False, "auto_suppressed": True}, f)
    mgr = _mgr(cc, fake_hass)
    assert mgr.active is False and mgr._auto_suppressed is True


async def test_a_restart_mid_lockdown_restores_it_without_touching_any_device(cc, fake_hass):
    fake_hass.states.set("binary_sensor.window", "on", device_class="window")
    first = _mgr(cc, fake_hass, lockdown_auto_on_arm=True)
    await first.engage("alarm armed", auto=True)
    fake_hass.close_pending()
    fake_hass.service_calls.clear()
    second = _mgr(cc, fake_hass, lockdown_auto_on_arm=True)                         # the restart
    assert (second.active, second.auto, second.reason) == (True, True, "alarm armed")
    assert second.exempt_windows == {"binary_sensor.window"}                        # the window stays adopted
    assert fake_hass.service_calls == []
    assert second._secured_by_us == set()                                           # but what Nova closed is forgotten


async def test_after_a_restart_a_disarm_still_lifts_an_alarm_owned_lockdown(cc, fake_hass):
    fake_hass.states.set(ALARM, "armed_away")
    first = _auto(cc, fake_hass)
    await first.tick()
    fake_hass.close_pending()
    second = _auto(cc, fake_hass)
    assert second.active is True
    assert await second.tick() == []                                                # still armed
    fake_hass.states.set(ALARM, "disarmed")
    assert [a["type"] for a in await second.tick()] == ["lockdown_disengaged"]


async def test_a_state_file_that_cannot_be_written_never_breaks_engage(cc, fake_hass, monkeypatch):
    def boom(*a, **k):
        raise OSError("read only")
    monkeypatch.setattr(cc, "write_json_atomic", boom)
    mgr = _mgr(cc, fake_hass)
    assert (await mgr.engage("x")) is not None and mgr.active is True


def test_status_reports_the_basics(cc, fake_hass):
    mgr = _mgr(cc, fake_hass)
    mgr.active, mgr.since, mgr.reason, mgr.auto = True, 5.0, "r", True
    mgr.exempt_windows = {"a", "b"}
    assert mgr.status() == {"active": True, "since": 5.0, "reason": "r", "auto": True,
                            "exempt_windows": 2}


# ── module level wiring ─────────────────────────────────────────────────────

def test_lockdown_status_and_is_lockdown_without_a_manager(cc):
    assert cc.is_lockdown() is False
    assert cc.lockdown_status() == {"active": False, "since": 0.0, "reason": "",
                                    "auto": False, "exempt_windows": 0}


def test_lockdown_status_comes_from_the_manager(cc, fake_hass):
    mgr = _mgr(cc, fake_hass)
    mgr.active = True
    cc._CORE.lockdown_mgr = mgr
    assert cc.is_lockdown() is True and cc.lockdown_status()["active"] is True


def test_the_nighttime_exempt_lock_source_follows_the_manager(cc, fake_hass):
    assert cc._lockdown_exempt_locks() == cc.LOCKDOWN_EXEMPT_LOCKS_DEFAULT
    cc._CORE.lockdown_mgr = _mgr(cc, fake_hass, lockdown_exempt_locks=["lock.x"])
    assert cc._lockdown_exempt_locks() == {"lock.x"}


async def test_the_manager_is_created_on_demand_and_failure_to_create_returns_none(
        cc, fake_hass, monkeypatch):
    assert cc._ensure_lockdown_mgr(None) is None                                    # nothing to build it from
    mgr = cc._ensure_lockdown_mgr(fake_hass)
    assert mgr is not None and cc._CORE.hass is fake_hass
    assert cc._ensure_lockdown_mgr(None) is mgr                                     # reused
    cc._CORE.lockdown_mgr = None

    def boom(hass, config):
        raise RuntimeError("cannot build")
    monkeypatch.setattr(cc, "LockdownManager", boom)
    assert cc._ensure_lockdown_mgr(fake_hass) is None


async def test_ensure_lockdown_registers_one_listener_and_adopts_an_armed_alarm_silently(
        cc, fake_hass, emitted):
    cc._CORE.config = {"lockdown_auto_on_arm": True, "security_alarm_entity": ALARM}
    fake_hass.states.set(ALARM, "armed_away")
    listened = []
    fake_hass.bus.async_listen = lambda event, handler: listened.append((event, handler)) or (lambda: None)
    await cc.ensure_lockdown(fake_hass, cc._CORE.config)
    await cc.ensure_lockdown(fake_hass, cc._CORE.config)
    fake_hass.close_pending()
    assert [e for e, _ in listened] == ["state_changed"]                            # idempotent
    assert cc.is_lockdown() is True
    assert emitted == []                                                            # a reboot is not announced


async def test_request_lockdown_announces_and_a_manual_lift_while_armed_suppresses(
        cc, fake_hass, emitted):
    cc._CORE.config = {"security_alarm_entity": ALARM}
    fake_hass.states.set(ALARM, "armed_home")
    assert await cc.request_lockdown(True, reason="button", hass=fake_hass) is True
    fake_hass.close_pending()
    assert [a["type"] for a, _ in emitted] == ["lockdown_engaged"]
    assert await cc.request_lockdown(True, reason="again", hass=fake_hass) is True  # already on: no second alert
    assert len(emitted) == 1
    assert await cc.request_lockdown(False, reason="done", hass=fake_hass) is True
    assert [a["type"] for a, _ in emitted] == ["lockdown_engaged", "lockdown_disengaged"]
    assert cc._CORE.lockdown_mgr._auto_suppressed is True


async def test_request_lockdown_tells_the_announcer_whether_the_house_is_asleep(
        cc, fake_hass, emitted, monkeypatch, load):
    sd = load("sleep_detection")
    monkeypatch.setattr(sd, "is_sleeping", lambda hass, **kw: (True, "test"))
    await cc.request_lockdown(True, reason="button", hass=fake_hass)
    fake_hass.close_pending()
    assert emitted[0][1] is True
    monkeypatch.setattr(sd, "is_sleeping", lambda hass, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    await cc.request_lockdown(False, reason="done", hass=fake_hass)
    assert emitted[1][1] is False                                                   # a broken sleep check means awake


@pytest.mark.parametrize("old_state,announces", [
    ("disarmed", True), ("unknown", False), ("unavailable", False), ("none", False), ("", False)])
async def test_only_a_real_arm_is_announced_not_an_entity_coming_back_up(
        cc, fake_hass, emitted, old_state, announces):
    cc._CORE.hass = fake_hass
    cc._CORE.config = {"lockdown_auto_on_arm": True, "security_alarm_entity": ALARM}
    cc._CORE.lockdown_mgr = _mgr(cc, fake_hass, lockdown_auto_on_arm=True, security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    await cc._on_lockdown_state(_ev(_st(ALARM, old_state), _st(ALARM, "armed_away")))
    fake_hass.close_pending()
    assert cc.is_lockdown() is True
    assert (len(emitted) == 1) is announces


async def test_an_alarm_event_with_no_old_state_is_adopted_silently(cc, fake_hass, emitted):
    cc._CORE.hass = fake_hass
    cc._CORE.config = {"lockdown_auto_on_arm": True, "security_alarm_entity": ALARM}
    cc._CORE.lockdown_mgr = _mgr(cc, fake_hass, lockdown_auto_on_arm=True, security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    await cc._on_lockdown_state(_ev(None, _st(ALARM, "armed_away")))
    fake_hass.close_pending()
    assert cc.is_lockdown() is True and emitted == []


async def test_door_events_are_routed_to_the_manager_only_while_active(
        cc, fake_hass, emitted, monkeypatch):
    cc._CORE.hass = fake_hass
    mgr = _mgr(cc, fake_hass)
    cc._CORE.lockdown_mgr = mgr
    handled = []

    async def spy(eid, old, new):
        handled.append(eid)
        return {"type": "lockdown_breach", "urgency": "high", "message": "m", "auto_act": True}
    monkeypatch.setattr(mgr, "handle_state_change", spy)
    event = _ev(_st("lock.front", "locked"), _st("lock.front", "unlocked"))
    await cc._on_lockdown_state(event)                                              # not active
    assert handled == []
    mgr.active = True
    await cc._on_lockdown_state(event)
    await cc._on_lockdown_state(_ev(_st("light.x", "off"), _st("light.x", "on")))   # not a security domain
    await cc._on_lockdown_state(types.SimpleNamespace(data={"entity_id": "lock.front", "old_state": None,
                                                            "new_state": None}))   # a removed entity
    assert handled == ["lock.front"]
    assert [a["type"] for a, _ in emitted] == ["lockdown_breach"] and emitted[0][1] is False


async def test_the_state_listener_never_raises(cc, fake_hass):
    await cc._on_lockdown_state(types.SimpleNamespace(data=None))


async def test_sync_does_nothing_without_the_opt_in_or_a_manager(cc, fake_hass, emitted):
    await cc._sync_lockdown_to_alarm("x")                                           # no manager at all
    cc._CORE.hass = fake_hass
    cc._CORE.lockdown_mgr = _mgr(cc, fake_hass, security_alarm_entity=ALARM)
    cc._CORE.config = {"security_alarm_entity": ALARM}                              # opt in not set
    fake_hass.states.set(ALARM, "armed_away")
    await cc._sync_lockdown_to_alarm("x")
    assert cc.is_lockdown() is False and emitted == []


async def test_sync_announce_false_engages_silently_and_a_failing_announcer_is_swallowed(
        cc, fake_hass, emitted, monkeypatch):
    cc._CORE.hass = fake_hass
    cc._CORE.config = {"lockdown_auto_on_arm": True, "security_alarm_entity": ALARM}
    cc._CORE.lockdown_mgr = _mgr(cc, fake_hass, lockdown_auto_on_arm=True, security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    await cc._sync_lockdown_to_alarm("startup", announce=False)
    assert cc.is_lockdown() is True and emitted == []

    fake_hass.states.set(ALARM, "disarmed")

    async def boom(*a, **k):
        raise RuntimeError("announce broke")
    monkeypatch.setattr(cc, "_emit_action", boom)
    await cc._sync_lockdown_to_alarm("alarm")                                       # must not raise
    assert cc.is_lockdown() is False


async def test_apply_runtime_config_keeps_manual_lockdown_when_auto_is_switched_off(cc, fake_hass):
    mgr = _mgr(cc, fake_hass, lockdown_auto_on_arm=True)
    await mgr.engage("button", auto=False)
    cc._CORE.lockdown_mgr = mgr
    cc._CORE.hass = fake_hass
    cc._CORE.config = {"lockdown_auto_on_arm": True}
    await cc.apply_runtime_config("lockdown_auto_on_arm", False)
    assert mgr.active is True                                                       # only alarm owned state is cleared


async def test_apply_runtime_config_ignores_unknown_keys_and_pushes_the_alarm_choice(cc, fake_hass):
    safety = cc.SafetyManager(fake_hass, {})
    mgr = _mgr(cc, fake_hass)
    cc._CORE.safety_mgr, cc._CORE.lockdown_mgr = safety, mgr
    cc._CORE.config = {}
    await cc.apply_runtime_config("something_else", 1)
    assert "something_else" not in cc._CORE.config
    await cc.apply_runtime_config("security_alarm_entity", ALARM)
    assert safety.config["security_alarm_entity"] == ALARM == mgr.config["security_alarm_entity"]
    await cc.apply_runtime_config("intrusion_requires_confinement", "true")         # not a real boolean
    assert safety.config["intrusion_requires_confinement"] is False
    await cc.apply_runtime_config("face_stand_down", True)
    assert safety.config["face_stand_down"] is True and mgr.config["face_stand_down"] is True
