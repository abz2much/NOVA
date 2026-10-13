"""Hold ``nova.speak`` requests that arrive while the config entry starts up.

Requests are copied into a bounded FIFO. Once the integration reports it is
ready, everything held is replayed, oldest first, through a caller-supplied
async dispatch function. On overflow the oldest request is dropped so the
newest alerts survive. Nothing here depends on Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX = 25


class AlertBuffer:
    """FIFO holding pen for speak requests received before Nova is ready."""

    def __init__(self, maxsize: int = DEFAULT_MAX) -> None:
        self.ready: bool = False
        # asyncio.Queue treats maxsize <= 0 as unbounded.
        self._queue: asyncio.Queue[dict[Any, Any]] = asyncio.Queue(maxsize=maxsize)

    def begin(self) -> None:
        """Close the gate again; anything already held is kept for replay."""
        self.ready = False

    def enqueue(self, data: dict) -> None:
        """Store a shallow copy of ``data``, evicting the oldest when full."""
        item = dict(data)  # invalid input raises here, before any change
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            _LOGGER.warning("Alert buffer full; dropped the oldest held alert")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except Exception:  # pylint: disable=broad-except
                pass

    async def mark_ready(self, dispatch: Callable[[dict], Awaitable[None]]) -> int:
        """Open the gate and replay held requests; return successful dispatches."""
        if self.ready:
            return 0
        self.ready = True
        delivered = 0
        # Checked before every removal, so items added mid-drain replay too.
        while not self._queue.empty():
            item = self._queue.get_nowait()
            try:
                await dispatch(item)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Failed to replay buffered alert")
                continue
            delivered += 1
        return delivered

    def pending(self) -> int:
        """Number of requests currently held."""
        return self._queue.qsize()
