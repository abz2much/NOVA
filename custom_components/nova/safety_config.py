"""Strict helpers for settings that can cause physical device actions."""
from __future__ import annotations

import json
import re

LOCKDOWN_AUTO_KEY = "lockdown_auto_on_arm"


def automatic_lockdown_enabled(config: dict | None) -> bool:
    """Only the literal JSON boolean true enables automatic device control."""
    return isinstance(config, dict) and config.get(LOCKDOWN_AUTO_KEY) is True


def valid_panel_value(key: str, value) -> bool:
    """Reject truthy strings and numbers for the automatic safety opt in."""
    if key == LOCKDOWN_AUTO_KEY:
        return type(value) is bool
    if key == "notify_services":
        if not isinstance(value, str):
            return False
        try:
            services = json.loads(value)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(services, list)
            and all(isinstance(item, str)
                    and re.fullmatch(r"notify\.[a-z0-9_]+", item) is not None
                    for item in services)
        )
    return True
