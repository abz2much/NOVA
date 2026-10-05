"""The cognitive core's one shared state object, _CORE.

Other core modules read it as core_state._CORE when they run and never bind
it by name, so a test that swaps _CORE on cognitive_core (which forwards the
write here) is seen everywhere. The manager classes are imported only
for _CoreState's attribute annotations.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant

from .core_autonomy import AutonomyManager
from .core_ignore import IgnoreManager
from .core_lockdown import LockdownManager
from .core_pattern_store import StateLogger
from .core_proactive import ProactiveManager
from .core_safety import SafetyManager

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


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
