"""Per-entry registry of disposables released when the config entry unloads.

Setup code registers three kinds of things here: plain cancel callbacks
("unsubs"), objects with a synchronous ``shutdown()``/``close()`` method, and
objects with a coroutine ``async_close()`` method. A single teardown call then
disposes each of them independently, counting successes and failures, and
forgets them so a repeated teardown is a no-op. Nothing here depends on
Home Assistant.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _sync_disposer(obj: Any) -> Any:
    """Return the method used to tear ``obj`` down: ``shutdown`` unless falsy, else ``close``."""
    method = getattr(obj, "shutdown", None)
    if not method:
        method = getattr(obj, "close", None)
    return method


class NovaResources:
    """Collects disposables during setup and releases them on unload."""

    def __init__(self) -> None:
        self._unsubs: list[Any] = []
        self._closeables: list[Any] = []
        self._async_closeables: list[Any] = []

    # -- registration -----------------------------------------------------

    def add_unsub(self, unsub: Callable[[], Any] | Any) -> None:
        """Register a zero-argument cancel callback; non-callables are ignored."""
        if callable(unsub):
            self._unsubs.append(unsub)

    def add_unsubs(self, unsubs: Iterable[Any] | None) -> None:
        """Register every callable in ``unsubs``; a falsy argument registers nothing."""
        if not unsubs:
            return
        for unsub in unsubs:
            self.add_unsub(unsub)

    def add_closeable(self, obj: Any) -> None:
        """Register an object disposed via ``shutdown()`` or ``close()``."""
        if obj is not None:
            self._closeables.append(obj)

    def add_async_closeable(self, obj: Any) -> None:
        """Register an object with a callable ``async_close``; others are ignored."""
        if obj is not None and callable(getattr(obj, "async_close", None)):
            self._async_closeables.append(obj)

    # -- teardown ---------------------------------------------------------

    def close_all(self) -> dict[str, int]:
        """Dispose unsubs then closeables, forget them, and return the counts."""
        summary = {"unsubs": 0, "closeables": 0, "errors": 0}

        # Both loops iterate the live lists, so items registered during their
        # own pass are disposed in this same call.
        for unsub in self._unsubs:
            try:
                if not callable(unsub):
                    continue
                unsub()
                summary["unsubs"] += 1
            except Exception:  # noqa: BLE001 - every failure is isolated
                summary["errors"] += 1
                _LOGGER.warning("Nova unload: cancel callback %r failed", unsub,
                                exc_info=True)

        for obj in self._closeables:
            try:
                method = _sync_disposer(obj)
                if not callable(method):
                    continue
                method()
                summary["closeables"] += 1
            except Exception:  # noqa: BLE001
                summary["errors"] += 1
                _LOGGER.warning("Nova unload: closing %r failed", obj, exc_info=True)

        self._unsubs.clear()
        self._closeables.clear()
        return summary

    async def async_close_all(self) -> dict[str, int]:
        """Run :meth:`close_all`, then await each async closeable in order."""
        summary = self.close_all()
        summary["async_closeables"] = 0

        # Detach before awaiting so anything registered meanwhile is left for
        # the next teardown.
        pending, self._async_closeables = self._async_closeables, []
        for obj in pending:
            try:
                await obj.async_close()
                summary["async_closeables"] += 1
            except Exception:  # noqa: BLE001
                summary["errors"] += 1
                _LOGGER.warning("Nova unload: closing %r failed", obj, exc_info=True)
        return summary
