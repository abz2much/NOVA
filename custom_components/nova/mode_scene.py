"""What a mode does to the room when it switches on (v7.15.0).

Only Movie mode has a mood: the lights in its room go down to a level the
user chose. The work lives here, not in modes.py, because modes.py holds
plain saved state with no Home Assistant in it. Every failure is caught and
logged, so a mood can never stop a mode from switching.
"""
from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)

_DEFAULT_DIM = 15


def _movie_area(hass, nova_config, audio_routing) -> Optional[str]:
    named = nova_config.get("movie_area", "") or ""
    if named:
        return str(named)
    player = nova_config.get("movie_media_player", "") or ""
    if not player:
        return None
    return audio_routing.entity_area(hass, player)


def _dim_level(nova_config) -> int:
    try:
        level = int(nova_config.get("movie_dim_pct", _DEFAULT_DIM))
    except Exception:
        level = _DEFAULT_DIM
    return min(100, max(0, level))


async def apply_mode_entry(
    hass, mode: str, *,
    request_id: Optional[str] = None,
    source: str = "chat",
    requested_by_user_id: Optional[str] = None,
    requested_by_name: Optional[str] = None,
) -> None:
    """Run the mood for a mode that has just switched on.

    Both the `set_mode` conversational tool and the panel's mode button call
    this, and each is a real top-level trigger for the Action Audit Log. A
    caller that already holds a request id for the mode change passes it, so
    the light change joins that request. With no id, this call starts its
    own request."""
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    try:
        if mode != "movie":
            return
        from . import audio_routing, nova_config
        area = _movie_area(hass, nova_config, audio_routing)
        if not area:
            return
        level = _dim_level(nova_config)
        lights = [
            state.entity_id
            for state in hass.states.async_all("light")
            if audio_routing.entity_area(hass, state.entity_id) == area
        ]
        if not lights:
            return

        def _open_row():
            return action_log.start(
                request_id, "mode_entry_mood", source,
                requested_by_user_id=requested_by_user_id,
                requested_by_name=requested_by_name,
                domain="light", entity_id=f"area:{area}",
                requested_state=f"{level}%",
            )

        row = await hass.async_add_executor_job(_open_row)
        if level == 0:
            service, data = "turn_off", {"entity_id": lights}
        else:
            service, data = "turn_on", {"entity_id": lights,
                                        "brightness_pct": level}
        await hass.services.async_call("light", service, data, blocking=False)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(row, "accepted"))
        _LOGGER.info("Movie mood: dimmed %d light(s) in %s to %d%%",
                     len(lights), area, level)
    except Exception as exc:
        _LOGGER.debug("apply_mode_entry(%s) failed: %s", mode, exc)
