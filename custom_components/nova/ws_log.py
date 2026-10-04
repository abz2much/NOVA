"""The panel's debug log: two in memory ring buffers and a persistent file.

Moved verbatim out of websocket.py so it can be read and tested without Home
Assistant. This module must import nothing from Home Assistant.

* _DEBUG_LOG keeps the last 500 entries of every category; _CONV_LOG keeps the
  last 80 conversation and reply routing entries, so noise cannot evict them.
* nova_log() appends to the buffers and queues the line for a daemon thread,
  which appends it to <config>/nova/nova.log (rotated above 2 MB). Nothing
  on the event loop touches the disk.
* The persisted file is read back once, when this module is first imported,
  which happens when Home Assistant imports Nova (before paths.configure).

websocket.py re-exports nova_log, recent_debug_log and recent_conversation_log;
other modules import them from there.
"""
from __future__ import annotations

from collections import deque as _deque
from datetime import datetime as _datetime
from pathlib import Path as _Path
import threading as _threading
import queue as _queue
from typing import Optional

from . import paths

_DEBUG_LOG: _deque = _deque(maxlen=500)
# A dedicated buffer for conversation + reply-routing entries only, so a burst of
# observer/anomaly noise (e.g. many alarm-zone escalations at once) can't evict
# the reply-delivery decisions before they're read from diagnostics.
_CONV_CATEGORIES = frozenset({"CONV", "LOCAL", "AGENT", "REPLY", "ROUTE", "OFFLINE", "ERROR"})
_CONV_LOG: _deque = _deque(maxlen=80)
_LOG_FILE: Optional[_Path] = None  # override; None resolves via paths.py


def _log_file() -> _Path:
    return _LOG_FILE or _Path(paths.nova_path("nova.log"))


# Persistent-log writes happen on a dedicated daemon thread, never on the
# event loop. nova_log() is called synchronously from event-loop callbacks
# (the classifier, observer, etc.) — doing file I/O there blocks the loop, and
# under an announcement/classify storm that stall degrades the ESPHome
# satellite connections (mic ESP_ERR_TIMEOUT → crash-loop). Enqueue instead;
# the writer thread does the blocking open()/write()/rotate() off-loop.
_LOG_QUEUE: "_queue.Queue[dict]" = _queue.Queue(maxsize=2000)
_WRITER_STARTED = False
_WRITER_LOCK = _threading.Lock()


def _log_writer_loop() -> None:
    """Drain the log queue and write to disk. Runs on a daemon thread."""
    while True:
        entry = _LOG_QUEUE.get()
        try:
            if entry is None:
                continue
            _log_file().parent.mkdir(parents=True, exist_ok=True)
            with open(_log_file(), "a") as f:
                f.write(f"{entry['date']} {entry['ts']} [{entry['cat']}] {entry['msg']}\n")
            # Rotate if file gets too large (>2MB)
            if _log_file().stat().st_size > 2_000_000:
                lines = _log_file().read_text().splitlines()
                _log_file().write_text("\n".join(lines[-2000:]) + "\n")
        except Exception:
            pass
        finally:
            _LOG_QUEUE.task_done()


def _ensure_writer() -> None:
    """Start the background writer thread once, lazily."""
    global _WRITER_STARTED
    if _WRITER_STARTED:
        return
    with _WRITER_LOCK:
        if _WRITER_STARTED:
            return
        t = _threading.Thread(
            target=_log_writer_loop, name="nova-log-writer", daemon=True,
        )
        t.start()
        _WRITER_STARTED = True


def _persist_log_entry(entry: dict) -> None:
    """Queue a log entry for the background writer (never blocks the caller)."""
    _ensure_writer()
    try:
        _LOG_QUEUE.put_nowait(entry)
    except _queue.Full:
        pass  # under extreme load, drop the persisted copy rather than block


def _load_persisted_log() -> None:
    """Load recent entries from persistent log on startup."""
    try:
        if _log_file().exists():
            import re
            lines = _log_file().read_text().splitlines()[-200:]
            for line in lines:
                m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \[(\w+)\] (.+)", line)
                if m:
                    _entry = {
                        "date": m.group(1),
                        "ts": m.group(2),
                        "cat": m.group(3),
                        "msg": m.group(4),
                    }
                    _DEBUG_LOG.append(_entry)
                    if m.group(3) in _CONV_CATEGORIES:
                        _CONV_LOG.append(_entry)
    except Exception:
        pass


# Load on import
_load_persisted_log()


def nova_log(category: str, message: str) -> None:
    """Add to Nova debug log (visible in panel Log tab + persistent file)."""
    now = _datetime.now()
    entry = {
        "date": now.strftime("%Y-%m-%d"),
        "ts": now.strftime("%H:%M:%S"),
        "cat": category,
        "msg": message[:500],
    }
    _DEBUG_LOG.append(entry)
    if category in _CONV_CATEGORIES:
        _CONV_LOG.append(entry)
    _persist_log_entry(entry)


def recent_conversation_log(n: int = 80) -> list:
    """Last ``n`` conversation/reply-routing entries, from a dedicated buffer that
    observer/anomaly noise cannot evict. Included in diagnostics so a spoken-reply
    delivery problem is diagnosable even when the main log is flooded."""
    entries = list(_CONV_LOG)
    return entries[-n:] if n and n > 0 else entries


def recent_debug_log(n: int = 150) -> list:
    """Return the last ``n`` debug-log entries (used by the diagnostics export so
    a spoken-reply issue is diagnosable from the downloaded file, not just the
    live Logs tab)."""
    entries = list(_DEBUG_LOG)
    return entries[-n:] if n and n > 0 else entries
