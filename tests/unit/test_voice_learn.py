"""Learning voices from everyday use (8.38.0, voice_learn.py).

Commits go to a fake app (tests/voice_id_fakes.VoiceApp) over real Wyoming.
Presence, faces and rooms are set per test through identity's own helpers.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from voice_id_fakes import TOKEN, VoiceApp

BOB = "person.bob"
DEVICE = "dev-kitchen"


@pytest.fixture
def env(load, fake_hass, monkeypatch):
    learn = load("voice_learn")
    idn = load("identity")
    vid = load("voice_id")
    vc = load("voice_confirm")
    cfg = {"voice_id_identity": True, "voice_id_learning": True}
    monkeypatch.setattr(idn, "_cfg", lambda key, default: cfg.get(key, default))
    monkeypatch.setattr(learn, "_cfg", lambda key, default: cfg.get(key, default))
    signals = {"home": [], "faces": {}, "room": {}, "prox": {}}
    monkeypatch.setattr(idn, "_home_people", lambda hass: list(signals["home"]))
    monkeypatch.setattr(idn, "_face_votes", lambda hass, now, names=None: dict(signals["faces"]))
    monkeypatch.setattr(idn, "_room_votes",
                        lambda hass, area, now: dict(signals["room"]) if area == "kitchen" else {})
    monkeypatch.setattr(idn, "_proximity_votes", lambda hass, area: dict(signals["prox"]))
    satellites = {DEVICE, "dev-office"}
    monkeypatch.setattr(vc, "is_voice_satellite_device",
                        lambda hass, device_id, strict=False: device_id in satellites)
    monkeypatch.setattr(learn, "_satellite_area", lambda hass, device_id: "kitchen")
    version = {"v": (0, 3, 0)}
    monkeypatch.setattr(learn, "_app_version", lambda entry: version["v"])
    fake_hass.states.async_set(BOB, "home", {"friendly_name": "Bob Jones"})
    fake_hass.states.async_set("person.alice", "home", {"friendly_name": "Alice"})
    lines = []
    ws_log = load("ws_log")
    monkeypatch.setattr(ws_log, "nova_log", lambda cat, msg: lines.append((cat, msg)))
    learn.clear()
    learn.reset()
    yield types.SimpleNamespace(learn=learn, idn=idn, vid=vid, cfg=cfg, signals=signals,
                                hass=fake_hass, version=version, lines=lines,
                                satellites=satellites)
    learn.clear()


@pytest.fixture
async def app(load, monkeypatch):
    store = load("voice_id_store")
    async with VoiceApp() as fake:
        monkeypatch.setattr(store, "get_uri", lambda hass, entry: fake.uri)

        async def _token(hass):
            return TOKEN
        monkeypatch.setattr(store, "async_get_token", _token)
        yield fake


def _voice(env, **over):
    block = {"v": 1, "status": "known", "person": BOB, "score": 0.82, "speech_s": 2.5,
             "sample_id": "s1", "model": "m", "ms": 40}
    block.update(over)
    return env.vid.parse_block({k: v for k, v in block.items() if v is not None})


def _cand(env, device=DEVICE, method="sole_occupant+voice", **over):
    ident = env.idn.Identification("Bob Jones", 0.9, method, {"Bob Jones": 1.5})
    return env.learn.Candidate(device_id=device, voice=_voice(env, **over), ident=ident)


def _bob_alone(env):
    env.signals["home"][:] = ["Bob Jones"]


async def test_all_conditions_met_commits_once_as_learned(env, app):
    _bob_alone(env)
    out = await env.learn.async_learn(env.hass, None, _cand(env))
    assert out.learned is True
    assert app.commits == [("s1", BOB)] and app.sources == ["learned"]
    assert app.management[0].data == {"token": TOKEN, "sample_id": "s1", "person": BOB,
                                      "source": "learned"}
    assert env.lines == [("VOICE", f"person={BOB} device={DEVICE} + learned")]


@pytest.mark.parametrize("agree", [
    {"faces": {"bob_jones": 0.7}, "home": ["Bob Jones", "Alice"]},     # a face, Alice only 0.15
    {"room": {"bob_jones": 0.75}, "home": ["Bob Jones", "Alice"]},     # only one in the room
])
async def test_a_face_or_the_room_can_agree_instead(env, app, agree):
    env.signals.update({k: v for k, v in agree.items()})
    assert (await env.learn.async_learn(env.hass, None, _cand(env))).learned is True


@pytest.mark.parametrize("setup,reason", [
    (lambda e: e.cfg.update(voice_id_identity=False), "off"),
    (lambda e: e.cfg.update(voice_id_learning=False), "off"),
    (lambda e: e.version.update(v=(0, 2, 0)), "app too old"),
    (lambda e: e.version.update(v=()), "app too old"),
    (lambda e: e.signals.update(home=[]), "no other signal agrees"),
    (lambda e: e.signals.update(home=["Bob Jones", "Alice"]), "no other signal agrees"),
    (lambda e: e.signals.update(home=["Alice"]), "no other signal agrees"),
    (lambda e: e.signals.update(home=["Bob Jones", "Alice"], faces={"bob_jones": 0.7,
                                                                     "alice": 0.2}),
     "someone else is likely"),
    (lambda e: e.signals.update(home=["Bob Jones", "Alice"], faces={"bob_jones": 0.7},
                                prox={"alice": 0.35}), "someone else is likely"),
    (lambda e: e.signals.update(home=["Bob Jones", "Alice"], prox={"bob_jones": 0.35},
                                room={"bob_jones": 0.3, "alice": 0.3}), "no other signal agrees"),
])
async def test_each_condition_on_its_own_blocks_the_commit(env, app, setup, reason):
    _bob_alone(env)
    setup(env)
    out = await env.learn.async_learn(env.hass, None, _cand(env))
    assert (out.learned, out.reason) == (False, reason)
    assert app.management == [] and env.lines == []


@pytest.mark.parametrize("over,reason", [
    ({"status": "not_sure", "person": None}, "not known"),
    ({"score": 0.69}, "low score"),
    ({"speech_s": 1.99}, "short speech"),
    ({"sample_id": None}, "no sample"),
])
async def test_the_result_must_be_sure_and_long_enough(env, app, over, reason):
    _bob_alone(env)
    out = await env.learn.async_learn(env.hass, None, _cand(env, **over))
    assert (out.learned, out.reason) == (False, reason) and app.management == []


async def test_the_thresholds_live_in_const(load):
    const = load("const")
    assert (const.VOICE_LEARN_MIN_SCORE, const.VOICE_LEARN_MIN_SPEECH_S) == (0.70, 2.0)


async def test_a_carried_turn_never_learns(env, app):
    _bob_alone(env)
    out = await env.learn.async_learn(env.hass, None, _cand(env, method="sole_occupant+voice_carry"))
    assert out.reason == "carried" and app.management == []


@pytest.mark.parametrize("reason", ["confirmation answer", "offer answer"])
async def test_a_blocked_turn_never_learns(env, app, reason):
    _bob_alone(env)
    env.learn.offer(DEVICE, _voice(env), env.idn.Identification("Bob Jones", 0.9, "voice"))
    env.learn.block(reason)
    out = await env.learn.async_learn(env.hass, None, env.learn.take())
    assert out.reason == reason and app.management == []


async def test_a_device_that_is_not_a_satellite_never_learns(env, app):
    _bob_alone(env)
    out = await env.learn.async_learn(env.hass, None, _cand(env, device="dev-phone"))
    assert out.reason == "not a satellite" and app.management == []


async def test_a_refused_sample_is_logged_without_words_and_never_sent_again(env, app):
    _bob_alone(env)
    app.refuse_learned = True
    out = await env.learn.async_learn(env.hass, None, _cand(env))
    assert (out.learned, out.reason) == (False, "refused")
    assert env.lines == [("VOICE", f"person={BOB} device={DEVICE} learn refused (refused)")]
    app.refuse_learned = False
    again = await env.learn.async_learn(env.hass, None, _cand(env))
    assert again.reason == "already sent"
    assert len(app.management) == 1
    assert env.learn.was_sent("s1")          # the recording path checks this too


async def test_an_error_reply_is_debug_only(env, app):
    _bob_alone(env)
    app.token = "rotated"
    out = await env.learn.async_learn(env.hass, None, _cand(env))
    assert (out.learned, out.reason) == (False, "unauthorised")
    assert env.lines == []


async def test_one_learned_commit_in_flight_per_person(env, app, load, monkeypatch):
    _bob_alone(env)
    gate = asyncio.Event()
    real = env.vid.enrol_commit

    async def slow(*a, **k):
        await gate.wait()
        return await real(*a, **k)
    monkeypatch.setattr(env.vid, "enrol_commit", slow)
    first = asyncio.ensure_future(env.learn.async_learn(env.hass, None, _cand(env)))
    await asyncio.sleep(0)
    second = await env.learn.async_learn(env.hass, None, _cand(env, sample_id="s2"))
    assert second.reason == "in flight"
    gate.set()
    assert (await first).learned is True
    assert app.commits == [("s1", BOB)]


async def test_two_satellites_at_once_never_swap(env, app):
    """Each turn keeps its own candidate (a ContextVar in its own task)."""
    env.signals["home"][:] = ["Bob Jones"]
    gate = asyncio.Event()

    async def turn(device, person, sample, wait):
        env.learn.reset()
        voice = _voice(env, person=person, sample_id=sample)
        env.learn.offer(device, voice, env.idn.Identification("x", 0.9, "voice"))
        if wait:
            await gate.wait()
        else:
            gate.set()
        return env.learn.take()

    a, b = await asyncio.gather(turn(DEVICE, BOB, "sa", True),
                                turn("dev-office", "person.alice", "sb", False))
    assert (a.device_id, a.voice.person, a.voice.sample_id) == (DEVICE, BOB, "sa")
    assert (b.device_id, b.voice.person, b.voice.sample_id) == ("dev-office", "person.alice", "sb")


def test_the_breakdown_matches_resolve_without_voice(env):
    env.cfg["voice_id_identity"] = False
    env.signals.update(home=["Bob Jones", "Alice"], faces={"bob_jones": 0.5},
                       prox={"alice": 0.35})
    breakdown = env.idn.non_voice_breakdown(env.hass)
    resolved = env.idn.resolve(env.hass, device_id=DEVICE)
    totals = {k: round(sum(v.values()), 3) for k, v in breakdown.items()}
    assert totals == {env.idn.normalize(k): v for k, v in resolved.candidates.items()}
    assert breakdown["bob_jones"] == {"home_prior": 0.15, "face": 0.5}


def test_the_breakdown_never_uses_voice(env):
    env.cfg["identity_voice_fingerprint"] = True
    env.idn.register_voice_provider(lambda hass, dev: {"Bob Jones": 1.0})
    try:
        assert env.idn.non_voice_breakdown(env.hass) == {}
    finally:
        env.idn.register_voice_provider(None)


async def test_the_recording_path_never_resends_a_sample(env, app, load, monkeypatch):
    enrol = load("voice_enrol")
    enrol.clear()
    env.learn.mark_sent("s9")
    enrol._SESSION = enrol.Session(device_id=DEVICE, satellite="assist_satellite.k",
                                   person=BOB, name="Bob", lang="en",
                                   expires_at=enrol._now() + 300)
    store = load("voice_id_store")
    reply = await enrol.async_handle_turn(env.hass, None, DEVICE, _voice(env, sample_id="s9"))
    assert reply.startswith("Sorry, once more please")
    assert app.management == []
    enrol.clear()
    assert store is not None
