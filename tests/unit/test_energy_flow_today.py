"""Tests for the Energy tab's battery store and time estimate (in
nova/energy_flow "status") and today's totals (the "today" action).

The pure functions are table tested with plain numbers. energy_flow_today()
is run with solar._daily_sum stubbed, so the source aggregation, the None
rules and the 60 second cache are checked without a recorder."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def ef(load):
    mod = load("energy_flow")
    mod._today_cache.clear()
    yield mod
    mod._today_cache.clear()


_T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


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


def _stub_prefs(ef, monkeypatch, sources):
    async def _fake(hass):
        return None if sources is None else {"energy_sources": sources}
    monkeypatch.setattr(ef, "_read_prefs", _fake)


def _stub_totals(ef, monkeypatch, totals):
    calls = []

    async def _sum(hass, eid):
        calls.append(eid)
        return totals.get(eid)
    monkeypatch.setattr(ef, "_daily_sum", _sum)
    return calls


# ── battery_store ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("packs, expected", [
    ([(10.0, 54.0)], (10.0, 5.4)),
    ([(10.0, 50.0), (5.0, 100.0)], (15.0, 10.0)),     # every source counts
    ([(10.0, 54.0), (5.0, None)], (None, None)),      # one pack has no percentage
    ([(10.0, 54.0), (None, 80.0)], (None, None)),     # one pack has no capacity
    ([(0.0, 54.0)], (None, None)),                    # a zero capacity is not real
    ([(10.0, 104.0)], (10.0, 10.0)),                  # percent is clamped to 100
    ([], (None, None)),
])
def test_battery_store(ef, packs, expected):
    assert ef.battery_store(packs) == expected


# ── battery_eta ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cap, stored, w, state, expected", [
    (10.0, 5.4, 2199, "discharging", (145, "empty")),   # 5.4 / 2.199 h = 147.3 min -> 145
    (10.0, 5.4, 2000, "charging", (140, "full")),       # 4.6 / 2.0 h = 138 min -> 140
    (10.0, 5.4, 12, "idle", (None, None)),
    (10.0, 5.4, None, None, (None, None)),               # power unknown
    (None, None, 2199, "discharging", (None, None)),     # no capacity
    (10.0, 9.0, 30, "discharging", (None, None)),        # 300 h: over 48 h
    (10.0, 10.0, 500, "charging", (None, None)),         # already full: nothing to show
    (10.0, 5.0, 2500, "discharging", (120, "empty")),
])
def test_battery_eta(ef, cap, stored, w, state, expected):
    assert ef.battery_eta(cap, stored, w, state) == expected


def test_status_carries_the_battery_store_and_eta(ef, monkeypatch):
    # The real case: a 10 kWh battery ("capacity": 10) discharging at 2199 W.
    _stub_prefs(ef, monkeypatch, [{
        "type": "battery", "capacity": 10, "stat_rate": "sensor.bat_power",
        "stat_soc": "sensor.bat_soc",
        "stat_energy_from": "sensor.bat_out", "stat_energy_to": "sensor.bat_in",
    }])
    hass = _Hass({
        "sensor.bat_power": _State("-2199"), "sensor.bat_soc": _State("54", unit="%"),
        "sensor.bat_out": _State("1", "kWh", _T0 + timedelta(seconds=30)),
        "sensor.bat_in": _State("1", "kWh", _T0),
    })
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["battery"] == {"w": 2199, "state": "discharging", "pct": 54.0,
                              "capacity_kwh": 10.0, "stored_kwh": 5.4,
                              "eta_min": 145, "eta_to": "empty"}


def test_status_with_a_capacity_but_no_soc_sensor_gives_no_store(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, [{"type": "battery", "capacity": "10",
                                   "stat_rate": "sensor.bat_power"}])
    out = asyncio.run(ef.energy_flow_status(_Hass({"sensor.bat_power": _State("500")})))
    assert out["battery"]["capacity_kwh"] is None and out["battery"]["eta_min"] is None


@pytest.mark.parametrize("raw, expected", [(10, 10.0), ("12.5", 12.5), (None, None),
                                           ("big", None), (0, None), (-3, None)])
def test_capacity_is_read_as_kwh_or_not_at_all(ef, raw, expected):
    assert ef._capacity_kwh({"capacity": raw}) == expected


# ── home_today, self_sufficiency, build_today ───────────────────────────────

@pytest.mark.parametrize("parts, expected", [
    ((12.0, 3.0, 5.0, 4.0, 2.5), 8.5),          # 12 + 3 + 2.5 - 5 - 4
    ((0.0, 7.2, 0.0, 0.0, 0.0), 7.2),           # grid only
    ((5.0, 0.0, 5.02, 0.0, 0.0), 0.0),          # tiny negative clamps to 0
    ((1.0, 0.0, 6.0, 0.0, 0.0), None),          # totals disagree
    ((None, 3.0, 5.0, 4.0, 2.5), None),
    ((12.0, 3.0, None, 4.0, 2.5), None),
])
def test_home_today(ef, parts, expected):
    got = ef.home_today(*parts)
    assert got == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("grid_import, home, expected", [
    (3.0, 8.5, 64.7),
    (0.0, 8.5, 100.0),
    (10.0, 8.5, 0.0),     # clamped
    (3.0, 0.0, None),
    (None, 8.5, None),
    (3.0, None, None),
])
def test_self_sufficiency(ef, grid_import, home, expected):
    assert ef.self_sufficiency(grid_import, home) == expected


def test_build_today_rounds_and_combines(ef):
    assert ef.build_today(12.0, 3.0, 5.0, 4.0, 2.5) == {
        "configured": True, "solar_kwh": 12.0, "grid_import_kwh": 3.0,
        "grid_export_kwh": 5.0, "battery_charged_kwh": 4.0,
        "battery_discharged_kwh": 2.5, "home_kwh": 8.5, "self_sufficiency_pct": 64.7,
    }


# ── energy_flow_today: aggregation and None rules ───────────────────────────

_SOURCES = [
    {"type": "solar", "stat_energy_from": "sensor.pv_east"},
    {"type": "solar", "stat_energy_from": "sensor.pv_west"},
    {"type": "battery", "stat_energy_from": "sensor.bat1_out", "stat_energy_to": "sensor.bat1_in"},
    {"type": "battery", "stat_energy_from": "sensor.bat2_out", "stat_energy_to": "sensor.bat2_in"},
    {"type": "grid", "stat_energy_from": "sensor.grid_in", "stat_energy_to": "sensor.grid_out"},
]
_TOTALS = {"sensor.pv_east": 7.0, "sensor.pv_west": 5.0,
           "sensor.bat1_out": 1.5, "sensor.bat1_in": 3.0,
           "sensor.bat2_out": 1.0, "sensor.bat2_in": 1.0,
           "sensor.grid_in": 3.0, "sensor.grid_out": 5.0}


async def test_today_sums_every_source_of_each_type(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, _SOURCES)
    _stub_totals(ef, monkeypatch, _TOTALS)
    out = await ef.energy_flow_today(_Hass())
    assert out == {
        "configured": True, "solar_kwh": 12.0, "grid_import_kwh": 3.0,
        "grid_export_kwh": 5.0, "battery_charged_kwh": 4.0,
        "battery_discharged_kwh": 2.5, "home_kwh": 8.5, "self_sufficiency_pct": 64.7,
    }


async def test_today_reads_the_nested_grid_layout(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, [_SOURCES[0], {
        "type": "grid", "cost_adjustment_day": 0.0, "power": [],
        "flow_from": [{"stat_energy_from": "sensor.grid_in"}, {"stat_energy_from": "sensor.grid_in2"}],
        "flow_to": [{"stat_energy_to": "sensor.grid_out"}],
    }])
    _stub_totals(ef, monkeypatch, {**_TOTALS, "sensor.grid_in2": 0.5})
    out = await ef.energy_flow_today(_Hass())
    assert out["grid_import_kwh"] == 3.5 and out["grid_export_kwh"] == 5.0
    assert out["battery_charged_kwh"] == 0.0     # not configured counts as 0
    assert out["home_kwh"] == 5.5


@pytest.mark.parametrize("missing, key", [
    ("sensor.pv_west", "solar_kwh"),
    ("sensor.grid_in", "grid_import_kwh"),
    ("sensor.grid_out", "grid_export_kwh"),
    ("sensor.bat2_in", "battery_charged_kwh"),
    ("sensor.bat1_out", "battery_discharged_kwh"),
])
async def test_today_one_unreadable_total_makes_its_value_and_home_none(ef, monkeypatch, missing, key):
    _stub_prefs(ef, monkeypatch, _SOURCES)
    _stub_totals(ef, monkeypatch, {k: v for k, v in _TOTALS.items() if k != missing})
    out = await ef.energy_flow_today(_Hass())
    assert out[key] is None
    assert out["home_kwh"] is None and out["self_sufficiency_pct"] is None
    assert out["configured"] is True


async def test_today_unconfigured_and_prefs_missing(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, None)
    _stub_totals(ef, monkeypatch, {})
    assert await ef.energy_flow_today(_Hass()) == ef.empty_today()
    ef._today_cache.clear()
    _stub_prefs(ef, monkeypatch, [{"type": "gas", "stat_energy_from": "sensor.gas"}])
    assert (await ef.energy_flow_today(_Hass()))["configured"] is False


async def test_today_never_raises_and_flags_the_error(ef, monkeypatch):
    async def boom(hass):
        raise RuntimeError("prefs exploded")
    monkeypatch.setattr(ef, "_read_prefs", boom)
    out = await ef.energy_flow_today(_Hass())
    assert out == {**ef.empty_today(), "error": True}
    assert ef._today_cache == {}              # an error is not cached


# ── the 60 second cache ─────────────────────────────────────────────────────

async def test_today_is_cached_for_60_seconds(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, _SOURCES)
    calls = _stub_totals(ef, monkeypatch, _TOTALS)
    clock = [1000.0]
    monkeypatch.setattr(ef, "_clock", lambda: clock[0])
    first = await ef.energy_flow_today(_Hass())
    n = len(calls)
    assert n == 8
    for step in (5, 30, 59.9):
        clock[0] = 1000.0 + step
        assert await ef.energy_flow_today(_Hass()) == first
    assert len(calls) == n                     # no recorder reads inside 60 s
    clock[0] = 1060.0
    await ef.energy_flow_today(_Hass())
    assert len(calls) == 2 * n                 # read again after 60 s


async def test_a_cached_result_cannot_be_changed_by_the_caller(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, _SOURCES)
    _stub_totals(ef, monkeypatch, _TOTALS)
    first = await ef.energy_flow_today(_Hass())
    first["solar_kwh"] = -1
    assert (await ef.energy_flow_today(_Hass()))["solar_kwh"] == 12.0
