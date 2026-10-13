"""Per-entry owner of Nova's recurring background ticks.

Each periodic job is registered once under a unique name. Its timer callback
is wrapped so every tick records timing and outcome, and a failing tick is
logged and counted instead of escaping into the timer. ``status()`` gives a
diagnostics snapshot and ``shutdown()`` cancels every timer in one safe call.

The timer itself comes from an injectable tracker so the logic can run without
Home Assistant; the default tracker imports Home Assistant lazily.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

TickCallback = Callable[[Any], Awaitable[Any]]
Tracker = Callable[[Any, Callable[..., Awaitable[None]], timedelta], Any]


def _default_tracker(
    hass: Any, wrapper: Callable[..., Awaitable[None]], interval: timedelta
) -> Any:
    """Schedule via Home Assistant; imported here so the module stays HA-free."""
    from homeassistant.helpers.event import async_track_time_interval

    return async_track_time_interval(hass, wrapper, interval)


@dataclass
class _Task:
    """Registration and run bookkeeping for one named task."""

    name: str
    interval_s: float
    cb: TickCallback
    unsub: Any = None
    runs: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_run: float = 0.0
    last_success: float = 0.0
    last_duration_ms: int = 0
    last_error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": int(self.interval_s),
            "runs": self.runs,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "last_run": self.last_run or None,
            "last_success": self.last_success or None,
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "healthy": self.consecutive_errors == 0,
        }


class NovaScheduler:
    """Registers named interval tasks and tracks how each tick went."""

    def __init__(self, hass: Any = None, tracker: Tracker | None = None) -> None:
        self._hass = hass
        self._tracker: Tracker = tracker or _default_tracker
        self._tasks: dict[str, _Task] = {}

    def add(self, name: str, interval: Any, cb: TickCallback) -> bool:
        """Register ``cb`` to run every ``interval``; False if skipped or refused."""
        if name in self._tasks:
            _LOGGER.debug("Scheduler task %s already registered; skipping", name)
            return False

        seconds = (
            interval.total_seconds()
            if isinstance(interval, timedelta)
            else float(interval)
        )
        task = _Task(name=name, interval_s=seconds, cb=cb)

        try:
            task.unsub = self._tracker(
                self._hass, self._make_wrapper(task), timedelta(seconds=seconds)
            )
        except Exception as exc:  # noqa: BLE001 - registration must not raise
            _LOGGER.warning("Could not schedule task %s: %s", name, exc)
            return False

        self._tasks[name] = task
        return True

    @staticmethod
    def _make_wrapper(task: _Task) -> Callable[..., Awaitable[None]]:
        async def _tick(now: Any = None) -> None:
            task.last_run = time.time()
            task.runs += 1
            started = time.monotonic()
            try:
                await task.cb(now)
            except Exception as exc:  # noqa: BLE001 - contain failing ticks
                task.errors += 1
                task.consecutive_errors += 1
                task.last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning(
                    "Scheduler task %s failed (%d consecutive): %s",
                    task.name,
                    task.consecutive_errors,
                    task.last_error,
                )
            else:
                task.last_success = time.time()
                task.consecutive_errors = 0
                task.last_error = ""
            finally:
                task.last_duration_ms = int((time.monotonic() - started) * 1000)

        return _tick

    def status(self) -> list[dict[str, Any]]:
        """Return a fresh snapshot per task, in registration order."""
        return [task.snapshot() for task in self._tasks.values()]

    def task_names(self) -> list[str]:
        return list(self._tasks)

    def shutdown(self) -> int:
        """Cancel every timer and forget all tasks; return how many unsubs ran."""
        cancelled = 0
        for task in self._tasks.values():
            unsub, task.unsub = task.unsub, None
            if not unsub:
                continue
            try:
                unsub()
            except Exception:  # noqa: BLE001 - teardown must never raise
                continue
            cancelled += 1
        self._tasks.clear()
        return cancelled
