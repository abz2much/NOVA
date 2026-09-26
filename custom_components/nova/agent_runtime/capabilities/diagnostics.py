"""Cognitive-core, connectivity and service diagnostics."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_cognitive_status(hass: HomeAssistant, args: dict) -> str:
    """Get cognitive core status and learning stats."""
    try:
        from ... import cognitive_core
        from ...automation.patterns import get_analyzer
        status = cognitive_core.status()
        analyzer = get_analyzer()
        status["pattern_analysis"] = await hass.async_add_executor_job(
            analyzer.get_stats)
        return json.dumps(status)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_connectivity_status(hass: HomeAssistant, args: dict) -> str:
    """Get cloud LLM connectivity / circuit-breaker status."""
    try:
        from ... import connectivity
        return json.dumps(connectivity.status())
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_root_cause(hass: HomeAssistant, args: dict) -> str:
    """Root-cause analysis: gather evidence in an executor, return a compact
    findings block the model can narrate from."""
    from ... import rca
    entity_id = (args.get("entity_id") or "").strip()
    if not entity_id:
        return "root_cause needs an entity_id (resolve names with search_entities first)."
    event_time = (args.get("event_time") or "").strip() or None
    try:
        window = int(float(args.get("window_minutes") or 30) * 60)
    except (TypeError, ValueError):
        window = rca.DEFAULT_WINDOW_SECS
    result = await hass.async_add_executor_job(
        lambda: rca.analyze(entity_id, event_time, window))

    ev = result.get("event") or {}
    from ..presentation import display_name
    shown = display_name(hass, entity_id)
    lines = [f"Root cause analysis for {shown}"
             + (f" ({entity_id}):" if shown != entity_id else ":")]
    if ev.get("timestamp"):
        lines.append(f"Event: {ev.get('old_state')} → {ev.get('new_state')} "
                     f"at {ev['timestamp']}"
                     + (f" (area {ev['area_id']})" if ev.get("area_id") else ""))
    lines.append(f"Verdict: {result.get('summary', '')}")
    cands = result.get("candidates") or []
    if cands:
        lines.append("Ranked causes:")
        for i, c in enumerate(cands, 1):
            lines.append(f"  {i}. [{int(c['confidence'] * 100)}%] "
                         f"{c['cause']} — {c['evidence']}")
    tl = result.get("timeline") or []
    if tl:
        lines.append("Timeline (most recent last):")
        for item in tl[-12:]:
            lines.append(f"  {item['t']} [{item['src']}] {item['text']}")
    return "\n".join(lines)


async def _exec_system_diagnostics(hass: HomeAssistant, args: dict) -> str:
    """Report core dependency health (LLM, embeddings, TTS, STT) (v6.60.0)."""
    try:
        from ... import diagnostics
        res = await diagnostics.run_service_health(hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
