"""Comfort and efficiency offers: a dark room with someone in it, a light
left on in an empty room, heating or cooling an empty house.

cognitive_core.py re-exports ProactiveManager under its old name.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import core_common as _m_common
from .core_common import (
    DARK_LUX_THRESHOLD,
    PROACTIVE_CHECK_INTERVAL,
    PROACTIVE_OFFER_COOLDOWN,
    STALE_LIGHT_MINUTES,
    _persona,
)
from .device_capability import can_perform

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

_PRESENCE_CLASSES = ("motion", "occupancy", "presence")


def _info_only(kind: str, key: str, message: str) -> dict:
    """A notice Nova cannot act on: no question, no action, nothing to
    learn. The offer_key still gives it the usual cooldown."""
    return {"type": kind, "urgency": "low", "offer": False, "offer_key": key,
            "message": message}


def _offer(kind: str, key: str, message: str, action: dict, pattern: str) -> dict:
    return {"type": kind, "urgency": "low", "offer": True, "offer_key": key,
            "message": message, "action_data": action, "pattern_key": pattern}


def _entity_registry(hass):
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass)


class ProactiveManager:
    """Notices when a small action would make the home nicer or cheaper and
    offers it. Safety is SafetyManager's job; this is about friction.

    An offer is only ever a spoken or pushed question, accepted by voice in
    the room that heard it within a minute. Nova never carries one out by
    itself, however often it was accepted before. The proactive switch,
    quiet hours and sleep, ignore rules and a cooldown per opportunity all
    apply, so nothing is asked twice in a row.
    """

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self._last_check = 0.0
        self._offer_cooldowns: dict[str, float] = {}   # offer key -> when offered

    def _on_cooldown(self, key: str) -> bool:
        offered = self._offer_cooldowns.get(key, 0.0)
        return time.time() - offered < PROACTIVE_OFFER_COOLDOWN

    def _mark_offered(self, key: str) -> None:
        """Called by the tick once an offer was really delivered, so one held
        back for now comes round again."""
        self._offer_cooldowns[key] = time.time()

    async def tick(self, sleeping: bool, anyone_home: bool,
                   nobody_home: Optional[bool] = None) -> list[dict]:
        """The offers due now, in the same dict shape SafetyManager uses.

        ``nobody_home`` is True only when household.py is sure everyone is
        out; left as None it is the opposite of ``anyone_home``.
        """
        now = time.time()
        if now - self._last_check < PROACTIVE_CHECK_INTERVAL:
            return []
        self._last_check = now
        if sleeping:
            return []   # comfort waits for the morning
        if nobody_home is None:
            nobody_home = not anyone_home

        checks = (
            ("dark-room", self._check_dark_occupied_room, anyone_home),
            ("stale-light", self._check_stale_lights, anyone_home),
            # "No one's home" only when that is known for sure.
            ("HVAC", self._check_hvac_efficiency, not nobody_home),
        )
        offers: list[dict] = []
        for label, check, arg in checks:
            try:
                found = await check(arg)
            except Exception as exc:
                _LOGGER.debug("Proactive %s check error: %s", label, exc)
                continue
            if found:
                offers.append(found)
        return offers

    async def _check_dark_occupied_room(self, anyone_home: bool) -> Optional[dict]:
        """A dark room with someone in it and every light off: offer to turn
        on the lights Nova can switch. A room on cooldown is passed over
        for the next one."""
        if not anyone_home:
            return None
        reg = _entity_registry(self.hass)
        states = self.hass.states
        for sensor in states.async_all("sensor"):
            if sensor.attributes.get("device_class") != "illuminance":
                continue
            try:
                if float(sensor.state) > DARK_LUX_THRESHOLD:
                    continue
            except (TypeError, ValueError):
                continue
            entry = reg.async_get(sensor.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id or not self._area_has_presence(area_id, reg):
                continue
            lights = self._area_lights(area_id, reg)
            if not lights:
                continue
            if any((st := states.get(lid)) and st.state == "on" for lid in lights):
                continue
            switchable = [lid for lid in lights
                          if can_perform(self.hass, lid, "light", "turn_on")]
            key = f"dark:{area_id}"
            if not switchable or self._on_cooldown(key):
                continue
            message = _persona().lead_in(
                _m_common._live_honorific(self.hass),
                f"it's quite dark in the {self._area_name(area_id)} and someone's "
                "in there. Shall I turn the lights on?")
            return _offer("proactive_lights", key, message,
                          {"domain": "light", "service": "turn_on",
                           "entity_ids": switchable},
                          f"lights_on_when_dark:{area_id}")
        return None

    async def _check_stale_lights(self, anyone_home: bool) -> Optional[dict]:
        """A light on for a long while in a room its sensors show empty.
        A room with no motion or presence sensor is never called empty: Nova
        cannot see whether anyone is in it. A light on cooldown is passed
        over for the next one."""
        reg = _entity_registry(self.hass)
        now = dt_util.utcnow()
        for light in self.hass.states.async_all("light"):
            if light.state != "on" or not light.last_changed:
                continue
            minutes = (now - light.last_changed).total_seconds() / 60.0
            if minutes < STALE_LIGHT_MINUTES:
                continue
            entry = reg.async_get(light.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id or not self._area_known_empty(area_id, reg):
                continue
            key = f"stale:{light.entity_id}"
            if self._on_cooldown(key):
                continue
            name = light.attributes.get("friendly_name", light.entity_id)
            said = (f"the {name} has been on for {int(minutes)} minutes in the "
                    f"{self._area_name(area_id)}, which appears empty.")
            honorific = _m_common._live_honorific(self.hass)
            if not can_perform(self.hass, light.entity_id, "light", "turn_off"):
                return _info_only("proactive_stale_light", key,
                                  _persona().lead_in(honorific, said))
            return _offer("proactive_stale_light", key,
                          _persona().lead_in(honorific, f"{said} Shall I turn it off?"),
                          {"domain": "light", "service": "turn_off",
                           "entity_ids": [light.entity_id]},
                          f"lights_off_when_empty:{area_id}")
        return None

    async def _check_hvac_efficiency(self, anyone_home: bool) -> Optional[dict]:
        """Heating or cooling with nobody home: offer the eco preset where
        the thermostat has one, else just say so. A thermostat on cooldown
        is passed over for the next one."""
        if anyone_home:
            return None
        eco = {"preset_mode": "eco"}
        for climate in self.hass.states.async_all("climate"):
            action = climate.attributes.get("hvac_action")
            if action not in ("heating", "cooling"):
                continue
            key = f"hvac:{climate.entity_id}"
            if self._on_cooldown(key):
                continue
            name = climate.attributes.get("friendly_name", climate.entity_id)
            said = f"the {name} is {action} but no one's home."
            honorific = _m_common._live_honorific(self.hass)
            if not can_perform(self.hass, climate.entity_id, "climate",
                               "set_preset_mode", eco):
                return _info_only("proactive_hvac", key,
                                  _persona().lead_in(honorific, said))
            return _offer("proactive_hvac", key,
                          _persona().lead_in(honorific, f"{said} Would you like me "
                                                        "to set it back to save energy?"),
                          {"domain": "climate", "service": "set_preset_mode",
                           "entity_ids": [climate.entity_id], "service_data": eco},
                          "hvac_eco_when_away")
        return None

    # ── rooms ────────────────────────────────────────────────────────

    def _presence_readings(self, area_id: str, ent_reg) -> list[str]:
        """States of the area's motion, occupancy and presence sensors. A
        camera's car, animal or package sensor is not a person and is left
        out."""
        from .entity_filter import is_object_sensor
        readings = []
        for sensor in self.hass.states.async_all("binary_sensor"):
            if sensor.attributes.get("device_class") not in _PRESENCE_CLASSES:
                continue
            if is_object_sensor(sensor.entity_id, sensor.attributes.get("friendly_name")):
                continue
            entry = ent_reg.async_get(sensor.entity_id)
            if entry and entry.area_id == area_id:
                readings.append(sensor.state)
        return readings

    def _area_has_presence(self, area_id: str, ent_reg) -> bool:
        """Someone is sensed in the area right now."""
        return "on" in self._presence_readings(area_id, ent_reg)

    def _area_known_empty(self, area_id: str, ent_reg) -> bool:
        """The area's sensors say nobody is there: at least one reads off and
        none reads on. No sensor, or only unavailable ones, is not known."""
        readings = self._presence_readings(area_id, ent_reg)
        return "off" in readings and "on" not in readings

    def _area_lights(self, area_id: str, ent_reg) -> list[str]:
        """Every light in the area."""
        return [light.entity_id for light in self.hass.states.async_all("light")
                if (entry := ent_reg.async_get(light.entity_id))
                and entry.area_id == area_id]

    def _area_name(self, area_id: str) -> str:
        try:
            from homeassistant.helpers import area_registry as ar
            area = ar.async_get(self.hass).async_get_area(area_id)
        except Exception:
            return area_id
        return area.name if area else area_id
