"""Wyoming client for the Nova Voice ID app (8.35.0).

The app is a Wyoming speech-to-text server that sits in front of Whisper. It
returns Whisper's words unchanged, plus one extra key on each transcript:
``"nova_voice_id": {"v": 1, "status": ...}``. Phase 1b only reads that key.

No Home Assistant imports here, so the unit tests drive this module against a
real (fake) Wyoming server. stt.py is the HA entity that uses it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.info import Describe, Info

# Connect before any audio is read. If the app is down, the fallback still
# gets the untouched stream, so this must stay short.
CONNECT_TIMEOUT = 2.0
# Per event, after the audio is sent: Whisper's own time to transcribe.
READ_TIMEOUT = 30.0
# Describe/Info at setup and on the 5-minute refresh.
INFO_TIMEOUT = 5.0

# The only audio format HA's pipeline hands an STT entity that accepts it:
# 16 kHz, 16-bit, mono PCM.
RATE = 16000
WIDTH = 2
CHANNELS = 1

RESULT_KEY = "nova_voice_id"


class VoiceIdUnreachable(Exception):
    """The app could not be reached. No audio has been read."""


class VoiceIdStreamError(Exception):
    """The app failed after audio started. The stream is partly used."""


def parse_uri(uri: Any) -> tuple[str, int] | None:
    """``tcp://host:port`` → (host, port). Anything else → None."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    try:
        parsed = urlparse(uri.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "tcp" or not parsed.hostname or port is None:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return parsed.hostname, port


def valid_uri(value: Any) -> bool:
    """What the panel may save: empty (off) or a ``tcp://host:port`` address."""
    return value == "" or parse_uri(value) is not None


@dataclass
class AppInfo:
    """What the app says about itself in reply to Describe."""

    name: str = ""
    version: str = ""
    models: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


def app_info(info: Info) -> AppInfo:
    """The app's name, version, installed models and their languages."""
    if not info.asr:
        return AppInfo()
    program = info.asr[0]
    models: list[str] = []
    languages: list[str] = []
    for model in program.models:
        if not model.installed:
            continue
        models.append(model.name)
        for lang in model.languages:
            if lang not in languages:
                languages.append(lang)
    return AppInfo(
        name=program.name or "",
        version=program.version or "",
        models=models,
        languages=languages,
    )


def _client(uri: str, read_timeout: float) -> AsyncTcpClient:
    target = parse_uri(uri)
    if target is None:
        raise VoiceIdUnreachable(f"not a tcp://host:port address: {uri!r}")
    host, port = target
    return AsyncTcpClient(host, port, connect_timeout=CONNECT_TIMEOUT,
                          read_timeout=read_timeout)


async def connect(uri: str, read_timeout: float = READ_TIMEOUT) -> AsyncTcpClient:
    """Open a connection to the app. Raises VoiceIdUnreachable on any failure."""
    client = _client(uri, read_timeout)
    try:
        await client.connect()
    except (OSError, asyncio.TimeoutError) as exc:
        await close(client)
        raise VoiceIdUnreachable(_describe_exc(exc)) from exc
    return client


async def close(client: AsyncTcpClient | None) -> None:
    """Close a connection. Never raises."""
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001 - closing must never fail the request
        pass


async def fetch_info(uri: str, timeout: float = INFO_TIMEOUT) -> AppInfo:
    """Ask the app to describe itself. Raises VoiceIdUnreachable."""
    client = await connect(uri, read_timeout=timeout)
    try:
        await client.write_event(Describe().event())
        while True:
            event = await client.read_event()
            if event is None:
                raise VoiceIdUnreachable("connection closed before Info")
            if Info.is_type(event.type):
                return app_info(Info.from_event(event))
    except VoiceIdUnreachable:
        raise
    except (OSError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
        raise VoiceIdUnreachable(_describe_exc(exc)) from exc
    finally:
        await close(client)


@dataclass
class Transcription:
    """The app's answer. ``voice_id`` is the extra key, or None."""

    text: str
    voice_id: Any = None


async def transcribe(
    client: AsyncTcpClient, language: str | None, stream: AsyncIterable[bytes],
) -> Transcription:
    """Send the audio as it arrives, then wait for the transcript.

    Runs in the caller's task, so the result (and in Phase 2, anything set
    from ``voice_id``) comes straight back to the caller. Raises
    VoiceIdStreamError for a lost connection, a timeout or an Error event.
    """
    try:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(
            AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        async for chunk in stream:
            await client.write_event(
                AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS,
                           audio=chunk).event())
        await client.write_event(AudioStop().event())

        while True:
            event = await client.read_event()
            if event is None:
                raise VoiceIdStreamError("connection lost before the transcript")
            if Error.is_type(event.type):
                err = Error.from_event(event)
                raise VoiceIdStreamError(
                    f"app error {err.code or ''}: {err.text}".replace("  ", " "))
            if Transcript.is_type(event.type):
                data = event.data or {}
                return Transcription(text=data["text"], voice_id=data.get(RESULT_KEY))
    except VoiceIdStreamError:
        raise
    except (OSError, asyncio.TimeoutError, KeyError, TypeError) as exc:
        raise VoiceIdStreamError(_describe_exc(exc)) from exc


def voice_id_status(voice_id: Any) -> str:
    """The extra key's status, for the log. Unknown shapes read as "none"."""
    if isinstance(voice_id, dict):
        status = voice_id.get("status")
        if isinstance(status, str) and status:
            return status[:32]
    return "none"


def _describe_exc(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
