"""Only offer actions Nova can actually perform (8.14.0).

Every proactive offer that proposes an action ("Shall I turn the lights on?",
"Would you like me to set it back?", "Want me to hold the dryer?") must first
pass one shared check: the entity is in the right domain, is not unavailable,
Home Assistant has the service, and the entity says it supports the action.
When the check fails, the alert stays but becomes information only, with no
question and nothing to accept."""
import sys
import types
from datetime import datetime, timezone

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)
from fakes import FakeEntityRegistry, FakeRegistryEntry

NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)


@pytest.fixture
def capability(load):
    return load("device_capability")


def _services(hass, *pairs):
    hass.services.has_service = lambda d, s: (d, s) in set(pairs)


# ── the shared check ───────────────────────────────────────────────────────

def test_a_sensor_only_door_cannot_be_closed(capability, fake_hass):
    _services(fake_hass, ("cover", "close_cover"))
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    assert capability.can_perform(fake_hass, "binary_sensor.back_door", "cover", "close_cover") is False


def test_a_cover_with_close_support_can_be_closed(capability, fake_hass):
    _services(fake_hass, ("cover", "close_cover"))
    fake_hass.states.set("cover.garage_door", "open", supported_features=3)   # OPEN | CLOSE
    assert capability.can_perform(fake_hass, "cover.garage_door", "cover", "close_cover") is True


def test_a_cover_without_close_support_cannot_be_closed(capability, fake_hass):
    _services(fake_hass, ("cover", "close_cover"))
    fake_hass.states.set("cover.skylight", "open", supported_features=1)       # OPEN only
    assert capability.can_perform(fake_hass, "cover.skylight", "cover", "close_cover") is False


def test_a_lock_that_is_unavailable_cannot_be_locked(capability, fake_hass):
    _services(fake_hass, ("lock", "lock"))
    fake_hass.states.set("lock.front", "unavailable")
    assert capability.can_perform(fake_hass, "lock.front", "lock", "lock") is False


def test_an_available_lock_can_be_locked_and_unlocked(capability, fake_hass):
    _services(fake_hass, ("lock", "lock"), ("lock", "unlock"))
    fake_hass.states.set("lock.front", "unlocked")
    assert capability.can_perform(fake_hass, "lock.front", "lock", "lock") is True
    assert capability.can_perform(fake_hass, "lock.front", "lock", "unlock") is True


def test_opening_a_lock_needs_open_support(capability, fake_hass):
    _services(fake_hass, ("lock", "open"))
    fake_hass.states.set("lock.front", "locked", supported_features=0)
    assert capability.can_perform(fake_hass, "lock.front", "lock", "open") is False
    fake_hass.states.set("lock.front", "locked", supported_features=1)
    assert capability.can_perform(fake_hass, "lock.front", "lock", "open") is True


def test_a_missing_service_or_entity_cannot_be_used(capability, fake_hass):
    _services(fake_hass)
    fake_hass.states.set("light.lamp", "off")
    assert capability.can_perform(fake_hass, "light.lamp", "light", "turn_on") is False
    _services(fake_hass, ("light", "turn_on"))
    assert capability.can_perform(fake_hass, "light.gone", "light", "turn_on") is False
    assert capability.can_perform(fake_hass, "light.lamp", "light", "turn_on") is True


@pytest.mark.parametrize("features, presets, expected", [
    (16, ["eco", "comfort"], True),
    (16, ["comfort", "away"], False),     # no eco preset
    (0, ["eco"], False),                   # presets not supported
    (16, None, False),
])
def test_a_thermostat_needs_the_eco_preset(capability, fake_hass, features, presets, expected):
    _services(fake_hass, ("climate", "set_preset_mode"))
    attrs = {"supported_features": features}
    if presets is not None:
        attrs["preset_modes"] = presets
    fake_hass.states.set("climate.hall", "heat", **attrs)
    assert capability.can_perform(fake_hass, "climate.hall", "climate", "set_preset_mode",
                                  {"preset_mode": "eco"}) is expected


def test_never_raises(capability):
    assert capability.can_perform(None, "light.x", "light", "turn_on") is False
    assert capability.can_perform(object(), None, "light", "turn_on") is False


# ── the proactive offers use it ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def audit_db(load, tmp_path, monkeypatch):
    monkeypatch.setattr(load("action_log"), "_DEFAULT_DB", str(tmp_path / "audit.db"))


@pytest.fixture
def registry(monkeypatch):
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
    _services(fake_hass, ("light", "turn_on"), ("light", "turn_off"),
              ("climate", "set_preset_mode"))
    return cc.ProactiveManager(fake_hass, {})


def _dark_room(registry, hass, light_state="off"):
    registry.areas["lounge"] = "Lounge"
    hass.states.set("sensor.lounge_lux", "5", device_class="illuminance")
    registry.add(FakeRegistryEntry("sensor.lounge_lux", "x", area_id="lounge"))
    hass.states.set("light.lounge", light_state)
    registry.add(FakeRegistryEntry("light.lounge", "x", area_id="lounge"))
    hass.states.set("binary_sensor.lounge_motion", "on", device_class="motion")
    registry.add(FakeRegistryEntry("binary_sensor.lounge_motion", "x", area_id="lounge"))


async def test_dark_room_offer_for_a_working_light(proactive, registry, fake_hass):
    _dark_room(registry, fake_hass)
    offer = await proactive._check_dark_occupied_room(True)
    assert offer["action_data"]["entity_ids"] == ["light.lounge"]


async def test_no_dark_room_offer_when_the_only_light_is_unavailable(proactive, registry, fake_hass):
    _dark_room(registry, fake_hass, light_state="unavailable")
    assert await proactive._check_dark_occupied_room(True) is None


async def test_eco_offer_when_the_thermostat_has_an_eco_preset(proactive, fake_hass):
    fake_hass.states.set("climate.hall", "heat", hvac_action="heating", friendly_name="Hall",
                         supported_features=16, preset_modes=["eco", "comfort"])
    offer = await proactive._check_hvac_efficiency(False)
    assert offer["offer"] is True and "action_data" in offer
    assert "Would you like me to set it back" in offer["message"]


async def test_eco_without_an_eco_preset_is_information_only(proactive, fake_hass):
    fake_hass.states.set("climate.hall", "heat", hvac_action="heating", friendly_name="Hall",
                         supported_features=16, preset_modes=["comfort", "away"])
    offer = await proactive._check_hvac_efficiency(False)
    assert offer is not None                                  # the useful alert stays
    assert offer.get("offer") is False and "action_data" not in offer and "pattern_key" not in offer
    assert "Hall is heating but no one's home" in offer["message"]
    assert "?" not in offer["message"]


async def test_energy_opt_in_offer_is_information_only(load, fake_hass, monkeypatch):
    energy = load("energy")
    status = {"over_peak": True, "agency": "opt_in", "kw": 9.2, "running": [
        {"name": "Dryer", "entity": "sensor.dryer_power", "watts": 4200, "shed_ok": True},
        {"name": "Oven", "entity": "sensor.oven_power", "watts": 3000, "shed_ok": True}]}
    monkeypatch.setattr(energy, "power_status", lambda hass: status)
    offer = energy.evaluate_for_proactive(fake_hass)
    assert offer is not None and offer["auto_act"] is False
    assert "?" not in offer["message"] and "Want me to" not in offer["message"]
