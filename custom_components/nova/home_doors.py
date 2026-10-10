"""Does the home have a garage, and which exit doors did the user pick (8.26.0).

Display only. Nothing here changes what Nova secures, checks or alerts on:
lockdown, the night sweep, intrusion and the world model go by device class
over every entity, whatever this module says. It decides only whether the
panel and the AI show garage words, and it describes the exit doors the user
picked on the Residence tab.

has_garage() is True only on a reliable signal, unless the user's "Garage"
setting overrides it:
  * a cover with device class garage
  * a binary_sensor with device class garage_door
  * a Home Assistant area named garage
  * a Garage Door slot the user mapped on the Residence tab
A name that merely contains "garage", the default floor plan's Garage room and
the garage bays count never count: a gate or a car sensor can be named garage.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

GARAGE_MODES = ("auto", "yes", "no")
DEFAULT_GARAGE_MODE = "auto"

# Door mapping slots that mean "a garage door" (residence.js: garage, garage_1..8).
_GARAGE_SLOT = re.compile(r"^garage(?:_[1-8])?$")
_GARAGE_WORD = re.compile(r"\bgarages?\b", re.IGNORECASE)

# Exit doors the user may pick, and how many.
EXIT_DOOR_DOMAINS = ("binary_sensor", "lock", "cover")
MAX_EXIT_DOORS = 12
_MAX_NAME = 40


def _json(value: Any, default):
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return default
        return parsed if isinstance(parsed, type(default)) else default
    return default


def garage_mode(config: Optional[dict]) -> str:
    mode = str((config or {}).get("garage_mode") or DEFAULT_GARAGE_MODE).lower()
    return mode if mode in GARAGE_MODES else DEFAULT_GARAGE_MODE


def _area_names(hass) -> list:
    try:
        from homeassistant.helpers import area_registry as ar
        return [str(getattr(a, "name", "") or "") for a in ar.async_get(hass).async_list_areas()]
    except Exception:
        return []


def garage_signals(hass, config: Optional[dict] = None) -> list:
    """The reliable signals found, as short labels. Empty when none."""
    found = []
    for st in hass.states.async_all("cover"):
        if st.attributes.get("device_class") == "garage":
            found.append(f"cover:{st.entity_id}")
    for st in hass.states.async_all("binary_sensor"):
        if st.attributes.get("device_class") == "garage_door":
            found.append(f"sensor:{st.entity_id}")
    for name in _area_names(hass):
        if _GARAGE_WORD.search(name):
            found.append(f"area:{name}")
    mapping = _json((config or {}).get("door_mapping"), {})
    for slot, eid in mapping.items():
        if _GARAGE_SLOT.match(str(slot)) and eid:
            found.append(f"mapped:{slot}")
    return found


def has_garage(hass, config: Optional[dict] = None) -> bool:
    """Does this home have a garage? Yes and No override everything; Auto
    looks for a reliable signal. Never raises: a failed read is "no garage",
    which only hides words, never a safety check."""
    mode = garage_mode(config)
    if mode == "yes":
        return True
    if mode == "no":
        return False
    try:
        return bool(garage_signals(hass, config))
    except Exception:
        return False


# ── exit doors the user picked ──────────────────────────────────────────────

def exit_doors(config: Optional[dict]) -> list:
    """The user's exit doors, cleaned: [{"entity_id", "name"}], in order, at
    most MAX_EXIT_DOORS, no repeats, only the allowed domains."""
    out, seen = [], set()
    for item in _json((config or {}).get("exit_doors"), []):
        if not isinstance(item, dict):
            continue
        eid = str(item.get("entity_id") or "").strip()
        if eid.split(".")[0] not in EXIT_DOOR_DOMAINS or eid in seen:
            continue
        name = " ".join(str(item.get("name") or "").split())[:_MAX_NAME]
        seen.add(eid)
        out.append({"entity_id": eid, "name": name})
        if len(out) >= MAX_EXIT_DOORS:
            break
    return out


def safety_coverage(hass, state, exempt_locks=()) -> list:
    """Which of Nova's safety checks already cover this entity, using the same
    rules they use. Read only: picking an exit door never adds it to a check.
    Returns names from: lockdown, night_sweep, intrusion, world_model."""
    from . import household
    from .core_lockdown import LockdownManager
    from .world import _ENTRY_COVERS, _OPENING_SENSORS
    dom = state.entity_id.split(".")[0]
    dc = state.attributes.get("device_class")
    out = []
    if dom == "lock":
        if state.entity_id not in set(exempt_locks):
            out += ["lockdown", "night_sweep"]
        return out
    way_in = household.is_way_in(hass, state)
    if dom == "cover":
        if dc in LockdownManager._CLOSEABLE_COVERS:
            out.append("lockdown")
        out.append("night_sweep")
        if dc in _ENTRY_COVERS and way_in:
            out += ["intrusion", "world_model"]
    elif dom == "binary_sensor":
        if dc in LockdownManager._DOOR_WINDOW_BS and way_in:
            out.append("lockdown")
        if dc in _OPENING_SENSORS and way_in:
            out += ["intrusion", "world_model"]
    return out


def exit_door_status(hass, config: Optional[dict] = None, exempt_locks=()) -> list:
    """Each picked exit door with its live state and the checks that already
    cover it. A door whose entity is gone is listed as missing."""
    out = []
    for door in exit_doors(config):
        st = hass.states.get(door["entity_id"])
        row = dict(door, state=None, checks=[])
        if st is not None:
            row["state"] = str(st.state)
            if not row["name"]:
                row["name"] = str(st.attributes.get("friendly_name") or door["entity_id"])
            try:
                row["checks"] = safety_coverage(hass, st, exempt_locks)
            except Exception:
                row["checks"] = []
        out.append(row)
    return out


def exit_door_candidates(hass, config: Optional[dict] = None, limit: int = 30) -> list:
    """Suggestions only: door and opening sensors, door, gate and garage
    covers, and locks. Nothing is added until the user picks one, and the
    user may pick any lock, cover or binary sensor, suggested or not.
    Already picked and already mapped entities are left out."""
    taken = {d["entity_id"] for d in exit_doors(config)}
    taken |= {str(v) for v in _json((config or {}).get("door_mapping"), {}).values() if v}
    out = []
    for dom in EXIT_DOOR_DOMAINS:
        for st in hass.states.async_all(dom):
            dc = st.attributes.get("device_class")
            if st.entity_id in taken:
                continue
            if dom == "binary_sensor" and dc not in ("door", "opening", "garage_door"):
                continue
            if dom == "cover" and dc not in ("door", "garage", "gate"):
                continue
            name = str(st.attributes.get("friendly_name") or st.entity_id)
            out.append({"entity_id": st.entity_id, "name": name})
    out.sort(key=lambda r: (r["name"].lower(), r["entity_id"]))
    return out[:limit]
