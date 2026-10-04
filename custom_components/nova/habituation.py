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

import asyncio
import datetime
import fnmatch
import logging
import threading
import time
from typing import Optional
from . import paths

_LOGGER = logging.getLogger(__name__)

STATE_FILE: Optional[str] = None  # override; None resolves via paths.py


def _state_file() -> str:
    return STATE_FILE or paths.nova_path("habituation.json")


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
            with open(_state_file(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _state = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            _LOGGER.debug("habituation: could not read %s: %s", _state_file(), exc)
    return _state


_save_lock = threading.Lock()
_save_gen = 0      # bumped for every save request
_written_gen = 0   # the newest request already on disk


def _write(snapshot: dict, gen: int) -> None:
    """Write one snapshot. Blocking. A snapshot older than the one already
    written is skipped, so writes that finish out of order never go back."""
    global _written_gen
    with _save_lock:
        if gen < _written_gen:
            return
        try:
            from .persistence.files import write_json_atomic
            write_json_atomic(_state_file(), snapshot, indent=2)
            _written_gen = gen
        except Exception as exc:
            _LOGGER.warning("habituation: could not save: %s", exc)


def _save() -> None:
    """Save the state. On the event loop the file write goes to the executor,
    so callers in async code never block the loop. Elsewhere it is written
    straight away."""
    global _save_gen
    _save_gen += 1
    gen = _save_gen
    snapshot = {k: dict(v) for k, v in _load().items()}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _write(snapshot, gen)
    else:
        loop.run_in_executor(None, _write, snapshot, gen)


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
