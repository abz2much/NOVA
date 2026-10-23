"""A mode's entry mood: Movie mode dims the lights in its bound room.

These pin what apply_mode_entry sends to Home Assistant and records in the
action log, and that it never raises.
"""
import logging
import sys
import types

import pytest

from fakes import FakeHass


class _Log:
    def __init__(self):
        self.started = []
        self.executions = []
        self.minted = 0

    def new_request_id(self):
        self.minted += 1
        return "minted-1"

    def start(self, request_id, action, source, **kw):
        self.started.append((request_id, action, source, kw))
        return 77

    def set_execution(self, action_id, state, *a, **kw):
        self.executions.append((action_id, state))


@pytest.fixture
def env(load, monkeypatch):
    log = _Log()
    config = {"movie_area": "living_room", "movie_dim_pct": 15}
    areas = {}

    nc = types.ModuleType("jc.nova_config")
    nc.get = lambda key, default=None: config.get(key, default)
    ar = types.ModuleType("jc.audio_routing")
    ar.entity_area = lambda hass, eid: areas.get(eid)
    al = types.ModuleType("jc.action_log")
    al.new_request_id = log.new_request_id
    al.start = log.start
    al.set_execution = log.set_execution
    for name, mod in (("nova_config", nc), ("audio_routing", ar), ("action_log", al)):
        monkeypatch.setitem(sys.modules, f"jc.{name}", mod)

    hass = FakeHass()
    for eid, area in (("light.lamp", "living_room"), ("light.strip", "living_room"),
                      ("light.hall", "hallway"), ("light.nowhere", None)):
        hass.states.set(eid, "on")
        areas[eid] = area
    hass.states.set("switch.plug", "on")
    areas["switch.plug"] = "living_room"
    return types.SimpleNamespace(ms=load("mode_scene"), log=log, config=config,
                                 areas=areas, hass=hass)


async def test_movie_dims_the_lights_in_the_bound_room(env):
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r1", source="voice",
                                  requested_by_user_id="u1", requested_by_name="Alex")
    assert env.hass.service_calls == [
        ("light", "turn_on",
         {"entity_id": ["light.lamp", "light.strip"], "brightness_pct": 15})]
    assert env.log.started == [("r1", "mode_entry_mood", "voice", {
        "requested_by_user_id": "u1", "requested_by_name": "Alex",
        "domain": "light", "entity_id": "area:living_room", "requested_state": "15%"})]
    assert env.log.executions == [(77, "accepted")]
    assert env.log.minted == 0


async def test_a_request_id_is_minted_when_the_caller_has_none(env):
    await env.ms.apply_mode_entry(env.hass, "movie")
    assert env.log.minted == 1
    assert env.log.started[0][0] == "minted-1"
    assert env.log.started[0][2] == "chat"
    assert env.log.started[0][3]["requested_by_user_id"] is None
    assert env.log.started[0][3]["requested_by_name"] is None


async def test_a_request_id_is_minted_even_for_a_mode_with_no_mood(env):
    await env.ms.apply_mode_entry(env.hass, "sleep")
    assert env.log.minted == 1
    assert env.hass.service_calls == [] and env.log.started == []


@pytest.mark.parametrize("mode", ["normal", "sleep", "away", "", "Movie"])
async def test_only_movie_mode_has_a_mood(env, mode):
    await env.ms.apply_mode_entry(env.hass, mode, request_id="r")
    assert env.hass.service_calls == [] and env.log.started == []


@pytest.mark.parametrize("raw, sent", [
    (40, 40), ("40", 40), (150, 100), (-5, 0), (100, 100), (1, 1), (7.9, 7),
    ("abc", 15), (None, 15), ([], 15)])
async def test_the_level_is_a_whole_percent_kept_between_0_and_100(env, raw, sent):
    env.config["movie_dim_pct"] = raw
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    if sent == 0:
        assert env.hass.service_calls == [
            ("light", "turn_off", {"entity_id": ["light.lamp", "light.strip"]})]
    else:
        assert env.hass.service_calls == [
            ("light", "turn_on",
             {"entity_id": ["light.lamp", "light.strip"], "brightness_pct": sent})]
    assert env.log.started[0][3]["requested_state"] == f"{sent}%"


async def test_the_default_level_is_15_when_none_is_set(env):
    del env.config["movie_dim_pct"]
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls[0][2]["brightness_pct"] == 15


async def test_zero_turns_the_lights_off_and_still_logs_acceptance(env):
    env.config["movie_dim_pct"] = 0
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert [c[:2] for c in env.hass.service_calls] == [("light", "turn_off")]
    assert env.log.executions == [(77, "accepted")]


async def test_the_area_comes_from_the_media_player_when_none_is_named(env):
    env.config["movie_area"] = ""
    env.config["movie_media_player"] = "media_player.tv"
    env.areas["media_player.tv"] = "hallway"
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls == [
        ("light", "turn_on", {"entity_id": ["light.hall"], "brightness_pct": 15})]
    assert env.log.started[0][3]["entity_id"] == "area:hallway"


async def test_a_named_area_wins_over_the_media_player(env):
    env.config["movie_media_player"] = "media_player.tv"
    env.areas["media_player.tv"] = "hallway"
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.log.started[0][3]["entity_id"] == "area:living_room"


async def test_a_non_text_area_is_used_as_text(env):
    env.config["movie_area"] = 5
    env.areas["light.lamp"] = "5"
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls[0][2]["entity_id"] == ["light.lamp"]


@pytest.mark.parametrize("config", [
    {}, {"movie_area": ""}, {"movie_area": None, "movie_media_player": None},
    {"movie_area": "", "movie_media_player": "media_player.unknown"}])
async def test_with_no_room_nothing_happens(env, config):
    env.config.clear()
    env.config.update(config)
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls == [] and env.log.started == []


async def test_a_room_with_no_lights_is_left_alone(env):
    env.config["movie_area"] = "garage"
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls == [] and env.log.started == []


async def test_a_failing_light_service_is_swallowed_and_noted_at_debug(env, caplog):
    async def boom(*a, **kw):
        raise RuntimeError("no such service")
    env.hass.services.async_call = boom
    with caplog.at_level(logging.DEBUG, logger=env.ms.__name__):
        await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.log.executions == []
    rec = [r for r in caplog.records if r.name == env.ms.__name__]
    assert [(r.levelno, r.getMessage()) for r in rec] == [
        (logging.DEBUG, "apply_mode_entry(movie) failed: no such service")]


async def test_a_failing_action_log_is_swallowed(env, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("db locked")
    monkeypatch.setattr(sys.modules["jc.action_log"], "start", boom)
    await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    assert env.hass.service_calls == []


async def test_a_dim_is_noted_at_info(env, caplog):
    with caplog.at_level(logging.INFO, logger=env.ms.__name__):
        await env.ms.apply_mode_entry(env.hass, "movie", request_id="r")
    rec = [r for r in caplog.records if r.name == env.ms.__name__]
    assert [(r.levelno, r.getMessage()) for r in rec] == [
        (logging.INFO, "Movie mood: dimmed 2 light(s) in living_room to 15%")]

