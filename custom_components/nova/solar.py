"""
Nova solar/battery/grid visibility (v7.91.0).

Distinct from energy.py, which is exclusively about whole-home CONSUMPTION
management (peak-shedding, appliance staggering) and explicitly excludes
solar/grid/battery sensors from its own appliance discovery
(appliance_monitor.py's meter-discovery keyword filter). This module is about
production/storage instead: how much the panels are generating, whether the
battery is charging or discharging and how full it is, and whether the house
is importing from or exporting to the grid.

Reads the SAME configuration Home Assistant's own Energy dashboard uses
(homeassistant.components.energy's EnergyManager) rather than any
Nova-specific setup — a household that has already configured HA's Energy
dashboard gets this for free, with no separate Nova-side entity picker to
maintain. This is deliberate: Nova is a public integration installed by
people whose solar/battery/grid entity IDs are nothing like any other
install's, so nothing here may assume a specific entity_id.

Nothing here raises to the caller; every entry point returns a status dict.
"""
from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)


async def _read_prefs(hass) -> Optional[dict]:
    """The Energy dashboard's own preferences (energy_sources, etc.), read
    straight from HA's EnergyManager. None if the Energy dashboard has never
    been configured (manager.data is None until prefs are saved once) or
    anything goes wrong. Must run on the event loop — EnergyManager isn't
    safe to fetch from an executor thread, unlike most of this codebase's
    hass.async_add_executor_job pattern."""
    try:
        from homeassistant.components.energy.data import async_get_manager
        manager = await async_get_manager(hass)
        return manager.data
    except Exception as exc:
        _LOGGER.debug("solar: could not read Energy dashboard prefs: %s", exc)
        return None


def _live_watts(hass, entity_id: Optional[str]) -> Optional[float]:
    """Current reading of a power (rate) sensor in watts. None if missing,
    unavailable, or non-numeric. Handles a kW-reporting sensor the same way
    energy.py already does."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    try:
        val = float(st.state)
    except (ValueError, TypeError):
        return None
    unit = (st.attributes.get("unit_of_measurement") or "").lower()
    if unit == "kw":
        val *= 1000.0
    return val


def _live_pct(hass, entity_id: Optional[str]) -> Optional[float]:
    """Current reading of a percentage-ish sensor (e.g. battery state of
    charge). None if missing, unavailable, or non-numeric."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    try:
        return float(st.state)
    except (ValueError, TypeError):
        return None


def _sum_rate(hass, sources: list[dict]) -> Optional[float]:
    """Sum the live stat_rate across every source of a given type — some
    installs have more than one solar array/inverter. None only when NONE of
    them have a usable reading (as opposed to 0.0, which is a real reading)."""
    total = 0.0
    seen = False
    for src in sources:
        w = _live_watts(hass, src.get("stat_rate"))
        if w is not None:
            total += w
            seen = True
    return total if seen else None


def _grid_direction(hass, grid: dict, grid_w: Optional[float]) -> Optional[str]:
    """Which way power is currently flowing on the grid connection.

    A live-caught bug (13 Sept 2026): a real install's grid rate sensor
    reports POSITIVE while EXPORTING — the opposite of what a "positive =
    drawing from the grid" guess would assume. Sign convention on stat_rate
    isn't standardised across inverter integrations, so it's used only for
    magnitude here, never for direction.

    Direction instead compares which of the two cumulative energy totals
    (stat_energy_from = imported-so-far, stat_energy_to = exported-so-far)
    moved MORE RECENTLY. A state_class: total sensor only changes state
    when it actually accumulates, so whichever one just ticked is the
    direction currently active — true regardless of any inverter's rate-
    sensor sign convention. Falls back to the (unreliable) rate sign only
    when one or both totals aren't configured/available."""
    from_eid = grid.get("stat_energy_from")
    to_eid = grid.get("stat_energy_to")
    from_st = hass.states.get(from_eid) if from_eid else None
    to_st = hass.states.get(to_eid) if to_eid else None
    if from_st is not None and to_st is not None:
        try:
            if to_st.last_changed > from_st.last_changed:
                return "export"
            if from_st.last_changed > to_st.last_changed:
                return "import"
        except TypeError:
            pass  # non-comparable last_changed values (e.g. test fakes) — fall through

    if grid_w is None:
        return None
    if abs(grid_w) < 1.0:
        return "balanced"
    return "import" if grid_w > 0 else "export"


async def solar_status(hass) -> dict:
    """The current solar/battery/grid picture for the panel/agent. Never
    raises. `configured: False` when the Energy dashboard has no solar
    source set up — that's the normal, honest state for anyone who hasn't
    got solar, not an error."""
    prefs = await _read_prefs(hass)
    sources = (prefs or {}).get("energy_sources") or []
    solar_sources = [s for s in sources if s.get("type") == "solar"]
    battery_sources = [s for s in sources if s.get("type") == "battery"]
    grid_sources = [s for s in sources if s.get("type") == "grid"]

    if not solar_sources:
        return {
            "configured": False,
            "solar_w": None,
            "battery_w": None,
            "battery_pct": None,
            "grid_w": None,
            "grid_direction": None,
            "self_sufficiency_pct": None,
            "advice": [
                "No solar source is set up in Home Assistant's Energy "
                "dashboard yet — add one in Settings → Dashboards → "
                "Energy to enable this."
            ],
        }

    solar_w = _sum_rate(hass, solar_sources)

    battery_w = None
    battery_pct = None
    if battery_sources:
        battery = battery_sources[0]
        battery_w = _live_watts(hass, battery.get("stat_rate"))
        battery_pct = _live_pct(hass, battery.get("stat_soc"))

    grid_w = None
    grid_direction = None
    if grid_sources:
        grid = grid_sources[0]
        grid_w = _live_watts(hass, grid.get("stat_rate"))
        grid_direction = _grid_direction(hass, grid, grid_w)

    self_sufficiency_pct = None
    if solar_w is not None:
        grid_import_w = grid_w if (grid_direction == "import" and grid_w is not None) else 0.0
        denom = solar_w + grid_import_w
        self_sufficiency_pct = round((solar_w / denom) * 100, 1) if denom > 0 else 100.0

    advice = []
    if solar_w is not None:
        line = f"Generating {solar_w / 1000:.1f} kW of solar right now."
        if grid_direction == "export":
            line += f" Exporting {abs(grid_w) / 1000:.1f} kW to the grid."
        elif grid_direction == "import":
            line += f" Still importing {grid_w / 1000:.1f} kW from the grid."
        if battery_pct is not None:
            line += f" Battery at {battery_pct:.0f}%."
        advice.append(line)

    return {
        "configured": True,
        "solar_w": round(solar_w) if solar_w is not None else None,
        "battery_w": round(battery_w) if battery_w is not None else None,
        "battery_pct": round(battery_pct, 1) if battery_pct is not None else None,
        "grid_w": round(grid_w) if grid_w is not None else None,
        "grid_direction": grid_direction,
        "self_sufficiency_pct": self_sufficiency_pct,
        "advice": advice,
    }
