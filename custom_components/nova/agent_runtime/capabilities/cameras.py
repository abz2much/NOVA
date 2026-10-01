"""Camera vision and face recognition."""
from __future__ import annotations

import json
import logging
import time

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


async def _exec_who_do_you_see(hass: HomeAssistant, args: dict) -> str:
    """Report who Nova currently recognizes by face (v6.66.0)."""
    try:
        from ... import recognition
        res = await hass.async_add_executor_job(recognition.who_do_you_see, hass)
        return json.dumps(res)
    except Exception as exc:
        return json.dumps({"error": str(exc), "seen": [], "any": False})


async def _analyze_camera(hass, entity_id: str, prompt: str, announce: bool,
                          honorific: str) -> dict:
    """The camera vision call, isolated as a module-level function so tests can
    patch it deterministically — replacing a name in this module's own namespace,
    rather than depending on how `from ...camera import …` resolves. That import
    resolution behaves differently across CPython builds and was the source of a
    persistent CI-only failure; calling through this indirection sidesteps it."""
    from ...camera import async_analyze_camera, _FakeCall
    # A question and its answer is not a description of the scene, so it is
    # never kept in scene memory.
    fc = _FakeCall({"entity_id": entity_id, "prompt": prompt, "announce": announce,
                    "record_scene": False})
    # groq_client=None → analyze path resolves the configured vision client
    # itself; degrades gracefully with a clear error if none is set up.
    return await async_analyze_camera(
        hass, fc, None, honorific, None, [], gate_announce=False)


def _shape_look_at_camera_result(result: dict, entity_id: str) -> str:
    """Shape the vision result into the tool's JSON response. Pure (no I/O,
    no imports) so the success/failure contract is testable directly, without
    the camera/vision stack or any monkeypatching."""
    if not result.get("success"):
        return json.dumps({
            "success": False,
            "camera": result.get("camera", entity_id),
            "error": result.get("error", "vision analysis failed"),
            "hint": ("this camera may be WebRTC-only/offline, or no vision "
                     "provider is configured (set vision_provider + key)"),
        })
    return json.dumps({
        "success": True,
        "camera": result.get("camera"),
        "answer": result.get("analysis"),
        "source": result.get("source"),
    })


async def _exec_look_at_camera(hass: HomeAssistant, args: dict) -> str:
    """Vision query: snapshot a camera and answer a question about it (v6.58.0).
    Powers on-demand visual checks and standing vision monitors. Reuses the
    camera pipeline's analyze path with the user's question as the prompt."""
    entity_id = str(args.get("entity_id", "")).strip()
    question = str(args.get("question", "")).strip()
    if not entity_id or not question:
        return json.dumps({"error": "entity_id and question are required"})
    try:
        from ... import honorific as honorific_mod
        honorific = honorific_mod.effective_honorific(hass)  # Phase C: presence-aware
        prompt = (
            f"Answer this question about what you see, concisely and factually: "
            f"{question} If the thing asked about is present, say so and briefly "
            f"describe it; if not, say it is not present. Do not speculate beyond "
            f"the image."
        )
        result = await _analyze_camera(
            hass, entity_id, prompt, bool(args.get("announce", False)), honorific)
        if result.get("success"):
            try:
                from ...websocket import nova_log
                nova_log("CAMERA", f"visual query on {result.get('camera')}: "
                                     f"{question[:60]}")
            except Exception:
                pass
        return _shape_look_at_camera_result(result, entity_id)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


_SCENE_OFF = ("Scene memory is off. Turn on Scene memory in Settings if you "
              "want Nova to remember what its cameras described.")


def _ago(ts) -> str:
    try:
        secs = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return ""
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} minutes ago"
    if secs < 129600:
        return f"{secs // 3600} hours ago"
    return f"{secs // 86400} days ago"


def _camera_label(hass: HomeAssistant, camera: str, area) -> str:
    try:
        from ...camera import _camera_friendly_name
        name = _camera_friendly_name(hass, camera)
    except Exception:
        name = camera
    return f"{name} ({area})" if area and str(area) != str(name) else str(name)


async def _exec_where_last_seen(hass: HomeAssistant, args: dict) -> str:
    """Search scene memory for the last time something was described."""
    term = str(args.get("term", "") or "").strip()
    if not term:
        return json.dumps({"error": "term is required", "found": False})
    try:
        from ... import scene_memory
        if not scene_memory.enabled():
            return json.dumps({"found": False, "enabled": False, "hint": _SCENE_OFF})
        hit = await hass.async_add_executor_job(scene_memory.where_last_seen, term)
        if not hit:
            return json.dumps({"found": False, "enabled": True, "term": term})
        return json.dumps({
            "found": True, "enabled": True, "term": term,
            "camera": _camera_label(hass, hit["camera"], hit.get("area")),
            "when": _ago(hit["ts"]), "timestamp": hit["ts"],
            "description": hit["description"][:300],
            "note": "From a past camera description, not a live view.",
        })
    except Exception as exc:
        return json.dumps({"error": str(exc), "found": False})


async def _exec_what_changed(hass: HomeAssistant, args: dict) -> str:
    """Compare a camera's latest description with an earlier one."""
    camera = str(args.get("camera", "") or "").strip()
    if not camera:
        return json.dumps({"error": "camera is required", "found": False})
    try:
        hours = float(args.get("hours", 24) or 24)
    except (TypeError, ValueError):
        hours = 24.0
    hours = min(max(hours, 1.0), 24.0 * 90)
    try:
        from ... import scene_memory
        if not scene_memory.enabled():
            return json.dumps({"found": False, "enabled": False, "hint": _SCENE_OFF})
        res = await hass.async_add_executor_job(
            scene_memory.what_changed, camera, time.time() - hours * 3600.0)
        if not res.get("found"):
            return json.dumps({"found": False, "enabled": True, "camera": camera})
        out = {
            "found": True, "enabled": True,
            "camera": _camera_label(hass, res["camera"], res.get("area")),
            "latest": _ago(res["latest_ts"]),
        }
        if res.get("baseline_ts") is None:
            out["comparison"] = "nothing older than that to compare with"
        else:
            out.update(compared_with=_ago(res["baseline_ts"]),
                       added=res["added"][:30], removed=res["removed"][:30],
                       note="Word level comparison of past camera descriptions.")
        return json.dumps(out)
    except Exception as exc:
        return json.dumps({"error": str(exc), "found": False})
