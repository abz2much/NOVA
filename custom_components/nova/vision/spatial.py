"""Fuse per-area presence signals into an occupancy confidence score.

Three sensors per area are consulted: a Frigate person count, a camera gaze
detector and an mmWave presence sensor. The result also says whether a spoken
announcement can drop its preamble because someone is present and looking.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_TRUTHY_STATES = frozenset({"on", "true", "detected", "home", "occupied"})


class SpatialContextEngine:
    """Score how confidently an area is occupied by an attentive listener."""

    PERSON_COUNT_WEIGHT = 0.60
    GAZE_WEIGHT = 0.20
    PRESENCE_WEIGHT = 0.35
    CONFIDENCE_MIN = 0.0
    CONFIDENCE_MAX = 1.0

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    def _state(self, entity_id: str) -> Any:
        obj = self.hass.states.get(entity_id)
        return None if obj is None else obj.state

    def _person_count(self, entity_id: str) -> int:
        raw = self._state(entity_id)
        if raw is None:
            return 0
        try:
            value = float(raw)
            # int() of an infinite float raises OverflowError, which is
            # deliberately not caught here (preserved behaviour).
            count = int(value)
        except (TypeError, ValueError):
            return 0
        return max(count, 0)

    def _flag(self, entity_id: str) -> bool:
        raw = self._state(entity_id)
        return raw is not None and str(raw).lower() in _TRUTHY_STATES

    def evaluate(self, area_id: str) -> dict:
        """Read the area's sensors now and return the fused assessment."""
        person_count = self._person_count(f"sensor.{area_id}_frigate_person_count")
        gaze = self._flag(f"binary_sensor.{area_id}_camera_gaze_detected")
        presence = self._flag(f"binary_sensor.{area_id}_mmwave_presence")

        score = 0.0
        if person_count > 0:
            score += self.PERSON_COUNT_WEIGHT
        if gaze:
            score += self.GAZE_WEIGHT
        if presence:
            score += self.PRESENCE_WEIGHT
        confidence = round(
            min(max(score, self.CONFIDENCE_MIN), self.CONFIDENCE_MAX), 2
        )
        skip_preamble = gaze and presence

        _LOGGER.debug(
            "Spatial context for %s: persons=%s gaze=%s presence=%s -> %.2f",
            area_id, person_count, gaze, presence, confidence,
        )
        return {
            "confidence": confidence,
            "skip_preamble": skip_preamble,
            "person_count": person_count,
            "gaze": gaze,
            "presence": presence,
        }
