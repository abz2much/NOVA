"""Diagnostic tools for the agent: the cognitive core, connectivity, root cause
analysis and service health."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

from ...safe_errors import tool_error_text

# The agent logs under …nova.agent from every module, so filters and levels
# set for it keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _error_reply(exc: Exception, tool: str) -> str:
    return json.dumps({"error": tool_error_text(exc, tool)})


async def _exec_cognitive_status(hass: HomeAssistant, args: dict) -> str:
    """Status of the cognitive core, with the learning statistics."""
    try:
        from ... import cognitive_core
        from ...automation.patterns import get_analyzer
        report = cognitive_core.status()
        stats = get_analyzer().get_stats
        report["pattern_analysis"] = await hass.async_add_executor_job(stats)
        return json.dumps(report)
    except Exception as exc:
        return _error_reply(exc, "cognitive_status")


async def _exec_connectivity_status(hass: HomeAssistant, args: dict) -> str:
    """State of the cloud LLM connection and its circuit breaker."""
    try:
        from ... import connectivity
        return json.dumps(connectivity.status())
    except Exception as exc:
        return _error_reply(exc, "connectivity_status")


def _window_seconds(minutes, default: int) -> int:
    """The analysis window in seconds: minutes as given, 30 when none, and the
    default when the value is not a number."""
    try:
        return int(float(minutes or 30) * 60)
    except (TypeError, ValueError):
        return default


def _root_cause_report(shown: str, entity_id: str, result: dict) -> str:
    """The findings of a root cause analysis as the text the model narrates."""
    out = [f"Root cause analysis for {shown}"
           + (f" ({entity_id}):" if shown != entity_id else ":")]
    event = result.get("event") or {}
    if event.get("timestamp"):
        area = f" (area {event['area_id']})" if event.get("area_id") else ""
        out.append(f"Event: {event.get('old_state')} → {event.get('new_state')} "
                   f"at {event['timestamp']}{area}")
    out.append(f"Verdict: {result.get('summary', '')}")
    candidates = result.get("candidates") or []
    if candidates:
        out.append("Ranked causes:")
        out.extend(
            f"  {rank}. [{int(item['confidence'] * 100)}%] "
            f"{item['cause']} — {item['evidence']}"
            for rank, item in enumerate(candidates, 1))
    timeline = result.get("timeline") or []
    if timeline:
        out.append("Timeline (most recent last):")
        out.extend(f"  {row['t']} [{row['src']}] {row['text']}"
                   for row in timeline[-12:])
    return "\n".join(out)


async def _exec_root_cause(hass: HomeAssistant, args: dict) -> str:
    """Work out why an entity changed. The evidence is gathered in an executor
    and returned as a short findings block the model can narrate from."""
    from ... import rca
    entity_id = (args.get("entity_id") or "").strip()
    if not entity_id:
        return "root_cause needs an entity_id (resolve names with search_entities first)."
    event_time = (args.get("event_time") or "").strip() or None
    window = _window_seconds(args.get("window_minutes"), rca.DEFAULT_WINDOW_SECS)
    names = rca.entity_names(hass)

    def gather():
        return rca.analyze(entity_id, event_time, window, names=names)

    result = await hass.async_add_executor_job(gather)

    from ..presentation import display_name
    return _root_cause_report(display_name(hass, entity_id), entity_id, result)


async def _exec_system_diagnostics(hass: HomeAssistant, args: dict) -> str:
    """Health of the services Nova depends on: LLM, embeddings, TTS and STT
    (v6.60.0)."""
    try:
        from ... import diagnostics
        return json.dumps(await diagnostics.run_service_health(hass))
    except Exception as exc:
        return _error_reply(exc, "system_diagnostics")
