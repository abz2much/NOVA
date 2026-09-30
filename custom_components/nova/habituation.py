"""Nova — go quiet about notifications that are normal for this home.

When Nova gives the same notification on three days in a row, it is part of
how this home runs (a nightly backup, a light that switches itself), not news.
The third one carries a short line saying Nova will stop mentioning it, and
from then on that notification stays quiet. A missed day starts the count
again. Emergencies never go quiet: critical urgency (smoke, CO, gas, leaks,
glass break), break-in and lockdown alerts, freeze alerts, locks and alarm
panels.

A notification is identified by a key the caller chooses (entity and
category for observer alerts, the pattern key for anticipation). The user
brings one back by asking Nova to stop ignoring it: cognitive_core.unignore
calls forget(), so the existing ignore / unignore tools cover this too.

State is a small JSON file, read once and written only when a key's day
changes, so at most once per key per day.
"""
from __future__ import annotations

import datetime
import fnmatch
import logging
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

STATE_FILE = "/config/nova/habituation.json"
QUIET_AFTER_DAYS = 3
QUIET_NOTE = "This has come up three days running, so I'll stop mentioning it."

_EXEMPT_TYPE_PREFIXES = ("intrusion", "lockdown", "freeze")
_EXEMPT_DOMAINS = ("lock", "alarm_control_panel")

_state: Optional[dict] = None


def _day(now: float) -> int:
    return datetime.datetime.fromtimestamp(now).toordinal()


def _load() -> dict:
    global _state
    if _state is None:
        _state = {}
        try:
            import json
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _state = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            _LOGGER.debug("habituation: could not read %s: %s", STATE_FILE, exc)
    return _state


def _save() -> None:
    try:
        from .persistence.files import write_json_atomic
        write_json_atomic(STATE_FILE, _load(), indent=2)
    except Exception as exc:
        _LOGGER.warning("habituation: could not save: %s", exc)


def exempt(*, urgency: str = "", kind: str = "", entity_id: str = "") -> bool:
    """True for emergencies, which always get through."""
    if str(urgency or "").lower() == "critical":
        return True
    if str(kind or "").lower().startswith(_EXEMPT_TYPE_PREFIXES):
        return True
    return str(entity_id or "").split(".", 1)[0] in _EXEMPT_DOMAINS


def is_quiet(key: str) -> bool:
    """Whether this notification has gone quiet."""
    return bool(key) and bool(_load().get(key, {}).get("quiet"))


def will_go_quiet(key: str, now: Optional[float] = None) -> bool:
    """Whether delivering this notification now makes it the last one: it
    was also given on each of the previous QUIET_AFTER_DAYS - 1 days."""
    if not key:
        return False
    rec = _load().get(key)
    if not rec or rec.get("quiet"):
        return False
    today = _day(now or time.time())
    last = int(rec.get("last_day") or 0)
    streak = int(rec.get("streak") or 0)
    return last == today - 1 and streak + 1 >= QUIET_AFTER_DAYS


def with_note(key: str, message: str, now: Optional[float] = None) -> str:
    """The message, with the going-quiet line added when this is the last one."""
    if will_go_quiet(key, now):
        return f"{message.rstrip()} {QUIET_NOTE}"
    return message


def record(key: str, entity_id: str = "", now: Optional[float] = None) -> None:
    """Count a delivered notification. Repeats on the same day count once."""
    if not key:
        return
    now = now or time.time()
    today = _day(now)
    state = _load()
    rec = state.get(key) or {}
    if rec.get("quiet"):
        return
    last = int(rec.get("last_day") or 0)
    if last == today:
        return
    streak = int(rec.get("streak") or 0) + 1 if last == today - 1 else 1
    rec = {"entity_id": entity_id or rec.get("entity_id", ""),
           "last_day": today, "streak": streak}
    if streak >= QUIET_AFTER_DAYS:
        rec["quiet"] = True
        rec["quiet_since"] = now
        _LOGGER.info("habituation: %s came up %d days running, now quiet", key, streak)
    state[key] = rec
    _save()


def forget(pattern: str) -> list[str]:
    """Bring back the notifications whose entity or key matches `pattern`
    (an entity_id or glob). Returns the keys cleared."""
    pattern = str(pattern or "").strip()
    if not pattern:
        return []
    state = _load()
    cleared = [k for k, r in state.items()
               if fnmatch.fnmatch(str(r.get("entity_id") or ""), pattern)
               or fnmatch.fnmatch(k, pattern) or pattern in k]
    for k in cleared:
        del state[k]
    if cleared:
        _save()
    return cleared


def quiet_list() -> list[dict]:
    """The notifications that have gone quiet, for the agent's context."""
    return [{"key": k, "entity_id": r.get("entity_id", ""),
             "quiet_since": r.get("quiet_since", 0)}
            for k, r in _load().items() if r.get("quiet")]
