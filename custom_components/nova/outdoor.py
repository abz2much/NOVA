"""Indoor/outdoor classification for Home Assistant entities.

Nova uses this module as its single answer to "is this entity outside the
house?", so intrusion logic can disregard yard, driveway and street sensors.
It also decides which outdoor detection types deserve a notification, and
offers two pure helpers the panel uses to pin an entity indoor or outdoor.
"""
from __future__ import annotations

import fnmatch
import json
import re
from typing import Any, Optional

DEFAULT_OUTDOOR_AREAS: set[str] = {
    "backyard", "back_yard", "front_yard", "front_door", "driveway", "patio",
    "porch", "side_yard", "deck", "garden", "yard", "pool", "exterior",
    "outside", "outdoor", "outdoors", "shed", "carport", "walkway", "sidewalk",
    "lawn", "curb", "gazebo",
}

# Which outdoor detection types are worth telling the user about.
NOTABLE_OUTDOOR: dict[str, bool] = {
    "person": True,
    "package": True,
    "mail": True,
    "damage": True,
    "vehicle": False,
    "animal": False,
    "motion": False,
}

# Name keywords that mark an entity as outdoor. "front", "door" and "garage"
# are deliberately absent: front-door contacts and garage doors are envelope.
_OUTDOOR_KEYWORDS = (
    "outdoor", "outside", "exterior", "backyard", "back_yard", "frontyard",
    "front_yard", "sideyard", "side_yard", "yard", "garden", "driveway",
    "porch", "patio", "deck", "shed", "gazebo", "carport", "curb", "street",
    "sidewalk", "walkway", "lawn", "pool", "doorbell", "mailbox", "gate",
    "fence",
)

# A keyword counts only as a whole word: bounded by text edges, ".", "_",
# "-" or whitespace.
_KEYWORD_RE = re.compile(
    r"(?:^|(?<=[._\s-]))(?:"
    + "|".join(re.escape(k) for k in _OUTDOOR_KEYWORDS)
    + r")(?=$|[._\s-])",
    re.IGNORECASE,
)


def _slug(text: str) -> str:
    """Strip, lower-case and collapse whitespace runs into underscores."""
    return re.sub(r"\s+", "_", text.strip().lower())


def _cfg_list(key: str) -> list[str]:
    """Read a list-valued Nova option, normalised; [] on anything unexpected."""
    try:
        from . import nova_config

        value: Any = nova_config.get(key)
        if isinstance(value, str) and value.strip():
            value = json.loads(value)
        if not isinstance(value, (list, tuple)):
            return []
        items = (str(item).lower().strip() for item in value)
        return [item for item in items if item]
    except Exception:  # noqa: BLE001 - config problems must never propagate
        return []


def _area_slug(hass: Any, entity_id: str) -> str:
    """Return the slugged HA area name of an entity, or "" if it has none."""
    if hass is None:
        return ""
    try:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(hass).async_get(entity_id)
        if entry is None:
            return ""
        area_id = getattr(entry, "area_id", None)
        device_id = getattr(entry, "device_id", None)
        if not area_id and device_id:
            device = dr.async_get(hass).async_get(device_id)
            area_id = getattr(device, "area_id", None) if device else None
        if not area_id:
            return ""
        area = ar.async_get(hass).async_get_area(area_id)
        name = getattr(area, "name", None) if area else None
        return _slug(str(name or area_id))
    except Exception:  # noqa: BLE001 - registry access is best effort
        return ""


def _matches_any(patterns: list[str], *texts: str) -> bool:
    return any(fnmatch.fnmatch(text, pat) for pat in patterns for text in texts)


def is_outdoor(hass: Any, entity_id: str, friendly_name: Optional[str] = None) -> bool:
    """Classify an entity as outdoor (True) or indoor/envelope (False)."""
    eid = (entity_id or "").lower()
    if not friendly_name and hass is not None:
        try:
            state = hass.states.get(entity_id)
            friendly_name = state.attributes.get("friendly_name") if state else None
        except Exception:  # noqa: BLE001
            friendly_name = None
    name = (friendly_name or "").lower()

    # Layer 1: explicit indoor pins win over everything.
    if _matches_any(_cfg_list("indoor_entities"), eid, name):
        return False
    # Layer 2: explicit outdoor pins.
    if _matches_any(_cfg_list("outdoor_entities"), eid, name):
        return True
    # Layer 3: the entity's Home Assistant area (looked up via module global
    # so tests can monkeypatch it).
    area = _area_slug(hass, entity_id)
    if area and (area in DEFAULT_OUTDOOR_AREAS or area in _cfg_list("outdoor_areas")):
        return True
    # Layer 4: whole-word keywords in the id or friendly name.
    return bool(_KEYWORD_RE.search(eid) or _KEYWORD_RE.search(name))


def location_mode(entity_id: str, indoor: list[str], outdoor: list[str]) -> str:
    """Report how an entity is pinned: "indoor", "outdoor" or "auto"."""
    eid = (entity_id or "").lower()
    if any(eid == str(item).lower() for item in indoor or []):
        return "indoor"
    if any(eid == str(item).lower() for item in outdoor or []):
        return "outdoor"
    return "auto"


def set_entity_location(
    indoor: list[str], outdoor: list[str], entity_id: str, mode: str
) -> tuple[list[str], list[str]]:
    """Return new (indoor, outdoor) lists with the entity pinned per ``mode``."""
    eid = (entity_id or "").lower().strip()
    new_indoor = [item for item in indoor or [] if str(item).lower() != eid]
    new_outdoor = [item for item in outdoor or [] if str(item).lower() != eid]
    if mode == "indoor":
        new_indoor.append(eid)
    elif mode == "outdoor":
        new_outdoor.append(eid)
    return new_indoor, new_outdoor


def notable(
    hass: Any,
    entity_id: str,
    detection_type: str = "motion",
    area_name: Optional[str] = None,
) -> bool:
    """Whether a detection is worth notifying about (outdoor and notable type)."""
    if area_name is not None:
        slug = _slug(area_name)
        outside = slug in DEFAULT_OUTDOOR_AREAS or slug in _cfg_list("outdoor_areas")
    else:
        outside = is_outdoor(hass, entity_id)
    if not outside:
        return False
    return NOTABLE_OUTDOOR.get(str(detection_type).lower(), False)
