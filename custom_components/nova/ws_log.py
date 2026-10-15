"""The panel's debug log: two in memory ring buffers and a persistent file.

Kept apart from websocket.py so it can be read and tested without Home
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

import queue as _queue
import re as _re
import threading as _threading
from collections import deque as _deque
from datetime import datetime as _datetime
from pathlib import Path as _Path
from typing import Optional

from . import paths

# Categories that also go in the conversation buffer, so a burst of observer
# noise (many alarm zones at once, say) cannot push out the reply routing
# lines before diagnostics reads them.
_CONV_CATEGORIES = frozenset({"CONV", "LOCAL", "AGENT", "REPLY", "ROUTE", "OFFLINE", "ERROR"})
_DEBUG_LOG: _deque = _deque(maxlen=500)
_CONV_LOG: _deque = _deque(maxlen=80)
_LOG_FILE: Optional[_Path] = None   # set to use another file; None asks paths.py

_ROTATE_ABOVE = 2_000_000           # bytes
_KEEP_ON_ROTATE = 2000              # lines kept when the file is cut back
_READ_BACK = 200                    # lines read back at import
_LINE = _re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \[(\w+)\] (.+)")


def _log_file() -> _Path:
    return _LOG_FILE or _Path(paths.nova_path("nova.log"))


# The file is written by one daemon thread, never on the event loop. nova_log
# is called from loop callbacks (the classifier, the observer); file work
# there stalled the loop, and in an announcement storm that dropped the
# ESPHome satellites (mic timeouts, then a crash loop). Lines are queued and
# the thread opens, writes and rotates.
_LOG_QUEUE: "_queue.Queue[dict]" = _queue.Queue(maxsize=2000)
_WRITER_STARTED = False
_WRITER_LOCK = _threading.Lock()


def _append_to_file(entry: dict) -> None:
    path = _log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(f"{entry['date']} {entry['ts']} [{entry['cat']}] {entry['msg']}\n")
    if path.stat().st_size > _ROTATE_ABOVE:
        kept = path.read_text().splitlines()[-_KEEP_ON_ROTATE:]
        path.write_text("\n".join(kept) + "\n")


def _log_writer_loop() -> None:
    """The writer thread: take queued lines and append them, for ever."""
    while True:
        entry = _LOG_QUEUE.get()
        try:
            if entry is not None:
                _append_to_file(entry)
        except Exception:
            pass
        finally:
            _LOG_QUEUE.task_done()


def _ensure_writer() -> None:
    """Start the writer thread the first time it is needed."""
    global _WRITER_STARTED
    if _WRITER_STARTED:
        return
    with _WRITER_LOCK:
        if not _WRITER_STARTED:
            _threading.Thread(target=_log_writer_loop, name="nova-log-writer",
                              daemon=True).start()
            _WRITER_STARTED = True


def _persist_log_entry(entry: dict) -> None:
    """Hand a line to the writer. Never blocks: when the queue is full the
    file copy is dropped."""
    _ensure_writer()
    try:
        _LOG_QUEUE.put_nowait(entry)
    except _queue.Full:
        pass


def _remember(entry: dict) -> None:
    _DEBUG_LOG.append(entry)
    if entry["cat"] in _CONV_CATEGORIES:
        _CONV_LOG.append(entry)


def _load_persisted_log() -> None:
    """Fill the buffers from the end of the file, once, at import."""
    try:
        path = _log_file()
        if not path.exists():
            return
        for line in path.read_text().splitlines()[-_READ_BACK:]:
            found = _LINE.match(line)
            if found:
                date, ts, cat, msg = found.groups()
                _remember({"date": date, "ts": ts, "cat": cat, "msg": msg})
    except Exception:
        pass


_load_persisted_log()


def nova_log(category: str, message: str) -> None:
    """One line for the panel's Log tab and the log file."""
    now = _datetime.now()
    entry = {"date": now.strftime("%Y-%m-%d"), "ts": now.strftime("%H:%M:%S"),
             "cat": category, "msg": message[:500]}
    _remember(entry)
    _persist_log_entry(entry)


def _tail(buffer, n: int) -> list:
    entries = list(buffer)
    return entries[-n:] if n and n > 0 else entries


def recent_conversation_log(n: int = 80) -> list:
    """The last ``n`` conversation and reply routing lines, from their own
    buffer that noise cannot flush. In diagnostics, so a spoken reply problem
    can be traced even when the main log is flooded."""
    return _tail(_CONV_LOG, n)


def recent_debug_log(n: int = 150) -> list:
    """The last ``n`` lines of the main log, for the diagnostics download, so a
    reply problem can be traced from the file and not only the Log tab."""
    return _tail(_DEBUG_LOG, n)
