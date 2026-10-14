"""
Nova — the output gate.

Everything Nova says on its own initiative passes through here last. The gate
holds back a message when:

* the blanket shush is on, or its entity or category has been muted;
* the thing has come up days running and is now normal for the home
  (habituation.py), unless it is exempt;
* the hourly cap of spoken announcements is reached (high urgency is let
  through; the optional adaptive budget can tighten the cap);
* the same message, or one with the same repeat key, went out recently.

Critical urgency skips every one of these, so no shush can silence a safety
alert. Mutes are saved to output_mutes.json in Nova's data folder and loaded
once at setup; the history used for the cap, repeats and status lives in
memory only. Each announcement is also written to the activity log.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

MUTES_FILE = "output_mutes.json"
DEFAULT_MAX_PER_HOUR = 6
DEFAULT_DEDUP_MINUTES = 10
_HOUR = 3600
_BUDGET_TTL = 60.0

_LOGGER = logging.getLogger(__name__)


@dataclass
class Announcement:
    """One thing Nova said, or would have said had the gate let it."""
    timestamp: float
    entity_id: str
    category: str
    urgency: str
    message: str
    was_spoken: bool = True


@dataclass
class GateState:
    """The gate's memory; one per Home Assistant process."""
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    muted_entities: set[str] = field(default_factory=set)
    muted_categories: set[str] = field(default_factory=set)
    recent_messages: deque = field(default_factory=lambda: deque(maxlen=20))
    mute_all: bool = False


_STATE = GateState()
_SAVE_LOCK = asyncio.Lock()

# The adaptive budget's last answer and when it was read, so the decision
# record is consulted at most once a minute.
_BUDGET_CACHE = {"ts": 0.0, "mult": 1.0}


def _now() -> float:
    return time.time()


def _recent_within(history: deque, seconds: float) -> list[Announcement]:
    """Spoken announcements from the last ``seconds``."""
    since = _now() - seconds
    return [ann for ann in history if ann.was_spoken and ann.timestamp >= since]


def _budget_multiplier() -> float:
    """How far the hourly cap shrinks (at most 1.0) when the adaptive
    interruption budget is on: recent dismissed or false alarms make Nova
    interrupt less. 1.0 when it is off or anything fails."""
    try:
        from . import nova_config
        enabled = nova_config.get("adaptive_interruption_budget", False)
    except Exception:  # noqa: BLE001
        return 1.0
    if not enabled:
        return 1.0
    now = _now()
    if now - _BUDGET_CACHE["ts"] < _BUDGET_TTL:
        return _BUDGET_CACHE["mult"]
    try:
        from . import decision_record
        budget = decision_record.interruption_budget()
        multiplier = float(budget.get("multiplier", 1.0))
    except Exception:  # noqa: BLE001
        multiplier = 1.0
    _BUDGET_CACHE["ts"], _BUDGET_CACHE["mult"] = now, multiplier
    return multiplier


def _habit_key(entity_id: str, habit_key: Optional[str]) -> str:
    """The key habituation counts days under: the caller's own when given
    ("" meaning the caller counts its own), otherwise the entity."""
    if habit_key is None:
        return entity_id
    return habit_key


def _habituated(key: str, urgency: str, category: str, entity_id: str = "") -> bool:
    if not key:
        return False
    try:
        from . import habituation
        if habituation.exempt(urgency=urgency, kind=category, entity_id=entity_id or key):
            return False
        return bool(habituation.is_quiet(key))
    except Exception:  # noqa: BLE001
        return False


def _is_repeat(message: str, dedup_key: str, window_minutes: int) -> Optional[str]:
    since = _now() - window_minutes * 60
    for said in _STATE.recent_messages:
        if said["timestamp"] < since:
            continue
        if dedup_key and said.get("key") == dedup_key:
            return "repeat of a recent message"
        if _messages_similar(said["message"], message):
            return "duplicate of recent message"
    return None


def can_announce(
    *, entity_id: str, category: str, urgency: str, message: str,
    max_per_hour: int = DEFAULT_MAX_PER_HOUR,
    dedup_minutes: int = DEFAULT_DEDUP_MINUTES,
    dedup_key: str = "", habit_key: Optional[str] = None,
) -> tuple[bool, str]:
    """Whether this announcement may go out now, and why.

    Critical urgency always passes. Otherwise, in order: the blanket shush,
    entity and category mutes, habituation, the hourly cap (not applied to
    high urgency) and repeats within ``dedup_minutes``, where a matching
    ``dedup_key`` counts as the same message whatever its words."""
    if urgency == "critical":
        return True, "critical bypass"
    if _STATE.mute_all:
        return False, "blanket shush active"
    if entity_id in _STATE.muted_entities:
        return False, f"entity {entity_id} is muted"
    if category in _STATE.muted_categories:
        return False, f"category {category} is muted"
    if _habituated(_habit_key(entity_id, habit_key), urgency, category, entity_id):
        return False, f"{entity_id} is normal for this home (came up days running)"

    cap = max(1, int(round(max_per_hour * _budget_multiplier())))
    spoken = len(_recent_within(_STATE.history, _HOUR))
    if urgency != "high" and spoken >= cap:
        return False, f"rate limit ({spoken}/{cap}/hour)"

    reason = _is_repeat(message, dedup_key, dedup_minutes)
    if reason:
        return False, reason
    return True, "ok"


def habit_note(*, entity_id: str, category: str, urgency: str, message: str,
               habit_key: Optional[str] = None) -> str:
    """``message`` plus, on the third day running, a closing line saying Nova
    will stop mentioning it (habituation.py). Exempt messages are unchanged."""
    try:
        from . import habituation
        if habituation.exempt(urgency=urgency, kind=category, entity_id=entity_id):
            return message
        key = _habit_key(entity_id, habit_key)
        if not key:
            return message
        return habituation.with_note(key, message)
    except Exception:  # noqa: BLE001
        return message


def record_announcement(
    *, entity_id: str, category: str, urgency: str, message: str, was_spoken: bool,
    dedup_key: str = "", habit_key: Optional[str] = None, source: str = "observer",
    pushed: bool = False, outcome: str = "", request_id: str = "",
) -> None:
    """Remember an announcement for the cap, repeats and status, count its day
    for habituation when spoken, and add a row (with the delivery outcome) to
    the activity log. Never raises."""
    stamp = _now()
    _STATE.history.append(Announcement(
        timestamp=stamp, entity_id=entity_id, category=category, urgency=urgency,
        message=message, was_spoken=was_spoken))
    if was_spoken:
        _STATE.recent_messages.append(
            {"timestamp": _now(), "message": message, "key": dedup_key or ""})
        _count_habit_day(entity_id, category, urgency, habit_key)
    try:
        from .database import save_activity
        save_activity(entity_id=entity_id, category=category, urgency=urgency,
                      message=message, was_spoken=was_spoken, source=source,
                      pushed=pushed, outcome=outcome, request_id=request_id)
    except Exception:  # noqa: BLE001 - the activity log is best effort
        pass


def _count_habit_day(entity_id: str, category: str, urgency: str,
                     habit_key: Optional[str]) -> None:
    try:
        from . import habituation
        key = _habit_key(entity_id, habit_key)
        exempt = habituation.exempt(urgency=urgency, kind=category, entity_id=entity_id)
        if key and not exempt:
            habituation.record(key, entity_id)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("habituation record failed: %s", exc)


def shush(entity_id: Optional[str] = None, category: Optional[str] = None,
          all: bool = False) -> dict:
    """Mute an entity, a category, or (``all``) every non critical
    announcement until unshush. With no arguments, mutes the entity of the
    last spoken announcement."""
    result: dict = {"muted_entities": [], "muted_categories": [], "all": False}
    if all:
        _STATE.mute_all = True
        result["all"] = True
        _LOGGER.warning("Nova blanket shush on: non critical announcements are held "
                        "until nova.unshush (kept over a restart); critical ones pass")
        return result
    if entity_id is None and category is None:
        last = next((ann for ann in reversed(_STATE.history) if ann.was_spoken), None)
        if last is not None:
            entity_id = last.entity_id
            _LOGGER.info("Shushing the last announcement: %s (%s)", entity_id, last.category)
    for name, muted, listed in ((entity_id, _STATE.muted_entities, "muted_entities"),
                                (category, _STATE.muted_categories, "muted_categories")):
        if name:
            muted.add(name)
            result[listed].append(name)
    return result


def unshush(entity_id: Optional[str] = None, category: Optional[str] = None) -> dict:
    """Lift one mute, or with no arguments every mute and the blanket shush."""
    if entity_id is None and category is None:
        summary = {
            "cleared_entities": list(_STATE.muted_entities),
            "cleared_categories": list(_STATE.muted_categories),
            "blanket_cleared": _STATE.mute_all,
        }
        _STATE.muted_entities.clear()
        _STATE.muted_categories.clear()
        _STATE.mute_all = False
        return summary
    result: dict = {"cleared_entities": [], "cleared_categories": []}
    if entity_id in _STATE.muted_entities:
        _STATE.muted_entities.remove(entity_id)
        result["cleared_entities"].append(entity_id)
    if category in _STATE.muted_categories:
        _STATE.muted_categories.remove(category)
        result["cleared_categories"].append(category)
    return result


# ─── Saved mutes ─────────────────────────────────────────────────────────────

def _mutes_path() -> str:
    from . import paths
    return paths.nova_path(MUTES_FILE)


def _mutes_snapshot() -> dict:
    return {"entities": sorted(_STATE.muted_entities),
            "categories": sorted(_STATE.muted_categories),
            "all": bool(_STATE.mute_all)}


def _names(value) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def load_mutes() -> None:
    """Load the saved mutes. Called once at setup, after paths.configure(),
    never on import. A missing, empty or unreadable file leaves the gate as
    it is; never raises."""
    try:
        from .persistence.files import MISSING, OK, read_json
        read = read_json(_mutes_path())
        if read.status == MISSING:
            _LOGGER.debug("output gate: no saved mutes")
            return
        saved = read.value
        if read.status != OK or not isinstance(saved, dict):
            _LOGGER.debug("output gate: saved mutes unreadable (%s), ignored",
                          read.error or "not an object")
            return
        _STATE.muted_entities = _names(saved.get("entities"))
        _STATE.muted_categories = _names(saved.get("categories"))
        _STATE.mute_all = saved.get("all") is True
        _LOGGER.debug("output gate: %d entity and %d category mutes loaded, blanket %s",
                      len(_STATE.muted_entities), len(_STATE.muted_categories),
                      _STATE.mute_all)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("output gate: saved mutes not loaded: %s", exc)


def _write_mutes(data: dict) -> None:
    from .persistence.files import write_json_atomic
    write_json_atomic(_mutes_path(), data, indent=2)


async def async_save_mutes(hass) -> None:
    """Save the mutes off the event loop. The snapshot is taken inside the
    lock, just before writing, so overlapping saves always end with the
    newest state on disk. Never raises."""
    try:
        async with _SAVE_LOCK:
            await hass.async_add_executor_job(_write_mutes, _mutes_snapshot())
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("output gate: could not save mutes: %s", exc)


# ─── Reporting ───────────────────────────────────────────────────────────────

def status() -> dict:
    """The gate's state for the nova.observer_status service."""
    since = _now() - _HOUR
    last_hour = [ann for ann in _STATE.history if ann.timestamp >= since]
    spoken = [ann for ann in last_hour if ann.was_spoken]
    held = [ann for ann in last_hour if not ann.was_spoken]
    latest = spoken[-1] if spoken else None
    return {
        "announcements_last_hour": len(spoken),
        "suppressed_last_hour": len(held),
        "muted_entities": sorted(_STATE.muted_entities),
        "muted_categories": sorted(_STATE.muted_categories),
        "mute_all": bool(_STATE.mute_all),
        "last_announcement": None if latest is None else {
            "message": latest.message,
            "seconds_ago": int(_now() - latest.timestamp),
        },
    }


def recent_announcements(n: int = 5) -> list[str]:
    """The last ``n`` spoken messages, oldest first."""
    return [ann.message for ann in _STATE.history if ann.was_spoken][-n:]


def _messages_similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """Whether two messages share at least ``threshold`` of the words of the
    shorter one, counting only words longer than three letters (trailing
    punctuation dropped, case ignored)."""
    def words(text: str) -> set[str]:
        return {word.lower().strip(".,!?:;") for word in text.split() if len(word) > 3}

    first, second = words(a), words(b)
    if not first or not second:
        return False
    shared = len(first & second)
    return shared / min(len(first), len(second)) >= threshold
