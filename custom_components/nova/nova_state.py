"""Nova's state as Home Assistant entities: what each one shows.

sensor.py and binary_sensor.py stay thin and read everything from here, so
the rules can be tested without Home Assistant. Nothing here keeps its own
copy of a state: each reader asks the module that owns it (output_gate,
sleep_detection, the lockdown manager, reminders, proactive_briefing, the
suggestion store). The one exception is the latest briefing, which nothing
kept before; it is saved to latest_briefing.json in Nova's data folder.

Modules that change something an entity shows call notify(), which sends
SIGNAL_STATE through Home Assistant's dispatcher from any thread. The
entities also listen to STATE_EVENTS on the bus. Only the energy note runs
on a timer.

No public function raises, except where noted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

SIGNAL_STATE = "nova_state_changed"
# Bus events that change what the entities show.
STATE_EVENTS = ("nova_observer_nap", "nova_observer_shushed", "nova_observer_unshushed")

STATUS_READY = "ready"
STATUS_QUIET = "quiet"
STATUS_NAPPING = "napping"
STATUS_LOCKDOWN = "lockdown"
STATUS_OBSERVER_OFF = "observer_off"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OPTIONS = [STATUS_READY, STATUS_QUIET, STATUS_NAPPING, STATUS_LOCKDOWN,
                  STATUS_OBSERVER_OFF]

BRIEFING_FILE = "latest_briefing.json"
MAX_STATE = 255
MAX_REMINDERS = 5
MAX_CAMERA = 5
MAX_SUGGESTIONS = 10
CAMERA_HOURS = 24
ENERGY_REFRESH = timedelta(minutes=30)
# Today within this share of yesterday reads as "about the same".
_SAME_SHARE = 0.05

# The Home Assistant the entities are set up in, so notify() can reach the
# dispatcher from modules that have no hass of their own.
_HASS: dict[str, Any] = {"hass": None}
_BRIEFING: dict[str, Optional[str]] = {"text": None, "time": None}


# ── Change signal ───────────────────────────────────────────────────────────

def attach(hass) -> None:
    """Start sending change signals to this Home Assistant."""
    _HASS["hass"] = hass


def detach(hass) -> None:
    """Stop sending change signals (the entities are unloading)."""
    if _HASS["hass"] is hass:
        _HASS["hass"] = None


def notify() -> None:
    """Tell the entities something changed. Safe from any thread; does
    nothing when no entities are set up."""
    hass = _HASS["hass"]
    if hass is None:
        return
    try:
        from homeassistant.helpers.dispatcher import dispatcher_send
        dispatcher_send(hass, SIGNAL_STATE)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("nova_state: change signal not sent: %s", exc)


# ── Status, quiet, nap and lockdown ─────────────────────────────────────────

def pick_status(*, loaded: bool, lockdown: bool, napping: bool, quiet: bool,
                observer_on: bool) -> str:
    """The one Status value, in order: unavailable, lockdown, napping,
    quiet, observer_off, ready."""
    if not loaded:
        return STATUS_UNAVAILABLE
    if lockdown:
        return STATUS_LOCKDOWN
    if napping:
        return STATUS_NAPPING
    if quiet:
        return STATUS_QUIET
    if not observer_on:
        return STATUS_OBSERVER_OFF
    return STATUS_READY


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def quiet() -> tuple[bool, dict]:
    """Whether any mute or the blanket shush is on, and the Quiet
    attributes. ``until`` is when every mute ends, None when one lasts until
    unshush or nothing is muted."""
    from . import output_gate
    gate = output_gate.status()
    attrs = {
        "mute_all": bool(gate["mute_all"]),
        "muted_categories": list(gate["muted_categories"]),
        "muted_entities": list(gate["muted_entities"]),
        "until": _iso(output_gate.quiet_until()),
    }
    on = attrs["mute_all"] or bool(attrs["muted_categories"] or attrs["muted_entities"])
    return on, attrs


def nap_until() -> Optional[datetime]:
    from . import sleep_detection
    return sleep_detection.nap_until()


def lockdown_active() -> bool:
    from . import core_lockdown_sync
    return bool(core_lockdown_sync.lockdown_status().get("active"))


def status(entry) -> str:
    """Status for a config entry, read live. The entry counts as loaded
    while it has its runtime, which it has from before its entities are set
    up until unload. Raises when a source fails."""
    from .runtime import lifecycle_runtime
    runtime = lifecycle_runtime(entry)
    if runtime is None:
        return STATUS_UNAVAILABLE
    return pick_status(loaded=True, lockdown=lockdown_active(),
                       napping=nap_until() is not None, quiet=quiet()[0],
                       observer_on=bool(runtime.observer_running))


# ── Announcements, reminders, cameras, suggestions ─────────────────────────

def last_announcement() -> tuple[Optional[datetime], Optional[str]]:
    """When Nova last spoke and what it said, or (None, None)."""
    from . import output_gate
    last = output_gate.last_spoken()
    if last is None:
        return None, None
    return datetime.fromtimestamp(last.timestamp, timezone.utc), last.message


def next_reminders() -> tuple[int, list[dict]]:
    """How many reminders are due in the next 24 hours, and the first few
    as {label, time, repeat}. Reads the database: run in the executor."""
    from . import reminders
    pending = reminders.pending_reminders(hours=24, limit=50, with_repeat=True)
    shown = [{"label": r["label"], "time": r["at"], "repeat": r.get("repeat")}
             for r in pending[:MAX_REMINDERS]]
    return len(pending), shown


def camera_summary() -> tuple[Optional[datetime], Optional[str], list[dict]]:
    """Time and text of the latest camera summary, and the last few
    summaries newest first as {camera, time, text}. Text is kept as plain
    text, exactly as stored."""
    from . import proactive_briefing
    snaps = proactive_briefing.get_recent_snapshots(CAMERA_HOURS)
    if not snaps:
        return None, None, []
    latest = snaps[-1]
    recent = [{"camera": s.camera_name,
               "time": datetime.fromtimestamp(s.timestamp, timezone.utc).isoformat(),
               "text": str(s.analysis)}
              for s in reversed(snaps[-MAX_CAMERA:])]
    return (datetime.fromtimestamp(latest.timestamp, timezone.utc),
            str(latest.analysis), recent)


def suggestion_items(names: Optional[dict] = None) -> tuple[int, list[dict]]:
    """How many suggestions are pending, and the first few as {id, text}.
    Reads the database: run in the executor."""
    from .automation.patterns import get_analyzer
    from .automation.api import panel_suggestion_items
    items = panel_suggestion_items(get_analyzer().get_pending_suggestions(), names)
    return len(items), [{"id": i.get("id"), "text": i.get("description", "")}
                        for i in items[:MAX_SUGGESTIONS]]


# ── Latest briefing ─────────────────────────────────────────────────────────

def _briefing_path() -> str:
    from . import paths
    return paths.nova_path(BRIEFING_FILE)


def latest_briefing() -> tuple[Optional[datetime], Optional[str]]:
    """When the latest briefing ran and its text, or (None, None)."""
    stamp = _BRIEFING.get("time")
    try:
        when = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        when = None
    return when, _BRIEFING.get("text")


def load_briefing() -> None:
    """Load the saved latest briefing. Blocking: run in the executor."""
    try:
        from .persistence.files import OK, read_json
        read = read_json(_briefing_path())
        if read.status == OK and isinstance(read.value, dict):
            text, stamp = read.value.get("text"), read.value.get("time")
            if isinstance(text, str) and isinstance(stamp, str):
                _BRIEFING.update(text=text, time=stamp)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("nova_state: saved briefing not loaded: %s", exc)


def _write_briefing(data: dict) -> None:
    from .persistence.files import write_json_atomic
    write_json_atomic(_briefing_path(), data, indent=2)


async def async_record_briefing(hass, text: str) -> None:
    """Keep this briefing as the latest (replacing the one before), save
    it, and tell the entities."""
    if not text:
        return
    _BRIEFING.update(text=text, time=datetime.now(timezone.utc).isoformat())
    try:
        await hass.async_add_executor_job(_write_briefing, dict(_BRIEFING))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("nova_state: could not save the latest briefing: %s", exc)
    notify()


# ── Energy note ─────────────────────────────────────────────────────────────

def hours_between(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour starts from ``start`` up to, not including, ``end``."""
    first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last = end.astimezone(timezone.utc)
    out = []
    while first < last:
        out.append(first)
        first += timedelta(hours=1)
    return out


def total_for(by_hour: dict[datetime, float], hours: list[datetime]) -> Optional[float]:
    """The sum over ``hours``, or None when there are none or one is missing."""
    if not hours or any(h not in by_hour for h in hours):
        return None
    return sum(by_hour[h] for h in hours)


def _compare(today: float, yesterday: float) -> str:
    if yesterday <= 0:
        return f"against {yesterday:.1f} kWh"
    share = (today - yesterday) / yesterday
    if abs(share) < _SAME_SHARE:
        return "about the same as"
    return f"{abs(share) * 100:.0f}% {'more' if share > 0 else 'less'} than"


def energy_sentence(today: Optional[float], yesterday: Optional[float],
                    solar_today: Optional[float] = None,
                    solar_yesterday: Optional[float] = None) -> Optional[str]:
    """One plain sentence comparing today so far with the same time
    yesterday, with solar when both solar figures are known. None without
    both usage figures. At most MAX_STATE characters."""
    if today is None or yesterday is None:
        return None
    text = (f"So far today the house has used {today:.1f} kWh, "
            f"{_compare(today, yesterday)} by this time yesterday.")
    if solar_today is not None and solar_yesterday is not None:
        text += (f" Solar has made {solar_today:.1f} kWh, "
                 f"{_compare(solar_today, solar_yesterday)} yesterday.")
    return text[:MAX_STATE]


def _solar_hours(series: list[dict]) -> dict[datetime, float]:
    """Solar kWh per hour where every solar sensor has a reading."""
    if not series:
        return {}
    common = set.intersection(*(set(s) for s in series))
    return {h: sum(s[h] for s in series) for h in common}


async def async_energy_note(hass) -> Optional[str]:
    """The energy sentence from the Energy dashboard's sources and the
    recorder's hourly statistics, the data the energy outlook reads. None
    when the house has no sources or a whole hour is missing on either day."""
    from . import energy_outlook as eo
    from .solar import _grid_sensors, _read_prefs

    prefs = await _read_prefs(hass)
    sources = (prefs or {}).get("energy_sources") or []
    kinds = eo.source_kinds(
        [_grid_sensors(s) for s in sources if s.get("type") == "grid"],
        [_grid_sensors(s) for s in sources if s.get("type") == "battery"],
        [s.get("stat_energy_from") for s in sources
         if s.get("type") == "solar" and s.get("stat_energy_from")])
    ids = sorted({e for found in kinds.values() for e in found})
    if not ids:
        return None
    now, tz = eo._now(hass)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    yesterday = (midnight - timedelta(hours=12)).replace(hour=0)
    changes = await eo._hourly_changes(hass, ids, yesterday, hour_now)
    series = {k: [changes.get(e, {}) for e in found] for k, found in kinds.items()}
    home = eo.home_hours(series)
    today_hours = hours_between(midnight, hour_now)
    then_hours = hours_between(yesterday, yesterday + (hour_now - midnight))
    solar = _solar_hours(series["solar"]) if kinds["solar"] else {}
    return energy_sentence(
        total_for(home, today_hours), total_for(home, then_hours),
        total_for(solar, today_hours) if kinds["solar"] else None,
        total_for(solar, then_hours) if kinds["solar"] else None)


# ── Admin only services ─────────────────────────────────────────────────────

async def async_check_admin(hass, call) -> None:
    """Raise Unauthorized when a person who is not an admin made the call.
    A call with no user (an automation or script) is allowed, as Home
    Assistant's own admin services allow it."""
    from homeassistant.exceptions import Unauthorized, UnknownUser
    user_id = getattr(getattr(call, "context", None), "user_id", None)
    if user_id is None:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context, user_id=user_id)
    if not user.is_admin:
        raise Unauthorized(context=call.context, user_id=user_id)
