"""One delivery request for every proactive message (8.44.0, design D3).

Before 8.44.0 each proactive sender (hazards, packages, the doorbell,
reminders, briefings and the rest) applied its own mix of the household's
rules: some heard the Announcements switch, some honoured "shush", some had
their own idea of quiet hours. A sender now describes its message as a
DeliveryRequest, and async_deliver() applies the same rules, in the same
order, to every one of them:

1. Critical speaks everywhere and goes to the phones. Nothing holds it.
2. A mute, "shush", the hourly limit or a repeat of the same message holds
   it (output_gate). Only a source that always pushes (a hazard warning, a
   briefing) still reaches the phones: shush quiets the speakers.
3. With the Announcements switch off nothing is spoken. A high message, or
   one the source always pushes, still goes to the phones.
4. In quiet hours, or while the house is asleep, nothing is spoken. A high
   message, the person's own, or one the source always pushes goes to the
   phones instead; an everyday (medium or low) alert is dropped (8.45.0).
5. With "Notifications only" on, the message goes to the phones instead of
   the speakers.

Reminders and briefings the person set themselves (requested_by_user) skip
steps 2 and 3: shush and the Announcements switch never swallow something
they asked for. Quiet hours and sleep still apply to them, unless the person
chose the time themselves (quiet_ok: a reminder set to speak in quiet hours
too, a briefing on their schedule, or nova.briefing called now). Sleep
still applies, except to a hazard warning at or above the household's night
level (sleep_ok), which is meant to wake the house.

Each request may carry a repeat label (dedup_key): the same label within
the output gate's repeat window counts as a repeat whatever the words.
Without one, repeats are judged on the words. Habituation ("I'll stop
mentioning this" after three days running) counts days by the entity, or by
habit_key when given ("" when the source counts its own, as the core does).

Every request's outcome (spoken, pushed, the reason and its Actions log
request) is written to the activity log (8.45.0); safety kinds also go to
the Decision Record (record_kind).

Quiet hours are the household's one setting (observer_quiet_start and
observer_quiet_end), read by in_quiet_hours(); no sender keeps its own.

decide() is the rule order on its own, with no Home Assistant access, so it
can be tested and read in one place. Conversation replies never come here.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

URGENCIES = ("low", "medium", "high", "critical")

# When a message may also go to the phones.
PUSH_NEVER = "never"          # speakers only; held if it cannot be spoken
PUSH_FALLBACK = "fallback"    # the phones only when it cannot be spoken
PUSH_HIGH = "high"            # high and critical are pushed as well as spoken;
                              # below high only with "Notifications only" on
PUSH_ALWAYS = "always"        # always pushed, spoken too when allowed
PUSH_MODES = (PUSH_NEVER, PUSH_FALLBACK, PUSH_HIGH, PUSH_ALWAYS)


@dataclass(frozen=True)
class DeliveryRequest:
    """One proactive message and how its source wants it delivered."""
    source: str                       # e.g. "hazard", "reminder", "doorbell"
    message: str
    urgency: str = "medium"
    entity_id: str = ""
    category: str = ""                # output_gate category; source when empty
    requested_by_user: bool = False   # a reminder or briefing the person set
    speakers: tuple = ()              # the source's own speakers; empty: routed
    push: str = PUSH_FALLBACK
    all_phones: bool = False          # every companion app, not the chosen services
    push_kind: str = ""               # the push's kind, for its title; source when empty
    title: str = ""
    tts_context: str = "sentinel"
    tts_entity: str = ""              # the source's own TTS engine; empty: resolved
    spoken: str = ""                  # the words to speak, when not the message
    phone_only: bool = False          # the source wants it pushed, not spoken
    push_first: bool = False          # push before speaking (a hazard warning)
    quiet_ok: bool = False            # the person asked for it in quiet hours too
    sleep_ok: bool = False            # spoken even while the house sleeps (a
                                      # hazard at or above the night level)
    # The source's own config, for its chosen phone services. The live
    # settings still win (notify_targets).
    config: Optional[dict] = field(default=None, compare=False, hash=False)
    dedup_key: str = ""               # the repeat label (8.45.0); "": by the words
    habit_key: Optional[str] = None   # habituation key; None: the entity,
                                      # "": the source counts its own days
    asleep: Optional[bool] = None     # the source's own sleep reading (the
                                      # core's tick); None: read here
    # The reading the source already took for this event (an
    # alert_path.Situation, 8.46.0): sleep, quiet hours and who is home come
    # from it instead of being read again. None: read here.
    reading: Any = field(default=None, compare=False, hash=False)
    motion_counts: bool = False       # speaker routing may count live motion as
                                      # someone home (an intruder's movement)
    record_kind: str = ""             # Decision Record kind; empty: not recorded
    record_facts: Optional[dict] = field(default=None, compare=False, hash=False)
    record_assessment: str = ""

    @property
    def gate_category(self) -> str:
        return self.category or self.source


@dataclass(frozen=True)
class Conditions:
    """The household's rules at the moment of delivery."""
    announcements_on: bool = True
    notify_only: bool = False
    quiet_hours: bool = False
    asleep: bool = False
    gate_allowed: bool = True
    gate_reason: str = ""
    anyone_home: Optional[bool] = None   # None: unknown
    # The reading these came from, kept for the Decision Record (8.46.0).
    situation: Any = field(default=None, compare=False)


@dataclass
class Decision:
    speak: bool
    push: bool
    reason: str = ""

    @property
    def delivered(self) -> bool:
        return self.speak or self.push


@dataclass
class Outcome:
    """What happened to one request."""
    decision: Decision
    spoken: bool = False
    pushed: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    request_id: str = ""


def decide(req: DeliveryRequest, cond: Conditions) -> Decision:
    """The rule order for one request. Pure: reads nothing else."""
    can_push = req.push != PUSH_NEVER
    if req.urgency == "critical":
        return Decision(not req.phone_only, can_push, "critical")
    also_push = req.push == PUSH_ALWAYS or (
        req.push == PUSH_HIGH and req.urgency == "high")
    if not req.requested_by_user:
        if not cond.gate_allowed:
            return Decision(False, req.push == PUSH_ALWAYS,
                            f"held: {cond.gate_reason or 'output gate'}")
        if not cond.announcements_on:
            return Decision(False, also_push, "announcements off")
    if req.phone_only:
        return Decision(False, can_push, "phone only")
    # Held for the night: only what matters goes to the phones instead.
    night_push = can_push and (req.urgency == "high" or req.requested_by_user
                               or req.push == PUSH_ALWAYS)
    if cond.quiet_hours and not req.quiet_ok:
        return Decision(False, night_push, "quiet hours")
    if cond.asleep and not req.sleep_ok:
        return Decision(False, night_push, "asleep")
    if cond.notify_only:
        return Decision(False, can_push, "notifications only")
    return Decision(True, also_push, "")


# ── reading the household ───────────────────────────────────────────────────

def _setting(hass, key: str, default):
    """The live panel setting, then the saved config, then `default`
    (world.setting). A loaded entry that has lost its runtime reads the
    default: an alert, a critical one above all, is never dropped for it."""
    from . import world
    return world.setting(hass, key, default)


def _on(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in (
        "1", "true", "yes", "on")


def quiet_hours(hass) -> tuple:
    """The household's quiet hours as ("HH:MM", "HH:MM") (world.py)."""
    from . import world
    return world.quiet_window(hass)


def in_quiet_hours(hass) -> bool:
    """Whether it is the household's quiet hours now (world.py). Unknown
    reads as not quiet hours. Never raises."""
    try:
        from . import world
        return world.quiet_hours(hass).value is True
    except Exception:
        return False


def conditions(hass, req: DeliveryRequest) -> Conditions:
    """Read the switches, quiet hours, sleep and the output gate. Never
    raises: an unreadable rule reads as its default."""
    from . import nova_config, settings_schema
    announcements_on = _on(_setting(
        hass, "announcements_enabled", settings_schema.default("announcements_enabled")))
    try:
        notify_only = nova_config.announce_notify_only(hass)
    except Exception:
        notify_only = False
    # Presence, sleep and quiet hours (8.46.0): the reading the source took
    # for this event, or one read here from the world.py resolvers. Read
    # once, and kept with the decision.
    from . import alert_path, household, world
    sit = req.reading
    if sit is None:
        quiet_start, quiet_end = quiet_hours(hass)
        quiet = world.Fact(bool(in_quiet_hours(hass)),
                           f"quiet hours setting {quiet_start}-{quiet_end}", time.time())
        if req.asleep is None:
            asleep = world.asleep(hass)
        else:
            asleep = world.Fact(bool(req.asleep),
                                f"{req.source or 'the source'}'s own reading", time.time())
        try:
            sit = alert_path.situation(hass, sleeping=asleep, quiet=quiet)
        except Exception:
            sit = None
    quiet_on = bool(getattr(sit, "quiet", False)) if sit is not None else quiet.value is True
    if req.asleep is not None:
        asleep_on = bool(req.asleep)
    elif sit is not None:
        asleep_on = bool(sit.sleeping)
    else:
        asleep_on = asleep.value is True
    anyone_home = None
    if sit is not None and sit.residents != household.UNKNOWN:
        anyone_home = sit.anyone_home
    gate_allowed, gate_reason = True, ""
    if req.urgency != "critical" and not req.requested_by_user:
        try:
            from . import output_gate
            gate_allowed, gate_reason = output_gate.can_announce(
                entity_id=req.entity_id, category=req.gate_category,
                urgency=req.urgency, message=req.message, dedup_key=req.dedup_key,
                habit_key=req.habit_key)
        except Exception as exc:
            _LOGGER.debug("delivery: output gate unreadable: %s", exc)
    return Conditions(announcements_on=announcements_on, notify_only=notify_only,
                      quiet_hours=quiet_on, asleep=asleep_on,
                      gate_allowed=bool(gate_allowed), gate_reason=str(gate_reason or ""),
                      anyone_home=anyone_home, situation=sit)


def _speakers(hass, req: DeliveryRequest, cond: Conditions) -> tuple:
    """(targets, mode): the source's own speakers, or the announcement
    speakers chosen by urgency and who is home. Mode "notify_only" means the
    routing sends it to the phones (nobody home, or "Notifications only");
    no targets otherwise means no speaker should take it."""
    if req.speakers:
        return list(req.speakers), "source"
    try:
        from . import audio_routing
        targets, mode = audio_routing.observer_speak_target(
            hass, urgency=req.urgency,
            broadcast_group=((req.config or {}).get("broadcast_group")
                             or _setting(hass, "broadcast_group", None) or None),
            announcement_speakers=_announcement_speakers(hass),
            is_sleeping=cond.asleep or cond.quiet_hours,
            authoritative_anyone_home=None if req.motion_counts else cond.anyone_home)
        return list(targets or []), str(mode or "")
    except Exception as exc:
        _LOGGER.warning("delivery: speaker routing failed: %s", exc)
        return [], "failed"


def _announcement_speakers(hass) -> Optional[list]:
    import json
    raw = _setting(hass, "announcement_speakers", None)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return list(value) if isinstance(value, list) and value else None


# ── delivering ──────────────────────────────────────────────────────────────

async def _speak(hass, req: DeliveryRequest, message: str, targets: list,
                 request_id: str) -> bool:
    try:
        from .tts_helper import async_announce, resolve_tts_for_context
        tts = req.tts_entity or resolve_tts_for_context(
            hass, req.tts_context, str(_setting(hass, "tts_engine", "auto")),
            _setting(hass, "tts_premium_engine", None) or None,
            _setting(hass, "tts_premium_contexts", []) or [])
        if not tts:
            return False
        return bool(await async_announce(
            hass, message, tts, targets, context=req.tts_context,
            action_request_id=request_id))
    except Exception as exc:
        _LOGGER.warning("delivery: %s speech failed: %s", req.source, exc)
        return False


def _notify_config(hass, req: DeliveryRequest) -> dict:
    """The chosen phone services: the source's config, else the settings."""
    from .const import CONF_NOTIFY_SERVICE, CONF_NOTIFY_SERVICES
    cfg = dict(req.config or {})
    for key in (CONF_NOTIFY_SERVICES, CONF_NOTIFY_SERVICE):
        if cfg.get(key) in (None, ""):
            value = _setting(hass, key, None)
            if value not in (None, ""):
                cfg[key] = value
    return cfg


async def _push(hass, req: DeliveryRequest, request_id: str) -> list:
    try:
        cfg = _notify_config(hass, req)
        if req.all_phones:
            from . import core_delivery
            await core_delivery._notify_all_devices(
                hass, cfg, req.message, req.push_kind or req.source,
                request_id=request_id)
            return ["all"]
        from . import notify_targets
        payload = {"message": req.message}
        if req.title:
            payload["title"] = req.title
        return list(await notify_targets.async_send_configured_notifications(
            hass, cfg, payload, source=req.source, request_id=request_id,
            entity_id=req.entity_id or None) or [])
    except Exception as exc:
        _LOGGER.warning("delivery: %s push failed: %s", req.source, exc)
        return []


async def async_deliver(hass, req: DeliveryRequest, *, speak=None, push=None) -> Outcome:
    """Apply the household's rules to one request and deliver it. Never
    raises. Speech that reaches no speaker goes to the phones instead,
    unless the source never pushes. A source with its own way of speaking
    (the infrastructure audit's area speech) passes `speak`, an async
    function of the words that returns whether anything was spoken. A
    source with its own phone notification (the core's snapshots and rating
    buttons) passes `push`, an async function of the request id that
    returns what took it."""
    from . import action_log
    cond = conditions(hass, req)
    decision = decide(req, cond)
    out = Outcome(decision=decision, request_id=action_log.new_request_id())
    pushed_yet = False
    async def _send() -> list:
        if push is None:
            return await _push(hass, req, out.request_id)
        try:
            return list(await push(out.request_id) or [])
        except Exception as exc:
            _LOGGER.warning("delivery: %s push failed: %s", req.source, exc)
            return []

    if decision.push and req.push_first:
        out.pushed, pushed_yet = await _send(), True
    if decision.speak:
        mode = "source"
        if speak is None:
            out.targets, mode = _speakers(hass, req, cond)
        out.spoken = await _say(hass, req, out, speak)
        if not out.spoken and req.push != PUSH_NEVER and mode == "notify_only":
            decision.push = True
            decision.reason = decision.reason or "routed to the phones"
        elif not out.spoken and _falls_back(req):
            decision.push = True
            decision.reason = decision.reason or "no speaker took it"
    if decision.push and not pushed_yet:
        out.pushed = await _send()
    if (decision.reason == "notifications only" and not out.pushed
            and (req.speakers or speak is not None)):
        # "Notifications only" with no phone that took it: spoken after all,
        # so it is never silently dropped.
        out.targets = list(req.speakers)
        out.spoken = await _say(hass, req, out, speak)
        if out.spoken:
            decision.reason = "notifications only, no phone took it"
    _LOGGER.info("delivery: %s urgency=%s spoken=%s pushed=%s %s",
                 req.source, req.urgency, out.spoken, bool(out.pushed), decision.reason)
    try:
        from . import output_gate
        output_gate.record_announcement(
            entity_id=req.entity_id, category=req.gate_category, urgency=req.urgency,
            message=req.message, was_spoken=out.spoken, dedup_key=req.dedup_key,
            habit_key=req.habit_key,
            source=req.source, pushed=bool(out.pushed),
            outcome=decision.reason or ("spoken" if out.spoken else ""),
            request_id=out.request_id)
    except Exception as exc:
        _LOGGER.debug("delivery: record failed: %s", exc)
    if req.record_kind:
        await _record(hass, req, cond, out)
    return out


async def _say(hass, req: DeliveryRequest, out: Outcome, speak) -> bool:
    """Speak the request: through the source's own `speak`, else on
    out.targets. Whether anything was spoken."""
    message = _habit_note(req)
    if speak is not None:
        try:
            return bool(await speak(message))
        except Exception as exc:
            _LOGGER.warning("delivery: %s speech failed: %s", req.source, exc)
            return False
    return bool(out.targets) and await _speak(hass, req, message, out.targets, out.request_id)


def _falls_back(req: DeliveryRequest) -> bool:
    """Whether speech no speaker took goes to the phones instead."""
    if req.push == PUSH_HIGH:
        return req.urgency in ("high", "critical")
    return req.push != PUSH_NEVER


def _habit_note(req: DeliveryRequest) -> str:
    """The words to speak, with the "I'll stop mentioning this" line on its
    third day running (habituation.py). A request the person set never
    gets it."""
    words = req.spoken or req.message
    if req.requested_by_user:
        return words
    try:
        from . import output_gate
        return output_gate.habit_note(
            entity_id=req.entity_id, category=req.gate_category,
            urgency=req.urgency, message=words, habit_key=req.habit_key)
    except Exception:
        return words


async def _record(hass, req: DeliveryRequest, cond: Conditions, out: Outcome) -> None:
    """One Decision Record entry in the shared format (alert_path)."""
    from . import alert_path
    sit = cond.situation or alert_path.situation(hass, sleeping=cond.asleep,
                                                 quiet=cond.quiet_hours)
    plan = alert_path.Plan(alert=out.decision.delivered, speak=out.spoken,
                           push=bool(out.pushed), reason=out.decision.reason)
    await alert_path.async_record_decision(
        hass, req.record_kind, source=req.source, entity_id=req.entity_id or None,
        message=req.message, sit=sit, plan=plan,
        facts={"urgency": req.urgency, **(req.record_facts or {})},
        assessment=req.record_assessment, request_ids=[out.request_id],
        delivery=out.decision.reason)
