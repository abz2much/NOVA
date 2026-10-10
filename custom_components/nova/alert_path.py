"""One decide and deliver path for Nova's alerts (8.22.0).

Before 8.22.0 every alert source decided for itself whether to alert, who to
tell and how, each with its own copy of the quiet hours, sleep and presence
rules. This module holds all of those decisions:

* situation()  reads the household once: residents and alarm posture from
               household.py, plus whether the house is asleep and whether it
               is quiet hours.
* for_*()      one function per source decides: alert or not, speak (and on
               which speakers), push to phones, or both. Each keeps the rule
               that source used before; where a source never looked at who is
               home, it still does not, so nothing it delivers has changed.
* deliver()    carries out a plan with the source's own speak and push
               functions, in the source's order.
* nobody_home  the wording rule: "no one is home" is only ever said when the
               residents are known to be away.

Intrusion decisions stay in core_safety.py; their actions reach the speakers
and phones through for_core_action(), like every other core action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

_LOGGER = logging.getLogger(__name__)

HIGH_OR_CRITICAL = ("high", "critical")


# ── the situation ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Situation:
    """One reading of the house for one alert."""
    house: object = None          # household.Household, or None if unreadable
    sleeping: bool = False
    quiet: bool = False

    @property
    def residents(self) -> str:
        from . import household
        return getattr(self.house, "residents", household.UNKNOWN)

    @property
    def anyone_home(self) -> bool:
        return bool(getattr(self.house, "anyone_home", False))


def situation(hass, config: Optional[dict] = None, *, sleeping: bool = False,
              quiet: bool = False, house=None) -> Situation:
    """Read the household (household.py) and pair it with the sleep and quiet
    hours readings the source already took. Never raises."""
    if house is None:
        try:
            from . import household
            house = household.snapshot(hass, config)
        except Exception as exc:
            _LOGGER.debug("alert_path: household reading failed: %s", exc)
            house = None
    return Situation(house=house, sleeping=bool(sleeping), quiet=bool(quiet))


def nobody_home(sit: Situation) -> bool:
    """The wording rule: say "no one is home" only when the residents are
    known to be away. Unknown is never away."""
    from . import household
    return sit.residents == household.AWAY


# ── the plan ────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    """What to do with one alert."""
    alert: bool = True                # False: nothing is spoken or pushed
    speak: bool = False
    targets: list = field(default_factory=list)
    push: bool = False
    push_first: bool = False          # push, then speak (else speak, then push)
    speak_if_unsent: bool = False     # speak after all if no phone took the push
    pushed_instead: bool = False      # a core alert pushed in place of speech
    record: bool = True               # the source records it in the output gate
    mode: str = ""                    # the speaker routing mode, for the log
    reason: str = ""

    @property
    def phone_only(self) -> bool:
        return self.push and not self.speak


_SILENT = "silent"


async def deliver(plan: Plan, *, speak: Optional[Callable[[list], Awaitable]] = None,
                  push: Optional[Callable[[], Awaitable]] = None) -> dict:
    """Carry out `plan` with the source's own functions. `speak(targets)` and
    `push()` are awaited; push returns what was sent (a list, or a truthy
    value). Errors are the source's own to handle, as before. Returns
    {"spoke": bool, "pushed": value}."""
    out = {"spoke": False, "pushed": None}
    if not plan.alert:
        return out

    async def _do_speak():
        if plan.speak and speak is not None:
            out["speak_result"] = await speak(plan.targets)
            out["spoke"] = True

    async def _do_push():
        if plan.push and push is not None:
            out["pushed"] = await push()

    if plan.push_first:
        await _do_push()
        await _do_speak()
    else:
        await _do_speak()
        await _do_push()
    if (plan.speak_if_unsent and plan.push and not plan.speak
            and not out["pushed"] and speak is not None):
        out["speak_result"] = await speak(plan.targets)
        out["spoke"] = True
    return out


def _log(source: str, sit: Situation, urgency: str, plan: Plan) -> Plan:
    _LOGGER.debug(
        "alert_path: %s urgency=%s residents=%s sleeping=%s quiet=%s -> "
        "alert=%s speak=%s push=%s mode=%s %s",
        source, urgency, sit.residents, sit.sleeping, sit.quiet,
        plan.alert, plan.speak, plan.push, plan.mode, plan.reason)
    return plan


# ── the output gate (mute list, rate limit, repeats) ────────────────────────

def gate(*, entity_id: str, category: str, urgency: str, message: str) -> tuple:
    """Ask the output gate whether this message may go out. Returns
    (allowed, message): a blocked message is recorded as not spoken, an
    allowed one gets its habit note."""
    from . import output_gate
    allowed, reason = output_gate.can_announce(
        entity_id=entity_id, category=category, urgency=urgency, message=message)
    if not allowed:
        _LOGGER.info("alert_path: output gate held '%s' — %s", message[:80], reason)
        output_gate.record_announcement(
            entity_id=entity_id, category=category,
            urgency=urgency, message=message, was_spoken=False)
        return False, message
    return True, output_gate.habit_note(
        entity_id=entity_id, category=category, urgency=urgency, message=message)


# ── one decision per source ─────────────────────────────────────────────────

def held(sit: Situation, urgency: str) -> bool:
    """Below critical, an observer alert is held while the house is asleep."""
    from .cognitive import evaluators
    return evaluators.held_for_sleep(urgency, sit.sleeping)


def for_observer(hass, sit: Situation, urgency: str, *, broadcast_group=None,
                 announcement_speakers=None, announcements_on: bool = True) -> Plan:
    """Household events the observer reasoned about.

    Below critical nothing is spoken while asleep or in quiet hours. The
    speakers are chosen by urgency and presence, with "is anyone home" taken
    from the household reading (people and their linked trackers, never
    motion). High and critical are always pushed too. With announcements
    off, high and critical are pushed and nothing is spoken."""
    from . import audio_routing
    if held(sit, urgency):
        return _log("observer", sit, urgency, Plan(alert=False, reason="held for sleep"))
    targets, mode = audio_routing.observer_speak_target(
        hass, urgency=urgency, broadcast_group=broadcast_group,
        announcement_speakers=announcement_speakers,
        is_sleeping=(sit.sleeping or sit.quiet),
        authoritative_anyone_home=sit.anyone_home,
    )
    high = urgency in HIGH_OR_CRITICAL
    if mode == "suppressed":
        plan = Plan(alert=False, mode=mode, reason="route suppressed")
    elif mode == "notify_only":
        plan = Plan(push=True, mode=mode, reason="notify only")
    elif not targets:
        plan = Plan(alert=False, record=False, mode=mode, reason="no speakers")
    elif not announcements_on:
        plan = Plan(alert=high, push=high, mode=mode, reason="announcements off")
    else:
        plan = Plan(speak=True, targets=list(targets), push=high, mode=mode)
    return _log("observer", sit, urgency, plan)


def for_appliance(hass, sit: Situation, *, broadcast_group=None,
                  announcement_speakers=None, announcements_on: bool = True) -> Plan:
    """An appliance finished its cycle (always medium urgency).

    Unchanged from before 8.22.0: nothing with announcements off; otherwise
    the speakers are chosen like any medium alert while asleep or awake. The
    speaker routing still judges "is anyone home" from live motion as well
    as people, and quiet hours are not applied here."""
    from .audio_routing import observer_speak_target
    if not announcements_on:
        return _log("appliance", sit, "medium",
                    Plan(alert=False, reason="announcements off"))
    targets, mode = observer_speak_target(
        hass, urgency="medium", broadcast_group=broadcast_group,
        announcement_speakers=announcement_speakers, is_sleeping=sit.sleeping)
    if mode == "notify_only":
        plan = Plan(push=True, mode=mode, reason="notify only")
    elif mode == "suppressed" or not targets:
        plan = Plan(alert=False, mode=mode, reason="no speakers")
    else:
        plan = Plan(speak=True, targets=list(targets), mode=mode)
    return _log("appliance", sit, "medium", plan)


def for_sentinel(sit: Situation, *, notify_only_setting: bool) -> Plan:
    """A door, window, garage or lock left open (Sentinel).

    Always pushed to the phones. Spoken unless the house is asleep. With
    "announce notify only" on, the push is the alert, and it is spoken only
    if no phone took it."""
    if sit.sleeping:
        plan = Plan(push=True, reason="asleep")
    elif notify_only_setting:
        plan = Plan(push=True, speak_if_unsent=True, reason="notify only setting")
    else:
        plan = Plan(speak=True, push=True)
    return _log("sentinel", sit, "medium", plan)


def for_hazard(sit: Situation, *, speak_allowed: bool) -> Plan:
    """A hazard feed: earthquakes, weather warnings, natural disasters.

    Always pushed to every phone first. Spoken on the whole house speakers
    only when the caller's level rules allow it: the legacy feeds outside
    quiet hours, the warnings by their speak and night levels."""
    plan = Plan(push=True, push_first=True, speak=bool(speak_allowed))
    return _log("hazard", sit, "high", plan)


def for_package(sit: Situation, kind: str, *, announcements_on: bool) -> Plan:
    """A package delivered, mail arrived, or a package removed.

    Spoken with announcements on and outside quiet hours. A package removed
    is only an alert while the residents are away (unknown counts as home),
    and is then pushed to the phones as well, quiet hours or not."""
    can_speak = announcements_on and not sit.quiet
    if kind == "removed":
        if not nobody_home(sit):
            plan = Plan(alert=False, reason="someone may be home")
        else:
            plan = Plan(push=True, push_first=True, speak=can_speak)
    else:
        plan = Plan(alert=can_speak, speak=can_speak,
                    reason="" if can_speak else "quiet hours or announcements off")
    return _log("package", sit, "medium", plan)


def for_doorbell(sit: Situation) -> Plan:
    """A notable doorbell or camera event Nova chose to speak. Spoken on the
    speakers the doorbell handler chose; no phone push, as before."""
    return _log("doorbell", sit, "medium", Plan(speak=True))


def for_core_action(hass, config: dict, sit: Situation, action: dict) -> Plan:
    """An action from the cognitive core: intrusion, lockdown, freeze, the
    proactive offers and the anticipation alerts.

    Unchanged from before 8.22.0. Below critical, asleep, quiet hours or a
    phone only action (the first intrusion alert with residents home) is
    pushed and not spoken. Otherwise the speakers are chosen by urgency
    (routing still judges presence from live motion as well as people, so an
    intruder's movement while armed away is still spoken), and high and
    critical are pushed too. With "announce notify only" on, a lower alert
    that would have been spoken is pushed instead."""
    urgency = action.get("urgency", "medium")
    if ((sit.sleeping or sit.quiet or action.get("phone_only"))
            and urgency != "critical"):
        return _log("core", sit, urgency,
                    Plan(push=True, reason="asleep, quiet hours or phone only"))

    mode, targets = "suppressed", []
    try:
        targets, mode = _core_speakers(hass, config, urgency, sit.sleeping)
    except Exception as exc:
        _LOGGER.warning("Cognitive: action routing failed: %s", exc)
        mode, targets = "suppressed", []

    pushed_instead = False
    try:
        from . import nova_config
        pushed_instead = (mode == "notify_only" and urgency not in HIGH_OR_CRITICAL
                          and nova_config.announce_notify_only(hass))
    except Exception:
        pushed_instead = False
    speak = bool(targets) and mode not in ("suppressed",)
    plan = Plan(speak=speak, targets=list(targets or []),
                push=(urgency in HIGH_OR_CRITICAL or pushed_instead),
                pushed_instead=pushed_instead, mode=mode,
                reason="pushed instead of spoken" if pushed_instead else "")
    return _log("core", sit, urgency, plan)


def _core_speakers(hass, config: dict, urgency: str, sleeping: bool):
    """The announcement speakers for a core action, from the panel's
    selection or the broadcast group in config."""
    import json
    from .audio_routing import observer_speak_target
    ann_speakers = None
    try:
        from . import core_delivery
        rc = core_delivery._live_runtime_config()
    except Exception as exc:
        _LOGGER.warning("Cognitive: live settings unavailable, using defaults: %s", exc)
        rc = {}
    try:
        raw = rc.get("announcement_speakers")
        if raw:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list) and parsed:
                ann_speakers = parsed
    except Exception:
        pass
    return observer_speak_target(
        hass, urgency=urgency,
        broadcast_group=config.get("broadcast_group") or None,
        announcement_speakers=ann_speakers,
        is_sleeping=sleeping,
    )
