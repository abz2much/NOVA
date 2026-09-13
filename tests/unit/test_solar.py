"""Tests for solar/battery/grid visibility (v7.91.0). Reads the same
energy_sources config Home Assistant's own Energy dashboard uses, so the
coverage here focuses on: no solar source configured (the honest default,
not an error), reading live values off whatever entities the config points
at, summing multiple solar sources, a battery with no state-of-charge sensor
degrading gracefully instead of erroring, grid import/export direction, and
the self-sufficiency calculation."""
import asyncio

import pytest


@pytest.fixture
def solar(load):
    return load("solar")


class _State:
    def __init__(self, state, unit="W"):
        self.state = state
        self.attributes = {"unit_of_measurement": unit}


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


def _prefs(sources):
    return {"energy_sources": sources}


def _stub_prefs(solar, monkeypatch, prefs):
    async def _fake(hass):
        return prefs
    monkeypatch.setattr(solar, "_read_prefs", _fake)


# ── not configured ───────────────────────────────────────────────────────────

def test_not_configured_when_no_prefs(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, None)
    out = asyncio.run(solar.solar_status(_Hass()))
    assert out["configured"] is False
    assert out["solar_w"] is None
    assert "Energy dashboard" in out["advice"][0]


def test_not_configured_when_no_solar_source(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([{"type": "grid", "stat_rate": "sensor.grid_power"}]))
    out = asyncio.run(solar.solar_status(_Hass()))
    assert out["configured"] is False


# ── solar + battery + grid together ──────────────────────────────────────────

def test_full_picture_with_export(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        {"type": "battery", "stat_rate": "sensor.battery_power", "stat_soc": "sensor.battery_soc"},
        {"type": "grid", "stat_rate": "sensor.grid_power"},
    ]))
    hass = _Hass({
        "sensor.solar_power": _State("3200"),
        "sensor.battery_power": _State("-450"),
        "sensor.battery_soc": _State("82", unit="%"),
        "sensor.grid_power": _State("-650"),
    })
    out = asyncio.run(solar.solar_status(hass))
    assert out["configured"] is True
    assert out["solar_w"] == 3200
    assert out["battery_pct"] == 82.0
    assert out["grid_direction"] == "export"
    assert out["grid_w"] == -650
    # not importing at all -> fully self-sufficient
    assert out["self_sufficiency_pct"] == 100.0


def test_grid_import_lowers_self_sufficiency(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        {"type": "grid", "stat_rate": "sensor.grid_power"},
    ]))
    hass = _Hass({
        "sensor.solar_power": _State("1000"),
        "sensor.grid_power": _State("1000"),  # importing
    })
    out = asyncio.run(solar.solar_status(hass))
    assert out["grid_direction"] == "import"
    assert out["self_sufficiency_pct"] == 50.0


def test_small_grid_reading_is_balanced(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        {"type": "grid", "stat_rate": "sensor.grid_power"},
    ]))
    hass = _Hass({
        "sensor.solar_power": _State("500"),
        "sensor.grid_power": _State("0.2"),
    })
    out = asyncio.run(solar.solar_status(hass))
    assert out["grid_direction"] == "balanced"


# ── multiple solar sources summed ────────────────────────────────────────────

def test_multiple_solar_sources_summed(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.array_1_power"},
        {"type": "solar", "stat_rate": "sensor.array_2_power"},
    ]))
    hass = _Hass({
        "sensor.array_1_power": _State("1200"),
        "sensor.array_2_power": _State("800"),
    })
    out = asyncio.run(solar.solar_status(hass))
    assert out["solar_w"] == 2000


# ── battery without a state-of-charge sensor ─────────────────────────────────

def test_battery_without_soc_degrades_gracefully(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        {"type": "battery", "stat_rate": "sensor.battery_power"},  # no stat_soc key at all
    ]))
    hass = _Hass({
        "sensor.solar_power": _State("1000"),
        "sensor.battery_power": _State("300"),
    })
    out = asyncio.run(solar.solar_status(hass))
    assert out["battery_w"] == 300
    assert out["battery_pct"] is None


# ── kW-unit sensors are converted, like energy.py already does ──────────────

def test_kw_unit_sensor_converted_to_watts(solar, monkeypatch):
    _stub_prefs(solar, monkeypatch, _prefs([
        {"type": "solar", "stat_rate": "sensor.solar_power"},
    ]))
    hass = _Hass({"sensor.solar_power": _State("3.2", unit="kW")})
    out = asyncio.run(solar.solar_status(hass))
    assert out["solar_w"] == 3200
