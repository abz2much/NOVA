"""Nova intrusion support: snapshots, standing down, and learning from labels.

The investigation itself belongs to the safety manager. This module adds:

  * Snapshots. A confirmed intrusion on camera saves a still from that camera,
    for the alert and the panel.
  * Standing down. The user may call an alert a false alarm, which stops
    escalation for a cooldown and is remembered, or just acknowledge it, which
    holds the no answer escalation for a while.
  * A reviewable log of intrusion events the user can label real or false.

Snapshots live in a private folder under the Nova data directory, never under
/config/www, which Home Assistant serves at /local/ without any login. They
reach the panel only as base64 over the admin only nova/intrusion websocket
command. Snapshots from before that move are relocated by
migrate_legacy_snapshots, never deleted.

Nothing in this module raises to its caller.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from . import paths

_LOGGER = logging.getLogger(__name__)

# Folders, each overridable (tests); None means the location from paths.py.
SNAPSHOT_DIR: Optional[str] = None
_LEGACY_SNAPSHOT_DIR: Optional[str] = None
LOG_PATH: Optional[Path] = None

_MAX_SNAPSHOTS = 40
_DEFAULT_NOTIFY_IMAGE_TTL_MIN = 60.0
_CALLOFF_COOLDOWN = 600.0      # quiet after a false alarm call-off
_ACK_WINDOW = 300.0            # how long an acknowledgement holds escalation
_FALSE_ALARM_KEEP = 50
_CALL_OFF_REACH = 7200.0       # how far back a call-off looks for its alert

# Stand down state. The investigation itself is the safety manager's.
_called_off_until = 0.0
_acknowledged_ts = 0.0
_last_snapshot: dict = {}      # {path, camera, ts} of the latest capture
_false_alarms: list = []       # recent {ts, reason, camera}
_last_decision_id: Optional[int] = None   # Decision Record id of the latest intrusion


def _snapshot_dir() -> str:
    return SNAPSHOT_DIR or paths.nova_path("intrusion")


def _legacy_snapshot_dir() -> str:
    """Where snapshots were kept before they went private."""
    return _LEGACY_SNAPSHOT_DIR or paths.config_path("www", "nova", "intrusion")


def _because(reason: str) -> str:
    return f" ({reason})" if reason else ""


# ── acknowledge and call-off ────────────────────────────────────────────────

def acknowledge(reason: str = "") -> dict:
    """"I see it": the user is dealing with it, but it is not a false alarm.
    Holds the no answer escalation for a while; fresh evidence, such as a
    person on camera, still alerts."""
    global _acknowledged_ts
    _acknowledged_ts = time.time()
    _LOGGER.info("Nova: intrusion acknowledged by user%s, holding auto-escalation",
                 _because(reason))
    return {"ok": True, "held_seconds": int(_ACK_WINDOW)}


def is_acknowledged() -> bool:
    """True while an acknowledgement still holds the no answer escalation."""
    return time.time() - _acknowledged_ts < _ACK_WINDOW


def set_last_decision_id(record_id) -> None:
    """Keep the Decision Record id of the newest intrusion, so a call-off
    judges that record and not just the newest of its kind (overlapping
    intrusions would get it wrong)."""
    global _last_decision_id
    if record_id is None:
        return
    try:
        _last_decision_id = int(record_id)
    except Exception:
        pass


def _judge_false_alarm() -> None:
    """Mark the intrusion's Decision Record wrong: the exact record when one is
    known and still open, else the newest intrusion of the last two hours."""
    global _last_decision_id
    from . import decision_record
    exact = _last_decision_id
    judged = exact is not None and decision_record.set_outcome(exact, "wrong",
                                                               "dismiss_intrusion")
    if not judged:
        decision_record.set_outcome_recent("intrusion", "wrong", "dismiss_intrusion",
                                           max_age=_CALL_OFF_REACH)
    # Forgotten only once the verdict is in.
    _last_decision_id = None


def dismiss_intrusion(reason: str = "") -> dict:
    """Call the current or last intrusion a false alarm. The safety manager
    sees is_called_off() and drops its investigation; the call-off is kept
    for learning."""
    global _called_off_until
    now = time.time()
    _called_off_until = now + _CALLOFF_COOLDOWN
    entry = {"ts": int(now), "reason": str(reason or ""),
             "camera": (_last_snapshot or {}).get("camera", "")}
    _false_alarms.append(entry)
    del _false_alarms[:-_FALSE_ALARM_KEEP]
    _LOGGER.info("Nova: intrusion called off by user%s, suppressing escalation for %ds",
                 _because(reason), int(_CALLOFF_COOLDOWN))
    # The person's call-off is a confirmed label for learned damping.
    try:
        _label_call_off(_last_decision_id, time.time())
    except Exception:
        pass
    try:
        _judge_false_alarm()
    except Exception:
        pass
    return {"ok": True, "suppressed_seconds": int(_CALLOFF_COOLDOWN), "recorded": entry}


def is_called_off() -> bool:
    """True while a call-off is holding escalation."""
    return _called_off_until > time.time()


def clear_calloff() -> None:
    """Lift both the call-off and any acknowledgement (a new, unrelated
    trigger)."""
    global _called_off_until, _acknowledged_ts
    _called_off_until = _acknowledged_ts = 0.0


def false_alarm_count(within_seconds: float = 86400.0) -> int:
    """Call-offs within the window (a day by default)."""
    since = time.time() - within_seconds
    return len([entry for entry in _false_alarms if entry.get("ts", 0) >= since])


def status() -> dict:
    """The panel's and agent's view of snapshots and stand down state."""
    left = int(_called_off_until - time.time())
    return {"last_snapshot": _last_snapshot or None, "called_off": is_called_off(),
            "acknowledged": is_acknowledged(), "suppressed_for": max(0, left),
            "false_alarms_24h": false_alarm_count()}


# ── snapshots ───────────────────────────────────────────────────────────────

def _write_bytes(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


async def capture_snapshot(hass, camera_entity: str,
                           tag: str = "intrusion") -> Optional[dict]:
    """Save a still from the camera into the private folder. Returns
    {path, camera, ts}, or None on any failure."""
    global _last_snapshot
    if not camera_entity:
        return None
    try:
        from homeassistant.components.camera import async_get_image
        image = await async_get_image(hass, camera_entity, timeout=10)
        data = getattr(image, "content", None)
        if not data:
            return None
        folder = _snapshot_dir()
        os.makedirs(folder, exist_ok=True)
        taken = int(time.time())
        name = camera_entity.split(".", 1)[-1]
        path = os.path.join(folder, f"{tag}_{name}_{taken}.jpg")
        await hass.async_add_executor_job(_write_bytes, path, data)
        _prune_old()
        _last_snapshot = {"path": path, "camera": camera_entity, "ts": taken}
        _LOGGER.info("Nova: intrusion snapshot from %s saved to %s", camera_entity, path)
        return _last_snapshot
    except Exception as exc:
        _LOGGER.debug("intrusion: snapshot from %s failed: %s", camera_entity, exc)
        return None


def _read_b64(path: str) -> Optional[str]:
    """A snapshot as base64, or None. Paths come from Nova's own records, but
    anything outside the snapshot folder is still refused."""
    if not path:
        return None
    try:
        folder = os.path.realpath(_snapshot_dir())
        target = os.path.realpath(path)
        if os.path.commonpath([folder, target]) != folder:
            _LOGGER.warning("intrusion: refused to read snapshot outside %s: %s",
                            _snapshot_dir(), path)
            return None
        return base64.b64encode(Path(target).read_bytes()).decode("ascii")
    except Exception as exc:
        _LOGGER.debug("intrusion: reading snapshot %s failed: %s", path, exc)
        return None


async def get_snapshot_b64(hass, path: str) -> Optional[str]:
    """_read_b64 in the executor."""
    return await hass.async_add_executor_job(_read_b64, path)


def _notify_ttl_minutes(hass) -> float:
    """Minutes a notification's image copy lives
    (intrusion_notify_image_ttl_minutes), at least one."""
    try:
        from . import nova_config
        minutes = nova_config.get("intrusion_notify_image_ttl_minutes",
                                  _DEFAULT_NOTIFY_IMAGE_TTL_MIN)
        return max(1.0, float(minutes))
    except Exception:
        return _DEFAULT_NOTIFY_IMAGE_TTL_MIN


def _copy_for_notify(snapshot_path: str, sub_dir: str) -> Optional[str]:
    """Copy a snapshot into the media folder under a random name; returns the
    name, or None. Runs in the executor."""
    try:
        os.makedirs(sub_dir, exist_ok=True)
        name = f"{secrets.token_urlsafe(16)}.jpg"
        data = Path(snapshot_path).read_bytes()
        Path(sub_dir, name).write_bytes(data)
        return name
    except Exception as exc:
        _LOGGER.debug("intrusion: copying the notification image failed: %s", exc)
        return None


def _delete_notify_copy(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def _media_source(hass) -> Optional[str]:
    """The media folder to use: "local" when present, else the first."""
    folders = getattr(hass.config, "media_dirs", None) or {}
    if "local" in folders:
        return "local"
    return next(iter(folders), None)


async def get_notification_image_url(hass, snapshot_path: str) -> Optional[str]:
    """A short lived signed URL for a phone notification's picture, or None.

    Phones fetch a notification's picture with a plain URL and cannot send a
    login, so the snapshot is copied, under a random name, into Home
    Assistant's media folder. /media/ needs a login or a signed path, unlike
    /local/. The signature expires after _notify_ttl_minutes and the copy is
    deleted on the same timer, used or not. The private snapshot is left as
    it is, and no URL ever points at it or at /local/. Without a media folder,
    or on any failure, the notification just goes without a picture.
    """
    if not snapshot_path:
        return None
    try:
        source = _media_source(hass)
        if not source:
            _LOGGER.debug("intrusion: no media folder configured, sending without a photo")
            return None
        folder = os.path.join(hass.config.media_dirs[source], "nova_intrusion")
        name = await hass.async_add_executor_job(_copy_for_notify, snapshot_path, folder)
        if not name:
            return None
        copy = os.path.join(folder, name)
        minutes = _notify_ttl_minutes(hass)
        try:
            from homeassistant.components.http.auth import async_sign_path
            signed = async_sign_path(hass, f"/media/{source}/nova_intrusion/{name}",
                                     timedelta(minutes=minutes))
        except Exception as exc:
            _LOGGER.debug("intrusion: signing the notification image failed: %s", exc)
            await hass.async_add_executor_job(_delete_notify_copy, copy)
            return None
        try:
            from homeassistant.helpers.event import async_call_later

            def _expire(_now):
                return hass.async_add_executor_job(_delete_notify_copy, copy)
            async_call_later(hass, minutes * 60, _expire)
        except Exception as exc:
            _LOGGER.debug("intrusion: no cleanup timer for the notification image, "
                          "deleting it now instead: %s", exc)
            await hass.async_add_executor_job(_delete_notify_copy, copy)
            return None
        return signed
    except Exception as exc:
        _LOGGER.debug("intrusion: notification image url failed: %s", exc)
        return None


def _prune_old() -> None:
    """Keep only the newest _MAX_SNAPSHOTS stills."""
    try:
        folder = _snapshot_dir()
        stills = [os.path.join(folder, name) for name in os.listdir(folder)
                  if name.endswith(".jpg")]
        newest_first = sorted(stills, key=os.path.getmtime, reverse=True)
        for path in newest_first[_MAX_SNAPSHOTS:]:
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception:
        pass


def _repoint_log(old: str, new: str) -> None:
    """Make log entries that name the old folder name the new one."""
    _load_log()
    touched = False
    for event in _log:
        where = event.get("snapshot_path") or ""
        if where.startswith(old):
            event["snapshot_path"] = where.replace(old, new, 1)
            touched = True
    if touched:
        _save_log()


def migrate_legacy_snapshots() -> dict:
    """Move snapshots out of the old public folder into the private one and
    fix log entries that point at them. A file whose name already exists in
    the private folder stays where it is: nothing is overwritten or deleted.
    Safe on every start up; a no op once the old folder is empty."""
    moved = skipped = 0
    try:
        legacy, private = _legacy_snapshot_dir(), _snapshot_dir()
        if not os.path.isdir(legacy):
            return {"moved": 0, "skipped": 0}
        os.makedirs(private, exist_ok=True)
        for name in os.listdir(legacy):
            if not name.endswith(".jpg"):
                continue
            target = os.path.join(private, name)
            try:
                if os.path.exists(target):
                    skipped += 1
                else:
                    os.rename(os.path.join(legacy, name), target)
                    moved += 1
            except Exception as exc:
                _LOGGER.warning("intrusion: could not migrate legacy snapshot %s: %s",
                                name, exc)
                skipped += 1
        if moved:
            _repoint_log(legacy, private)
            _LOGGER.warning("Nova: moved %d intrusion snapshot(s) from the public %s to %s "
                            "(%d left in place)%s", moved, legacy, private, skipped,
                            ", the rest are untouched" if skipped else "")
    except Exception as exc:
        _LOGGER.debug("intrusion: legacy snapshot migration skipped: %s", exc)
    return {"moved": moved, "skipped": skipped}


def last_snapshot() -> Optional[dict]:
    """The newest snapshot's details, or None."""
    return _last_snapshot or None


# ── the event log and learning ──────────────────────────────────────────────
# Every intrusion event is logged with its snapshot for the user to label real
# or false. A place and time repeatedly labelled false stops the weak alerts
# there (the first "investigating" ping and the unanswered notice). A
# confirmed intrusion, by vision or by motion along a real route inside,
# always alerts, whatever the labels say.

_MAX_LOG = 200
_LEARN_WINDOW = 30 * 86400.0   # labels older than this no longer count
_LEARN_MIN_FALSE = 3           # false labels needed before weak alerts are damped
_log: list = []
_log_loaded = False

LABEL_SOURCE_PANEL = "panel_label"
LABEL_SOURCE_CALL_OFF = "dismiss_intrusion"
# Only labels a person gave are learned from. A label with no source predates
# sources and could only have come from the panel.
_CONFIRMED_LABEL_SOURCES = {LABEL_SOURCE_PANEL, LABEL_SOURCE_CALL_OFF}


def _log_path() -> Path:
    return LOG_PATH or Path(paths.nova_path("intrusion_log.json"))


def _load_log() -> list:
    """The saved log, read from disk once per run."""
    global _log, _log_loaded
    if _log_loaded:
        return _log
    _log_loaded = True
    try:
        source = _log_path()
        if source.exists():
            saved = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                _log = saved[-_MAX_LOG:]
    except Exception as exc:
        _LOGGER.debug("intrusion log: load failed: %s", exc)
        _log = []
    return _log


def _save_log() -> None:
    try:
        target = _log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_log[-_MAX_LOG:], indent=2, default=str),
                          encoding="utf-8")
    except Exception as exc:
        _LOGGER.debug("intrusion log: save failed: %s", exc)


def _pattern_key(area: Optional[str], camera: Optional[str],
                 ts: Optional[float] = None) -> str:
    """"place|slot": the area (else the camera) and which of the day's eight
    three hour slots, so one place at one time of day is one pattern."""
    place = (area or camera or "unknown").strip().lower()
    slot = time.localtime(ts or time.time()).tm_hour // 3
    return f"{place}|{slot}"


def record_event(kind: str, reason: str = "", breach: Optional[str] = None,
                 breach_area: Optional[str] = None, camera: Optional[str] = None,
                 snapshot: Optional[dict] = None, zones: Optional[list] = None,
                 max_depth: Optional[int] = None, decision_id: Optional[int] = None) -> dict:
    """Log an intrusion event: investigating, unresolved, confirmed or
    false_alarm."""
    _load_log()
    now = time.time()
    shot = snapshot or {}
    event = {
        "id": f"evt_{int(now)}_{len(_log)}", "ts": now,
        "kind": str(kind or "investigating"), "reason": reason or "",
        "breach": breach or "", "breach_area": breach_area or "",
        "camera": camera or shot.get("camera") or "",
        "snapshot_path": shot.get("path") or "",
        "zones": list(zones or []), "max_depth": max_depth, "label": None,
        "pattern": _pattern_key(breach_area, camera, now),
        # A panel label on this event also judges this decision.
        "decision_id": decision_id,
    }
    _log.append(event)
    del _log[:-_MAX_LOG]
    _save_log()
    return event


def get_log(limit: int = 50) -> list:
    """The newest events first, for the panel."""
    _load_log()
    try:
        count = min(max(int(limit), 1), _MAX_LOG)
    except (TypeError, ValueError):
        count = 50
    return _log[-count:][::-1]


def _label_source(ev: dict) -> str:
    return ev.get("label_source") or LABEL_SOURCE_PANEL


def _judge_labelled(event: dict, label: str) -> None:
    """A panel label also judges the event's Decision Record entry."""
    try:
        from . import decision_record as dr
        verdict = dr.OUTCOME_WRONG if label == "false" else dr.OUTCOME_GOOD
        dr.set_outcome(int(event["decision_id"]), verdict, dr.SOURCE_PANEL_LABEL)
    except Exception:
        pass


def label_event(event_id: str, label: str, source: str = LABEL_SOURCE_PANEL) -> dict:
    """Label an event "real" or "false", or clear it with None or "". This is
    what learning learns from."""
    _load_log()
    if label not in ("real", "false", None, ""):
        return {"ok": False, "error": "label must be 'real' or 'false'"}
    label = label or None
    event = next((ev for ev in _log if ev.get("id") == event_id), None)
    if event is None:
        return {"ok": False, "error": "event not found"}
    event.update(label=label, label_source=source if label else None, labeled_ts=time.time())
    _save_log()
    if label and source == LABEL_SOURCE_PANEL and event.get("decision_id") is not None:
        _judge_labelled(event, label)
    return {"ok": True, "id": event_id, "label": label}


def _label_call_off(decision_id: Optional[int], now: float) -> Optional[str]:
    """Label the intrusion's first alert "false" after a call-off. That
    decision's alert is preferred, else the newest first alert of the last two
    hours. If that alert already has a label it is left alone. Returns the
    event id labelled, or None."""
    _load_log()
    recent = [ev for ev in _log if ev.get("kind") == "investigating"
              and now - float(ev.get("ts") or 0) <= _CALL_OFF_REACH]
    same = [ev for ev in recent if decision_id is not None and ev.get("decision_id") == decision_id]
    chosen = (same or recent)[-1:]
    if not chosen or chosen[0].get("label"):
        return None
    event = chosen[0]
    event.update(label="false", label_source=LABEL_SOURCE_CALL_OFF, labeled_ts=now)
    _save_log()
    return event.get("id")


def pattern_verdict(area: Optional[str], camera: Optional[str],
                    ts: Optional[float] = None) -> dict:
    """The labels a person gave this pattern in the learning window. damp is
    True once there are enough false labels and not one real one: a place
    that ever had a genuine intruder is never learned away."""
    _load_log()
    key = _pattern_key(area, camera, ts)
    oldest = time.time() - _LEARN_WINDOW
    tally = {"false": 0, "real": 0}
    for ev in _log:
        if ev.get("pattern") != key or ev.get("ts", 0) < oldest:
            continue
        if _label_source(ev) in _CONFIRMED_LABEL_SOURCES and ev.get("label") in tally:
            tally[ev["label"]] += 1
    damp = tally["real"] == 0 and tally["false"] >= _LEARN_MIN_FALSE
    return {"false_count": tally["false"], "real_count": tally["real"], "damp": damp,
            "pattern": key}


def should_damp_weak_alert(area: Optional[str], camera: Optional[str]) -> bool:
    """Whether this pattern's weak alerts stay quiet. Only the first
    investigating ping and the unanswered notice ask; a confirmed intrusion
    never does."""
    try:
        return bool(pattern_verdict(area, camera).get("damp"))
    except Exception:
        return False


def learning_summary() -> dict:
    """The panel's view of what the labels have taught."""
    _load_log()
    labelled = [ev for ev in _log if ev.get("label")]
    patterns: dict = {}
    for ev in labelled:
        counts = patterns.setdefault(ev.get("pattern") or "?", {"false": 0, "real": 0})
        counts[ev["label"]] = counts.get(ev["label"], 0) + 1
    damped = [key for key, counts in patterns.items()
              if not counts.get("real", 0) and counts.get("false", 0) >= _LEARN_MIN_FALSE]
    return {"events": len(_log), "labeled": len(labelled), "patterns": patterns,
            "damped_patterns": damped, "min_false_to_damp": _LEARN_MIN_FALSE}
