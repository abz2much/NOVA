"""ProactiveManager (the comfort and efficiency offers), AutonomyManager (earned
trust), the pending offer API, the ignore rules and the small public API of
cognitive_core.

Characterisation tests (8.7.15). None of this existed in the unit tests before:
ProactiveManager and AutonomyManager were at 0% coverage. test_current_behaviour_*
tests pin something odd that is not being changed here (see "Found, not fixed"
in the PR).
"""
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)
from fakes import FakeEntityRegistry, FakeRegistryEntry

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def audit_db(load, tmp_path, monkeypatch):
    monkeypatch.setattr(load("action_log"), "_DEFAULT_DB", str(tmp_path / "audit.db"))


@pytest.fixture
def registry(monkeypatch):
    """The entity registry and area registry ProactiveManager reads. Add an
    entity with registry.add(...) and an area name with registry.areas[...]."""
    er = sys.modules["homeassistant.helpers.entity_registry"]
    ar = sys.modules["homeassistant.helpers.area_registry"]
    reg = FakeEntityRegistry()
    reg.areas = {}
    monkeypatch.setattr(er, "async_get", lambda hass: reg)
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_get_area=lambda area_id: (types.SimpleNamespace(name=reg.areas[area_id])
                                        if area_id in reg.areas else None)))
    return reg


@pytest.fixture
def proactive(cc, fake_hass, monkeypatch):
    monkeypatch.setattr(cc.dt_util, "utcnow", lambda: NOW)
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    return cc.ProactiveManager(fake_hass, {})


def _room(registry, hass, area="lounge", *, lux=5, lit=False, presence=True, name="Lounge"):
    """A dark room with one lux sensor, one light and (optionally) presence."""
    registry.areas[area] = name
    hass.states.set(f"sensor.{area}_lux", str(lux), device_class="illuminance")
    registry.add(FakeRegistryEntry(f"sensor.{area}_lux", "x", area_id=area))
    hass.states.set(f"light.{area}", "on" if lit else "off")
    registry.add(FakeRegistryEntry(f"light.{area}", "x", area_id=area))
    hass.states.set(f"binary_sensor.{area}_motion", "on" if presence else "off", device_class="motion")
    registry.add(FakeRegistryEntry(f"binary_sensor.{area}_motion", "x", area_id=area))


# ── ProactiveManager.tick ───────────────────────────────────────────────────

async def test_tick_runs_at_most_every_two_minutes(proactive, clock, registry, fake_hass):
    _room(registry, fake_hass)
    assert len(await proactive.tick(False, True)) == 1
    clock["now"] += 119
    assert await proactive.tick(False, True) == []
    clock["now"] += 1
    assert len(await proactive.tick(False, True)) == 1


async def test_tick_is_silent_while_asleep_but_still_spends_the_interval(
        proactive, clock, registry, fake_hass):
    _room(registry, fake_hass)
    assert await proactive.tick(True, True) == []
    assert await proactive.tick(False, True) == []                                    # the same two minutes still apply
    clock["now"] += 120
    assert len(await proactive.tick(False, True)) == 1


async def test_tick_returns_offers_in_dark_stale_hvac_order(
        proactive, clock, registry, fake_hass):
    _room(registry, fake_hass)
    fake_hass.states.set("light.hall", "on", last_changed=NOW - timedelta(minutes=120))
    registry.add(FakeRegistryEntry("light.hall", "x", area_id="hall"))
    fake_hass.states.set("climate.main", "heat", hvac_action="heating")
    offers = await proactive.tick(False, True)
    assert [o["type"] for o in offers] == ["proactive_lights", "proactive_stale_light"]
    clock["now"] += 120
    fake_hass.states.set("person.username", "not_home")
    offers = await proactive.tick(False, False)                                       # empty house: only the hvac check applies
    assert [o["type"] for o in offers] == ["proactive_stale_light", "proactive_hvac"]


async def test_one_failing_check_does_not_take_the_others_down(
        proactive, clock, registry, fake_hass, monkeypatch):
    fake_hass.states.set("climate.main", "heat", hvac_action="cooling")

    async def boom(anyone_home):
        raise RuntimeError("check broke")
    monkeypatch.setattr(proactive, "_check_dark_occupied_room", boom)
    monkeypatch.setattr(proactive, "_check_stale_lights", boom)
    offers = await proactive.tick(False, False)
    assert [o["type"] for o in offers] == ["proactive_hvac"]


# ── the dark room offer ─────────────────────────────────────────────────────

async def test_a_dark_occupied_room_with_its_lights_off_gets_an_offer(
        proactive, registry, fake_hass):
    _room(registry, fake_hass, "lounge", lux=5)
    offer = await proactive._check_dark_occupied_room(True)
    assert offer == {
        "type": "proactive_lights", "urgency": "low", "offer": True, "offer_key": "dark:lounge",
        "message": "Sir, it's quite dark in the Lounge and someone's in there. Shall I turn the lights on?",
        "action_data": {"domain": "light", "service": "turn_on", "entity_ids": ["light.lounge"]},
        "pattern_key": "lights_on_when_dark:lounge"}


@pytest.mark.parametrize("lux,expected", [(15, True), (15.5, False), (0, True), ("dark", False)])
async def test_the_dark_threshold_is_15_lux_inclusive(proactive, registry, fake_hass, lux, expected):
    _room(registry, fake_hass, lux=lux)
    assert (await proactive._check_dark_occupied_room(True) is not None) is expected


async def test_nobody_home_means_no_dark_room_offer(proactive, registry, fake_hass):
    _room(registry, fake_hass)
    assert await proactive._check_dark_occupied_room(False) is None


@pytest.mark.parametrize("kwargs", [{"presence": False}, {"lit": True}])
async def test_no_offer_for_an_empty_room_or_one_that_is_already_lit(
        proactive, registry, fake_hass, kwargs):
    _room(registry, fake_hass, **kwargs)
    assert await proactive._check_dark_occupied_room(True) is None


async def test_no_offer_when_the_sensor_has_no_area_or_the_room_has_no_lights(
        proactive, registry, fake_hass):
    fake_hass.states.set("sensor.x_lux", "3", device_class="illuminance")
    assert await proactive._check_dark_occupied_room(True) is None                    # not in the registry
    registry.add(FakeRegistryEntry("sensor.x_lux", "x", area_id=None))
    assert await proactive._check_dark_occupied_room(True) is None                    # no area
    registry.add(FakeRegistryEntry("sensor.x_lux", "x", area_id="study"))
    fake_hass.states.set("binary_sensor.study_motion", "on", device_class="motion")
    registry.add(FakeRegistryEntry("binary_sensor.study_motion", "x", area_id="study"))
    assert await proactive._check_dark_occupied_room(True) is None                    # presence, but no lights


async def test_only_illuminance_sensors_count_as_lux(proactive, registry, fake_hass):
    _room(registry, fake_hass)
    fake_hass.states.set("sensor.lounge_lux", "5", device_class="humidity")
    assert await proactive._check_dark_occupied_room(True) is None


async def test_the_dark_offer_is_not_repeated_within_30_minutes_of_being_delivered(
        proactive, clock, registry, fake_hass):
    _room(registry, fake_hass)
    offer = await proactive._check_dark_occupied_room(True)
    assert await proactive._check_dark_occupied_room(True) is not None                # only delivery starts the cooldown
    proactive._mark_offered(offer["offer_key"])
    assert await proactive._check_dark_occupied_room(True) is None
    clock["now"] += 1799
    assert await proactive._check_dark_occupied_room(True) is None
    clock["now"] += 1
    assert await proactive._check_dark_occupied_room(True) is not None


async def test_current_behaviour_a_cooldown_on_one_dark_room_hides_every_other_dark_room(
        proactive, registry, fake_hass):
    """On a cooldown the check returns None instead of moving on, so a second
    dark, occupied room is not offered until the first room's cooldown ends."""
    _room(registry, fake_hass, "lounge")
    _room(registry, fake_hass, "study", name="Study")
    first = await proactive._check_dark_occupied_room(True)
    assert first["offer_key"] == "dark:lounge"
    proactive._mark_offered("dark:lounge")
    assert await proactive._check_dark_occupied_room(True) is None


async def test_the_area_name_falls_back_to_the_area_id(proactive, registry, fake_hass):
    _room(registry, fake_hass, "back_room")
    del registry.areas["back_room"]
    offer = await proactive._check_dark_occupied_room(True)
    assert "the back_room" in offer["message"]


def test_area_helpers(proactive, registry, fake_hass):
    _room(registry, fake_hass, "lounge")
    _room(registry, fake_hass, "study", presence=False, name="Study")
    assert proactive._area_has_presence("lounge", registry) is True
    assert proactive._area_has_presence("study", registry) is False
    assert proactive._area_has_presence("nowhere", registry) is False
    assert proactive._area_lights("lounge", registry) == ["light.lounge"]
    assert proactive._area_name("lounge") == "Lounge"


def test_area_name_survives_a_broken_registry(proactive, monkeypatch):
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(ar, "async_get", lambda hass: (_ for _ in ()).throw(RuntimeError("x")))
    assert proactive._area_name("lounge") == "lounge"


# ── the stale light offer ───────────────────────────────────────────────────

def _stale(registry, hass, minutes, area="hall", present=False):
    hass.states.set("light.hall", "on", friendly_name="Hall Light",
                    last_changed=NOW - timedelta(minutes=minutes))
    registry.add(FakeRegistryEntry("light.hall", "x", area_id=area))
    registry.areas.setdefault(area, "Hall")
    if present:
        hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
        registry.add(FakeRegistryEntry("binary_sensor.hall_motion", "x", area_id=area))


async def test_a_light_left_on_in_an_empty_room_gets_an_offer(proactive, registry, fake_hass):
    _stale(registry, fake_hass, 100)
    offer = await proactive._check_stale_lights(True)
    assert offer == {
        "type": "proactive_stale_light", "urgency": "low", "offer": True,
        "offer_key": "stale:light.hall",
        "message": "Sir, the Hall Light has been on for 100 minutes in the Hall, which appears empty. Shall I turn it off?",
        "action_data": {"domain": "light", "service": "turn_off", "entity_ids": ["light.hall"]},
        "pattern_key": "lights_off_when_empty:hall"}


@pytest.mark.parametrize("minutes,expected", [(89, False), (90, True)])
async def test_a_light_is_stale_from_90_minutes(proactive, registry, fake_hass, minutes, expected):
    _stale(registry, fake_hass, minutes)
    assert (await proactive._check_stale_lights(True) is not None) is expected


async def test_no_stale_offer_for_an_occupied_room_an_unplaced_light_or_a_light_that_is_off(
        proactive, registry, fake_hass):
    _stale(registry, fake_hass, 200, present=True)
    assert await proactive._check_stale_lights(True) is None
    fake_hass.states.remove("binary_sensor.hall_motion")
    registry.entities.pop("binary_sensor.hall_motion")
    registry.entities["light.hall"].area_id = None
    assert await proactive._check_stale_lights(True) is None
    registry.entities["light.hall"].area_id = "hall"
    fake_hass.states.set("light.hall", "off", last_changed=NOW - timedelta(minutes=200))
    assert await proactive._check_stale_lights(True) is None


async def test_the_stale_offer_does_not_depend_on_anyone_being_home(proactive, registry, fake_hass):
    _stale(registry, fake_hass, 100)
    assert await proactive._check_stale_lights(False) is not None


async def test_the_stale_light_cooldown_starts_when_delivered(proactive, clock, registry, fake_hass):
    _stale(registry, fake_hass, 100)
    proactive._mark_offered("stale:light.hall")
    assert await proactive._check_stale_lights(True) is None
    clock["now"] += 1800
    assert await proactive._check_stale_lights(True) is not None


# ── the HVAC offer ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("action,expected", [
    ("heating", True), ("cooling", True), ("idle", False), ("off", False), (None, False)])
async def test_hvac_offer_only_while_heating_or_cooling(proactive, fake_hass, action, expected):
    attrs = {"hvac_action": action} if action else {}
    fake_hass.states.set("climate.main", "heat", friendly_name="Main", **attrs)
    assert (await proactive._check_hvac_efficiency(False) is not None) is expected


async def test_hvac_offer_shape_and_it_needs_an_empty_house(proactive, fake_hass):
    fake_hass.states.set("climate.main", "heat", hvac_action="cooling", friendly_name="Main")
    assert await proactive._check_hvac_efficiency(True) is None
    assert await proactive._check_hvac_efficiency(False) == {
        "type": "proactive_hvac", "urgency": "low", "offer": True, "offer_key": "hvac:climate.main",
        "message": "Sir, the Main is cooling but no one's home. Would you like me to set it back to save energy?",
        "action_data": {"domain": "climate", "service": "set_preset_mode",
                        "entity_ids": ["climate.main"], "service_data": {"preset_mode": "eco"}},
        "pattern_key": "hvac_eco_when_away"}


async def test_current_behaviour_the_hvac_cooldown_on_one_thermostat_hides_the_others(
        proactive, clock, fake_hass):
    fake_hass.states.set("climate.a", "heat", hvac_action="heating")
    fake_hass.states.set("climate.b", "heat", hvac_action="heating")
    proactive._mark_offered("hvac:climate.a")
    assert await proactive._check_hvac_efficiency(False) is None                       # b is hidden behind a's cooldown
    clock["now"] += 1800
    assert (await proactive._check_hvac_efficiency(False))["offer_key"] == "hvac:climate.a"


# ── AutonomyManager ─────────────────────────────────────────────────────────

@pytest.fixture
def autonomy(cc):
    return cc.AutonomyManager()


def test_three_confident_acceptances_earn_autonomy(cc, autonomy):
    for n in (1, 2):
        grant = autonomy.record_acceptance("lights:lounge", confidence=0.9)
        assert grant["approvals"] == n and grant["granted"] is False
        assert autonomy.is_autonomous("lights:lounge") is False
    grant = autonomy.record_acceptance("lights:lounge", confidence=0.9)
    assert grant["granted"] is True and "granted_at" in grant
    assert autonomy.is_autonomous("lights:lounge") is True
    assert cc.AUTONOMY_TRUST_THRESHOLD == 3 and cc.AUTONOMY_MIN_CONFIDENCE == 0.80


def test_low_confidence_acceptances_never_earn_autonomy_until_confidence_rises(autonomy):
    for _ in range(5):
        grant = autonomy.record_acceptance("k", confidence=0.79)
    assert grant["approvals"] == 5 and grant["granted"] is False
    assert autonomy.record_acceptance("k", confidence=0.8)["granted"] is True        # the best confidence seen counts


def test_confidence_is_the_best_seen_not_the_latest(autonomy):
    autonomy.record_acceptance("k", confidence=0.95)
    assert autonomy.record_acceptance("k", confidence=0.1)["confidence"] == 0.95


def test_an_empty_pattern_key_records_nothing(autonomy):
    assert autonomy.record_acceptance("") == {}
    assert autonomy.list_grants() == []


def test_one_rejection_takes_trust_back_to_zero(autonomy):
    for _ in range(3):
        autonomy.record_acceptance("k", confidence=0.9)
    autonomy.record_rejection("k")
    assert autonomy.is_autonomous("k") is False
    (grant,) = autonomy.list_grants()
    assert grant["approvals"] == 0 and grant["granted"] is False
    autonomy.record_rejection("never_seen")                                           # unknown key: a no-op
    assert len(autonomy.list_grants()) == 1


def test_revoke_and_re_earning(autonomy):
    assert autonomy.revoke("nope") is False
    for _ in range(3):
        autonomy.record_acceptance("k", confidence=0.9)
    assert autonomy.revoke("k") is True and autonomy.is_autonomous("k") is False
    for _ in range(2):
        autonomy.record_acceptance("k", confidence=0.9)
    assert autonomy.is_autonomous("k") is False
    autonomy.record_acceptance("k", confidence=0.9)
    assert autonomy.is_autonomous("k") is True


def test_an_active_mode_can_suppress_auto_actions_but_not_grants(cc, autonomy, load, monkeypatch):
    for _ in range(3):
        autonomy.record_acceptance("k", confidence=0.9)
    modes = load("modes")
    monkeypatch.setattr(modes, "mode_allows_auto_actions", lambda: False)
    assert autonomy.is_autonomous("k") is False
    assert autonomy.list_grants()[0]["granted"] is True                                 # the grant itself is untouched
    monkeypatch.setattr(modes, "mode_allows_auto_actions", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert autonomy.is_autonomous("k") is True                                          # a broken mode check never blocks


def test_unknown_or_ungranted_patterns_are_not_autonomous(autonomy):
    assert autonomy.is_autonomous("never_seen") is False
    autonomy.record_acceptance("k", confidence=0.9)
    assert autonomy.is_autonomous("k") is False


def test_grants_survive_a_restart_and_list_with_the_threshold(cc, autonomy, tmp_path):
    for _ in range(3):
        autonomy.record_acceptance("k", confidence=0.9123)
    reloaded = cc.AutonomyManager()
    assert reloaded.is_autonomous("k") is True
    assert reloaded.list_grants() == [{"pattern_key": "k", "approvals": 3, "granted": True,
                                       "threshold": 3, "confidence": 0.91}]
    assert json.loads((tmp_path / "autonomy_grants.json").read_text())["k"]["granted"] is True


def test_a_corrupt_grants_file_starts_empty_and_a_failed_save_is_swallowed(cc, tmp_path, monkeypatch):
    (tmp_path / "autonomy_grants.json").write_text("{broken")
    mgr = cc.AutonomyManager()
    assert mgr.list_grants() == []
    monkeypatch.setattr(cc, "write_json_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert mgr.record_acceptance("k", confidence=0.9)["approvals"] == 1                 # still tracked in memory


def test_a_grants_file_that_is_json_null_is_treated_as_empty(cc, tmp_path):
    (tmp_path / "autonomy_grants.json").write_text("null")
    assert cc.AutonomyManager().list_grants() == []


# ── the pending offer API ───────────────────────────────────────────────────

def _offer(**over):
    offer = {"type": "proactive_lights", "pattern_key": "lights:lounge", "offer_key": "dark:lounge",
             "action_data": {"domain": "light", "service": "turn_on", "entity_ids": ["light.lounge"]}}
    offer.update(over)
    return offer


async def test_accepting_with_no_pending_offer_does_nothing(cc, fake_hass):
    cc._CORE.hass = fake_hass
    assert await cc.accept_pending_offer() == {"ok": False, "reason": "no pending offer"}
    assert fake_hass.service_calls == []


async def test_accepting_runs_the_action_and_counts_toward_autonomy(cc, fake_hass):
    cc._CORE.hass = fake_hass
    cc._CORE.autonomy_mgr = cc.AutonomyManager()
    results = []
    for _ in range(3):
        cc._CORE.pending_offer = _offer()
        results.append(await cc.accept_pending_offer())
    assert [r["approvals"] for r in results] == [1, 2, 3]
    assert [r["now_autonomous"] for r in results] == [False, False, True]             # 0.9 confidence clears the bar
    assert cc._CORE.pending_offer is None and cc._CORE.actions_taken == 3
    assert len(service_calls(fake_hass, "light", "turn_on")) == 3


async def test_accepting_an_offer_whose_action_fails_counts_nothing(cc, fake_hass):
    cc._CORE.hass = fake_hass
    cc._CORE.autonomy_mgr = cc.AutonomyManager()

    async def boom(*a, **k):
        raise RuntimeError("device offline")
    fake_hass.services.async_call = boom
    cc._CORE.pending_offer = _offer()
    assert await cc.accept_pending_offer() == {"ok": False}
    assert cc._CORE.autonomy_mgr.list_grants() == [] and cc._CORE.actions_taken == 0


async def test_accepting_an_offer_without_a_pattern_key_runs_it_but_earns_nothing(cc, fake_hass):
    cc._CORE.hass = fake_hass
    cc._CORE.autonomy_mgr = cc.AutonomyManager()
    cc._CORE.pending_offer = _offer(pattern_key="")
    assert await cc.accept_pending_offer() == {"ok": True}
    assert cc._CORE.autonomy_mgr.list_grants() == []


async def test_declining_clears_the_offer_and_resets_that_patterns_trust(cc, fake_hass):
    cc._CORE.autonomy_mgr = cc.AutonomyManager()
    for _ in range(3):
        cc._CORE.autonomy_mgr.record_acceptance("lights:lounge", confidence=0.9)
    cc._CORE.pending_offer = _offer()
    assert cc.decline_pending_offer() == {"ok": True}
    assert cc._CORE.pending_offer is None
    assert cc._CORE.autonomy_mgr.is_autonomous("lights:lounge") is False
    assert cc.decline_pending_offer() == {"ok": True}                                   # nothing pending: still fine
    assert cc.get_pending_offer() is None


def test_get_pending_offer_returns_the_current_one(cc):
    cc._CORE.pending_offer = _offer()
    assert cc.get_pending_offer()["offer_key"] == "dark:lounge"


def test_revoke_autonomy_through_the_core(cc):
    assert cc.revoke_autonomy("k") == {"ok": False, "reason": "no such grant"}
    cc._CORE.autonomy_mgr = cc.AutonomyManager()
    cc._CORE.autonomy_mgr.record_acceptance("k", confidence=0.9)
    assert cc.revoke_autonomy("k") == {"ok": True, "pattern_key": "k"}


# ── ignore rules ────────────────────────────────────────────────────────────

def test_ignore_rules_match_globs_expire_and_persist(cc, clock):
    mgr = cc.IgnoreManager()
    mgr.add("binary_sensor.garage*", duration_minutes=10, reason="working in there")
    mgr.add("light.porch", reason="forever")
    assert mgr.is_ignored("binary_sensor.garage_side") and mgr.is_ignored("light.porch")
    assert not mgr.is_ignored("light.other")
    assert [r["remaining_min"] for r in mgr.list_rules()] == [10, "permanent"]
    clock["now"] += 601
    assert not mgr.is_ignored("binary_sensor.garage_side")                              # expired
    assert [r["pattern"] for r in mgr.list_rules()] == ["light.porch"]
    assert [r["pattern"] for r in cc.IgnoreManager().list_rules()] == ["light.porch"]    # saved and reloaded


def test_ignore_remove_clear_and_a_corrupt_file(cc, tmp_path):
    mgr = cc.IgnoreManager()
    mgr.add("a.*")
    mgr.add("b.*")
    assert mgr.remove("a.*") is True and mgr.remove("a.*") is False
    mgr.clear_all()
    assert mgr.list_rules() == []
    (tmp_path / "ignore_rules.json").write_text("{broken")
    assert cc.IgnoreManager().list_rules() == []


def test_the_public_ignore_api_needs_a_running_core(cc):
    assert cc.ignore("light.x", 5)["success"] is False
    assert cc.is_ignored("light.x") is False
    assert cc.list_ignores() == [] or isinstance(cc.list_ignores(), list)
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    out = cc.ignore("light.x", 5, "because")
    assert out == {"success": True, "enforced": True, "pattern": "light.x", "duration": 5, "reason": "because"}
    assert cc.is_ignored("light.x") is True
    assert cc.unignore("light.x")["success"] is True
    assert cc.unignore("light.x")["success"] is False


def test_unignore_without_a_core_reports_not_running(cc, monkeypatch, load):
    monkeypatch.setattr(load("habituation"), "forget", lambda pattern: [])
    assert cc.unignore("light.x") == {"success": False, "error": "Cognitive core not running"}


def test_unignore_also_brings_back_a_habituated_notification(cc, monkeypatch, load):
    monkeypatch.setattr(load("habituation"), "forget", lambda pattern: ["k1", "k2"])
    assert cc.unignore("light.x") == {"success": True, "pattern": "light.x", "restored_notifications": 2}
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    assert cc.unignore("light.x") == {"success": True, "pattern": "light.x", "restored_notifications": 2}


def test_list_ignores_includes_habituated_notifications(cc, monkeypatch, load):
    monkeypatch.setattr(load("habituation"), "quiet_list",
                        lambda: [{"entity_id": "light.k", "key": "k"}, {"entity_id": "", "key": "only_key"}])
    rules = cc.list_ignores()
    assert [r["pattern"] for r in rules] == ["light.k", "only_key"]
    assert rules[0]["remaining_min"] == "normal for this home"


# ── _offer_area ─────────────────────────────────────────────────────────────

def test_offer_area_comes_from_the_action_target(cc, fake_hass, monkeypatch, load):
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "entity_area", lambda hass, eid: {"light.a": "lounge"}.get(eid))
    assert cc._offer_area(fake_hass, {"action_data": {"entity_id": "light.a"}}) == "lounge"
    assert cc._offer_area(fake_hass, {"action_data": {"entity_id": ["light.a", "light.b"]}}) == "lounge"
    assert cc._offer_area(fake_hass, {"entity_id": "light.a"}) == "lounge"             # falls back to the offer's own entity
    assert cc._offer_area(fake_hass, {"action_data": {"entity_id": []}}) is None
    assert cc._offer_area(fake_hass, {}) is None
    assert cc._offer_area(fake_hass, "not a dict") is None


def test_current_behaviour_offer_area_reads_entity_id_not_the_entity_ids_proactive_offers_carry(
        cc, fake_hass, monkeypatch, load):
    """ProactiveManager offers put their targets in action_data["entity_ids"],
    but _offer_area only reads "entity_id", so a room scoped mode never matches
    a real proactive offer and the offer is not dropped for that room."""
    ar = load("audio_routing")
    monkeypatch.setattr(ar, "entity_area", lambda hass, eid: "lounge")
    offer = {"type": "proactive_lights", "action_data": {
        "domain": "light", "service": "turn_on", "entity_ids": ["light.lounge"]}}
    assert cc._offer_area(fake_hass, offer) is None
