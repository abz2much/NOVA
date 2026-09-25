"""Lifecycle resource registry for a Nova config entry.

Collects the disposables created during setup — event-listener unsub callbacks,
background asyncio tasks, and objects with a shutdown()/close() — behind one
handle, so async_unload_entry can tear everything down with a single fail-safe
close_all(). Each item is disposed independently (one failure never blocks the
rest), and close_all() is idempotent, so a reload can't double-dispose or leak.

Owners whose teardown is itself asynchronous (the ProviderManager, which
closes provider clients off the event loop) register with
add_async_closeable(); async_unload_entry awaits async_close_all(), which
runs close_all() and then awaits each of them once.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


class NovaResources:
    """A bag of things to dispose when the entry unloads."""

    def __init__(self):
        self._unsubs: list = []       # callables returned by track/listen
        self._tasks: list = []        # asyncio tasks/handles with .cancel()
        self._closeables: list = []   # objects with .shutdown() or .close()
        self._async_closeables: list = []   # objects with async_close()

    def add_unsub(self, unsub) -> None:
        if callable(unsub):
            self._unsubs.append(unsub)

    def add_unsubs(self, unsubs) -> None:
        for u in (unsubs or []):
            self.add_unsub(u)

    def add_task(self, task) -> None:
        if task is not None:
            self._tasks.append(task)

    def add_closeable(self, obj) -> None:
        if obj is not None:
            self._closeables.append(obj)

    def add_async_closeable(self, obj) -> None:
        if obj is not None and callable(getattr(obj, "async_close", None)):
            self._async_closeables.append(obj)

    async def async_close_all(self) -> dict:
        """close_all(), then await every async closeable once. Fail-safe and
        idempotent like close_all()."""
        summary = self.close_all()
        summary["async_closeables"] = 0
        pending, self._async_closeables = self._async_closeables, []
        for obj in pending:
            try:
                await obj.async_close()
                summary["async_closeables"] += 1
            except Exception:
                summary["errors"] += 1
        return summary

    def close_all(self) -> dict:
        """Dispose everything registered, fail-safe and idempotent. Returns a
        small summary ({unsubs, tasks, closeables, errors})."""
        summary = {"unsubs": 0, "tasks": 0, "closeables": 0, "errors": 0}
        for unsub in self._unsubs:
            try:
                if callable(unsub):
                    unsub()
                    summary["unsubs"] += 1
            except Exception:
                summary["errors"] += 1
        for task in self._tasks:
            try:
                cancel = getattr(task, "cancel", None)
                if callable(cancel):
                    cancel()
                    summary["tasks"] += 1
            except Exception:
                summary["errors"] += 1
        for obj in self._closeables:
            try:
                fn = getattr(obj, "shutdown", None) or getattr(obj, "close", None)
                if callable(fn):
                    fn()
                    summary["closeables"] += 1
            except Exception:
                summary["errors"] += 1
        self._unsubs.clear()
        self._tasks.clear()
        self._closeables.clear()
        return summary
