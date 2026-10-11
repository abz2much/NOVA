"""Nova Voice ID speech-to-text entity (8.35.0, Phase 1b).

The user picks "Nova Voice ID" as the speech to text in their voice
assistant. Audio goes to the Nova Voice ID app, which passes it to Whisper and
returns Whisper's words unchanged. If the app can't be reached, the audio goes
untouched to the fallback STT entity, so voice keeps working.

    satellite → HA pipeline → stt.nova_voice_id → Nova Voice ID app → Whisper
                                        └─ app unreachable → fallback STT entity

The transcript's extra ``nova_voice_id`` block is checked and kept for this
one request in voice_id.VOICE_RESULT (8.36.0). It is set in
async_process_audio_stream itself: a ContextVar set inside a child task never
reaches the caller. Nova does not use it to decide who is speaking.

The fallback is called through its own async_process_audio_stream after
check_metadata, the same way HA's assist pipeline calls an STT entity
(assist_pipeline/default_pipeline.py, HA 2026.10.1).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import dataclasses
import logging
import time
from typing import Any

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import voice_id

_LOGGER = logging.getLogger(__name__)

INFO_REFRESH_S = 300
INFO_TASK_NAME = "voice_id_info"
DEFAULT_LANGUAGES = ["en"]
HEALTH_KEY = "voice_id"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entity = NovaVoiceIdStt(hass, config_entry)
    async_add_entities([entity])

    # One recurring Info refresh, owned by the entry's scheduler. It looks
    # the entity up each tick, so a disabled-then-enabled entity is still
    # refreshed and a removed one is simply skipped.
    try:
        from .runtime import get_runtime
        runtime = get_runtime(config_entry)

        async def _refresh(_now=None) -> None:
            current = getattr(runtime, "voice_id", None)
            if current is not None:
                await current.async_refresh_info()

        runtime.scheduler.add(INFO_TASK_NAME, INFO_REFRESH_S, _refresh)
    except Exception as exc:  # noqa: BLE001 - never block the platform
        _LOGGER.warning("Nova Voice ID: Info refresh not scheduled: %s", exc)


class NovaVoiceIdStt(stt.SpeechToTextEntity):
    """Speech to text through the Nova Voice ID app, with a fallback."""

    _attr_name = "Nova Voice ID"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-voice-id-stt"
        self._info: voice_id.AppInfo | None = None
        # The app's last known languages. Kept while the app is down, so the
        # list HA's pipeline checks stays stable (see supported_languages).
        self._app_languages: list[str] = []
        self._info_uri = ""
        self._state = "not_set"          # not_set | connected | unreachable
        self._error = ""
        self._checked_at: float | None = None
        self._default_lang_logged = False
        self._refresh_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        runtime = self._runtime()
        if runtime is not None:
            runtime.voice_id = self
        # Fetch Info in the background: setup never waits on the app.
        self.hass.async_create_background_task(
            self.async_refresh_info(), "nova voice id info")

    async def async_will_remove_from_hass(self) -> None:
        runtime = self._runtime()
        if runtime is not None and getattr(runtime, "voice_id", None) is self:
            runtime.voice_id = None
        await super().async_will_remove_from_hass()

    def _runtime(self):
        try:
            from .runtime import get_runtime
            return get_runtime(self._entry)
        except Exception:  # noqa: BLE001
            return None

    # ── settings ─────────────────────────────────────────────────────────
    def _setting(self, key: str) -> str:
        try:
            from . import nova_config
            value = nova_config.runtime_get(self.hass, self._entry, key, "")
        except Exception:  # noqa: BLE001
            value = ""
        return str(value or "").strip()

    def _uri(self) -> str:
        return self._setting("voice_id_uri")

    def _fallback_entity(self) -> stt.SpeechToTextEntity | None:
        entity_id = self._setting("voice_id_fallback_stt")
        if not entity_id or entity_id == self.entity_id:
            # Never Nova's own entity: that would call itself forever.
            return None
        entity = stt.async_get_speech_to_text_entity(self.hass, entity_id)
        if entity is self or isinstance(entity, NovaVoiceIdStt):
            return None
        return entity

    # ── Info ─────────────────────────────────────────────────────────────
    async def async_refresh_info(self) -> None:
        """Ask the app for its Info. Never raises."""
        async with self._refresh_lock:
            uri = self._uri()
            self._checked_at = time.time()
            if uri != self._info_uri:
                self._app_languages = []      # another app: forget the old list
            if not uri:
                self._info, self._info_uri = None, ""
                self._state, self._error = "not_set", ""
                return
            try:
                info = await voice_id.fetch_info(uri)
            except Exception as exc:  # noqa: BLE001 - VoiceIdUnreachable and anything else
                if uri == self._uri():
                    self._info, self._info_uri = None, uri
                    self._state, self._error = "unreachable", str(exc)[:120]
                _LOGGER.debug("Nova Voice ID: Info failed: %s", exc)
                return
            if uri != self._uri():
                return                        # the address changed meanwhile
            self._info, self._info_uri = info, uri
            self._state, self._error = "connected", ""
            if info.languages:
                self._app_languages = list(info.languages)
                self._default_lang_logged = False

    def status(self) -> dict[str, Any]:
        """For the panel's Voice ID card and the health row."""
        uri = self._uri()
        if not uri:
            state = "not_set"
        elif uri != self._info_uri:
            state = "checking"           # address changed, refresh pending
        else:
            state = self._state
        info = self._info if state == "connected" else None
        return {
            "state": state,
            "name": info.name if info else "",
            "version": info.version if info else "",
            "models": list(info.models) if info else [],
            "languages": list(info.languages) if info else [],
            "error": self._error if state == "unreachable" else "",
            "checked_at": self._checked_at,
            "entity_id": self.entity_id,
        }

    # ── stt.SpeechToTextEntity ───────────────────────────────────────────
    def _fallback_languages(self, fallback=None) -> list[str]:
        fallback = fallback or self._fallback_entity()
        if fallback is None:
            return []
        try:
            return list(fallback.supported_languages)
        except Exception:  # noqa: BLE001
            return []

    @property
    def supported_languages(self) -> list[str]:
        """The app's languages and the fallback's, together.

        HA's pipeline checks this list against its fixed language before
        every request, so it must not flip when the app goes up or down
        (with a fallback that uses region codes, "en" and "en-US", the
        request would be refused before Nova runs). Each side then gets the
        pipeline's language in its own code (voice_id.match_language)."""
        langs: list[str] = []
        if self._uri():
            langs.extend(self._app_languages)
        for code in self._fallback_languages():
            if code not in langs:
                langs.append(code)
        if langs:
            return langs
        if not self._default_lang_logged:
            self._default_lang_logged = True
            _LOGGER.info("Nova Voice ID: no languages from the app or a "
                         "fallback; using %s", DEFAULT_LANGUAGES)
        return list(DEFAULT_LANGUAGES)

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Route the request, then keep its voice result for this request.

        VOICE_RESULT is set here, in this method's own task, which is the
        task HA's pipeline goes on to run the conversation agent in (8.36.0).
        _route is awaited, never a child task, so nothing it does is lost.
        The fallback and every error give status "error"."""
        voice: list[voice_id.VoiceResult] = []
        try:
            return await self._route(metadata, stream, voice)
        finally:
            voice_id.set_turn_voice(voice[0] if voice else voice_id.error_result())

    async def _route(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes],
        voice: list,
    ) -> stt.SpeechResult:
        start = time.monotonic()
        uri = self._uri()

        # Connect first, before any audio is read, so a down app leaves the
        # stream untouched for the fallback.
        client = None
        reason = "no app address set"
        if uri:
            try:
                client = await voice_id.connect(uri)
            except Exception as exc:  # noqa: BLE001 - no audio read yet: fall back
                reason = f"app unreachable ({exc})"
                if uri == self._uri():        # not a stale address
                    self._info, self._info_uri = None, uri
                    self._state, self._error = "unreachable", str(exc)[:120]

        if client is None:
            return await self._use_fallback(metadata, stream, start, reason,
                                            app_failed=bool(uri))

        try:
            language = (voice_id.match_language(metadata.language, self._app_languages)
                        or metadata.language)
            result = await voice_id.transcribe(client, language, stream)
        except asyncio.CancelledError:
            raise
        except voice_id.VoiceIdStreamError as exc:
            # Audio has started: the stream is partly used, so never hand it
            # to the fallback.
            return self._error_result(start, f"app failed mid stream ({exc})",
                                      app_failed=True)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Nova Voice ID: unexpected error")
            return self._error_result(start, f"unexpected error ({type(exc).__name__})",
                                      app_failed=True)
        finally:
            await voice_id.close(client)

        voice.append(voice_id.parse_block(result.voice_id))
        status = voice[0].status
        _LOGGER.debug("Nova Voice ID: status %s", status)
        if self._state != "connected":
            # The app is back: fetch its Info again for the card and languages.
            self.hass.async_create_background_task(
                self.async_refresh_info(), "nova voice id info")
        self._log("app", start, status)
        self._record(True)
        return stt.SpeechResult(result.text, stt.SpeechResultState.SUCCESS)

    async def _use_fallback(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes],
        start: float, reason: str, *, app_failed: bool,
    ) -> stt.SpeechResult:
        fallback = self._fallback_entity()
        if fallback is None:
            return self._error_result(start, f"{reason}; no fallback set",
                                      app_failed=app_failed)
        language = voice_id.match_language(
            metadata.language, self._fallback_languages(fallback))
        if language and language != metadata.language:
            metadata = dataclasses.replace(metadata, language=language)
        if not language or not fallback.check_metadata(metadata):
            return self._error_result(
                start, f"{reason}; fallback {fallback.entity_id} can't take "
                       f"{metadata.language}", app_failed=app_failed)
        if app_failed:
            self._record(False, f"{reason}; used fallback")
        try:
            result = await fallback.async_process_audio_stream(metadata, stream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Nova Voice ID: fallback %s failed: %s",
                            fallback.entity_id, exc)
            self._log("error", start, "-", f"fallback {fallback.entity_id} failed")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        route = "fallback" if result.result == stt.SpeechResultState.SUCCESS else "error"
        self._log(route, start, "-", f"{fallback.entity_id}; {reason}")
        return result

    def _error_result(self, start: float, detail: str, *,
                      app_failed: bool) -> stt.SpeechResult:
        _LOGGER.warning("Nova Voice ID: %s", detail)
        self._log("error", start, "-", detail)
        if app_failed:
            self._record(False, detail)
        return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

    # ── logging and health ───────────────────────────────────────────────
    @staticmethod
    def _log(route: str, start: float, status: str, detail: str = "") -> None:
        """One Nova Log line per request. Never the words: CONV logs those."""
        ms = int((time.monotonic() - start) * 1000)
        line = f"route={route} ms={ms} app_status={status}"
        if detail:
            line += f" ({detail})"
        try:
            from .ws_log import nova_log
            nova_log("STT", line)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _record(ok: bool, detail: str = "") -> None:
        try:
            from .diagnostics.service_health import record_usage
            record_usage(HEALTH_KEY, ok, detail)
        except Exception:  # noqa: BLE001
            pass
