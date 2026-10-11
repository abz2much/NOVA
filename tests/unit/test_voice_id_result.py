"""Nova Voice ID 8.36.0: the per-request voice result and the management calls.

The result is the app's nova_voice_id block, checked, kept in
voice_id.VOICE_RESULT by the STT entity in its own task and read once by the
turn (take_turn_voice). VoiceApp (tests/voice_id_fakes.py) is a real Wyoming
server standing in for the app.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from voice_id_fakes import TEXT, TOKEN, VoiceApp, closed_port_uri, install_stt_stub

install_stt_stub()
STT = sys.modules["homeassistant.components.stt"]

CHUNKS = [b"\x01\x00" * 160, b"\x02\x00" * 160]
KNOWN = {"v": 1, "status": "known", "person": "person.abi", "score": 0.71,
         "second": "person.sam", "second_score": 0.48, "margin": 0.23,
         "speech_s": 2.4, "sample_id": "abc_123", "model": "wespeaker", "ms": 85}


def _meta():
    return STT.SpeechMetadata(
        language="en", format=STT.AudioFormats.WAV, codec=STT.AudioCodecs.PCM,
        bit_rate=STT.AudioBitRates.BITRATE_16,
        sample_rate=STT.AudioSampleRates.SAMPLERATE_16000,
        channel=STT.AudioChannels.CHANNEL_MONO)


async def _stream():
    for c in CHUNKS:
        yield c


class _Fallback(STT.SpeechToTextEntity):
    entity_id = "stt.whisper"
    supported_languages = ["en"]
    supported_formats = [STT.AudioFormats.WAV]
    supported_codecs = [STT.AudioCodecs.PCM]
    supported_bit_rates = [STT.AudioBitRates.BITRATE_16]
    supported_sample_rates = [STT.AudioSampleRates.SAMPLERATE_16000]
    supported_channels = [STT.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        [c async for c in stream]
        return STT.SpeechResult("fallback words", STT.SpeechResultState.SUCCESS)


class _Hass:
    def async_create_background_task(self, coro, name):
        coro.close()


@pytest.fixture
def vid(load):
    mod = load("voice_id")
    mod.VOICE_RESULT.set(None)
    mod._invalid_logged = False
    return mod


@pytest.fixture
def stt_mod(load, monkeypatch):
    mod = load("stt")
    ws_log = load("ws_log")
    monkeypatch.setattr(ws_log, "nova_log", lambda cat, msg: None)
    STT.ENTITIES.clear()
    yield mod
    STT.ENTITIES.clear()


def _entity(stt_mod, settings):
    ent = stt_mod.NovaVoiceIdStt(_Hass(), types.SimpleNamespace(entry_id="E1"))
    ent.entity_id = "stt.nova_voice_id"
    ent._setting = lambda key: settings.get(key, "")
    ent._state = "connected"
    return ent


# ── checking the block ────────────────────────────────────────────────────

def test_a_valid_block_keeps_every_field(vid):
    r = vid.parse_block(KNOWN)
    assert (r.status, r.person, r.score, r.second, r.second_score, r.margin,
            r.speech_s, r.sample_id, r.model, r.ms) == (
        "known", "person.abi", 0.71, "person.sam", 0.48, 0.23, 2.4, "abc_123", "wespeaker", 85)


@pytest.mark.parametrize("change", [
    {"extra": 1},                          # unknown key
    {"v": 2}, {"v": True}, {"v": None},    # wrong v
    {"status": "maybe"},                   # unknown status
    {"person": "person.Abi"},              # bad person id
    {"person": "light.kitchen"},
    {"second": "person.a b"},
    {"score": 1.5}, {"score": float("nan")}, {"score": True}, {"score": "0.7"},
    {"margin": -0.1}, {"speech_s": -1}, {"speech_s": 601}, {"ms": -1}, {"ms": 10**9},
    {"sample_id": "../etc"}, {"model": "x" * 200},
])
def test_an_invalid_block_becomes_error(vid, change):
    assert vid.parse_block({**KNOWN, **change}).status == "error"


@pytest.mark.parametrize("block", [None, "known", [], {"status": "known"}])
def test_no_block_or_a_broken_one_becomes_error(vid, block):
    assert vid.parse_block(block).status == "error"


def test_known_without_a_person_is_invalid(vid):
    assert vid.parse_block({k: v for k, v in KNOWN.items() if k != "person"}).status == "error"


def test_a_person_without_known_is_invalid(vid):
    assert vid.parse_block({**KNOWN, "status": "not_sure"}).status == "error"
    assert vid.parse_block({k: v for k, v in KNOWN.items() if k != "person"}
                           | {"status": "not_sure"}).status == "not_sure"


def test_app_0_1_0_status_off_is_accepted(vid):
    assert vid.parse_block({"v": 1, "status": "off"}).status == "off"


def test_an_invalid_block_is_logged_once_and_never_its_content(vid, caplog):
    with caplog.at_level(logging.WARNING):
        vid.parse_block({**KNOWN, "person": "person.Secret_Name"})
        vid.parse_block({**KNOWN, "extra": 1})
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Secret_Name" not in warnings[0]


# ── one request, read once ────────────────────────────────────────────────

async def test_a_valid_block_sets_the_result_and_it_is_read_once(vid, stt_mod):
    async with VoiceApp(blocks=[KNOWN]) as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        result = await ent.async_process_audio_stream(_meta(), _stream())
    assert result.text == TEXT
    got = vid.take_turn_voice()
    assert got is not None and got.status == "known" and got.person == "person.abi"
    assert vid.take_turn_voice() is None


async def test_the_fallback_route_sets_error(vid, stt_mod):
    STT.ENTITIES["stt.whisper"] = _Fallback()
    ent = _entity(stt_mod, {"voice_id_uri": closed_port_uri(),
                            "voice_id_fallback_stt": "stt.whisper"})
    result = await ent.async_process_audio_stream(_meta(), _stream())
    assert result.text == "fallback words"
    assert vid.take_turn_voice().status == "error"


async def test_no_app_set_sets_error_too(vid, stt_mod):
    STT.ENTITIES["stt.whisper"] = _Fallback()
    ent = _entity(stt_mod, {"voice_id_fallback_stt": "stt.whisper"})
    await ent.async_process_audio_stream(_meta(), _stream())
    assert vid.take_turn_voice().status == "error"


async def test_the_error_route_sets_error(vid, stt_mod):
    ent = _entity(stt_mod, {"voice_id_uri": closed_port_uri()})   # no fallback
    result = await ent.async_process_audio_stream(_meta(), _stream())
    assert result.result == STT.SpeechResultState.ERROR
    assert vid.take_turn_voice().status == "error"


async def test_an_invalid_block_from_the_app_sets_error(vid, stt_mod):
    async with VoiceApp(blocks=[{**KNOWN, "v": 9}]) as app:
        ent = _entity(stt_mod, {"voice_id_uri": app.uri})
        result = await ent.async_process_audio_stream(_meta(), _stream())
    assert result.text == TEXT                  # the words still come back
    assert vid.take_turn_voice().status == "error"


async def test_two_runs_at_once_never_see_each_others_result(vid, stt_mod):
    """Each pipeline run is its own task, as in HA."""
    other = {**KNOWN, "person": "person.sam", "second": "person.abi", "sample_id": "zz"}
    async with VoiceApp(blocks=[KNOWN]) as app_a, VoiceApp(blocks=[other]) as app_b:
        ent_a = _entity(stt_mod, {"voice_id_uri": app_a.uri})
        ent_b = _entity(stt_mod, {"voice_id_uri": app_b.uri})
        gate = asyncio.Event()

        async def run(ent, wait):
            await ent.async_process_audio_stream(_meta(), _stream())
            if wait:
                await gate.wait()          # the other run sets its result meanwhile
            else:
                gate.set()
            return vid.take_turn_voice()

        got_a, got_b = await asyncio.gather(run(ent_a, True), run(ent_b, False))
    assert got_a.person == "person.abi"
    assert got_b.person == "person.sam"
    assert vid.VOICE_RESULT.get() is None      # nothing leaked to the caller


def test_a_stale_result_is_never_handed_to_a_turn(vid, monkeypatch):
    vid.set_turn_voice(vid.parse_block(KNOWN))
    real = vid.time.monotonic
    monkeypatch.setattr(vid.time, "monotonic", lambda: real() + vid.MAX_RESULT_AGE_S + 1)
    assert vid.take_turn_voice() is None
    assert vid.VOICE_RESULT.get() is None


# ── the VOICE log line ────────────────────────────────────────────────────

def test_the_voice_line_has_the_numbers_and_never_words_or_token(vid):
    line = vid.log_line(vid.parse_block(KNOWN), "dev1")
    assert line == ("status=known person=person.abi score=0.71 second=person.sam "
                    "margin=0.23 speech_s=2.40 ms=85 device=dev1")
    assert TEXT not in line and TOKEN not in line and "abc_123" not in line


def test_the_voice_line_without_a_result(vid):
    assert vid.log_line(None, "dev1") == "status=none device=dev1"


# ── management calls ──────────────────────────────────────────────────────

async def test_commit_sends_the_token_sample_and_person(vid):
    async with VoiceApp() as app:
        count = await vid.enrol_commit(app.uri, TOKEN, "abc_123", "person.abi")
    assert count == 1
    event = app.management[0]
    assert event.type == "nvid-enrol-commit"
    assert event.data == {"token": TOKEN, "sample_id": "abc_123", "person": "person.abi"}


async def test_every_call_sends_the_token(vid):
    async with VoiceApp() as app:
        await vid.profile_list(app.uri, TOKEN)
        await vid.stats(app.uri, TOKEN)
        await vid.profile_delete(app.uri, TOKEN, "person.abi")
    assert [e.data.get("token") for e in app.management] == [TOKEN] * 3


async def test_an_unauthorised_reply_shows_the_right_message(vid):
    async with VoiceApp() as app:
        with pytest.raises(vid.VoiceIdManagementError) as err:
            await vid.profile_list(app.uri, "wrong")
    assert err.value.code == "unauthorised"
    assert str(err.value) == ("The app's token doesn't match. "
                              "Copy it from the app's Configuration tab.")


@pytest.mark.parametrize("code", ["not_found", "bad_request", "failed"])
async def test_each_error_code_has_its_own_message(vid, code):
    async with VoiceApp(errors={"nvid-enrol-commit": code}) as app:
        with pytest.raises(vid.VoiceIdManagementError) as err:
            await vid.enrol_commit(app.uri, TOKEN, "abc", "person.abi")
    assert err.value.code == code and str(err.value) == vid.ERROR_MESSAGES[code]


async def test_no_token_and_no_app_have_messages_too(vid):
    with pytest.raises(vid.VoiceIdManagementError) as err:
        await vid.profile_list("tcp://127.0.0.1:1", "")
    assert err.value.code == "no_token"
    with pytest.raises(vid.VoiceIdManagementError) as err:
        await vid.profile_list(closed_port_uri(), TOKEN)
    assert err.value.code == "unreachable"


async def test_delete_and_delete_all_send_the_right_events(vid):
    profiles = [{"person": "person.abi", "samples": 8, "updated": 1.0},
                {"person": "person.sam", "samples": 3, "updated": 2.0}]
    async with VoiceApp(profiles=profiles) as app:
        one = await vid.profile_delete(app.uri, TOKEN, "person.abi")
        everyone = await vid.profile_delete(app.uri, TOKEN, everyone=True)
    assert [e.data for e in app.management] == [
        {"token": TOKEN, "person": "person.abi"}, {"token": TOKEN, "all": True}]
    assert one == {"deleted": ["person.abi"], "all": False}
    assert everyone == {"deleted": ["person.sam"], "all": True}


async def test_profile_list_drops_bad_rows(vid):
    profiles = [{"person": "person.abi", "samples": 8, "updated": 1.5},
                {"person": "Not A Person", "samples": 1, "updated": 1.0}]
    async with VoiceApp(profiles=profiles) as app:
        listing = await vid.profile_list(app.uri, TOKEN)
    assert listing == {"profiles": [{"person": "person.abi", "samples": 8, "updated": 1.5}],
                       "model": "m"}


async def test_a_bad_person_is_refused_before_sending(vid):
    async with VoiceApp() as app:
        with pytest.raises(vid.VoiceIdManagementError):
            await vid.profile_delete(app.uri, TOKEN, "person.Bad Name")
    assert app.management == []
