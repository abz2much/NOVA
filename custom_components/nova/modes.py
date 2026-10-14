"""Nova's operational modes (the Directive Layer).

Exactly one named mode is active at a time (``normal`` by default), and it is
persisted under Nova's folder (nova/mode_state.json) so it survives a
restart. A mode states only the behaviour fields it changes; the rest fall
back to ``normal``. The proactive layer, autonomy, persona banter, announce
scope and room scope consult the active mode; safety never does (lockdown is
separate). The ``custom_modes`` setting can add or override modes, and an
optional automatic mode keeps ``away``/``normal`` in step with occupancy.
Nothing here raises to callers.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from . import paths

_LOGGER = logging.getLogger(__name__)

# Tests point this at a temporary file; None means Nova's own folder.
MODE_STATE_PATH: Optional[str] = None

DEFAULT_MODE = "normal"

# name: (proactive, banter or None, announce scope, auto actions, quiet, description)
_MODE_TABLE = (
    ("normal", True, None, "notable", True, False,
     "Default operation — balanced proactivity and voice."),
    ("party", False, 2, "critical", False, False,
     "Guests over — relax nagging, keep it light, only surface genuinely notable things."),
    ("lab", False, 1, "critical", False, True,
     "Focused workshop/lab work — minimal interruptions, dry tone, safety still fully active."),
    ("movie", False, 0, "critical", False, True,
     "Movie/theater mode — near-silent, only critical alerts, no convenience chatter."),
    ("guest", True, 1, "notable", False, False,
     "Houseguest staying — softer autonomy, plainer voice, avoid acting on unfamiliar patterns."),
    ("away", False, None, "notable", True, False,
     "Household away — proactive convenience off, security posture "
     "emphasized, only notable/critical events."),
    ("focus", False, 1, "critical", True, True,
     "Deep-focus/work — hold non-critical interrupts, terse tone."),
)


def _row(proactive, banter, scope, auto, quiet, text) -> dict:
    mode = {"description": text, "proactive": proactive, "announce_scope": scope,
            "auto_actions": auto, "quiet": quiet}
    if banter is not None:
        mode["banter"] = banter
    return mode


_BUILTIN_MODES: dict[str, dict] = {name: _row(*rest) for name, *rest in _MODE_TABLE}

_state: dict[str, Any] = {"mode": DEFAULT_MODE, "since": 0.0, "reason": ""}
_loaded = False


def _mode_state_path() -> str:
    return MODE_STATE_PATH or paths.nova_path("mode_state.json")


def _cfg(key: str, default=None):
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _all_modes() -> dict[str, dict]:
    """Built-in modes merged with the user's custom_modes setting."""
    modes = dict(_BUILTIN_MODES)
    custom = _cfg("custom_modes", {})
    if isinstance(custom, dict):
        for name, spec in custom.items():
            if not isinstance(spec, dict):
                continue
            key = str(name).strip().lower()
            if key:
                modes[key] = {**modes.get(key, {}), **spec}
    return modes


def _resolve(name: str) -> dict:
    """The full behaviour profile of a mode, with normal as the fallback."""
    modes = _all_modes()
    merged = dict(modes.get("normal", {}))
    merged.update(modes.get(name, {}))
    return {
        "proactive": bool(merged.get("proactive", True)),
        "banter": merged.get("banter", None),
        "announce_scope": merged.get("announce_scope", "notable"),
        "auto_actions": bool(merged.get("auto_actions", True)),
        "quiet": bool(merged.get("quiet", False)),
    }


def _load() -> None:
    """Restore the persisted mode, once per process."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _mode_state_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        mode = str(data.get("mode", DEFAULT_MODE)).lower()
        if mode in _all_modes():
            _state["mode"] = mode
            _state["since"] = float(data.get("since", time.time()))
            _state["reason"] = str(data.get("reason", "restored"))
            if mode != DEFAULT_MODE:
                _LOGGER.info("Operational mode restored: %s", mode)
    except Exception as exc:
        _LOGGER.warning("Could not restore operational mode: %s", exc)


def _persist() -> None:
    path = _mode_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f)
        os.replace(tmp, path)
    except Exception as exc:
        _LOGGER.debug("Could not persist operational mode: %s", exc)


def active_mode() -> str:
    _load()
    return _state.get("mode", DEFAULT_MODE)


def mode_info() -> dict:
    _load()
    name = active_mode()
    modes = _all_modes()
    spec = modes.get(name, modes["normal"])
    return {
        "active": name,
        "description": spec.get("description", ""),
        "since": _state.get("since", 0.0), "reason": _state.get("reason", ""),
        "overrides": _resolve(name),
        "available": [{"name": n, "description": modes[n].get("description", "")}
                      for n in sorted(modes)],
    }


def mode_overrides() -> dict:
    return _resolve(active_mode())


def mode_allows_proactive() -> bool:
    return mode_overrides()["proactive"]


def mode_allows_auto_actions() -> bool:
    return mode_overrides()["auto_actions"]


def mode_banter_level() -> Optional[int]:
    return mode_overrides()["banter"]


def set_mode(name: str, reason: str = "") -> dict:
    _load()
    key = str(name or "").strip().lower()
    modes = _all_modes()
    if key not in modes:
        return {"ok": False, "error": f"unknown mode '{name}'", "available": sorted(modes)}
    _state["mode"] = key
    _state["since"] = time.time()
    _state["reason"] = str(reason or "")
    _persist()
    _LOGGER.info("Operational mode set to %s (%s)", key, _state["reason"] or "no reason")
    return {"ok": True, "mode": key, "overrides": _resolve(key)}


def clear_mode(reason: str = "") -> dict:
    return set_mode("normal", reason or "cleared")


def auto_enabled() -> bool:
    value = _cfg("operational_mode_auto", True)
    return True if value is None else bool(value)


def auto_evaluate(occupied: bool) -> Optional[dict]:
    """Keep away/normal in step with occupancy; discretionary modes are left
    alone while someone is home."""
    try:
        if not auto_enabled():
            return None
        _load()
        current = _state.get("mode", "normal")
        if not occupied and current != "away":
            return set_mode("away", "auto: home empty")
        if occupied and current == "away":
            return set_mode("normal", "auto: someone home")
        return None
    except Exception as exc:
        _LOGGER.debug("Automatic mode evaluation failed: %s", exc)
        return None


def mode_scoped_to_areas() -> list:
    """Areas the active mode is confined to (lab and movie only)."""
    try:
        mode = active_mode()
        if mode == "lab":
            areas = _cfg("lab_areas", []) or []
            if isinstance(areas, str):
                try:
                    areas = json.loads(areas)
                except ValueError:
                    return []
            if isinstance(areas, list):
                return [str(a) for a in areas if a]
            return []
        if mode == "movie":
            area = _cfg("movie_area", "") or ""
            return [str(area)] if area else []
        return []
    except Exception:
        return []
