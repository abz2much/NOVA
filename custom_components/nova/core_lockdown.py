"""The formal lockdown: its announcement and LockdownManager.

LockdownManager engages and lifts the lockdown, enforces it when something
is opened or unlocked, and checks in the background that what Nova tried to
secure really is. cognitive_core.py exports every name defined here as the
same object.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from homeassistant.core import HomeAssistant

from . import core_common as _m_common
from .core_bridge import _alarm_state_view, _emit_action, _lockdown_state_path
from .core_common import (
    ALARM_ARMED_STATES,
    LOCKDOWN_EXEMPT_LOCKS_DEFAULT,
    LOCKDOWN_SECURE_VERIFY_DELAY,
    _notify_i18n,
    _persona,
)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

# Readings that say nothing about whether something is open or shut.
_UNREADABLE = {"unknown", "unavailable", "none", ""}

# Lock readings _lock_all already deals with: locked (nothing to do), locking
# (on its way) and unlocked (it sends the command). Any other reading, such
# as jammed, open or offline, leaves the lock not secured.
_LOCK_STATES_HANDLED = {"locked", "locking", "unlocked"}


def _is_unreadable(state) -> bool:
    return str(state).lower() in _UNREADABLE


def build_lockdown_message(honorific: str, locked: list, closed: list,
                           open_names: list, lang: str = "en",
                           failed: Optional[list] = None,
                           unreadable: Optional[list] = None) -> str:
    """The "lockdown engaged" announcement. Pure, so it is unit tested.

    Each outcome is reported apart:

    * ``locked``: locks a lock command was sent to;
    * ``closed``: closeable openings (garage doors, motorised covers) a
      close command was sent to;
    * ``open_names``: openings Nova cannot secure remotely (bare window
      contacts), framed as the gap to close by hand, never as a footnote;
    * ``failed``: locks that are not secured (the command failed, or the
      lock reads jammed, open or offline), never counted as done;
    * ``unreadable``: doors and windows that cannot be read, which are
      never taken as closed.

    Locked and closed are only what was sent: their background checks have
    not run when this is composed, so the wording promises the follow up
    alert rather than claiming the home is secure. "Already fully secured"
    is said only when nothing was done and nothing is open, failed or
    unreadable.

    Every part is localised (verb phrases, the list join and the wrappers);
    device and area names pass through as they are.
    """
    i18n = _notify_i18n()

    def names(items) -> str:
        return i18n.join_names(list(items), lang)

    def say(key: str, **values) -> str:
        return i18n.message(key, lang, **values)

    # An empty honorific means nobody in particular is home; i18n.message
    # then capitalises the sentence instead of prefixing it, so it is not
    # replaced with "sir" here.
    h = _persona().capitalise(honorific)

    actions = []
    if locked:
        actions.append(say("lockdown_lock_pending", names=names(locked)))
    if closed:
        actions.append(say("lockdown_close_pending", names=names(closed)))
    did = names(actions)   # "lock X and close Y", localised

    # Everything wrong, as one clause: failed locks, then what Nova cannot
    # close, then what cannot be read.
    problems = []
    if failed:
        problems.append(say("lockdown_secure_failed", names=names(failed)))
    if open_names:
        if len(open_names) == 1:
            problems.append(say("lockdown_gap_one", names=names(open_names)))
        elif len(open_names) <= 3:
            problems.append(say("lockdown_gap_few", names=names(open_names)))
        else:
            problems.append(say("lockdown_gap_many", count=len(open_names)))
    if unreadable:
        problems.append(say("lockdown_unreadable", names=names(unreadable)))
    problem = "; ".join(problems)

    if did:
        if problem:
            return say("lockdown_did_gap_pending", honorific=h, did=did, gap=problem)
        return say("lockdown_did_pending", honorific=h, did=did)
    if failed:
        return say("lockdown_failed_only", honorific=h, gap=problem)
    if problem:
        return say("lockdown_gap_only", honorific=h, gap=problem)
    return say("lockdown_already_secured", honorific=h)


# Shared with the nighttime sweep (core_safety.py), which chooses locks and
# sends commands the same way. Which covers each one closes stays separate:
# the sweep closes every open cover, the lockdown only door, garage, window
# and gate covers.

def exempt_lock_set(value) -> set:
    """The saved list of exempt locks as a set of lock ids. The panel saves
    a JSON string, the v8 migration a list. Anything unreadable gives an
    empty set, so every lock is locked rather than fewer."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text) if text else []
        except ValueError:
            _LOGGER.warning("lockdown_exempt_locks is not a JSON list; exempting nothing")
            return set()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {item for item in value if isinstance(item, str) and item.startswith("lock.")}


def unlocked_locks(hass, exempt) -> list:
    """Lock states reading unlocked, other than the exempt ones."""
    return [state for state in hass.states.async_all("lock")
            if state.entity_id not in exempt and state.state == "unlocked"]


class NotSecured(PermissionError):
    """The live check or the policy stopped a lock or close. ``code`` is
    the reason for the Actions log, ``note`` what Nova reports."""

    def __init__(self, note: str, code: str = "not_authorized"):
        super().__init__(note)
        self.note = note
        self.code = code


async def secure_device(hass, entity_id: str, domain: str) -> str:
    """Lock a lock or close a cover, waiting for the call.

    It passes the one authority check as Nova acting on its own; locking and
    closing are low risk, so they are allowed. The device is read live first:
    "already_secure" when it already reads locked or closed (nothing sent),
    otherwise "sent". NotSecured when it is skipped because it cannot be read
    or its door is open. A failing call raises, for the caller to log and
    audit.
    """
    from . import policy
    is_lock = domain == "lock"
    request = policy.AuthorityRequest(
        "lock" if is_lock else "cover", "lock" if is_lock else "close_cover",
        entity_id, source=policy.SOURCE_AUTOMATIC)
    decision = policy.authorize_now(hass, request)
    if decision.already_secure:
        return "already_secure"
    if not decision.allowed:
        raise NotSecured(decision.note or "not authorized", decision.code or "not_authorized")
    if is_lock:
        await hass.services.async_call("lock", "lock", {"entity_id": entity_id},
                                       blocking=True)
    else:
        await hass.services.async_call("cover", "close_cover", {"entity_id": entity_id},
                                       blocking=True)
    return "sent"


class LockdownManager:
    """Formal lockdown, engaged when the alarm arms or on request.

    Engaging locks every lock, closes every open door or garage cover, and
    notes the windows already open so they are left alone (knowingly left
    open). While active, a reopened door or unlocked lock is a breach and is
    secured again; a newly opened window is flagged, since a contact sensor
    cannot be closed. Disarming lifts it only when the alarm engaged it; a
    lockdown someone asked for stays until it is lifted.
    """

    # Which doors, windows and covers count.
    _DOOR_WINDOW_BS = ("door", "window", "garage_door", "opening")
    _CLOSEABLE_COVERS = {"door", "garage", "garage_door", "window", "gate"}

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass, self.config = hass, config
        self.active, self.since, self.reason = False, 0.0, ""
        self.auto = False   # engaged by the alarm, so lifted on disarm
        # Doors and windows open at engage, or later taken as left open on
        # purpose; never a lock.
        self.exempt_windows: set = set()
        # Locks lockdown never touches (thermostat child locks and so on).
        # A missing setting means the default; an explicit [] means exempt
        # nothing, which an "or" would wrongly discard.
        saved = config.get("lockdown_exempt_locks", None)
        if saved is None:
            self.exempt_locks: set = set(LOCKDOWN_EXEMPT_LOCKS_DEFAULT)
        else:
            self.exempt_locks = exempt_lock_set(saved)
        self._secured_by_us: set = set()   # secured by Nova in this lockdown
        self._alerted: set = set()         # already alerted about in this lockdown
        self._silent: set = set()          # stopped reporting, already alerted
        self._last_breach_alert = 0.0
        self._automatic_generation = 0
        # (entity_id, friendly name) of each lock the last _lock_all() could
        # not lock.
        self._lock_failures: list = []
        # Set when someone lifts lockdown while the alarm is still armed, so
        # it does not re-engage until the alarm is disarmed and armed again.
        self._auto_suppressed = False
        # Restored so a restart or reload keeps the left open windows and a
        # manual exit.
        self._load_state()

    # Saved state

    def _clear_engaged_state(self) -> None:
        self.active = False
        self.since = 0.0
        self.reason = ""
        self.auto = False
        self.exempt_windows = set()
        self._auto_suppressed = False

    def _load_state(self) -> None:
        try:
            path = _lockdown_state_path()
            if not Path(path).exists():
                return
            with open(path) as handle:
                saved = json.load(handle)
            self._auto_suppressed = bool(saved.get("auto_suppressed", False))
            if not saved.get("active"):
                return
            self.active = True
            self.since = saved.get("since", time.time())
            self.reason = saved.get("reason", "restored")
            self.auto = bool(saved.get("auto", False))
            # A lock is never exempt; files from older versions could list one.
            self.exempt_windows = {e for e in saved.get("exempt_windows", [])
                                   if not str(e).startswith("lock.")}
            _LOGGER.warning(
                "Lockdown state RESTORED (auto=%s, %d exempt windows)",
                self.auto, len(self.exempt_windows))
            # Before automatic lockdown was opt in, an alarm owned lockdown
            # could be saved without the user ever enabling it. Only Nova's
            # own state is cleared: disengage() is not called because it
            # speaks, and no unlock or disarm is ever sent.
            from . import safety_config
            if self.auto and not safety_config.automatic_lockdown_enabled(self.config):
                self._clear_engaged_state()
                self._persist_sync()
                _LOGGER.warning("Cleared legacy automatic lockdown state; devices unchanged")
        except Exception as exc:
            _LOGGER.warning("Lockdown state restore failed: %s", exc)

    def _persist_sync(self) -> None:
        snapshot = {"active": self.active, "since": self.since, "reason": self.reason,
                    "auto": self.auto, "exempt_windows": sorted(self.exempt_windows),
                    "auto_suppressed": self._auto_suppressed}
        try:
            _m_common.write_json_atomic(_lockdown_state_path(), snapshot)
        except Exception as exc:
            _LOGGER.debug("Lockdown state persist failed: %s", exc)

    async def _persist(self) -> None:
        with contextlib.suppress(Exception):
            await self.hass.async_add_executor_job(self._persist_sync)

    def status(self) -> dict:
        return {"active": self.active, "since": self.since, "reason": self.reason,
                "auto": self.auto, "exempt_windows": len(self.exempt_windows)}

    def _alarm_armed(self) -> bool:
        from . import alarm_source
        return any(state.state in ALARM_ARMED_STATES
                   for state in alarm_source.states(self.hass, self.config))

    # What counts as open, secure and securable

    def _openings(self):
        """(state, is a sensor) for every door or window sensor and every
        closeable cover. A sensor counts only when it is a way into the house
        (household.is_way_in), so a fridge door or a shed is never named;
        covers keep their own device class rule."""
        from . import household
        for state in self.hass.states.async_all("binary_sensor"):
            if (state.attributes.get("device_class") in self._DOOR_WINDOW_BS
                    and household.is_way_in(self.hass, state)):
                yield state, True
        for state in self.hass.states.async_all("cover"):
            if state.attributes.get("device_class") in self._CLOSEABLE_COVERS:
                yield state, False

    def _open_openings(self) -> set:
        """Every door and window open right now, sensors and covers."""
        return {state.entity_id for state, sensor in self._openings()
                if (state.state == "on" if sensor else state.state in ("open", "opening"))}

    def _unreadable_openings(self) -> list:
        """Friendly names, sorted, of the doors, windows and closeable covers
        reading unknown or unavailable: Nova cannot tell they are shut."""
        return sorted(self._friendly(state.entity_id) for state, _sensor in self._openings()
                      if _is_unreadable(state.state))

    def _unsecured_locks(self) -> list:
        """(entity_id, friendly name) of each lock, exempt ones aside, whose
        reading leaves it not secured and that _lock_all cannot act on:
        jammed, open, unknown, unavailable and the like."""
        return [(state.entity_id, state.attributes.get("friendly_name", state.entity_id))
                for state in self.hass.states.async_all("lock")
                if state.entity_id not in self.exempt_locks
                and str(state.state).lower() not in _LOCK_STATES_HANDLED]

    def _is_relevant(self, dom: str, dc, eid: str = None) -> bool:
        if dom == "lock":
            return eid not in self.exempt_locks
        if dom == "cover":
            return dc in self._CLOSEABLE_COVERS
        return dom == "binary_sensor" and dc in self._DOOR_WINDOW_BS

    def _is_secure(self, dom: str, state) -> bool:
        reading = str(state).lower()
        secure = {"lock": ("locked",), "cover": ("closed", "closing"),
                  # an "off" contact sensor is shut
                  "binary_sensor": ("off", "closed", "false")}
        return reading in secure[dom] if dom in secure else True

    def _can_secure(self, dom: str, dc) -> bool:
        """Whether Nova can lock or close it; a bare contact sensor cannot."""
        return dom == "lock" or (dom == "cover" and dc in self._CLOSEABLE_COVERS)

    async def _secure_entity(self, eid: str, dom: str) -> bool:
        """True when the command was sent or it already reads secure."""
        try:
            await secure_device(self.hass, eid, dom)
        except NotSecured as exc:
            _LOGGER.warning("Lockdown: %s skipped: %s", eid, exc.note)
            await self._record("not secured", exc.note, entity_id=eid, assessment=exc.code)
            return False
        except Exception as exc:
            _LOGGER.warning("Lockdown: secure %s failed: %s", eid, exc)
            return False
        return True

    async def _record(self, decision: str, reason: str, *, entity_id=None,
                      assessment: str = "", request_ids=None, **facts) -> None:
        """One lockdown decision in the Decision Record, in the shared safety
        format (alert_path). Never raises."""
        from . import alert_path
        await alert_path.async_record_decision(
            self.hass, "lockdown", source="lockdown", entity_id=entity_id,
            sit=alert_path.situation(self.hass, self.config),
            facts=dict(facts, auto=self.auto), assessment=assessment,
            decision=decision, reason=reason,
            ref=f"lockdown:{int(self.since)}" if self.since else None,
            request_ids=request_ids)

    def _engage_ids(self) -> Optional[list]:
        request_id = getattr(self, "_engage_request_id", None)
        return [request_id] if request_id else None

    def _friendly(self, eid: str) -> str:
        """The entity's friendly name, or its id."""
        state = self.hass.states.get(eid)
        name = state.attributes.get("friendly_name") if state else None
        return name or eid

    def set_automatic_lockdown(self, enabled: bool) -> None:
        self.config["lockdown_auto_on_arm"] = enabled is True
        self._automatic_generation += 1

    def _automatic_operation_current(self, generation: Optional[int]) -> bool:
        """An automatic run stops as soon as the setting changes or goes off."""
        if generation is None:
            return True
        from . import safety_config
        if generation != self._automatic_generation:
            return False
        return safety_config.automatic_lockdown_enabled(self.config)

    # Engaging

    async def _log_rows(self, request_id, targets: list) -> dict:
        """One Actions log batch for this lockdown request: every target is
        part of the one action, not an action of its own."""
        from . import action_log
        return await self.hass.async_add_executor_job(
            lambda: action_log.start_many(request_id, "lockdown_engage", "safety", targets))

    async def _set_row(self, row_id, outcome: str, reason_code=None) -> None:
        if row_id is None:
            return
        from . import action_log
        if reason_code is None:
            await self.hass.async_add_executor_job(
                lambda: action_log.set_execution(row_id, outcome))
        else:
            await self.hass.async_add_executor_job(
                lambda: action_log.set_execution(row_id, outcome, reason_code=reason_code))

    async def _lock_all(self, request_id: Optional[str] = None,
                        automatic_generation: Optional[int] = None) -> list:
        """Lock every unlocked lock. Returns (entity_id, friendly name) for
        each one the command was sent to, so engage() can check each later.
        Locks that could not be locked go to self._lock_failures.

        With a ``request_id`` (engage's own), every lock is logged as a
        target of that one lockdown request.
        """
        candidates = unlocked_locks(self.hass, self.exempt_locks)
        rows: dict = {}
        if request_id and candidates:
            rows = await self._log_rows(request_id, [
                {"key": st.entity_id, "domain": "lock", "service": "lock",
                 "entity_id": st.entity_id} for st in candidates])
        sent = []
        self._lock_failures = []
        for state in candidates:
            if not self._automatic_operation_current(automatic_generation):
                break
            eid = state.entity_id
            name = state.attributes.get("friendly_name", eid)
            row_id = rows.get(eid)
            try:
                result = await secure_device(self.hass, eid, "lock")
            except Exception as exc:
                # The live check skipped it (door open, or unreadable) or the
                # command failed: either way it is named as not locked.
                skipped = isinstance(exc, NotSecured)
                _LOGGER.warning("Lockdown: %s %s: %s",
                                "skipped" if skipped else "failed to lock", eid, exc)
                self._lock_failures.append((eid, name))
                if skipped:
                    await self._record("not locked", exc.note, entity_id=eid,
                                       assessment=exc.code)
                    await self._set_row(row_id, "blocked", exc.code)
                else:
                    await self._set_row(row_id, "failed", "service_call_failed")
                continue
            if not self._automatic_operation_current(automatic_generation):
                break
            if result == "already_secure":
                # Locked since the list was made; nothing was sent.
                await self._set_row(row_id, "blocked", "already_secure")
                continue
            sent.append((eid, name))
            _LOGGER.info("Lockdown: locked %s", eid)
            await self._set_row(row_id, "accepted")
        return sent

    def _check_later(self, eid: str, dom: str, name: str) -> None:
        self.hass.async_create_task(self._verify_secured(eid, dom, name))

    async def _close_openings(self, request_id, generation) -> Optional[tuple]:
        """Close every open closeable opening (garage doors, motorised
        covers). Their own safety sensors make an obstruction fail the close,
        which the background check then reports. Bare contacts cannot be
        closed. Returns (closed names, ids left open), or None when an
        automatic run was cancelled part way."""
        closed: list = []
        left_open: set = set()
        to_close: list = []
        for eid in self._open_openings():
            dom = eid.split(".", 1)[0]
            state = self.hass.states.get(eid)
            dc = state.attributes.get("device_class") if state else None
            if self._can_secure(dom, dc):
                to_close.append((eid, dom))
            else:
                left_open.add(eid)
        rows: dict = {}
        if to_close:
            rows = await self._log_rows(request_id, [
                {"key": eid, "domain": dom, "service": "close_cover", "entity_id": eid}
                for eid, dom in to_close])
        for eid, dom in to_close:
            if not self._automatic_operation_current(generation):
                return None
            name = self._friendly(eid)
            if not await self._secure_entity(eid, dom):
                left_open.add(eid)   # the close itself failed
                await self._set_row(rows.get(eid), "failed", "service_call_failed")
                continue
            if not self._automatic_operation_current(generation):
                return None
            closed.append(name)
            self._secured_by_us.add(eid)
            self._check_later(eid, dom, name)
            await self._set_row(rows.get(eid), "accepted")
        return closed, left_open

    async def engage(self, reason: str, auto: bool = False,
                     announce: bool = True) -> Optional[dict]:
        if self.active:
            return None
        generation = self._automatic_generation if auto else None
        if not self._automatic_operation_current(generation):
            return None
        self.active, self.since, self.reason, self.auto = True, time.time(), reason, auto
        _state_changed()
        self._secured_by_us, self._alerted, self._silent = set(), set(), set()
        self._last_breach_alert = 0.0
        honorific = _m_common._live_honorific(self.hass)

        # One request id for the whole lockdown: every lock and close below
        # is a target of it, and later checks link back to it.
        from . import action_log
        request_id = action_log.new_request_id()
        self._engage_request_id = request_id

        locked_pairs = await self._lock_all(request_id=request_id,
                                            automatic_generation=generation)
        if not self._automatic_operation_current(generation):
            return None
        for eid, name in locked_pairs:
            self._check_later(eid, "lock", name)
        # A lock whose command failed is named as not secured and checked
        # like the rest, so one still unlocked raises the critical alert.
        failed_pairs, self._lock_failures = list(self._lock_failures), []
        for eid, name in failed_pairs:
            self._check_later(eid, "lock", name)
        # A lock that is jammed, open or offline is named as not secured
        # too; nothing was sent to it, so there is nothing to check later.
        known = {eid for eid, _name in failed_pairs}
        failed_pairs += [pair for pair in self._unsecured_locks() if pair[0] not in known]
        locked = [name for _eid, name in locked_pairs]
        failed_locks = [name for _eid, name in failed_pairs]

        outcome = await self._close_openings(request_id, generation)
        if outcome is None:
            return None
        closed, left_open = outcome
        # What cannot be secured is left and taken as intentional: named once
        # here, with no nagging later.
        self.exempt_windows = left_open
        open_names = sorted(self._friendly(eid) for eid in left_open)

        if not self._automatic_operation_current(generation):
            return None
        unreadable = self._unreadable_openings()
        message = build_lockdown_message(honorific, locked, closed, open_names,
                                         lang=_m_common._hass_lang(self.hass),
                                         failed=failed_locks, unreadable=unreadable)
        _LOGGER.warning(
            "Lockdown ENGAGED (%s): locked=%s closed=%s left-open=%d "
            "lock-failed=%s announce=%s",
            reason, locked, closed, len(left_open), failed_locks, announce)
        await self._persist()
        await self._record(
            "lockdown engaged", reason, assessment="secure the house",
            locked=locked, closed=closed, left_open=open_names,
            failed=failed_locks, unreadable=unreadable, announced=announce,
            request_ids=[request_id])
        if not announce:
            return None
        return {"type": "lockdown_engaged", "urgency": "high",
                "message": message, "auto_act": True}

    async def disengage(self, reason: str, manual: bool = False) -> Optional[dict]:
        if not self.active:
            return None
        # Lifted by hand while the alarm is still armed: stay out until the
        # alarm is disarmed and armed again.
        if manual and self._alarm_armed():
            self._auto_suppressed = True
        self.active, self.reason, self.auto = False, "", False
        _state_changed()
        self.exempt_windows = set()
        self._secured_by_us, self._alerted, self._silent = set(), set(), set()
        honorific = _persona().capitalise(_m_common._live_honorific(self.hass))
        _LOGGER.warning("Lockdown DISENGAGED (%s, manual=%s, auto_suppressed=%s)",
                        reason, manual, self._auto_suppressed)
        await self._persist()
        await self._record("lockdown lifted", reason, manual=manual,
                           auto_suppressed=self._auto_suppressed)
        text = _notify_i18n().message("lockdown_lifted", _m_common._hass_lang(self.hass),
                                      honorific=honorific)
        return {"type": "lockdown_disengaged", "urgency": "low",
                "message": text, "auto_act": True}

    # Enforcing

    def _breach(self, urgency: str, honorific, text: str) -> dict:
        return {"type": "lockdown_breach", "urgency": urgency, "auto_act": True,
                "message": _persona().lead_in(honorific, text)}

    async def _stopped_reporting(self, eid: str, name: str) -> Optional[dict]:
        """A watched device went unknown or unavailable. That is not an
        opening: it is recorded and alerted once, and it is never taken as
        left open on purpose, so a later reading is judged as usual."""
        if eid in self._silent:
            return None
        self._silent.add(eid)
        await self._record("could not check", "stopped reporting during lockdown",
                           entity_id=eid, assessment="stopped reporting")
        honorific = _m_common._live_honorific(self.hass)
        return self._breach("high", honorific,
                            f"{name} stopped reporting during lockdown, so I can't "
                            f"tell if it's secure.")

    async def handle_state_change(self, eid: str, old, new) -> Optional[dict]:
        """A door, window or lock changed while lockdown is active.

        * Anything open at engage (or since taken as intentional) is ignored.
        * A reading going unknown or unavailable is its own case: one alert
          that it stopped reporting.
        * Otherwise only a change from secure (or from no reading) to not
          secure counts.
        * What Nova can close or lock is secured again, with an alert if that
          does not work.
        * A bare contact sensor cannot be closed, so its opening is taken as
          intentional and left alone.
        * Something Nova closed being opened again means the person wants it
          open: adopted and said once. A lock unlocked again after Nova
          locked it is a critical alert instead.

        Returns an alert to announce, or None.
        """
        if not self.active or eid in self.exempt_windows:
            return None
        dom = eid.split(".", 1)[0]
        dc = new.attributes.get("device_class")
        if not self._is_relevant(dom, dc, eid):
            return None
        name = new.attributes.get("friendly_name", eid)
        if _is_unreadable(new.state):
            return await self._stopped_reporting(eid, name)
        self._silent.discard(eid)
        if self._is_secure(dom, new.state):
            if dom == "lock":
                self._alerted.discard(eid)   # locked again, so a new unlock alerts
            return None
        if (old is not None and not self._is_secure(dom, old.state)
                and not _is_unreadable(old.state)):
            return None   # it was already not secure: not a new opening
        honorific = _m_common._live_honorific(self.hass)

        if not self._can_secure(dom, dc):
            self.exempt_windows.add(eid)
            await self._persist()
            _LOGGER.info("Lockdown: %s opened (not controllable) — treating as intentional", eid)
            await self._record("left open as intentional", "Nova cannot close it",
                               entity_id=eid, assessment="opened during lockdown")
            return None

        if eid in self._secured_by_us:
            return await self._reopened(eid, dom, name, honorific)
        return await self._secure_again(eid, dom, name, honorific)

    async def _reopened(self, eid: str, dom: str, name: str, honorific) -> Optional[dict]:
        if dom == "lock":
            # Never adopted, never left out and never locked a second time
            # (Nova does not fight someone at the door): a critical alert,
            # once until it is locked again.
            if eid in self._alerted:
                return None
            self._alerted.add(eid)
            await self._record("critical alert, not locked again",
                               "unlocked again after Nova locked it",
                               entity_id=eid, assessment="unlocked during lockdown")
            return self._breach("critical", honorific,
                                f"{name} was unlocked again during lockdown after I locked it. "
                                f"Please check it.")
        # Shut once by Nova and open again: the person wants it open.
        self._secured_by_us.discard(eid)
        self.exempt_windows.add(eid)
        await self._persist()
        await self._record("left open as intentional", "reopened after Nova secured it",
                           entity_id=eid, assessment="reopened during lockdown")
        if eid in self._alerted:
            return None
        self._alerted.add(eid)
        return self._breach("high", honorific,
                            f"{name} reopened after I secured it — I'll leave it open.")

    async def _secure_again(self, eid: str, dom: str, name: str, honorific) -> Optional[dict]:
        _LOGGER.warning("Lockdown: securing %s after it opened", eid)
        from . import action_log
        request_id = action_log.new_request_id()
        service = "lock" if dom == "lock" else "close_cover"
        action_id = await self.hass.async_add_executor_job(
            lambda: action_log.start(request_id, "lockdown_breach_resecure", "safety",
                                     domain=dom, service=service, entity_id=eid))
        ok = await self._secure_entity(eid, dom)
        await self.hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "accepted" if ok else "failed",
                reason_code=None if ok else "service_call_failed"))
        await self._record("secured again" if ok else "could not secure",
                           "opened during lockdown", entity_id=eid,
                           assessment="opened during lockdown", request_ids=[request_id])
        if ok:
            self._secured_by_us.add(eid)
            # Slow covers report late, so check it really shut.
            self._check_later(eid, dom, name)
            return None
        if eid in self._alerted:
            return None
        self._alerted.add(eid)
        return self._breach("critical", honorific,
                            f"{name} opened during lockdown and I couldn't secure it.")

    async def _verify_secured(self, eid: str, dom: str, name: str) -> None:
        """After a lock or close, wait, then make sure it took."""
        try:
            await asyncio.sleep(LOCKDOWN_SECURE_VERIFY_DELAY)
            if not self.active or eid in self.exempt_windows:
                return
            state = self.hass.states.get(eid)
            if state is None:
                # No reading means unverified, never secured. Recorded only:
                # a missing reading raises no alarm.
                await self._record("could not check", "no reading after the command",
                                   entity_id=eid, assessment="unverified",
                                   request_ids=self._engage_ids())
                return
            if self._is_secure(dom, state.state):
                await self._record("checked, secured", "read secure after the command",
                                   entity_id=eid, assessment="verified",
                                   request_ids=self._engage_ids())
                return
            if eid in self._alerted:
                return
            self._alerted.add(eid)
            await self._record("critical alert", "still not secure after Nova tried",
                               entity_id=eid, assessment="secure check failed",
                               request_ids=self._engage_ids())
            still = "unlocked" if dom == "lock" else "open"
            alert = self._breach(
                "critical", _m_common._live_honorific(self.hass),
                f"I tried to secure {name} during lockdown but it's still {still}.")
            await _emit_action(self.hass, self.config, alert, False)
        except Exception as exc:
            _LOGGER.debug("lockdown secure-verify error: %s", exc)

    async def tick(self) -> list[dict]:
        """Engage or lift with the alarm. Breaches are handled as they happen
        (handle_state_change), so not here."""
        from . import safety_config
        armed, disarm_confirmed, indeterminate = _alarm_state_view(self.hass, self.config)
        if indeterminate:
            return []
        actions = []
        # A manual exit holds only until the alarm is disarmed again.
        if disarm_confirmed and self._auto_suppressed:
            self._auto_suppressed = False
            await self._persist()
        if (safety_config.automatic_lockdown_enabled(self.config) and armed
                and not self.active and not self._auto_suppressed):
            action = await self.engage("alarm armed", auto=True)
        elif self.active and self.auto and disarm_confirmed:
            action = await self.disengage("alarm disarmed")
        else:
            action = None
        if action:
            actions.append(action)
        return actions


def _state_changed() -> None:
    """Tell Nova's entities that lockdown went on or off."""
    from . import nova_state
    nova_state.notify()
