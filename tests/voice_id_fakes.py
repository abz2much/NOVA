"""Shared fakes for the Nova Voice ID tests (8.35.0, 8.36.0).

install_stt_stub() puts a small stand-in for homeassistant.components.stt in
sys.modules for tests/unit (the integration tests use the real one).
VoiceApp is a fake Nova Voice ID app: a real Wyoming server on 127.0.0.1
that answers transcripts with a chosen nova_voice_id block and the
token-protected management events (the app's docs/protocol.md).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import enum
import socket
import sys
import types

from wyoming.asr import Transcript
from wyoming.audio import AudioStop
from wyoming.error import Error
from wyoming.event import Event, async_read_event, async_write_event

TEXT = "turn on the kitchen lights"
TOKEN = "s3cret-Token_123"


# ── stand-in for homeassistant.components.stt ─────────────────────────────
def install_stt_stub() -> None:
    if "homeassistant.components.stt" in sys.modules:
        return
    mod = types.ModuleType("homeassistant.components.stt")

    mod.AudioFormats = enum.Enum("AudioFormats", {"WAV": "wav", "OGG": "ogg"})
    mod.AudioCodecs = enum.Enum("AudioCodecs", {"PCM": "pcm", "OPUS": "opus"})
    mod.AudioBitRates = enum.Enum("AudioBitRates", {"BITRATE_16": 16})
    mod.AudioSampleRates = enum.Enum("AudioSampleRates", {"SAMPLERATE_16000": 16000})
    mod.AudioChannels = enum.Enum("AudioChannels", {"CHANNEL_MONO": 1})
    mod.SpeechResultState = enum.Enum("SpeechResultState",
                                      {"SUCCESS": "success", "ERROR": "error"})

    @dataclass
    class SpeechMetadata:
        language: str
        format: object
        codec: object
        bit_rate: object
        sample_rate: object
        channel: object

    @dataclass
    class SpeechResult:
        text: str | None
        result: object

    class SpeechToTextEntity:
        entity_id = None

        async def async_added_to_hass(self):
            pass

        async def async_will_remove_from_hass(self):
            pass

        def check_metadata(self, metadata):
            return (metadata.language in self.supported_languages
                    and metadata.format in self.supported_formats
                    and metadata.codec in self.supported_codecs
                    and metadata.bit_rate in self.supported_bit_rates
                    and metadata.sample_rate in self.supported_sample_rates
                    and metadata.channel in self.supported_channels)

    mod.SpeechMetadata = SpeechMetadata
    mod.SpeechResult = SpeechResult
    mod.SpeechToTextEntity = SpeechToTextEntity
    mod.ENTITIES = {}
    mod.async_get_speech_to_text_entity = lambda hass, eid: mod.ENTITIES.get(eid)
    sys.modules["homeassistant.components.stt"] = mod
    sys.modules["homeassistant.components"].stt = mod

    if "homeassistant.helpers.entity_platform" not in sys.modules:
        ep = types.ModuleType("homeassistant.helpers.entity_platform")
        ep.AddEntitiesCallback = object
        sys.modules["homeassistant.helpers.entity_platform"] = ep




class VoiceApp:
    """A fake app. ``blocks`` are handed out one per transcript, in turn
    (the last repeats). ``errors`` maps a management event to an error code.
    ``commits`` and ``management`` record what Nova sent."""

    def __init__(self, blocks=None, token=TOKEN, errors=None, profiles=None, text=TEXT):
        self.blocks = list(blocks or [{"v": 1, "status": "no_profiles", "model": "m", "ms": 3}])
        self.token = token
        self.errors = dict(errors or {})
        self.profiles = {p["person"]: dict(p) for p in (profiles or [])}
        self.text = text
        self.management: list[Event] = []
        self.commits: list[tuple[str, str]] = []
        self.transcripts = 0
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def uri(self):
        return f"tcp://127.0.0.1:{self.port}"

    def _block(self):
        block = self.blocks[min(self.transcripts, len(self.blocks) - 1)]
        self.transcripts += 1
        return block

    def _manage(self, event):
        data = event.data or {}
        if data.get("token") != self.token:
            return Error(text="Missing or wrong token", code="unauthorised").event()
        if event.type in self.errors:
            return Error(text="nope", code=self.errors[event.type]).event()
        if event.type == "nvid-enrol-commit":
            person = data["person"]
            self.commits.append((data["sample_id"], person))
            prof = self.profiles.setdefault(person, {"person": person, "samples": 0,
                                                     "updated": 1760000000.0})
            prof["samples"] += 1
            return Event(type="nvid-enrol-commit-result",
                         data={"person": person, "samples": prof["samples"]})
        if event.type == "nvid-profile-list":
            return Event(type="nvid-profile-list-result",
                         data={"profiles": list(self.profiles.values()), "model": "m"})
        if event.type == "nvid-profile-delete":
            if data.get("all") is True:
                gone = sorted(self.profiles)
                self.profiles.clear()
                return Event(type="nvid-profile-delete-result", data={"deleted": gone, "all": True})
            found = self.profiles.pop(data.get("person"), None)
            return Event(type="nvid-profile-delete-result",
                         data={"deleted": [data.get("person")] if found else [], "all": False})
        if event.type == "nvid-stats":
            return Event(type="nvid-stats-result", data={"days": 7, "rows": [], "truncated": False})
        return Error(text="Unknown event type", code="bad_request").event()

    async def _handle(self, reader, writer):
        try:
            while (event := await async_read_event(reader)) is not None:
                if event.type.startswith("nvid-"):
                    self.management.append(event)
                    await async_write_event(self._manage(event), writer)
                elif AudioStop.is_type(event.type):
                    tr = Transcript(text=self.text).event()
                    block = self._block()
                    if block is not None:
                        tr.data["nova_voice_id"] = block
                    await async_write_event(tr, writer)
        finally:
            writer.close()


def closed_port_uri() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"tcp://127.0.0.1:{port}"
