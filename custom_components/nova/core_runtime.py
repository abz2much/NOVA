"""Runtime of Nova's cognitive core.

What lives here: the state_changed listener that feeds pattern learning,
the 30 second tick and the loop that drives it, start and stop, the
proactive offer handshake, and the small public API the rest of Nova calls.
cognitive_core.py re-exports every public name from here as the same object.

The tick keeps safety work fast. Pattern analysis with its AI review, due
follow ups and due goals can wait on an AI provider for a long time, so the
tick hands them to one background job at a time; whatever that job wants to
say is delivered by the next tick, through the same gated path as
everything else.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from homeassistant.core import Event, HomeAssistant

from . import core_alarm as _m_alarm
from . import core_common as _m_common
from . import core_delivery as _m_delivery
from . import core_hazards as _m_hazards
from . import core_ignore as _m_ignore
from . import core_learning as _m_learning
from . import core_lockdown as _m_lockdown
from . import core_lockdown_sync as _m_lockdown_sync
from . import core_state as _m_state
from .core_autonomy import AutonomyManager
from .core_bridge import _patterns_db
from .core_common import TICK_INTERVAL
from .core_learning import _pattern_rate_ok
from .core_pattern_store import StateLogger
from .core_proactive import ProactiveManager
from .core_safety import SafetyManager

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

_UNREADABLE = ("unavailable", "unknown")

# Best guess attribution for pattern telemetry: the leading candidate is
# recorded when it leads the runner up by this share, at a capped confidence.
_LEAD_SHARE = 0.2
_GUESS_CONFIDENCE_CAP = 0.44


def _core():
    return _m_state._CORE


def _nova_log(kind: str, text: str) -> None:
    from .websocket import nova_log
    nova_log(kind, text)


# ── state_changed listener ──────────────────────────────────────────────────

def _offer_area(hass, offer):
    """The area of the entity an offer would act on, or None. A mode bound
    to some rooms uses it to stay quiet about just those rooms."""
    try:
        data = (offer.get("action_data") or {}) if isinstance(offer, dict) else {}
        target = data.get("entity_id") or offer.get("entity_id")
        if isinstance(target, (list, tuple)):
            target = target[0] if target else None
        if not target:
            return None
        from . import audio_routing
        return audio_routing.entity_area(hass, target)
    except Exception:
        return None


def _learned_values(entity_id: str, new_state, old_val: str, new_val: str):
    """The (old, new) pair pattern learning should record, or None to skip.

    An event entity's state is a timestamp; what it means is its event_type,
    and a pulse without one is dropped. A scene's state is also a timestamp,
    so every activation is recorded as "activated"."""
    domain = entity_id.partition(".")[0]
    if domain == "scene":
        return "", "activated"
    if domain != "event":
        return old_val, new_val
    try:
        event_type = new_state.attributes.get("event_type") or ""
    except Exception:
        event_type = ""
    return ("", event_type) if event_type else None


def _area_of(hass, entity_id: str) -> str:
    """The entity's own area, else its device's, else ""."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        reg_entry = er.async_get(hass).async_get(entity_id)
        if reg_entry is None:
            return ""
        if reg_entry.area_id:
            return reg_entry.area_id
        if reg_entry.device_id:
            device = dr.async_get(hass).async_get(reg_entry.device_id)
            return (device.area_id if device else "") or ""
    except Exception:
        pass
    return ""


def _who_did_it(hass, area_id: str) -> tuple[str, float]:
    """A cheap guess at who caused a change in this area, for telemetry only.

    Uses the quick presence check, never the full face and voice resolver,
    because this runs on every state change. When nobody clears the bar,
    a candidate that clearly leads is recorded at a low confidence so that
    person's routines can build up; a near tie stays unknown. Actions still
    need the strict check in identity.resolve()."""
    try:
        from . import identity
        guess = identity.quick_identify(hass, area_id)
        if guess.person != identity.UNKNOWN or not guess.candidates:
            return guess.person, guess.confidence
        ranked = sorted(guess.candidates.items(), key=lambda kv: kv[1], reverse=True)
        best = float(ranked[0][1])
        if best <= 0:
            return guess.person, guess.confidence
        runner_up = float(ranked[1][1]) if len(ranked) > 1 else 0.0
        lead = (best - runner_up) / best
        if len(ranked) > 1 and lead < _LEAD_SHARE:
            return guess.person, guess.confidence
        confidence = round(min(_GUESS_CONFIDENCE_CAP, best * (0.4 + 0.6 * lead)), 3)
        return ranked[0][0], confidence
    except Exception:
        return "unknown", 0.0


def _caused_by(new_state) -> tuple[str, str, float]:
    """(kind, entity_id, confidence) of what changed the state, if known."""
    contexts = _core().automation_contexts
    if contexts is None:
        return "unknown", "", 0.0
    try:
        source = contexts.resolve_state(new_state)
        return source.kind, source.entity_id, source.confidence
    except Exception:
        return "unknown", "", 0.0


def _on_state_changed(event: Event) -> None:
    """Record real state changes for pattern learning, after the hazard
    check, ignore rules, exclusions and the per entity rate limit."""
    core = _core()
    if not core.running:
        return
    data = event.data
    new_state = data.get("new_state")
    if not new_state:
        return
    entity_id = data.get("entity_id", "")
    old_state = data.get("old_state")
    # camera_event.* rows are written by camera_semantic with their own
    # confidence, dedup and resident rules; logging them here would add a
    # second row with a presence guess those events must not carry.
    if entity_id.startswith("camera_event."):
        return
    # Smoke, CO, gas and leak alarms are announced before any ignore rule.
    if _m_hazards.on_state_changed(core.hass, core.config, event):
        return
    # So is the chosen alarm going off (8.83.0).
    if _m_alarm.on_state_changed(core.hass, core.config, event):
        return
    if core.ignore_mgr and core.ignore_mgr.is_ignored(entity_id):
        return
    try:
        from .entity_filter import is_excluded
        if is_excluded(core.hass, entity_id):
            return
    except Exception:
        pass

    new_val = new_state.state
    old_val = old_state.state if old_state else "unknown"
    if new_val in _UNREADABLE or new_val == old_val:
        return
    try:
        device_class = new_state.attributes.get("device_class") or ""
    except Exception:
        device_class = ""

    # An opening or lock reappearing after being unavailable is not a person
    # doing something.
    from .cognitive.evaluators import is_entry_state_recovery
    if is_entry_state_recovery(entity_id, device_class, old_val, new_val):
        return
    values = _learned_values(entity_id, new_state, old_val, new_val)
    if values is None:
        return
    old_val, new_val = values

    # Chatty sources (media players, motion pulses) get one row per interval
    # each; this also skips the lookups below for dropped churn.
    if not _pattern_rate_ok(entity_id, time.time(), device_class):
        return

    area_id = _area_of(core.hass, entity_id)
    logger = core.state_logger
    if not logger:
        return
    person, person_conf = _who_did_it(core.hass, area_id)
    try:
        forced = _m_learning._pattern_opted_in(entity_id, device_class)
    except Exception:
        forced = False
    kind, source_entity, source_conf = _caused_by(new_state)
    logger.log_state_change(
        entity_id, old_val, new_val, area_id,
        triggered_by=kind,
        source_entity_id=source_entity,
        source_confidence=source_conf,
        person=person,
        person_confidence=person_conf,
        force_include=forced,
    )


# ── slow AI work, one job at a time ─────────────────────────────────────────

def _analysis_thresholds(config) -> tuple[int, float]:
    """Pattern thresholds from settings: at least 4 occurrences and 0.55
    confidence unless the panel saved something else that reads as a number."""
    try:
        occurrences = int(config.get("pattern_min_occurrences", 4) or 4)
    except Exception:
        occurrences = 4
    try:
        confidence = float(config.get("pattern_confidence", 0.55) or 0.55)
    except Exception:
        confidence = 0.55
    return occurrences, confidence


async def _analyse_patterns(hass, config) -> None:
    # Run pattern analysis periodically
    from .automation.patterns import get_analyzer, set_thresholds
    analyzer = get_analyzer()
    # A manual analysis already running covers this round, and should_analyze
    # reads patterns.db, so it is asked off the event loop.
    due = not analyzer.analysis_running and await hass.async_add_executor_job(
        analyzer.should_analyze)
    if not due:
        return
    set_thresholds(*_analysis_thresholds(config))
    from . import suggestion_review
    reviewer = await suggestion_review.reviewer_for(hass)
    if reviewer is None:
        patterns = await analyzer.analyze(hass)
    else:
        patterns = await analyzer.analyze(hass, reviewer=reviewer)
    from . import nova_state
    nova_state.notify()   # suggestions may have been added or retired
    if not patterns:
        return
    _nova_log("LEARN", f"Pattern analysis: {len(patterns)} patterns found")
    waiting = await hass.async_add_executor_job(analyzer.get_pending_suggestions)
    if waiting:
        _nova_log("LEARN", f"{len(waiting)} automation suggestion(s) pending review")


async def _ai_work(hass, config) -> list:
    """Pattern analysis, then due follow ups, then due goals. Each part
    fails on its own; the actions to deliver are returned."""
    try:
        await _analyse_patterns(hass, config)
    except Exception as exc:
        _LOGGER.debug("Pattern analysis tick error: %s", exc)
    found: list = []
    runner = _make_followup_runner(hass, config)
    # Follow ups the agent queued for itself ("check the garage closed in
    # five minutes"), run back through the same agent and reported.
    try:
        from . import followups
        found += await followups.async_process_due(hass, config, runner=runner)
    except Exception as exc:
        _LOGGER.debug("Follow-up tick error: %s", exc)
    # Goals: outcomes Nova pursues over time, quiet until there is news.
    try:
        from . import goals
        found += await goals.async_process_due(hass, config, runner=runner)
    except Exception as exc:
        _LOGGER.debug("Goal tick error: %s", exc)
    return found


def _take_ai_actions() -> list:
    """What the last background job left to deliver, once."""
    core = _core()
    queued = getattr(core, "ai_actions", None) or []
    core.ai_actions = []
    return queued


def _start_ai_work(hass, config) -> None:
    """Start the background job unless one is still running."""
    core = _core()
    if getattr(core, "ai_busy", False):
        return

    async def _job():
        try:
            found = await _ai_work(hass, config)
            if found:
                core.ai_actions = (getattr(core, "ai_actions", None) or []) + found
        finally:
            core.ai_busy = False

    core.ai_busy = True
    try:
        if hasattr(hass, "async_create_background_task"):
            core.ai_task = hass.async_create_background_task(_job(), "nova_cognitive_ai_work")
        else:
            core.ai_task = hass.async_create_task(_job())
    except Exception as exc:
        core.ai_busy = False
        _LOGGER.debug("Could not start the cognitive AI work: %s", exc)


async def _stop_ai_work() -> None:
    core = _core()
    job = getattr(core, "ai_task", None)
    core.ai_task = None
    core.ai_busy = False
    core.ai_actions = []
    if isinstance(job, asyncio.Future) and not job.done():
        job.cancel()
        try:
            await job
        except (asyncio.CancelledError, Exception):
            pass


# ── the tick ────────────────────────────────────────────────────────────────

def _proactive_scope(config) -> tuple[bool, list]:
    """(proactive on, rooms a mode is bound to).

    A mode bound to rooms keeps the house proactive and only drops offers
    about those rooms; an unbound mode that forbids offers switches them off
    everywhere. Safety is never gated by this."""
    enabled = config.get("observer_proactive", True)
    bound: list = []
    try:
        from . import modes
        bound = modes.mode_scoped_to_areas()
        if not bound and not modes.mode_allows_proactive():
            enabled = False
    except Exception:
        pass
    return enabled, bound


async def _gather_offers(hass, sleeping, anyone_home, house, bound_rooms) -> list:
    """This tick's proactive offers. Only the first eligible one is asked:
    it becomes the offer waiting for a yes (unless it is information only)
    and is marked offered; the rest come back on a later tick. Nothing is
    ever carried out without a yes."""
    from . import household
    core = _core()
    offers = await core.proactive_mgr.tick(
        sleeping, anyone_home, nobody_home=(house.residents == household.AWAY))
    for offer in offers:
        if bound_rooms:
            room = _offer_area(core.hass, offer)
            if room and room in bound_rooms:
                continue
        if offer.get("offer") is not False:
            core.offers_made += 1
            offer = _bindable_offer(offer)
            core.pending_offer = offer
        key = offer.get("offer_key")
        if key:
            core.proactive_mgr._mark_offered(key)
        return [offer]
    return []


async def _gather_anticipation(hass, config) -> list:
    """Unusual states for the time of day and due departure reminders.

    Occupancy is sampled every OCC_SAMPLE_INTERVAL and the full predictor
    set runs then; the presence and departure predictors run every tick
    because their reminders are due to the minute."""
    from . import cognition
    try:
        from . import observer
        enabled = observer._cognition_enabled()
    except Exception:
        enabled = bool(config.get("cognition_enabled", True))
    if not enabled:
        return []
    core = _core()
    now = time.time()
    full_cycle = now - getattr(core, "_last_cog_cycle", 0.0) >= cognition.OCC_SAMPLE_INTERVAL
    if full_cycle:
        core._last_cog_cycle = now
        cognition.sample_occupancy(hass, now)
        cognition.sample_presence(hass, now)
    try:
        # Warms the cache the predictors read.
        from . import adaptive_awareness
        await adaptive_awareness.async_refresh(hass)
    except Exception:
        pass
    found = cognition.predict_presence(hass, now)
    if full_cycle:
        for predictor in (cognition.predict, cognition.predict_overdue,
                          cognition.predict_proximity, cognition.predict_routine_start):
            found += predictor(hass, now)
    found += await cognition.predict_departure(hass, now)
    for prediction in found:
        _nova_log("LEARN", f"anticipation: {prediction.get('message', '')[:80]}")
    if full_cycle or found:
        # Save straight away, so a restart cannot repeat a reminder.
        await hass.async_add_executor_job(cognition.save_to_db, _patterns_db())
    return found


async def _tick():
    """One pass over the house: gather what needs saying, then deliver it."""
    core = _core()
    hass = core.hass
    config = core.config
    core.tick_count += 1
    core.last_tick = time.time()

    # One read only picture of the house for this tick (world.py): who is
    # home, asleep and quiet hours from the live settings, what is open or
    # unlocked, lockdown and open situations. Dropped when the tick ends.
    from . import world
    asleep_fact = world.asleep(hass)
    quiet_fact = world.quiet_hours(hass)
    sleeping = asleep_fact.value is True
    snap = world.begin_tick(hass, config, asleep=asleep_fact, quiet_hours=quiet_fact,
                            tick=core.tick_count)
    try:
        from . import household
        house = snap.house if snap.house is not None else household.snapshot(hass, config)
        anyone_home = house.anyone_home
        actions: list = []

        # Lockdown first, so the night sweep can defer to it.
        if core.lockdown_mgr:
            try:
                actions += await core.lockdown_mgr.tick()
            except Exception as exc:
                _LOGGER.debug("Lockdown tick error: %s", exc)

        # Away and normal follow occupancy unless the user is in charge. An
        # unknown reading is not an empty house, so it changes nothing.
        try:
            from . import modes
            if house.residents != household.UNKNOWN:
                modes.auto_evaluate(anyone_home)
        except Exception as exc:
            _LOGGER.debug("auto-mode eval error: %s", exc)

        # Safety: an error here keeps what is already gathered (a lockdown
        # announcement would not come again) and the rest of the tick.
        try:
            actions += await core.safety_mgr.tick(sleeping, anyone_home, house=house)
        except Exception as exc:
            _LOGGER.warning("Cognitive safety tick error: %s", exc)

        proactive_enabled, bound_rooms = _proactive_scope(core.config)
        if proactive_enabled and core.proactive_mgr:
            try:
                actions += await _gather_offers(hass, sleeping, anyone_home, house, bound_rooms)
            except Exception as exc:
                _LOGGER.debug("Proactive tick error: %s", exc)

        # Energy advice under the same gate; a critical load is never shed
        # (energy.evaluate_for_proactive sees to that).
        if proactive_enabled:
            try:
                from . import energy
                shed = energy.evaluate_for_proactive(hass)
                if shed:
                    actions.append(shed)
                # Outlook advice only, never a device action.
                outlook = await energy.evaluate_outlook_for_proactive(hass)
                if outlook:
                    actions.append(outlook)
            except Exception as exc:
                _LOGGER.debug("Energy tick error: %s", exc)

        if proactive_enabled:
            try:
                actions += await _gather_anticipation(hass, config)
            except Exception as exc:
                _LOGGER.debug("Cognition anticipation tick error: %s", exc)

        # Reports from the background job (follow ups and goals), then start
        # the next one; it never holds up this tick.
        actions += _take_ai_actions()
        _start_ai_work(hass, config)

        # Deliver. Anticipation alerts take the convenience path, which works
        # out its verdict from this tick's reading first. Each action is sent
        # on its own, so one failure loses only that one.
        from . import alert_path, convenience
        reading = alert_path.situation(hass, config, sleeping=asleep_fact,
                                       quiet=quiet_fact, house=house)
        for action in actions:
            try:
                if convenience.eligible(action):
                    await convenience.async_handle(hass, config, action, sleeping, reading)
                    continue
                await _m_delivery._emit_action(hass, config, action, sleeping)
                # An offer nobody heard cannot be answered by voice.
                if action is core.pending_offer and action.get("spoken_at") is None:
                    offer_not_spoken(action)
            except Exception as exc:
                kind = action.get("type", "?") if isinstance(action, dict) else "?"
                _LOGGER.warning("Cognitive action delivery error (%s): %s", kind, exc)
    finally:
        world.end_tick()


def _make_followup_runner(hass, config):
    """A headless agent run for follow ups and goals: Nova's own brain and
    tools with nobody present, reporting back on work it queued itself."""
    async def _run(instruction: str, context: str) -> str:
        from .agent import run_agent
        from .llm_provider import resolve_provider_credential, resolve_provider_endpoint
        try:
            from .const import CONF_MODEL, DEFAULT_MODEL
            model = config.get(CONF_MODEL, DEFAULT_MODEL)
        except Exception:
            model = config.get("model", "")
        honorific = _m_common._live_honorific(hass)
        audience = f"to {honorific} " if honorific else ""
        # Nobody is present, so the run gets the look and report grant and
        # cannot act. The note was written by the model when it scheduled
        # this, possibly from untrusted text, so it is fenced as quoted data
        # rather than put in the instructions.
        persona = (
            "You are Nova. You scheduled this follow-up yourself earlier and it is now due. "
            "No one is present for this scheduled run: you can check states, look and "
            "diagnose with your tools, but you cannot control devices or change anything. "
            f"Check what the follow-up asks, then report the outcome {audience}"
            "in one or two spoken-style sentences; if something needs doing, say what, "
            "so the household can decide. If everything is fine, say so briefly."
        )
        if context:
            from .prompt_fence import fence
            persona += "\n\n" + fence(
                context,
                label="FOLLOWUP_CONTEXT",
                noun="is a note you saved with this follow-up when you scheduled it",
                callback_noun="a saved note",
            )
        provider = config.get("llm_provider", "groq")
        return await run_agent(
            hass,
            messages=[{"role": "user", "content": instruction}],
            persona=persona,
            provider_name=provider,
            api_key=resolve_provider_credential(config, provider),
            model=model,
            base_url=resolve_provider_endpoint(config, provider),
            temperature=0.4,
            config=config,
        )
    return _run


async def _loop():
    """Tick every TICK_INTERVAL seconds while the core runs."""
    _LOGGER.info("Cognitive Core loop started")
    # Let Home Assistant finish bringing entities up before the first tick.
    try:
        await asyncio.sleep(min(TICK_INTERVAL, 30))
    except asyncio.CancelledError:
        return
    while _core().running:
        try:
            await _tick()
        except Exception as exc:
            _LOGGER.warning("Cognitive tick error: %s", exc)
        await asyncio.sleep(TICK_INTERVAL)


# ── status and settings ─────────────────────────────────────────────────────

def intrusion_status() -> dict:
    """The intrusion investigation in progress, for the Residence view:
    where it started and which rooms activity has reached since."""
    manager = _core().safety_mgr
    case = getattr(manager, "_investigation", None) if manager else None
    if not case:
        return {"active": False, "confirmed": False}
    return {
        "active": True,
        "confirmed": bool(case.get("escalated")),
        "breach_area": case.get("breach_area"),
        "breach_name": case.get("breach_name"),
        "path": list(case.get("path", [])),
        "zones": sorted(str(zone) for zone in case.get("zones", set())),
    }


_LIVE_SAFETY_KEYS = ("lockdown_auto_on_arm", "security_alarm_entity",
                     "intrusion_requires_confinement", "face_stand_down",
                     "lockdown_exempt_locks")


def _safety_parts():
    core = _core()
    return [part for part in (core.safety_mgr, core.lockdown_mgr) if part is not None]


async def _clear_automatic_lockdown(manager) -> None:
    """Forget an automatic lockdown's state without touching any device."""
    manager.active = False
    manager.since = 0.0
    manager.reason = ""
    manager.auto = False
    manager.exempt_windows = set()
    manager._secured_by_us = set()
    manager._alerted = set()
    manager._auto_suppressed = False
    await manager._persist()
    _LOGGER.warning(
        "Automatic lockdown disabled; cleared Nova lockdown state without device actions")


async def apply_runtime_config(key: str, value) -> None:
    """Apply a safety setting at once, without reloading Nova."""
    if key not in _LIVE_SAFETY_KEYS:
        return
    core = _core()
    if not isinstance(core.config, dict):
        core.config = {}
    if key == "lockdown_auto_on_arm":
        enabled = value is True
        core.config[key] = enabled
        for part in _safety_parts():
            part.set_automatic_lockdown(enabled)
        lockdown = core.lockdown_mgr
        if not enabled and lockdown and lockdown.active and lockdown.auto:
            await _clear_automatic_lockdown(lockdown)
        return
    if key == "lockdown_exempt_locks":
        # Lockdown, the night sweep and the unlocked lists all read the
        # manager's set, so this applies everywhere at once.
        from .core_lockdown import exempt_lock_set
        locks = sorted(exempt_lock_set(value))
        core.config[key] = locks
        lockdown = core.lockdown_mgr
        if lockdown is not None:
            lockdown.exempt_locks = set(locks)
            if isinstance(lockdown.config, dict):
                lockdown.config[key] = locks
        return
    if key in ("intrusion_requires_confinement", "face_stand_down"):
        value = value is True
    core.config[key] = value
    for part in _safety_parts():
        if isinstance(part.config, dict):
            part.config[key] = value


def status() -> dict:
    """The cognitive core at a glance, for diagnostics."""
    core = _core()
    now = time.time()
    learning = core.state_logger.get_pattern_stats() if core.state_logger else {}
    try:
        from .automation.patterns import get_analyzer
        last_analysis = dict(get_analyzer()._last_result)
    except Exception:
        last_analysis = {}
    grants = core.autonomy_mgr.list_grants() if core.autonomy_mgr else []
    return {
        "running": core.running,
        "tick_count": core.tick_count,
        "actions_taken": core.actions_taken,
        "offers_made": core.offers_made,
        "autonomous_actions": core.autonomous_actions,
        "autonomy_grants": grants,
        "uptime_hours": round((now - core.startup_time) / 3600, 1) if core.startup_time else 0,
        "last_tick_ago": round(now - core.last_tick, 1) if core.last_tick else 0,
        "ignore_rules": len(list_ignores()),
        "learning": learning,
        "last_analysis": last_analysis,
    }


# ── ignore rules ────────────────────────────────────────────────────────────

_NOT_RUNNING = "Cognitive core not running"


def ignore(entity_pattern: str, duration_minutes: int = 0, reason: str = "") -> dict:
    """Add an ignore rule (the agent's ignore tool). A pattern that matches
    everything is refused; no duration means the default one."""
    manager = _core().ignore_mgr
    if not manager:
        return {"success": False, "enforced": False, "error": _NOT_RUNNING}
    try:
        rule = manager.add(entity_pattern, duration_minutes, reason)
    except ValueError as exc:
        return {"success": False, "enforced": False, "error": str(exc)}
    minutes = duration_minutes if duration_minutes > 0 else _m_ignore.DEFAULT_IGNORE_MINUTES
    return {"success": True, "enforced": True, "pattern": rule.entity_pattern,
            "duration": minutes, "reason": reason}


def unignore(entity_pattern: str) -> dict:
    """Remove an ignore rule, and bring back matching notifications that
    went quiet after coming up three days running."""
    try:
        from . import habituation
        restored = habituation.forget(entity_pattern)
    except Exception as exc:
        _LOGGER.debug("habituation forget failed: %s", exc)
        restored = []
    manager = _core().ignore_mgr
    if manager:
        removed = manager.remove(entity_pattern)
        return {"success": bool(removed or restored), "pattern": entity_pattern,
                "restored_notifications": len(restored)}
    if not restored:
        return {"success": False, "error": _NOT_RUNNING}
    return {"success": True, "pattern": entity_pattern,
            "restored_notifications": len(restored)}


def list_ignores() -> list[dict]:
    """Active ignore rules, then the notifications habituation has quietened."""
    manager = _core().ignore_mgr
    rules = manager.list_rules() if manager else []
    try:
        from . import habituation
        rules += [{"pattern": quiet["entity_id"] or quiet["key"],
                   "reason": "came up three days running",
                   "expires_at": 0,
                   "remaining_min": "normal for this home"}
                  for quiet in habituation.quiet_list()]
    except Exception:
        pass
    return rules


def is_ignored(entity_id: str) -> bool:
    manager = _core().ignore_mgr
    return manager.is_ignored(entity_id) if manager else False


# ── proactive offers ────────────────────────────────────────────────────────
#
# An offer is answered only by voice, on a satellite in a room where it was
# spoken, within OFFER_ANSWER_SECONDS. Pushed only or undelivered offers
# cannot be accepted by voice. Every offer has its own offer_id, so a yes
# runs the offer that was heard and never a newer one.

OFFER_ANSWER_SECONDS = 60.0


def _bindable_offer(offer: dict) -> dict:
    """A copy of the offer with a fresh id and no delivery recorded."""
    return {**offer, "offer_id": uuid.uuid4().hex, "heard_by": [], "spoken_at": None}


def offer_spoken(offer: dict, speakers, *, now: Optional[float] = None) -> None:
    """Delivery reports the pending offer was spoken on these speakers.
    Calls about any other offer are ignored."""
    if offer is None or offer is not _core().pending_offer:
        return
    offer["heard_by"] = [str(speaker) for speaker in speakers or [] if speaker]
    offer["spoken_at"] = time.monotonic() if now is None else now


def offer_not_spoken(offer: dict) -> None:
    """Delivery reports the pending offer was only pushed, or not sent at
    all: nobody can answer it by voice, so it is dropped."""
    core = _core()
    if offer is not None and offer is core.pending_offer:
        core.pending_offer = None


def _with_group_members(hass, speakers) -> set:
    """The speakers, plus each member of any speaker group among them."""
    heard = set(speakers)
    for speaker in list(heard):
        attrs = getattr(hass.states.get(speaker), "attributes", None) or {}
        for key in ("group_members", "entity_id"):
            members = attrs.get(key)
            if isinstance(members, (list, tuple)):
                heard.update(str(m) for m in members if m)
    return heard


def _offer_heard_on(hass, device_id: str, heard_by) -> bool:
    """Whether this voice device is in a room where the offer was spoken:
    one of its satellites is explicitly paired with one of the speakers, or
    shares an area with one. Any doubt counts as not heard."""
    if not device_id or not heard_by:
        return False
    try:
        heard = _with_group_members(hass, heard_by)
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from . import audio_routing, voice_confirm
        entities = er.async_get(hass)
        satellites = [
            state.entity_id for state in hass.states.async_all("assist_satellite")
            if getattr(entities.async_get(state.entity_id), "device_id", None) == device_id
        ]
        rooms = set()
        device = dr.async_get(hass).async_get(device_id)
        if device is not None and device.area_id:
            rooms.add(device.area_id)
        # Only an explicit pairing counts, never a shared fallback speaker.
        pairings = voice_confirm._satellite_pairings(hass) or {}
        for satellite in satellites:
            if pairings.get(satellite) in heard:
                return True
            room = audio_routing.entity_area(hass, satellite)
            if room:
                rooms.add(room)
        return bool(rooms) and any(
            audio_routing.entity_area(hass, speaker) in rooms for speaker in heard)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Offer room check failed (treated as not heard): %s", exc)
        return False


def get_pending_offer() -> Optional[dict]:
    """The offer waiting for a yes or no, if any."""
    return _core().pending_offer


def pending_offer_for(hass, device_id: Optional[str], *,
                      now: Optional[float] = None) -> Optional[dict]:
    """The pending offer if this voice device may answer it now: spoken in
    its room less than OFFER_ANSWER_SECONDS ago. An expired offer is dropped.
    Chat, typed turns and other rooms get None."""
    core = _core()
    offer = core.pending_offer
    if not offer or offer.get("spoken_at") is None:
        return None
    clock = time.monotonic() if now is None else now
    if clock - float(offer["spoken_at"]) > OFFER_ANSWER_SECONDS:
        core.pending_offer = None
        return None
    if device_id and _offer_heard_on(hass, device_id, offer.get("heard_by")):
        return offer
    return None


async def accept_pending_offer(offer_id: Optional[str] = None) -> dict:
    """A yes: run the pending offer once. Given an offer_id, only that offer
    runs; if it has been replaced or cleared, nothing does."""
    core = _core()
    offer = core.pending_offer
    if not offer:
        return {"ok": False, "reason": "no pending offer"}
    if offer_id is not None and offer.get("offer_id") != offer_id:
        return {"ok": False, "reason": "offer replaced"}
    core.pending_offer = None
    ok = await _m_delivery._execute_action_data(
        core.hass, offer.get("action_data", {}), source="proactive_accepted")
    pattern = offer.get("pattern_key", "")
    if not (ok and pattern and core.autonomy_mgr):
        return {"ok": ok}
    grant = core.autonomy_mgr.record_acceptance(pattern, confidence=0.9)
    core.actions_taken += 1
    return {"ok": True, "pattern_key": pattern, "approvals": grant.get("approvals", 0)}


def decline_pending_offer(offer_id: Optional[str] = None) -> dict:
    """A no: clear the offer (only that one, given an offer_id) and reset
    its acceptance count."""
    core = _core()
    offer = core.pending_offer
    if offer and offer_id is not None and offer.get("offer_id") != offer_id:
        return {"ok": False, "reason": "offer replaced"}
    core.pending_offer = None
    pattern = offer.get("pattern_key", "") if offer else ""
    if pattern and core.autonomy_mgr:
        core.autonomy_mgr.record_rejection(pattern)
    return {"ok": True}


def revoke_autonomy(pattern_key: str) -> dict:
    """Reset an offer's acceptance count."""
    manager = _core().autonomy_mgr
    if manager and manager.revoke(pattern_key):
        return {"ok": True, "pattern_key": pattern_key}
    return {"ok": False, "reason": "no such grant"}


# ── start and stop ──────────────────────────────────────────────────────────

async def start(hass: HomeAssistant, config: dict, entry=None) -> None:
    """Start the cognitive core for `entry`, whose runtime supplies the
    automation context tracker and the live panel settings."""
    # Resolved before anything changes: a loaded entry that has lost its
    # runtime raises here and leaves the core as it was.
    contexts = None
    if entry is not None:
        from .runtime import current_runtime
        runtime = current_runtime(entry)
        if runtime is not None:
            contexts = runtime.automation_contexts

    core = _core()
    if core.running:
        await stop()

    core.hass = hass
    core.config = config
    core.running = True
    core.startup_time = time.time()
    core.tick_count = core.actions_taken = core.offers_made = core.autonomous_actions = 0
    core.pending_offer = None
    core.automation_contexts = contexts
    core.entry = entry

    # Files are read in the executor: ignore rules, lockdown state,
    # autonomy grants and the pattern store.
    core.ignore_mgr = await hass.async_add_executor_job(_m_ignore.IgnoreManager)
    core.safety_mgr = SafetyManager(hass, config)
    # An investigation or freeze warning open before a restart carries on.
    core.safety_mgr.restore_situations()
    core.lockdown_mgr = await hass.async_add_executor_job(
        _m_lockdown.LockdownManager, hass, config)
    core.proactive_mgr = ProactiveManager(hass, config)
    core.autonomy_mgr = await hass.async_add_executor_job(AutonomyManager)
    core.state_logger = await hass.async_add_executor_job(StateLogger)

    # Alarm to lockdown sync is event driven, separate from the loop, and
    # applies the alarm's current state now.
    try:
        await _m_lockdown_sync.ensure_lockdown(hass, config)
    except Exception as exc:
        _LOGGER.warning("Lockdown wiring in start() failed: %s", exc)

    # The anticipation model carries on from where it was.
    try:
        from . import cognition
        await hass.async_add_executor_job(cognition.load_from_db, _patterns_db())
    except Exception as exc:
        _LOGGER.debug("cognition load on start failed: %s", exc)

    core.unsub = hass.bus.async_listen("state_changed", _on_state_changed)
    # The loop never returns, so it must be a background task: Home
    # Assistant's startup waits on ordinary setup tasks and would time out.
    if hasattr(hass, "async_create_background_task"):
        core.task = hass.async_create_background_task(_loop(), "nova_cognitive_loop")
    else:
        core.task = hass.async_create_task(_loop())

    learned = await hass.async_add_executor_job(core.state_logger.get_pattern_stats)
    _LOGGER.info(
        "Nova Cognitive Core started — %d days of data, "
        "%d state changes logged, %d patterns learned, "
        "%d active ignore rules, %d autonomy grants",
        learned.get("days_of_data", 0),
        learned.get("state_changes", 0),
        learned.get("patterns", 0),
        len(core.ignore_mgr.list_rules()),
        len(core.autonomy_mgr.list_grants()),
    )


def _unsubscribe(core, attr: str) -> None:
    """Call and clear a stored listener remover, at most once."""
    remove = getattr(core, attr)
    if not remove:
        return
    try:
        remove()
    except Exception:
        pass
    setattr(core, attr, None)


async def stop() -> None:
    """Stop the cognitive core. Safe to call twice."""
    core = _core()
    core.running = False
    core.pending_offer = None          # a stale offer never outlives a restart
    core.automation_contexts = None
    core.entry = None
    if core.task:
        core.task.cancel()
        try:
            await core.task
        except (asyncio.CancelledError, Exception):
            pass
        core.task = None
    await _stop_ai_work()
    _unsubscribe(core, "unsub")
    _unsubscribe(core, "alarm_unsub")
    _LOGGER.info("Nova Cognitive Core stopped — %d ticks, %d actions taken",
                 core.tick_count, core.actions_taken)


def release_runtime() -> None:
    """Drop the hass reference, config and lockdown manager an unloaded entry
    owned, so the next setup builds a fresh manager.

    async_unload_entry calls this after stop(); stop() alone (observer stop,
    a core restart) keeps the manager. No device is touched and nothing is
    written: the saved lockdown state stays on disk for the next manager.
    Idempotent."""
    core = _core()
    core.lockdown_mgr = None
    core.hass = None
    core.config = {}
