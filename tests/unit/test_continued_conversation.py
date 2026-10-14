"""Fresh behaviour tests for continued_conversation.py: when a reply keeps
the satellite mic open, the speaker-aware reopen, multi-satellite handoff,
and the one-shot conversation carry-over after a Nova-driven reopen.

Config and routing are swapped through sys.modules entries for the `jc`
package; asyncio.sleep is replaced with a recorder so nothing waits."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


@pytest.fixture
def cc(load):
    m = load("continued_conversation")
    m._REOPENED.clear()
    yield m
    m._REOPENED.clear()


def _config(cc, monkeypatch, values=None, broken=False):
    pkg = cc.__name__.rsplit(".", 1)[0]
    values = values or {}

    def get(key, default=None):
        if broken:
            raise RuntimeError("config unreadable")
        return values.get(key, default)
    monkeypatch.setitem(sys.modules, f"{pkg}.nova_config", types.SimpleNamespace(get=get))


def _routing(cc, monkeypatch, *, area_of=None, occupied=(), sats=None, busy=None, broken=False):
    pkg = cc.__name__.rsplit(".", 1)[0]
    area_of, sats, busy = area_of or {}, sats or {}, busy or {}

    def entity_area(hass, ent):
        if broken:
            raise RuntimeError("registry gone")
        return area_of.get(ent)
    ns = types.SimpleNamespace(
        entity_area=entity_area,
        is_area_occupied=lambda hass, a: busy.get(a, False),
        currently_occupied_areas=lambda hass: list(occupied),
        satellites_in_area=lambda hass, a: list(sats.get(a, [])))
    monkeypatch.setitem(sys.modules, f"{pkg}.audio_routing", ns)


# ── should_continue ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Which lamp did you mean?", "  Ready?  ", "?",
    "I can dim it. Shall I?", "SHOULD I turn it off.", "Would you like the fan on.",
    "Let me know when you are back.", "Do you want me to keep it running.",
    "Which would you prefer, the red or the blue.",
    "Which one would you prefer.", "I could, if you want me to."])
def test_invitations_continue(cc, text):
    assert cc.should_continue(text) is True


@pytest.mark.parametrize("text", [
    "", "   ", None, "The heater is on.", "Why? Because the window was open.",
    "Shall it rain tomorrow, the forecast says no.", "Ready?\"",
    "Done — lights off.", "I wanted me tonight."])
def test_statements_do_not(cc, text):
    assert cc.should_continue(text) is False


# ── flags ───────────────────────────────────────────────────────────────────

def test_flag_defaults(cc, monkeypatch):
    _config(cc, monkeypatch)
    assert cc.enabled() is False
    assert cc.speaker_reopen_enabled() is True
    assert cc._multi_satellite_enabled() is False


def test_flags_follow_config_truthiness(cc, monkeypatch):
    _config(cc, monkeypatch, {"continued_conversation_enabled": "false",
                              "continued_conversation_speaker_reopen": 0,
                              "continued_conversation_multi_satellite": 1})
    assert cc.enabled() is True                       # non-empty string is truthy
    assert cc.speaker_reopen_enabled() is False
    assert cc._multi_satellite_enabled() is True


def test_flags_fall_back_when_config_breaks(cc, monkeypatch):
    _config(cc, monkeypatch, broken=True)
    assert cc.enabled() is False
    assert cc.speaker_reopen_enabled() is True
    assert cc._multi_satellite_enabled() is False


# ── satellite_for_device ────────────────────────────────────────────────────

def _registry(monkeypatch, entries, broken=False):
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def async_get(hass):
        if broken:
            raise RuntimeError("no registry")
        return types.SimpleNamespace(entities={e.entity_id: e for e in entries})
    monkeypatch.setattr(er, "async_get", async_get)


def _ent(entity_id, device_id):
    return types.SimpleNamespace(entity_id=entity_id, device_id=device_id,
                                 domain=entity_id.split(".")[0])


def test_satellite_lookup(cc, fake_hass, monkeypatch):
    _registry(monkeypatch, [_ent("media_player.dev1", "dev1"),
                            _ent("assist_satellite.dev1", "dev1"),
                            _ent("assist_satellite.dev2", "dev2")])
    assert cc.satellite_for_device(fake_hass, "dev1") == "assist_satellite.dev1"
    assert cc.satellite_for_device(fake_hass, "dev2") == "assist_satellite.dev2"
    assert cc.satellite_for_device(fake_hass, "dev3") is None


def test_satellite_lookup_blank_or_broken(cc, fake_hass, monkeypatch):
    _registry(monkeypatch, [_ent("assist_satellite.dev1", "dev1")], broken=True)
    assert cc.satellite_for_device(fake_hass, "dev1") is None
    assert cc.satellite_for_device(fake_hass, "") is None
    assert cc.satellite_for_device(fake_hass, None) is None


# ── _speech_seconds ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, secs", [
    ("", 1.5), (None, 1.5), ("x" * 6, 1.5), ("x" * 120, 10.8), ("x" * 1000, 20.0)])
def test_speech_estimate(cc, text, secs):
    assert cc._speech_seconds(text) == pytest.approx(secs)


# ── _pick_reopen_target ─────────────────────────────────────────────────────

SAT = "assist_satellite.hall"


def test_handoff_off_stays(cc, monkeypatch):
    _config(cc, monkeypatch)
    _routing(cc, monkeypatch, area_of={SAT: "hall"}, occupied=["den"],
             sats={"den": ["assist_satellite.den"]})
    assert cc._pick_reopen_target(None, SAT) == SAT


def test_handoff_to_first_occupied_room_with_a_satellite(cc, monkeypatch):
    _config(cc, monkeypatch, {"continued_conversation_multi_satellite": True})
    _routing(cc, monkeypatch, area_of={SAT: "hall"},
             occupied=["hall", "porch", "den", "study"],
             sats={"den": ["assist_satellite.den", "assist_satellite.den2"],
                   "study": ["assist_satellite.study"], "hall": [SAT]})
    assert cc._pick_reopen_target(None, SAT) == "assist_satellite.den"


@pytest.mark.parametrize("kw", [
    dict(area_of={}, occupied=["den"], sats={"den": ["assist_satellite.den"]}),
    dict(area_of={SAT: "hall"}, busy={"hall": True}, occupied=["den"],
         sats={"den": ["assist_satellite.den"]}),
    dict(area_of={SAT: "hall"}, occupied=["den"], sats={}),
    dict(area_of={SAT: "hall"}, occupied=[], sats={"den": ["assist_satellite.den"]}),
    dict(broken=True),
])
def test_handoff_stays_when_ambiguous(cc, monkeypatch, kw):
    _config(cc, monkeypatch, {"continued_conversation_multi_satellite": True})
    _routing(cc, monkeypatch, **kw)
    assert cc._pick_reopen_target(None, SAT) == SAT


# ── _reopen_after_speaker ───────────────────────────────────────────────────

@pytest.fixture
def sleeps(monkeypatch):
    rec = types.SimpleNamespace(calls=[], hook=None)

    async def fake_sleep(seconds):
        rec.calls.append(seconds)
        if rec.hook:
            rec.hook(len(rec.calls))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return rec


SPK = "media_player.hall_speaker"


async def test_waits_for_speaker_to_finish(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    fake_hass.states.set(SPK, "buffering")
    sleeps.hook = lambda n: fake_hass.states.set(SPK, "idle") if n == 4 else None
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "Which one?")
    assert sleeps.calls == [0.25, 0.25, 0.25, 0.25, 0.5]
    assert fake_hass.service_calls == [
        ("assist_satellite", "start_conversation", {"entity_id": SAT, "start_message": ""})]


async def test_speaker_disappearing_ends_wait(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    fake_hass.states.set(SPK, "playing")
    sleeps.hook = lambda n: fake_hass.states.remove(SPK) if n == 2 else None
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?")
    assert sleeps.calls == [0.25, 0.25, 0.5]
    assert len(fake_hass.service_calls) == 1


async def test_speaker_starting_late_in_phase_one(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    fake_hass.states.set(SPK, "idle")

    def hook(n):
        if n == 5:
            fake_hass.states.set(SPK, "playing")
        if n == 7:
            fake_hass.states.set(SPK, "paused")
    sleeps.hook = hook
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?")
    assert sleeps.calls == [0.25] * 7 + [0.5]


async def test_speaker_never_plays_uses_estimate(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    text = "x" * 60
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, text)    # no state at all
    assert sleeps.calls == [0.25] * 16 + [cc._speech_seconds(text)]
    assert fake_hass.service_calls[-1][1] == "start_conversation"


@pytest.mark.parametrize("done", sorted({"idle", "paused", "standby", "off",
                                         "unavailable", "unknown"}))
async def test_every_done_state_ends_wait(cc, fake_hass, sleeps, monkeypatch, done):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    fake_hass.states.set(SPK, "playing")
    sleeps.hook = lambda n: fake_hass.states.set(SPK, done) if n == 2 else None
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?")
    assert sleeps.calls == [0.25, 0.25, 0.5]


def test_state_sets(cc):
    assert cc._PLAYING_STATES == {"playing", "buffering"}
    assert cc._DONE_STATES == {"idle", "paused", "standby", "off", "unavailable", "unknown"}


async def test_follow_uses_handoff_target_and_held_does_not(cc, fake_hass, sleeps, monkeypatch):
    asked = []

    def pick(hass, sat):
        asked.append(sat)
        return "assist_satellite.other"
    monkeypatch.setattr(cc, "_pick_reopen_target", pick)
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?")
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?", follow=False)
    assert asked == [SAT]
    assert [c[2]["entity_id"] for c in fake_hass.service_calls] == [
        "assist_satellite.other", SAT]


async def test_service_failure_is_swallowed(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: sat)
    seen = {}

    async def boom(domain, service, data=None, blocking=True, **kw):
        seen["blocking"] = blocking
        raise RuntimeError("satellite offline")
    monkeypatch.setattr(fake_hass.services, "async_call", boom)
    await cc._reopen_after_speaker(fake_hass, SAT, SPK, "ok?")
    assert seen["blocking"] is False


# ── schedule_reopen ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("sat, spk", [("", SPK), (SAT, ""), (None, SPK), (SAT, None)])
def test_schedule_needs_both_ids(cc, fake_hass, sat, spk):
    assert cc.schedule_reopen(fake_hass, sat, spk, "hi") is False
    assert fake_hass._tasks == []


async def test_schedule_creates_a_task_that_reopens(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: "assist_satellite.moved")
    assert cc.schedule_reopen(fake_hass, SAT, SPK, "Shall I?") is True
    assert len(fake_hass._tasks) == 1 and fake_hass.service_calls == []
    await fake_hass.drain()
    assert fake_hass.service_calls[-1][2]["entity_id"] == "assist_satellite.moved"


async def test_schedule_passes_follow_false(cc, fake_hass, sleeps, monkeypatch):
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: "assist_satellite.moved")
    assert cc.schedule_reopen(fake_hass, SAT, SPK, "Yes or no?", follow=False) is True
    await fake_hass.drain()
    assert fake_hass.service_calls[-1][2]["entity_id"] == SAT


# ── remember / take ─────────────────────────────────────────────────────────

def test_ttl_constant(cc):
    assert cc.REOPEN_TTL_SECONDS == 60.0


def test_remember_then_take_once(cc):
    cc.remember_reopen("devA", "conv-x", now=10.0)
    assert cc._REOPENED == {"devA": ("conv-x", 70.0)}
    assert cc.take_reopened("devA", now=69.9) == "conv-x"
    assert cc.take_reopened("devA", now=20.0) is None


def test_expiry_is_strict_and_consumes(cc):
    cc.remember_reopen("devA", "conv-x", now=10.0)
    assert cc.take_reopened("devA", now=70.0) is None
    assert cc._REOPENED == {}


def test_keys_are_per_device_and_stringified(cc):
    cc.remember_reopen(7, 99, now=0.0)
    assert cc._REOPENED == {"7": ("99", 60.0)}
    assert cc.take_reopened("other", now=1.0) is None
    assert cc.take_reopened("7", now=1.0) == "99"


def test_blank_inputs(cc):
    cc.remember_reopen("", "c", now=0.0)
    cc.remember_reopen("d", "", now=0.0)
    cc.remember_reopen(None, "c", now=0.0)
    assert cc._REOPENED == {}
    cc.remember_reopen("d", "c", now=0.0)
    assert cc.take_reopened("", now=1.0) is None
    assert cc.take_reopened(None, now=1.0) is None
    assert "d" in cc._REOPENED                      # blank take removes nothing


def test_newer_reopen_replaces_older(cc):
    cc.remember_reopen("devA", "first", now=0.0)
    cc.remember_reopen("devA", "second", now=30.0)
    assert cc.take_reopened("devA", now=80.0) == "second"


def test_monotonic_clock_by_default(cc, monkeypatch):
    import time
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    cc.remember_reopen("devA", "c")
    assert cc._REOPENED["devA"] == ("c", 1060.0)
    clock["t"] = 1059.0
    assert cc.take_reopened("devA") == "c"
    cc.remember_reopen("devA", "c")
    clock["t"] = 1200.0
    assert cc.take_reopened("devA") is None
