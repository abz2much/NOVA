"""Nova's binary sensors: Quiet and Lockdown."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import nova_state
from .entity import NovaStateEntity, async_attach


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    await async_attach(hass, entry)
    async_add_entities([NovaQuiet(entry), NovaLockdown(entry)])


class NovaQuiet(NovaStateEntity, BinarySensorEntity):
    """On while any mute or the blanket shush is active."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "quiet")

    def _read(self) -> None:
        self._attr_is_on, self._attr_extra_state_attributes = nova_state.quiet()


class NovaLockdown(NovaStateEntity, BinarySensorEntity):
    """On while lockdown is active."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "lockdown")

    def _read(self) -> None:
        self._attr_is_on = nova_state.lockdown_active()
