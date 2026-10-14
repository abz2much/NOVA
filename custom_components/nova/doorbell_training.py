"""The doorbell log: analysed doorbell events kept as JSON Lines.

Every analysed doorbell press is appended here (camera.py and the Eufy path
call log_event), and the Nest doorbell's recorded history can be scanned
into it as well (scan_backlog). The panel reads it back through
load_events and stats, and find_patterns looks for visitors who keep
turning up at about the same time of day.

This is not face recognition. It groups only the vision model's category
label (delivery, mail, person and so on) by camera and hour, and reports a
pattern only when it was seen on enough separate days. Identity comes from
recognition.py alone.

The log helpers use the standard library only and never raise into the
caller. The backlog scan reaches into the Nest integration's objects, so
every step of it is guarded and reported rather than raised.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import paths

_LOGGER = logging.getLogger(__name__)

# Test overrides. None means the Nova folder and doorbell_log.jsonl in it.
LOG_DIR: Optional[str] = None
LOG_PATH: Optional[str] = None

MAX_RECORDS = 5000          # the log keeps only this many newest records
_RESULT_TEXT = ("summary", "analysis", "category")

CATEGORY_LABEL = {
    "delivery": "a delivery", "package": "a package drop-off", "mail": "mail",
    "person": "someone", "known_resident": "a resident", "vehicle": "a vehicle",
    "animal": "an animal", "empty": "nothing", "other": "something",
}


def _log_dir() -> str:
    return LOG_DIR or paths.nova_path()


def _log_path() -> str:
    return LOG_PATH or os.path.join(_log_dir(), "doorbell_log.jsonl")


# ── the log ─────────────────────────────────────────────────────────────────

def _utc_stamp() -> str:
    """Now in UTC as a naive ISO time with a trailing Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def log_event(camera: str, entity_id: str, source: str, result: dict) -> None:
    """Add one analysed event. ``source`` says where the frame came from
    ("live", "event-media", "backlog", "eufy"). The spoken line is kept even
    when announcements were off, so it can be checked later. Never raises."""
    try:
        found = result or {}
        record: dict[str, Any] = {
            "ts": _utc_stamp(),
            "camera": camera,
            "entity_id": entity_id,
            "image_source": source,
        }
        for key in _RESULT_TEXT:
            record[key] = found.get(key, "")
        record["notable"] = bool(found.get("notable", False))
        record["speak"] = found.get("speak") or ""
        os.makedirs(_log_dir(), exist_ok=True)
        with open(_log_path(), "a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        _keep_newest()
    except Exception as err:  # noqa: BLE001 - the log must never break a caller
        _LOGGER.debug("Doorbell log write failed: %s", err)


def _keep_newest() -> None:
    """Cut the file back to the newest MAX_RECORDS lines."""
    try:
        target = _log_path()
        if not os.path.exists(target):
            return
        with open(target, encoding="utf-8") as src:
            lines = src.readlines()
        excess = len(lines) - MAX_RECORDS
        if excess > 0:
            with open(target, "w", encoding="utf-8") as out:
                out.writelines(lines[excess:])
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Doorbell log trim failed: %s", err)


def load_events(limit: Optional[int] = None) -> list[dict]:
    """The logged events, oldest first; with ``limit``, only the newest that
    many. Blank or unreadable lines are skipped. Never raises."""
    target = _log_path()
    events: list[dict] = []
    try:
        if not os.path.exists(target):
            return []
        with open(target, encoding="utf-8") as src:
            for raw in src:
                text = raw.strip()
                if not text:
                    continue
                try:
                    events.append(json.loads(text))
                except ValueError:
                    continue
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Doorbell log read failed: %s", err)
        return []
    if limit:
        return events[-limit:]
    return events


def stats() -> dict:
    """Counts for the panel: totals, by category and source, first and last."""
    events = load_events()
    categories: Counter = Counter()
    sources: Counter = Counter()
    for event in events:
        categories[event.get("category", "?")] += 1
        sources[event.get("image_source", "?")] += 1
    return {
        "total": len(events),
        "notable": sum(1 for event in events if event.get("notable")),
        "by_category": dict(categories),
        "by_source": dict(sources),
        "first": events[0]["ts"] if events else None,
        "last": events[-1]["ts"] if events else None,
    }


# ── visitor patterns ────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> Optional[datetime]:
    """A logged UTC stamp as a naive datetime, or None when unreadable."""
    try:
        return datetime.fromisoformat(ts[:-1] if ts.endswith("Z") else ts)
    except (ValueError, TypeError):
        return None


def _hours_apart(a: int, b: int) -> int:
    """Distance between two clock hours, going round midnight."""
    gap = abs(a - b) % 24
    return min(gap, 24 - gap)


def _describe(camera: str, category: str, hour: int, days: int, observed: int) -> str:
    label = CATEGORY_LABEL.get(category, category)
    return (f"{label.capitalize()} at {camera}, mostly around {hour:02d}:00 — "
            f"seen on {days} of the last {observed} days")


def find_patterns(
    min_occurrences: int = 3,
    lookback_days: int = 30,
    utc_offset_minutes: int = 0,
) -> list[dict]:
    """Recurring visitors in the log, most regular first (at most eight).

    Events inside the lookback window are grouped by camera and category.
    A group is a pattern when it has at least ``min_occurrences`` events on
    at least that many separate days. Times are moved to local time by
    ``utc_offset_minutes``, which the caller works out. Never raises."""
    try:
        shift = timedelta(minutes=utc_offset_minutes)
        since = datetime.utcnow() + shift - timedelta(days=lookback_days)
        groups: dict[tuple[str, str], list[datetime]] = {}
        for event in load_events():
            when = _parse_ts(event.get("ts", ""))
            if when is None:
                continue
            when += shift
            if when < since:
                continue
            camera = event.get("camera") or event.get("entity_id") or "camera"
            category = event.get("category") or "other"
            groups.setdefault((camera, category), []).append(when)
        if not groups:
            return []

        every = [when for times in groups.values() for when in times]
        span = (max(every).date() - min(every).date()).days + 1
        observed = max(1, min(lookback_days, span))

        found = []
        for (camera, category), times in groups.items():
            days = len({when.date() for when in times})
            if len(times) < min_occurrences or days < min_occurrences:
                continue
            hours = [when.hour for when in times]
            usual = Counter(hours).most_common(1)[0][0]
            close = sum(1 for hour in hours if _hours_apart(hour, usual) <= 1)
            found.append({
                "camera": camera,
                "category": category,
                "occurrences": len(times),
                "distinct_days": days,
                "observed_days": observed,
                "typical_hour": usual,
                "consistency": round(close / len(times), 2),
                "description": _describe(camera, category, usual, days, observed),
            })
        found.sort(key=lambda pattern: pattern["distinct_days"], reverse=True)
        return found[:8]
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Doorbell pattern mining failed: %s", err)
        return []


# ── the Nest backlog ────────────────────────────────────────────────────────

def _field(holder: Any, name: str) -> Any:
    """``holder.name``, or ``holder[name]`` when holder is a dict."""
    value = getattr(holder, name, None)
    if value is None and isinstance(holder, dict):
        value = holder.get(name)
    return value


def _loaded_nest_entries(hass) -> list:
    entries = hass.config_entries
    try:
        return list(entries.async_loaded_entries("nest"))
    except AttributeError:
        pass
    try:
        return [entry for entry in entries.async_entries("nest")
                if "LOADED" in str(getattr(entry, "state", "")).upper()]
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Doorbell backlog: listing Nest entries failed: %s", err)
        return []


def _iter_nest_doorbell_devices(hass) -> list[tuple[str, Any]]:
    """(device id, device) for each Nest device with recorded event media.

    The device manager is read the way Home Assistant itself reads it, from
    the loaded entry's runtime data, with the Nest integration's hass.data
    as the fallback on older cores."""
    devices: list[tuple[str, Any]] = []
    for entry in _loaded_nest_entries(hass):
        manager = _field(getattr(entry, "runtime_data", None), "device_manager")
        if manager is None:
            nest = hass.data.get("nest") or {}
            manager = _field(nest.get(getattr(entry, "entry_id", "")), "device_manager")
        known = getattr(manager, "devices", None)
        if not known or not hasattr(known, "items"):
            continue
        devices.extend((device_id, device) for device_id, device in known.items()
                       if getattr(device, "event_media_manager", None) is not None)
    return devices


_SESSION_LISTS = (("clip", "async_clip_preview_sessions"),
                  ("image", "async_image_sessions"))


async def _events_for_device(dev) -> list[tuple[str, Any]]:
    """The device's recorded sessions as (kind, event): battery models keep
    clip previews, wired ones keep images."""
    media = getattr(dev, "event_media_manager", None)
    if media is None:
        return []
    sessions: list[tuple[str, Any]] = []
    for kind, name in _SESSION_LISTS:
        lister = getattr(media, name, None)
        if lister is None:
            continue
        try:
            listed = lister()
            if hasattr(listed, "__aiter__"):
                items = [item async for item in listed]
            else:
                items = list(await listed or [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Doorbell backlog: %s failed: %s", name, err)
            continue
        sessions.extend((kind, item) for item in items)
    return sessions


def _looks_like_image(data: Optional[bytes]) -> bool:
    """A real still: not tiny, and not an MP4 (whose box type is "ftyp")."""
    return bool(data) and len(data) > 2000 and not data[4:12].startswith(b"ftyp")


async def _event_image_bytes(dev, kind: str, event_obj) -> Optional[bytes]:
    """A still for one event, or None. A clip tries its thumbnail first, an
    image session its media first."""
    media = getattr(dev, "event_media_manager", None)
    token = getattr(event_obj, "event_token", None)
    if media is None or not token:
        return None
    order = ["get_media_from_token", "get_clip_thumbnail_from_token"]
    if kind == "clip":
        order.reverse()
    for name in order:
        fetch = getattr(media, name, None)
        if fetch is None:
            continue
        try:
            got = await fetch(token)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Doorbell backlog: %s failed: %s", name, err)
            continue
        data = getattr(got, "contents", None) if got is not None else None
        if _looks_like_image(data):
            return data
    return None


def _event_label(event_obj) -> str:
    kinds = getattr(event_obj, "event_types", None) or [getattr(event_obj, "event_type", None)]
    names = [str(kind).rsplit(".", 1)[-1] for kind in kinds if kind]
    return ", ".join(names) or "doorbell event"


_NO_DEVICES = (
    "No loaded Nest config entry exposed a device manager with event "
    "media. Discovery mirrors HA core's own enumeration "
    "(entry.runtime_data.device_manager), so if the Media browser shows "
    "events, check that the Nest entry is fully loaded."
)


def _why_nothing(found: int, no_image: int) -> str:
    if found and no_image == found:
        return (f"Found {found} event(s) but none yielded an image — clip "
                "thumbnails require ffmpeg transcoding via the Nest store; "
                "check home-assistant.log for transcoder errors.")
    return (f"Found {found} event record(s) but could not extract analysable "
            f"images ({no_image} without media).")


async def scan_backlog(hass, analyze_image: Callable, honorific: str,
                       limit: int = 40) -> dict:
    """Analyse recorded Nest doorbell events into the log.

    ``analyze_image(image_bytes, label)`` comes from the caller. At most
    ``limit`` events are analysed (always at least one). Returns a report
    and never raises; ``reason`` explains a scan that analysed nothing."""
    report: dict[str, Any] = {"ok": False, "devices": 0, "found": 0, "analyzed": 0,
                              "no_image": 0, "errors": [], "reason": ""}
    devices = _iter_nest_doorbell_devices(hass)
    report["devices"] = len(devices)
    if not devices:
        report["reason"] = _NO_DEVICES
        return report

    budget = max(1, int(limit))
    for device_id, device in devices:
        if report["analyzed"] >= budget:
            break
        try:
            sessions = await _events_for_device(device)
        except Exception as err:  # noqa: BLE001
            report["errors"].append(f"events:{err}")
            continue
        report["found"] += len(sessions)
        for kind, event_obj in sessions:
            if report["analyzed"] >= budget:
                break
            try:
                image = await _event_image_bytes(device, kind, event_obj)
                if not image:
                    report["no_image"] += 1
                    continue
                result = await analyze_image(image, _event_label(event_obj))
                if result and result.get("success"):
                    log_event(result.get("camera", device_id), f"nest:{device_id}",
                              "backlog", result)
                    report["analyzed"] += 1
            except Exception as err:  # noqa: BLE001
                report["errors"].append(str(err))

    report["ok"] = report["analyzed"] > 0
    if not report["ok"]:
        report["reason"] = _why_nothing(report["found"], report["no_image"])
    report["errors"] = report["errors"][:5]
    return report
