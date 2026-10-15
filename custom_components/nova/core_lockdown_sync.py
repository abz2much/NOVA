"""How lockdown is wired in: following the alarm, the one state listener,
the manager made on demand, manual requests and the panel's status.

cognitive_core.py re-exports every name here as the same object.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from homeassistant.core import HomeAssistant

from . import core_delivery as _m_delivery
from . import core_lockdown as _m_lockdown
from . import core_state as _m_state
from .core_common import ALARM_ARMED_STATES, LOCKDOWN_EXEMPT_LOCKS_DEFAULT
from .core_lockdown import LockdownManager  # for annotations; code uses core_lockdown.LockdownManager

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

# Readings that say nothing about the alarm: its integration is down or
# still starting, not the alarm off.
_ALARM_INDET_STATES = {"unavailable", "unknown", "none", ""}
_ALARM_INDET_LOG_TS = 0.0
_INDET_LOG_EVERY_S = 600
_EMPTY_STATUS = {"active": False, "since": 0.0, "reason": "", "auto": False,
                 "exempt_windows": 0}
_WATCHED_DOMAINS = ("binary_sensor", "cover", "lock")


def _core():
    return _m_state._CORE


def _lockdown_exempt_locks() -> set:
    """Locks no sweep may touch (a thermostat's child lock, say). Taken from
    the lockdown manager's own settings so the night sweep and the formal
    lockdown always agree."""
    mgr = _core().lockdown_mgr
    return mgr.exempt_locks if mgr is not None else LOCKDOWN_EXEMPT_LOCKS_DEFAULT


def is_lockdown() -> bool:
    """Whether a formal lockdown is on now."""
    mgr = _core().lockdown_mgr
    return bool(mgr and mgr.active)


def lockdown_status() -> dict:
    """The lockdown state for the panel."""
    mgr = _core().lockdown_mgr
    return mgr.status() if mgr else dict(_EMPTY_STATUS)


def _ensure_lockdown_mgr(hass: HomeAssistant = None) -> Optional["LockdownManager"]:
    """The lockdown manager, made here if it does not exist yet.

    Lockdown is security, so it cannot rely on the cognitive core having
    started cleanly: a start that failed part way (and was logged as non
    fatal) leaves the manager to be made the first time it is needed.
    """
    core = _core()
    if core.lockdown_mgr is not None:
        return core.lockdown_mgr
    owner = core.hass or hass
    if owner is None:
        return None
    try:
        core.lockdown_mgr = _m_lockdown.LockdownManager(owner, core.config or {})
    except Exception as exc:
        _LOGGER.error("Lockdown manager create failed: %s", exc)
        return None
    if core.hass is None:
        core.hass = owner
    _LOGGER.warning("Lockdown manager created on demand (core start had not initialised it)")
    return core.lockdown_mgr


async def _deliver(action, sleeping: bool = False, hass=None) -> None:
    core = _core()
    target = hass or core.hass
    if action and target:
        await _m_delivery._emit_action(target, core.config or {}, action, sleeping)


async def ensure_lockdown(hass: HomeAssistant, config: dict) -> None:
    """Set lockdown up on its own, apart from the observer and the cognitive
    loop: the manager, one listener for the alarm and for breaches, and the
    alarm's current state applied at once so a restart while armed comes
    back locked down. Safe to call more than once (setup and start both do).
    """
    core = _core()
    if core.hass is None:
        core.hass = hass
    if not core.config:
        core.config = config or {}
    _ensure_lockdown_mgr(hass)
    if core.alarm_unsub is None:
        core.alarm_unsub = hass.bus.async_listen("state_changed", _on_lockdown_state)
        _LOGGER.info("Lockdown listener registered (alarm sync + breach enforcement)")
    # An alarm that was already armed is taken on without a word: announcing
    # "lockdown engaged" on every restart or reload, with nothing changed,
    # repeated the notification. A fresh arm is announced from the listener.
    await _sync_lockdown_to_alarm("startup", announce=False)


def _real_arm(old_state) -> bool:
    """A change from a real state. An alarm entity appearing at startup
    (from nothing, unknown or unavailable) already armed is not an arming."""
    if not old_state:
        return False
    return str(getattr(old_state, "state", "")).lower() not in _ALARM_INDET_STATES


async def _on_lockdown_state(event) -> None:
    """The one listener lockdown needs: keep up with the alarm, and while a
    lockdown is on, secure any door, window, cover or lock that comes open."""
    try:
        data = event.data
        entity_id = data.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        if domain == "alarm_control_panel":
            await _sync_lockdown_to_alarm("alarm " + entity_id,
                                          announce=_real_arm(data.get("old_state")))
            return
        mgr = _core().lockdown_mgr
        if mgr is None or not mgr.active or domain not in _WATCHED_DOMAINS:
            return
        new = data.get("new_state")
        if new is None:
            return
        await _deliver(await mgr.handle_state_change(entity_id, data.get("old_state"), new))
    except Exception as exc:
        _LOGGER.debug("lockdown state handler error: %s", exc)


async def _sync_lockdown_to_alarm(reason: str, announce: bool = True) -> None:
    """Follow the alarm: arming engages lockdown, and a confirmed disarm lifts
    one the alarm engaged. Respects the automatic lockdown setting and a
    manual lift (which holds off re-engaging until the alarm disarms).
    ``announce=False`` takes on an armed alarm silently, as at startup.

    An alarm reading unavailable or unknown is neither armed nor disarmed:
    its cloud has dropped (a dropout once lifted an armed night lockdown as
    a disarm). Such a reading changes nothing, and is logged as a safety
    line at most every ten minutes. Only a real "disarmed" lifts.
    """
    core = _core()
    mgr = core.lockdown_mgr
    if mgr is None or core.hass is None:
        return
    from . import safety_config
    if not safety_config.automatic_lockdown_enabled(core.config or {}):
        return
    try:
        armed, disarm_confirmed, indeterminate = _alarm_state_view(core.hass)
    except Exception:
        return
    if indeterminate:
        _log_alarm_indeterminate(reason, lockdown_active=mgr.active)
        return   # no engage, no lift, and the manual lift hold stays
    if not armed and mgr._auto_suppressed:
        mgr._auto_suppressed = False
        await mgr._persist()
    if armed and not mgr.active and not mgr._auto_suppressed:
        action = await mgr.engage("alarm armed", auto=True, announce=announce)
    elif mgr.active and mgr.auto and disarm_confirmed:
        action = await mgr.disengage("alarm disarmed")
    else:
        return
    try:
        await _deliver(action)
    except Exception as exc:
        _LOGGER.debug("lockdown alarm-sync emit failed: %s", exc)


def _alarm_state_view(hass, config: Optional[dict] = None) -> tuple:
    """(armed, disarm_confirmed, indeterminate) for the selected alarm.

    Armed when any panel is armed; a confirmed disarm when none is armed and
    at least one says disarmed; otherwise (only unknown or unavailable, or no
    panel at all) indeterminate: the integration is down, not the alarm off.
    """
    from . import alarm_source
    chosen = (_core().config or {}) if config is None else config
    readings = {str(st.state).lower() for st in alarm_source.states(hass, chosen)}
    if readings & ALARM_ARMED_STATES:
        return True, False, False
    if "disarmed" in readings:
        return False, True, False
    return False, False, True


def _log_alarm_indeterminate(reason: str, lockdown_active: bool) -> None:
    """A safety line that the alarm cannot be read, at most every ten
    minutes: a cloud drop must show without filling the log."""
    global _ALARM_INDET_LOG_TS
    now = time.time()
    if now - _ALARM_INDET_LOG_TS < _INDET_LOG_EVERY_S:
        return
    _ALARM_INDET_LOG_TS = now
    held = "ACTIVE" if lockdown_active else "state"
    msg = (f"alarm panel unavailable/unknown ({reason}) — holding lockdown {held}; "
           "only a confirmed disarm lifts it")
    _LOGGER.warning("Lockdown: %s", msg)
    try:
        from .websocket import nova_log
        nova_log("SAFETY", f"Lockdown: {msg}")
    except Exception:
        pass


def _sleeping(hass) -> bool:
    try:
        from . import sleep_detection
        cfg = _core().config or {}
        asleep, _ = sleep_detection.is_sleeping(
            hass,
            bedroom_area_ids=cfg.get("bedroom_areas", []) or [],
            quiet_start=cfg.get("observer_quiet_start", "22:00"),
            quiet_end=cfg.get("observer_quiet_end", "07:00"),
        )
        return asleep
    except Exception:
        return False


async def request_lockdown(on: bool, reason: str = "requested", hass: HomeAssistant = None) -> bool:
    """Lockdown on or off by hand (service, voice or panel), announced. The
    manager is made if needed, so this works even when the cognitive core
    never set it up. False when there is no manager or hass to act with."""
    mgr = _ensure_lockdown_mgr(hass)
    owner = _core().hass or hass
    verb = "engage" if on else "lift"
    if mgr is None or owner is None:
        _LOGGER.warning("Lockdown %s request ignored — manager/hass unavailable", verb)
        return False
    if on:
        action = await mgr.engage(reason, auto=False)
    else:
        action = await mgr.disengage(reason, manual=True)
    _LOGGER.info("Lockdown %s requested (%s) → active=%s", verb, reason, mgr.active)
    if action:
        await _deliver(action, _sleeping(owner), owner)
    return True
