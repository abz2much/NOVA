"""Proactive offers are bound to the room that heard them (8.39.1).

An offer can only be answered by voice, from a satellite in a room where it
was spoken, within a minute, and a yes runs only the offer it was given to.
Held voice confirmations carry a request id and check the answering user.
"""
import sys
import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401  (fixtures)


@pytest.fixture
def rt(load):
    return load("core_runtime")


@pytest.fixture
def house(monkeypatch, load):
    """Two rooms: a lounge satellite paired with the lounge speaker, and a
    bedroom satellite paired with the bedroom speaker."""
    areas = {"assist_satellite.lounge": "lounge", "media_player.lounge": "lounge",
             "assist_satellite.bedroom": "bedroom", "media_player.bedroom": "bedroom"}
    devices = {"assist_satellite.lounge": "dev_lounge",
               "assist_satellite.bedroom": "dev_bedroom"}
    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: types.SimpleNamespace(
        async_get=lambda eid: (types.SimpleNamespace(device_id=devices[eid])
                               if eid in devices else None)))
    monkeypatch.setattr(dr, "async_get", lambda hass: types.SimpleNamespace(
        async_get=lambda did: types.SimpleNamespace(area_id=None)))
    monkeypatch.setattr(load("voice_confirm"), "_satellite_pairings",
                        lambda hass: {"assist_satellite.lounge": "media_player.lounge",
                                      "assist_satellite.bedroom": "media_player.bedroom"})
    monkeypatch.setattr(load("audio_routing"), "entity_area",
                        lambda hass, eid: areas.get(eid))
    groups = {"media_player.everywhere": types.SimpleNamespace(
        attributes={"group_members": ["media_player.lounge", "media_player.bedroom"]})}
    return types.SimpleNamespace(states=types.SimpleNamespace(
        get=groups.get,
        async_all=lambda domain: [types.SimpleNamespace(entity_id=e) for e in devices]
        if domain == "assist_satellite" else []))


def _spoken_offer(cc, rt, speakers=("media_player.lounge",), at=100.0):
    offer = rt._bindable_offer({"type": "proactive_lights", "offer_key": "dark:lounge",
                                "action_data": {}})
    cc._CORE.pending_offer = offer
    rt.offer_spoken(offer, list(speakers), now=at)
    return offer


def test_only_the_room_that_heard_the_offer_can_answer_it(cc, rt, house):
    offer = _spoken_offer(cc, rt)
    assert rt.pending_offer_for(house, "dev_lounge", now=110.0) is offer
    assert rt.pending_offer_for(house, "dev_bedroom", now=110.0) is None
    assert rt.pending_offer_for(house, None, now=110.0) is None          # Chat or typed
    assert cc._CORE.pending_offer is offer                                 # another room leaves it alone


def test_an_offer_expires_a_minute_after_it_was_spoken(cc, rt, house):
    _spoken_offer(cc, rt, at=100.0)
    assert rt.pending_offer_for(house, "dev_lounge", now=160.0) is not None
    assert rt.pending_offer_for(house, "dev_lounge", now=160.5) is None
    assert cc._CORE.pending_offer is None


def test_an_offer_that_was_never_spoken_cannot_be_answered(cc, rt, house):
    cc._CORE.pending_offer = rt._bindable_offer({"type": "proactive_lights"})
    assert rt.pending_offer_for(house, "dev_lounge", now=0.0) is None


def test_only_the_pending_offer_can_be_marked_spoken_or_dropped(cc, rt):
    current = rt._bindable_offer({"type": "a"})
    stale = rt._bindable_offer({"type": "b"})
    cc._CORE.pending_offer = current
    rt.offer_spoken(stale, ["media_player.lounge"], now=1.0)
    rt.offer_not_spoken(stale)
    assert stale["spoken_at"] is None and cc._CORE.pending_offer is current
    rt.offer_not_spoken(current)
    assert cc._CORE.pending_offer is None


def test_a_broken_room_check_fails_closed(cc, rt, house, monkeypatch, load):
    _spoken_offer(cc, rt)
    monkeypatch.setattr(load("audio_routing"), "entity_area",
                        lambda hass, eid: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(load("voice_confirm"), "_satellite_pairings", lambda hass: None)
    assert rt.pending_offer_for(house, "dev_lounge", now=110.0) is None


def test_a_pairing_alone_is_enough_without_areas(cc, rt, house, monkeypatch, load):
    _spoken_offer(cc, rt)
    monkeypatch.setattr(load("audio_routing"), "entity_area", lambda hass, eid: None)
    assert rt.pending_offer_for(house, "dev_lounge", now=110.0) is not None
    assert rt.pending_offer_for(house, "dev_bedroom", now=110.0) is None


def test_a_room_alone_is_enough_without_a_pairing(cc, rt, house, monkeypatch, load):
    _spoken_offer(cc, rt)
    monkeypatch.setattr(load("voice_confirm"), "_satellite_pairings", lambda hass: None)
    assert rt.pending_offer_for(house, "dev_lounge", now=110.0) is not None
    assert rt.pending_offer_for(house, "dev_bedroom", now=110.0) is None


def test_an_offer_spoken_on_a_speaker_group_reaches_every_room_in_it(cc, rt, house):
    _spoken_offer(cc, rt, speakers=("media_player.everywhere",))
    assert rt.pending_offer_for(house, "dev_lounge", now=110.0) is not None
    assert rt.pending_offer_for(house, "dev_bedroom", now=110.0) is not None


def test_each_offer_gets_its_own_id(rt):
    a, b = rt._bindable_offer({}), rt._bindable_offer({})
    assert a["offer_id"] != b["offer_id"]
    assert a["heard_by"] == [] and a["spoken_at"] is None


# ── held voice confirmations ────────────────────────────────────────────────

@pytest.fixture
def pc(load):
    mod = load("pending_confirm")
    mod.clear_all()
    yield mod
    mod.clear_all()


def test_each_held_request_has_its_own_request_id(pc):
    a = pc.put("dev", pc.KIND_SCENE_SCRIPT, "scene.a", "user1", now=0.0)
    b = pc.put("dev", pc.KIND_SCENE_SCRIPT, "scene.a", "user1", now=0.0)
    assert a.request_id and b.request_id and a.request_id != b.request_id


async def test_a_different_user_cannot_answer_a_held_request(pc, monkeypatch):
    ran = []

    async def replay(hass, item, honorific):
        ran.append(item.ref)
    monkeypatch.setattr(pc, "_replay_scene_script", replay)
    pc.put("dev", pc.KIND_SCENE_SCRIPT, "scene.a", "user1")
    assert await pc.answer(None, "dev", "yes", user_id="user2") is None
    assert ran == [] and pc.get("dev") is None


async def test_the_same_user_or_an_unknown_one_can_answer(pc, monkeypatch):
    ran = []

    async def replay(hass, item, honorific):
        ran.append(item.ref)
        return "done"
    monkeypatch.setattr(pc, "_replay_scene_script", replay)
    pc.put("dev", pc.KIND_SCENE_SCRIPT, "scene.a", "user1")
    assert await pc.answer(None, "dev", "yes", user_id="user1") == "done"
    pc.put("dev", pc.KIND_SCENE_SCRIPT, "scene.b", "user1")
    assert await pc.answer(None, "dev", "yes") == "done"
    assert ran == ["scene.a", "scene.b"]
