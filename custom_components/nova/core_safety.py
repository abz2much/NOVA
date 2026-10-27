"""The safety manager: pipe freeze warnings, the intrusion investigation and
the night sweep that locks and closes up while the house sleeps.

cognitive_core.py exports every name defined here, as the same object.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant

from . import core_common as _m_common
from .core_bridge import _lockdown_exempt_locks, is_lockdown
from .core_common import (
    ALARM_ARMED_STATES,  # noqa: F401 - cognitive_core re-exports it from here
    FREEZE_CRITICAL_TEMP_F,
    FREEZE_REMINDER_SECONDS,
    FREEZE_WARN_TEMP_F,
    INTRUSION_CLEAR_QUIET_SECS,
    INTRUSION_INWARD_DEPTH,
    INTRUSION_MAX_INVESTIGATE_SECS,
    INTRUSION_RESPONSE_TIMEOUT_SECS,
    INTRUSION_SPREAD_ZONES,
    INTRUSION_SUSTAINED_SECS,
    LOCKDOWN_CHECK_INTERVAL,
    LOCKDOWN_SECURE_VERIFY_DELAY,
    _f_to_unit,
    _fmt_temp,
    _notify_i18n,
    _persona,
    _temp_to_f,
    discover_outdoor_temp,
)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

# The night sweep's backstop: never more than this many lock and close
# commands in any hour, whatever else happens.
SWEEP_MAX_COMMANDS_PER_HOUR = 12

_FREEZE_COOLDOWN = 3600.0       # seconds between any two freeze alerts
_FREEZE_EPISODE_ENDS = 5        # °F above the warning level that ends a cold spell
_REPEAT_ALERT_GAP = 300.0       # seconds before a new intrusion alert may start
_OPENING_SENSORS = ("door", "window", "garage_door", "opening")
_OPENING_COVERS = ("door", "garage", "garage_door", "gate")
_MOTION_CLASSES = ("motion", "occupancy", "presence")
# How the vision model may answer a "is there a person?" question in free text.
_NO_PERSON = ("no person", "not a person", "no one", "nobody", "empty", "no people",
              "there is no", "cannot see a person", "don't see a person", "no human")
_A_PERSON = ("a person", "person is", "someone is", "an intruder", "a man", "a woman",
             "a human", "people are")
_VISION_PROMPT = " ".join((
    "Security check. Look at this indoor camera frame.",
    "Is there a PERSON (a human being) actually present in the frame right now?",
    "Answer strictly: reply 'PERSON: YES' if a real person is clearly visible,",
    "or 'PERSON: NO' if there is no person (e.g. it is an empty room, a shadow,",
    "a light, a reflection, a pet, or an object).",
    "Do not guess — if you cannot clearly see a person, answer NO.",
))


def _intrusion():
    """intrusion.py, read at call time so tests and reloads see the live one."""
    from . import intrusion
    return intrusion


def _best_effort(work, default=None):
    """``work()``, or ``default`` if it raises. For the bookkeeping that must
    never change what the safety code decides."""
    try:
        return work()
    except Exception:
        return default


def _changed_ts(state) -> Optional[float]:
    """When a state last changed, as a timestamp, or None if unreadable."""
    try:
        return state.last_changed.timestamp()
    except Exception:
        return None


def _setting(config: dict, key: str, default, kind):
    """A number from the config, or ``default`` when it is not one."""
    try:
        return kind(config.get(key, default))
    except (ValueError, TypeError):
        return default


def _vision_verdict(text: str) -> Optional[bool]:
    """Read the vision model's answer: True a person, False none, None when
    it cannot be told. The explicit tag wins over the wording."""
    if "person: yes" in text:
        return True
    if "person: no" in text:
        return False
    saw_none = any(phrase in text for phrase in _NO_PERSON)
    saw_one = any(phrase in text for phrase in _A_PERSON)
    if saw_none != saw_one:
        return saw_one
    return None


class SafetyManager:
    """Watches for safety critical situations and acts on them."""

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass, self.config = hass, config
        # When each kind of alert or check last went out (epoch seconds).
        self._last_intrusion_alert = self._last_freeze_alert = 0.0
        self._last_lockdown_check = 0.0
        self._investigation = None   # the intrusion investigation under way, if any
        self._freeze_warned = False
        # One cold spell is one episode: the critical alert is spoken once,
        # reminders after that go to the phones only, and the episode ends
        # when it warms up again.
        self._freeze_critical_spoken = False
        self._automatic_generation = 0
        # How long the night sweep waits before checking what it secured;
        # slow covers report late.
        self.sweep_verify_delay = LOCKDOWN_SECURE_VERIFY_DELAY
        # What the night sweep knows about tonight: what it secured, what it
        # is leaving alone until morning and why, and when it sent commands.
        # A new night (noon to noon) starts it afresh.
        self._sweep_night: dict = {}
        # Saving to situations.py starts only once restore_situations() has
        # run, which the live core does.
        self._persist = False
        self._saved: tuple = (None, None)
        # The household reading for the tick under way (household.py); None
        # between ticks, when the helpers below read the house live.
        self._house = None
        # Doors and windows already open when the alarm armed home or night,
        # and that arming's time. A sensor dropping out and coming back
        # resets its change time, so these stay left out until seen shut.
        self._open_when_armed: set[str] = set()
        self._open_when_armed_at: Optional[float] = None

    # ── surviving a restart ─────────────────────────────────────────────────

    def restore_situations(self) -> None:
        """After a restart or reload, pick up the intrusion investigation and
        the freeze state from situations.py. Only state comes back; the logic
        is the same. Nothing saved, or a stale save, starts fresh. Never
        raises."""
        try:
            from . import situations
            if not situations.started():
                return
            investigation, last_alert = situations.restore_intrusion()
            if investigation is not None:
                self._investigation = investigation
                self._last_intrusion_alert = last_alert
                _LOGGER.warning("intrusion: investigation restored after a restart")
            self._freeze_warned, self._last_freeze_alert = situations.restore_freeze()
            self._freeze_critical_spoken = situations.restore_freeze_critical()
            self._persist = True
            self._saved = self._situation_signature()
        except Exception as exc:
            _LOGGER.warning("Safety: could not restore situations: %s", exc)

    def _situation_signature(self) -> tuple:
        from . import situations
        encoded = situations._encode_investigation(self._investigation)
        intrusion = json.dumps([encoded, self._last_intrusion_alert], sort_keys=True, default=str)
        freeze = (self._freeze_warned, self._last_freeze_alert, self._freeze_critical_spoken)
        return intrusion, freeze

    def _save_situations(self) -> None:
        """Save whichever of the intrusion and freeze state changed. Never
        raises: a failed save must not touch the safety checks."""
        if not self._persist:
            return
        try:
            from . import situations
            now = self._situation_signature()
            if now[0] != self._saved[0]:
                situations.save_intrusion(self._investigation, self._last_intrusion_alert)
            if now[1] != self._saved[1]:
                situations.save_freeze(*now[1])
            self._saved = now
        except Exception as exc:
            _LOGGER.debug("Safety: could not save situations: %s", exc)

    # ── automatic lockdown switch ───────────────────────────────────────────

    def set_automatic_lockdown(self, enabled: bool) -> None:
        self.config["lockdown_auto_on_arm"] = enabled is True
        self._automatic_generation += 1

    def _automatic_operation_current(self, generation: int) -> bool:
        """Whether a sweep started under ``generation`` may carry on: the
        switch is still on and was not flipped since it started."""
        from . import safety_config
        return (generation == self._automatic_generation
                and safety_config.automatic_lockdown_enabled(self.config))

    # ── the tick ────────────────────────────────────────────────────────────

    async def tick(self, sleeping: bool, anyone_home: bool, house=None) -> list[dict]:
        """Run every safety check and return the actions taken.

        Freeze, intrusion and the night sweep are each guarded on their own:
        an error in one is logged, the others still run, and whatever was
        already raised is still returned, so a fault here can never swallow
        an alert.

        ``house`` is this tick's household reading (household.snapshot); one
        is taken here when it is not given, and every check uses it."""
        try:
            from . import household
            self._house = house or household.snapshot(self.hass, self.config)
        except Exception as exc:
            _LOGGER.warning("Safety tick: household reading failed: %s", exc)
            self._house = None
        try:
            return await self._tick(sleeping, anyone_home)
        finally:
            self._house = None
            self._save_situations()

    async def _tick(self, sleeping: bool, anyone_home: bool) -> list[dict]:
        actions: list = []
        try:
            self._note_open_when_armed()
        except Exception as exc:
            _LOGGER.warning("Safety tick: noting openings at arming failed: %s", exc)
        try:
            freeze = await self._check_freeze()
            if freeze:
                actions.append(freeze)
        except Exception as exc:
            _LOGGER.warning("Safety tick: freeze check failed: %s", exc)
        try:
            intrusion = await self._intrusion_stage(sleeping, anyone_home)
            if intrusion:
                actions.append(intrusion)
        except Exception as exc:
            _LOGGER.warning("Safety tick: intrusion check failed: %s", exc)
        try:
            self._maybe_start_sweep(sleeping)
        except Exception as exc:
            _LOGGER.warning("Safety tick: nighttime lockdown failed: %s", exc)
        return actions

    async def _intrusion_stage(self, sleeping: bool, anyone_home: bool) -> Optional[dict]:
        """Intrusion watching runs only when the residents are confidently
        away (tracked away, or armed away) or asleep, never merely because no
        one shows up as present, which happens with an untracked person home.

        With intrusion_requires_confinement on, confinement is the master
        switch instead: watching runs only while a lockdown is engaged or the
        chosen alarm is armed, and ending confinement stops it at once,
        dropping any investigation under way."""
        from . import safety_config
        if safety_config.intrusion_requires_confinement(self.config):
            # A triggered alarm still counts as confined: that is the moment
            # with the strongest evidence, not the moment to stop.
            if is_lockdown() or self._alarm_armed() or self._alarm_triggered():
                return await self._check_intrusion(anyone_home, sleeping, confined=True)
            self._investigation = None
            return None
        # Turned off mid investigation: drop one that existed only because
        # of confinement.
        if self._investigation is not None and self._investigation.get("trigger") == "confined":
            self._investigation = None
        if self._residents_away() or sleeping or self._investigation is not None:
            return await self._check_intrusion(anyone_home, sleeping)
        return None

    def _maybe_start_sweep(self, sleeping: bool) -> None:
        """Start the night sweep when automatic lockdown is on, the house is
        asleep, no formal lockdown is doing the job already and the interval
        has passed. It runs in the background, since it waits to check what
        it secured and must never hold up the safety loop. Its message goes
        to the phones, as a sleeping house's alerts do; it is never spoken."""
        from . import safety_config
        now = time.time()
        if not (safety_config.automatic_lockdown_enabled(self.config) and sleeping
                and not is_lockdown()
                and now - self._last_lockdown_check > LOCKDOWN_CHECK_INTERVAL):
            return
        self._last_lockdown_check = now
        self.hass.async_create_task(self._sweep_and_report(self._automatic_generation))

    # ── freeze ──────────────────────────────────────────────────────────────

    async def _check_freeze(self) -> Optional[dict]:
        """Watch the outdoor temperature for frozen pipes.

        The reading is in Home Assistant's own unit (°C on a metric install),
        so it is compared in °F and reported in the household's unit; a mild
        18 °C day never sounds like frost.

        A cold spell is one episode. Once the critical alert has gone out,
        a milder "freeze warning" is not sent as it warms slightly; only a
        critical reminder, to the phones, every few hours while it stays
        critical. The episode ends when it is clearly warmer again."""
        now = time.time()
        if now - self._last_freeze_alert < _FREEZE_COOLDOWN:
            return None
        found = discover_outdoor_temp(self.hass)
        if found is None:
            return None
        reading, unit = found
        fahrenheit = _temp_to_f(reading, unit)
        shown = _fmt_temp(reading, unit)
        title = _persona().capitalise(_m_common._live_honorific(self.hass))
        lang = _m_common._hass_lang(self.hass)

        if fahrenheit <= FREEZE_CRITICAL_TEMP_F:
            reminder = self._freeze_critical_spoken
            if reminder and now - self._last_freeze_alert < FREEZE_REMINDER_SECONDS:
                return None
            self._last_freeze_alert = now
            self._freeze_critical_spoken = True
            return {
                "type": "freeze_critical",
                # Spoken once a cold spell, then reminders to the phones.
                "urgency": "high" if reminder else "critical",
                "phone_only": reminder,
                "message": _notify_i18n().message(
                    "freeze_critical", lang, honorific=title, reading=shown,
                    set_to=_fmt_temp(_f_to_unit(55, unit), unit, decimals=0)),
                # Advice only: nothing acts on it (there is no heating or
                # plumbing actuator behind it), and the message says so.
                "auto_act": False,
            }
        if fahrenheit > FREEZE_WARN_TEMP_F + _FREEZE_EPISODE_ENDS:
            self._freeze_warned = False
            self._freeze_critical_spoken = False
            return None
        if (fahrenheit <= FREEZE_WARN_TEMP_F and not self._freeze_warned
                and not self._freeze_critical_spoken):
            self._last_freeze_alert, self._freeze_warned = now, True
            text = _notify_i18n().message("freeze_warning", lang, honorific=title,
                                          reading=shown)
            return {"type": "freeze_warning", "urgency": "high", "message": text,
                    "auto_act": False}
        return None

    # ── reading the house ───────────────────────────────────────────────────

    def _household(self):
        """This tick's household reading, or a live one between ticks."""
        if self._house is not None:
            return self._house
        from . import household
        return household.snapshot(self.hass, self.config)

    def _alarm_armed(self) -> bool:
        return self._household().armed

    def _alarm_armed_away(self) -> bool:
        """Only the armed states that mean nobody should be moving about
        (away, vacation), not home or night."""
        from . import household
        return self._household().posture == household.AWAY

    def _alarm_triggered(self) -> bool:
        """The alarm has gone off, not merely been armed."""
        return self._household().triggered

    def _residents_home_guard(self, sleeping: bool) -> bool:
        """Residents are home and armed for being home, or asleep. Then a
        resident walking about looks just like an intruder moving through, so
        motion alone must never confirm an intrusion. The rule itself is
        household.Household.residents_home_guard."""
        return self._household().residents_home_guard(sleeping)

    def _residents_away(self) -> bool:
        """A confident "the residents are away", for intrusion only.

        From household.py: a person, or a tracker linked to one, reading home
        always wins; otherwise away when every person reads away, or the alarm
        is armed away or on vacation. Never from motion, an unknown person is
        never away, and a tracker linked to nobody (a TV, a hub) does not
        count."""
        return self._household().residents_away

    def _friendly(self, eid: Optional[str]) -> Optional[str]:
        if not eid:
            return None
        state = self.hass.states.get(eid)
        return (state.attributes.get("friendly_name") if state else None) or eid

    def _open_entry(self, areas: Optional[Iterable[str]] = None,
                    opened_after: Optional[float] = None) -> Optional[str]:
        """An open outside door or window, the way a real entry would come
        in, or None when everything is shut.

        Only the house itself counts: a fridge or oven door, anything the user
        excluded from Nova, or a driveway gate or shed door is never a way in
        (household.is_way_in), so an open garden gate cannot turn a curtain
        flutter into a corroborated intrusion. The garage is part of the house.
        ``areas`` limits it to those areas (the ground floor check).
        ``opened_after`` (a timestamp) leaves out anything that was already
        open by then, or was open at arming and not seen shut since; one whose
        change time cannot be read still counts."""
        from . import household
        allowed = set(areas) if areas else None

        def counts(state) -> bool:
            if opened_after is not None:
                if state.entity_id in self._open_when_armed:
                    return False
                changed = _changed_ts(state)
                if changed is not None and changed <= opened_after:
                    return False
            return (household.is_way_in(self.hass, state)
                    and (allowed is None or self._breach_area(state.entity_id) in allowed))

        for state in self.hass.states.async_all("binary_sensor"):
            if (state.attributes.get("device_class") in _OPENING_SENSORS
                    and state.state == "on" and counts(state)):
                return state.entity_id
        for state in self.hass.states.async_all("cover"):
            if (state.attributes.get("device_class") in _OPENING_COVERS
                    and state.state in ("open", "opening") and counts(state)):
                return state.entity_id
        return None

    def _ground_floor_open_entry(self, opened_after: Optional[float] = None
                                 ) -> tuple[Optional[str], bool]:
        """(an open outside door or window, whether ground floor areas are
        set). With ground_floor_areas set, only those count; without, every
        floor does, so leaving it unset never means less protection."""
        ground = self.config.get("ground_floor_areas") or []
        if not ground:
            return self._open_entry(opened_after=opened_after), False
        return self._open_entry(areas=ground, opened_after=opened_after), True

    def _armed_home_since(self) -> Optional[float]:
        """When the alarm armed for people at home (home, night, custom
        bypass), as a timestamp, or None when it is not armed that way or the
        time cannot be read. A window left open before arming, or one opened
        from inside while armed for being home, is then no sign of a way in."""
        from . import alarm_source, household
        if self._household().posture != household.HOME:
            return None
        try:
            times = [_changed_ts(st) for st in alarm_source.states(self.hass, self.config)
                     if household.ALARM_POSTURE.get(str(st.state).lower()) == household.HOME]
        except Exception:
            return None
        times = [t for t in times if t is not None]
        return min(times) if times else None

    def _note_open_when_armed(self) -> None:
        """Keep _open_when_armed up to date. On a new arming for people at
        home it records every door and window open since before it; after
        that one is dropped once it reads shut (an unreadable state is not
        shut). Disarmed, or armed any other way, it is cleared."""
        since = self._armed_home_since()
        if since is None:
            self._open_when_armed, self._open_when_armed_at = set(), None
            return
        if since != self._open_when_armed_at:
            self._open_when_armed_at = since
            self._open_when_armed = set()
            for state in self._opening_states():
                changed = _changed_ts(state)
                if (state.state in ("on", "open", "opening")
                        and changed is not None and changed <= since):
                    self._open_when_armed.add(state.entity_id)
            return
        for eid in list(self._open_when_armed):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("off", "closed", "closing"):
                self._open_when_armed.discard(eid)

    def _opening_states(self) -> list:
        """Every door and window sensor and cover _open_entry looks at."""
        return ([st for st in self.hass.states.async_all("binary_sensor")
                 if st.attributes.get("device_class") in _OPENING_SENSORS]
                + [st for st in self.hass.states.async_all("cover")
                   if st.attributes.get("device_class") in _OPENING_COVERS])

    def _breach_area(self, entry_eid: Optional[str]) -> Optional[str]:
        """The area of the way in, where an intruder would enter."""
        def area():
            from . import audio_routing
            return audio_routing.entity_area(self.hass, entry_eid)
        return _best_effort(area) if entry_eid else None

    def _motion_key(self, eid: str) -> str:
        """A motion sensor's zone: its area (its own or its device's), else
        its entity id. Different zones mean movement across the home."""
        try:
            from homeassistant.helpers import device_registry as dr
            from homeassistant.helpers import entity_registry as er
            entry = er.async_get(self.hass).async_get(eid)
            area = entry.area_id if entry else None
            if entry and not area and entry.device_id:
                device = dr.async_get(self.hass).async_get(entry.device_id)
                area = device.area_id if device else None
        except Exception:
            area = None
        return area or eid

    def _qualifying_motion(self, sleeping: bool) -> list:
        """Indoor motion sensors detecting now, as [(entity_id, name)].
        While asleep, bedrooms are left out."""
        from . import household
        bedrooms = self.config.get("bedroom_areas", []) if sleeping else []
        found = []
        for state in self.hass.states.async_all("binary_sensor"):
            if (state.attributes.get("device_class", "") not in _MOTION_CLASSES
                    or state.state != "on"):
                continue
            # A camera's car, animal or parcel sensor, a sensor the user
            # excluded and outdoor motion never start or spread an intrusion.
            if not household.is_person_motion(self.hass, state):
                continue
            if sleeping and bedrooms and self._motion_key(state.entity_id) in bedrooms:
                continue
            found.append((state.entity_id,
                          state.attributes.get("friendly_name") or state.entity_id))
        return found

    def _person_on_camera(self, indoor_only: bool = True) -> bool:
        """A camera reports a person now (Frigate's binary_sensor.<cam>_person
        and the like). Indoors only by default, which is what confirmation
        uses: a courier on the driveway camera is a doorstep event, not proof
        that someone is inside."""
        return self._person_camera_entity(indoor_only) is not None

    def _person_camera_entity(self, indoor_only: bool = True) -> Optional[str]:
        """The camera behind a sensor that sees a person now, for a snapshot,
        or None. binary_sensor.<cam>_person maps to camera.<cam> when that
        exists, else to a camera sharing its first word; failing both, the
        guessed camera id is returned anyway as a handle."""
        from . import outdoor
        for state in self.hass.states.async_all("binary_sensor"):
            if state.state != "on":
                continue
            shown = state.attributes.get("friendly_name") or ""
            if "person" not in f"{state.entity_id} {shown}".lower():
                continue
            if state.attributes.get("device_class") not in _MOTION_CLASSES + (None,):
                continue
            if indoor_only and outdoor.is_outdoor(self.hass, state.entity_id, shown):
                continue
            slug = state.entity_id.split(".", 1)[-1]
            for suffix in ("_person", "_person_occupancy", "_occupancy"):
                if slug.endswith(suffix):
                    slug = slug[: -len(suffix)]
                    break
            camera = f"camera.{slug}"
            if self.hass.states.get(camera) is not None:
                return camera
            from .camera import active_camera_states
            first_word = slug.split("_")[0]
            for candidate in active_camera_states(self.hass):
                if first_word in candidate.entity_id:
                    return candidate.entity_id
            return camera
        return None

    # ── intrusion: starting an investigation ────────────────────────────────

    def _begin_investigation(self, *, now: float, trigger: str, presence: str,
                             breach: Optional[str], breach_name: Optional[str],
                             armed: bool, eid: str, where: str, honorific: str,
                             reason: str) -> Optional[dict]:
        """Open an investigation (for the away, confined and sleeping cases
        alike), record the decision and the event, and return the first
        "investigating" alert, or None when this place and time has been
        learned to be harmless (the investigation still runs underneath)."""
        breach_area = self._breach_area(breach)
        # The search starts at the way in: the breach room and the rooms next
        # to it are where a real entry shows up first. Motion elsewhere is
        # still watched but never concluded to be an intrusion on its own,
        # which is what keeps false alarms down.
        connected = {breach_area} if breach_area else set()
        hops: dict = {}
        try:
            from . import residence_graph
            connected |= residence_graph.adjacent_areas(self.hass, self.config, breach_area)
            # Every room's distance from the way in, to tell motion moving
            # inward from motion lingering at the entry.
            hops = residence_graph.hops_from_breach(self.hass, self.config, breach_area)
        except Exception:
            pass
        connected.discard(None)
        start = self._motion_key(eid)
        depth = hops.get(start, 0) if hops else 0
        self._investigation = {
            "start": now, "last_motion": now,
            "zones": {start}, "path": [start], "escalated": False, "soft_notice": False,
            "breach_area": breach_area, "breach_name": breach_name, "connected": connected,
            "hops": hops, "max_depth": depth, "trigger": trigger,
        }
        message = self._first_alert_text(trigger, breach_name, armed, honorific, where)
        record_id = self._record_first_decision(trigger, eid, message, where, breach_name,
                                                armed, presence, reason)
        # A place and time repeatedly called a false alarm stays quiet on this
        # first, weak alert. Learning can only silence the weak alert: a real
        # route inward still confirms and raises the alarm.
        damped = False
        try:
            intrusion = _intrusion()
            damped = intrusion.should_damp_weak_alert(breach_area, None)
            intrusion.record_event(
                "investigating", reason="damped (learned benign)" if damped else reason,
                breach=breach_name, breach_area=breach_area, zones=[start],
                max_depth=depth, decision_id=record_id)
        except Exception:
            pass   # bookkeeping only; the investigation stands either way
        if damped:
            _LOGGER.info("intrusion: initial alert damped for learned-benign pattern "
                         "at %s (still investigating)", breach_area)
            return None
        action = {"type": "intrusion_investigating", "urgency": "high",
                  "message": message, "auto_act": True, "entity_id": eid}
        # Residents home and armed for it, or asleep: the first alert goes to
        # the phones only. The away case and the confirmed alert are spoken.
        if self._residents_home_guard(trigger == "sleeping"):
            action["phone_only"] = True
        return action

    def _first_alert_text(self, trigger: str, breach_name: Optional[str], armed: bool,
                          honorific: str, where: str) -> str:
        i18n = _notify_i18n()
        lang = _m_common._hass_lang(self.hass)
        if breach_name:
            context = i18n.message("intrusion_ctx_open", lang, name=breach_name)
        elif armed:
            context = i18n.message("intrusion_ctx_armed", lang)
        else:
            context = ""
        key = {"away": "intrusion_alert",
               "confined": "intrusion_alert_confined"}.get(trigger, "intrusion_alert_sleep")
        return i18n.message(key, lang, honorific=_persona().capitalise(honorific),
                            where=where, ctx=context)

    def _record_first_decision(self, trigger, eid, message, where, breach_name, armed,
                               presence, reason):
        """Write the decision record for a new investigation, in the shared
        safety format, and return its id (None on failure). Written here
        rather than at delivery, so a damped alert is recorded too and a call
        off attaches to it."""
        try:
            from . import alert_path
            situation = alert_path.situation(self.hass, self.config,
                                             sleeping=trigger == "sleeping",
                                             house=self._household())
            record_id = alert_path.record_decision(
                "intrusion", source="intrusion", entity_id=eid, message=message,
                sit=situation,
                facts={"location": where, "breach": breach_name, "alarm_armed": armed,
                       "presence": presence, "trigger": trigger},
                assessment="possible intrusion — investigating from the point of entry",
                decision="raise initial intrusion alert and investigate silently",
                reason=reason)
        except Exception:
            return None
        _best_effort(lambda: _intrusion().set_last_decision_id(record_id))
        return record_id

    async def _face_stand_down(self, trigger: str, where: str) -> bool:
        """Opt in (face_stand_down): True when a recognised resident should
        stop a new investigation from opening. Asked only where one would
        open; it never ends one already open, and nothing else (critical
        alerts, lockdown, freeze, the mute rules) reads it. The rules are
        face_roster.evaluate_stand_down. Every stand down is written to the
        action log first; if that fails, or anything else goes wrong, the
        alert goes ahead."""
        try:
            from . import safety_config
            if not safety_config.face_stand_down_enabled(self.config):
                return False
            from . import action_log, face_roster
            decision = face_roster.evaluate_stand_down()
            if decision.get("stand_down") is not True:
                return False
            reason = (f"{decision['reason']}. No {trigger} intrusion investigation "
                      f"was opened for motion at {where}. Opt in setting face_stand_down.")
            logged = await self.hass.async_add_executor_job(
                lambda: action_log.start(
                    action_log.new_request_id(), "intrusion_face_stand_down", "safety",
                    requested_by_name=decision["name"], entity_id=decision["camera_entity"],
                    execution_result="accepted", reason_code="face_stand_down",
                    reason_text=reason[:500]))
            if logged is None:
                _LOGGER.warning("intrusion: face stand down not recorded in the "
                                "action log, so the alert goes ahead")
                return False
            _LOGGER.info("intrusion: standing down (%s)", reason)
            return True
        except Exception as exc:
            _LOGGER.debug("intrusion: face stand down check failed, alerting: %s", exc)
            return False

    async def _check_intrusion(self, anyone_home: bool, sleeping: bool,
                               confined: bool = False) -> Optional[dict]:
        """Spot an entry while away, asleep or confined. One alert goes out,
        then the investigation continues silently until it is confirmed (the
        whole house and every device are alerted) or found harmless."""
        now = time.time()
        away = self._residents_away()
        # A recent "it's a false alarm" holds off new alerts.
        if _best_effort(lambda: _intrusion().is_called_off(), False):
            return None
        # During an investigation: keep watching, escalate once at most, and
        # otherwise stay quiet. This is what stops repeated motion alerts.
        if self._investigation is not None:
            return await self._investigate_step(now, away, sleeping)
        if now - self._last_intrusion_alert < _REPEAT_ALERT_GAP:
            return None
        # Armed away or on vacation, the alarm itself watches the house and
        # Nova trusts it: motion alone never opens an investigation here.
        # When the alarm goes off it says so (triggered is not armed away).
        if self._alarm_armed_away():
            return None
        motion = self._qualifying_motion(sleeping)
        if not motion:
            return None
        eid, where = motion[0]
        honorific = _m_common._live_honorific(self.hass)
        corroborate = self.config.get("intrusion_require_corroboration", True)

        if away:
            armed, entry = False, None
            if corroborate:
                armed, entry = self._alarm_armed(), self._open_entry()
                if not (armed or entry):
                    return None
            plan = ("away", "away", entry, armed,
                    "motion while away with corroborating breach (open entry or armed alarm)")
        elif confined and not sleeping:
            # Home, awake and confined (armed home or night, or a lockdown).
            # Moving about is normal, so being armed is not corroboration: it
            # takes an open way in, or an alarm armed away or on vacation.
            armed, entry = self._alarm_armed_away(), None
            if corroborate:
                # Armed home or night: only a way in opened since arming, on
                # the ground floor, counts.
                since = self._armed_home_since()
                if since is None:
                    entry = self._open_entry()
                else:
                    entry, _configured = self._ground_floor_open_entry(opened_after=since)
                if not (armed or entry):
                    return None
            plan = ("confined", "home", entry, armed,
                    "motion while confined with a corroborating breach")
        elif sleeping:
            # Asleep, it takes a real way in: an outside door or window on
            # the ground floor actually open. Someone up for water, or a pet,
            # makes no alert at all. Armed home or night, it must also have
            # opened since arming.
            entry, _configured = self._ground_floor_open_entry(
                opened_after=self._armed_home_since())
            if not entry:
                return None
            plan = ("sleeping", "asleep", entry, False,
                    "motion while asleep with a corroborating ground-floor breach")
        else:
            return None

        trigger, presence, entry, armed, reason = plan
        breach_name = self._friendly(entry) if entry else None
        if await self._face_stand_down(trigger, where):
            return None
        self._last_intrusion_alert = now
        return self._begin_investigation(
            now=now, trigger=trigger, presence=presence, breach=entry,
            breach_name=breach_name, armed=armed, eid=eid, where=where,
            honorific=honorific, reason=reason)

    async def _confirm_person_with_vision(self, cam_entity: str) -> Optional[bool]:
        """A second opinion on a camera's person detection. Frigate's person
        sensor is a good trigger but is fooled by shadows, headlights,
        reflections and pets, so before escalating Nova's own vision model
        looks at a frame. True: a person is there. False: nobody (a false
        detection). None: it could not tell or could not run, and the old
        behaviour stands, so a broken vision path never hides a real alert."""
        if not cam_entity or not str(cam_entity).startswith("camera."):
            return None
        if not self.config.get("intrusion_vision_confirm", True):
            return None   # switched off: camera detection alone
        try:
            from .camera import _FakeCall, async_analyze_camera
            honorific = _m_common._live_honorific(self.hass)
            # A yes or no question, not a scene description: kept out of scene
            # memory, or "PERSON: NO" would be stored as a sighting.
            call = _FakeCall({"entity_id": cam_entity, "prompt": _VISION_PROMPT,
                              "announce": False, "record_scene": False})
            client = getattr(self, "groq_client", None) or getattr(self, "_groq", None)
            result = await async_analyze_camera(self.hass, call, client, honorific, None, [],
                                                gate_announce=True)
            text = ""
            if isinstance(result, dict):
                text = (result.get("analysis") or result.get("description")
                        or result.get("message") or "")
            text = str(text).lower()
            return _vision_verdict(text) if text else None
        except Exception as exc:
            _LOGGER.debug("intrusion: vision confirm failed: %s", exc)
            return None

    # ── intrusion: one step of an open investigation ────────────────────────

    async def camera_answer(self, entity_id: str) -> dict:
        """What the camera for this sensor's room shows, for the alarm going
        off (core_alarm.py, 8.83.0): {"camera", "vision", "snapshot_url",
        "snapshot_path"}, or {} with no camera for that room. vision is True
        (a person), False (nobody) or None (could not tell)."""
        camera = self._camera_for({"breach_area": self._breach_area(entity_id)})
        if not camera:
            return {}
        vision = await self._confirm_person_with_vision(camera)
        snap = await self._snapshot(camera)
        holder: dict = {}
        await self._attach_snapshot(holder, snap)
        return {"camera": camera, "vision": vision,
                "snapshot_url": holder.get("snapshot_url"),
                "snapshot_path": holder.get("snapshot_path")}

    async def _investigate_step(self, now: float, away: bool, sleeping: bool) -> Optional[dict]:
        """One tick of an open investigation: confirm, clear, or keep watching
        quietly. It escalates only when activity forms a route inward from the
        way in, or a camera confirms a person; motion unrelated to the entry is
        watched, not concluded. Returns an alert only when it escalates."""
        inv = self._investigation
        # Stand down once the situation that opened it is over: the residents
        # came home (away), or the house woke (sleeping). Old investigations
        # have no trigger and count as away. Confinement ending is handled by
        # the tick, which drops the investigation.
        trigger = inv.get("trigger", "away")
        if trigger != "confined" and not (away if trigger == "away" else sleeping):
            self._investigation = None
            return None
        active = self._follow_motion(inv, now, sleeping)
        camera = self._camera_for(inv)
        confirmed, reason, vision = await self._judge(inv, active, now, camera, sleeping)
        # "It's a false alarm": stand down, never escalate.
        if _best_effort(lambda: _intrusion().is_called_off(), False):
            self._investigation = None
            return None
        quiet_for = now - inv["last_motion"]
        soft_done = inv.get("soft_notice", False)
        if not inv["escalated"] and confirmed:
            return await self._escalate(inv, reason, camera)
        if not inv["escalated"] and not soft_done and self._went_unanswered(inv, now, quiet_for):
            return await self._soft_notice(inv, camera, self._residents_home_guard(sleeping))
        self._maybe_close(inv, now, quiet_for, soft_done)
        return None

    def _follow_motion(self, inv: dict, now: float, sleeping: bool) -> set:
        """Add the zones moving now to the investigation's route, and note how
        far inward from the way in motion has reached. A real intruder moves
        from the entry to rooms further in; motion lingering at the entry
        never raises the depth."""
        active = {self._motion_key(eid) for eid, _ in self._qualifying_motion(sleeping)}
        if not active:
            return active
        inv["last_motion"] = now
        for zone in active:
            if zone not in inv["zones"]:
                inv["path"].append(zone)
        inv["zones"] |= active
        hops = inv.get("hops") or {}
        for zone in active:
            depth = hops.get(zone)
            if depth is not None and depth > inv.get("max_depth", 0):
                inv["max_depth"] = depth
        return active

    def _camera_for(self, inv: dict) -> Optional[str]:
        """The camera to judge with: preferably one whose saved coverage sees
        the breach area (a camera next door with a sightline counts), else
        any indoor camera seeing a person now."""
        camera = None
        try:
            breach_area = inv.get("breach_area") if isinstance(inv, dict) else None
            if breach_area:
                from . import camera_coverage
                camera = camera_coverage.camera_for_area(self.hass, breach_area)
        except Exception:
            camera = None
        return camera or self._person_camera_entity()

    async def _judge(self, inv: dict, active: set, now: float, camera: Optional[str],
                     sleeping: bool) -> tuple:
        """(confirmed, reason, vision verdict) for this step."""
        spread = len(inv["zones"]) >= self.config.get("intrusion_spread_zones",
                                                        INTRUSION_SPREAD_ZONES)
        sustained = bool(active) and now - inv["start"] >= INTRUSION_SUSTAINED_SECS
        # Inward progress: motion must reach a set number of rooms in from the
        # way in. An air conditioner in an open window, or a curtain, never
        # gets there. Without a room map, fall back to spread near the breach,
        # so a house with no floor plan is still protected.
        need = _setting(self.config, "intrusion_inward_depth", INTRUSION_INWARD_DEPTH, int)
        if inv.get("hops") or {}:
            inward = inv.get("max_depth", 0) >= need
        else:
            inward = spread and bool(inv["zones"] & (inv.get("connected") or set()))
        while_out = " while no one is home" if self._residents_away() else ""
        route = (inward, "someone is moving inward through the house from the point of entry")
        movement = (spread and sustained, f"sustained movement through the house{while_out}")
        fallback = route if inv.get("breach_area") else movement

        vision = None
        if camera is not None:
            # A camera saw a person, but cameras are fooled, so Nova's own
            # vision looks first.
            vision = await self._confirm_person_with_vision(camera)
            if vision is True:
                confirmed, reason = True, "a person is on camera (confirmed by vision)"
            elif vision is False:
                # The camera said person, vision says nobody: a false
                # detection, so a real route through the house is needed.
                _LOGGER.info("intrusion: Frigate person on %s NOT confirmed by "
                             "vision — requiring inward motion route", camera)
                confirmed, reason = fallback
            else:
                # Vision could not tell: trust the camera, so a broken vision
                # path never hides a real alert.
                confirmed, reason = True, "a person is on camera"
        else:
            # A known way in needs a route inward; with none, sustained motion
            # across several rooms rather than a passing blip.
            confirmed, reason = fallback

        # Residents home and armed for it, or asleep: a resident walking room
        # to room is exactly what a route inward looks like, so that alone
        # never confirms. It takes the alarm going off, or a person vision has
        # confirmed; an unsure vision check does not count here.
        guard = self._residents_home_guard(sleeping)
        if confirmed and guard and vision is not True:
            confirmed = False
        if not confirmed and guard and self._alarm_triggered():
            confirmed, reason = True, "the alarm has gone off while residents are home"
        return confirmed, reason, vision

    def _went_unanswered(self, inv: dict, now: float, quiet_for: float) -> bool:
        """The first alert went unanswered (neither acknowledged nor called
        off) for the response window while something is still moving. Motion
        that has stopped still clears as harmless instead; only an ongoing
        situation fails toward alerting. An acknowledgement holds it."""
        acknowledged = _best_effort(lambda: _intrusion().is_acknowledged(), False)
        window = _setting(self.config, "intrusion_response_timeout",
                          INTRUSION_RESPONSE_TIMEOUT_SECS, float)
        return (not acknowledged and quiet_for < INTRUSION_CLEAR_QUIET_SECS
                and now - inv["start"] >= window)

    async def _snapshot(self, camera: Optional[str]):
        if not camera:
            return None
        try:
            return await _intrusion().capture_snapshot(self.hass, camera)
        except Exception:
            return None

    async def _attach_snapshot(self, action: dict, snap) -> Optional[str]:
        """Put a snapshot on an alert; returns the notification image url."""
        if not snap:
            return None
        try:
            url = await _intrusion().get_notification_image_url(self.hass, snap.get("path"))
        except Exception:
            url = None
        action["snapshot_url"] = url
        action["snapshot_path"] = snap.get("path")
        action["camera"] = snap.get("camera")
        return url

    def _announce_event(self, name: str, reason: str, url, snap) -> None:
        snap = snap or {}
        _best_effort(lambda: self.hass.bus.async_fire(name, {
            "reason": reason, "snapshot_url": url,
            "snapshot_path": snap.get("path"), "camera": snap.get("camera")}))

    def _log_outcome(self, kind: str, reason: str, inv: dict, camera, snap) -> None:
        _best_effort(lambda: _intrusion().record_event(
            kind, reason=reason, breach=inv.get("breach_name"),
            breach_area=inv.get("breach_area"), camera=camera, snapshot=snap,
            zones=sorted(inv.get("zones") or []), max_depth=inv.get("max_depth")))

    async def _escalate(self, inv: dict, reason: str, camera: Optional[str]) -> dict:
        """The confirmed alarm, to the whole house and every device, once. A
        confirmed intrusion is never damped by learning."""
        inv["escalated"] = True
        honorific = _m_common._live_honorific(self.hass)
        snap = await self._snapshot(camera)
        note = " A snapshot is available." if snap else ""
        action = {
            "type": "intrusion_confirmed", "urgency": "critical",
            "message": _persona().lead_in(
                honorific, f"intrusion confirmed — {reason}.{note} Alerting the house "
                           f"and every device. Say 'it's a false alarm' to call it off."),
            "auto_act": True, "notify_all": True, "can_dismiss": True,
        }
        url = await self._attach_snapshot(action, snap)
        self._announce_event("nova_intrusion_confirmed", reason, url, snap)
        self._log_outcome("confirmed", reason, inv, camera, snap)
        return action

    async def _soft_notice(self, inv: dict, camera: Optional[str], guard: bool) -> Optional[dict]:
        """Unanswered, something still moving, but no confirmed route: rather
        than claim "intrusion confirmed", a softer "please check". Only the
        soft notice is marked done, so a route or a person confirmed later
        still raises the critical alert, once. It is a weak alert, so a place
        and time learned to be harmless stays quiet."""
        inv["soft_notice"] = True
        honorific = _m_common._live_honorific(self.hass)
        damped = _best_effort(
            lambda: _intrusion().should_damp_weak_alert(inv.get("breach_area"), camera), False)
        snap = await self._snapshot(camera)
        why = "no response; unconfirmed activity"
        self._log_outcome("unresolved", "damped (learned benign)" if damped else why,
                          inv, camera, snap)
        if damped:
            _LOGGER.info("intrusion: unresolved alert damped for "
                         "learned-benign pattern at %s", inv.get("breach_area"))
            return None
        note = " A snapshot is available." if snap else ""
        where = inv.get("breach_name") or "the point of entry"
        action = {
            "type": "intrusion_unresolved", "urgency": "high",
            "message": _persona().lead_in(
                honorific, f"I flagged possible activity near {where} and couldn't reach "
                           f"you, but I have not confirmed anyone moving through the "
                           f"house.{note} Please check when you can — say 'it's a false "
                           f"alarm' to clear it."),
            "auto_act": True, "notify_all": True, "can_dismiss": True,
        }
        if guard:
            action["phone_only"] = True   # residents home: phones only, as at first
        url = await self._attach_snapshot(action, snap)
        self._announce_event("nova_intrusion_unresolved", why, url, snap)
        return action

    def _maybe_close(self, inv: dict, now: float, quiet_for: float, soft_done: bool) -> None:
        """Close the investigation: harmless when nothing came of it and it
        went quiet or ran too long; settled when an alert went out and it has
        been quiet since."""
        alerted = inv["escalated"] or soft_done
        if not alerted and (quiet_for > INTRUSION_CLEAR_QUIET_SECS
                            or now - inv["start"] > INTRUSION_MAX_INVESTIGATE_SECS):
            self._investigation = None
        elif alerted and quiet_for > INTRUSION_CLEAR_QUIET_SECS:
            self._investigation = None

    # ── the night sweep ─────────────────────────────────────────────────────

    async def _sweep_and_report(self, automatic_generation: int) -> None:
        """Run the night sweep and deliver what it says as a sleeping house's
        alert (phones only below critical). Never raises."""
        try:
            from .core_bridge import _emit_action
            for action in await self._nighttime_lockdown(automatic_generation):
                await _emit_action(self.hass, self.config, action, True)
        except Exception as exc:
            _LOGGER.warning("Safety tick: nighttime lockdown failed: %s", exc)

    def _sweep_memory(self, now: float) -> dict:
        """Tonight's sweep memory, started afresh at a new night's first
        sweep. A night runs noon to noon, so one night's sleep is one night."""
        night = datetime.datetime.fromtimestamp(now - 12 * 3600).date().toordinal()
        if self._sweep_night.get("night") != night:
            self._sweep_night = {"night": night, "secured": set(), "left_alone": {},
                                 "commands": []}
        return self._sweep_night

    async def _nighttime_lockdown(self, automatic_generation: int) -> list[dict]:
        """Lock what is unlocked and close what is open while the house
        sleeps, then check it took.

        Nova never fights a person at the door, and the sweep follows that
        too: anything it could not secure, or that was opened again after it
        secured it, is left alone until morning and named once. An hourly
        cap on commands is the backstop. Returns the report, or [] when the
        automatic lockdown was switched off part way."""
        from . import action_log
        from .core_lockdown import unlocked_locks
        title = _persona().capitalise(_m_common._live_honorific(self.hass))
        now = time.time()
        memory = self._sweep_memory(now)
        request_id = action_log.new_request_id()
        # Locks are chosen as the formal lockdown chooses them. Every open
        # cover is closed whatever its class, unlike the formal lockdown,
        # which closes only doors, garages, windows and gates; kept so on
        # purpose.
        locks = unlocked_locks(self.hass, _lockdown_exempt_locks())
        covers = [s for s in self.hass.states.async_all("cover") if s.state == "open"]
        locks, covers, reopened = self._tonights_targets(memory, locks, covers)
        locks, covers = self._within_hourly_cap(memory, now, locks, covers)
        planned = ([(s.entity_id, "lock", "lock") for s in locks]
                   + [(s.entity_id, "cover", "close_cover") for s in covers])
        rows = await self.hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, "lockdown", "safety_routine",
                [{"key": eid, "domain": domain, "service": service, "entity_id": eid}
                 for eid, domain, service in planned])) if planned else {}

        sweep = {"locked": [], "closed": [], "failed": [], "failed_eids": [], "sent": []}
        for states, domain in ((locks, "lock"), (covers, "cover")):
            if not await self._secure_all(states, domain, rows, sweep, automatic_generation):
                return []
        if not self._automatic_operation_current(automatic_generation):
            return []
        not_secured: list = []
        if sweep["sent"]:
            # After a short wait each lock must read locked and each cover
            # closed; only those count as secured, so the message never says
            # "secured" before checking.
            await asyncio.sleep(self.sweep_verify_delay)
            if not self._automatic_operation_current(automatic_generation):
                return []
            not_secured = await self._verify(sweep, memory)
            sweep["failed"] = not_secured + sweep["failed"]
        # One try a night: what did not take is left alone until morning, so
        # it is never commanded or named again tonight.
        for eid in sweep["failed_eids"]:
            memory["left_alone"].setdefault(eid, "did not secure")
        if planned or reopened:
            await self._record_sweep(sweep, not_secured, reopened, request_id if planned else None)
        return self._sweep_report(sweep, reopened, title)

    @staticmethod
    def _tonights_targets(memory: dict, locks: list, covers: list) -> tuple:
        """Leave out what the sweep already left alone tonight. Something it
        secured earlier tonight that is open or unlocked again was opened on
        purpose: it is left alone too, and named once."""
        left_alone = memory["left_alone"]
        reopened: list = []

        def keep(state) -> bool:
            eid = state.entity_id
            if eid in left_alone:
                return False
            if eid in memory["secured"]:
                left_alone[eid] = "opened again after the sweep secured it"
                reopened.append(state.attributes.get("friendly_name", eid))
                return False
            return True
        return [s for s in locks if keep(s)], [s for s in covers if keep(s)], reopened

    @staticmethod
    def _within_hourly_cap(memory: dict, now: float, locks: list, covers: list) -> tuple:
        """Trim to what the hourly command cap allows; the rest waits for a
        later sweep. The commands about to be sent are counted."""
        memory["commands"] = [t for t in memory["commands"] if now - t < 3600]
        room = max(0, SWEEP_MAX_COMMANDS_PER_HOUR - len(memory["commands"]))
        wanted = len(locks) + len(covers)
        if wanted > room:
            _LOGGER.warning("Cognitive lockdown: hourly command cap reached; "
                            "%d device(s) wait for a later sweep", wanted - room)
            locks = locks[:room]
            covers = covers[:max(0, room - len(locks))]
        memory["commands"].extend([now] * (len(locks) + len(covers)))
        return locks, covers

    async def _log_row(self, row_id, result: str, code: Optional[str] = None) -> None:
        from . import action_log
        if code is None:
            await self.hass.async_add_executor_job(
                lambda: action_log.set_execution(row_id, result))
        else:
            await self.hass.async_add_executor_job(
                lambda: action_log.set_execution(row_id, result, reason_code=code))

    async def _secure_all(self, states: list, domain: str, rows: dict, sweep: dict,
                          generation: int) -> bool:
        """Lock (or close) each one. False when the automatic lockdown was
        switched off part way, which abandons the sweep."""
        from .core_lockdown import NotSecured, secure_device
        wanted = "unlocked" if domain == "lock" else "open"
        done_list = sweep["locked"] if domain == "lock" else sweep["closed"]
        verb = "lock" if domain == "lock" else "close"
        for state in states:
            if not self._automatic_operation_current(generation):
                return False
            if state.state != wanted:
                continue
            eid = state.entity_id
            name = state.attributes.get("friendly_name", eid)
            row_id = rows.get(eid)
            try:
                result = await secure_device(self.hass, eid, domain)
                if not self._automatic_operation_current(generation):
                    return False
                if result == "already_secure":
                    # Secured since the list was made: nothing was sent.
                    await self._log_row(row_id, "blocked", "already_secure")
                    continue
                done_list.append(name)
                sweep["sent"].append((eid, name, domain, row_id))
                _LOGGER.info("Cognitive lockdown: %s %s", "locked" if domain == "lock"
                             else "closed", eid)
                await self._log_row(row_id, "accepted")
            except Exception as exc:
                # Skipped by the live check (its door is open, or it cannot
                # be read) or the command failed: named as not secured.
                skipped = isinstance(exc, NotSecured)
                _LOGGER.warning("Cognitive lockdown: %s %s: %s",
                                "skipped" if skipped else f"failed to {verb}", eid, exc)
                sweep["failed"].append(name)
                sweep["failed_eids"].append(eid)
                await self._log_row(row_id, "blocked" if skipped else "failed",
                                    exc.code if skipped else "service_call_failed")
        return True

    async def _verify(self, sweep: dict, memory: dict) -> list:
        """Read back everything a command was sent to. Returns the names that
        did not read secured; the locked and closed lists keep only what did."""
        from . import action_log, entity_verify
        sweep["locked"], sweep["closed"] = [], []
        not_secured: list = []
        for eid, name, domain, row_id in sweep["sent"]:
            secured = entity_verify.check_state_once(
                self.hass, eid, "locked" if domain == "lock" else "closed")
            if secured:
                (sweep["locked"] if domain == "lock" else sweep["closed"]).append(name)
                memory["secured"].add(eid)
            else:
                not_secured.append(name)
                sweep["failed_eids"].append(eid)
            if row_id is not None:
                await self.hass.async_add_executor_job(
                    lambda rid=row_id, ok=secured: action_log.set_execution(
                        rid, "verified" if ok else "unverified",
                        reason_code=None if ok else "not_secure_after_check"))
        return not_secured

    async def _record_sweep(self, sweep: dict, not_secured: list, reopened: list,
                            request_id) -> None:
        """The sweep's decision, in the shared safety format, written after
        the check so it says what was confirmed."""
        from . import alert_path
        failed = sweep["failed"]
        await alert_path.async_record_decision(
            self.hass, "lockdown_sweep", source="nighttime_sweep",
            sit=alert_path.situation(self.hass, self.config, sleeping=True,
                                     house=self._household()),
            facts={"locked": sweep["locked"], "closed": sweep["closed"],
                   "not_secured_after_check": not_secured,
                   "failed": [name for name in failed if name not in not_secured],
                   "left_alone_reopened": reopened},
            request_ids=[request_id] if request_id else None,
            assessment="household asleep with locks unlocked or covers open",
            decision="lock and close, checked" + (", some not secured" if failed else ""),
            reason="automatic lockdown at night")

    def _sweep_report(self, sweep: dict, reopened: list, title: str) -> list[dict]:
        """The sweep's message, or nothing when it had nothing to say. "The
        house is secured" is said only when nothing failed and nothing was
        left alone. It reports what Nova changed, so it always reaches the
        phones, at night too; trouble is high urgency."""
        locked, closed, failed = sweep["locked"], sweep["closed"], sweep["failed"]
        if not (locked or closed or failed or reopened):
            return []
        i18n = _notify_i18n()
        lang = _m_common._hass_lang(self.hass)
        done = []
        if locked:
            done.append(i18n.message("lockdown_locked", lang, names=i18n.join_names(locked, lang)))
        if closed:
            done.append(i18n.message("lockdown_closed", lang, names=i18n.join_names(closed, lang)))
        if not failed and not reopened:
            message = i18n.message("lockdown_nighttime", lang, honorific=title,
                                   body=i18n.join_names(done, lang))
        else:
            trouble = []
            if failed:
                trouble.append(i18n.message("lockdown_secure_failed", lang,
                                            names=i18n.join_names(failed, lang)))
            if reopened:
                trouble.append(i18n.message("lockdown_left_alone", lang,
                                            names=i18n.join_names(reopened, lang)))
            if done:
                message = i18n.message("lockdown_nighttime_partial", lang, honorific=title,
                                       body=i18n.join_names(done, lang),
                                       failed="; ".join(trouble))
            else:
                message = i18n.message("lockdown_nighttime_failed_only", lang,
                                       honorific=title, failed="; ".join(trouble))
        return [{"type": "lockdown", "urgency": "high" if (failed or reopened) else "low",
                 "message": message, "auto_act": True, "reports_change": True}]
