"""The learned reasoning cache.

Remembers the provider's announce / stay-quiet verdicts under a coarse event
signature (domain, device class, category, normalised transition, home or
away, urgency). Repeated patterns can then be decided locally with no
provider call, and the Local Mind keeps case memory while the provider is
unreachable. The whole cache persists as one JSON file under Nova's folder
(nova/reasoning_cache.json). Session counters of provider calls against
local decisions feed the panel. One lock guards everything; save() never
raises.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from . import paths
from .persistence.files import CORRUPT, OK, read_json, write_json_atomic

_LOGGER = logging.getLogger(__name__)

# Tests point this at a temporary file; None means Nova's own folder.
CACHE_PATH: Optional[Path] = None

REFRESH_AGE = 14 * 86400   # an entry older than this is stale for get()
MAX_ENTRIES = 2000
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_BLANK_STATES = ("unknown", "unavailable", "none", "")

_cache: dict = {}
_loaded = False
_CLOUD_CALLS = 0
_CACHE_HITS = 0
_lock = Lock()   # not reentrant: never call a locking function while held


def _cache_path() -> Path:
    return CACHE_PATH or Path(paths.nova_path("reasoning_cache.json"))


def _norm_state(s) -> str:
    """Collapse a state to a coarse token: '?' for no value, '#' for numbers."""
    if s is None:
        return "?"
    text = str(s).strip().lower()
    if text in _BLANK_STATES:
        return "?"
    if _NUMERIC_RE.match(text):
        return "#"
    return text


def signature(domain, device_class, category, from_state, to_state,
              anyone_home, urgency="") -> str:
    return (f"{domain or '-'}|{device_class or '-'}|{category or '-'}|"
            f"{_norm_state(from_state)}->{_norm_state(to_state)}|"
            f"home={1 if anyone_home else 0}|u={urgency or '-'}")


def load() -> int:
    """Read the file once per process; later calls only report the size."""
    global _cache, _loaded
    if _loaded:
        return len(_cache)
    with _lock:
        if not _loaded:
            read = read_json(_cache_path())
            if read.status == CORRUPT:
                _LOGGER.warning("Reasoning cache file is corrupt, starting empty: %s",
                                read.error)
            if read.status == OK and isinstance(read.value, dict):
                _cache = read.value
            else:
                _cache = {}
            _loaded = True
            if read.status == OK:
                _LOGGER.info("Reasoning cache loaded: %d learned patterns", len(_cache))
        return len(_cache)


def save() -> None:
    """Trim to MAX_ENTRIES (oldest refresh first) and write the file."""
    try:
        with _lock:
            excess = len(_cache) - MAX_ENTRIES
            if excess > 0:
                by_age = sorted(_cache, key=lambda k: (_cache[k] or {}).get("refreshed", 0))
                for key in by_age[:excess]:
                    del _cache[key]
            write_json_atomic(_cache_path(), _cache)
    except Exception as exc:
        _LOGGER.debug("Reasoning cache save failed: %s", exc)


def get(sig: str, ignore_age: bool = False) -> Optional[dict]:
    if not _loaded:
        load()
    entry = _cache.get(sig)
    if not entry:
        return None
    if not ignore_age and time.time() - entry.get("refreshed", 0) > REFRESH_AGE:
        return None
    return dict(entry)


def similar(domain, device_class, category, anyone_home) -> tuple[int, int]:
    """(spoken, silent) tallies for past verdicts on this kind of event."""
    if not _loaded:
        load()
    prefix = f"{domain or '-'}|{device_class or '-'}|{category or '-'}|"
    token = f"|home={1 if anyone_home else 0}|"
    spoke = quiet = 0
    for sig, entry in list(_cache.items()):
        if not sig.startswith(prefix) or token not in sig:
            continue
        if (entry or {}).get("speak"):
            spoke += 1
        else:
            quiet += 1
    return (spoke, quiet)


def remember(sig: str, speak: bool, urgency: str) -> None:
    """Record a provider verdict (only the coordinator calls this)."""
    if not _loaded:
        load()
    now = time.time()
    with _lock:
        entry = _cache.get(sig)
        if entry:
            entry["speak"] = bool(speak)
            entry["urgency"] = urgency
            entry["refreshed"] = now
        else:
            _cache[sig] = {"speak": bool(speak), "urgency": urgency, "hits": 0,
                           "created": now, "refreshed": now}
    save()


def note_hit(sig: str) -> None:
    """Count a decision made without the provider."""
    global _CACHE_HITS
    with _lock:
        _CACHE_HITS += 1
        entry = _cache.get(sig)
        if entry is not None:
            entry["hits"] = entry.get("hits", 0) + 1


def note_cloud_call() -> None:
    global _CLOUD_CALLS
    _CLOUD_CALLS += 1


def stats() -> dict:
    """Pattern count and how often decisions stayed local this session."""
    if not _loaded:
        load()
    with _lock:
        cloud, local, patterns = _CLOUD_CALLS, _CACHE_HITS, len(_cache)
    asked = cloud + local
    rate = round(local * 100 / asked) if asked else 0
    return {"learned_patterns": patterns, "cloud_calls": cloud,
            "local_decisions": local, "local_rate": rate}


def reset() -> None:
    """Forget every learned pattern and zero the session counters."""
    global _cache, _CLOUD_CALLS, _CACHE_HITS
    with _lock:
        _cache, _CLOUD_CALLS, _CACHE_HITS = {}, 0, 0
    save()
