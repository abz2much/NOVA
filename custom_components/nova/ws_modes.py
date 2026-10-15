"""Panel commands for modes, intrusion, hazards, energy, solar and wellbeing.

nova/mode, nova/intrusion, nova/hazard, nova/energy, nova/energy_flow,
nova/energy_outlook, nova/subscribe_presentation, nova/get_reminders,
nova/solar and nova/biometrics. websocket.py imports each handler by name
to register it. The logger keeps websocket.py's name.

Commands that only read (solar, energy flow and outlook, presentation,
reminders, hazard) are open to any panel user, like the other read only panel data; anything that
changes a setting or an alert needs an admin.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_log import nova_log

_LOGGER = logging.getLogger(f"{__package__}.websocket")


def _log_failure(handler: str, exc: Exception) -> str:
    """Log a failed command with its traceback; the text safe to send back."""
    _LOGGER.exception("%s failed: %s", handler, exc)
    return safe_error_message(exc)


# ── wellbeing ───────────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/biometrics",
    vol.Required("action"): vol.In(["status", "enable", "disable"]),
})
@websocket_api.async_response
async def ws_biometrics(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                        msg: dict) -> None:
    """Turn the wearable wellbeing context on or off, and list the wearable
    entities found. Context for replies only, never medical advice."""
    try:
        from . import biometrics, nova_config
        action = msg["action"]
        if action in ("enable", "disable"):
            on = action == "enable"
            nova_config.set("biometrics_enabled", on)
            nova_log("BIO", f"biometric context {action}d")
        found = await hass.async_add_executor_job(biometrics.discover, hass)
        entities = [{"kind": kind, **ent}
                    for kind, ents in found.items() for ent in ents]
        connection.send_result(msg["id"], {
            "enabled": bool(nova_config.get("biometrics_enabled", False)),
            "found": len(entities),
            "entities": entities,
        })
    except Exception as exc:
        connection.send_error(msg["id"], "biometrics_failed", _log_failure("ws_biometrics", exc))


# ── energy ──────────────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy",
    vol.Required("action"): vol.In(["status", "set_agency"]),
    vol.Optional("agency"): str,
})
@websocket_api.async_response
async def ws_energy(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                    msg: dict) -> None:
    """The power picture with its advice, after setting how far Nova may go
    with energy (advisory, opt_in or autonomous) when asked to."""
    try:
        from . import energy, nova_config
        if msg["action"] == "set_agency":
            level = str(msg.get("agency", "") or "").lower()
            known = (energy.AGENCY_ADVISORY, energy.AGENCY_OPT_IN,
                     energy.AGENCY_AUTONOMOUS)
            if level not in known:
                connection.send_error(msg["id"], "bad_agency",
                                      f"unknown agency '{level}'")
                return
            nova_config.set("energy_agency", level)
            nova_log("ENERGY", f"agency → {level}")
        status = await hass.async_add_executor_job(energy.power_status, hass)
        connection.send_result(msg["id"], status)
    except Exception as exc:
        connection.send_error(msg["id"], "energy_failed", _log_failure("ws_energy", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/solar",
    vol.Required("action"): vol.In(["status"]),
})
@websocket_api.async_response
async def ws_solar(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                   msg: dict) -> None:
    """Solar, battery and grid from Home Assistant's own Energy dashboard
    settings. Read only."""
    try:
        from . import solar
        connection.send_result(msg["id"], await solar.solar_status(hass))
    except Exception as exc:
        connection.send_error(msg["id"], "solar_failed", _log_failure("ws_solar", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy_flow",
    vol.Required("action"): vol.In(["status", "today"]),
})
@websocket_api.async_response
async def ws_energy_flow(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                         msg: dict) -> None:
    """The Energy tab's live power ("status") or today's totals ("today").
    Read only."""
    try:
        from . import energy_flow
        if msg["action"] == "today":
            result = await energy_flow.energy_flow_today(hass)
        else:
            result = await energy_flow.energy_flow_status(hass)
        connection.send_result(msg["id"], result)
    except Exception as exc:
        connection.send_error(msg["id"], "energy_flow_failed", _log_failure("ws_energy_flow", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/subscribe_presentation",
})
@websocket_api.async_response
async def ws_subscribe_presentation(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                                    msg: dict) -> None:
    """Push the card Nova shows beside the hologram while presenting. The
    latest unexpired card is sent on subscribe, new ones as they are made.
    Read only; the subscription ends through Home Assistant's own
    unsubscribe."""
    try:
        from .runtime import domain_runtime
        runtime = domain_runtime(hass)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "Nova is not loaded")
            return
        store = runtime.presentation

        def forward(card: dict) -> None:
            connection.send_message(websocket_api.event_message(msg["id"], card))

        connection.subscriptions[msg["id"]] = store.subscribe(forward)
        connection.send_result(msg["id"])
        card = store.current()
        if card is not None:
            forward(card)
    except Exception as exc:
        connection.send_error(msg["id"], "presentation_failed",
                              _log_failure("ws_subscribe_presentation", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_reminders",
})
@websocket_api.async_response
async def ws_get_reminders(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict) -> None:
    """The reminders still to come (due now or in the next 24 hours), for the
    Command Center's reminder card. Read only: label, time and whether due."""
    try:
        from . import reminders
        items = await hass.async_add_executor_job(reminders.pending_reminders)
        connection.send_result(msg["id"], {"items": items})
    except Exception as exc:
        connection.send_error(msg["id"], "reminders_failed", _log_failure("ws_get_reminders", exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy_outlook",
})
@websocket_api.async_response
async def ws_energy_outlook(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                            msg: dict) -> None:
    """The Outlook card: tariff, solar and usage for the next 36 hours with
    some advice. Read only."""
    try:
        from . import energy_outlook
        connection.send_result(msg["id"], await energy_outlook.energy_outlook_status(hass))
    except Exception as exc:
        connection.send_error(msg["id"], "energy_outlook_failed", _log_failure("ws_energy_outlook", exc))


# ── hazards and modes ───────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/hazard",
    vol.Required("action"): vol.In(["status", "scan"]),
})
@websocket_api.async_response
async def ws_hazard(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                    msg: dict) -> None:
    """The hazard monitor's settings and monitoring centre ("status"), or a
    live check of every feed ("scan") so the user can see it covers their
    area. A scan never alerts and never uses up deduplication."""
    try:
        from . import hazard_monitor
        if msg["action"] == "scan":
            result = await hazard_monitor.scan_now(hass)
        else:
            result = await hazard_monitor.status(hass)
        connection.send_result(msg["id"], result)
    except Exception as exc:
        connection.send_error(msg["id"], "hazard_failed", _log_failure("ws_hazard", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/mode",
    vol.Required("action"): vol.In(["status", "set"]),
    vol.Optional("mode"): str, vol.Optional("reason"): str,
})
@websocket_api.async_response
async def ws_mode(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                  msg: dict) -> None:
    """The active operational mode and the list of modes, after switching
    when asked. A mode changes how proactive and chatty Nova is, never
    safety."""
    try:
        from . import modes
        if msg["action"] != "set":
            connection.send_result(msg["id"], modes.mode_info())
            return
        result = await hass.async_add_executor_job(
            modes.set_mode, msg.get("mode", ""), msg.get("reason", ""))
        if result.get("ok"):
            nova_log("MODE", f"mode → {result['mode']} (panel)")
            user = connection.user
            try:
                from . import mode_scene
                await mode_scene.apply_mode_entry(
                    hass, result["mode"], source="panel",
                    requested_by_user_id=getattr(user, "id", None),
                    requested_by_name=getattr(user, "name", None))
            except Exception:
                pass
        connection.send_result(msg["id"], {**result, **modes.mode_info()})
    except Exception as exc:
        connection.send_error(msg["id"], "mode_failed", _log_failure("ws_mode", exc))


# ── intrusion ───────────────────────────────────────────────────────────────

async def _intrusion_status(hass, intrusion) -> dict:
    """The intrusion status with the last snapshot's image inlined. Images
    are never served over HTTP: they are read from a private folder and only
    reach the panel through this admin command."""
    status = intrusion.status()
    snap = status.get("last_snapshot")
    if snap and snap.get("path"):
        snap["image_b64"] = await intrusion.get_snapshot_b64(hass, snap["path"])
    return status


def _stand_down_investigation() -> None:
    """A called off alert also ends the safety manager's investigation."""
    try:
        from . import cognitive_core
        safety = getattr(getattr(cognitive_core, "_CORE", None), "safety_mgr", None)
        if safety:
            safety._investigation = None
    except Exception:
        pass


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/intrusion",
    vol.Required("action"): vol.In(
        ["status", "dismiss", "acknowledge", "log", "label", "learning"]),
    vol.Optional("reason"): str,
    vol.Optional("event_id"): str,
    vol.Optional("label"): vol.Any(str, None),
    vol.Optional("limit"): int,
})
@websocket_api.async_response
async def ws_intrusion(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                       msg: dict) -> None:
    """Intrusion state for the panel: the status with its snapshot, calling
    off an alert as a false alarm ("dismiss"), holding escalation without
    calling it off ("acknowledge"), the event log with snapshots, and
    labelling an event real or false, which is what learning learns from."""
    try:
        from . import intrusion
        action, msg_id = msg["action"], msg["id"]
        if action == "dismiss":
            done = intrusion.dismiss_intrusion(msg.get("reason", "panel"))
            _stand_down_investigation()
            nova_log("SAFETY", "Intrusion called off from panel (false alarm)")
            connection.send_result(msg_id, {**done, **await _intrusion_status(hass, intrusion)})
        elif action == "acknowledge":
            done = intrusion.acknowledge(msg.get("reason", "panel"))
            nova_log("SAFETY", "Intrusion acknowledged from panel (holding escalation)")
            connection.send_result(msg_id, {**done, **await _intrusion_status(hass, intrusion)})
        elif action == "log":
            events = intrusion.get_log(msg.get("limit", 50))
            for event in events:
                path = event.get("snapshot_path")
                if path:
                    event["image_b64"] = await intrusion.get_snapshot_b64(hass, path)
            connection.send_result(msg_id, {"events": events,
                                            "learning": intrusion.learning_summary()})
        elif action == "label":
            event_id, label = msg.get("event_id", ""), msg.get("label")
            done = intrusion.label_event(event_id, label)
            nova_log("SAFETY", f"Intrusion event labelled: "
                               f"{msg.get('event_id')} = {label}")
            connection.send_result(msg_id, {**done, "learning": intrusion.learning_summary()})
        elif action == "learning":
            connection.send_result(msg_id, intrusion.learning_summary())
        else:
            connection.send_result(msg_id, await _intrusion_status(hass, intrusion))
    except Exception as exc:
        connection.send_error(msg["id"], "intrusion_failed", _log_failure("ws_intrusion", exc))
