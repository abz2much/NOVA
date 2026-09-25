"""Attribute observed state changes to the automation run that caused them.

Home Assistant fires ``automation_triggered`` with the run's context before
the action script runs, and the actions' service calls carry that context.
A bounded, time-limited in-memory map from context id to the automation lets
the state logger tag each change it records, so automation-caused changes
never train pattern learning. Nothing here is persisted.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Optional

from .models import SourceAttribution

if TYPE_CHECKING:
    from .inventory import AutomationInventory


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


class AutomationContextTracker:
    """Correlate HA automation contexts with the state changes they cause.

    ``automation_triggered`` is emitted before the action script runs and uses
    the same context passed into its service calls.  A fixed-size TTL map makes
    attribution a constant-time lookup on the state-change hot path.
    """

    def __init__(self, inventory: Optional[AutomationInventory] = None, *,
                 ttl: float = 21600.0, max_entries: int = 4096):
        self.inventory = inventory
        self.ttl = max(60.0, float(ttl))
        self.max_entries = max(64, int(max_entries))
        self._contexts: OrderedDict[str, tuple[float, SourceAttribution]] = OrderedDict()

    @staticmethod
    def _context_id(context: Any, field_name: str = "id") -> str:
        value = _safe_attr(context, field_name, "") if context is not None else ""
        return str(value or "")

    def record_trigger(self, event: Any, now: Optional[float] = None) -> None:
        entity_id = str(getattr(event, "data", {}).get("entity_id") or "")
        context_id = self._context_id(getattr(event, "context", None))
        if not entity_id or not context_id:
            return
        record = self.inventory.get(entity_id) if self.inventory else None
        kind = "nova_automation" if record and record.origin == "nova" else "automation"
        stamp = time.monotonic() if now is None else float(now)
        self._contexts[context_id] = (
            stamp, SourceAttribution(kind, entity_id, 1.0))
        self._contexts.move_to_end(context_id)
        self._prune(stamp)

    def resolve_state(self, state: Any, now: Optional[float] = None) -> SourceAttribution:
        context = _safe_attr(state, "context")
        if context is None:
            return SourceAttribution()
        # A user_id is direct evidence of a user-initiated state/service action.
        if _safe_attr(context, "user_id"):
            return SourceAttribution("user", "", 1.0)
        stamp = time.monotonic() if now is None else float(now)
        self._prune(stamp)
        for context_id in (self._context_id(context),
                           self._context_id(context, "parent_id")):
            found = self._contexts.get(context_id)
            if found is not None:
                return found[1]
        # HA supplied a context but it was not one of the automation runs Nova
        # observed. It may be an integration or physical-device event; do not
        # claim a more precise source than the evidence supports.
        return SourceAttribution("device_or_integration", "", 0.5)

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl
        while self._contexts:
            _key, (stamp, _source) = next(iter(self._contexts.items()))
            if stamp >= cutoff and len(self._contexts) <= self.max_entries:
                break
            self._contexts.popitem(last=False)

    def close(self) -> None:
        self._contexts.clear()
        self.inventory = None
