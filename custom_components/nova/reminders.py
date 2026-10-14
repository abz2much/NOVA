"""Reminders the person sets ("remind me at 9 to put the bins out").

Reminders live in SQLite and survive restarts. A watcher looks for due
ones every 30 seconds. A due reminder waits out the household's quiet
hours (unless it opts out) and, when it needs someone home, waits until
someone is. It then goes to the one proactive delivery path, is marked
fired, and moves on to its next time if it repeats. Reminders can be
acknowledged ("got it, Nova") to self-silence.

The ``nova.add_reminder`` service handler parses the time, stores the
reminder and confirms it out loud.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import paths
from .persistence import sqlite as _store
from .presence import get_presence_summary
from .tts_helper import async_announce

_LOGGER = logging.getLogger(__name__)

# Tests point this at a temporary file; None means the usual location.
DB_PATH: Optional[Path] = None

_TICK = timedelta(seconds=30)
_STEPS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "hourly": timedelta(hours=1),
}

_INSERT = (
    "INSERT INTO reminders "
    "(created, label, trigger_at, repeat, require_home, respect_quiet) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_SELECT_DUE = (
    "SELECT * FROM reminders "
    "WHERE trigger_at <= ? AND acknowledged = 0 "
    "AND (last_fired IS NULL OR last_fired < trigger_at)"
)


# ── store ────────────────────────────────────────────────────────────────────

def _db_path():
    if DB_PATH:
        return DB_PATH
    return Path(paths.nova_path("reminders.db"))


def _connect():
    conn = _store.connect(_db_path(), busy_timeout_ms=None)
    _store.ensure(conn, "reminders")
    conn.commit()
    return conn


def _now_local() -> datetime:
    """Naive local time in Home Assistant's configured timezone."""
    return dt_util.as_local(dt_util.utcnow()).replace(tzinfo=None)


def add_reminder(label: str, trigger_at: datetime, repeat: Optional[str] = None,
                 require_home: bool = True, respect_quiet: bool = True) -> int:
    """Store one reminder; its id, or -1 when it could not be stored."""
    try:
        created = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        with _connect() as conn:
            cur = conn.execute(_INSERT, (
                created, label, trigger_at.isoformat(), repeat,
                1 if require_home else 0, 1 if respect_quiet else 0,
            ))
            return int(cur.lastrowid or 0)
    except Exception as err:
        _LOGGER.warning("Could not store reminder %r: %s", label, err)
        return -1


def get_due_reminders(now: Optional[datetime] = None) -> list[dict]:
    """Every reminder whose time has come and that has not fired since."""
    if now is None:
        now = _now_local()
    try:
        with _connect() as conn:
            return [dict(row) for row in conn.execute(_SELECT_DUE, (now.isoformat(),))]
    except Exception as err:
        _LOGGER.warning("Could not read due reminders: %s", err)
        return []


def mark_fired(reminder_id: int) -> None:
    try:
        with _connect() as conn:
            conn.execute("UPDATE reminders SET last_fired = ? WHERE id = ?",
                         (_now_local().isoformat(), reminder_id))
    except Exception as err:
        _LOGGER.debug("Could not mark reminder %s fired: %s", reminder_id, err)


def _advance_repeating(reminder: dict) -> None:
    """Move a repeating reminder to its first time after now, so one held
    back (quiet hours, nobody home) fires once rather than once per missed
    interval."""
    try:
        step = reminder.get("repeat")
        if not step:
            return
        previous = datetime.fromisoformat(reminder["trigger_at"])
        delta = _STEPS.get(step)
        if delta is None:
            return
        following = previous + delta
        now = _now_local() if previous.tzinfo is None else dt_util.utcnow()
        if following <= now:
            following += delta * ((now - following) // delta + 1)
        with _connect() as conn:
            conn.execute(
                "UPDATE reminders SET trigger_at = ?, last_fired = NULL WHERE id = ?",
                (following.isoformat(), reminder["id"]))
    except Exception as err:
        _LOGGER.debug("Could not advance reminder %s: %s", reminder.get("id"), err)


# ── watcher ──────────────────────────────────────────────────────────────────

def _reminder_text(honorific, label) -> str:
    from . import persona
    text = str(label).rstrip()
    if not text.endswith((".", "!", "?")):
        text += "."
    return persona.lead_in(honorific, f"reminder: {text}")


class ReminderWatcher:
    """Checks for due reminders on a 30-second timer and delivers them."""

    def __init__(self, hass: HomeAssistant, honorific_getter, tts_getter,
                 speakers_getter) -> None:
        self._unsub = None
        self._speakers_getter = speakers_getter
        self._tts_getter = tts_getter
        self._honorific_getter = honorific_getter
        self.hass = hass

    async def async_start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._check, _TICK)
        _LOGGER.info("Reminder watcher started")

    async def async_stop(self) -> None:
        unsub, self._unsub = self._unsub, None
        if unsub:
            unsub()

    @callback
    def _check(self, now) -> None:
        self.hass.async_create_task(self._check_async())

    async def _check_async(self) -> None:
        hass = self.hass
        due = await hass.async_add_executor_job(get_due_reminders)
        if not due:
            return
        honorific = self._honorific_getter()
        tts_entity = self._tts_getter()
        speakers = self._speakers_getter()
        presence = get_presence_summary(hass)

        from . import delivery

        for reminder in due:
            respect_quiet = reminder.get("respect_quiet")
            if respect_quiet and delivery.in_quiet_hours(hass):
                _LOGGER.debug("Reminder %s held for quiet hours", reminder.get("id"))
                continue
            if reminder.get("require_home") and not presence.get("anyone_home"):
                _LOGGER.debug("Reminder %s held until someone is home", reminder.get("id"))
                continue
            request = delivery.DeliveryRequest(
                source="reminder",
                message=_reminder_text(honorific, reminder["label"]),
                category="reminder",
                requested_by_user=True,
                quiet_ok=not respect_quiet,
                speakers=tuple(speakers or ()),
                tts_entity=tts_entity or "",
                tts_context="reminder",
            )
            await delivery.async_deliver(hass, request)
            await hass.async_add_executor_job(mark_fired, reminder["id"])
            await hass.async_add_executor_job(_advance_repeating, reminder)


# ── service ──────────────────────────────────────────────────────────────────

async def async_add_reminder_service(hass: HomeAssistant, call: ServiceCall,
                                     honorific: str, tts_entity: str | None,
                                     speakers: list[str]) -> dict:
    """The nova.add_reminder handler."""
    data = call.data
    label = data["label"]
    raw_time = data["trigger_at"]
    repeat = data.get("repeat")
    require_home = data.get("require_home", True)
    respect_quiet = data.get("respect_quiet", True)

    try:
        trigger_at = datetime.fromisoformat(raw_time)
    except ValueError:
        return {"success": False, "error": "invalid_datetime"}

    rid = await hass.async_add_executor_job(
        add_reminder, label, trigger_at, repeat, require_home, respect_quiet)
    if rid < 0:
        return {"success": False, "error": "db_error"}

    addr = f", {honorific}" if honorific else ""
    await async_announce(hass, f"Reminder saved{addr}. I'll let you know.",
                         tts_entity, speakers, context="reminder")
    return {"success": True, "reminder_id": rid}
