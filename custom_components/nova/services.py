"""Nova's Home Assistant services, registered once for the process lifetime.

async_setup() in __init__.py calls async_setup_services() at integration
scope, so the 32 nova.* services exist from the first setup until Home
Assistant stops. Config-entry setup, unload, reload and setup failure never
register or remove them.

A registered service is not a loaded Nova. Every handler therefore resolves
the loaded config entry and its NovaRuntime when it is called, never when it
is registered, and a call in any other state fails with a translated
ServiceValidationError:

* no Nova entry configured          -> no_entry
* configured but not loaded         -> not_loaded
* setting up or unloading           -> reloading
* setup failed, retrying or stuck   -> setup_failed
* more than one Nova entry          -> multiple_entries

A LOADED entry without a NovaRuntime is an internal lifecycle fault and
still raises NovaRuntimeUnavailable (from get_runtime).

nova.speak has one narrow exception, the boot-alert buffer: while the entry
is SETUP_IN_PROGRESS and its current runtime has an alert buffer that is
not ready yet, the call is queued there exactly as before and replayed in
order once setup marks the buffer ready (proactive_audio.mark_boot_ready).

Handlers never capture an entry, provider client, sentinel or runtime
configuration: async_setup_services() only defines them and registers them,
and each reads runtime.client, runtime.sentinel and runtime configuration
through the entry it resolved for that call. Nothing here reads or writes
hass.data.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import proactive_audio
from .audio_routing import broadcast_target
from .briefing import async_briefing
from .camera import async_analyze_camera, async_auto_analyze_on_event
from .const import (
    CONF_BEDROOM_AREAS,
    CONF_BROADCAST_GROUP,
    CONF_TTS_ENGINE,
    CONF_TTS_PREMIUM_CONTEXTS,
    CONF_TTS_PREMIUM_ENGINE,
    DEFAULT_HONORIFIC,
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_PREMIUM_CONTEXTS,
    DEFAULT_TTS_PREMIUM_ENGINE,
    DOMAIN,
)
from .database import get_stats, purge_old_records
from .proactive_audio import (
    PROCESS_INTENT_SCHEMA,
    SERVICE_PROCESS_INTENT,
    SERVICE_SPEAK,
    SPEAK_SCHEMA,
)
from .reminders import async_add_reminder_service
from .routines import async_run_routine
from .runtime import (
    NovaConfigEntry,
    NovaRuntime,
    get_runtime,
    lifecycle_runtime,
    lifecycle_runtime_config,
    set_observer_running,
)
from .scenes import async_activate_by_intent
from .summary import async_summarise

_LOGGER = logging.getLogger(__name__)

# ── Lifecycle resolution ──────────────────────────────────────────────────────

# Translation keys (strings.json "exceptions") for an entry that is not
# LOADED. A state Home Assistant adds later reads as not_loaded.
_STATE_ERRORS: dict[ConfigEntryState, str] = {
    ConfigEntryState.NOT_LOADED: "not_loaded",
    ConfigEntryState.SETUP_IN_PROGRESS: "reloading",
    ConfigEntryState.UNLOAD_IN_PROGRESS: "reloading",
    ConfigEntryState.SETUP_ERROR: "setup_failed",
    ConfigEntryState.SETUP_RETRY: "setup_failed",
    ConfigEntryState.MIGRATION_ERROR: "setup_failed",
    ConfigEntryState.FAILED_UNLOAD: "setup_failed",
}


def _service_error(translation_key: str) -> ServiceValidationError:
    """A user-facing service error. The message comes only from the
    translation: no configuration, credential or exception detail."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=translation_key)


def _nova_entries(hass: HomeAssistant) -> list[NovaConfigEntry]:
    """Every Nova config entry (ignored discovery entries aside)."""
    return hass.config_entries.async_entries(DOMAIN, include_ignore=False)


@callback
def async_get_loaded_entry(hass: HomeAssistant) -> NovaConfigEntry:
    """The one LOADED Nova config entry, resolved for this call.

    Raises ServiceValidationError (translated) when there is no entry, more
    than one, or the entry is in any state other than LOADED."""
    entries = _nova_entries(hass)
    if not entries:
        raise _service_error("no_entry")
    if len(entries) > 1:
        raise _service_error("multiple_entries")
    entry = entries[0]
    if entry.state is ConfigEntryState.LOADED:
        return entry
    raise _service_error(_STATE_ERRORS.get(entry.state, "not_loaded"))


@callback
def async_resolve_loaded(hass: HomeAssistant) -> tuple[NovaConfigEntry, NovaRuntime]:
    """The loaded entry and its NovaRuntime, resolved for this call.

    Lifecycle states raise ServiceValidationError (async_get_loaded_entry);
    a LOADED entry without a runtime raises NovaRuntimeUnavailable."""
    entry = async_get_loaded_entry(hass)
    return entry, get_runtime(entry)


@callback
def _boot_alert_buffer(hass: HomeAssistant):
    """The alert buffer nova.speak may queue into, or None.

    Only while the one Nova entry is SETUP_IN_PROGRESS, entry.runtime_data
    already holds its NovaRuntime, and that runtime's alert buffer exists and
    is not ready yet. Never builds a buffer."""
    entries = _nova_entries(hass)
    if len(entries) != 1:
        return None
    entry = entries[0]
    if entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
        return None
    runtime = lifecycle_runtime(entry)
    if runtime is None:
        return None
    buffer = runtime.alert_buffer
    if buffer is None or buffer.ready:
        return None
    return buffer


# ── Shared helpers ────────────────────────────────────────────────────────────

def _live_honorific(hass: HomeAssistant) -> str:
    """Presence-aware honorific (Phase C), resolved fresh on every call.
    Every call site below already re-fetched entry.options/data on each
    event/service-call/tick (never cached across them), so this is a
    straight replacement — solo occupant gets their own honorific (or the
    global default), nobody/multiple home gets none at all. See
    honorific.py. Falls back to DEFAULT_HONORIFIC only if the lookup
    itself errors."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return DEFAULT_HONORIFIC


def _get_tts(hass: HomeAssistant, entry: NovaConfigEntry, context: str = "chat") -> str | None:
    """
    Return the TTS entity to use for this context.

    For 'premium' contexts (briefing/doorbell/camera/recognition by default)
    we route to the premium TTS engine (ElevenLabs) if configured.
    All other contexts use the regular engine (Piper or similar).
    """
    from .tts_helper import resolve_tts_for_context
    # Effective config (nova_config wins). entry.options is empty when all
    # config lives in the panel store, which previously left the briefing unable
    # to resolve its TTS engine — so it silently bailed before announcing.
    try:
        from . import nova_config
        cfg = nova_config.effective_config(entry)
    except Exception:
        cfg = {**dict(entry.data), **dict(entry.options)}

    regular = cfg.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE)
    premium = cfg.get(CONF_TTS_PREMIUM_ENGINE, DEFAULT_TTS_PREMIUM_ENGINE)
    premium_contexts = cfg.get(CONF_TTS_PREMIUM_CONTEXTS, DEFAULT_TTS_PREMIUM_CONTEXTS)

    return resolve_tts_for_context(
        hass, context, regular, premium, premium_contexts
    )


def _get_speakers(hass: HomeAssistant, entry: NovaConfigEntry) -> list[str]:
    """
    Return the list of speakers for PROACTIVE ANNOUNCEMENTS.
    Checks runtime_config.announcement_speakers first, falls back to
    broadcast_group from entry options/data.
    """
    import json as _json

    def _as_list(raw):
        if not raw:
            return None
        try:
            v = _json.loads(raw) if isinstance(raw, str) else raw
            return v if isinstance(v, list) and v else None
        except Exception:
            return None

    # Panel-live value first, from NovaRuntime. A loaded entry with no
    # runtime raises here rather than hiding behind the fallback below.
    rc = lifecycle_runtime_config(entry)
    try:
        live = _as_list(rc.get("announcement_speakers"))
        if live:
            _LOGGER.debug("Announcement speakers from panel config: %s", live)
            return live
    except Exception as exc:
        _LOGGER.debug("Error reading announcement_speakers: %s", exc)

    # Authoritative effective config (nova_config wins). entry.options is empty
    # when all config lives in the panel store — previously this fell through to
    # an empty broadcast group, leaving announcements with no speakers.
    try:
        from . import nova_config
        cfg = nova_config.effective_config(entry)
    except Exception:
        cfg = {**dict(entry.data), **dict(entry.options)}
    speakers = _as_list(cfg.get("announcement_speakers"))
    if speakers:
        return speakers
    result = broadcast_target(hass, broadcast_group=(cfg.get(CONF_BROADCAST_GROUP) or None))
    _LOGGER.debug("Announcement speakers from broadcast_group: %s", result)
    return result


# ── Registration ──────────────────────────────────────────────────────────────

@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register all 32 Nova services once, at integration setup.

    Idempotent: a service that is already registered keeps its handler.
    Nothing here captures an entry, runtime, client, sentinel or config;
    every handler resolves those per call."""

    def _register(name: str, handler, schema: vol.Schema | None = None) -> None:
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

    async def _camera(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="camera")
        spk = _get_speakers(hass, entry)
        await async_analyze_camera(hass, call, runtime.client, honorific, tts, spk)

    _register(
        "analyze_camera", _camera,
        vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("prompt"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
            # Short clip capture (documented in services.yaml). No schema
            # default: an omitted value keeps the handler's own fallback
            # (1 frame, 1.2 s apart).
            vol.Optional("frames"): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)),
            vol.Optional("interval"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=5)),
        }),
    )

    async def _analyze_on_event(call: ServiceCall) -> None:
        """Push-triggered analyze — intended for doorbell/motion automations."""
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="doorbell")
        spk = _get_speakers(hass, entry)
        entity_id = call.data["entity_id"]
        reason    = call.data.get("reason", "Activity detected")
        await async_auto_analyze_on_event(
            hass, runtime.client, honorific, tts, spk, entity_id, reason
        )

    _register(
        "analyze_on_event", _analyze_on_event,
        vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("reason", default="Activity detected"): cv.string,
        }),
    )

    # ── Doorbell backlog → training data ───────────────────────────────────────
    async def _train_backlog(call: ServiceCall) -> None:
        """Analyse the Nest doorbell's recorded event history into the training
        log. Best-effort; reports how many events it managed to analyse."""
        entry, runtime = async_resolve_loaded(hass)
        client = runtime.client
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        limit = int(call.data.get("limit", 40) or 40)
        from . import doorbell_training
        from .camera import _FakeCall, active_cameras, active_camera_states

        # Resolve a doorbell camera entity for naming/attribution
        doorbell_entity = None
        for st in active_camera_states(hass):
            if any(k in st.entity_id for k in ("doorbell", "front_door")):
                doorbell_entity = st.entity_id
                break
        if doorbell_entity is None:
            cams = active_cameras(hass)
            doorbell_entity = cams[0] if cams else "camera.front_doorbell"

        async def _analyze_image(image_bytes, label):
            # No honorific in the task instruction — see camera.py's
            # _analyze_doorbell_press comment: "what {honorific} would want
            # to know" leaks into the model's own third-person phrasing
            # ("Sir has a visitor..." instead of "You have a visitor, sir").
            prompt = (
                f"Recorded doorbell event ({label}). Identify who is at the door — "
                f"appearance, clothing, packages, vehicles. "
                f"Focus on what the resident would want to know."
            )
            fc = _FakeCall({"entity_id": doorbell_entity, "prompt": prompt, "announce": False})
            return await async_analyze_camera(
                hass, fc, client, honorific, None, [],
                gate_announce=True, force_images=[image_bytes],
            )

        report = await doorbell_training.scan_backlog(hass, _analyze_image, honorific, limit=limit)
        _LOGGER.info("Nova doorbell backlog scan: %s", report)
        try:
            from .websocket import nova_log
            if report.get("ok"):
                nova_log("CAMERA",
                           f"Backlog training: analysed {report['analyzed']} doorbell "
                           f"event(s) into the dataset (of {report['found']} found)")
            else:
                nova_log("CAMERA", f"Backlog training: {report.get('reason', 'no events analysed')}")
        except Exception:
            pass

    _register(
        "train_doorbell_backlog", _train_backlog,
        vol.Schema({
            vol.Optional("limit", default=40): vol.Coerce(int),
        }),
    )

    # ── Package / mail — on-demand check ───────────────────────────────────────
    async def _check_packages(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="package")
        spk = _get_speakers(hass, entry)
        from . import package_monitor
        cam = call.data.get("entity_id")
        report = await package_monitor.periodic_check(
            hass, runtime.client, honorific, tts, spk, configured_camera=cam
        )
        _LOGGER.info("Nova manual package check: %s", report)

    _register(
        "check_packages", _check_packages,
        vol.Schema({
            vol.Optional("entity_id"): cv.entity_id,
        }),
    )

    # ── Briefing ──────────────────────────────────────────────────────────────
    async def _briefing(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="briefing")
        spk = _get_speakers(hass, entry)
        await async_briefing(hass, call, runtime.client, honorific, tts, spk)

    async def _nova_backup(call):
        entry, runtime = async_resolve_loaded(hass)
        from . import action_log
        from .backup import create_backup
        request_id = action_log.new_request_id()
        requested_by_user_id = getattr(getattr(call, "context", None), "user_id", None)
        action_id = await hass.async_add_executor_job(
            lambda: action_log.start(
                request_id, "backup", "ha_service",
                requested_by_user_id=requested_by_user_id,
            )
        )
        try:
            path = await hass.async_add_executor_job(create_backup, hass.config.path())
        except Exception:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "failed", reason_code="backup_failed")
            )
            raise
        _LOGGER.info("Nova state backed up to %s", path)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        await hass.services.async_call("persistent_notification", "create", {
            "title": "Nova backup",
            "message": (f"Nova state saved to:\n`{path}`\n\nDownload this file before "
                        "re-flashing so memory, patterns and knowledge survive a wipe."),
            "notification_id": "nova_backup",
        }, blocking=False)

    _register("backup", _nova_backup)

    async def _nova_restore(call):
        entry, runtime = async_resolve_loaded(hass)
        from . import action_log
        from .backup import restore_backup
        archive = (call.data or {}).get("archive", "") or ""
        request_id = action_log.new_request_id()
        requested_by_user_id = getattr(getattr(call, "context", None), "user_id", None)
        action_id = await hass.async_add_executor_job(
            lambda: action_log.start(
                request_id, "restore", "ha_service",
                requested_by_user_id=requested_by_user_id,
            )
        )
        try:
            path = await hass.async_add_executor_job(
                restore_backup, hass.config.path(), archive)
        except Exception:
            await hass.async_add_executor_job(
                lambda: action_log.set_execution(
                    action_id, "failed", reason_code="restore_failed")
            )
            raise
        _LOGGER.info("Nova state restored from %s", path)
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "accepted")
        )
        await hass.services.async_call("persistent_notification", "create", {
            "title": "Nova restore",
            "message": (f"Nova state restored from:\n`{path}`\n\nRestart Home Assistant "
                        "to load the restored memory and patterns."),
            "notification_id": "nova_restore",
        }, blocking=False)

    _register(
        "restore", _nova_restore,
        vol.Schema({vol.Optional("archive"): str}))

    _register(
        "briefing", _briefing,
        vol.Schema({
            vol.Optional("announce", default=True): cv.boolean,
            vol.Optional("include_weather", default=True): cv.boolean,
            vol.Optional("include_calendar", default=True): cv.boolean,
            vol.Optional("include_presence", default=True): cv.boolean,
            vol.Optional("include_events", default=True): cv.boolean,
            vol.Optional("include_energy", default=True): cv.boolean,
            vol.Optional("hours", default=12): vol.All(int, vol.Range(min=1, max=48)),
        }),
    )

    # ── Scene by intent ───────────────────────────────────────────────────────
    async def _scene_intent(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="chat")
        spk = _get_speakers(hass, entry)
        await async_activate_by_intent(hass, call, runtime.client, honorific, tts, spk)

    _register(
        "scene_by_intent", _scene_intent,
        vol.Schema({
            vol.Required("intent"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
        }),
    )

    # ── Routine ───────────────────────────────────────────────────────────────
    async def _routine(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="routine")
        spk = _get_speakers(hass, entry)
        await async_run_routine(hass, call, honorific, tts, spk)

    _register(
        "routine", _routine,
        vol.Schema({
            vol.Required("name"): cv.string,
        }),
    )

    # ── Add reminder ──────────────────────────────────────────────────────────
    async def _add_reminder(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="reminder")
        spk = _get_speakers(hass, entry)
        await async_add_reminder_service(hass, call, honorific, tts, spk)

    _register(
        "add_reminder", _add_reminder,
        vol.Schema({
            vol.Required("label"): cv.string,
            vol.Required("trigger_at"): cv.string,
            vol.Optional("repeat"): vol.In(["daily", "weekly", "hourly"]),
            vol.Optional("require_home", default=True): cv.boolean,
            vol.Optional("respect_quiet", default=True): cv.boolean,
        }),
    )

    async def _summary(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="summary")
        spk = _get_speakers(hass, entry)
        await async_summarise(hass, call, runtime.client, honorific, tts, spk)

    _register(
        "conversation_summary", _summary,
        vol.Schema({
            vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=168)),
            vol.Optional("device_id"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
            vol.Optional("store", default=True): cv.boolean,
        }),
    )

    async def _sentinel_start(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        await runtime.sentinel.async_start()

    _register("sentinel_start", _sentinel_start)

    async def _sentinel_stop(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        await runtime.sentinel.async_stop()

    _register("sentinel_stop", _sentinel_stop)

    async def _db_purge(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        days = call.data.get("days", 30)
        deleted = await hass.async_add_executor_job(purge_old_records, days)
        _LOGGER.info("Nova DB purge: %d records deleted (>%d days)", deleted, days)

    _register(
        "database_purge", _db_purge,
        vol.Schema({
            vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=365))
        }),
    )

    async def _db_stats(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        stats = await hass.async_add_executor_job(get_stats)
        hass.bus.async_fire("nova_db_stats", stats)

    _register("database_stats", _db_stats)

    async def _replay_policy(call: ServiceCall) -> None:
        """Replay recorded decisions of a kind against candidate confidence
        thresholds and report the best-separating threshold from real outcomes.
        Read-only — evaluates history, never changes behaviour."""
        entry, runtime = async_resolve_loaded(hass)
        from . import replay as _replay
        kind = str(call.data.get("kind", "")).strip()
        min_samples = call.data.get("min_samples") or _replay.DEFAULT_MIN_SAMPLES
        result = await hass.async_add_executor_job(
            _replay.replay_kind, kind, int(min_samples))
        hass.bus.async_fire("nova_replay_result", result)
        if result.get("ready"):
            rec = result["recommended"]
            _LOGGER.info(
                "Replay[%s]: %d judged decisions — best threshold %.2f "
                "(accuracy %.0f%%; would avoid %d mistakes, lose %d good calls)",
                kind, result.get("samples", 0), rec["threshold"],
                rec["accuracy"] * 100, rec["mistakes_avoided"], rec["good_calls_lost"])
        else:
            _LOGGER.info("Replay[%s]: %s", kind, result.get("reason", "no data"))

    _register(
        "replay_policy", _replay_policy,
        vol.Schema({
            vol.Required("kind"): str,
            vol.Optional("min_samples"): vol.All(int, vol.Range(min=1, max=100000)),
        }),
    )

    # ── v5.2 Observer Mode services ──────────────────────────────────────────

    async def _nap(call: ServiceCall) -> None:
        """Manual mute for N minutes (default 30). Suppresses non-critical
        announcements until the duration elapses."""
        entry, runtime = async_resolve_loaded(hass)
        from . import sleep_detection as sd
        duration = call.data.get("duration_minutes", 30)
        sd.set_nap(duration)
        hass.bus.async_fire("nova_observer_nap", {"duration_minutes": duration})

    _register(
        "nap", _nap,
        vol.Schema({
            vol.Optional("duration_minutes", default=30):
                vol.All(int, vol.Range(min=1, max=480)),
        }),
    )

    async def _shush(call: ServiceCall) -> None:
        """Tell Nova to stop announcing. Pass all=true to mute every
        non-critical announcement; critical safety alerts always pass."""
        entry, runtime = async_resolve_loaded(hass)
        from . import output_gate
        entity_id = call.data.get("entity_id")
        category  = call.data.get("category")
        shush_all = bool(call.data.get("all", False))
        result = output_gate.shush(entity_id=entity_id, category=category, all=shush_all)
        hass.bus.async_fire("nova_observer_shushed", result)
        _LOGGER.info("Nova shushed: %s", result)

    _register(
        "shush", _shush,
        vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
            vol.Optional("all"): cv.boolean,
        }),
    )

    async def _unshush(call: ServiceCall) -> None:
        """Undo a shush. Called with no args clears ALL mutes."""
        entry, runtime = async_resolve_loaded(hass)
        from . import output_gate
        entity_id = call.data.get("entity_id")
        category  = call.data.get("category")
        result = output_gate.unshush(entity_id=entity_id, category=category)
        hass.bus.async_fire("nova_observer_unshushed", result)

    _register(
        "unshush", _unshush,
        vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
        }),
    )

    async def _observer_start(call: ServiceCall) -> None:
        """Start the observer manually (even if config has it disabled)."""
        # Ownership first: never start an unowned observer.
        entry, runtime = async_resolve_loaded(hass)
        from . import observer as observer_mod, nova_config as _jc
        # Fresh effective config (data + options + panel, panel winning) so a
        # manual start honors current panel settings, not stale entry data.
        observer_config = await hass.async_add_executor_job(_jc.effective_config, entry)
        await observer_mod.start(hass, observer_config, entry=entry)
        set_observer_running(entry, True)
        _LOGGER.info("Observer started via service call")

    _register("observer_start", _observer_start)

    async def _lockdown(call: ServiceCall) -> None:
        """Engage or lift the formal lockdown state (alarm-armed posture)."""
        # Lifecycle first: after unload or during a reload this must fail
        # before cognitive_core could build a lockdown manager on demand.
        entry, runtime = async_resolve_loaded(hass)
        from . import cognitive_core
        raw = call.data.get("state", call.data.get("enabled", "on"))
        on = raw in (True, "on", "true", "True", "engage", "lock", 1, "1")
        ok = await cognitive_core.request_lockdown(
            on, reason=call.data.get("reason", "requested via service"), hass=hass)
        if not ok:
            _LOGGER.warning("Lockdown service: request could not be handled (no hass)")
        else:
            _LOGGER.info("Lockdown %s via service call", "engaged" if on else "lifted")

    _register("lockdown", _lockdown)

    async def _remember(call: ServiceCall) -> None:
        """Teach Nova a durable fact or preference (knowledge store)."""
        entry, runtime = async_resolve_loaded(hass)
        from . import knowledge
        key = str(call.data.get("key", "")).strip()
        value = str(call.data.get("value", "")).strip()
        if not key or not value:
            _LOGGER.warning("nova.remember: 'key' and 'value' are required")
            return
        subject = str(call.data.get("subject", knowledge.DEFAULT_SUBJECT)).strip() \
            or knowledge.DEFAULT_SUBJECT
        kind = str(call.data.get("kind", "fact"))
        ttl = call.data.get("ttl_seconds")
        try:
            ttl = float(ttl) if ttl not in (None, "") else None
        except (TypeError, ValueError):
            ttl = None
        f = await hass.async_add_executor_job(
            lambda: knowledge.remember(key, value, subject=subject, kind=kind,
                                       source="stated", ttl_seconds=ttl))
        if f:
            _LOGGER.info("nova.remember: stored %s/%s", subject, key)
        else:
            _LOGGER.warning("nova.remember: store failed for %s/%s", subject, key)

    _register("remember", _remember)

    async def _forget(call: ServiceCall) -> None:
        """Forget a stored fact by id, or by key (with optional subject)."""
        entry, runtime = async_resolve_loaded(hass)
        from . import knowledge
        fid = call.data.get("id")
        key = call.data.get("key")
        subject = call.data.get("subject")
        try:
            fid = int(fid) if fid not in (None, "") else None
        except (TypeError, ValueError):
            fid = None
        removed = await hass.async_add_executor_job(
            lambda: knowledge.forget(fact_id=fid, subject=subject, key=key))
        _LOGGER.info("nova.forget: removed %d fact(s)", removed)

    _register("forget", _forget)

    async def _observer_stop(call: ServiceCall) -> None:
        """Stop the observer."""
        # Ownership first: never stop an unowned observer.
        entry, runtime = async_resolve_loaded(hass)
        from . import observer as observer_mod
        await observer_mod.stop()
        set_observer_running(entry, False)
        _LOGGER.info("Observer stopped via service call")

    _register("observer_stop", _observer_stop)

    async def _observer_status(call: ServiceCall) -> None:
        """Fire event with current observer state — mute list, recent activity."""
        entry, runtime = async_resolve_loaded(hass)
        from . import output_gate, observer as observer_mod, sleep_detection as sd
        status = output_gate.status()
        status["running"] = observer_mod.is_running()
        # Check if user is currently being treated as sleeping
        bedroom_areas = entry.options.get(
            CONF_BEDROOM_AREAS,
            entry.data.get(CONF_BEDROOM_AREAS, [])
        ) or []
        sleeping, reason = sd.is_sleeping(
            hass,
            bedroom_area_ids=bedroom_areas,
            quiet_start=entry.options.get(
                "observer_quiet_start",
                entry.data.get("observer_quiet_start", "22:00")
            ),
            quiet_end=entry.options.get(
                "observer_quiet_end",
                entry.data.get("observer_quiet_end", "07:00")
            ),
        )
        status["sleeping"] = sleeping
        status["sleep_reason"] = reason
        status["bedroom_areas"] = list(bedroom_areas)
        hass.bus.async_fire("nova_observer_status", status)
        _LOGGER.info("Observer status: %s", status)

    _register("observer_status", _observer_status)

    # v5.6.0: Automation creation service
    async def _create_automation(call: ServiceCall) -> None:
        """Create an HA automation from service call data."""
        entry, runtime = async_resolve_loaded(hass)
        from .automation_creator import create_automation
        result = await create_automation(
            hass,
            alias=call.data.get("alias", "Unnamed"),
            description=call.data.get("description", ""),
            trigger=call.data.get("trigger"),
            condition=call.data.get("condition"),
            action=call.data.get("action"),
            mode=call.data.get("mode", "single"),
            source="ha_service",
            requested_by_user_id=getattr(getattr(call, "context", None), "user_id", None),
        )
        if result.get("success"):
            hass.bus.async_fire("nova_automation_created", result)
        else:
            _LOGGER.warning("Automation creation failed: %s", result.get("error"))

    _register("create_automation", _create_automation)

    # v5.6.0: Doorbell pipeline diagnostic
    async def _diagnose_doorbell(call: ServiceCall) -> None:
        """Run doorbell pipeline diagnostics and fire event with results."""
        entry, runtime = async_resolve_loaded(hass)
        diag = {"checks": [], "verdict": "unknown"}

        # Check 1: Does the doorbell automation exist?
        auto_state = hass.states.get("automation.doorbell_motion_analysis")
        if auto_state:
            diag["checks"].append({"check": "automation exists", "ok": True, "state": auto_state.state})
        else:
            diag["checks"].append({"check": "automation exists", "ok": False, "detail": "automation.doorbell_motion_analysis not found"})
            diag["verdict"] = "Automation missing — create it or check the entity ID"
            hass.bus.async_fire("nova_doorbell_diag", diag)
            return

        # Check 2: Is it enabled?
        if auto_state.state != "on":
            diag["checks"].append({"check": "automation enabled", "ok": False, "state": auto_state.state})
            diag["verdict"] = "Automation exists but is disabled"
            hass.bus.async_fire("nova_doorbell_diag", diag)
            return
        diag["checks"].append({"check": "automation enabled", "ok": True})

        # Check 3: Do we have camera entities?
        from .camera import active_cameras as _active_cams
        cameras = _active_cams(hass)
        diag["checks"].append({"check": "cameras found", "ok": len(cameras) > 0, "cameras": cameras[:10]})

        # Check 4: Is nova.analyze_on_event ready? Registration alone no
        # longer means that (services outlive the entry), so the Nova entry
        # must also be loaded.
        svc_exists = (hass.services.has_service(DOMAIN, "analyze_on_event")
                      and entry.state is ConfigEntryState.LOADED)
        diag["checks"].append({"check": "analyze_on_event service", "ok": svc_exists})

        # Check 5: TTS working?
        tts_entities = [s.entity_id for s in hass.states.async_all("tts")]
        diag["checks"].append({"check": "TTS entities", "ok": len(tts_entities) > 0, "entities": tts_entities})

        if all(c["ok"] for c in diag["checks"]):
            diag["verdict"] = "All checks passed — trigger the doorbell and watch logs for nova.analyze_on_event"
        else:
            failed = [c["check"] for c in diag["checks"] if not c["ok"]]
            diag["verdict"] = f"Failed checks: {', '.join(failed)}"

        _LOGGER.info("Doorbell diagnostic: %s", diag)
        hass.bus.async_fire("nova_doorbell_diag", diag)

    _register("diagnose_doorbell", _diagnose_doorbell)

    # v5.6.5: Test notification service
    async def _test_notify(call: ServiceCall) -> None:
        """Send a test notification to every configured normal target."""
        entry, runtime = async_resolve_loaded(hass)
        rc = lifecycle_runtime_config(entry)
        notify_config = dict(entry.data)
        notify_config.update(entry.options)
        notify_config.update(rc)
        requested_by_user_id = getattr(getattr(call, "context", None), "user_id", None)
        from .notify_targets import async_send_configured_notifications
        sent = await async_send_configured_notifications(
            hass, notify_config,
            {
                "title": "Nova",
                "message": "This is a test notification from Nova. If you see this, phone notifications are working.",
            },
            action="test_notify", source="ha_service",
            requested_by_user_id=requested_by_user_id,
        )
        if not sent:
            _LOGGER.warning("Test notify: no configured service accepted the alert")

    _register("test_notify", _test_notify)

    # v5.6.7: Test TTS with Nova voice
    async def _test_tts(call: ServiceCall) -> None:
        """Play a test tone using Nova Piper voice on the broadcast group."""
        entry, runtime = async_resolve_loaded(hass)
        from .tts_helper import resolve_tts_entity, async_announce
        from .audio_routing import broadcast_target
        from . import nova_config
        cfg = nova_config.effective_config(entry)
        tts_entity = resolve_tts_entity(hass, cfg.get("tts_engine", "auto"))
        speakers = broadcast_target(
            hass,
            broadcast_group=(cfg.get("broadcast_group") or None),
            announcement_speakers=cfg.get("announcement_speakers"),
        )
        if tts_entity and speakers:
            await async_announce(
                hass,
                "Nova test tone. If you hear this in a British accent, the Nova voice is working.",
                tts_entity,
                speakers,
                context="test",
            )
            _LOGGER.info("Test TTS sent via %s → %s", tts_entity, speakers)
        else:
            _LOGGER.warning(
                "Test TTS: no announcement speakers configured — choose speakers "
                "in Settings → Announcement Speakers (tts=%s, speakers=%s)",
                tts_entity, speakers)

    _register("test_tts", _test_tts)

    # v5.7.00: Routing diagnostic — dumps current routing state to log
    async def _test_routing(call: ServiceCall) -> None:
        """Dump routing diagnostics to the HA log."""
        entry, runtime = async_resolve_loaded(hass)
        from .audio_routing import (
            broadcast_target, reply_target, observer_speak_target,
            currently_occupied_areas, anyone_home, all_areas_with_satellite,
            room_speaker, satellites_in_area,
        )
        from .tts_helper import resolve_tts_entity, find_best_tts_entity

        broadcast_group = entry.options.get(
            "broadcast_group", entry.data.get("broadcast_group", ""))
        tts_ent = resolve_tts_entity(
            hass, entry.options.get("tts_engine",
                                     entry.data.get("tts_engine", "auto")))
        bcast = broadcast_target(hass, broadcast_group=broadcast_group or None)
        occupied = currently_occupied_areas(hass)
        home = anyone_home(hass)
        sat_areas = all_areas_with_satellite(hass)

        # Read announcement_speakers and satellite_pairings from the one
        # live runtime_config dict.
        import json as _json
        rc = lifecycle_runtime_config(entry)
        ann_spk = None
        try:
            raw = rc.get("announcement_speakers")
            if raw:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, list):
                    ann_spk = parsed
        except Exception:
            pass

        # Read satellite_pairings
        sat_pairs = None
        try:
            raw = rc.get("satellite_pairings")
            if raw:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    sat_pairs = parsed
        except Exception:
            pass

        _LOGGER.warning("=== Nova ROUTING DIAGNOSTIC ===")
        _LOGGER.warning("TTS entity: %s", tts_ent)
        _LOGGER.warning("TTS auto-pick: %s", find_best_tts_entity(hass))
        _LOGGER.warning("Broadcast group (config): '%s'", broadcast_group)
        _LOGGER.warning("Broadcast target resolved: %s", bcast)
        _LOGGER.warning("Announcement speakers (panel): %s", ann_spk)
        _LOGGER.warning("Satellite pairings (panel): %s", sat_pairs)
        _LOGGER.warning("Anyone home: %s", home)
        _LOGGER.warning("Occupied areas: %s", occupied)
        _LOGGER.warning("Areas with satellites: %s", sat_areas)
        for area_id in sat_areas:
            sats = satellites_in_area(hass, area_id)
            assigned = room_speaker(hass, area_id)
            _LOGGER.warning("  Area '%s': sats=%s, assigned room speaker=%s",
                            area_id, sats, assigned)
            for sat in sats:
                target = reply_target(
                    hass, satellite_entity_id=sat,
                    satellite_pairings=sat_pairs,
                )
                _LOGGER.warning("    reply_target(%s) → %s", sat, target)

        # Test observer routing for each urgency
        for urg in ("low", "medium", "high", "critical"):
            targets, mode = observer_speak_target(
                hass, urgency=urg,
                broadcast_group=broadcast_group or None,
                announcement_speakers=ann_spk,
                is_sleeping=False,
            )
            _LOGGER.warning("  observer(%s): targets=%s, mode=%s",
                            urg, targets, mode)
        _LOGGER.warning("=== END ROUTING DIAGNOSTIC ===")

    _register("test_routing", _test_routing)

    # ── v6.8 Proactive audio (nova.speak / nova.process_intent) ──────────────
    async def _speak(call: ServiceCall) -> None:
        # Boot guard: only while the entry is still setting up and its own
        # runtime's buffer is gated, queue the call for in-order replay.
        buffer = _boot_alert_buffer(hass)
        if buffer is not None:
            buffer.enqueue(dict(call.data))
            _LOGGER.info("nova.speak buffered — Nova still initialising")
            return
        entry, runtime = async_resolve_loaded(hass)
        await proactive_audio._dispatch_speak(hass, runtime, call.data)

    _register(SERVICE_SPEAK, _speak, SPEAK_SCHEMA)

    async def _process_intent(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        phrase: str = call.data["phrase"]
        target: str = call.data["target_area"]
        user_id: str | None = call.data.get("user_id")

        area_id = proactive_audio._resolve_area_id(hass, target) or target
        router = proactive_audio._intent_router(hass, runtime)

        # If a confirmation window is open, an affirmative completes the pending
        # action; otherwise treat the phrase as a fresh local command.
        handled = await router.handle_voice_response(phrase)
        if handled.get("handled"):
            _LOGGER.info("nova.process_intent: confirmed → %s", handled)
            return
        result = await router.route(phrase, area_id, user_id=user_id)
        _LOGGER.info("nova.process_intent: %r → %s", phrase, result)

    _register(SERVICE_PROCESS_INTENT, _process_intent, PROCESS_INTENT_SCHEMA)
