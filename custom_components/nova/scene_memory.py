"""Nova scene memory (opt in): what the cameras described, kept over time.

Each analysed camera frame already produces a plain language description. This
module keeps those descriptions, with a short list of the things they mention,
so Nova can answer questions that need history:

    "where did I last see my keys?"      -> where_last_seen("keys")
    "what changed on the porch today?"   -> what_changed("porch", since)

Privacy and bounds:

  * Off unless ``scene_memory_enabled`` is true.
  * Text only. No images, faces or embeddings are stored.
  * Old rows are dropped after ``scene_memory_retention_days`` (default 14) and
    each camera keeps at most ``MAX_ROWS_PER_CAMERA`` rows.
  * ``forget_all`` wipes everything.

Matching is deliberately plain. A description is split into clauses, clauses
that deny something ("no package visible", "the porch is empty") are ignored,
and what is left is reduced to simple singular words. A search must match one
of those words, never loose text, so a denial can never count as a sighting.

All database functions are blocking. Call them from the executor.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

_DEFAULT_DB = "/config/nova/scene_memory.db"

ENABLED_KEY = "scene_memory_enabled"
RETENTION_KEY = "scene_memory_retention_days"
DEFAULT_RETENTION_DAYS = 14
RETENTION_RANGE = (1, 90)
MAX_ROWS_PER_CAMERA = 300
MAX_DESCRIPTION_CHARS = 600

_STOPWORDS = frozenset((
    "the a an is are was were of to in on at for and or with no not there here "
    "this that it its as by from into over near up down appears appear seems seem "
    "shows shown visible seen image frame camera scene view picture photo shot "
    "looks look some any left right front back side two three four five several "
    "few many one other another while also just very quite currently now still "
    "can could may might would should will being been have has had does do did "
    "which who what when where than then them they their these those such each "
    "both either neither more most much little small large big").split())

_NEGATIONS = re.compile(
    r"\b(no|not|none|nothing|nobody|without|empty|absent|neither|nor|never|"
    r"cannot|can't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|"
    r"unable|missing|gone|removed)\b", re.I)
_CLAUSES = re.compile(r"[.;:,!?\n]+|\b(?:but|however|although|though)\b", re.I)
_TOKEN = re.compile(r"[a-z][a-z0-9\-]{2,}")


def enabled() -> bool:
    try:
        from . import nova_config
        return nova_config.get(ENABLED_KEY, False) is True
    except Exception:
        return False


def retention_days() -> int:
    lo, hi = RETENTION_RANGE
    try:
        from . import nova_config
        value = nova_config.get(RETENTION_KEY, DEFAULT_RETENTION_DAYS)
        if type(value) is int and lo <= value <= hi:
            return value
    except Exception:
        pass
    return DEFAULT_RETENTION_DAYS


def _resolve(db_path: Optional[str]) -> str:
    return db_path or _DEFAULT_DB


def _connect(db_path: str):
    conn = _store.connect(db_path, wal=True)
    _store.ensure(conn, "scene_memory")
    return conn


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def normalise_term(term: str) -> list[str]:
    """The words a search term is reduced to, the same way stored text is."""
    out: list[str] = []
    for tok in _TOKEN.findall(str(term or "").lower()):
        if tok in _STOPWORDS:
            continue
        w = _singular(tok)
        if w not in out:
            out.append(w)
    return out


def extract_objects(text: str) -> list[str]:
    """Singular content words from the parts of a description that do not deny
    anything. Order kept, duplicates removed."""
    out: list[str] = []
    for clause in _CLAUSES.split(str(text or "")):
        if not clause or _NEGATIONS.search(clause):
            continue
        for tok in _TOKEN.findall(clause.lower()):
            if tok in _STOPWORDS:
                continue
            w = _singular(tok)
            if w not in out:
                out.append(w)
    return out


def _pack(objects: list[str]) -> str:
    return "\n" + "\n".join(objects) + "\n" if objects else ""


def _unpack(packed: str) -> list[str]:
    return [o for o in str(packed or "").split("\n") if o]


def _row(row) -> dict:
    return {
        "id": row["id"], "camera": row["camera"], "area": row["area"],
        "ts": row["ts"], "description": row["description"],
        "objects": _unpack(row["objects"]),
    }


def record_scene(camera: str, description: str, area: Optional[str] = None, *,
                 ts: Optional[float] = None, retention: Optional[int] = None,
                 db_path: Optional[str] = None) -> bool:
    """Store one description, then drop expired rows and trim this camera."""
    camera = str(camera or "").strip()
    description = " ".join(str(description or "").split())[:MAX_DESCRIPTION_CHARS]
    if not camera or not description:
        return False
    when = float(ts) if ts is not None else time.time()
    days = retention if retention is not None else retention_days()
    try:
        conn = _connect(_resolve(db_path))
    except Exception as exc:
        _LOGGER.warning("scene memory: cannot open store: %s", exc)
        return False
    try:
        with conn:
            conn.execute(
                "INSERT INTO sightings (camera, area, ts, description, objects) "
                "VALUES (?, ?, ?, ?, ?)",
                (camera, (str(area).strip() or None) if area else None, when,
                 description, _pack(extract_objects(description))))
            conn.execute("DELETE FROM sightings WHERE ts < ?",
                         (time.time() - days * 86400.0,))
            conn.execute(
                "DELETE FROM sightings WHERE camera = ? AND id NOT IN "
                "(SELECT id FROM sightings WHERE camera = ? ORDER BY ts DESC, id DESC "
                "LIMIT ?)", (camera, camera, MAX_ROWS_PER_CAMERA))
        return True
    except Exception as exc:
        _LOGGER.warning("scene memory: record failed: %s", exc)
        return False
    finally:
        conn.close()


async def async_remember(hass, camera: str, description: str,
                         area: Optional[str] = None) -> bool:
    """Record one analysed frame if scene memory is on. Never raises."""
    try:
        if not description or not enabled():
            return False
        return bool(await hass.async_add_executor_job(
            record_scene, camera, description, area))
    except Exception as exc:
        _LOGGER.debug("scene memory: remember skipped: %s", exc)
        return False


def where_last_seen(term: str, *, db_path: Optional[str] = None) -> Optional[dict]:
    """The latest sighting whose stored words include every word of ``term``."""
    words = normalise_term(term)
    if not words:
        return None
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return None
    try:
        sql = "SELECT * FROM sightings WHERE " + " AND ".join(
            "objects LIKE ? ESCAPE '\\'" for _ in words) + " ORDER BY ts DESC, id DESC LIMIT 1"
        params = ["%\n" + w.replace("\\", "\\\\").replace("%", "\\%")
                  .replace("_", "\\_") + "\n%" for w in words]
        row = conn.execute(sql, params).fetchone()
        return _row(row) if row else None
    except Exception as exc:
        _LOGGER.debug("scene memory: where_last_seen failed: %s", exc)
        return None
    finally:
        conn.close()


def what_changed(camera: str, since: float, *,
                 db_path: Optional[str] = None) -> dict:
    """Compare a camera's latest description with the last one at or before
    ``since``. ``camera`` may be a camera entity id or an area name."""
    key = str(camera or "").strip().lower()
    out = {"found": False, "added": [], "removed": [],
           "latest_ts": None, "baseline_ts": None, "camera": None, "area": None}
    if not key:
        return out
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return out
    try:
        match = "(lower(camera) = ? OR lower(COALESCE(area, '')) = ?)"
        latest = conn.execute(
            f"SELECT * FROM sightings WHERE {match} ORDER BY ts DESC, id DESC LIMIT 1",
            (key, key)).fetchone()
        if not latest:
            return out
        out.update(found=True, latest_ts=latest["ts"], camera=latest["camera"],
                   area=latest["area"])
        baseline = conn.execute(
            "SELECT * FROM sightings WHERE camera = ? AND ts <= ? "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (latest["camera"], float(since))).fetchone()
        if not baseline or baseline["id"] == latest["id"]:
            return out
        now_words = set(_unpack(latest["objects"]))
        then_words = set(_unpack(baseline["objects"]))
        out.update(added=sorted(now_words - then_words),
                   removed=sorted(then_words - now_words),
                   baseline_ts=baseline["ts"])
        return out
    except Exception as exc:
        _LOGGER.debug("scene memory: what_changed failed: %s", exc)
        return out
    finally:
        conn.close()


def stats(*, db_path: Optional[str] = None) -> dict:
    out = {"count": 0, "cameras": 0, "oldest": None}
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return out
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT camera), MIN(ts) FROM sightings").fetchone()
        out.update(count=int(row[0]), cameras=int(row[1]), oldest=row[2])
    except Exception:
        pass
    finally:
        conn.close()
    return out


def forget_all(*, db_path: Optional[str] = None) -> int:
    """Delete every stored description. Returns how many rows were removed."""
    try:
        conn = _connect(_resolve(db_path))
    except Exception:
        return 0
    try:
        with conn:
            cur = conn.execute("DELETE FROM sightings")
            return int(cur.rowcount or 0)
    except Exception as exc:
        _LOGGER.warning("scene memory: forget failed: %s", exc)
        return 0
    finally:
        conn.close()
