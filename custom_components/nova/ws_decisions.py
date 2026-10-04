"""The decision record, calibration and analysis commands.

Moved verbatim out of websocket.py: nova/list_decisions, nova/get_decision,
nova/set_decision_outcome, nova/replay_decision, nova/get_calibration,
nova/run_analysis, nova/get_cognitive_status and nova/root_cause, with the
helpers they use. The logger keeps the name it had in websocket.py.

websocket.py imports every handler by name (async_register registers them).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_panel_stats import _entity_names, _named_decision

_LOGGER = logging.getLogger(f"{__package__}.websocket")


@websocket_api.websocket_command({
    vol.Required("type"): "nova/root_cause",
    vol.Required("entity_id"): str,
    vol.Optional("event_time"): str,
    vol.Optional("window_secs"): int,
})
@websocket_api.async_response
async def ws_root_cause(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Root cause analysis for an entity's (latest or specified) change —
    the same engine the conversational 'why did …' tool uses, structured for
    the panel."""
    try:
        from . import rca
        names = rca.entity_names(hass)
        result = await hass.async_add_executor_job(
            lambda: rca.analyze(
                msg["entity_id"],
                msg.get("event_time"),
                int(msg.get("window_secs") or rca.DEFAULT_WINDOW_SECS),
                names=names))
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("root_cause failed: %s", exc)
        connection.send_error(msg["id"], "root_cause_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_calibration",
})
@websocket_api.async_response
async def ws_get_calibration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Confidence calibration + interruption-budget health for the dashboard."""
    try:
        from . import decision_record
        payload = {
            "calibration": decision_record.calibration(),
            "interruption_budget": decision_record.interruption_budget(),
            "stats": decision_record.stats(),
            "suggestion": decision_record.outcome_rate("suggestion"),
            "anticipation": decision_record.outcome_rate(
                "anticipation", None, None, True),
        }
        try:
            from . import adaptive_awareness
            payload["adaptive_awareness"] = adaptive_awareness.status()
        except Exception:
            pass
        try:
            from .automation import patterns as pattern_analyzer
            payload["suggestion_threshold"] = {
                "base": round(pattern_analyzer.CONFIDENCE_THRESHOLD, 3),
                "effective": round(pattern_analyzer._effective_threshold(), 3),
                "learned_delta": round(pattern_analyzer._learned_threshold_delta(), 3),
            }
        except Exception:
            pass
        connection.send_result(msg["id"], payload)
    except Exception as exc:
        connection.send_result(msg["id"], {
            "calibration": {"n": 0}, "interruption_budget": {"judged": 0},
            "error": safe_error_message(exc, where="get_calibration", log=True),
        })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_decisions",
    vol.Optional("kind"): str,
    vol.Optional("only_unjudged", default=False): bool,
    vol.Optional("limit", default=50): int,
    vol.Optional("cursor_ts"): vol.Coerce(float),
    vol.Optional("cursor_id"): int,
})
@websocket_api.async_response
async def ws_list_decisions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Bounded, cursor-paginated Decision Record browser for the Logs tab.
    Summary rows only (no observation/interpretation/evidence) — full detail
    is a separate nova/get_decision call."""
    try:
        from . import decision_record
        limit = max(1, min(int(msg.get("limit", 50)), 200))
        result = await hass.async_add_executor_job(
            lambda: decision_record.page(
                limit=limit,
                kind=msg.get("kind"),
                only_unjudged=bool(msg.get("only_unjudged", False)),
                cursor_ts=msg.get("cursor_ts"),
                cursor_id=msg.get("cursor_id"),
            )
        )
        names = _entity_names(hass)
        connection.send_result(msg["id"], {
            "decisions": [_named_decision(d, names) for d in result["items"]],
            "next_cursor": result["next_cursor"],
        })
    except Exception as exc:
        _LOGGER.exception("ws_list_decisions failed: %s", exc)
        connection.send_error(msg["id"], "list_decisions_failed", safe_error_message(exc))


_DECISION_FIELD_MAX_CHARS = 500


def _bound_decision_strings(obj):
    """Recursively cap every string at _DECISION_FIELD_MAX_CHARS. The
    observation/interpretation/evidence blobs can carry free text (a calendar
    event title, a routine description) with no length limit enforced at
    write time (decision_record._js() has none) — this bounds it before it
    ever reaches the panel. Never raises."""
    try:
        if isinstance(obj, str):
            return obj if len(obj) <= _DECISION_FIELD_MAX_CHARS else (
                obj[:_DECISION_FIELD_MAX_CHARS] + "…")
        if isinstance(obj, dict):
            return {k: _bound_decision_strings(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_bound_decision_strings(v) for v in obj]
        return obj
    except Exception:
        return obj


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_decision",
    vol.Required("decision_id"): int,
})
@websocket_api.async_response
async def ws_get_decision(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Full detail for one Decision Record — the drawer behind nova/list_decisions.
    Defence in depth (today's writers put nothing sensitive here — verified):
    redacted the same way the config-entry diagnostics dump already is, then
    string-bounded, before this ever reaches the panel."""
    try:
        from . import decision_record
        from .diagnostics import _redact
        rec = await hass.async_add_executor_job(decision_record.get, msg["decision_id"])
        if rec is None:
            connection.send_error(msg["id"], "not_found", "decision not found")
            return
        connection.send_result(
            msg["id"], {"decision": _bound_decision_strings(
                _named_decision(_redact(rec), _entity_names(hass)))})
    except Exception as exc:
        _LOGGER.exception("ws_get_decision failed: %s", exc)
        connection.send_error(msg["id"], "get_decision_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/set_decision_outcome",
    vol.Required("decision_id"): int,
    vol.Required("verdict"): vol.In(["good", "unnecessary", "wrong"]),
})
@websocket_api.async_response
async def ws_set_decision_outcome(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Record Helpful/Unnecessary/Wrong feedback on a Decision Record.
    Set-once: an already-judged record reports "already_judged", not a silent
    no-op, so the panel can tell the two apart from "not_found"."""
    try:
        from . import decision_record
        status = await hass.async_add_executor_job(
            decision_record.set_outcome_checked, msg["decision_id"], msg["verdict"], "panel")
        connection.send_result(msg["id"], {"status": status})
    except Exception as exc:
        _LOGGER.exception("ws_set_decision_outcome failed: %s", exc)
        connection.send_error(msg["id"], "set_decision_outcome_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/replay_decision",
    vol.Required("decision_id"): int,
})
@websocket_api.async_response
async def ws_replay_decision(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Read-only Decision Lab replay — current policy only, never a
    historical reconstruction (see replay.replay_one's docstring). No writes,
    no service calls, no LLM/cloud calls: replay_one takes a plain record
    dict, not hass, so it has no way to perform any of those even by
    accident."""
    try:
        from . import decision_record, replay
        rec = await hass.async_add_executor_job(decision_record.get, msg["decision_id"])
        if rec is None:
            connection.send_error(msg["id"], "not_found", "decision not found")
            return
        connection.send_result(msg["id"], replay.replay_one(rec))
    except Exception as exc:
        _LOGGER.exception("ws_replay_decision failed: %s", exc)
        connection.send_error(msg["id"], "replay_decision_failed", safe_error_message(exc))


def _name_diagnostic(res, names: dict) -> None:
    """Add a `name` beside each entity_id in the analysis diagnostic, in
    place. Never raises."""
    try:
        dg = res.get("diagnostic") if isinstance(res, dict) else None
        if not isinstance(dg, dict):
            return
        from .cognitive.naming import name_for
        for key in ("candidates", "top_sources"):
            for row in dg.get(key) or []:
                if isinstance(row, dict) and row.get("entity_id"):
                    row["name"] = name_for(row["entity_id"], names)
    except Exception:
        pass


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/run_analysis",
})
@websocket_api.async_response
async def ws_run_analysis(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Force a pattern-analysis pass now (manual 'Analyze Now')."""
    try:
        from . import cognitive_core
        res = await cognitive_core.run_analysis_now(hass)
        _name_diagnostic(res, _entity_names(hass))
        connection.send_result(msg["id"], res)
    except Exception as exc:
        connection.send_result(msg["id"], {"ran": False, "error": safe_error_message(exc, where="analyze_now", log=True)})


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_cognitive_status",
})
@websocket_api.async_response
async def ws_get_cognitive_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return Nova cognitive core status for the dashboard."""
    try:
        from . import cognitive_core
        status = cognitive_core.status()
        connection.send_result(msg["id"], status)
    except Exception as exc:
        connection.send_result(msg["id"], {
            "running": False,
            "error": safe_error_message(exc, where="get_cognitive_status", log=True),
            "learning": {},
        })
