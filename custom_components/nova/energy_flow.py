"""
Nova live energy flow: solar, house, battery and grid power right now.

Reads the same Home Assistant Energy dashboard config solar.py reads, through
solar.py's own helpers, so nothing here names an entity or an integration.
Unlike solar.py, every battery and grid source counts, not just the first.

Direction never comes from a rate sensor's sign alone: sign conventions are
not standardised, and a real install's grid sensor read positive while
exporting. Rate sensors give the size of the flow. Direction comes from
whichever cumulative total (stat_energy_from or stat_energy_to) changed most
recently, the same test solar._grid_direction makes. Only when the totals are
missing or cannot be compared does the sign decide, and the output then says
direction_source "sign" instead of "totals".

In the Energy dashboard prefs a grid's stat_energy_from is import and its
stat_energy_to is export; a battery's stat_energy_from is discharge and its
stat_energy_to is charge. Here a positive signed value always means the
"from" side: power flowing into the house.

The pure functions take plain numbers so they can be tested without
Home Assistant. energy_flow_status() is the only part that reads hass, and it
never raises.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .solar import (
    _daily_sum, _grid_direction, _grid_sensors, _live_pct, _live_watts, _read_prefs,
)

_LOGGER = logging.getLogger(__name__)

# Below this many watts a flow counts as idle.
IDLE_W = 20.0

# A battery time estimate longer than this is not shown.
ETA_MAX_MIN = 48 * 60

# Today's totals come from the recorder; a 5 second poll reuses them for
# this many seconds. A small negative home total (kWh) is rounding noise.
TODAY_TTL_S = 60.0
TODAY_NOISE_KWH = 0.05
_today_cache: dict = {}

_STATES = {
    "battery": ("discharging", "charging"),
    "grid": ("importing", "exporting"),
}


# ── pure functions ──────────────────────────────────────────────────────────

def source_flow(rate_w: Optional[float], side: Optional[str]
                ) -> tuple[Optional[float], Optional[str]]:
    """One battery or grid source's signed watts, and how its direction was
    found. Positive is the "from" side (import, discharge), negative the "to"
    side (export, charge).

    side is "from" or "to" when the cumulative totals said which moved last,
    else None. Returns (None, None) when the rate is unavailable, and
    (0.0, None) for an idle source, whose direction does not matter. Without
    totals the sign decides, positive meaning "from"; that is a guess, so it
    is reported as "sign"."""
    if rate_w is None:
        return None, None
    mag = abs(rate_w)
    if mag < IDLE_W:
        return 0.0, None
    if side == "from":
        return mag, "totals"
    if side == "to":
        return -mag, "totals"
    return (mag if rate_w > 0 else -mag), "sign"


def net_flow(flows: list[tuple[Optional[float], Optional[str]]]) -> Optional[float]:
    """Sum of the signed watts of every source of one type. None when there
    are no sources or any one of them is unavailable: a partial sum would
    understate the flow."""
    if not flows or any(w is None for w, _ in flows):
        return None
    return sum(w for w, _ in flows)


def flow_state(net_w: Optional[float], kind: str) -> tuple[Optional[int], Optional[str]]:
    """(magnitude in watts, state word) for a battery or grid net value."""
    if net_w is None:
        return None, None
    mag = abs(net_w)
    if mag < IDLE_W:
        return round(mag), "idle"
    inflow, outflow = _STATES[kind]
    return round(mag), inflow if net_w > 0 else outflow


def solar_power(rates: list[Optional[float]]) -> Optional[float]:
    """Total solar watts across every array. None when there are none or any
    one is unavailable."""
    if not rates or any(r is None for r in rates):
        return None
    return sum(abs(r) for r in rates)


def house_power(solar_w: Optional[float], battery_net: Optional[float],
                grid_net: Optional[float], *, solar_configured: bool,
                battery_configured: bool, grid_configured: bool) -> Optional[int]:
    """House watts from the energy balance: solar + grid import + battery
    discharge - grid export - battery charge. A source type that is not
    configured counts as 0; one that is configured but unavailable makes the
    result None. A small negative (under IDLE_W) is rounding noise and reads
    as 0; a larger one means the readings disagree, so the result is None."""
    total = 0.0
    for configured, value in ((solar_configured, solar_w),
                              (battery_configured, battery_net),
                              (grid_configured, grid_net)):
        if not configured:
            continue
        if value is None:
            return None
        total += value
    if total < 0:
        return 0 if total > -IDLE_W else None
    return round(total)


def mean_pct(values: list[Optional[float]]) -> Optional[float]:
    """Mean of the readings that exist, to one decimal. None when none do."""
    seen = [v for v in values if v is not None]
    if not seen:
        return None
    return round(sum(seen) / len(seen), 1)


def direction_source(*flow_lists: list[tuple[Optional[float], Optional[str]]]
                     ) -> Optional[str]:
    """"sign" when any source's direction was guessed from its sign, else
    "totals" when any came from the totals, else None."""
    found = {how for flows in flow_lists for _, how in flows if how}
    if "sign" in found:
        return "sign"
    if "totals" in found:
        return "totals"
    return None


def battery_store(packs: list[tuple[Optional[float], Optional[float]]]
                  ) -> tuple[Optional[float], Optional[float]]:
    """(capacity kWh, stored kWh) across every battery source, from
    (capacity kWh, percent) pairs. Both None unless every source has a
    positive capacity and a percentage: a partial total would be a guess."""
    if not packs:
        return None, None
    capacity = stored = 0.0
    for cap, pct in packs:
        if cap is None or pct is None or cap <= 0:
            return None, None
        capacity += cap
        stored += cap * max(0.0, min(100.0, pct)) / 100.0
    return round(capacity, 2), round(stored, 2)


def battery_eta(capacity_kwh: Optional[float], stored_kwh: Optional[float],
                w: Optional[float], state: Optional[str]
                ) -> tuple[Optional[int], Optional[str]]:
    """(minutes, "empty" or "full") at the current rate, rounded to 5
    minutes. None when idle, when anything is unknown, when it rounds to
    nothing, or when it is over 48 hours."""
    if capacity_kwh is None or stored_kwh is None or not w or state not in ("charging", "discharging"):
        return None, None
    kw = abs(w) / 1000.0
    if state == "discharging":
        hours, to = stored_kwh / kw, "empty"
    else:
        hours, to = max(0.0, capacity_kwh - stored_kwh) / kw, "full"
    minutes = int(hours * 60 / 5 + 0.5) * 5
    if minutes <= 0 or minutes > ETA_MAX_MIN:
        return None, None
    return minutes, to


def empty_status() -> dict:
    return {
        "configured": False,
        "solar": {"w": None},
        "battery": {"w": None, "state": None, "pct": None, "capacity_kwh": None,
                    "stored_kwh": None, "eta_min": None, "eta_to": None},
        "grid": {"w": None, "state": None},
        "house": {"w": None},
        "direction_source": None,
    }


def build_status(solar_rates: list[Optional[float]],
                 battery_flows: list[tuple[Optional[float], Optional[str]]],
                 battery_pcts: list[Optional[float]],
                 grid_flows: list[tuple[Optional[float], Optional[str]]],
                 battery_packs: list[tuple[Optional[float], Optional[float]]] = ()) -> dict:
    """The status dict from already read values, one entry per configured
    source. Unconfigured when there is no solar, battery or grid source.
    battery_packs is one (capacity kWh, percent) pair per battery source."""
    if not solar_rates and not battery_flows and not grid_flows:
        return empty_status()
    solar_w = solar_power(solar_rates)
    battery_net = net_flow(battery_flows)
    grid_net = net_flow(grid_flows)
    battery_w, battery_state = flow_state(battery_net, "battery")
    grid_w, grid_state = flow_state(grid_net, "grid")
    capacity_kwh, stored_kwh = battery_store(list(battery_packs))
    eta_min, eta_to = battery_eta(capacity_kwh, stored_kwh, battery_w, battery_state)
    return {
        "configured": True,
        "solar": {"w": round(solar_w) if solar_w is not None else None},
        "battery": {"w": battery_w, "state": battery_state, "pct": mean_pct(battery_pcts),
                    "capacity_kwh": capacity_kwh, "stored_kwh": stored_kwh,
                    "eta_min": eta_min, "eta_to": eta_to},
        "grid": {"w": grid_w, "state": grid_state},
        "house": {"w": house_power(
            solar_w, battery_net, grid_net,
            solar_configured=bool(solar_rates),
            battery_configured=bool(battery_flows),
            grid_configured=bool(grid_flows))},
        "direction_source": direction_source(battery_flows, grid_flows),
    }


def empty_today() -> dict:
    return {
        "configured": False,
        "solar_kwh": None,
        "grid_import_kwh": None,
        "grid_export_kwh": None,
        "battery_charged_kwh": None,
        "battery_discharged_kwh": None,
        "home_kwh": None,
        "self_sufficiency_pct": None,
    }


def home_today(solar: Optional[float], grid_import: Optional[float], grid_export: Optional[float],
               charged: Optional[float], discharged: Optional[float]) -> Optional[float]:
    """kWh the home used today: solar + grid import + battery discharged -
    grid export - battery charged. None if any part is None. A negative
    under TODAY_NOISE_KWH reads as 0; a larger one means the totals
    disagree, so the result is None."""
    parts = (solar, grid_import, grid_export, charged, discharged)
    if any(p is None for p in parts):
        return None
    total = solar + grid_import + discharged - grid_export - charged
    if total < 0:
        return 0.0 if total > -TODAY_NOISE_KWH else None
    return total


def self_sufficiency(grid_import: Optional[float], home: Optional[float]) -> Optional[float]:
    """Share of today's home use not bought from the grid, 0 to 100."""
    if grid_import is None or home is None or home <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - grid_import / home) * 100)), 1)


def build_today(solar: Optional[float], grid_import: Optional[float], grid_export: Optional[float],
                charged: Optional[float], discharged: Optional[float]) -> dict:
    """The today dict from already summed totals (0 for a type that is not
    configured, None for one whose total could not be read)."""
    home = home_today(solar, grid_import, grid_export, charged, discharged)
    r = lambda v: round(v, 2) if v is not None else None  # noqa: E731
    return {
        "configured": True,
        "solar_kwh": r(solar),
        "grid_import_kwh": r(grid_import),
        "grid_export_kwh": r(grid_export),
        "battery_charged_kwh": r(charged),
        "battery_discharged_kwh": r(discharged),
        "home_kwh": r(home),
        "self_sufficiency_pct": self_sufficiency(grid_import, home),
    }


# ── Home Assistant side ─────────────────────────────────────────────────────

def _totals_side(hass, source: dict) -> Optional[str]:
    """"from" or "to": which cumulative total moved most recently. None when
    either is missing or they cannot be compared. Passing no rate makes
    solar._grid_direction answer from the totals only, never the sign."""
    found = _grid_direction(hass, source, None)
    return {"import": "from", "export": "to"}.get(found)


def _source_rate(hass, source: dict) -> Optional[float]:
    """Signed watts of one battery or grid source: the sum of its rate
    sensors in either grid layout (see solar._grid_sensors). None when it
    has none or any one is unavailable."""
    values = [_live_watts(hass, e) for e in _grid_sensors(source)["rates"]]
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def _flows(hass, sources: list[dict]) -> list[tuple[Optional[float], Optional[str]]]:
    return [source_flow(_source_rate(hass, s), _totals_side(hass, s)) for s in sources]


async def energy_flow_status(hass) -> dict:
    """Live solar, house, battery and grid power. Never raises: on an
    unexpected failure it logs and returns the unconfigured shape with
    "error": True, so the panel can say the read failed rather than ask
    for setup."""
    try:
        prefs = await _read_prefs(hass)
        sources = (prefs or {}).get("energy_sources") or []
        solar_sources = [s for s in sources if s.get("type") == "solar"]
        battery_sources = [s for s in sources if s.get("type") == "battery"]
        grid_sources = [s for s in sources if s.get("type") == "grid"]
        return build_status(
            [_live_watts(hass, s.get("stat_rate")) for s in solar_sources],
            _flows(hass, battery_sources),
            [_live_pct(hass, s.get("stat_soc")) for s in battery_sources if s.get("stat_soc")],
            _flows(hass, grid_sources),
            [(_capacity_kwh(s), _live_pct(hass, s.get("stat_soc"))) for s in battery_sources],
        )
    except Exception as exc:
        _LOGGER.debug("energy_flow: status failed: %s", exc)
        return {**empty_status(), "error": True}


def _capacity_kwh(source: dict) -> Optional[float]:
    """A battery source's "capacity" from the Energy dashboard prefs, read
    as kWh. None when missing or not a positive number."""
    try:
        cap = float(source.get("capacity"))
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def _clock() -> float:
    return time.monotonic()


async def _sum_totals(hass, entity_ids: list) -> Optional[float]:
    """Today's kWh across cumulative total sensors: 0 with none configured,
    None if any one could not be read."""
    total = 0.0
    for eid in entity_ids:
        v = await _daily_sum(hass, eid)
        if v is None:
            return None
        total += v
    return total


async def energy_flow_today(hass) -> dict:
    """Today's totals since local midnight, from the recorder through
    solar._daily_sum. Cached for TODAY_TTL_S so a 5 second poll does not
    query the recorder each time. Never raises: on an unexpected failure it
    returns the unconfigured shape with "error": True (not cached)."""
    now = _clock()
    hit = _today_cache.get("value")
    if hit is not None and now - _today_cache.get("at", 0.0) < TODAY_TTL_S:
        return dict(hit)
    try:
        prefs = await _read_prefs(hass)
        sources = (prefs or {}).get("energy_sources") or []
        solar_sources = [s for s in sources if s.get("type") == "solar"]
        battery_sources = [s for s in sources if s.get("type") == "battery"]
        grid_sources = [s for s in sources if s.get("type") == "grid"]
        if not (solar_sources or battery_sources or grid_sources):
            out = empty_today()
        else:
            grids = [_grid_sensors(g) for g in grid_sources]
            batteries = [_grid_sensors(b) for b in battery_sources]
            out = build_today(
                await _sum_totals(hass, [s.get("stat_energy_from") for s in solar_sources]),
                await _sum_totals(hass, [e for g in grids for e in g["imports"]]),
                await _sum_totals(hass, [e for g in grids for e in g["exports"]]),
                # A battery's stat_energy_to is charge, stat_energy_from discharge.
                await _sum_totals(hass, [e for b in batteries for e in b["exports"]]),
                await _sum_totals(hass, [e for b in batteries for e in b["imports"]]),
            )
    except Exception as exc:
        _LOGGER.debug("energy_flow: today failed: %s", exc)
        return {**empty_today(), "error": True}
    _today_cache.update(value=dict(out), at=now)
    return out
