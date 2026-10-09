"""The mode, intrusion, hazard, energy, solar and biometrics commands.

Moved verbatim out of websocket.py: nova/intrusion, nova/mode, nova/hazard,
nova/energy, nova/solar and nova/biometrics. The logger keeps the name it had
in websocket.py. nova/energy_flow (the Energy tab's live readout) was added
here later, next to nova/solar, and nova/energy_outlook (its Outlook card)
next to that.

websocket.py imports every handler by name (async_register registers them).
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_log import nova_log

_LOGGER = logging.getLogger(f"{__package__}.websocket")


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/biometrics",
    vol.Required("action"): vol.In(["status", "enable", "disable"]),
})
@websocket_api.async_response
async def ws_biometrics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Wellbeing/biometric context for the panel (v6.63.0) — discover connected
    wearable entities and toggle the feature. Context only; never medical."""
    try:
        from . import biometrics, nova_config
        if msg["action"] == "enable":
            nova_config.set("biometrics_enabled", True)
            nova_log("BIO", "biometric context enabled")
        elif msg["action"] == "disable":
            nova_config.set("biometrics_enabled", False)
            nova_log("BIO", "biometric context disabled")
        enabled = bool(nova_config.get("biometrics_enabled", False))
        found = await hass.async_add_executor_job(biometrics.discover, hass)
        # flatten discovered entities for the panel
        entities = []
        for kind, ents in found.items():
            for e in ents:
                entities.append({"kind": kind, **e})
        connection.send_result(msg["id"], {
            "enabled": enabled,
            "found": len(entities),
            "entities": entities,
        })
    except Exception as exc:
        _LOGGER.exception("ws_biometrics failed: %s", exc)
        connection.send_error(msg["id"], "biometrics_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy",
    vol.Required("action"): vol.In(["status", "set_agency"]),
    vol.Optional("agency"): str,
})
@websocket_api.async_response
async def ws_energy(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Energy management for the panel (v6.62.0): report the current power
    picture + advice, or set the agency level (advisory/opt_in/autonomous)."""
    try:
        from . import energy, nova_config
        if msg["action"] == "set_agency":
            level = str(msg.get("agency", "") or "").lower()
            if level not in (energy.AGENCY_ADVISORY, energy.AGENCY_OPT_IN,
                             energy.AGENCY_AUTONOMOUS):
                connection.send_error(msg["id"], "bad_agency",
                                      f"unknown agency '{level}'")
                return
            nova_config.set("energy_agency", level)
            nova_log("ENERGY", f"agency → {level}")
            res = await hass.async_add_executor_job(energy.power_status, hass)
            connection.send_result(msg["id"], res)
        else:
            res = await hass.async_add_executor_job(energy.power_status, hass)
            connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_energy failed: %s", exc)
        connection.send_error(msg["id"], "energy_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/solar",
    vol.Required("action"): vol.In(["status"]),
})
@websocket_api.async_response
async def ws_solar(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Solar/battery/grid picture for the panel (v7.91.0), read straight from
    Home Assistant's own Energy dashboard config. Pure read, no mutating
    action — left open like other read-only panel data (get_panel_data,
    get_activity_log, etc.), not admin-gated."""
    try:
        from . import solar
        res = await solar.solar_status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_solar failed: %s", exc)
        connection.send_error(msg["id"], "solar_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy_flow",
    vol.Required("action"): vol.In(["status", "today"]),
})
@websocket_api.async_response
async def ws_energy_flow(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Live solar, house, battery and grid power ("status") and today's
    totals ("today") for the Energy tab, from Home Assistant's own Energy
    dashboard config. Read only, so not admin gated, the same as nova/solar."""
    try:
        from . import energy_flow
        if msg["action"] == "today":
            res = await energy_flow.energy_flow_today(hass)
        else:
            res = await energy_flow.energy_flow_status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_energy_flow failed: %s", exc)
        connection.send_error(msg["id"], "energy_flow_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy_outlook",
})
@websocket_api.async_response
async def ws_energy_outlook(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """The next 36 hours of tariff, solar and usage, with a few pieces of
    advice, for the Energy tab's Outlook card. Advice only. Read only, so
    not admin gated, the same as nova/energy_flow."""
    try:
        from . import energy_outlook
        res = await energy_outlook.energy_outlook_status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_energy_outlook failed: %s", exc)
        connection.send_error(msg["id"], "energy_outlook_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/hazard",
    vol.Required("action"): vol.In(["status", "scan"]),
})
@websocket_api.async_response
async def ws_hazard(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Multi-hazard monitor for the panel (v6.71.0): 'status' returns config +
    the resolved monitoring center; 'scan' runs a live read-only check of all
    feeds so the user can confirm it's wired to their area (does not alert or
    consume dedup)."""
    try:
        from . import hazard_monitor
        if msg["action"] == "scan":
            res = await hazard_monitor.scan_now(hass)
        else:
            res = await hazard_monitor.status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_hazard failed: %s", exc)
        connection.send_error(msg["id"], "hazard_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/mode",
    vol.Required("action"): vol.In(["status", "set"]),
    vol.Optional("mode"): str,
    vol.Optional("reason"): str,
})
@websocket_api.async_response
async def ws_mode(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Operational mode control for the panel (Directive Layer, v6.61.0):
    report the active mode + available modes, or switch modes. Modes shift the
    whole behavior profile (proactivity, tone, event scope) but never disable
    safety."""
    try:
        from . import modes
        if msg["action"] == "set":
            res = await hass.async_add_executor_job(
                modes.set_mode, msg.get("mode", ""), msg.get("reason", ""))
            if res.get("ok"):
                nova_log("MODE", f"mode → {res['mode']} (panel)")
                try:
                    from . import mode_scene
                    await mode_scene.apply_mode_entry(
                        hass, res["mode"], source="panel",
                        requested_by_user_id=getattr(connection.user, "id", None),
                        requested_by_name=getattr(connection.user, "name", None),
                    )
                except Exception:
                    pass
            connection.send_result(msg["id"], {**res, **modes.mode_info()})
        else:
            connection.send_result(msg["id"], modes.mode_info())
    except Exception as exc:
        _LOGGER.exception("ws_mode failed: %s", exc)
        connection.send_error(msg["id"], "mode_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/intrusion",
    vol.Required("action"): vol.In(["status", "dismiss", "acknowledge",
                                    "log", "label", "learning"]),
    vol.Optional("reason"): str,
    vol.Optional("event_id"): str,
    vol.Optional("label"): vol.Any(str, None),
    vol.Optional("limit"): int,
})
@websocket_api.async_response
async def ws_intrusion(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Intrusion snapshot + call-off for the panel (v6.68.0): report the last
    snapshot and call-off state, dismiss an active alert as a false alarm, or
    acknowledge it (hold auto-escalation without cancelling) (v6.69.0).

    Snapshot images are never served over HTTP (v7.102.0) — they live in a
    private directory and are read back and base64-inlined here, so they can
    only ever reach the panel through this already-@require_admin command.
    """
    try:
        from . import intrusion

        async def _status_with_image() -> dict:
            s = intrusion.status()
            snap = s.get("last_snapshot")
            if snap and snap.get("path"):
                snap["image_b64"] = await intrusion.get_snapshot_b64(hass, snap["path"])
            return s

        if msg["action"] == "dismiss":
            res = intrusion.dismiss_intrusion(msg.get("reason", "panel"))
            try:
                from . import cognitive_core
                core = getattr(cognitive_core, "_CORE", None)
                if core and getattr(core, "safety_mgr", None):
                    core.safety_mgr._investigation = None
            except Exception:
                pass
            nova_log("SAFETY", "Intrusion called off from panel (false alarm)")
            connection.send_result(msg["id"], {**res, **await _status_with_image()})
        elif msg["action"] == "acknowledge":
            res = intrusion.acknowledge(msg.get("reason", "panel"))
            nova_log("SAFETY", "Intrusion acknowledged from panel (holding escalation)")
            connection.send_result(msg["id"], {**res, **await _status_with_image()})
        elif msg["action"] == "log":
            # Reviewable event history with snapshots (v6.76.0)
            events = intrusion.get_log(msg.get("limit", 50))
            for ev in events:
                p = ev.get("snapshot_path")
                if p:
                    ev["image_b64"] = await intrusion.get_snapshot_b64(hass, p)
            connection.send_result(msg["id"], {
                "events": events,
                "learning": intrusion.learning_summary(),
            })
        elif msg["action"] == "label":
            # The training signal: mark an event real or false
            res = intrusion.label_event(msg.get("event_id", ""),
                                        msg.get("label"))
            nova_log("SAFETY", f"Intrusion event labelled: "
                                 f"{msg.get('event_id')} = {msg.get('label')}")
            connection.send_result(msg["id"], {
                **res, "learning": intrusion.learning_summary()})
        elif msg["action"] == "learning":
            connection.send_result(msg["id"], intrusion.learning_summary())
        else:
            connection.send_result(msg["id"], await _status_with_image())
    except Exception as exc:
        _LOGGER.exception("ws_intrusion failed: %s", exc)
        connection.send_error(msg["id"], "intrusion_failed", safe_error_message(exc))
