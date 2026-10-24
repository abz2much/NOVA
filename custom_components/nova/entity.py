"""The shared base of Nova's state entities (sensor.py, binary_sensor.py).

Each entity reads its value from nova_state when added and again whenever
something changes: the SIGNAL_STATE dispatcher signal or one of the
STATE_EVENTS on the bus. None of them polls. A reader that fails makes only
that entity unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_point_in_utc_time

from . import nova_state
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_attach(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Point nova_state's change signal at this Home Assistant and load the
    saved briefing. Called by each platform; repeating it is harmless."""
    nova_state.attach(hass)
    entry.async_on_unload(lambda: nova_state.detach(hass))
    await hass.async_add_executor_job(nova_state.load_briefing)


class NovaStateEntity(Entity):
    """One piece of Nova's state, on the entry's Nova device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self.entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._cancel_wake: Optional[Callable[[], Any]] = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, nova_state.SIGNAL_STATE, self._async_changed))
        for event in nova_state.STATE_EVENTS:
            self.async_on_remove(self.hass.bus.async_listen(event, self._async_changed))
        self.async_on_remove(self._cancel_timer)
        await self._async_refresh()

    async def _async_changed(self, *_args: Any) -> None:
        await self._async_refresh()
        self.async_write_ha_state()

    async def _async_refresh(self) -> None:
        try:
            await self._async_read()
            self._attr_available = True
        except Exception as exc:  # noqa: BLE001 - one source failing is not fatal
            _LOGGER.debug("Nova %s unavailable: %s", self._attr_translation_key, exc)
            self._attr_available = False

    async def _async_read(self) -> None:
        """Set this entity's value. Raise to make it unavailable."""
        self._read()

    def _read(self) -> None:
        raise NotImplementedError

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel_wake is not None:
            self._cancel_wake()
            self._cancel_wake = None

    @callback
    def _wake_at(self, when: Optional[datetime]) -> None:
        """Read again at ``when`` (for a nap that ends with no event)."""
        self._cancel_timer()
        if when is not None:
            self._cancel_wake = async_track_point_in_utc_time(
                self.hass, self._async_woken, when)

    async def _async_woken(self, _now: datetime) -> None:
        self._cancel_wake = None
        await self._async_changed()
