"""
Nova — looking through the cameras.

Three jobs live here:

* Getting a usable frame. A camera backend (Frigate, Nest, Eufy and others in
  camera_backends) is asked first; then Home Assistant's own snapshot; then,
  for on demand streams that hand back black or placeholder frames while
  idle, the stream is woken and the snapshot tried once more.
* Analysing it. The frame (or a short clip tiled into one contact sheet) goes
  to the vision model, and a second camera reasoning model decides whether
  the scene deserves a mention. The result feeds memory, the observer, the
  briefing snapshot and semantic learning, and is spoken only when it should
  be.
* Reacting to camera events: Nest and Frigate events are cached so their
  recorded media can be fetched, doorbell presses get their own two pass
  analysis, and visitor sightings are logged silently for training.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall, Event, callback
from homeassistant.components.camera import async_get_image as camera_get_image
from homeassistant.helpers import entity_registry as er, device_registry as dr

from .camera_backends import find_backend
from .const import DEFAULT_VISION_MODEL, DOMAIN
from .database import save_message
from .directive_helper import build_system_prompt
from .tts_helper import async_announce

try:
    from .recognition import last_seen_at
    _RECOGNITION_AVAILABLE = True
except ImportError:
    _RECOGNITION_AVAILABLE = False

_LOGGER = logging.getLogger(__name__)

VISION_MODEL = DEFAULT_VISION_MODEL
_DEFAULT_REASONING_MODEL = "openai/gpt-oss-120b"

# A frame no bigger than this is broken or black.
MIN_IMAGE_SIZE = 2_000
# A streaming camera's "successful" snapshot no bigger than this is a
# placeholder thumbnail rather than a real frame, which runs to tens of KB.
SMALL_SUSPECT_SIZE = 12_000

# Latest Nest or Frigate event per camera entity, for fetching its media:
# {"event_id", "device_id", "ts" (naive UTC), "source": "nest" | "frigate"}.
_EVENT_CACHE: dict[str, dict] = {}
EVENT_FRESH_SECONDS = 120

_QUIET_CONTEXT = "quiet — no notable recent activity"
_DEFAULT_PROMPT = "Describe what you see. Note anything unusual or worth attention."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _nova_log(message: str) -> None:
    try:
        from .websocket import nova_log
        nova_log("CAMERA", message)
    except Exception:  # noqa: BLE001 - the panel log is best effort
        pass


# ─── Which model answers ─────────────────────────────────────────────────────

def _camera_entry(hass: HomeAssistant):
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _cfg_opt(hass: HomeAssistant, key: str, default=None):
    """One setting through the canonical resolver, live runtime values first.
    Reads the entry's runtime config, so it belongs on the event loop."""
    from . import nova_config
    return nova_config.runtime_get(hass, _camera_entry(hass), key, default)


async def _camera_config(hass: HomeAssistant) -> dict:
    """The whole effective configuration the camera roles pick their provider
    from (entry, panel config, secrets overlay, endpoint migration flag and
    live values). The runtime snapshot is taken here on the loop and the merge
    runs in the executor, fresh for each analysis."""
    from . import nova_config
    from .runtime import runtime_config_snapshot

    entry = _camera_entry(hass)
    if entry is None:
        snapshot = {}
    else:
        snapshot = runtime_config_snapshot(entry)
    return await hass.async_add_executor_job(
        nova_config.effective_config_with_runtime, entry, snapshot)


def _client_spec(config: dict, provider: str, model: str):
    """The ProviderSpec for a camera role, or None to use the fallback client.

    None when the provider is unknown, the model is blank, or a cloud provider
    has no credential of its own. Self hosted providers may run without one.
    The endpoint is the provider's own; a stale shared one is ignored after
    migration."""
    from .providers.registry import descriptor
    from .providers.routing import (
        ProviderSpec,
        resolve_provider_credential,
        resolve_provider_endpoint,
    )

    name = str(provider or "").strip().lower()
    info = descriptor(name)
    if info is None or not model:
        return None
    key = resolve_provider_credential(config, name)
    if not key and not info.self_hosted:
        return None
    return ProviderSpec(provider=name, model=str(model), api_key=key,
                        base_url=resolve_provider_endpoint(config, name))


@asynccontextmanager
async def _camera_client(hass: HomeAssistant, provider: str, model: str,
                         fallback, *, binding: str):
    """The client for one analysis in the role ``binding`` ("vision" or
    "camera_reasoning").

    The loaded entry's ProviderManager keeps it pooled under that binding,
    swaps it (closing the old one when idle) when the role's settings change
    and closes it on unload; with no loaded entry a short lived manager closes
    it afterwards. ``fallback`` is used whenever no client can be built."""
    from . import llm_provider
    from .providers.manager import provider_scope

    async with AsyncExitStack() as stack:
        client = fallback
        try:
            spec = _client_spec(await _camera_config(hass), provider, model)
            if spec is not None:
                manager = await stack.enter_async_context(
                    provider_scope(hass, entry=_camera_entry(hass)))

                def build():
                    return llm_provider.create_provider(
                        spec.provider, spec.api_key, spec.model, spec.base_url)
                client = await stack.enter_async_context(
                    manager.lease(spec, binding=binding, factory=build))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("camera: no %s/%s client (%s), using the fallback",
                            provider, model, type(exc).__name__)
            client = fallback
        yield client


def _vision_model_rejects_images(exc) -> bool:
    """Whether the vision call failed because the model only takes text."""
    from .providers.errors import rejects_images
    return rejects_images(exc)


# ─── Reading the models' answers ─────────────────────────────────────────────

_FENCE_OPEN = re.compile(r"^```[a-zA-Z]*\n?")
_OBJECT_SPAN = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_obj(raw: str):
    """The JSON in a model reply, tolerating code fences and chatter around
    a single object. None when nothing parses."""
    import json
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = _FENCE_OPEN.sub("", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    span = _OBJECT_SPAN.search(text)
    if span is None:
        return None
    try:
        return json.loads(span.group(0))
    except Exception:  # noqa: BLE001
        return None


_VISION_CATEGORY_MAP: dict[str, tuple[Optional[str], Optional[str]]] = {
    "delivery": ("package", "delivered"),
    "package": ("package", "delivered"),
    "mail": ("package", "mail"),
    "person": ("person", None),
    "known_resident": ("person", None),
    "vehicle": ("vehicle", None),
    "animal": ("animal", None),
    "other": ("activity", None),
}


def _map_vision_category(category: str) -> tuple[Optional[str], Optional[str]]:
    """The reasoning step's category as (semantic label, package state) for
    camera_semantic.record_event. "empty" and anything unexpected give
    (None, None), meaning nothing is recorded."""
    key = str(category or "").strip().lower()
    return _VISION_CATEGORY_MAP.get(key, (None, None))


def _guess_detection_type(prompt: str, analysis: str) -> str:
    said = (analysis or "").lower()
    if any(word in said for word in ("person", "someone", "individual")):
        return "person"
    if any(word in said for word in ("vehicle", "car", "truck")):
        return "vehicle"
    if "package" in said or "doorbell" in (prompt or "").lower():
        return "doorbell"
    return "motion"


_REASONING_RULES = (
    "You are Nova's camera-reasoning module. You are given a description of what "
    "a camera saw. Decide whether it is notable enough to mention to the resident. "
    "Routine or benign activity — a recognised resident arriving, an empty scene, "
    "a car passing on the public street — is NOT notable. Notable: an unrecognised "
    "person approaching or lingering, a delivery or package, mail, someone at an "
    "unusual hour, property damage, an animal where it shouldn't be, or anything "
    "that genuinely warrants attention. Be conservative about what you flag. "
    "Respond with ONLY a JSON object: "
    '{"notable": true|false, "category": '
    '"delivery|package|mail|person|known_resident|vehicle|animal|empty|other", '
    '"summary": "<one concise factual sentence for the log>", "speak": '
    '"<exactly what Nova should say aloud, in his voice, or empty string if not notable>"}'
)


def _recent_activity() -> str:
    try:
        from . import observer
        return observer.get_recent_context(600) or ""
    except Exception:  # noqa: BLE001
        return ""


async def _reason_about_scene(hass: HomeAssistant, reasoning_client, reasoning_model: str,
                              camera_name: str, description: str, det_type: str) -> dict:
    """Judge a vision description: {notable, category, summary, speak}.

    Any failure counts the scene as notable and speaks the description itself,
    so a reasoning fault can never swallow a real event."""
    stamp = datetime.now().strftime("%A %I:%M %p")
    recent = _recent_activity()
    from . import output_language
    # The spoken line follows Nova's language; the JSON keys stay as they are.
    system = _REASONING_RULES + output_language.field_directive(hass, "speak")
    lines = [f"Camera: {camera_name}", f"Time: {stamp}", f"Detection: {det_type}",
             f"Vision description: {description}", ""]
    user = "\n".join(lines)
    if recent and recent != _QUIET_CONTEXT:
        user += "\nRecent home activity:\n" + recent
    try:
        from .providers.activity import execute_chat
        reply = await execute_chat(
            hass, reasoning_client,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            role="camera_reasoning", data_category="text", max_tokens=220,
            temperature=0.3, model_override=reasoning_model or None,
        )
        verdict = _parse_json_obj((reply.text or "").strip())
        if isinstance(verdict, dict):
            line = str(verdict.get("speak", "") or "").strip()
            return {
                "notable": bool(verdict.get("notable", True)),
                "category": str(verdict.get("category", det_type) or det_type),
                "summary": str(verdict.get("summary", "") or description)[:300],
                "speak": line if line else None,
            }
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("camera: reasoning failed (%s), treating the scene as notable", exc)
    return {"notable": True, "category": det_type, "summary": description,
            "speak": description}


# ─── Which cameras, and whose ────────────────────────────────────────────────

def _disabled_cameras() -> set:
    """Cameras switched off in Settings, Cameras. Nova leaves these alone."""
    try:
        from . import nova_config
        value = nova_config.get("disabled_cameras")
        if isinstance(value, str) and value.strip():
            import json
            value = json.loads(value)
        if isinstance(value, (list, tuple)):
            return set(map(str, value))
    except Exception:  # noqa: BLE001
        pass
    return set()


def active_camera_states(hass) -> list:
    """States of the cameras Nova may use: every camera unless switched off.
    Every place that walks the cameras goes through here."""
    try:
        off = _disabled_cameras()
        return [st for st in hass.states.async_all("camera") if st.entity_id not in off]
    except Exception:  # noqa: BLE001
        return []


def active_cameras(hass) -> list:
    """Entity ids of the cameras Nova may use."""
    return [st.entity_id for st in active_camera_states(hass)]


_PLATFORM_KIND = {"frigate": "frigate", "nest": "nest", "eufy_security": "eufy"}


def _camera_integration(hass: HomeAssistant, entity_id: str) -> str:
    """'frigate', 'nest', 'eufy' or 'other', from the entity's platform."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return "other"
    return _PLATFORM_KIND.get(entry.platform, "other")


async def _fetch_event_media_image(hass: HomeAssistant, entity_id: str) -> Optional[bytes]:
    """The frame recorded at the moment of the last event, where the backend
    keeps one: Nest event media or Eufy's event image entity. Other cameras
    have nothing here."""
    kind = _camera_integration(hass, entity_id)
    if kind == "nest":
        return await _fetch_nest_event_image(hass, entity_id)
    if kind != "eufy":
        return None
    from . import eufy
    image_entity = eufy.discover_roles(hass, entity_id).get("event_image")
    if not image_entity:
        return None
    return await eufy.async_fetch_event_image(hass, image_entity)


def _camera_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    st = hass.states.get(entity_id)
    if st and st.attributes.get("friendly_name"):
        return st.attributes.get("friendly_name")
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return entity_id
    return entry.name or entry.original_name or entity_id


def _frigate_camera_name(entity_id: str) -> str:
    """Frigate's name for a camera: the entity id without its domain."""
    return entity_id.split(".", 1)[-1]


async def _fetch_nest_event_image(
    hass: HomeAssistant, entity_id: str
) -> Optional[bytes]:
    """The recorded media of the camera's cached Nest event, through the Nest
    integration's own media store. None when there is no fresh Nest event or
    the media cannot be read; the caller then falls back to a live frame."""
    event = _EVENT_CACHE.get(entity_id)
    if not event or event.get("source") != "nest":
        return None
    age = (_utcnow() - event["ts"]).total_seconds()
    if age >= EVENT_FRESH_SECONDS:
        _LOGGER.debug("Nova: Nest event for %s is %ds old, too old", entity_id, int(age))
        return None
    device_id, event_id = event.get("device_id"), event.get("event_id")
    if not (device_id and event_id):
        return None
    try:
        nest = hass.data.get("nest")
        for subscriber in (getattr(nest, "subscribers", []) if nest else []):
            try:
                if not subscriber.device_manager.devices.get(device_id):
                    continue
                store = getattr(subscriber, "media_store", None)
                if not store:
                    continue
                media = await store.async_get_media(device_id, event_id)
                body = media.contents if media and hasattr(media, "contents") else None
                if body and len(body) > MIN_IMAGE_SIZE:
                    _LOGGER.debug("Nova: Nest event media, %d bytes", len(body))
                    return body
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Nova: Nest media store error: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: could not read the Nest integration's data: %s", exc)
    _LOGGER.debug("Nova: no Nest event media for %s, a live frame comes next", entity_id)
    return None


# ─── Frame quality ───────────────────────────────────────────────────────────

async def _prewarm_stream(hass: HomeAssistant, entity_id: str, settle: float = 2.5) -> bool:
    """Wake an on demand stream, as opening the camera in the UI would, then
    give it ``settle`` seconds. True when a stream was requested; any failure
    gives False."""
    try:
        from homeassistant.components import camera as ha_camera
        request = getattr(ha_camera, "async_request_stream", None)
        if request is not None:
            try:
                await request(hass, entity_id, fmt="hls")
            except TypeError:
                await request(hass, entity_id, "hls")  # older signature
            await asyncio.sleep(settle)
            return True
        # Without a stream request, resolving the source can start it.
        find_source = getattr(ha_camera, "async_get_stream_source", None)
        if find_source is not None and await find_source(hass, entity_id):
            await asyncio.sleep(settle)
            return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: could not wake the stream for %s: %s", entity_id, exc)
    return False


def frame_stats(jpeg_bytes: bytes) -> Optional[tuple]:
    """(mean luma, luma standard deviation, width, height), or None when
    Pillow is missing or cannot read the image."""
    try:
        import io
        from PIL import Image, ImageStat
        grey = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
        stats = ImageStat.Stat(grey)
        return (stats.mean[0], stats.stddev[0], grey.width, grey.height)
    except Exception:  # noqa: BLE001
        return None


def _looks_blank(jpeg_bytes: bytes) -> bool:
    """Whether the frame is the near uniform black an idle stream returns.
    Dark night scenes keep sensor noise and pass. Unreadable means not blank."""
    stats = frame_stats(jpeg_bytes)
    return stats is not None and stats[0] < 12 and stats[1] < 8


def _content(image) -> bytes:
    if image and image.content:
        return image.content
    return b""


def _usable(image, min_size: int = MIN_IMAGE_SIZE) -> Optional[bytes]:
    body = _content(image)
    if len(body) > min_size and not _looks_blank(body):
        return body
    return None


def _kb(size: int) -> int:
    return size // 1024


def _luma_note(body: bytes) -> str:
    stats = frame_stats(body)
    if not stats:
        return ""
    return " · lum μ%.0f σ%.0f %d×%d" % stats


def _no_frame_verdict(out: dict) -> str:
    if out.get("platform") != "nest":
        return "NO FRAME from any tier — check the camera's source integration."
    return ("NO FRAME from any tier. Nest cameras only yield event media after a "
            "motion/doorbell event, and WebRTC-only models can't snapshot while "
            "idle — walk in front of it, then probe again. If that still fails, "
            "check the Google Nest integration's event subscriptions (Pub/Sub).")


async def probe_camera(hass: HomeAssistant, entity_id: str) -> dict:
    """The panel's camera diagnostic: walks the same tiers as _get_best_image
    (override, backend, snapshot, stream wake) and reports what each one did,
    with a verdict and the time taken. Keep the tiers in step with
    _get_best_image."""
    import time
    started = time.monotonic()
    report: dict = {"entity_id": entity_id, "tiers": []}
    tiers = report["tiers"]

    def done(verdict=None) -> dict:
        if verdict is not None:
            report["verdict"] = verdict
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return report

    source = resolve_camera_source(hass, entity_id)
    if source != entity_id:
        report["override"] = source
        tiers.append(["override", f"frames sourced from {source} (camera_overrides)"])
        entity_id = source

    st = hass.states.get(entity_id)
    report["state"] = st.state if st else "MISSING"
    report["available"] = bool(st) and st.state != "unavailable"
    attrs = (st.attributes if st else {}) or {}
    report["attrs"] = {
        "brand": attrs.get("brand"),
        "model": attrs.get("model_name"),
        "supported_features": attrs.get("supported_features"),
        "frontend_stream_type": str(attrs.get("frontend_stream_type") or ""),
    }
    try:
        entry = er.async_get(hass).async_get(entity_id)
        report["platform"] = entry.platform if entry else None
    except Exception:  # noqa: BLE001
        report["platform"] = None
    if not report["available"]:
        if not st:
            return done("entity missing from HA")
        return done("entity UNAVAILABLE in HA — fix the source integration first "
                    "(nothing Nova can fetch)")

    backend = None
    try:
        backend = find_backend(hass, entity_id)
    except Exception as exc:  # noqa: BLE001
        tiers.append(["backend-match", f"error: {exc}"])
    if backend:
        label = f"backend:{backend.name}"
        try:
            body = await backend.fetch_best_image(hass, entity_id, _EVENT_CACHE)
            if body:
                tiers.append([label, f"OK {_kb(len(body))}KB"])
                return done(f"frames available via {backend.name} backend")
            if _EVENT_CACHE.get(entity_id):
                tiers.append([label, "no image — cached event unusable"])
            else:
                tiers.append([label, "no image — no recent event media cached"])
        except Exception as exc:  # noqa: BLE001
            tiers.append([label, f"error: {exc}"])
    else:
        tiers.append(["backend-match", "none (generic camera path)"])

    placeholder = None
    try:
        body = _content(await camera_get_image(hass, entity_id, timeout=8))
        note = _luma_note(body)
        size_kb = _kb(len(body))
        if len(body) <= MIN_IMAGE_SIZE:
            tiers.append(["snapshot", f"too small ({len(body)}B)"])
        elif _looks_blank(body):
            tiers.append(["snapshot",
                          f"BLANK frame ({size_kb}KB{note}) — camera idle, black filler"])
        elif len(body) <= SMALL_SUSPECT_SIZE:
            placeholder = body
            tiers.append(["snapshot", f"SUSPECT: only {size_kb}KB{note} — placeholder-"
                                      "sized, not a real frame; trying stream wake"])
        else:
            tiers.append(["snapshot", f"OK {size_kb}KB{note}"])
            return done("frames available via standard snapshot")
    except Exception as exc:  # noqa: BLE001
        tiers.append(["snapshot", f"error: {type(exc).__name__}: {exc}"])

    placeholder_note = "only a placeholder-sized frame available"
    try:
        await _prewarm_stream(hass, entity_id)
        body = _content(await camera_get_image(hass, entity_id, timeout=10))
        note = _luma_note(body)
        better = not placeholder or len(body) > len(placeholder)
        if len(body) > MIN_IMAGE_SIZE and not _looks_blank(body) and better:
            tiers.append(["wake-retry", f"OK {_kb(len(body))}KB{note}"])
            report["verdict"] = "frames available after stream wake (slow path)"
        elif placeholder:
            tiers.append(["wake-retry", f"no better ({len(body)}B{note})"])
            report["verdict"] = (placeholder_note + " — the camera/HA is serving a "
                                 "thumbnail, not real video. " + _no_frame_verdict(report))
        else:
            tiers.append(["wake-retry", f"still unusable ({len(body)}B{note})"])
            report["verdict"] = _no_frame_verdict(report)
    except Exception as exc:  # noqa: BLE001
        tiers.append(["wake-retry", f"error: {type(exc).__name__}: {exc}"])
        verdict = _no_frame_verdict(report)
        report["verdict"] = placeholder_note + ". " + verdict if placeholder else verdict
    return done()


# ─── Names and sources ───────────────────────────────────────────────────────

def display_name(entity_id: str, friendly: Optional[str],
                 names: Optional[dict]) -> str:
    """The camera's name inside Nova: a non blank entry in the camera_names
    map, else the Home Assistant friendly name, else the entity id."""
    custom = (names or {}).get(entity_id)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return friendly or entity_id


def merge_camera_name(names: Optional[dict], entity_id: str,
                      new_name: Optional[str]) -> dict:
    """A new camera_names map with ``entity_id`` named ``new_name``, or with
    its entry dropped when the name is blank. The caller saves it."""
    updated = dict(names or {})
    cleaned = new_name.strip() if isinstance(new_name, str) else ""
    if cleaned:
        updated[entity_id] = cleaned
    else:
        updated.pop(entity_id, None)
    return updated


def resolve_camera_source(hass: HomeAssistant, entity_id: str) -> str:
    """The entity frames should come from. The camera_overrides map lets a
    camera (a Nest one restreamed through go2rtc or Frigate, say) keep its own
    identity while its frames come from the restream. An override pointing at
    a missing entity, or at itself, is ignored."""
    try:
        from . import nova_config
        target = (nova_config.get("camera_overrides", {}) or {}).get(entity_id)
        if target and target != entity_id and hass.states.get(target):
            return str(target)
    except Exception:  # noqa: BLE001
        pass
    return entity_id


# ─── Getting frames ──────────────────────────────────────────────────────────

async def _get_best_image(hass: HomeAssistant, entity_id: str) -> Optional[bytes]:
    """The best frame this camera can give, or None.

    Order: the camera's override source; its backend from camera_backends; a
    standard snapshot of a real frame's size; then a stream wake and one more
    snapshot. A placeholder sized first frame is returned only when the retry
    is no bigger. A black frame is never returned, since the vision model
    reads it as an obstructed lens."""
    entity_id = resolve_camera_source(hass, entity_id)
    backend = find_backend(hass, entity_id)
    if backend:
        try:
            body = await backend.fetch_best_image(hass, entity_id, _EVENT_CACHE)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: backend '%s' failed: %s, trying a snapshot",
                          backend.name, exc)
        else:
            if body:
                _LOGGER.debug("Nova: backend '%s' gave %d bytes", backend.name, len(body))
                return body
            _LOGGER.debug("Nova: backend '%s' had no image, trying a snapshot",
                          backend.name)

    first = None
    try:
        first = await camera_get_image(hass, entity_id, timeout=10)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: first snapshot of %s failed: %s", entity_id, exc)
    if first:
        full = _usable(first, min_size=SMALL_SUSPECT_SIZE)
        if full:
            return full
        placeholder = _usable(first)
    else:
        placeholder = None

    _LOGGER.info("Nova: %s gave no real frame, waking its stream and retrying", entity_id)
    await _prewarm_stream(hass, entity_id)
    try:
        second = await camera_get_image(hass, entity_id, timeout=15)
        body = _usable(second) if second else None
        if body and (placeholder is None or len(body) > len(placeholder)):
            return body
        if placeholder:
            _LOGGER.debug("Nova: %s retry was no better, using the %dB first frame",
                          entity_id, len(placeholder))
            return placeholder
        _LOGGER.warning("Nova: %s is still blank after a stream wake (%d bytes); an "
                        "on demand WebRTC camera with no stream running?",
                        entity_id, len(_content(second)))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Nova: retry snapshot of %s failed: %s", entity_id, exc)
        return placeholder
    return None


def _downscale_jpeg(jpeg_bytes: bytes, max_dim: int = 1024) -> bytes:
    """The frame shrunk so its longer side is at most ``max_dim``, to save
    vision tokens. Unchanged when already small, without Pillow, or on error."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg_bytes))
        longest = max(img.size)
        if longest <= max_dim:
            return jpeg_bytes
        ratio = max_dim / float(longest)
        size = tuple(max(1, int(side * ratio)) for side in img.size)
        buf = io.BytesIO()
        img.convert("RGB").resize(size).save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return jpeg_bytes


async def _capture_frame_sequence(hass: HomeAssistant, entity_id: str, count: int = 3,
                                  interval: float = 1.2,
                                  max_dim: int = 1024) -> list[bytes]:
    """Up to ``count`` usable, downscaled frames ``interval`` seconds apart,
    after waking the stream once. Frames come from the camera's override
    source, as single snapshots do. Falls back to the single best image when
    none of the grabs is usable."""
    source = resolve_camera_source(hass, entity_id)
    try:
        await _prewarm_stream(hass, source)
    except Exception:  # noqa: BLE001
        pass
    total = max(1, count)
    frames: list[bytes] = []
    for index in range(total):
        try:
            body = _content(await camera_get_image(hass, source, timeout=10))
            if len(body) > MIN_IMAGE_SIZE and not _looks_blank(body):
                frames.append(_downscale_jpeg(body, max_dim))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: frame %d of %s failed: %s", index, entity_id, exc)
        if index + 1 < total:
            await asyncio.sleep(interval)
    if not frames:
        best = await _get_best_image(hass, entity_id)
        if best:
            frames.append(_downscale_jpeg(best, max_dim))
    _LOGGER.debug("Nova: %d frame(s) from %s", len(frames), entity_id)
    return frames


def _make_contact_sheet(frames: list[bytes], max_cols: int = 2,
                        cell_max: int = 512) -> Optional[bytes]:
    """Two or more frames tiled into one image labelled Frame 1, Frame 2 and
    so on in time order, so any single image vision model can follow a clip.
    None for fewer than two readable frames or without Pillow."""
    if len(frames) < 2:
        return None
    try:
        import io
        from PIL import Image, ImageDraw
        cells = []
        for raw in frames:
            try:
                cell = Image.open(io.BytesIO(raw)).convert("RGB")
            except Exception:  # noqa: BLE001
                continue
            ratio = cell_max / float(max(cell.size))
            if ratio < 1:
                cell = cell.resize(tuple(max(1, int(side * ratio)) for side in cell.size))
            cells.append(cell)
        if len(cells) < 2:
            return None
        gap, caption = 6, 18
        cols = min(max_cols, len(cells))
        rows = -(-len(cells) // cols)
        cell_w = max(c.width for c in cells)
        cell_h = max(c.height for c in cells)
        width = cols * cell_w + (cols + 1) * gap
        height = rows * (cell_h + caption) + (rows + 1) * gap
        sheet = Image.new("RGB", (width, height), (16, 16, 20))
        pen = ImageDraw.Draw(sheet)
        for number, cell in enumerate(cells, start=1):
            row, col = divmod(number - 1, cols)
            left = gap + col * (cell_w + gap)
            top = gap + row * (cell_h + caption + gap)
            pen.text((left + 2, top + 2), f"Frame {number}", fill=(120, 200, 255))
            sheet.paste(cell, (left, top + caption))
        buf = io.BytesIO()
        sheet.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


# ─── Analysis ────────────────────────────────────────────────────────────────

def _recognition_hint(hass: HomeAssistant, entity_id: str) -> str:
    """A prompt line naming the person in frame, when recognition knows them
    confidently from the last minute."""
    if not _RECOGNITION_AVAILABLE:
        return ""
    try:
        seen = last_seen_at(hass, entity_id)
        if not seen or seen.get("is_confident") is False:
            return ""
        name = seen.get("name", "")
        score = seen.get("confidence", 0)
        if not name or name.lower() == "unknown":
            return ""
        if score < 60 or seen.get("age_seconds", 999) >= 60:
            return ""
        return ("\n\nIMPORTANT: Face recognition has identified the person in frame as "
                f"'{name}' (confidence {score:.0f}%). Refer to them by name in your "
                "description.")
    except Exception:  # noqa: BLE001
        return ""


def _vision_task(camera_name: str, clip_count: int, sheet: bool, interval: float,
                 hint: str) -> str:
    if not clip_count:
        return (f"You are analysing the camera feed '{camera_name}'. Describe what you "
                "see clearly and concisely — as Nova would. Note specific details: "
                "people, vehicles, packages, unusual activity. Under 80 words unless "
                "something truly warrants more detail." + hint)
    if sheet:
        what = ("a single contact sheet whose tiles are labelled Frame 1, Frame 2, … "
                "in chronological order")
    else:
        what = f"{clip_count} sequential frames in chronological order"
    return (f"You are analysing {what} from the camera feed '{camera_name}', captured "
            f"about {interval:.0f}s apart. Describe what HAPPENS across the frames — "
            "motion, who or what appears or leaves, packages set down or removed, "
            "direction of travel. If the scene is static, say so in a few words. "
            "Under 90 words." + hint)


async def _frames_for(hass: HomeAssistant, entity_id: str, data: dict,
                      force_images: Optional[list]) -> tuple[list, float]:
    wanted = max(1, int(data.get("frames", 1) or 1))
    interval = float(data.get("interval", 1.2) or 1.2)
    if force_images:
        return [frame for frame in force_images if frame], interval
    if wanted > 1:
        return await _capture_frame_sequence(
            hass, entity_id, count=wanted, interval=interval), interval
    best = await _get_best_image(hass, entity_id)
    return ([best] if best else []), interval


async def async_analyze_camera(hass: HomeAssistant, call: ServiceCall, groq_client,
                               honorific: str, tts_entity: str | None,
                               speakers: list[str], gate_announce: bool = False,
                               force_images: Optional[list] = None) -> dict:
    """The nova.analyze_camera service, also used by the automatic paths.

    Gets a frame (or a clip, with ``frames`` above one, or the given
    ``force_images``), describes it with the vision model, judges it with the
    camera reasoning model and records the result across Nova. With
    ``gate_announce`` only notable scenes are spoken; a manual request always
    reports back."""
    entity_id: str = call.data["entity_id"]
    prompt: str = call.data.get("prompt", _DEFAULT_PROMPT)
    announce: bool = call.data.get("announce", True)
    camera_name = _camera_friendly_name(hass, entity_id)
    integration = _camera_integration(hass, entity_id)
    _LOGGER.info("Nova: analysing %s (%s, via %s)", entity_id, camera_name, integration)

    frames, interval = await _frames_for(hass, entity_id, call.data, force_images)
    if not frames:
        # The honorific is "" when nobody in particular is home; the sentence
        # then starts with a capital. persona.lead_in is not used, since it
        # would title case the honorific.
        sentence = (f"I could not get a usable image from {camera_name}. This camera may "
                    "use WebRTC (no snapshot support) or be offline.")
        message = f"{honorific}, {sentence}" if honorific else sentence[:1].upper() + sentence[1:]
        _LOGGER.error("Nova: no usable image for %s", entity_id)
        _nova_log(f"{camera_name}: no usable image (WebRTC/offline?)")
        if announce:
            await async_announce(hass, message, tts_entity, speakers, context="camera")
        return {"success": False, "error": "no_image", "camera": camera_name}

    clip = len(frames) > 1
    sheet = _make_contact_sheet(frames) if clip else None
    payload = [sheet] if sheet is not None else frames
    encoded = [base64.b64encode(frame).decode() for frame in payload]

    task = _vision_task(camera_name, len(encoded) if clip else 0, sheet is not None,
                        interval, _recognition_hint(hass, entity_id))
    system = build_system_prompt(hass, honorific, task)
    from . import output_language
    system = output_language.with_language(hass, system)   # the description may be spoken
    vision_provider = _cfg_opt(hass, "vision_provider", "groq") or "groq"
    vision_model = _cfg_opt(hass, "vision_model", VISION_MODEL) or VISION_MODEL
    content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}
               for b64 in encoded]
    content.append({"type": "text", "text": prompt})
    try:
        from .providers.activity import execute_chat
        async with _camera_client(hass, vision_provider, vision_model, groq_client,
                                  binding="vision") as vision_client:
            reply = await execute_chat(
                hass, vision_client,
                [{"role": "system", "content": system}, {"role": "user", "content": content}],
                role="vision", data_category="vision", max_tokens=300, temperature=0.7,
                model_override=vision_model or None,
            )
        analysis = reply.text.strip()
    except Exception as exc:  # noqa: BLE001
        if _vision_model_rejects_images(exc):
            # A text only model refuses the image list with a 400; say how to fix it.
            friendly = (f"vision model '{vision_model}' does not accept images — set "
                        "'vision_model' to a vision-capable model (e.g. 'moondream' on "
                        "Ollama, or a Llama/Qwen vision model on Groq)")
            _LOGGER.error("Nova vision (%s/%s): %s [raw: %s]",
                          vision_provider, vision_model, friendly, exc)
            _nova_log(f"{camera_name}: {friendly}")
            return {"success": False, "error": friendly, "camera": camera_name}
        _LOGGER.error("Nova vision error (%s/%s): %s", vision_provider, vision_model, exc)
        _nova_log(f"{camera_name}: vision error ({vision_provider}/{vision_model}) — {exc}")
        return {"success": False, "error": str(exc)}

    det_type = _guess_detection_type(prompt, analysis)
    reasoning_provider = _cfg_opt(hass, "camera_reasoning_provider", "groq") or "groq"
    reasoning_model = (_cfg_opt(hass, "camera_reasoning_model", _DEFAULT_REASONING_MODEL)
                       or _DEFAULT_REASONING_MODEL)
    async with _camera_client(hass, reasoning_provider, reasoning_model, groq_client,
                              binding="camera_reasoning") as reasoning_client:
        judgment = await _reason_about_scene(
            hass, reasoning_client, reasoning_model, camera_name, analysis, det_type)
    summary = judgment["summary"]
    category = judgment["category"]
    notable = judgment["notable"]
    _nova_log(f"{camera_name} [{category}]{' ⚠NOTABLE' if notable else ''}: {summary}")

    await _record_scene(hass, call, entity_id, camera_name, analysis, judgment)

    spoke = False
    if announce and notable and judgment.get("speak"):
        # A notable scene is a proactive alert, so it goes through delivery
        # (shush, quiet hours, the Announcements switch, notifications only).
        from . import delivery
        sent = await delivery.async_deliver(hass, delivery.DeliveryRequest(
            source="camera", message=judgment["speak"], entity_id=entity_id,
            category="camera", speakers=tuple(speakers or ()),
            tts_entity=tts_entity or "", tts_context="camera"))
        spoke = sent.spoken
    elif announce and not gate_announce:
        # Someone asked, so a quiet scene is still reported.
        await async_announce(hass, analysis, tts_entity, speakers, context="camera")
        spoke = True

    _LOGGER.info("Nova: %s notable=%s category=%s | %s",
                 camera_name, notable, category, summary[:80])
    return {
        "success": True,
        "analysis": analysis,
        "summary": summary,
        "notable": notable,
        "category": category,
        "speak": judgment.get("speak"),
        "spoke": spoke,
        "camera": camera_name,
        "source": integration,
        "image_bytes": sum(map(len, frames)),
    }


async def _record_scene(hass: HomeAssistant, call, entity_id: str, camera_name: str,
                        analysis: str, judgment: dict) -> None:
    """Spread one analysed scene across Nova: chat memory, the observer's
    awareness, scene memory (when on and asked), semantic learning and the
    briefing snapshot. Only memory may raise."""
    summary = judgment["summary"]
    await hass.async_add_executor_job(
        save_message, "assistant", f"[Camera: {camera_name}] {summary}", "camera")
    try:
        from . import observer
        observer.record_camera_event(camera_name, summary, judgment["category"],
                                     judgment["notable"])
    except Exception:  # noqa: BLE001
        pass
    if call.data.get("record_scene", True):
        try:
            from . import camera_semantic, scene_memory
            if scene_memory.enabled():
                await scene_memory.async_remember(
                    hass, entity_id, analysis,
                    camera_semantic.resolve_location(hass, entity_id))
        except Exception:  # noqa: BLE001
            pass
    # Semantic learning records known residents too (a person arriving can
    # start a routine); only an empty scene is skipped. Only the fixed
    # category goes in, never the description.
    try:
        _vision_label, _vision_pkg_state = _map_vision_category(judgment["category"])
        if _vision_label:
            from . import camera_semantic
            hass.async_create_task(camera_semantic.record_event(
                hass, label=_vision_label, camera_entity=entity_id, source="vision",
                package_state=_vision_pkg_state, detail=judgment["category"],
            ))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .proactive_briefing import record_snapshot
        record_snapshot(camera_name=camera_name, camera_entity=entity_id,
                        analysis=summary, detection_type=judgment["category"],
                        urgency="medium" if judgment["notable"] else "low")
    except Exception:  # noqa: BLE001
        pass


# ─── Camera events on the bus ────────────────────────────────────────────────

def _nest_device_to_camera(hass: HomeAssistant, nest_device_id: str) -> Optional[str]:
    """The camera entity of the Home Assistant device whose Nest identifier
    contains ``nest_device_id``."""
    try:
        owner = next(
            (device.id for device in dr.async_get(hass).devices.values()
             if any(len(ident) >= 2 and ident[0] == "nest"
                    and nest_device_id in str(ident[1])
                    for ident in device.identifiers)),
            None)
        if owner is None:
            return None
        for entity in er.async_get(hass).entities.values():
            if entity.device_id == owner and entity.domain == "camera":
                return entity.entity_id
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: Nest device lookup failed: %s", exc)
    return None


def _fire_camera_event(hass: HomeAssistant, entity_id: str, source: str, label: str,
                       confidence) -> None:
    hass.bus.async_fire("nova_camera_event", {"entity_id": entity_id, "source": source,
                                              "label": label, "confidence": confidence})


@callback
def _handle_nest_event(hass: HomeAssistant, event: Event) -> None:
    """Remember a Nest event so its media can be fetched, and announce it on
    the bus as nova_camera_event."""
    try:
        data = event.data
        device_id = data.get("device_id") or data.get("nest_device_id")
        event_id = data.get("nest_event_id") or data.get("event_id")
        if not (device_id and event_id):
            return
        entity_id = _nest_device_to_camera(hass, device_id)
        if not entity_id:
            return
        _EVENT_CACHE[entity_id] = {"event_id": event_id, "device_id": device_id,
                                   "ts": _utcnow(), "source": "nest"}
        _LOGGER.debug("Nova: Nest event %s on %s", event_id, entity_id)
        try:
            kind = data.get("event_type") or data.get("type") or "motion"
            _fire_camera_event(hass, entity_id, "nest", str(kind), None)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: could not record a Nest event: %s", exc)


@callback
def _handle_frigate_event(hass: HomeAssistant, event: Event) -> None:
    """Remember a new Frigate event on a camera Home Assistant knows, and
    announce it on the bus as nova_camera_event with its score as a percent."""
    try:
        data = event.data
        if data.get("type") != "new":
            return
        detail = data.get("after") or data.get("before") or {}
        name, event_id = detail.get("camera"), detail.get("id")
        if not (name and event_id):
            return
        entity_id = "camera." + name.lower()
        if not hass.states.get(entity_id):
            return
        _EVENT_CACHE[entity_id] = {"event_id": event_id, "device_id": name,
                                   "ts": _utcnow(), "source": "frigate"}
        _LOGGER.debug("Nova: Frigate event %s on %s", event_id, entity_id)
        try:
            score = detail.get("score") or detail.get("top_score")
            percent = None if score is None else round(float(score) * 100)
            _fire_camera_event(hass, entity_id, "frigate",
                               str(detail.get("label", "object")), percent)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: could not record a Frigate event: %s", exc)


_EXACT_BUS_LABELS = {
    "person": "person", "car": "vehicle", "truck": "vehicle", "motorcycle": "vehicle",
    "dog": "animal", "cat": "animal", "package": "package", "bicycle": "activity",
}
_PARTIAL_BUS_LABELS = (("person", "person"), ("package", "package"),
                       ("motion", "activity"))


def _map_bus_label(raw) -> Optional[str]:
    """A nova_camera_event label (Frigate's object label, or Nest's event type)
    as a camera_semantic label, or None. A chime and anything without a clear
    match are skipped; the doorbell press is learned from its own analysis."""
    label = str(raw or "").strip().lower()
    if not label:
        return None
    if label in _EXACT_BUS_LABELS:
        return _EXACT_BUS_LABELS[label]
    for needle, mapped in _PARTIAL_BUS_LABELS:
        if needle in label:
            return mapped
    return None


@callback
def _handle_semantic_camera_event(hass: HomeAssistant, event: Event) -> None:
    """Pass a Nest or Frigate nova_camera_event to semantic learning, with only
    its short label as detail."""
    try:
        data = event.data
        entity_id = data.get("entity_id")
        source = data.get("source")
        if not entity_id or source not in ("nest", "frigate"):
            return
        label = _map_bus_label(data.get("label"))
        if label is None:
            return
        from . import camera_semantic
        hass.async_create_task(camera_semantic.record_event(
            hass, label=label, camera_entity=str(entity_id), source=str(source),
            confidence=data.get("confidence"), detail=str(data.get("label") or "")[:40],
        ))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: semantic camera event not recorded: %s", exc)


def register_event_listeners(hass: HomeAssistant) -> list:
    """Listen for nest_event, frigate_event and Nova's own nova_camera_event.
    Returns the unsubscribe callables."""
    routes = (
        ("nest_event", "_handle_nest_event"),
        ("frigate_event", "_handle_frigate_event"),
        ("nova_camera_event", "_handle_semantic_camera_event"),
    )
    unsubs = []
    for event_type, name in routes:
        try:
            unsubs.append(hass.bus.async_listen(
                event_type, lambda e, name=name: globals()[name](hass, e)))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: no listener for %s: %s", event_type, exc)
    _LOGGER.info("Nova: %d camera event listener(s) registered", len(unsubs))
    return unsubs


# ─── Doorbell presses, visitors and automatic analysis ───────────────────────

class _FakeCall:
    """Just enough of a ServiceCall for internal async_analyze_camera calls."""

    def __init__(self, data: dict):
        self.data = data


_SUBJECT_WORDS = (
    "person|people|man|woman|men|women|someone|somebody|individual|child|children|"
    "kid|figure|delivery|courier|mail\\s*carrier|package|parcel|box|vehicle|car|truck|"
    "van|suv|motorcyc|bicycle|dog|cat|animal|visitor|stranger|guest|face"
)
_EMPTY_WORDS = (
    "no one|no-one|nobody|no person|no people|empty|deserted|unoccupied|"
    "nothing notable|nothing of note|no visible|no activity|no movement|"
    "appears? (?:empty|clear|quiet|still)|static scene|"
    "just (?:the )?(?:yard|street|driveway|grass|lawn|porch)"
)
_SUBJECT_PAT = re.compile(r"\b(" + _SUBJECT_WORDS + r")\b", re.I)
_NO_SUBJECT_PAT = re.compile(r"\b(" + _EMPTY_WORDS + r")\b", re.I)


def _scene_has_subject(analysis: str) -> bool:
    """Whether a description mentions someone or something (person, vehicle,
    package, animal) rather than an empty scene."""
    if not analysis:
        return False
    return _SUBJECT_PAT.search(analysis) is not None


async def _analyze_doorbell_press(hass: HomeAssistant, groq_client, honorific: str,
                                  tts_entity: str | None, speakers: list[str],
                                  entity_id: str, reason: str) -> dict:
    """Analyse a doorbell press and announce it once.

    A live clip goes first, since the visitor may still be there. When it shows
    nobody, the event media (frozen at the press) is tried; it wins if it shows
    someone or if the live pass failed. The chosen result is delivered when
    notable, logged for training and handed to the package watch."""
    # The task never names the honorific: the model would echo it as a third
    # person ("Sir has a visitor"). build_system_prompt handles addressing.
    prompt = (f"{reason}. Someone is at the door. Identify who is there and what they "
              "are doing — appearance, clothing, whether they carry a package or wait, "
              "any vehicle behind them. Focus on what the resident would want to know.")
    result = await async_analyze_camera(
        hass, _FakeCall({"entity_id": entity_id, "prompt": prompt, "announce": False,
                         "frames": 3, "interval": 1.2}),
        groq_client, honorific, tts_entity, speakers, gate_announce=True)
    used = "live"

    if not (result.get("success") and _scene_has_subject(result.get("analysis", ""))):
        media = await _fetch_event_media_image(hass, entity_id)
        if media:
            moment = await async_analyze_camera(
                hass, _FakeCall({
                    "entity_id": entity_id,
                    "prompt": prompt + " (This frame was captured at the moment the "
                                       "doorbell rang.)",
                    "announce": False}),
                groq_client, honorific, tts_entity, speakers,
                gate_announce=True, force_images=[_downscale_jpeg(media)])
            found = _scene_has_subject(moment.get("analysis", ""))
            if moment.get("success") and (found or not result.get("success")):
                result, used = moment, "event-media"

    if result.get("success"):
        if result.get("notable") and result.get("speak"):
            from . import delivery
            sent = await delivery.async_deliver(hass, delivery.DeliveryRequest(
                source="doorbell", message=result["speak"], entity_id=entity_id,
                category="doorbell", speakers=tuple(speakers or ()),
                tts_entity=tts_entity or "", tts_context="camera"))
            result["spoke"] = sent.spoken
        await _log_training(hass, entity_id, used, result)
        try:
            from . import package_monitor
            await package_monitor.note_from_doorbell(
                hass, groq_client, honorific, tts_entity, speakers,
                entity_id, result.get("analysis", ""))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Nova: doorbell package check failed: %s", exc)

    _LOGGER.info("Nova: doorbell press on %s analysed from %s frames, notable=%s",
                 entity_id, used, result.get("notable"))
    return result


async def _log_training(hass: HomeAssistant, entity_id: str, kind: str, result: dict) -> None:
    try:
        from . import doorbell_training
        await hass.async_add_executor_job(
            doorbell_training.log_event,
            _camera_friendly_name(hass, entity_id), entity_id, kind, result)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova: doorbell training log failed: %s", exc)


async def async_visitor_observation(hass: HomeAssistant, groq_client, honorific: str,
                                    entity_id: str) -> None:
    """Quietly learn from a person at the door (no press): analyse the event
    media, or a live frame, and log it for training. Never spoken or pushed.
    Keep it for events worth a vision call, such as a stranger."""
    await asyncio.sleep(2)
    frame = await _fetch_event_media_image(hass, entity_id)
    if not frame:
        frame = await _get_best_image(hass, entity_id)
    if not frame:
        return
    request = _FakeCall({
        "entity_id": entity_id,
        "prompt": ("A person was detected at the door (no doorbell press). Briefly note "
                   "who: appearance, apparent purpose (delivery, passer-by, approaching, "
                   "leaving), any package or vehicle. One or two sentences."),
        "announce": False,
    })
    result = await async_analyze_camera(
        hass, request, groq_client, honorific, None, [],
        gate_announce=True, force_images=[_downscale_jpeg(frame)])
    if not result.get("success"):
        return
    await _log_training(hass, entity_id, "visitor", result)
    _LOGGER.info("Nova: visitor on %s logged quietly", entity_id)


async def async_auto_analyze_on_event(hass: HomeAssistant, groq_client, honorific: str,
                                      tts_entity: str | None, speakers: list[str],
                                      entity_id: str, reason: str = "Motion detected",
                                      doorbell: bool = False) -> None:
    """Analyse a camera event after a short wait for its media. A doorbell
    press takes the doorbell path; anything else gets a short live clip,
    spoken only when notable."""
    await asyncio.sleep(3)
    if doorbell:
        await _analyze_doorbell_press(
            hass, groq_client, honorific, tts_entity, speakers, entity_id, reason)
        return
    # As with the doorbell, the task leaves the honorific out.
    request = _FakeCall({
        "entity_id": entity_id,
        "prompt": (f"{reason}. Describe what you see clearly. If there is a person, "
                   "describe their appearance and what they're doing. If there is a "
                   "vehicle or package, note it. Focus on what the resident would "
                   "want to know."),
        "announce": True,
        "frames": 3,
        "interval": 1.2,
    })
    await async_analyze_camera(
        hass, request, groq_client, honorific, tts_entity, speakers, gate_announce=True)
