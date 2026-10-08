"""Sensor units in the Energy tab's totals and live power.

solar._daily_sum returns an amount in the sensor's own unit. A battery whose
totals report Wh used to show "Battery charged 10227.0 kWh", which also drove
the home balance negative so Home used and Self sufficiency read "—".
solar._energy_to_kwh converts every total; solar._live_watts converts power.
"""
import pytest


@pytest.fixture
def solar(load):
    return load("solar")


@pytest.fixture
def ef(load):
    mod = load("energy_flow")
    mod._today_cache.clear()
    yield mod
    mod._today_cache.clear()


class _State:
    def __init__(self, state="0", unit=None, device_class=None):
        self.state = state
        self.attributes = {}
        if unit is not None:
            self.attributes["unit_of_measurement"] = unit
        if device_class is not None:
            self.attributes["device_class"] = device_class
        self.last_changed = None


class _Hass:
    def __init__(self, states=None):
        self._states = states or {}

    @property
    def states(self):
        outer = self

        class _States:
            def get(self, eid):
                return outer._states.get(eid)
        return _States()


# ── _energy_to_kwh ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("unit, device_class, value, expected", [
    ("Wh", None, 10227.0, 10.227),
    ("kWh", None, 6.46, 6.46),
    ("MWh", None, 0.0125, 12.5),
    ("mWh", None, 2_000_000.0, 2.0),
    ("kwh", None, 3.0, 3.0),                  # lower case still known
    ("wh", None, 500.0, 0.5),
    (None, "energy", 12.0, 12.0),             # no unit, energy sensor, plausible
    (None, "energy", 10227.0, None),          # no unit, implausible for kWh
    (None, None, 12.0, None),                 # no unit and not an energy sensor
    ("J", None, 12.0, None),                  # a unit we do not convert
    ("kWh", None, None, None),                # nothing read
])
def test_energy_to_kwh(solar, unit, device_class, value, expected):
    hass = _Hass({"sensor.e": _State(unit=unit, device_class=device_class)})
    got = solar._energy_to_kwh(hass, "sensor.e", value)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_energy_to_kwh_without_a_state_or_entity(solar):
    assert solar._energy_to_kwh(_Hass(), "sensor.gone", 5.0) is None
    assert solar._energy_to_kwh(_Hass(), None, 5.0) is None


# ── _live_watts ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state, unit, expected", [
    ("650", "W", 650.0),
    ("2.199", "kW", 2199.0),
    ("0.0032", "MW", 3200.0),
    ("450000", "mW", 450.0),
    ("1.5", "kw", 1500.0),                    # lower case kW, as before
    ("-2199", None, -2199.0),                 # no unit: watts, as before
    ("unavailable", "W", None),
])
def test_live_watts_units(solar, state, unit, expected):
    hass = _Hass({"sensor.p": _State(state, unit=unit)})
    got = solar._live_watts(hass, "sensor.p")
    assert got == (pytest.approx(expected) if expected is not None else None)


# ── today's totals end to end ───────────────────────────────────────────────

_SOURCES = [
    {"type": "solar", "stat_energy_from": "sensor.pv"},
    {"type": "battery", "stat_energy_from": "sensor.bat_out", "stat_energy_to": "sensor.bat_in"},
    {"type": "grid", "stat_energy_from": "sensor.grid_in", "stat_energy_to": "sensor.grid_out"},
]


def _setup(ef, monkeypatch, totals, units):
    async def _prefs(hass):
        return {"energy_sources": _SOURCES}

    async def _sum(hass, eid):
        return totals.get(eid)
    monkeypatch.setattr(ef, "_read_prefs", _prefs)
    monkeypatch.setattr(ef, "_daily_sum", _sum)
    return _Hass({eid: _State(unit=u) for eid, u in units.items()})


async def test_the_reported_case_wh_battery_with_kwh_grid_and_solar(ef, monkeypatch):
    hass = _setup(ef, monkeypatch,
                  {"sensor.pv": 6.46, "sensor.grid_in": 14.45, "sensor.grid_out": 1.11,
                   "sensor.bat_in": 10227.0, "sensor.bat_out": 7994.0},
                  {"sensor.pv": "kWh", "sensor.grid_in": "kWh", "sensor.grid_out": "kWh",
                   "sensor.bat_in": "Wh", "sensor.bat_out": "Wh"})
    out = await ef.energy_flow_today(hass)
    assert out["battery_charged_kwh"] == 10.23
    assert out["battery_discharged_kwh"] == 7.99
    assert out["home_kwh"] == pytest.approx(17.57, abs=0.01)
    assert out["self_sufficiency_pct"] == pytest.approx(17.7, abs=0.1)
    assert round(out["self_sufficiency_pct"]) == 18


async def test_kwh_only(ef, monkeypatch):
    hass = _setup(ef, monkeypatch,
                  {"sensor.pv": 12.0, "sensor.grid_in": 3.0, "sensor.grid_out": 5.0,
                   "sensor.bat_in": 4.0, "sensor.bat_out": 2.5},
                  {e: "kWh" for e in ("sensor.pv", "sensor.grid_in", "sensor.grid_out",
                                      "sensor.bat_in", "sensor.bat_out")})
    out = await ef.energy_flow_today(hass)
    assert out["home_kwh"] == 8.5 and out["self_sufficiency_pct"] == 64.7


async def test_mwh_grid(ef, monkeypatch):
    hass = _setup(ef, monkeypatch,
                  {"sensor.pv": 6.0, "sensor.grid_in": 0.004, "sensor.grid_out": 0.001,
                   "sensor.bat_in": 0.0, "sensor.bat_out": 0.0},
                  {"sensor.pv": "kWh", "sensor.grid_in": "MWh", "sensor.grid_out": "MWh",
                   "sensor.bat_in": "kWh", "sensor.bat_out": "kWh"})
    out = await ef.energy_flow_today(hass)
    assert out["grid_import_kwh"] == 4.0 and out["grid_export_kwh"] == 1.0
    assert out["home_kwh"] == 9.0


async def test_missing_unit_makes_that_value_and_home_none(ef, monkeypatch):
    hass = _setup(ef, monkeypatch,
                  {"sensor.pv": 6.46, "sensor.grid_in": 14.45, "sensor.grid_out": 1.11,
                   "sensor.bat_in": 10227.0, "sensor.bat_out": 7994.0},
                  {"sensor.pv": "kWh", "sensor.grid_in": "kWh", "sensor.grid_out": "kWh",
                   "sensor.bat_out": "Wh"})          # sensor.bat_in has no unit
    out = await ef.energy_flow_today(hass)
    assert out["battery_charged_kwh"] is None
    assert out["battery_discharged_kwh"] == 7.99
    assert out["home_kwh"] is None and out["self_sufficiency_pct"] is None


async def test_large_negative_balance_still_returns_none(ef, monkeypatch):
    # Charged reported in kWh but really far larger than what came in.
    hass = _setup(ef, monkeypatch,
                  {"sensor.pv": 1.0, "sensor.grid_in": 1.0, "sensor.grid_out": 0.0,
                   "sensor.bat_in": 50.0, "sensor.bat_out": 0.0},
                  {e: "kWh" for e in ("sensor.pv", "sensor.grid_in", "sensor.grid_out",
                                      "sensor.bat_in", "sensor.bat_out")})
    out = await ef.energy_flow_today(hass)
    assert out["home_kwh"] is None


# ── solar.daily_report had the same bug ─────────────────────────────────────

async def test_daily_report_converts_wh_battery_totals(solar, monkeypatch):
    async def _prefs(hass):
        return {"energy_sources": _SOURCES}
    totals = {"sensor.pv": 6.46, "sensor.grid_in": 14.45, "sensor.grid_out": 1.11,
              "sensor.bat_in": 10227.0, "sensor.bat_out": 7994.0}

    async def _sum(hass, eid):
        return totals.get(eid)

    async def _cost(hass, imported, exported):
        return None, "unavailable"
    monkeypatch.setattr(solar, "_read_prefs", _prefs)
    monkeypatch.setattr(solar, "_daily_sum", _sum)
    monkeypatch.setattr(solar, "_forecast_values", lambda hass: {})
    monkeypatch.setattr(solar, "_cost_today", _cost)
    hass = _Hass({"sensor.pv": _State(unit="kWh"), "sensor.grid_in": _State(unit="kWh"),
                  "sensor.grid_out": _State(unit="kWh"), "sensor.bat_in": _State(unit="Wh"),
                  "sensor.bat_out": _State(unit="Wh")})
    out = await solar.daily_report(hass)
    assert out["battery_charged_kwh"] == 10.23
    assert out["battery_discharged_kwh"] == 7.99
    assert out["imported_kwh"] == 14.45
