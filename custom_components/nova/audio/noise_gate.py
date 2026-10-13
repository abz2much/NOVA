"""Take running appliances' noise off the room's measured sound level.

Reads appliance power sensors from hass.states; imports no Home Assistant
modules so the file loads on its own.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

DEFAULT_POWER_ON_W = 10.0

# Power sensor -> dB the appliance adds to the room while running.
DEFAULT_PROFILES: dict[str, float] = {
    f"sensor.{name}_power": db for name, db in (
        ("dishwasher", 8.0), ("washer", 7.0), ("dryer", 9.0),
        ("microwave", 11.0), ("sump_pump", 9.0), ("dehumidifier", 5.0))
}


def _watts(value: Any) -> Optional[float]:
    if value is not None and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return None


class NoiseGate:
    def __init__(self, hass: Any, profiles: dict[str, float] | None = None, *,
                 power_on_threshold: float = DEFAULT_POWER_ON_W) -> None:
        self.hass = hass
        self.profiles = DEFAULT_PROFILES if profiles is None else profiles
        self.threshold = power_on_threshold

    def running_appliances(self) -> list[str]:
        running = []
        for entity_id in self.profiles:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            watts = _watts(state.state)
            if watts is not None and watts > self.threshold:
                running.append(entity_id)
        return running

    def attenuation(self) -> float:
        running = self.running_appliances()
        if not running:
            return 0.0
        return max(self.profiles[eid] for eid in running)

    def compensated_db(self, ambient_db: float | None) -> float | None:
        if ambient_db is None:
            return None
        cut = self.attenuation()
        if cut <= 0:
            return ambient_db
        result = max(0.0, ambient_db - cut)
        _LOGGER.debug("Noise gate: %.1f dB raw, %.1f dB appliance noise, %.1f dB used",
                      ambient_db, cut, result)
        return result
