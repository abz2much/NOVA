"""Nova's sensors: what Nova is doing, what it last said, what is coming up.

Long text (briefing, camera summaries, reminders, suggestions, the last
message) is kept in attributes the recorder does not store.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval

from . import nova_state
from .entity import NovaStateEntity, async_attach


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    await async_attach(hass, entry)
    async_add_entities([
        NovaStatus(entry), NovaNapEnds(entry), NovaLastAnnouncement(entry),
        NovaNextReminder(entry), NovaBriefing(entry), NovaCameraSummary(entry),
        NovaEnergyNote(entry), NovaSuggestions(entry),
    ])


class NovaStatus(NovaStateEntity, SensorEntity):
    """ready, quiet, napping, lockdown or observer_off."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = nova_state.STATUS_OPTIONS

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "status")

    def _read(self) -> None:
        value = nova_state.status(self.entry)
        if value == nova_state.STATUS_UNAVAILABLE:
            raise RuntimeError("entry not loaded")
        self._attr_native_value = value
        self._wake_at(nova_state.nap_until())


class NovaNapEnds(NovaStateEntity, SensorEntity):
    """When the current nap ends; unknown when not napping."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "nap_ends")

    def _read(self) -> None:
        self._attr_native_value = until = nova_state.nap_until()
        self._wake_at(until)


class NovaLastAnnouncement(NovaStateEntity, SensorEntity):
    """When Nova last spoke, with what it said."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"message"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "last_announcement")

    def _read(self) -> None:
        when, message = nova_state.last_announcement()
        self._attr_native_value = when
        self._attr_extra_state_attributes = {"message": message}


class NovaNextReminder(NovaStateEntity, SensorEntity):
    """How many reminders are due in the next 24 hours."""

    _unrecorded_attributes = frozenset({"reminders"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "next_reminder")

    async def _async_read(self) -> None:
        count, shown = await self.hass.async_add_executor_job(nova_state.next_reminders)
        self._attr_native_value = count
        self._attr_extra_state_attributes = {"reminders": shown}


class NovaBriefing(NovaStateEntity, SensorEntity):
    """When the latest briefing ran, with its text."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"text"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "briefing")

    def _read(self) -> None:
        when, text = nova_state.latest_briefing()
        self._attr_native_value = when
        self._attr_extra_state_attributes = {"text": text}


class NovaCameraSummary(NovaStateEntity, SensorEntity):
    """Time of the latest camera summary, with the latest and recent text."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"summary", "recent"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "camera_summary")

    def _read(self) -> None:
        when, summary, recent = nova_state.camera_summary()
        self._attr_native_value = when
        self._attr_extra_state_attributes = {"summary": summary, "recent": recent}


class NovaSuggestions(NovaStateEntity, SensorEntity):
    """How many automation suggestions are waiting."""

    _unrecorded_attributes = frozenset({"suggestions"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "suggestions")

    async def _async_read(self) -> None:
        from .ws_panel_stats import _entity_names
        names = _entity_names(self.hass)
        count, shown = await self.hass.async_add_executor_job(
            nova_state.suggestion_items, names)
        self._attr_native_value = count
        self._attr_extra_state_attributes = {"suggestions": shown}


class NovaEnergyNote(NovaStateEntity, SensorEntity):
    """One sentence on today's energy against yesterday. The only entity on
    a timer: every 30 minutes and just after midnight."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "energy_note")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_track_time_interval(
            self.hass, self._async_changed, nova_state.ENERGY_REFRESH))
        self.async_on_remove(async_track_time_change(
            self.hass, self._async_changed, hour=0, minute=0, second=30))
        await self._async_refresh()

    async def _async_read(self) -> None:
        self._attr_native_value = await nova_state.async_energy_note(self.hass)
