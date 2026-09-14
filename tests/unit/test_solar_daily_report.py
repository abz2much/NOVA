"""Tests for solar.py's daily report: today's totals, forecast discovery,
and cost. `_daily_sum`/`_forecast_values`/`_cost_today` are exercised
directly against a stubbed recorder/entity-registry so the reset-safe delta
math and discovery logic are verified without a real Home Assistant
instance.
"""
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def solar(load):
    return load("solar")


class _FakeInstance:
    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def _install_recorder_stub(monkeypatch, history_by_entity):
    rec_mod = types.ModuleType("homeassistant.components.recorder")
    rec_mod.get_instance = lambda hass: _FakeInstance()
    hist_mod = types.SimpleNamespace(
        get_significant_states=lambda hass, start, end, ids, **kw: {
            eid: history_by_entity.get(eid, []) for eid in ids
        }
    )
    rec_mod.history = hist_mod
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", rec_mod)


def _install_fixed_clock(monkeypatch, now):
    dt_mod = sys.modules["homeassistant.util.dt"]
    monkeypatch.setattr(dt_mod, "utcnow", lambda: now)
    monkeypatch.setattr(dt_mod, "as_local", lambda v: v)
    monkeypatch.setattr(dt_mod, "as_utc", lambda v: v, raising=False)


# ── _daily_sum: reset-safe delta ─────────────────────────────────────────────

async def test_daily_sum_simple_increase(solar, fake_hass, monkeypatch):
    now = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
    _install_fixed_clock(monkeypatch, now)
    _install_recorder_stub(monkeypatch, {
        "sensor.solar_energy": [{"state": "5.0"}, {"state": "12.5"}, {"state": "19.19"}],
    })
    result = await solar._daily_sum(fake_hass, "sensor.solar_energy")
    assert result == pytest.approx(14.19)


async def test_daily_sum_handles_meter_reset(solar, fake_hass, monkeypatch):
    now = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
    _install_fixed_clock(monkeypatch, now)
    # counter resets partway through the day (e.g. device reboot) -- the drop
    # itself must not be read as negative consumption.
    _install_recorder_stub(monkeypatch, {
        "sensor.grid_import": [{"state": "100.0"}, {"state": "104.0"}, {"state": "2.0"}, {"state": "6.5"}],
    })
    result = await solar._daily_sum(fake_hass, "sensor.grid_import")
    assert result == pytest.approx(4.0 + 4.5)


async def test_daily_sum_no_history_returns_none(solar, fake_hass, monkeypatch):
    now = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
    _install_fixed_clock(monkeypatch, now)
    _install_recorder_stub(monkeypatch, {})
    assert await solar._daily_sum(fake_hass, "sensor.brand_new") is None


async def test_daily_sum_no_entity_returns_none(solar, fake_hass):
    assert await solar._daily_sum(fake_hass, "") is None
    assert await solar._daily_sum(fake_hass, None) is None


async def test_daily_sum_recorder_unavailable_returns_none(solar, fake_hass, monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", None)
    assert await solar._daily_sum(fake_hass, "sensor.solar_energy") is None


# ── _forecast_values: discovery by platform + unique_id suffix ──────────────

class _RegEntry:
    def __init__(self, entity_id, platform, unique_id):
        self.entity_id = entity_id
        self.platform = platform
        self.unique_id = unique_id


def test_forecast_values_matches_open_meteo_by_unique_id(solar, fake_hass, monkeypatch):
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    entries = {
        "sensor.home_solar_forecast_energy_production_today": _RegEntry(
            "sensor.home_solar_forecast_energy_production_today",
            "open_meteo_solar_forecast", "ENTRY1_energy_production_today"),
        "sensor.home_solar_forecast_energy_production_today_remaining": _RegEntry(
            "sensor.home_solar_forecast_energy_production_today_remaining",
            "open_meteo_solar_forecast", "ENTRY1_energy_production_today_remaining"),
    }
    monkeypatch.setattr(er_mod, "async_get",
                         lambda hass: types.SimpleNamespace(entities=entries))
    fake_hass.states.set("sensor.home_solar_forecast_energy_production_today", "19.52")
    fake_hass.states.set("sensor.home_solar_forecast_energy_production_today_remaining", "1.2")

    out = solar._forecast_values(fake_hass)
    assert out["forecast_today_total_kwh"] == pytest.approx(19.52)
    assert out["forecast_remaining_kwh"] == pytest.approx(1.2)


def test_forecast_values_ignores_stale_config_entry_link(solar, fake_hass, monkeypatch):
    """Discovery goes by platform + unique_id, not the Energy dashboard's
    config_entry_solar_forecast cross-reference -- so a stale link (pointing
    at a deleted config entry, a live-caught case) doesn't break this."""
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    entries = {
        "sensor.fc_today": _RegEntry("sensor.fc_today", "forecast_solar",
                                      "SOME_OTHER_ENTRY_energy_production_today"),
    }
    monkeypatch.setattr(er_mod, "async_get",
                         lambda hass: types.SimpleNamespace(entities=entries))
    fake_hass.states.set("sensor.fc_today", "20.0")
    out = solar._forecast_values(fake_hass)
    assert out["forecast_today_total_kwh"] == pytest.approx(20.0)


def test_forecast_values_empty_when_no_forecast_integration(solar, fake_hass, monkeypatch):
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er_mod, "async_get",
                         lambda hass: types.SimpleNamespace(entities={}))
    assert solar._forecast_values(fake_hass) == {}


# ── _cost_today: configured entity vs. estimate ──────────────────────────────

async def test_cost_today_prefers_configured_entity(solar, fake_hass, monkeypatch, load):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [])
    nova_config = load("nova_config")
    monkeypatch.setattr(nova_config, "runtime_get", lambda hass, entry, key, default="":
                         {"energy_cost_today_entity": "sensor.electricity_cost_today",
                          "energy_cost_net_entity": "sensor.net_electricity_cost_today"}
                         .get(key, default))
    fake_hass.states.set("sensor.electricity_cost_today", "1.94")
    fake_hass.states.set("sensor.net_electricity_cost_today", "-3.19")

    cost, source = await solar._cost_today(fake_hass, imported_kwh=10.0, exported_kwh=5.0)
    assert source == "configured_entity"
    assert cost == {"today": 1.94, "net_today": -3.19}


async def test_cost_today_falls_back_to_estimate(solar, fake_hass, monkeypatch, load):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [])
    nova_config = load("nova_config")
    monkeypatch.setattr(nova_config, "runtime_get", lambda hass, entry, key, default="": default)

    async def _fake_prefs(hass):
        return {"energy_sources": [{
            "type": "grid",
            "entity_energy_price": "input_number.price",
            "number_energy_price_export": 0.29,
        }]}
    monkeypatch.setattr(solar, "_read_prefs", _fake_prefs)
    fake_hass.states.set("input_number.price", "0.25")

    cost, source = await solar._cost_today(fake_hass, imported_kwh=10.0, exported_kwh=4.0)
    assert source == "estimate"
    assert cost["today"] == pytest.approx(2.5)
    assert cost["net_today"] == pytest.approx(2.5 - 4 * 0.29)


async def test_cost_today_unavailable_without_import_or_price(solar, fake_hass, monkeypatch, load):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [])
    nova_config = load("nova_config")
    monkeypatch.setattr(nova_config, "runtime_get", lambda hass, entry, key, default="": default)
    cost, source = await solar._cost_today(fake_hass, imported_kwh=None, exported_kwh=None)
    assert cost is None
    assert source == "unavailable"


# ── daily_report: end-to-end composition, no solar configured ───────────────

async def test_daily_report_not_configured_when_no_solar_source(solar, fake_hass, monkeypatch):
    async def _fake_prefs(hass):
        return {"energy_sources": []}
    monkeypatch.setattr(solar, "_read_prefs", _fake_prefs)
    result = await solar.daily_report(fake_hass)
    assert result["configured"] is False
