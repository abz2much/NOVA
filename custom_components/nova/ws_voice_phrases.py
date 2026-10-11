"""The Voice Phrases card's commands (8.32.0).

nova/voice_phrases_list, nova/voice_phrases_save, nova/voice_phrases_delete,
nova/voice_phrases_set_enabled and nova/voice_phrases_test. All admin only.
Every entry is checked here, whatever the panel sent (voice_phrases.py). The
test command only matches; it never runs anything.

Writes build a new value and save it with nova_config.set; they never change
the object nova_config.get returned. voice_phrases is not in
PANEL_WRITABLE_KEYS, so nova/update_config can't write it.

websocket.py imports the handlers by name (async_register registers them).
"""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from . import voice_phrases
from .safe_errors import safe_error_message

_LOGGER = logging.getLogger(f"{__package__}.websocket")

TEST_MAX_CHARS = 500
# One save at a time, so two quick saves can't lose each other's change.
_WRITE_LOCK = asyncio.Lock()


def _allowed_domains() -> set:
    from .agent_runtime.capabilities.control import _EXECUTE_PLAN_ALLOWED_DOMAINS
    return set(_EXECUTE_PLAN_ALLOWED_DOMAINS)


def _current() -> dict:
    """A copy of the saved phrases, always in the full shape."""
    from . import nova_config
    raw = copy.deepcopy(nova_config.get(voice_phrases.CONFIG_KEY))
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("entries")
    return {
        "enabled": raw.get("enabled", True) is not False,
        "entries": [e for e in entries if isinstance(e, dict)]
        if isinstance(entries, list) else [],
    }


async def _store(hass, value: dict) -> bool:
    from . import nova_config
    return bool(await hass.async_add_executor_job(
        nova_config.set, voice_phrases.CONFIG_KEY, value))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_phrases_list",
})
@websocket_api.async_response
async def ws_voice_phrases_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """The switch, every entry, and the domains a phrase may run."""
    try:
        cfg = _current()
        connection.send_result(msg["id"], {
            "enabled": cfg["enabled"],
            "entries": cfg["entries"],
            "allowed_domains": sorted(_allowed_domains() | {voice_phrases.SHELL_DOMAIN}),
        })
    except Exception as exc:
        connection.send_error(msg["id"], "voice_phrases_failed",
                              safe_error_message(exc, where="voice_phrases_list", log=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_phrases_save",
    vol.Required("entry"): dict,
})
@websocket_api.async_response
async def ws_voice_phrases_save(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add an entry, or update the one its phrase_id names. Answers {entry,
    warnings}, or invalid_value with every problem found."""
    try:
        async with _WRITE_LOCK:
            cfg = _current()
            clean, errors, warnings = voice_phrases.validate_entry(
                msg["entry"], cfg["entries"], allowed_domains=_allowed_domains())
            if clean is not None:
                domain, _, service = clean["action"]["service"].partition(".")
                if not hass.services.has_service(domain, service):
                    errors = [f"Home Assistant has no {clean['action']['service']} service."]
            if errors or clean is None:
                connection.send_error(msg["id"], "invalid_value", " ".join(errors))
                return
            for eid in voice_phrases.target_ids(clean):
                if hass.states.get(eid) is None:
                    warnings.append(f"{eid} was not found in Home Assistant.")
            entries = [clean if e.get("phrase_id") == clean["phrase_id"] else e
                       for e in cfg["entries"]]
            if not any(e.get("phrase_id") == clean["phrase_id"] for e in cfg["entries"]):
                entries.append(clean)
            if not await _store(hass, {"enabled": cfg["enabled"], "entries": entries}):
                warnings.append("Saved for now, but it could not be written to disk.")
        connection.send_result(msg["id"], {"entry": clean, "warnings": warnings})
    except Exception as exc:
        connection.send_error(msg["id"], "voice_phrases_failed",
                              safe_error_message(exc, where="voice_phrases_save", log=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_phrases_delete",
    vol.Required("phrase_id"): str,
})
@websocket_api.async_response
async def ws_voice_phrases_delete(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete one entry. {ok} is False when there was no such entry."""
    try:
        async with _WRITE_LOCK:
            cfg = _current()
            keep = [e for e in cfg["entries"] if e.get("phrase_id") != msg["phrase_id"]]
            if len(keep) == len(cfg["entries"]):
                connection.send_result(msg["id"], {"ok": False})
                return
            ok = await _store(hass, {"enabled": cfg["enabled"], "entries": keep})
        connection.send_result(msg["id"], {"ok": ok})
    except Exception as exc:
        connection.send_error(msg["id"], "voice_phrases_failed",
                              safe_error_message(exc, where="voice_phrases_delete", log=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_phrases_set_enabled",
    vol.Required("enabled"): bool,
})
@websocket_api.async_response
async def ws_voice_phrases_set_enabled(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Turn every voice phrase on or off at once."""
    try:
        async with _WRITE_LOCK:
            cfg = _current()
            await _store(hass, {"enabled": bool(msg["enabled"]), "entries": cfg["entries"]})
        connection.send_result(msg["id"], {"enabled": bool(msg["enabled"])})
    except Exception as exc:
        connection.send_error(msg["id"], "voice_phrases_failed",
                              safe_error_message(exc, where="voice_phrases_set_enabled",
                                                 log=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_phrases_test",
    vol.Required("text"): vol.All(str, vol.Length(min=1, max=TEST_MAX_CHARS)),
})
@websocket_api.async_response
async def ws_voice_phrases_test(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Which phrase these words would match right now, if any, exactly as
    a turn would match them. Runs nothing."""
    try:
        found = voice_phrases.match(msg["text"], _current())
        connection.send_result(msg["id"], {"match": None if found is None else {
            "phrase_id": found.phrase_id, "name": found.name,
            "phrase": found.phrase, "tier": found.tier}})
    except Exception as exc:
        connection.send_error(msg["id"], "voice_phrases_failed",
                              safe_error_message(exc, where="voice_phrases_test", log=True))
