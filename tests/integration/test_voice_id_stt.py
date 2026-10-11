"""Nova Voice ID on a real Home Assistant (8.35.0, Phase 1b).

What tests/unit/test_voice_id.py can't prove with a stand-in stt module:
the entity really registers, a real STT entity really works as the fallback,
and Supervisor discovery updates the one Nova entry without creating another.
"""
import asyncio
from collections.abc import AsyncIterable

import pytest

from homeassistant import config_entries
from homeassistant.components import stt
from homeassistant.components.stt.const import DATA_COMPONENT
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component

from wyoming.asr import Transcript
from wyoming.audio import AudioStop
from wyoming.event import async_read_event, async_write_event

from .conftest import MockConfigEntry

DOMAIN = "nova"
TEXT = "what's the weather"
CHUNKS = [b"\x01\x00" * 160, b"\x02\x00" * 160]
KEYS = ("voice_id_uri", "voice_id_fallback_stt", "voice_id_uri_discovered",
        "voice_id_token_discovered_sha256")


def _meta(language="en"):
    return stt.SpeechMetadata(
        language=language, format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO)


class Stream:
    def __init__(self):
        self.read = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in CHUNKS:
            self.read += 1
            yield c


class FallbackStt(stt.SpeechToTextEntity):
    """A plain STT entity in HA's own stt component, standing in for Whisper."""

    _attr_name = "Test Whisper"
    _attr_unique_id = "test-whisper"

    def __init__(self):
        self.calls = []

    supported_languages = ["en"]
    supported_formats = [stt.AudioFormats.WAV]
    supported_codecs = [stt.AudioCodecs.PCM]
    supported_bit_rates = [stt.AudioBitRates.BITRATE_16]
    supported_sample_rates = [stt.AudioSampleRates.SAMPLERATE_16000]
    supported_channels = [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream: AsyncIterable[bytes]):
        before = getattr(stream, "read", None)
        got = [c async for c in stream]
        self.calls.append((before, got))
        return stt.SpeechResult("from fallback", stt.SpeechResultState.SUCCESS)


async def _fake_app():
    async def handle(reader, writer):
        try:
            while (event := await async_read_event(reader)) is not None:
                if AudioStop.is_type(event.type):
                    tr = Transcript(text=TEXT).event()
                    tr.data["nova_voice_id"] = {"v": 1, "status": "off"}
                    await async_write_event(tr, writer)
        finally:
            writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": 7},
        options={},
    )


@pytest.fixture
def clean_voice_id_config(hass):
    """PHACC shares one config folder between runs: never leave the keys."""
    from custom_components.nova import nova_config, paths
    paths.configure(hass)

    def _clear():
        for key in KEYS:
            nova_config.delete(key)
    _clear()
    yield
    _clear()


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "stt", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _nova_entity(hass, entry):
    entity_id = er.async_get(hass).async_get_entity_id(
        "stt", DOMAIN, f"{entry.entry_id}-voice-id-stt")
    assert entity_id
    return stt.async_get_speech_to_text_entity(hass, entity_id)


async def _set(hass, entry, key, value):
    from custom_components.nova import nova_config
    entry.runtime_data.runtime_config[key] = value
    await hass.async_add_executor_job(nova_config.set, key, value)


async def test_entity_exists_with_nothing_set(hass, clean_voice_id_config):
    entry = await _setup_nova(hass)
    entity_id = er.async_get(hass).async_get_entity_id(
        "stt", DOMAIN, f"{entry.entry_id}-voice-id-stt")
    assert entity_id == "stt.nova_voice_id"
    assert hass.states.get(entity_id) is not None
    entity = _nova_entity(hass, entry)
    assert entity.status()["state"] == "not_set"
    assert entity.supported_languages == ["en"]
    assert entry.runtime_data.voice_id is entity
    assert "voice_id_info" in entry.runtime_data.scheduler.task_names()


async def test_app_up_returns_the_text_through_real_ha(hass, clean_voice_id_config, socket_enabled):
    entry = await _setup_nova(hass)
    server, uri = await _fake_app()
    try:
        await _set(hass, entry, "voice_id_uri", uri)
        entity = _nova_entity(hass, entry)
        result = await entity.async_process_audio_stream(_meta(), Stream())
    finally:
        server.close()
        await server.wait_closed()
    assert result == stt.SpeechResult(TEXT, stt.SpeechResultState.SUCCESS)


async def test_real_fallback_entity_gets_the_untouched_stream(hass, clean_voice_id_config, socket_enabled):
    entry = await _setup_nova(hass)
    fallback = FallbackStt()
    await hass.data[DATA_COMPONENT].async_add_entities([fallback])
    await hass.async_block_till_done()
    assert fallback.entity_id == "stt.test_whisper"

    await _set(hass, entry, "voice_id_uri", "tcp://127.0.0.1:1")   # nothing listens
    await _set(hass, entry, "voice_id_fallback_stt", fallback.entity_id)
    entity = _nova_entity(hass, entry)
    assert entity.check_metadata(_meta())
    result = await entity.async_process_audio_stream(_meta(), Stream())
    assert result == stt.SpeechResult("from fallback", stt.SpeechResultState.SUCCESS)
    assert fallback.calls == [(0, CHUNKS)]


async def test_discovery_updates_the_existing_entry(hass, clean_voice_id_config):
    from custom_components.nova import nova_config
    entry = await _setup_nova(hass)
    uri = "tcp://abc123-nova-voice-id:10350"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(config={"uri": uri}, name="Nova Voice ID",
                               slug="abc123_nova_voice_id", uuid="1234"))
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "updated"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.runtime_data.runtime_config["voice_id_uri"] == uri
    assert nova_config.get("voice_id_uri") == uri


async def test_discovery_without_nova_creates_nothing(hass, clean_voice_id_config):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(config={"uri": "tcp://x:10350"}, name="Nova Voice ID",
                               slug="x", uuid="1"))
    assert result["type"] == "abort" and result["reason"] == "no_nova_entry"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_discovery_with_a_bad_address_changes_nothing(hass, clean_voice_id_config):
    from custom_components.nova import nova_config
    await _setup_nova(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(config={"uri": "http://x"}, name="Nova Voice ID",
                               slug="x", uuid="2"))
    assert result["reason"] == "invalid_discovery_info"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert not nova_config.get("voice_id_uri")
