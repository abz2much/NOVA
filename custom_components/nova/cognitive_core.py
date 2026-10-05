"""
Nova — Cognitive Core (v5.8.03).

The autonomous AI brain. Runs continuously in the background,
monitoring home state, managing safety, learning patterns, and making
decisions on the household's behalf as Nova's always-on steward.

Architecture:
  - 30-second evaluation loop: reviews full home state each tick
  - Safety manager: pipe freeze prevention, unauthorized entry,
    nighttime lockdown
  - Ignore system: honors "ignore X for Y duration" commands
  - Outdoor event filter: only surfaces notable events
  - State logger: records every meaningful state change for
    pattern learning (separate module)
  - Suggestion engine: proposes automations based on observed patterns

Philosophy:
  - Suggest, don't act (initially) — earn trust first
  - Pipe freeze, intrusion: alert immediately and recommend action — Nova
    does not itself adjust the thermostat or otherwise act on these (fixed
    Sept 2026: the code used to claim it did)
  - Nighttime lockdown: locks/doors → act automatically (this one genuinely
    does act, via LockdownManager)
  - Everything else: observe, learn, suggest
  - Approved suggestions become automations over time

Where the code lives (8.7.17). This module is the public compatibility
facade, like agent.py. The implementation is in sibling modules:
  - core_autonomy.py: graduated autonomy
  - core_bridge.py: call time lookups that avoid import cycles
  - core_common.py: constants and shared helpers
  - core_ignore.py: ignore rules and the outdoor filter
  - core_pattern_store.py: StateLogger

This module keeps every name that production code and tests import from
cognitive_core as the very same object, and keeps each one patchable here:
setting (or deleting) a moved name on this module sets it on the module that
owns it too, and reading it reads the owner's current value. Nothing is
copied: there is one definition and one value of each. The persisted paths
(LOCKDOWN_STATE_PATH, IGNORE_FILE, AUTONOMY_FILE, PATTERNS_DB and their
helpers) stay defined here: they are the storage identity of the cognitive
core (tests/fixtures/contracts/storage.json).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.util import dt as dt_util

from .persistence import sqlite as _store
from . import paths
from . import core_autonomy as _m_autonomy
from . import core_common as _m_common
from . import core_ignore as _m_ignore
from . import core_pattern_store as _m_pattern_store
from .core_autonomy import AutonomyManager
from .core_common import (
    ALARM_ARMED_STATES,
    AUTONOMY_MIN_CONFIDENCE,
    AUTONOMY_TRUST_THRESHOLD,
    DARK_LUX_THRESHOLD,
    FREEZE_CRITICAL_TEMP_F,
    FREEZE_WARN_TEMP_F,
    HIGH_TEMP_AWAY_F,
    INTRUSION_CLEAR_QUIET_SECS,
    INTRUSION_INWARD_DEPTH,
    INTRUSION_MAX_INVESTIGATE_SECS,
    INTRUSION_RESPONSE_TIMEOUT_SECS,
    INTRUSION_SPREAD_ZONES,
    INTRUSION_SUSTAINED_SECS,
    LOCKDOWN_BREACH_COOLDOWN,
    LOCKDOWN_CHECK_INTERVAL,
    LOCKDOWN_DOOR_COVER_CLASSES,
    LOCKDOWN_EXEMPT_LOCKS_DEFAULT,
    LOCKDOWN_SECURE_VERIFY_DELAY,
    LOW_TEMP_AWAY_F,
    PROACTIVE_CHECK_INTERVAL,
    PROACTIVE_OFFER_COOLDOWN,
    STALE_LIGHT_MINUTES,
    TICK_INTERVAL,
    _f_to_unit,
    _fmt_temp,
    _hass_lang,
    _live_honorific,
    _notify_i18n,
    _persona,
    _temp_to_f,
    discover_outdoor_temp,
    write_json_atomic,
)
from .core_ignore import IgnoreManager, IgnoreRule, is_outdoor_notable
from .core_pattern_store import StateLogger

_LOGGER = logging.getLogger(__name__)
LOCKDOWN_STATE_PATH: Optional[str] = None  # override; None resolves via paths.py; survives reboots/reloads


def _lockdown_state_path() -> str:
    return LOCKDOWN_STATE_PATH or paths.nova_path("lockdown_state.json")
IGNORE_FILE: Optional[str] = None  # override; None resolves via paths.py


def _ignore_file() -> str:
    return IGNORE_FILE or paths.config_path(".nova_ignore_rules.json")
def _offer_area(hass, offer):
    """Best-effort area for a proactive offer, from its action target entity.
    Used to scope a room-bound mode's quiet to just that room. None if unknown."""
    try:
        ad = (offer.get("action_data") or {}) if isinstance(offer, dict) else {}
        eid = ad.get("entity_id") or offer.get("entity_id")
        if isinstance(eid, (list, tuple)):
            eid = eid[0] if eid else None
        if not eid:
            return None
        from . import audio_routing
        return audio_routing.entity_area(hass, eid)
    except Exception:
        return None
AUTONOMY_FILE: Optional[str] = None  # override; None resolves via paths.py


def _autonomy_file() -> str:
    return AUTONOMY_FILE or paths.nova_path("autonomy_grants.json")


PATTERNS_DB: Optional[str] = None  # override; None resolves via paths.py; learned patterns and the cognition model


def _patterns_db() -> str:
    return PATTERNS_DB or paths.patterns_db()


# ── Safety Manager ──────────────────────────────────────────────────────────

class SafetyManager:
    """Monitors for safety-critical conditions and acts."""

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self._last_freeze_alert = 0.0
        self._last_lockdown_check = 0.0
        self._last_intrusion_alert = 0.0
        self._investigation = None   # active intrusion investigation, or None
        self._freeze_warned = False
        self._automatic_generation = 0

    def set_automatic_lockdown(self, enabled: bool) -> None:
        self.config["lockdown_auto_on_arm"] = enabled is True
        self._automatic_generation += 1

    def _automatic_operation_current(self, generation: int) -> bool:
        from . import safety_config
        return (generation == self._automatic_generation
                and safety_config.automatic_lockdown_enabled(self.config))

    async def tick(self, sleeping: bool, anyone_home: bool) -> list[dict]:
        """Run all safety checks. Returns list of actions taken.

        Each stage (freeze, intrusion, nighttime sweep) is guarded on its own:
        an error in one is logged and the others still run, and anything an
        earlier stage already gathered is still returned, so a fault in the
        safety code can never swallow an alert that was already raised."""
        actions = []
        now = time.time()

        # ── Pipe freeze prevention ──────────────────────────────────
        try:
            freeze_action = await self._check_freeze()
            if freeze_action:
                actions.append(freeze_action)
        except Exception as exc:
            _LOGGER.warning("Safety tick: freeze check failed: %s", exc)

        # ── Unauthorized entry detection ────────────────────────────
        # Only when residents are CONFIDENTLY away (tracked away / armed-away) or
        # asleep — never on the mere absence of occupancy, which falsely fires when
        # someone is home but untracked.
        # Opt in (intrusion_requires_confinement): confinement is the master
        # switch instead. Monitoring runs only while a formal lockdown is
        # engaged or the selected alarm is armed, and ending confinement stops
        # it at once, dropping any investigation in progress.
        try:
            from . import safety_config as _sc
            if _sc.intrusion_requires_confinement(self.config):
                if is_lockdown() or self._alarm_armed():
                    intrusion = await self._check_intrusion(
                        anyone_home, sleeping, confined=True)
                    if intrusion:
                        actions.append(intrusion)
                else:
                    self._investigation = None
            else:
                # The setting was turned off mid investigation: drop one that only
                # existed because of confinement.
                if (self._investigation is not None
                        and self._investigation.get("trigger") == "confined"):
                    self._investigation = None
                if self._residents_away() or sleeping or self._investigation is not None:
                    intrusion = await self._check_intrusion(anyone_home, sleeping)
                    if intrusion:
                        actions.append(intrusion)
        except Exception as exc:
            _LOGGER.warning("Safety tick: intrusion check failed: %s", exc)

        # ── Nighttime lockdown ──────────────────────────────────────
        # Skipped when a formal lockdown is already active (it handles securing).
        try:
            from . import safety_config
            if (safety_config.automatic_lockdown_enabled(self.config)
                    and sleeping and not is_lockdown()
                    and (now - self._last_lockdown_check) > LOCKDOWN_CHECK_INTERVAL):
                self._last_lockdown_check = now
                generation = self._automatic_generation
                lockdown = await self._nighttime_lockdown(generation)
                if lockdown:
                    actions.extend(lockdown)
        except Exception as exc:
            _LOGGER.warning("Safety tick: nighttime lockdown failed: %s", exc)

        return actions

    async def _check_freeze(self) -> Optional[dict]:
        """Monitor outdoor temperature for pipe freeze risk.

        Unit-aware: the value read is in Home Assistant's configured unit (°C on
        a metric install), so it is converted to °F for the threshold comparison
        and the message is rendered in the user's own unit. A metric home no
        longer false-fires a freeze warning on a mild 18°C day.
        """
        now = time.time()
        if (now - self._last_freeze_alert) < 3600:  # 1hr cooldown
            return None

        found = discover_outdoor_temp(self.hass)
        if found is None:
            return None
        outdoor_temp, unit = found

        temp_f = _temp_to_f(outdoor_temp, unit)
        reading = _fmt_temp(outdoor_temp, unit)
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware
        lang = _hass_lang(self.hass)

        if temp_f <= FREEZE_CRITICAL_TEMP_F:
            self._last_freeze_alert = now
            set_to = _fmt_temp(_f_to_unit(55, unit), unit, decimals=0)
            return {
                "type": "freeze_critical",
                "urgency": "critical",
                "message": _notify_i18n().message(
                    "freeze_critical", lang, honorific=honorific.title(),
                    reading=reading, set_to=set_to),
                # Alert-only (fixed Sept 2026): this said auto_act=True and
                # "act without approval", but nothing has ever consulted
                # auto_act to actually do anything — Nova has no thermostat/
                # plumbing actuator tied to this. The message itself already
                # phrases it as a recommendation, which is the truth.
                "auto_act": False,
            }
        elif temp_f <= FREEZE_WARN_TEMP_F and not self._freeze_warned:
            self._freeze_warned = True
            self._last_freeze_alert = now
            return {
                "type": "freeze_warning",
                "urgency": "high",
                "message": _notify_i18n().message(
                    "freeze_warning", lang, honorific=honorific.title(),
                    reading=reading),
                "auto_act": False,
            }
        elif temp_f > FREEZE_WARN_TEMP_F + 5:
            self._freeze_warned = False

        return None

    def _alarm_armed(self) -> bool:
        from . import alarm_source
        for st in alarm_source.states(self.hass, self.config):
            if st.state in ALARM_ARMED_STATES:
                return True
        return False

    def _alarm_armed_away(self) -> bool:
        """True only for the armed states that mean nobody is meant to be
        moving about (away or vacation), not home or night."""
        from . import alarm_source
        return any(st.state in ("armed_away", "armed_vacation")
                   for st in alarm_source.states(self.hass, self.config))

    def _friendly(self, eid: Optional[str]) -> Optional[str]:
        if not eid:
            return None
        st = self.hass.states.get(eid)
        return ((st.attributes.get("friendly_name") if st else None) or eid)

    def _open_entry(self, areas: Optional[Iterable[str]] = None) -> Optional[str]:
        """The entity_id of an exterior door/window that's currently open — the
        breach point a real entry would come through. None if all are shut.
        Property-perimeter openings (a driveway or side gate, a shed door) are
        excluded: they aren't the house envelope, and an open yard gate must not
        turn a curtain-flutter into a corroborated intrusion. The garage IS
        envelope, so anything garage-named stays in.

        `areas`, when given, restricts qualifying entries to that set of HA
        area_ids (used to scope the sleeping-household check to the ground
        floor — see `_ground_floor_open_entry`)."""
        from . import outdoor

        area_filter = set(areas) if areas else None

        def _envelope(st) -> bool:
            fname = st.attributes.get("friendly_name") or ""
            if "garage" in (st.entity_id + " " + fname).lower():
                return True
            return not outdoor.is_outdoor(self.hass, st.entity_id, fname)

        def _in_scope(st) -> bool:
            return area_filter is None or self._breach_area(st.entity_id) in area_filter

        for st in self.hass.states.async_all("binary_sensor"):
            if (st.attributes.get("device_class") in ("door", "window", "garage_door", "opening")
                    and st.state == "on" and _envelope(st) and _in_scope(st)):
                return st.entity_id
        for st in self.hass.states.async_all("cover"):
            if (st.attributes.get("device_class") in ("door", "garage", "garage_door", "gate")
                    and st.state in ("open", "opening") and _envelope(st) and _in_scope(st)):
                return st.entity_id
        return None

    def _ground_floor_open_entry(self) -> tuple[Optional[str], bool]:
        """Open exterior door/window, scoped to `ground_floor_areas` when the
        user has configured any (v7.86.0). Returns (entity_id_or_None,
        configured) — `configured` is False when ground_floor_areas is empty,
        in which case every exterior door/window on every floor qualifies.
        Unconfigured must never mean LESS protection than before."""
        ground_areas = self.config.get("ground_floor_areas") or []
        if not ground_areas:
            return self._open_entry(), False
        return self._open_entry(areas=ground_areas), True

    def _breach_area(self, entry_eid: Optional[str]) -> Optional[str]:
        """The HA area of the breach entity — where the intruder would enter."""
        if not entry_eid:
            return None
        try:
            from . import audio_routing
            return audio_routing.entity_area(self.hass, entry_eid)
        except Exception:
            return None

    def _motion_key(self, eid: str) -> str:
        """A stable 'zone' key for a motion sensor — its area if resolvable,
        else the entity id. Distinct zones ⇒ movement across the home."""
        try:
            from homeassistant.helpers import (
                entity_registry as er, device_registry as dr,
            )
            ent = er.async_get(self.hass).async_get(eid)
            if ent:
                area = ent.area_id
                if not area and ent.device_id:
                    dev = dr.async_get(self.hass).async_get(ent.device_id)
                    area = dev.area_id if dev else None
                if area:
                    return area
        except Exception:
            pass
        return eid

    def _qualifying_motion(self, sleeping: bool) -> list:
        """Active indoor motion sensors worth considering — skips outdoor
        sensors and (while asleep) bedroom sensors. Returns [(entity_id, name)]."""
        from . import outdoor
        out = []
        bedroom_areas = self.config.get("bedroom_areas", []) if sleeping else []
        for state in self.hass.states.async_all("binary_sensor"):
            if state.attributes.get("device_class", "") not in ("motion", "occupancy", "presence"):
                continue
            if state.state != "on":
                continue
            eid = state.entity_id
            fname = (state.attributes.get("friendly_name") or "")
            # Outdoor motion never seeds or spreads an *indoor* intrusion — that
            # is the outdoor filter's job to surface (if notable), not ours.
            if outdoor.is_outdoor(self.hass, eid, fname):
                continue
            if sleeping and bedroom_areas and self._motion_key(eid) in bedroom_areas:
                continue
            out.append((eid, fname or eid))
        return out

    def _person_on_camera(self, indoor_only: bool = True) -> bool:
        """Best-effort: a camera reports a person right now (e.g. Frigate's
        binary_sensor.<cam>_person). With indoor_only (the default, and what
        intrusion confirmation uses), OUTDOOR cameras are excluded — a delivery
        driver on the driveway cam is a doorstep event, not proof someone is
        inside the house."""
        return self._person_camera_entity(indoor_only) is not None

    def _person_camera_entity(self, indoor_only: bool = True) -> Optional[str]:
        """Like _person_on_camera, but returns the CAMERA entity_id backing the
        person-detecting binary_sensor (so we can snapshot it), or None. Maps
        binary_sensor.<cam>_person → camera.<cam> when such a camera exists,
        else returns the sensor's entity_id as a fallback handle."""
        from . import outdoor
        for st in self.hass.states.async_all("binary_sensor"):
            if st.state != "on":
                continue
            fname = st.attributes.get("friendly_name") or ""
            low = (st.entity_id + " " + fname).lower()
            if "person" not in low:
                continue
            if st.attributes.get("device_class") not in (
                    "occupancy", "motion", "presence", None):
                continue
            if indoor_only and outdoor.is_outdoor(self.hass, st.entity_id, fname):
                continue
            # derive a camera entity from the sensor slug, e.g.
            # binary_sensor.dining_room_person → camera.dining_room
            slug = st.entity_id.split(".", 1)[-1]
            for suffix in ("_person", "_person_occupancy", "_occupancy"):
                if slug.endswith(suffix):
                    slug = slug[: -len(suffix)]
                    break
            cam = f"camera.{slug}"
            if self.hass.states.get(cam) is not None:
                return cam
            # fall back: any camera whose name shares the area word
            from .camera import active_camera_states
            for cst in active_camera_states(self.hass):
                if slug.split("_")[0] in cst.entity_id:
                    return cst.entity_id
            return cam        # best-effort handle even if not yet resolvable
        return None

    def _begin_investigation(self, *, now: float, trigger: str, presence: str,
                              breach: Optional[str], breach_name: Optional[str],
                              armed: bool, eid: str, where: str, honorific: str,
                              reason: str) -> Optional[dict]:
        """Shared investigation kickoff for both the away and sleeping
        triggers (v7.86.0): computes breach adjacency/depth, seeds
        self._investigation, logs the decision + intrusion event (with
        learned damping), and returns the 'investigating' action — or None
        if this exact location/time pattern is learned-benign."""
        breach_area = self._breach_area(breach)
        # Anchor the search at the breach: the intruder enters there, so the
        # breach room and the rooms adjacent to it are where a real entry
        # first shows up. Motion elsewhere is still investigated, but not
        # concluded to be an intrusion on its own — that discernment is what
        # avoids false alarms.
        connected = {breach_area} if breach_area else set()
        hops = {}
        try:
            from . import residence_graph
            connected |= residence_graph.adjacent_areas(
                self.hass, self.config, breach_area)
            # Depth of every room from the breach, so we can tell inward
            # (entry → deeper) motion from motion that lingers at the entry.
            hops = residence_graph.hops_from_breach(
                self.hass, self.config, breach_area)
        except Exception:
            pass
        connected.discard(None)
        start_zone = self._motion_key(eid)
        # deepest room motion has reached so far (breach itself = 0)
        start_depth = hops.get(start_zone, 0) if hops else 0
        self._investigation = {
            "start": now, "last_motion": now,
            "zones": {start_zone}, "path": [start_zone], "escalated": False,
            "soft_notice": False,
            "breach_area": breach_area, "breach_name": breach_name,
            "connected": connected,
            "hops": hops, "max_depth": start_depth,
            "trigger": trigger,
        }
        _i18n = _notify_i18n()
        _lang = _hass_lang(self.hass)
        if breach_name:
            ctx = _i18n.message("intrusion_ctx_open", _lang, name=breach_name)
        elif armed:
            ctx = _i18n.message("intrusion_ctx_armed", _lang)
        else:
            ctx = ""
        msg_key = {"away": "intrusion_alert",
                   "confined": "intrusion_alert_confined"}.get(
                       trigger, "intrusion_alert_sleep")
        msg = _i18n.message(
            msg_key, _lang, honorific=honorific.title(),
            where=where, ctx=ctx)
        # Decision Record (v7.39.0): log the proactive intrusion judgement. Best-effort;
        # a logging failure must never affect the alert.
        try:
            from . import decision_record
            _rid = decision_record.record(
                "intrusion",
                observation={"location": where, "breach": breach_name,
                             "alarm_armed": armed, "presence": presence},
                interpretation={"assessment": "possible intrusion — investigating from the point of entry"},
                decision="raise initial intrusion alert and investigate silently",
                reason=reason,
            )
            try:  # so a later call-off attaches to this exact record
                from . import intrusion as _intr_rec
                _intr_rec.set_last_decision_id(_rid)
            except Exception:
                pass
        except Exception:
            pass
        # Learned damping (v6.76.0): if this location/time pattern has been
        # repeatedly labelled a false alarm, stay QUIET on this initial
        # low-confidence ping. The investigation still runs underneath, so a
        # real inward route still confirms and alarms — learning can only
        # silence the weak alert, never a confirmed intrusion.
        damped = False
        try:
            from . import intrusion as _intr
            damped = _intr.should_damp_weak_alert(breach_area, None)
            _intr.record_event(
                "investigating", reason=("damped (learned benign)" if damped
                                         else reason),
                breach=breach_name, breach_area=breach_area,
                zones=[start_zone], max_depth=start_depth)
        except Exception:
            pass
        if damped:
            _LOGGER.info("intrusion: initial alert damped for learned-benign "
                         "pattern at %s (still investigating)", breach_area)
            return None
        return {
            "type": "intrusion_investigating", "urgency": "high",
            "message": msg, "auto_act": True, "entity_id": eid,
        }

    async def _face_stand_down(self, trigger: str, where: str) -> bool:
        """Opt in (face_stand_down): True when a recognised household resident
        should stop a NEW intrusion investigation from opening. Called only
        where an investigation would be opened; it can never end one that is
        already open, and nothing else (critical alerts, lockdown, freeze, the
        mute rules) reads it. The rules are in face_roster.evaluate_stand_down.
        Every stand down is written to the Action Audit Log first; if that
        write fails, or anything else goes wrong, the alert goes ahead."""
        try:
            from . import safety_config as _sc
            if not _sc.face_stand_down_enabled(self.config):
                return False
            from . import action_log, face_roster
            decision = face_roster.evaluate_stand_down()
            if decision.get("stand_down") is not True:
                return False
            who, cam = decision["name"], decision["camera_entity"]
            reason = (f"{decision['reason']}. No {trigger} intrusion investigation "
                      f"was opened for motion at {where}. Opt in setting face_stand_down.")
            row_id = await self.hass.async_add_executor_job(
                lambda: action_log.start(
                    action_log.new_request_id(), "intrusion_face_stand_down", "safety",
                    requested_by_name=who, entity_id=cam,
                    execution_result="accepted",
                    reason_code="face_stand_down", reason_text=reason[:500])
            )
            if row_id is None:
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
        """Detect unauthorized entry when away or asleep. Fires ONE alert, then
        investigates silently until it's a confirmed intrusion (escalated to the
        whole house + every device) or confirmed benign."""
        now = time.time()
        away = self._residents_away()

        # A recent user "false alarm" call-off suppresses new intrusion alerts.
        try:
            from . import intrusion as _intr
            if _intr.is_called_off():
                return None
        except Exception:
            pass

        # Mid-investigation: keep watching, escalate at most once, stay quiet
        # otherwise. This is what stops the stream of repeat "motion" alerts.
        if self._investigation is not None:
            return await self._investigate_step(now, away, sleeping)

        if (now - self._last_intrusion_alert) < 300:
            return None

        motion = self._qualifying_motion(sleeping)
        if not motion:
            return None
        eid, where = motion[0]
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware

        if away:
            armed = False
            entry = None
            if self.config.get("intrusion_require_corroboration", True):
                armed = self._alarm_armed()
                entry = self._open_entry()
                if not (armed or entry):
                    return None
            breach_name = self._friendly(entry) if entry else None
            if await self._face_stand_down("away", where):
                return None
            self._last_intrusion_alert = now
            return self._begin_investigation(
                now=now, trigger="away", presence="away",
                breach=entry, breach_name=breach_name, armed=armed,
                eid=eid, where=where, honorific=honorific,
                reason="motion while away with corroborating breach (open entry or armed alarm)",
            )

        if confined and not sleeping:
            # Confined while residents are home and awake (armed home or night,
            # or a lockdown). Movement is normal here, so the armed state alone
            # is not corroboration: it takes an open entry, or an alarm armed
            # away or on vacation.
            armed = self._alarm_armed_away()
            entry = None
            if self.config.get("intrusion_require_corroboration", True):
                entry = self._open_entry()
                if not (armed or entry):
                    return None
            if await self._face_stand_down("confined", where):
                return None
            self._last_intrusion_alert = now
            return self._begin_investigation(
                now=now, trigger="confined", presence="home",
                breach=entry, breach_name=self._friendly(entry) if entry else None,
                armed=armed, eid=eid, where=where, honorific=honorific,
                reason="motion while confined with a corroborating breach",
            )

        if sleeping:
            # Require an actual breach — a ground-floor exterior door/window
            # actually open — before alerting at all (v7.86.0). Plain
            # movement in a sleeping household (someone up for water, the
            # loo, a pet) is normal and produces zero notifications; only a
            # real entry point being open starts the same silent, room-by-room
            # investigation the away-branch already uses.
            entry, _ = self._ground_floor_open_entry()
            if not entry:
                return None
            breach_name = self._friendly(entry)
            if await self._face_stand_down("sleeping", where):
                return None
            self._last_intrusion_alert = now
            return self._begin_investigation(
                now=now, trigger="sleeping", presence="asleep",
                breach=entry, breach_name=breach_name, armed=False,
                eid=eid, where=where, honorific=honorific,
                reason="motion while asleep with a corroborating ground-floor breach",
            )

        return None

    async def _confirm_person_with_vision(self, cam_entity: str) -> Optional[bool]:
        """Second opinion on Frigate's person detection (v6.73.0). Frigate's
        person sensor is a good TRIGGER but false-positives (shadows, headlights,
        reflections, pets), so before escalating to a full intrusion we snapshot
        the camera and ask Nova's OWN vision model whether a person is actually
        there. Returns:
          True  — vision confirms a person (escalate)
          False — vision says no person (Frigate false-positive; don't escalate)
          None  — vision couldn't run/was inconclusive (fall back to prior
                  behavior, so a broken vision path never SUPPRESSES a real alert)
        """
        if not cam_entity or not str(cam_entity).startswith("camera."):
            return None
        # Respect a config kill-switch: some users may want Frigate-only.
        if not self.config.get("intrusion_vision_confirm", True):
            return None
        try:
            from .camera import async_analyze_camera, _FakeCall
            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            prompt = (
                "Security check. Look at this indoor camera frame. Is there a "
                "PERSON (a human being) actually present in the frame right now? "
                "Answer strictly: reply 'PERSON: YES' if a real person is clearly "
                "visible, or 'PERSON: NO' if there is no person (e.g. it is an "
                "empty room, a shadow, a light, a reflection, a pet, or an "
                "object). Do not guess — if you cannot clearly see a person, "
                "answer NO."
            )
            # A yes/no probe, not a scene description: keep it out of scene
            # memory, or "PERSON: NO" would be stored as a person sighting.
            fc = _FakeCall({"entity_id": cam_entity, "prompt": prompt,
                            "announce": False, "record_scene": False})
            groq_client = getattr(self, "groq_client", None) or getattr(self, "_groq", None)
            res = await async_analyze_camera(
                self.hass, fc, groq_client, honorific, None, [], gate_announce=True)
            text = ""
            if isinstance(res, dict):
                text = (res.get("analysis") or res.get("description")
                        or res.get("message") or "")
            text = str(text).lower()
            if not text:
                return None                       # inconclusive → fall back
            # explicit signal first
            if "person: yes" in text:
                return True
            if "person: no" in text:
                return False
            # heuristic fallback on the free text
            neg = any(p in text for p in (
                "no person", "not a person", "no one", "nobody", "empty",
                "no people", "there is no", "cannot see a person",
                "don't see a person", "no human"))
            pos = any(p in text for p in (
                "a person", "person is", "someone is", "an intruder",
                "a man", "a woman", "a human", "people are"))
            if neg and not pos:
                return False
            if pos and not neg:
                return True
            return None                           # ambiguous → fall back
        except Exception as exc:
            _LOGGER.debug("intrusion: vision confirm failed: %s", exc)
            return None                           # never suppress on error

    async def _investigate_step(self, now: float, away: bool,
                                sleeping: bool) -> Optional[dict]:
        """One tick of an active investigation: confirm, clear, or keep watching
        silently. Escalates only when activity forms a coherent route from the
        breach point (or a camera confirms a person). Motion unrelated to the
        entry is watched, not concluded — that discernment avoids false alarms.
        Returns an escalation action only on confirmation (once)."""
        inv = self._investigation

        # Stand down once the situation that started this investigation no
        # longer holds: residents came home (away-triggered), or the
        # household woke up (sleeping-triggered, v7.86.0). Older investigation
        # dicts have no "trigger" key — default to "away" so pre-existing
        # away-branch behaviour is unchanged.
        trigger = inv.get("trigger", "away")
        if trigger == "confined":
            # tick() already drops the investigation when confinement ends.
            situation_active = True
        else:
            situation_active = away if trigger == "away" else sleeping
        if not situation_active:
            self._investigation = None
            return None

        active = {self._motion_key(eid) for eid, _ in self._qualifying_motion(sleeping)}
        if active:
            inv["last_motion"] = now
            for z in active:
                if z not in inv["zones"]:
                    inv["path"].append(z)         # record the route, in order
            inv["zones"] |= active
            # Track how far INWARD from the breach motion has reached. A real
            # intruder's motion propagates entry → deeper rooms; motion that just
            # lingers at the entry never increases this. (v6.74.0)
            hops = inv.get("hops") or {}
            if hops:
                for z in active:
                    d = hops.get(z)
                    if d is not None and d > inv.get("max_depth", 0):
                        inv["max_depth"] = d

        spread_needed = self.config.get("intrusion_spread_zones", INTRUSION_SPREAD_ZONES)
        connected = inv.get("connected") or set()
        near_breach = bool(inv["zones"] & connected)
        spread = len(inv["zones"]) >= spread_needed
        sustained = bool(active) and (now - inv["start"]) >= INTRUSION_SUSTAINED_SECS

        # Directional inward progression (v6.74.0): a real intruder enters at the
        # breach and moves INWARD to progressively deeper rooms. Motion that
        # merely lingers at/near the entry (an AC unit in the open window, a
        # curtain) never propagates inward, so it must NOT confirm. We require
        # motion to have reached a configurable depth of rooms FROM the breach.
        try:
            need_depth = int(self.config.get("intrusion_inward_depth",
                                             INTRUSION_INWARD_DEPTH))
        except (ValueError, TypeError):
            need_depth = INTRUSION_INWARD_DEPTH
        hops = inv.get("hops") or {}
        max_depth = inv.get("max_depth", 0)
        # inward progression only means something if we have a room-depth map;
        # when we do, require reaching need_depth rooms in. When we don't (no
        # floor plan), fall back to the old spread+near_breach heuristic so a
        # user without a mapped house still gets protection.
        if hops:
            inward = max_depth >= need_depth
        else:
            inward = spread and near_breach

        # Prefer a camera whose saved coverage actually sees the breach area — lets
        # Nova confirm a person via a camera in an adjacent room with a sightline
        # (e.g. the dining camera seeing the living room through the open staircase),
        # not only a camera physically in that room (v7.20.0).
        cam_entity = None
        try:
            _ba = inv.get("breach_area") if isinstance(inv, dict) else None
            if _ba:
                from . import camera_coverage as _cc
                cam_entity = _cc.camera_for_area(self.hass, _ba)
        except Exception:
            cam_entity = None
        if not cam_entity:
            cam_entity = self._person_camera_entity()
        camera = cam_entity is not None

        if camera:
            # Frigate flagged a person — but Frigate false-positives, so get a
            # second opinion from Nova's OWN vision before escalating (v6.73.0).
            vision = await self._confirm_person_with_vision(cam_entity)
            if vision is True:
                confirmed, reason = True, "a person is on camera (confirmed by vision)"
            elif vision is False:
                # Frigate said person, Nova's eyes say no → false positive.
                # Do NOT escalate on the camera signal; require a real inward
                # route through the house instead (v6.74.0).
                _LOGGER.info("intrusion: Frigate person on %s NOT confirmed by "
                             "vision — requiring inward motion route", cam_entity)
                if inv.get("breach_area"):
                    confirmed = inward
                    reason = ("someone is moving inward through the house from "
                              "the point of entry")
                else:
                    confirmed = spread and sustained
                    reason = "sustained movement through the house while no one is home"
            else:
                # Vision inconclusive/unavailable → fall back to prior behavior
                # (trust the camera) so a broken vision path never suppresses a
                # real alert. Fail toward safety.
                confirmed, reason = True, "a person is on camera"
        elif inv.get("breach_area"):
            # Entry point known: require motion to have travelled INWARD from the
            # breach (a genuine intrusion route), not merely lingered near it.
            confirmed = inward
            reason = ("someone is moving inward through the house from the "
                      "point of entry")
        else:
            # No location on the breach — be conservative: sustained multi-room
            # movement, not a momentary blip.
            confirmed = spread and sustained
            reason = "sustained movement through the house while no one is home"

        # User called it off as a false alarm → stand down, don't escalate.
        try:
            from . import intrusion as _intr
            if _intr.is_called_off():
                self._investigation = None
                return None
        except Exception:
            pass

        elapsed = now - inv["start"]

        # Response-timeout escalation (v6.69.0): the initial "investigating"
        # alert went out to notifications + voice. If the user hasn't responded
        # (neither acknowledged nor called it off) within the response window,
        # the situation is STILL ACTIVE (motion hasn't gone quiet), and it hasn't
        # otherwise cleared, escalate anyway — an unanswered *ongoing* possible
        # break-in fails toward alerting. Motion that started and then stopped
        # still clears as benign below (a curtain flutter shouldn't escalate just
        # because no one answered); the timeout only bites while something is
        # actively still happening. An acknowledgement ("I'm looking") holds it;
        # a false-alarm call-off (handled above) stops it entirely.
        quiet_for = now - inv["last_motion"]
        timed_out = False
        # The soft notice is tracked on its own ("soft_notice") so it can never
        # use up the critical alert: "escalated" means the critical alert has
        # gone out, "soft_notice" means the soft one has been dealt with.
        soft_done = inv.get("soft_notice", False)
        if not inv["escalated"] and not soft_done and not confirmed:
            try:
                from . import intrusion as _intr
                acknowledged = _intr.is_acknowledged()
            except Exception:
                acknowledged = False
            try:
                resp_timeout = float(self.config.get(
                    "intrusion_response_timeout", INTRUSION_RESPONSE_TIMEOUT_SECS))
            except (ValueError, TypeError):
                resp_timeout = INTRUSION_RESPONSE_TIMEOUT_SECS
            still_active = quiet_for < INTRUSION_CLEAR_QUIET_SECS
            if not acknowledged and still_active and elapsed >= resp_timeout:
                timed_out = True

        # A confirmed intrusion (real inward route, or a person confirmed on
        # camera) fires the full critical alarm. A timeout WITHOUT that evidence
        # no longer masquerades as a confirmed intrusion — instead it sends a
        # softer "couldn't reach you, please check" notice. This is the fix for
        # false "intrusion confirmed" alerts firing on unanswered lingering
        # motion with no real route through the house (v6.74.0).
        if not inv["escalated"] and confirmed:
            inv["escalated"] = True
            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            snap = None
            if cam_entity:
                try:
                    from . import intrusion as _intr
                    snap = await _intr.capture_snapshot(self.hass, cam_entity)
                except Exception:
                    snap = None
            snap_note = " A snapshot is available." if snap else ""
            msg = _persona().lead_in(honorific,
                   f"intrusion confirmed — {reason}.{snap_note} "
                   f"Alerting the house and every device. Say 'it's a false alarm' "
                   f"to call it off.")
            action = {
                "type": "intrusion_confirmed", "urgency": "critical",
                "message": msg, "auto_act": True, "notify_all": True,
                "can_dismiss": True,
            }
            notify_url = None
            if snap:
                try:
                    from . import intrusion as _intr
                    notify_url = await _intr.get_notification_image_url(
                        self.hass, snap.get("path"))
                except Exception:
                    notify_url = None
                action["snapshot_url"] = notify_url
                action["snapshot_path"] = snap.get("path")
                action["camera"] = snap.get("camera")
            try:
                self.hass.bus.async_fire("nova_intrusion_confirmed", {
                    "reason": reason,
                    "snapshot_url": notify_url,
                    "snapshot_path": (snap or {}).get("path"),
                    "camera": (snap or {}).get("camera"),
                })
            except Exception:
                pass
            # Log it for review/labeling. A CONFIRMED intrusion is never damped
            # by learning — it always alerts (v6.76.0).
            try:
                from . import intrusion as _intr
                _intr.record_event(
                    "confirmed", reason=reason,
                    breach=inv.get("breach_name"), breach_area=inv.get("breach_area"),
                    camera=cam_entity, snapshot=snap,
                    zones=sorted(inv.get("zones") or []),
                    max_depth=inv.get("max_depth"))
            except Exception:
                pass
            return action

        if not inv["escalated"] and not soft_done and timed_out:
            # Unanswered, still some motion, but NO confirming inward route.
            # Don't cry "intrusion confirmed" — send a soft check-in instead.
            # Only the soft notice is marked done here, so a route or a person
            # confirmed later still raises the critical alert, once.
            inv["soft_notice"] = True          # don't repeat this either
            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            # Learned damping applies here too: this is a LOW-CONFIDENCE alert
            # (nothing was confirmed), so a pattern repeatedly labelled a false
            # alarm stays quiet (v6.76.0).
            damped_soft = False
            try:
                from . import intrusion as _intr
                damped_soft = _intr.should_damp_weak_alert(
                    inv.get("breach_area"), cam_entity)
            except Exception:
                damped_soft = False
            snap = None
            if cam_entity:
                try:
                    from . import intrusion as _intr
                    snap = await _intr.capture_snapshot(self.hass, cam_entity)
                except Exception:
                    snap = None
            try:
                from . import intrusion as _intr
                _intr.record_event(
                    "unresolved",
                    reason=("damped (learned benign)" if damped_soft
                            else "no response; unconfirmed activity"),
                    breach=inv.get("breach_name"), breach_area=inv.get("breach_area"),
                    camera=cam_entity, snapshot=snap,
                    zones=sorted(inv.get("zones") or []),
                    max_depth=inv.get("max_depth"))
            except Exception:
                pass
            if damped_soft:
                _LOGGER.info("intrusion: unresolved alert damped for "
                             "learned-benign pattern at %s", inv.get("breach_area"))
                return None
            snap_note = " A snapshot is available." if snap else ""
            where = inv.get("breach_name") or "the point of entry"
            msg = _persona().lead_in(honorific,
                   f"I flagged possible activity near {where} "
                   f"and couldn't reach you, but I have not confirmed anyone moving "
                   f"through the house.{snap_note} Please check when you can — say "
                   f"'it's a false alarm' to clear it.")
            action = {
                "type": "intrusion_unresolved", "urgency": "high",
                "message": msg, "auto_act": True, "notify_all": True,
                "can_dismiss": True,
            }
            notify_url = None
            if snap:
                try:
                    from . import intrusion as _intr
                    notify_url = await _intr.get_notification_image_url(
                        self.hass, snap.get("path"))
                except Exception:
                    notify_url = None
                action["snapshot_url"] = notify_url
                action["snapshot_path"] = snap.get("path")
                action["camera"] = snap.get("camera")
            try:
                self.hass.bus.async_fire("nova_intrusion_unresolved", {
                    "reason": "no response; unconfirmed activity",
                    "snapshot_url": notify_url,
                    "snapshot_path": (snap or {}).get("path"),
                    "camera": (snap or {}).get("camera"),
                })
            except Exception:
                pass
            return action

        if not inv["escalated"] and not soft_done and (
                quiet_for > INTRUSION_CLEAR_QUIET_SECS
                or elapsed > INTRUSION_MAX_INVESTIGATE_SECS):
            self._investigation = None            # nothing of note
        elif (inv["escalated"] or soft_done) and quiet_for > INTRUSION_CLEAR_QUIET_SECS:
            self._investigation = None            # situation settled
        return None

    async def _nighttime_lockdown(self, automatic_generation: int) -> list[dict]:
        """Check and secure all locks and doors during sleep."""
        actions = []
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware

        # Action Audit Log (top-level boundary: this sweep decides which
        # entities need securing — it owns one request_id for every target
        # it touches this run; nothing it calls creates its own row).
        from . import action_log
        request_id = action_log.new_request_id()
        candidates: list[tuple[str, str, str]] = []  # (entity_id, domain, service)
        for state in self.hass.states.async_all("lock"):
            if state.state == "unlocked" and state.entity_id not in _lockdown_exempt_locks():
                candidates.append((state.entity_id, "lock", "lock"))
        for state in self.hass.states.async_all("cover"):
            if state.state == "open":
                candidates.append((state.entity_id, "cover", "close_cover"))
        row_ids = await self.hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, "lockdown", "safety_routine",
                [{"key": eid, "domain": d, "service": s, "entity_id": eid}
                 for eid, d, s in candidates],
            )
        ) if candidates else {}

        # Check locks
        unlocked = []
        failed = []   # friendly names of anything the sweep could not secure
        for state in self.hass.states.async_all("lock"):
            if not self._automatic_operation_current(automatic_generation):
                return []
            if state.state == "unlocked":
                eid = state.entity_id
                if eid in _lockdown_exempt_locks():
                    continue
                fname = state.attributes.get("friendly_name", eid)
                row_id = row_ids.get(eid)
                # Auto-lock
                try:
                    await self.hass.services.async_call(
                        "lock", "lock", {"entity_id": eid}, blocking=True,
                    )
                    if not self._automatic_operation_current(automatic_generation):
                        return []
                    unlocked.append(fname)
                    _LOGGER.info("Cognitive lockdown: locked %s", eid)
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
                except Exception as exc:
                    _LOGGER.warning("Cognitive lockdown: failed to lock %s: %s", eid, exc)
                    failed.append(fname)
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )

        # Check covers/garage
        open_covers = []
        for state in self.hass.states.async_all("cover"):
            if not self._automatic_operation_current(automatic_generation):
                return []
            if state.state == "open":
                eid = state.entity_id
                fname = state.attributes.get("friendly_name", eid)
                row_id = row_ids.get(eid)
                try:
                    await self.hass.services.async_call(
                        "cover", "close_cover", {"entity_id": eid}, blocking=True,
                    )
                    if not self._automatic_operation_current(automatic_generation):
                        return []
                    open_covers.append(fname)
                    _LOGGER.info("Cognitive lockdown: closed %s", eid)
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(rid, "accepted")
                    )
                except Exception as exc:
                    _LOGGER.warning("Cognitive lockdown: failed to close %s: %s", eid, exc)
                    failed.append(fname)
                    await self.hass.async_add_executor_job(
                        lambda rid=row_id: action_log.set_execution(
                            rid, "failed", reason_code="service_call_failed")
                    )

        if not self._automatic_operation_current(automatic_generation):
            return []
        if unlocked or open_covers or failed:
            i18n = _notify_i18n()
            lang = _hass_lang(self.hass)
            parts = []
            if unlocked:
                parts.append(i18n.message("lockdown_locked", lang,
                                          names=i18n.join_names(unlocked, lang)))
            if open_covers:
                parts.append(i18n.message("lockdown_closed", lang,
                                          names=i18n.join_names(open_covers, lang)))
            if not failed:
                message = i18n.message(
                    "lockdown_nighttime", lang,
                    honorific=honorific.title(),
                    body=i18n.join_names(parts, lang),
                )
            else:
                # "The house is secured." is only said when nothing failed.
                problem = i18n.message("lockdown_secure_failed", lang,
                                       names=i18n.join_names(failed, lang))
                if parts:
                    message = i18n.message(
                        "lockdown_nighttime_partial", lang,
                        honorific=honorific.title(),
                        body=i18n.join_names(parts, lang), failed=problem)
                else:
                    message = i18n.message(
                        "lockdown_nighttime_failed_only", lang,
                        honorific=honorific.title(), failed=problem)
            actions.append({
                "type": "lockdown",
                # A failure to secure at night is raised to high so it is
                # pushed to the phone and spoken when someone is awake.
                "urgency": "high" if failed else "low",
                "message": message,
                "auto_act": True,
            })

        return actions

    def _residents_away(self) -> bool:
        """Confident 'the residents are away' — for intrusion only.

        Based on tracked presence (person / device_tracker) or an explicitly
        armed-away alarm, NEVER on motion/occupancy: intrusion exists to judge
        motion, so motion cannot also be the signal that says whether anyone is
        home. Crucially, the ABSENCE of tracking is not 'away' — with no person/
        device_tracker entities we cannot claim the house is empty, so this returns
        False and motion is never treated as an intruder. That is what prevents the
        false "motion … while no one is home" alerts when someone is home but their
        phone isn't tracked."""
        # A resident's device/person reading 'home' wins outright.
        for st in self.hass.states.async_all("person"):
            if str(st.state).lower() == "home":
                return False
        for st in self.hass.states.async_all("device_tracker"):
            if str(st.state).lower() == "home":
                return False
        # An intentionally armed-away alarm is a strong 'away' signal.
        from . import alarm_source
        for st in alarm_source.states(self.hass, self.config):
            if str(st.state).lower() in ("armed_away", "armed_vacation"):
                return True
        # Otherwise, only 'away' if presence is actually tracked and reads away.
        tracked = False
        for st in self.hass.states.async_all("person"):
            tracked = True
        for st in self.hass.states.async_all("device_tracker"):
            if str(st.state).lower() in ("home", "not_home", "away"):
                tracked = True
        return tracked


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
                self.exempt_windows = set(d.get("exempt_windows", []))
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
            write_json_atomic(_lockdown_state_path(), {
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

    def _anyone_home(self) -> bool:
        """True if anyone is home — by tracked presence OR live occupancy. Used by
        lockdown / efficiency checks, where active occupancy legitimately means
        'someone is home' (these checks are not motion-triggered, so counting
        occupancy here is safe). Intrusion deliberately does NOT use this — it uses
        _residents_away, because an intruder's own motion would otherwise mask the
        alarm."""
        for st in self.hass.states.async_all("person"):
            if str(st.state).lower() == "home":
                return True
        for st in self.hass.states.async_all("device_tracker"):
            if str(st.state).lower() == "home":
                return True
        occ_on = ("on", "detected", "occupied", "home", "true")
        for st in self.hass.states.async_all("binary_sensor"):
            if (st.attributes.get("device_class") in ("occupancy", "motion", "presence")
                    and str(st.state).lower() in occ_on):
                return True
        return False

    # ── opening / secure-state model (doors + windows + locks) ──────────────
    _DOOR_WINDOW_BS = ("door", "window", "garage_door", "opening")
    _CLOSEABLE_COVERS = {"door", "garage", "garage_door", "window", "gate"}

    def _open_openings(self) -> set:
        """Every door/window currently open right now (sensors + covers)."""
        out = set()
        for st in self.hass.states.async_all("binary_sensor"):
            if st.attributes.get("device_class") in self._DOOR_WINDOW_BS and st.state == "on":
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
            if dom == "lock":
                await self.hass.services.async_call("lock", "lock", {"entity_id": eid}, blocking=True)
            else:
                await self.hass.services.async_call("cover", "close_cover", {"entity_id": eid}, blocking=True)
            return True
        except Exception as exc:
            _LOGGER.warning("Lockdown: secure %s failed: %s", eid, exc)
            return False

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
        candidates = [
            st for st in self.hass.states.async_all("lock")
            if st.entity_id not in self.exempt_locks and st.state == "unlocked"
        ]
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
                await self.hass.services.async_call(
                    "lock", "lock", {"entity_id": eid}, blocking=True)
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
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware

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
                                          lang=_hass_lang(self.hass),
                                          failed=failed_locks)
        _LOGGER.warning(
            "Lockdown ENGAGED (%s): locked=%s closed=%s left-open=%d "
            "lock-failed=%s announce=%s",
            reason, locked, closed, len(uncloseable), failed_locks, announce)
        await self._persist()
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
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware
        _LOGGER.warning("Lockdown DISENGAGED (%s, manual=%s, auto_suppressed=%s)",
                        reason, manual, self._auto_suppressed)
        await self._persist()
        return {
            "type": "lockdown_disengaged",
            "urgency": "low",
            "message": _notify_i18n().message(
                "lockdown_lifted", _hass_lang(self.hass), honorific=honorific.title()),
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
            return None
        if old is not None and not self._is_secure(dom, old.state):
            return None  # was already unsecure — not a fresh transition
        name = new.attributes.get("friendly_name", eid)
        honorific = _live_honorific(self.hass)  # Phase C: presence-aware

        if not self._can_secure(dom, dc):
            # Nothing Nova can do about a contact sensor → assume intentional.
            self.exempt_windows.add(eid)
            await self._persist()
            _LOGGER.info("Lockdown: %s opened (not controllable) — treating as intentional", eid)
            return None

        if eid in self._secured_by_us:
            # We shut it once and it's open again → the user wants it open.
            self._secured_by_us.discard(eid)
            self.exempt_windows.add(eid)
            await self._persist()
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
            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
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


# ── Proactive Intelligence (v5.9.07) ────────────────────────────────────────

class ProactiveManager:
    """
    Pursues comfort & efficiency opportunities — not just safety.

    Where SafetyManager prevents harm, ProactiveManager reduces friction:
    it notices when a small action would help (dark room with someone in it,
    a light left on in an empty room, HVAC fighting an empty house) and
    OFFERS to act. It never forces — offers are spoken/pushed suggestions the
    user can accept by voice. Graduated autonomy (see AutonomyManager) can
    later promote a repeatedly-approved offer to silent auto-execution.

    All offers respect: the global proactive kill-switch, quiet hours/sleep,
    ignore rules, and a per-opportunity cooldown so Nova never nags.
    """

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self._last_check = 0.0
        self._offer_cooldowns: dict[str, float] = {}  # opportunity_key -> ts

    def _on_cooldown(self, key: str) -> bool:
        last = self._offer_cooldowns.get(key, 0.0)
        return (time.time() - last) < PROACTIVE_OFFER_COOLDOWN

    def _mark_offered(self, key: str) -> None:
        self._offer_cooldowns[key] = time.time()

    async def tick(self, sleeping: bool, anyone_home: bool) -> list[dict]:
        """
        Evaluate comfort/efficiency opportunities. Returns a list of offer
        actions (same dict shape SafetyManager uses, with offer=True).
        """
        now = time.time()
        if (now - self._last_check) < PROACTIVE_CHECK_INTERVAL:
            return []
        self._last_check = now

        # Proactive offers are silent during sleep — comfort can wait.
        if sleeping:
            return []

        offers: list[dict] = []

        try:
            dark = await self._check_dark_occupied_room(anyone_home)
            if dark:
                offers.append(dark)
        except Exception as exc:
            _LOGGER.debug("Proactive dark-room check error: %s", exc)

        try:
            stale = await self._check_stale_lights(anyone_home)
            if stale:
                offers.append(stale)
        except Exception as exc:
            _LOGGER.debug("Proactive stale-light check error: %s", exc)

        try:
            hvac = await self._check_hvac_efficiency(anyone_home)
            if hvac:
                offers.append(hvac)
        except Exception as exc:
            _LOGGER.debug("Proactive HVAC check error: %s", exc)

        return offers

    async def _check_dark_occupied_room(self, anyone_home: bool) -> Optional[dict]:
        """Someone present in a room that's dark and has lights off → offer."""
        if not anyone_home:
            return None
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(self.hass)

        # Find lux sensors that read dark
        for s in self.hass.states.async_all("sensor"):
            if s.attributes.get("device_class") != "illuminance":
                continue
            try:
                lux = float(s.state)
            except (ValueError, TypeError):
                continue
            if lux > DARK_LUX_THRESHOLD:
                continue

            # Determine the area of this sensor
            entry = ent_reg.async_get(s.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id:
                continue

            # Is there occupancy (motion/presence) in this area?
            occupied = self._area_has_presence(area_id, ent_reg)
            if not occupied:
                continue

            # Are the lights in this area already off?
            lights = self._area_lights(area_id, ent_reg)
            if not lights:
                continue
            if any(self.hass.states.get(l).state == "on" for l in lights if self.hass.states.get(l)):
                continue  # already lit

            key = f"dark:{area_id}"
            if self._on_cooldown(key):
                return None
            # Cooldown is marked by the tick only when this offer is actually
            # delivered (see _tick), so deferred offers re-surface naturally.

            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            area_name = self._area_name(area_id)
            return {
                "type": "proactive_lights",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"it's quite dark in the {area_name} "
                    f"and someone's in there. Shall I turn the lights on?"
                ),
                "action_data": {"domain": "light", "service": "turn_on",
                                "entity_ids": lights},
                "pattern_key": f"lights_on_when_dark:{area_id}",
            }
        return None

    async def _check_stale_lights(self, anyone_home: bool) -> Optional[dict]:
        """Light on a long time in an unoccupied area → offer to turn off."""
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(self.hass)
        now = dt_util.utcnow()

        for s in self.hass.states.async_all("light"):
            if s.state != "on":
                continue
            # How long has it been on?
            last_changed = s.last_changed
            if not last_changed:
                continue
            mins_on = (now - last_changed).total_seconds() / 60.0
            if mins_on < STALE_LIGHT_MINUTES:
                continue

            entry = ent_reg.async_get(s.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id:
                continue

            # Only flag if the area has NO presence
            if self._area_has_presence(area_id, ent_reg):
                continue

            key = f"stale:{s.entity_id}"
            if self._on_cooldown(key):
                return None

            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            name = s.attributes.get("friendly_name", s.entity_id)
            area_name = self._area_name(area_id)
            return {
                "type": "proactive_stale_light",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"the {name} has been on for "
                    f"{int(mins_on)} minutes in the {area_name}, which appears "
                    f"empty. Shall I turn it off?"
                ),
                "action_data": {"domain": "light", "service": "turn_off",
                                "entity_ids": [s.entity_id]},
                "pattern_key": f"lights_off_when_empty:{area_id}",
            }
        return None

    async def _check_hvac_efficiency(self, anyone_home: bool) -> Optional[dict]:
        """Climate actively heating/cooling while the house is empty → flag."""
        if anyone_home:
            return None
        for s in self.hass.states.async_all("climate"):
            action = s.attributes.get("hvac_action")
            if action not in ("heating", "cooling"):
                continue

            key = f"hvac:{s.entity_id}"
            if self._on_cooldown(key):
                return None

            honorific = _live_honorific(self.hass)  # Phase C: presence-aware
            name = s.attributes.get("friendly_name", s.entity_id)
            return {
                "type": "proactive_hvac",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"the {name} is {action} but no one's "
                    f"home. Would you like me to set it back to save energy?"
                ),
                "action_data": {"domain": "climate", "service": "set_preset_mode",
                                "entity_ids": [s.entity_id],
                                "service_data": {"preset_mode": "eco"}},
                "pattern_key": "hvac_eco_when_away",
            }
        return None

    # ── Area helpers ─────────────────────────────────────────────────
    def _area_has_presence(self, area_id: str, ent_reg) -> bool:
        """True if any motion/occupancy/presence sensor in the area is active."""
        for s in self.hass.states.async_all("binary_sensor"):
            dc = s.attributes.get("device_class")
            if dc not in ("motion", "occupancy", "presence"):
                continue
            entry = ent_reg.async_get(s.entity_id)
            if entry and entry.area_id == area_id and s.state == "on":
                return True
        return False

    def _area_lights(self, area_id: str, ent_reg) -> list[str]:
        """All light entity_ids in the given area."""
        out = []
        for s in self.hass.states.async_all("light"):
            entry = ent_reg.async_get(s.entity_id)
            if entry and entry.area_id == area_id:
                out.append(s.entity_id)
        return out

    def _area_name(self, area_id: str) -> str:
        try:
            from homeassistant.helpers import area_registry as ar
            reg = ar.async_get(self.hass)
            area = reg.async_get_area(area_id)
            return area.name if area else area_id
        except Exception:
            return area_id


# ── Core State ──────────────────────────────────────────────────────────────

class _CoreState:
    def __init__(self):
        self.hass: Optional[HomeAssistant] = None
        self.config: dict = {}
        self.running: bool = False
        self.task: Optional[asyncio.Task] = None
        self.unsub: Optional[object] = None
        self.alarm_unsub: Optional[object] = None  # alarm_control_panel → lockdown sync listener
        self.ignore_mgr: Optional[IgnoreManager] = None
        self.safety_mgr: Optional[SafetyManager] = None
        self.lockdown_mgr: Optional["LockdownManager"] = None
        self.proactive_mgr: Optional[ProactiveManager] = None
        self.autonomy_mgr: Optional[AutonomyManager] = None
        self.state_logger: Optional[StateLogger] = None
        self.automation_contexts = None
        # The config entry that started the core; its NovaRuntime owns the
        # live panel settings read when an action is announced.
        self.entry = None
        self.tick_count: int = 0
        self.actions_taken: int = 0
        self.offers_made: int = 0
        self.autonomous_actions: int = 0
        self.last_tick: float = 0.0
        self.startup_time: float = 0.0
        # Pending offer awaiting a yes/no from the user (set when an offer is
        # spoken, consumed by the conversation layer on "yes"/"no").
        self.pending_offer: Optional[dict] = None

_CORE = _CoreState()


@callback
def _pattern_opted_in(entity_id: str, device_class: str = "") -> bool:
    """Whether a normally-excluded entity (door/window, presence) should still be
    learned for routines — via a per-entity opt-in or a domain group toggle
    (v7.11.0). Default is off; noisy domains stay filtered unless chosen."""
    try:
        from . import nova_config
    except Exception:
        return False
    try:
        incl = nova_config.get("pattern_include_entities", []) or []
        if entity_id in incl:
            return True
        domain = entity_id.split(".")[0]
        if domain == "binary_sensor" and nova_config.get("pattern_learn_doors", False):
            return device_class in ("door", "window", "garage_door", "opening")
        if domain == "binary_sensor" and nova_config.get("pattern_learn_motion", False):
            # Motion/occupancy triggers — learnable for "when X, do Y" automations
            # (motion → light). Chatty, so the class-aware rate limit below caps
            # them hard; opt-in and off by default.
            return device_class in ("motion", "occupancy", "presence", "moving")
        if domain in ("device_tracker", "person") and nova_config.get("pattern_learn_presence", False):
            return True
        if domain == "event" and nova_config.get("pattern_learn_buttons", False):
            # Button/remote presses surface as event.* entities; learnable for
            # "press → scene/action" automations. Opt-in, off by default.
            return True
    except Exception:
        pass
    return False


# Per-entity rate limit for pattern logging (v7.67.0): entity_id -> last logged
# epoch. Caps a flapping / high-frequency actuator from flooding the store.
_PATTERN_LOG_LAST: dict = {}

# Device classes that pulse rapidly — capped far harder than the base interval
# (see _pattern_log_interval) so re-including them as triggers can't re-flood.
_HIGH_FREQ_CLASSES = {"motion", "occupancy", "presence", "moving", "vibration", "sound"}


def _pattern_log_interval(device_class: str = "") -> float:
    """Minimum seconds between logged pattern changes for the SAME entity.

    Base is configurable via ``pattern_log_min_interval`` (0 disables). High-
    frequency trigger classes (motion/occupancy/presence) use a much larger
    floor (``pattern_motion_min_interval``, default 5 min) so re-including them
    as triggers logs at most one "it fired in this window" marker — enough for
    sequence detection, never a flood.
    """
    try:
        base = max(0.0, float((_CORE.config or {}).get("pattern_log_min_interval", 60)))
    except Exception:
        base = 60.0
    if device_class in _HIGH_FREQ_CLASSES:
        try:
            hf = float((_CORE.config or {}).get("pattern_motion_min_interval", 300))
        except Exception:
            hf = 300.0
        return max(base, hf)
    return base


def _pattern_rate_ok(entity_id: str, now: float, device_class: str = "") -> bool:
    """Per-entity rate gate for pattern logging. Returns True (and records the
    time) when this entity may be logged now, False to drop it. Pure + testable
    — a motion storm through this gate stays bounded to one log per interval."""
    if (now - _PATTERN_LOG_LAST.get(entity_id, 0.0)) < _pattern_log_interval(device_class):
        return False
    _PATTERN_LOG_LAST[entity_id] = now
    return True


# ── State Change Listener ──────────────────────────────────────────────────

def _on_state_changed(event: Event) -> None:
    """Log state changes and check ignore rules."""
    if not _CORE.running:
        return

    entity_id = event.data.get("entity_id", "")
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")

    if not new_state:
        return

    # camera_event.* (Phase 4, v7.109.0) is written directly by
    # camera_semantic.record_event -> log_camera_event, with its own
    # confidence floor, dedup, resident attribution, and learning-enabled
    # check — richer and more correct than this generic path (which would
    # otherwise also attribute a "who's home" best guess, exactly what
    # camera semantic events are deliberately NOT supposed to use). Skip it
    # here entirely so setting the live state for automation-trigger
    # purposes can never double-log the same event.
    if entity_id.startswith("camera_event."):
        return

    # Check ignore rules
    if _CORE.ignore_mgr and _CORE.ignore_mgr.is_ignored(entity_id):
        return

    # User exclusion (specific entities / domains / labels) — don't monitor,
    # log, or learn from it at all.
    try:
        from .entity_filter import is_excluded
        if is_excluded(_CORE.hass, entity_id):
            return
    except Exception:
        pass

    old_val = old_state.state if old_state else "unknown"
    new_val = new_state.state

    # Skip unavailable/unknown transitions
    if new_val in ("unavailable", "unknown") or old_val == new_val:
        return

    _dc = ""
    try:
        _dc = new_state.attributes.get("device_class") or ""
    except Exception:
        _dc = ""

    # A door, window, opening, garage door or lock coming back from
    # unknown/unavailable is not a physical action: never a pattern (v7.120.1).
    from .cognitive.evaluators import is_entry_state_recovery
    if is_entry_state_recovery(entity_id, _dc, old_val, new_val):
        return

    # Event entities are stateless pulses — HA puts a timestamp in `.state`, but
    # the meaningful value is the `event_type` attribute (e.g. "single",
    # "double"). Substitute it so "button single-press → scene" is minable, and
    # drop fires that carry no type. Scenes are similar: `.state` is a timestamp
    # that changes on each activation, so record a stable "activated" marker.
    _dom0 = entity_id.split(".", 1)[0]
    if _dom0 == "event":
        try:
            _et = new_state.attributes.get("event_type") or ""
        except Exception:
            _et = ""
        if not _et:
            return
        new_val = _et
        old_val = ""
    elif _dom0 == "scene":
        new_val = "activated"
        old_val = ""

    # Per-entity rate limit for pattern logging. A high-frequency source — a
    # streaming media_player flipping playing/buffering/paused, or a motion
    # sensor pulsing every few seconds — would otherwise flood the pattern store
    # with tens of thousands of near-worthless rows that bloat every analysis
    # pass. The gate caps each entity to one logged change per interval (motion/
    # occupancy far harder, see _pattern_log_interval): infrequent routine
    # transitions are unaffected, entities are independent so cross-entity
    # sequences still record, and dropped churn skips the area/person lookups.
    if not _pattern_rate_ok(entity_id, time.time(), _dc):
        return

    # Get area
    area_id = ""
    try:
        from homeassistant.helpers import entity_registry as er, device_registry as dr
        ent_reg = er.async_get(_CORE.hass)
        dev_reg = dr.async_get(_CORE.hass)
        entry = ent_reg.async_get(entity_id)
        if entry:
            area_id = entry.area_id or ""
            if not area_id and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                area_id = device.area_id if device else ""
    except Exception:
        pass

    # Log for pattern learning. v6.41.0: stamp the sole-occupant person when
    # unambiguous (cheap presence check — the full face/voice resolver is
    # too costly to run on every state event; commands already get that
    # treatment on the much lower-volume conversation path).
    if _CORE.state_logger:
        person = "unknown"
        person_conf = 0.0
        try:
            from . import identity
            # v6.77.0: room-aware — pass the area the event happened in so
            # room-scoped presence/recognition and proximity can attribute it
            # even when several people are home.
            ident = identity.quick_identify(_CORE.hass, area_id)
            person = ident.person
            person_conf = ident.confidence
            # Best-guess attribution (v7.9.0): if no single person cleared the
            # certainty bar, record the LEADING candidate anyway — with a
            # confidence scaled by how decisively it leads — instead of dropping
            # the event to 'unknown'. This lets per-person routines accumulate and
            # their owner firm up as recognition improves (the pattern analyzer
            # weights occurrences by this confidence). The strict threshold still
            # gates high-certainty ACTIONS via identity.resolve(), not this
            # high-volume telemetry. A genuine tie (no clear leader) stays unknown.
            if person == identity.UNKNOWN and ident.candidates:
                ranked = sorted(ident.candidates.items(),
                                key=lambda kv: kv[1], reverse=True)
                top = float(ranked[0][1])
                second = float(ranked[1][1]) if len(ranked) > 1 else 0.0
                lead = (top - second) / top if top > 0 else 0.0
                # Record the leader when it CLEARLY leads (looser than the old
                # 1.5x gate, so more per-person routines accumulate) — but a genuine
                # near-tie coin-flip stays 'unknown'; we don't invent attribution.
                if top > 0 and (len(ranked) == 1 or lead >= 0.2):
                    person = ranked[0][0]
                    person_conf = round(min(0.44, top * (0.4 + 0.6 * lead)), 3)
        except Exception:
            pass
        _fi = False
        try:
            _fi = _pattern_opted_in(entity_id, _dc)
        except Exception:
            _fi = False
        source_kind = "unknown"
        source_entity_id = ""
        source_confidence = 0.0
        if _CORE.automation_contexts is not None:
            try:
                source = _CORE.automation_contexts.resolve_state(new_state)
                source_kind = source.kind
                source_entity_id = source.entity_id
                source_confidence = source.confidence
            except Exception:
                pass
        _CORE.state_logger.log_state_change(
            entity_id, old_val, new_val, area_id,
            triggered_by=source_kind,
            source_entity_id=source_entity_id,
            source_confidence=source_confidence,
            person=person,
            person_confidence=person_conf,
            force_include=_fi,
        )


# ── Main Evaluation Loop ───────────────────────────────────────────────────

async def _tick():
    """Single evaluation tick — reviews home state and decides actions."""
    hass = _CORE.hass
    config = _CORE.config
    _CORE.tick_count += 1
    _CORE.last_tick = time.time()

    # Determine home state
    anyone_home = any(
        s.state == "home" for s in hass.states.async_all("person")
    )

    from . import sleep_detection
    bedroom_areas = config.get("bedroom_areas", []) or []
    # (offer-area resolution for room-scoped modes lives in _offer_area, below.)
    sleeping, _ = sleep_detection.is_sleeping(
        hass,
        bedroom_area_ids=bedroom_areas,
        quiet_start=config.get("observer_quiet_start", "22:00"),
        quiet_end=config.get("observer_quiet_end", "07:00"),
    )

    # Lockdown runs first so the nighttime sweep can defer to it when active.
    actions = []
    if _CORE.lockdown_mgr:
        try:
            actions.extend(await _CORE.lockdown_mgr.tick())
        except Exception as exc:
            _LOGGER.debug("Lockdown tick error: %s", exc)

    # Auto operational-mode (v7.14.0): keep AWAY/NORMAL in step with occupancy
    # unless the user has chosen hands-on control. Never affects safety.
    try:
        from . import modes as _auto_modes
        _auto_modes.auto_evaluate(anyone_home)
    except Exception as exc:
        _LOGGER.debug("auto-mode eval error: %s", exc)

    # Run safety checks. An error here must not lose what is already gathered
    # (a lockdown announcement: the manager has already changed state and will
    # not announce it again) or stop the rest of the tick.
    try:
        actions.extend(await _CORE.safety_mgr.tick(sleeping, anyone_home))
    except Exception as exc:
        _LOGGER.warning("Cognitive safety tick error: %s", exc)

    # ── Proactive comfort/efficiency offers (v5.9.07) ───────────────
    # Gated by the global proactive kill-switch AND the active operational mode
    # (a mode like party/movie/lab can silence convenience offers). Safety always
    # runs regardless — mode never gates SafetyManager.
    proactive_enabled = _CORE.config.get("observer_proactive", True)
    mode_scope_areas = []
    try:
        from . import modes
        mode_scope_areas = modes.mode_scoped_to_areas()
        # A room-scoped mode (lab/movie bound to rooms) keeps the house proactive;
        # only offers about the bound rooms are dropped in the loop below. An
        # unscoped suppressing mode still silences proactivity house-wide.
        if not mode_scope_areas and not modes.mode_allows_proactive():
            proactive_enabled = False
    except Exception:
        pass
    if proactive_enabled and _CORE.proactive_mgr:
        try:
            offers = await _CORE.proactive_mgr.tick(sleeping, anyone_home)
            spoke_offer = False  # only ONE spoken offer per tick (avoid stacking
                                 # questions when only one pending_offer is tracked)
            for offer in offers:
                # Room-scoped mode: stay quiet about the focused room(s) only.
                if mode_scope_areas:
                    _oa = _offer_area(_CORE.hass, offer)
                    if _oa and _oa in mode_scope_areas:
                        continue
                pkey = offer.get("pattern_key", "")
                # Graduated autonomy: trusted actions execute silently — all of
                # them, since they don't need a yes/no.
                if pkey and _CORE.autonomy_mgr and _CORE.autonomy_mgr.is_autonomous(pkey):
                    ok = await _execute_action_data(
                        _CORE.hass, offer.get("action_data", {}),
                        source="proactive_autonomous")
                    if ok:
                        _CORE.autonomous_actions += 1
                        # Mark cooldown so the same autonomous action doesn't
                        # re-fire every proactive cycle.
                        okey = offer.get("offer_key")
                        if okey:
                            _CORE.proactive_mgr._mark_offered(okey)
                        done_msg = _autonomous_done_message(offer)
                        actions.append({
                            "type": offer.get("type", "proactive") + "_auto",
                            "urgency": "low",
                            "message": done_msg,
                            "auto_act": True,
                        })
                        from .websocket import nova_log
                        nova_log("AUTO", f"autonomous: {pkey} → {done_msg[:60]}")
                elif not spoke_offer:
                    # Offer the FIRST non-autonomous opportunity; remaining ones
                    # wait for a later tick (their cooldown isn't marked, so they
                    # re-surface naturally next cycle).
                    _CORE.offers_made += 1
                    _CORE.pending_offer = offer
                    okey = offer.get("offer_key")
                    if okey:
                        _CORE.proactive_mgr._mark_offered(okey)
                    actions.append(offer)
                    spoke_offer = True
        except Exception as exc:
            _LOGGER.debug("Proactive tick error: %s", exc)

    # ── Energy management (v6.62.0) ─────────────────────────────────
    # Same gating as proactive offers (kill-switch + mode). Surfaces high-draw
    # situations; at higher agency levels may propose/auto-defer a load. Never
    # sheds a critical load (handled inside energy.evaluate_for_proactive).
    if proactive_enabled:
        try:
            from . import energy
            e_offer = energy.evaluate_for_proactive(hass)
            if e_offer:
                actions.append(e_offer)
        except Exception as exc:
            _LOGGER.debug("Energy tick error: %s", exc)

    # Run pattern analysis periodically
    try:
        from .automation.patterns import get_analyzer, set_thresholds
        analyzer = get_analyzer()
        # should_analyze reads patterns.db: keep SQLite off the event loop.
        # A manual analysis already running covers this tick (single flight).
        if not analyzer.analysis_running and await hass.async_add_executor_job(
                analyzer.should_analyze):
            # Loosened-reins defaults (occurrences 4, confidence 0.55) — API spend
            # is no longer the constraint; user can tune via panel-saved keys.
            try:
                _occ = int(config.get("pattern_min_occurrences", 4) or 4)
            except Exception:
                _occ = 4
            try:
                _conf = float(config.get("pattern_confidence", 0.55) or 0.55)
            except Exception:
                _conf = 0.55
            set_thresholds(_occ, _conf)
            from . import suggestion_review
            _reviewer = await suggestion_review.reviewer_for(hass)
            if _reviewer is not None:
                patterns = await analyzer.analyze(hass, reviewer=_reviewer)
            else:
                patterns = await analyzer.analyze(hass)
            if patterns:
                from .websocket import nova_log
                nova_log("LEARN", f"Pattern analysis: {len(patterns)} patterns found")
                # Notify about new high-confidence suggestions
                pending = await hass.async_add_executor_job(
                    analyzer.get_pending_suggestions)
                if pending:
                    honorific = config.get("honorific", "sir")
                    nova_log(
                        "LEARN",
                        f"{len(pending)} automation suggestion(s) pending review",
                    )
    except Exception as exc:
        _LOGGER.debug("Pattern analysis tick error: %s", exc)

    # ── Local cognition: anticipation (v5.9.30) ─────────────────────────────
    # Every ~15 min, sample occupancy and flag entities in a state that's
    # unusual for this time of day ("garage usually closed by now"). Gated by
    # the proactive kill-switch + the cognition toggle. Predictions are appended
    # as actions and flow through the same gated announce path below (so they
    # push-instead-of-speak while you're asleep). Model is persisted each cycle.
    try:
        from . import cognition
        cog_on = True
        try:
            from . import observer as _obs
            cog_on = _obs._cognition_enabled()
        except Exception:
            cog_on = bool(config.get("cognition_enabled", True))

        if proactive_enabled and cog_on:
            now_t = time.time()
            cycle = now_t - getattr(_CORE, "_last_cog_cycle", 0.0) >= cognition.OCC_SAMPLE_INTERVAL
            if cycle:
                _CORE._last_cog_cycle = now_t
                cognition.sample_occupancy(hass, now_t)
                cognition.sample_presence(hass, now_t)
            # Keep the adaptive awareness adjustment warm from off the loop; the
            # predictors below only read its cache.
            try:
                from . import adaptive_awareness
                await adaptive_awareness.async_refresh(hass)
            except Exception:
                pass
            # Departure reminders are due at a minute, not a 15-minute cycle,
            # and are in-memory checks, so they run every tick.
            preds = cognition.predict_presence(hass, now_t)
            if cycle:
                preds += (cognition.predict(hass, now_t)
                          + cognition.predict_overdue(hass, now_t)
                          + cognition.predict_proximity(hass, now_t)
                          + cognition.predict_routine_start(hass, now_t))
                preds += await cognition.predict_departure(hass, now_t)
            for pred in preds:
                actions.append(pred)
                from .websocket import nova_log
                nova_log("LEARN", f"anticipation: {pred.get('message','')[:80]}")
            if cycle or preds:
                # Persist the model and the once-a-day ledger; a reminder
                # between cycles is saved at once so a restart can't repeat it.
                await hass.async_add_executor_job(
                    cognition.save_to_db, _patterns_db()
                )
    except Exception as exc:
        _LOGGER.debug("Cognition anticipation tick error: %s", exc)

    # v6.38: Execute the agent's self-scheduled follow-ups. The agent queued
    # these itself ("check the garage actually closed in 5 minutes") — running
    # them back through its own brain closes the loop across time. Results join
    # the normal action flow so quiet hours / urgency routing apply.
    try:
        from . import followups as _fu
        actions.extend(await _fu.async_process_due(
            hass, config, runner=_make_followup_runner(hass, config)))
    except Exception as exc:
        _LOGGER.debug("Follow-up tick error: %s", exc)

    # v6.40: Engage due goals — outcomes Nova is pursuing across time. Same
    # headless brain as follow-ups; quiet while working, speaks on completion.
    try:
        from . import goals as _goals
        actions.extend(await _goals.async_process_due(
            hass, config, runner=_make_followup_runner(hass, config)))
    except Exception as exc:
        _LOGGER.debug("Goal tick error: %s", exc)

    # Process actions
    for action in actions:
        await _emit_action(hass, config, action, sleeping)


def _make_followup_runner(hass, config):
    """A headless agent invocation for self-scheduled follow-ups: same brain,
    same tools, no user turn. The persona tells the model it queued this work
    itself, so replies read as Nova reporting back, not answering a question."""
    async def _run(instruction: str, context: str) -> str:
        from .agent import run_agent
        from .llm_provider import (
            resolve_provider_credential,
            resolve_provider_endpoint,
        )
        try:
            from .const import CONF_MODEL, DEFAULT_MODEL
            model = config.get(CONF_MODEL, DEFAULT_MODEL)
        except Exception:
            model = config.get("model", "")
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        report_to = f"to {honorific} " if honorific else ""
        # A scheduled run has no one present, so the agent gives it the
        # headless grant (look, check and report — never act). The context
        # was written by the model when it scheduled this, possibly from
        # untrusted text it had read, so it goes in fenced as quoted data,
        # never as instructions in the system prompt.
        persona = (
            f"You are Nova. You scheduled this follow-up yourself earlier and "
            f"it is now due. No one is present for this scheduled run: you can "
            f"check states, look and diagnose with your tools, but you cannot "
            f"control devices or change anything. Check what the follow-up "
            f"asks, then report the outcome {report_to}in one or two "
            f"spoken-style sentences; if something needs doing, say what, so "
            f"the household can decide. If everything is fine, say so briefly."
        )
        if context:
            from .prompt_fence import fence
            persona += "\n\n" + fence(
                context,
                label="FOLLOWUP_CONTEXT",
                noun="is a note you saved with this follow-up when you scheduled it",
                callback_noun="a saved note",
            )
        provider_name = config.get("llm_provider", "groq")
        return await run_agent(
            hass,
            messages=[{"role": "user", "content": instruction}],
            persona=persona,
            provider_name=provider_name,
            api_key=resolve_provider_credential(config, provider_name),
            model=model,
            base_url=resolve_provider_endpoint(config, provider_name),
            temperature=0.4,
            config=config,
        )
    return _run


def _live_runtime_config() -> dict:
    """The owning entry's live runtime_config, read on the event loop. {}
    when the core has no owning entry or that entry is not loaded; a loaded
    entry that has lost its runtime raises NovaRuntimeUnavailable. Never
    reads hass.data."""
    entry = _CORE.entry
    if entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(entry)


async def _emit_action(hass, config, action, sleeping):
    """Announce / push a single cognitive action via the standard routing."""
    message = action.get("message", "")
    urgency = action.get("urgency", "medium")
    action_type = action.get("type", "unknown")
    notify_all = bool(action.get("notify_all", False))

    # Given three days running, it's normal for this home: the third says so,
    # later ones stay quiet (habituation.py). Emergencies are exempt.
    habit_key = action.get("habit_key") or action.get("pattern_key") or action.get("offer_key")
    try:
        from . import habituation
        if habit_key and not habituation.exempt(
                urgency=urgency, kind=action_type, entity_id=action.get("entity_id", "")):
            if habituation.is_quiet(habit_key):
                _LOGGER.info("Cognitive action [%s] quiet (normal for this home): %s",
                             action_type, message[:100])
                return
            message = habituation.with_note(habit_key, message)
            habituation.record(habit_key, action.get("entity_id", ""))
    except Exception as exc:
        _LOGGER.debug("habituation check failed: %s", exc)

    _CORE.actions_taken += 1

    _LOGGER.info(
        "Cognitive action [%s] urgency=%s: %s",
        action_type, urgency, message[:100],
    )

    # One request_id for this whole alert — whichever notify path(s) fire
    # below, and the voice announcement's Spoken History link, all share it.
    from . import action_log
    request_id = action_log.new_request_id()
    # Set on anticipation alerts: the Decision Record the user can rate.
    decision_id = action.get("decision_id")

    # Route announcement. Speech and the phone push each have their own error
    # handling (8.7.16): an announcement that raises must not skip the push for
    # a critical alert, and a push that raises must not undo the speech.
    _snap_url = action.get("snapshot_url")

    async def _push() -> None:
        try:
            if notify_all:
                await _notify_all_devices(hass, config, message, action_type, _snap_url,
                                           request_id=request_id)
            else:
                # Adaptive awareness: the alert itself carries the rating buttons.
                await _push_notification(hass, config, message, action_type, _snap_url,
                                          request_id=request_id,
                                          extra_data=_rating_data(decision_id))
        except Exception as exc:
            _LOGGER.warning("Cognitive: action push failed: %s", exc)

    # Quiet hours: only CRITICAL may speak. Non-critical → phone push only.
    # Time-based (independent of bedroom presence), so nothing slips through.
    in_quiet = False
    try:
        from . import sleep_detection
        in_quiet = sleep_detection._in_quiet_hours(
            config.get("observer_quiet_start", "22:00"),
            config.get("observer_quiet_end", "07:00"),
        )
    except Exception:
        in_quiet = False

    if (sleeping or in_quiet) and urgency != "critical":
        # Push to phone only (no spoken announcement)
        await _push()
        return

    # Speech. "suppressed" is what a failure here means for the push decision
    # below: critical and high alerts are pushed whatever the speech did.
    mode = "suppressed"
    try:
        from .tts_helper import resolve_tts_for_context, async_announce
        from .audio_routing import observer_speak_target

        # Get announcement speakers from config. A loaded entry that has lost
        # its runtime raises here; fall back to no panel selection (the
        # broadcast group in config still applies) and carry on.
        ann_speakers = None
        try:
            rc = _live_runtime_config()
        except Exception as exc:
            _LOGGER.warning(
                "Cognitive: live settings unavailable, using defaults: %s", exc)
            rc = {}
        try:
            raw = rc.get("announcement_speakers")
            if raw:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, list) and parsed:
                    ann_speakers = parsed
        except Exception:
            pass

        broadcast_group = config.get("broadcast_group") or None
        targets, mode = observer_speak_target(
            hass, urgency=urgency,
            broadcast_group=broadcast_group,
            announcement_speakers=ann_speakers,
            is_sleeping=sleeping,
        )

        if targets and mode not in ("suppressed",):
            tts_entity = resolve_tts_for_context(
                hass, "sentinel",
                config.get("tts_engine", "auto"),
                config.get("tts_premium_engine") or None,
                config.get("tts_premium_contexts") or [],
            )
            if tts_entity:
                await async_announce(
                    hass, message, tts_entity, targets,
                    context="sentinel", action_request_id=request_id,
                )
    except Exception as exc:
        _LOGGER.warning("Cognitive: action routing failed: %s", exc)

    # announce_notify_only: a non-critical alert that would have been
    # spoken is routed to notify_only, so the phone gets the alert
    # itself (with the rating buttons) in place of the speech. With
    # the setting off, notify_only keeps its old meaning here (only
    # critical and high alerts are pushed).
    try:
        from . import nova_config
        alert_pushed_instead = (
            mode == "notify_only" and urgency not in ("critical", "high")
            and nova_config.announce_notify_only(hass)
        )
    except Exception:
        alert_pushed_instead = False

    # Adaptive awareness: a spoken alert gets a silent phone
    # notification with the rating buttons, so it can be rated too.
    if (decision_id is not None and urgency not in ("critical", "high")
            and not alert_pushed_instead):
        try:
            from . import adaptive_awareness
            await adaptive_awareness.async_send_rating_prompt(
                hass, config, message, decision_id)
        except Exception as exc:
            _LOGGER.debug("rating prompt failed: %s", exc)

    # Also push critical/high alerts to phones
    if urgency in ("critical", "high") or alert_pushed_instead:
        await _push()


def _rating_data(decision_id) -> dict:
    """Adaptive awareness rating buttons for a phone alert, or {}."""
    if decision_id is None:
        return {}
    try:
        from . import adaptive_awareness
        return adaptive_awareness.rating_push_data(decision_id)
    except Exception:
        return {}


def _lockdown_exempt_locks() -> set:
    """Locks the nighttime sweep must never touch (thermostat child locks,
    etc). Single source of truth is whatever LockdownManager loaded from
    config, so this and the formal lockdown sweep never drift apart."""
    if _CORE.lockdown_mgr is not None:
        return _CORE.lockdown_mgr.exempt_locks
    return LOCKDOWN_EXEMPT_LOCKS_DEFAULT


def is_lockdown() -> bool:
    """True if a formal lockdown is currently active."""
    return bool(_CORE.lockdown_mgr and _CORE.lockdown_mgr.active)


def intrusion_status() -> dict:
    """Snapshot of any active intrusion investigation, for the panel — where the
    search started (the breach) and which rooms activity has reached, so the
    Residence view can show the intruder's route."""
    mgr = _CORE.safety_mgr
    inv = getattr(mgr, "_investigation", None) if mgr else None
    if not inv:
        return {"active": False, "confirmed": False}
    return {
        "active": True,
        "confirmed": bool(inv.get("escalated")),
        "breach_area": inv.get("breach_area"),
        "breach_name": inv.get("breach_name"),
        "path": list(inv.get("path", [])),
        "zones": sorted(str(z) for z in inv.get("zones", set())),
    }


def lockdown_status() -> dict:
    """Lockdown state snapshot for the panel / observability."""
    if _CORE.lockdown_mgr:
        return _CORE.lockdown_mgr.status()
    return {"active": False, "since": 0.0, "reason": "", "auto": False, "exempt_windows": 0}


async def apply_runtime_config(key: str, value) -> None:
    """Apply safety settings immediately without reloading the integration."""
    if key not in ("lockdown_auto_on_arm", "security_alarm_entity",
                   "intrusion_requires_confinement", "face_stand_down"):
        return
    if not isinstance(_CORE.config, dict):
        _CORE.config = {}
    if key == "lockdown_auto_on_arm":
        enabled = value is True
        _CORE.config[key] = enabled
        for component in (_CORE.safety_mgr, _CORE.lockdown_mgr):
            if component is not None:
                component.set_automatic_lockdown(enabled)
    elif key in ("intrusion_requires_confinement", "face_stand_down"):
        enabled = value is True
        _CORE.config[key] = enabled
        for component in (_CORE.safety_mgr, _CORE.lockdown_mgr):
            if component is not None and isinstance(component.config, dict):
                component.config[key] = enabled
    else:
        _CORE.config[key] = value
        for component in (_CORE.safety_mgr, _CORE.lockdown_mgr):
            if component is not None and isinstance(component.config, dict):
                component.config[key] = value
    mgr = _CORE.lockdown_mgr
    if key == "lockdown_auto_on_arm" and value is not True and mgr and mgr.active and mgr.auto:
        mgr.active = False
        mgr.since = 0.0
        mgr.reason = ""
        mgr.auto = False
        mgr.exempt_windows = set()
        mgr._secured_by_us = set()
        mgr._alerted = set()
        mgr._auto_suppressed = False
        await mgr._persist()
        _LOGGER.warning(
            "Automatic lockdown disabled; cleared Nova lockdown state without device actions")


def _ensure_lockdown_mgr(hass: HomeAssistant = None) -> Optional["LockdownManager"]:
    """
    Return the lockdown manager, creating it on demand. Lockdown is a security
    feature, so it must not depend on the cognitive-core loop having started
    cleanly — if start() was interrupted (and swallowed as non-fatal), the
    manager is created here the first time it's needed.
    """
    if _CORE.lockdown_mgr is None:
        h = _CORE.hass or hass
        if h is None:
            return None
        try:
            _CORE.lockdown_mgr = LockdownManager(h, _CORE.config or {})
            if _CORE.hass is None:
                _CORE.hass = h
            _LOGGER.warning("Lockdown manager created on demand (core start had not initialised it)")
        except Exception as exc:
            _LOGGER.error("Lockdown manager create failed: %s", exc)
            return None
    return _CORE.lockdown_mgr


async def ensure_lockdown(hass: HomeAssistant, config: dict) -> None:
    """
    Wire lockdown up independently of the observer / cognitive loop: make sure
    the manager exists, register an event-driven alarm→lockdown sync, and apply
    the current alarm state immediately (so a reboot while the alarm is armed
    re-engages lockdown). Idempotent — safe to call from setup and from start().
    """
    if _CORE.hass is None:
        _CORE.hass = hass
    if not _CORE.config:
        _CORE.config = config or {}
    _ensure_lockdown_mgr(hass)
    if _CORE.alarm_unsub is None:
        _CORE.alarm_unsub = hass.bus.async_listen("state_changed", _on_lockdown_state)
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
        mgr = _CORE.lockdown_mgr
        if mgr is None or not mgr.active or dom not in ("binary_sensor", "cover", "lock"):
            return
        new = event.data.get("new_state")
        if new is None:
            return
        action = await mgr.handle_state_change(eid, event.data.get("old_state"), new)
        if action and _CORE.hass:
            await _emit_action(_CORE.hass, _CORE.config or {}, action, False)
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
    mgr = _CORE.lockdown_mgr
    if mgr is None or _CORE.hass is None:
        return
    from . import safety_config
    if not safety_config.automatic_lockdown_enabled(_CORE.config or {}):
        return
    try:
        armed, disarm_confirmed, indeterminate = _alarm_state_view(_CORE.hass)
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
    if action and _CORE.hass:
        try:
            await _emit_action(_CORE.hass, _CORE.config or {}, action, False)
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
    selected_config = (_CORE.config or {}) if config is None else config
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
    h = _CORE.hass or hass
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
            cfg = _CORE.config or {}
            sleeping, _ = sleep_detection.is_sleeping(
                h,
                bedroom_area_ids=cfg.get("bedroom_areas", []) or [],
                quiet_start=cfg.get("observer_quiet_start", "22:00"),
                quiet_end=cfg.get("observer_quiet_end", "07:00"),
            )
        except Exception:
            sleeping = False
        await _emit_action(h, _CORE.config or {}, action, sleeping)
    return True


async def _push_notification(hass, config, message, action_type, snapshot_url=None,
                              *, request_id=None, extra_data=None):
    """Push notification to phone, with an optional snapshot image (v6.69.0).

    request_id, when given, is the caller's (_emit_action's or
    _notify_all_devices's fallback) — this never mints a second request for
    the same alert."""
    from .notify_targets import async_send_configured_notifications

    data = {"message": message,
            "title": _notify_i18n().title(action_type, _hass_lang(hass))}
    img_data = dict(_notification_image_data(hass, snapshot_url))
    img_data.update(extra_data or {})
    if img_data:
        data["data"] = img_data
    return await async_send_configured_notifications(
        hass, config, data,
        request_id=request_id, action="cognitive_alert", source="proactive",
        requested_state=action_type,
    )


def _notification_image_data(hass, snapshot_url):
    """Build the mobile_app notification `data` block that attaches an image.
    iOS uses `attachment.url`; Android uses `image`. We set both so whichever
    platform receives it renders the snapshot. Returns {} if no snapshot."""
    if not snapshot_url:
        return {}
    # Make the /local path absolute so the companion app can fetch it off-LAN.
    url = snapshot_url
    try:
        if url.startswith("/"):
            base = ""
            try:
                base = str(hass.config.external_url or hass.config.internal_url or "").rstrip("/")
            except Exception:
                base = ""
            if base:
                url = base + url
    except Exception:
        pass
    return {
        "image": url,                       # Android
        "attachment": {"url": url},         # iOS
    }


async def _notify_all_devices(hass, config, message, action_type, snapshot_url=None,
                               *, request_id=None):
    """Push to EVERY connected device — every `notify.mobile_app_*` service the
    HA companion app registered — plus a persistent notification for confirmed
    intrusions. Attaches a snapshot image when provided (v6.69.0). Falls back to
    the single configured service if no per-device services exist.

    request_id, when given, is _emit_action's — every device target and the
    persistent-notification catch-all below are logged under that ONE shared
    request, not one apiece."""
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    title = _notify_i18n().title(action_type, _hass_lang(hass))
    img_data = _notification_image_data(hass, snapshot_url)
    sent = 0
    try:
        services = hass.services.async_services().get("notify", {})
        names = [n for n in services if n.startswith("mobile_app_")]
    except Exception as exc:
        _LOGGER.debug("Cognitive: enumerate notify services failed: %s", exc)
        names = []

    row_ids: dict = {}
    if names:
        row_ids = await hass.async_add_executor_job(
            lambda: action_log.start_many(
                request_id, "cognitive_alert", "proactive",
                [{"key": n, "domain": "notify", "service": n} for n in names],
            )
        )
    for name in names:
        row_id = row_ids.get(name)
        try:
            payload = {"message": message, "title": title}
            if img_data:
                payload["data"] = img_data
            await hass.services.async_call("notify", name, payload, blocking=False)
            sent += 1
            if row_id is not None:
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.set_execution(rid, "accepted")
                )
        except Exception as exc:
            _LOGGER.debug("notify.%s failed: %s", name, exc)
            if row_id is not None:
                await hass.async_add_executor_job(
                    lambda rid=row_id: action_log.set_execution(
                        rid, "failed", reason_code="service_call_failed")
                )

    # Fall back to the configured single service if nothing device-specific fired.
    if sent == 0:
        await _push_notification(hass, config, message, action_type, snapshot_url,
                                  request_id=request_id)

    # Always-visible catch-all for a confirmed intrusion.
    if action_type == "intrusion_confirmed":
        pn_action_id = await hass.async_add_executor_job(
            lambda: action_log.start(
                request_id, "cognitive_alert", "proactive",
                domain="persistent_notification", service="create",
            )
        )
        try:
            await hass.services.async_call(
                "persistent_notification", "create",
                {"message": message, "title": title,
                 "notification_id": "nova_intrusion"},
                blocking=False)
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(pn_action_id, "accepted")
            )
        except Exception:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    pn_action_id, "failed", reason_code="service_call_failed")
            )


async def _execute_action_data(
    hass, action_data: dict, *,
    request_id: Optional[str] = None, source: str = "proactive",
) -> bool:
    """
    Execute a proactive action's service call.

    action_data shape:
      {"domain": "light", "service": "turn_on",
       "entity_ids": ["light.x", ...], "service_data": {...optional...}}

    request_id/source let the caller (an autonomous tick or an explicit
    accept_pending_offer()) own the logged action; if the caller doesn't
    pass one, this mints its own — either way it's the sole logger for this
    one service call, never both."""
    if not action_data:
        return False
    domain = action_data.get("domain")
    service = action_data.get("service")
    entity_ids = action_data.get("entity_ids", [])
    extra = action_data.get("service_data", {}) or {}
    if not domain or not service or not entity_ids:
        return False
    from . import action_log
    if request_id is None:
        request_id = action_log.new_request_id()
    entity_repr = (
        ", ".join(entity_ids) if isinstance(entity_ids, list) else str(entity_ids))
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(
            request_id, "proactive_offer_execute", source,
            domain=domain, service=service, entity_id=entity_repr,
        )
    )
    try:
        await hass.services.async_call(
            domain, service,
            {"entity_id": entity_ids, **extra},
            blocking=True,
        )
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        return True
    except Exception as exc:
        _LOGGER.warning("Proactive action failed (%s.%s): %s", domain, service, exc)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(
                action_id, "failed", reason_code="service_call_failed")
        )
        return False


def _autonomous_done_message(offer: dict) -> str:
    """Convert an offer into a past-tense 'I did this' notification."""
    t = offer.get("type", "")
    ad = offer.get("action_data", {})
    n = len(ad.get("entity_ids", []))
    if t == "proactive_lights":
        return "I turned the lights on for you — it was dark and you were there."
    if t == "proactive_stale_light":
        return "I turned off a light left on in an empty room to save energy."
    if t == "proactive_hvac":
        return "I set the climate back to eco — no one's home."
    return f"I handled {n} device(s) for you automatically."


async def _loop():
    """Main cognitive loop — runs every TICK_INTERVAL seconds."""
    _LOGGER.info("Cognitive Core loop started")
    # Yield before the first tick. Even as a background task, running a full
    # state-scanning tick synchronously at entry would do real work while HA is
    # still bringing entities up — both wasteful (state is incomplete) and
    # needless load during boot. A short delay lets startup settle first.
    try:
        await asyncio.sleep(min(TICK_INTERVAL, 30))
    except asyncio.CancelledError:
        return
    while _CORE.running:
        try:
            await _tick()
        except Exception as exc:
            _LOGGER.warning("Cognitive tick error: %s", exc)
        await asyncio.sleep(TICK_INTERVAL)


# ── Public API ──────────────────────────────────────────────────────────────

def ignore(entity_pattern: str, duration_minutes: int = 0,
           reason: str = "") -> dict:
    """Add an ignore rule. Called by the agent's 'ignore' tool."""
    if _CORE.ignore_mgr:
        rule = _CORE.ignore_mgr.add(entity_pattern, duration_minutes, reason)
        return {
            "success": True,
            "enforced": True,
            "pattern": rule.entity_pattern,
            "duration": duration_minutes,
            "reason": reason,
        }
    return {"success": False, "enforced": False, "error": "Cognitive core not running"}


def unignore(entity_pattern: str) -> dict:
    """Remove an ignore rule, and bring back any matching notification that
    went quiet after three days running (habituation.py)."""
    restored = []
    try:
        from . import habituation
        restored = habituation.forget(entity_pattern)
    except Exception as exc:
        _LOGGER.debug("habituation forget failed: %s", exc)
    if _CORE.ignore_mgr:
        removed = _CORE.ignore_mgr.remove(entity_pattern)
        return {"success": bool(removed or restored), "pattern": entity_pattern,
                "restored_notifications": len(restored)}
    if restored:
        return {"success": True, "pattern": entity_pattern,
                "restored_notifications": len(restored)}
    return {"success": False, "error": "Cognitive core not running"}


def list_ignores() -> list[dict]:
    """List all active ignore rules, plus the notifications that went quiet
    after three days running (remaining_min "normal for this home")."""
    rules = _CORE.ignore_mgr.list_rules() if _CORE.ignore_mgr else []
    try:
        from . import habituation
        rules += [{"pattern": q["entity_id"] or q["key"],
                   "reason": "came up three days running",
                   "expires_at": 0,
                   "remaining_min": "normal for this home"}
                  for q in habituation.quiet_list()]
    except Exception:
        pass
    return rules


def is_ignored(entity_id: str) -> bool:
    """Check if an entity is currently ignored."""
    if _CORE.ignore_mgr:
        return _CORE.ignore_mgr.is_ignored(entity_id)
    return False


def log_command(text: str, handled_by: str = "agent",
                entity_ids: list = None, person: str = "unknown"):
    """Record a command for pattern learning."""
    if _CORE.state_logger:
        _CORE.state_logger.log_command(text, handled_by, entity_ids, person)


def learning_active() -> bool:
    """Whether Nova's pattern-learning subsystem is currently running —
    the master learning setting every downstream consumer (including
    camera_semantic.py, Phase 4 v7.109.0) gates on. There's no separate
    always-on pattern-learning process independent of Observer/Cognitive
    Core: state_logger is only ever created in start() and torn down in
    stop(), so this is the same on/off surface every other state-change
    row already depends on."""
    return bool(_CORE.running and _CORE.state_logger)


def log_camera_event(entity_id: str, new_state: str, area_id: str = "",
                      person: str = "unknown", person_confidence: float = 0.0,
                      detection_confidence: Optional[float] = None) -> bool:
    """Record a synthetic camera-event state change for pattern learning
    (Phase 4, v7.109.0) — the SAME state_changes table and StateLogger every
    other entity's pattern data already goes through, not a parallel store.
    `entity_id` is the synthetic, non-actuating camera_event.<location> id;
    `new_state` is the canonical label or, for package events, the specific
    delivered/stranded/taken/mail sub-state.

    Returns False (a no-op, nothing written) when the learning subsystem
    isn't running — camera events must not be stored while learning is
    disabled, same as everything else state_logger gates on.

    Blocking (SQLite) — callers MUST invoke this via the executor, never
    directly from the event loop."""
    if not learning_active():
        return False
    _CORE.state_logger.log_state_change(
        entity_id, "", new_state, area_id,
        triggered_by="camera", person=person,
        person_confidence=float(person_confidence or 0.0),
        detection_confidence=detection_confidence,
    )
    return True


def status() -> dict:
    """Return cognitive core status for diagnostics."""
    stats = {}
    if _CORE.state_logger:
        stats = _CORE.state_logger.get_pattern_stats()
    last_analysis = {}
    try:
        from .automation.patterns import get_analyzer
        last_analysis = dict(get_analyzer()._last_result)
    except Exception:
        last_analysis = {}
    return {
        "running": _CORE.running,
        "tick_count": _CORE.tick_count,
        "actions_taken": _CORE.actions_taken,
        "offers_made": _CORE.offers_made,
        "autonomous_actions": _CORE.autonomous_actions,
        "autonomy_grants": _CORE.autonomy_mgr.list_grants() if _CORE.autonomy_mgr else [],
        "uptime_hours": round((time.time() - _CORE.startup_time) / 3600, 1)
        if _CORE.startup_time else 0,
        "last_tick_ago": round(time.time() - _CORE.last_tick, 1)
        if _CORE.last_tick else 0,
        "ignore_rules": len(list_ignores()),
        "learning": stats,
        "last_analysis": last_analysis,
    }


def _backfill_filter_states(events: list, interval: float, cap: int = 1000) -> list:
    """Chronological ``(epoch, state)`` events → filtered ``[(epoch, state)]``
    for backfill: drop unavailable/unknown and no-change, apply the per-entity
    rate limit, and keep only the most recent ``cap``. This is the anti-flood
    core — importing 30 days of a chatty motion sensor's history through this
    stays bounded, so backfill can't reintroduce the flood that was just killed.
    """
    from collections import deque
    out: deque = deque(maxlen=max(1, cap))
    last = None
    prev = None
    for epoch, st in events:
        if st is None or st in ("unavailable", "unknown") or st == prev:
            continue
        prev = st
        if last is not None and (epoch - last) < interval:
            continue
        out.append((epoch, st))
        last = epoch
    return list(out)


async def backfill_from_history(hass: HomeAssistant, days: int = 30) -> dict:
    """Import recent recorder history for pattern-relevant entities Nova isn't
    already logging (e.g. motion/occupancy just opted in), so Analyze Now can
    find routines from PAST behavior instead of only data since a setting was
    enabled. Safe by construction: applies the same domain filter + class-aware
    rate limit as live logging (motion capped hard), only touches entities with
    no existing rows (no double-count), and is capped per-entity and globally.
    """
    out = {"imported": 0, "entities": 0, "considered": 0}
    if not _CORE.state_logger:
        return out
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
        from .automation.recorder_time import recorder_epoch
    except Exception:
        return out

    _meta =("automation", "script", "scene", "input_boolean", "input_number")
    _noisy = ("sensor", "binary_sensor", "weather", "sun", "update", "device_tracker")
    dc_by: dict = {}
    relevant: list = []
    try:
        for state in hass.states.async_all():
            eid = state.entity_id
            dom = eid.split(".")[0]
            dc = state.attributes.get("device_class") or ""
            dc_by[eid] = dc
            if dom in _meta:
                continue
            if dom in _noisy and not _pattern_opted_in(eid, dc):
                continue
            relevant.append(eid)
    except Exception:
        return out
    if not relevant:
        return out

    existing = await hass.async_add_executor_job(
        _CORE.state_logger.distinct_logged_entities)
    todo = [e for e in relevant if e not in existing][:250]   # cap breadth
    out["considered"] = len(todo)
    if not todo:
        return out

    try:
        days = max(1, min(int(days), 30))
    except Exception:
        days = 30
    end = dt_util.utcnow()
    start = end - timedelta(days=days)

    def _fetch():
        return history.get_significant_states(
            hass, start, end, todo, minimal_response=True, no_attributes=True)

    try:
        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        _LOGGER.debug("Nova backfill: history fetch failed: %s", exc)
        return out
    if not raw:
        return out

    rows: list = []
    for eid, states in raw.items():
        dom = eid.split(".")[0]
        interval = _pattern_log_interval(dc_by.get(eid, ""))
        events: list = []
        for s in states:
            try:
                st = getattr(s, "state", None)
                when = (getattr(s, "last_changed", None)
                        or getattr(s, "last_updated", None))
                if st is None and isinstance(s, dict):
                    st = s.get("state")
                    when = s.get("last_changed") or s.get("last_updated")
                if st is None or when is None:
                    continue
                epoch = recorder_epoch(when)
                if epoch is not None:
                    events.append((epoch, st))
            except Exception:
                continue
        kept = _backfill_filter_states(events, interval)
        for epoch, st in kept:
            rows.append((datetime.fromtimestamp(epoch).isoformat(),
                         eid, dom, "unknown", st))
        if kept:
            out["entities"] += 1
        if len(rows) >= 20000:                                # global safety cap
            break

    out["imported"] = await hass.async_add_executor_job(
        _CORE.state_logger.bulk_insert_history, rows)
    return out


async def run_analysis_now(hass: HomeAssistant) -> dict:
    """Force a pattern-analysis pass now, bypassing ONLY the 6-hour throttle.

    The data-sufficiency gate still applies (>= 7 days and >= 50 recorded
    changes), so this can't produce noise on a fresh install. Returns a result
    dict the panel can show: whether it ran, and if not, why; if it did, how many
    patterns were found and suggestions stored.
    """
    from .automation.patterns import get_analyzer, set_thresholds
    analyzer = get_analyzer()
    analyzer._last_analysis = 0.0  # bypass the 6h throttle for this manual run

    # Backfill recorder history for any pattern-relevant entities we aren't
    # logging yet (e.g. motion/occupancy just opted in), so this finds routines
    # from PAST behavior instead of only data since the setting was enabled.
    backfill = {}
    try:
        backfill = await backfill_from_history(hass, days=30)
    except Exception:
        backfill = {}

    stats = {}
    if _CORE.state_logger:
        try:
            stats = await hass.async_add_executor_job(
                _CORE.state_logger.get_pattern_stats)
        except Exception:
            stats = {}

    if not await hass.async_add_executor_job(analyzer.should_analyze):
        return {
            "ran": False,
            "reason": ("Not enough history yet — pattern learning needs about a "
                       "week of data (\u2265 7 days and \u2265 50 recorded changes)."),
            "days_of_data": stats.get("days_of_data", 0),
            "state_changes": stats.get("state_changes", 0),
            "backfill": backfill,
        }

    cfg = _CORE.config or {}
    try:
        _occ = int(cfg.get("pattern_min_occurrences", 4) or 4)
    except Exception:
        _occ = 4
    try:
        _conf = float(cfg.get("pattern_confidence", 0.55) or 0.55)
    except Exception:
        _conf = 0.55
    set_thresholds(_occ, _conf)

    from . import suggestion_review
    _reviewer = await suggestion_review.reviewer_for(hass)
    if _reviewer is not None:
        patterns = await analyzer.analyze(hass, reviewer=_reviewer)
    else:
        patterns = await analyzer.analyze(hass)
    res = dict(analyzer._last_result)
    res["ran"] = True
    res.setdefault("patterns_found", len(patterns))
    res["state_changes"] = stats.get("state_changes", 0)
    res["days_of_data"] = stats.get("days_of_data", 0)
    res["backfill"] = backfill
    try:
        res["diagnostic"] = await hass.async_add_executor_job(
            analyzer.pattern_diagnostic)
    except Exception:
        res["diagnostic"] = {}
    return res


# ── Proactive offer API (v5.9.07) ───────────────────────────────────────────

def get_pending_offer() -> Optional[dict]:
    """Return the offer currently awaiting a yes/no, if any."""
    return _CORE.pending_offer


async def accept_pending_offer() -> dict:
    """
    User said yes to the pending proactive offer. Execute it and record the
    acceptance toward graduated autonomy. Returns a result dict.
    """
    offer = _CORE.pending_offer
    if not offer:
        return {"ok": False, "reason": "no pending offer"}
    _CORE.pending_offer = None
    ok = await _execute_action_data(
        _CORE.hass, offer.get("action_data", {}), source="proactive_accepted")
    pkey = offer.get("pattern_key", "")
    if ok and pkey and _CORE.autonomy_mgr:
        grant = _CORE.autonomy_mgr.record_acceptance(pkey, confidence=0.9)
        _CORE.actions_taken += 1
        return {
            "ok": True, "pattern_key": pkey,
            "approvals": grant.get("approvals", 0),
            "now_autonomous": grant.get("granted", False),
        }
    return {"ok": ok}


def decline_pending_offer() -> dict:
    """User said no. Clear the offer and reset trust toward that pattern."""
    offer = _CORE.pending_offer
    _CORE.pending_offer = None
    if offer and _CORE.autonomy_mgr:
        pkey = offer.get("pattern_key", "")
        if pkey:
            _CORE.autonomy_mgr.record_rejection(pkey)
    return {"ok": True}


def revoke_autonomy(pattern_key: str) -> dict:
    """Revoke a previously-granted autonomous action."""
    if _CORE.autonomy_mgr and _CORE.autonomy_mgr.revoke(pattern_key):
        return {"ok": True, "pattern_key": pattern_key}
    return {"ok": False, "reason": "no such grant"}


# ── Start / Stop ────────────────────────────────────────────────────────────

async def start(hass: HomeAssistant, config: dict, entry=None) -> None:
    """Start the cognitive core.

    `entry` is the config entry that owns the core (the observer passes its
    own): its NovaRuntime supplies the automation-context tracker and the
    live panel settings."""
    # Ownership first: a loaded entry that has lost its runtime raises here,
    # before the core changes any state.
    contexts = None
    if entry is not None:
        from .runtime import current_runtime
        runtime = current_runtime(entry)
        if runtime is not None:
            contexts = runtime.automation_contexts

    if _CORE.running:
        await stop()

    _CORE.hass = hass
    _CORE.config = config
    _CORE.running = True
    _CORE.startup_time = time.time()
    _CORE.tick_count = 0
    _CORE.actions_taken = 0
    _CORE.offers_made = 0
    _CORE.autonomous_actions = 0
    _CORE.pending_offer = None
    # The owning entry's tracker, resolved above before any state changed;
    # state changes never look it up again.
    _CORE.automation_contexts = contexts
    _CORE.entry = entry

    _CORE.ignore_mgr = await hass.async_add_executor_job(IgnoreManager)
    _CORE.safety_mgr = SafetyManager(hass, config)
    _CORE.lockdown_mgr = await hass.async_add_executor_job(
        LockdownManager, hass, config)          # __init__ reads lockdown_state.json
    _CORE.proactive_mgr = ProactiveManager(hass, config)
    _CORE.autonomy_mgr = await hass.async_add_executor_job(AutonomyManager)
    _CORE.state_logger = await hass.async_add_executor_job(StateLogger)

    # Lockdown alarm-sync (engage on arm / lift on disarm), event-driven and
    # independent of the loop below; applies the current alarm state now.
    try:
        await ensure_lockdown(hass, config)
    except Exception as exc:
        _LOGGER.warning("Lockdown wiring in start() failed: %s", exc)

    # Restore the cognition model (per-entity rhythm) so anticipation survives
    # restarts and keeps accumulating across days.
    try:
        from . import cognition
        await hass.async_add_executor_job(cognition.load_from_db, _patterns_db())
    except Exception as exc:
        _LOGGER.debug("cognition load on start failed: %s", exc)

    _CORE.unsub = hass.bus.async_listen("state_changed", _on_state_changed)
    # The cognitive loop runs for the lifetime of the integration. It MUST be a
    # *background* task — a plain async_create_task is tracked as part of config-
    # entry setup, so HA's bootstrap waits on it to finish before completing
    # startup. Since the loop never returns, that wait runs to the full timeout
    # and HA logs "Something is blocking Home Assistant from wrapping up the
    # start up phase … waiting for tasks: _loop()". Background tasks are exempt
    # from that wait by design. (Fallback for cores predating the helper.)
    if hasattr(hass, "async_create_background_task"):
        _CORE.task = hass.async_create_background_task(_loop(), "nova_cognitive_loop")
    else:
        _CORE.task = hass.async_create_task(_loop())

    stats = await hass.async_add_executor_job(
        _CORE.state_logger.get_pattern_stats
    )
    _LOGGER.info(
        "Nova Cognitive Core started — %d days of data, "
        "%d state changes logged, %d patterns learned, "
        "%d active ignore rules, %d autonomy grants",
        stats.get("days_of_data", 0),
        stats.get("state_changes", 0),
        stats.get("patterns", 0),
        len(_CORE.ignore_mgr.list_rules()),
        len(_CORE.autonomy_mgr.list_grants()),
    )


async def stop() -> None:
    """Stop the cognitive core."""
    _CORE.running = False
    _CORE.pending_offer = None  # don't let a stale offer survive a restart
    _CORE.automation_contexts = None
    _CORE.entry = None
    if _CORE.task:
        _CORE.task.cancel()
        try:
            await _CORE.task
        except (asyncio.CancelledError, Exception):
            pass
        _CORE.task = None
    if _CORE.unsub:
        try:
            _CORE.unsub()
        except Exception:
            pass
        # Cleared so a second stop() (unload after a failed setup, repeated
        # unload) can't call Home Assistant's remove-listener twice.
        _CORE.unsub = None
    if _CORE.alarm_unsub:
        try:
            _CORE.alarm_unsub()
        except Exception:
            pass
        _CORE.alarm_unsub = None
    _LOGGER.info(
        "Nova Cognitive Core stopped — %d ticks, %d actions taken",
        _CORE.tick_count, _CORE.actions_taken,
    )


def release_runtime() -> None:
    """Drop the Home Assistant reference, config and lockdown manager owned by
    an unloaded entry, so the next setup's ensure_lockdown() builds a fresh
    manager from the new hass and current config.

    Called by async_unload_entry after stop(), never while Nova is loaded
    (stop() alone also runs for nova.observer_stop and a core restart, where
    the manager must stay). Touches no device and writes nothing: the
    persisted lockdown state stays on disk and the next LockdownManager
    restores it. Idempotent.
    """
    _CORE.lockdown_mgr = None
    _CORE.hass = None
    _CORE.config = {}


# Name -> the core module that owns it.
_OWNERS = {
    'ALARM_ARMED_STATES': _m_common,
    'AUTONOMY_MIN_CONFIDENCE': _m_common,
    'AUTONOMY_TRUST_THRESHOLD': _m_common,
    'AutonomyManager': _m_autonomy,
    'DARK_LUX_THRESHOLD': _m_common,
    'FREEZE_CRITICAL_TEMP_F': _m_common,
    'FREEZE_WARN_TEMP_F': _m_common,
    'HIGH_TEMP_AWAY_F': _m_common,
    'INTRUSION_CLEAR_QUIET_SECS': _m_common,
    'INTRUSION_INWARD_DEPTH': _m_common,
    'INTRUSION_MAX_INVESTIGATE_SECS': _m_common,
    'INTRUSION_RESPONSE_TIMEOUT_SECS': _m_common,
    'INTRUSION_SPREAD_ZONES': _m_common,
    'INTRUSION_SUSTAINED_SECS': _m_common,
    'IgnoreManager': _m_ignore,
    'IgnoreRule': _m_ignore,
    'LOCKDOWN_BREACH_COOLDOWN': _m_common,
    'LOCKDOWN_CHECK_INTERVAL': _m_common,
    'LOCKDOWN_DOOR_COVER_CLASSES': _m_common,
    'LOCKDOWN_EXEMPT_LOCKS_DEFAULT': _m_common,
    'LOCKDOWN_SECURE_VERIFY_DELAY': _m_common,
    'LOW_TEMP_AWAY_F': _m_common,
    'PROACTIVE_CHECK_INTERVAL': _m_common,
    'PROACTIVE_OFFER_COOLDOWN': _m_common,
    'STALE_LIGHT_MINUTES': _m_common,
    'StateLogger': _m_pattern_store,
    'TICK_INTERVAL': _m_common,
    '_f_to_unit': _m_common,
    '_fmt_temp': _m_common,
    '_hass_lang': _m_common,
    '_live_honorific': _m_common,
    '_notify_i18n': _m_common,
    '_persona': _m_common,
    '_temp_to_f': _m_common,
    'discover_outdoor_temp': _m_common,
    'is_outdoor_notable': _m_ignore,
    'write_json_atomic': _m_common,
}


class _Facade(types.ModuleType):
    """Forward writes of an owned name to its owning module as well, and
    read an owned name from its owner, so a value the owner reassigns
    itself (_ALARM_INDET_LOG_TS) is never read stale from here."""

    def __getattribute__(self, name):
        owner = _OWNERS.get(name)
        if owner is not None:
            return getattr(owner, name)
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        owner = _OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        owner = _OWNERS.get(name)
        if owner is not None and hasattr(owner, name):
            delattr(owner, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade
