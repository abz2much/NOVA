"""Nova Voice ID 8.38.0 on a real Home Assistant: learning from everyday use.

Through the real conversation stack, the sample is committed as learned only
after the reply is built, only with both switches on, and never for Chat, a
recording turn or a confirmation answer. The app's answer never affects the
turn. The satellite check and the app version (from the app's Info) are
stood in for, so the fake app need not describe itself.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.core import Context

from .test_chat_turn import _setup_nova, _speech
from .test_voice_id_turn import (  # noqa: F401 - the fixture is used by name
    DEVICE, TEXT, VoiceApp, _log, _set_app, _voice_turn, isolated,
)

BOB = {"v": 1, "status": "known", "person": "person.bob", "score": 0.84,
       "speech_s": 2.6, "sample_id": "s1", "model": "m", "ms": 70}


class _Turn:
    def __init__(self, app):
        self.app = app
        self.sent_before_reply = None

    def patches(self):
        async def local(*a, **k):
            return SimpleNamespace(handled=False, text="")

        async def agent(*a, **k):
            # the reply is being built: nothing may have been sent yet
            self.sent_before_reply = len(self.app.management)
            return "From the agent."
        return [patch("custom_components.nova.local_engine.try_local", new=local),
                patch("custom_components.nova.agent.run_agent", new=agent),
                patch("custom_components.nova.voice_confirm.is_voice_satellite_device",
                      new=lambda hass, device_id, strict=False: device_id in (DEVICE, "dev-office")),
                patch("custom_components.nova.voice_learn._app_version",
                      new=lambda entry: (0, 3, 0)),
                # Faces, rooms and trackers from other tests share module state;
                # these tests decide who is home through person entities only.
                patch("custom_components.nova.identity._face_votes",
                      new=lambda hass, now, names=None: {}),
                patch("custom_components.nova.identity._room_votes",
                      new=lambda hass, area, now: {}),
                patch("custom_components.nova.identity._proximity_votes",
                      new=lambda hass, area: {})]


def _switches(hass, identity=True, learning=True):
    from custom_components.nova import nova_config
    entry = hass.config_entries.async_entries("nova")[0]
    for key, on in (("voice_id_identity", identity), ("voice_id_learning", learning)):
        entry.runtime_data.runtime_config[key] = on
        nova_config.set(key, on)


@pytest.fixture
def clean(hass):
    yield
    from custom_components.nova import identity, nova_config, pending_confirm, voice_learn
    for key in ("voice_id_identity", "voice_id_learning"):
        nova_config.delete(key)
    identity.clear_voice_carry()
    voice_learn.clear()
    pending_confirm._ITEMS.clear()


async def _run(hass, monkeypatch, conv, *, identity=True, learning=True, text=TEXT,
               block=None, before=None, refuse=False):
    hass.states.async_set("person.bob", "home", {"friendly_name": "Bob Jones"})
    hass.states.async_set("person.alice", "not_home", {"friendly_name": "Alice"})
    _switches(hass, identity, learning)
    lines = _log(monkeypatch)
    async with VoiceApp(blocks=[block or BOB], text=text, refuse_learned=refuse) as app:
        await _set_app(hass, app.uri)
        turn = _Turn(app)
        ps = turn.patches()
        for p in ps:
            p.start()
        try:
            if before:
                before()
            _heard, result = await _voice_turn(hass, conv, text, DEVICE)
            await hass.async_block_till_done(wait_background_tasks=True)
        finally:
            for p in ps:
                p.stop()
    return result, app, turn, [m for c, m in lines if c == "VOICE"]


async def test_all_conditions_met_one_learned_commit_after_the_reply(
        hass, isolated, clean, monkeypatch, socket_enabled):
    conv = await _setup_nova(hass)
    result, app, turn, voice = await _run(hass, monkeypatch, conv)
    assert _speech(result) == "From the agent."
    assert turn.sent_before_reply == 0                    # nothing before the reply
    assert app.sources == ["learned"] and app.commits == [("s1", "person.bob")]
    assert voice[-1] == f"person=person.bob device={DEVICE} + learned"
    assert all(TEXT not in v for v in voice)


@pytest.mark.parametrize("identity,learning", [(True, False), (False, True), (False, False)])
async def test_either_switch_off_means_no_commit(
        hass, isolated, clean, monkeypatch, socket_enabled, identity, learning):
    conv = await _setup_nova(hass)
    result, app, _turn, voice = await _run(hass, monkeypatch, conv,
                                           identity=identity, learning=learning)
    assert _speech(result) == "From the agent."
    assert app.management == [] and len(voice) == 1     # only the turn's own VOICE line


async def test_a_refused_reply_never_affects_the_turn(
        hass, isolated, clean, monkeypatch, socket_enabled):
    conv = await _setup_nova(hass)
    result, app, _turn, voice = await _run(hass, monkeypatch, conv, refuse=True)
    assert _speech(result) == "From the agent."
    assert app.commits == []
    assert voice[-1] == f"person=person.bob device={DEVICE} learn refused (refused)"


async def test_a_confirmation_answer_never_learns(
        hass, isolated, clean, monkeypatch, socket_enabled):
    from custom_components.nova import pending_confirm
    conv = await _setup_nova(hass)

    def hold():
        pending_confirm.put(DEVICE, pending_confirm.KIND_SCENE_SCRIPT, "script.heating_boost")
    _result, app, _turn, _voice = await _run(hass, monkeypatch, conv, text="no", before=hold)
    assert app.management == []


async def test_a_recording_turn_commits_as_recorded_only(
        hass, isolated, clean, monkeypatch, socket_enabled):
    from custom_components.nova import voice_enrol
    conv = await _setup_nova(hass)

    def arm():
        voice_enrol._SESSION = voice_enrol.Session(
            device_id=DEVICE, satellite="assist_satellite.kitchen", person="person.bob",
            name="Bob", lang="en", expires_at=voice_enrol._now() + 300)
    try:
        _result, app, _turn, _voice = await _run(hass, monkeypatch, conv, before=arm)
    finally:
        voice_enrol.clear()
    assert app.sources == ["enrolled"]
    assert "source" not in app.management[0].data


async def test_a_typed_chat_turn_never_learns(hass, isolated, clean, monkeypatch, socket_enabled):
    from homeassistant.components import conversation as conversation_component
    from custom_components.nova import policy, voice_id
    conv = await _setup_nova(hass)
    hass.states.async_set("person.bob", "home", {"friendly_name": "Bob Jones"})
    _switches(hass)
    async with VoiceApp(blocks=[BOB]) as app:
        await _set_app(hass, app.uri)
        voice_id.set_turn_voice(voice_id.parse_block(BOB))   # as if a result lingered
        turn = _Turn(app)
        ps = turn.patches()
        for p in ps:
            p.start()
        try:
            user = await hass.auth.async_create_user("Chat user")
            await conversation_component.async_converse(
                hass, "hello", policy.chat_conversation_id(user.id), Context(user_id=user.id),
                agent_id=conv)
            await hass.async_block_till_done(wait_background_tasks=True)
        finally:
            for p in ps:
                p.stop()
            voice_id.VOICE_RESULT.set(None)
    assert app.management == []



async def test_a_duplicate_on_a_second_satellite_is_never_learned(
        hass, isolated, clean, monkeypatch, socket_enabled):
    """Two satellites hear the same sentence: dedup answers the second from
    the first's reply, so only the first, which Nova answered, learns."""
    from custom_components.nova import stt as nova_stt
    from homeassistant.components import conversation as conversation_component, stt
    conv = await _setup_nova(hass)
    hass.states.async_set("person.bob", "home", {"friendly_name": "Bob Jones"})
    hass.states.async_set("person.alice", "not_home", {"friendly_name": "Alice"})
    _switches(hass)
    second = {**BOB, "sample_id": "s2"}
    async with VoiceApp(blocks=[BOB, second]) as app:
        await _set_app(hass, app.uri)
        turn = _Turn(app)
        ps = turn.patches()
        for p in ps:
            p.start()
        try:
            entity = stt.async_get_speech_to_text_entity(hass, "stt.nova_voice_id")
            assert isinstance(entity, nova_stt.NovaVoiceIdStt)
            from .test_voice_id_turn import _meta, _stream
            from custom_components.nova import conversation as nova_conv
            nova_conv._dedup_cache.clear()
            import time as _time
            for device in (DEVICE, "dev-office"):
                if device == "dev-office":
                    # The second satellite heard the same sentence at once:
                    # its turn falls inside the dedup window. (A test turn
                    # takes longer than the 4 s window, so set it directly.)
                    key = nova_conv._dedup_key(TEXT)
                    _ts, cached, winner = nova_conv._dedup_cache[key]
                    nova_conv._dedup_cache[key] = (_time.time(), cached, winner)
                await entity.async_process_audio_stream(_meta(), _stream())
                await conversation_component.async_converse(
                    hass, TEXT, None, Context(), agent_id=conv, device_id=device)
                # the first turn's commit finishes before the second turn, so
                # the in-flight guard can't be what stops the second one
                await hass.async_block_till_done(wait_background_tasks=True)
        finally:
            for p in ps:
                p.stop()
    assert app.commits == [("s1", "person.bob")]


async def test_remove_learned_is_refused_for_an_older_app(
        hass, isolated, clean, hass_ws_client, socket_enabled):
    """An app older than 0.3.0 doesn't know "source", so it is never sent one."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    async with VoiceApp(profiles=[{"person": "person.bob", "samples": 8}]) as app:
        await _set_app(hass, app.uri)
        with patch("custom_components.nova.voice_learn._app_version", new=lambda entry: (0, 2, 0)):
            await client.send_json({"id": 1, "type": "nova/voice_id_delete",
                                    "person": "person.bob", "source": "learned"})
            reply = await client.receive_json()
    assert reply["result"]["ok"] is False and reply["result"]["code"] == "bad_request"
    assert app.management == []
