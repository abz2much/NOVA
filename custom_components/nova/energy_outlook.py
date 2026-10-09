"""
Nova energy outlook: the next 36 hours of tariff, solar and usage, and a few
pieces of plain advice. Advice only: nothing here changes any device.

Three inputs, all read from Home Assistant's own Energy dashboard config
through solar.py's helpers, so nothing names an entity, a brand, a currency
or a tariff:

  - the tariff schedule, learned from the grid price entity's recorder
    history (the most common price in each half hour over the last 7 days);
  - the hourly solar forecast, from an installed forecast integration;
  - the usual home use by hour, from the recorder's long term statistics of
    the Energy dashboard's own totals (solar + import + battery discharge -
    export - battery charge, per hour, the same balance energy_flow uses).

It learns only two things: that usage profile, and how far the forecast
usually over or under shoots (the forecast factor). Both live in memory and
are rebuilt from the recorder, so there is no store of its own.

A simple hourly battery simulation over those inputs drives four kinds of
advice: hold the battery for dearer hours, top up in the cheap window
before a dull day, the best time for a big appliance, and an unusually high
use today. Band labels (low, mid, high) come from the price alone, so it
works for any tariff.

The pure functions take plain data and return plain data. The Home
Assistant side is at the bottom. energy_outlook_status() is the only entry
point and it never raises.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from bisect import bisect_right
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .solar import (
    _FORECAST_PLATFORMS, _energy_to_kwh, _forecast_entries, _forecast_values, _grid_sensors,
    _live_pct, _read_prefs, _resolve_price,
)

_LOGGER = logging.getLogger(__name__)

# Battery is never planned below this share of its capacity.
FLOOR_PCT = 10
# Round trip efficiency, applied when the battery charges.
EFFICIENCY = 0.9
# How far ahead the simulation looks, in hours.
HORIZON_H = 36
# The smallest saving worth a piece of advice, in the household's currency.
MIN_SAVING = 0.20

# Tariff learning: days of price history read, and the least that counts.
TARIFF_DAYS = 7
TARIFF_MIN_DAYS = 3
SLOTS = 48
# Weekday and weekend get their own tables only when this many slots differ.
SPLIT_MIN_SLOTS = 4

# Usage learning: days of statistics read, and the least that counts.
LOAD_DAYS = 21
LOAD_MIN_DAYS = 7
# A home total below this (kWh) is rounding noise and reads as 0.
LOAD_NOISE_KWH = 0.05
# One source moving more than this in one hour is a bad reading.
MAX_HOUR_KWH = 100.0

# Forecast accuracy: days compared, the least that counts, the smallest
# forecast day that counts, the clamp, the cautious default, and the local
# hour the day's forecast is read at (before production starts).
FACTOR_DAYS = 14
FACTOR_MIN_DAYS = 5
FACTOR_MIN_FORECAST_KWH = 2.0
FACTOR_MIN, FACTOR_MAX = 0.5, 1.2
FACTOR_DEFAULT = 0.85
FACTOR_HOUR = 5

# Cheap top up advice is only given from this local hour.
TOPUP_FROM_HOUR = 18
# Unusually high use today: this share and this many kWh above usual.
HIGH_USE_RATIO = 1.4
HIGH_USE_MIN_KWH = 1.5

CACHE_TTL_S = 300.0
_cache: dict = {}
_lock: Optional[asyncio.Lock] = None


# ── time helpers ────────────────────────────────────────────────────────────

def hour_starts(start: datetime, n: int) -> list[datetime]:
    """n hourly local times from start, stepped in UTC so a clock change
    does not repeat or skip an hour."""
    tz = start.tzinfo
    base = start.astimezone(timezone.utc)
    return [(base + timedelta(hours=i)).astimezone(tz) for i in range(n)]


def _local_at(day: date, minutes: int, tz) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=minutes)


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def _value_at(times: list[float], values: list, t: datetime):
    """The value in effect at t, from sorted UTC timestamps. None before the
    first sample."""
    i = bisect_right(times, t.timestamp()) - 1
    return values[i] if i >= 0 else None


# ── tariff ──────────────────────────────────────────────────────────────────

def _mode(values: list[float]) -> float:
    """The most common value. A tie goes to the value seen first, and
    callers pass the newest day first."""
    return Counter(values).most_common(1)[0][0]


def tariff_table_from_history(samples: list[tuple[datetime, Optional[float]]],
                              first_day: date, days: int, tz) -> dict:
    """A 48 slot price table from price history.

    samples are (time, price) in time order, the first one in effect at the
    start (None for an unavailable state). A day counts only when every one
    of its 48 slots has a price, read at the middle of the slot. Each slot
    takes its most common price over the days that count. Weekday and
    weekend get separate tables only when they differ in SPLIT_MIN_SLOTS or
    more slots.

    Returns {"status": "ok" | "learning" | "flat", "days": n, "tables":
    {"all": [...]} or {"weekday": [...], "weekend": [...]}}. tables is empty
    while learning."""
    times = [t.timestamp() for t, _ in samples]
    values = [v for _, v in samples]
    by_day: list[tuple[date, list[float]]] = []
    for d in range(days):
        day = first_day + timedelta(days=d)
        row = [_value_at(times, values, _local_at(day, s * 30 + 15, tz)) for s in range(SLOTS)]
        if all(v is not None for v in row):
            by_day.append((day, row))
    by_day.reverse()  # newest first, so a tie in the mode goes to the newest

    if len(by_day) < TARIFF_MIN_DAYS:
        return {"status": "learning", "days": len(by_day), "tables": {}}

    def table(rows):
        return [_mode([r[s] for r in rows]) for s in range(SLOTS)]

    tables = {"all": table([r for _, r in by_day])}
    weekdays = [r for d, r in by_day if not _is_weekend(d)]
    weekends = [r for d, r in by_day if _is_weekend(d)]
    if weekdays and weekends:
        wd, we = table(weekdays), table(weekends)
        if sum(a != b for a, b in zip(wd, we)) >= SPLIT_MIN_SLOTS:
            tables = {"weekday": wd, "weekend": we}
    flat = len({p for t in tables.values() for p in t}) == 1
    return {"status": "flat" if flat else "ok", "days": len(by_day), "tables": tables}


def flat_tariff(price: Optional[float]) -> Optional[dict]:
    """A one price tariff, for a grid set up with a fixed number."""
    if price is None:
        return None
    return {"status": "flat", "days": 0, "tables": {"all": [float(price)] * SLOTS}}


def table_for(tariff: Optional[dict], day: date) -> Optional[list]:
    """The day's 48 slot table, or None while learning or unknown."""
    tables = (tariff or {}).get("tables") or {}
    if "all" in tables:
        return tables["all"]
    return tables.get("weekend" if _is_weekend(day) else "weekday")


def label_price(price: float, lo: float, hi: float) -> str:
    """low, mid or high: the bottom, middle or top third of the day's price
    range. A day with one price is all low."""
    span = hi - lo
    if span <= 0 or price <= lo + span / 3:
        return "low"
    if price >= hi - span / 3:
        return "high"
    return "mid"


def bands_from_table(table: list[float]) -> list[dict]:
    """Runs of equal price through one day: {start, end, price, label} with
    "HH:MM" times (the last band ends at "24:00")."""
    lo, hi = min(table), max(table)
    out: list[dict] = []
    for s, price in enumerate(table):
        if out and out[-1]["price"] == price:
            out[-1]["end_slot"] = s + 1
            continue
        out.append({"start_slot": s, "end_slot": s + 1, "price": price})
    fmt = lambda slot: f"{slot // 2:02d}:{(slot % 2) * 30:02d}"  # noqa: E731
    return [{"start": fmt(b["start_slot"]), "end": fmt(b["end_slot"]), "price": b["price"],
             "label": label_price(b["price"], lo, hi)} for b in out]


def outlook_bands(tariff: Optional[dict], today: date, tz, days: int = 2) -> list[dict]:
    """Bands for `days` days from today with ISO start and end times, labelled
    within each day. A band running over midnight at the same price and
    label is one band."""
    out: list[dict] = []
    for d in range(days):
        day = today + timedelta(days=d)
        table = table_for(tariff, day)
        if not table:
            continue
        for b in bands_from_table(table):
            h1, m1 = map(int, b["start"].split(":"))
            h2, m2 = map(int, b["end"].split(":"))
            start = _local_at(day, h1 * 60 + m1, tz)
            end = _local_at(day, h2 * 60 + m2, tz)
            if out and out[-1]["end"] == start.isoformat() and out[-1]["price"] == b["price"] \
                    and out[-1]["label"] == b["label"]:
                out[-1]["end"] = end.isoformat()
                continue
            out.append({"start": start.isoformat(), "end": end.isoformat(),
                        "price": b["price"], "label": b["label"]})
    return out


def hourly_prices(tariff: Optional[dict], hours: list[datetime]) -> list[Optional[float]]:
    """One price per hour: the mean of its two half hour slots. None when
    the tariff is not known for that day."""
    out: list[Optional[float]] = []
    for h in hours:
        table = table_for(tariff, h.date())
        if not table:
            out.append(None)
            continue
        s = h.hour * 2
        out.append(round((table[s] + table[s + 1]) / 2, 5))
    return out


# ── usual usage ─────────────────────────────────────────────────────────────

LOAD_KINDS = {"solar": 1, "import": 1, "discharge": 1, "export": -1, "charge": -1}


def home_hours(series_by_kind: dict[str, list[dict]]) -> dict[datetime, float]:
    """Home kWh per hour from hourly changes of every source:
    solar + import + discharge - export - charge.

    series_by_kind maps a kind in LOAD_KINDS to a list of {hour start: kWh}
    dicts, one per sensor. A kind with no sensors counts as 0. An hour is
    skipped when any sensor has no reading for it or an implausible one, or
    when the total is below -LOAD_NOISE_KWH. A smaller negative reads as 0."""
    all_series = [(sign, s) for kind, sign in LOAD_KINDS.items()
                  for s in series_by_kind.get(kind) or []]
    if not all_series:
        return {}
    hours = set().union(*(s.keys() for _, s in all_series))
    out: dict[datetime, float] = {}
    for h in hours:
        total = 0.0
        for sign, s in all_series:
            v = s.get(h)
            if v is None or not math.isfinite(v) or abs(v) > MAX_HOUR_KWH:
                break
            total += sign * v
        else:
            if total < -LOAD_NOISE_KWH:
                continue
            out[h] = max(0.0, total)
    return out


def load_profile(home_by_hour: dict[datetime, float], tz) -> dict:
    """Average home kWh for each local hour of day, separately for weekdays
    and weekends. A day type with no data for an hour borrows the other's.

    Returns {"status": "ok" | "learning", "days": n, "profile": {"weekday":
    [24], "weekend": [24]}}. The profile is empty while learning."""
    buckets: dict[tuple[str, int], list[float]] = {}
    days = set()
    for h, kwh in home_by_hour.items():
        local = h.astimezone(tz)
        days.add(local.date())
        kind = "weekend" if _is_weekend(local.date()) else "weekday"
        buckets.setdefault((kind, local.hour), []).append(kwh)
    if len(days) < LOAD_MIN_DAYS:
        return {"status": "learning", "days": len(days), "profile": {}}

    def avg(kind, hour):
        vals = buckets.get((kind, hour)) or []
        return sum(vals) / len(vals) if vals else None

    profile = {}
    for kind, other in (("weekday", "weekend"), ("weekend", "weekday")):
        profile[kind] = [round(v, 3) if v is not None else None for v in
                         (avg(kind, h) if avg(kind, h) is not None else avg(other, h)
                          for h in range(24))]
    return {"status": "ok", "days": len(days), "profile": profile}


def usual_kwh(profile: dict, at: datetime) -> Optional[float]:
    """The usual home kWh in the local hour at."""
    hours = (profile.get("profile") or {}).get("weekend" if _is_weekend(at.date()) else "weekday")
    return hours[at.hour] if hours else None


def daily_totals(series: list[dict], tz) -> dict[date, float]:
    """kWh per local day across sensors' hourly changes. A day counts only
    when every sensor has at least 20 hourly readings for it."""
    per_sensor: list[dict[date, list[float]]] = []
    for s in series:
        days: dict[date, list[float]] = {}
        for h, v in s.items():
            if v is None or not math.isfinite(v) or abs(v) > MAX_HOUR_KWH:
                continue
            days.setdefault(h.astimezone(tz).date(), []).append(v)
        per_sensor.append(days)
    if not per_sensor:
        return {}
    common = set.intersection(*(set(d) for d in per_sensor))
    return {day: round(sum(sum(d[day]) for d in per_sensor), 3) for day in common
            if all(len(d[day]) >= 20 for d in per_sensor)}


# ── forecast ────────────────────────────────────────────────────────────────

def forecast_factor(pairs: list[tuple[float, float]]) -> tuple[float, str, int]:
    """(factor, "learned" | "default", days used) from (actual kWh,
    forecast kWh) per day. Days with a forecast under FACTOR_MIN_FORECAST_KWH
    are ignored. The factor is the median of actual / forecast, clamped;
    with too few days it is the cautious FACTOR_DEFAULT."""
    ratios = [a / f for a, f in pairs
              if a is not None and f is not None and f >= FACTOR_MIN_FORECAST_KWH and a >= 0]
    if len(ratios) < FACTOR_MIN_DAYS:
        return FACTOR_DEFAULT, "default", len(ratios)
    return round(max(FACTOR_MIN, min(FACTOR_MAX, statistics.median(ratios))), 3), "learned", len(ratios)


def morning_values(samples: list[tuple[datetime, Optional[float]]], days: list[date],
                   tz, hour: int = FACTOR_HOUR) -> dict[date, float]:
    """The value in effect at the given local hour on each day, for a
    sensor's history. Days without a value are left out."""
    times = [t.timestamp() for t, _ in samples]
    values = [v for _, v in samples]
    out = {}
    for day in days:
        v = _value_at(times, values, _local_at(day, hour * 60, tz))
        if v is not None:
            out[day] = v
    return out


def spread_daily(total_kwh: float, sunrise: datetime, sunset: datetime,
                 hours: list[datetime]) -> dict[datetime, float]:
    """A daily solar total spread over the given hours along a half sine
    from sunrise to sunset. Only the hours given share the total, so passing
    the hours left today spreads today's remaining forecast."""
    if total_kwh is None or total_kwh <= 0 or sunset <= sunrise:
        return {}
    a, b = sunrise.timestamp(), sunset.timestamp()

    def area(t0, t1):  # integral of sin(pi * (t - a) / (b - a)) over [t0, t1]
        t0, t1 = max(t0, a), min(t1, b)
        if t1 <= t0:
            return 0.0
        k = math.pi / (b - a)
        return (math.cos(k * (t0 - a)) - math.cos(k * (t1 - a))) / k

    weights = {h: area(h.timestamp(), h.timestamp() + 3600) for h in hours}
    total_w = sum(weights.values())
    if total_w <= 0:
        return {}
    return {h: total_kwh * w / total_w for h, w in weights.items() if w > 0}


# ── simulation ──────────────────────────────────────────────────────────────

def simulate(start_dt: datetime, stored_kwh: Optional[float], capacity_kwh: Optional[float],
             floor_pct: float, price_by_hour: list, export_price_by_hour: list,
             solar_kwh_by_hour: list, load_kwh_by_hour: list,
             efficiency: float = EFFICIENCY) -> list[dict]:
    """Hour by hour battery and grid plan.

    solar_kwh_by_hour is the forecast already multiplied by the forecast
    factor. Each hour: a surplus charges the battery (times efficiency) up
    to capacity and the rest is exported; a shortfall comes from the
    battery down to the floor and the rest is imported. With no capacity
    known the battery takes no part. Energy balance every hour:
    solar + import + discharge = load + export + charge."""
    cap = capacity_kwh if capacity_kwh and capacity_kwh > 0 else 0.0
    stored = max(0.0, min(cap, stored_kwh or 0.0)) if cap else 0.0
    floor_kwh = cap * floor_pct / 100.0
    points = []
    for i, t in enumerate(hour_starts(start_dt, len(load_kwh_by_hour))):
        solar = max(0.0, solar_kwh_by_hour[i] or 0.0)
        load = max(0.0, load_kwh_by_hour[i] or 0.0)
        price = price_by_hour[i]
        export_price = export_price_by_hour[i] if export_price_by_hour else None
        net = solar - load
        charge = discharge = imported = exported = 0.0
        if net > 0:
            room = (cap - stored) / efficiency if cap else 0.0
            charge = min(net, max(0.0, room))
            stored += charge * efficiency
            exported = net - charge
        elif net < 0:
            need = -net
            discharge = min(need, max(0.0, stored - floor_kwh))
            stored -= discharge
            imported = need - discharge
        cost = imported * (price or 0.0) - exported * (export_price or 0.0)
        r = lambda v: round(v, 3)  # noqa: E731
        points.append({
            "t": t.isoformat(), "price": price, "solar_kwh": r(solar), "load_kwh": r(load),
            "soc_pct": round(stored / cap * 100, 1) if cap else None,
            "grid_import_kwh": r(imported), "grid_export_kwh": r(exported),
            "charge_kwh": r(charge), "discharge_kwh": r(discharge), "cost": round(cost, 4),
        })
    return points


# ── advice ──────────────────────────────────────────────────────────────────

def _t(p: dict) -> datetime:
    return datetime.fromisoformat(p["t"])


def _runs(points: list[dict], first: int = 0) -> list[tuple[int, int]]:
    """(start, end) index ranges of consecutive points with the same price."""
    out: list[tuple[int, int]] = []
    for i in range(first, len(points)):
        if out and points[i]["price"] == points[out[-1][1] - 1]["price"]:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
    return out


def advice_battery_hold(points: list[dict], stored_kwh: Optional[float],
                        capacity_kwh: Optional[float], floor_pct: float = FLOOR_PCT) -> Optional[dict]:
    """Hold the battery for dearer hours.

    Takes the first later run of hours at a higher price than now in which
    the plan imports from the grid (a later one is not worth holding for:
    the battery would be used and refilled before then). The energy worth
    keeping is the least of: what the battery holds above the floor now,
    the load in that run, and what the plan would draw from the battery
    before the run starts. The saving is that times the price gap times
    EFFICIENCY. Silent unless it is at least MIN_SAVING."""
    if not points or not capacity_kwh or stored_kwh is None or points[0]["price"] is None:
        return None
    now_price = points[0]["price"]
    usable = stored_kwh - capacity_kwh * floor_pct / 100.0
    if usable <= 0:
        return None
    best = None
    for a, b in _runs(points, 1):
        price = points[a]["price"]
        run = points[a:b]
        if price is None or price <= now_price or sum(p["grid_import_kwh"] for p in run) <= 0:
            continue
        drawn_before = sum(p["discharge_kwh"] for p in points[:a])
        kept = min(usable, sum(p["load_kwh"] for p in run), drawn_before)
        saving = kept * (price - now_price) * EFFICIENCY
        best = {"a": a, "b": b, "price": price, "kept": kept, "saving": saving}
        break
    if best is None or best["saving"] < MIN_SAVING:
        return None
    start = _t(points[best["a"]])
    end = _t(points[best["b"] - 1]) + timedelta(hours=1)
    return {
        "kind": "battery_hold", "key": f"battery_hold:{start.date().isoformat()}",
        "level": "suggest", "title": "Hold the battery for the expensive hours.",
        "message": (f"Power costs more from {_hhmm(start)} to {_hhmm(end)}. Using the grid "
                    f"now and keeping about {best['kept']:.1f} kWh in the battery for then "
                    f"would save around {best['saving']:.2f}."),
        "when": start.isoformat(), "kwh": round(best["kept"], 1),
        "saving": round(best["saving"], 2),
    }


def advice_cheap_topup(now: datetime, points: list[dict], bands: list[dict],
                       capacity_kwh: Optional[float], usual_by_day: dict[date, float],
                       solar_by_day: dict[date, float]) -> Optional[dict]:
    """Top up in the cheap window before a dull day.

    Only from TOPUP_FROM_HOUR until the start of the cheapest low band that
    starts within 24 hours, and only when the adjusted solar forecast for
    the day after that band is below its usual use. The energy needed is
    the grid import the plan expects at a dearer price in the 24 hours after
    the band, counted only until the battery would be full anyway (charge
    bought after that point would be wasted), capped by the room the battery
    will have when the band starts. Silent unless the saving is at least
    MIN_SAVING."""
    if not points or not capacity_kwh:
        return None
    lows = [b for b in bands if b["label"] == "low"
            and now < datetime.fromisoformat(b["start"]) <= now + timedelta(hours=24)]
    if not lows:
        return None
    band = min(lows, key=lambda b: (b["price"], b["start"]))
    b_start, b_end = datetime.fromisoformat(band["start"]), datetime.fromisoformat(band["end"])
    evening = b_start.replace(hour=TOPUP_FROM_HOUR, minute=0, second=0, microsecond=0)
    if evening >= b_start:
        evening -= timedelta(days=1)
    if not evening <= now < b_start:
        return None

    day = b_end.date()
    usual, sun = usual_by_day.get(day), solar_by_day.get(day)
    if usual is None or sun is None or sun >= usual:
        return None

    before = [p for p in points if _t(p) < b_start.replace(minute=0)]
    soc = before[-1]["soc_pct"] if before else points[0]["soc_pct"]
    if soc is None:
        return None
    room = capacity_kwh * (1 - soc / 100.0)

    imported = cost = 0.0
    for p in points:
        t = _t(p)
        if t < b_end or t >= b_end + timedelta(hours=24):
            continue
        if p["soc_pct"] is not None and p["soc_pct"] >= 99.9:
            break  # full from the sun: charge bought earlier would be wasted
        if p["price"] is not None and p["price"] > band["price"] and p["grid_import_kwh"] > 0:
            imported += p["grid_import_kwh"]
            cost += p["grid_import_kwh"] * p["price"]
    need = min(imported, room)
    if need <= 0:
        return None
    later_price = cost / imported
    saving = need * (later_price - band["price"]) * EFFICIENCY
    if saving < MIN_SAVING:
        return None
    which = "Tomorrow" if day > now.date() else "Today"
    return {
        "kind": "cheap_topup", "key": f"cheap_topup:{now.date().isoformat()}",
        "level": "suggest", "title": "Top up in the cheap window.",
        "message": (f"{which} looks dull, about {sun:.1f} kWh of sun against your usual "
                    f"{usual:.0f} kWh of use. Charging about {need:.1f} kWh between "
                    f"{_hhmm(b_start)} and {_hhmm(b_end)} would save around {saving:.2f}."),
        "when": b_start.isoformat(), "kwh": round(need, 1), "saving": round(saving, 2),
    }


def best_window(points: list[dict], hours: int = 2, within_h: int = 24,
                flat: bool = False) -> Optional[dict]:
    """The cheapest run of `hours` hours starting in the next within_h hours
    for a big appliance, by marginal cost: an hour where the plan exports
    (spare solar the battery cannot take) is free, any other hour costs its
    price. Running on battery is not free, because that energy would
    otherwise be used later. Ties go to the earlier start. On a flat tariff
    only a spare solar window is worth naming."""
    try:
        hours = max(1, int(hours))
    except (TypeError, ValueError):
        hours = 2
    best = None
    for i in range(0, min(within_h, len(points) - hours + 1)):
        window = points[i:i + hours]
        if any(p["price"] is None for p in window):
            continue
        cost = sum(0.0 if p["grid_export_kwh"] > 0 else p["price"] for p in window)
        if best is None or cost < best[0] - 1e-9:
            best = (cost, i)
    if best is None:
        return None
    cost, i = best
    window = points[i:i + hours]
    spare = cost == 0
    if flat and not spare:
        return None
    return {
        "start": window[0]["t"],
        "end": (_t(window[-1]) + timedelta(hours=1)).isoformat(),
        "reason": "spare solar" if spare else "cheapest rate",
        "price": round(sum(p["price"] for p in window) / hours, 4),
    }


def advice_high_use(now: datetime, today_home_kwh: Optional[float], profile: dict) -> Optional[dict]:
    """Today's use so far is well above the usual for the hours gone by."""
    hours = (profile.get("profile") or {}).get("weekend" if _is_weekend(now.date()) else "weekday")
    if today_home_kwh is None or not hours:
        return None
    usual = sum(v or 0.0 for v in hours[:now.hour]) + (hours[now.hour] or 0.0) * now.minute / 60
    if usual <= 0 or today_home_kwh < usual * HIGH_USE_RATIO or today_home_kwh - usual < HIGH_USE_MIN_KWH:
        return None
    return {
        "kind": "high_use_today", "key": f"high_use_today:{now.date().isoformat()}",
        "level": "info", "title": "Using more than usual today.",
        "message": (f"The home has used {today_home_kwh:.1f} kWh so far today. "
                    f"By this time it usually uses about {usual:.1f} kWh."),
        "when": now.isoformat(), "kwh": round(today_home_kwh - usual, 1), "saving": None,
    }


# ── the whole outlook ───────────────────────────────────────────────────────

_MESSAGES = {
    "tariff_learning": "Learning your tariff times: {n} of {need} days.",
    "load_learning": "Learning your usual usage: {n} of {need} days.",
    "flat": "Your tariff has one price all day, so there is no cheaper time to move use to.",
    "no_price": "Your tariff is not known yet, so price advice is off.",
    "no_forecast": "No solar forecast is set up, so advice that needs the sun is off.",
    "no_capacity": "Battery size is not known, so battery advice is off.",
}


def empty_outlook() -> dict:
    return {
        "configured": False, "error": False, "status": "unavailable", "currency": None,
        "points": [], "bands": [], "advice": [], "best_window": None, "messages": [],
        "learned": {"tariff_days": 0, "load_days": 0, "forecast_factor": None,
                    "factor_source": None, "forecast_shape": "none"},
        "updated_at": None,
    }


def build_outlook(now: datetime, tariff: Optional[dict], export_tariff: Optional[dict],
                  profile: dict, solar_by_hour: dict[datetime, float], forecast_shape: str,
                  factor: tuple[float, str, int], capacity_kwh: Optional[float],
                  stored_kwh: Optional[float], today_home_kwh: Optional[float],
                  currency: Optional[str] = None, hours: int = 2) -> dict:
    """The outlook from already read inputs. now is a local aware time;
    solar_by_hour maps local or UTC hour starts to forecast kWh before the
    factor. Never invents a number: what is missing is left out and named
    in "messages"."""
    tz = now.tzinfo
    start = now.replace(minute=0, second=0, microsecond=0)
    hs = hour_starts(start, HORIZON_H)
    tariff_status = (tariff or {}).get("status")
    priced = tariff_status in ("ok", "flat")
    has_forecast = forecast_shape != "none"
    solar_lookup = {h.timestamp(): v for h, v in (solar_by_hour or {}).items()}
    f = factor[0]
    solar = [solar_lookup.get(h.timestamp(), 0.0) * f if has_forecast else 0.0 for h in hs]

    out = empty_outlook()
    out.update(configured=True, currency=currency, updated_at=now.isoformat())
    out["learned"] = {
        "tariff_days": (tariff or {}).get("days", 0), "load_days": profile.get("days", 0),
        "forecast_factor": f, "factor_source": factor[1], "forecast_shape": forecast_shape,
    }
    # Bands from today to the last day the horizon reaches, so the strip's
    # price bar runs its whole length.
    out["bands"] = outlook_bands(tariff, now.date(), tz, (hs[-1].date() - now.date()).days + 1) \
        if priced else []

    messages, states = [], []
    if tariff_status == "learning":
        messages.append(_MESSAGES["tariff_learning"].format(n=tariff["days"], need=TARIFF_MIN_DAYS))
        states.append("learning")
    elif not priced:
        messages.append(_MESSAGES["no_price"])
    if profile.get("status") != "ok":
        messages.append(_MESSAGES["load_learning"].format(n=profile.get("days", 0), need=LOAD_MIN_DAYS))
        states.append("learning")
    if tariff_status == "flat":
        messages.append(_MESSAGES["flat"])
        states.append("flat")
    if not has_forecast:
        messages.append(_MESSAGES["no_forecast"])
        states.append("no_forecast")
    if not capacity_kwh:
        messages.append(_MESSAGES["no_capacity"])
        states.append("no_capacity")
    out["messages"] = messages
    out["status"] = states[0] if states else "ok"

    if profile.get("status") != "ok":
        return out  # no usual usage: no plan, but the bands above still show

    loads = [usual_kwh(profile, h) or 0.0 for h in hs]
    prices = hourly_prices(tariff, hs) if priced else [None] * len(hs)
    export_prices = hourly_prices(export_tariff, hs) if export_tariff else [None] * len(hs)
    points = simulate(start, stored_kwh, capacity_kwh, FLOOR_PCT, prices, export_prices,
                      solar, loads)
    out["points"] = points

    advice = []
    if tariff_status == "ok" and capacity_kwh:
        advice.append(advice_battery_hold(points, stored_kwh, capacity_kwh))
        if has_forecast:
            usual_by_day: dict[date, float] = {}
            solar_by_day: dict[date, float] = {}
            for h, load, s in zip(hs, loads, solar):
                usual_by_day[h.date()] = usual_by_day.get(h.date(), 0.0) + load
                solar_by_day[h.date()] = solar_by_day.get(h.date(), 0.0) + s
            # Only whole days count: the day of the band must be fully inside
            # the horizon, so its totals are not partial.
            full = {h.date() for h in hs if h.hour == 0}
            full = {d for d in full if sum(1 for h in hs if h.date() == d) >= 23}
            advice.append(advice_cheap_topup(
                now, points, out["bands"], capacity_kwh,
                {d: v for d, v in usual_by_day.items() if d in full},
                {d: v for d, v in solar_by_day.items() if d in full}))
    advice.append(advice_high_use(now, today_home_kwh, profile))
    advice = [a for a in advice if a]
    advice.sort(key=lambda a: (a["level"] == "info", -(a.get("saving") or 0.0)))
    out["advice"] = advice[:3]
    out["best_window"] = best_window(points, hours, flat=tariff_status == "flat") if priced else None
    return out


# ── Home Assistant side ─────────────────────────────────────────────────────

class _ReadError(Exception):
    """A recorder read failed: the outlook is an error, not unconfigured."""


def _row_time(row) -> Optional[datetime]:
    raw = row.get("last_changed") if isinstance(row, dict) else getattr(row, "last_changed", None)
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, timezone.utc)
    return raw if isinstance(raw, datetime) else None


async def _state_history(hass, entity_id: str, start: datetime, end: datetime
                         ) -> list[tuple[datetime, Optional[float]]]:
    """(time, value) changes of an entity, the first one in effect at start
    (include_start_time_state, as solar._daily_sum). Unavailable or non
    numeric states read as None. Raises _ReadError on a recorder failure."""
    try:
        from homeassistant.components.recorder import get_instance, history

        def _fetch():
            return history.get_significant_states(
                hass, start, end, [entity_id], minimal_response=True, no_attributes=True)

        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        raise _ReadError(f"history of {entity_id}: {exc}") from exc
    out = []
    for row in (raw or {}).get(entity_id) or []:
        t = _row_time(row)
        if t is None:
            continue
        state = row.get("state") if isinstance(row, dict) else getattr(row, "state", None)
        try:
            v = float(state)
            v = v if math.isfinite(v) else None
        except (TypeError, ValueError):
            v = None
        out.append((t, v))
    out.sort(key=lambda tv: tv[0])
    return out


async def _hourly_changes(hass, ids: list[str], start: datetime, end: datetime
                          ) -> dict[str, dict[datetime, float]]:
    """Hourly "change" of each statistic from the recorder's long term
    statistics, in kWh (the units argument converts Wh and MWh). Runs in the
    recorder's executor. Raises _ReadError on a recorder failure."""
    if not ids:
        return {}
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        def _fetch():
            return statistics_during_period(
                hass, start, end, set(ids), "hour", {"energy": "kWh"}, {"change"})

        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        raise _ReadError(f"statistics: {exc}") from exc
    out: dict[str, dict[datetime, float]] = {}
    for sid in ids:
        series: dict[datetime, float] = {}
        for row in (raw or {}).get(sid) or []:
            t = row.get("start")
            if isinstance(t, (int, float)):
                t = datetime.fromtimestamp(t, timezone.utc)
            change = row.get("change")
            if isinstance(t, datetime) and isinstance(change, (int, float)):
                series[t.astimezone(timezone.utc)] = float(change)
        out[sid] = series
    return out


def _parse_hourly(mapping: dict, scale: float) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for key, val in (mapping or {}).items():
        try:
            t = datetime.fromisoformat(str(key)).astimezone(timezone.utc)
            out[t.replace(minute=0, second=0, microsecond=0)] = \
                out.get(t.replace(minute=0, second=0, microsecond=0), 0.0) + float(val) * scale
        except (TypeError, ValueError):
            continue
    return out


def _merge(into: dict, more: dict) -> None:
    for k, v in more.items():
        into[k] = into.get(k, 0.0) + v


async def _forecast_hourly(hass, now: datetime, tz) -> tuple[dict[datetime, float], str]:
    """Hourly solar forecast in kWh for the next 48 hours, summed over every
    installed forecast entry, and its shape: "hourly", "estimated" or "none".

    In order: Forecast.Solar's get_forecast action; the forecast
    integration's energy platform (the source HA's own Energy dashboard
    uses, also offered by Open-Meteo Solar Forecast); the daily totals
    spread along a half sine between sunrise and sunset. Never raises."""
    try:
        entries = _forecast_entries(hass)
    except Exception as exc:
        _LOGGER.debug("energy_outlook: forecast discovery failed: %s", exc)
        return {}, "none"
    by_platform: dict[str, set] = {}
    for e in entries:
        if getattr(e, "config_entry_id", None):
            by_platform.setdefault(e.platform, set()).add(e.config_entry_id)
    if not by_platform:
        return {}, "none"

    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=48)

    out: dict[datetime, float] = {}
    fs = by_platform.get("forecast_solar") or set()
    if fs and hass.services.has_service("forecast_solar", "get_forecast"):
        try:
            for entry_id in fs:
                resp = await hass.services.async_call(
                    "forecast_solar", "get_forecast",
                    {"config_entry": entry_id, "start": start.isoformat(),
                     "end": end.isoformat(), "resolution": "hourly"},
                    blocking=True, return_response=True)
                _merge(out, _parse_hourly((resp or {}).get("wh_period"), 0.001))
            if out:
                return out, "hourly"
        except Exception as exc:
            _LOGGER.debug("energy_outlook: get_forecast failed: %s", exc)
            out = {}

    try:
        from homeassistant.loader import async_get_integration
        for platform in _FORECAST_PLATFORMS:
            ids = by_platform.get(platform)
            if not ids:
                continue
            integration = await async_get_integration(hass, platform)
            getter = getattr(integration, "async_get_platform", None)
            mod = await getter("energy") if getter else integration.get_platform("energy")
            for entry_id in ids:
                data = await mod.async_get_solar_forecast(hass, entry_id)
                _merge(out, _parse_hourly((data or {}).get("wh_hours"), 0.001))
        if out:
            return out, "hourly"
    except Exception as exc:
        _LOGGER.debug("energy_outlook: energy platform forecast failed: %s", exc)
        out = {}

    try:
        from homeassistant.helpers.sun import get_astral_event_date
        totals = _forecast_values(hass)
        days = ((now.date(), totals.get("forecast_remaining_kwh")),
                (now.date() + timedelta(days=1), totals.get("forecast_tomorrow_kwh")))
        hs = hour_starts(start, 48)
        for day, total in days:
            rise = get_astral_event_date(hass, "sunrise", day)
            sets = get_astral_event_date(hass, "sunset", day)
            if total is None or rise is None or sets is None:
                continue
            day_hours = [h for h in hs if h.date() == day]
            if day == now.date():  # the remaining total covers the rest of the hour too
                day_hours = [h for h in day_hours if h >= start]
            _merge(out, {h.astimezone(timezone.utc): v
                         for h, v in spread_daily(total, rise, sets, day_hours).items()})
        if out:
            return out, "estimated"
    except Exception as exc:
        _LOGGER.debug("energy_outlook: estimated forecast failed: %s", exc)
    return {}, "none"


def _forecast_today_entity(hass) -> Optional[str]:
    try:
        for e in _forecast_entries(hass):
            if e.unique_id.endswith("energy_production_today"):
                return e.entity_id
    except Exception:
        pass
    return None


def _tz(hass):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(hass.config.time_zone)
    except Exception:
        return timezone.utc


async def _tariff(hass, entity_id: Optional[str], number, now: datetime, tz) -> Optional[dict]:
    """The learned tariff for a price entity, else a flat one for a fixed
    number, else None."""
    if entity_id:
        midnight = _local_at(now.date(), 0, tz)
        first_day = now.date() - timedelta(days=TARIFF_DAYS)
        samples = await _state_history(hass, entity_id, _local_at(first_day, 0, tz), midnight)
        return tariff_table_from_history(samples, first_day, TARIFF_DAYS, tz)
    if number is not None:
        try:
            return flat_tariff(float(number))
        except (TypeError, ValueError):
            return None
    return None


async def _compute(hass, now: datetime, tz) -> dict:
    """Read every input and build the outlook. Raises _ReadError when a
    recorder read fails."""
    from .energy_flow import energy_flow_status, energy_flow_today

    prefs = await _read_prefs(hass)
    sources = (prefs or {}).get("energy_sources") or []
    grids = [_grid_sensors(s) for s in sources if s.get("type") == "grid"]
    if not grids or grids[0]["import_price"] == (None, None):
        return empty_outlook()
    grid = grids[0]
    batteries = [_grid_sensors(s) for s in sources if s.get("type") == "battery"]
    solar_ids = [s.get("stat_energy_from") for s in sources
                 if s.get("type") == "solar" and s.get("stat_energy_from")]

    tariff = await _tariff(hass, *grid["import_price"], now, tz)
    export_tariff = None
    exp_entity, exp_number = grid["export_price"]
    if exp_entity or exp_number is not None:
        export_tariff = await _tariff(hass, exp_entity, exp_number, now, tz)
        if (export_tariff or {}).get("status") not in ("ok", "flat"):
            export_tariff = flat_tariff(_resolve_price(hass, exp_entity, exp_number))

    kinds = {
        "solar": solar_ids,
        "import": [e for g in grids for e in g["imports"]],
        "export": [e for g in grids for e in g["exports"]],
        # A battery's stat_energy_from is discharge, stat_energy_to charge.
        "discharge": [e for b in batteries for e in b["imports"]],
        "charge": [e for b in batteries for e in b["exports"]],
    }
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    changes = await _hourly_changes(
        hass, sorted({e for ids in kinds.values() for e in ids}),
        hour_now - timedelta(days=LOAD_DAYS), hour_now)
    series = {k: [changes.get(e, {}) for e in ids] for k, ids in kinds.items()}
    profile = load_profile(home_hours(series), tz)

    pairs = []
    fc_entity = _forecast_today_entity(hass)
    if fc_entity and solar_ids:
        days = [now.date() - timedelta(days=d) for d in range(FACTOR_DAYS, 0, -1)]
        samples = await _state_history(hass, fc_entity, _local_at(days[0], 0, tz),
                                       _local_at(now.date(), 0, tz))
        morning = morning_values(samples, days, tz)
        actual = daily_totals(series["solar"], tz)
        for day, raw in morning.items():
            fc = _energy_to_kwh(hass, fc_entity, raw)
            if day in actual and fc is not None:
                pairs.append((actual[day], fc))
    factor = forecast_factor(pairs)

    solar_by_hour, shape = await _forecast_hourly(hass, now, tz)
    flow = await energy_flow_status(hass)
    battery = flow.get("battery") or {}
    today = await energy_flow_today(hass)
    currency = getattr(hass.config, "currency", None)
    return build_outlook(now, tariff, export_tariff, profile, solar_by_hour, shape, factor,
                         battery.get("capacity_kwh"), battery.get("stored_kwh"),
                         today.get("home_kwh"), currency)


def _clock() -> float:
    return time.monotonic()


def _now(hass) -> tuple[datetime, object]:
    tz = _tz(hass)
    return datetime.now(timezone.utc).astimezone(tz), tz


def _with_window(result: dict, hours) -> dict:
    """The cached outlook with best_window worked out for another length."""
    out = dict(result)
    if hours not in (None, 2) and out.get("points"):
        flat = out.get("status") == "flat"
        priced = any(p.get("price") is not None for p in out["points"])
        out["best_window"] = best_window(out["points"], hours, flat=flat) if priced else None
    return out


async def energy_outlook_status(hass, hours: int = 2) -> dict:
    """The energy outlook for the panel, the chat tool and the proactive
    hook. Cached for CACHE_TTL_S; callers arriving together share one
    calculation. Never raises: a failed read returns the outlook shape with
    "error": True (not cached), never the unconfigured one."""
    global _lock
    hit = _cache.get("value")
    if hit is not None and _clock() - _cache.get("at", 0.0) < CACHE_TTL_S:
        return _with_window(hit, hours)
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        hit = _cache.get("value")
        if hit is not None and _clock() - _cache.get("at", 0.0) < CACHE_TTL_S:
            return _with_window(hit, hours)
        try:
            now, tz = _now(hass)
            result = await _compute(hass, now, tz)
        except Exception as exc:
            _LOGGER.debug("energy_outlook: status failed: %s", exc)
            return {**empty_outlook(), "configured": True, "error": True}
        _cache.update(value=result, at=_clock())
        return _with_window(result, hours)
