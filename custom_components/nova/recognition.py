"""
Nova — who has been seen, and on which camera.

Identity reaches Nova from up to three MQTT feeds and one set of sensors:

* Double Take publishes matches on ``double-take/matches``.
* Frigate publishes person events on ``frigate/events``; a recognised face may
  ride along as the event's ``sub_label``.
* Newer Frigate releases publish face results on
  ``frigate/tracked_object_update`` (``{"type": "face", "name", "score",
  "camera"}``), the more dependable of the two Frigate routes.
* Frigate's Home Assistant integration keeps a
  ``sensor.<camera>_last_recognized_face`` per camera, read on demand.

The ``recognition_source`` setting (off, doubletake, frigate or both) picks
which feeds may report identity. Person detection on ``frigate/events`` runs
whatever it says. Every reported identity lands in the same places: the per
camera cache, the recognition log and a ``nova_face_recognized`` event, so the
code downstream never needs to know which backend spoke.

Only the backends write identity here. Nothing a person types or says can.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

MATCHES_TOPIC = "double-take/matches"
CAMERAS_TOPIC = "double-take/cameras"
TRACKED_OBJECT_TOPIC = "frigate/tracked_object_update"
_FRIGATE_EVENTS_TOPIC = "frigate/events"

# Latest result per camera entity:
# {"name", "confidence" (percent), "ts" (naive UTC), "unknown_count"}.
_RECOGNITION_CACHE: dict[str, dict] = {}
# Latest Frigate person event per camera entity, kept for snapshot lookups.
_RECENT_EVENTS: dict[str, dict] = {}
CACHE_MAX_AGE = timedelta(hours=2)
CONFIDENCE_THRESHOLD = 60  # percent; below this a match is uncertain

# How far back the Faces tab looks.
RECENT_FACES_MAX_AGE_S = 24 * 3600

# Every identity a backend reported, oldest first. The cache only holds the
# newest result per camera, which would let a resident hide a stranger seen a
# moment earlier; the intrusion stand down and the Faces tab read this log
# instead. remember_recognition() is its only writer.
_FACE_LOG: deque = deque(maxlen=300)
# Each Frigate person detection, named or not.
_PERSON_LOG: deque = deque(maxlen=100)

# Labels the backends give a face they could not put a name to.
UNKNOWN_NAMES = ("unknown", "unknown person", "unknown_face", "unrecognized",
                 "unrecognised")
_BLANK_NAMES = ("", "none", "null")

# Sensor states meaning "no recognised person".
_FACE_SENSOR_EMPTY = ("none", "unknown", "unavailable", "unknown_face", "")
_FACE_SENSOR_SUFFIX = "_last_recognized_face"
_SENSOR_PREFIX = "sensor."


def is_unknown_name(name) -> bool:
    """Whether ``name`` is blank or one of the "unknown face" labels."""
    label = str(name or "").strip().lower()
    return label in UNKNOWN_NAMES or label in _BLANK_NAMES


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_score(raw) -> float:
    """A score as a percent to one decimal place. Values up to 1.0 are taken
    as fractions; anything unreadable is 0.0."""
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 1.0:
        value *= 100.0
    return round(value, 1)


def _sensor_score(attrs) -> float:
    if "score" in attrs:
        return _normalize_score(attrs["score"])
    if "confidence" in attrs:
        return _normalize_score(attrs["confidence"])
    return _normalize_score(attrs.get("sub_label_score"))


def _sensor_camera(entity_id: str) -> str:
    return entity_id[len(_SENSOR_PREFIX):len(entity_id) - len(_FACE_SENSOR_SUFFIX)]


def read_frigate_face_sensors(hass) -> list[dict]:
    """Known people named by Frigate's last recognised face sensors, as
    ``{camera, camera_entity, name, entity, confidence}``.

    These sensors keep the last face until another replaces it, so a row means
    "most recently seen", not "in view now". Never raises."""
    try:
        sensors = hass.states.async_all("sensor")
    except Exception:  # noqa: BLE001
        return []
    found: list[dict] = []
    for st in sensors:
        try:
            entity_id = st.entity_id
            if not entity_id.endswith(_FACE_SENSOR_SUFFIX):
                continue
            name = str(st.state or "").strip()
            if name.lower() in _FACE_SENSOR_EMPTY:
                continue
            camera = _sensor_camera(entity_id)
            found.append({
                "camera": camera,
                "camera_entity": "camera." + camera,
                "name": name,
                "entity": entity_id,
                "confidence": _sensor_score(st.attributes or {}),
            })
        except Exception:  # noqa: BLE001 - one odd sensor never hides the rest
            continue
    return found


def _fresh(ts, now: datetime) -> bool:
    return not ts or now - ts <= CACHE_MAX_AGE


def who_do_you_see(hass) -> dict:
    """Everyone Nova can currently name, for "can you recognise me".

    Sensor rows come first (all of them, in ``detail``), then any person in the
    cache within CACHE_MAX_AGE who is not already listed. Returns
    ``{"seen": [names], "detail": [...], "any": bool}``."""
    seen: list[str] = []
    detail: list[dict] = []
    for row in read_frigate_face_sensors(hass):
        detail.append(dict(row, source="frigate_sensor"))
        if row["name"] not in seen:
            seen.append(row["name"])
    now = _utcnow()
    for camera_entity, entry in _RECOGNITION_CACHE.items():
        name = entry.get("name", "")
        if not name or name.lower() in _FACE_SENSOR_EMPTY:
            continue
        if not _fresh(entry.get("ts"), now) or name in seen:
            continue
        seen.append(name)
        detail.append({"camera_entity": camera_entity, "name": name,
                       "confidence": entry.get("confidence", 0.0),
                       "source": "recent_cache"})
    return {"seen": seen, "detail": detail, "any": len(seen) > 0}


def _parse_sub_label(sub) -> tuple[str, float]:
    """Frigate's ``sub_label`` as ``(name, percent)``.

    Accepts a bare name or a ``[name, score]`` sequence. Anything else, or a
    score that is not a number, gives ``("", 0.0)``."""
    if not sub:
        return "", 0.0
    if isinstance(sub, str):
        return sub.strip(), 0.0
    if not isinstance(sub, (list, tuple)):
        return "", 0.0
    name = str(sub[0] or "").strip()
    score = sub[1] if len(sub) > 1 else None
    if score is None:
        return name, 0.0
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "", 0.0
    if value <= 1.0:
        value *= 100.0
    return name, round(value, 1)


def _camera_entity_from_name(camera_name: str) -> str:
    """Frigate and Double Take name cameras like ``front_door``; Home Assistant
    calls the same camera ``camera.front_door``."""
    return "camera." + camera_name.lower()


def remember_recognition(camera_name: str, name: str, confidence: float,
                         source: str = "") -> None:
    """Record one backend result in the recognition log and the camera's cache
    entry. ``source`` names the backend."""
    camera_entity = _camera_entity_from_name(camera_name)
    stamp = _utcnow()
    try:
        _FACE_LOG.append({"camera_entity": camera_entity, "name": str(name or ""),
                          "confidence": float(confidence or 0.0), "ts": stamp,
                          "source": source})
    except Exception:  # noqa: BLE001
        pass
    if name.lower() == "unknown":
        misses = _RECOGNITION_CACHE.get(camera_entity, {}).get("unknown_count", 0) + 1
    else:
        misses = 0
    _RECOGNITION_CACHE[camera_entity] = {"name": name, "confidence": confidence,
                                         "ts": stamp, "unknown_count": misses}
    _schedule_save()


# ── keeping the recognition log across a restart (8.58.7) ───────────────────
# Names, cameras, scores and times only, never an image. Written at most once
# every SAVE_EVERY_S seconds, and only after restore() has run at setup, so
# nothing is written until Nova has loaded what was saved.
FACE_LOG_FILE = "recent_faces.json"
SAVE_EVERY_S = 30
SAVE_KEEP = 100
_HASS = None
_SAVE_HANDLE = None


def _log_path() -> str:
    from . import paths
    return paths.nova_path(FACE_LOG_FILE)


def _stamp_out(entry: dict) -> dict:
    return {"camera_entity": entry["camera_entity"], "name": entry["name"],
            "confidence": entry["confidence"], "source": entry.get("source") or "",
            "ts": entry["ts"].replace(tzinfo=timezone.utc).isoformat()}


def _write_log(rows: list) -> None:
    from .persistence.files import write_json_atomic
    write_json_atomic(_log_path(), {"faces": rows}, indent=2, mode=0o600)


def _read_log() -> list:
    """Saved sightings from the last day, oldest first. Anything unreadable or
    odd is dropped; a missing or corrupt file gives an empty list."""
    from .persistence.files import OK, read_json
    res = read_json(_log_path())
    items = res.value.get("faces") if res.status == OK and isinstance(res.value, dict) else None
    now = _utcnow()
    kept: list = []
    for raw in items if isinstance(items, list) else []:
        try:
            ts = datetime.fromisoformat(str(raw["ts"]))
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts
            camera = str(raw["camera_entity"])
            name = str(raw["name"])
            if (not name or not camera.startswith("camera.") or ts > now
                    or (now - ts).total_seconds() > RECENT_FACES_MAX_AGE_S):
                continue
            kept.append({"camera_entity": camera, "name": name[:80],
                         "confidence": float(raw.get("confidence") or 0.0), "ts": ts,
                         "source": str(raw.get("source") or "")[:20]})
        except Exception:  # noqa: BLE001
            continue
    return kept[-SAVE_KEEP:]


def _save_now() -> None:
    global _SAVE_HANDLE
    _SAVE_HANDLE = None
    hass = _HASS
    if hass is None:
        return
    try:
        rows = [_stamp_out(e) for e in list(_FACE_LOG)[-SAVE_KEEP:]]
        hass.async_add_executor_job(_write_log, rows)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("recognition: could not save the recent faces: %s", exc)


def _arm() -> None:
    global _SAVE_HANDLE
    if _HASS is not None and _SAVE_HANDLE is None:
        _SAVE_HANDLE = _HASS.loop.call_later(SAVE_EVERY_S, _save_now)


def _schedule_save() -> None:
    """Ask for a save in SAVE_EVERY_S seconds, from any thread."""
    hass = _HASS
    if hass is None or _SAVE_HANDLE is not None:
        return
    try:
        hass.loop.call_soon_threadsafe(_arm)
    except Exception:  # noqa: BLE001
        pass


async def restore(hass) -> None:
    """Load the saved sightings into the log (older than anything new), then
    start saving. Never raises."""
    global _HASS
    try:
        saved = await hass.async_add_executor_job(_read_log)
        if saved and not _FACE_LOG:
            _FACE_LOG.extend(saved)
            last: dict = {}
            for entry in saved:
                last[entry["camera_entity"]] = entry
            for camera_entity, entry in last.items():
                _RECOGNITION_CACHE.setdefault(camera_entity, {
                    "name": entry["name"], "confidence": entry["confidence"],
                    "ts": entry["ts"], "unknown_count": 0})
        _LOGGER.debug("recognition: restored %d recent sightings", len(saved))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("recognition: could not restore the recent faces: %s", exc)
    _HASS = hass


def note_person_detected(camera_name: str) -> None:
    """Record a Frigate person detection on this camera."""
    try:
        _PERSON_LOG.append({"camera_entity": _camera_entity_from_name(camera_name),
                            "ts": _utcnow()})
    except Exception:  # noqa: BLE001
        pass


def _within(log: deque, window_s: float) -> list[dict]:
    try:
        now = _utcnow()
        picked = []
        for entry in list(log):
            age = (now - entry["ts"]).total_seconds()
            if 0 <= age <= window_s:
                picked.append(dict(entry, age_seconds=int(age)))
        return picked
    except Exception:  # noqa: BLE001
        return []


def recent_face_events(window_s: float) -> list[dict]:
    """Logged recognitions from the last ``window_s`` seconds, oldest first,
    each with ``age_seconds``. Never raises."""
    return _within(_FACE_LOG, window_s)


def recent_person_detections(window_s: float) -> list[dict]:
    """Logged person detections from the last ``window_s`` seconds. Never raises."""
    return _within(_PERSON_LOG, window_s)


def last_seen_at(hass: HomeAssistant, camera_entity: str) -> Optional[dict]:
    """The camera's cache entry plus ``age_seconds`` and ``camera_entity``,
    or None when there is none within CACHE_MAX_AGE."""
    entry = _RECOGNITION_CACHE.get(camera_entity)
    if not entry:
        return None
    age = _utcnow() - entry["ts"]
    if age > CACHE_MAX_AGE:
        return None
    result = dict(entry)
    result["age_seconds"] = int(age.total_seconds())
    result["camera_entity"] = camera_entity
    return result


def who_is_where(hass: HomeAssistant) -> dict[str, str]:
    """``{camera_entity: name}`` for confident results within CACHE_MAX_AGE."""
    oldest = _utcnow() - CACHE_MAX_AGE
    return {camera_entity: entry["name"]
            for camera_entity, entry in _RECOGNITION_CACHE.items()
            if entry["ts"] >= oldest and entry["confidence"] >= CONFIDENCE_THRESHOLD}


def _ago(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    return "%dh ago" % (seconds // 3600)


def recognition_context_string(hass: HomeAssistant) -> str:
    """One line for the agent's system prompt naming recent faces, from the
    cache first and then the Frigate sensors. Empty when there are none."""
    parts: list[str] = []
    named: set[str] = set()
    now = _utcnow()
    for camera_entity, name in who_is_where(hass).items():
        age = int((now - _RECOGNITION_CACHE[camera_entity]["ts"]).total_seconds())
        where = camera_entity.replace("camera.", "").replace("_", " ")
        parts.append("%s seen at %s (%s)" % (name, where, _ago(age)))
        named.add(name.lower())
    try:
        for row in read_frigate_face_sensors(hass):
            name = row.get("name", "")
            if not name or name.lower() in named:
                continue
            score = row.get("confidence", 0)
            suffix = " ~%.0f%%" % score if score else ""
            parts.append("%s recognized at %s%s" % (name, row["camera"].replace("_", " "),
                                                     suffix))
            named.add(name.lower())
    except Exception:  # noqa: BLE001
        pass
    return "Recent faces: " + "; ".join(parts) + "." if parts else ""


def _roster_names() -> set[str]:
    try:
        from . import face_roster
        return face_roster.normalized_names()
    except Exception:  # noqa: BLE001
        return set()


def _sensor_age_seconds(st) -> Optional[int]:
    try:
        stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
        if stamp is None:
            return None
        if getattr(stamp, "tzinfo", None) is not None:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0, int((_utcnow() - stamp).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def _clamp_limit(limit) -> int:
    try:
        return min(100, max(1, int(limit)))
    except (TypeError, ValueError):
        return 20


def recent_faces(hass, limit: int = 20) -> list[dict]:
    """Faces for the Faces tab, newest first, at most ``limit`` (1 to 100).

    Sources are the recognition log, the per camera cache and the Frigate
    sensors. One row per person (by identity.normalize) and camera, the newest
    sighting winning:
    ``{name, camera, camera_entity, confidence, age_seconds, known, resident,
    source}``. ``known`` is False for an unknown label and ``resident`` means
    the person is on the face roster. Rows older than RECENT_FACES_MAX_AGE_S
    are left out. Names and numbers only, nothing visual. Never raises."""
    count = _clamp_limit(limit)
    try:
        from .identity import normalize
        residents = _roster_names()
        newest: dict[tuple[str, str], dict] = {}

        def consider(name, camera_entity, confidence, age, source):
            name = str(name or "").strip()
            if not name or age is None:
                return
            key = (normalize(name), camera_entity)
            held = newest.get(key)
            if held is not None and held["age_seconds"] <= age:
                return
            known = not is_unknown_name(name)
            newest[key] = {
                "name": name,
                "camera": camera_entity.replace("camera.", ""),
                "camera_entity": camera_entity,
                "confidence": round(float(confidence or 0.0), 1),
                "age_seconds": int(age),
                "known": known,
                "resident": known and normalize(name) in residents,
                "source": source,
            }

        now = _utcnow()
        for entry in list(_FACE_LOG):
            consider(entry["name"], entry["camera_entity"], entry["confidence"],
                     (now - entry["ts"]).total_seconds(), entry.get("source") or "mqtt")
        for camera_entity, entry in list(_RECOGNITION_CACHE.items()):
            consider(entry.get("name"), camera_entity, entry.get("confidence"),
                     (now - entry["ts"]).total_seconds(), "recent_cache")
        for st in hass.states.async_all("sensor"):
            entity_id = str(getattr(st, "entity_id", ""))
            if not entity_id.endswith(_FACE_SENSOR_SUFFIX):
                continue
            name = str(st.state or "").strip()
            if name.lower() in _FACE_SENSOR_EMPTY:
                continue
            consider(name, "camera." + _sensor_camera(entity_id),
                     _sensor_score(st.attributes or {}), _sensor_age_seconds(st),
                     "frigate_sensor")
        rows = sorted(newest.values(), key=lambda row: row["age_seconds"])
        rows = [row for row in rows if row["age_seconds"] <= RECENT_FACES_MAX_AGE_S]
        return rows[:count]
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("recognition: recent_faces failed: %s", exc)
        return []


def source_status(hass) -> dict[str, Any]:
    """Which identity sources are in reach, for the Faces tab's empty state.
    Never raises."""
    status: dict[str, Any] = {"recognition_source": "off", "mqtt": False,
                              "frigate_sensors": 0, "seen_recently": len(_FACE_LOG) > 0}
    try:
        from . import nova_config
        chosen = nova_config.get("recognition_source", "off")
        status["recognition_source"] = str(chosen or "off").lower()
    except Exception:  # noqa: BLE001
        pass
    try:
        status["mqtt"] = bool(hass.services.has_service("mqtt", "publish"))
    except Exception:  # noqa: BLE001
        pass
    try:
        status["frigate_sensors"] = len([
            st for st in hass.states.async_all("sensor")
            if str(getattr(st, "entity_id", "")).endswith(_FACE_SENSOR_SUFFIX)])
    except Exception:  # noqa: BLE001
        pass
    status["configured"] = bool(status["mqtt"] or status["frigate_sensors"]
                                or status["seen_recently"])
    return status


# ─── MQTT feeds ──────────────────────────────────────────────────────────────

def _identity_feeds() -> tuple[bool, bool]:
    """(Double Take allowed, Frigate identity allowed) from recognition_source.
    The default is off, so nothing reports identity until Faces is set up."""
    try:
        from . import nova_config
        chosen = str(nova_config.get("recognition_source", "off") or "off").lower()
    except Exception:  # noqa: BLE001
        chosen = "off"
    return chosen in ("both", "doubletake"), chosen in ("both", "frigate")


def _face_event(camera: str, camera_entity: str, name: str, confidence: float,
                is_unknown: bool) -> dict:
    return {"camera": camera, "camera_entity": camera_entity, "name": name,
            "confidence": confidence, "is_unknown": is_unknown,
            "is_confident": confidence >= CONFIDENCE_THRESHOLD}


async def _subscribe(mqtt, hass, topic: str, handler, unsubs: list, what: str,
                     loud: bool = False) -> None:
    try:
        unsubs.append(await mqtt.async_subscribe(hass, topic, handler))
    except Exception as exc:  # noqa: BLE001
        (_LOGGER.warning if loud else _LOGGER.debug)(
            "Nova: no subscription to %s (%s): %s", topic, what, exc)
        return
    _LOGGER.info("Nova: listening on %s for %s", topic, what)


async def register_recognition_listener(hass: HomeAssistant) -> list:
    """Subscribe to the identity feeds ``recognition_source`` allows, plus
    Frigate person detection. Returns the unsubscribe callables, none when
    MQTT is missing or not set up."""
    unsubs: list = []
    await restore(hass)
    try:
        from homeassistant.components import mqtt
    except ImportError:
        _LOGGER.info("Nova: no MQTT component, so no face recognition feeds")
        return unsubs
    if not hass.services.has_service("mqtt", "publish"):
        _LOGGER.info("Nova: MQTT is not set up, so no face recognition feeds")
        return unsubs

    doubletake_on, frigate_ids_on = _identity_feeds()

    @callback
    def on_doubletake(msg):
        body = msg.payload
        if isinstance(body, (str, bytes)):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                return
        camera = body.get("camera") or body.get("camera_name")
        match = body.get("match") or {}
        if not camera or not match:
            return
        name = match.get("name", "unknown")
        confidence = float(match.get("confidence", 0))
        remember_recognition(camera, name, confidence, source="doubletake")
        hass.bus.async_fire("nova_face_recognized", _face_event(
            camera, _camera_entity_from_name(camera), name, confidence,
            name.lower() == "unknown"))
        _LOGGER.info("Nova: Double Take named %s on %s (%.1f%%)", name, camera, confidence)

    async def on_frigate_event(msg):
        try:
            body = json.loads(msg.payload)
            after = body.get("after", {})
            label = after.get("label", "")
            camera = after.get("camera", "")
            score = after.get("top_score", 0)
            if body.get("type") != "new" or label != "person" or score < 0.7:
                return
            camera_entity = "camera." + camera
            event_id = after.get("id", "")
            _LOGGER.info("Nova: Frigate saw a person on %s (%.1f%%, event %s)",
                         camera, score * 100, event_id[:8])
            note_person_detected(camera)
            _RECENT_EVENTS[camera_entity] = {"event_id": event_id, "source": "frigate",
                                             "ts": datetime.now()}
            hass.bus.async_fire("nova_person_detected", {
                "camera": camera, "camera_entity": camera_entity, "label": label,
                "score": score, "event_id": event_id})
            if not frigate_ids_on:
                return
            name, confidence = _parse_sub_label(after.get("sub_label"))
            if not name:
                return
            remember_recognition(camera, name, confidence, source="frigate")
            event = _face_event(camera, camera_entity, name, confidence,
                                name.lower() in ("unknown", "unknown person"))
            event["source"] = "frigate"
            hass.bus.async_fire("nova_face_recognized", event)
            _LOGGER.info("Nova: Frigate sub_label named %s on %s (%.1f%%)",
                         name, camera, confidence)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: unreadable Frigate event: %s", exc)

    async def on_tracked_update(msg):
        try:
            body = json.loads(msg.payload)
            if body.get("type") != "face":
                return
            name = str(body.get("name") or "").strip()
            if name.lower() in _BLANK_NAMES:
                return
            camera = str(body.get("camera") or "").strip()
            confidence = _normalize_score(body.get("score"))
            unknown = name.lower() in ("unknown", "unknown person")
            if camera:
                remember_recognition(camera, name, confidence, source="frigate_tracked")
            event = _face_event(camera, "camera." + camera if camera else "", name,
                                confidence, unknown)
            event["source"] = "frigate_tracked_object"
            hass.bus.async_fire("nova_face_recognized", event)
            if not unknown:
                _LOGGER.info("Nova: Frigate face update named %s on %s (%.1f%%)",
                             name, camera or "?", confidence)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: unreadable Frigate face update: %s", exc)

    if doubletake_on:
        await _subscribe(mqtt, hass, MATCHES_TOPIC, on_doubletake, unsubs,
                         "Double Take matches", loud=True)
    else:
        _LOGGER.info("Nova: Double Take identity is off by recognition_source")
    await _subscribe(mqtt, hass, _FRIGATE_EVENTS_TOPIC, on_frigate_event, unsubs,
                     "Frigate person detection")
    if frigate_ids_on:
        await _subscribe(mqtt, hass, TRACKED_OBJECT_TOPIC, on_tracked_update, unsubs,
                         "Frigate face recognition")
    return unsubs
