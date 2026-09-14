"""
Nova — Eufy Security native-sensor discovery.

The `eufy_security` HACS integration exposes a doorbell/camera as a whole
family of sibling entities on one HA device: a press sensor, person/stranger/
pet/crying/sound detection, three-state package tracking, and a ready event
snapshot — all computed by Eufy's own on-device/cloud models, at zero LLM
cost and (for stranger-vs-known) more reliably than a vision model guessing
from a photo.

Nova's existing camera pipeline (camera.py) was built around Nest/Frigate,
which fire custom HA bus events (`nest_event`, `frigate_event`). Eufy fires
no such event — every signal here is a plain entity state change — so this
module exists to (a) find which of Nova's cameras are Eufy-backed and what
their sibling entities are, and (b) do it in a way that survives the user
renaming entities.

Discovery is unique_id-based, not entity_id-based. `unique_id` is the
immutable half of HA's entity identity (entity_id is the mutable, renameable
half) and eufy_security encodes each sensor's role directly in it:
  eufy_security_<serial>_device_<role>
e.g. "eufy_security_T82145102549156A_device_strangerPersonDetected". Matching
on this suffix means a rename ("Front Door" -> "Porch Camera") can never
break discovery, unlike matching on entity_id text.
"""
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORM = "eufy_security"

# unique_id suffix (after "..._device_") -> canonical role name. Verified
# live against a real eufy_security install (v7.101.4) rather than guessed —
# case matters, these are exact.
_ROLE_SUFFIXES = {
    "ringing": "ringing",
    "strangerPersonDetected": "stranger",
    "identityPersonDetected": "identity",
    "personDetected": "person",           # checked after stranger/identity —
                                           # both of those ALSO end in a form
                                           # of "...PersonDetected" territory
                                           # conceptually, so order matters if
                                           # eufy ever nests the strings; today
                                           # they're distinct exact suffixes.
    "petDetected": "pet",
    "cryingDetected": "crying",
    "soundDetected": "sound",
    "packageDelivered": "package_delivered",
    "packageStranded": "package_stranded",
    "packageTaken": "package_taken",
    "snooze": "snooze",
}
# Longest-suffix-first so an exact match always wins over a shorter one that
# happens to also be a suffix of it (defensive; today no role is a suffix of
# another, but this keeps it correct if Eufy ever adds e.g. a plain
# "personDetected" alias of "strangerPersonDetected").
_ROLE_ORDER = sorted(_ROLE_SUFFIXES, key=len, reverse=True)


def _role_for_unique_id(unique_id: str) -> Optional[str]:
    if not unique_id:
        return None
    for suffix in _ROLE_ORDER:
        if unique_id.endswith(f"_device_{suffix}"):
            return _ROLE_SUFFIXES[suffix]
    return None


def is_eufy_camera(hass: HomeAssistant, camera_entity_id: str) -> bool:
    """Whether this camera entity belongs to the eufy_security integration."""
    try:
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(camera_entity_id)
        return bool(entry and entry.platform == PLATFORM)
    except Exception:
        return False


def discover_roles(hass: HomeAssistant, camera_entity_id: str) -> dict[str, str]:
    """
    Role name -> entity_id for every recognised sibling sensor on this
    camera's Eufy device, plus "event_image" for its image.* entity if one
    exists. Empty dict if this isn't a Eufy camera or has no device.
    Never raises.
    """
    out: dict[str, str] = {}
    try:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        cam_entry = reg.async_get(camera_entity_id)
        if not cam_entry or cam_entry.platform != PLATFORM or not cam_entry.device_id:
            return out
        device_id = cam_entry.device_id
        for entry in reg.entities.values():
            if entry.device_id != device_id or entry.platform != PLATFORM:
                continue
            if entry.domain == "image":
                out.setdefault("event_image", entry.entity_id)
                continue
            role = _role_for_unique_id(entry.unique_id or "")
            if role:
                out[role] = entry.entity_id
    except Exception as exc:
        _LOGGER.debug("Nova eufy: discover_roles failed for %s: %s", camera_entity_id, exc)
    return out


def all_camera_roles(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """camera_entity_id -> its role map, for every active Eufy camera Nova
    knows about. Only cameras with at least one recognised sibling are
    included. Never raises."""
    out: dict[str, dict[str, str]] = {}
    try:
        from .camera import active_cameras
        for cam in active_cameras(hass):
            if not is_eufy_camera(hass, cam):
                continue
            roles = discover_roles(hass, cam)
            if roles:
                out[cam] = roles
    except Exception as exc:
        _LOGGER.debug("Nova eufy: all_camera_roles failed: %s", exc)
    return out


async def async_fetch_event_image(hass: HomeAssistant, image_entity_id: str) -> Optional[bytes]:
    """Fetch the current bytes of an image.* entity (Eufy's 'event image' —
    the frame frozen at the moment of the triggering event). Generic HA
    image-component fetch, not Eufy-specific. Never raises."""
    try:
        from homeassistant.components.image import async_get_image
        img = await async_get_image(hass, image_entity_id)
        return getattr(img, "content", None)
    except Exception as exc:
        _LOGGER.debug("Nova eufy: event image fetch failed for %s: %s", image_entity_id, exc)
        return None
