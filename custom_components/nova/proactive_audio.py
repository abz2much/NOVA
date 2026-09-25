"""Proactive audio + infrastructure audit bridge for the Nova integration.

This module owns the ``nova.speak`` service (area-aware, prosody-shaped TTS
with media ducking) and the 15-minute infrastructure audit. It is wired into the
existing integration via two calls from ``__init__.py``:

    async_setup_entry   →  await async_setup_proactive_audio(hass, entry)
    async_unload_entry  →  await async_unload_proactive_audio(hass, entry)

It deliberately keeps the area-driven design from the feature spec rather than
routing through audio_routing/tts_helper, so the two systems stay decoupled.

Ownership: everything here belongs to the loaded Nova config entry. Its
NovaRuntime holds the intent router, state ledger, entity-lock registry and
alert buffer (each built lazily on first use, dropped on unload, rebuilt fresh
on reload) and the audit-in-progress flag. The entry's NovaResources owns the
audit timers' unsubscribe callbacks. The domain-level nova.speak and
nova.process_intent handlers resolve the loaded entry's runtime on every call
and fail with NovaRuntimeUnavailable when there is none.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import audio_routing
from .audio import NoiseGate, ProsodyController
from .automation import EntityLockRegistry, PredictiveHabitMatrix
from .boot_guard import AlertBuffer
from .const import CONF_BROADCAST_GROUP, CONF_HONORIFIC, DEFAULT_HONORIFIC, DOMAIN
from .diagnostics import FaultLog, InfrastructureTriage
from .intent import LocalIntentRouter
from .runtime import NovaRuntime, NovaRuntimeUnavailable, domain_runtime, get_runtime
from .runtime import lifecycle_runtime
from .state_ledger import StateLedger
from .vision import SpatialContextEngine

_LOGGER = logging.getLogger(__name__)

SERVICE_SPEAK = "speak"
SERVICE_PROCESS_INTENT = "process_intent"

# ── Tunables ──────────────────────────────────────────────────────────────────
# TTS entity for tts.speak. Override per-install via runtime_config key
# "proactive_tts_entity" (panel Settings), else this default is used. Set this
# to YOUR configured TTS entity id (the custom Piper voice → typically tts.piper).
DEFAULT_TTS_ENTITY = "tts.piper"
DEFAULT_ANNOUNCE_PLAYER = ""            # optional fallback player when an area has none
MEDIA_DUCK_LEVEL = 0.10                 # spec background-duck floor — see _announce() note
AUDIT_INTERVAL = timedelta(minutes=15)
AUDIT_STARTUP_DELAY = timedelta(seconds=60)
AUDIT_TARGET_AREA = "office"            # ← set to your office area_id

# Predictive habit matrix: record occupancy each audit tick and surface likely
# upcoming actions. Pre-emptive *execution* is OFF by default — Nova earns
# autonomy; until then due preemptions are logged as suggestions only.
PREDICTOR_AUTOEXECUTE = False

# Spoken-duration estimate.
WORDS_PER_SECOND = 2.6                  # ≈ 156 wpm at normal rate
TTS_PADDING_S = 0.9
TTS_MIN_S = 1.5
TTS_MAX_S = 30.0

SPEAK_SCHEMA = vol.Schema(
    {
        vol.Required("message"): cv.string,
        vol.Required("target_area"): cv.string,
        vol.Optional("critical", default=False): cv.boolean,
        vol.Optional("user_id"): cv.string,
        vol.Optional("expect_response", default=False): cv.boolean,
        vol.Optional("confirm_intent"): cv.string,
    }
)

PROCESS_INTENT_SCHEMA = vol.Schema(
    {
        vol.Required("phrase"): cv.string,
        vol.Required("target_area"): cv.string,
        vol.Optional("user_id"): cv.string,
    }
)

# Single shared controller; quiet hours fall back to its defaults (22→7), which
# match the integration's DEFAULT_OBSERVER_QUIET_START/END.
_PROSODY = ProsodyController()


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _resolve_honorific(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Presence-aware honorific (Phase C) — resolved fresh on every call, not
    cached, so a caller in a recurring callback (like _run_audit below) must
    call this again each time rather than reusing a value captured once."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        from . import nova_config
        return nova_config.runtime_get(hass, entry, CONF_HONORIFIC, DEFAULT_HONORIFIC)


def _resolve_tts_entity(runtime: NovaRuntime) -> str:
    """Prefer a panel-configured TTS entity (runtime_config), else the default."""
    rc = runtime.runtime_config
    if rc.get("proactive_tts_entity"):
        return str(rc["proactive_tts_entity"])
    return DEFAULT_TTS_ENTITY


# ── Area / entity resolution ──────────────────────────────────────────────────
@callback
def _resolve_area_id(hass: HomeAssistant, target: str) -> str | None:
    """Accept an area_id or an area name and return the canonical area_id."""
    area_reg = ar.async_get(hass)
    if area_reg.async_get_area(target) is not None:
        return target
    by_name = area_reg.async_get_area_by_name(target)
    return by_name.id if by_name is not None else None


@callback
def _resolve_broadcast_speakers(hass: HomeAssistant, runtime: NovaRuntime) -> list[str]:
    """House-wide fallback, resolved exactly like the rest of Nova: a panel
    `announcement_speakers` override, else the configured `broadcast_group`, else
    every non-satellite speaker (via audio_routing.broadcast_target)."""
    speakers = runtime.runtime_config.get("announcement_speakers")
    if speakers:
        valid = [s for s in speakers if hass.states.get(s)]
        if valid:
            return valid
    group = ""
    from . import nova_config
    for entry in hass.config_entries.async_entries(DOMAIN):
        group = (
            nova_config.runtime_get(hass, entry, CONF_BROADCAST_GROUP, "")
            or group
        )
        if group:
            break
    return audio_routing.broadcast_target(hass, broadcast_group=group)


@callback
def _resolve_targets(
    hass: HomeAssistant, runtime: NovaRuntime, area_id: str,
) -> tuple[list[str], str]:
    """Resolve announcement speakers through Nova's own routing.

    Primary: the ONE speaker explicitly assigned to the requested area
    (audio_routing.room_speaker) — replaced area auto-discovery (v7.92.0)
    since it let a stray, untagged duplicate media_player (e.g. a Music
    Assistant/AirPlay entity for a TV that wasn't tagged device_class 'tv')
    slip through and get spoken to. Falls back to the general speaker, then
    the house broadcast set, so an announcement is never silently dropped.
    Returns (targets, mode) where mode is 'area', 'broadcast', or 'none'.
    """
    assigned = audio_routing.room_speaker(hass, area_id)
    if assigned:
        return [assigned], "area"
    general = audio_routing.general_speaker_target(hass)
    if general:
        return [general], "area"

    broadcast = [
        s for s in _resolve_broadcast_speakers(hass, runtime)
        if not s.startswith("assist_satellite.")
    ]
    if broadcast:
        return broadcast, "broadcast"
    if DEFAULT_ANNOUNCE_PLAYER:
        return [DEFAULT_ANNOUNCE_PLAYER], "broadcast"
    return [], "none"


@callback
def _build_telemetry(
    hass: HomeAssistant, area_id: str, targets: list[str], critical: bool
) -> dict:
    """Ambient telemetry for prosody. Light/noise come from sensors in the
    requested area (resolved with the same audio_routing.entity_area logic used
    for speakers); media activity comes from the resolved target speakers."""
    lux_vals: list[float] = []
    db_vals: list[float] = []

    for st in hass.states.async_all("sensor"):
        if audio_routing.entity_area(hass, st.entity_id) != area_id:
            continue
        eid = st.entity_id
        device_class = st.attributes.get("device_class")
        if device_class == "illuminance" or "lux" in eid or "illuminance" in eid:
            if (v := _as_float(st.state)) is not None:
                lux_vals.append(v)
        elif device_class == "sound_pressure" or any(
            k in eid for k in ("noise", "sound", "decibel", "_db")
        ):
            if (v := _as_float(st.state)) is not None:
                db_vals.append(v)

    media_active = any(
        (s := hass.states.get(t)) is not None and str(s.state).lower() == "playing"
        for t in targets
    )

    # Fuse spatial presence (Frigate + gaze + mmWave) to decide whether the
    # listener is attending closely enough that we can skip the preamble.
    spatial = SpatialContextEngine(hass).evaluate(area_id)

    # Differential noise compensation: discount running-appliance noise so a loud
    # dishwasher doesn't push prosody to project unnecessarily.
    raw_db = max(db_vals) if db_vals else None
    ambient_db = NoiseGate(hass).compensated_db(raw_db)

    return {
        "critical_alert": critical,
        "ambient_lux": min(lux_vals) if lux_vals else None,
        "ambient_db": ambient_db,
        "media_active": media_active,
        "skip_preamble": spatial["skip_preamble"],
        "spatial_confidence": spatial["confidence"],
    }


# ── Announcement primitives ───────────────────────────────────────────────────
def _estimate_duration(message: str, speech_rate: float) -> float:
    words = max(1, len(message.split()))
    effective_wps = WORDS_PER_SECOND * max(speech_rate, 0.5)
    seconds = words / effective_wps + TTS_PADDING_S
    return max(TTS_MIN_S, min(seconds, TTS_MAX_S))


def _tts_options(profile: dict) -> dict:
    return {"rate": round(float(profile["speech_rate"]), 2)}


async def _set_volume(hass: HomeAssistant, entity_id: str, level: float) -> None:
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": entity_id, "volume_level": max(0.0, min(1.0, level))},
        blocking=True,
    )


async def _speak_tts(
    hass: HomeAssistant, runtime: NovaRuntime, targets: list[str], message: str,
    profile: dict,
) -> list[str]:
    """Call tts.speak, retrying without options if the engine rejects them.
    Returns the targets Home Assistant actually accepted the call for
    (after drop_display_targets filtering), or [] if none — used only to
    decide whether/what to record in Spoken History; existing routing,
    ducking, and error-handling behavior is otherwise unchanged."""
    try:
        from .audio_routing import drop_display_targets
        targets = drop_display_targets(hass, targets, "proactive_audio")
    except Exception:
        pass
    if not targets:
        return []
    payload = {
        "entity_id": _resolve_tts_entity(runtime),
        "media_player_entity_id": targets,
        "message": message,
    }
    try:
        await hass.services.async_call(
            "tts", "speak", {**payload, "options": _tts_options(profile)}, blocking=True
        )
        return targets
    except (vol.Invalid, HomeAssistantError):
        _LOGGER.debug("TTS rejected options; retrying without them")
        await hass.services.async_call("tts", "speak", payload, blocking=True)
        return targets


async def _announce(
    hass: HomeAssistant, runtime: NovaRuntime, message: str, area_id: str,
    critical: bool,
) -> None:
    """Shape, duck, speak, and restore — best-effort, always restoring volumes.

    Targets are resolved through audio_routing (speakers in the area, with a
    house-broadcast fallback), so nova.speak uses the same speaker selection as
    the rest of Nova. We duck the resolved targets to the computed profile
    volume for the announcement window (whisper ≈0.25 … critical =1.0) — not a
    flat 0.10, which would render an authoritative alert inaudible — and restore
    the original levels in a finally block.
    """
    targets, mode = _resolve_targets(hass, runtime, area_id)
    if not targets:
        _LOGGER.warning(
            "nova.speak: no speaker resolved for area '%s' (no area speaker and "
            "no broadcast/default fallback) — nothing to announce on", area_id,
        )
        return

    telemetry = _build_telemetry(hass, area_id, targets, critical)
    profile = _PROSODY.calculate_vocal_profile(telemetry)
    announce_volume = float(profile["volume"])

    # Duck/restore the speakers we actually announce through.
    original: dict[str, float] = {}
    for eid in targets:
        st = hass.states.get(eid)
        if st is not None and (v := _as_float(st.attributes.get("volume_level"))) is not None:
            original[eid] = v

    _LOGGER.debug(
        "nova.speak → area=%s mode=%s style=%s vol=%.2f targets=%s",
        area_id, mode, profile["style"], announce_volume, targets,
    )

    delivered: list[str] = []
    try:
        if profile["duck_media"] or original:
            for eid in original:
                try:
                    await _set_volume(hass, eid, announce_volume)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("nova.speak: failed to set volume for %s", eid)

        delivered = await _speak_tts(hass, runtime, targets, message, profile)
        await asyncio.sleep(_estimate_duration(message, float(profile["speech_rate"])))
    except Exception:  # noqa: BLE001
        _LOGGER.exception("nova.speak: announcement failed in area '%s'", area_id)
        delivered = []
    finally:
        for eid, level in original.items():
            try:
                await _set_volume(hass, eid, level)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("nova.speak: failed to restore volume for %s", eid)

    if delivered:
        try:
            from . import spoken_history
            await hass.async_add_executor_job(
                spoken_history.record, message, "manual", delivered, None,
            )
        except Exception:
            pass  # recording must never turn a delivered announcement into a failed one


def _history_phrase(matches: list[dict], honorific: str) -> str:
    """A short clause folding prior occurrences into the spoken warning."""
    count = len(matches)
    if count <= 0:
        return ""
    # honorific may be "" once nobody specific is home to address (see
    # honorific.py) — addr collapses to a single comma instead of two.
    addr = f", {honorific.title()}" if honorific else ""
    if count == 1:
        return f" For context{addr}, this has occurred once before."
    return f" For context{addr}, this has occurred {count} times before."


async def _run_predictor(hass: HomeAssistant, predictor: PredictiveHabitMatrix) -> None:
    """Sample current occupancy into the habit matrix and surface due
    pre-emptions. Execution is gated behind PREDICTOR_AUTOEXECUTE (default off) —
    until Nova has earned that autonomy, candidates are logged as suggestions.
    """
    try:
        occupied = audio_routing.currently_occupied_areas(hass)
    except Exception:  # noqa: BLE001
        occupied = []
    for area in occupied:
        await hass.async_add_executor_job(predictor.record_event, f"{area}_entry")

    due = await hass.async_add_executor_job(predictor.due_preemptions)
    for item in due:
        if PREDICTOR_AUTOEXECUTE:
            _LOGGER.info(
                "Predictor: pre-empting %s (p=%.2f) — wire a per-action handler",
                item["key"], item["probability"],
            )
        else:
            _LOGGER.info(
                "Predictor suggestion: %s likely soon (p=%.2f); auto-execute off",
                item["key"], item["probability"],
            )


# ── Service registration ──────────────────────────────────────────────────────
# ── Entry-owned proactive objects ─────────────────────────────────────────────
# Each lives on the loaded entry's NovaRuntime, is built on first use, and is
# dropped by async_unload_proactive_audio, so a reload starts with new ones.
def _state_ledger(runtime: NovaRuntime) -> StateLedger:
    """The entry's write-ahead recovery ledger."""
    if runtime.state_ledger is None:
        runtime.state_ledger = StateLedger()
    return runtime.state_ledger


def _entity_locks(runtime: NovaRuntime) -> EntityLockRegistry:
    """The entry's entity-concurrency registry."""
    if runtime.entity_locks is None:
        runtime.entity_locks = EntityLockRegistry()
    return runtime.entity_locks


def _intent_router(hass: HomeAssistant, runtime: NovaRuntime) -> LocalIntentRouter:
    """The entry's one router, so a feedback window opened by nova.speak
    survives until process_intent delivers the response."""
    if runtime.intent_router is None:
        runtime.intent_router = LocalIntentRouter(
            hass, ledger=_state_ledger(runtime), mutex=_entity_locks(runtime)
        )
    return runtime.intent_router


def _service_runtime(hass: HomeAssistant) -> NovaRuntime:
    """The runtime of the loaded Nova entry, for the domain-level service
    handlers. Raises NovaRuntimeUnavailable when no entry owns one (a loaded
    entry that lost it, or a call racing an unload): never builds objects
    outside an entry."""
    runtime = domain_runtime(hass)   # raises for a loaded entry without runtime
    if runtime is None:
        raise NovaRuntimeUnavailable("Nova is not loaded")
    return runtime


async def _reconcile_state_ledger(
    hass: HomeAssistant, runtime: NovaRuntime,
) -> list[dict]:
    """During boot, replay outstanding high-stakes intents and check whether the
    physical device actually reached the desired state — surfacing actions a
    crash or power loss interrupted. File reads run off-loop; state reads on-loop."""
    ledger = _state_ledger(runtime)
    pending = await hass.async_add_executor_job(ledger.pending_intents)
    discrepancies: list[dict] = []
    for intent in pending:
        st = hass.states.get(intent["entity_id"])
        actual = st.state if st is not None else None
        if str(actual) != intent["desired_state"]:
            discrepancies.append({**intent, "actual": actual})
    for d in discrepancies:
        _LOGGER.warning(
            "State ledger: %s was meant to be '%s' before shutdown but is '%s' — "
            "a prior action may have been interrupted",
            d.get("entity_id"), d.get("desired_state"), d.get("actual"),
        )
    # Resolved or stale intents shouldn't linger across boots.
    await hass.async_add_executor_job(ledger.compact)
    return discrepancies


# ── Boot guard + alert queue ──────────────────────────────────────────────────
# Until the integration finishes initialising (and after any config-entry reload),
# nova.speak calls are buffered rather than dropped or fired into a half-built
# system, then replayed in order once Nova reports ready. The buffer belongs to
# the entry, so a reload re-gates with a new one.
def _alert_buffer(runtime: NovaRuntime) -> AlertBuffer:
    """The entry's boot-guard buffer."""
    if runtime.alert_buffer is None:
        runtime.alert_buffer = AlertBuffer()
    return runtime.alert_buffer


def _boot_ready(runtime: NovaRuntime) -> bool:
    return _alert_buffer(runtime).ready


async def _dispatch_speak(hass: HomeAssistant, runtime: NovaRuntime, data: dict) -> None:
    """Resolve the target area and deliver one announcement. Shared by the live
    service handler and the boot-queue drainer."""
    message: str = data["message"]
    target: str = data["target_area"]
    critical: bool = data.get("critical", False)
    user_id: str | None = data.get("user_id")
    expect_response: bool = data.get("expect_response", False)
    confirm_intent: str | None = data.get("confirm_intent")

    area_id = _resolve_area_id(hass, target)
    if area_id is None:
        _LOGGER.warning("nova.speak: unknown area %r — ignoring", target)
        return
    if user_id:
        # Reserved for per-user biometric/profile filtering; threaded through
        # and logged until a profile store exists.
        _LOGGER.debug("nova.speak: addressed to user_id=%s", user_id)

    await _announce(hass, runtime, message, area_id, critical)

    # Optionally open a short voice-confirmation window for an actionable
    # announcement ("Shall I secure the garage, sir?").
    if expect_response and confirm_intent:
        try:
            await _intent_router(hass, runtime).open_feedback_window(
                {"intent": confirm_intent, "area": area_id}
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("nova.speak: failed to open feedback window")


def _boot_begin(runtime: NovaRuntime) -> None:
    """Mark the integration as initialising (gates nova.speak). Idempotent and
    reload-safe — resets readiness so a reload re-gates until ready again."""
    _alert_buffer(runtime).begin()


async def mark_boot_ready(hass: HomeAssistant, runtime: NovaRuntime) -> None:
    """Flip to ready and replay any alerts buffered during initialisation, in the
    order they arrived."""
    buffer = _alert_buffer(runtime)

    async def _cb(data: dict) -> None:
        await _dispatch_speak(hass, runtime, data)

    replayed = await buffer.mark_ready(_cb)
    if replayed:
        _LOGGER.info("Nova ready — replayed %d buffered alert(s)", replayed)


async def async_register_services(hass: HomeAssistant) -> None:
    """Register nova.speak and nova.process_intent. Idempotent — safe across
    multiple config entries."""
    if hass.services.has_service(DOMAIN, SERVICE_SPEAK):
        return

    async def _handle_speak(call: ServiceCall) -> None:
        runtime = _service_runtime(hass)
        # Boot guard: buffer until the integration is fully initialised.
        buffer = _alert_buffer(runtime)
        if not buffer.ready:
            buffer.enqueue(dict(call.data))
            _LOGGER.info("nova.speak buffered — Nova still initialising")
            return
        await _dispatch_speak(hass, runtime, call.data)

    async def _handle_process_intent(call: ServiceCall) -> None:
        phrase: str = call.data["phrase"]
        target: str = call.data["target_area"]
        user_id: str | None = call.data.get("user_id")

        runtime = _service_runtime(hass)
        area_id = _resolve_area_id(hass, target) or target
        router = _intent_router(hass, runtime)

        # If a confirmation window is open, an affirmative completes the pending
        # action; otherwise treat the phrase as a fresh local command.
        handled = await router.handle_voice_response(phrase)
        if handled.get("handled"):
            _LOGGER.info("nova.process_intent: confirmed → %s", handled)
            return
        result = await router.route(phrase, area_id, user_id=user_id)
        _LOGGER.info("nova.process_intent: %r → %s", phrase, result)

    hass.services.async_register(DOMAIN, SERVICE_SPEAK, _handle_speak, schema=SPEAK_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_PROCESS_INTENT, _handle_process_intent, schema=PROCESS_INTENT_SCHEMA
    )
    _LOGGER.info(
        "Registered services %s.%s and %s.%s",
        DOMAIN, SERVICE_SPEAK, DOMAIN, SERVICE_PROCESS_INTENT,
    )


# ── Entry wiring (called from __init__.py) ────────────────────────────────────
async def async_setup_proactive_audio(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the speak service and schedule the infrastructure audit. The
    audit timers' unsubs go to the entry's NovaResources, which cancels them
    alongside the integration's other listeners on unload."""
    runtime = get_runtime(entry)   # ownership first; setup built it already
    await async_register_services(hass)

    # Boot guard: gate nova.speak until this setup completes (reload-safe).
    _boot_begin(runtime)

    fault_log = FaultLog()
    predictor = PredictiveHabitMatrix()

    async def _run_audit(_now=None) -> None:
        # This tick's owner, checked on every tick: a loaded entry that has
        # lost its runtime raises instead of auditing unowned.
        tick_runtime = get_runtime(entry)
        if not _boot_ready(tick_runtime):
            return  # hold monitoring until the integration reports ready
        if tick_runtime.audit_running:
            return  # don't overlap a slow announcement with the next tick
        tick_runtime.audit_running = True
        # Resolved fresh every tick, not once at setup (Phase C) — this
        # runs on a recurring timer for as long as HA is up, and who's
        # actually home changes over that time.
        honorific = _resolve_honorific(hass, entry)
        try:
            verdict = InfrastructureTriage(hass, honorific=honorific).evaluate()
            if verdict["alert_required"]:
                message = verdict["message"]
                tags = verdict.get("tags", [])
                # Recall prior occurrences (file I/O off the event loop) and fold
                # them into the spoken warning.
                matches = await hass.async_add_executor_job(
                    fault_log.query_related_faults, tags
                )
                if matches:
                    message += _history_phrase(matches, honorific)
                _LOGGER.info("Infrastructure audit: %s", message)
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_SPEAK,
                    {
                        "message": message,
                        "target_area": AUDIT_TARGET_AREA,
                        "critical": verdict["critical"],
                    },
                    blocking=False,
                )
                # Persist this occurrence for future recall.
                await hass.async_add_executor_job(
                    fault_log.commit_event, verdict["message"], tags
                )

            # Habit modelling: sample occupancy and surface likely upcoming actions.
            await _run_predictor(hass, predictor)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Infrastructure audit failed")
        finally:
            tick_runtime.audit_running = False

    unsub_interval = async_track_time_interval(hass, _run_audit, AUDIT_INTERVAL)
    unsub_startup = async_call_later(
        hass, AUDIT_STARTUP_DELAY.total_seconds(), _run_audit
    )
    unsubs = [unsub_interval, unsub_startup]
    runtime.resources.add_unsubs(unsubs)
    _LOGGER.debug("Proactive audio scheduled (audit every %s)", AUDIT_INTERVAL)

    # Recover from any high-stakes action interrupted by a crash before opening
    # the gate, then replay anything buffered during initialisation.
    try:
        await _reconcile_state_ledger(hass, runtime)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("State ledger reconciliation failed")
    await mark_boot_ready(hass, runtime)


async def async_unload_proactive_audio(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the services if no other entry needs them, and release this
    entry's proactive objects. The audit timers are NovaResources' to cancel
    (async_unload_entry closes it before calling this). Safe to repeat, and
    safe for an entry whose setup never built a runtime."""
    others = [
        other for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id and lifecycle_runtime(other) is not None
    ]
    if not others:
        for svc in (SERVICE_SPEAK, SERVICE_PROCESS_INTENT):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    runtime = lifecycle_runtime(entry)
    if runtime is not None:
        runtime.intent_router = None
        runtime.state_ledger = None
        runtime.entity_locks = None
        runtime.alert_buffer = None
        runtime.audit_running = False
