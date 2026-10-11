"""Nova Voice ID 8.36.0: recording a voice from the panel (voice_enrol.py).

Commits go to a fake app (tests/voice_id_fakes.VoiceApp) over real Wyoming.
"""
from __future__ import annotations

import sys
import types

import pytest

from voice_id_fakes import TOKEN, VoiceApp

DEVICE = "dev-kitchen"
OTHER_DEVICE = "dev-office"


def _res(vid, **over):
    block = {"v": 1, "status": "not_sure", "score": 0.6, "speech_s": 2.2,
             "sample_id": f"s{over.pop('n', 0)}", "model": "m", "ms": 40}
    block.update(over)
    return vid.parse_block({k: v for k, v in block.items() if v is not None})


@pytest.fixture
def vid(load):
    return load("voice_id")


@pytest.fixture
def enrol(load, fake_hass, monkeypatch):
    mod = load("voice_enrol")
    mod.clear()
    fake_hass.states.async_set("person.abi", "home", {"friendly_name": "Abi Adeyoola"})
    fake_hass.states.async_set("person.sam", "home", {"friendly_name": "Sam"})
    fake_hass.states.async_set("assist_satellite.kitchen", "idle", {})
    er = sys.modules["homeassistant.helpers.entity_registry"]
    registry = types.SimpleNamespace(
        entities={},
        async_get=lambda eid: types.SimpleNamespace(device_id=DEVICE)
        if eid == "assist_satellite.kitchen" else None)
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    out = load("output_language")
    monkeypatch.setattr(out, "resolve", lambda hass, request_language=None: "en")
    yield mod
    mod.clear()


@pytest.fixture
async def app(load, monkeypatch):
    store = load("voice_id_store")
    async with VoiceApp() as fake:
        monkeypatch.setattr(store, "get_uri", lambda hass, entry: fake.uri)

        async def _token(hass):
            return TOKEN
        monkeypatch.setattr(store, "async_get_token", _token)
        yield fake


async def _start(enrol, hass):
    return await enrol.async_start(hass, None, "person.abi", "assist_satellite.kitchen")


async def test_start_arms_one_session_and_announces_the_first_sentence(enrol, app, fake_hass):
    state = await _start(enrol, fake_hass)
    assert state["state"] == "active" and state["committed"] == 0 and state["needed"] == 8
    assert fake_hass.service_calls == [("assist_satellite", "announce", {
        "entity_id": "assist_satellite.kitchen",
        "message": "Abi, after the wake word, please say: "
                   "The quick brown fox jumps over the lazy dog."})]
    with pytest.raises(enrol.EnrolError) as err:
        await _start(enrol, fake_hass)
    assert err.value.code == "busy"


async def test_the_session_devices_turns_are_committed(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    reply = await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1))
    assert reply == "Thanks. Next, please say: A cup of tea always tastes better in the morning."
    assert app.commits == [("s1", "person.abi")]
    assert app.management[0].data["token"] == TOKEN
    assert enrol.status()["committed"] == 1


async def test_other_devices_turns_are_never_committed(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    assert await enrol.async_handle_turn(fake_hass, None, OTHER_DEVICE, _res(vid, n=1)) is None
    assert await enrol.async_handle_turn(fake_hass, None, None, _res(vid, n=2)) is None
    assert app.commits == []


async def test_short_samples_are_retried_at_most_twice(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    first = "The quick brown fox jumps over the lazy dog."
    short = _res(vid, n=1, speech_s=1.2)
    assert await enrol.async_handle_turn(fake_hass, None, DEVICE, short) == \
        f"Sorry, once more please: {first}"
    assert await enrol.async_handle_turn(fake_hass, None, DEVICE, short) == \
        f"Sorry, once more please: {first}"
    third = await enrol.async_handle_turn(fake_hass, None, DEVICE, short)
    assert third == ("Let's try a different one. Please say: "
                     "A cup of tea always tastes better in the morning.")
    assert app.commits == []


@pytest.mark.parametrize("over", [{"sample_id": None}, {"speech_s": None},
                                  {"status": "short", "score": None, "sample_id": None}])
async def test_a_turn_without_a_usable_sample_is_retried(enrol, app, fake_hass, vid, over):
    await _start(enrol, fake_hass)
    reply = await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1, **over))
    assert reply.startswith("Sorry, once more please")
    assert app.commits == []


async def test_a_result_of_none_or_error_is_retried(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    assert (await enrol.async_handle_turn(fake_hass, None, DEVICE, None)).startswith("Sorry")
    assert (await enrol.async_handle_turn(
        fake_hass, None, DEVICE, vid.error_result())).startswith("Sorry")
    assert app.commits == []


async def _commit_two(enrol, hass, vid):
    for n in (1, 2):
        await enrol.async_handle_turn(hass, None, DEVICE, _res(vid, n=n))


@pytest.mark.parametrize("over", [
    {"status": "known", "person": "person.sam", "second": "person.abi"},   # known as someone else
    {"second": "person.abi"},                                              # this person second
])
async def test_a_sample_that_best_matches_someone_else_is_retried(enrol, app, fake_hass, vid, over):
    await _start(enrol, fake_hass)
    await _commit_two(enrol, fake_hass, vid)
    reply = await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=3, **over))
    assert reply.startswith("Sorry, once more please")
    assert [c[0] for c in app.commits] == ["s1", "s2"]


async def test_the_first_two_samples_skip_the_match_check(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    await enrol.async_handle_turn(fake_hass, None, DEVICE,
                                  _res(vid, n=1, second="person.abi"))
    assert app.commits == [("s1", "person.abi")]


@pytest.mark.parametrize("over", [
    {"status": "known", "person": "person.abi", "second": "person.sam"},
    {"second": "person.sam"},          # not sure, this person not second
    {},                                # one profile only: no second
])
async def test_from_the_third_sample_a_match_or_unclear_result_commits(enrol, app, fake_hass, vid, over):
    await _start(enrol, fake_hass)
    await _commit_two(enrol, fake_hass, vid)
    await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=3, **over))
    assert len(app.commits) == 3


async def test_eight_commits_end_the_session(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    replies = [await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=n))
               for n in range(1, 9)]
    assert replies[-1] == "Thanks, Abi, that's done."
    assert enrol.status()["state"] == "done" and enrol.status()["committed"] == 8
    assert enrol.current() is None
    assert await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=9)) is None
    assert len(app.commits) == 8


async def test_cancel_ends_the_session(enrol, app, fake_hass, vid):
    await _start(enrol, fake_hass)
    await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1))
    assert enrol.cancel() is True
    assert enrol.status()["state"] == "cancelled"
    assert await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=2)) is None
    assert app.commits == [("s1", "person.abi")]          # kept


async def test_expiry_ends_it_quietly_and_keeps_the_commits(enrol, app, fake_hass, vid, monkeypatch):
    await _start(enrol, fake_hass)
    await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1))
    real = enrol._now
    monkeypatch.setattr(enrol, "_now", lambda: real() + enrol.SESSION_S + 1)
    assert await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=2)) is None
    assert enrol.status()["state"] == "expired"
    assert len(fake_hass.service_calls) == 1               # nothing announced
    assert app.commits == [("s1", "person.abi")]


async def test_an_expired_sample_is_just_retried(enrol, app, fake_hass, vid):
    app.errors["nvid-enrol-commit"] = "not_found"
    await _start(enrol, fake_hass)
    reply = await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1))
    assert reply.startswith("Sorry, once more please")
    assert enrol.status()["state"] == "active"


async def test_a_wrong_token_stops_the_session_with_the_message(enrol, app, fake_hass, vid):
    app.token = "rotated"
    await _start(enrol, fake_hass)
    reply = await enrol.async_handle_turn(fake_hass, None, DEVICE, _res(vid, n=1))
    assert reply == "Sorry, I couldn't save your voice. Check the Voice ID card in Nova."
    st = enrol.status()
    assert st["state"] == "error" and "token doesn't match" in st["message"]


@pytest.mark.parametrize("person,satellite,code", [
    ("person.nobody", "assist_satellite.kitchen", "bad_person"),
    ("light.kitchen", "assist_satellite.kitchen", "bad_person"),
    ("person.abi", "assist_satellite.gone", "bad_satellite"),
    ("person.abi", "media_player.kitchen", "bad_satellite"),
])
async def test_start_checks_the_person_and_satellite(enrol, app, fake_hass, person, satellite, code):
    with pytest.raises(enrol.EnrolError) as err:
        await enrol.async_start(fake_hass, None, person, satellite)
    assert err.value.code == code
    assert fake_hass.service_calls == []


async def test_start_needs_the_token(enrol, app, fake_hass, load, monkeypatch):
    store = load("voice_id_store")

    async def _none(hass):
        return ""
    monkeypatch.setattr(store, "async_get_token", _none)
    with pytest.raises(enrol.EnrolError) as err:
        await _start(enrol, fake_hass)
    assert err.value.code == "no_token"


def test_eight_sentences_in_every_language(enrol):
    for lang, items in enrol.SENTENCES.items():
        assert len(items) == 8 and len(set(items)) == 8, lang
        assert all(not s.endswith("?") for s in items), lang
        assert all(4 <= len(s.split()) <= 12 for s in items), lang
    for key, table in enrol.PHRASES.items():
        assert set(table) == set(enrol.SENTENCES), key
