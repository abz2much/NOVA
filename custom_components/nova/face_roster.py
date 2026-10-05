"""Nova's household resident roster.

A short list of the names Nova treats as residents. Nova has no face engine of
its own: names come from Frigate or Double Take (recognition.py). The roster
only says which of those names are household members. It is used by the Faces
tab, and, when the opt in `face_stand_down` setting is on, by the intrusion
stand down (see evaluate_stand_down).

Saved to <config>/nova/face_roster.json, readable only by Home Assistant's own
user (0600), written atomically in the executor under a lock. Loaded once at
setup, after paths.configure(); never at import time. A missing, empty or
corrupt file means an empty roster. Nothing here raises.

Names are compared with identity.normalize, so "Sam", "sam" and " Sam " are one
person. The file holds names only. No image is ever stored.
"""
from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

ROSTER_FILE = "face_roster.json"
MAX_NAME_LEN = 60
MAX_RESIDENTS = 50
STAND_DOWN_WINDOW_S = 180

_NAMES: dict[str, str] = {}        # normalised name -> display name
_SAVE_LOCK = asyncio.Lock()


def _normalize(name: str) -> str:
    from .identity import normalize
    return normalize(name)


def _path() -> str:
    from . import paths
    return paths.nova_path(ROSTER_FILE)


def clean_name(raw: Any) -> Optional[str]:
    """The name to store, or None when it is not acceptable: it must be a
    string, 1 to 60 characters after trimming and collapsing spaces, with no
    control characters, and must not be one of the backends' "unknown" labels
    (an "unknown" resident would make every stranger a resident)."""
    if type(raw) is not str:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in raw):
        return None
    name = " ".join(raw.split())
    if not name or len(name) > MAX_NAME_LEN:
        return None
    try:
        from .recognition import is_unknown_name
        if is_unknown_name(name):
            return None
    except Exception:
        return None
    return name


def names() -> list[str]:
    """The residents' display names, sorted."""
    return sorted(_NAMES.values(), key=str.lower)


def normalized_names() -> set[str]:
    return set(_NAMES)


def is_resident(name: Any) -> bool:
    """Whether `name` is on the roster (case and spacing do not matter)."""
    try:
        return isinstance(name, str) and _normalize(name) in _NAMES
    except Exception:
        return False


def load() -> None:
    """Load the saved roster. Call once at setup, after paths.configure().
    A missing, empty or corrupt file never raises: the roster is empty."""
    try:
        from .persistence.files import MISSING, OK, read_json
        res = read_json(_path())
        if res.status == MISSING:
            _LOGGER.debug("face roster: no saved roster")
            return
        data = res.value
        if res.status != OK or not isinstance(data, dict):
            _LOGGER.debug("face roster: ignoring unreadable roster (%s)",
                          res.error or "not an object")
            return
        loaded: dict[str, str] = {}
        items = data.get("residents")
        for raw in items if isinstance(items, list) else []:
            name = clean_name(raw)
            if name and len(loaded) < MAX_RESIDENTS:
                loaded.setdefault(_normalize(name), name)
        _NAMES.clear()
        _NAMES.update(loaded)
        _LOGGER.debug("face roster: loaded %d residents", len(_NAMES))
    except Exception as exc:
        _LOGGER.debug("face roster: could not load: %s", exc)


def _write(data: dict) -> None:
    from .persistence.files import write_json_atomic
    write_json_atomic(_path(), data, indent=2, mode=0o600)


async def _save(hass) -> bool:
    """Save without blocking the event loop. The snapshot is taken under the
    lock just before the write, so concurrent saves leave the newest state.
    Never raises; False when the write failed."""
    try:
        async with _SAVE_LOCK:
            await hass.async_add_executor_job(_write, {"residents": names()})
        return True
    except Exception as exc:
        _LOGGER.warning("face roster: could not save: %s", exc)
        return False


async def async_add(hass, raw: Any) -> dict:
    """Add a resident. {"ok", "added", "error", "residents"}. Never raises."""
    name = clean_name(raw)
    if name is None:
        return {"ok": False, "added": False, "error": "invalid_name", "residents": names()}
    try:
        key = _normalize(name)
        if key in _NAMES:
            return {"ok": True, "added": False, "error": None, "residents": names()}
        if len(_NAMES) >= MAX_RESIDENTS:
            return {"ok": False, "added": False, "error": "roster_full", "residents": names()}
        _NAMES[key] = name
        saved = await _save(hass)
        return {"ok": True, "added": True, "error": None if saved else "not_saved",
                "residents": names()}
    except Exception as exc:
        _LOGGER.debug("face roster: add failed: %s", exc)
        return {"ok": False, "added": False, "error": "failed", "residents": names()}


async def async_remove(hass, raw: Any) -> dict:
    """Remove a resident. {"ok", "removed", "error", "residents"}. Never raises."""
    if type(raw) is not str or not raw.strip():
        return {"ok": False, "removed": False, "error": "invalid_name", "residents": names()}
    try:
        removed = _NAMES.pop(_normalize(raw), None) is not None
        saved = await _save(hass) if removed else True
        return {"ok": True, "removed": removed, "error": None if saved else "not_saved",
                "residents": names()}
    except Exception as exc:
        _LOGGER.debug("face roster: remove failed: %s", exc)
        return {"ok": False, "removed": False, "error": "failed", "residents": names()}


# ── intrusion stand down ────────────────────────────────────────────────────

def evaluate_stand_down(window_s: float = STAND_DOWN_WINDOW_S) -> dict:
    """Whether a recognised resident should stop Nova opening a NEW intrusion
    investigation right now. Pure decision, no side effects, never raises; any
    error answers "do not stand down" so the alert goes ahead.

    Stand down only when ALL hold, using only what the backends reported to
    Nova (recognition.remember_recognition) in the last `window_s` seconds:
      1. a roster name was recognised at or above CONFIDENCE_THRESHOLD;
      2. no other face was reported in that window on any camera (an unknown
         face, a face below the threshold, or a named face that is not on the
         roster all count as "other");
      3. every Frigate person detection in the window was on a camera where a
         resident was recognised (an unexplained person blocks it).
    Returns {"stand_down": bool, "reason": str, and, when True, name, camera_entity,
    confidence, age_seconds, source}."""
    no = {"stand_down": False}
    try:
        from . import recognition
        threshold = recognition.CONFIDENCE_THRESHOLD
        events = recognition.recent_face_events(window_s)

        def _trusted(e: dict) -> bool:
            return (not recognition.is_unknown_name(e.get("name"))
                    and is_resident(e.get("name"))
                    and float(e.get("confidence") or 0.0) >= threshold)

        residents = [e for e in events if _trusted(e)]
        if not residents:
            return {**no, "reason": "no trusted resident recognition in the window"}
        others = [e for e in events if not _trusted(e)]
        if others:
            o = others[-1]
            what = ("an unknown face" if recognition.is_unknown_name(o.get("name"))
                    else "a face that is not a confirmed resident")
            return {**no, "reason": f"{what} was also seen on "
                                    f"{str(o.get('camera_entity', '')).replace('camera.', '')}"}
        covered = {e["camera_entity"] for e in residents}
        loose = [p for p in recognition.recent_person_detections(window_s)
                 if p["camera_entity"] not in covered]
        if loose:
            return {**no, "reason": "a person was detected on "
                                    f"{loose[-1]['camera_entity'].replace('camera.', '')} "
                                    "with no resident recognised there"}
        best = max(residents, key=lambda e: (float(e.get("confidence") or 0.0),
                                             -int(e.get("age_seconds", 0))))
        return {
            "stand_down": True,
            "reason": (f"{best['name']} (resident) recognised on "
                       f"{best['camera_entity'].replace('camera.', '')} at "
                       f"{float(best['confidence']):.0f}% {int(best['age_seconds'])}s ago; "
                       f"no other face or unexplained person in the last {int(window_s)}s"),
            "name": best["name"],
            "camera_entity": best["camera_entity"],
            "confidence": float(best["confidence"]),
            "age_seconds": int(best["age_seconds"]),
            "source": best.get("source", ""),
        }
    except Exception as exc:
        _LOGGER.debug("face roster: stand down evaluation failed: %s", exc)
        return {**no, "reason": "error while checking, so the alert goes ahead"}
