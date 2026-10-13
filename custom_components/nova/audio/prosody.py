"""Pick how Nova should sound for one announcement from room telemetry.

Pure: reads only the telemetry dict and the clock. Standard library only so
the file can be loaded on its own.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

DARK_LUX = 5.0
QUIET_DB = 45.0
LOUD_DB = 60.0
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 7


@dataclass(frozen=True)
class VocalProfile:
    volume: float
    speech_rate: float
    style: str
    whisper_mode: bool
    duck_media: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _number(value: Any) -> Optional[float]:
    """A finite float, or None when the value can't be read as one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


class ProsodyController:
    def __init__(self, quiet_hours_start: int = DEFAULT_QUIET_START,
                 quiet_hours_end: int = DEFAULT_QUIET_END, *,
                 dark_lux: float = DARK_LUX, quiet_db: float = QUIET_DB,
                 loud_db: float = LOUD_DB,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.quiet_hours_start = int(quiet_hours_start) % 24
        self.quiet_hours_end = int(quiet_hours_end) % 24
        self.dark_lux = dark_lux
        self.quiet_db = quiet_db
        self.loud_db = loud_db
        self._clock: Callable[[], datetime] = clock or datetime.now

    def in_quiet_hours(self, hour: int) -> bool:
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _hour(self, telemetry: dict) -> int:
        hour = telemetry.get("hour")
        if isinstance(hour, int) and 0 <= hour <= 23:
            return hour
        return self._clock().hour

    def calculate_vocal_profile(self, telemetry: dict) -> dict:
        telemetry = telemetry or {}
        critical = bool(telemetry.get("critical_alert", False))
        lux = _number(telemetry.get("ambient_lux"))
        db = _number(telemetry.get("ambient_db"))
        if "media_active" in telemetry:
            media = bool(telemetry["media_active"])
        else:
            media = bool(telemetry.get("media_playing", False))
        skip_preamble = bool(telemetry.get("skip_preamble", False))

        if critical:
            profile = VocalProfile(1.0, 1.15, "authoritative", False, True)
        else:
            quiet = self.in_quiet_hours(self._hour(telemetry))
            dark = lux is not None and lux < self.dark_lux
            quiet_room = db is not None and db < self.quiet_db
            loud_room = db is not None and db >= self.loud_db
            if quiet and dark and quiet_room:
                profile = VocalProfile(0.25, 0.95, "whisper", True, False)
            elif quiet or (dark and quiet_room):
                profile = VocalProfile(0.4, 0.97, "subdued", False, media)
            elif loud_room or media:
                profile = VocalProfile(0.9, 1.0, "projected", False, True)
            else:
                profile = VocalProfile(0.6, 1.0, "neutral", False, False)

        out = profile.as_dict()
        if skip_preamble:
            out["speech_rate"] = round(max(0.5, out["speech_rate"] - 0.05), 2)
        return out
