"""Nova AI Assistant — Home Assistant integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change

from .const import (
    CONF_BEDROOM_AREAS,
    CONF_BROADCAST_GROUP,
    CONF_HONORIFIC,
    CONF_TTS_ENGINE,
    CONF_TTS_PREMIUM_ENGINE,
    CONF_TTS_PREMIUM_CONTEXTS,
    DEFAULT_HONORIFIC,
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_PREMIUM_ENGINE,
    DEFAULT_TTS_PREMIUM_CONTEXTS,
    DOMAIN,
)
from .audio_routing import broadcast_target
from .camera import (
    async_analyze_camera,
    async_auto_analyze_on_event,
    register_event_listeners,
)
from .briefing import async_briefing
from .scenes import async_activate_by_intent
from .routines import async_run_routine, list_routines
from .reminders import async_add_reminder_service, ReminderWatcher
from .recognition import register_recognition_listener
from .summary import async_summarise
from .sentinel import NovaSentinel
from .database import purge_old_records, get_stats
from .llm_provider import (
    create_provider,
    resolve_provider_credential,
    resolve_provider_endpoint,
)
from .migrations import migrate_config, CURRENT_SCHEMA_VERSION
from .runtime import (
    NovaConfigEntry,
    NovaRuntime,
    clear_runtime,
    NovaRuntimeUnavailable,
    get_runtime,
    lifecycle_runtime,
    lifecycle_runtime_config,
    runtime_config_snapshot,
    set_observer_running,
)
from .panel_register import async_register_panel, async_unregister_panel
from .websocket import async_register as async_register_ws
from .proactive_audio import (
    async_setup_proactive_audio,
    async_unload_proactive_audio,
)

_LOGGER = logging.getLogger(__name__)


def _prewarm_persisted_state() -> None:
    """Read persisted state files once, off the event loop (blocking-I/O
    hygiene). Best-effort — a failure here must never block setup."""
    try:
        from . import ha_secrets
        ha_secrets._read_secrets(force=True)      # refresh + cache secrets.yaml
    except Exception:
        pass
    try:
        from . import modes
        modes._load()
    except Exception:
        pass
    try:
        from . import intrusion
        intrusion._load_log()
    except Exception:
        pass
    try:
        from . import reasoning_cache
        reasoning_cache.load()
    except Exception:
        pass


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


PLATFORMS = ["conversation"]
# Config-entry only (v6.45.0): warns users who still have `nova:` in YAML.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# v6.45.0: the legacy add-on machinery is gone. Setup is config-entry only
# (HACS → Add Integration), the conversation agent registers via PLATFORMS,
# and /config/nova/config.json is the runtime store owned by the panel —
# nothing external writes it. The old nova_config.json import trigger,
# async_setup/async_setup_post_start hooks, and the ADDON_OWNED_KEYS
# reconcile block were all paths for an add-on that no longer exists.


async def async_setup_entry(hass: HomeAssistant, entry: NovaConfigEntry) -> bool:
    """Set up Nova from a config entry."""
    # ── Run config migrations if entry is from an older schema ──────────────
    current_version = entry.data.get("schema_version", 1)
    if current_version < CURRENT_SCHEMA_VERSION:
        new_data   = dict(entry.data)
        new_options = dict(entry.options)
        new_data, new_options, new_version = migrate_config(
            new_data, new_options, current_version
        )
        new_data["schema_version"] = new_version
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options
        )
        _LOGGER.info(
            "Nova: migrated config from schema v%d to v%d",
            current_version, new_version,
        )

    honorific = entry.options.get(CONF_HONORIFIC, entry.data.get(CONF_HONORIFIC, DEFAULT_HONORIFIC))

    # ── LLM provider — resolved from the single source of truth (nova_config
    # wins over stale entry data/options) so the boot client, conversation, and
    # agent never diverge on which model to run. ───────────────────────────
    from . import nova_config as _jc
    _jc.configure(hass)
    _eff = await hass.async_add_executor_job(_jc.effective_config, entry)
    # Warm the remaining persisted-state caches off the event loop too, so the
    # hot paths that read them (observer tick, panel data, intrusion log) don't
    # trip Home Assistant's blocking-I/O detector on first access.
    await hass.async_add_executor_job(_prewarm_persisted_state)

    # Spoken History (v7.104.0): point the store at this instance's own
    # config directory, then load the most recent entry into memory so a
    # voice "repeat that" works right after a restart without Nova needing
    # to speak something new first. Must fail open — a history-database
    # problem can never be allowed to block Nova from loading at all.
    try:
        from . import spoken_history
        spoken_history.configure(hass)
        await hass.async_add_executor_job(spoken_history.hydrate)
    except Exception as exc:
        _LOGGER.warning("Nova: spoken history hydrate failed (non-fatal): %s", exc)

    # Action Audit Log: same portability fix — point the store at this
    # instance's own config directory rather than the hardcoded default.
    # No hydrate step needed (nothing to warm into memory); must fail open
    # the same way, though configure() itself can't meaningfully raise.
    try:
        from . import action_log
        action_log.configure(hass)
    except Exception as exc:
        _LOGGER.warning("Nova: action log configure failed (non-fatal): %s", exc)
    llm_provider_name = _eff.get("llm_provider", "groq")
    # Phase 2 (v7.107.0): each provider's own dedicated credential, with a
    # narrow fallback to the legacy shared key only when llm_provider_name is
    # the installation's saved primary provider (see resolve_provider_credential).
    api_key = resolve_provider_credential(_eff, llm_provider_name)
    llm_model         = _eff.get("model", "openai/gpt-oss-120b")
    llm_base_url      = resolve_provider_endpoint(_eff, llm_provider_name)

    try:
        llm_client = await hass.async_add_executor_job(
            create_provider,
            llm_provider_name, api_key, llm_model, llm_base_url,
        )
        _LOGGER.info(
            "Nova: LLM provider '%s' initialised (model=%s)",
            llm_provider_name, llm_model,
        )
    except Exception as exc:
        _LOGGER.error("Nova: LLM provider init failed: %s", exc)
        return False

    sentinel = NovaSentinel(hass, llm_client, honorific, entry=entry)

    # Register camera event listeners (nest_event, frigate_event)
    camera_unsubs = register_event_listeners(hass)

    # Central scheduler for periodic sweeps + a resource registry for one-call,
    # fail-safe teardown on unload/reload (v7.43.0).
    from .scheduler import NovaScheduler
    from .resources import NovaResources
    from .automation_inventory import AutomationContextTracker
    sched = NovaScheduler(hass)
    resources = NovaResources()
    automation_contexts = AutomationContextTracker()
    resources.add_closeable(automation_contexts)

    # ── Auto-analyze camera events GOING FORWARD (doorbell / person) ─────────
    # The listeners above only CACHE Nest/Frigate events — historically nothing
    # was analyzed unless a user automation called nova.analyze_on_event. These
    # listeners make Nova inspect notable events itself: a doorbell PRESS always
    # gets a look; person/motion are throttled per-camera so a busy street/sidewalk
    # can't spam the vision model, and the spoken announcement is still notability-
    # gated (only deliveries, unfamiliar people, etc. are voiced). Toggle:
    # General → "Camera Watch" (camera_auto_analyze); motion behind a second flag.
    import time as _auto_time
    from .camera import _nest_device_to_camera as _nest2cam

    _auto_cd: dict[str, float] = {}      # person/motion throttle, per entity
    _chime_cd: dict[str, float] = {}     # doorbell-press anti-double, per entity

    def _auto_flag(key: str, default: bool) -> bool:
        # Live panel value from NovaRuntime. An event that arrives during
        # setup, before the runtime exists, gets {} and so the default. A
        # loaded entry with no runtime raises here instead of using defaults.
        _rc = lifecycle_runtime_config(entry)
        try:
            if key in _rc:
                _v = _rc[key]
                return _v if isinstance(_v, bool) else str(_v).lower() in ("1", "true", "yes", "on")
        except Exception:
            pass
        return default

    def _auto_fire(entity_id: str, reason: str, ctx: str, doorbell: bool = False) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context=ctx)
        spk = _get_speakers(hass, entry)
        hass.async_create_task(
            async_auto_analyze_on_event(
                hass, llm_client, honorific, tts, spk, entity_id, reason, doorbell=doorbell
            )
        )

    @callback
    def _auto_nest(event) -> None:
        if not _auto_flag("camera_auto_analyze", True):
            return
        try:
            data = event.data
            device_id = data.get("device_id") or data.get("nest_device_id")
            etype = str(data.get("type") or data.get("event_type") or "").lower()
            if not device_id:
                return
            # Doorbell PRESS → the full announced analysis. Person events feed
            # SILENT visitor learning (training data only, never spoken) when
            # enabled; motion/sound stay ignored.
            if "chime" not in etype and "doorbell" not in etype:
                if "person" in etype and _auto_flag("visitor_learning", True):
                    entity_id = _nest2cam(hass, device_id)
                    if not entity_id:
                        return
                    now = _auto_time.monotonic()
                    if now - _auto_cd.get(entity_id, float("-inf")) < 180.0:
                        return
                    _auto_cd[entity_id] = now
                    honorific = _live_honorific(hass)  # Phase C: presence-aware
                    from .camera import async_visitor_observation
                    hass.async_create_task(
                        async_visitor_observation(hass, llm_client, honorific, entity_id)
                    )
                return
            entity_id = _nest2cam(hass, device_id)
            if not entity_id:
                return
            now = _auto_time.monotonic()
            if now - _chime_cd.get(entity_id, float("-inf")) < 12.0:
                return  # collapse a rapid double-press
            _chime_cd[entity_id] = now
            _auto_fire(entity_id, "Someone is at the front door", "doorbell", doorbell=True)
        except Exception as exc:
            _LOGGER.debug("Nova auto-analyze (nest) error: %s", exc)

    @callback
    def _auto_frigate(event) -> None:
        # Frigate has no doorbell-press concept; its events are person/object
        # detections. With the doorbell-only default, leave Frigate dormant unless
        # the user opts into the noisier non-doorbell analysis.
        if not (_auto_flag("camera_auto_analyze", True)
                and _auto_flag("camera_auto_analyze_motion", False)):
            return
        try:
            data = event.data
            if data.get("type") != "new":
                return
            after = data.get("after") or data.get("before") or {}
            cam = after.get("camera")
            label = str(after.get("label") or "").lower()
            if not cam:
                return
            entity_id = f"camera.{str(cam).lower()}"
            if not hass.states.get(entity_id):
                return
            if label and label not in (
                "person", "car", "truck", "package", "dog", "cat", "bicycle", "motorcycle",
            ):
                return  # ignore irrelevant tracked objects
            now = _auto_time.monotonic()
            if now - _auto_cd.get(entity_id, float("-inf")) < 120.0:
                return
            _auto_cd[entity_id] = now
            _auto_fire(entity_id, f"{label.capitalize()} detected" if label else "Motion detected", "camera")
        except Exception as exc:
            _LOGGER.debug("Nova auto-analyze (frigate) error: %s", exc)

    try:
        camera_unsubs.append(hass.bus.async_listen("nest_event", _auto_nest))
        camera_unsubs.append(hass.bus.async_listen("frigate_event", _auto_frigate))
        _LOGGER.info("Nova: camera auto-analysis active (doorbell always, person/motion throttled)")
    except Exception as exc:
        _LOGGER.debug("Nova: auto-analyze listener registration failed: %s", exc)

    # ── Eufy Security — native sensors, no bus event to listen for ──────────
    # Nest/Frigate fire a custom bus event Nova can subscribe to once. Eufy
    # doesn't — every signal is a plain entity state change on one of the
    # sibling sensors eufy.py discovers per camera (unique_id-based, survives
    # renames). Ringing gets the same full analysis path as a Nest chime.
    # stranger_person_detected is the only "unfamiliar person" case worth a
    # vision call; plain person_detected (a known/regular face) just logs for
    # free — no LLM call, per the same cost reasoning that already governs
    # the once-per-camera cooldowns below. Package delivered/taken feed
    # package_monitor's existing state machine directly; stranded is its own
    # one-shot nag. This block re-resolves the camera->role map at setup only
    # — a camera added after startup needs a reload to be picked up, same as
    # every other camera-discovery path in this integration.
    try:
        from homeassistant.helpers.event import async_track_state_change_event
        from . import eufy as _eufy

        _eufy_roles = _eufy.all_camera_roles(hass)
        _eufy_reverse: dict[str, tuple[str, str]] = {}
        # "pet" added for Phase 4 (v7.109.0) semantic learning only — it has
        # no announcement/action branch below, same as before; this purely
        # lets an animal detection reach camera_semantic.record_event.
        _EUFY_WATCHED_ROLES = ("ringing", "stranger", "person", "vehicle", "pet",
                               "package_delivered", "package_stranded", "package_taken")
        for _cam, _roles in _eufy_roles.items():
            for _role in _EUFY_WATCHED_ROLES:
                _ent = _roles.get(_role)
                if _ent:
                    _eufy_reverse[_ent] = (_cam, _role)

        if _eufy_reverse:
            @callback
            def _auto_eufy(event) -> None:
                # camera_auto_analyze ("Camera Watch") gates ringing/stranger/
                # person the same way it already gates the Nest path above —
                # but NOT package roles: the pre-existing periodic vision-sweep
                # this replaces for Eufy cameras was always independently
                # gated on package_detection alone, never on Camera Watch, so
                # a house with Camera Watch off but Package Watch on (a real,
                # supported combination) must keep working exactly as before.
                new_state = event.data.get("new_state")
                if new_state is None or new_state.state != "on":
                    return
                hit = _eufy_reverse.get(event.data.get("entity_id"))
                if not hit:
                    return
                entity_id, role = hit
                now = _auto_time.monotonic()

                if role.startswith("package_"):
                    if not _auto_flag("package_detection", True):
                        return
                    honorific = _live_honorific(hass)
                    tts = _get_tts(hass, entry, context="package")
                    spk = _get_speakers(hass, entry)
                    from . import package_monitor
                    hass.async_create_task(
                        package_monitor.note_from_eufy(hass, honorific, tts, spk, entity_id, role)
                    )
                    return

                if not _auto_flag("camera_auto_analyze", True):
                    return

                if role == "ringing":
                    if now - _chime_cd.get(entity_id, float("-inf")) < 12.0:
                        return  # collapse a rapid double-press
                    _chime_cd[entity_id] = now
                    _auto_fire(entity_id, "Someone is ringing the doorbell", "doorbell", doorbell=True)
                    return

                if role == "stranger":
                    if not _auto_flag("visitor_learning", True):
                        return
                    if now - _auto_cd.get(entity_id, float("-inf")) < 180.0:
                        return
                    _auto_cd[entity_id] = now
                    honorific = _live_honorific(hass)
                    from .camera import async_visitor_observation
                    hass.async_create_task(
                        async_visitor_observation(hass, llm_client, honorific, entity_id)
                    )
                    # Semantic learning (Phase 4, v7.109.0): additive only —
                    # the vision observation above is the existing action
                    # path and is untouched. attribute=False: Eufy's own
                    # "stranger" verdict must stay unattributed even if
                    # Nova's separate recognition cache has a stale match
                    # for this camera.
                    from . import camera_semantic
                    hass.async_create_task(camera_semantic.record_event(
                        hass, label="person", camera_entity=entity_id,
                        source="eufy", attribute=False, detail="stranger",
                    ))
                    return

                if role == "person":
                    # A known/regular face — worth logging for the pattern
                    # engine, not worth an LLM vision call (stranger_person_
                    # detected already covers the case that IS worth one).
                    if not _auto_flag("visitor_learning", True):
                        return
                    if now - _auto_cd.get(f"{entity_id}:known", float("-inf")) < 180.0:
                        return
                    _auto_cd[f"{entity_id}:known"] = now
                    async def _log_known_visitor() -> None:
                        try:
                            from . import doorbell_training
                            from .camera import _camera_friendly_name
                            name = _camera_friendly_name(hass, entity_id)
                            await hass.async_add_executor_job(
                                doorbell_training.log_event, name, entity_id, "eufy",
                                {"summary": "", "analysis": "", "category": "known_resident",
                                 "notable": False},
                            )
                        except Exception as exc:
                            _LOGGER.debug("Nova eufy: known-visitor log failed: %s", exc)
                    hass.async_create_task(_log_known_visitor())
                    # Semantic learning (Phase 4, v7.109.0): additive to the
                    # existing doorbell_training log above, not a replacement
                    # for it — that log is visitor-training data; this is the
                    # pattern-sequence store. Resident attribution IS
                    # attempted here (default attribute=True): Eufy's own
                    # "person" role means a known/regular face.
                    from . import camera_semantic
                    hass.async_create_task(camera_semantic.record_event(
                        hass, label="person", camera_entity=entity_id,
                        source="eufy", detail="known_person",
                    ))
                    return

                if role == "vehicle":
                    # Direct announcement, no vision call — same cost
                    # reasoning as the package/known-visitor paths. Signal is
                    # the motionDetectionTypeVehicle switch, not the
                    # vehicleDetected binary_sensor (see eufy.py's role map
                    # comment for why: the binary_sensor ships disabled and
                    # unproven, the switch already had a working automation).
                    if now - _auto_cd.get(f"{entity_id}:vehicle", float("-inf")) < 300.0:
                        return
                    _auto_cd[f"{entity_id}:vehicle"] = now
                    async def _announce_vehicle() -> None:
                        try:
                            from .camera import _camera_friendly_name
                            from .tts_helper import async_announce
                            from . import persona
                            honorific = _live_honorific(hass)
                            tts = _get_tts(hass, entry, context="camera")
                            spk = _get_speakers(hass, entry)
                            name = _camera_friendly_name(hass, entity_id)
                            await async_announce(
                                hass,
                                persona.lead_in(honorific, f"a vehicle was detected at {name}."),
                                tts, spk, context="camera",
                            )
                        except Exception as exc:
                            _LOGGER.debug("Nova eufy: vehicle announce failed: %s", exc)
                    hass.async_create_task(_announce_vehicle())
                    # Semantic learning (Phase 4, v7.109.0): additive to the
                    # announcement above, not a replacement for it. Never
                    # attributed — a vehicle is never a resident.
                    from . import camera_semantic
                    hass.async_create_task(camera_semantic.record_event(
                        hass, label="vehicle", camera_entity=entity_id,
                        source="eufy", detail="motion_detection_type_vehicle",
                    ))
                    return

                if role == "pet":
                    # No announcement/action exists for this role — Phase 4
                    # (v7.109.0) adds ONLY semantic recording here, not a new
                    # spoken behaviour. Same cooldown pattern as vehicle.
                    if now - _auto_cd.get(f"{entity_id}:pet", float("-inf")) < 300.0:
                        return
                    _auto_cd[f"{entity_id}:pet"] = now
                    from . import camera_semantic
                    hass.async_create_task(camera_semantic.record_event(
                        hass, label="animal", camera_entity=entity_id,
                        source="eufy", detail="pet_detected",
                    ))
                    return

            camera_unsubs.append(async_track_state_change_event(
                hass, list(_eufy_reverse.keys()), _auto_eufy))
            _LOGGER.info(
                "Nova: Eufy native-sensor watch active (%d camera(s), %d sensor(s))",
                len(_eufy_roles), len(_eufy_reverse),
            )
    except Exception as exc:
        _LOGGER.debug("Nova: Eufy listener registration failed: %s", exc)

    # ── Package & mail detection — periodic porch check ─────────────────────
    # Deliveries often don't ring the bell (carrier drops and leaves), so a low-
    # frequency vision sweep of the doorbell/porch camera catches them. Per-camera
    # state means a package sitting all day is announced once, on arrival. Skipped
    # during quiet hours. Toggle: General → "Package Watch" (package_detection).
    PKG_INTERVAL = timedelta(minutes=15)

    async def _package_tick(_now) -> None:
        if not _auto_flag("package_detection", True):
            return
        try:
            from . import package_monitor
            honorific = _live_honorific(hass)  # Phase C: presence-aware
            tts = _get_tts(hass, entry, context="package")
            spk = _get_speakers(hass, entry)
            report = await package_monitor.periodic_check(
                hass, llm_client, honorific, tts, spk, configured_camera=None
            )
            _LOGGER.debug("Nova package check: %s", report)
        except Exception as exc:
            _LOGGER.debug("Nova package tick error: %s", exc)

    if sched.add("package", PKG_INTERVAL, _package_tick):
        _LOGGER.info("Nova: package/mail detection active (porch sweep every %s min)",
                     int(PKG_INTERVAL.total_seconds() // 60))

    # Hourly gentle service-health sweep (v6.70.3): re-runs the core-dependency
    # checks on its own so the panel stays current without the user opening it.
    # It's reachability-only and never alarms — a synthetic miss yields IDLE, and
    # only a real-usage failure (recorded by the actual call sites) shows DOWN.
    HEALTH_INTERVAL = timedelta(hours=1)

    async def _health_tick(_now) -> None:
        try:
            from .diagnostics import run_service_health
            res = await run_service_health(hass)
            _LOGGER.debug("Nova hourly health: %s", res.get("summary"))
        except Exception as exc:
            _LOGGER.debug("Nova health tick error: %s", exc)

    if sched.add("health", HEALTH_INTERVAL, _health_tick):
        _LOGGER.info("Nova: hourly service-health sweep active")

    # Host-health awareness (Phase 10, v7.112.0): samples System Monitor
    # entities and advances the persistence/cooldown state machine every
    # tick. Off by default (host_health.tick itself no-ops when the master
    # toggle is off, so this line always registers — same convention as
    # hazard_monitor's periodic_check). Sampling cadence intentionally
    # shorter than the persistence window (host_health.TICK_INTERVAL_SECONDS,
    # 2 minutes) so a 10-minute-default persistence window still gets
    # several samples.
    from .host_health import TICK_INTERVAL_SECONDS as _HH_INTERVAL_SECONDS
    HOST_HEALTH_INTERVAL = timedelta(seconds=_HH_INTERVAL_SECONDS)

    async def _host_health_tick(_now) -> None:
        try:
            from . import host_health, nova_config as _jc3
            # Snapshot the live dict each tick for the executor: panel
            # changes apply on the next sample.
            rc = runtime_config_snapshot(entry)
            cfg = await hass.async_add_executor_job(
                _jc3.effective_config_with_runtime, entry, rc)
            res = await host_health.tick(hass, cfg)
            _LOGGER.debug("Nova host-health tick: %s", res)
        except NovaRuntimeUnavailable as exc:
            _LOGGER.warning("Nova host-health tick skipped: %s", exc)
        except Exception as exc:
            _LOGGER.debug("Nova host-health tick error: %s", exc)

    if sched.add("host_health", HOST_HEALTH_INTERVAL, _host_health_tick):
        _LOGGER.info("Nova: host-health sampling active (every %s)",
                     HOST_HEALTH_INTERVAL)

    # Multi-hazard monitor (v6.71.0): polls USGS/NWS/EONET every 10 min for new
    # nearby significant events, scoped to home coordinates (or a panel override).
    # No-op unless the user enables it; each feed fails safe (never fabricates).
    HAZARD_INTERVAL = timedelta(minutes=10)

    async def _hazard_tick(_now) -> None:
        try:
            from . import hazard_monitor
            honorific = _live_honorific(hass)  # Phase C: presence-aware
            res = await hazard_monitor.periodic_check(hass, honorific)
            if res.get("fired"):
                _LOGGER.debug("Nova hazard sweep: %s", res)
        except Exception as exc:
            _LOGGER.debug("Nova hazard tick error: %s", exc)

    if sched.add("hazard", HAZARD_INTERVAL, _hazard_tick):
        _LOGGER.info("Nova: multi-hazard monitor active (sweep every %s min)",
                     int(HAZARD_INTERVAL.total_seconds() // 60))

    # Automatic document ingestion (v6.79.0): pick up files dropped into
    # /config/nova/documents (and any configured watch folders) without needing
    # the manual Scan button. Incremental — only new/changed files are ingested,
    # so this never re-embeds the whole library on a timer.
    DOCS_SCAN_INTERVAL = timedelta(minutes=10)

    async def _docs_tick(_now) -> None:
        try:
            from . import documents
            res = await documents.auto_ingest_new(hass)
            watch = await documents.scan_watch_folders(hass)
            n = (res.get("new_files", 0) or 0) + (watch.get("new_files", 0) or 0)
            if n:
                _LOGGER.info("Nova: auto-ingested %d new document(s)", n)
        except Exception as exc:
            _LOGGER.debug("Nova docs auto-ingest error: %s", exc)

    if sched.add("documents", DOCS_SCAN_INTERVAL, _docs_tick):
        _LOGGER.info("Nova: document auto-ingest active (scan every %s min)",
                     int(DOCS_SCAN_INTERVAL.total_seconds() // 60))

    # Nightly "Heading to bed?" sleep-state prompt (v7.86.0). Checked every 10
    # min; the function itself no-ops until it's actually time (past
    # sleep_prompt_time, TV off, not already asked/overridden tonight) — see
    # sleep_detection.maybe_prompt_sleep.
    SLEEP_PROMPT_INTERVAL = timedelta(minutes=10)

    async def _sleep_prompt_tick(_now) -> None:
        try:
            from . import sleep_detection as sd, nova_config as _jc2
            # Snapshot the live dict each tick for the executor: panel
            # changes apply on the next check.
            rc = runtime_config_snapshot(entry)
            cfg = await hass.async_add_executor_job(
                _jc2.effective_config_with_runtime, entry, rc)
            await sd.maybe_prompt_sleep(hass, cfg)
        except NovaRuntimeUnavailable as exc:
            _LOGGER.warning("Nova sleep-prompt tick skipped: %s", exc)
        except Exception as exc:
            _LOGGER.debug("Nova sleep-prompt tick error: %s", exc)

    if sched.add("sleep_prompt", SLEEP_PROMPT_INTERVAL, _sleep_prompt_tick):
        _LOGGER.info("Nova: nightly sleep-state prompt active (checked every %s min)",
                     int(SLEEP_PROMPT_INTERVAL.total_seconds() // 60))

    # Automatic database purge (Nova Unification item 3, 11 Sept 2026). Both
    # nova.database_purge and knowledge.purge_expired() already existed but
    # nothing ever called them automatically — cleanup was manual-only, so a
    # DB that's never manually pruned just grows forever. Daily sweep, 30-day
    # retention, matching the existing service's own default.
    DB_PURGE_INTERVAL = timedelta(hours=24)

    async def _db_purge_tick(_now) -> None:
        try:
            deleted = await hass.async_add_executor_job(purge_old_records, 30)
            from . import knowledge
            expired = await hass.async_add_executor_job(knowledge.purge_expired)
            if deleted or expired:
                _LOGGER.info("Nova daily purge: %d conversation record(s), %d expired fact(s)",
                             deleted, expired)
        except Exception as exc:
            _LOGGER.debug("Nova db purge tick error: %s", exc)

    if sched.add("db_purge", DB_PURGE_INTERVAL, _db_purge_tick):
        _LOGGER.info("Nova: automatic database purge active (daily, 30-day retention)")

    # ── Scheduled briefings (v6.78.0) ─────────────────────────────────────────
    # Nova delivers its own morning and evening briefing at configured clock
    # times. Off by default; enable per-briefing in Settings. Each run reuses the
    # same content engine as the nova.briefing service, so what you hear is
    # identical to calling it by hand.
    class _SchedCall:
        """Minimal ServiceCall stand-in for a scheduled (non-service) run."""
        def __init__(self, data: dict):
            self.data = data

    def _brief_cfg(key, default):
        try:
            from . import nova_config
            v = nova_config.get(key, default)
            return v if v is not None else default
        except Exception:
            return default

    def _parse_hhmm(value, fallback_h, fallback_m):
        try:
            h, m = str(value).split(":")[:2]
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except Exception:
            pass
        return fallback_h, fallback_m

    async def _run_briefing(kind: str) -> None:
        """Deliver a scheduled briefing if it's enabled and someone's home."""
        try:
            if not bool(_brief_cfg(f"briefing_{kind}_enabled", False)):
                return
            # Don't talk to an empty house unless explicitly allowed.
            if bool(_brief_cfg("briefing_require_home", True)):
                # Only skip when we are CONFIDENT the house is empty — every
                # tracked person is explicitly away. Unknown/unavailable presence
                # must not suppress the briefing (fail open); the old check
                # silenced scheduled briefings whenever presence was not a clean
                # "home".
                try:
                    from .presence import everyone_confidently_away
                    if everyone_confidently_away(hass):
                        _LOGGER.debug("Nova: skipping %s briefing — everyone away", kind)
                        return
                except Exception:
                    pass
            honorific = entry.options.get(CONF_HONORIFIC,
                                          entry.data.get(CONF_HONORIFIC, DEFAULT_HONORIFIC))
            tts = _get_tts(hass, entry, context="briefing")
            spk = _get_speakers(hass, entry)
            call = _SchedCall({
                "announce": True,
                "include_weather": bool(_brief_cfg("briefing_include_weather", True)),
                "include_calendar": bool(_brief_cfg("briefing_include_calendar", True)),
                "include_presence": bool(_brief_cfg("briefing_include_presence", True)),
                "include_events": bool(_brief_cfg("briefing_include_events", True)),
                "include_energy": bool(_brief_cfg("briefing_include_energy", True)),
                "include_hazards": bool(_brief_cfg("briefing_include_hazards", True)),
                # morning looks back overnight; evening looks back over the day
                "hours": 12 if kind == "morning" else 14,
            })
            await async_briefing(hass, call, llm_client, honorific, tts, spk)
            _LOGGER.info("Nova: delivered %s briefing", kind)
        except Exception as exc:
            # A scheduled briefing failing must be VISIBLE — this was
            # previously debug-level, which hid a NameError entirely.
            _LOGGER.warning("Nova %s briefing failed: %s", kind, exc)

    async def _morning_briefing(_now) -> None:
        await _run_briefing("morning")

    async def _evening_briefing(_now) -> None:
        await _run_briefing("evening")

    try:
        mh, mm = _parse_hhmm(_brief_cfg("briefing_morning_time", "07:30"), 7, 30)
        eh, em = _parse_hhmm(_brief_cfg("briefing_evening_time", "19:30"), 19, 30)
        camera_unsubs.append(async_track_time_change(
            hass, _morning_briefing, hour=mh, minute=mm, second=0))
        camera_unsubs.append(async_track_time_change(
            hass, _evening_briefing, hour=eh, minute=em, second=0))
        _LOGGER.info("Nova: briefings scheduled (morning %02d:%02d, evening %02d:%02d)",
                     mh, mm, eh, em)
    except Exception as exc:
        _LOGGER.debug("Nova: briefing scheduler registration failed: %s", exc)

    # Register DoubleTake MQTT face recognition listener
    recognition_unsubs = await register_recognition_listener(hass)

    # Automation probation (Phase 3) — observe confirmed runs of Nova-installed
    # automations via Home Assistant's own automation_triggered event (the
    # documented, supported mechanism; see automation_trials.py). Every
    # automation in the house fires this, not just Nova's, so the listener is
    # a single cheap subscription rather than one per installed automation.
    @callback
    def _on_automation_triggered(event) -> None:
        from . import automation_trials
        # Record provenance synchronously before the automation's action state
        # changes arrive. The lookup is in-memory and constant-time.
        automation_contexts.record_trigger(event)
        hass.async_create_task(automation_trials.async_handle_triggered(hass, event))

    try:
        automation_trial_unsub = hass.bus.async_listen(
            "automation_triggered", _on_automation_triggered)
        resources.add_unsub(automation_trial_unsub)
    except Exception as exc:
        _LOGGER.debug("Nova: automation probation listener registration failed: %s", exc)

    # Reminder watcher — checks every 30 seconds for due reminders
    reminder_watcher = ReminderWatcher(
        hass,
        honorific_getter=lambda: _live_honorific(hass),  # Phase C: presence-aware
        tts_getter=lambda: _get_tts(hass, entry, context="reminder"),
        speakers_getter=lambda: _get_speakers(hass, entry),
    )

    # Hand every disposable to the resource registry so unload tears them all
    # down in one fail-safe call. The scheduler is a closeable (its shutdown()
    # cancels every timer); the listener unsubs are collected too.
    resources.add_unsubs(camera_unsubs)
    resources.add_unsubs(recognition_unsubs)
    resources.add_closeable(sched)

    # Typed runtime: every field exists by now, so the runtime is never
    # exposed half-built. It is the only place Nova keeps entry state.
    runtime = NovaRuntime(
        client=llm_client,
        llm_provider_name=llm_provider_name,
        sentinel=sentinel,
        reminder_watcher=reminder_watcher,
        scheduler=sched,
        resources=resources,
        automation_contexts=automation_contexts,
    )
    entry.runtime_data = runtime

    # Read-only inventory of every automation Home Assistant has actually
    # loaded (UI, YAML, packages, and blueprints).  Build once now and refresh
    # only on automation_reloaded; live state events never rescan config.
    try:
        from .automation_inventory import AutomationInventory
        automation_inventory = AutomationInventory(hass)
        automation_inventory.start()
        automation_contexts.inventory = automation_inventory
        runtime.automation_inventory = automation_inventory
        resources.add_closeable(automation_inventory)
    except Exception as exc:
        _LOGGER.warning("Nova automation inventory unavailable (non-fatal): %s", exc)

    # Restore persisted panel settings via centralized nova_config module.
    # This loads from /config/nova/config.json (or migrates from old path).
    # We restore EVERY panel-writable key (LLM provider/model selections,
    # cognition tunables, floor plan, etc.) so choices made in the panel win
    # over addon-config defaults and survive reboots/updates. runtime_config
    # takes precedence over entry.options/data, so this is authoritative.
    # Secrets (api_key, gemini_api_key) are intentionally NOT panel-writable and
    # therefore stay addon-controlled via the reconcile above.
    try:
        from . import nova_config
        from .websocket import PANEL_WRITABLE_KEYS

        # Initialize config from entry data (backfill any missing keys)
        await hass.async_add_executor_job(
            nova_config.init_from_entry,
            dict(entry.data), dict(entry.options),
        )

        # Load persisted settings into runtime_config
        cfg = await hass.async_add_executor_job(nova_config.get_all)
        # v6.48.0: a hand-edited config.json that couldn't be used was
        # sidelined by nova_config.load() — tell the user loudly instead of
        # silently reverting every setting to defaults.
        if getattr(nova_config, "last_load_error", None):
            try:
                await hass.services.async_call(
                    "persistent_notification", "create", {
                        "title": "Nova: config.json was invalid",
                        "message": (
                            f"{nova_config.last_load_error}. Nova started "
                            "with defaults. Fix the JSON in the preserved file "
                            "and copy it back to /config/nova/config.json, "
                            "then restart."),
                        "notification_id": "nova_config_corrupt",
                    }, blocking=False)
            except Exception:
                pass
        restore_keys = set(PANEL_WRITABLE_KEYS) | {
            "broadcast_group", "observer_quiet_start",
            "observer_quiet_end", "bedroom_areas",
            "movie_media_player",  # so TTS routing can exclude the TV in-memory
        }
        rc = {k: cfg[k] for k in restore_keys if k in cfg}
        if rc:
            # Fill the runtime's own dict in place.
            runtime.runtime_config.update(rc)
            _LOGGER.info(
                "Restored %d panel settings from nova_config (%d total keys in file)",
                len(rc), len(cfg),
            )
    except Exception as exc:
        _LOGGER.debug("Config restore: %s", exc)

    # Register services — guard against double-registration on reload
    # Move any plaintext LLM credentials into secrets.yaml (v6.83.0). Safe:
    # verify-before-strip; config.json is left untouched on any failure.
    try:
        from . import ha_secrets as _hs
        await _hs.relocate_plaintext_credentials(hass)
    except Exception as exc:
        # Safety property holds regardless (verify-before-strip means a
        # credential is never lost or exposed by a failure here) but a
        # failure was previously only visible at DEBUG — you'd have no way
        # to know a credential is still sitting in plaintext in config.json.
        _LOGGER.warning("Credential relocation failed — credential(s) remain "
                        "in config.json, not moved to secrets.yaml: %s", exc)

    # Split the legacy shared credential into its own provider-specific slot
    # (Phase 2, v7.107.0). Safe: verify-before-strip, idempotent, never
    # guesses or deletes on ambiguous ownership — see split_shared_credential.
    try:
        from . import ha_secrets as _hs2
        await _hs2.split_shared_credential(hass)
    except Exception as exc:
        _LOGGER.warning("Credential provider-split migration failed — the "
                        "shared credential remains in place, unmigrated: %s", exc)

    # Move any intrusion snapshots left under the old, unauthenticated
    # /config/www location (pre-v7.102.0) to the private snapshot dir
    # (v7.102.0). Safe: files are moved, never deleted; a no-op once nothing
    # legacy remains.
    try:
        from . import intrusion as _intrusion
        await hass.async_add_executor_job(_intrusion.migrate_legacy_snapshots)
    except Exception as exc:
        _LOGGER.debug("Intrusion snapshot migration: %s", exc)

    try:
        _register_services(hass, entry, llm_client, sentinel)

        # Reload services when options change
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Auto-start Sentinel + Reminder watcher
        await sentinel.async_start()
        await reminder_watcher.async_start()
    except Exception:
        # Confirmed by tests/integration/test_setup_failure_cleanup.py: any of
        # the three calls above failing used to leave the just-registered
        # services, camera/Eufy bus listeners, and every scheduled sweep
        # behind — nothing tears them down on a setup exception, since HA
        # doesn't call async_unload_entry for you here. async_unload_entry is
        # already fail-safe/idempotent (that's what NovaResources.close_all()
        # is for) and tolerates a partial setup — unloading a platform that
        # was never forwarded is a no-op — so reuse it instead of duplicating
        # its teardown. clear_runtime() then drops runtime_data even if the
        # platform unload reported failure.
        await async_unload_entry(hass, entry)
        clear_runtime(entry)
        raise

    # ── v5.2 Observer Mode ──────────────────────────────────────────────────
    # Observer subscribes to state_changed events and proactively announces
    # interesting things through the LLM tier pipeline. OFF by default. Enabled
    # via the panel (nova_config) or add-on config — read from effective_config
    # so a panel-enabled observer stays on across a restart (v7.45.1).
    observer_enabled = bool(_eff.get("observer_enabled", False))
    if observer_enabled:
        # Observer's own settings come from the same effective config (data +
        # options + panel, panel winning), not bare entry, for the same reason.
        observer_config = dict(_eff)
        from . import observer as observer_mod
        try:
            await observer_mod.start(hass, observer_config, entry=entry)
        except Exception:
            # Same rule as the block above: a setup exception gets no
            # async_unload_entry from HA, so tear down what's registered.
            # Marked running so unload's observer.stop() clears a partial start.
            set_observer_running(entry, True)
            await async_unload_entry(hass, entry)
            clear_runtime(entry)
            raise
        set_observer_running(entry, True)
        _LOGGER.info("Nova Observer mode ENABLED — watching for interesting events")
    else:
        # A fresh runtime already starts with False.
        _LOGGER.info(
            "Nova Observer mode disabled. Enable via addon config → observer_enabled=true"
        )

    # ── Lockdown (security) — wired independently of Observer / cognitive loop ─
    # Lockdown is safety-critical, so it must not depend on observer being on or
    # on the cognitive-core start completing cleanly. Set up the manager + the
    # event-driven alarm→lockdown sync here, regardless of the above.
    try:
        from . import cognitive_core, nova_config
        # The runtime is assigned above, so a missing one is a real fault;
        # the strict snapshot raises into this block's non-fatal warning.
        # The executor gets a copy, never the loop-owned live dict.
        rc = runtime_config_snapshot(entry, strict=True)
        lockdown_config = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry, rc)
        await cognitive_core.ensure_lockdown(hass, lockdown_config)
    except Exception as exc:
        _LOGGER.warning("Nova lockdown wiring failed (non-fatal): %s", exc)

    # ── v5.4 Command Center panel ──────────────────────────────────────────
    # Register sidebar panel. Idempotent — safe if called after reload.
    try:
        await async_register_panel(hass)
    except Exception as exc:
        _LOGGER.warning("Nova panel registration failed (non-fatal): %s", exc)

    # Register WebSocket API command for live panel data
    try:
        async_register_ws(hass)
    except Exception as exc:
        _LOGGER.warning("Nova WS command registration failed (non-fatal): %s", exc)

    # ── v6.8 Proactive audio (nova.speak) + infrastructure audit ─────────
    try:
        await async_setup_proactive_audio(hass, entry)
    except Exception as exc:
        _LOGGER.warning("Nova proactive-audio setup failed (non-fatal): %s", exc)

    # ── v6.28 In-process bootstrap ─────────────────────────────────────────
    # Re-homes the old add-on's voice-stack setup (Piper/Whisper/openWakeWord
    # install, Nova voice download, Assist pipeline) into the integration.
    # No-ops cleanly off-Supervisor; runs once per version as a background task.
    try:
        from . import bootstrap
        # The returned handle cancels a pending start listener or a running
        # bootstrap task on unload, so neither outlives this entry.
        resources.add_closeable(bootstrap.schedule_bootstrap(hass))
    except Exception as exc:
        _LOGGER.warning("Nova bootstrap scheduling failed (non-fatal): %s", exc)

    # ── v6.34 Voice recognition ────────────────────────────────────────────
    # Plug an external speaker-recognition service (VoiceBM, speaker-recognition,
    # etc.) into the identity resolver's voice tier. No-op until enabled + a
    # source entity is configured.
    try:
        from . import voice_recognition
        voice_recognition.register(hass)
    except Exception as exc:
        _LOGGER.warning("Nova voice recognition registration failed (non-fatal): %s", exc)

    _LOGGER.info("Nova online. Good day, %s. Routines available: %s",
                 honorific, ", ".join(list_routines()))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NovaConfigEntry) -> bool:
    """Unload Nova."""
    # NovaRuntime owns the entry's live objects. It is absent only after a
    # partial setup that failed before it was assigned, or on a repeated
    # unload; then there is nothing entry-owned left to stop, and the
    # domain-level teardown below still runs.
    runtime = lifecycle_runtime(entry)
    sentinel: NovaSentinel | None = None
    reminder_watcher = None
    resources = None
    observer_running = False
    if runtime is not None:
        sentinel = runtime.sentinel
        reminder_watcher = runtime.reminder_watcher
        resources = runtime.resources
        observer_running = runtime.observer_running

    # Unregister the panel early — best effort
    try:
        async_unregister_panel(hass)
    except Exception as exc:
        _LOGGER.debug("Panel unregister note: %s", exc)

    if sentinel:
        await sentinel.async_stop()

    if reminder_watcher:
        await reminder_watcher.async_stop()

    # Stop observer if it's running. observer.stop() also stops the cognitive
    # core; otherwise stop the core here. Setup always wires lockdown
    # (cognitive_core.ensure_lockdown) even with the observer off, and its
    # alarm listener must not outlive the entry. cognitive_core.stop() is
    # idempotent, so a partial observer stop followed by this is safe.
    core_stopped = False
    if observer_running:
        try:
            from . import observer as observer_mod
            await observer_mod.stop()
            core_stopped = True
            if runtime is not None:
                set_observer_running(entry, False)
        except Exception as exc:
            _LOGGER.debug("Observer stop failed: %s", exc)
    if not core_stopped:
        try:
            from . import cognitive_core
            await cognitive_core.stop()
        except Exception as exc:
            _LOGGER.debug("Cognitive core stop failed: %s", exc)
    # Release the stopped core's hass/config/lockdown manager so a reload
    # builds lockdown from the new instance and config, not this entry's.
    try:
        from . import cognitive_core
        cognitive_core.release_runtime()
    except Exception as exc:
        _LOGGER.debug("Cognitive core release failed: %s", exc)

    # Clear the voice-fingerprint provider registered at setup.
    try:
        from . import voice_recognition
        voice_recognition.unregister()
    except Exception as exc:
        _LOGGER.debug("Voice recognition unregister failed: %s", exc)

    # Tear down listeners, timers, and the scheduler in one fail-safe call.
    if resources is not None:
        try:
            summary = resources.close_all()
            _LOGGER.debug("Nova: resource teardown %s", summary)
        except Exception as exc:
            _LOGGER.debug("Resource teardown note: %s", exc)

    # Cancel proactive-audio listeners (audit interval + startup) and service
    try:
        await async_unload_proactive_audio(hass, entry)
    except Exception as exc:
        _LOGGER.debug("Proactive-audio unload note: %s", exc)

    # Remove services registered by this entry
    for service in ("analyze_camera", "analyze_on_event",
                    "conversation_summary", "briefing",
                    "scene_by_intent", "routine", "add_reminder",
                    "sentinel_start", "sentinel_stop",
                    "database_purge", "database_stats",
                    "nap", "shush", "unshush",
                    "observer_start", "observer_stop", "observer_status",
                    "replay_policy", "create_automation", "diagnose_doorbell",
                    "test_notify", "test_tts", "test_routing", "lockdown",
                    "train_doorbell_backlog", "check_packages", "speak",
                    "process_intent", "remember", "forget",
                    "backup", "restore"):
        hass.services.async_remove(DOMAIN, service)

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        clear_runtime(entry)
    return ok


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_tts(hass: HomeAssistant, entry: ConfigEntry, context: str = "chat") -> str | None:
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


def _get_speakers(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
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


def _register_services(
    hass: HomeAssistant,
    entry: ConfigEntry,
    groq_client,
    sentinel: NovaSentinel,
) -> None:
    """Register all Nova services. Called once per entry setup."""

    async def _camera(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="camera")
        spk = _get_speakers(hass, entry)
        await async_analyze_camera(hass, call, groq_client, honorific, tts, spk)

    hass.services.async_register(
        DOMAIN, "analyze_camera", _camera,
        schema=vol.Schema({
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
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="doorbell")
        spk = _get_speakers(hass, entry)
        entity_id = call.data["entity_id"]
        reason    = call.data.get("reason", "Activity detected")
        await async_auto_analyze_on_event(
            hass, groq_client, honorific, tts, spk, entity_id, reason
        )

    hass.services.async_register(
        DOMAIN, "analyze_on_event", _analyze_on_event,
        schema=vol.Schema({
            vol.Required("entity_id"): cv.entity_id,
            vol.Optional("reason", default="Activity detected"): cv.string,
        }),
    )

    # ── Doorbell backlog → training data ───────────────────────────────────────
    async def _train_backlog(call: ServiceCall) -> None:
        """Analyse the Nest doorbell's recorded event history into the training
        log. Best-effort; reports how many events it managed to analyse."""
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        limit = int(call.data.get("limit", 40) or 40)
        from . import doorbell_training
        from .camera import async_analyze_camera, _FakeCall, active_cameras, active_camera_states

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
                hass, fc, groq_client, honorific, None, [],
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

    hass.services.async_register(
        DOMAIN, "train_doorbell_backlog", _train_backlog,
        schema=vol.Schema({
            vol.Optional("limit", default=40): vol.Coerce(int),
        }),
    )

    # ── Package / mail — on-demand check ───────────────────────────────────────
    async def _check_packages(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="package")
        spk = _get_speakers(hass, entry)
        from . import package_monitor
        cam = call.data.get("entity_id")
        report = await package_monitor.periodic_check(
            hass, groq_client, honorific, tts, spk, configured_camera=cam
        )
        _LOGGER.info("Nova manual package check: %s", report)

    hass.services.async_register(
        DOMAIN, "check_packages", _check_packages,
        schema=vol.Schema({
            vol.Optional("entity_id"): cv.entity_id,
        }),
    )

    # ── Briefing ──────────────────────────────────────────────────────────────
    async def _briefing(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="briefing")
        spk = _get_speakers(hass, entry)
        await async_briefing(hass, call, groq_client, honorific, tts, spk)

    async def _nova_backup(call):
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

    hass.services.async_register(DOMAIN, "backup", _nova_backup)

    async def _nova_restore(call):
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

    hass.services.async_register(
        DOMAIN, "restore", _nova_restore,
        schema=vol.Schema({vol.Optional("archive"): str}))

    hass.services.async_register(
        DOMAIN, "briefing", _briefing,
        schema=vol.Schema({
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
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="chat")
        spk = _get_speakers(hass, entry)
        await async_activate_by_intent(hass, call, groq_client, honorific, tts, spk)

    hass.services.async_register(
        DOMAIN, "scene_by_intent", _scene_intent,
        schema=vol.Schema({
            vol.Required("intent"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
        }),
    )

    # ── Routine ───────────────────────────────────────────────────────────────
    async def _routine(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="routine")
        spk = _get_speakers(hass, entry)
        await async_run_routine(hass, call, honorific, tts, spk)

    hass.services.async_register(
        DOMAIN, "routine", _routine,
        schema=vol.Schema({
            vol.Required("name"): cv.string,
        }),
    )

    # ── Add reminder ──────────────────────────────────────────────────────────
    async def _add_reminder(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="reminder")
        spk = _get_speakers(hass, entry)
        await async_add_reminder_service(hass, call, honorific, tts, spk)

    hass.services.async_register(
        DOMAIN, "add_reminder", _add_reminder,
        schema=vol.Schema({
            vol.Required("label"): cv.string,
            vol.Required("trigger_at"): cv.string,
            vol.Optional("repeat"): vol.In(["daily", "weekly", "hourly"]),
            vol.Optional("require_home", default=True): cv.boolean,
            vol.Optional("respect_quiet", default=True): cv.boolean,
        }),
    )

    async def _summary(call: ServiceCall) -> None:
        honorific = _live_honorific(hass)  # Phase C: presence-aware
        tts = _get_tts(hass, entry, context="summary")
        spk = _get_speakers(hass, entry)
        await async_summarise(hass, call, groq_client, honorific, tts, spk)

    hass.services.async_register(
        DOMAIN, "conversation_summary", _summary,
        schema=vol.Schema({
            vol.Optional("hours", default=24): vol.All(int, vol.Range(min=1, max=168)),
            vol.Optional("device_id"): cv.string,
            vol.Optional("announce", default=True): cv.boolean,
            vol.Optional("store", default=True): cv.boolean,
        }),
    )

    async def _sentinel_start(call: ServiceCall) -> None:
        await sentinel.async_start()

    hass.services.async_register(DOMAIN, "sentinel_start", _sentinel_start)

    async def _sentinel_stop(call: ServiceCall) -> None:
        await sentinel.async_stop()

    hass.services.async_register(DOMAIN, "sentinel_stop", _sentinel_stop)

    async def _db_purge(call: ServiceCall) -> None:
        days = call.data.get("days", 30)
        deleted = await hass.async_add_executor_job(purge_old_records, days)
        _LOGGER.info("Nova DB purge: %d records deleted (>%d days)", deleted, days)

    hass.services.async_register(
        DOMAIN, "database_purge", _db_purge,
        schema=vol.Schema({
            vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=365))
        }),
    )

    async def _db_stats(call: ServiceCall) -> None:
        stats = await hass.async_add_executor_job(get_stats)
        hass.bus.async_fire("nova_db_stats", stats)

    hass.services.async_register(DOMAIN, "database_stats", _db_stats)

    async def _replay_policy(call: ServiceCall) -> None:
        """Replay recorded decisions of a kind against candidate confidence
        thresholds and report the best-separating threshold from real outcomes.
        Read-only — evaluates history, never changes behaviour."""
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

    hass.services.async_register(
        DOMAIN, "replay_policy", _replay_policy,
        schema=vol.Schema({
            vol.Required("kind"): str,
            vol.Optional("min_samples"): vol.All(int, vol.Range(min=1, max=100000)),
        }),
    )

    # ── v5.2 Observer Mode services ──────────────────────────────────────────

    async def _nap(call: ServiceCall) -> None:
        """Manual mute for N minutes (default 30). Suppresses non-critical
        announcements until the duration elapses."""
        from . import sleep_detection as sd
        duration = call.data.get("duration_minutes", 30)
        sd.set_nap(duration)
        hass.bus.async_fire("nova_observer_nap", {"duration_minutes": duration})

    hass.services.async_register(
        DOMAIN, "nap", _nap,
        schema=vol.Schema({
            vol.Optional("duration_minutes", default=30):
                vol.All(int, vol.Range(min=1, max=480)),
        }),
    )

    async def _shush(call: ServiceCall) -> None:
        """Tell Nova to stop announcing. Pass all=true to mute every
        non-critical announcement; critical safety alerts always pass."""
        from . import output_gate
        entity_id = call.data.get("entity_id")
        category  = call.data.get("category")
        shush_all = bool(call.data.get("all", False))
        result = output_gate.shush(entity_id=entity_id, category=category, all=shush_all)
        hass.bus.async_fire("nova_observer_shushed", result)
        _LOGGER.info("Nova shushed: %s", result)

    hass.services.async_register(
        DOMAIN, "shush", _shush,
        schema=vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
            vol.Optional("all"): cv.boolean,
        }),
    )

    async def _unshush(call: ServiceCall) -> None:
        """Undo a shush. Called with no args clears ALL mutes."""
        from . import output_gate
        entity_id = call.data.get("entity_id")
        category  = call.data.get("category")
        result = output_gate.unshush(entity_id=entity_id, category=category)
        hass.bus.async_fire("nova_observer_unshushed", result)

    hass.services.async_register(
        DOMAIN, "unshush", _unshush,
        schema=vol.Schema({
            vol.Optional("entity_id"): cv.string,
            vol.Optional("category"): cv.string,
        }),
    )

    async def _observer_start(call: ServiceCall) -> None:
        """Start the observer manually (even if config has it disabled)."""
        from . import observer as observer_mod, nova_config as _jc
        get_runtime(entry)   # ownership first: never start an unowned observer
        # Fresh effective config (data + options + panel, panel winning) so a
        # manual start honors current panel settings, not stale entry data.
        observer_config = await hass.async_add_executor_job(_jc.effective_config, entry)
        await observer_mod.start(hass, observer_config, entry=entry)
        set_observer_running(entry, True)
        _LOGGER.info("Observer started via service call")

    hass.services.async_register(DOMAIN, "observer_start", _observer_start)

    async def _lockdown(call: ServiceCall) -> None:
        """Engage or lift the formal lockdown state (alarm-armed posture)."""
        from . import cognitive_core
        raw = call.data.get("state", call.data.get("enabled", "on"))
        on = raw in (True, "on", "true", "True", "engage", "lock", 1, "1")
        ok = await cognitive_core.request_lockdown(
            on, reason=call.data.get("reason", "requested via service"), hass=hass)
        if not ok:
            _LOGGER.warning("Lockdown service: request could not be handled (no hass)")
        else:
            _LOGGER.info("Lockdown %s via service call", "engaged" if on else "lifted")

    hass.services.async_register(DOMAIN, "lockdown", _lockdown)

    async def _remember(call: ServiceCall) -> None:
        """Teach Nova a durable fact or preference (knowledge store)."""
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

    hass.services.async_register(DOMAIN, "remember", _remember)

    async def _forget(call: ServiceCall) -> None:
        """Forget a stored fact by id, or by key (with optional subject)."""
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

    hass.services.async_register(DOMAIN, "forget", _forget)

    async def _observer_stop(call: ServiceCall) -> None:
        """Stop the observer."""
        from . import observer as observer_mod
        get_runtime(entry)   # ownership first: never stop an unowned observer
        await observer_mod.stop()
        set_observer_running(entry, False)
        _LOGGER.info("Observer stopped via service call")

    hass.services.async_register(DOMAIN, "observer_stop", _observer_stop)

    async def _observer_status(call: ServiceCall) -> None:
        """Fire event with current observer state — mute list, recent activity."""
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

    hass.services.async_register(DOMAIN, "observer_status", _observer_status)

    # v5.6.0: Automation creation service
    async def _create_automation(call: ServiceCall) -> None:
        """Create an HA automation from service call data."""
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

    hass.services.async_register(DOMAIN, "create_automation", _create_automation)

    # v5.6.0: Doorbell pipeline diagnostic
    async def _diagnose_doorbell(call: ServiceCall) -> None:
        """Run doorbell pipeline diagnostics and fire event with results."""
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

        # Check 4: Is nova.analyze_on_event registered?
        svc_exists = hass.services.has_service(DOMAIN, "analyze_on_event")
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

    hass.services.async_register(DOMAIN, "diagnose_doorbell", _diagnose_doorbell)

    # v5.6.5: Test notification service
    async def _test_notify(call: ServiceCall) -> None:
        """Send a test notification to every configured normal target."""
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

    hass.services.async_register(DOMAIN, "test_notify", _test_notify)

    # v5.6.7: Test TTS with Nova voice
    async def _test_tts(call: ServiceCall) -> None:
        """Play a test tone using Nova Piper voice on the broadcast group."""
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

    hass.services.async_register(DOMAIN, "test_tts", _test_tts)

    # v5.7.00: Routing diagnostic — dumps current routing state to log
    async def _test_routing(call: ServiceCall) -> None:
        """Dump routing diagnostics to the HA log."""
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

    hass.services.async_register(DOMAIN, "test_routing", _test_routing)
