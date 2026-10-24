"""The nova.* Home Assistant services.

async_setup() in __init__.py calls async_setup_services() once per Home
Assistant run, so all 29 services exist from the first setup until Home
Assistant stops. Setting up, unloading, reloading or failing a config entry
never adds or removes one.

Because a service can outlive its entry, no handler holds on to an entry,
runtime, provider client, sentinel or configuration. Each call looks up the
loaded entry and its NovaRuntime afresh, and reads what it needs from them.
When there is no loaded entry the call fails with a translated
ServiceValidationError:

    no Nova entry                       no_entry
    entry configured but not loaded     not_loaded
    entry setting up or unloading       reloading
    setup failed, retrying or stuck     setup_failed
    more than one Nova entry            multiple_entries

A LOADED entry without a runtime is a lifecycle fault, not a user error, and
raises NovaRuntimeUnavailable from get_runtime.

Nothing in this module reads or writes hass.data.
"""
from __future__ import annotations

import json
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import repair_notices
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
from .reminders import async_add_reminder_service
from .repair_notices import RETIRED_SERVICES
from .runtime import (
    NovaConfigEntry,
    NovaRuntime,
    get_runtime,
    lifecycle_runtime_config,
    set_observer_running,
)
from .summary import async_summarise

_LOGGER = logging.getLogger(__name__)

# translations/en.json "exceptions" key for each entry state other than
# LOADED. Any state Home Assistant adds later is reported as not_loaded.
_STATE_ERRORS: dict[ConfigEntryState, str] = {
    ConfigEntryState.NOT_LOADED: "not_loaded",
    ConfigEntryState.SETUP_IN_PROGRESS: "reloading",
    ConfigEntryState.UNLOAD_IN_PROGRESS: "reloading",
    ConfigEntryState.SETUP_ERROR: "setup_failed",
    ConfigEntryState.SETUP_RETRY: "setup_failed",
    ConfigEntryState.MIGRATION_ERROR: "setup_failed",
    ConfigEntryState.FAILED_UNLOAD: "setup_failed",
}


# ── Finding the loaded entry ────────────────────────────────────────────────

def _service_error(translation_key: str) -> ServiceValidationError:
    """An error for the caller, worded only by the translation, so no
    setting, credential or exception text can leak into it."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=translation_key)


def _nova_entries(hass: HomeAssistant) -> list[NovaConfigEntry]:
    """Nova's config entries, leaving out ignored discoveries."""
    return hass.config_entries.async_entries(DOMAIN, include_ignore=False)


@callback
def async_get_loaded_entry(hass: HomeAssistant) -> NovaConfigEntry:
    """The single Nova entry, provided it is LOADED.

    Otherwise raises the translated ServiceValidationError for the case:
    no entry, several entries, or the entry's current state."""
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
    """The loaded entry and its runtime, looked up for this call only."""
    entry = async_get_loaded_entry(hass)
    return entry, get_runtime(entry)


# ── Voice and speaker settings, read per call ───────────────────────────────

def _live_honorific(hass: HomeAssistant) -> str:
    """How to address whoever is home right now (honorific.py): a lone
    occupant's own honorific or the household default, nothing when several
    people or nobody are home. DEFAULT_HONORIFIC only if the lookup fails."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return DEFAULT_HONORIFIC


def _speaker_list(raw) -> list | None:
    """A non empty list from a list or its JSON text; None otherwise."""
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    return value if isinstance(value, list) and value else None


def _get_tts(hass: HomeAssistant, entry: NovaConfigEntry, context: str = "chat") -> str | None:
    """The TTS entity for this kind of announcement. Contexts listed as
    premium (briefing, doorbell, camera and recognition unless changed) use
    the premium engine when one is set; the rest use the regular engine."""
    from .tts_helper import resolve_tts_for_context
    # Saved panel settings over the entry: a panel configured install keeps
    # nothing in entry.options, and the briefing used to find no engine.
    try:
        from . import nova_config
        cfg = nova_config.effective_config(entry)
    except Exception:
        cfg = {**dict(entry.data), **dict(entry.options)}
    return resolve_tts_for_context(
        hass, context,
        cfg.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE),
        cfg.get(CONF_TTS_PREMIUM_ENGINE, DEFAULT_TTS_PREMIUM_ENGINE),
        cfg.get(CONF_TTS_PREMIUM_CONTEXTS, DEFAULT_TTS_PREMIUM_CONTEXTS),
    )


def _get_speakers(hass: HomeAssistant, entry: NovaConfigEntry) -> list[str]:
    """Where proactive announcements play.

    In order: announcement_speakers from the live panel settings, then from
    the saved config, then whatever the broadcast group resolves to. The
    runtime read stays outside the try, so a loaded entry without a runtime
    raises instead of quietly using the fallbacks."""
    rc = lifecycle_runtime_config(entry)
    try:
        live = _speaker_list(rc.get("announcement_speakers"))
    except Exception as exc:
        _LOGGER.debug("Error reading announcement_speakers: %s", exc)
        live = None
    if live:
        _LOGGER.debug("Announcement speakers from panel config: %s", live)
        return live
    try:
        from . import nova_config
        cfg = nova_config.effective_config(entry)
    except Exception:
        cfg = {**dict(entry.data), **dict(entry.options)}
    saved = _speaker_list(cfg.get("announcement_speakers"))
    if saved:
        return saved
    resolved = broadcast_target(hass, broadcast_group=(cfg.get(CONF_BROADCAST_GROUP) or None))
    _LOGGER.debug("Announcement speakers from broadcast_group: %s", resolved)
    return resolved


def _voice(hass: HomeAssistant, entry: NovaConfigEntry, context: str):
    """(honorific, tts entity, speakers) for a handler that speaks."""
    return (_live_honorific(hass), _get_tts(hass, entry, context=context),
            _get_speakers(hass, entry))


def _caller_id(call: ServiceCall) -> str | None:
    """The Home Assistant user behind the call, if known."""
    return getattr(getattr(call, "context", None), "user_id", None)


def _setting(hass: HomeAssistant, entry: NovaConfigEntry, key: str, default=None):
    """One setting as Nova itself sees it: live panel value, then saved
    panel config, then entry options and data (nova_config.runtime_get)."""
    from . import nova_config
    return nova_config.runtime_get(hass, entry, key, default)


def _doorbell_automations(hass: HomeAssistant) -> list:
    """Automation entities made from Nova's doorbell blueprint, whatever
    folder it was imported into."""
    from .automation.inventory import _runtime_entities
    found = []
    for entity in _runtime_entities(hass):
        try:
            path = str(getattr(entity, "referenced_blueprint", None) or "")
        except Exception:
            continue
        if path.replace("\\", "/").rsplit("/", 1)[-1] == "nova_doorbell_analyze.yaml":
            found.append(entity)
    return found


async def _log_action(hass: HomeAssistant, kind: str, call: ServiceCall, job):
    """Record a backup or restore in the action log, run `job` in the
    executor and mark the row accepted, or failed before re-raising."""
    from . import action_log
    request_id = action_log.new_request_id()
    user_id = _caller_id(call)
    action_id = await hass.async_add_executor_job(
        lambda: action_log.start(request_id, kind, "ha_service",
                                 requested_by_user_id=user_id))
    try:
        result = await hass.async_add_executor_job(job)
    except Exception:
        await hass.async_add_executor_job(
            lambda: action_log.set_execution(action_id, "failed",
                                             reason_code=f"{kind}_failed"))
        raise
    await hass.async_add_executor_job(
        lambda: action_log.set_execution(action_id, "accepted"))
    return result


# ── Registration ────────────────────────────────────────────────────────────

@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the 29 services. Safe to call again: a name that is already
    registered keeps its handler. Handlers look up the entry, runtime,
    client and settings on each call; nothing is captured here."""

    def _register(name: str, handler, schema: vol.Schema | None = None) -> None:
        if not hass.services.has_service(DOMAIN, name):
            if name in RETIRED_SERVICES:
                handler = _retiring(name, handler)
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

    def _retiring(name: str, handler):
        """Wrap a service that is being phased out: it still runs, and its
        first call each run warns and raises a notice naming the
        replacement."""
        async def _with_notice(call: ServiceCall) -> None:
            repair_notices.note_retired_service(hass, name, RETIRED_SERVICES[name])
            await handler(call)
        return _with_notice

    # ── Cameras and doorbell ────────────────────────────────────────────────

    async def _camera(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "camera")
        await async_analyze_camera(hass, call, runtime.client, honorific, tts, speakers)

    async def _analyze_on_event(call: ServiceCall) -> None:
        """For doorbell and motion automations: look now and report."""
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "doorbell")
        await async_auto_analyze_on_event(
            hass, runtime.client, honorific, tts, speakers,
            call.data["entity_id"], call.data.get("reason", "Activity detected"))

    async def _train_backlog(call: ServiceCall) -> None:
        """Run the doorbell's recorded events through image analysis into
        the training log, best effort, and log how many were analysed."""
        entry, runtime = async_resolve_loaded(hass)
        client = runtime.client
        honorific = _live_honorific(hass)
        limit = int(call.data.get("limit", 40) or 40)
        from . import doorbell_training
        from .camera import _FakeCall, active_camera_states, active_cameras

        # A camera to name the events after: one that looks like the
        # doorbell, else the first active camera.
        camera_id = next(
            (s.entity_id for s in active_camera_states(hass)
             if "doorbell" in s.entity_id or "front_door" in s.entity_id), None)
        if camera_id is None:
            cameras = active_cameras(hass)
            camera_id = cameras[0] if cameras else "camera.front_doorbell"

        async def _analyze_image(image_bytes, label):
            # The instruction leaves the honorific out: with it the model
            # tends to narrate in the third person ("Sir has a visitor").
            # Old recordings stay out of scene memory, which would file
            # them as seen now.
            prompt = (
                f"Recorded doorbell event ({label}). Identify who is at the door — "
                f"appearance, clothing, packages, vehicles. "
                f"Focus on what the resident would want to know."
            )
            fake = _FakeCall({"entity_id": camera_id, "prompt": prompt,
                              "announce": False, "record_scene": False})
            return await async_analyze_camera(
                hass, fake, client, honorific, None, [],
                gate_announce=True, force_images=[image_bytes])

        report = await doorbell_training.scan_backlog(hass, _analyze_image, honorific, limit=limit)
        _LOGGER.info("Nova doorbell backlog scan: %s", report)
        try:
            from .websocket import nova_log
            if report.get("ok"):
                line = (f"Backlog training: analysed {report['analyzed']} doorbell "
                        f"event(s) into the dataset (of {report['found']} found)")
            else:
                line = f"Backlog training: {report.get('reason', 'no events analysed')}"
            nova_log("CAMERA", line)
        except Exception:
            pass

    async def _check_packages(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "package")
        from . import package_monitor
        report = await package_monitor.periodic_check(
            hass, runtime.client, honorific, tts, speakers,
            configured_camera=call.data.get("entity_id"))
        _LOGGER.info("Nova manual package check: %s", report)

    async def _diagnose_doorbell(call: ServiceCall) -> None:
        """Check the doorbell pipeline step by step and fire
        nova_doorbell_diag with each check and a verdict."""
        entry, runtime = async_resolve_loaded(hass)
        checks: list[dict] = []
        diag = {"checks": checks, "verdict": "unknown"}

        made = _doorbell_automations(hass)
        if not made:
            checks.append({"check": "automation exists", "ok": False,
                           "detail": "no automation made from Nova's doorbell blueprint"})
            diag["verdict"] = ("Automation missing — create one from Nova's "
                               "doorbell blueprint")
            hass.bus.async_fire("nova_doorbell_diag", diag)
            return
        enabled = [e for e in made if getattr(e, "state", None) == "on"]
        shown = (enabled or made)[0]
        checks.append({"check": "automation exists", "ok": True,
                       "state": getattr(shown, "state", None),
                       "entity_id": getattr(shown, "entity_id", None)})
        if not enabled:
            checks.append({"check": "automation enabled", "ok": False,
                           "state": getattr(shown, "state", None)})
            diag["verdict"] = "Automation exists but is disabled"
            hass.bus.async_fire("nova_doorbell_diag", diag)
            return
        checks.append({"check": "automation enabled", "ok": True})

        from .camera import active_cameras
        cameras = active_cameras(hass)
        checks.append({"check": "cameras found", "ok": bool(cameras),
                       "cameras": cameras[:10]})
        # Being registered isn't enough (services outlive the entry); the
        # entry has to be loaded too.
        checks.append({"check": "analyze_on_event service",
                       "ok": (hass.services.has_service(DOMAIN, "analyze_on_event")
                              and entry.state is ConfigEntryState.LOADED)})
        voices = [s.entity_id for s in hass.states.async_all("tts")]
        checks.append({"check": "TTS entities", "ok": bool(voices), "entities": voices})

        failed = [c["check"] for c in checks if not c["ok"]]
        diag["verdict"] = (
            f"Failed checks: {', '.join(failed)}" if failed else
            "All checks passed — trigger the doorbell and watch logs for nova.analyze_on_event")
        _LOGGER.info("Doorbell diagnostic: %s", diag)
        hass.bus.async_fire("nova_doorbell_diag", diag)

    _register(
        "analyze_camera", _camera,
        vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("prompt"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
            # A short clip (services.yaml). No defaults on purpose, so the
            # handler's own one frame, 1.2 s apart, applies when omitted.
            vol.Optional("frames"): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)),
            vol.Optional("interval"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=5)),
        }),
    )
    _register(
        "analyze_on_event", _analyze_on_event,
        vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("reason", default="Activity detected"): cv.string,
        }),
    )
    _register(
        "train_doorbell_backlog", _train_backlog,
        vol.Schema({
            vol.Optional("limit", default=40): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
        }),
    )
    _register(
        "check_packages", _check_packages,
        vol.Schema({vol.Optional("entity_id"): cv.entity_id}),
    )
    _register("diagnose_doorbell", _diagnose_doorbell)

    # ── Spoken features ─────────────────────────────────────────────────────

    async def _briefing(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "briefing")
        result = await async_briefing(hass, call, runtime.client, honorific, tts, speakers)
        from . import nova_state
        await nova_state.async_record_briefing(hass, (result or {}).get("briefing", ""))

    async def _add_reminder(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "reminder")
        await async_add_reminder_service(hass, call, honorific, tts, speakers)

    async def _summary(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        honorific, tts, speakers = _voice(hass, entry, "summary")
        await async_summarise(hass, call, runtime.client, honorific, tts, speakers)

    _register(
        "briefing", _briefing,
        vol.Schema({
            vol.Optional("announce", default=True): cv.boolean,
            vol.Optional("include_weather", default=True): cv.boolean,
            vol.Optional("include_calendar", default=True): cv.boolean,
            vol.Optional("include_presence", default=True):
                cv.boolean,
            vol.Optional("include_events", default=True): cv.boolean,
            vol.Optional("include_energy", default=True):
                cv.boolean,
            vol.Optional("hours", default=12): vol.All(int, vol.Range(min=1, max=48)),
        }),
    )
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
    _register(
        "conversation_summary", _summary,
        vol.Schema({
            vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=168)),
            vol.Optional("device_id"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
            vol.Optional("store", default=True): cv.boolean,
        }),
    )

    # ── Backup, restore and the database ────────────────────────────────────

    async def _nova_backup(call):
        entry, runtime = async_resolve_loaded(hass)
        from .backup import create_backup
        path = await _log_action(hass, "backup", call,
                                 lambda: create_backup(hass.config.path()))
        _LOGGER.info("Nova state backed up to %s", path)
        text = (f"Nova state saved to:\n`{path}`\n\nDownload this file before "
                "re-flashing so memory, patterns and knowledge survive a wipe.")
        await hass.services.async_call(
            "persistent_notification", "create",
            {"notification_id": "nova_backup", "title": "Nova backup", "message": text},
            blocking=False)

    async def _nova_restore(call):
        entry, runtime = async_resolve_loaded(hass)
        from .backup import restore_backup
        archive = (call.data or {}).get("archive", "") or ""
        path = await _log_action(hass, "restore", call,
                                 lambda: restore_backup(hass.config.path(), archive))
        _LOGGER.info("Nova state restored from %s", path)
        text = (f"Nova state restored from:\n`{path}`\n\nRestart Home Assistant "
                "to load the restored memory and patterns.")
        await hass.services.async_call(
            "persistent_notification", "create",
            {"notification_id": "nova_restore", "title": "Nova restore", "message": text},
            blocking=False)

    async def _db_purge(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        days = call.data.get("days", 30)
        removed = await hass.async_add_executor_job(purge_old_records, days)
        _LOGGER.info("Nova DB purge: %d records deleted (>%d days)", removed, days)

    async def _db_stats(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        hass.bus.async_fire("nova_db_stats", await hass.async_add_executor_job(get_stats))

    _register("backup", _nova_backup)
    _register("restore", _nova_restore, vol.Schema({vol.Optional("archive"): str}))
    _register(
        "database_purge", _db_purge,
        vol.Schema({vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=365))}),
    )
    _register("database_stats", _db_stats)

    # ── Sentinel, observer and quiet ────────────────────────────────────────

    async def _sentinel_start(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        await runtime.sentinel.async_start()

    async def _sentinel_stop(call: ServiceCall) -> None:
        entry, runtime = async_resolve_loaded(hass)
        await runtime.sentinel.async_stop()

    async def _nap(call: ServiceCall) -> None:
        """Hold back everything but critical announcements for a while
        (30 minutes unless given)."""
        entry, runtime = async_resolve_loaded(hass)
        from . import sleep_detection
        minutes = call.data.get("duration_minutes", 30)
        sleep_detection.set_nap(minutes)
        hass.bus.async_fire("nova_observer_nap", {"duration_minutes": minutes})

    async def _shush(call: ServiceCall) -> None:
        """Mute announcements for an entity, a category, or (all=true)
        everything that isn't a critical safety alert, until unshush or for
        the given minutes. Mutes and their end times are saved and survive a
        restart."""
        entry, runtime = async_resolve_loaded(hass)
        from . import output_gate
        result = output_gate.shush(entity_id=call.data.get("entity_id"),
                                   category=call.data.get("category"),
                                   all=bool(call.data.get("all", False)),
                                   minutes=call.data.get("minutes"))
        await output_gate.async_save_mutes(hass)
        output_gate.reschedule_expiry()
        hass.bus.async_fire("nova_observer_shushed", result)
        _LOGGER.info("Nova shushed: %s", result)

    async def _unshush(call: ServiceCall) -> None:
        """Lift a mute; with no arguments, lift every mute."""
        entry, runtime = async_resolve_loaded(hass)
        from . import output_gate
        result = output_gate.unshush(entity_id=call.data.get("entity_id"),
                                     category=call.data.get("category"))
        await output_gate.async_save_mutes(hass)
        output_gate.reschedule_expiry()
        hass.bus.async_fire("nova_observer_unshushed", result)

    async def _observer_start(call: ServiceCall) -> None:
        """Start the observer by hand, even when settings have it off."""
        # The entry check comes first, so an unowned observer never starts.
        entry, runtime = async_resolve_loaded(hass)
        from . import nova_config, observer as observer_mod
        # Current settings with the panel's on top, not the entry's old data.
        settings = await hass.async_add_executor_job(nova_config.effective_config, entry)
        await observer_mod.start(hass, settings, entry=entry)
        set_observer_running(entry, True)
        _LOGGER.info("Observer started via service call")

    async def _observer_stop(call: ServiceCall) -> None:
        # The entry check comes first, so an unowned observer is never stopped.
        entry, runtime = async_resolve_loaded(hass)
        from . import observer as observer_mod
        await observer_mod.stop()
        set_observer_running(entry, False)
        _LOGGER.info("Observer stopped via service call")

    async def _observer_status(call: ServiceCall) -> None:
        """Fire nova_observer_status: mutes, recent activity, whether the
        observer runs and whether the household counts as asleep, judged
        with the same settings the observer uses."""
        entry, runtime = async_resolve_loaded(hass)
        from . import observer, output_gate, sleep_detection
        status = output_gate.status()
        status["running"] = observer.is_running()
        bedrooms = _setting(hass, entry, CONF_BEDROOM_AREAS, []) or []
        asleep, why = sleep_detection.is_sleeping(
            hass, bedroom_area_ids=bedrooms,
            quiet_start=_setting(hass, entry, "observer_quiet_start", "22:00"),
            quiet_end=_setting(hass, entry, "observer_quiet_end", "07:00"),
        )
        status.update(sleeping=asleep, sleep_reason=why, bedroom_areas=list(bedrooms))
        hass.bus.async_fire("nova_observer_status", status)
        _LOGGER.info("Observer status: %s", status)

    _register("sentinel_start", _sentinel_start)
    _register("sentinel_stop", _sentinel_stop)
    _register(
        "nap", _nap,
        vol.Schema({
            vol.Optional("duration_minutes", default=30):
                vol.All(int, vol.Range(min=1, max=480)),
        }),
    )
    _register(
        "shush", _shush,
        vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
            vol.Optional("all"): cv.boolean,
            vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        }),
    )
    _register(
        "unshush", _unshush,
        vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
        }),
    )
    _register("observer_start", _observer_start)
    _register("observer_stop", _observer_stop)
    _register("observer_status", _observer_status)

    # ── Lockdown and knowledge ──────────────────────────────────────────────

    async def _lockdown(call: ServiceCall) -> None:
        """Engage or lift the formal lockdown (the alarm armed posture)."""
        # The entry check runs first, so after an unload or mid reload this
        # fails before cognitive_core could build a lockdown manager.
        entry, runtime = async_resolve_loaded(hass)
        from . import cognitive_core
        # The schema has turned state into True, False, or a lockdown word.
        state = call.data["state"]
        on = state is True or state in ("engage", "lock")
        handled = await cognitive_core.request_lockdown(
            on, reason=call.data.get("reason", "requested via service"), hass=hass)
        if handled:
            _LOGGER.info("Lockdown %s via service call", "engaged" if on else "lifted")
        else:
            _LOGGER.warning("Lockdown service: request could not be handled (no hass)")

    async def _remember(call: ServiceCall) -> None:
        """Store a lasting fact or preference in the knowledge store."""
        entry, runtime = async_resolve_loaded(hass)
        from . import knowledge
        key = str(call.data.get("key", "")).strip()
        value = str(call.data.get("value", "")).strip()
        if not (key and value):
            _LOGGER.warning("nova.remember: 'key' and 'value' are required")
            return
        subject = (str(call.data.get("subject", knowledge.DEFAULT_SUBJECT)).strip()
                   or knowledge.DEFAULT_SUBJECT)
        kind = str(call.data.get("kind", "fact"))
        ttl = call.data.get("ttl_seconds")
        try:
            ttl = None if ttl in (None, "") else float(ttl)
        except (TypeError, ValueError):
            ttl = None
        stored = await hass.async_add_executor_job(
            lambda: knowledge.remember(key, value, subject=subject, kind=kind,
                                       source="stated", ttl_seconds=ttl))
        if stored:
            _LOGGER.info("nova.remember: stored %s/%s", subject, key)
        else:
            _LOGGER.warning("nova.remember: store failed for %s/%s", subject, key)

    async def _forget(call: ServiceCall) -> None:
        """Remove a stored fact by its id, or by key (and subject)."""
        entry, runtime = async_resolve_loaded(hass)
        from . import knowledge
        fact_id = call.data.get("id")
        try:
            fact_id = None if fact_id in (None, "") else int(fact_id)
        except (TypeError, ValueError):
            fact_id = None
        key, subject = call.data.get("key"), call.data.get("subject")
        removed = await hass.async_add_executor_job(
            lambda: knowledge.forget(fact_id=fact_id, subject=subject, key=key))
        _LOGGER.info("nova.forget: removed %d fact(s)", removed)

    async def _create_automation(call: ServiceCall) -> None:
        """Create a Home Assistant automation from the call data."""
        entry, runtime = async_resolve_loaded(hass)
        from .automation_creator import create_automation
        data = call.data
        result = await create_automation(
            hass, alias=data.get("alias", "Unnamed"),
            description=data.get("description", ""),
            trigger=data.get("trigger"), condition=data.get("condition"),
            action=data.get("action"), mode=data.get("mode", "single"),
            source="ha_service", requested_by_user_id=_caller_id(call))
        if result.get("success"):
            hass.bus.async_fire("nova_automation_created", result)
        else:
            _LOGGER.warning("Automation creation failed: %s", result.get("error"))

    # Lockdown: "on" or "off" as documented, plus the older words. Anything
    # else is refused by the schema instead of lifting the lockdown.
    _register(
        "lockdown", _lockdown,
        vol.Schema({
            vol.Required("state"): vol.Any(cv.boolean, vol.In(["engage", "lock", "lift", "unlock"])),
            vol.Optional("reason"): cv.string,
        }),
    )
    _register(
        "remember", _remember,
        vol.Schema({
            vol.Required("key"): vol.All(cv.string, vol.Strip, vol.Length(min=1)),
            vol.Required("value"): vol.All(cv.string, vol.Strip, vol.Length(min=1)),
            vol.Optional("subject"): cv.string,
            vol.Optional("kind"): vol.In(["fact", "preference", "event", "profile"]),
            vol.Optional("ttl_seconds"): vol.All(vol.Coerce(float), vol.Range(min=0, max=31536000)),
        }),
    )
    _register(
        "forget", _forget,
        vol.Schema({
            vol.Optional("id"): vol.All(vol.Coerce(int), vol.Range(min=0)),
            vol.Optional("key"): cv.string,
            vol.Optional("subject"): cv.string,
        }),
    )
    _register("create_automation", _create_automation)

    # ── Automation suggestions (admin only) ─────────────────────────────────

    async def _suggestion(call: ServiceCall, action: str) -> None:
        from . import nova_state
        from .ws_automation import async_suggestion_action
        await nova_state.async_check_admin(hass, call)
        user_id = _caller_id(call)
        user = await hass.auth.async_get_user(user_id) if user_id else None
        await async_suggestion_action(
            hass, int(call.data["suggestion_id"]), action,
            requested_by_user_id=user_id,
            requested_by_name=getattr(user, "name", None))

    async def _suggestion_accept(call: ServiceCall) -> None:
        """Accept a suggestion: installs its automation, as the panel does."""
        entry, runtime = async_resolve_loaded(hass)
        await _suggestion(call, "approve")

    async def _suggestion_dismiss(call: ServiceCall) -> None:
        """Dismiss a suggestion, as the panel does."""
        entry, runtime = async_resolve_loaded(hass)
        await _suggestion(call, "dismiss")

    _register("suggestion_accept", _suggestion_accept,
              vol.Schema({vol.Required("suggestion_id"): vol.Coerce(int)}))
    _register("suggestion_dismiss", _suggestion_dismiss,
              vol.Schema({vol.Required("suggestion_id"): vol.Coerce(int)}))

    # ── Tests and diagnostics ───────────────────────────────────────────────

    async def _test_notify(call: ServiceCall) -> None:
        """Send a test message to every configured normal notify target."""
        entry, runtime = async_resolve_loaded(hass)
        rc = lifecycle_runtime_config(entry)
        notify_config = dict(entry.data)
        notify_config.update(entry.options)
        notify_config.update(rc)
        from .notify_targets import async_send_configured_notifications
        delivered = await async_send_configured_notifications(
            hass, notify_config,
            {"title": "Nova",
             "message": "This is a test notification from Nova. If you see this, phone notifications are working."},
            action="test_notify", source="ha_service",
            requested_by_user_id=_caller_id(call),
        )
        if not delivered:
            _LOGGER.warning("Test notify: no configured service accepted the alert")

    async def _test_tts(call: ServiceCall) -> None:
        """Play a short test line in Nova's voice on the announcement speakers."""
        entry, runtime = async_resolve_loaded(hass)
        from . import nova_config
        from .tts_helper import async_announce, resolve_tts_entity
        cfg = nova_config.effective_config(entry)
        voice = resolve_tts_entity(hass, cfg.get("tts_engine", "auto"))
        speakers = broadcast_target(
            hass, broadcast_group=(cfg.get("broadcast_group") or None),
            announcement_speakers=cfg.get("announcement_speakers"))
        if not (voice and speakers):
            _LOGGER.warning(
                "Test TTS: no announcement speakers configured — choose speakers "
                "in Settings → Announcement Speakers (tts=%s, speakers=%s)",
                voice, speakers)
            return
        await async_announce(
            hass,
            "Nova test tone. If you hear this in a British accent, the Nova voice is working.",
            voice, speakers, context="test")
        _LOGGER.info("Test TTS sent via %s → %s", voice, speakers)

    async def _test_routing(call: ServiceCall) -> None:
        """Write how announcements and replies would be routed right now to
        the Home Assistant log, using the live settings."""
        entry, runtime = async_resolve_loaded(hass)
        from .audio_routing import (
            all_areas_with_satellite, anyone_home, currently_occupied_areas,
            observer_speak_target, reply_target, room_speaker, satellites_in_area,
        )
        from .tts_helper import find_best_tts_entity, resolve_tts_entity

        rc = lifecycle_runtime_config(entry)

        def _panel_value(key: str, kind: type):
            raw = rc.get(key)
            if not raw:
                return None
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                return None
            return value if isinstance(value, kind) else None

        speakers = _panel_value("announcement_speakers", list)
        pairings = _panel_value("satellite_pairings", dict)
        group = _setting(hass, entry, CONF_BROADCAST_GROUP, "") or ""
        voice = resolve_tts_entity(hass, _setting(hass, entry, CONF_TTS_ENGINE, "auto"))
        satellite_areas = all_areas_with_satellite(hass)

        log = _LOGGER.warning
        log("=== Nova ROUTING DIAGNOSTIC ===")
        log("TTS entity: %s", voice)
        log("TTS auto-pick: %s", find_best_tts_entity(hass))
        log("Broadcast group (config): '%s'", group)
        log("Broadcast target resolved: %s", broadcast_target(hass, broadcast_group=group or None))
        log("Announcement speakers (panel): %s", speakers)
        log("Satellite pairings (panel): %s", pairings)
        log("Anyone home: %s", anyone_home(hass))
        log("Occupied areas: %s", currently_occupied_areas(hass))
        log("Areas with satellites: %s", satellite_areas)
        for area_id in satellite_areas:
            in_area = satellites_in_area(hass, area_id)
            log("  Area '%s': sats=%s, assigned room speaker=%s",
                area_id, in_area, room_speaker(hass, area_id))
            for satellite in in_area:
                log("    reply_target(%s) → %s", satellite, reply_target(
                    hass, satellite_entity_id=satellite, satellite_pairings=pairings))
        for urgency in ("low", "medium", "high", "critical"):
            targets, mode = observer_speak_target(
                hass, urgency=urgency, broadcast_group=group or None,
                announcement_speakers=speakers, is_sleeping=False)
            log("  observer(%s): targets=%s, mode=%s", urgency, targets, mode)
        log("=== END ROUTING DIAGNOSTIC ===")

    _register("test_notify", _test_notify)
    _register("test_tts", _test_tts)
    _register("test_routing", _test_routing)
