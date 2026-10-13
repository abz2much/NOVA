"""Per-entity locks with priority preemption.

Nova commands take a lock on the entity they are about to drive. A request with
a strictly higher priority than the current holder takes the lock over (the old
token is flagged invalid); anything equal or lower is turned away. There is no
queueing and no cancellation: this is plain bookkeeping for Home Assistant's
single-threaded event loop. Standard library only, so it can load standalone.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import IntEnum

_LOGGER = logging.getLogger(__name__)


class Priority(IntEnum):
    """Well-known priority levels; plain ints are accepted too."""

    PREDICTIVE = 10
    ROUTINE = 20
    INTENT = 30
    VISUAL = 40
    SAFETY = 50


class LockToken:
    """Proof of holding an entity's lock; ``valid`` goes False once lost."""

    __slots__ = ("entity", "priority", "valid")

    def __init__(self, entity: str, priority: int) -> None:
        self.entity = entity
        self.priority = int(priority)
        self.valid = True

    def __repr__(self) -> str:
        return f"LockToken({self.entity!r}, {self.priority}, valid={self.valid})"


class EntityLockRegistry:
    """Tracks the current lock holder for each entity id."""

    def __init__(self) -> None:
        self._holders: dict[str, LockToken] = {}

    def _holder(self, entity: str) -> LockToken | None:
        token = self._holders.get(entity)
        return token if token is not None and token.valid else None

    def try_acquire(self, entity: str, priority: int) -> LockToken | None:
        """Take the lock, preempting a lower-priority holder; None if refused."""
        wanted = int(priority)
        current = self._holder(entity)
        if current is not None:
            if wanted <= current.priority:
                _LOGGER.debug(
                    "Refused lock on %s at %s (held at %s)", entity, wanted, current.priority
                )
                return None
            _LOGGER.debug(
                "Preempting lock on %s: %s -> %s", entity, current.priority, wanted
            )
            current.valid = False
        token = LockToken(entity, wanted)
        self._holders[entity] = token
        return token

    def release(self, token: LockToken | None) -> None:
        """Invalidate ``token``; free the entity only if it is the holder."""
        if token is None:
            return
        token.valid = False
        if self._holders.get(token.entity) is token:
            del self._holders[token.entity]

    def is_locked(self, entity: str) -> bool:
        return self._holder(entity) is not None

    def held_priority(self, entity: str) -> int | None:
        current = self._holder(entity)
        return current.priority if current is not None else None

    @asynccontextmanager
    async def guard(self, entity: str, priority: int) -> AsyncIterator[LockToken | None]:
        """Hold the lock for the block's duration (yields None if refused)."""
        token = self.try_acquire(entity, priority)
        try:
            yield token
        finally:
            self.release(token)
