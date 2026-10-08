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
from typing import Optional

from .solar import _grid_direction, _live_pct, _live_watts, _read_prefs

_LOGGER = logging.getLogger(__name__)

# Below this many watts a flow counts as idle.
IDLE_W = 20.0

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


def empty_status() -> dict:
    return {
        "configured": False,
        "solar": {"w": None},
        "battery": {"w": None, "state": None, "pct": None},
        "grid": {"w": None, "state": None},
        "house": {"w": None},
        "direction_source": None,
    }


def build_status(solar_rates: list[Optional[float]],
                 battery_flows: list[tuple[Optional[float], Optional[str]]],
                 battery_pcts: list[Optional[float]],
                 grid_flows: list[tuple[Optional[float], Optional[str]]]) -> dict:
    """The status dict from already read values, one entry per configured
    source. Unconfigured when there is no solar, battery or grid source."""
    if not solar_rates and not battery_flows and not grid_flows:
        return empty_status()
    solar_w = solar_power(solar_rates)
    battery_net = net_flow(battery_flows)
    grid_net = net_flow(grid_flows)
    battery_w, battery_state = flow_state(battery_net, "battery")
    grid_w, grid_state = flow_state(grid_net, "grid")
    return {
        "configured": True,
        "solar": {"w": round(solar_w) if solar_w is not None else None},
        "battery": {"w": battery_w, "state": battery_state, "pct": mean_pct(battery_pcts)},
        "grid": {"w": grid_w, "state": grid_state},
        "house": {"w": house_power(
            solar_w, battery_net, grid_net,
            solar_configured=bool(solar_rates),
            battery_configured=bool(battery_flows),
            grid_configured=bool(grid_flows))},
        "direction_source": direction_source(battery_flows, grid_flows),
    }


# ── Home Assistant side ─────────────────────────────────────────────────────

def _totals_side(hass, source: dict) -> Optional[str]:
    """"from" or "to": which cumulative total moved most recently. None when
    either is missing or they cannot be compared. Passing no rate makes
    solar._grid_direction answer from the totals only, never the sign."""
    found = _grid_direction(hass, source, None)
    return {"import": "from", "export": "to"}.get(found)


def _flows(hass, sources: list[dict]) -> list[tuple[Optional[float], Optional[str]]]:
    return [source_flow(_live_watts(hass, s.get("stat_rate")), _totals_side(hass, s))
            for s in sources]


async def energy_flow_status(hass) -> dict:
    """Live solar, house, battery and grid power. Never raises: on an
    unexpected failure it logs and returns the unconfigured shape."""
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
        )
    except Exception as exc:
        _LOGGER.debug("energy_flow: status failed: %s", exc)
        return empty_status()
