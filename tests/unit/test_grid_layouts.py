"""Both Energy dashboard grid layouts read the same (solar._grid_sensors).

Current Home Assistant stores a grid source flat: stat_rate,
stat_energy_from and stat_energy_to on the source, with the prices beside
them. Older Home Assistant (read in 2026.2.3's energy/data.py) nests them:
flow_from[] (stat_energy_from + import price), flow_to[] (stat_energy_to +
export price) and power[] (stat_rate + power_config). Nova supports
HA 2024.10.0 and later, so solar.py and energy_flow.py must read both.
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

# A real install's grid source on HA core 2026.10.0, exactly as stored.
FLAT_GRID = {"type": "grid", "cost_adjustment_day": 0, "entity_energy_price": "input_number.current_electricity_price", "entity_energy_price_export": None, "number_energy_price": None, "number_energy_price_export": 0.216, "power_config": {"stat_rate": "sensor.solar_inverter_pcsmeterpower"}, "stat_compensation": None, "stat_cost": None, "stat_energy_from": "sensor.grid_import_total_energy", "stat_energy_to": "sensor.grid_export_total_energy", "stat_rate": "sensor.solar_inverter_pcsmeterpower"}

# The same grid in the nested layout of HA 2026.2.3's GRID_SOURCE_SCHEMA.
NESTED_GRID = {
    "type": "grid",
    "flow_from": [{
        "stat_energy_from": "sensor.grid_import_total_energy",
        "stat_cost": None,
        "entity_energy_price": "input_number.current_electricity_price",
        "number_energy_price": None,
    }],
    "flow_to": [{
        "stat_energy_to": "sensor.grid_export_total_energy",
        "stat_compensation": None,
        "entity_energy_price": None,
        "number_energy_price": 0.216,
    }],
    "power": [{
        "stat_rate": "sensor.solar_inverter_pcsmeterpower",
        "power_config": {"stat_rate": "sensor.solar_inverter_pcsmeterpower"},
    }],
    "cost_adjustment_day": 0.0,
}

_EXPECTED = {
    "rates": ["sensor.solar_inverter_pcsmeterpower"],
    "imports": ["sensor.grid_import_total_energy"],
    "exports": ["sensor.grid_export_total_energy"],
    "import_price": ("input_number.current_electricity_price", None),
    "export_price": (None, 0.216),
}

_T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def solar(load):
    return load("solar")


@pytest.fixture
def ef(load):
    return load("energy_flow")


class _State:
    def __init__(self, state, unit="W", last_changed=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit}
        self.last_changed = last_changed


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


def _stub_prefs(mod, monkeypatch, sources):
    async def _fake(hass):
        return {"energy_sources": sources}
    monkeypatch.setattr(mod, "_read_prefs", _fake)


def _exporting_states(rate="650"):
    """Grid rate positive while exporting; the export total changed last."""
    return {
        "sensor.solar_power": _State("3200"),
        "sensor.solar_inverter_pcsmeterpower": _State(rate),
        "sensor.grid_import_total_energy": _State("100", unit="kWh", last_changed=_T0),
        "sensor.grid_export_total_energy": _State("50", unit="kWh", last_changed=_T0 + timedelta(seconds=20)),
    }


# ── _grid_sensors ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("grid", [FLAT_GRID, NESTED_GRID], ids=["flat", "nested"])
def test_both_layouts_give_the_same_sensors_and_prices(solar, grid):
    assert solar._grid_sensors(grid) == _EXPECTED


def test_several_import_export_and_rate_entries(solar):
    grid = {
        "type": "grid",
        "flow_from": [{"stat_energy_from": "sensor.import_peak", "entity_energy_price": None, "number_energy_price": 0.31},
                      {"stat_energy_from": "sensor.import_offpeak", "entity_energy_price": None, "number_energy_price": 0.12}],
        "flow_to": [{"stat_energy_to": "sensor.export_a"}, {"stat_energy_to": "sensor.export_b"}],
        "power": [{"stat_rate": "sensor.phase_1"}, {"stat_rate": "sensor.phase_2"}],
        "cost_adjustment_day": 0.0,
    }
    out = solar._grid_sensors(grid)
    assert out["imports"] == ["sensor.import_peak", "sensor.import_offpeak"]
    assert out["exports"] == ["sensor.export_a", "sensor.export_b"]
    assert out["rates"] == ["sensor.phase_1", "sensor.phase_2"]
    assert out["import_price"] == (None, 0.31)     # the first entry with a price
    assert out["export_price"] == (None, None)


@pytest.mark.parametrize("grid", [
    {"type": "grid"},
    {"type": "grid", "flow_from": [], "flow_to": [], "power": [], "cost_adjustment_day": 0.0},
    {"type": "grid", "flow_from": "not a list", "power": [None, 3]},
], ids=["bare", "empty-lists", "malformed"])
def test_a_source_with_neither_layout_reads_nothing(solar, grid):
    assert solar._grid_sensors(grid) == {
        "rates": [], "imports": [], "exports": [],
        "import_price": (None, None), "export_price": (None, None),
    }


def test_a_battery_source_reads_through_the_same_flat_keys(solar):
    out = solar._grid_sensors({"type": "battery", "stat_rate": "sensor.bat_power",
                               "stat_energy_from": "sensor.bat_out", "stat_energy_to": "sensor.bat_in"})
    assert (out["rates"], out["imports"], out["exports"]) == (
        ["sensor.bat_power"], ["sensor.bat_out"], ["sensor.bat_in"])


# ── solar.py reads both layouts ─────────────────────────────────────────────

@pytest.mark.parametrize("grid", [FLAT_GRID, NESTED_GRID], ids=["flat", "nested"])
def test_solar_status_reads_both_layouts(solar, monkeypatch, grid):
    _stub_prefs(solar, monkeypatch, [{"type": "solar", "stat_rate": "sensor.solar_power"}, grid])
    out = asyncio.run(solar.solar_status(_Hass(_exporting_states())))
    assert out["grid_w"] == 650
    assert out["grid_direction"] == "export"        # from the totals, not the + sign


def test_grid_direction_uses_the_latest_of_several_totals(solar):
    grid = {"type": "grid",
            "flow_from": [{"stat_energy_from": "sensor.i1"}, {"stat_energy_from": "sensor.i2"}],
            "flow_to": [{"stat_energy_to": "sensor.e1"}]}
    hass = _Hass({
        "sensor.i1": _State("1", "kWh", _T0),
        "sensor.i2": _State("1", "kWh", _T0 + timedelta(seconds=40)),   # newest
        "sensor.e1": _State("1", "kWh", _T0 + timedelta(seconds=20)),
    })
    assert solar._grid_direction(hass, grid, None) == "import"


@pytest.mark.parametrize("grid", [FLAT_GRID, NESTED_GRID], ids=["flat", "nested"])
async def test_cost_estimate_reads_both_layouts(solar, fake_hass, monkeypatch, load, grid):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [])
    nova_config = load("nova_config")
    monkeypatch.setattr(nova_config, "runtime_get", lambda hass, entry, key, default="": default)
    _stub_prefs(solar, monkeypatch, [grid])
    fake_hass.states.set("input_number.current_electricity_price", "0.25")
    cost, source = await solar._cost_today(fake_hass, imported_kwh=10.0, exported_kwh=4.0)
    assert source == "estimate"
    assert cost["today"] == pytest.approx(2.5)
    assert cost["net_today"] == round(2.5 - 4 * 0.216, 2)


@pytest.mark.parametrize("grid", [FLAT_GRID, NESTED_GRID], ids=["flat", "nested"])
async def test_daily_report_grid_totals_read_both_layouts(solar, fake_hass, monkeypatch, grid):
    _stub_prefs(solar, monkeypatch, [{"type": "solar", "stat_energy_from": "sensor.solar_total"}, grid])
    totals = {"sensor.solar_total": 12.0, "sensor.grid_import_total_energy": 3.5,
              "sensor.grid_export_total_energy": 6.25}

    async def _sum(hass, eid):
        return totals.get(eid)
    monkeypatch.setattr(solar, "_daily_sum", _sum)
    monkeypatch.setattr(solar, "_forecast_values", lambda hass, prefer=None: {})
    for eid in totals:
        fake_hass.states.set(eid, "0", unit_of_measurement="kWh")

    async def _cost(hass, imported, exported):
        return None, "unavailable"
    monkeypatch.setattr(solar, "_cost_today", _cost)
    out = await solar.daily_report(fake_hass)
    assert out["imported_kwh"] == 3.5 and out["exported_kwh"] == 6.25


# ── energy_flow.py reads both layouts ───────────────────────────────────────

@pytest.mark.parametrize("grid", [FLAT_GRID, NESTED_GRID], ids=["flat", "nested"])
def test_energy_flow_reads_both_layouts(ef, monkeypatch, grid):
    _stub_prefs(ef, monkeypatch, [{"type": "solar", "stat_rate": "sensor.solar_power"}, grid])
    out = asyncio.run(ef.energy_flow_status(_Hass(_exporting_states())))
    assert out["grid"] == {"w": 650, "state": "exporting"}
    assert out["house"] == {"w": 2550}
    assert out["direction_source"] == "totals"


def test_energy_flow_sums_several_grid_rates(ef, monkeypatch):
    grid = {**NESTED_GRID, "power": [{"stat_rate": "sensor.phase_1"}, {"stat_rate": "sensor.phase_2"}]}
    _stub_prefs(ef, monkeypatch, [grid])
    states = _exporting_states()
    states.update({"sensor.phase_1": _State("400"), "sensor.phase_2": _State("250")})
    out = asyncio.run(ef.energy_flow_status(_Hass(states)))
    assert out["grid"] == {"w": 650, "state": "exporting"}


def test_energy_flow_grid_with_one_rate_unavailable_reads_none(ef, monkeypatch):
    grid = {**NESTED_GRID, "power": [{"stat_rate": "sensor.phase_1"}, {"stat_rate": "sensor.phase_2"}]}
    _stub_prefs(ef, monkeypatch, [grid])
    states = _exporting_states()
    states["sensor.phase_1"] = _State("400")
    out = asyncio.run(ef.energy_flow_status(_Hass(states)))
    assert out["grid"] == {"w": None, "state": None} and out["house"] == {"w": None}


def test_energy_flow_grid_with_neither_layout_reads_none(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, [{"type": "grid"}])
    out = asyncio.run(ef.energy_flow_status(_Hass()))
    assert out["configured"] is True
    assert out["grid"] == {"w": None, "state": None}
