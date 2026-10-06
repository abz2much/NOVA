"""Hazard warnings: the lifecycle, the memory and the wording (8.8.0).

Met Éireann (hazard_met_eireann.py) and a custom CAP feed (hazard_cap.py)
both turn their feed into warning dicts:

  id, msg_type (Alert/Update/Cancel), refs (ids it updates or cancels),
  source, source_label, type, level (yellow/orange/red), onset, expiry,
  headline, description (both exactly as published), areas (names shown to
  the person) and area_keys (county codes, or area names, for matching).

reconcile() turns one poll into events: a new warning, a level change (up
or down), or a cancellation. What was announced is remembered on disk
(hazard_warnings.json, atomic writes, size capped, expired entries
dropped), so a restart never repeats a standing warning.

A cancellation only ever comes from a complete, successfully parsed list,
or from a CAP Cancel message that references the warning. A failed fetch,
a bad status code or an unparseable body is not an empty list.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

from . import hazard_cap

_LOGGER = logging.getLogger(__name__)

LEVELS = ("yellow", "orange", "red")
_RANK = {level: i for i, level in enumerate(LEVELS)}

STORE_FILE = "hazard_warnings.json"
STORE_MAX_ENTRIES = 200
STORE_MAX_BYTES = 256 * 1024


def level_rank(level) -> int:
    """yellow 0, orange 1, red 2; anything else -1."""
    return _RANK.get(str(level or "").strip().lower(), -1)


def at_least(level, threshold) -> bool:
    return level_rank(level) >= 0 and level_rank(level) >= level_rank(threshold)


def speak_allowed(level, now_in_quiet: bool, speak_level, push_level, night_level) -> bool:
    """Whether a warning that is being pushed may also be spoken.

    Daytime keeps the existing speak and push thresholds.  During quiet
    hours the separate night threshold replaces the daytime speak threshold;
    ``off`` keeps every warning phone-only.  A warning below the push
    threshold is never spoken on its own.
    """
    if not at_least(level, push_level):
        return False
    if now_in_quiet:
        return night_level != "off" and at_least(level, night_level)
    return at_least(level, speak_level)


def _expired(expiry: str, now: datetime) -> bool:
    dt = hazard_cap.parse_time(expiry)
    return dt is not None and dt <= now


# ── memory ───────────────────────────────────────────────────────────────────

def _store_path() -> str:
    from . import paths
    return paths.nova_path(STORE_FILE)


def _clean_entry(entry) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    if str(entry.get("level", "")).lower() not in LEVELS:
        return None
    return {
        "level": str(entry["level"]).lower(),
        "expiry": str(entry.get("expiry", "")),
        "type": str(entry.get("type", "")),
        "areas": [str(a) for a in entry.get("areas", []) if isinstance(a, str)],
        "area_keys": [str(a) for a in entry.get("area_keys", []) if isinstance(a, str)],
        "headline": str(entry.get("headline", "")),
        "source_label": str(entry.get("source_label", "")),
        "notified": entry.get("notified") is True,
    }


def _trim(entries: dict, now: datetime) -> dict:
    """Drop expired entries, then keep at most STORE_MAX_ENTRIES, latest
    expiry first."""
    live = {k: v for k, v in entries.items() if not _expired(v["expiry"], now)}
    if len(live) <= STORE_MAX_ENTRIES:
        return live
    order = sorted(live, key=lambda k: hazard_cap.parse_time(live[k]["expiry"])
                   or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
    return {k: live[k] for k in order[:STORE_MAX_ENTRIES]}


def load_store(now: datetime, path: Optional[str] = None) -> dict:
    """{source: {id: entry}}. A missing, corrupt or oversized file loads as
    empty (and is logged); invalid entries are dropped."""
    import os
    from .persistence import files
    path = path or _store_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > STORE_MAX_BYTES:
            _LOGGER.warning("Nova hazard: %s is too large; starting with no saved warnings", path)
            return {}
    except OSError:
        return {}
    read = files.read_json(path)
    if read.status == files.CORRUPT:
        _LOGGER.warning("Nova hazard: %s is corrupt (%s); starting with no saved warnings",
                        path, read.error)
        return {}
    if read.status != files.OK or not isinstance(read.value, dict):
        return {}
    out: dict = {}
    for source, entries in read.value.items():
        if not isinstance(entries, dict):
            continue
        clean = {str(k): e for k, v in entries.items() if (e := _clean_entry(v)) is not None}
        out[str(source)] = _trim(clean, now)
    return out


def save_store(store: dict, now: datetime, path: Optional[str] = None) -> bool:
    from .persistence import files
    try:
        files.write_json_atomic(path or _store_path(),
                                {s: _trim(e, now) for s, e in store.items()})
        return True
    except Exception as exc:
        _LOGGER.warning("Nova hazard: could not save warnings: %s", type(exc).__name__)
        return False


# ── the lifecycle ────────────────────────────────────────────────────────────

def _entry_for(w: dict, notified: bool) -> dict:
    return {"level": w["level"], "expiry": w.get("expiry", ""), "type": w.get("type", ""),
            "areas": list(w.get("areas", [])), "area_keys": list(w.get("area_keys", [])),
            "headline": w.get("headline", ""), "source_label": w.get("source_label", ""),
            "notified": notified}


def reconcile(entries: dict, current: list[dict], *, complete: bool, now: datetime,
              push_level: str) -> tuple[dict, list[tuple]]:
    """One poll of one source. ``entries`` is that source's memory
    ({id: entry}); returns (new entries, events). Events:

      ("new", warning)
      ("upgraded" | "lowered", warning, old_level)
      ("cancelled", entry)

    A warning below ``push_level`` is remembered but not announced (and a
    later rise to the push level announces it). A warning that replaces an
    earlier one (its CAP references, or, for a source with no references,
    the same type sharing an area with a warning that has gone) carries on
    that warning: a level change is announced, an unchanged level is not.
    """
    entries = {k: dict(v) for k, v in entries.items()}
    events: list[tuple] = []
    cancel_ids: list[str] = []
    live = []
    for w in current:
        if w.get("msg_type") == "Cancel":
            cancel_ids.extend(w.get("refs", []))
            continue
        if _expired(w.get("expiry", ""), now):
            continue
        if w.get("level") not in LEVELS:
            continue
        live.append(w)
    live_ids = {w["id"] for w in live}
    consumed: set[str] = set()
    for w in live:
        prev = w["id"] if w["id"] in entries else None
        if prev is None:
            prev = next((r for r in w.get("refs", []) if r in entries), None)
        if prev is None and not w.get("refs"):
            keys = set(w.get("area_keys", []))
            prev = next((sid for sid, e in entries.items()
                         if sid not in live_ids and sid not in consumed
                         and e["type"].lower() == str(w.get("type", "")).lower()
                         and keys & set(e["area_keys"])), None)
        wants = at_least(w["level"], push_level)
        if prev is None:
            entries[w["id"]] = _entry_for(w, wants)
            if wants:
                events.append(("new", w))
            continue
        old = entries.pop(prev)
        consumed.add(prev)
        notified = old["notified"]
        if w["level"] != old["level"] and (notified or wants):
            kind = "upgraded" if level_rank(w["level"]) > level_rank(old["level"]) else "lowered"
            events.append((kind, w, old["level"]))
            notified = True
        entries[w["id"]] = _entry_for(w, notified)
    for cid in cancel_ids:
        old = entries.pop(cid, None)
        if old and old["notified"] and not _expired(old["expiry"], now):
            events.append(("cancelled", old))
    if complete:
        for sid in [s for s in entries if s not in live_ids]:
            old = entries.pop(sid)
            if old["notified"] and not _expired(old["expiry"], now):
                events.append(("cancelled", old))
    return _trim(entries, now), events


# ── wording ──────────────────────────────────────────────────────────────────

class _TextOf(HTMLParser):
    """Text of published HTML: every word kept, tags become line breaks and
    list bullets. Nothing is shortened or reworded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in ("p", "br", "div", "ul", "ol"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "ul", "ol"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def text_of(published: str) -> str:
    """Plain text of a headline or description as published."""
    raw = str(published or "")
    if "<" not in raw:
        return html.unescape(raw).strip()
    parser = _TextOf()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


_TYPE_WORDS = {"snow-ice": "snow and ice"}


def type_words(wtype: str) -> str:
    t = str(wtype or "").strip()
    return _TYPE_WORDS.get(t.lower(), t.replace("-", " ").lower()) or "weather"


def _and(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "your area"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def local_time(text: str, tz_name: Optional[str]) -> str:
    """An ISO time as 'Tue 9 Sep 12:00' in Home Assistant's time zone."""
    dt = hazard_cap.parse_time(text)
    if dt is None:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(tz_name)) if tz_name else dt
    except Exception:
        pass
    return f"{dt:%a} {dt.day} {dt:%b %H:%M}"


def window(w: dict, tz_name: Optional[str]) -> str:
    start, end = local_time(w.get("onset", ""), tz_name), local_time(w.get("expiry", ""), tz_name)
    if start and end:
        return f"from {start} to {end}"
    return f"until {end}" if end else ""


def _addr(honorific: str) -> str:
    return f", {honorific}" if honorific else ""


def spoken(kind: str, w: dict, honorific: str, tz_name: Optional[str],
           old_level: str = "") -> str:
    """Nova's own sentence (source, level, type, areas, time window) and
    then the headline exactly as published. The description is never
    spoken, so it is never shortened."""
    label = w.get("source_label") or "The warning service"
    level = str(w.get("level", "")).capitalize()
    what = type_words(w.get("type", ""))
    where = _and(list(w.get("areas", [])))
    when = window(w, tz_name)
    when = f", {when}" if when else ""
    if kind == "new":
        lead = f"{label} {level} {what} warning for {where}{when}{_addr(honorific)}."
    elif kind == "cancelled":
        lead = f"{label} has cancelled the {level} {what} warning for {where}{_addr(honorific)}."
    else:
        verb = "upgraded" if kind == "upgraded" else "lowered"
        lead = (f"{label} has {verb} the {what} warning for {where} from "
                f"{str(old_level).capitalize()} to {level}{when}{_addr(honorific)}.")
    headline = text_of(w.get("headline", ""))
    return f"{lead} {headline}" if headline else lead


def pushed(kind: str, w: dict, honorific: str, tz_name: Optional[str],
           old_level: str = "") -> str:
    """The phone notification: the spoken text, the full description as
    published (as plain text) and the source credit."""
    parts = [spoken(kind, w, honorific, tz_name, old_level)]
    desc = text_of(w.get("description", ""))
    if desc and kind != "cancelled":
        parts.append(desc)
    parts.append(f"Source: {w.get('source_label') or 'unknown'}")
    return "\n\n".join(parts)


def for_panel(w: dict, tz_name: Optional[str]) -> dict:
    """A warning as the panel, the agent and the briefing read it. The
    headline and description are as published; *_text are the same words
    as plain text."""
    return {
        "id": w.get("id"), "source": w.get("source"), "source_label": w.get("source_label"),
        "type": w.get("type"), "level": w.get("level"),
        "counties": list(w.get("areas", [])), "area_keys": list(w.get("area_keys", [])),
        "onset": w.get("onset"), "expiry": w.get("expiry"),
        "from": local_time(w.get("onset", ""), tz_name),
        "to": local_time(w.get("expiry", ""), tz_name),
        "headline": w.get("headline", ""), "description": w.get("description", ""),
        "headline_text": text_of(w.get("headline", "")),
        "description_text": text_of(w.get("description", "")),
    }
