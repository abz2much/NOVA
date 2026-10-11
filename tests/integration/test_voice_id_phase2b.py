"""Nova Voice ID 8.36.0 on a real Home Assistant (PHACC).

The voice result travels from the STT entity to the turn in one task, as in
HA's pipeline; the turn logs it and changes nothing else; a recording turn is
answered before dedup, phrases, the local engine and the AI; discovery keeps
typed values; the panel commands work over a real websocket.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.components import stt
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .test_chat_turn import _isolate_conversations_db, _setup_nova, _speech

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_id_fakes import TEXT, TOKEN, VoiceApp  # noqa: E402

DOMAIN = "nova"
DEVICE = "dev-kitchen"
KNOWN = {"v": 1, "status": "known", "person": "person.abi", "score": 0.71,
         "speech_s": 2.4, "sample_id": "abc_123", "model": "m", "ms": 85}
CONFIG_KEYS = ("voice_id_uri", "voice_id_fallback_stt", "voice_id_uri_discovered",
               "voice_id_token_discovered_sha256", "voice_id_hidden_people")


def _meta():
    return stt.SpeechMetadata(
        language="en", format=stt.AudioFormats.WAV, codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO)


async def _stream():
    yield b"\x01\x00" * 320


@pytest.fixture
def isolated(hass, tmp_path, monkeypatch):
    """Own conversations.db and secrets.yaml; no Voice ID keys left behind."""
    from custom_components.nova import ha_secrets, nova_config, paths, voice_enrol
    _isolate_conversations_db(monkeypatch, tmp_path)
    paths.configure(hass)
    monkeypatch.setattr(ha_secrets, "_secrets_path", lambda: tmp_path / "secrets.yaml")
    ha_secrets._reset_secrets_cache()
    for key in CONFIG_KEYS:
        nova_config.delete(key)
    voice_enrol.clear()
    yield
    voice_enrol.clear()
    for key in CONFIG_KEYS:
        nova_config.delete(key)
    ha_secrets._reset_secrets_cache()


class _Turn:
    """Records what a turn reached, with no provider called."""

    def __init__(self):
        self.dedup = 0
        self.phrases = 0
        self.local = 0
        self.agent = 0

    def patches(self):
        from custom_components.nova import conversation as conv
        real_dedup = conv._check_and_claim_dedup

        def dedup(*a, **k):
            self.dedup += 1
            return real_dedup(*a, **k)

        async def phrase(*a, **k):
            from custom_components.nova.voice_phrase_runner import PhraseStep
            self.phrases += 1
            return PhraseStep()                  # no phrase matched

        async def local(*a, **k):
            self.local += 1
            return SimpleNamespace(handled=False, text="")

        async def agent(*a, **k):
            self.agent += 1
            return "From the agent."
        return [patch("custom_components.nova.conversation._check_and_claim_dedup", new=dedup),
                patch("custom_components.nova.voice_phrase_runner.try_phrase", new=phrase),
                patch("custom_components.nova.local_engine.try_local", new=local),
                patch("custom_components.nova.agent.run_agent", new=agent)]


async def _voice_turn(hass, conv_entity, text, device_id):
    """STT then the conversation agent, awaited in one task as HA's pipeline does."""
    from homeassistant.components import conversation as conversation_component
    from custom_components.nova import stt as nova_stt
    stt_entity = stt.async_get_speech_to_text_entity(hass, "stt.nova_voice_id")
    assert isinstance(stt_entity, nova_stt.NovaVoiceIdStt)
    heard = await stt_entity.async_process_audio_stream(_meta(), _stream())
    result = await conversation_component.async_converse(
        hass, text, None, Context(), agent_id=conv_entity, device_id=device_id)
    return heard, result


async def _set_app(hass, uri, token=TOKEN):
    from custom_components.nova import voice_id_store
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entry.runtime_data.runtime_config["voice_id_uri"] = uri
    await voice_id_store.async_set_token(hass, token)


def _log(monkeypatch):
    from custom_components.nova import websocket
    lines = []
    real = websocket.nova_log

    def record(cat, msg):
        lines.append((cat, msg))
        real(cat, msg)
    monkeypatch.setattr(websocket, "nova_log", record)
    return lines


async def test_the_turn_logs_its_voice_result_and_behaves_as_before(
        hass, isolated, monkeypatch, socket_enabled):
    conv_entity = await _setup_nova(hass)
    lines = _log(monkeypatch)
    turn = _Turn()
    async with VoiceApp(blocks=[KNOWN]) as app:
        await _set_app(hass, app.uri)
        ps = turn.patches()
        for p in ps:
            p.start()
        try:
            heard, result = await _voice_turn(hass, conv_entity, TEXT, DEVICE)
        finally:
            for p in ps:
                p.stop()
    assert heard.text == TEXT
    assert _speech(result) == "From the agent."
    assert (turn.dedup, turn.agent) == (1, 1)               # the normal path
    voice = [m for c, m in lines if c == "VOICE"]
    assert voice == ["status=known person=person.abi score=0.71 second=- margin=- "
                     "speech_s=2.40 ms=85 device=dev-kitchen"]
    cats = [c for c, _ in lines]
    assert cats.index("VOICE") == cats.index("CONV") + 1    # right after ENTRY
    from custom_components.nova import voice_id
    assert voice_id.VOICE_RESULT.get() is None              # read once
    for _cat, msg in lines:
        if _cat in ("VOICE", "STT"):
            assert TEXT not in msg and TOKEN not in msg


async def test_a_typed_turn_has_no_voice_line(hass, isolated, monkeypatch):
    conv_entity = await _setup_nova(hass)
    lines = _log(monkeypatch)
    turn = _Turn()
    ps = turn.patches()
    for p in ps:
        p.start()
    try:
        from homeassistant.components import conversation as conversation_component
        await conversation_component.async_converse(
            hass, "hello", None, Context(), agent_id=conv_entity)
    finally:
        for p in ps:
            p.stop()
    assert not [m for c, m in lines if c == "VOICE"]


async def test_a_recording_turn_never_reaches_dedup_phrases_engine_or_ai(
        hass, isolated, monkeypatch, socket_enabled):
    from custom_components.nova import voice_enrol
    conv_entity = await _setup_nova(hass)
    hass.states.async_set("person.abi", "home", {"friendly_name": "Abi"})
    turn = _Turn()
    async with VoiceApp(blocks=[KNOWN]) as app:
        await _set_app(hass, app.uri)
        voice_enrol._SESSION = voice_enrol.Session(
            device_id=DEVICE, satellite="assist_satellite.kitchen", person="person.abi",
            name="Abi", lang="en", expires_at=voice_enrol._now() + 300)
        ps = turn.patches()
        for p in ps:
            p.start()
        try:
            _heard, result = await _voice_turn(hass, conv_entity, TEXT, DEVICE)
            assert (turn.dedup, turn.phrases, turn.local, turn.agent) == (0, 0, 0, 0)
            assert _speech(result).startswith("Thanks. Next, please say:")
            assert app.commits == [("abc_123", "person.abi")]
            # Another satellite during the session: handled normally, never committed.
            _heard, other = await _voice_turn(hass, conv_entity, TEXT, "dev-office")
        finally:
            for p in ps:
                p.stop()
    assert _speech(other) == "From the agent."
    assert turn.agent == 1 and len(app.commits) == 1
    assert voice_enrol.status()["committed"] == 1


async def test_discovery_fills_empty_keeps_typed_and_never_adds_an_entry(hass, isolated):
    from custom_components.nova import voice_id_store
    await _setup_nova(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    async def discover(uri, token):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
            data=HassioServiceInfo(config={"uri": uri, "token": token}, name="Nova Voice ID",
                                   slug="abc_nova_voice_id", uuid="1"))
        await hass.async_block_till_done()
        assert result["type"] == "abort" and result["reason"] == "updated"

    await discover("tcp://a:10350", "tok-1")
    assert voice_id_store.get_uri(hass, entry) == "tcp://a:10350"
    assert await voice_id_store.async_get_token(hass) == "tok-1"
    await discover("tcp://b:10350", "tok-2")                # still the discovered values
    assert voice_id_store.get_uri(hass, entry) == "tcp://b:10350"
    assert await voice_id_store.async_get_token(hass) == "tok-2"
    await voice_id_store.async_set_uri(hass, entry, "tcp://mine:10350")
    await voice_id_store.async_set_token(hass, "typed")
    await discover("tcp://c:10350", "tok-3")
    assert voice_id_store.get_uri(hass, entry) == "tcp://mine:10350"
    assert await voice_id_store.async_get_token(hass) == "typed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_panel_commands_over_a_real_websocket(
        hass, isolated, hass_ws_client, socket_enabled):
    await _setup_nova(hass)
    hass.states.async_set("person.abi", "home", {"friendly_name": "Abi"})
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    client = await hass_ws_client(hass)
    n = iter(range(1, 100))

    async def call(**msg):
        await client.send_json({"id": next(n), **msg})
        reply = await client.receive_json()
        assert reply["success"], reply
        return reply["result"]

    assert (await call(type="nova/voice_id_token_set", token="tok"))["ok"]
    assert (await call(type="nova/voice_id_token_get"))["token"] == "tok"
    profiles = [{"person": "person.abi", "samples": 8, "updated": 1760000000.0}]
    async with VoiceApp(token="tok", profiles=profiles) as app:
        await _set_app(hass, app.uri, token="tok")
        listing = await call(type="nova/voice_id_profiles")
        assert listing["ok"] and [(p["person"], p["samples"]) for p in listing["people"]] == [
            ("person.abi", 8), ("person.sam", 0)]
        one = await call(type="nova/voice_id_delete", person="person.abi")
        assert one == {"ok": True, "deleted": ["person.abi"], "all": False}
        everyone = await call(type="nova/voice_id_delete", all=True)
        assert everyone["ok"] and everyone["all"] is True
        assert [e.data for e in app.management[1:]] == [
            {"token": "tok", "person": "person.abi"}, {"token": "tok", "all": True}]
        await call(type="nova/voice_id_token_set", token="wrong")
        bad = await call(type="nova/voice_id_profiles")
        assert bad["ok"] is False and bad["code"] == "unauthorised"
        assert bad["error"] == ("The app's token doesn't match. "
                                "Copy it from the app's Configuration tab.")
    panel = await call(type="nova/get_panel_data")
    cfg = panel["config"]
    assert cfg["voice_id_token_set"] is True and "wrong" not in str(panel)
    assert cfg["voice_id_enrol"] == {"state": "idle"}
