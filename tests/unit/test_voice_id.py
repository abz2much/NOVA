"""Nova Voice ID speech to text (8.35.0, Phase 1b).

A fake Wyoming server stands in for the Nova Voice ID app. The entity is
driven through a small stand-in for homeassistant.components.stt, so these
run without Home Assistant. tests/integration/test_voice_id_stt.py covers the
real HA side (entity registration, real fallback entity, discovery).
"""
from __future__ import annotations

import asyncio
import enum
import socket
import sys
import time
import types
from dataclasses import dataclass

import pytest

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import async_read_event, async_write_event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info

TEXT = "turn on the kitchen lights"
CHUNKS = [b"\x01\x00" * 160, b"\x02\x00" * 160, b"\x03\x00" * 160]


# ── stand-in for homeassistant.components.stt ─────────────────────────────
def _install_stt_stub() -> None:
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


_install_stt_stub()
STT = sys.modules["homeassistant.components.stt"]


def _meta(language="en"):
    return STT.SpeechMetadata(
        language=language, format=STT.AudioFormats.WAV, codec=STT.AudioCodecs.PCM,
        bit_rate=STT.AudioBitRates.BITRATE_16,
        sample_rate=STT.AudioSampleRates.SAMPLERATE_16000,
        channel=STT.AudioChannels.CHANNEL_MONO)


class Stream:
    """An audio stream that records how much of it was read."""

    def __init__(self, chunks=CHUNKS):
        self.chunks = list(chunks)
        self.read = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            self.read += 1
            yield c


class FakeFallback(STT.SpeechToTextEntity):
    """A fallback STT entity that records the stream it was handed."""

    def __init__(self, entity_id="stt.whisper", languages=("en", "fr")):
        self.entity_id = entity_id
        self._langs = list(languages)
        self.calls = []

    supported_formats = [STT.AudioFormats.WAV]
    supported_codecs = [STT.AudioCodecs.PCM]
    supported_bit_rates = [STT.AudioBitRates.BITRATE_16]
    supported_sample_rates = [STT.AudioSampleRates.SAMPLERATE_16000]
    supported_channels = [STT.AudioChannels.CHANNEL_MONO]

    @property
    def supported_languages(self):
        return self._langs

    async def async_process_audio_stream(self, metadata, stream):
        already_read = getattr(stream, "read", None)
        got = [c async for c in stream]
        self.calls.append({"already_read": already_read, "chunks": got})
        return STT.SpeechResult("fallback words", STT.SpeechResultState.SUCCESS)


# ── fake Nova Voice ID app ────────────────────────────────────────────────
class FakeApp:
    """A Wyoming server. mode: ok | drop | error | no_models."""

    def __init__(self, mode="ok", version="0.1.0"):
        self.mode = mode
        self.version = version
        self.received: list = []
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

    def _info(self):
        models = [] if self.mode == "no_models" else [AsrModel(
            name="tiny-int8", description="", attribution=Attribution(name="x", url="y"),
            installed=True, version="1", languages=["en", "de"])]
        return Info(asr=[AsrProgram(
            name="Nova Voice ID", description="proxy",
            attribution=Attribution(name="abz2much", url="u"),
            installed=True, version=self.version, models=models)])

    async def _handle(self, reader, writer):
        try:
            while True:
                event = await async_read_event(reader)
                if event is None:
                    break
                self.received.append(event)
                if Describe.is_type(event.type):
                    await async_write_event(self._info().event(), writer)
                elif AudioChunk.is_type(event.type) and self.mode == "drop":
                    writer.close()
                    return
                elif AudioStop.is_type(event.type):
                    if self.mode == "error":
                        await async_write_event(
                            Error(text="Upstream unavailable", code="upstream").event(), writer)
                    else:
                        tr = Transcript(text=TEXT).event()
                        tr.data["nova_voice_id"] = {"v": 1, "status": "off"}
                        await async_write_event(tr, writer)
        finally:
            writer.close()


def _closed_port_uri() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"tcp://127.0.0.1:{port}"


# ── fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture
def voice_id(load):
    return load("voice_id")


@pytest.fixture
def stt_mod(load):
    return load("stt")


@pytest.fixture
def log_lines(load, monkeypatch):
    ws_log = load("ws_log")
    lines = []
    monkeypatch.setattr(ws_log, "nova_log", lambda cat, msg: lines.append((cat, msg)))
    return lines


@pytest.fixture
def health(load):
    sh = load("diagnostics.service_health")
    sh._USAGE.clear()
    yield sh
    sh._USAGE.clear()


class _Hass:
    def __init__(self):
        self.tasks = []

    def async_create_background_task(self, coro, name):
        self.tasks.append(asyncio.ensure_future(coro))


def _entity(stt_mod, settings):
    entry = types.SimpleNamespace(entry_id="E1")
    ent = stt_mod.NovaVoiceIdStt(_Hass(), entry)
    ent.entity_id = "stt.nova_voice_id"
    ent._setting = lambda key: settings.get(key, "")
    return ent


@pytest.fixture(autouse=True)
def _clear_entities():
    STT.ENTITIES.clear()
    yield
    STT.ENTITIES.clear()


# ── voice_id.py ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("uri,ok", [
    ("", True), ("tcp://abc-nova-voice-id:10350", True), ("tcp://10.0.0.5:10350", True),
    ("tcp://host", False), ("http://host:10350", False), ("host:10350", False),
    ("tcp://host:10350/x", False), ("tcp://host:99999", False), (None, False),
])
def test_valid_uri(voice_id, uri, ok):
    assert voice_id.valid_uri(uri) is ok


def test_connect_timeout_is_two_seconds(voice_id):
    assert voice_id.CONNECT_TIMEOUT == 2.0


async def test_fetch_info_reads_version_models_and_languages(voice_id):
    async with FakeApp(version="0.1.0") as app:
        info = await voice_id.fetch_info(app.uri)
    assert (info.name, info.version, info.models, info.languages) == (
        "Nova Voice ID", "0.1.0", ["tiny-int8"], ["en", "de"])


async def test_fetch_info_unreachable_raises(voice_id):
    with pytest.raises(voice_id.VoiceIdUnreachable):
        await voice_id.fetch_info(_closed_port_uri())


async def test_transcribe_sends_the_wyoming_sequence(voice_id):
    async with FakeApp() as app:
        client = await voice_id.connect(app.uri)
        try:
            result = await voice_id.transcribe(client, "en", Stream())
        finally:
            await voice_id.close(client)
        await asyncio.sleep(0.05)
    types_seen = [e.type for e in app.received]
    assert types_seen == ["transcribe", "audio-start", "audio-chunk", "audio-chunk",
                          "audio-chunk", "audio-stop"]
    assert Transcribe.from_event(app.received[0]).language == "en"
    start = AudioStart.from_event(app.received[1])
    assert (start.rate, start.width, start.channels) == (16000, 2, 1)
    assert [e.payload for e in app.received[2:5]] == CHUNKS
    assert result.text == TEXT
    assert result.voice_id == {"v": 1, "status": "off"}


def test_voice_id_status(voice_id):
    assert voice_id.voice_id_status({"v": 1, "status": "off"}) == "off"
    assert voice_id.voice_id_status(None) == "none"
    assert voice_id.voice_id_status("junk") == "none"


# ── the entity ────────────────────────────────────────────────────────────
async def test_app_up_returns_the_text_unchanged(stt_mod, log_lines, health):
    async with FakeApp() as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.text == TEXT
    assert result.result == STT.SpeechResultState.SUCCESS
    assert health._USAGE["voice_id"].get("ok_ts")
    assert len(log_lines) == 1
    cat, line = log_lines[0]
    assert cat == "STT" and "route=app" in line and "app_status=off" in line


async def test_no_uri_hands_the_untouched_stream_to_the_fallback(stt_mod, log_lines, health):
    fb = FakeFallback()
    STT.ENTITIES[fb.entity_id] = fb
    ent = _entity(stt_mod, {"voice_id_fallback_stt": fb.entity_id})
    result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.text == "fallback words"
    assert fb.calls == [{"already_read": 0, "chunks": CHUNKS}]
    assert "route=fallback" in log_lines[0][1]
    assert "voice_id" not in health._USAGE   # no app set: nothing to report


async def test_app_refused_hands_the_untouched_stream_to_the_fallback(stt_mod, log_lines, health):
    fb = FakeFallback()
    STT.ENTITIES[fb.entity_id] = fb
    ent = _entity(stt_mod, {"voice_id_uri": _closed_port_uri(),
                            "voice_id_fallback_stt": fb.entity_id})
    result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.text == "fallback words"
    assert fb.calls == [{"already_read": 0, "chunks": CHUNKS}]
    assert "route=fallback" in log_lines[0][1]
    assert health._USAGE["voice_id"].get("fail_ts")


async def test_app_connect_timeout_after_two_seconds_uses_the_fallback(
        stt_mod, log_lines, monkeypatch):
    async def _hang(*a, **k):
        await asyncio.sleep(60)
    monkeypatch.setattr(asyncio, "open_connection", _hang)
    fb = FakeFallback()
    STT.ENTITIES[fb.entity_id] = fb
    ent = _entity(stt_mod, {"voice_id_uri": "tcp://10.9.9.9:10350",
                            "voice_id_fallback_stt": fb.entity_id})
    t0 = time.monotonic()
    result = await ent.async_process_audio_stream(_meta(), Stream())
    elapsed = time.monotonic() - t0
    assert 1.9 <= elapsed < 3.0
    assert result.text == "fallback words"
    assert fb.calls == [{"already_read": 0, "chunks": CHUNKS}]
    assert "timed out" in log_lines[0][1]


async def test_app_drops_mid_stream_is_an_error_and_skips_the_fallback(
        stt_mod, log_lines, health):
    fb = FakeFallback()
    STT.ENTITIES[fb.entity_id] = fb
    async with FakeApp(mode="drop") as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri,
                                "voice_id_fallback_stt": fb.entity_id})
        stream = Stream([b"\x00\x00" * 4096] * 200)   # enough to hit the closed socket
        result = await ent.async_process_audio_stream(_meta(), stream)
    assert result.result == STT.SpeechResultState.ERROR
    assert result.text is None
    assert fb.calls == []
    assert "route=error" in log_lines[0][1]
    assert health._USAGE["voice_id"].get("fail_ts")


async def test_app_error_event_is_an_error(stt_mod, log_lines):
    async with FakeApp(mode="error") as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.result == STT.SpeechResultState.ERROR
    assert "route=error" in log_lines[0][1]


async def test_no_fallback_and_app_down_is_an_error_not_a_crash(stt_mod, log_lines):
    ent = _entity(stt_mod, {"voice_id_uri": _closed_port_uri()})
    result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.result == STT.SpeechResultState.ERROR
    assert "no fallback set" in log_lines[0][1]


async def test_fallback_is_never_nova_itself(stt_mod, log_lines):
    ent = _entity(stt_mod, {"voice_id_fallback_stt": "stt.nova_voice_id"})
    STT.ENTITIES["stt.nova_voice_id"] = ent
    result = await ent.async_process_audio_stream(_meta(), Stream())
    assert result.result == STT.SpeechResultState.ERROR


async def test_fallback_that_cant_take_the_language_is_an_error(stt_mod, log_lines):
    fb = FakeFallback(languages=["fr"])
    STT.ENTITIES[fb.entity_id] = fb
    ent = _entity(stt_mod, {"voice_id_fallback_stt": fb.entity_id})
    result = await ent.async_process_audio_stream(_meta("en"), Stream())
    assert result.result == STT.SpeechResultState.ERROR
    assert fb.calls == []


async def test_languages_come_from_the_app_info(stt_mod):
    async with FakeApp() as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        await ent.async_refresh_info()
    assert ent.supported_languages == ["en", "de"]
    st = ent.status()
    assert (st["state"], st["version"], st["models"]) == ("connected", "0.1.0", ["tiny-int8"])


async def test_languages_come_from_the_fallback_when_the_app_is_down(stt_mod):
    fb = FakeFallback(languages=["en", "fr"])
    STT.ENTITIES[fb.entity_id] = fb
    ent = _entity(stt_mod, {"voice_id_uri": _closed_port_uri(),
                            "voice_id_fallback_stt": fb.entity_id})
    await ent.async_refresh_info()
    assert ent.status()["state"] == "unreachable"
    assert ent.supported_languages == ["en", "fr"]


async def test_languages_come_from_the_fallback_when_whisper_has_no_models(stt_mod):
    fb = FakeFallback(languages=["en", "fr"])
    STT.ENTITIES[fb.entity_id] = fb
    async with FakeApp(mode="no_models") as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri,
                                "voice_id_fallback_stt": fb.entity_id})
        await ent.async_refresh_info()
    assert ent.supported_languages == ["en", "fr"]


def test_languages_default_to_english_and_log_once(stt_mod, caplog):
    ent = _entity(stt_mod, {})
    with caplog.at_level("INFO"):
        assert ent.supported_languages == ["en"]
        assert ent.supported_languages == ["en"]
    assert sum("no languages" in r.getMessage() for r in caplog.records) == 1


def test_status_not_set_without_an_address(stt_mod):
    assert _entity(stt_mod, {}).status()["state"] == "not_set"


async def test_the_stt_log_line_never_contains_the_words(stt_mod, log_lines):
    fb = FakeFallback()
    STT.ENTITIES[fb.entity_id] = fb
    async with FakeApp() as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        await ent.async_process_audio_stream(_meta(), Stream())
    ent2 = _entity(stt_mod, {"voice_id_fallback_stt": fb.entity_id})
    await ent2.async_process_audio_stream(_meta(), Stream())
    assert len(log_lines) == 2
    for _cat, line in log_lines:
        assert TEXT not in line and "kitchen" not in line
        assert "fallback words" not in line


async def test_the_connection_is_always_closed(stt_mod, voice_id, monkeypatch):
    opened, closed = [], []
    real_connect, real_close = voice_id.connect, voice_id.close

    async def _connect(uri, *a, **k):
        client = await real_connect(uri, *a, **k)
        opened.append(client)
        return client

    async def _close(client):
        closed.append(client)
        await real_close(client)
    monkeypatch.setattr(voice_id, "connect", _connect)
    monkeypatch.setattr(voice_id, "close", _close)
    for mode in ("ok", "drop", "error"):
        async with FakeApp(mode=mode) as app:
            ent = _entity(stt_mod, {"voice_id_uri": app.uri})
            await ent.async_process_audio_stream(_meta(), Stream([b"\x00\x00" * 4096] * 200))
            await asyncio.gather(*ent.hass.tasks)   # the Info refresh after success
    assert opened and all(c in closed for c in opened)
    assert all(c._writer is None for c in opened)


async def test_a_failed_connect_drops_the_app_languages(stt_mod):
    fb = FakeFallback(languages=["en", "fr"])
    STT.ENTITIES[fb.entity_id] = fb
    settings = {"voice_id_fallback_stt": fb.entity_id}
    async with FakeApp() as app:
        settings["voice_id_uri"] = app.uri
        ent = _entity(stt_mod, settings)
        await ent.async_refresh_info()
        assert ent.supported_languages == ["en", "de"]
    await ent.async_process_audio_stream(_meta(), Stream())   # app now gone
    assert ent.status()["state"] == "unreachable"
    assert ent.supported_languages == ["en", "fr"]


@pytest.mark.parametrize("key,value,ok", [
    ("voice_id_uri", "", True),
    ("voice_id_uri", "tcp://abc-nova-voice-id:10350", True),
    ("voice_id_uri", " tcp://abc-nova-voice-id:10350 ", True),
    ("voice_id_uri", "http://abc:10350", False),
    ("voice_id_uri", 10350, False),
    ("voice_id_fallback_stt", "", True),
    ("voice_id_fallback_stt", "stt.faster_whisper", True),
    ("voice_id_fallback_stt", "tts.piper", False),
    ("voice_id_fallback_stt", None, False),
])
def test_panel_values_are_checked(load, key, value, ok):
    assert load("safety_config").valid_panel_value(key, value) is ok
