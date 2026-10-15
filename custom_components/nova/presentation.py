"""Presenting: a small card of real figures shown beside the hologram.

The card is chosen from the tools the agent ran in a turn, never from the
model's own text, and it is built from Nova's own live energy readings (the
same source as the Energy tab). Only the latest card is kept, in memory on
the entry's runtime, so a panel opened inside the card's life still gets it.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Iterable, Optional

_LOGGER = logging.getLogger(__name__)

CARD_VERSION = 1
CARD_TTL_S = 30
ENERGY_TOOLS = frozenset({"energy_status", "solar_status", "energy_report", "energy_outlook"})


class PresentationStore:
    """The latest card and the panels listening for the next one."""

    def __init__(self) -> None:
        self.latest: Optional[dict] = None
        self._listeners: set[Callable[[dict], None]] = set()

    def current(self, now: Optional[float] = None) -> Optional[dict]:
        """The latest card while it is still unexpired, else None."""
        card = self.latest
        if card is None or card["expires"] <= (time.time() if now is None else now):
            return None
        return card

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def publish(self, card: dict) -> None:
        self.latest = card
        for listener in list(self._listeners):
            try:
                listener(card)
            except Exception as exc:   # one bad panel never blocks the rest
                _LOGGER.debug("presentation listener failed: %s", exc)


def _num(value: Any, digits: int) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


def energy_card_data(status: dict, today: dict) -> Optional[dict]:
    """The card figures from Nova's own energy readings. A figure Nova cannot
    read is None; with none readable there is no card (None). The day curve
    stays empty: Nova keeps no hourly series, and the panel hides it."""
    status = status if isinstance(status, dict) else {}
    today = today if isinstance(today, dict) else {}
    data = {
        "solar_today_kwh": _num(today.get("solar_kwh"), 1),
        "battery_pct": _num((status.get("battery") or {}).get("pct"), 0),
        "export_kwh": _num(today.get("grid_export_kwh"), 1),
        "home_kwh": _num(today.get("home_kwh"), 1),
        "solar_w": _num((status.get("solar") or {}).get("w"), 0),
        "curve": [],
    }
    if all(v is None for k, v in data.items() if k != "curve"):
        return None
    return data


async def build_energy_card(hass, *, now: Optional[float] = None) -> Optional[dict]:
    from . import energy_flow
    data = energy_card_data(await energy_flow.energy_flow_status(hass),
                            await energy_flow.energy_flow_today(hass))
    if data is None:
        return None
    created = time.time() if now is None else now
    return {"v": CARD_VERSION, "kind": "energy", "id": str(uuid.uuid4()),
            "created": created, "expires": created + CARD_TTL_S, "data": data}


async def present_turn(hass, tools_used: Iterable[str]) -> Optional[dict]:
    """After a turn that produced a reply: publish an energy card when an
    energy tool ran. Never raises; the reply is never held up by a card."""
    try:
        if not ENERGY_TOOLS.intersection(tools_used):
            return None
        from .runtime import domain_runtime
        runtime = domain_runtime(hass)
        if runtime is None:
            return None
        card = await build_energy_card(hass)
        if card is not None:
            runtime.presentation.publish(card)
        return card
    except Exception as exc:
        _LOGGER.debug("presentation: card failed: %s", exc)
        return None
