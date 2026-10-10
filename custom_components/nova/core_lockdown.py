"""The formal lockdown: the announcement text and LockdownManager (engage, lift,
breach enforcement and the background secure check).

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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


# ── Lockdown (v5.9.36) ──────────────────────────────────────────────────────

def build_lockdown_message(honorific: str, locked: list, closed: list,
                           open_names: list, lang: str = "en",
                           failed: Optional[list] = None) -> str:
    """
    Compose the lockdown-engaged announcement. Pure (no I/O) so it's unit-tested.

    Three outcomes are reported distinctly: locks Nova sent a lock command to,
    closeable openings it sent a close command to (garage doors / motorized
    covers), and openings it can't secure remotely (bare window contacts) —
    those are named and framed as the gap to close by hand, never a footnote.
    A fourth, `failed`, names the locks whose lock command itself failed
    (8.7.16): they are said to be unsecured, never counted as done. The message
    never claims the home is secure while something is open or failed, and
    never announces a non-event: "already fully secured" is only said when
    nothing was done, nothing is open and nothing failed.

    Phase 3 (honest verification): engage() only ever gets here with a
    non-empty `locked`/`closed` list when it just called the lock/close
    service and scheduled a background _verify_secured() for each of those
    entities — at the moment this message is composed, none of that has been
    confirmed yet. So the "did" branches report what was SENT, not what was
    achieved, and promise the alert _verify_secured() delivers on failure,
    rather than asserting the home is secure. The "already secured" and
    "gap only" branches are unaffected: they're only reached when nothing was
    acted on (everything was already observed secure before any action), so
    there's nothing pending to be honest about.

    Fully localized (v7.80.0): the composed variants are stitched from localized
    verb phrases, a localized list join, and per-language wrappers, so a
    non-English household gets the whole message — including the device list — in
    their language. Device/area names pass through untranslated.
    """
    i18n = _notify_i18n()
    # Not coerced to "sir" here — an empty honorific means nobody specifically
    # home to address (see honorific.py), and i18n.message() already handles
    # that by capitalizing the sentence instead of prefixing it.
    h = (honorific or "").title()

    actions = []
    if locked:
        actions.append(i18n.message("lockdown_lock_pending", lang,
                                    names=i18n.join_names(locked, lang)))
    if closed:
        actions.append(i18n.message("lockdown_close_pending", lang,
                                    names=i18n.join_names(closed, lang)))
    did = i18n.join_names(actions, lang)   # localized "lock X and close Y"

    def gap(names: list) -> str:
        if len(names) == 1:
            return i18n.message("lockdown_gap_one", lang,
                                names=i18n.join_names(names, lang))
        if len(names) <= 3:
            return i18n.message("lockdown_gap_few", lang,
                                names=i18n.join_names(names, lang))
        return i18n.message("lockdown_gap_many", lang, count=len(names))

    # What is wrong, as one clause: the locks that failed first, then the
    # openings Nova cannot close remotely.
    problems = []
    if failed:
        problems.append(i18n.message("lockdown_secure_failed", lang,
                                     names=i18n.join_names(list(failed), lang)))
    if open_names:
        problems.append(gap(open_names))
    problem = "; ".join(problems)

    if did and problem:
        return i18n.message("lockdown_did_gap_pending", lang, honorific=h, did=did,
                            gap=problem)
    if did:
        return i18n.message("lockdown_did_pending", lang, honorific=h, did=did)
    if failed:
        return i18n.message("lockdown_failed_only", lang, honorific=h, gap=problem)
    if open_names:
        return i18n.message("lockdown_gap_only", lang, honorific=h,
                            gap=problem)
    return i18n.message("lockdown_already_secured", lang, honorific=h)


# ── shared with the nighttime sweep (8.23.0) ────────────────────────────────
# The formal lockdown and the nighttime sweep (core_safety.py) pick the locks
# to lock the same way and send the same commands. Which covers each closes
# is still their own: the sweep closes every open cover, lockdown only door,
# garage, window and gate covers (left as it is; see the 8.23.0 notes).

def unlocked_locks(hass, exempt) -> list:
    """Every lock reading unlocked that is not on the exempt list."""
    return [st for st in hass.states.async_all("lock")
            if st.state == "unlocked" and st.entity_id not in exempt]


async def secure_device(hass, entity_id: str, domain: str) -> None:
    """Lock a lock, or close a cover, and wait for the call. Raises if the
    call fails; callers log and audit it their own way. Goes through the one
    authority check as Nova acting on its own (8.24.0): locking and closing
    are low risk, so this is allowed; anything else would be refused."""
    from . import policy
    service = "lock" if domain == "lock" else "close_cover"
    decision = policy.authorize_now(hass, policy.AuthorityRequest(
        "lock" if domain == "lock" else "cover", service, entity_id,
        source=policy.SOURCE_AUTOMATIC))
    if not decision.allowed:
        raise PermissionError(decision.note or "not authorized")
    if domain == "lock":
        await hass.services.async_call(
            "lock", "lock", {"entity_id": entity_id}, blocking=True)
    else:
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": entity_id}, blocking=True)


class LockdownManager:
    """
    Formal lockdown state — engaged when the alarm is armed or on explicit
    request. On engage it LOCKS every lock and CLOSES every open door/garage
    cover, and snapshots the windows that are already open so they're IGNORED
    for the duration (knowingly left open). While active it actively re-secures
    any door reopened or lock unlocked (a breach), and flags any NEW window that
    opens (it can't close a window sensor, but it warns). Auto-disengages when
    the alarm is disarmed — but only if it was the alarm that engaged it; a
    manually-requested lockdown stays until explicitly lifted.
    """

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self.active = False
        self.since = 0.0
        self.reason = ""
        self.auto = False                 # engaged by the alarm (auto-lift on disarm)
        self.exempt_windows: set = set()   # openings open at engage / adopted as intentional (doors + windows)
        # Lock entities Lockdown must never touch (e.g. thermostat child
        # locks) — config-overridable, defaults to LOCKDOWN_EXEMPT_LOCKS_DEFAULT.
        # NOTE: distinguish "key absent" (None -> use default) from an
        # explicit empty list (a deliberate "exempt nothing" override) — an
        # `or` here would silently discard a real [] override (falsy).
        _exempt_cfg = config.get("lockdown_exempt_locks", None)
        self.exempt_locks: set = set(
            _exempt_cfg if _exempt_cfg is not None else LOCKDOWN_EXEMPT_LOCKS_DEFAULT)
        self._secured_by_us: set = set()   # entities Nova closed/locked this lockdown (reopen ⇒ intentional)
        self._alerted: set = set()         # entities already alerted about this lockdown
        self._last_breach_alert = 0.0
        self._automatic_generation = 0
        # (entity_id, friendly_name) of every lock the last _lock_all() could
        # not lock because the lock command itself failed (8.7.16).
        self._lock_failures: list = []
        # When the user manually lifts lockdown while the alarm is still armed,
        # this suppresses auto re-engage until the alarm is disarmed and re-armed
        # — so "exit lockdown" from the UI actually keeps you out.
        self._auto_suppressed = False
        # Restore across restarts so a pre-existing exempt window (or a manual
        # exit) isn't lost on a reboot/integration reload.
        self._load_state()

    def _load_state(self) -> None:
        try:
            if not os.path.exists(_lockdown_state_path()):
                return
            with open(_lockdown_state_path()) as f:
                d = json.load(f)
            self._auto_suppressed = bool(d.get("auto_suppressed", False))
            if d.get("active"):
                self.active = True
                self.since = d.get("since", time.time())
                self.reason = d.get("reason", "restored")
                self.auto = bool(d.get("auto", False))
                # A lock is never exempt (8.7.20): a state file written by an
                # older version may still list one adopted as "left open".
                self.exempt_windows = {e for e in d.get("exempt_windows", [])
                                       if not str(e).startswith("lock.")}
                _LOGGER.warning(
                    "Lockdown state RESTORED (auto=%s, %d exempt windows)",
                    self.auto, len(self.exempt_windows))
                # Versions before the automatic-lockdown opt-in could persist
                # an alarm-owned lockdown even though the user never enabled
                # automatic device control. Clear only that Nova state. Do not
                # call disengage(): it emits speech, and neither path sends an
                # unlock or disarm command.
                from . import safety_config
                if self.auto and not safety_config.automatic_lockdown_enabled(self.config):
                    self.active = False
                    self.since = 0.0
                    self.reason = ""
                    self.auto = False
                    self.exempt_windows = set()
                    self._auto_suppressed = False
                    self._persist_sync()
                    _LOGGER.warning(
                        "Cleared legacy automatic lockdown state; devices unchanged")
        except Exception as exc:
            _LOGGER.warning("Lockdown state restore failed: %s", exc)

    def _persist_sync(self) -> None:
        try:
            _m_common.write_json_atomic(_lockdown_state_path(), {
                "active": self.active,
                "since": self.since,
                "reason": self.reason,
                "auto": self.auto,
                "exempt_windows": sorted(self.exempt_windows),
                "auto_suppressed": self._auto_suppressed,
            })
        except Exception as exc:
            _LOGGER.debug("Lockdown state persist failed: %s", exc)

    async def _persist(self) -> None:
        try:
            await self.hass.async_add_executor_job(self._persist_sync)
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "active": self.active,
            "since": self.since,
            "reason": self.reason,
            "auto": self.auto,
            "exempt_windows": len(self.exempt_windows),
        }

    def _alarm_armed(self) -> bool:
        from . import alarm_source
        for st in alarm_source.states(self.hass, self.config):
            if st.state in ALARM_ARMED_STATES:
                return True
        return False

    # ── opening / secure-state model (doors + windows + locks) ──────────────
    _DOOR_WINDOW_BS = ("door", "window", "garage_door", "opening")
    _CLOSEABLE_COVERS = {"door", "garage", "garage_door", "window", "gate"}

    def _open_openings(self) -> set:
        """Every door/window currently open right now (sensors + covers).

        A door or window sensor counts only when it is a way into the house
        (household.is_way_in, 8.21.0): a fridge door, an excluded sensor or a
        shed door is not named as a gap to close by hand. Covers keep their
        own device class rule, so which covers lockdown closes is unchanged."""
        from . import household
        out = set()
        for st in self.hass.states.async_all("binary_sensor"):
            if (st.attributes.get("device_class") in self._DOOR_WINDOW_BS and st.state == "on"
                    and household.is_way_in(self.hass, st)):
                out.add(st.entity_id)
        for st in self.hass.states.async_all("cover"):
            if st.attributes.get("device_class") in self._CLOSEABLE_COVERS and st.state in ("open", "opening"):
                out.add(st.entity_id)
        return out

    def _is_relevant(self, dom: str, dc, eid: str = None) -> bool:
        if dom == "lock":
            return eid not in self.exempt_locks
        if dom == "cover":
            return dc in self._CLOSEABLE_COVERS
        if dom == "binary_sensor":
            return dc in self._DOOR_WINDOW_BS
        return False

    def _is_secure(self, dom: str, state) -> bool:
        s = str(state).lower()
        if dom == "lock":
            return s == "locked"
        if dom == "cover":
            return s in ("closed", "closing")
        if dom == "binary_sensor":
            return s in ("off", "closed", "false")     # off ⇒ closed/secure
        return True

    def _can_secure(self, dom: str, dc) -> bool:
        """Can Nova actually close/lock this? A bare contact sensor cannot."""
        if dom == "lock":
            return True
        if dom == "cover":
            return dc in self._CLOSEABLE_COVERS
        return False

    async def _secure_entity(self, eid: str, dom: str) -> bool:
        try:
            await secure_device(self.hass, eid, dom)
            return True
        except Exception as exc:
            _LOGGER.warning("Lockdown: secure %s failed: %s", eid, exc)
            return False

    async def _record(self, decision: str, reason: str, *, entity_id=None,
                      assessment: str = "", **facts) -> None:
        """Write one lockdown decision to the Decision Record in the shared
        safety format (alert_path, 8.23.0). Never raises."""
        from . import alert_path
        await alert_path.async_record_decision(
            self.hass, "lockdown", source="lockdown", entity_id=entity_id,
            sit=alert_path.situation(self.hass, self.config),
            facts=dict(facts, auto=self.auto), assessment=assessment,
            decision=decision, reason=reason)

    def _friendly(self, eid: str) -> str:
        """Friendly name for an entity (falls back to its id)."""
        st = self.hass.states.get(eid)
        return ((st.attributes.get("friendly_name") if st else None) or eid)

    def set_automatic_lockdown(self, enabled: bool) -> None:
        self.config["lockdown_auto_on_arm"] = enabled is True
        self._automatic_generation += 1

    def _automatic_operation_current(self, generation: Optional[int]) -> bool:
        if generation is None:
            return True
        from . import safety_config
        return (generation == self._automatic_generation
                and safety_config.automatic_lockdown_enabled(self.config))

    async def _lock_all(self, request_id: Optional[str] = None,
                        automatic_generation: Optional[int] = None) -> list:
        """Returns (entity_id, friendly_name) pairs for every lock the call
        actually reached — the entity_id is needed so engage() can schedule
        _verify_secured() per lock, the same honest background-confirmation
        step the cover/opening sweep below already uses.

        request_id, when given, is engage()'s own request_id — every lock
        touched here is logged as a target of THAT one lockdown request, not
        a separate action per lock (start_many, one batch)."""
        candidates = unlocked_locks(self.hass, self.exempt_locks)
        from . import action_log
        row_ids: dict = {}
        if request_id and candidates:
            row_ids = await self.hass.async_add_executor_job(
                lambda: action_log.start_many(
                    request_id, "lockdown_engage", "safety",
                    [{"key": st.entity_id, "domain": "lock", "service": "lock",
                      "entity_id": st.entity_id} for st in candidates],
                )
            )
        locked = []
        self._lock_failures = []
        for st in candidates:
            if not self._automatic_operation_current(automatic_generation):
                break
            eid = st.entity_id
            fname = st.attributes.get("friendly_name", eid)
            row_id = row_ids.get(eid)
            try:
                await secure_device(self.hass, eid, "lock")
                if not self._automatic_operation_current(automatic_generation):
                    break
                locked.append((eid, fname))
                _LOGGER.info("Lockdown: locked %s", eid)
                if row_id is not None:
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
            except Exception as exc:
                _LOGGER.warning("Lockdown: failed to lock %s: %s", eid, exc)
                self._lock_failures.append((eid, fname))
                if row_id is not None:
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )
        return locked

    async def engage(self, reason: str, auto: bool = False,
                     announce: bool = True) -> Optional[dict]:
        if self.active:
            return None
        automatic_generation = self._automatic_generation if auto else None
        if not self._automatic_operation_current(automatic_generation):
            return None
        self.active = True
        self.since = time.time()
        self.reason = reason
        self.auto = auto
        self._secured_by_us = set()
        self._alerted = set()
        self._last_breach_alert = 0.0
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware

        # One request_id for the whole engage() call — locks and closed
        # openings below are both targets of this ONE lockdown action, not
        # separate logged actions.
        from . import action_log
        request_id = action_log.new_request_id()

        # 1) Lock every closed-but-unlocked lock.
        locked_pairs = await self._lock_all(
            request_id=request_id,
            automatic_generation=automatic_generation,
        )
        if not self._automatic_operation_current(automatic_generation):
            return None
        locked = [fname for _eid, fname in locked_pairs]
        for eid, fname in locked_pairs:
            self.hass.async_create_task(self._verify_secured(eid, "lock", fname))
        # A lock whose command failed is named in the message as not secured
        # and gets the same background check as the ones that were sent, so a
        # lock that is still unlocked is raised as a critical alert too.
        failed_pairs, self._lock_failures = list(self._lock_failures), []
        failed_locks = [fname for _eid, fname in failed_pairs]
        for eid, fname in failed_pairs:
            self.hass.async_create_task(self._verify_secured(eid, "lock", fname))

        # 2) Close every open *closeable* opening (garage doors / motorized
        #    covers). These have safety sensors, so an obstruction simply fails
        #    the close — the verify step catches that and surfaces it. Bare
        #    contacts (windows) have no actuator and can't be closed.
        closed: list = []
        uncloseable: set = set()
        close_candidates: list = []
        for eid in self._open_openings():
            dom = eid.split(".", 1)[0]
            st = self.hass.states.get(eid)
            dc = st.attributes.get("device_class") if st else None
            if self._can_secure(dom, dc):
                close_candidates.append((eid, dom))
            else:
                uncloseable.add(eid)
        close_row_ids: dict = {}
        if close_candidates:
            close_row_ids = await self.hass.async_add_executor_job(
                lambda: action_log.start_many(
                    request_id, "lockdown_engage", "safety",
                    [{"key": eid, "domain": dom, "service": "close_cover",
                      "entity_id": eid} for eid, dom in close_candidates],
                )
            )
        for eid, dom in close_candidates:
            if not self._automatic_operation_current(automatic_generation):
                return None
            name = self._friendly(eid)
            row_id = close_row_ids.get(eid)
            if await self._secure_entity(eid, dom):
                if not self._automatic_operation_current(automatic_generation):
                    return None
                closed.append(name)
                self._secured_by_us.add(eid)
                self.hass.async_create_task(self._verify_secured(eid, dom, name))
                if row_id is not None:
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
            else:
                uncloseable.add(eid)   # the close call failed outright
                if row_id is not None:
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )

        # 3) What we can't secure is left as-is and adopted as intentional — the
        #    user is alerted once here and not nagged afterwards (no action means
        #    they meant to leave it open).
        self.exempt_windows = uncloseable
        open_names = sorted(self._friendly(eid) for eid in uncloseable)

        if not self._automatic_operation_current(automatic_generation):
            return None
        message = build_lockdown_message(honorific, locked, closed, open_names,
                                          lang=_m_common._hass_lang(self.hass),
                                          failed=failed_locks)
        _LOGGER.warning(
            "Lockdown ENGAGED (%s): locked=%s closed=%s left-open=%d "
            "lock-failed=%s announce=%s",
            reason, locked, closed, len(uncloseable), failed_locks, announce)
        await self._persist()
        await self._record(
            "lockdown engaged", reason, assessment="secure the house",
            locked=locked, closed=closed, left_open=open_names,
            failed=failed_locks, announced=announce)
        if not announce:
            return None
        return {
            "type": "lockdown_engaged",
            "urgency": "high",
            "message": message,
            "auto_act": True,
        }

    async def disengage(self, reason: str, manual: bool = False) -> Optional[dict]:
        if not self.active:
            return None
        # If the user manually lifts lockdown while the alarm is still armed,
        # remember not to auto re-engage until the alarm is disarmed/re-armed.
        if manual and self._alarm_armed():
            self._auto_suppressed = True
        self.active = False
        self.reason = ""
        self.auto = False
        self.exempt_windows = set()
        self._secured_by_us = set()
        self._alerted = set()
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
        _LOGGER.warning("Lockdown DISENGAGED (%s, manual=%s, auto_suppressed=%s)",
                        reason, manual, self._auto_suppressed)
        await self._persist()
        await self._record("lockdown lifted", reason, manual=manual,
                           auto_suppressed=self._auto_suppressed)
        return {
            "type": "lockdown_disengaged",
            "urgency": "low",
            "message": _notify_i18n().message(
                "lockdown_lifted", _m_common._hass_lang(self.hass), honorific=honorific.title()),
            "auto_act": True,
        }

    async def handle_state_change(self, eid: str, old, new) -> Optional[dict]:
        """
        A door/window/lock changed while lockdown is active. Policy:
          • ignore anything already open at engage (the exempt baseline);
          • only react to a secure→unsecure transition;
          • if Nova can close/lock it, do so — and if it doesn't take, alert;
          • if it can't be closed (a bare contact sensor), assume it was opened
            intentionally and leave it (no nagging);
          • if something Nova closed gets reopened, the user means it — adopt it
            and say so once.
        Returns an alert action to announce, or None.
        """
        if not self.active or eid in self.exempt_windows:
            return None
        dom = eid.split(".", 1)[0]
        dc = new.attributes.get("device_class")
        if not self._is_relevant(dom, dc, eid):
            return None
        if self._is_secure(dom, new.state):
            if dom == "lock":
                self._alerted.discard(eid)   # locked again: a later unlock alerts again
            return None
        if old is not None and not self._is_secure(dom, old.state):
            return None  # was already unsecure — not a fresh transition
        name = new.attributes.get("friendly_name", eid)
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware

        if not self._can_secure(dom, dc):
            # Nothing Nova can do about a contact sensor → assume intentional.
            self.exempt_windows.add(eid)
            await self._persist()
            _LOGGER.info("Lockdown: %s opened (not controllable) — treating as intentional", eid)
            await self._record("left open as intentional", "Nova cannot close it",
                               entity_id=eid, assessment="opened during lockdown")
            return None

        if eid in self._secured_by_us and dom == "lock":
            # A lock unlocked again after Nova locked it (8.7.20). It is never
            # adopted as intentional, never left out of the lockdown and not
            # locked a second time (Nova does not fight a person at the door):
            # it is a critical alert, once until it is locked again.
            if eid in self._alerted:
                return None
            self._alerted.add(eid)
            await self._record("critical alert, not locked again",
                               "unlocked again after Nova locked it",
                               entity_id=eid, assessment="unlocked during lockdown")
            return {
                "type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                "message": _persona().lead_in(honorific,
                    f"{name} was unlocked again during lockdown after I locked it. "
                    f"Please check it."),
            }

        if eid in self._secured_by_us:
            # We shut it once and it's open again → the user wants it open.
            self._secured_by_us.discard(eid)
            self.exempt_windows.add(eid)
            await self._persist()
            await self._record("left open as intentional",
                               "reopened after Nova secured it",
                               entity_id=eid, assessment="reopened during lockdown")
            if eid in self._alerted:
                return None
            self._alerted.add(eid)
            return {
                "type": "lockdown_breach", "urgency": "high", "auto_act": True,
                "message": _persona().lead_in(honorific,
                    f"{name} reopened after I secured it — I'll leave it open."),
            }

        _LOGGER.warning("Lockdown: securing %s after it opened", eid)
        from . import action_log
        breach_request_id = action_log.new_request_id()
        breach_action_id = await self.hass.async_add_executor_job(
            lambda: action_log.start(
                breach_request_id, "lockdown_breach_resecure", "safety",
                domain=dom, service=("lock" if dom == "lock" else "close_cover"),
                entity_id=eid,
            )
        )
        ok = await self._secure_entity(eid, dom)
        await self.hass.async_add_executor_job(
            lambda: action_log.set_execution(
                breach_action_id, "accepted" if ok else "failed",
                reason_code=None if ok else "service_call_failed")
        )
        await self._record("secured again" if ok else "could not secure",
                           "opened during lockdown", entity_id=eid,
                           assessment="opened during lockdown")
        if ok:
            self._secured_by_us.add(eid)
            # Confirm it actually shut (slow covers report late) and alert if not.
            self.hass.async_create_task(self._verify_secured(eid, dom, name))
            return None
        if eid in self._alerted:
            return None
        self._alerted.add(eid)
        return {
            "type": "lockdown_breach", "urgency": "critical", "auto_act": True,
            "message": _persona().lead_in(honorific,
                f"{name} opened during lockdown and I couldn't secure it."),
        }

    async def _verify_secured(self, eid: str, dom: str, name: str) -> None:
        """After trying to close/lock something, make sure it actually took."""
        try:
            await asyncio.sleep(LOCKDOWN_SECURE_VERIFY_DELAY)
            if not self.active or eid in self.exempt_windows:
                return
            st = self.hass.states.get(eid)
            if st is None or self._is_secure(dom, st.state):
                return  # secure now — nothing to report
            if eid in self._alerted:
                return
            self._alerted.add(eid)
            await self._record("critical alert", "still not secure after Nova tried",
                               entity_id=eid, assessment="secure check failed")
            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
            await _emit_action(self.hass, self.config, {
                "type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                "message": _persona().lead_in(honorific,
                    f"I tried to secure {name} during lockdown but it's still open."),
            }, False)
        except Exception as exc:
            _LOGGER.debug("lockdown secure-verify error: %s", exc)

    async def tick(self) -> list[dict]:
        """Alarm-driven auto engage/disengage. Breach enforcement is event-driven
        (handle_state_change), so it isn't repeated here."""
        actions = []
        from . import safety_config
        armed, disarm_confirmed, indeterminate = _alarm_state_view(
            self.hass, self.config)
        if indeterminate:
            return actions
        auto_on_arm = safety_config.automatic_lockdown_enabled(self.config)

        # Clear a manual-exit suppression once the alarm is disarmed again.
        if disarm_confirmed and self._auto_suppressed:
            self._auto_suppressed = False
            await self._persist()

        if auto_on_arm and armed and not self.active and not self._auto_suppressed:
            a = await self.engage("alarm armed", auto=True)
            if a:
                actions.append(a)
        elif self.active and self.auto and disarm_confirmed:
            a = await self.disengage("alarm disarmed")
            if a:
                actions.append(a)
        return actions
