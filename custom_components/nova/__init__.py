"""Nova, the Home Assistant integration.

``async_setup`` runs once per Home Assistant run and registers the services.
``async_setup_entry`` builds the entry's NovaRuntime together with everything
that lives exactly as long as the entry: camera reactions, package and mail
checks, periodic sweeps and the clock-time briefings. ``async_unload_entry``
takes all of it down again in a fixed order.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from functools import partial
from inspect import isawaitable
from types import SimpleNamespace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.typing import ConfigType

from .const import CONF_HONORIFIC, DEFAULT_HONORIFIC, DOMAIN
from .camera import async_auto_analyze_on_event, register_event_listeners
from .briefing import async_briefing
from .routines import list_routines
from .reminders import ReminderWatcher
from .recognition import register_recognition_listener
from .sentinel import NovaSentinel
from .database import purge_old_records
from .llm_provider import (
    create_provider,
    resolve_provider_credential,
    resolve_provider_endpoint,
)
from .migrations import CURRENT_SCHEMA_VERSION, migrate_config, schema_version_is_valid
from .runtime import (
    NovaConfigEntry,
    NovaRuntime,
    NovaRuntimeUnavailable,
    clear_runtime,
    lifecycle_runtime,
    lifecycle_runtime_config,
    runtime_config_snapshot,
    set_observer_running,
)
from .panel_register import async_register_panel, async_unregister_panel
from .websocket import async_register as async_register_ws
from .proactive_audio import async_setup_proactive_audio, async_unload_proactive_audio
# The speaker, TTS and honorific helpers live in services.py. The listeners
# and ticks below use them, and other code reaches them through the package.
from .services import (
    _get_speakers,
    _get_tts,
    _live_honorific,
    async_setup_services,
)

_LOGGER = logging.getLogger(__name__)


def _prewarm_persisted_state() -> None:
    """Fill the caches of persisted state once, on a worker thread.

    The observer tick, the panel and the intrusion log read these later on
    the event loop; warming them here keeps their first read off it. Every
    step is independent and best effort: none of them may stop setup."""
    with suppress(Exception):
        from . import ha_secrets
        ha_secrets._read_secrets(force=True)
    with suppress(Exception):
        from . import modes
        modes._load()
    with suppress(Exception):
        from . import intrusion
        intrusion._load_log()
    with suppress(Exception):
        from . import reasoning_cache
        reasoning_cache.load()
    with suppress(Exception):
        # Cached from here on; the panel's status handler reads it on the loop.
        from . import habituation
        habituation._load()
    with suppress(Exception):
        # Expired rows go even on a day when nothing new is written.
        from . import scene_memory
        scene_memory.prune()


PLATFORMS = ["conversation", "stt"]
# Nova is set up through its config entry only; a `nova:` block in YAML
# gets Home Assistant's standard warning.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Once per Home Assistant run: point the stores at this instance and
    register the services.

    The services outlive every entry load, reload, unload and failed setup;
    each call looks up the loaded entry and its NovaRuntime for itself."""
    from . import paths
    paths.configure(hass)
    async_setup_services(hass)
    # Saved mutes and the face roster load after the paths are known, off the
    # event loop. A missing or unreadable file is not an error.
    from . import face_roster, output_gate
    await hass.async_add_executor_job(output_gate.load_mutes)
    await hass.async_add_executor_job(face_roster.load)
    return True


async def _attempt(message: str, work: Callable[[], Any], *,
                   level: int = logging.WARNING) -> bool:
    """Run one optional setup or teardown step.

    `work` may be plain or return an awaitable. A failure is logged with
    `message` at `level` and never propagates."""
    try:
        outcome = work()
        if isawaitable(outcome):
            await outcome
    except Exception as exc:
        _LOGGER.log(level, message, exc)
        return False
    return True


# ── Panel switches ──────────────────────────────────────────────────────────

_ON_WORDS = ("1", "true", "yes", "on")


def _auto_flag(entry: NovaConfigEntry, key: str, default: bool) -> bool:
    """A panel switch, read from the live runtime settings on every call.

    During setup, before the runtime exists, there are no panel settings yet
    and `default` applies. A loaded entry that has lost its runtime raises
    NovaRuntimeUnavailable, which is why the read sits outside the guard."""
    live = lifecycle_runtime_config(entry)
    try:
        if key not in live:
            return default
        value = live[key]
        if isinstance(value, bool):
            return value
        return str(value).lower() in _ON_WORDS
    except Exception:
        return default


# ── Camera reactions ────────────────────────────────────────────────────────

# Seconds before the same camera may trigger the same reaction again.
_PRESS_GAP = 12.0          # a doorbell double press counts once
_VISITOR_GAP = 180.0       # person sightings feeding visitor learning
_DETECTION_GAP = 120.0     # Frigate object detections
_QUIET_GAP = 300.0         # Eufy vehicle and pet signals

_FRIGATE_LABELS = frozenset({
    "person", "car", "truck", "package", "dog", "cat", "bicycle", "motorcycle",
})
# Eufy sibling sensors Nova listens to. "pet" only feeds semantic learning.
_EUFY_ROLES = (
    "ringing", "stranger", "person", "vehicle", "pet",
    "package_delivered", "package_stranded", "package_taken",
)


class _CameraWatch:
    """Nova's own reactions to doorbell and camera signals for one entry.

    Nest and Frigate put events on the bus; Eufy only flips sibling sensors.
    A doorbell press always gets the full announced analysis. Person, motion,
    vehicle and pet signals are throttled per camera and mostly silent, and
    package signals go to package_monitor. "Camera Watch"
    (camera_auto_analyze) gates everything except the package signals, which
    follow "Package Watch" (package_detection) alone."""

    def __init__(self, hass: HomeAssistant, entry: NovaConfigEntry, client: Any):
        from .camera import _nest_device_to_camera
        self.hass = hass
        self.entry = entry
        self.client = client
        self._nest_camera = _nest_device_to_camera
        # One timestamp table serves person, motion, vehicle and pet across
        # every source; doorbell presses keep their own.
        self._recent: dict[str, float] = {}
        self._pressed: dict[str, float] = {}
        self._eufy: dict[str, tuple[str, str]] = {}

    def _flag(self, key: str, default: bool) -> bool:
        return _auto_flag(self.entry, key, default)

    @staticmethod
    def _due(seen: dict[str, float], key: str, gap: float) -> bool:
        """Stamp `key` and return True, unless it was stamped under `gap` ago."""
        now = time.monotonic()
        last = seen.get(key)
        if last is not None and now - last < gap:
            return False
        seen[key] = now
        return True

    def _analyse(self, camera: str, reason: str, context: str,
                 doorbell: bool = False) -> None:
        """The full vision analysis, announced when notable."""
        hass, entry = self.hass, self.entry
        honorific = _live_honorific(hass)
        tts = _get_tts(hass, entry, context=context)
        speakers = _get_speakers(hass, entry)
        hass.async_create_task(async_auto_analyze_on_event(
            hass, self.client, honorific, tts, speakers, camera, reason,
            doorbell=doorbell))

    def _observe_visitor(self, camera: str) -> None:
        """Silent visitor learning: training data only, never spoken."""
        from .camera import async_visitor_observation
        honorific = _live_honorific(self.hass)
        self.hass.async_create_task(
            async_visitor_observation(self.hass, self.client, honorific, camera))

    def _remember(self, camera: str, **fields: Any) -> None:
        """Add a sighting to the semantic pattern store (additive only)."""
        from . import camera_semantic
        self.hass.async_create_task(camera_semantic.record_event(
            self.hass, camera_entity=camera, source="eufy", **fields))

    def listen_bus(self, unsubs: list) -> None:
        unsubs.append(self.hass.bus.async_listen("nest_event", self.on_nest))
        unsubs.append(self.hass.bus.async_listen("frigate_event", self.on_frigate))
        _LOGGER.info("Nova: camera auto-analysis active (doorbell always, "
                     "person/motion throttled)")

    # Nest ------------------------------------------------------------------

    @callback
    def on_nest(self, event: Any) -> None:
        if not self._flag("camera_auto_analyze", True):
            return
        try:
            self._nest(event.data)
        except Exception as exc:
            _LOGGER.debug("Nova auto-analyze (nest) error: %s", exc)

    def _nest(self, data: dict) -> None:
        device = data.get("device_id") or data.get("nest_device_id")
        kind = str(data.get("type") or data.get("event_type") or "").lower()
        if not device:
            return
        pressed = "chime" in kind or "doorbell" in kind
        # Apart from a press, only a person sighting matters, and only for
        # visitor learning; motion and sound are ignored.
        if not pressed and ("person" not in kind
                            or not self._flag("visitor_learning", True)):
            return
        camera = self._nest_camera(self.hass, device)
        if not camera:
            return
        if pressed:
            if self._due(self._pressed, camera, _PRESS_GAP):
                self._analyse(camera, "Someone is at the front door", "doorbell",
                              doorbell=True)
        elif self._due(self._recent, camera, _VISITOR_GAP):
            self._observe_visitor(camera)

    # Frigate ---------------------------------------------------------------

    @callback
    def on_frigate(self, event: Any) -> None:
        # Frigate reports detections, never a press, so with the press-only
        # default it stays quiet unless motion analysis is opted into too.
        if not (self._flag("camera_auto_analyze", True)
                and self._flag("camera_auto_analyze_motion", False)):
            return
        try:
            self._frigate(event.data)
        except Exception as exc:
            _LOGGER.debug("Nova auto-analyze (frigate) error: %s", exc)

    def _frigate(self, data: dict) -> None:
        if data.get("type") != "new":
            return
        tracked = data.get("after") or data.get("before") or {}
        name = tracked.get("camera")
        label = str(tracked.get("label") or "").lower()
        if not name:
            return
        camera = f"camera.{str(name).lower()}"
        if not self.hass.states.get(camera):
            return
        if label and label not in _FRIGATE_LABELS:
            return
        if not self._due(self._recent, camera, _DETECTION_GAP):
            return
        reason = f"{label.capitalize()} detected" if label else "Motion detected"
        self._analyse(camera, reason, "camera")

    # Eufy ------------------------------------------------------------------

    def watch_eufy(self, unsubs: list) -> None:
        """Map each watched Eufy sensor to its camera and role, and listen.

        The map is built once: a camera added later needs a reload, as with
        every other camera discovery in Nova."""
        from . import eufy
        cameras = eufy.all_camera_roles(self.hass)
        for camera, roles in cameras.items():
            for role in _EUFY_ROLES:
                sensor = roles.get(role)
                if sensor:
                    self._eufy[sensor] = (camera, role)
        if not self._eufy:
            return
        unsubs.append(async_track_state_change_event(
            self.hass, list(self._eufy), self.on_eufy))
        _LOGGER.info("Nova: Eufy native-sensor watch active "
                     "(%d camera(s), %d sensor(s))", len(cameras), len(self._eufy))

    @callback
    def on_eufy(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != "on":
            return
        hit = self._eufy.get(event.data.get("entity_id"))
        if not hit:
            return
        camera, role = hit
        if role.startswith("package_"):
            # Package Watch alone, as with the porch sweep this replaced, so
            # Camera Watch off with Package Watch on keeps working.
            if self._flag("package_detection", False):
                self._eufy_package(camera, role)
            return
        if not self._flag("camera_auto_analyze", True):
            return
        getattr(self, f"_eufy_{role}")(camera)

    def _eufy_package(self, camera: str, role: str) -> None:
        from . import package_monitor
        hass, entry = self.hass, self.entry
        honorific = _live_honorific(hass)
        tts = _get_tts(hass, entry, context="package")
        speakers = _get_speakers(hass, entry)
        hass.async_create_task(package_monitor.note_from_eufy(
            hass, honorific, tts, speakers, camera, role))

    def _eufy_ringing(self, camera: str) -> None:
        if self._due(self._pressed, camera, _PRESS_GAP):
            self._analyse(camera, "Someone is ringing the doorbell", "doorbell",
                          doorbell=True)

    def _eufy_stranger(self, camera: str) -> None:
        # The one person signal worth a vision call.
        if not self._flag("visitor_learning", True):
            return
        if not self._due(self._recent, camera, _VISITOR_GAP):
            return
        self._observe_visitor(camera)
        # Eufy already judged the face a stranger: the record stays
        # unattributed even if Nova's recognition cache has a stale match.
        self._remember(camera, label="person", attribute=False, detail="stranger")

    def _eufy_person(self, camera: str) -> None:
        # A known face: logged for the pattern engine, no model call.
        if not self._flag("visitor_learning", True):
            return
        if not self._due(self._recent, f"{camera}:known", _VISITOR_GAP):
            return
        self.hass.async_create_task(self._log_known_visitor(camera))
        self._remember(camera, label="person", detail="known_person")

    async def _log_known_visitor(self, camera: str) -> None:
        try:
            from . import doorbell_training
            from .camera import _camera_friendly_name
            name = _camera_friendly_name(self.hass, camera)
            await self.hass.async_add_executor_job(
                doorbell_training.log_event, name, camera, "eufy",
                {"summary": "", "analysis": "", "category": "known_resident",
                 "notable": False})
        except Exception as exc:
            _LOGGER.debug("Nova eufy: known-visitor log failed: %s", exc)

    def _eufy_vehicle(self, camera: str) -> None:
        # Announced straight away, no vision call. The signal is Eufy's
        # vehicle detection switch (see eufy.py for why not the sensor).
        if not self._due(self._recent, f"{camera}:vehicle", _QUIET_GAP):
            return
        self.hass.async_create_task(self._announce_vehicle(camera))
        # Never attributed: a vehicle is never a resident.
        self._remember(camera, label="vehicle", detail="motion_detection_type_vehicle")

    async def _announce_vehicle(self, camera: str) -> None:
        try:
            from . import delivery, persona
            from .camera import _camera_friendly_name
            hass, entry = self.hass, self.entry
            honorific = _live_honorific(hass)
            tts = _get_tts(hass, entry, context="camera")
            speakers = _get_speakers(hass, entry)
            place = _camera_friendly_name(hass, camera)
            # A proactive alert, so the household's delivery rules decide.
            await delivery.async_deliver(hass, delivery.DeliveryRequest(
                source="vehicle", entity_id=camera, category="vehicle",
                message=persona.lead_in(
                    honorific, f"a vehicle was detected at {place}."),
                speakers=tuple(speakers or ()), tts_entity=tts or "",
                tts_context="camera"))
        except Exception as exc:
            _LOGGER.debug("Nova eufy: vehicle announce failed: %s", exc)

    def _eufy_pet(self, camera: str) -> None:
        # Recorded only; there is no spoken behaviour for animals.
        if self._due(self._recent, f"{camera}:pet", _QUIET_GAP):
            self._remember(camera, label="animal", detail="pet_detected")


# ── Package and mail ────────────────────────────────────────────────────────

def _watch_package_sensors(hass: HomeAssistant, entry: NovaConfigEntry,
                           client: Any, unsubs: list) -> None:
    """Porch motion and mailbox sensors that bring the package check forward.

    Found once at setup, so a sensor added later needs a reload. Motion only
    moves the existing porch sweep earlier; a mailbox runs the same state
    machine with mail set. package_monitor owns quiet hours, wording and its
    cooldown; Eufy cameras with package sensors go through note_from_eufy."""
    from . import package_monitor
    sensors = package_monitor.discover_trigger_sensors(hass)
    if not sensors:
        return
    running: set = set()

    def delivery_context() -> tuple:
        return (_live_honorific(hass), _get_tts(hass, entry, context="package"),
                _get_speakers(hass, entry))

    async def check(kind: str, sensor: str) -> None:
        try:
            if kind == "mailbox":
                await package_monitor.note_from_mailbox(
                    hass, client, delivery_context, sensor)
            else:
                await package_monitor.note_from_motion(
                    hass, client, delivery_context, sensor)
        except Exception as exc:  # cancellation is not an Exception
            _LOGGER.debug("Nova package sensor trigger error (%s): %s", sensor, exc)

    @callback
    def on_change(event: Any) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        # A real off -> on only: coming back from unavailable after a restart
        # or reconnect is nobody at the door.
        if new_state is None or new_state.state != "on":
            return
        if old_state is None or old_state.state != "off":
            return
        sensor = event.data.get("entity_id")
        kind = sensors.get(sensor)
        if not kind or not _auto_flag(entry, "package_detection", False):
            return
        # In the background: the check may wait 20 s and must not hold up
        # Home Assistant's task tracking or shutdown.
        task = hass.async_create_background_task(
            check(kind, sensor), "nova_package_sensor_trigger")
        running.add(task)
        task.add_done_callback(running.discard)

    def cancel_running() -> None:
        for task in list(running):
            task.cancel()

    unsubs.append(async_track_state_change_event(hass, list(sensors), on_change))
    unsubs.append(cancel_running)
    kinds = list(sensors.values())
    _LOGGER.info("Nova: package early-check sensors active (%d motion, %d mailbox)",
                 kinds.count("motion"), kinds.count("mailbox"))


# ── Periodic sweeps (run by NovaScheduler, which also counts failures) ──────

async def _package_tick(hass: HomeAssistant, entry: NovaConfigEntry,
                        client: Any, _now: Any) -> None:
    """Porch sweep for deliveries that never rang the bell. A parcel left all
    day is announced once, on arrival; quiet hours are respected."""
    if not _auto_flag(entry, "package_detection", False):
        return
    try:
        from . import package_monitor
        honorific = _live_honorific(hass)
        tts = _get_tts(hass, entry, context="package")
        speakers = _get_speakers(hass, entry)
        report = await package_monitor.periodic_check(
            hass, client, honorific, tts, speakers, configured_camera=None)
        _LOGGER.debug("Nova package check: %s", report)
    except Exception as exc:
        _LOGGER.debug("Nova package tick error: %s", exc)


async def _health_tick(hass: HomeAssistant, _now: Any) -> None:
    """Reachability of core dependencies, so the panel stays current. A
    synthetic miss reads as idle; only real usage failures show as down."""
    try:
        from .diagnostics import run_service_health
        result = await run_service_health(hass)
        _LOGGER.debug("Nova hourly health: %s", result.get("summary"))
    except Exception as exc:
        _LOGGER.debug("Nova health tick error: %s", exc)


async def _host_health_tick(hass: HomeAssistant, entry: NovaConfigEntry,
                            _now: Any) -> None:
    """One System Monitor sample for host-health awareness (a no-op while
    its master switch is off). Panel changes apply on the next sample."""
    try:
        from . import host_health, nova_config
        rc = runtime_config_snapshot(entry)
        cfg = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry, rc)
        result = await host_health.tick(hass, cfg)
        _LOGGER.debug("Nova host-health tick: %s", result)
    except NovaRuntimeUnavailable as exc:
        _LOGGER.warning("Nova host-health tick skipped: %s", exc)
    except Exception as exc:
        _LOGGER.debug("Nova host-health tick error: %s", exc)


async def _hazard_tick(hass: HomeAssistant, _now: Any) -> None:
    """Nearby earthquakes, weather warnings and natural events (off unless
    enabled; each feed fails safe and never invents an event)."""
    try:
        from . import hazard_monitor
        result = await hazard_monitor.periodic_check(hass, _live_honorific(hass))
        if result.get("fired"):
            _LOGGER.debug("Nova hazard sweep: %s", result)
    except Exception as exc:
        _LOGGER.debug("Nova hazard tick error: %s", exc)


async def _docs_tick(hass: HomeAssistant, _now: Any) -> None:
    """Ingest new or changed files from the documents and watch folders."""
    try:
        from . import documents
        dropped = await documents.auto_ingest_new(hass)
        watched = await documents.scan_watch_folders(hass)
        added = sum((found.get("new_files", 0) or 0) for found in (dropped, watched))
        if added:
            _LOGGER.info("Nova: auto-ingested %d new document(s)", added)
    except Exception as exc:
        _LOGGER.debug("Nova docs auto-ingest error: %s", exc)


async def _sleep_prompt_tick(hass: HomeAssistant, entry: NovaConfigEntry,
                             _now: Any) -> None:
    """The nightly "heading to bed?" prompt; sleep_detection decides whether
    it is time. Panel changes apply on the next check."""
    try:
        from . import nova_config, sleep_detection
        rc = runtime_config_snapshot(entry)
        cfg = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry, rc)
        await sleep_detection.maybe_prompt_sleep(hass, cfg)
    except NovaRuntimeUnavailable as exc:
        _LOGGER.warning("Nova sleep-prompt tick skipped: %s", exc)
    except Exception as exc:
        _LOGGER.debug("Nova sleep-prompt tick error: %s", exc)


_PURGE_KEEP_DAYS = 30


async def _db_purge_tick(hass: HomeAssistant, _now: Any) -> None:
    """Old conversation rows and expired facts, once a day."""
    try:
        deleted = await hass.async_add_executor_job(purge_old_records, _PURGE_KEEP_DAYS)
        from . import knowledge
        expired = await hass.async_add_executor_job(knowledge.purge_expired)
        if deleted or expired:
            _LOGGER.info("Nova daily purge: %d conversation record(s), "
                         "%d expired fact(s)", deleted, expired)
    except Exception as exc:
        _LOGGER.debug("Nova db purge tick error: %s", exc)


# ── Scheduled briefings ─────────────────────────────────────────────────────
# Morning and evening briefings at clock times, off unless switched on. The
# content comes from the same engine as the nova.briefing service.

def _briefing_setting(key: str, default: Any) -> Any:
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _parse_hhmm(value: Any, fallback_h: int, fallback_m: int) -> tuple[int, int]:
    """"HH:MM" (anything after the minutes ignored) as (hour, minute), or the
    fallback when it is not a valid time of day."""
    try:
        hour, minute = (int(part) for part in str(value).split(":")[:2])
    except Exception:
        return fallback_h, fallback_m
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return fallback_h, fallback_m


def _house_empty(hass: HomeAssistant) -> bool:
    """True only when every person reads away. Unknown presence, or no
    people at all, never counts as empty (fails open)."""
    try:
        from . import household
        return bool(household.everyone_away(hass))
    except Exception:
        return False


async def _run_briefing(hass: HomeAssistant, entry: NovaConfigEntry,
                        llm_client: Any, kind: str) -> None:
    try:
        if not bool(_briefing_setting(f"briefing_{kind}_enabled", False)):
            return
        if bool(_briefing_setting("briefing_require_home", True)) and _house_empty(hass):
            _LOGGER.debug("Nova: skipping %s briefing — everyone away", kind)
            return
        honorific = entry.options.get(
            CONF_HONORIFIC, entry.data.get(CONF_HONORIFIC, DEFAULT_HONORIFIC))
        tts = _get_tts(hass, entry, context="briefing")
        speakers = _get_speakers(hass, entry)
        data: dict[str, Any] = {"announce": True}
        for section in ("weather", "calendar", "presence", "events", "energy",
                        "hazards"):
            data[f"include_{section}"] = bool(
                _briefing_setting(f"briefing_include_{section}", True))
        # The morning looks back over the night, the evening over the day.
        data["hours"] = 12 if kind == "morning" else 14
        call = SimpleNamespace(data=data)
        await async_briefing(hass, call, llm_client, honorific, tts, speakers)
        _LOGGER.info("Nova: delivered %s briefing", kind)
    except Exception as exc:
        # Visible on purpose: at debug level a failure here once went unseen.
        _LOGGER.warning("Nova %s briefing failed: %s", kind, exc)


async def _morning_briefing(hass: HomeAssistant, entry: NovaConfigEntry,
                            llm_client: Any, _now: Any) -> None:
    await _run_briefing(hass, entry, llm_client, "morning")


async def _evening_briefing(hass: HomeAssistant, entry: NovaConfigEntry,
                            llm_client: Any, _now: Any) -> None:
    await _run_briefing(hass, entry, llm_client, "evening")


def _schedule_briefings(hass: HomeAssistant, entry: NovaConfigEntry,
                        llm_client: Any, unsubs: list) -> None:
    morning = _parse_hhmm(_briefing_setting("briefing_morning_time", "07:30"), 7, 30)
    evening = _parse_hhmm(_briefing_setting("briefing_evening_time", "19:30"), 19, 30)
    for (hour, minute), job in ((morning, _morning_briefing),
                                (evening, _evening_briefing)):
        unsubs.append(async_track_time_change(
            hass, partial(job, hass, entry, llm_client),
            hour=hour, minute=minute, second=0))
    _LOGGER.info("Nova: briefings scheduled (morning %02d:%02d, evening %02d:%02d)",
                 *morning, *evening)


# ── Setup steps ─────────────────────────────────────────────────────────────

def _migrate_schema(hass: HomeAssistant, entry: NovaConfigEntry) -> None:
    """Bring an entry saved by an older Nova up to the current schema."""
    version = entry.data.get("schema_version", 1)
    # Anything that is not a number (None, text) still goes to migrate_config,
    # which reports it; comparing it here would fail setup.
    if isinstance(version, (int, float)) and not version < CURRENT_SCHEMA_VERSION:
        return
    data, options, new_version = migrate_config(
        dict(entry.data), dict(entry.options), version)
    # An unusable version was reported above; saving it back would only
    # repeat the report on every start with nothing migrated.
    if not schema_version_is_valid(version):
        return
    data["schema_version"] = new_version
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    _LOGGER.info("Nova: migrated config from schema v%d to v%d", version, new_version)


async def _hydrate_spoken_history(hass: HomeAssistant) -> None:
    # Loading the latest entry lets "repeat that" work right after a restart.
    from . import spoken_history
    spoken_history.configure(hass)
    await hass.async_add_executor_job(spoken_history.hydrate)


def _configure_action_log(hass: HomeAssistant) -> None:
    from . import action_log
    action_log.configure(hass)


def _listen_adaptive(hass: HomeAssistant, resources: Any) -> None:
    # The Helpful / Not helpful buttons on routine alerts; taps are ignored
    # while the setting is off.
    from . import adaptive_awareness
    resources.add_unsub(adaptive_awareness.async_listen(hass))


def _start_inventory(hass: HomeAssistant, runtime: NovaRuntime,
                     contexts: Any, resources: Any) -> None:
    """Read-only inventory of every automation Home Assistant has loaded,
    rebuilt only on automation_reloaded, never on state events."""
    from .automation.inventory import AutomationInventory
    inventory = AutomationInventory(hass)
    inventory.start()
    contexts.inventory = inventory
    runtime.automation_inventory = inventory
    resources.add_closeable(inventory)


# Restored from config.json besides the panel-writable keys.
_EXTRA_RESTORED_KEYS = frozenset({
    "broadcast_group", "observer_quiet_start", "observer_quiet_end",
    "bedroom_areas",
    "movie_media_player",   # lets TTS routing leave the TV out in memory
})


async def _restore_panel_settings(hass: HomeAssistant, entry: NovaConfigEntry,
                                  runtime: NovaRuntime) -> None:
    """Load the panel's saved settings (config.json) into the runtime, so
    choices made in the panel win over entry defaults across restarts.
    Credentials are not panel-writable and are never restored here."""
    from . import nova_config
    from .websocket import PANEL_WRITABLE_KEYS
    await hass.async_add_executor_job(
        nova_config.init_from_entry, dict(entry.data), dict(entry.options))
    saved = await hass.async_add_executor_job(nova_config.get_all)
    if getattr(nova_config, "last_load_error", None):
        # The unusable file was set aside by nova_config: say so loudly
        # rather than quietly running on defaults.
        with suppress(Exception):
            await hass.services.async_call("persistent_notification", "create", {
                "title": "Nova: config.json was invalid",
                "message": (
                    f"{nova_config.last_load_error}. Nova started with defaults. "
                    "Fix the JSON in the preserved file and copy it back to "
                    f"{nova_config._config_path()}, then restart."),
                "notification_id": "nova_config_corrupt",
            }, blocking=False)
    wanted = set(PANEL_WRITABLE_KEYS) | _EXTRA_RESTORED_KEYS
    restored = {key: saved[key] for key in wanted if key in saved}
    if not restored:
        return
    runtime.runtime_config.update(restored)   # in place: the runtime owns it
    _LOGGER.info("Restored %d panel settings from nova_config "
                 "(%d total keys in file)", len(restored), len(saved))


async def _relocate_credentials(hass: HomeAssistant) -> None:
    # Plaintext model credentials move to secrets.yaml. Verify before strip:
    # on any failure config.json is left as it was.
    from . import ha_secrets
    await ha_secrets.relocate_plaintext_credentials(hass)


async def _split_credentials(hass: HomeAssistant) -> None:
    # The legacy shared credential gets a provider-specific slot. Idempotent,
    # and it never guesses or deletes when ownership is unclear.
    from . import ha_secrets
    await ha_secrets.split_shared_credential(hass)


async def _move_legacy_snapshots(hass: HomeAssistant) -> None:
    # Intrusion snapshots under the old public www folder move to the private
    # one. Moved, never deleted; a no-op once none are left.
    from . import intrusion
    await hass.async_add_executor_job(intrusion.migrate_legacy_snapshots)


async def _resume_situations(hass: HomeAssistant) -> None:
    # An intrusion check, a waiting parcel or a freeze warning that was open
    # before a restart carries on. Started before the cognitive core.
    from . import package_monitor, situations
    await situations.async_start(hass)
    package_monitor.restore_from_situations()


def _schedule_bootstrap(hass: HomeAssistant, resources: Any) -> None:
    # The voice stack set-up, once per version in the background (a no-op
    # off Supervisor). The handle cancels a pending start or a running task.
    from . import bootstrap
    resources.add_closeable(bootstrap.schedule_bootstrap(hass))


def _register_voice_recognition(hass: HomeAssistant) -> None:
    # An external speaker-recognition service as the identity resolver's
    # voice tier; idle until enabled with a source entity.
    from . import voice_recognition
    voice_recognition.register(hass)


async def async_setup_entry(hass: HomeAssistant, entry: NovaConfigEntry) -> bool:
    """Set up one Nova config entry and its NovaRuntime."""
    # async_setup has normally done this; an entry set up on its own must
    # still never fall back to the default root.
    from . import paths
    paths.configure(hass)
    _migrate_schema(hass, entry)
    honorific = entry.options.get(
        CONF_HONORIFIC, entry.data.get(CONF_HONORIFIC, DEFAULT_HONORIFIC))

    # The model settings come from one place (saved panel values beat stale
    # entry data), so the boot client, the agent and the tools always agree.
    from . import nova_config
    nova_config.configure(hass)
    effective = await hass.async_add_executor_job(nova_config.effective_config, entry)
    await hass.async_add_executor_job(_prewarm_persisted_state)

    # SQLite stores upgrade before anything reads them, each file in one
    # transaction; a failed file rolls back and only that store degrades.
    try:
        from .persistence import sqlite as stores
        await hass.async_add_executor_job(stores.upgrade_existing, hass.config.path())
    except Exception as exc:
        _LOGGER.warning("Nova: storage upgrade skipped (non-fatal): %s", exc)
    await _attempt("Nova: spoken history hydrate failed (non-fatal): %s",
                   partial(_hydrate_spoken_history, hass))
    await _attempt("Nova: action log configure failed (non-fatal): %s",
                   partial(_configure_action_log, hass))

    provider_name = effective.get("llm_provider", "groq")
    # The provider's own credential; the legacy shared key is used only for
    # the installation's saved primary provider.
    api_key = resolve_provider_credential(effective, provider_name)
    model = effective.get("model", "openai/gpt-oss-120b")
    base_url = resolve_provider_endpoint(effective, provider_name)

    from .automation.attribution import AutomationContextTracker
    from .providers.manager import ProviderManager
    from .providers.routing import ProviderSpec
    from .resources import NovaResources
    from .scheduler import NovaScheduler

    # The teardown registry exists before the first client, so the provider
    # manager owning every client is released on any later exit.
    resources = NovaResources()
    providers = ProviderManager(hass)
    resources.add_async_closeable(providers)
    try:
        llm_client = await providers.async_acquire(
            ProviderSpec(provider=provider_name, model=model, api_key=api_key,
                         base_url=base_url),
            binding="primary",
            factory=lambda: create_provider(provider_name, api_key, model, base_url),
        )
    except Exception as exc:
        _LOGGER.error("Nova: LLM provider init failed: %s", exc)
        await providers.async_close()
        return False
    _LOGGER.info("Nova: LLM provider '%s' initialised (model=%s)", provider_name, model)

    sentinel = NovaSentinel(hass, llm_client, honorific, entry=entry)
    # Nest and Frigate event caches; every listener below joins this list.
    unsubs = register_event_listeners(hass)
    sched = NovaScheduler(hass)
    automation_contexts = AutomationContextTracker()
    resources.add_closeable(automation_contexts)

    cameras = _CameraWatch(hass, entry, llm_client)
    await _attempt("Nova: auto-analyze listener registration failed: %s",
                   partial(cameras.listen_bus, unsubs), level=logging.DEBUG)
    await _attempt("Nova: Eufy listener registration failed: %s",
                   partial(cameras.watch_eufy, unsubs), level=logging.DEBUG)

    if sched.add("package", timedelta(minutes=15),
                 partial(_package_tick, hass, entry, llm_client)):
        _LOGGER.info("Nova: package/mail detection active "
                     "(porch sweep every %s min)", 15)
    await _attempt("Nova: package sensor listener registration failed: %s",
                   partial(_watch_package_sensors, hass, entry, llm_client, unsubs),
                   level=logging.DEBUG)
    if sched.add("health", timedelta(hours=1), partial(_health_tick, hass)):
        _LOGGER.info("Nova: hourly service-health sweep active")
    # Sampled more often than the persistence window, so even the default
    # window sees several samples.
    from .host_health import TICK_INTERVAL_SECONDS
    host_every = timedelta(seconds=TICK_INTERVAL_SECONDS)
    if sched.add("host_health", host_every, partial(_host_health_tick, hass, entry)):
        _LOGGER.info("Nova: host-health sampling active (every %s)", host_every)
    if sched.add("hazard", timedelta(minutes=10), partial(_hazard_tick, hass)):
        _LOGGER.info("Nova: multi-hazard monitor active (sweep every %s min)", 10)
    if sched.add("documents", timedelta(minutes=10), partial(_docs_tick, hass)):
        _LOGGER.info("Nova: document auto-ingest active (scan every %s min)", 10)
    if sched.add("sleep_prompt", timedelta(minutes=10),
                 partial(_sleep_prompt_tick, hass, entry)):
        _LOGGER.info("Nova: nightly sleep-state prompt active "
                     "(checked every %s min)", 10)
    if sched.add("db_purge", timedelta(hours=24), partial(_db_purge_tick, hass)):
        _LOGGER.info("Nova: automatic database purge active (daily, 30-day retention)")
    await _attempt("Nova: briefing scheduler registration failed: %s",
                   partial(_schedule_briefings, hass, entry, llm_client, unsubs),
                   level=logging.DEBUG)

    # DoubleTake (MQTT) face recognition.
    recognition_unsubs = await register_recognition_listener(hass)

    @callback
    def _on_automation_triggered(event: Any) -> None:
        # Every automation fires this, so one cheap listener serves the
        # probation of all Nova-installed ones. Provenance is recorded first,
        # before the automation's own state changes arrive.
        from .automation import trials
        automation_contexts.record_trigger(event)
        hass.async_create_task(trials.async_handle_triggered(hass, event))

    await _attempt(
        "Nova: automation probation listener registration failed: %s",
        lambda: resources.add_unsub(
            hass.bus.async_listen("automation_triggered", _on_automation_triggered)),
        level=logging.DEBUG)
    await _attempt("Nova: adaptive awareness listener registration failed: %s",
                   partial(_listen_adaptive, hass, resources), level=logging.DEBUG)

    # Due reminders are checked every 30 seconds.
    reminder_watcher = ReminderWatcher(
        hass,
        honorific_getter=lambda: _live_honorific(hass),
        tts_getter=lambda: _get_tts(hass, entry, context="reminder"),
        speakers_getter=lambda: _get_speakers(hass, entry),
    )

    # Unload releases every listener, timer and the scheduler in one call.
    resources.add_unsubs(unsubs)
    resources.add_unsubs(recognition_unsubs)
    resources.add_closeable(sched)

    # Built only now that every field exists, so it is never seen half made.
    # It is the one place the entry keeps state.
    runtime = NovaRuntime(
        client=llm_client,
        llm_provider_name=provider_name,
        sentinel=sentinel,
        reminder_watcher=reminder_watcher,
        scheduler=sched,
        resources=resources,
        automation_contexts=automation_contexts,
        providers=providers,
    )
    entry.runtime_data = runtime

    await _attempt("Nova automation inventory unavailable (non-fatal): %s",
                   partial(_start_inventory, hass, runtime, automation_contexts,
                           resources))
    await _attempt("Config restore: %s",
                   partial(_restore_panel_settings, hass, entry, runtime),
                   level=logging.DEBUG)
    await _attempt("Credential relocation failed — credential(s) remain in "
                   "config.json, not moved to secrets.yaml: %s",
                   partial(_relocate_credentials, hass))
    await _attempt("Credential provider-split migration failed — the shared "
                   "credential remains in place, unmigrated: %s",
                   partial(_split_credentials, hass))
    await _attempt("Intrusion snapshot migration: %s",
                   partial(_move_legacy_snapshots, hass), level=logging.DEBUG)

    try:
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await sentinel.async_start()
        await reminder_watcher.async_start()
    except Exception:
        # Home Assistant does not unload an entry whose setup raised, so
        # every listener and sweep above would outlive it. Unload copes with
        # a partial setup (a platform never forwarded unloads as a no-op);
        # clear_runtime then drops the runtime even if that unload failed.
        await async_unload_entry(hass, entry)
        clear_runtime(entry)
        raise

    await _attempt("Nova situations restore failed (non-fatal): %s",
                   partial(_resume_situations, hass))

    # The Observer (off by default) announces interesting state changes. Its
    # switch and settings come from the effective config, so turning it on
    # in the panel survives a restart.
    if bool(effective.get("observer_enabled", False)):
        from . import observer
        try:
            await observer.start(hass, dict(effective), entry=entry)
        except Exception:
            # Same as above. Marked running first, so unload's observer.stop()
            # also clears a partial start.
            set_observer_running(entry, True)
            await async_unload_entry(hass, entry)
            clear_runtime(entry)
            raise
        set_observer_running(entry, True)
        _LOGGER.info("Nova Observer mode ENABLED — watching for interesting events")
    else:
        _LOGGER.info("Nova Observer mode disabled. Enable via addon config → "
                     "observer_enabled=true")

    # Lockdown is safety-critical, so it is wired whether or not the Observer
    # runs. The runtime exists by now, so a missing one is a real fault and
    # the strict snapshot raises into the warning; the executor gets a copy.
    try:
        from . import cognitive_core
        rc = runtime_config_snapshot(entry, strict=True)
        core_config = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry, rc)
        await cognitive_core.ensure_lockdown(hass, core_config)
    except Exception as exc:
        _LOGGER.warning("Nova lockdown wiring failed (non-fatal): %s", exc)
        core_config = None

    # The cognitive core (smoke, CO, gas and leak alarms, freeze warnings,
    # intrusion checks) runs on every install, Observer or not.
    try:
        from . import cognitive_core
        if core_config is None:
            core_config = dict(effective)
        await cognitive_core.start(hass, core_config, entry=entry)
    except Exception as exc:
        _LOGGER.error("Nova cognitive core failed to start: %s", exc)

    # The sidebar panel (idempotent across reloads) and its websocket API.
    await _attempt("Nova panel registration failed (non-fatal): %s",
                   lambda: async_register_panel(hass))
    await _attempt("Nova WS command registration failed (non-fatal): %s",
                   lambda: async_register_ws(hass))
    await _attempt("Nova proactive-audio setup failed (non-fatal): %s",
                   lambda: async_setup_proactive_audio(hass, entry))
    await _attempt("Nova bootstrap scheduling failed (non-fatal): %s",
                   partial(_schedule_bootstrap, hass, resources))
    await _attempt("Nova voice recognition registration failed (non-fatal): %s",
                   partial(_register_voice_recognition, hass))

    _LOGGER.info("Nova online. Good day, %s. Routines available: %s",
                 honorific, ", ".join(list_routines()))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed: reload the entry so every part picks them up."""
    await hass.config_entries.async_reload(entry.entry_id)


def _forget_voice_sessions() -> None:
    """A recording session, what Voice ID learning has sent and the Voice ID
    follow-up memory never outlive their entry."""
    with suppress(Exception):
        from . import voice_enrol
        voice_enrol.clear()
    with suppress(Exception):
        from . import voice_learn
        voice_learn.clear()
    with suppress(Exception):
        from . import identity
        identity.clear_voice_carry()


async def _stop_core() -> None:
    # Started on every install, so its alarm listener must not outlive the
    # entry. Idempotent.
    from . import cognitive_core
    await cognitive_core.stop()


def _release_core() -> None:
    # Drop the stopped core's hass, config and lockdown manager, so a reload
    # builds lockdown from the new instance and config.
    from . import cognitive_core
    cognitive_core.release_runtime()


def _stop_situations() -> None:
    # Saved situations stay on disk for the next start.
    from . import situations
    situations.stop()


def _unregister_voice_recognition() -> None:
    from . import voice_recognition
    voice_recognition.unregister()


async def async_unload_entry(hass: HomeAssistant, entry: NovaConfigEntry) -> bool:
    """Unload Nova: stop what the entry started, then release its runtime.

    Also used to undo a setup that failed part way, so every step tolerates
    a missing runtime. Services stay registered: with no loaded entry they
    fail with a translated error."""
    # No runtime is a valid state here: setup failed before building it, or
    # this is a second unload. The domain-level teardown still runs.
    runtime = lifecycle_runtime(entry)
    observer_was_running = runtime is not None and runtime.observer_running

    await _attempt("Panel unregister note: %s", lambda: async_unregister_panel(hass),
                   level=logging.DEBUG)
    if runtime is not None:
        if runtime.sentinel:
            await runtime.sentinel.async_stop()
        if runtime.reminder_watcher:
            await runtime.reminder_watcher.async_stop()

    if observer_was_running:
        try:
            from . import observer
            await observer.stop()
            set_observer_running(entry, False)
        except Exception as exc:
            _LOGGER.debug("Observer stop failed: %s", exc)
    await _attempt("Cognitive core stop failed: %s", _stop_core, level=logging.DEBUG)
    await _attempt("Cognitive core release failed: %s", _release_core,
                   level=logging.DEBUG)
    await _attempt("Situations stop failed: %s", _stop_situations, level=logging.DEBUG)
    # The voice-fingerprint provider registered at setup.
    await _attempt("Voice recognition unregister failed: %s",
                   _unregister_voice_recognition, level=logging.DEBUG)

    # Listeners, timers and the scheduler, in one fail-safe call.
    if runtime is not None and runtime.resources is not None:
        try:
            summary = await runtime.resources.async_close_all()
            _LOGGER.debug("Nova: resource teardown %s", summary)
        except Exception as exc:
            _LOGGER.debug("Resource teardown note: %s", exc)

    # The entry's proactive-audio objects; their timers went with resources.
    await _attempt("Proactive-audio unload note: %s",
                   lambda: async_unload_proactive_audio(hass, entry),
                   level=logging.DEBUG)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        clear_runtime(entry)
        _forget_voice_sessions()
    return unloaded
