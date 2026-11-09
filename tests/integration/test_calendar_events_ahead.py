"""comms.gather_events_ahead against a real calendar entity and the real
calendar.get_events service: the request shape and the reply shape that the
unit tests can only assume. The calendar lists two events; its entity shows
only the first."""
import datetime

import pytest
from homeassistant.components.calendar import DATA_COMPONENT, CalendarEntity, CalendarEvent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.nova import comms


class _TwoEventCalendar(CalendarEntity):
    _attr_name = "Test Diary"

    def __init__(self):
        now = dt_util.now()
        self._events = [
            CalendarEvent(start=now - datetime.timedelta(minutes=10),
                          end=now + datetime.timedelta(minutes=20), summary="Running Meeting"),
            CalendarEvent(start=now + datetime.timedelta(minutes=60),
                          end=now + datetime.timedelta(minutes=90), summary="Dentist",
                          location="High Street"),
        ]

    @property
    def event(self):
        return self._events[0]            # an entity shows one event only

    async def async_get_events(self, hass, start_date, end_date):
        return [e for e in self._events
                if e.end_datetime_local > start_date and e.start_datetime_local < end_date]


# A calendar entity schedules a timer for its next event; it outlives the test.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_real_calendar_service_shows_the_event_after_the_running_one(hass):
    assert await async_setup_component(hass, "calendar", {})
    await hass.data[DATA_COMPONENT].async_add_entities([_TwoEventCalendar()])
    await hass.async_block_till_done()
    assert hass.states.get("calendar.test_diary") is not None

    assert [e["title"] for e in comms.gather_events(hass)] == ["Running Meeting"]
    events = await comms.gather_events_ahead(hass, 3)

    assert sorted(e["title"] for e in events) == ["Dentist", "Running Meeting"]
    dentist = next(e for e in events if e["title"] == "Dentist")
    assert dentist["location"] == "High Street"
    assert dentist["calendar"] == "calendar.test_diary" and dentist["all_day"] is False
