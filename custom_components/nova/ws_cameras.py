"""Websocket commands for the panel's cameras and mmWave rooms.

Commands: nova/compute_camera_coverage, nova/camera_snapshot,
nova/rename_camera, nova/camera_location, nova/camera_diagnostics and
nova/mmwave_overview. websocket.py imports each handler by name and
registers it. Log lines go out under the websocket module's logger name so
existing log filters still match.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
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
    area_scan,
)
from .ws_bridge import _get_entry
from .ws_log import nova_log

_LOGGER = logging.getLogger(f"{__package__}.websocket")

_Conn = websocket_api.ActiveConnection

# The panel asks for a snapshot every few seconds; a broken camera logs at
# most once in this window.
_SNAP_LOG_QUIET_S = 300
_SNAP_LOG_TS: dict[str, float] = {}

_PROBE_TIMEOUT_S = 30


def _snap_log(entity_id: str, msg: str) -> None:
    """Put a snapshot problem in Nova's debug log, throttled per camera."""
    now = time.time()
    last = _SNAP_LOG_TS.get(entity_id, 0)
    if now - last >= _SNAP_LOG_QUIET_S:
        _SNAP_LOG_TS[entity_id] = now
        nova_log("CAMERA", f"{entity_id} snapshot: {msg}")


def _is_live_camera(hass: HomeAssistant, entity_id: str) -> bool:
    return entity_id.startswith("camera.") and hass.states.get(entity_id) is not None


def _reason(what: str, exc: Exception) -> str:
    """Log the failure in full; the panel gets the error type only."""
    _LOGGER.exception("%s failed: %s", what, exc)
    return safe_error_message(exc)


# ── coverage ────────────────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/compute_camera_coverage",
    vol.Required("camera"): dict,
})
@websocket_api.async_response
async def ws_compute_camera_coverage(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """Which rooms one camera can confirm, and why, worked out from the
    geometric candidates the panel sends (AI review with a geometry only
    fallback)."""
    try:
        from . import camera_coverage, nova_config
        config: dict = {}
        entry = _get_entry(hass)
        if entry:
            try:
                config = nova_config.effective_config(entry)
            except Exception:
                config = {}
        connection.send_result(
            msg["id"], await camera_coverage.infer_coverage(hass, config, msg["camera"]))
    except Exception as exc:
        connection.send_error(msg["id"], "coverage_failed", _reason("compute_camera_coverage", exc))


# ── snapshot ────────────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_snapshot",
    vol.Required("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_snapshot(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """One frame from Nova's camera backends, as base64 JPEG.

    The panel falls back to this when Home Assistant's camera proxy has
    nothing, for example a WebRTC only camera that gives no stills while
    idle."""
    entity_id = str(msg["entity_id"])
    try:
        if not _is_live_camera(hass, entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import camera as cam
        frame = await cam._get_best_image(hass, entity_id)
        if frame:
            small = cam._downscale_jpeg(frame, 960)
            connection.send_result(msg["id"], {"image": base64.b64encode(small).decode()})
            return
        _snap_log(entity_id, "no frame — backend and proxy paths all empty "
                             "(Nest: check integration is loaded and events enabled)")
        connection.send_result(msg["id"], {"image": None})
    except Exception as exc:
        # Full detail only in the Home Assistant log; the panel's debug log
        # shows the error type.
        reason = safe_error_message(exc)
        _LOGGER.debug("camera_snapshot failed for %s: %s", entity_id, exc)
        _snap_log(entity_id, f"error — {reason}")
        connection.send_error(msg["id"], "snapshot_failed", reason)


# ── name and location (Nova's own settings) ─────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/rename_camera",
    vol.Required("entity_id"): str,
    vol.Required("name"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_rename_camera(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """Give a camera a name used only inside Nova; a blank name goes back
    to Home Assistant's. The entity itself is never renamed."""
    entity_id = str(msg["entity_id"])
    try:
        if not _is_live_camera(hass, entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config
        from .camera import merge_camera_name
        names = merge_camera_name(_get_camera_names(), entity_id, msg.get("name"))
        await hass.async_add_executor_job(nova_config.set, "camera_names", names)
        shown = names.get(entity_id)
        change = f"renamed to '{shown}'" if shown else "name reverted"
        nova_log("CONFIG", f"camera {entity_id} {change} (Nova only)")
        connection.send_result(msg["id"], {
            "ok": True,
            "camera_names": names,
            "cameras": _get_cameras(hass),
        })
    except Exception as exc:
        connection.send_error(msg["id"], "rename_failed", _reason("rename_camera", exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_location",
    vol.Required("entity_id"): str,
    vol.Required("mode"): vol.In(["auto", "indoor", "outdoor"]),
})
@websocket_api.async_response
async def ws_camera_location(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """Mark a camera indoor or outdoor, or hand it back to the heuristics.

    The entity goes into the indoor_entities or outdoor_entities list, which
    outdoor.py trusts over everything else, so the intrusion check, the
    outdoor event filter and the motion scan all follow it at once."""
    entity_id = str(msg["entity_id"])
    mode = str(msg["mode"])
    try:
        if not _is_live_camera(hass, entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config, outdoor
        indoor, outdoor_list = outdoor.set_entity_location(
            outdoor._cfg_list("indoor_entities"),
            outdoor._cfg_list("outdoor_entities"),
            entity_id,
            mode,
        )
        await hass.async_add_executor_job(
            nova_config.set_many,
            {"indoor_entities": indoor, "outdoor_entities": outdoor_list},
        )
        note = " (heuristics)" if mode == "auto" else ""
        nova_log("CONFIG", f"camera {entity_id} location → {mode.upper()}{note}")
        connection.send_result(msg["id"], {"ok": True, "cameras": _get_cameras(hass)})
    except Exception as exc:
        connection.send_error(msg["id"], "camera_location_failed", _reason("camera_location", exc))


# ── diagnostics ─────────────────────────────────────────────────────────────

def _camera_platforms(hass: HomeAssistant) -> tuple[list[dict], dict[str, int]]:
    """Every camera entity with the integration behind it, plus a count per
    integration ("?" when the registry can't say)."""
    try:
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
    except Exception:
        registry = None

    def platform_of(entity_id: str):
        if registry is None:
            return None
        try:
            found = registry.async_get(entity_id)
        except Exception:
            return None
        return found.platform if found else None

    rows: list[dict] = []
    counts: dict[str, int] = {}
    for state in hass.states.async_all("camera"):
        platform = platform_of(state.entity_id)
        key = platform or "?"
        counts[key] = counts.get(key, 0) + 1
        rows.append({"entity_id": state.entity_id, "state": state.state, "platform": platform})
    return rows, counts


@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_diagnostics",
    vol.Optional("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_diagnostics(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """A list of every camera Home Assistant has, and when an entity is
    given, a test of each way Nova can get a frame from it."""
    try:
        summary, platforms = _camera_platforms(hass)
        probe = None
        target = msg.get("entity_id")
        if target:
            from . import camera as cam
            try:
                probe = await asyncio.wait_for(
                    cam.probe_camera(hass, str(target)), timeout=_PROBE_TIMEOUT_S)
            except asyncio.TimeoutError:
                probe = {
                    "entity_id": target,
                    "tiers": [],
                    "verdict": "probe timed out after 30s (stream wake hanging?)",
                }
            nova_log("CAMERA", f"diag {target}: {probe.get('verdict', '?')}")
        connection.send_result(msg["id"], {
            "summary": summary,
            "platforms": platforms,
            "probe": probe,
        })
    except Exception as exc:
        connection.send_error(msg["id"], "camera_diag_failed", _reason("camera_diagnostics", exc))


# ── mmWave rooms ────────────────────────────────────────────────────────────

def _room_sensors(hass: HomeAssistant, entity_ids, now: float):
    """Live rows for a room's presence sensors, how many are on, and the age
    in seconds of the newest change (None when no sensor has one)."""
    rows: list[dict] = []
    on_count = 0
    newest = None
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        detecting = state.state == "on"
        on_count += detecting
        age = None
        try:
            age = now - state.last_changed.timestamp()
        except Exception:
            pass
        if age is not None and (newest is None or age < newest):
            newest = age
        rows.append({
            "entity_id": entity_id,
            "name": state.attributes.get("friendly_name") or entity_id,
            "detecting": detecting,
            "age": _format_duration(age),
        })
    return rows, on_count, newest


@websocket_api.websocket_command({
    vol.Required("type"): "nova/mmwave_overview",
})
@websocket_api.async_response
async def ws_mmwave_overview(hass: HomeAssistant, connection: _Conn, msg: dict) -> None:
    """Rooms with presence, occupancy or motion sensors, for the residence
    tab, each with its sensors' live state. A room with only a door contact
    is left out, so the tab shows real presence coverage.

    Order: rooms detecting now first, then the most recently active, then
    by name."""
    try:
        from . import audio_routing
        with area_scan(hass):
            area_ids = _all_areas_with_anything(hass)
        now = time.time()
        rooms: list[tuple[tuple, dict]] = []
        for area_id in area_ids:
            entity_ids = audio_routing.presence_entities_in_area(hass, area_id)
            if not entity_ids:
                continue
            rows, on_count, newest = _room_sensors(hass, entity_ids, now)
            name = _area_name(hass, area_id)
            room = {
                "area_id": area_id,
                "name": name,
                "outdoor": _is_outdoor_area(hass, area_id),
                "sensor_count": len(rows),
                "detecting_count": on_count,
                "state": "detecting" if on_count else "clear",
                "freshest": _format_duration(newest),
                "sensors": rows,
            }
            recency = newest if newest is not None else float("inf")
            rooms.append(((on_count == 0, recency, name.lower()), room))
        rooms.sort(key=lambda pair: pair[0])
        listed = [room for _, room in rooms]
        connection.send_result(msg["id"], {
            "rooms": listed,
            "summary": {
                "rooms_with_mmwave": len(listed),
                "rooms_detecting": sum(1 for r in listed if r["detecting_count"]),
                "total_sensors": sum(r["sensor_count"] for r in listed),
            },
        })
    except Exception as exc:
        connection.send_error(msg["id"], "mmwave_overview_failed", _reason("mmwave_overview", exc))
