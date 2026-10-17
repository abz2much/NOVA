"""Nova — the infrastructure audit and the way it speaks.

The audit says an alert in one area: it picks the area's speaker, shapes
the voice to the room (light, noise, whether media is playing, how closely
someone is attending), lowers and then restores the speaker volume around
the announcement, and records what was said in spoken history.
(nova.speak and nova.process_intent used these helpers until 8.60.0.)
The config entry wires it:

    async_setup_entry   ->  await async_setup_proactive_audio(hass, entry)
    async_unload_entry  ->  await async_unload_proactive_audio(hass, entry)

Speaker choice uses audio_routing's room and general speakers, then the
whole house speakers; the voice shaping is this module's own.

Every object here belongs to the loaded entry. Its NovaRuntime holds the
boot alert buffer (built on first use, dropped on unload, built again on
reload) and the flag that stops audit ticks overlapping. The entry's
NovaResources holds the audit timer callbacks; nothing here is built outside
the entry's lifetime.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import audio_routing, delivery
from .audio import NoiseGate, ProsodyController
from .automation import PredictiveHabitMatrix
from .boot_guard import AlertBuffer
from .const import CONF_BROADCAST_GROUP, CONF_HONORIFIC, DEFAULT_HONORIFIC, DOMAIN
from .diagnostics import FaultLog, InfrastructureTriage
from .runtime import NovaRuntime, get_runtime, lifecycle_runtime
from .vision import SpatialContextEngine

_LOGGER = logging.getLogger(__name__)

# The TTS engine the audit speaks with unless the panel sets proactive_tts_entity.
DEFAULT_TTS_ENTITY = "tts.piper"
# A last resort player for an area with no speaker at all; empty means none.
DEFAULT_ANNOUNCE_PLAYER = ""
# The original design's background duck level. Not used: _announce sets the
# speaker to the voice profile's own volume instead.
MEDIA_DUCK_LEVEL = 0.10

AUDIT_INTERVAL = timedelta(minutes=15)
AUDIT_STARTUP_DELAY = timedelta(seconds=60)
# Setting naming the area the audit speaks in; empty means it only logs.
AUDIT_AREA_KEY = "infrastructure_audit_area"
# Setting listing the sensors the audit checks; empty means no audit.
AUDIT_SENSORS_KEY = "infrastructure_audit_sensors"

# The habit model only suggests (in the log) until Nova is trusted to act.
PREDICTOR_AUTOEXECUTE = False

# How long an announcement takes to say, for holding the lowered volume.
WORDS_PER_SECOND = 2.6
TTS_PADDING_S = 0.9
TTS_MIN_S = 1.5
TTS_MAX_S = 30.0

# One controller for every announcement. Its own quiet hours (22 to 7) are
# only a fallback: telemetry passes the household's.
_PROSODY = ProsodyController()


def _audit_sensors(hass, entry) -> list:
    """The audit's sensor list as entity ids, from a list or its JSON text.
    Anything unreadable gives an empty list."""
    import json
    from . import nova_config
    try:
        listed = nova_config.runtime_get(hass, entry, AUDIT_SENSORS_KEY, [])
    except Exception:
        return []
    if isinstance(listed, str):
        if not listed.strip():
            return []
        try:
            listed = json.loads(listed)
        except ValueError:
            return []
    if not isinstance(listed, list):
        return []
    return [str(item) for item in listed if isinstance(item, str) and item.strip()]


def _as_float(value: object) -> float | None:
    """A finite float, or None for booleans, text and anything infinite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _resolve_honorific(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """How to address whoever is home, worked out on every call (the audit
    asks again each tick). Falls back to the saved honorific."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        from . import nova_config
        return nova_config.runtime_get(hass, entry, CONF_HONORIFIC, DEFAULT_HONORIFIC)


def _resolve_tts_entity(runtime: NovaRuntime) -> str:
    chosen = runtime.runtime_config.get("proactive_tts_entity")
    return str(chosen) if chosen else DEFAULT_TTS_ENTITY


# ── Areas and speakers ─────────────────────────────────────────────────────

@callback
def _resolve_area_id(hass: HomeAssistant, target: str) -> str | None:
    """An area id from either the id itself or the area's name."""
    areas = ar.async_get(hass)
    if areas.async_get_area(target) is not None:
        return target
    named = areas.async_get_area_by_name(target)
    return None if named is None else named.id


@callback
def _resolve_broadcast_speakers(hass: HomeAssistant, runtime: NovaRuntime) -> list[str]:
    """The whole house speakers: the panel's announcement speakers that
    exist, else the first Nova entry's broadcast group, through
    audio_routing.broadcast_target (silent when neither is set)."""
    chosen = runtime.runtime_config.get("announcement_speakers")
    if chosen:
        live = [s for s in chosen if hass.states.get(s)]
        if live:
            return live
    from . import nova_config
    group = ""
    for entry in hass.config_entries.async_entries(DOMAIN):
        group = nova_config.runtime_get(hass, entry, CONF_BROADCAST_GROUP, "") or group
        if group:
            break
    return audio_routing.broadcast_target(hass, broadcast_group=group)


@callback
def _resolve_targets(
    hass: HomeAssistant, runtime: NovaRuntime, area_id: str,
) -> tuple[list[str], str]:
    """(speakers, mode) for an announcement in this area. The area's
    assigned speaker or else the general speaker gives mode "area"; the
    whole house speakers (satellites removed) or the default player give
    "broadcast"; nothing gives ([], "none"). Speakers are only ever the
    ones the user chose, never discovered from the area."""
    local = (audio_routing.room_speaker(hass, area_id)
             or audio_routing.general_speaker_target(hass))
    if local:
        return [local], "area"
    house = [s for s in _resolve_broadcast_speakers(hass, runtime)
             if not s.startswith("assist_satellite.")]
    if house:
        return house, "broadcast"
    if DEFAULT_ANNOUNCE_PLAYER:
        return [DEFAULT_ANNOUNCE_PLAYER], "broadcast"
    return [], "none"


# ── Voice shaping ──────────────────────────────────────────────────────────

def _is_light_sensor(entity_id: str, device_class) -> bool:
    return device_class == "illuminance" or "lux" in entity_id or "illuminance" in entity_id


def _is_noise_sensor(entity_id: str, device_class) -> bool:
    return device_class == "sound_pressure" or any(
        word in entity_id for word in ("noise", "sound", "decibel", "_db"))


@callback
def _build_telemetry(
    hass: HomeAssistant, area_id: str, targets: list[str], critical: bool,
) -> dict:
    """What the prosody controller needs: the darkest light reading and the
    loudest noise reading in the area (noise corrected for running
    appliances), whether a target speaker is playing, the spatial attention
    estimate, and the household's quiet hours."""
    light: list[float] = []
    noise: list[float] = []
    for sensor in hass.states.async_all("sensor"):
        if audio_routing.entity_area(hass, sensor.entity_id) != area_id:
            continue
        kind = sensor.attributes.get("device_class")
        if _is_light_sensor(sensor.entity_id, kind):
            bucket = light
        elif _is_noise_sensor(sensor.entity_id, kind):
            bucket = noise
        else:
            continue
        reading = _as_float(sensor.state)
        if reading is not None:
            bucket.append(reading)

    playing = False
    for target in targets:
        state = hass.states.get(target)
        if state is not None and str(state.state).lower() == "playing":
            playing = True
            break

    attention = SpatialContextEngine(hass).evaluate(area_id)
    loudest = max(noise) if noise else None
    return {
        "critical_alert": critical,
        "ambient_lux": min(light) if light else None,
        "ambient_db": NoiseGate(hass).compensated_db(loudest),
        "media_active": playing,
        "skip_preamble": attention["skip_preamble"],
        "spatial_confidence": attention["confidence"],
        "quiet_hours": delivery.in_quiet_hours(hass),
    }


def _estimate_duration(message: str, speech_rate: float) -> float:
    """Seconds to say the message, between TTS_MIN_S and TTS_MAX_S. Rates
    below half speed count as half speed."""
    word_count = max(1, len(message.split()))
    rate = WORDS_PER_SECOND * max(speech_rate, 0.5)
    return min(TTS_MAX_S, max(TTS_MIN_S, word_count / rate + TTS_PADDING_S))


def _tts_options(profile: dict) -> dict:
    return {"rate": round(float(profile["speech_rate"]), 2)}


# ── Speaking ───────────────────────────────────────────────────────────────

async def _set_volume(hass: HomeAssistant, entity_id: str, level: float) -> None:
    clamped = min(1.0, max(0.0, level))
    await hass.services.async_call(
        "media_player", "volume_set",
        {"entity_id": entity_id, "volume_level": clamped}, blocking=True)


def _without_televisions(hass: HomeAssistant, targets: list[str]) -> list[str]:
    """audio_routing's television guard; if the guard itself fails, the
    targets are used as they are."""
    try:
        return audio_routing.drop_display_targets(hass, targets, "proactive_audio")
    except Exception:
        return targets


async def _speak_tts(
    hass: HomeAssistant, runtime: NovaRuntime, targets: list[str], message: str,
    profile: dict,
) -> list[str]:
    """Say the message on the targets (televisions removed) with the
    profile's rate. If the engine refuses the options, say it again without
    them. Returns the targets used, or [] when none were left."""
    targets = _without_televisions(hass, targets)
    if not targets:
        return []
    request = {"entity_id": _resolve_tts_entity(runtime),
               "media_player_entity_id": targets, "message": message}
    try:
        await hass.services.async_call(
            "tts", "speak", {**request, "options": _tts_options(profile)}, blocking=True)
    except (vol.Invalid, HomeAssistantError):
        _LOGGER.debug("TTS rejected options; retrying without them")
        await hass.services.async_call("tts", "speak", request, blocking=True)
    return targets


def _current_volumes(hass: HomeAssistant, targets: list[str]) -> dict[str, float]:
    levels: dict[str, float] = {}
    for target in targets:
        state = hass.states.get(target)
        level = None if state is None else _as_float(state.attributes.get("volume_level"))
        if level is not None:
            levels[target] = level
    return levels


async def _announce(
    hass: HomeAssistant, runtime: NovaRuntime, message: str, area_id: str,
    critical: bool,
) -> bool:
    """Say one message in an area. Speakers with a known volume are set to
    the voice profile's volume (from a whisper up to full for critical)
    for as long as the message takes, then put back, whatever happens.
    Returns whether it was spoken; spoken messages go to spoken history."""
    targets, mode = _resolve_targets(hass, runtime, area_id)
    if not targets:
        _LOGGER.warning(
            "Nova audio: no speaker resolved for area '%s' (no area speaker and "
            "no broadcast/default fallback) — nothing to announce on", area_id)
        return False

    profile = _PROSODY.calculate_vocal_profile(
        _build_telemetry(hass, area_id, targets, critical))
    level = float(profile["volume"])
    before = _current_volumes(hass, targets)
    _LOGGER.debug("Nova audio: area=%s mode=%s style=%s volume=%.2f targets=%s",
                  area_id, mode, profile["style"], level, targets)

    spoken_on: list[str] = []
    try:
        for target in before:
            try:
                await _set_volume(hass, target, level)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Nova audio: failed to set volume for %s", target)
        spoken_on = await _speak_tts(hass, runtime, targets, message, profile)
        await asyncio.sleep(_estimate_duration(message, float(profile["speech_rate"])))
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Nova audio: announcement failed in area '%s'", area_id)
        spoken_on = []
    finally:
        for target, previous in before.items():
            try:
                await _set_volume(hass, target, previous)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Nova audio: failed to restore volume for %s", target)

    if not spoken_on:
        return False
    try:
        from . import spoken_history
        await hass.async_add_executor_job(
            spoken_history.record, message, "manual", spoken_on, None)
    except Exception:
        pass  # a history failure never undoes a spoken announcement
    return True


# ── Audit helpers ──────────────────────────────────────────────────────────

def _history_phrase(matches: list[dict], honorific: str) -> str:
    """" For context, ..." noting how often this fault happened before."""
    if not matches:
        return ""
    from .persona import capitalise
    address = f", {capitalise(honorific)}" if honorific else ""
    times = "once" if len(matches) == 1 else f"{len(matches)} times"
    return f" For context{address}, this has occurred {times} before."


async def _run_predictor(hass: HomeAssistant, predictor: PredictiveHabitMatrix) -> None:
    """Feed the occupied rooms into the habit model and log what it expects
    next. Acting on it waits for PREDICTOR_AUTOEXECUTE."""
    try:
        rooms = audio_routing.currently_occupied_areas(hass)
    except Exception:  # noqa: BLE001
        rooms = []
    for room in rooms:
        await hass.async_add_executor_job(predictor.record_event, f"{room}_entry")
    for item in await hass.async_add_executor_job(predictor.due_preemptions):
        if PREDICTOR_AUTOEXECUTE:
            _LOGGER.info("Predictor: pre-empting %s (p=%.2f) — wire a per-action handler",
                         item["key"], item["probability"])
        else:
            _LOGGER.info("Predictor suggestion: %s likely soon (p=%.2f); auto-execute off",
                         item["key"], item["probability"])


# ── Objects owned by the entry's runtime ───────────────────────────────────

def _alert_buffer(runtime: NovaRuntime) -> AlertBuffer:
    """The startup gate: the audit waits until it is ready. A reload brings a
    new one. (It used to hold nova.speak calls too, until 8.60.0.)"""
    if runtime.alert_buffer is None:
        runtime.alert_buffer = AlertBuffer()
    return runtime.alert_buffer


def _boot_ready(runtime: NovaRuntime) -> bool:
    return _alert_buffer(runtime).ready


async def _deliver_audit(hass: HomeAssistant, runtime: NovaRuntime, message: str,
                         target: str, critical: bool) -> None:
    """Send one audit alert through delivery, so shush, the Announcements
    switch, quiet hours, sleep and "Notifications only" apply below
    critical. When it is spoken, it is said in the chosen area; when not,
    it goes to the phones."""
    area_id = _resolve_area_id(hass, target)

    async def say_in_area(words: str) -> bool:
        if area_id is None:
            _LOGGER.warning("Nova audio: unknown area %r — ignoring", target)
            return False
        return await _announce(hass, runtime, words, area_id, critical)

    request = delivery.DeliveryRequest(
        source="infrastructure", message=message, category="infrastructure",
        urgency="critical" if critical else "high")
    await delivery.async_deliver(hass, request, speak=say_in_area)


def _boot_begin(runtime: NovaRuntime) -> None:
    """Close the startup gate until ready. Safe to call again on a reload."""
    _alert_buffer(runtime).begin()


async def mark_boot_ready(hass: HomeAssistant, runtime: NovaRuntime) -> None:
    """Open the startup gate. Nothing is held any more, so nothing is replayed."""
    async def replay(data: dict) -> None:
        return None

    await _alert_buffer(runtime).mark_ready(replay)


# ── Entry setup and unload (called from __init__.py) ────────────────────────

async def _audit_alert(hass, entry, runtime, fault_log, verdict, honorific) -> None:
    """Log the alert with any earlier occurrences folded in, deliver it in
    the chosen area (if one is set), and store this occurrence."""
    tags = verdict.get("tags", [])
    message = verdict["message"]
    earlier = await hass.async_add_executor_job(fault_log.query_related_faults, tags)
    if earlier:
        message += _history_phrase(earlier, honorific)
    _LOGGER.info("Infrastructure audit: %s", message)
    from . import nova_config
    area = str(nova_config.runtime_get(hass, entry, AUDIT_AREA_KEY, "") or "").strip()
    if area:
        # Through delivery, so shush, quiet hours and sleep apply.
        hass.async_create_task(_deliver_audit(
            hass, runtime, message, area, bool(verdict["critical"])))
    await hass.async_add_executor_job(fault_log.commit_event, verdict["message"], tags)


async def async_setup_proactive_audio(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Close the startup gate, schedule the audit (its
    timer callbacks go to the entry's NovaResources, which cancels them on
    unload), check the state ledger, then release the held calls."""
    runtime = get_runtime(entry)
    _boot_begin(runtime)

    fault_log = FaultLog()
    predictor = PredictiveHabitMatrix()

    async def _run_audit(_now=None) -> None:
        # Looked up each tick: a loaded entry without a runtime raises here.
        tick_runtime = get_runtime(entry)
        if not _boot_ready(tick_runtime) or tick_runtime.audit_running:
            return
        tick_runtime.audit_running = True
        honorific = _resolve_honorific(hass, entry)   # who is home changes
        try:
            verdict = InfrastructureTriage(
                hass, honorific=honorific,
                sensors=_audit_sensors(hass, entry)).evaluate()
            if verdict["alert_required"]:
                await _audit_alert(hass, entry, tick_runtime, fault_log, verdict, honorific)
            await _run_predictor(hass, predictor)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Infrastructure audit failed")
        finally:
            tick_runtime.audit_running = False

    unsubs = [
        async_track_time_interval(hass, _run_audit, AUDIT_INTERVAL),
        async_call_later(hass, AUDIT_STARTUP_DELAY.total_seconds(), _run_audit),
    ]
    runtime.resources.add_unsubs(unsubs)
    _LOGGER.debug("Proactive audio scheduled (audit every %s)", AUDIT_INTERVAL)

    await mark_boot_ready(hass, runtime)


async def async_unload_proactive_audio(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget this entry's objects so a reload starts fresh. The services
    stay registered and the timers were already cancelled by
    NovaResources. Safe to repeat, and safe when setup never built a
    runtime."""
    runtime = lifecycle_runtime(entry)
    if runtime is None:
        return
    runtime.alert_buffer = None
    runtime.audit_running = False
