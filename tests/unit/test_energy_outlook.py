"""Tests for the energy outlook (energy_outlook.py).

The fixtures are one real household's shapes, kept here and never in the
code: a time of use tariff (0.0859 from 02:00 to 06:00, 0.2146 from 23:00 to
02:00 and 06:00 to 08:00, 0.4047 from 08:00 to 17:00 and 19:00 to 23:00,
0.4538 from 17:00 to 19:00, export 0.216 flat), a 10 kWh battery, and about
17 kWh of use a day, higher from 17:00 to 22:00.

The pure functions are tested with plain data. energy_outlook_status() is
run with its readers stubbed, so the statuses, the error shape, the cache
and the single flight lock are checked without Home Assistant."""
import ast
import asyncio
import pathlib
import sys
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/Dublin")
FIRST_DAY = date(2026, 10, 7)          # a Wednesday; 7 days to Tuesday 13th
NOW_DAY = date(2026, 10, 14)           # a Wednesday


@pytest.fixture
def eo(load):
    mod = load("energy_outlook")
    mod._cache.clear()
    mod._lock = None
    yield mod
    mod._cache.clear()
    mod._lock = None


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def tou_price(minute):
    h = minute / 60
    if 2 <= h < 6:
        return 0.0859
    if 17 <= h < 19:
        return 0.4538
    if 8 <= h < 17 or 19 <= h < 23:
        return 0.4047
    return 0.2146


TOU_TABLE = [tou_price(s * 30) for s in range(48)]


def usual_hour(h):
    if h < 6:
        return 0.35
    if 17 <= h < 22:
        return 1.4
    return 0.6


PROFILE = {"status": "ok", "days": 21,
           "profile": {"weekday": [usual_hour(h) for h in range(24)],
                       "weekend": [usual_hour(h) for h in range(24)]}}
TOU = {"status": "ok", "days": 7, "tables": {"all": TOU_TABLE}}
EXPORT = {"status": "flat", "days": 0, "tables": {"all": [0.216] * 48}}


def history(price_fn, first_day=FIRST_DAY, days=7):
    """Half hourly price samples, as recorder history would give them."""
    out = []
    t = at(first_day, 0)
    for _ in range(days * 48):
        out.append((t, price_fn(t)))
        t = (t.astimezone(timezone.utc) + timedelta(minutes=30)).astimezone(TZ)
    return out


def solar_day(eo, day, total_kwh):
    """A day's forecast spread between 08:00 and 18:30."""
    hours = [at(day, h) for h in range(24)]
    return eo.spread_daily(total_kwh, at(day, 8), at(day, 18, 30), hours)


def solar_days(eo, *totals, first=NOW_DAY):
    out = {}
    for i, total in enumerate(totals):
        out.update(solar_day(eo, first + timedelta(days=i), total))
    return out


def outlook(eo, now, *, tariff=TOU, solar=None, shape="hourly", stored=5.0, capacity=10.0,
            today_home=None, factor=(0.85, "default", 0), profile=PROFILE):
    return eo.build_outlook(now, tariff, EXPORT, profile, solar or {}, shape, factor,
                            capacity, stored, today_home, "EUR")


# ── a. tariff learned from history ─────────────────────────────────────────

def test_tariff_learned_from_seven_days(eo):
    samples = history(lambda t: tou_price(t.hour * 60 + t.minute))
    tariff = eo.tariff_table_from_history(samples, FIRST_DAY, 7, TZ)
    assert tariff["status"] == "ok" and tariff["days"] == 7
    assert tariff["tables"] == {"all": TOU_TABLE}
    bands = eo.bands_from_table(tariff["tables"]["all"])
    assert [(b["start"], b["end"], b["price"]) for b in bands] == [
        ("00:00", "02:00", 0.2146), ("02:00", "06:00", 0.0859), ("06:00", "08:00", 0.2146),
        ("08:00", "17:00", 0.4047), ("17:00", "19:00", 0.4538), ("19:00", "23:00", 0.4047),
        ("23:00", "24:00", 0.2146)]
    assert len({b["price"] for b in bands}) == 4
    # Thirds of 0.0859 to 0.4538: low up to 0.2085, high from 0.3312. So the
    # night rate is low, the day shoulder mid, both day rates high.
    labels = {b["price"]: b["label"] for b in bands}
    assert labels == {0.0859: "low", 0.2146: "mid", 0.4047: "high", 0.4538: "high"}


def test_outlook_bands_merge_over_midnight(eo):
    bands = eo.outlook_bands(TOU, NOW_DAY, TZ)
    late = [b for b in bands if b["start"] == at(NOW_DAY, 23).isoformat()]
    assert late and late[0]["end"] == at(NOW_DAY + timedelta(days=1), 2).isoformat()
    assert bands[-1]["end"] == at(NOW_DAY + timedelta(days=2), 0).isoformat()


def test_hourly_price_is_mean_of_half_hours(eo):
    tariff = {"status": "ok", "days": 7, "tables": {"all": [0.1] * 3 + [0.3] + [0.1] * 44}}
    assert eo.hourly_prices(tariff, [at(NOW_DAY, 0), at(NOW_DAY, 1)]) == [0.1, 0.2]


# ── b. learning and flat ───────────────────────────────────────────────────

def test_fewer_than_three_days_is_learning(eo):
    samples = history(lambda t: tou_price(t.hour * 60), first_day=date(2026, 10, 12), days=2)
    tariff = eo.tariff_table_from_history(samples, FIRST_DAY, 7, TZ)
    assert tariff == {"status": "learning", "days": 2, "tables": {}}


def test_unavailable_gaps_do_not_count_as_days(eo):
    samples = history(lambda t: None if t.day == 9 else tou_price(t.hour * 60))
    assert eo.tariff_table_from_history(samples, FIRST_DAY, 7, TZ)["days"] == 6


def test_price_that_never_changes_is_flat_and_gives_no_tariff_advice(eo):
    tariff = eo.tariff_table_from_history(history(lambda t: 0.30), FIRST_DAY, 7, TZ)
    assert tariff["status"] == "flat"
    out = outlook(eo, at(NOW_DAY, 19), tariff=tariff, solar=solar_days(eo, 0, 4), stored=3.0)
    assert out["status"] == "flat"
    assert not [a for a in out["advice"] if a["kind"] in ("battery_hold", "cheap_topup")]
    assert out["best_window"] is None       # no spare solar after dark, one price
    assert "one price all day" in " ".join(out["messages"])


# ── c. weekday and weekend split ───────────────────────────────────────────

def test_weekend_table_only_when_it_really_differs(eo):
    def weekend_cheap(t):
        if t.weekday() >= 5 and 10 <= t.hour < 14:
            return 0.10
        return tou_price(t.hour * 60 + t.minute)
    split = eo.tariff_table_from_history(history(weekend_cheap), FIRST_DAY, 7, TZ)
    assert set(split["tables"]) == {"weekday", "weekend"}
    assert eo.table_for(split, date(2026, 10, 17))[20] == 0.10      # Saturday 10:00
    assert eo.table_for(split, date(2026, 10, 14))[20] == 0.4047    # Wednesday 10:00

    def weekend_one_hour(t):  # 2 slots differ: under the 4 slot rule
        if t.weekday() >= 5 and t.hour == 12:
            return 0.10
        return tou_price(t.hour * 60 + t.minute)
    one = eo.tariff_table_from_history(history(weekend_one_hour), FIRST_DAY, 7, TZ)
    assert set(one["tables"]) == {"all"}


# ── d. usual usage ─────────────────────────────────────────────────────────

def _hours(days, end=at(NOW_DAY, 0)):
    end = end.astimezone(timezone.utc)
    return [end - timedelta(hours=i) for i in range(days * 24, 0, -1)]


def test_load_profile_needs_seven_days(eo):
    six = {h: 0.5 for h in _hours(6)}
    assert eo.load_profile(six, TZ) == {"status": "learning", "days": 6, "profile": {}}
    seven = eo.load_profile({h: 0.5 for h in _hours(7)}, TZ)
    assert seven["status"] == "ok" and seven["days"] == 7
    assert seven["profile"]["weekday"] == [0.5] * 24


def test_home_hours_balance_and_skips(eo):
    h1, h2, h3, h4 = _hours(1)[:4]
    series = {
        "solar": [{h1: 2.0, h2: 0.0, h3: 0.0, h4: 0.0}],
        "import": [{h1: 0.1, h2: 0.5, h3: 0.0, h4: 0.01}],
        "export": [{h1: 0.5, h3: 2.0, h4: 0.0}],          # h2 missing: skip h2
        "discharge": [{h1: 0.0, h2: 0.0, h3: 0.0, h4: 0.0}],
        "charge": [{h1: 1.0, h2: 0.0, h3: 0.0, h4: 0.04}],
    }
    home = eo.home_hours(series)
    assert home[h1] == pytest.approx(2.0 + 0.1 - 0.5 - 1.0)
    assert h2 not in home                   # a source has no reading
    assert h3 not in home                   # -2 kWh: the readings disagree
    assert home[h4] == 0.0                  # -0.03 kWh is noise, reads as 0


def test_home_hours_skip_unconverted_wh(eo):
    h = _hours(1)[0]
    # A Wh total the units argument failed to convert reads 1000 times too big.
    assert eo.home_hours({"import": [{h: 1500.0}]}) == {}


def test_hourly_changes_ask_for_kwh_and_use_the_executor(eo, monkeypatch):
    calls = {}

    def fake_stats(hass, start, end, ids, period, units, types_):
        calls.update(ids=ids, period=period, units=units, types=types_)
        t0 = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
        return {"sensor.import_wh": [{"start": t0.timestamp(), "change": 1.25},
                                     {"start": (t0 + timedelta(hours=1)).timestamp(), "change": None}]}

    jobs = []

    class _Recorder:
        async def async_add_executor_job(self, fn):
            jobs.append(fn)
            return fn()

    rec = types.ModuleType("homeassistant.components.recorder")
    rec.get_instance = lambda hass: _Recorder()
    stats = types.ModuleType("homeassistant.components.recorder.statistics")
    stats.statistics_during_period = fake_stats
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", rec)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder.statistics", stats)

    out = asyncio.run(eo._hourly_changes(object(), ["sensor.import_wh"],
                                         at(NOW_DAY, 0), at(NOW_DAY, 1)))
    assert calls == {"ids": {"sensor.import_wh"}, "period": "hour",
                     "units": {"energy": "kWh"}, "types": {"change"}}
    assert len(jobs) == 1
    assert out == {"sensor.import_wh": {datetime(2026, 10, 13, 10, tzinfo=timezone.utc): 1.25}}


# ── e. forecast factor ─────────────────────────────────────────────────────

def test_forecast_factor(eo):
    pairs = [(8.0, 10.0), (9.0, 10.0), (7.0, 10.0), (10.0, 10.0), (6.0, 10.0)]
    assert eo.forecast_factor(pairs) == (0.8, "learned", 5)
    assert eo.forecast_factor(pairs[:4]) == (0.85, "default", 4)
    assert eo.forecast_factor([(30.0, 10.0)] * 5) == (1.2, "learned", 5)
    assert eo.forecast_factor([(1.0, 10.0)] * 5) == (0.5, "learned", 5)
    # A dull day under 2 kWh of forecast says nothing about accuracy.
    assert eo.forecast_factor(pairs[:4] + [(0.1, 1.5)] * 3) == (0.85, "default", 4)


def test_morning_values_read_before_production(eo):
    day = date(2026, 10, 13)
    samples = [(at(day, 0, 10), 12.0), (at(day, 4, 59), 14.5), (at(day, 13), 9.0)]
    assert eo.morning_values(samples, [day, date(2026, 10, 12)], TZ) == {day: 14.5}


def test_daily_totals_need_a_whole_day(eo):
    hours = _hours(2)
    full = {h: 0.5 for h in hours}
    short = {h: 0.5 for h in hours[24:]} | {h: 0.5 for h in hours[:10]}
    totals = eo.daily_totals([full], TZ)
    assert set(totals.values()) == {12.0}
    assert eo.daily_totals([full, short], TZ) == {date(2026, 10, 13): 24.0}


# ── f. simulate ────────────────────────────────────────────────────────────

def test_simulate_charges_exports_discharges_and_imports(eo):
    start = at(NOW_DAY, 10)
    solar = [6.0, 6.0, 0.0, 0.0, 0.0]
    load = [1.0, 1.0, 3.0, 3.0, 3.0]
    pts = eo.simulate(start, 5.0, 10.0, 10, [0.4] * 5, [0.2] * 5, solar, load, 0.9)
    # Hour 0: 5 kWh surplus, room 5 / 0.9 = 5.556: all 5 charge, nothing exported.
    assert pts[0]["charge_kwh"] == 5.0 and pts[0]["grid_export_kwh"] == 0.0
    assert pts[0]["soc_pct"] == 95.0
    # Hour 1: only 0.5 kWh of room (0.556 in): the rest is exported.
    assert pts[1]["soc_pct"] == 100.0
    assert pts[1]["grid_export_kwh"] == pytest.approx(5.0 - 0.5 / 0.9, abs=1e-3)
    # Then 9 kWh above the 1 kWh floor: 3 + 3 + 3, the last hour imports nothing.
    assert [p["discharge_kwh"] for p in pts[2:]] == [3.0, 3.0, 3.0]
    assert pts[4]["soc_pct"] == 10.0
    more = eo.simulate(start, 1.5, 10.0, 10, [0.4], [0.2], [0.0], [2.0], 0.9)
    assert more[0]["discharge_kwh"] == 0.5 and more[0]["grid_import_kwh"] == 1.5
    assert more[0]["cost"] == pytest.approx(0.6)
    for p in pts + more:   # energy balance every hour
        assert p["solar_kwh"] + p["grid_import_kwh"] + p["discharge_kwh"] == pytest.approx(
            p["load_kwh"] + p["grid_export_kwh"] + p["charge_kwh"], abs=2e-3)


def test_simulate_without_capacity_has_no_battery(eo):
    pts = eo.simulate(at(NOW_DAY, 10), None, None, 10, [0.4] * 2, [None] * 2,
                      [3.0, 0.0], [1.0, 1.0], 0.9)
    assert pts[0]["grid_export_kwh"] == 2.0 and pts[1]["grid_import_kwh"] == 1.0
    assert pts[0]["soc_pct"] is None


def test_simulate_hours_step_through_a_clock_change(eo):
    pts = eo.simulate(at(date(2026, 10, 25), 0), 5, 10, 10, [0.1] * 4, [None] * 4,
                      [0] * 4, [0] * 4)
    times = [datetime.fromisoformat(p["t"]) for p in pts]
    assert [t.hour for t in times] == [0, 1, 1, 2]       # 01:00 happens twice
    assert len({t.astimezone(timezone.utc) for t in times}) == 4


# ── g. cheap top up ────────────────────────────────────────────────────────

def test_cheap_topup_fires_before_a_dull_day(eo):
    out = outlook(eo, at(NOW_DAY, 19), solar=solar_days(eo, 0, 4), stored=3.0)
    topup = [a for a in out["advice"] if a["kind"] == "cheap_topup"]
    assert topup, out["advice"]
    a = topup[0]
    assert a["when"] == at(NOW_DAY + timedelta(days=1), 2).isoformat()
    assert a["kwh"] == 9.0                                   # all the room above the floor
    assert a["saving"] >= eo.MIN_SAVING
    assert a["message"].startswith("Tomorrow looks dull, about 3.4 kWh of sun against your usual 17")
    assert "between 02:00 and 06:00" in a["message"]
    assert a["key"] == "cheap_topup:2026-10-14"


def test_cheap_topup_silent_on_a_sunny_day(eo):
    out = outlook(eo, at(NOW_DAY, 19), solar=solar_days(eo, 0, 22), stored=3.0)
    assert not [a for a in out["advice"] if a["kind"] == "cheap_topup"]


def test_cheap_topup_silent_when_the_saving_is_small(eo):
    narrow = {"status": "ok", "days": 7,
              "tables": {"all": [0.20 if 4 <= s < 12 else 0.21 for s in range(48)]}}
    out = outlook(eo, at(NOW_DAY, 19), tariff=narrow, solar=solar_days(eo, 0, 4), stored=3.0)
    assert not [a for a in out["advice"] if a["kind"] == "cheap_topup"]


def test_cheap_topup_only_in_the_evening(eo):
    out = outlook(eo, at(NOW_DAY, 15), solar=solar_days(eo, 0, 4), stored=3.0)
    assert not [a for a in out["advice"] if a["kind"] == "cheap_topup"]


# ── h. battery hold ────────────────────────────────────────────────────────

def test_battery_hold_silent_for_the_small_gap(eo):
    # At 14:00 on 0.4047 the only dearer band is 0.4538: about 0.05 apart.
    out = outlook(eo, at(NOW_DAY, 14), solar=solar_days(eo, 0.5, 0.5), stored=4.0)
    assert any(p["grid_import_kwh"] > 0 and p["price"] == 0.4538 for p in out["points"])
    assert not [a for a in out["advice"] if a["kind"] == "battery_hold"]


def test_battery_hold_fires_when_the_later_band_is_much_dearer(eo):
    dear = {"status": "ok", "days": 7,
            "tables": {"all": [0.60 if 34 <= s < 38 else 0.15 for s in range(48)]}}
    out = outlook(eo, at(NOW_DAY, 14), tariff=dear, solar=solar_days(eo, 0.5, 0.5), stored=4.0)
    hold = [a for a in out["advice"] if a["kind"] == "battery_hold"]
    assert hold, out["advice"]
    assert hold[0]["when"] == at(NOW_DAY, 17).isoformat()
    # Kept = what 14:00 to 17:00 would draw from the battery (a little sun
    # helps), well under the 3 kWh above the floor and the 2.8 kWh in the band.
    drawn = sum(p["discharge_kwh"] for p in out["points"][:3])
    assert 1.5 < drawn < 1.8
    assert hold[0]["kwh"] == round(drawn, 1)
    assert hold[0]["saving"] == pytest.approx(drawn * 0.45 * 0.9, abs=0.01)
    assert "from 17:00 to 19:00" in hold[0]["message"]


def test_battery_hold_silent_at_the_floor(eo):
    dear = {"status": "ok", "days": 7,
            "tables": {"all": [0.60 if 34 <= s < 38 else 0.15 for s in range(48)]}}
    out = outlook(eo, at(NOW_DAY, 14), tariff=dear, solar=solar_days(eo, 0.5, 0.5), stored=1.0)
    assert not [a for a in out["advice"] if a["kind"] == "battery_hold"]


# ── i. best window ─────────────────────────────────────────────────────────

def test_best_window_uses_spare_solar_on_a_sunny_day(eo):
    out = outlook(eo, at(NOW_DAY, 8), solar=solar_days(eo, 22, 22), stored=9.0)
    bw = out["best_window"]
    assert bw["reason"] == "spare solar"
    start = datetime.fromisoformat(bw["start"])
    assert start.date() == NOW_DAY and 9 <= start.hour <= 15


def test_best_window_overnight_is_the_cheap_band(eo):
    out = outlook(eo, at(NOW_DAY, 20), solar=solar_days(eo, 0, 4), stored=3.0)
    assert out["best_window"] == {
        "start": at(NOW_DAY + timedelta(days=1), 2).isoformat(),
        "end": at(NOW_DAY + timedelta(days=1), 4).isoformat(),
        "reason": "cheapest rate", "price": 0.0859}


def test_best_window_ties_go_to_the_earlier_start(eo):
    pts = eo.simulate(at(NOW_DAY, 0), None, None, 10, [0.2, 0.1, 0.1, 0.1, 0.2],
                      [None] * 5, [0] * 5, [1] * 5)
    assert eo.best_window(pts, 2)["start"] == at(NOW_DAY, 1).isoformat()
    assert eo.best_window(pts, 3)["start"] == at(NOW_DAY, 1).isoformat()


def test_high_use_today(eo):
    # By 12:30 the usual is 6 x 0.35 + 6.5 x 0.6 = 6.0 kWh.
    now = at(NOW_DAY, 12, 30)
    assert eo.advice_high_use(now, 8.0, PROFILE) is None          # 33 percent over
    hi = eo.advice_high_use(now, 9.0, PROFILE)
    assert hi["level"] == "info" and hi["saving"] is None
    assert eo.advice_high_use(at(NOW_DAY, 2), 2.0, PROFILE) is None  # 0.7 usual: under 1.5 kWh over


# ── statuses and messages ──────────────────────────────────────────────────

def test_learning_still_shows_the_bands(eo):
    learning = {"status": "learning", "days": 3, "profile": {}}
    out = outlook(eo, at(NOW_DAY, 19), profile=learning, solar=solar_days(eo, 0, 4))
    assert out["status"] == "learning"
    assert out["bands"] and out["points"] == [] and out["advice"] == []
    assert "Learning your usual usage: 3 of 7 days." in out["messages"]


def test_tariff_learning_has_no_prices(eo):
    out = outlook(eo, at(NOW_DAY, 19), tariff={"status": "learning", "days": 1, "tables": {}},
                  solar=solar_days(eo, 0, 4))
    assert out["status"] == "learning" and out["bands"] == []
    assert all(p["price"] is None for p in out["points"])
    assert out["best_window"] is None
    assert "Learning your tariff times: 1 of 3 days." in out["messages"]


def test_no_forecast_and_no_capacity(eo):
    out = outlook(eo, at(NOW_DAY, 19), shape="none", capacity=None, stored=None)
    assert out["status"] == "no_forecast"
    assert not [a for a in out["advice"] if a["kind"] in ("battery_hold", "cheap_topup")]
    assert all(p["solar_kwh"] == 0 and p["soc_pct"] is None for p in out["points"])
    assert len(out["messages"]) == 2
    out = outlook(eo, at(NOW_DAY, 19), solar=solar_days(eo, 0, 4), capacity=None)
    assert out["status"] == "no_capacity"


def test_points_and_bands_cover_the_horizon(eo):
    out = outlook(eo, at(NOW_DAY, 19, 40), solar=solar_days(eo, 0, 4))
    assert len(out["points"]) == eo.HORIZON_H
    assert out["bands"][-1]["end"] >= out["points"][-1]["t"]
    assert out["bands"][-1]["end"] == at(NOW_DAY + timedelta(days=3), 0).isoformat()
    assert out["points"][0]["t"] == at(NOW_DAY, 19).isoformat()
    assert out["learned"] == {"tariff_days": 7, "load_days": 21, "forecast_factor": 0.85,
                              "factor_source": "default", "forecast_shape": "hourly"}


def test_user_text_has_no_hyphens(eo):
    texts = list(eo._MESSAGES.values())
    for out in (outlook(eo, at(NOW_DAY, 19), solar=solar_days(eo, 0, 4), stored=3.0,
                        today_home=20.0),):
        texts += [a["title"] + " " + a["message"] for a in out["advice"]]
    assert texts and not [t for t in texts if "-" in t]


# ── j, k. the Home Assistant side ──────────────────────────────────────────

class _Hass:
    def __init__(self):
        self.config = types.SimpleNamespace(time_zone="Europe/Dublin", currency="EUR")
        self.services = types.SimpleNamespace(has_service=lambda d, s: False)
        self.states = types.SimpleNamespace(get=lambda eid: None)


GRID = {"type": "grid", "stat_energy_from": "sensor.imp", "stat_energy_to": "sensor.exp",
        "entity_energy_price": "sensor.price", "number_energy_price_export": 0.216}
BATTERY = {"type": "battery", "stat_energy_from": "sensor.dis", "stat_energy_to": "sensor.chg",
           "stat_soc": "sensor.soc", "capacity": 10}
SOLAR = {"type": "solar", "stat_energy_from": "sensor.pv"}


@pytest.fixture
def stubbed(eo, load, monkeypatch):
    ef = load("energy_flow")
    state = {"prefs": [GRID, BATTERY, SOLAR], "capacity": 10.0, "stats_error": False,
             "shape": "hourly"}

    async def prefs(hass):
        return {"energy_sources": state["prefs"]} if state["prefs"] is not None else None

    async def hist(hass, eid, start, end):
        return history(lambda t: tou_price(t.hour * 60 + t.minute))

    async def changes(hass, ids, start, end):
        if state["stats_error"]:
            raise eo._ReadError("recorder down")
        hours = _hours(21)
        return {e: {h: (usual_hour(h.astimezone(TZ).hour) if e == "sensor.imp" else 0.0)
                    for h in hours} for e in ids}

    async def forecast(hass, now, tz):
        if state["shape"] == "none":
            return {}, "none"
        return solar_days(eo, 0, 4), state["shape"]

    async def flow(hass):
        return {"battery": {"capacity_kwh": state["capacity"],
                            "stored_kwh": 3.0 if state["capacity"] else None}}

    async def today(hass):
        return {"home_kwh": 9.0}

    monkeypatch.setattr(eo, "_read_prefs", prefs)
    monkeypatch.setattr(eo, "_state_history", hist)
    monkeypatch.setattr(eo, "_hourly_changes", changes)
    monkeypatch.setattr(eo, "_forecast_hourly", forecast)
    monkeypatch.setattr(eo, "_forecast_today_entity", lambda hass: None)
    monkeypatch.setattr(eo, "_now", lambda hass: (at(NOW_DAY, 19), TZ))
    monkeypatch.setattr(ef, "energy_flow_status", flow)
    monkeypatch.setattr(ef, "energy_flow_today", today)
    return state


def test_status_ok(eo, stubbed):
    out = asyncio.run(eo.energy_outlook_status(_Hass()))
    assert out["configured"] and not out["error"]
    assert out["status"] == "ok" and out["currency"] == "EUR"
    assert len(out["points"]) == eo.HORIZON_H
    assert [a["kind"] for a in out["advice"]][0] == "cheap_topup"
    assert out["learned"]["tariff_days"] == 7 and out["learned"]["load_days"] == 21


def test_status_not_configured_without_a_grid(eo, stubbed):
    stubbed["prefs"] = [SOLAR]
    out = asyncio.run(eo.energy_outlook_status(_Hass()))
    assert out["configured"] is False and out["error"] is False
    stubbed["prefs"] = None
    eo._cache.clear()
    assert asyncio.run(eo.energy_outlook_status(_Hass()))["configured"] is False


def test_status_no_capacity_and_no_forecast(eo, stubbed):
    stubbed["capacity"] = None
    out = asyncio.run(eo.energy_outlook_status(_Hass()))
    assert out["status"] == "no_capacity" and not out["error"]
    eo._cache.clear()
    stubbed["capacity"], stubbed["shape"] = 10.0, "none"
    out = asyncio.run(eo.energy_outlook_status(_Hass()))
    assert out["status"] == "no_forecast" and not out["error"]


def test_recorder_error_is_an_error_not_unconfigured(eo, stubbed):
    stubbed["stats_error"] = True
    out = asyncio.run(eo.energy_outlook_status(_Hass()))
    assert out["error"] is True and out["configured"] is True
    assert out["status"] == "unavailable"


def test_state_history_failure_raises_read_error(eo):
    # No recorder at all (here: the module is not installed): a read error.
    with pytest.raises(eo._ReadError):
        asyncio.run(eo._state_history(_Hass(), "sensor.price", at(NOW_DAY, 0), at(NOW_DAY, 1)))


def test_cache_and_single_flight(eo, monkeypatch):
    runs = []
    clock = [1000.0]

    async def compute(hass, now, tz):
        runs.append(1)
        await asyncio.sleep(0.01)
        return {**eo.empty_outlook(), "configured": True, "status": "ok"}

    monkeypatch.setattr(eo, "_compute", compute)
    monkeypatch.setattr(eo, "_now", lambda hass: (at(NOW_DAY, 19), TZ))
    monkeypatch.setattr(eo, "_clock", lambda: clock[0])

    async def both():
        return await asyncio.gather(eo.energy_outlook_status(_Hass()),
                                    eo.energy_outlook_status(_Hass()))
    a, b = asyncio.run(both())
    assert len(runs) == 1 and a == b
    clock[0] += eo.CACHE_TTL_S - 1
    asyncio.run(eo.energy_outlook_status(_Hass()))
    assert len(runs) == 1
    clock[0] += 2
    asyncio.run(eo.energy_outlook_status(_Hass()))
    assert len(runs) == 2


def test_errors_are_not_cached(eo, monkeypatch):
    runs = []

    async def compute(hass, now, tz):
        runs.append(1)
        raise eo._ReadError("down")

    monkeypatch.setattr(eo, "_compute", compute)
    monkeypatch.setattr(eo, "_now", lambda hass: (at(NOW_DAY, 19), TZ))
    asyncio.run(eo.energy_outlook_status(_Hass()))
    asyncio.run(eo.energy_outlook_status(_Hass()))
    assert len(runs) == 2


def test_cached_outlook_answers_another_window_length(eo, stubbed):
    two = asyncio.run(eo.energy_outlook_status(_Hass()))
    three = asyncio.run(eo.energy_outlook_status(_Hass(), hours=3))
    assert two["best_window"]["start"] == three["best_window"]["start"]
    assert three["best_window"]["end"] != two["best_window"]["end"]


# ── l. forecast fallbacks ──────────────────────────────────────────────────

class _Entry:
    def __init__(self, platform, uid, entry_id="abc", entity_id="sensor.fc_today"):
        self.platform, self.unique_id = platform, uid
        self.config_entry_id, self.entity_id = entry_id, entity_id


def test_forecast_uses_get_forecast_when_registered(eo, monkeypatch):
    calls = []
    hass = _Hass()
    hass.services = types.SimpleNamespace(has_service=lambda d, s: (d, s) == ("forecast_solar", "get_forecast"))

    async def call(domain, service, data, blocking=False, return_response=False):
        calls.append((domain, service, data, blocking, return_response))
        return {"watts": {}, "wh_period": {"2026-10-14T10:00:00+01:00": 500,
                                           "2026-10-14T11:00:00+01:00": 1500}}
    hass.services.async_call = call
    monkeypatch.setattr(eo, "_forecast_entries", lambda h: [_Entry("forecast_solar", "abc-energy_production_today")])
    out, shape = asyncio.run(eo._forecast_hourly(hass, at(NOW_DAY, 9, 20), TZ))
    assert shape == "hourly"
    assert out == {datetime(2026, 10, 14, 9, tzinfo=timezone.utc): 0.5,
                   datetime(2026, 10, 14, 10, tzinfo=timezone.utc): 1.5}
    d = calls[0][2]
    assert calls[0][3:] == (True, True)
    assert d["resolution"] == "hourly" and d["config_entry"] == "abc"
    assert d["start"] == at(NOW_DAY, 9).isoformat()
    assert d["end"] == at(NOW_DAY + timedelta(days=2), 9).isoformat()


def test_forecast_falls_back_to_the_energy_platform(eo, monkeypatch):
    energy = types.SimpleNamespace()

    async def get_solar_forecast(hass, entry_id):
        return {"wh_hours": {"2026-10-14T12:00:00+01:00": 2000}}
    energy.async_get_solar_forecast = get_solar_forecast

    class _Integration:
        async def async_get_platform(self, name):
            assert name == "energy"
            return energy

    async def get_integration(hass, domain):
        return _Integration()
    loader = types.ModuleType("homeassistant.loader")
    loader.async_get_integration = get_integration
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader)
    monkeypatch.setattr(eo, "_forecast_entries",
                        lambda h: [_Entry("open_meteo_solar_forecast", "x-energy_production_today")])
    out, shape = asyncio.run(eo._forecast_hourly(_Hass(), at(NOW_DAY, 9), TZ))
    assert shape == "hourly"
    assert out == {datetime(2026, 10, 14, 11, tzinfo=timezone.utc): 2.0}


def test_forecast_falls_back_to_an_estimate(eo, monkeypatch):
    loader = types.ModuleType("homeassistant.loader")

    async def no_integration(hass, domain):
        raise RuntimeError("no energy platform")
    loader.async_get_integration = no_integration
    sun = types.ModuleType("homeassistant.helpers.sun")
    sun.get_astral_event_date = lambda hass, event, day: at(day, 8) if event == "sunrise" else at(day, 18)
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.sun", sun)
    monkeypatch.setattr(eo, "_forecast_entries", lambda h: [_Entry("forecast_solar", "x-energy_production_today")])
    monkeypatch.setattr(eo, "_forecast_values",
                        lambda h: {"forecast_remaining_kwh": 3.0, "forecast_tomorrow_kwh": 10.0})
    out, shape = asyncio.run(eo._forecast_hourly(_Hass(), at(NOW_DAY, 13), TZ))
    assert shape == "estimated"
    today = sum(v for k, v in out.items() if k.astimezone(TZ).date() == NOW_DAY)
    tomorrow = sum(v for k, v in out.items() if k.astimezone(TZ).date() > NOW_DAY)
    assert today == pytest.approx(3.0) and tomorrow == pytest.approx(10.0)
    assert min(k.astimezone(TZ).hour for k in out if k.astimezone(TZ).date() == NOW_DAY) == 13
    assert all(8 <= k.astimezone(TZ).hour < 18 for k in out)


def test_no_forecast_integration(eo, monkeypatch):
    monkeypatch.setattr(eo, "_forecast_entries", lambda h: [])
    assert asyncio.run(eo._forecast_hourly(_Hass(), at(NOW_DAY, 9), TZ)) == ({}, "none")


def test_only_service_call_is_the_read_only_forecast():
    """Advice only: the module's one service call is the forecast lookup."""
    src = pathlib.Path(__file__).parents[2] / "custom_components/nova/energy_outlook.py"
    calls = [n for n in ast.walk(ast.parse(src.read_text()))
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "async_call"]
    assert len(calls) == 1
    assert [a.value for a in calls[0].args[:2]] == ["forecast_solar", "get_forecast"]
