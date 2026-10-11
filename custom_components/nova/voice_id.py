"""Wyoming client for the Nova Voice ID app (8.35.0, 8.36.0).

The app is a Wyoming speech-to-text server that sits in front of Whisper. It
returns Whisper's words unchanged, plus one extra key on each transcript:
``"nova_voice_id": {"v": 1, "status": ...}`` (the app's docs/protocol.md).

8.36.0 keeps that block, checked, for the one request it came from
(VOICE_RESULT, read once with take_turn_voice), and adds the app's
token-protected management events. Nova never uses the result to decide who
is speaking: identity, the AI prompt and memory don't read it.

No Home Assistant imports here, so the unit tests drive this module against a
real (fake) Wyoming server. stt.py is the HA entity that uses it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import math
import re
import time
from typing import Any
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info

_LOGGER = logging.getLogger(__name__)

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


def match_language(language: str | None, supported: list[str]) -> str | None:
    """The code in ``supported`` that best fits ``language``: the same code,
    else the same base language ("en-US" ↔ "en", "en_GB" ↔ "en-GB"),
    preferring the bare base code. None when nothing fits."""
    if not language or not supported:
        return None
    if language in supported:
        return language

    def norm(code: str) -> str:
        return str(code).replace("_", "-").lower()

    wanted = norm(language)
    for code in supported:
        if norm(code) == wanted:
            return code
    base = wanted.split("-")[0]
    same_base = [c for c in supported if norm(c).split("-")[0] == base]
    for code in same_base:
        if norm(code) == base:
            return code
    return same_base[0] if same_base else None


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
    try:
        client = _client(uri, read_timeout)
    except ValueError as exc:
        raise VoiceIdUnreachable(_describe_exc(exc)) from exc
    try:
        await client.connect()
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        # ValueError covers a host name that can't be encoded (UnicodeError).
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


# ── The voice result, per request (8.36.0) ──────────────────────────────────

# The protocol's statuses, plus "off": what app 0.1.0 sends (no recognition).
STATUSES = frozenset({"known", "not_sure", "unknown", "short", "no_profiles",
                      "error", "timeout", "off"})
PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLOCK_KEYS = frozenset({"v", "status", "person", "score", "second", "second_score",
                         "margin", "speech_s", "sample_id", "model", "ms"})
# A result older than this is never handed to a turn: STT and the
# conversation agent run seconds apart in one pipeline run.
MAX_RESULT_AGE_S = 60.0


@dataclass(frozen=True)
class VoiceResult:
    """One request's checked nova_voice_id block (the app's protocol.md)."""

    status: str
    person: str | None = None
    score: float | None = None
    second: str | None = None
    second_score: float | None = None
    margin: float | None = None
    speech_s: float | None = None
    sample_id: str | None = None
    model: str | None = None
    ms: int | None = None
    # When Nova made it (monotonic). Not part of the protocol.
    at: float = field(default_factory=time.monotonic, compare=False)


def error_result() -> VoiceResult:
    return VoiceResult(status="error")


# Set by NovaVoiceIdStt.async_process_audio_stream in its own task, which is
# the task HA's pipeline then runs the conversation agent in. A child task's
# set never reaches the caller, so it is never set anywhere else.
VOICE_RESULT: ContextVar[VoiceResult | None] = ContextVar("nova_voice_result", default=None)

_invalid_logged = False


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("out of range")
    return value


def _check_block(block: Any) -> VoiceResult:
    if not isinstance(block, dict):
        raise ValueError("not an object")
    extra = set(block) - _BLOCK_KEYS
    if extra:
        raise ValueError("unknown key")
    if block.get("v") != 1 or isinstance(block.get("v"), bool):
        raise ValueError("wrong version")
    status = block.get("status")
    if status not in STATUSES:
        raise ValueError("unknown status")
    out: dict[str, Any] = {"status": status}
    for key in ("person", "second"):
        if block.get(key) is not None:
            if not isinstance(block[key], str) or not PERSON_RE.fullmatch(block[key]):
                raise ValueError("bad person id")
            out[key] = block[key]
    for key in ("score", "second_score"):
        if block.get(key) is not None:
            out[key] = _number(block[key], -1.0, 1.0)
    if block.get("margin") is not None:
        out["margin"] = _number(block["margin"], 0.0, 2.0)
    if block.get("speech_s") is not None:
        out["speech_s"] = _number(block["speech_s"], 0.0, 600.0)
    if block.get("ms") is not None:
        out["ms"] = int(_number(block["ms"], 0, 600_000))
    if block.get("sample_id") is not None:
        if not isinstance(block["sample_id"], str) or not SAMPLE_ID_RE.fullmatch(block["sample_id"]):
            raise ValueError("bad sample id")
        out["sample_id"] = block["sample_id"]
    if block.get("model") is not None:
        if not isinstance(block["model"], str) or len(block["model"]) > 128:
            raise ValueError("bad model")
        out["model"] = block["model"]
    if out.get("person") and status != "known":
        raise ValueError("person without known")
    return VoiceResult(**out)


def parse_block(block: Any) -> VoiceResult:
    """The block as a VoiceResult. Anything invalid, or no block at all,
    becomes status error, logged once at warning (never the block itself)."""
    global _invalid_logged
    try:
        return _check_block(block)
    except (ValueError, TypeError) as exc:
        if not _invalid_logged:
            _invalid_logged = True
            _LOGGER.warning("Nova Voice ID: the app sent a result Nova can't use "
                            "(%s); treating it as an error", exc)
        return error_result()


def set_turn_voice(result: VoiceResult) -> None:
    """Called only by the STT entity, in its own task."""
    VOICE_RESULT.set(result)


def take_turn_voice() -> VoiceResult | None:
    """This request's result, once: read and clear. A stale one is None."""
    result = VOICE_RESULT.get()
    if result is not None:
        VOICE_RESULT.set(None)
        if time.monotonic() - result.at > MAX_RESULT_AGE_S:
            return None
    return result


def log_line(result: VoiceResult | None, device_id: str | None) -> str:
    """The Nova Log VOICE line: numbers and ids only, never words."""
    if result is None:
        return f"status=none device={device_id}"

    def num(v: float | None) -> str:
        return "-" if v is None else f"{v:.2f}"
    return (f"status={result.status} person={result.person or '-'} "
            f"score={num(result.score)} second={result.second or '-'} "
            f"margin={num(result.margin)} speech_s={num(result.speech_s)} "
            f"ms={'-' if result.ms is None else result.ms} device={device_id}")


# ── Management events (8.36.0) ──────────────────────────────────────────────

MANAGE_TIMEOUT = 5.0

ERROR_MESSAGES = {
    "unauthorised": "The app's token doesn't match. Copy it from the app's Configuration tab.",
    "not_found": "The app doesn't have that any more. A recording may have expired; try again.",
    "bad_request": "The app couldn't use that request. Update Nova and the app.",
    "failed": "The app couldn't save or delete its files. Check the app's log.",
    "unreachable": "Nova can't reach the Nova Voice ID app. Check it is running and the address is right.",
    "no_token": "Set the app's token first. Copy it from the app's Configuration tab.",
    "bad_reply": "The app sent a reply Nova doesn't understand. Update the app.",
}


class VoiceIdManagementError(Exception):
    """A management call failed. ``code`` is a key of ERROR_MESSAGES."""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_MESSAGES else "failed"
        self.message = ERROR_MESSAGES[self.code]     # fixed text, for the panel
        super().__init__(self.message)


async def _manage(uri: str, token: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """One management event on its own connection, the reply within 5 s."""
    if not token:
        raise VoiceIdManagementError("no_token")
    try:
        client = await connect(uri, read_timeout=MANAGE_TIMEOUT)
    except VoiceIdUnreachable as exc:
        raise VoiceIdManagementError("unreachable") from exc
    try:
        await client.write_event(Event(type=event_type, data={**data, "token": token}))
        while True:
            event = await client.read_event()
            if event is None:
                raise VoiceIdManagementError("unreachable")
            if Error.is_type(event.type):
                raise VoiceIdManagementError(str((event.data or {}).get("code") or "failed"))
            if event.type == f"{event_type}-result":
                reply = event.data or {}
                if not isinstance(reply, dict):
                    raise VoiceIdManagementError("bad_reply")
                return reply
    except VoiceIdManagementError:
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        raise VoiceIdManagementError("unreachable") from exc
    finally:
        await close(client)


def _check_person(person: str) -> None:
    if not isinstance(person, str) or not PERSON_RE.fullmatch(person):
        raise VoiceIdManagementError("bad_request")


async def enrol_commit(uri: str, token: str, sample_id: str, person: str) -> int:
    """Save a pending sample to a person. Returns their sample count."""
    _check_person(person)
    if not isinstance(sample_id, str) or not SAMPLE_ID_RE.fullmatch(sample_id):
        raise VoiceIdManagementError("bad_request")
    reply = await _manage(uri, token, "nvid-enrol-commit",
                          {"sample_id": sample_id, "person": person})
    samples = reply.get("samples")
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise VoiceIdManagementError("bad_reply")
    return samples


async def profile_delete(uri: str, token: str, person: str | None = None, *,
                         everyone: bool = False) -> dict[str, Any]:
    """Delete one person's voice, or everyone's. Returns the app's reply."""
    if everyone:
        data: dict[str, Any] = {"all": True}
    else:
        _check_person(person or "")
        data = {"person": person}
    reply = await _manage(uri, token, "nvid-profile-delete", data)
    deleted = reply.get("deleted")
    if not isinstance(deleted, list) or not all(isinstance(p, str) for p in deleted):
        raise VoiceIdManagementError("bad_reply")
    return {"deleted": deleted, "all": reply.get("all") is True}


async def profile_list(uri: str, token: str) -> dict[str, Any]:
    """{"profiles": [{"person", "samples", "updated"}], "model"}."""
    reply = await _manage(uri, token, "nvid-profile-list", {})
    raw = reply.get("profiles")
    if not isinstance(raw, list):
        raise VoiceIdManagementError("bad_reply")
    profiles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        person, samples, updated = item.get("person"), item.get("samples"), item.get("updated")
        if (not isinstance(person, str) or not PERSON_RE.fullmatch(person)
                or isinstance(samples, bool) or not isinstance(samples, int)):
            continue
        if isinstance(updated, bool) or not isinstance(updated, (int, float)):
            updated = None
        profiles.append({"person": person, "samples": samples, "updated": updated})
    model = reply.get("model")
    return {"profiles": profiles, "model": model if isinstance(model, str) else ""}


async def stats(uri: str, token: str, days: float = 7) -> dict[str, Any]:
    """The app's score log rows (nvid-stats)."""
    reply = await _manage(uri, token, "nvid-stats", {"days": days})
    if not isinstance(reply.get("rows"), list):
        raise VoiceIdManagementError("bad_reply")
    return reply
