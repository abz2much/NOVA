"""Operational modes and security-alert handling."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_set_mode(hass: HomeAssistant, args: dict) -> str:
    """Switch the operational mode (Directive Layer, v6.61.0)."""
    try:
        from ... import modes
        res = await hass.async_add_executor_job(
            modes.set_mode, args.get("mode", ""), args.get("reason", ""))
        if res.get("ok"):
            try:
                from ... import mode_scene
                # Note: this tool's dispatch signature carries no device_id
                # (same disclosed limitation as _exec_run_scene_script).
                await mode_scene.apply_mode_entry(hass, res["mode"], source="chat")
            except Exception:
                pass
            info = modes.mode_info()
            try:
                from ...websocket import nova_log
                nova_log("MODE", f"mode → {res['mode']}"
                                   + (f" ({args.get('reason')})" if args.get("reason") else ""))
            except Exception:
                pass
            return json.dumps({
                "ok": True, "mode": res["mode"],
                "description": info.get("description", ""),
                "note": "safety (pipe-freeze, intrusion, lockdown) remains fully "
                        "active in every mode",
            })
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_acknowledge_alert(hass: HomeAssistant, args: dict) -> str:
    """Acknowledge an alert without calling it off — holds auto-escalation (v6.69.0)."""
    try:
        from ... import intrusion
        res = intrusion.acknowledge(args.get("reason", ""))
        return json.dumps({"ok": True, **res,
                           "message": "Acknowledged — holding the automatic alert. "
                                      "I'll still escalate if I see a person on camera."})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def _exec_dismiss_intrusion(hass: HomeAssistant, args: dict) -> str:
    """Call off an active intrusion as a false alarm (v6.68.0)."""
    try:
        from ... import intrusion, cognitive_core
        res = intrusion.dismiss_intrusion(args.get("reason", ""))
        # Also clear any live investigation in the SafetyManager immediately.
        try:
            core = getattr(cognitive_core, "_CORE", None)
            if core and getattr(core, "safety_mgr", None):
                core.safety_mgr._investigation = None
        except Exception:
            pass
        try:
            from ...websocket import nova_log
            nova_log("SAFETY", "Intrusion called off by user (false alarm)"
                       + (f": {args.get('reason')}" if args.get("reason") else ""))
        except Exception:
            pass
        return json.dumps({"ok": True, **res,
                           "message": "Intrusion called off. Standing down."})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
