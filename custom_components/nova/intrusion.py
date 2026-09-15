"""
Nova — Intrusion snapshots & false-alarm call-off (v6.68.0).

Two additions to the existing SafetyManager intrusion flow:

  1. Snapshot: when an intrusion is confirmed on camera, grab a still from that
     camera and make it available with the alert — so the notification can show
     WHO/what triggered it, and the panel can display it.

  2. Call-off: let the user declare a false alarm. dismiss_intrusion() clears the
     active investigation, suppresses further escalation for a cooldown, and
     records the false alarm so repeated benign triggers can be learned from.

Snapshots are written under /config/nova/intrusion — NOT /config/www, which
Home Assistant serves at /local/... with no authentication at all. These
images can show a confirmed intruder's face; they're read back and sent to
the panel as base64 over the existing @require_admin `nova/intrusion`
websocket command instead; there is no HTTP path serving them at all, so
there's nothing new to authenticate. (Pre-v7.102.0 this lived under
/config/www/nova/intrusion; see migrate_legacy_snapshots() for the one-time
move of any pre-existing files — old snapshots are relocated, never deleted.)
Everything here is defensive and never raises to the caller.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import time
import json
from datetime import timedelta
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)

_DEFAULT_NOTIFY_IMAGE_TTL_MIN = 60.0

# Private snapshot dir — never under /config/www (see module docstring).
SNAPSHOT_DIR = "/config/nova/intrusion"
_LEGACY_SNAPSHOT_DIR = "/config/www/nova/intrusion"  # pre-v7.102.0 location
_MAX_SNAPSHOTS = 40           # keep the last N, prune older

# Call-off state (module-level; the investigation itself lives in SafetyManager)
_called_off_until = 0.0       # suppress escalation until this ts
_CALLOFF_COOLDOWN = 600.0     # 10 min quiet after a false-alarm call-off
_acknowledged_ts = 0.0        # user said "I see it, stand by" (not a false alarm)
_ACK_WINDOW = 300.0           # an acknowledgement holds the auto-escalation this long
_last_snapshot: dict = {}     # {path, url, camera, ts} of the most recent capture
_false_alarms: list = []      # recent {ts, camera, area} for learning
_last_decision_id: Optional[int] = None  # decision_record row id of the latest intrusion


def acknowledge(reason: str = "") -> dict:
    """User acknowledges the alert ('I see it', 'I'm looking', 'standby') WITHOUT
    declaring it a false alarm. This holds the no-response auto-escalation for a
    window — the user is handling it — but does NOT suppress evidence-based
    escalation (if a person appears on camera, Nova still alerts). Never
    raises."""
    global _acknowledged_ts
    _acknowledged_ts = time.time()
    _LOGGER.info("Nova: intrusion acknowledged by user%s — holding auto-escalation",
                 f" ({reason})" if reason else "")
    return {"ok": True, "held_seconds": int(_ACK_WINDOW)}


def is_acknowledged() -> bool:
    """Whether a recent user acknowledgement is holding the no-response timeout."""
    return (time.time() - _acknowledged_ts) < _ACK_WINDOW


async def capture_snapshot(hass, camera_entity: str,
                           tag: str = "intrusion") -> Optional[dict]:
    """Grab a still from camera_entity and write it to the private snapshot
    dir. Returns {path, camera, ts} or None. Never raises."""
    if not camera_entity:
        return None
    try:
        from homeassistant.components.camera import async_get_image as _get_image
        image = await _get_image(hass, camera_entity, timeout=10)
        content = getattr(image, "content", None)
        if not content:
            return None
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        ts = int(time.time())
        slug = camera_entity.split(".", 1)[-1]
        fname = f"{tag}_{slug}_{ts}.jpg"
        path = os.path.join(SNAPSHOT_DIR, fname)
        await hass.async_add_executor_job(_write_bytes, path, content)
        _prune_old()
        info = {
            "path": path,
            "camera": camera_entity,
            "ts": ts,
        }
        global _last_snapshot
        _last_snapshot = info
        _LOGGER.info("Nova: intrusion snapshot saved from %s → %s",
                     camera_entity, path)
        return info
    except Exception as exc:
        _LOGGER.debug("intrusion snapshot failed for %s: %s", camera_entity, exc)
        return None


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _read_b64(path: str) -> Optional[str]:
    """Read a snapshot file and return its base64-encoded bytes, or None.
    Refuses to read anything outside SNAPSHOT_DIR — path comes from our own
    stored metadata, not user input, but this costs nothing and closes off
    a path-traversal class of bug outright. Never raises."""
    try:
        if not path:
            return None
        real_dir = os.path.realpath(SNAPSHOT_DIR)
        real_path = os.path.realpath(path)
        if os.path.commonpath([real_dir, real_path]) != real_dir:
            _LOGGER.warning("intrusion: refused to read snapshot outside %s: %s",
                            SNAPSHOT_DIR, path)
            return None
        with open(real_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception as exc:
        _LOGGER.debug("intrusion: snapshot read failed for %s: %s", path, exc)
        return None


async def get_snapshot_b64(hass, path: str) -> Optional[str]:
    """Async wrapper for _read_b64 — file I/O off the event loop."""
    return await hass.async_add_executor_job(_read_b64, path)


def _notify_ttl_minutes(hass) -> float:
    """How long a notification's signed image copy lives before being
    deleted. Configurable via `intrusion_notify_image_ttl_minutes`
    (Settings → Devices & Services → Nova → Configure)."""
    try:
        from . import nova_config
        v = nova_config.get("intrusion_notify_image_ttl_minutes",
                            _DEFAULT_NOTIFY_IMAGE_TTL_MIN)
        return max(1.0, float(v))
    except Exception:
        return _DEFAULT_NOTIFY_IMAGE_TTL_MIN


def _copy_for_notify(snapshot_path: str, sub_dir: str) -> Optional[str]:
    """Copy the private snapshot into HA's local media directory under a
    fresh, unguessable filename. Returns the filename, or None on failure.
    Runs in the executor."""
    try:
        os.makedirs(sub_dir, exist_ok=True)
        fname = f"{secrets.token_urlsafe(16)}.jpg"
        dst = os.path.join(sub_dir, fname)
        with open(snapshot_path, "rb") as src_f:
            data = src_f.read()
        with open(dst, "wb") as dst_f:
            dst_f.write(data)
        return fname
    except Exception as exc:
        _LOGGER.debug("intrusion: notification image copy failed: %s", exc)
        return None


def _delete_notify_copy(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


async def get_notification_image_url(hass, snapshot_path: str) -> Optional[str]:
    """Return a short-lived, cryptographically SIGNED url for a mobile push
    notification's image attachment. Never returns the persistent, private
    archive path directly and never falls back to the old /config/www /
    /local/... mechanism.

    Phones fetch a push notification's image attachment directly (iOS/
    Android — no Authorization header support), so the file has to be
    reachable by a plain URL. Rather than storing it anywhere permanently
    unauthenticated, this uses Home Assistant's own media_source support:
    the image is copied into HA's configured local media directory (under
    an unguessable, per-notification filename) and served at /media/...,
    which — unlike /config/www's /local/... — is a real HomeAssistantView
    that requires either a normal auth token or a valid signed path
    (homeassistant.components.http.auth.async_sign_path). The signed URL
    this returns expires after `_notify_ttl_minutes()`, and the copy is
    deleted from disk on that same schedule regardless of whether the
    signature was ever used. The permanent, private archive in
    SNAPSHOT_DIR is completely untouched by any of this.

    Returns None (never raises) if no local media directory is configured,
    or on any failure — the notification still sends, just without a photo.
    """
    if not snapshot_path:
        return None
    try:
        media_dirs = getattr(hass.config, "media_dirs", None) or {}
        source_dir_id = "local" if "local" in media_dirs else next(iter(media_dirs), None)
        if not source_dir_id:
            _LOGGER.debug("intrusion: no local media directory configured; "
                          "notification will send without a photo")
            return None
        sub_dir = os.path.join(media_dirs[source_dir_id], "nova_intrusion")

        fname = await hass.async_add_executor_job(_copy_for_notify, snapshot_path, sub_dir)
        if not fname:
            return None
        dst_path = os.path.join(sub_dir, fname)

        ttl_minutes = _notify_ttl_minutes(hass)
        try:
            from homeassistant.components.http.auth import async_sign_path
            url_path = f"/media/{source_dir_id}/nova_intrusion/{fname}"
            signed_url = async_sign_path(hass, url_path, timedelta(minutes=ttl_minutes))
        except Exception as exc:
            _LOGGER.debug("intrusion: signing notification image path failed: %s", exc)
            await hass.async_add_executor_job(_delete_notify_copy, dst_path)
            return None

        try:
            from homeassistant.helpers.event import async_call_later
            async_call_later(hass, ttl_minutes * 60,
                             lambda _now: hass.async_add_executor_job(
                                 _delete_notify_copy, dst_path))
        except Exception as exc:
            _LOGGER.debug("intrusion: could not schedule notification image "
                          "cleanup, deleting immediately as a fallback: %s", exc)
            await hass.async_add_executor_job(_delete_notify_copy, dst_path)
            return None

        return signed_url
    except Exception as exc:
        _LOGGER.debug("intrusion: get_notification_image_url failed: %s", exc)
        return None


def _prune_old() -> None:
    try:
        files = [
            os.path.join(SNAPSHOT_DIR, f)
            for f in os.listdir(SNAPSHOT_DIR)
            if f.endswith(".jpg")
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[_MAX_SNAPSHOTS:]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass


def migrate_legacy_snapshots() -> dict:
    """One-time move of any snapshots left under the old, unauthenticated
    /config/www/nova/intrusion location (pre-v7.102.0) to the private
    SNAPSHOT_DIR, and repoint any log entries that still reference the old
    path. Files are moved, never deleted — if a destination file somehow
    already exists, the legacy copy is left in place rather than overwritten
    or dropped. Safe to call on every startup: a no-op once nothing legacy
    remains. Never raises."""
    moved = 0
    skipped = 0
    try:
        if not os.path.isdir(_LEGACY_SNAPSHOT_DIR):
            return {"moved": 0, "skipped": 0}
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        for fname in os.listdir(_LEGACY_SNAPSHOT_DIR):
            if not fname.endswith(".jpg"):
                continue
            src = os.path.join(_LEGACY_SNAPSHOT_DIR, fname)
            dst = os.path.join(SNAPSHOT_DIR, fname)
            try:
                if os.path.exists(dst):
                    skipped += 1
                    continue
                os.rename(src, dst)
                moved += 1
            except Exception as exc:
                _LOGGER.warning("intrusion: could not migrate legacy snapshot %s: %s",
                                fname, exc)
                skipped += 1
        if moved:
            _load_log()
            changed = False
            for ev in _log:
                p = ev.get("snapshot_path") or ""
                if p.startswith(_LEGACY_SNAPSHOT_DIR):
                    ev["snapshot_path"] = p.replace(_LEGACY_SNAPSHOT_DIR, SNAPSHOT_DIR, 1)
                    changed = True
            if changed:
                _save_log()
            _LOGGER.warning(
                "Nova: migrated %d intrusion snapshot(s) from the old, "
                "unauthenticated %s to %s (%d left in place)%s",
                moved, _LEGACY_SNAPSHOT_DIR, SNAPSHOT_DIR, skipped,
                " — remaining legacy files were left untouched" if skipped else "",
            )
    except Exception as exc:
        _LOGGER.debug("intrusion: legacy snapshot migration skipped: %s", exc)
    return {"moved": moved, "skipped": skipped}


def last_snapshot() -> Optional[dict]:
    """The most recent intrusion snapshot info, or None."""
    return _last_snapshot or None


# ── false-alarm call-off ─────────────────────────────────────────────────────

def set_last_decision_id(record_id) -> None:
    """Remember the decision_record row id of the most recent intrusion, so a
    later call-off attaches its outcome to *that* record rather than guessing
    the most-recent-of-kind (which mis-attributes when intrusions overlap)."""
    global _last_decision_id
    if record_id is not None:
        try:
            _last_decision_id = int(record_id)
        except Exception:
            pass


def dismiss_intrusion(reason: str = "") -> dict:
    """Declare the current/last intrusion a false alarm. Sets a suppression
    window so the SafetyManager stops escalating, and records it. The
    SafetyManager consults is_called_off() and clears its investigation. Never
    raises."""
    global _called_off_until, _last_decision_id
    _called_off_until = time.time() + _CALLOFF_COOLDOWN
    rec = {
        "ts": int(time.time()),
        "reason": str(reason or ""),
        "camera": (_last_snapshot or {}).get("camera", ""),
    }
    _false_alarms.append(rec)
    if len(_false_alarms) > 50:
        del _false_alarms[:-50]
    _LOGGER.info("Nova: intrusion called off by user%s — suppressing escalation "
                 "for %ds", f" ({reason})" if reason else "", int(_CALLOFF_COOLDOWN))
    try:  # Decision Record outcome: a called-off intrusion was a false alarm.
        from . import decision_record
        if _last_decision_id is not None:
            # Attach the verdict to the exact record for this intrusion.
            if not decision_record.set_outcome(
                    _last_decision_id, "wrong", "dismiss_intrusion"):
                # Already judged or gone — fall back to most-recent-of-kind.
                decision_record.set_outcome_recent(
                    "intrusion", "wrong", "dismiss_intrusion", max_age=7200.0)
            _last_decision_id = None
        else:
            decision_record.set_outcome_recent(
                "intrusion", "wrong", "dismiss_intrusion", max_age=7200.0)
    except Exception:
        pass
    return {"ok": True, "suppressed_seconds": int(_CALLOFF_COOLDOWN), "recorded": rec}


def is_called_off() -> bool:
    """Whether a user call-off is currently suppressing escalation."""
    return time.time() < _called_off_until


def clear_calloff() -> None:
    """Reset the suppression (e.g. on a genuinely new, unrelated trigger)."""
    global _called_off_until, _acknowledged_ts
    _called_off_until = 0.0
    _acknowledged_ts = 0.0


def false_alarm_count(within_seconds: float = 86400.0) -> int:
    """How many false alarms the user has called off recently (for learning /
    threshold tuning). Default window: 24h."""
    cutoff = time.time() - within_seconds
    return sum(1 for r in _false_alarms if r.get("ts", 0) >= cutoff)


def status() -> dict:
    """Snapshot + call-off status for the panel/agent."""
    return {
        "last_snapshot": _last_snapshot or None,
        "called_off": is_called_off(),
        "acknowledged": is_acknowledged(),
        "suppressed_for": max(0, int(_called_off_until - time.time())),
        "false_alarms_24h": false_alarm_count(),
    }


# ── event log + labeling + learning (v6.76.0) ────────────────────────────────
# A reviewable history of every intrusion event with its snapshot, which the
# user can label real/false. Those labels feed a narrow suppression: a pattern
# repeatedly labelled a false alarm stops firing the LOW-CONFIDENCE alerts (the
# initial "investigating" ping and the unanswered "unresolved" notice).
#
# HARD SAFETY RULE: learning may NEVER suppress a CONFIRMED intrusion — a person
# confirmed on camera by vision, or motion tracing a real inward route, always
# alerts regardless of how many times a pattern was called a false alarm. The
# learning only damps the noisy, unconfirmed path.

LOG_PATH = Path("/config/nova/intrusion_log.json")
_MAX_LOG = 200                 # keep the last N events
_LEARN_WINDOW = 30 * 86400.0   # labels older than this stop counting
_LEARN_MIN_FALSE = 3           # this many false labels ⇒ damp the weak alerts
_log: list = []
_log_loaded = False


def _load_log() -> list:
    """Read the persisted event log (once per run). Never raises."""
    global _log, _log_loaded
    if _log_loaded:
        return _log
    _log_loaded = True
    try:
        if LOG_PATH.exists():
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _log = data[-_MAX_LOG:]
    except Exception as exc:
        _LOGGER.debug("intrusion log: load failed: %s", exc)
        _log = []
    return _log


def _save_log() -> None:
    """Persist the event log. Never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(_log[-_MAX_LOG:], f, indent=2, default=str)
    except Exception as exc:
        _LOGGER.debug("intrusion log: save failed: %s", exc)


def _pattern_key(area: Optional[str], camera: Optional[str],
                 ts: Optional[float] = None) -> str:
    """Group events by where + roughly when, so 'the kitchen window in the
    afternoon' is one learnable pattern."""
    where = (area or camera or "unknown").strip().lower()
    hour = time.localtime(ts or time.time()).tm_hour
    bucket = hour // 3           # 8 three-hour buckets across the day
    return f"{where}|{bucket}"


def record_event(kind: str, reason: str = "", breach: Optional[str] = None,
                 breach_area: Optional[str] = None, camera: Optional[str] = None,
                 snapshot: Optional[dict] = None, zones: Optional[list] = None,
                 max_depth: Optional[int] = None) -> dict:
    """Append an intrusion event to the reviewable log. kind is one of
    'investigating' | 'unresolved' | 'confirmed' | 'false_alarm'. Never raises."""
    _load_log()
    ts = time.time()
    ev = {
        "id": f"evt_{int(ts)}_{len(_log)}",
        "ts": ts,
        "kind": str(kind or "investigating"),
        "reason": reason or "",
        "breach": breach or "",
        "breach_area": breach_area or "",
        "camera": camera or (snapshot or {}).get("camera") or "",
        "snapshot_path": (snapshot or {}).get("path") or "",
        "zones": list(zones or []),
        "max_depth": max_depth,
        "label": None,
        "pattern": _pattern_key(breach_area, camera, ts),
    }
    _log.append(ev)
    if len(_log) > _MAX_LOG:
        del _log[:-_MAX_LOG]
    _save_log()
    return ev


def get_log(limit: int = 50) -> list:
    """Most recent events first, for the panel."""
    _load_log()
    try:
        limit = max(1, min(int(limit), _MAX_LOG))
    except (ValueError, TypeError):
        limit = 50
    return list(reversed(_log[-limit:]))


def label_event(event_id: str, label: str) -> dict:
    """Mark an event 'real' or 'false' (or None to clear). This is the training
    signal. Never raises."""
    _load_log()
    if label not in ("real", "false", None, ""):
        return {"ok": False, "error": "label must be 'real' or 'false'"}
    label = label or None
    for ev in _log:
        if ev.get("id") == event_id:
            ev["label"] = label
            ev["labeled_ts"] = time.time()
            _save_log()
            return {"ok": True, "id": event_id, "label": label}
    return {"ok": False, "error": "event not found"}


def pattern_verdict(area: Optional[str], camera: Optional[str],
                    ts: Optional[float] = None) -> dict:
    """How this location/time pattern has been labelled historically.
    {false_count, real_count, damp} — damp=True means the weak alerts for this
    pattern should stay quiet. A single 'real' label anywhere in the window
    cancels damping outright: if it was ever genuinely an intruder here, we do
    not learn to ignore it."""
    _load_log()
    key = _pattern_key(area, camera, ts)
    cutoff = time.time() - _LEARN_WINDOW
    false_n = real_n = 0
    for ev in _log:
        if ev.get("pattern") != key or ev.get("ts", 0) < cutoff:
            continue
        if ev.get("label") == "false":
            false_n += 1
        elif ev.get("label") == "real":
            real_n += 1
    damp = (real_n == 0) and (false_n >= _LEARN_MIN_FALSE)
    return {"false_count": false_n, "real_count": real_n, "damp": damp,
            "pattern": key}


def should_damp_weak_alert(area: Optional[str], camera: Optional[str]) -> bool:
    """True when the low-confidence alerts for this pattern should stay quiet.
    NEVER consulted for a confirmed intrusion — only for the initial
    'investigating' ping and the unanswered 'unresolved' notice."""
    try:
        return bool(pattern_verdict(area, camera).get("damp"))
    except Exception:
        return False


def learning_summary() -> dict:
    """Panel summary of what Nova has learned from labels."""
    _load_log()
    labeled = [e for e in _log if e.get("label")]
    patterns: dict = {}
    for e in labeled:
        p = e.get("pattern") or "?"
        d = patterns.setdefault(p, {"false": 0, "real": 0})
        d[e["label"]] = d.get(e["label"], 0) + 1
    damped = [p for p, d in patterns.items()
              if d.get("real", 0) == 0 and d.get("false", 0) >= _LEARN_MIN_FALSE]
    return {
        "events": len(_log),
        "labeled": len(labeled),
        "patterns": patterns,
        "damped_patterns": damped,
        "min_false_to_damp": _LEARN_MIN_FALSE,
    }
