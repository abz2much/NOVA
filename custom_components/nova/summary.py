"""Conversation summary service: a short spoken briefing of recent history.

Reads the recent conversation log, asks the model (through the provider
activity boundary) for a few sentences in Nova's voice, optionally writes the
summary back into the log and optionally announces it. An empty log yields a
fixed "nothing to report" line without calling the model.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .safe_errors import safe_error_message
from .const import NOVA_PERSONA  # noqa: F401
from .database import get_recent_messages, save_message
from .directive_helper import build_system_prompt
from .tts_helper import async_announce

_LOGGER = logging.getLogger(__name__)

SUMMARY_MODEL = "openai/gpt-oss-120b"


def _period(hours: int) -> str:
    """Plain-English span for ``hours``: whole days of two or more in days,
    anything else in hours."""
    if hours <= 1:
        return "hour"
    if hours <= 24 or hours % 24:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _transcript_line(message: dict) -> str:
    stamp = message["timestamp"][:16].replace("T", " ")
    speaker = "You" if message["role"] == "user" else "Nova"
    return f"[{stamp}] {speaker}: {message['content']}"


async def async_summarise(hass: HomeAssistant, call: ServiceCall, groq_client,
                          honorific: str, tts_entity: str | None,
                          speakers: list[str]) -> dict:
    """Summarise recent conversation; store and/or announce the result."""
    hours = int(call.data.get("hours", 24))
    device_id = call.data.get("device_id")
    announce = call.data.get("announce", True)
    store = call.data.get("store", True)
    period = _period(hours)

    messages = await hass.async_add_executor_job(
        lambda: get_recent_messages(hours=hours, device_id=device_id, limit=300)
    )

    if not messages:
        addr = f", {honorific}" if honorific else ""
        quiet = (f"Nothing to report{addr}. The last {period} "
                 f"{'appears' if period == 'hour' else 'appear'} "
                 "to have been remarkably quiet.")
        if announce:
            await async_announce(hass, quiet, tts_entity, speakers,
                                 context="summary")
        return {"success": True, "summary": quiet, "message_count": 0}

    transcript = "\n".join(_transcript_line(m) for m in messages)
    audience = f"to {honorific}" if honorific else "to the household"
    task = (
        f"Summarise the conversation transcript from the past {period}. "
        "Keep it to 3–5 sentences maximum. Highlight anything notable, "
        "unusual or actionable. Speak as Nova giving a briefing "
        f"{audience}. Extract the essence; do not list every exchange."
    )
    system = build_system_prompt(hass, honorific, task)
    from . import output_language
    system = output_language.with_language(hass, system)

    try:
        from .providers.activity import execute_chat
        result = await execute_chat(
            hass,
            groq_client,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "Transcript:\n\n" + transcript},
            ],
            role="summary",
            data_category="text",
            max_tokens=300,
            temperature=0.7,
        )
        summary = result.text.strip()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Nova: conversation summary failed: %s", exc)
        return {"success": False, "error": safe_error_message(exc)}

    if store:
        await hass.async_add_executor_job(
            save_message, "assistant", f"[Summary — {period}] {summary}", "summary"
        )
    if announce:
        await async_announce(hass, summary, tts_entity, speakers,
                             context="summary")

    _LOGGER.info("Nova: summarised %d messages from the last %s",
                 len(messages), period)
    return {"success": True, "summary": summary,
            "message_count": len(messages), "period_hours": hours}
