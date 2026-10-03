"""
Nova in-process bootstrap.

Re-homes the old add-on's post-install orchestration into the integration, so a
HACS install gets the same zero-touch voice setup with no separate container.

Runs once per version as a background task from async_setup_entry, only under
Supervisor (installing add-ons needs the Supervisor API). Every phase is
idempotent and best-effort — failures are logged, never fatal.

  1. Ensure Piper / Whisper / openWakeWord add-ons installed + started  (Supervisor REST)
  2. Reload Wyoming config entries                                      (in-process)
  3. Create/update the Nova Assist pipeline                           (in-process, best-effort)

Nova speaks with whichever voice Home Assistant (Piper) provides by default.

The HA-side steps the add-on did over a WebSocket are done in-process here (we
have `hass`), which is both cleaner and more robust than talking to HA's own
WebSocket from inside HA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from . import paths

_LOGGER = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
PIPER_DIR = Path("/share/piper")
MARKER_PATH: Optional[Path] = None  # override; None resolves via paths.py


def _marker_path() -> Path:
    return MARKER_PATH or Path(paths.nova_path(".bootstrap_done"))

REQUIRED_ADDONS = {
    "core_piper":        "Piper TTS",
    "core_whisper":      "Whisper STT",
    "core_openwakeword": "openWakeWord",
}


def supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def is_supervised() -> bool:
    """True only when running under the Supervisor (HA OS / Supervised)."""
    return bool(supervisor_token())


# ── Supervisor REST ──────────────────────────────────────────────────────────

async def _sup(hass: HomeAssistant, method: str, path: str,
               *, timeout: int = 60) -> tuple[int, Optional[dict]]:
    """Call the Supervisor REST API. Returns (status, body|None)."""
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {supervisor_token()}"}
    try:
        async with session.request(
            method, f"{SUPERVISOR}{path}", headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = None
            return resp.status, body
    except Exception as exc:
        _LOGGER.warning("Nova bootstrap: supervisor %s %s failed: %s", method, path, exc)
        return 0, None


async def _addon_info(hass: HomeAssistant, slug: str) -> Optional[dict]:
    status, body = await _sup(hass, "GET", f"/addons/{slug}/info")
    if status == 200 and body and "data" in body:
        return body["data"]
    return None


async def _addon_action(hass: HomeAssistant, slug: str, action: str,
                        *, timeout: int = 600) -> bool:
    if action == "install":
        status, _ = await _sup(hass, "POST", f"/store/addons/{slug}/install", timeout=timeout)
        if status in (200, 202):
            return True
        status, _ = await _sup(hass, "POST", f"/addons/{slug}/install", timeout=timeout)
        return status in (200, 202)
    status, _ = await _sup(hass, "POST", f"/addons/{slug}/{action}", timeout=timeout)
    return status in (200, 202)


async def _wait_addon_state(hass: HomeAssistant, slug: str, target: str,
                            tries: int = 30, delay: float = 2.0) -> bool:
    for _ in range(tries):
        info = await _addon_info(hass, slug) or {}
        if info.get("state") == target:
            return True
        await asyncio.sleep(delay)
    return False


async def _ensure_addon(hass: HomeAssistant, slug: str, friendly: str) -> bool:
    """Install (if needed) + start (if needed). Returns True on success."""
    info = await _addon_info(hass, slug)
    if info is None or not info.get("version"):
        _LOGGER.info("Nova bootstrap: installing %s", friendly)
        if not await _addon_action(hass, slug, "install"):
            return False
        for _ in range(60):  # up to 5 min for the image pull
            await asyncio.sleep(5)
            info = await _addon_info(hass, slug) or {}
            if info.get("version"):
                break
        else:
            _LOGGER.warning("Nova bootstrap: %s install timed out", friendly)
            return False
    if (info or {}).get("state") == "started":
        return True
    _LOGGER.info("Nova bootstrap: starting %s", friendly)
    if not await _addon_action(hass, slug, "start", timeout=60):
        return False
    return await _wait_addon_state(hass, slug, "started")


# ── Legacy Nova voice ────────────────────────────────────────────────────────

LEGACY_VOICE_PREFIX = "en_GB-nova-"


def _legacy_voice_present(voice: str) -> bool:
    """True if a leftover Nova voice file from an older release is still on disk."""
    return (PIPER_DIR / f"{voice}.onnx").exists()


# ── HA-side steps (in-process) ───────────────────────────────────────────────

async def _reload_wyoming(hass: HomeAssistant) -> int:
    """Reload all Wyoming config entries so HA reconnects to the add-on services."""
    n = 0
    for entry in hass.config_entries.async_entries("wyoming"):
        try:
            await hass.config_entries.async_reload(entry.entry_id)
            n += 1
        except Exception as exc:
            _LOGGER.debug("Nova bootstrap: wyoming reload %s failed: %s", entry.entry_id, exc)
    if n:
        _LOGGER.info("Nova bootstrap: reloaded %d Wyoming entr%s", n, "y" if n == 1 else "ies")
    return n


def _find_nova_agent(hass: HomeAssistant) -> Optional[str]:
    """The conversation entity this integration registered (its agent id)."""
    try:
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(hass)
        for ent in reg.entities.values():
            if ent.domain == "conversation" and ent.platform == "nova":
                return ent.entity_id
    except Exception:
        pass
    # fallback: a conversation.* state mentioning nova
    for state in hass.states.async_all("conversation"):
        if "nova" in state.entity_id.lower():
            return state.entity_id
    return None


def _find_engine(hass: HomeAssistant, domain: str, hint: str) -> Optional[str]:
    """Pick an engine entity_id in `domain` matching `hint` (e.g. stt/whisper)."""
    states = hass.states.async_all(domain)
    for st in states:
        if hint in st.entity_id.lower():
            return st.entity_id
    return states[0].entity_id if states else None


async def _wait_for_agent(hass: HomeAssistant, tries: int = 40, delay: float = 3.0) -> Optional[str]:
    for _ in range(tries):
        agent = _find_nova_agent(hass)
        if agent:
            return agent
        await asyncio.sleep(delay)
    return None


def _manual_pipeline_hint() -> None:
    _LOGGER.info(
        "Nova bootstrap: set the pipeline up manually under Settings → Voice "
        "Assistants — Conversation: Nova, STT: faster-whisper, TTS: piper, "
        "Wake word: openWakeWord.")


async def _create_pipeline(hass: HomeAssistant) -> bool:
    """
    Create/update the Nova Assist pipeline and set it preferred. Best-effort:
    the assist_pipeline API varies across HA versions, so any failure logs clear
    manual steps instead of raising. The pipeline uses Home Assistant's default
    voice for the TTS engine.
    """
    try:
        from homeassistant.components import assist_pipeline
    except Exception:
        _manual_pipeline_hint()
        return False

    agent = _find_nova_agent(hass)
    stt = _find_engine(hass, "stt", "whisper")
    tts = _find_engine(hass, "tts", "piper")
    if not agent or not stt or not tts:
        _LOGGER.warning("Nova bootstrap: agent=%s stt=%s tts=%s — can't build pipeline yet",
                        agent, stt, tts)
        _manual_pipeline_hint()
        return False

    try:
        # Don't duplicate if a Nova pipeline already exists.
        existing = None
        try:
            for p in assist_pipeline.async_get_pipelines(hass):
                if "nova" in (getattr(p, "name", "") or "").lower():
                    existing = p
                    break
        except Exception:
            existing = None
        if existing is not None:
            # A Nova pipeline exists — ensure its CONVERSATION AGENT is Nova's
            # own entity. async_create_default_pipeline (and manual setups) leave
            # the agent on HA's default (or an LLM integration), and then Nova's
            # reply routing — delivering the reply to the paired room/Cast speaker
            # — never runs, because the turn is handled by the wrong agent. Repair
            # it in place rather than leaving it as-is.
            #
            # Also clear a leftover Nova voice from an older release when its
            # file is gone, so the pipeline falls back to Home Assistant's
            # default voice instead of failing with VoiceNotFound.
            try:
                updates: dict = {}
                if getattr(existing, "conversation_engine", None) != agent:
                    updates["conversation_engine"] = agent
                cur_voice = getattr(existing, "tts_voice", "") or ""
                if cur_voice.startswith(LEGACY_VOICE_PREFIX):
                    present = await hass.async_add_executor_job(
                        _legacy_voice_present, cur_voice)
                    if not present:
                        updates["tts_voice"] = None
                        _LOGGER.info(
                            "Nova bootstrap: pipeline voice '%s' is gone; "
                            "using the Home Assistant default voice", cur_voice)
                if updates:
                    await assist_pipeline.async_update_pipeline(hass, existing, **updates)
                    _LOGGER.info("Nova bootstrap: updated pipeline '%s' (%s)",
                                 getattr(existing, "name", "?"), ", ".join(updates))
                else:
                    _LOGGER.info("Nova bootstrap: Nova pipeline already correct (agent=%s)", agent)
            except Exception as exc:
                _LOGGER.warning(
                    "Nova bootstrap: couldn't update existing pipeline: %s", exc)
            return True

        pipeline = await assist_pipeline.async_create_default_pipeline(
            hass, stt_engine_id=stt, tts_engine_id=tts, pipeline_name="Nova")
        if pipeline is None:
            _LOGGER.warning("Nova bootstrap: default pipeline creation returned nothing")
            _manual_pipeline_hint()
            return False
        # async_create_default_pipeline uses HA's default conversation agent, which
        # is not guaranteed to be Nova's. Set it explicitly so Nova handles the
        # turn. The voice stays on Home Assistant's default.
        try:
            await assist_pipeline.async_update_pipeline(
                hass, pipeline, conversation_engine=agent)
        except Exception as exc:
            _LOGGER.warning(
                "Nova bootstrap: created pipeline but couldn't set the agent (%s) "
                "— select Nova under Voice assistants", exc)
        _LOGGER.info("Nova bootstrap: created Nova pipeline (agent=%s stt=%s tts=%s)",
                     agent, stt, tts)
        return True
    except Exception as exc:
        _LOGGER.warning("Nova bootstrap: pipeline creation failed (%s)", exc)
        _manual_pipeline_hint()
        return False


# ── Run-once marker ──────────────────────────────────────────────────────────

def _read_marker() -> dict:
    try:
        if _marker_path().exists():
            return json.loads(_marker_path().read_text())
    except Exception:
        pass
    return {}


def _write_marker(version: str, status: dict) -> None:
    try:
        _marker_path().parent.mkdir(parents=True, exist_ok=True)
        _marker_path().write_text(json.dumps({"version": version, **status}))
    except Exception as exc:
        _LOGGER.debug("Nova bootstrap: could not write marker: %s", exc)


def _current_version() -> str:
    try:
        manifest = Path(__file__).parent / "manifest.json"
        return json.loads(manifest.read_text()).get("version", "0")
    except Exception:
        return "0"


# ── Orchestration ────────────────────────────────────────────────────────────

async def async_run_bootstrap(hass: HomeAssistant, *, force: bool = False) -> dict:
    """
    Full bootstrap. Idempotent + best-effort. Returns a status dict. Safe to call
    on every setup — it self-gates on the run-once marker and the Supervisor.
    """
    from . import nova_config

    status = {"supervised": False, "addons_ok": False,
              "wyoming_ok": False, "pipeline_ok": False, "skipped": None}

    if not bool(nova_config.get("auto_bootstrap", True)):
        status["skipped"] = "auto_bootstrap disabled"
        return status

    if not is_supervised():
        status["skipped"] = "no Supervisor — voice auto-setup needs HA OS/Supervised"
        _LOGGER.info("Nova bootstrap: %s; skipping voice-stack setup", status["skipped"])
        return status
    status["supervised"] = True

    version = await hass.async_add_executor_job(_current_version)
    marker = await hass.async_add_executor_job(_read_marker)
    if not force and marker.get("version") == version and marker.get("addons_ok"):
        status["skipped"] = "already bootstrapped this version"
        return status

    auto_pipeline = bool(nova_config.get("auto_pipeline", True))

    _LOGGER.info("Nova bootstrap: starting (version=%s)", version)

    # Phase 1 — prerequisite add-ons
    addons_ok = True
    for slug, friendly in REQUIRED_ADDONS.items():
        if not await _ensure_addon(hass, slug, friendly):
            addons_ok = False
    status["addons_ok"] = addons_ok

    # Phase 2 — reload Wyoming
    try:
        await _reload_wyoming(hass)
        status["wyoming_ok"] = True
    except Exception as exc:
        _LOGGER.debug("Nova bootstrap: wyoming reload error: %s", exc)

    # Phase 3 — Assist pipeline (best-effort)
    if auto_pipeline:
        agent = await _wait_for_agent(hass)
        if agent:
            status["pipeline_ok"] = await _create_pipeline(hass)
        else:
            _LOGGER.warning("Nova bootstrap: conversation agent didn't register in time")
            _manual_pipeline_hint()

    await hass.async_add_executor_job(_write_marker, version, status)
    _LOGGER.info("Nova bootstrap: complete — %s", status)
    return status


async def async_ensure_pipeline_agent(hass: HomeAssistant) -> None:
    """Ensure Nova's voice pipeline(s) run Nova's own conversation entity.

    async_create_default_pipeline (and manual setups) leave the pipeline's
    conversation agent on HA's default / an LLM integration, and then Nova's
    reply routing — delivering the spoken reply to the paired room/Cast speaker —
    never runs, because a different agent handled the turn (and no Nova turn is
    logged). This corrects it: idempotent (no-op when already right), runs on every
    setup (NOT marker-gated, NOT Supervisor-gated), never raises. Matches a Nova
    pipeline by its name OR its Nova Piper voice, so it works no matter what the
    pipeline is called.
    """
    try:
        from homeassistant.components import assist_pipeline
    except Exception:
        return
    agent = await _wait_for_agent(hass, tries=20, delay=3.0)
    if not agent:
        return
    try:
        pipelines = list(assist_pipeline.async_get_pipelines(hass))
    except Exception:
        return
    for p in pipelines:
        name = (getattr(p, "name", "") or "").lower()
        voice = (getattr(p, "tts_voice", "") or "").lower()
        if "nova" not in name and "nova" not in voice:
            continue
        if getattr(p, "conversation_engine", None) == agent:
            continue
        try:
            await assist_pipeline.async_update_pipeline(
                hass, p, conversation_engine=agent)
            _LOGGER.info(
                "Nova: set pipeline '%s' conversation agent -> %s (was %s)",
                getattr(p, "name", "?"), agent,
                getattr(p, "conversation_engine", None))
        except Exception as exc:
            _LOGGER.warning(
                "Nova: couldn't set pipeline '%s' conversation agent: %s",
                getattr(p, "name", "?"), exc)


class BootstrapHandle:
    """Owns the bootstrap's pending start listener and its background task so
    unloading Nova can stop both. shutdown() is idempotent and never raises;
    after it, a start event that still arrives schedules nothing."""

    def __init__(self) -> None:
        self.unsub_start = None   # listen_once remover, until the event fires
        self.task = None          # the running bootstrap task, once started
        self.closed = False

    def shutdown(self) -> None:
        self.closed = True
        unsub, self.unsub_start = self.unsub_start, None
        if unsub is not None:
            try:
                unsub()
            except Exception as exc:
                _LOGGER.debug("Nova bootstrap: start listener removal: %s", exc)
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()


def schedule_bootstrap(hass: HomeAssistant) -> BootstrapHandle:
    """
    Launch the bootstrap as a background task once HA has finished starting.
    The pipeline-agent repair runs on every start (everywhere); the add-on/voice
    bootstrap is Supervisor-only and marker-gated. Called from async_setup_entry,
    which registers the returned handle so unload cancels whatever is pending.
    """
    handle = BootstrapHandle()

    async def _runner(_event=None) -> None:
        # Always, first: point Nova's voice pipeline at Nova's own agent so its
        # reply routing runs. Not marker-gated and not behind the add-on phases, so
        # it can't be raced or skipped the way the once-per-version bootstrap can.
        try:
            await async_ensure_pipeline_agent(hass)
        except Exception as exc:
            _LOGGER.debug("Nova: pipeline-agent repair error: %s", exc)
        if is_supervised():
            try:
                await async_run_bootstrap(hass)
            except Exception as exc:
                _LOGGER.warning("Nova bootstrap: unexpected error: %s", exc)
        # Last, once voice setup has settled: the one time "Nova is ready"
        # notice for a fresh install (self-gated, see welcome.py).
        try:
            from . import welcome
            await welcome.async_maybe_show(hass)
        except Exception as exc:
            _LOGGER.debug("Nova: welcome notification error: %s", exc)

    def _start() -> None:
        if not handle.closed:
            handle.task = hass.async_create_background_task(_runner(), "nova_bootstrap")

    @callback
    def _on_started(_event) -> None:
        handle.unsub_start = None   # listen_once has already removed itself
        _start()

    if hass.is_running:
        _start()
    else:
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
        handle.unsub_start = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started)
    return handle
