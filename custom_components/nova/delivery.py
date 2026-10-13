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
4. In quiet hours, or while the house is asleep, nothing is spoken and the
   message goes to the phones instead.
5. With "Notifications only" on, the message goes to the phones instead of
   the speakers.

Reminders and briefings the person set themselves (requested_by_user) skip
steps 2 and 3: shush and the Announcements switch never swallow something
they asked for. Quiet hours and sleep still apply to them, unless the person
chose the time themselves (quiet_ok: a reminder set to speak in quiet hours
too, a briefing on their schedule, or nova.briefing called now). Sleep
still applies, except to a hazard warning at or above the household's night
level (sleep_ok), which is meant to wake the house.

Quiet hours are the household's one setting (observer_quiet_start and
observer_quiet_end), read by in_quiet_hours(); no sender keeps its own.

decide() is the rule order on its own, with no Home Assistant access, so it
can be tested and read in one place. Conversation replies never come here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

_LOGGER = logging.getLogger(__name__)

URGENCIES = ("low", "medium", "high", "critical")

# When a message may also go to the phones.
PUSH_NEVER = "never"          # speakers only; held if it cannot be spoken
PUSH_FALLBACK = "fallback"    # the phones only when it cannot be spoken
PUSH_HIGH = "high"            # as fallback, and high is pushed as well as spoken
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
    if cond.quiet_hours and not req.quiet_ok:
        return Decision(False, can_push, "quiet hours")
    if cond.asleep and not req.sleep_ok:
        return Decision(False, can_push, "asleep")
    if cond.notify_only:
        return Decision(False, can_push, "notifications only")
    return Decision(True, also_push, "")


# ── reading the household ───────────────────────────────────────────────────

def _setting(hass, key: str, default):
    try:
        from . import nova_config
        from .runtime import domain_runtime_config
        rc = domain_runtime_config(hass)
        if key in rc and rc[key] not in (None, ""):
            return rc[key]
        value = nova_config.get(key, None)
        return default if value in (None, "") else value
    except Exception:
        return default


def _on(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in (
        "1", "true", "yes", "on")


def quiet_hours(hass) -> tuple:
    """The household's quiet hours as ("HH:MM", "HH:MM")."""
    from .const import DEFAULT_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_START
    return (str(_setting(hass, "observer_quiet_start", DEFAULT_OBSERVER_QUIET_START)),
            str(_setting(hass, "observer_quiet_end", DEFAULT_OBSERVER_QUIET_END)))


def in_quiet_hours(hass) -> bool:
    """Whether it is the household's quiet hours now. Never raises."""
    try:
        from . import sleep_detection
        return bool(sleep_detection._in_quiet_hours(*quiet_hours(hass)))
    except Exception:
        return False


def conditions(hass, req: DeliveryRequest) -> Conditions:
    """Read the switches, quiet hours, sleep and the output gate. Never
    raises: an unreadable rule reads as its default."""
    from . import nova_config, settings_schema, sleep_detection
    announcements_on = _on(_setting(
        hass, "announcements_enabled", settings_schema.default("announcements_enabled")))
    try:
        notify_only = nova_config.announce_notify_only(hass)
    except Exception:
        notify_only = False
    quiet_start, quiet_end = quiet_hours(hass)
    quiet = in_quiet_hours(hass)
    try:
        asleep, _why = sleep_detection.is_sleeping(
            hass, bedroom_area_ids=_setting(hass, "bedroom_areas", []) or [],
            quiet_start=quiet_start, quiet_end=quiet_end)
    except Exception:
        asleep = False
    anyone_home = None
    try:
        from . import alert_path, household
        sit = alert_path.situation(hass)
        if sit.residents != household.UNKNOWN:
            anyone_home = sit.anyone_home
    except Exception:
        anyone_home = None
    gate_allowed, gate_reason = True, ""
    if req.urgency != "critical" and not req.requested_by_user:
        try:
            from . import output_gate
            gate_allowed, gate_reason = output_gate.can_announce(
                entity_id=req.entity_id, category=req.gate_category,
                urgency=req.urgency, message=req.message)
        except Exception as exc:
            _LOGGER.debug("delivery: output gate unreadable: %s", exc)
    return Conditions(announcements_on=announcements_on, notify_only=notify_only,
                      quiet_hours=bool(quiet), asleep=bool(asleep),
                      gate_allowed=bool(gate_allowed), gate_reason=str(gate_reason or ""),
                      anyone_home=anyone_home)


def _speakers(hass, req: DeliveryRequest, cond: Conditions) -> list:
    """The source's own speakers, or the announcement speakers chosen by
    urgency and who is home. Empty when no speaker should take it."""
    if req.speakers:
        return list(req.speakers)
    try:
        from . import audio_routing
        targets, _mode = audio_routing.observer_speak_target(
            hass, urgency=req.urgency,
            broadcast_group=_setting(hass, "broadcast_group", None) or None,
            announcement_speakers=_announcement_speakers(hass),
            is_sleeping=cond.asleep or cond.quiet_hours,
            authoritative_anyone_home=cond.anyone_home)
        return list(targets or [])
    except Exception as exc:
        _LOGGER.debug("delivery: speaker routing failed: %s", exc)
        return []


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


async def async_deliver(hass, req: DeliveryRequest, *, speak=None) -> Outcome:
    """Apply the household's rules to one request and deliver it. Never
    raises. Speech that reaches no speaker goes to the phones instead,
    unless the source never pushes. A source with its own way of speaking
    (the infrastructure audit's area speech) passes `speak`, an async
    function of the words that returns whether anything was spoken."""
    from . import action_log
    cond = conditions(hass, req)
    decision = decide(req, cond)
    out = Outcome(decision=decision, request_id=action_log.new_request_id())
    pushed_yet = False
    if decision.push and req.push_first:
        out.pushed, pushed_yet = await _push(hass, req, out.request_id), True
    if decision.speak:
        message = _habit_note(req)
        if speak is not None:
            try:
                out.spoken = bool(await speak(message))
            except Exception as exc:
                _LOGGER.warning("delivery: %s speech failed: %s", req.source, exc)
        else:
            out.targets = _speakers(hass, req, cond)
            out.spoken = bool(out.targets) and await _speak(
                hass, req, message, out.targets, out.request_id)
        if not out.spoken and req.push != PUSH_NEVER:
            decision.push = True
            decision.reason = decision.reason or "no speaker took it"
    if decision.push and not pushed_yet:
        out.pushed = await _push(hass, req, out.request_id)
    _LOGGER.info("delivery: %s urgency=%s spoken=%s pushed=%s %s",
                 req.source, req.urgency, out.spoken, bool(out.pushed), decision.reason)
    try:
        from . import output_gate
        output_gate.record_announcement(
            entity_id=req.entity_id, category=req.gate_category, urgency=req.urgency,
            message=req.message, was_spoken=out.spoken)
    except Exception as exc:
        _LOGGER.debug("delivery: record failed: %s", exc)
    if req.record_kind:
        await _record(hass, req, cond, out)
    return out


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
            urgency=req.urgency, message=words)
    except Exception:
        return words


async def _record(hass, req: DeliveryRequest, cond: Conditions, out: Outcome) -> None:
    """One Decision Record entry in the shared format (alert_path)."""
    from . import alert_path
    sit = alert_path.situation(hass, sleeping=cond.asleep, quiet=cond.quiet_hours)
    plan = alert_path.Plan(alert=out.decision.delivered, speak=out.spoken,
                           push=bool(out.pushed), reason=out.decision.reason)
    await alert_path.async_record_decision(
        hass, req.record_kind, source=req.source, entity_id=req.entity_id or None,
        message=req.message, sit=sit, plan=plan,
        facts={"urgency": req.urgency, **(req.record_facts or {})},
        assessment=req.record_assessment, request_ids=[out.request_id])
