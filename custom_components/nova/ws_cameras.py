"""The camera commands.

Moved verbatim out of websocket.py: nova/camera_snapshot, nova/rename_camera,
nova/camera_location, nova/camera_diagnostics, nova/compute_camera_coverage
and nova/mmwave_overview, with _snap_log and its throttle state _SNAP_LOG_TS.
The logger keeps the name it had in websocket.py.

websocket.py imports every handler by name (async_register registers them).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .safe_errors import safe_error_message
from .ws_area_helpers import (
    _all_areas_with_anything,
    _area_name,
    _format_duration,
    _get_camera_names,
    _get_cameras,
    _is_outdoor_area,
)
from .ws_bridge import _get_entry
from .ws_log import nova_log

_LOGGER = logging.getLogger(f"{__package__}.websocket")


@websocket_api.websocket_command({
    vol.Required("type"): "nova/compute_camera_coverage",
    vol.Required("camera"): dict,
})
@websocket_api.async_response
async def ws_compute_camera_coverage(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Judge one camera's coverage (which rooms it can confirm + a human reason)
    from the geometric candidates the panel supplies. Uses the reasoning LLM,
    falling back to a geometry-only summary."""
    try:
        from . import camera_coverage, nova_config
        entry = _get_entry(hass)
        try:
            config = nova_config.effective_config(entry) if entry else {}
        except Exception:
            config = {}
        result = await camera_coverage.infer_coverage(hass, config, msg["camera"])
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("compute_camera_coverage failed: %s", exc)
        connection.send_error(msg["id"], "coverage_failed", safe_error_message(exc))


_SNAP_LOG_TS: dict[str, float] = {}



def _snap_log(entity_id: str, msg: str) -> None:
    """CAMERA-log a snapshot failure at most once per 5 min per entity —
    the panel polls this tier every 6s, and a broken camera shouldn't
    flood the log while still leaving a visible trail."""
    now = time.time()
    if now - _SNAP_LOG_TS.get(entity_id, 0) < 300:
        return
    _SNAP_LOG_TS[entity_id] = now
    nova_log("CAMERA", f"{entity_id} snapshot: {msg}")



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_snapshot",
    vol.Required("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """A frame via Nova's camera backend registry (Nest event media,
    Frigate snapshot, stream-wake). The panel's last-resort tile source for
    cameras where /api/camera_proxy* fails — WebRTC-only Nest cams have no
    MJPEG stream and can't produce stills while idle, so both proxy tiers
    404 and the tile went permanently blank (v6.46.0)."""
    import base64
    entity_id = str(msg["entity_id"])
    try:
        if not hass.states.get(entity_id) or not entity_id.startswith("camera."):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import camera as cam
        img = await cam._get_best_image(hass, entity_id)
        if not img:
            _snap_log(entity_id,
                      "no frame — backend and proxy paths all empty "
                      "(Nest: check integration is loaded and events enabled)")
            connection.send_result(msg["id"], {"image": None})
            return
        img = cam._downscale_jpeg(img, 960)
        connection.send_result(
            msg["id"], {"image": base64.b64encode(img).decode()})
    except Exception as exc:
        _LOGGER.debug("camera_snapshot failed for %s: %s", entity_id, exc)
        _snap_log(entity_id, f"error — {exc}")
        connection.send_error(msg["id"], "snapshot_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/rename_camera",
    vol.Required("entity_id"): str,
    vol.Required("name"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_rename_camera(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set a Nova-only display name for a camera (v6.48.0) — chips, strip,
    and pickers use it; HA's entity name is untouched. Blank name reverts."""
    entity_id = str(msg["entity_id"])
    new_name = msg.get("name")
    try:
        if not entity_id.startswith("camera.") or not hass.states.get(entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config
        from .camera import merge_camera_name
        names = merge_camera_name(_get_camera_names(), entity_id, new_name)
        await hass.async_add_executor_job(nova_config.set, "camera_names", names)
        shown = names.get(entity_id)
        nova_log("CONFIG", f"camera {entity_id} "
                             + (f"renamed to '{shown}'" if shown else "name reverted")
                             + " (Nova only)")
        connection.send_result(msg["id"], {
            "ok": True, "camera_names": names, "cameras": _get_cameras(hass),
        })
    except Exception as exc:
        _LOGGER.exception("rename_camera failed: %s", exc)
        connection.send_error(msg["id"], "rename_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/mmwave_overview",
})
@websocket_api.async_response
async def ws_mmwave_overview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Per-area mmWave presence overview for the residence tab (v6.53.0).

    Distinct from the generic area grid: this reports *only* rooms with
    presence/occupancy/motion sensors, and for each the live sensor breakdown —
    how many sensors, how many currently detecting, the freshest detection age —
    so the panel can show genuine mmWave coverage and live state rather than a
    binary 'occupied' flag that could come from a door contact."""
    import time as _t
    try:
        from . import audio_routing
        rooms = []
        total_sensors = 0
        rooms_detecting = 0
        for aid in _all_areas_with_anything(hass):
            sensors = audio_routing.presence_entities_in_area(hass, aid)
            if not sensors:
                continue
            detecting = 0
            freshest = None            # seconds since most-recent change
            sensor_rows = []
            for eid in sensors:
                st = hass.states.get(eid)
                if st is None:
                    continue
                on = st.state == "on"
                if on:
                    detecting += 1
                age = None
                try:
                    age = _t.time() - st.last_changed.timestamp()
                    if freshest is None or age < freshest:
                        freshest = age
                except Exception:
                    pass
                sensor_rows.append({
                    "entity_id": eid,
                    "name": (st.attributes.get("friendly_name") or eid),
                    "detecting": on,
                    "age": _format_duration(age),
                })
            total_sensors += len(sensor_rows)
            if detecting:
                rooms_detecting += 1
            rooms.append({
                "area_id": aid,
                "name": _area_name(hass, aid),
                "outdoor": _is_outdoor_area(hass, aid),
                "sensor_count": len(sensor_rows),
                "detecting_count": detecting,
                "state": ("detecting" if detecting else "clear"),
                "freshest": _format_duration(freshest),
                "sensors": sensor_rows,
            })
        # Detecting rooms first, then most-recently-active, then name
        rooms.sort(key=lambda r: (r["detecting_count"] == 0, r["name"].lower()))
        connection.send_result(msg["id"], {
            "rooms": rooms,
            "summary": {
                "rooms_with_mmwave": len(rooms),
                "rooms_detecting": rooms_detecting,
                "total_sensors": total_sensors,
            },
        })
    except Exception as exc:
        _LOGGER.exception("mmwave_overview failed: %s", exc)
        connection.send_error(msg["id"], "mmwave_overview_failed", safe_error_message(exc))



@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_location",
    vol.Required("entity_id"): str,
    vol.Required("mode"): vol.In(["auto", "indoor", "outdoor"]),
})
@websocket_api.async_response
async def ws_camera_location(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Designate a camera indoor/outdoor (or auto = heuristics), v6.49.0.
    Pins the exact entity id into the existing indoor_entities /
    outdoor_entities lists — outdoor.py's most-authoritative layer — so the
    designation immediately governs the intrusion investigator, the
    notable-outdoor-event filter, and the motion scan alike."""
    entity_id = str(msg["entity_id"])
    mode = str(msg["mode"])
    try:
        if not entity_id.startswith("camera.") or not hass.states.get(entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config, outdoor
        new_in, new_out = outdoor.set_entity_location(
            outdoor._cfg_list("indoor_entities"),
            outdoor._cfg_list("outdoor_entities"),
            entity_id, mode,
        )
        await hass.async_add_executor_job(
            nova_config.set_many,
            {"indoor_entities": new_in, "outdoor_entities": new_out},
        )
        nova_log("CONFIG", f"camera {entity_id} location → {mode.upper()}"
                             + ("" if mode != "auto" else " (heuristics)"))
        connection.send_result(msg["id"], {
            "ok": True, "cameras": _get_cameras(hass),
        })
    except Exception as exc:
        _LOGGER.exception("camera_location failed: %s", exc)
        connection.send_error(msg["id"], "camera_location_failed", safe_error_message(exc))



@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_diagnostics",
    vol.Optional("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_diagnostics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """End-to-end probe of one camera's frame sources, plus a platform
    summary of every camera entity HA has — answers both "why is this tile
    blank" and "do my Nest entities even exist" in one call (v6.46.2)."""
    import asyncio as _aio
    try:
        summary = []
        platforms: dict[str, int] = {}
        try:
            from homeassistant.helpers import entity_registry as er
            reg = er.async_get(hass)
        except Exception:
            reg = None
        for st in hass.states.async_all("camera"):
            plat = None
            if reg:
                try:
                    e = reg.async_get(st.entity_id)
                    plat = e.platform if e else None
                except Exception:
                    plat = None
            platforms[plat or "?"] = platforms.get(plat or "?", 0) + 1
            summary.append({"entity_id": st.entity_id,
                            "state": st.state, "platform": plat})

        probe = None
        entity_id = msg.get("entity_id")
        if entity_id:
            from . import camera as cam
            try:
                probe = await _aio.wait_for(
                    cam.probe_camera(hass, str(entity_id)), timeout=30)
            except _aio.TimeoutError:
                probe = {"entity_id": entity_id, "tiers": [],
                         "verdict": "probe timed out after 30s "
                                    "(stream wake hanging?)"}
            nova_log("CAMERA", f"diag {entity_id}: {probe.get('verdict', '?')}")

        connection.send_result(msg["id"], {
            "summary": summary, "platforms": platforms, "probe": probe,
        })
    except Exception as exc:
        _LOGGER.exception("camera_diagnostics failed: %s", exc)
        connection.send_error(msg["id"], "camera_diag_failed", safe_error_message(exc))
