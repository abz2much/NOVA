"""Turn a Home Assistant recorder time into a POSIX epoch."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def recorder_epoch(value: Any) -> Optional[float]:
    """Epoch seconds for one recorder history time, or None.

    ``get_significant_states(minimal_response=True)`` returns a native
    ``State`` (``datetime``) for an entity's first entry and plain dicts with an
    ISO 8601 UTC string for the rest; the compressed format uses a float epoch.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return None
