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

# Solar-forecast integrations HA core ships that share the same sensor
# unique_id key scheme (verified live, Sept 2026: an install's Energy
# dashboard `config_entry_solar_forecast` link had gone stale — pointed at a
# deleted config entry after switching forecast providers — so discovery
# here goes by platform + the entities' own stable unique_id suffix instead
# of trusting that cross-reference).
_FORECAST_PLATFORMS = ("forecast_solar", "open_meteo_solar_forecast")
_FORECAST_KEY_SUFFIXES = {
    "energy_production_today": "forecast_today_total_kwh",
    "energy_production_today_remaining": "forecast_remaining_kwh",
    "energy_production_tomorrow": "forecast_tomorrow_kwh",
}


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


# ── daily report: today's totals, forecast remaining, and cost ──────────────

async def _daily_sum(hass, entity_id: Optional[str]) -> Optional[float]:
    """How much a cumulative energy sensor (state_class total/total_increasing)
    has accumulated since local midnight. Reset-safe: sums only the positive
    deltas between consecutive recorded states, so a meter rollover/reset
    partway through the day doesn't get read as a huge negative swing or
    corrupt the total. None if there's no recorded history yet (e.g. the
    entity was only just added) or on any failure. Never raises.

    Deliberately built on `recorder.history.get_significant_states` (already
    proven elsewhere in this codebase, see activity_history.py) rather than
    `recorder.statistics.statistics_during_period` — the latter's row shape
    isn't something this codebase has exercised before, and getting a
    reset-compensation edge case wrong would silently misreport real kWh/cost
    figures."""
    if not entity_id:
        return None
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
    except Exception:
        return None

    now = dt_util.utcnow()
    local_midnight = dt_util.as_local(now).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = dt_util.as_utc(local_midnight)

    def _fetch():
        # include_start_time_state (default True) prepends the value that
        # was already current at local midnight -- our baseline, even if it
        # didn't change again until later in the day.
        return history.get_significant_states(
            hass, start, now, [entity_id], minimal_response=True, no_attributes=True)

    try:
        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        _LOGGER.debug("solar: daily_sum history fetch failed for %s: %s", entity_id, exc)
        return None

    rows = (raw or {}).get(entity_id) or []
    values = []
    for row in rows:
        raw_state = row.get("state") if isinstance(row, dict) else getattr(row, "state", None)
        try:
            values.append(float(raw_state))
        except (TypeError, ValueError):
            continue
    if len(values) < 2:
        return None

    total = 0.0
    for prev, cur in zip(values, values[1:]):
        if cur >= prev:
            total += cur - prev
        # a drop means the counter reset -- skip it; the next rise accumulates
        # fresh from wherever the counter restarted.
    return round(total, 3)


def _forecast_values(hass) -> dict:
    """today/remaining-today/tomorrow straight off an installed solar-forecast
    integration, matched by platform + the stable unique_id suffix both
    Forecast.Solar and Open-Meteo Solar Forecast share -- not by the Energy
    dashboard's config_entry_solar_forecast link, which can go stale. Empty
    dict if no such integration is installed. Never raises."""
    out: dict = {}
    try:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        for entry in reg.entities.values():
            platform = getattr(entry, "platform", None)
            uid = getattr(entry, "unique_id", None)
            if platform not in _FORECAST_PLATFORMS or not uid:
                continue
            for suffix, out_key in _FORECAST_KEY_SUFFIXES.items():
                if uid.endswith(suffix) and out_key not in out:
                    val = _live_pct(hass, entry.entity_id)  # plain numeric read
                    if val is not None:
                        out[out_key] = round(val, 2)
                    break
    except Exception as exc:
        _LOGGER.debug("solar: forecast entity discovery failed: %s", exc)
    return out


def _resolve_price(hass, entity_field: Optional[str], number_field) -> Optional[float]:
    """A price-per-kWh from the Energy dashboard's own config: a live entity
    if one is set, else a fixed number. None if neither is configured."""
    if entity_field:
        v = _live_pct(hass, entity_field)
        if v is not None:
            return v
    if number_field is not None:
        try:
            return float(number_field)
        except (TypeError, ValueError):
            pass
    return None


async def _cost_today(hass, imported_kwh: Optional[float], exported_kwh: Optional[float]
                       ) -> tuple[Optional[dict], str]:
    """Today's cost, and where it came from. Prefers a user-declared cost
    sensor (Settings: energy_cost_today_entity / energy_cost_net_entity) when
    configured -- exact for installs that already track tariff cost
    themselves. Falls back to a plain estimate (imported kWh x current price,
    minus exported kWh x export price) from the Energy dashboard's own price
    fields, clearly labeled as an estimate since it can't account for a price
    that changed during the day. (None, "unavailable") if neither is possible.
    Never raises, except NovaRuntimeUnavailable when the Nova entry is loaded
    but has lost its runtime (its settings would otherwise be made up)."""
    from .runtime import NovaRuntimeUnavailable
    try:
        from .const import DOMAIN
        from . import nova_config
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
        today_eid = nova_config.runtime_get(hass, entry, "energy_cost_today_entity", "") or ""
        net_eid = nova_config.runtime_get(hass, entry, "energy_cost_net_entity", "") or ""
    except NovaRuntimeUnavailable:
        raise
    except Exception:
        today_eid = net_eid = ""

    if today_eid:
        today_val = _live_pct(hass, today_eid)
        if today_val is not None:
            net_val = _live_pct(hass, net_eid) if net_eid else None
            return {"today": round(today_val, 2), "net_today":
                    round(net_val, 2) if net_val is not None else None}, "configured_entity"

    if imported_kwh is None:
        return None, "unavailable"

    try:
        prefs = await _read_prefs(hass)
        grid_sources = [s for s in (prefs or {}).get("energy_sources") or []
                        if s.get("type") == "grid"]
    except Exception:
        grid_sources = []
    if not grid_sources:
        return None, "unavailable"

    grid = grid_sources[0]
    price = _resolve_price(hass, grid.get("entity_energy_price"), grid.get("number_energy_price"))
    if price is None:
        return None, "unavailable"
    export_price = _resolve_price(
        hass, grid.get("entity_energy_price_export"), grid.get("number_energy_price_export"))

    cost = imported_kwh * price
    credit = (exported_kwh or 0.0) * (export_price or 0.0)
    return {"today": round(cost, 2), "net_today": round(cost - credit, 2)}, "estimate"


async def daily_report(hass) -> dict:
    """Today's solar/energy picture for the panel/agent: generated, self-
    consumed, exported, imported, battery flow, forecast remaining/tomorrow,
    and cost -- built from the same Energy dashboard configuration
    solar_status() reads, using the recorder's own daily history rather than
    any Nova-specific entity assumption. Never raises. `configured: False`
    mirrors solar_status when no solar source is set up."""
    prefs = await _read_prefs(hass)
    sources = (prefs or {}).get("energy_sources") or []
    solar_sources = [s for s in sources if s.get("type") == "solar"]
    battery_sources = [s for s in sources if s.get("type") == "battery"]
    grid_sources = [s for s in sources if s.get("type") == "grid"]

    if not solar_sources:
        return {
            "configured": False,
            "advice": [
                "No solar source is set up in Home Assistant's Energy "
                "dashboard yet — add one in Settings → Dashboards → "
                "Energy to enable this."
            ],
        }

    generated = None
    for src in solar_sources:
        v = await _daily_sum(hass, src.get("stat_energy_from"))
        if v is not None:
            generated = (generated or 0.0) + v

    imported = exported = None
    if grid_sources:
        grid = grid_sources[0]
        imported = await _daily_sum(hass, grid.get("stat_energy_from"))
        exported = await _daily_sum(hass, grid.get("stat_energy_to"))

    battery_charged = battery_discharged = None
    if battery_sources:
        battery = battery_sources[0]
        battery_discharged = await _daily_sum(hass, battery.get("stat_energy_from"))
        battery_charged = await _daily_sum(hass, battery.get("stat_energy_to"))

    self_consumed = None
    if generated is not None:
        self_consumed = max(0.0, generated - (exported or 0.0))

    consumed_today = None
    if self_consumed is not None or imported is not None:
        consumed_today = (self_consumed or 0.0) + (imported or 0.0)

    forecast = _forecast_values(hass)
    cost, cost_source = await _cost_today(hass, imported, exported)

    advice = []
    if generated is not None:
        line = f"Generated {generated:.1f} kWh of solar today."
        if forecast.get("forecast_remaining_kwh") is not None:
            line += f" ~{forecast['forecast_remaining_kwh']:.1f} kWh more expected today."
        advice.append(line)
    if consumed_today is not None:
        advice.append(f"Used {consumed_today:.1f} kWh today.")
    if cost is not None:
        note = " (estimate)" if cost_source == "estimate" else ""
        advice.append(f"Cost today: {cost['today']:.2f}{note}.")

    return {
        "configured": True,
        "generated_today_kwh": round(generated, 2) if generated is not None else None,
        "self_consumed_kwh": round(self_consumed, 2) if self_consumed is not None else None,
        "exported_kwh": round(exported, 2) if exported is not None else None,
        "imported_kwh": round(imported, 2) if imported is not None else None,
        "consumed_today_kwh": round(consumed_today, 2) if consumed_today is not None else None,
        "battery_charged_kwh": round(battery_charged, 2) if battery_charged is not None else None,
        "battery_discharged_kwh": round(battery_discharged, 2) if battery_discharged is not None else None,
        "forecast_today_total_kwh": forecast.get("forecast_today_total_kwh"),
        "forecast_remaining_kwh": forecast.get("forecast_remaining_kwh"),
        "forecast_tomorrow_kwh": forecast.get("forecast_tomorrow_kwh"),
        "cost_today": cost.get("today") if cost else None,
        "net_cost_today": cost.get("net_today") if cost else None,
        "cost_source": cost_source,
        "advice": advice,
    }
