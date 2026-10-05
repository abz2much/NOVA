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
  - core_learning.py: pattern logging, backfill and analysis
  - core_lockdown.py: build_lockdown_message and LockdownManager
  - core_lockdown_sync.py: alarm sync and the lockdown entry points
  - core_pattern_store.py: StateLogger
  - core_proactive.py: ProactiveManager
  - core_runtime.py: listener, tick, loop, start/stop, public API
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
from . import core_learning as _m_learning
from . import core_lockdown as _m_lockdown
from . import core_lockdown_sync as _m_lockdown_sync
from . import core_pattern_store as _m_pattern_store
from . import core_proactive as _m_proactive
from . import core_runtime as _m_runtime
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
from .core_learning import (
    _HIGH_FREQ_CLASSES,
    _PATTERN_LOG_LAST,
    _backfill_filter_states,
    _pattern_log_interval,
    _pattern_opted_in,
    _pattern_rate_ok,
    backfill_from_history,
    learning_active,
    log_camera_event,
    log_command,
    run_analysis_now,
)
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
from .core_runtime import (
    _loop,
    _make_followup_runner,
    _offer_area,
    _on_state_changed,
    _tick,
    accept_pending_offer,
    apply_runtime_config,
    decline_pending_offer,
    get_pending_offer,
    ignore,
    intrusion_status,
    is_ignored,
    list_ignores,
    release_runtime,
    revoke_autonomy,
    start,
    status,
    stop,
    unignore,
)
from .core_safety import SafetyManager
from .core_state import _CORE, _CoreState

_LOGGER = logging.getLogger(__name__)


# ── Storage identity ─────────────────────────────────────────────────────
# The files the cognitive core persists. tests/fixtures/contracts/storage.json
# reads these literals from this file, and tests point the overrides at a
# temporary directory. The core modules call the helpers through core_bridge.

LOCKDOWN_STATE_PATH: Optional[str] = None  # override; None resolves via paths.py; survives reboots/reloads


def _lockdown_state_path() -> str:
    return LOCKDOWN_STATE_PATH or paths.nova_path("lockdown_state.json")


IGNORE_FILE: Optional[str] = None  # override; None resolves via paths.py


def _ignore_file() -> str:
    return IGNORE_FILE or paths.config_path(".nova_ignore_rules.json")


AUTONOMY_FILE: Optional[str] = None  # override; None resolves via paths.py


def _autonomy_file() -> str:
    return AUTONOMY_FILE or paths.nova_path("autonomy_grants.json")


PATTERNS_DB: Optional[str] = None  # override; None resolves via paths.py; learned patterns and the cognition model


def _patterns_db() -> str:
    return PATTERNS_DB or paths.patterns_db()


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
    '_HIGH_FREQ_CLASSES': _m_learning,
    '_PATTERN_LOG_LAST': _m_learning,
    '_alarm_state_view': _m_lockdown_sync,
    '_autonomous_done_message': _m_delivery,
    '_backfill_filter_states': _m_learning,
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
    '_loop': _m_runtime,
    '_make_followup_runner': _m_runtime,
    '_notification_image_data': _m_delivery,
    '_notify_all_devices': _m_delivery,
    '_notify_i18n': _m_common,
    '_offer_area': _m_runtime,
    '_on_lockdown_state': _m_lockdown_sync,
    '_on_state_changed': _m_runtime,
    '_pattern_log_interval': _m_learning,
    '_pattern_opted_in': _m_learning,
    '_pattern_rate_ok': _m_learning,
    '_persona': _m_common,
    '_push_notification': _m_delivery,
    '_rating_data': _m_delivery,
    '_sync_lockdown_to_alarm': _m_lockdown_sync,
    '_temp_to_f': _m_common,
    '_tick': _m_runtime,
    'accept_pending_offer': _m_runtime,
    'apply_runtime_config': _m_runtime,
    'backfill_from_history': _m_learning,
    'build_lockdown_message': _m_lockdown,
    'decline_pending_offer': _m_runtime,
    'discover_outdoor_temp': _m_common,
    'ensure_lockdown': _m_lockdown_sync,
    'get_pending_offer': _m_runtime,
    'ignore': _m_runtime,
    'intrusion_status': _m_runtime,
    'is_ignored': _m_runtime,
    'is_lockdown': _m_lockdown_sync,
    'is_outdoor_notable': _m_ignore,
    'learning_active': _m_learning,
    'list_ignores': _m_runtime,
    'lockdown_status': _m_lockdown_sync,
    'log_camera_event': _m_learning,
    'log_command': _m_learning,
    'release_runtime': _m_runtime,
    'request_lockdown': _m_lockdown_sync,
    'revoke_autonomy': _m_runtime,
    'run_analysis_now': _m_learning,
    'start': _m_runtime,
    'status': _m_runtime,
    'stop': _m_runtime,
    'unignore': _m_runtime,
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
