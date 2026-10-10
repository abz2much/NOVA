"""Garage words only where a garage exists (8.26.0).

home_doors.has_garage() decides whether the panel shows garage items. It is
True only on a reliable signal, and the user's Garage setting (Auto, Yes, No)
overrides it. It is display only: the last tests prove lockdown, the night
sweep and intrusion act the same whatever the setting says. All state is fake.
"""
import pathlib
import sys
import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, run_sweeps  # noqa: F401

ALARM = "alarm_control_panel.home_security"
NOVA = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def hd(load):
    return load("home_doors")


@pytest.fixture
def areas(monkeypatch):
    """Set the Home Assistant area names the registry returns."""
    names = []
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_list_areas=lambda: [types.SimpleNamespace(name=n) for n in names]))
    return names


# ── reliable signals ────────────────────────────────────────────────────────

def test_a_garage_cover_is_a_garage(hd, fake_hass):
    fake_hass.states.set("cover.big_door", "closed", device_class="garage")
    assert hd.has_garage(fake_hass, {}) is True


def test_a_garage_door_sensor_is_a_garage(hd, fake_hass):
    fake_hass.states.set("binary_sensor.roller", "off", device_class="garage_door")
    assert hd.has_garage(fake_hass, {}) is True


@pytest.mark.parametrize("name", ["Garage", "garage", "Double Garage"])
def test_an_area_named_garage_is_a_garage(hd, fake_hass, areas, name):
    areas.append(name)
    assert hd.has_garage(fake_hass, {}) is True


@pytest.mark.parametrize("slot", ["garage", "garage_1", "garage_3"])
def test_a_mapped_garage_door_slot_is_a_garage(hd, fake_hass, slot):
    assert hd.has_garage(fake_hass, {"door_mapping": {slot: "cover.x"}}) is True
    assert hd.has_garage(fake_hass, {"door_mapping": f'{{"{slot}": "cover.x"}}'}) is True


# ── signals that never count ────────────────────────────────────────────────

def test_a_name_alone_is_not_a_garage(hd, fake_hass):
    fake_hass.states.set("cover.garage_gate", "closed", device_class="gate",
                         friendly_name="Garage gate")
    fake_hass.states.set("cover.garage_shutter", "closed", device_class="shutter")
    fake_hass.states.set("cover.garage", "closed")                     # no device class
    fake_hass.states.set("binary_sensor.garage_car", "on", device_class="occupancy")
    fake_hass.states.set("sensor.garage_temperature", "12")
    fake_hass.states.set("light.garage", "off")
    assert hd.has_garage(fake_hass, {}) is False


def test_the_default_floor_plan_room_and_bays_are_not_a_garage(hd, fake_hass):
    cfg = {"floor_plan_rooms": {"1f": {"rooms": [{"name": "Garage"}]}}, "garage_bays": 3}
    assert hd.has_garage(fake_hass, cfg) is False


def test_an_unset_or_garage_rear_slot_is_not_a_garage(hd, fake_hass):
    cfg = {"door_mapping": {"garage": "", "garage_rear": "lock.back", "kitchen_garage": "lock.k"}}
    assert hd.has_garage(fake_hass, cfg) is False


@pytest.mark.parametrize("name", ["Garden", "Carport", "Garageband studio"])
def test_other_area_names_are_not_a_garage(hd, fake_hass, areas, name):
    areas.append(name)
    assert hd.has_garage(fake_hass, {}) is False


# ── no covers at all ────────────────────────────────────────────────────────

def test_no_covers_and_nothing_else_is_no_garage(hd, fake_hass, areas):
    fake_hass.states.set("lock.back_door", "locked")
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door")
    assert hd.has_garage(fake_hass, {}) is False


def test_no_covers_but_a_garage_area_is_a_garage(hd, fake_hass, areas):
    areas.append("Garage")
    assert hd.has_garage(fake_hass, {}) is True


def test_no_covers_but_a_garage_door_sensor_is_a_garage(hd, fake_hass):
    fake_hass.states.set("binary_sensor.garage_contact", "off", device_class="garage_door")
    assert hd.has_garage(fake_hass, {}) is True


# ── the override ────────────────────────────────────────────────────────────

def test_no_overrides_a_detected_garage(hd, fake_hass):
    fake_hass.states.set("cover.big_door", "closed", device_class="garage")
    assert hd.has_garage(fake_hass, {"garage_mode": "no"}) is False


def test_yes_overrides_no_signal(hd, fake_hass):
    assert hd.has_garage(fake_hass, {"garage_mode": "yes"}) is True


@pytest.mark.parametrize("mode", [None, "", "maybe", 3])
def test_an_unknown_setting_is_auto(hd, fake_hass, mode):
    assert hd.garage_mode({"garage_mode": mode}) == "auto"
    fake_hass.states.set("cover.big_door", "closed", device_class="garage")
    assert hd.has_garage(fake_hass, {"garage_mode": mode}) is True


def test_a_failed_read_is_no_garage(hd, fake_hass, monkeypatch):
    monkeypatch.setattr(hd, "garage_signals", lambda *a: 1 / 0)
    assert hd.has_garage(fake_hass, {}) is False


def test_adding_or_removing_a_garage_changes_the_answer(hd, fake_hass):
    assert hd.has_garage(fake_hass, {}) is False
    fake_hass.states.set("cover.big_door", "closed", device_class="garage")
    assert hd.has_garage(fake_hass, {}) is True
    fake_hass.states.remove("cover.big_door")
    assert hd.has_garage(fake_hass, {}) is False


# ── display only: the flag never changes safety ─────────────────────────────

def test_no_safety_module_reads_the_flag():
    for name in ("core_safety.py", "core_lockdown.py", "core_lockdown_sync.py", "world.py",
                 "household.py", "alert_path.py", "sentinel.py", "cognitive_core.py"):
        text = (NOVA / name).read_text(encoding="utf-8")
        assert "home_doors" not in text and "garage_mode" not in text and "has_garage" not in text, name


def _obey(fake_hass):
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get((domain, service))
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        if st is not None and new:
            fake_hass.states.set(eid, new, **dict(st.attributes))
    fake_hass.services.async_call = call


def _calls(fake_hass):
    return sorted((c[0], c[1], c[2].get("entity_id")) for c in fake_hass.service_calls
                  if c[0] in ("lock", "cover"))


@pytest.mark.parametrize("mode", ["auto", "yes", "no"])
async def test_lockdown_closes_the_garage_whatever_the_setting(cc, fake_hass, mode):
    _obey(fake_hass)
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    action = await cc.LockdownManager(fake_hass, {"garage_mode": mode}).engage("alarm armed")
    fake_hass.close_pending()
    assert _calls(fake_hass) == [("cover", "close_cover", "cover.garage"), ("lock", "lock", "lock.back")]
    assert "Garage" in action["message"]


@pytest.mark.parametrize("mode", ["auto", "yes", "no"])
async def test_the_night_sweep_closes_the_garage_whatever_the_setting(cc, fake_hass, clock, mode):
    _obey(fake_hass)
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True,
                                          "garage_mode": mode})
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    await safety.tick(sleeping=True, anyone_home=True)
    alerts = await run_sweeps(safety, fake_hass)
    assert _calls(fake_hass) == [("cover", "close_cover", "cover.garage")]
    assert len(alerts) == 1 and "Garage" in alerts[0]["message"]


@pytest.mark.parametrize("mode", ["auto", "yes", "no"])
async def test_intrusion_treats_an_open_garage_the_same_whatever_the_setting(cc, fake_hass, clock, mode):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM,
                                          "garage_mode": mode})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert safety._open_entry() == "cover.garage"
    actions = await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    assert [a["type"] for a in actions] == ["intrusion_investigating"]
