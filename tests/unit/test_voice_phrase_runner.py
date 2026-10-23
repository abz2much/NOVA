"""Voice phrases (8.32.0): running a matched phrase.

The real policy and the real action log, with a fake confirmation layer and
a fake Home Assistant (the same fixtures as test_authority.py). Nothing here
touches a real device.
"""
import sys
import types

import pytest

from conftest import readable_devices


@pytest.fixture(autouse=True)
def _readable(fake_hass):
    """The lock, garage and alarm read a state that is not already secure,
    so the live check (8.46.0) lets the policy decide."""
    readable_devices(fake_hass, "lock.front", "cover.garage", "alarm_control_panel.home")

from test_authority import _VC


@pytest.fixture
def pol(load, monkeypatch):
    p = load("policy")
    fake = _VC()
    fake.install(load("voice_confirm"), monkeypatch)
    return p, fake


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture
def run(load, pol):
    return load("voice_phrase_runner")


@pytest.fixture
def calls(fake_hass, monkeypatch):
    seen = []

    async def async_call(domain, service, data=None, blocking=False, **kw):
        seen.append((domain, service, dict(data or {}), blocking))
    monkeypatch.setattr(fake_hass.services, "async_call", async_call)
    return seen


@pytest.fixture
def cfg(load):
    nc = load("nova_config")
    old_loaded, old_cache = nc._loaded, nc._cache
    nc._loaded = True
    nc._cache = {}
    yield nc
    nc._loaded, nc._cache = old_loaded, old_cache


def _entry(service="script.turn_on", target="script.heating_boost", data=None,
           reply="", allow_ai=False, name="House is cold", phrases=("the house is cold",)):
    action = {"service": service, "data": data or {}}
    if target is not None:
        action["target"] = {"entity_id": target}
    return {"phrase_id": "vp_3f9c1a2b", "name": name, "phrases": list(phrases),
            "match": "flexible", "allow_ai": allow_ai, "enabled": True,
            "action": action, "reply": reply}


def _rows(al):
    return [t for r in al.page_requests()["requests"] for t in r["targets"]]


async def _go(run, hass, entry, *, may_ask=False, honorific="sir", device_id=None):
    return await run.run_entry(hass, entry, device_id=device_id, user_id="u1",
                               honorific=honorific, may_ask=may_ask)


# ── allowed: one call, the right shape ──────────────────────────────────────

async def test_an_allowed_phrase_runs_once_with_merged_data(run, fake_hass, calls, al):
    entry = _entry(target=["script.a", "script.b"], data={"variables": {"level": 2}},
                   reply="Warming the house up{addr}.")
    out = await _go(run, fake_hass, entry)
    assert not out.deferred and out.status == "accepted"
    assert out.result.handled and out.result.success
    assert out.result.text == "Warming the house up, sir."
    assert calls == [("script", "turn_on",
                      {"variables": {"level": 2}, "entity_id": ["script.a", "script.b"]}, False)]
    rows = _rows(al)
    assert sorted(r["entity_id"] for r in rows) == ["script.a", "script.b"]
    # Accepted, then unverified: what a script does is not visible to Nova.
    assert {r["execution_result"] for r in rows} == {"unverified"}
    assert {r["reason_code"] for r in rows} == {"not_checkable"}
    assert {r["approval_result"] for r in rows} == {"not_required"}
    assert {r["action"] for r in rows} == {"voice_phrase"}


@pytest.mark.parametrize("service,target,blocking", [
    ("script.turn_on", "script.a", False),
    ("automation.trigger", "automation.a", False),
    ("scene.turn_on", "scene.a", True),
    ("light.turn_on", "light.a", True),
    ("climate.set_temperature", "climate.a", True),
])
async def test_blocking_only_for_quick_calls(run, fake_hass, calls, service, target, blocking):
    await _go(run, fake_hass, _entry(service=service, target=target))
    assert calls[0][3] is blocking
    assert calls[0][2] == {"entity_id": target}


async def test_a_shell_command_runs_without_blocking_and_logs_one_row(run, fake_hass, calls, al):
    out = await _go(run, fake_hass, _entry(service="shell_command.heat_boost", target=None))
    assert out.result.success and out.result.text == "Done, sir."
    assert calls == [("shell_command", "heat_boost", {}, False)]
    assert [r["entity_id"] for r in _rows(al)] == ["shell_command.heat_boost"]


async def test_a_call_with_no_target_logs_one_blank_row(run, fake_hass, calls, al):
    await _go(run, fake_hass, _entry(service="script.heating_boost", target=None))
    assert calls == [("script", "heating_boost", {}, False)]
    assert len(_rows(al)) == 1


async def test_the_shell_command_can_be_made_to_ask(run, pol, fake_hass, calls, al,
                                                     load, monkeypatch):
    _p, fake = pol
    fake.enabled = True
    seen = []

    def protected(hass, d, s, e=""):
        seen.append((d, s, e))
        return e == "shell_command.heat_boost"
    monkeypatch.setattr(load("voice_confirm"), "action_is_protected", protected)
    out = await _go(run, fake_hass, _entry(service="shell_command.heat_boost", target=None))
    assert out.deferred and calls == []
    assert seen == [("shell_command", "heat_boost", "shell_command.heat_boost")]


# ── deferred on the fast path ───────────────────────────────────────────────

async def test_fast_path_defers_when_confirmation_is_needed(run, pol, fake_hass, calls, al):
    _p, fake = pol
    fake.enabled = True            # scripts ask first with Voice Confirmation on
    out = await _go(run, fake_hass, _entry())
    assert out.deferred and out.result is None and out.status == "deferred"
    assert calls == [] and fake.asked == []      # nothing ran, nobody was asked
    (row,) = _rows(al)
    assert row["approval_result"] == "deferred"
    assert row["execution_result"] == "blocked"
    assert row["reason_code"] == "confirmation_not_approved"
    assert row["reason_text"] == "handed to agent"


async def test_a_refusal_on_the_second_target_runs_nothing(run, pol, fake_hass, calls, al,
                                                           load, monkeypatch):
    _p, fake = pol
    fake.enabled = True
    monkeypatch.setattr(load("voice_confirm"), "action_is_protected",
                        lambda hass, d, s, e="": e == "script.b")
    out = await _go(run, fake_hass, _entry(target=["script.a", "script.b", "script.c"]))
    assert out.deferred and calls == []
    rows = {r["entity_id"]: r for r in _rows(al)}
    assert rows["script.a"]["approval_result"] == "not_required"
    assert rows["script.b"]["approval_result"] == "deferred"
    assert {r["execution_result"] for r in rows.values()} == {"blocked"}
    assert {r["reason_code"] for r in rows.values()} == {"confirmation_not_approved"}
    # The row never decided is not left waiting for an answer.
    assert rows["script.c"]["approval_result"] == "not_required"


# ── asked (the agent tool) ──────────────────────────────────────────────────

async def test_asking_and_being_refused_blocks_and_logs(run, pol, fake_hass, calls, al):
    _p, fake = pol
    fake.enabled = True
    fake.spoken_answer = "rejected"
    out = await _go(run, fake_hass, _entry(), may_ask=True)
    assert not out.deferred and not out.result.success and out.status == "refused"
    assert out.result.text == "I need you to confirm House is cold first, sir."
    assert calls == [] and [how for how, _q in fake.asked] == ["spoken"]
    (row,) = _rows(al)
    assert row["approval_result"] == "rejected" and row["approval_required"]
    assert row["execution_result"] == "blocked"
    assert row["reason_code"] == "confirmation_not_approved"
    assert row["reason_text"] in (None, "")


async def test_asking_and_being_told_yes_runs(run, pol, fake_hass, calls, al):
    _p, fake = pol
    fake.enabled = True
    fake_hass.states.set("script.heating_boost", "off", friendly_name="Heating boost")
    out = await _go(run, fake_hass, _entry(), may_ask=True)
    assert out.result.success and len(calls) == 1
    assert "Heating boost" in fake.asked[0][1]          # the device's own name
    assert _rows(al)[0]["approval_result"] == "approved"


async def test_unlock_by_voice_still_needs_a_phone_tap(run, pol, fake_hass, calls, al):
    _p, fake = pol
    fake.satellites.add("sat1")
    fake.phone_answer = "rejected"
    out = await _go(run, fake_hass, _entry(service="lock.unlock", target="lock.front"),
                    may_ask=True, device_id="sat1")
    assert calls == [] and [how for how, _q in fake.asked] == ["phone"]
    assert not out.result.success
    # And on the fast path it is never run either.
    out = await _go(run, fake_hass, _entry(service="lock.unlock", target="lock.front"),
                    device_id="sat1")
    assert out.deferred and calls == []


# ── failures ────────────────────────────────────────────────────────────────

async def test_a_failed_call_says_so_without_the_error(run, fake_hass, al, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret token abc123")
    monkeypatch.setattr(fake_hass.services, "async_call", boom)
    out = await _go(run, fake_hass, _entry(target=["script.a", "script.b"]))
    assert out.result.text == "I couldn't run House is cold, sir."
    assert "secret" not in out.result.text and not out.result.success
    assert out.status == "failed"
    assert {r["execution_result"] for r in _rows(al)} == {"failed"}
    assert {r["reason_code"] for r in _rows(al)} == {"service_call_failed"}


@pytest.mark.parametrize("reply,honorific,expected", [
    ("", "sir", "Done, sir."),
    ("", "", "Done."),
    ("Warming up{addr}.", "", "Warming up."),
    ("Right away {honorific}.", "Abi", "Right away Abi."),
    ("{0.__class__} {addr} {name}", "sir", "{0.__class__} , sir {name}"),
    ("{", "sir", "{"),
])
async def test_replies_are_filled_by_plain_replacement(run, fake_hass, calls, reply,
                                                       honorific, expected):
    out = await _go(run, fake_hass, _entry(reply=reply), honorific=honorific)
    assert out.result.text == expected


async def test_run_entry_never_raises(run, fake_hass, al, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log down")
    monkeypatch.setattr(al, "start_many", boom)
    out = await _go(run, fake_hass, _entry())
    assert out.result.text == "I couldn't run House is cold, sir." and not out.deferred
    for bad in (None, {}, {"name": "x", "action": {"service": "nodot"}}, {"action": 5}):
        out = await _go(run, fake_hass, bad)
        assert out.result is not None and not out.result.success


# ── try_phrase: the conversation step ───────────────────────────────────────

# A looser match of "the house is cold" (tier flexible): it still needs a yes.
LOOSE = "the house is a bit cold"


def _broken_panel_log(monkeypatch):
    """A panel log that raises, so the caller's own work must carry on."""
    def nova_log(*_a, **_k):
        raise RuntimeError("panel log down")
    fake = types.ModuleType("jc.websocket")
    fake.nova_log = nova_log
    monkeypatch.setitem(sys.modules, "jc.websocket", fake)


async def _step(run, hass, text="Nova, the house is cold."):
    return await run.try_phrase(hass, text, honorific="sir", device_id=None, user_id="u1")


async def test_no_phrases_means_no_step(run, fake_hass, calls, cfg):
    for value in (None, {"enabled": True, "entries": []}):
        cfg._cache = {} if value is None else {"voice_phrases": value}
        step = await _step(run, fake_hass)
        assert step.result is None and step.defer_hint is None
    assert calls == []


async def test_a_match_runs_and_returns_a_local_result(run, fake_hass, calls, cfg):
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry()]}}
    step = await _step(run, fake_hass)
    assert step.defer_hint is None and step.result.handled and step.result.success
    assert len(calls) == 1


async def test_a_match_still_runs_when_the_panel_log_fails(run, fake_hass, calls, cfg, monkeypatch):
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry()]}}
    _broken_panel_log(monkeypatch)
    step = await _step(run, fake_hass)
    assert step.defer_hint is None and step.result.handled and step.result.success
    assert len(calls) == 1
    step = await _step(run, fake_hass, "the house is not cold")
    assert step.result is None and len(calls) == 1


async def test_the_step_reads_a_copy_of_the_config(run, fake_hass, calls, cfg, load,
                                                   monkeypatch):
    live = {"enabled": True, "entries": [_entry()]}
    cfg._cache = {"voice_phrases": live}
    seen = []
    real = load("voice_phrases").match

    def spy(text, config):
        seen.append(config)
        return real(text, config)
    monkeypatch.setattr(load("voice_phrases"), "match", spy)
    await _step(run, fake_hass)
    assert seen[0] == live and seen[0] is not live


@pytest.fixture
def online(load, monkeypatch):
    conn = load("connectivity")
    state = {"online": True}
    monkeypatch.setattr(conn, "is_online", lambda: state["online"])

    def no_probe():
        raise AssertionError("try_phrase used allow_request()")
    monkeypatch.setattr(conn, "allow_request", no_probe)
    return state


async def test_deferral_online_hands_the_agent_a_fenced_hint(run, pol, fake_hass, calls,
                                                             cfg, online):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _entry(target=["script.a", "script.b"])]}}
    step = await _step(run, fake_hass, LOOSE)
    assert step.result is None and calls == []
    assert "BEGIN_VOICE_PHRASE_MATCH_" in step.defer_hint
    assert "run_scene_or_script" in step.defer_hint
    assert "script.a, script.b" in step.defer_hint
    assert "run_voice_phrase" not in step.defer_hint


async def test_deferral_names_run_voice_phrase_when_the_ai_may_pick(run, pol, fake_hass,
                                                                    calls, cfg, online):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry(allow_ai=True)]}}
    step = await _step(run, fake_hass, LOOSE)
    assert "run_voice_phrase with phrase_id vp_3f9c1a2b" in step.defer_hint


async def test_deferral_offline_refuses_politely(run, pol, fake_hass, calls, cfg, online):
    _p, fake = pol
    fake.enabled = True
    online["online"] = False
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry()]}}
    step = await _step(run, fake_hass, LOOSE)
    assert step.defer_hint is None and calls == []
    assert step.result.text == (
        "House is cold needs confirming first, and I can't ask for that right now, sir.")
    assert not step.result.success


async def test_deferral_of_another_domain_refuses(run, pol, fake_hass, calls, cfg, online):
    _p, fake = pol
    fake.satellites.add("sat1")
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _entry(service="lock.unlock", target="lock.front")]}}
    step = await run.try_phrase(fake_hass, "the house is cold", honorific="",
                                device_id="sat1", user_id="u1")
    assert step.defer_hint is None and calls == [] and not step.result.success


async def test_try_phrase_never_raises(run, fake_hass, cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config down")
    monkeypatch.setattr(cfg, "get", boom)
    step = await _step(run, fake_hass)
    assert step.result is None and step.defer_hint is None


# ── 8.34.0: an exact match runs; a looser one on voice is asked by Nova ─────

@pytest.fixture
def pcf(load):
    mod = load("pending_confirm")
    mod.clear_all()
    yield mod
    mod.clear_all()


@pytest.mark.parametrize("device", ["sat1", None])
async def test_an_exact_match_runs_at_once_with_voice_confirmation_on(
        run, pol, fake_hass, calls, cfg, al, pcf, device):
    _p, fake = pol
    fake.enabled = True
    fake.satellites.add("sat1")
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _entry(reply="Warming the house up{addr}.")]}}
    step = await run.try_phrase(fake_hass, "Nova, the house is cold.", honorific="sir",
                                device_id=device, user_id="u1")
    assert step.defer_hint is None and step.result.success
    assert step.result.text == "Warming the house up, sir."
    assert calls == [("script", "turn_on", {"entity_id": "script.heating_boost"}, False)]
    assert fake.asked == [] and pcf.get("sat1") is None
    assert {r["approval_result"] for r in _rows(al)} == {"not_required"}


async def test_the_exact_scope_ends_with_the_run(run, pol, fake_hass, calls, cfg, load):
    p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry()]}}
    await run.try_phrase(fake_hass, "the house is cold", honorific="", device_id=None,
                         user_id="u1")
    later = p.authorize_now(fake_hass, p.AuthorityRequest(
        "script", "turn_on", "script.heating_boost"))
    assert not later.allowed and later.approval == "deferred"


@pytest.mark.parametrize("device", ["sat1", None])
@pytest.mark.parametrize("service,target", [
    ("lock.unlock", "lock.front"),
    ("alarm_control_panel.alarm_disarm", "alarm_control_panel.home"),
    ("cover.open_cover", "cover.garage"),
])
async def test_an_exact_match_never_skips_the_phone_tap_or_a_high_risk_question(
        run, pol, fake_hass, calls, cfg, online, pcf, device, service, target):
    _p, fake = pol
    fake.enabled = True            # Voice Confirmation on: typed requests ask too
    fake.satellites.add("sat1")
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _entry(service=service, target=target)]}}
    step = await run.try_phrase(fake_hass, "the house is cold", honorific="",
                                device_id=device, user_id="u1")
    assert calls == [] and fake.asked == [] and pcf.get("sat1") is None
    assert step.defer_hint is None and not step.result.success
    assert "needs confirming first" in step.result.text


def test_the_exact_scope_never_covers_high_risk(load, pol, fake_hass):
    p, fake = pol
    fake.enabled = True
    with p.exact_phrase_approval([("lock", "unlock", "lock.front"),
                                  ("cover", "open_cover", "cover.garage"),
                                  ("script", "turn_on", "script.a")]):
        assert p.requires_confirmation(fake_hass, "lock", "unlock", "lock.front")
        # A garage door rates medium, and is still never covered.
        assert p.requires_confirmation(fake_hass, "cover", "open_cover", "cover.garage")
        assert not p.requires_confirmation(fake_hass, "script", "turn_on", "script.a")
        assert p.requires_confirmation(fake_hass, "script", "turn_on", "script.b")


def test_the_exact_scope_fails_closed_when_risk_is_unknown(load, pol, fake_hass, monkeypatch):
    p, fake = pol
    fake.enabled = True

    def boom(*a, **k):
        raise RuntimeError("classify down")
    with p.exact_phrase_approval([("script", "turn_on", "script.a")]):
        monkeypatch.setattr(p, "classify", boom)
        assert p._exact_approved("script", "turn_on", "script.a") is False


async def test_a_looser_match_on_a_voice_satellite_is_asked_by_nova(
        run, pol, fake_hass, calls, cfg, online, pcf, load):
    _p, fake = pol
    fake.enabled = True
    fake.satellites.add("sat1")
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _entry(reply="Warming the house up.", allow_ai=True)]}}
    step = await run.try_phrase(fake_hass, LOOSE, honorific="", device_id="sat1",
                                user_id="u1")
    assert step.defer_hint is None                  # never handed to the AI
    assert step.result.handled and step.result.text == "House is cold?"
    item = pcf.get("sat1")
    assert (item.kind, item.ref, item.user_id) == ("voice_phrase", "vp_3f9c1a2b", "u1")
    assert calls == [] and fake.asked == []
    res = await pcf.answer(fake_hass, "sat1", "Yes.")
    assert res.text == "Warming the house up."
    assert calls == [("script", "turn_on", {"entity_id": "script.heating_boost"}, False)]


async def test_a_looser_match_from_chat_keeps_the_agent_path(run, pol, fake_hass, calls, cfg,
                                                            online, pcf):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry()]}}
    step = await run.try_phrase(fake_hass, LOOSE, honorific="", device_id=None, user_id="u1")
    assert step.result is None and "run_scene_or_script" in step.defer_hint
    assert pcf._ITEMS == {} and calls == []


async def test_the_ai_picking_a_phrase_still_needs_a_yes(pol, fake_hass, calls, cfg, load):
    _p, fake = pol
    fake.enabled = True
    fake.spoken_answer = "rejected"
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_entry(allow_ai=True)]}}
    ctl = load("agent_runtime.capabilities.control")
    import json
    res = json.loads(await ctl._exec_run_voice_phrase(
        fake_hass, {"phrase_id": "vp_3f9c1a2b"}, device_id=None, user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and calls == []


def test_is_voice_satellite_fails_closed(run, fake_hass, load, monkeypatch):
    assert run._is_voice_satellite(fake_hass, None) is False

    def boom(*a, **k):
        raise RuntimeError("registry down")
    monkeypatch.setattr(load("voice_confirm"), "is_voice_satellite_device", boom)
    assert run._is_voice_satellite(fake_hass, "sat1") is False
