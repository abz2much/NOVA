"""Per-signature provider holds, owned by one loaded Nova runtime.

After a provider reply that is not a valid decision, the event's reasoning
signature is held for a short time so a repeat of the same event goes to the
fallback instead of asking the same provider again. The holds belong to the
entry's NovaRuntime (``runtime.provider_holds``): a reload builds a new
runtime and so starts with none, and a changed provider or configuration can
never inherit them. Nothing here is persisted or kept at module level.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable

HOLD_S = 60.0          # matches the connectivity breaker's cooldown
MAX_HOLDS = 64         # bounded: the oldest hold is dropped beyond this


class ProviderHolds:
    """A bounded set of held signatures with expiry. Event-loop only."""

    __slots__ = ("hold_s", "max_holds", "_clock", "_until")

    def __init__(self, hold_s: float = HOLD_S, max_holds: int = MAX_HOLDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.hold_s = float(hold_s)
        self.max_holds = int(max_holds)
        self._clock = clock
        self._until: "OrderedDict[str, float]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._until)

    def held(self, sig: str) -> bool:
        until = self._until.get(sig)
        if until is None:
            return False
        if until <= self._clock():
            self._until.pop(sig, None)
            return False
        return True

    def hold(self, sig: str) -> None:
        now = self._clock()
        for key in [k for k, until in self._until.items() if until <= now]:
            self._until.pop(key, None)
        self._until[sig] = now + self.hold_s
        self._until.move_to_end(sig)
        while len(self._until) > self.max_holds:
            self._until.popitem(last=False)

    def clear(self) -> None:
        """Drop every hold. Idempotent."""
        self._until.clear()
