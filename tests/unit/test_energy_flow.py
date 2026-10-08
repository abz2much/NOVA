"""Tests for the Energy tab's live readout (energy_flow.py). Reads the same
energy_sources config Home Assistant's Energy dashboard uses, through
solar.py's helpers. The pure functions are table tested with plain numbers;
a few end to end tests go through energy_flow_status() with fake states, so
the direction really comes from which cumulative total changed last.

Sign conventions on rate sensors are not standardised, so the cases here
deliberately include rates whose sign disagrees with the real direction
(a battery discharging at -2199 W, a grid exporting at +650 W)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def ef(load):
    return load("energy_flow")


_T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

# Battery store and time estimate keys (added in 2c); None without a capacity.
_NO_STORE = {"capacity_kwh": None, "stored_kwh": None, "eta_min": None, "eta_to": None}


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


def _stub_prefs(ef, monkeypatch, prefs):
    async def _fake(hass):
        return prefs
    monkeypatch.setattr(ef, "_read_prefs", _fake)


def _totals(prefix, newer):
    """Two cumulative totals for one source; `newer` ("from" or "to") is the
    one that changed most recently."""
    old, new = _State("100", unit="kWh", last_changed=_T0), \
        _State("100", unit="kWh", last_changed=_T0 + timedelta(seconds=30))
    return {f"sensor.{prefix}_from": new if newer == "from" else old,
            f"sensor.{prefix}_to": new if newer == "to" else old}


def _source(kind, prefix, soc=False):
    src = {"type": kind, "stat_rate": f"sensor.{prefix}_power",
           "stat_energy_from": f"sensor.{prefix}_from",
           "stat_energy_to": f"sensor.{prefix}_to"}
    if soc:
        src["stat_soc"] = f"sensor.{prefix}_soc"
    return src


# ── source_flow: one source's signed watts and how direction was found ──────

@pytest.mark.parametrize("rate, side, expected", [
    (-2199.0, "from", (2199.0, "totals")),   # negative rate, totals say discharge
    (1200.0, "to", (-1200.0, "totals")),     # positive rate, totals say charge
    (650.0, "to", (-650.0, "totals")),       # grid positive while exporting
    (-900.0, None, (-900.0, "sign")),        # no totals: the sign decides
    (900.0, None, (900.0, "sign")),
    (15.0, "from", (0.0, None)),             # under 20 W is idle
    (-19.9, None, (0.0, None)),
    (None, "from", (None, None)),            # unavailable
])
def test_source_flow(ef, rate, side, expected):
    assert ef.source_flow(rate, side) == expected


# ── flow_state: magnitude and state word ────────────────────────────────────

@pytest.mark.parametrize("net, kind, expected", [
    (2199.0, "battery", (2199, "discharging")),
    (-1200.0, "battery", (1200, "charging")),
    (1500.0, "grid", (1500, "importing")),
    (-650.0, "grid", (650, "exporting")),
    (12.0, "grid", (12, "idle")),
    (0.0, "battery", (0, "idle")),
    (None, "grid", (None, None)),
])
def test_flow_state(ef, net, kind, expected):
    assert ef.flow_state(net, kind) == expected


# ── house_power: the energy balance ─────────────────────────────────────────

@pytest.mark.parametrize("solar, battery, grid, cfg, expected", [
    (3200.0, None, None, (True, False, False), 3200),     # solar only
    (3000.0, -1200.0, None, (True, True, False), 1800),   # battery charging
    (0.0, 2199.0, 0.0, (True, True, True), 2199),         # battery discharging
    (3200.0, -450.0, -650.0, (True, True, True), 2100),   # charging and exporting
    (None, None, 1500.0, (False, False, True), 1500),     # grid only
    (3000.0, None, 0.0, (True, True, True), None),        # configured battery unavailable
    (500.0, 0.0, -510.0, (True, True, True), 0),          # small negative clamps to 0
    (100.0, 0.0, -900.0, (True, True, True), None),       # readings disagree
])
def test_house_power(ef, solar, battery, grid, cfg, expected):
    s, b, g = cfg
    assert ef.house_power(solar, battery, grid, solar_configured=s,
                          battery_configured=b, grid_configured=g) == expected


# ── build_status: the whole readout from already read values ───────────────

def test_solar_only(ef):
    out = ef.build_status([3200.0], [], [], [])
    assert out == {
        "configured": True,
        "solar": {"w": 3200},
        "battery": {"w": None, "state": None, "pct": None, **_NO_STORE},
        "grid": {"w": None, "state": None},
        "house": {"w": 3200},
        "direction_source": None,
    }


def test_solar_plus_battery_charging(ef):
    out = ef.build_status([3000.0], [ef.source_flow(1200.0, "to")], [64.0], [])
    assert out["battery"] == {"w": 1200, "state": "charging", "pct": 64.0, **_NO_STORE}
    assert out["house"] == {"w": 1800}
    assert out["direction_source"] == "totals"


def test_battery_discharging_with_a_negative_rate(ef):
    # The real case: battery -2199 W, solar 0, grid 0. Totals say discharge.
    out = ef.build_status([0.0], [ef.source_flow(-2199.0, "from")], [],
                          [ef.source_flow(0.0, None)])
    assert out["battery"]["w"] == 2199 and out["battery"]["state"] == "discharging"
    assert out["grid"] == {"w": 0, "state": "idle"}
    assert out["house"]["w"] == pytest.approx(2200, abs=5)
    assert out["direction_source"] == "totals"


@pytest.mark.parametrize("rate, side, state", [
    (-1500.0, "from", "importing"),
    (650.0, "to", "exporting"),
])
def test_grid_import_and_export(ef, rate, side, state):
    out = ef.build_status([], [], [], [ef.source_flow(rate, side)])
    assert out["configured"] is True
    assert out["grid"]["state"] == state and out["grid"]["w"] == abs(int(rate))


def test_two_battery_sources_net_out(ef):
    flows = [ef.source_flow(800.0, "from"), ef.source_flow(-300.0, "to")]
    out = ef.build_status([], flows, [80.0, 60.0], [])
    assert out["battery"] == {"w": 500, "state": "discharging", "pct": 70.0, **_NO_STORE}


def test_two_batteries_charging_add_up(ef):
    flows = [ef.source_flow(-400.0, "to"), ef.source_flow(-600.0, "to")]
    out = ef.build_status([2000.0], flows, [], [])
    assert out["battery"]["w"] == 1000 and out["battery"]["state"] == "charging"
    assert out["house"]["w"] == 1000


@pytest.mark.parametrize("values, expected", [
    ([82.0], 82.0),
    ([80.0, None, 61.0], 70.5),
    ([], None),
    ([None], None),
])
def test_mean_pct(ef, values, expected):
    assert ef.mean_pct(values) == expected


def test_configured_source_unavailable(ef):
    out = ef.build_status([3000.0], [ef.source_flow(None, None)], [50.0], [])
    assert out["battery"] == {"w": None, "state": None, "pct": 50.0, **_NO_STORE}
    assert out["house"] == {"w": None}
    assert out["solar"] == {"w": 3000}


def test_both_totals_missing_falls_back_to_sign(ef):
    out = ef.build_status([3200.0], [], [], [ef.source_flow(-900.0, None)])
    assert out["grid"] == {"w": 900, "state": "exporting"}
    assert out["direction_source"] == "sign"


def test_any_sign_guess_is_reported_over_totals(ef):
    out = ef.build_status([], [ef.source_flow(500.0, "from")], [],
                          [ef.source_flow(300.0, None)])
    assert out["direction_source"] == "sign"


def test_idle_under_20_watts(ef):
    out = ef.build_status([0.0], [ef.source_flow(15.0, "to")], [], [ef.source_flow(-8.0, None)])
    assert out["battery"] == {"w": 0, "state": "idle", "pct": None, **_NO_STORE}
    assert out["grid"] == {"w": 0, "state": "idle"}
    assert out["direction_source"] is None


def test_unconfigured(ef):
    assert ef.build_status([], [], [], []) == ef.empty_status()
    assert ef.empty_status()["configured"] is False


# ── energy_flow_status: end to end with fake states ─────────────────────────

def test_prefs_missing(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, None)
    assert asyncio.run(ef.energy_flow_status(_Hass())) == ef.empty_status()


def test_no_solar_battery_or_grid_source(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [{"type": "gas", "stat_energy_from": "sensor.gas"}]})
    assert asyncio.run(ef.energy_flow_status(_Hass()))["configured"] is False


def test_grid_only_is_configured(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [_source("grid", "grid")]})
    hass = _Hass({"sensor.grid_power": _State("1.5", unit="kW"), **_totals("grid", "from")})
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["configured"] is True
    assert out["grid"] == {"w": 1500, "state": "importing"}
    assert out["house"] == {"w": 1500}
    assert out["direction_source"] == "totals"


def test_real_case_end_to_end(ef, monkeypatch):
    # Battery rate -2199 W while discharging, solar 0, grid 0.
    _stub_prefs(ef, monkeypatch, {"energy_sources": [
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        _source("battery", "bat", soc=True),
        _source("grid", "grid"),
    ]})
    hass = _Hass({
        "sensor.solar_power": _State("0"),
        "sensor.bat_power": _State("-2199"),
        "sensor.bat_soc": _State("57", unit="%"),
        "sensor.grid_power": _State("0"),
        **_totals("bat", "from"),
        **_totals("grid", "from"),
    })
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["battery"] == {"w": 2199, "state": "discharging", "pct": 57.0, **_NO_STORE}
    assert out["grid"] == {"w": 0, "state": "idle"}
    assert out["house"]["w"] == pytest.approx(2200, abs=5)
    assert out["direction_source"] == "totals"


def test_every_battery_and_grid_source_counts(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        _source("battery", "bat1", soc=True),
        _source("battery", "bat2"),               # no soc sensor
        _source("grid", "grid1"),
        _source("grid", "grid2"),
    ]})
    hass = _Hass({
        "sensor.solar_power": _State("4000"),
        "sensor.bat1_power": _State("700"), **_totals("bat1", "to"),    # charging
        "sensor.bat1_soc": _State("40", unit="%"),
        "sensor.bat2_power": _State("-300"), **_totals("bat2", "to"),   # charging
        "sensor.grid1_power": _State("650"), **_totals("grid1", "to"),  # exporting, positive
        "sensor.grid2_power": _State("150"), **_totals("grid2", "from"),
    })
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["battery"] == {"w": 1000, "state": "charging", "pct": 40.0, **_NO_STORE}
    assert out["grid"] == {"w": 500, "state": "exporting"}
    assert out["house"] == {"w": 2500}


def test_totals_with_the_same_timestamp_fall_back_to_sign(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [_source("grid", "grid")]})
    same = _State("1", unit="kWh", last_changed=_T0)
    hass = _Hass({"sensor.grid_power": _State("-400"),
                  "sensor.grid_from": same, "sensor.grid_to": same})
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["grid"] == {"w": 400, "state": "exporting"}
    assert out["direction_source"] == "sign"


def test_unavailable_rate_end_to_end(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [
        {"type": "solar", "stat_rate": "sensor.solar_power"},
        _source("grid", "grid"),
    ]})
    hass = _Hass({"sensor.solar_power": _State("1000"),
                  "sensor.grid_power": _State("unavailable"), **_totals("grid", "from")})
    out = asyncio.run(ef.energy_flow_status(hass))
    assert out["solar"] == {"w": 1000}
    assert out["grid"] == {"w": None, "state": None}
    assert out["house"] == {"w": None}


def test_a_read_failure_is_flagged_as_an_error(ef, monkeypatch):
    async def boom(hass):
        raise RuntimeError("prefs exploded")
    monkeypatch.setattr(ef, "_read_prefs", boom)
    out = asyncio.run(ef.energy_flow_status(_Hass()))
    assert out == {**ef.empty_status(), "error": True}
    assert out["configured"] is False


def test_a_failure_reading_a_state_is_flagged_as_an_error(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, {"energy_sources": [{"type": "solar", "stat_rate": "sensor.solar_power"}]})

    class _Broken:
        @property
        def states(self):
            raise RuntimeError("state machine gone")
    assert asyncio.run(ef.energy_flow_status(_Broken()))["error"] is True


def test_normal_results_carry_no_error_key(ef, monkeypatch):
    _stub_prefs(ef, monkeypatch, None)
    assert "error" not in asyncio.run(ef.energy_flow_status(_Hass()))
