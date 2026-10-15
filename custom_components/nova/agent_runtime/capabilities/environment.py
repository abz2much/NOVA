"""Energy, solar, hazards, weather and wellbeing telemetry."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_wellbeing_context(hass: HomeAssistant, args: dict) -> str:
    """Read non-medical wellbeing context from a wearable (v6.63.0).

    Only reaches the model when the main agent's configured LLM provider is
    local (Ollama) — v7.87.0. Biometric data (heart rate, sleep stage, etc.)
    must never leave the network to a cloud provider; README previously
    claimed this already, which wasn't true until this gate existed."""
    try:
        from ... import biometrics, nova_config
        provider = await hass.async_add_executor_job(nova_config.get, "llm_provider", "groq")
        if provider != "ollama":
            return json.dumps({
                "available": False,
                "summary": "wellbeing context is only available with a local "
                           "(Ollama) LLM provider, to keep biometric data off "
                           "the network",
            })
        res = await hass.async_add_executor_job(biometrics.wellbeing_context, hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_energy_status(hass: HomeAssistant, args: dict) -> str:
    """The whole home's power draw with energy advice."""
    try:
        from ... import energy
        return json.dumps(await hass.async_add_executor_job(energy.power_status, hass))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_solar_status(hass: HomeAssistant, args: dict) -> str:
    """Report solar/battery/grid picture, read from HA's own Energy
    dashboard config (v7.91.0). Async, awaited directly (not an executor
    job) — solar.solar_status needs the event loop for EnergyManager."""
    try:
        from ... import solar
        res = await solar.solar_status(hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_energy_report(hass: HomeAssistant, args: dict) -> str:
    """Report today's solar/energy totals, forecast remaining, and cost
    (v7.101.13). Async, awaited directly — solar.daily_report needs the
    event loop for EnergyManager and the recorder executor internally."""
    try:
        from ... import solar
        res = await solar.daily_report(hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_energy_outlook(hass: HomeAssistant, args: dict) -> str:
    """The 36 hour energy outlook and its advice (8.12.0). Advice only.
    Async, awaited directly: it reads EnergyManager on the event loop and
    the recorder through its executor. hours sets best_window's length."""
    try:
        from ... import energy_outlook
        try:
            hours = max(1, min(12, int(args.get("hours", 2) or 2)))
        except (TypeError, ValueError):
            hours = 2
        res = await energy_outlook.energy_outlook_status(hass, hours=hours)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_hazard_report(hass: HomeAssistant, args: dict) -> str:
    """A live scan for hazards nearby: earthquakes, severe weather and
    disasters, plus the weather warnings for the chosen counties or areas
    under "warnings" (county, type, level, from, to and the headline as
    published)."""
    try:
        from ... import hazard_monitor
        return json.dumps(await hazard_monitor.scan_now(hass))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


_FORECAST_KINDS = ("hourly", "daily", "twice_daily")
_FORECAST_LIMIT = 24
# Fields not every weather integration gives; passed on when present.
_FORECAST_EXTRAS = ("precipitation", "precipitation_probability", "templow",
                    "wind_speed", "humidity")
_FORECAST_NOTE = ("Each entry's 'datetime' is when that forecast period begins; "
                  "use condition/precipitation to say WHEN rain is expected.")


async def _forecast(hass, entity_id: str, kind: str):
    return await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": entity_id, "type": kind},
        blocking=True, return_response=True)


def _forecast_entries(reply, entity_id: str) -> list[dict]:
    periods = ((reply or {}).get(entity_id, {}).get("forecast") or [])[:_FORECAST_LIMIT]
    entries = []
    for period in periods:
        entry = {key: period.get(key) for key in ("datetime", "condition", "temperature")}
        entry.update({key: period.get(key) for key in _FORECAST_EXTRAS
                      if period.get(key) is not None})
        entries.append(entry)
    return entries


async def _exec_weather_forecast(hass: HomeAssistant, args: dict) -> str:
    """The forecast from a Home Assistant weather entity: hourly by default,
    daily or twice daily on request. With only current conditions, "what
    time is it supposed to rain?" had no answer and the model fell back to
    the clock. An entity that cannot give the type asked for is tried for
    daily instead."""
    try:
        kind = str(args.get("kind", "hourly") or "hourly").lower()
        if kind not in _FORECAST_KINDS:
            kind = "hourly"
        entity_id = args.get("entity_id") or next(
            (st.entity_id for st in hass.states.async_all("weather")), None)
        if not entity_id:
            return json.dumps({"error": "no weather entity is configured in "
                                        "Home Assistant"})
        try:
            reply = await _forecast(hass, entity_id, kind)
        except Exception as exc:
            if kind == "daily":
                return json.dumps({"error": f"forecast unavailable: {exc}"})
            try:
                reply = await _forecast(hass, entity_id, "daily")
            except Exception:
                return json.dumps({"error": f"forecast unavailable: {exc}"})
            kind = "daily"
        try:
            entries = _forecast_entries(reply, entity_id)
        except Exception as exc:
            return json.dumps({"error": f"could not read forecast: {exc}"})
        now = hass.states.get(entity_id)
        return json.dumps({
            "entity_id": entity_id,
            "type": kind,
            "current": {"condition": now.state if now else None,
                        "temperature": now.attributes.get("temperature") if now else None},
            "forecast": entries,
            "note": _FORECAST_NOTE,
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})
