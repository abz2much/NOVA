"""Which solar forecast Nova reads, and how (8.13.0).

Solcast PV Forecast (BJReplay/ha-solcast-solar, v4.6) is checked against
its real shapes: its sensors use the bare key as unique_id
(total_kwh_forecast_today, get_remaining_today, total_kwh_forecast_tomorrow,
in kWh), and its energy platform returns {"wh_hours": {...}} keyed by the
start of each 30 minute period in UTC, each value the period's Wh
(round(kW * 500), forecast.py make_energy_dict).

Also covered: the Energy dashboard's config_entry_solar_forecast link is
preferred for any integration, a stale link falls back to discovery, two
installed integrations are never added together, and an install with none
is unchanged. Everything here is read only."""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fakes import FakeHass

TZ = ZoneInfo("Europe/Dublin")
NOW = datetime(2026, 10, 14, 9, 20, tzinfo=TZ)


class _Reg:
    def __init__(self, entity_id, platform, unique_id, entry_id):
        self.entity_id, self.platform, self.unique_id = entity_id, platform, unique_id
        self.config_entry_id = entry_id


SOLCAST = [
    _Reg("sensor.solcast_pv_forecast_forecast_today", "solcast_solar", "total_kwh_forecast_today", "sol1"),
    _Reg("sensor.solcast_pv_forecast_forecast_remaining_today", "solcast_solar", "get_remaining_today", "sol1"),
    _Reg("sensor.solcast_pv_forecast_forecast_tomorrow", "solcast_solar", "total_kwh_forecast_tomorrow", "sol1"),
    # Other Solcast sensors share the platform but are not daily totals.
    _Reg("sensor.solcast_pv_forecast_forecast_next_hour", "solcast_solar", "forecast_next_hour", "sol1"),
    _Reg("sensor.solcast_pv_forecast_forecast_day_3", "solcast_solar", "total_kwh_forecast_d3", "sol1"),
]
FORECAST_SOLAR = [  # two arrays: one config entry each
    _Reg("sensor.east_today", "forecast_solar", "fsA_energy_production_today", "fsA"),
    _Reg("sensor.east_remaining", "forecast_solar", "fsA_energy_production_today_remaining", "fsA"),
    _Reg("sensor.east_tomorrow", "forecast_solar", "fsA_energy_production_tomorrow", "fsA"),
    _Reg("sensor.west_today", "forecast_solar", "fsB_energy_production_today", "fsB"),
    _Reg("sensor.west_remaining", "forecast_solar", "fsB_energy_production_today_remaining", "fsB"),
    _Reg("sensor.west_tomorrow", "forecast_solar", "fsB_energy_production_tomorrow", "fsB"),
]
STATES = {
    "sensor.solcast_pv_forecast_forecast_today": "18.4",
    "sensor.solcast_pv_forecast_forecast_remaining_today": "11.25",
    "sensor.solcast_pv_forecast_forecast_tomorrow": "6.1",
    "sensor.solcast_pv_forecast_forecast_next_hour": "900",
    "sensor.solcast_pv_forecast_forecast_day_3": "20",
    "sensor.east_today": "8", "sensor.east_remaining": "5", "sensor.east_tomorrow": "4",
    "sensor.west_today": "6", "sensor.west_remaining": "2", "sensor.west_tomorrow": "3",
}


@pytest.fixture
def solar(load):
    return load("solar")


@pytest.fixture
def eo(load):
    mod = load("energy_outlook")
    mod._cache.clear()
    mod._learned_cache.clear()
    yield mod
    mod._cache.clear()
    mod._learned_cache.clear()


def _registry(monkeypatch, entries):
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er_mod, "async_get", lambda hass: types.SimpleNamespace(
        entities={e.entity_id: e for e in entries}))


def _hass(entries_by_id=None):
    hass = FakeHass()
    for eid, val in STATES.items():
        hass.states.set(eid, val, unit_of_measurement="kWh", device_class="energy")
    known = entries_by_id or {}
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda entry_id: known.get(entry_id),
        async_entries=lambda domain=None: [])
    return hass


def _prefs(*links):
    return {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv",
                                "config_entry_solar_forecast": list(links)}]}


def _solcast_wh_hours():
    """Two hours of Solcast's energy platform output: 30 minute periods."""
    t = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    kw = [1.0, 1.4, 2.0, 2.2]          # pv_estimate per half hour, in kW
    return {"wh_hours": {(t + timedelta(minutes=30 * i)).isoformat(): round(v * 500, 0)
                         for i, v in enumerate(kw)}}


def _loader(monkeypatch, platforms):
    """homeassistant.loader with an energy platform per domain; records calls."""
    calls = []

    class _Integration:
        def __init__(self, domain):
            self.domain = domain

        async def async_get_platform(self, name):
            assert name == "energy"
            if self.domain not in platforms:
                raise ImportError(f"{self.domain} has no energy platform")
            data = platforms[self.domain]

            async def get(hass, entry_id):
                calls.append((self.domain, entry_id))
                return data(entry_id) if callable(data) else data
            return types.SimpleNamespace(async_get_solar_forecast=get)

    async def get_integration(hass, domain):
        return _Integration(domain)
    loader = types.ModuleType("homeassistant.loader")
    loader.async_get_integration = get_integration
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader)
    return calls


# ── daily totals through discovery ─────────────────────────────────────────

def test_solcast_daily_sensors_by_their_real_keys(solar, monkeypatch):
    _registry(monkeypatch, SOLCAST)
    assert solar._forecast_values(_hass()) == {
        "forecast_today_total_kwh": 18.4, "forecast_remaining_kwh": 11.25,
        "forecast_tomorrow_kwh": 6.1}


def test_two_arrays_of_one_integration_are_added(solar, monkeypatch):
    _registry(monkeypatch, FORECAST_SOLAR)
    assert solar._forecast_values(_hass()) == {
        "forecast_today_total_kwh": 14.0, "forecast_remaining_kwh": 7.0,
        "forecast_tomorrow_kwh": 7.0}


def test_two_integrations_are_never_added_together(solar, monkeypatch):
    """With no dashboard link the first in _FORECAST_PLATFORMS order wins
    (Forecast.Solar before Solcast); a link to Solcast makes it win."""
    _registry(monkeypatch, SOLCAST + FORECAST_SOLAR)
    assert solar._forecast_values(_hass())["forecast_today_total_kwh"] == 14.0
    assert solar._forecast_values(_hass(), "solcast_solar")["forecast_today_total_kwh"] == 18.4
    # A preference for an integration with no sensors falls back to the order.
    assert solar._forecast_values(_hass(), "some_other")["forecast_today_total_kwh"] == 14.0


def test_no_forecast_integration_is_unchanged(solar, monkeypatch):
    _registry(monkeypatch, [])
    assert solar._forecast_values(_hass()) == {}
    assert solar._forecast_choice(_hass()) == (None, [])


def test_dashboard_links_any_integration_and_skips_stale_ones(solar):
    hass = _hass({"sol1": types.SimpleNamespace(domain="solcast_solar"),
                  "x9": types.SimpleNamespace(domain="some_forecast")})
    assert solar._linked_forecast_entries(hass, _prefs("gone", "sol1", "x9", "sol1")) == [
        ("solcast_solar", "sol1"), ("some_forecast", "x9")]
    assert solar._linked_forecast_entries(hass, None) == []
    assert solar._linked_forecast_entries(hass, _prefs("gone")) == []


# ── hourly forecast ────────────────────────────────────────────────────────

def test_solcast_hourly_through_the_linked_energy_platform(eo, monkeypatch):
    _registry(monkeypatch, SOLCAST)
    calls = _loader(monkeypatch, {"solcast_solar": _solcast_wh_hours()})
    hass = _hass({"sol1": types.SimpleNamespace(domain="solcast_solar")})
    out, shape = asyncio.run(eo._forecast_hourly(hass, NOW, TZ, _prefs("sol1")))
    assert shape == "hourly" and calls == [("solcast_solar", "sol1")]
    # Half hours fold into their hour: (500 + 700) Wh and (1000 + 1100) Wh.
    assert out == pytest.approx({datetime(2026, 10, 14, 9, tzinfo=timezone.utc): 1.2,
                                 datetime(2026, 10, 14, 10, tzinfo=timezone.utc): 2.1})


def test_solcast_hourly_through_discovery_when_the_link_is_stale(eo, monkeypatch):
    _registry(monkeypatch, SOLCAST)
    calls = _loader(monkeypatch, {"solcast_solar": _solcast_wh_hours()})
    out, shape = asyncio.run(eo._forecast_hourly(_hass(), NOW, TZ, _prefs("deleted-entry")))
    assert shape == "hourly" and calls == [("solcast_solar", "sol1")]
    assert sum(out.values()) == pytest.approx(3.3)


def test_any_linked_integration_with_an_energy_platform_is_used(eo, monkeypatch):
    _registry(monkeypatch, [])                     # no sensors Nova knows
    calls = _loader(monkeypatch, {"some_forecast": {"wh_hours": {"2026-10-14T12:00:00+01:00": 2500}}})
    hass = _hass({"x9": types.SimpleNamespace(domain="some_forecast")})
    out, shape = asyncio.run(eo._forecast_hourly(hass, NOW, TZ, _prefs("x9")))
    assert shape == "hourly" and calls == [("some_forecast", "x9")]
    assert out == {datetime(2026, 10, 14, 11, tzinfo=timezone.utc): 2.5}


def test_two_integrations_use_one_and_add_its_entries(eo, monkeypatch):
    _registry(monkeypatch, SOLCAST + FORECAST_SOLAR)
    calls = _loader(monkeypatch, {
        "forecast_solar": lambda entry_id: {"wh_hours": {"2026-10-14T12:00:00+01:00": 1000}},
        "solcast_solar": _solcast_wh_hours()})
    out, shape = asyncio.run(eo._forecast_hourly(_hass(), NOW, TZ, None))
    assert shape == "hourly"
    assert calls == [("forecast_solar", "fsA"), ("forecast_solar", "fsB")]   # never Solcast
    assert out == {datetime(2026, 10, 14, 11, tzinfo=timezone.utc): 2.0}     # both arrays


def test_linked_integration_wins_over_discovery_order(eo, monkeypatch):
    _registry(monkeypatch, SOLCAST + FORECAST_SOLAR)
    calls = _loader(monkeypatch, {"forecast_solar": {"wh_hours": {}},
                                  "solcast_solar": _solcast_wh_hours()})
    hass = _hass({"sol1": types.SimpleNamespace(domain="solcast_solar")})
    _, shape = asyncio.run(eo._forecast_hourly(hass, NOW, TZ, _prefs("sol1")))
    assert shape == "hourly" and calls == [("solcast_solar", "sol1")]


def test_no_forecast_at_all_is_unchanged(eo, monkeypatch):
    _registry(monkeypatch, [])
    _loader(monkeypatch, {})
    assert asyncio.run(eo._forecast_hourly(_hass(), NOW, TZ, _prefs())) == ({}, "none")


def test_today_sensors_for_the_accuracy_factor_follow_the_choice(eo, monkeypatch):
    _registry(monkeypatch, SOLCAST + FORECAST_SOLAR)
    assert eo._forecast_today_entities(_hass()) == ["sensor.east_today", "sensor.west_today"]
    assert eo._forecast_today_entities(_hass(), "solcast_solar") == [
        "sensor.solcast_pv_forecast_forecast_today"]
    _registry(monkeypatch, [])
    assert eo._forecast_today_entities(_hass()) == []


def test_accuracy_factor_adds_the_arrays_forecasts(eo, monkeypatch):
    """Two arrays forecast 8 and 2 kWh each morning; the panels made 6 kWh
    each day, so the factor is 6 / 10."""
    async def history(hass, eid, start, end):
        return [(start - timedelta(days=1), {"sensor.east_today": 8.0, "sensor.west_today": 2.0}[eid])]

    async def changes(hass, ids, start, end):
        hours = [(end - timedelta(hours=i)).astimezone(timezone.utc) for i in range(21 * 24, 0, -1)]
        return {e: {h: 0.25 for h in hours} for e in ids}
    monkeypatch.setattr(eo, "_state_history", history)
    monkeypatch.setattr(eo, "_hourly_changes", changes)
    monkeypatch.setattr(eo, "_clock", lambda: 0.0)
    grid = {"import_price": (None, 0.3), "export_price": (None, None), "imports": ["sensor.imp"],
            "exports": [], "rates": []}
    learned = asyncio.run(eo._learned(_hass(), NOW, TZ, grid, [grid], [], ["sensor.pv"],
                                      ["sensor.east_today", "sensor.west_today"]))
    assert learned["factor"] == (0.6, "learned", 14)
