"""Pin what the voice and history commands in ws_voice.py do today (8.7.22, tests only).

nova/voice_confirm_test, nova/say_hello, nova/get_spoken_history,
nova/list_actions and nova/repeat_spoken. nova/repeat_spoken is the riskiest:
it speaks a stored line again, and picks the speakers itself when the
original ones are gone. The decorators are stubbed to pass through, so the
handler bodies run with a recording connection and fake history, audit,
speaker and TTS seams. Tests named test_current_behaviour_* pin behaviour
that looks wrong; each says why.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


class _CtxConn(_Conn):
    def context(self, msg):
        return ("ctx", msg["id"])


@pytest.fixture
def voice(load, monkeypatch):
    # ws_bridge looks _get_entry up on jc.websocket at call time.
    _stub_ws_api(monkeypatch)
    for name in ("jc.ws_voice", "jc.websocket"):
        sys.modules.pop(name, None)
    load("websocket")
    mod = load("ws_voice")
    yield mod
    for name in ("jc.ws_voice", "jc.websocket"):
        sys.modules.pop(name, None)


def _hass(**speakers):
    hass = FakeHass()
    hass.config_entries = types.SimpleNamespace(async_entries=lambda d: [])
    for eid, state in speakers.items():
        hass.states.set(eid.replace("__", "."), state)
    return hass


async def _call(handler, hass, **msg):
    conn = _CtxConn()
    await handler(hass, conn, {"id": 4, **msg})
    return conn


def _safe(conn, code):
    assert conn.errors == [(4, code, "RuntimeError (details are in the Home Assistant log)")]


def _raise(*a, **k):
    raise RuntimeError("token=abc")


async def _araise(*a, **k):
    raise RuntimeError("token=abc")


# ── nova/repeat_spoken ──────────────────────────────────────────────────────

@pytest.fixture
def speech(load, monkeypatch):
    sh = load("spoken_history")
    tts = load("tts_helper")
    ar = load("audio_routing")
    nc = load("nova_config")
    st = types.SimpleNamespace(
        row={"id": 11, "text": "The garage is open", "speakers": ["media_player.kitchen"]},
        announced=[], broadcast=["media_player.everywhere"], tts="tts.piper", ok=True,
        cfg={"tts_engine": "auto", "broadcast_group": "", "announcement_speakers": []})

    async def announce(hass, text, tts_entity, speakers, **kw):
        st.announced.append((text, tts_entity, list(speakers), kw))
        return st.ok
    monkeypatch.setattr(sh, "get", lambda spoken_id: st.row)
    monkeypatch.setattr(tts, "async_announce", announce)
    monkeypatch.setattr(tts, "resolve_tts_entity", lambda hass, configured: st.tts)
    monkeypatch.setattr(ar, "broadcast_target", lambda hass, **kw: list(st.broadcast))
    monkeypatch.setattr(nc, "effective_config", lambda entry=None: dict(st.cfg))
    return st


async def test_repeat_goes_to_the_original_speaker_when_it_is_there(voice, speech):
    conn = await _call(voice.ws_repeat_spoken, _hass(media_player__kitchen="idle"), spoken_id=11)
    assert conn.results == [(4, {"ok": True, "spoken": "The garage is open"})]
    assert speech.announced == [("The garage is open", "tts.piper", ["media_player.kitchen"],
                                 {"context": "repeat", "repeat_of_id": 11})]


@pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
async def test_repeat_is_refused_when_the_original_speaker_is_gone(voice, speech, state):
    # 8.7.23: was test_current_behaviour_repeat_falls_back_to_every_default_
    # speaker. A line is only repeated where it was said; it is no longer
    # sent to the house-wide default speakers.
    hass = _hass(**({"media_player__kitchen": state} if state else {}))
    conn = await _call(voice.ws_repeat_spoken, hass, spoken_id=11)
    assert conn.errors == [(4, "no_speaker", "The speaker this was said on is not available, "
                                             "so it was not repeated")]
    assert speech.announced == []


async def test_repeat_uses_only_the_original_speakers_still_there(voice, speech):
    speech.row["speakers"] = ["media_player.kitchen", "media_player.gone"]
    conn = await _call(voice.ws_repeat_spoken, _hass(media_player__kitchen="playing"),
                       spoken_id=11)
    assert conn.results[0][1]["ok"] is True
    assert speech.announced[0][2] == ["media_player.kitchen"]


async def test_repeat_of_a_missing_row_is_not_found(voice, speech):
    speech.row = None
    conn = await _call(voice.ws_repeat_spoken, _hass(), spoken_id=99)
    assert conn.errors == [(4, "not_found", "No spoken history entry with that id")]
    assert speech.announced == []


async def test_repeat_of_a_line_with_no_recorded_speaker_is_refused(voice, speech):
    # Before 8.7.23 this fell back to the default speakers too.
    speech.row["speakers"] = []
    conn = await _call(voice.ws_repeat_spoken, _hass(), spoken_id=11)
    assert conn.errors[0][:2] == (4, "no_speaker") and speech.announced == []


async def test_repeat_with_no_tts_entity_says_so(voice, speech):
    speech.tts = None
    conn = await _call(voice.ws_repeat_spoken, _hass(media_player__kitchen="idle"), spoken_id=11)
    assert conn.errors == [(4, "no_tts_entity", "No TTS entity available")]
    assert speech.announced == []


async def test_a_failed_repeat_reports_nothing_spoken(voice, speech):
    speech.ok = False
    conn = await _call(voice.ws_repeat_spoken, _hass(media_player__kitchen="idle"), spoken_id=11)
    assert conn.results == [(4, {"ok": False, "spoken": ""})]


async def test_repeat_failure_returns_no_raw_text(voice, speech, load, monkeypatch):
    monkeypatch.setattr(load("spoken_history"), "get", _raise)
    _safe(await _call(voice.ws_repeat_spoken, _hass(), spoken_id=11), "repeat_spoken_failed")


# ── nova/list_actions ───────────────────────────────────────────────────────

@pytest.fixture
def actions(load, monkeypatch):
    al = load("action_log")
    sh = load("spoken_history")
    st = types.SimpleNamespace(asked=[], links={"r1": 5}, link_error=False)

    def page_requests(limit, cursor_ts, cursor_request_id):
        st.asked.append((limit, cursor_ts, cursor_request_id))
        return {"requests": [{"request_id": "r1"}, {"request_id": "r2"}], "next_cursor": None}

    def find(ids):
        if st.link_error:
            raise RuntimeError("db locked")
        return {k: v for k, v in st.links.items() if k in ids}
    monkeypatch.setattr(al, "page_requests", page_requests)
    monkeypatch.setattr(sh, "find_by_action_request_id", find)
    return st


@pytest.mark.parametrize("asked,used", [(20, 20), (0, 1), (-9, 1), (500, 100)])
async def test_list_actions_limit_is_held_to_1_to_100(voice, actions, asked, used):
    await _call(voice.ws_list_actions, _hass(), limit=asked, cursor_ts=1.5,
                cursor_request_id="r0")
    assert actions.asked == [(used, 1.5, "r0")]


async def test_list_actions_links_spoken_history_by_reference(voice, actions):
    conn = await _call(voice.ws_list_actions, _hass(), limit=20)
    reqs = conn.results[0][1]["requests"]
    assert reqs == [{"request_id": "r1", "spoken_history_id": 5},
                    {"request_id": "r2", "spoken_history_id": None}]


async def test_a_failed_spoken_lookup_never_breaks_the_list(voice, actions):
    actions.link_error = True
    conn = await _call(voice.ws_list_actions, _hass(), limit=20)
    assert [r["spoken_history_id"] for r in conn.results[0][1]["requests"]] == [None, None]


async def test_list_actions_failure_returns_no_raw_text(voice, actions, load, monkeypatch):
    monkeypatch.setattr(load("action_log"), "page_requests", _raise)
    _safe(await _call(voice.ws_list_actions, _hass(), limit=20), "list_actions_failed")


# ── spoken history, voice confirm test, say hello ──────────────────────────

async def test_spoken_history_lists_entries(voice, load, monkeypatch):
    monkeypatch.setattr(load("spoken_history"), "list_recent", lambda: [{"id": 1}])
    conn = await _call(voice.ws_get_spoken_history, _hass())
    assert conn.results == [(4, {"entries": [{"id": 1}]})]
    monkeypatch.setattr(load("spoken_history"), "list_recent", _raise)
    _safe(await _call(voice.ws_get_spoken_history, _hass()), "get_spoken_history_failed")


async def test_voice_confirm_test_passes_the_result_through(voice, load, monkeypatch):
    vc = load("voice_confirm")

    async def announce_test(hass):
        return {"ok": True, "target": "assist_satellite.kitchen"}
    monkeypatch.setattr(vc, "announce_test", announce_test)
    conn = await _call(voice.ws_voice_confirm_test, _hass())
    assert conn.results == [(4, {"ok": True, "target": "assist_satellite.kitchen"})]
    monkeypatch.setattr(vc, "announce_test", _araise)
    _safe(await _call(voice.ws_voice_confirm_test, _hass()), "test_failed")


async def test_say_hello_sends_only_the_fixed_text_through_the_agent(voice, load, monkeypatch):
    seen = []

    async def say_hello(hass, context):
        seen.append(context)
        return {"ok": True, "reply": "Hello."}
    monkeypatch.setattr(load("welcome"), "async_say_hello", say_hello)
    conn = await _call(voice.ws_say_hello, _hass())
    assert conn.results == [(4, {"ok": True, "reply": "Hello."})] and seen == [("ctx", 4)]


async def test_say_hello_failure_is_an_error_result_not_a_raise(voice, load, monkeypatch, caplog):
    # 8.7.23: was test_current_behaviour_say_hello_raises_instead_of_
    # returning_an_error. The answer keeps welcome.async_say_hello's own
    # {ok, error} shape (the pinned contract has no error code here), with
    # safe text; the detail goes to the Home Assistant log.
    monkeypatch.setattr(load("welcome"), "async_say_hello", _araise)
    conn = await _call(voice.ws_say_hello, _hass())
    assert conn.errors == []
    assert conn.results == [(4, {"ok": False,
                                 "error": "RuntimeError (details are in the Home Assistant log)"})]
    assert "say_hello failed" in caplog.text
