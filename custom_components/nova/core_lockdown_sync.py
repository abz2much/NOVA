"""Lockdown wiring: the alarm to lockdown sync, the state listener, the
on demand manager, the manual request entry point and the panel status.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
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
from .core_lockdown import LockdownManager  # annotations only; code reads core_lockdown.LockdownManager

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


def _lockdown_exempt_locks() -> set:
    """Locks the nighttime sweep must never touch (thermostat child locks,
    etc). Single source of truth is whatever LockdownManager loaded from
    config, so this and the formal lockdown sweep never drift apart."""
    if _m_state._CORE.lockdown_mgr is not None:
        return _m_state._CORE.lockdown_mgr.exempt_locks
    return LOCKDOWN_EXEMPT_LOCKS_DEFAULT


def is_lockdown() -> bool:
    """True if a formal lockdown is currently active."""
    return bool(_m_state._CORE.lockdown_mgr and _m_state._CORE.lockdown_mgr.active)


def lockdown_status() -> dict:
    """Lockdown state snapshot for the panel / observability."""
    if _m_state._CORE.lockdown_mgr:
        return _m_state._CORE.lockdown_mgr.status()
    return {"active": False, "since": 0.0, "reason": "", "auto": False, "exempt_windows": 0}


def _ensure_lockdown_mgr(hass: HomeAssistant = None) -> Optional["LockdownManager"]:
    """
    Return the lockdown manager, creating it on demand. Lockdown is a security
    feature, so it must not depend on the cognitive-core loop having started
    cleanly — if start() was interrupted (and swallowed as non-fatal), the
    manager is created here the first time it's needed.
    """
    if _m_state._CORE.lockdown_mgr is None:
        h = _m_state._CORE.hass or hass
        if h is None:
            return None
        try:
            _m_state._CORE.lockdown_mgr = _m_lockdown.LockdownManager(h, _m_state._CORE.config or {})
            if _m_state._CORE.hass is None:
                _m_state._CORE.hass = h
            _LOGGER.warning("Lockdown manager created on demand (core start had not initialised it)")
        except Exception as exc:
            _LOGGER.error("Lockdown manager create failed: %s", exc)
            return None
    return _m_state._CORE.lockdown_mgr


async def ensure_lockdown(hass: HomeAssistant, config: dict) -> None:
    """
    Wire lockdown up independently of the observer / cognitive loop: make sure
    the manager exists, register an event-driven alarm→lockdown sync, and apply
    the current alarm state immediately (so a reboot while the alarm is armed
    re-engages lockdown). Idempotent — safe to call from setup and from start().
    """
    if _m_state._CORE.hass is None:
        _m_state._CORE.hass = hass
    if not _m_state._CORE.config:
        _m_state._CORE.config = config or {}
    _ensure_lockdown_mgr(hass)
    if _m_state._CORE.alarm_unsub is None:
        _m_state._CORE.alarm_unsub = hass.bus.async_listen("state_changed", _on_lockdown_state)
        _LOGGER.info("Lockdown listener registered (alarm sync + breach enforcement)")
    # Startup: adopt the current alarm state silently. Re-announcing "lockdown
    # engaged" on every reboot/reload (when nothing actually changed) was the
    # source of the repeated notifications — a fresh arm is announced via the
    # event path below, not here.
    await _sync_lockdown_to_alarm("startup", announce=False)


async def _on_lockdown_state(event) -> None:
    """One listener for everything lockdown cares about: alarm arm/disarm sync,
    plus securing doors/windows/locks that go unsecure while lockdown is active."""
    try:
        eid = event.data.get("entity_id", "")
        dom = eid.split(".", 1)[0]
        if dom == "alarm_control_panel":
            old = event.data.get("old_state")
            # A real arm is disarmed→armed. Entity initialisation on startup
            # (None / unknown / unavailable → armed) is NOT a fresh arm — adopt
            # it silently so reboots don't re-announce.
            genuine = bool(old) and str(getattr(old, "state", "")).lower() not in (
                "unknown", "unavailable", "none", "")
            await _sync_lockdown_to_alarm("alarm " + eid, announce=genuine)
            return
        mgr = _m_state._CORE.lockdown_mgr
        if mgr is None or not mgr.active or dom not in ("binary_sensor", "cover", "lock"):
            return
        new = event.data.get("new_state")
        if new is None:
            return
        action = await mgr.handle_state_change(eid, event.data.get("old_state"), new)
        if action and _m_state._CORE.hass:
            await _m_delivery._emit_action(_m_state._CORE.hass, _m_state._CORE.config or {}, action, False)
    except Exception as exc:
        _LOGGER.debug("lockdown state handler error: %s", exc)


async def _sync_lockdown_to_alarm(reason: str, announce: bool = True) -> None:
    """
    Engage lockdown when any alarm is armed, lift it (if it was the alarm that
    engaged it) when all alarms report a CONFIRMED disarm. Honours
    lockdown_auto_on_arm and the manual-exit suppression. Event-driven, so it
    does not depend on the loop. `announce=False` adopts an already-armed
    state silently (startup / reboot).

    v6.47.2: `unavailable`/`unknown` is neither armed nor disarmed — it's the
    alarm integration losing its cloud (Cove/Alula drops were LIFTING an
    armed-night lockdown as "alarm disarmed"). Indeterminate state now HOLDS
    the current lockdown, with a throttled SAFETY log so the dropout is
    visible. Only an actual 'disarmed' report lifts an auto-engaged lockdown.
    """
    mgr = _m_state._CORE.lockdown_mgr
    if mgr is None or _m_state._CORE.hass is None:
        return
    from . import safety_config
    if not safety_config.automatic_lockdown_enabled(_m_state._CORE.config or {}):
        return
    try:
        armed, disarm_confirmed, indeterminate = _alarm_state_view(_m_state._CORE.hass)
    except Exception:
        return
    if indeterminate:
        _log_alarm_indeterminate(reason, lockdown_active=mgr.active)
        return  # hold everything — no engage, no lift, no suppression reset
    if not armed and mgr._auto_suppressed:
        mgr._auto_suppressed = False
        await mgr._persist()
    action = None
    if armed and not mgr.active and not mgr._auto_suppressed:
        action = await mgr.engage("alarm armed", auto=True, announce=announce)
    elif mgr.active and mgr.auto and disarm_confirmed:
        action = await mgr.disengage("alarm disarmed")
    if action and _m_state._CORE.hass:
        try:
            await _m_delivery._emit_action(_m_state._CORE.hass, _m_state._CORE.config or {}, action, False)
        except Exception as exc:
            _LOGGER.debug("lockdown alarm-sync emit failed: %s", exc)


_ALARM_INDET_STATES = {"unavailable", "unknown", "none", ""}
_ALARM_INDET_LOG_TS = 0.0


def _alarm_state_view(hass, config: Optional[dict] = None) -> tuple:
    """(armed, disarm_confirmed, indeterminate) across all alarm panels.
    armed: any panel in an armed state. disarm_confirmed: no panel armed AND
    at least one affirmatively reports 'disarmed'. indeterminate: no panel
    armed and none disarmed either (all unavailable/unknown, or no panels) —
    the integration is down, not the alarm off."""
    armed = False
    disarmed = False
    from . import alarm_source
    selected_config = (_m_state._CORE.config or {}) if config is None else config
    for st in alarm_source.states(hass, selected_config):
        s = str(st.state).lower()
        if s in ALARM_ARMED_STATES:
            armed = True
        elif s == "disarmed":
            disarmed = True
    if armed:
        return True, False, False
    if disarmed:
        return False, True, False
    return False, False, True


def _log_alarm_indeterminate(reason: str, lockdown_active: bool) -> None:
    """SAFETY-log the alarm integration being unreadable, at most once per
    10 minutes — a Cove/Alula cloud drop shouldn't spam, but must be seen."""
    global _ALARM_INDET_LOG_TS
    now = time.time()
    if now - _ALARM_INDET_LOG_TS < 600:
        return
    _ALARM_INDET_LOG_TS = now
    msg = ("alarm panel unavailable/unknown (%s) — holding lockdown %s; "
           "only a confirmed disarm lifts it" %
           (reason, "ACTIVE" if lockdown_active else "state"))
    _LOGGER.warning("Lockdown: %s", msg)
    try:
        from .websocket import nova_log
        nova_log("SAFETY", f"Lockdown: {msg}")
    except Exception:
        pass


async def request_lockdown(on: bool, reason: str = "requested", hass: HomeAssistant = None) -> bool:
    """
    Manual lockdown entry point (service / voice / panel). Engages or lifts the
    lockdown and announces the result. Creates the manager on demand if needed,
    so it works even if the cognitive core didn't initialise it. Returns True if
    the request was handled.
    """
    mgr = _ensure_lockdown_mgr(hass)
    h = _m_state._CORE.hass or hass
    if mgr is None or h is None:
        _LOGGER.warning("Lockdown %s request ignored — manager/hass unavailable",
                        "engage" if on else "lift")
        return False
    action = await (mgr.engage(reason, auto=False) if on else mgr.disengage(reason, manual=True))
    _LOGGER.info("Lockdown %s requested (%s) → active=%s",
                 "engage" if on else "lift", reason, mgr.active)
    if action:
        try:
            from . import sleep_detection
            cfg = _m_state._CORE.config or {}
            sleeping, _ = sleep_detection.is_sleeping(
                h,
                bedroom_area_ids=cfg.get("bedroom_areas", []) or [],
                quiet_start=cfg.get("observer_quiet_start", "22:00"),
                quiet_end=cfg.get("observer_quiet_end", "07:00"),
            )
        except Exception:
            sleeping = False
        await _m_delivery._emit_action(h, _m_state._CORE.config or {}, action, sleeping)
    return True
