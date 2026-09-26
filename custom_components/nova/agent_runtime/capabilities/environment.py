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
    """Report whole-home power draw + energy advice (v6.62.0)."""
    try:
        from ... import energy
        res = await hass.async_add_executor_job(energy.power_status, hass)
        return json.dumps(res)
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


async def _exec_hazard_report(hass: HomeAssistant, args: dict) -> str:
    """Live nearby hazard scan — earthquakes, severe weather, disasters (v6.71.0)."""
    try:
        from ... import hazard_monitor
        res = await hazard_monitor.scan_now(hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_weather_forecast(hass: HomeAssistant, args: dict) -> str:
    """Hourly/daily forecast from a HA weather entity (v6.75.0). Nova could
    previously only see CURRENT conditions, so 'what time is it supposed to
    rain?' had no real answer and the model fell back to HA's clock intent.
    This exposes the actual forecast."""
    try:
        kind = str(args.get("kind", "hourly") or "hourly").lower()
        if kind not in ("hourly", "daily", "twice_daily"):
            kind = "hourly"
        # pick the requested entity, else the first weather.* entity
        eid = args.get("entity_id")
        if not eid:
            for s in hass.states.async_all("weather"):
                eid = s.entity_id
                break
        if not eid:
            return json.dumps({"error": "no weather entity is configured in "
                                        "Home Assistant"})
        try:
            res = await hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": eid, "type": kind},
                blocking=True, return_response=True,
            )
        except Exception as exc:
            # some entities don't support every forecast type
            if kind != "daily":
                try:
                    res = await hass.services.async_call(
                        "weather", "get_forecasts",
                        {"entity_id": eid, "type": "daily"},
                        blocking=True, return_response=True,
                    )
                    kind = "daily"
                except Exception:
                    return json.dumps({"error": f"forecast unavailable: {exc}"})
            else:
                return json.dumps({"error": f"forecast unavailable: {exc}"})

        entries = []
        try:
            data = (res or {}).get(eid, {})
            for f in (data.get("forecast") or [])[:24]:
                entry = {
                    "datetime": f.get("datetime"),
                    "condition": f.get("condition"),
                    "temperature": f.get("temperature"),
                }
                # precipitation fields vary by integration — include what exists
                for k in ("precipitation", "precipitation_probability",
                          "templow", "wind_speed", "humidity"):
                    if f.get(k) is not None:
                        entry[k] = f.get(k)
                entries.append(entry)
        except Exception as exc:
            return json.dumps({"error": f"could not read forecast: {exc}"})

        cur = hass.states.get(eid)
        return json.dumps({
            "entity_id": eid,
            "type": kind,
            "current": {
                "condition": cur.state if cur else None,
                "temperature": (cur.attributes.get("temperature") if cur else None),
            },
            "forecast": entries,
            "note": ("Each entry's 'datetime' is when that forecast period "
                     "begins; use condition/precipitation to say WHEN rain is "
                     "expected."),
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})
