"""Nova's voice setup, run inside Home Assistant after install.

Under the Supervisor (HA OS or Supervised) this brings up the local voice
stack with no separate container: the Piper, Whisper and openWakeWord add-ons
are installed and started, the Wyoming entries are reloaded so Home Assistant
reconnects to them, and an Assist pipeline named "Nova" is created or
repaired. That part runs once per Nova version, guarded by a marker file, and
every step is best effort: a failure is logged and reported through a Repair
notice, never raised.

Wherever Nova runs, each start also points any Nova pipeline at Nova's own
conversation entity, since a pipeline answered by another agent never reaches
Nova's reply routing. The pipeline keeps Home Assistant's default voice.
"""
from __future__ import annotations

import asyncio
import json
import logging
from os import environ
from pathlib import Path
from typing import Any, Optional

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import paths

_LOGGER = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
PIPER_DIR = Path("/share/piper")
# Tests point this elsewhere; left as None it resolves under Nova's folder.
MARKER_PATH: Optional[Path] = None

# Slug -> the name used in log lines, in the order they are brought up.
REQUIRED_ADDONS = {
    "core_piper": "Piper TTS",
    "core_whisper": "Whisper STT",
    "core_openwakeword": "openWakeWord",
}

# Older releases shipped their own Piper voice under this name.
LEGACY_VOICE_PREFIX = "en_GB-nova-"

_OK_STATUSES = (200, 202)
_PREFIX = "Nova bootstrap:"


def _marker_file() -> Path:
    if MARKER_PATH:
        return MARKER_PATH
    return Path(paths.nova_path(".bootstrap_done"))


def supervisor_token() -> str:
    return environ.get("SUPERVISOR_TOKEN", "")


def is_supervised() -> bool:
    """Whether Home Assistant runs under the Supervisor, which is the only
    place add-ons can be installed from."""
    return supervisor_token() != ""


# ── Supervisor add-ons ──────────────────────────────────────────────────────

async def _supervisor_call(hass: HomeAssistant, method: str, path: str,
                           *, timeout: int = 60) -> tuple[int, Any]:
    """One Supervisor REST request: (HTTP status, JSON body or None).
    A request that fails outright reads as status 0."""
    client = async_get_clientsession(hass)
    auth = {"Authorization": "Bearer " + supervisor_token()}
    try:
        async with client.request(method, SUPERVISOR + path, headers=auth,
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as reply:
            try:
                payload = await reply.json()
            except Exception:
                payload = None
            return reply.status, payload
    except Exception as exc:
        _LOGGER.warning("%s supervisor %s %s failed: %s", _PREFIX, method, path, exc)
        return 0, None


async def _fetch_addon(hass: HomeAssistant, slug: str) -> Optional[dict]:
    code, payload = await _supervisor_call(hass, "GET", f"/addons/{slug}/info")
    if code != 200 or not payload or "data" not in payload:
        return None
    return payload["data"]


async def _post_addon(hass: HomeAssistant, slug: str, verb: str,
                      *, timeout: int = 600) -> bool:
    """Ask the Supervisor to act on an add-on. An install goes through the
    store first, then the plain add-on endpoint if the store refuses."""
    if verb == "install":
        targets = [f"/store/addons/{slug}/install", f"/addons/{slug}/install"]
    else:
        targets = [f"/addons/{slug}/{verb}"]
    for target in targets:
        code, _ = await _supervisor_call(hass, "POST", target, timeout=timeout)
        if code in _OK_STATUSES:
            return True
    return False


async def _install_and_wait(hass: HomeAssistant, slug: str, friendly: str) -> Optional[dict]:
    """Install an add-on and give its image up to five minutes to land.
    Returns its info once it reports a version, or None."""
    _LOGGER.info("%s installing %s", _PREFIX, friendly)
    if not await _post_addon(hass, slug, "install"):
        return None
    for _ in range(60):
        await asyncio.sleep(5)
        details = await _fetch_addon(hass, slug) or {}
        if details.get("version"):
            return details
    _LOGGER.warning("%s %s install timed out", _PREFIX, friendly)
    return None


async def _reaches_state(hass: HomeAssistant, slug: str, wanted: str,
                         attempts: int = 30, pause: float = 2.0) -> bool:
    for _ in range(attempts):
        details = await _fetch_addon(hass, slug) or {}
        if details.get("state") == wanted:
            return True
        await asyncio.sleep(pause)
    return False


async def _ensure_addon(hass: HomeAssistant, slug: str, friendly: str) -> bool:
    """Make sure one add-on is installed and running."""
    details = await _fetch_addon(hass, slug)
    if not (details or {}).get("version"):
        details = await _install_and_wait(hass, slug, friendly)
        if details is None:
            return False
    if details is not None and details.get("state") == "started":
        return True
    _LOGGER.info("%s starting %s", _PREFIX, friendly)
    started = await _post_addon(hass, slug, "start", timeout=60)
    return started and await _reaches_state(hass, slug, "started")


def _legacy_voice_present(voice: str) -> bool:
    """Whether an old release's Nova voice model is still on disk."""
    return PIPER_DIR.joinpath(voice + ".onnx").exists()


# ── Home Assistant side ─────────────────────────────────────────────────────

async def _reload_wyoming(hass: HomeAssistant) -> int:
    """Reload every Wyoming entry so Home Assistant reconnects to the voice
    add-ons. An entry that fails to reload is skipped, and once the rest are
    done any failure is raised so the run reports Wyoming. Returns how many
    reloaded."""
    entries = hass.config_entries
    done = 0
    failed = 0
    for entry in entries.async_entries("wyoming"):
        try:
            await entries.async_reload(entry.entry_id)
        except Exception as exc:
            _LOGGER.debug("%s wyoming reload %s failed: %s", _PREFIX, entry.entry_id, exc)
            failed += 1
            continue
        done += 1
    if done > 0:
        noun = "entry" if done == 1 else "entries"
        _LOGGER.info("%s reloaded %d Wyoming %s", _PREFIX, done, noun)
    if failed:
        raise RuntimeError(f"{failed} Wyoming reload(s) failed")
    return done


def _registered_agent(hass: HomeAssistant) -> Optional[str]:
    try:
        from homeassistant.helpers import entity_registry as er
        for item in er.async_get(hass).entities.values():
            if item.platform == "nova" and item.domain == "conversation":
                return item.entity_id
    except Exception:
        return None
    return None


def _find_nova_agent(hass: HomeAssistant) -> Optional[str]:
    """Nova's conversation entity id: the registry entry first, else any
    conversation entity whose id mentions nova."""
    found = _registered_agent(hass)
    if found:
        return found
    named = [s.entity_id for s in hass.states.async_all("conversation")
             if "nova" in s.entity_id.lower()]
    return named[0] if named else None


def _find_engine(hass: HomeAssistant, domain: str, hint: str) -> Optional[str]:
    """An engine in `domain`, preferring one whose id contains `hint`, else
    the first one there is."""
    candidates = [s.entity_id for s in hass.states.async_all(domain)]
    preferred = [eid for eid in candidates if hint in eid.lower()]
    if preferred:
        return preferred[0]
    return candidates[0] if candidates else None


async def _wait_for_agent(hass: HomeAssistant, tries: int = 40,
                          delay: float = 3.0) -> Optional[str]:
    """Give Nova's conversation entity time to register."""
    for _ in range(tries):
        found = _find_nova_agent(hass)
        if found:
            return found
        await asyncio.sleep(delay)
    return None


def _log_manual_steps() -> None:
    _LOGGER.info(
        "%s set the pipeline up manually under Settings → Voice Assistants — "
        "Conversation: Nova, STT: faster-whisper, TTS: piper, Wake word: "
        "openWakeWord.", _PREFIX)


def _assist_component() -> Any:
    """Home Assistant's assist_pipeline component, or None when it cannot be
    imported."""
    try:
        from homeassistant.components import assist_pipeline
    except Exception:
        return None
    return assist_pipeline


def _name_of(pipeline: Any) -> str:
    return getattr(pipeline, "name", "?")


def _all_pipelines(component: Any, hass: HomeAssistant) -> list:
    try:
        return list(component.async_get_pipelines(hass))
    except Exception:
        return []


def _named_nova_pipeline(component: Any, hass: HomeAssistant) -> Any:
    """The first pipeline with "nova" in its name, or None."""
    try:
        for candidate in component.async_get_pipelines(hass):
            title = getattr(candidate, "name", "") or ""
            if "nova" in title.lower():
                return candidate
    except Exception:
        return None
    return None


async def _repair_existing(hass: HomeAssistant, component: Any, pipeline: Any,
                           agent: str) -> None:
    """Bring an existing Nova pipeline into line: Nova's own agent (another
    agent would skip Nova's reply routing), and no leftover Nova voice whose
    model file has gone (it would fail with VoiceNotFound). Never raises."""
    try:
        changes: dict[str, Any] = {}
        if getattr(pipeline, "conversation_engine", None) != agent:
            changes["conversation_engine"] = agent
        voice = getattr(pipeline, "tts_voice", "") or ""
        if voice.startswith(LEGACY_VOICE_PREFIX):
            on_disk = await hass.async_add_executor_job(_legacy_voice_present, voice)
            if not on_disk:
                changes["tts_voice"] = None
                _LOGGER.info("%s pipeline voice '%s' is gone; using the Home "
                             "Assistant default voice", _PREFIX, voice)
        if not changes:
            _LOGGER.info("%s Nova pipeline already correct (agent=%s)", _PREFIX, agent)
            return
        await component.async_update_pipeline(hass, pipeline, **changes)
        _LOGGER.info("%s updated pipeline '%s' (%s)", _PREFIX, _name_of(pipeline),
                     ", ".join(changes))
    except Exception as exc:
        _LOGGER.warning("%s couldn't update existing pipeline: %s", _PREFIX, exc)


async def _build_new(hass: HomeAssistant, component: Any, agent: str,
                     stt: str, tts: str) -> bool:
    made = await component.async_create_default_pipeline(
        hass, stt_engine_id=stt, tts_engine_id=tts, pipeline_name="Nova")
    if made is None:
        _LOGGER.warning("%s default pipeline creation returned nothing", _PREFIX)
        _log_manual_steps()
        return False
    # The default pipeline takes Home Assistant's default agent, which may
    # not be Nova, so the agent is set straight afterwards.
    try:
        await component.async_update_pipeline(hass, made, conversation_engine=agent)
    except Exception as exc:
        _LOGGER.warning("%s created pipeline but couldn't set the agent (%s) — "
                        "select Nova under Voice assistants", _PREFIX, exc)
    _LOGGER.info("%s created Nova pipeline (agent=%s stt=%s tts=%s)",
                 _PREFIX, agent, stt, tts)
    return True


async def _create_pipeline(hass: HomeAssistant) -> bool:
    """Create the Nova Assist pipeline, or repair the one already there.
    The assist_pipeline API shifts between Home Assistant versions, so any
    failure logs the manual steps and returns False rather than raising."""
    assist_pipeline = _assist_component()
    if assist_pipeline is None:
        _log_manual_steps()
        return False

    agent = _find_nova_agent(hass)
    stt = _find_engine(hass, "stt", "whisper")
    tts = _find_engine(hass, "tts", "piper")
    if not (agent and stt and tts):
        _LOGGER.warning("%s agent=%s stt=%s tts=%s — can't build pipeline yet",
                        _PREFIX, agent, stt, tts)
        _log_manual_steps()
        return False

    try:
        current = _named_nova_pipeline(assist_pipeline, hass)
        if current is not None:
            await _repair_existing(hass, assist_pipeline, current, agent)
            return True
        return await _build_new(hass, assist_pipeline, agent, stt, tts)
    except Exception as exc:
        _LOGGER.warning("%s pipeline creation failed (%s)", _PREFIX, exc)
        _log_manual_steps()
        return False


# ── Once-per-version marker ─────────────────────────────────────────────────

def _read_marker() -> dict:
    """The last run's status, or {} when there is none or it is unreadable."""
    try:
        target = _marker_file()
        if not target.exists():
            return {}
        found = json.loads(target.read_text())
    except Exception:
        return {}
    return found if isinstance(found, dict) else {}


def _write_marker(version: str, status: dict) -> None:
    record = {"version": version, **status}
    try:
        target = _marker_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record))
    except Exception as exc:
        _LOGGER.debug("%s could not write marker: %s", _PREFIX, exc)


def _current_version() -> str:
    """This package's version from its manifest, or "0"."""
    try:
        text = Path(__file__).with_name("manifest.json").read_text()
        return json.loads(text).get("version", "0")
    except Exception:
        return "0"


# ── The run ─────────────────────────────────────────────────────────────────

def _broken_parts(status: dict) -> list[str]:
    """What a run left unfinished, as the Repair notice names it."""
    parts = []
    if not status.get("addons_ok"):
        parts.append("Piper, Whisper, openWakeWord")
    if not status.get("wyoming_ok"):
        parts.append("Wyoming")
    if status.get("pipeline_tried") and not status.get("pipeline_ok"):
        parts.append("Assist pipeline")
    return parts


def _refresh_skip_notice(hass: HomeAssistant, marker: dict) -> None:
    """A start that skips the run still has to restore or clear the
    incomplete notice, since Home Assistant forgets non-persistent Repair
    issues on restart. Failed add-ons never get here (they rerun the
    bootstrap), so only the pipeline is checked, live, in case someone fixed
    it by hand. Never raises."""
    try:
        from . import repair_notices
        pipeline_failed = marker.get("pipeline_tried") and not marker.get("pipeline_ok")
        if pipeline_failed:
            from . import setup_health
            verdict = setup_health._check_assist_pipeline(hass).get("status")
            if verdict != "ok":
                repair_notices.note_voice_setup_incomplete(hass, ["Assist pipeline"])
                return
        repair_notices.clear_voice_setup(hass)
    except Exception as exc:
        _LOGGER.debug("%s voice notice recheck failed: %s", _PREFIX, exc)


async def async_run_bootstrap(hass: HomeAssistant, *, force: bool = False) -> dict:
    """Bring up the voice stack. Safe to call on every setup: it does nothing
    unless switched on, under the Supervisor, and not yet done for this
    version (force ignores the last condition). Returns the run's status."""
    from . import nova_config, repair_notices

    status: dict[str, Any] = {
        "supervised": False, "addons_ok": False, "wyoming_ok": False,
        "pipeline_ok": False, "skipped": None,
    }
    # A fresh install leaves this off (8.41.0) until the voice add-ons are
    # switched on in Settings.
    if not nova_config.get("auto_bootstrap", False):
        status["skipped"] = "auto_bootstrap disabled"
        return status
    if not is_supervised():
        status["skipped"] = "no Supervisor — voice auto-setup needs HA OS/Supervised"
        _LOGGER.info("%s %s; skipping voice-stack setup", _PREFIX, status["skipped"])
        return status
    status["supervised"] = True

    version = await hass.async_add_executor_job(_current_version)
    marker = await hass.async_add_executor_job(_read_marker)
    if not force and marker.get("version") == version and marker.get("addons_ok"):
        status["skipped"] = "already bootstrapped this version"
        _refresh_skip_notice(hass, marker)
        return status

    with_pipeline = bool(nova_config.get("auto_pipeline", True))
    status["pipeline_tried"] = with_pipeline
    steps = 3 if with_pipeline else 2
    # Progress is shown on the very first setup only, so later version
    # updates run quietly.
    first_setup = not marker

    def _step(name: str, index: int) -> None:
        if first_setup:
            repair_notices.note_voice_setup_step(hass, name, index, steps)

    _LOGGER.info("%s starting (version=%s)", _PREFIX, version)

    _step("addons", 1)
    results = [await _ensure_addon(hass, slug, friendly)
               for slug, friendly in REQUIRED_ADDONS.items()]
    status["addons_ok"] = all(results)

    _step("wyoming", 2)
    try:
        await _reload_wyoming(hass)
    except Exception as exc:
        _LOGGER.debug("%s wyoming reload error: %s", _PREFIX, exc)
    else:
        status["wyoming_ok"] = True

    if with_pipeline:
        _step("pipeline", 3)
        if await _wait_for_agent(hass):
            status["pipeline_ok"] = await _create_pipeline(hass)
        else:
            _LOGGER.warning("%s conversation agent didn't register in time", _PREFIX)
            _log_manual_steps()

    await hass.async_add_executor_job(_write_marker, version, status)
    broken = _broken_parts(status)
    if broken:
        repair_notices.note_voice_setup_incomplete(hass, broken)
    else:
        repair_notices.clear_voice_setup(hass)
    _LOGGER.info("%s complete — %s", _PREFIX, status)
    return status


async def async_ensure_pipeline_agent(hass: HomeAssistant) -> None:
    """Point every Nova pipeline at Nova's own conversation entity.

    A default or hand-made pipeline keeps Home Assistant's default agent (or
    an LLM integration's), and then a different agent answers the turn: Nova
    logs nothing and its reply never reaches the paired room or Cast speaker.
    A pipeline counts as Nova's when its name or its voice mentions nova.
    Runs on every start, outside the Supervisor and marker gates; does nothing
    when all is already right, and never raises.
    """
    assist_pipeline = _assist_component()
    agent = None
    if assist_pipeline is not None:
        agent = await _wait_for_agent(hass, tries=20, delay=3.0)
    if not agent:
        return
    for pipe in _all_pipelines(assist_pipeline, hass):
        labels = (getattr(pipe, "name", "") or "", getattr(pipe, "tts_voice", "") or "")
        if not any("nova" in label.lower() for label in labels):
            continue
        if getattr(pipe, "conversation_engine", None) == agent:
            continue
        title = _name_of(pipe)
        was = getattr(pipe, "conversation_engine", None)
        try:
            await assist_pipeline.async_update_pipeline(hass, pipe, conversation_engine=agent)
            _LOGGER.info("Nova: set pipeline '%s' conversation agent -> %s (was %s)",
                         title, agent, was)
        except Exception as exc:
            _LOGGER.warning("Nova: couldn't set pipeline '%s' conversation agent: %s",
                            title, exc)


# ── Scheduling ──────────────────────────────────────────────────────────────

class BootstrapHandle:
    """Holds the start listener and the background task, so unloading Nova
    can stop whichever is pending. shutdown() may be called any number of
    times and never raises; once it has run, a late start event schedules
    nothing."""

    def __init__(self) -> None:
        self.unsub_start: Any = None   # remover for the start listener, until it fires
        self.task: Any = None          # the background task, once it exists
        self.closed = False

    def shutdown(self) -> None:
        self.closed = True
        remover = self.unsub_start
        self.unsub_start = None
        if remover is not None:
            try:
                remover()
            except Exception as exc:
                _LOGGER.debug("%s start listener removal: %s", _PREFIX, exc)
        running = self.task
        self.task = None
        if running is not None and not running.done():
            running.cancel()


def schedule_bootstrap(hass: HomeAssistant) -> BootstrapHandle:
    """Start the voice setup in the background once Home Assistant is up.
    async_setup_entry registers the returned handle so an unload stops it."""
    handle = BootstrapHandle()

    async def _background() -> None:
        # The agent repair comes first and is never gated, so the once-per-
        # version run below cannot race it or skip it.
        try:
            await async_ensure_pipeline_agent(hass)
        except Exception as exc:
            _LOGGER.debug("Nova: pipeline-agent repair error: %s", exc)
        if is_supervised():
            try:
                await async_run_bootstrap(hass)
            except Exception as exc:
                _LOGGER.warning("%s unexpected error: %s", _PREFIX, exc)
        # Last, with voice settled: the one-off "Nova is ready" notice on a
        # fresh install (welcome.py decides whether it is due).
        try:
            from . import welcome
            await welcome.async_maybe_show(hass)
        except Exception as exc:
            _LOGGER.debug("Nova: welcome notification error: %s", exc)

    def _launch() -> None:
        if handle.closed:
            return
        handle.task = hass.async_create_background_task(_background(), "nova_bootstrap")

    @callback
    def _ha_started(_event: Any) -> None:
        # listen_once has already dropped the listener by now.
        handle.unsub_start = None
        _launch()

    if not hass.is_running:
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
        handle.unsub_start = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _ha_started)
    else:
        _launch()
    return handle
