"""Camera vision and face recognition."""
from __future__ import annotations

import json
import logging

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
    fc = _FakeCall({"entity_id": entity_id, "prompt": prompt, "announce": announce})
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
