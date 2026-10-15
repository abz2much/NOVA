"""Picking a scene from what someone says they want.

"Something relaxing", "bright and energetic", "going to bed": the AI is
given the home's real scenes and picks the one that fits, which is then run
through the usual authority check.
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.core import HomeAssistant, ServiceCall

from .tts_helper import async_announce

SCENE_MODEL = "openai/gpt-oss-120b"
_LOGGER = logging.getLogger(__name__)

_PICK_RULES = (
    "You are a smart home assistant that picks the best Home Assistant scene for "
    "a user's stated intent. You have a list of available scenes, the current "
    "time, and the user's intent. Return ONLY the scene's entity_id (the full "
    "'scene.xxx' format) and nothing else — no explanation, no punctuation, no "
    "quotes. If no scene fits, return the word 'none'."
)


def _list_scenes(hass: HomeAssistant) -> list[dict]:
    """Every scene in Home Assistant as {entity_id, name}."""
    return [{"entity_id": st.entity_id,
             "name": st.attributes.get("friendly_name", st.entity_id)}
            for st in hass.states.async_all("scene")]


def _pick_request(scenes: list[dict], intent: str) -> str:
    listed = "\n".join(f"- {scene['entity_id']}: {scene['name']}" for scene in scenes)
    when = datetime.now().strftime("%A, %-I:%M %p")
    return (f"Current time: {when}\n\nAvailable scenes:\n{listed}\n\n"
            f"User intent: {intent}\n\nBest matching scene (entity_id only):")


def _known(pick: str, valid: set) -> str:
    """The model's answer as a scene id, recovering one wrapped in quotes or
    followed by more words; "" when it names nothing known."""
    if pick != "none" and pick in valid:
        return pick
    cleaned = pick.strip('\'".,` ').split()[0] if pick else ""
    return cleaned if cleaned in valid else ""


async def async_activate_by_intent(hass: HomeAssistant, call: ServiceCall, groq_client,
                                   honorific: str, tts_entity: str | None,
                                   speakers: list[str]) -> dict:
    """The nova.scene_by_intent service: choose the scene that fits the
    words and run it."""
    intent: str = call.data["intent"]
    announce = call.data.get("announce", True)
    # No honorific (nobody in particular home) means no trailing ", sir".
    addr = f", {honorific}" if honorific else ""

    async def say(text: str, **kw) -> None:
        if announce:
            await async_announce(hass, text, tts_entity, speakers, **kw)

    scenes = _list_scenes(hass)
    if not scenes:
        await say(f"No scenes are defined{addr}. I have nothing to choose from.")
        return {"success": False, "error": "no_scenes"}

    try:
        from .providers.activity import execute_chat
        result = await execute_chat(
            hass, groq_client,
            [{"role": "system", "content": _PICK_RULES},
             {"role": "user", "content": _pick_request(scenes, intent)}],
            role="scenes", data_category="text", max_tokens=40, temperature=0.2)
        answer = result.text.strip().lower()
    except Exception as exc:
        _LOGGER.error("Nova scene-pick error: %s", exc)
        return {"success": False, "error": str(exc)}

    pick = _known(answer, {scene["entity_id"] for scene in scenes})
    if not pick:
        await say(f"Nothing quite matches that intent{addr}.")
        return {"success": False, "error": "no_match", "pick": answer}

    # Activate
    from . import action_log
    request_id = action_log.new_request_id()
    requested_by_user_id = getattr(getattr(call, "context", None), "user_id", None)
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "scene_by_intent", "ha_service",
            domain="scene", service="turn_on", entity_id=pick,
            requested_by_user_id=requested_by_user_id,
        )
    )
    # The one authority check (8.24.0): a scene's contents are opaque, so it
    # is confirmed when voice confirmation is on, like any other scene.
    from . import policy
    decision = await policy.authorize(hass, policy.AuthorityRequest(
        "scene", "turn_on", pick, user_id=requested_by_user_id or "",
        label="activate", target_name=pick))
    if not decision.allowed:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "blocked", reason_code="confirmation_not_approved"))
        return {"success": False, "error": "awaiting_confirmation", "pick": pick,
                "message": decision.note}
    try:
        await hass.services.async_call(
            "scene", "turn_on", {"entity_id": pick}, blocking=True
        )
    except Exception as exc:
        _LOGGER.error("Nova scene activation error: %s", exc)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "failed", reason_code="service_call_failed")
        )
        return {"success": False, "error": str(exc)}
    await hass.async_add_executor_job(
        lambda: action_log.set_execution(action_id, "accepted")
    )
    await hass.async_add_executor_job(
        lambda: action_log.mark_not_checkable(action_id))

    scene_name = next((s["name"] for s in scenes if s["entity_id"] == pick), pick)
    msg = f"Activating {scene_name}{addr}."
    if announce:
        await async_announce(hass, msg, tts_entity, speakers, context="scene")

    _LOGGER.info("Nova: scene '%s' activated for intent '%s'", pick, intent)
    return {"success": True, "scene": pick, "scene_name": scene_name}
