"""Situations that survive a restart or reload (8.24.0).

Some of what Nova is in the middle of used to live only in memory, so a
restart or a settings reload (the Configure dialog reloads Nova) lost it:
an open intrusion investigation ended silently, a parcel already on the step
was announced as newly delivered, and the freeze warning could repeat. This
module saves that state to one small file and gives it back on start:

  * intrusion  the investigation in progress, exactly as the safety manager
               holds it, plus when the last first alert went out. Restored
               only if saved in the last 10 minutes.
  * delivery   per camera: package present or gone, mail, count. Restored
               only if saved in the last 24 hours.
  * hazard     whether the freeze warning was given and when, and how many
               weather warnings are active. Restored if saved in the last
               24 hours.

The decisions themselves are unchanged: this only saves and restores state.
It is off until setup calls start(); before that every save and restore is a
no-op, so nothing writes to disk unless Nova is really running. Fails safe: a
missing or corrupt file starts fresh, exactly as before this existed, and a
save that fails is logged and ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from . import paths

_LOGGER = logging.getLogger(__name__)

STATE_FILE: Optional[str] = None  # override; None resolves via paths.py

INTRUSION_MAX_AGE = 600.0         # 10 minutes
DELIVERY_MAX_AGE = 86400.0        # 24 hours
HAZARD_MAX_AGE = 86400.0          # 24 hours

# Investigation keys that hold sets in memory and lists on disk.
_SET_KEYS = ("zones", "connected")

_state: Optional[dict] = None
_started = False


def _file() -> str:
    return STATE_FILE or paths.nova_path("situations.json")


def started() -> bool:
    return _started


def _read_file() -> dict:
    try:
        with open(_file(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        _LOGGER.warning("situations: %s is not a JSON object, starting fresh", _file())
    except FileNotFoundError:
        pass
    except Exception as exc:
        _LOGGER.warning("situations: could not read %s (%s), starting fresh", _file(), exc)
    return {}


def start(data: Optional[dict] = None) -> None:
    """Switch saving and restoring on. Blocking (reads the file) unless
    `data` is given; setup runs it in the executor."""
    global _state, _started
    _state = data if data is not None else _read_file()
    _started = True
    try:
        from . import world
        world.register_situation_source(open_kinds)
    except Exception:
        pass


async def async_start(hass) -> None:
    """start() with the file read off the event loop."""
    data = await hass.async_add_executor_job(_read_file)
    start(data)


def stop() -> None:
    global _state, _started
    _state, _started = None, False


def _write(snapshot: dict) -> None:
    try:
        from .persistence.files import write_json_atomic
        write_json_atomic(_file(), snapshot, indent=2)
    except Exception as exc:
        _LOGGER.warning("situations: could not save: %s", exc)


def _save() -> None:
    """Write the state. On the event loop the write goes to the executor."""
    if not _started or _state is None:
        return
    snapshot = json.loads(json.dumps(_state, default=str))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _write(snapshot)
    else:
        loop.run_in_executor(None, _write, snapshot)


def _fresh(saved_at: Any, max_age: float, now: Optional[float] = None) -> bool:
    try:
        return (now or time.time()) - float(saved_at) <= max_age
    except (TypeError, ValueError):
        return False


# ── intrusion ───────────────────────────────────────────────────────────────

def _encode_investigation(inv: Optional[dict]) -> Optional[dict]:
    if inv is None:
        return None
    out = dict(inv)
    for key in _SET_KEYS:
        if isinstance(out.get(key), (set, frozenset)):
            out[key] = sorted(str(x) for x in out[key] if x is not None)
    return out


def _decode_investigation(inv: Optional[dict]) -> Optional[dict]:
    if not isinstance(inv, dict):
        return None
    out = dict(inv)
    for key in _SET_KEYS:
        if isinstance(out.get(key), list):
            out[key] = set(out[key])
    return out


def save_intrusion(investigation: Optional[dict], last_alert: float) -> None:
    if not _started or _state is None:
        return
    _state["intrusion"] = {"saved_at": time.time(),
                           "investigation": _encode_investigation(investigation),
                           "last_alert": float(last_alert or 0.0)}
    _save()


def restore_intrusion(now: Optional[float] = None) -> tuple[Optional[dict], float]:
    """(investigation or None, last first-alert time). Older than 10 minutes
    is dropped: a stale investigation is never picked back up."""
    if not _started or _state is None:
        return None, 0.0
    rec = _state.get("intrusion")
    if not isinstance(rec, dict) or not _fresh(rec.get("saved_at"), INTRUSION_MAX_AGE, now):
        return None, 0.0
    try:
        last = float(rec.get("last_alert") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return _decode_investigation(rec.get("investigation")), last


# ── delivery ────────────────────────────────────────────────────────────────

def save_delivery(camera: str, state: dict) -> None:
    if not _started or _state is None or not camera:
        return
    deliveries = _state.setdefault("delivery", {})
    rec = {k: v for k, v in dict(state).items() if k in ("package", "mail", "count", "since",
                                                         "desc")}
    rec["saved_at"] = time.time()
    deliveries[str(camera)] = rec
    _save()


def restore_deliveries(now: Optional[float] = None) -> dict:
    """{camera: state} saved in the last 24 hours."""
    if not _started or _state is None:
        return {}
    out = {}
    for camera, rec in (_state.get("delivery") or {}).items():
        if isinstance(rec, dict) and _fresh(rec.get("saved_at"), DELIVERY_MAX_AGE, now):
            out[camera] = {k: v for k, v in rec.items() if k != "saved_at"}
    return out


# ── hazard ──────────────────────────────────────────────────────────────────

def save_freeze(warned: bool, last_alert: float) -> None:
    if not _started or _state is None:
        return
    hazard = _state.setdefault("hazard", {})
    hazard.update(freeze_warned=bool(warned), last_freeze_alert=float(last_alert or 0.0),
                  saved_at=time.time())
    _save()


def restore_freeze(now: Optional[float] = None) -> tuple[bool, float]:
    if not _started or _state is None:
        return False, 0.0
    hazard = _state.get("hazard")
    if not isinstance(hazard, dict) or not _fresh(hazard.get("saved_at"), HAZARD_MAX_AGE, now):
        return False, 0.0
    try:
        return bool(hazard.get("freeze_warned")), float(hazard.get("last_freeze_alert") or 0.0)
    except (TypeError, ValueError):
        return False, 0.0


def note_weather(active: int) -> None:
    """How many weather warnings are active now (the warnings themselves are
    already saved by hazard_monitor's own store)."""
    if not _started or _state is None:
        return
    hazard = _state.setdefault("hazard", {})
    if hazard.get("weather_active") == int(active):
        return
    hazard.update(weather_active=int(active), saved_at=time.time())
    _save()


# ── open situations, for world.py ───────────────────────────────────────────

def open_kinds(hass=None, now: Optional[float] = None) -> list[str]:
    """The kinds with something open: an intrusion investigation, a package
    waiting, a freeze warning or an active weather warning."""
    if not _started or _state is None:
        raise LookupError("situations not started")
    kinds = []
    if restore_intrusion(now)[0] is not None:
        kinds.append("intrusion")
    if any(rec.get("package") for rec in restore_deliveries(now).values()):
        kinds.append("delivery")
    hazard = _state.get("hazard") or {}
    if _fresh(hazard.get("saved_at"), HAZARD_MAX_AGE, now) and (
            hazard.get("freeze_warned") or int(hazard.get("weather_active") or 0) > 0):
        kinds.append("hazard")
    return kinds
