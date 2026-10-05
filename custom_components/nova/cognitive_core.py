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
  - core_delivery.py: action delivery and notifications
  - core_ignore.py: ignore rules and the outdoor filter
  - core_lockdown.py: build_lockdown_message and LockdownManager
  - core_lockdown_sync.py: alarm sync and the lockdown entry points
  - core_pattern_store.py: StateLogger
  - core_proactive.py: ProactiveManager
  - core_safety.py: SafetyManager
  - core_state.py: _CORE, the one shared state object

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
from . import core_delivery as _m_delivery
from . import core_ignore as _m_ignore
from . import core_lockdown as _m_lockdown
from . import core_lockdown_sync as _m_lockdown_sync
from . import core_pattern_store as _m_pattern_store
from . import core_proactive as _m_proactive
from . import core_safety as _m_safety
from . import core_state as _m_state
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
from .core_delivery import (
    _autonomous_done_message,
    _emit_action,
    _execute_action_data,
    _live_runtime_config,
    _notification_image_data,
    _notify_all_devices,
    _push_notification,
    _rating_data,
)
from .core_ignore import IgnoreManager, IgnoreRule, is_outdoor_notable
from .core_lockdown import build_lockdown_message, LockdownManager
from .core_lockdown_sync import (
    _ALARM_INDET_LOG_TS,
    _ALARM_INDET_STATES,
    _alarm_state_view,
    _ensure_lockdown_mgr,
    _lockdown_exempt_locks,
    _log_alarm_indeterminate,
    _on_lockdown_state,
    _sync_lockdown_to_alarm,
    ensure_lockdown,
    is_lockdown,
    lockdown_status,
    request_lockdown,
)
from .core_pattern_store import StateLogger
from .core_proactive import ProactiveManager
from .core_safety import SafetyManager
from .core_state import _CORE, _CoreState

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
    'LockdownManager': _m_lockdown,
    'PROACTIVE_CHECK_INTERVAL': _m_common,
    'PROACTIVE_OFFER_COOLDOWN': _m_common,
    'ProactiveManager': _m_proactive,
    'STALE_LIGHT_MINUTES': _m_common,
    'SafetyManager': _m_safety,
    'StateLogger': _m_pattern_store,
    'TICK_INTERVAL': _m_common,
    '_ALARM_INDET_LOG_TS': _m_lockdown_sync,
    '_ALARM_INDET_STATES': _m_lockdown_sync,
    '_CORE': _m_state,
    '_CoreState': _m_state,
    '_alarm_state_view': _m_lockdown_sync,
    '_autonomous_done_message': _m_delivery,
    '_emit_action': _m_delivery,
    '_ensure_lockdown_mgr': _m_lockdown_sync,
    '_execute_action_data': _m_delivery,
    '_f_to_unit': _m_common,
    '_fmt_temp': _m_common,
    '_hass_lang': _m_common,
    '_live_honorific': _m_common,
    '_live_runtime_config': _m_delivery,
    '_lockdown_exempt_locks': _m_lockdown_sync,
    '_log_alarm_indeterminate': _m_lockdown_sync,
    '_notification_image_data': _m_delivery,
    '_notify_all_devices': _m_delivery,
    '_notify_i18n': _m_common,
    '_on_lockdown_state': _m_lockdown_sync,
    '_persona': _m_common,
    '_push_notification': _m_delivery,
    '_rating_data': _m_delivery,
    '_sync_lockdown_to_alarm': _m_lockdown_sync,
    '_temp_to_f': _m_common,
    'build_lockdown_message': _m_lockdown,
    'discover_outdoor_temp': _m_common,
    'ensure_lockdown': _m_lockdown_sync,
    'is_lockdown': _m_lockdown_sync,
    'is_outdoor_notable': _m_ignore,
    'lockdown_status': _m_lockdown_sync,
    'request_lockdown': _m_lockdown_sync,
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
