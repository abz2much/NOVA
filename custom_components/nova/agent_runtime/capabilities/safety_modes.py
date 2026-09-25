"""Operational modes and security-alert handling."""
from __future__ import annotations

import json
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


def _source(hass, device_id: Optional[str]) -> str:
    """The audit source: 'voice' for a voice satellite, otherwise 'chat'."""
    try:
        from ...voice_confirm import is_voice_satellite_device
        return "voice" if is_voice_satellite_device(hass, device_id or "") else "chat"
    except Exception:
        return "chat"


async def _audit_start(hass, action: str, service: str, device_id: Optional[str],
                       user_id: Optional[str]) -> Optional[int]:
    """One action-audit row for a security-alert action, attributed to the
    request's real user and device (None when there is none)."""
    from ... import action_log
    request_id = action_log.new_request_id()
    return await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, action, _source(hass, device_id),
            requested_by_user_id=user_id or None,
            request_device_id=device_id or None,
            domain="nova", service=service,
        )
    )


async def _exec_set_mode(hass: HomeAssistant, args: dict, device_id: Optional[str] = None,
                         user_id: Optional[str] = None) -> str:
    """Switch the operational mode (Directive Layer, v6.61.0)."""
    try:
        from ... import modes
        res = await hass.async_add_executor_job(
            modes.set_mode, args.get("mode", ""), args.get("reason", ""))
        if res.get("ok"):
            try:
                from ... import mode_scene
                await mode_scene.apply_mode_entry(
                    hass, res["mode"], source=_source(hass, device_id),
                    requested_by_user_id=user_id or None)
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


async def _exec_acknowledge_alert(hass: HomeAssistant, args: dict,
                                  device_id: Optional[str] = None,
                                  user_id: Optional[str] = None) -> str:
    """Acknowledge an alert without calling it off — holds auto-escalation (v6.69.0).

    Not confirmation-gated: it only records that the alert was seen and holds
    the no-response escalation; evidence-based escalation (a person on
    camera) still fires, so it cannot disable protection. It is audited with
    the request's real user and device."""
    from ... import action_log
    action_id = await _audit_start(hass, "acknowledge_alert", "acknowledge",
                                   device_id, user_id)
    try:
        from ... import intrusion
        res = intrusion.acknowledge(args.get("reason", ""))
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted"))
        return json.dumps({"ok": True, **res,
                           "message": "Acknowledged — holding the automatic alert. "
                                      "I'll still escalate if I see a person on camera."})
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed",
                                             reason_code="acknowledge_failed"))
        return json.dumps({"error": str(exc)})


async def _exec_dismiss_intrusion(hass: HomeAssistant, args: dict,
                                  device_id: Optional[str] = None,
                                  user_id: Optional[str] = None) -> str:
    """Call off an active intrusion as a false alarm (v6.68.0).

    Stands down Nova's security response, so it needs a real requester and
    passes the policy confirmation boundary first (always confirmed; a phone
    tap when asked by voice). Without a user or device it fails closed; an
    unconfirmed or failed confirmation leaves the response running. Every
    attempt is audited."""
    from ... import action_log, policy
    action_id = await _audit_start(hass, "dismiss_intrusion", "dismiss_intrusion",
                                   device_id, user_id)
    if not (device_id or user_id):
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "blocked",
                                             reason_code="no_requester"))
        return json.dumps({
            "status": "blocked",
            "message": "I can't call off a security alert without knowing who is "
                       "asking. Please ask from the Nova app or a voice satellite.",
        })
    await hass.async_add_executor_job(lambda: action_log.mark_awaiting_approval(action_id))
    ok, note, approval_result = await policy.confirm_gate(
        hass, "nova", "dismiss_intrusion", "", "call off",
        device_id=device_id or "", target_name="the intrusion alert")
    await hass.async_add_executor_job(
        lambda: action_log.set_approval(
            action_id, approval_result,
            approval_required=(approval_result != "not_required")))
    if not ok:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "blocked",
                                             reason_code="confirmation_not_approved"))
        return json.dumps({
            "status": "awaiting_confirmation",
            "message": note or "Confirmation is required before I call off the alert.",
        })
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
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted"))
        return json.dumps({"ok": True, **res,
                           "message": "Intrusion called off. Standing down."})
    except Exception as exc:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed",
                                             reason_code="dismiss_failed"))
        return json.dumps({"error": str(exc)})
