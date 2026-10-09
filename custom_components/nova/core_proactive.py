"""ProactiveManager: comfort and efficiency offers (a dark occupied room, a light
left on in an empty room, heating or cooling an empty house).

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import core_common as _m_common
from .device_capability import can_perform
from .core_common import (
    DARK_LUX_THRESHOLD,
    PROACTIVE_CHECK_INTERVAL,
    PROACTIVE_OFFER_COOLDOWN,
    STALE_LIGHT_MINUTES,
    _persona,
)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# ── Proactive Intelligence (v5.9.07) ────────────────────────────────────────

def _info_only(kind: str, key: str, message: str) -> dict:
    """An alert with nothing to accept (8.14.0): Nova cannot do the action, so
    there is no question, no action and no pattern to learn. offer_key keeps
    the usual cooldown so it is not repeated every tick."""
    return {"type": kind, "urgency": "low", "offer": False, "offer_key": key,
            "message": message}


class ProactiveManager:
    """
    Pursues comfort & efficiency opportunities — not just safety.

    Where SafetyManager prevents harm, ProactiveManager reduces friction:
    it notices when a small action would help (dark room with someone in it,
    a light left on in an empty room, HVAC fighting an empty house) and
    OFFERS to act. It never forces — offers are spoken/pushed suggestions the
    user can accept by voice. Graduated autonomy (see AutonomyManager) can
    later promote a repeatedly-approved offer to silent auto-execution.

    All offers respect: the global proactive kill-switch, quiet hours/sleep,
    ignore rules, and a per-opportunity cooldown so Nova never nags.
    """

    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self._last_check = 0.0
        self._offer_cooldowns: dict[str, float] = {}  # opportunity_key -> ts

    def _on_cooldown(self, key: str) -> bool:
        last = self._offer_cooldowns.get(key, 0.0)
        return (time.time() - last) < PROACTIVE_OFFER_COOLDOWN

    def _mark_offered(self, key: str) -> None:
        self._offer_cooldowns[key] = time.time()

    async def tick(self, sleeping: bool, anyone_home: bool) -> list[dict]:
        """
        Evaluate comfort/efficiency opportunities. Returns a list of offer
        actions (same dict shape SafetyManager uses, with offer=True).
        """
        now = time.time()
        if (now - self._last_check) < PROACTIVE_CHECK_INTERVAL:
            return []
        self._last_check = now

        # Proactive offers are silent during sleep — comfort can wait.
        if sleeping:
            return []

        offers: list[dict] = []

        try:
            dark = await self._check_dark_occupied_room(anyone_home)
            if dark:
                offers.append(dark)
        except Exception as exc:
            _LOGGER.debug("Proactive dark-room check error: %s", exc)

        try:
            stale = await self._check_stale_lights(anyone_home)
            if stale:
                offers.append(stale)
        except Exception as exc:
            _LOGGER.debug("Proactive stale-light check error: %s", exc)

        try:
            hvac = await self._check_hvac_efficiency(anyone_home)
            if hvac:
                offers.append(hvac)
        except Exception as exc:
            _LOGGER.debug("Proactive HVAC check error: %s", exc)

        return offers

    async def _check_dark_occupied_room(self, anyone_home: bool) -> Optional[dict]:
        """Someone present in a room that's dark and has lights off → offer."""
        if not anyone_home:
            return None
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(self.hass)

        # Find lux sensors that read dark
        for s in self.hass.states.async_all("sensor"):
            if s.attributes.get("device_class") != "illuminance":
                continue
            try:
                lux = float(s.state)
            except (ValueError, TypeError):
                continue
            if lux > DARK_LUX_THRESHOLD:
                continue

            # Determine the area of this sensor
            entry = ent_reg.async_get(s.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id:
                continue

            # Is there occupancy (motion/presence) in this area?
            occupied = self._area_has_presence(area_id, ent_reg)
            if not occupied:
                continue

            # Are the lights in this area already off?
            lights = self._area_lights(area_id, ent_reg)
            if not lights:
                continue
            if any(self.hass.states.get(l).state == "on" for l in lights if self.hass.states.get(l)):
                continue  # already lit
            # Only lights Nova can really switch on (8.14.0). With none, there
            # is nothing to offer, so no message at all.
            lights = [l for l in lights if can_perform(self.hass, l, "light", "turn_on")]
            if not lights:
                continue

            key = f"dark:{area_id}"
            if self._on_cooldown(key):
                return None
            # Cooldown is marked by the tick only when this offer is actually
            # delivered (see _tick), so deferred offers re-surface naturally.

            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
            area_name = self._area_name(area_id)
            return {
                "type": "proactive_lights",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"it's quite dark in the {area_name} "
                    f"and someone's in there. Shall I turn the lights on?"
                ),
                "action_data": {"domain": "light", "service": "turn_on",
                                "entity_ids": lights},
                "pattern_key": f"lights_on_when_dark:{area_id}",
            }
        return None

    async def _check_stale_lights(self, anyone_home: bool) -> Optional[dict]:
        """Light on a long time in an unoccupied area → offer to turn off."""
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(self.hass)
        now = dt_util.utcnow()

        for s in self.hass.states.async_all("light"):
            if s.state != "on":
                continue
            # How long has it been on?
            last_changed = s.last_changed
            if not last_changed:
                continue
            mins_on = (now - last_changed).total_seconds() / 60.0
            if mins_on < STALE_LIGHT_MINUTES:
                continue

            entry = ent_reg.async_get(s.entity_id)
            area_id = entry.area_id if entry else None
            if not area_id:
                continue

            # Only flag if the area has NO presence
            if self._area_has_presence(area_id, ent_reg):
                continue

            key = f"stale:{s.entity_id}"
            if self._on_cooldown(key):
                return None

            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
            name = s.attributes.get("friendly_name", s.entity_id)
            area_name = self._area_name(area_id)
            if not can_perform(self.hass, s.entity_id, "light", "turn_off"):
                return _info_only("proactive_stale_light", key, _persona().lead_in(honorific,
                    f"the {name} has been on for {int(mins_on)} minutes in the "
                    f"{area_name}, which appears empty."))
            return {
                "type": "proactive_stale_light",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"the {name} has been on for "
                    f"{int(mins_on)} minutes in the {area_name}, which appears "
                    f"empty. Shall I turn it off?"
                ),
                "action_data": {"domain": "light", "service": "turn_off",
                                "entity_ids": [s.entity_id]},
                "pattern_key": f"lights_off_when_empty:{area_id}",
            }
        return None

    async def _check_hvac_efficiency(self, anyone_home: bool) -> Optional[dict]:
        """Climate actively heating/cooling while the house is empty → flag."""
        if anyone_home:
            return None
        for s in self.hass.states.async_all("climate"):
            action = s.attributes.get("hvac_action")
            if action not in ("heating", "cooling"):
                continue

            key = f"hvac:{s.entity_id}"
            if self._on_cooldown(key):
                return None

            honorific = _m_common._live_honorific(self.hass)  # Phase C: presence-aware
            name = s.attributes.get("friendly_name", s.entity_id)
            if not can_perform(self.hass, s.entity_id, "climate", "set_preset_mode",
                               {"preset_mode": "eco"}):
                return _info_only("proactive_hvac", key, _persona().lead_in(honorific,
                    f"the {name} is {action} but no one's home."))
            return {
                "type": "proactive_hvac",
                "urgency": "low",
                "offer": True,
                "offer_key": key,
                "message": _persona().lead_in(honorific,
                    f"the {name} is {action} but no one's "
                    f"home. Would you like me to set it back to save energy?"
                ),
                "action_data": {"domain": "climate", "service": "set_preset_mode",
                                "entity_ids": [s.entity_id],
                                "service_data": {"preset_mode": "eco"}},
                "pattern_key": "hvac_eco_when_away",
            }
        return None

    # ── Area helpers ─────────────────────────────────────────────────
    def _area_has_presence(self, area_id: str, ent_reg) -> bool:
        """True if any motion/occupancy/presence sensor in the area is active. A
        camera's car, animal or package sensor is not a person (8.14.0)."""
        from .entity_filter import is_object_sensor
        for s in self.hass.states.async_all("binary_sensor"):
            dc = s.attributes.get("device_class")
            if dc not in ("motion", "occupancy", "presence"):
                continue
            if is_object_sensor(s.entity_id, s.attributes.get("friendly_name")):
                continue
            entry = ent_reg.async_get(s.entity_id)
            if entry and entry.area_id == area_id and s.state == "on":
                return True
        return False

    def _area_lights(self, area_id: str, ent_reg) -> list[str]:
        """All light entity_ids in the given area."""
        out = []
        for s in self.hass.states.async_all("light"):
            entry = ent_reg.async_get(s.entity_id)
            if entry and entry.area_id == area_id:
                out.append(s.entity_id)
        return out

    def _area_name(self, area_id: str) -> str:
        try:
            from homeassistant.helpers import area_registry as ar
            reg = ar.async_get(self.hass)
            area = reg.async_get_area(area_id)
            return area.name if area else area_id
        except Exception:
            return area_id
