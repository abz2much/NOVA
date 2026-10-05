"""SafetyManager: pipe freeze warnings, the intrusion investigation and the
nighttime lockdown sweep.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant

from . import core_common as _m_common
from .core_bridge import _lockdown_exempt_locks, is_lockdown
from .core_common import (
    ALARM_ARMED_STATES,
    FREEZE_CRITICAL_TEMP_F,
    FREEZE_WARN_TEMP_F,
    INTRUSION_CLEAR_QUIET_SECS,
    INTRUSION_INWARD_DEPTH,
    INTRUSION_MAX_INVESTIGATE_SECS,
    INTRUSION_RESPONSE_TIMEOUT_SECS,
    INTRUSION_SPREAD_ZONES,
    INTRUSION_SUSTAINED_SECS,
    LOCKDOWN_CHECK_INTERVAL,
    _f_to_unit,
    _fmt_temp,
    _notify_i18n,
    _persona,
    _temp_to_f,
    discover_outdoor_temp,
)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


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
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
        lang = _m_common._hass_lang(self.hass)

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
        _lang = _m_common._hass_lang(self.hass)
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
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware

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
            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
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
            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
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
            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
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
        honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware

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
            lang = _m_common._hass_lang(self.hass)
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
