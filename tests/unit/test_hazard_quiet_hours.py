"""Quiet-hours delivery policy for weather warnings and legacy hazards."""
from __future__ import annotations

from datetime import datetime
import types

import pytest

from fakes import FakeHass


LEVELS = ("yellow", "orange", "red")
RANK = {level: i for i, level in enumerate(LEVELS)}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("night_level", (*LEVELS, "off"))
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("push_level", LEVELS)
def test_speak_policy_all_level_night_quiet_and_push_combinations(
        load, level, night_level, quiet, push_level):
    hw = load("hazard_warnings")
    pushed = RANK[level] >= RANK[push_level]
    if quiet:
        expected = pushed and night_level != "off" and RANK[level] >= RANK.get(night_level, 99)
    else:
        expected = pushed and RANK[level] >= RANK["orange"]
    assert hw.speak_allowed(level, quiet, "orange", push_level, night_level) is expected


@pytest.mark.parametrize(("now", "start", "end", "expected"), [
    (datetime(2026, 10, 6, 23, 0), "22:00", "07:00", True),
    (datetime(2026, 10, 6, 12, 0), "22:00", "07:00", False),
    (datetime(2026, 10, 6, 13, 0), "09:00", "17:00", True),
    (datetime(2026, 10, 6, 18, 0), "09:00", "17:00", False),
    (datetime(2026, 10, 6, 23, 0), "bad", "07:00", False),
    (datetime(2026, 10, 6, 12, 0), "12:00", "12:00", False),
])
def test_shared_quiet_window_edges(load, monkeypatch, now, start, end, expected):
    sd = load("sleep_detection")
    monkeypatch.setattr(sd.dt_util, "now", lambda: now)
    assert sd._in_quiet_hours(start, end) is expected


def _hass():
    hass = FakeHass()
    hass.config = types.SimpleNamespace(
        country="IE", latitude=52.85, longitude=-8.98,
        time_zone="Europe/Dublin", language="en",
    )
    return hass


@pytest.fixture
def delivery(load, monkeypatch):
    hm = load("hazard_monitor")
    hw = load("hazard_warnings")
    nc = load("nova_config")
    cc = load("cognitive_core")
    tts = load("tts_helper")
    audio = load("audio_routing")
    config = {"hazard_monitor_enabled": True}
    monkeypatch.setattr(nc, "get", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(hm, "_STORE", None)
    monkeypatch.setattr(hm, "_LAST_WARNINGS", {})
    monkeypatch.setattr(hw, "load_store", lambda now: {})
    monkeypatch.setattr(hw, "save_store", lambda store, now: True)
    pushed: list[tuple[str, str]] = []
    spoken: list[str] = []

    async def notify(hass, cfg, text, action, *args, **kwargs):
        pushed.append((text, action))

    async def announce(hass, text, tts_entity, speakers, **kwargs):
        spoken.append(text)

    monkeypatch.setattr(cc, "_notify_all_devices", notify)
    monkeypatch.setattr(tts, "find_best_tts_entity", lambda hass: "tts.test")
    monkeypatch.setattr(audio, "broadcast_target", lambda *args, **kwargs: ["media_player.all"])
    monkeypatch.setattr(tts, "async_announce", announce)
    return types.SimpleNamespace(
        hm=hm, config=config, pushed=pushed, spoken=spoken, monkeypatch=monkeypatch,
    )


def _warning(level: str) -> dict:
    return {
        "id": f"{level}-1", "msg_type": "Alert", "refs": [],
        "source": "met_eireann", "source_label": "Met Éireann",
        "type": "Wind", "level": level,
        "onset": "2099-09-09T12:00:00+01:00",
        "expiry": "2099-09-10T12:00:00+01:00",
        "headline": f"Status {level.title()} - Wind warning for Clare",
        "description": "Prepare.", "areas": ["Clare"], "area_keys": ["EI03"],
    }


async def _warning_run(env, level: str, *, quiet: bool):
    async def fetched(hass):
        return {"met_eireann": (True, [_warning(level)])}

    env.monkeypatch.setattr(env.hm, "_fetch_warnings", fetched)
    env.monkeypatch.setattr(env.hm, "_in_quiet_hours", lambda hass: quiet)
    await env.hm.periodic_check(_hass())


async def test_orange_at_night_uses_the_real_delivery_path_for_push_only(delivery):
    await _warning_run(delivery, "orange", quiet=True)
    assert len(delivery.pushed) == 1 and delivery.spoken == []


async def test_red_stays_spoken_at_night_by_default_through_real_delivery(delivery):
    await _warning_run(delivery, "red", quiet=True)
    assert len(delivery.pushed) == 1 and len(delivery.spoken) == 1
    assert delivery.spoken[0].startswith("Met Éireann Red wind warning")


async def test_night_level_off_silences_red_through_real_delivery(delivery):
    delivery.config["hazard_night_speak_level"] = "off"
    await _warning_run(delivery, "red", quiet=True)
    assert len(delivery.pushed) == 1 and delivery.spoken == []


async def test_outside_quiet_hours_keeps_orange_speech(delivery):
    await _warning_run(delivery, "orange", quiet=False)
    assert len(delivery.pushed) == 1 and len(delivery.spoken) == 1


@pytest.mark.parametrize(("quiet", "spoken_count"), [(False, 3), (True, 0)])
async def test_all_legacy_paths_are_phone_only_in_quiet_hours(
        delivery, quiet, spoken_count):
    hm = delivery.hm
    delivery.config.update({
        "hazard_met_eireann_on": False,
        "hazard_quakes_on": True,
        "hazard_weather_on": True,
        "hazard_disasters_on": True,
    })
    delivery.monkeypatch.setattr(hm, "_in_quiet_hours", lambda hass: quiet)

    async def quake(*args):
        return [{"mag": 5.0, "dist_km": 20, "place": "near home"}]

    async def weather(*args):
        return [{"severity": "Extreme", "event": "Storm", "area": "Home",
                 "instruction": "Stay inside"}]

    async def disaster(*args):
        return [{"title": "Wildfire", "category": "Wildfires", "dist_km": 30}]

    delivery.monkeypatch.setattr(hm, "_check_earthquakes", quake)
    delivery.monkeypatch.setattr(hm, "_check_weather", weather)
    delivery.monkeypatch.setattr(hm, "_check_disasters", disaster)
    await hm.periodic_check(_hass())
    assert len(delivery.pushed) == 3 and len(delivery.spoken) == spoken_count


def test_quiet_hours_read_live_runtime_and_fail_open(load, monkeypatch):
    hm = load("hazard_monitor")
    sd = load("sleep_detection")
    seen = []
    monkeypatch.setattr(hm, "_runtime", lambda hass, key, default: {
        "observer_quiet_start": "21:30", "observer_quiet_end": "06:15",
    }.get(key, default))
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda start, end:
                        seen.append((start, end)) or True)
    assert hm._in_quiet_hours(_hass()) is True
    assert seen == [("21:30", "06:15")]
    monkeypatch.setattr(sd, "_in_quiet_hours", lambda *args: (_ for _ in ()).throw(ValueError()))
    assert hm._in_quiet_hours(_hass()) is False


def test_quiet_hours_runtime_uses_live_value_and_missing_key_default(load, monkeypatch):
    hm = load("hazard_monitor")
    runtime = load("runtime")
    monkeypatch.setattr(runtime, "domain_runtime_config",
                        lambda hass: {"observer_quiet_start": "21:30"})
    assert hm._runtime(_hass(), "observer_quiet_start", "22:00") == "21:30"
    assert hm._runtime(_hass(), "observer_quiet_end", "07:00") == "07:00"
