"""Whole house energy (energy.py): agency, peak detection and the offer.

The parts that matter most for safety are covered from several sides: how far
the agency setting may climb, that critical loads are never put forward for
holding, and that no offer ever claims to act (auto_act is False at every
level, because Nova has no switch to act with). The live picture is built from
fake_hass states; the settings come from a dict in place of nova_config.
"""
import sys
import types

import pytest


@pytest.fixture
def energy(load, monkeypatch):
    module = load("energy")
    # Every test begins outside an over peak spell (8.49.37).
    monkeypatch.setattr(module, "_peak_spell_offered", False, raising=False)
    return module


@pytest.fixture(autouse=True)
def _no_modes_stub():
    """_mode_agency_bonus imports the sibling ``modes`` lazily; a stub left in
    sys.modules by one test would change the agency in the next, so none is
    allowed in or out."""
    sys.modules.pop("jc.modes", None)
    yield
    sys.modules.pop("jc.modes", None)


def _settings(energy, monkeypatch, **values):
    monkeypatch.setattr(energy, "_cfg", lambda key, default=None: values.get(key, default))


def _modes(monkeypatch, active):
    stub = types.ModuleType("jc.modes")
    stub.active_mode = active
    monkeypatch.setitem(sys.modules, "jc.modes", stub)
    return stub


def _load(name, entity, watts):
    return {"name": name, "entity": entity, "watts": watts, "shed_ok": True}


DRYER = _load("Dryer", "switch.dryer", 4000)
OVEN = _load("Oven", "switch.oven", 5000)


def _status(monkeypatch, energy, *, over, running=(), agency="advisory", watts=9000):
    picture = {"watts": watts, "kw": None if watts is None else watts / 1000,
               "over_peak": over, "agency": agency, "running": list(running)}
    monkeypatch.setattr(energy, "power_status", lambda hass: dict(picture))
    monkeypatch.setattr(energy, "effective_agency", lambda: agency)


# ── how far Nova may go ──────────────────────────────────────────────────────

@pytest.mark.parametrize("configured, bonus, expected", [
    (None, 0, "advisory"),            # unset: advisory by default
    ("autonomous", 0, "autonomous"),
    ("nonsense", 0, "advisory"),      # unknown words fall back to the lowest
    ("OPT_IN", 0, "opt_in"),          # case does not matter
    ("advisory", 0, "advisory"),      # an empty bump list changes nothing
    ("advisory", 1, "opt_in"),        # a bump mode raises exactly one step
    ("opt_in", 1, "autonomous"),
    ("autonomous", 1, "autonomous"),  # never above the top
])
def test_effective_agency_ladder(energy, monkeypatch, configured, bonus, expected):
    values = {} if configured is None else {"energy_agency": configured}
    _settings(energy, monkeypatch, **values)
    monkeypatch.setattr(energy, "_mode_agency_bonus", lambda: bonus)
    assert energy.effective_agency() == expected


def test_bump_applies_only_while_a_listed_mode_is_active(energy, monkeypatch):
    _settings(energy, monkeypatch, energy_mode_bump=["away"])
    stub = _modes(monkeypatch, lambda: "away")
    assert energy._mode_agency_bonus() == 1
    stub.active_mode = lambda: "normal"
    assert energy._mode_agency_bonus() == 0


def test_bump_ignores_a_setting_that_is_not_a_list(energy, monkeypatch):
    _settings(energy, monkeypatch, energy_mode_bump="away")
    _modes(monkeypatch, lambda: "away")
    assert energy._mode_agency_bonus() == 0


def test_bump_survives_the_modes_module_failing(energy, monkeypatch):
    def _broken():
        raise RuntimeError("modes not ready")
    _settings(energy, monkeypatch, energy_mode_bump=["away"], energy_agency="advisory")
    _modes(monkeypatch, _broken)
    assert energy._mode_agency_bonus() == 0
    assert energy.effective_agency() == "advisory"


# ── loads that are never put forward ─────────────────────────────────────────

@pytest.mark.parametrize("entity, name, protected", [
    ("switch.cpap_machine", "CPAP", True),
    ("sensor.fridge_power", "Refrigerator", True),
    ("switch.network_rack", "Server", True),
    ("switch.sump_pump", "Sump Pump", True),
    ("switch.plug_4", "Boiler", True),              # the name alone is enough
    ("switch.dryer", "Clothes Dryer", False),
    ("switch.dishwasher", "Dishwasher", False),
])
def test_never_shed_list(energy, entity, name, protected):
    assert energy._never_shed(entity, name) is protected


# ── the peak and the reading ─────────────────────────────────────────────────

@pytest.mark.parametrize("setting, expected", [
    (None, 8000.0), (5000, 5000.0), ("6500", 6500.0), ("lots", 8000.0), (0, 8000.0),
])
def test_peak_threshold(energy, monkeypatch, setting, expected):
    values = {} if setting is None else {"energy_peak_watts": setting}
    _settings(energy, monkeypatch, **values)
    assert energy._peak_threshold() == expected


def test_kilowatt_readings_are_converted(energy, fake_hass):
    reading = fake_hass.states.set("sensor.grid", "1.5", unit_of_measurement="kW")
    assert energy._watts(reading) == 1500.0
    reading = fake_hass.states.set("sensor.grid", "750", unit_of_measurement="W")
    assert energy._watts(reading) == 750.0


def test_running_appliances_sorted_and_filtered(energy, monkeypatch, fake_hass):
    _settings(energy, monkeypatch, appliances=[
        {"name": "Kettle", "entity": "sensor.kettle_power"},
        {"name": "Freezer", "entity": "sensor.freezer_power"},
        {"name": "Lamp", "entity": "sensor.lamp_power"},          # under 50 W
        {"name": "Washer", "entity": "sensor.washer_power"},      # not a number
        {"name": "Ghost", "entity": "sensor.not_there"},          # no state
        "not a dict",
    ])
    fake_hass.states.set("sensor.kettle_power", "2.2", unit_of_measurement="kW")
    fake_hass.states.set("sensor.freezer_power", "120", unit_of_measurement="W")
    fake_hass.states.set("sensor.lamp_power", "12", unit_of_measurement="W")
    fake_hass.states.set("sensor.washer_power", "unavailable", unit_of_measurement="W")
    assert energy._running_appliances(fake_hass) == [
        {"name": "Kettle", "entity": "sensor.kettle_power", "watts": 2200, "shed_ok": True},
        {"name": "Freezer", "entity": "sensor.freezer_power", "watts": 120, "shed_ok": False},
    ]


def test_running_appliances_with_a_bad_setting_is_empty(energy, monkeypatch, fake_hass):
    _settings(energy, monkeypatch, appliances={"name": "Kettle"})
    assert energy._running_appliances(fake_hass) == []


def test_power_status_over_peak_names_the_movable_loads(energy, monkeypatch, fake_hass):
    _settings(energy, monkeypatch, energy_peak_watts=6000, appliances=[
        {"name": "Dryer", "entity": "sensor.dryer_power"},
        {"name": "Oven", "entity": "sensor.oven_power"},
    ])
    monkeypatch.setattr(energy, "_read_whole_home_watts", lambda h: (9000.0, "sensor.grid"))
    fake_hass.states.set("sensor.dryer_power", "3000", unit_of_measurement="W")
    fake_hass.states.set("sensor.oven_power", "4000", unit_of_measurement="W")
    status = energy.power_status(fake_hass)
    assert (status["watts"], status["kw"], status["meter"]) == (9000, 9.0, "sensor.grid")
    assert status["peak_watts"] == 6000 and status["over_peak"] is True
    assert status["agency"] == status["configured_agency"] == "advisory"
    assert [item["name"] for item in status["running"]] == ["Oven", "Dryer"]
    assert status["advice"] == ["Drawing 9.0 kW (over the 6.0 kW peak) — Oven + Dryer "
                                "running at once. Staggering them would cut the peak."]


@pytest.mark.parametrize("watts, meter, running, line", [
    (None, "sensor.grid", [], "isn't reporting"),
    (None, "", [], "No whole-home power meter found"),
    (3000.0, "sensor.grid", [], "Drawing 3.0 kW — within the 8.0 kW comfort range."),
    (9000.0, "sensor.grid", [DRYER], "Drawing 9.0 kW, above the 8.0 kW peak."),
])
def test_advice_line_for_each_situation(energy, watts, meter, running, line):
    assert line in energy._advice_line(watts, meter, 8000.0, running)


def test_meter_found_but_silent_is_not_called_missing(energy, monkeypatch, fake_hass):
    """8.49.37: a meter that exists but gives no number gets its own line."""
    monkeypatch.setattr(energy, "_running_appliances", lambda h: [])
    monkeypatch.setattr(energy, "_read_whole_home_watts", lambda h: (None, "sensor.grid"))
    advice = energy.power_status(fake_hass)["advice"]
    assert len(advice) == 1
    assert "isn't reporting" in advice[0]
    assert "No whole-home power meter found" not in advice[0]
    monkeypatch.setattr(energy, "_read_whole_home_watts", lambda h: (None, ""))
    assert "No whole-home power meter found" in energy.power_status(fake_hass)["advice"][0]


# ── the proactive offer ──────────────────────────────────────────────────────

@pytest.mark.parametrize("agency, kind, names_a_load", [
    ("advisory", "energy_advice", False),
    ("opt_in", "energy_shed_offer", True),
    # The autonomous wording used to report auto_act True, but appliance
    # profiles only hold a power sensor, so nothing was ever switched. It now
    # says so instead of claiming an action it cannot take.
    ("autonomous", "energy_shed", True),
])
def test_offer_wording_per_agency_never_acts(energy, monkeypatch, fake_hass,
                                             agency, kind, names_a_load):
    _status(monkeypatch, energy, over=True, running=[DRYER, OVEN], agency=agency)
    offer = energy.evaluate_for_proactive(fake_hass)
    assert offer is not None
    assert offer["type"] == kind
    assert offer["auto_act"] is False
    assert offer["urgency"] == "low" and offer["energy"] is True
    assert "Dryer and Oven" in offer["message"] and "9.0 kW" in offer["message"]
    if names_a_load:
        assert offer["entity_id"] == "switch.dryer"   # the first movable load
    else:
        assert "entity_id" not in offer


@pytest.mark.parametrize("running", [
    [],                                                       # nothing running
    [_load("Oven", "switch.oven", 9000)],                     # one load cannot stagger
    [dict(DRYER, shed_ok=False), dict(OVEN, shed_ok=False)],  # both critical
], ids=["no-loads", "single-load", "critical-only"])
def test_no_offer_without_two_movable_loads(energy, monkeypatch, fake_hass, running):
    _status(monkeypatch, energy, over=True, running=running, agency="autonomous")
    assert energy.evaluate_for_proactive(fake_hass) is None


def test_no_offer_under_peak(energy, monkeypatch, fake_hass):
    _status(monkeypatch, energy, over=False, running=[DRYER, OVEN], watts=3000)
    assert energy.evaluate_for_proactive(fake_hass) is None


def test_no_offer_when_the_status_itself_fails(energy, monkeypatch, fake_hass):
    def _broken(hass):
        raise RuntimeError("recorder gone")
    monkeypatch.setattr(energy, "power_status", _broken)
    assert energy.evaluate_for_proactive(fake_hass) is None


def test_one_offer_per_over_peak_spell(energy, monkeypatch, fake_hass):
    """8.49.37: a long over peak evening is mentioned once; the spell ends
    with the next reading under peak."""
    _status(monkeypatch, energy, over=True, running=[DRYER, OVEN])
    outcomes = [energy.evaluate_for_proactive(fake_hass) is not None for _ in range(3)]
    assert outcomes == [True, False, False]
    _status(monkeypatch, energy, over=False, watts=3000)
    assert energy.evaluate_for_proactive(fake_hass) is None
    _status(monkeypatch, energy, over=True, running=[DRYER, OVEN])
    assert energy.evaluate_for_proactive(fake_hass) is not None


def test_an_unknown_reading_does_not_end_the_spell(energy, monkeypatch, fake_hass):
    _status(monkeypatch, energy, over=True, running=[DRYER, OVEN])
    assert energy.evaluate_for_proactive(fake_hass) is not None
    _status(monkeypatch, energy, over=False, watts=None)      # meter dropped out
    assert energy.evaluate_for_proactive(fake_hass) is None
    _status(monkeypatch, energy, over=True, running=[DRYER, OVEN])
    assert energy.evaluate_for_proactive(fake_hass) is None


# ── the agent can ask for it ────────────────────────────────────────────────

def test_agent_exposes_energy_status(load):
    agent = load("agent")
    assert "energy_status" in {tool["function"]["name"] for tool in agent.NOVA_TOOLS}
    assert "energy_status" in agent._TOOL_MAP


# ── finding a whole home meter by name ──────────────────────────────────────

@pytest.mark.parametrize("entity, attrs, found", [
    # EcoFlow PowerOcean calls its whole home sensor "Home Consumption"; it
    # was once missing from the list, leaving a correctly tagged meter unseen.
    ("sensor.inverter_house_power", {"device_class": "power", "unit_of_measurement": "W",
                                     "friendly_name": "Home Consumption"}, True),
    ("sensor.main_meter", {"device_class": "power", "unit_of_measurement": "W",
                           "friendly_name": "Main Power"}, True),
    ("sensor.mains", {"unit_of_measurement": "kW", "friendly_name": "Mains Power"}, True),
    ("sensor.washer_power", {"device_class": "power", "unit_of_measurement": "W",
                             "friendly_name": "Washer Power"}, False),
    ("sensor.home_energy_total", {"device_class": "energy", "unit_of_measurement": "kWh",
                                  "friendly_name": "Home Energy"}, False),
])
def test_fallback_meter_by_friendly_name(energy, fake_hass, entity, attrs, found):
    fake_hass.states.set(entity, "512.3", **attrs)
    assert energy._fallback_meter(fake_hass) == (entity if found else None)
