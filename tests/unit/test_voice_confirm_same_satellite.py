"""A confirmation asked by voice stays on the satellite you spoke to (8.33.0).

Part 1: the gate and the tools. A request from a voice satellite that needs
a spoken confirmation never asks separately: no announcement, no
ask_question, no start_conversation (either would cancel the user's own
running request). The tool records what a yes would run, per device, and
its result ends with a fixed question. approved_confirmation lets a later
spoken yes through for exactly that device and those targets, never past a
phone tap. All fake; nothing touches a real device.
"""
import json

import pytest

from test_authority import _VC


@pytest.fixture
def pol(load, monkeypatch):
    p = load("policy")
    fake = _VC()
    fake.install(load("voice_confirm"), monkeypatch)
    fake.satellites.update({"satA", "satB"})
    return p, fake


@pytest.fixture
def pc(load):
    mod = load("pending_confirm")
    mod.clear_all()
    yield mod
    mod.clear_all()


@pytest.fixture
def calls(fake_hass, monkeypatch):
    seen = []

    async def async_call(domain, service, data=None, blocking=False, **kw):
        seen.append((domain, service, dict(data or {})))
    monkeypatch.setattr(fake_hass.services, "async_call", async_call)
    return seen


@pytest.fixture
def no_question(load, monkeypatch):
    """Fails the test if anything tries to ask out loud."""
    vc = load("voice_confirm")
    asked = []

    async def confirm_typed(hass, q, entity_id=""):
        asked.append(q)
        return "deferred"
    monkeypatch.setattr(vc, "confirm_typed", confirm_typed)
    return asked


def _req(p, domain, service, entity, device="satA"):
    return p.AuthorityRequest(domain, service, entity, device_id=device, user_id="u1")


# ── the gate ────────────────────────────────────────────────────────────────

async def test_a_voice_satellite_request_is_deferred_without_asking(pol, fake_hass, calls,
                                                                     no_question):
    p, fake = pol
    fake.enabled = True
    d = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat"))
    assert not d.allowed and d.approval == "deferred"
    assert no_question == [] and fake.asked == [] and calls == []


async def test_without_a_device_the_gate_still_asks_as_before(pol, fake_hass, no_question):
    p, fake = pol
    fake.enabled = True
    d = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat", device=""))
    assert d.approval == "deferred" and len(no_question) == 1


async def test_a_typed_request_from_another_device_still_asks(pol, fake_hass, no_question):
    p, fake = pol
    fake.enabled = True
    await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat", device="phone1"))
    assert len(no_question) == 1


async def test_approved_confirmation_lets_exactly_that_target_through(pol, fake_hass,
                                                                       no_question):
    p, fake = pol
    fake.enabled = True
    with p.approved_confirmation("satA", [("script", "turn_on", "script.heat")]):
        d = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat"))
        assert d.allowed and d.approval == "approved"
        other = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.other"))
        assert not other.allowed
        wrong_dev = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat",
                                                      device="satB"))
        assert not wrong_dev.allowed
        wrong_svc = await p.authorize(fake_hass, _req(p, "script", "toggle", "script.heat"))
        assert not wrong_svc.allowed
    after = await p.authorize(fake_hass, _req(p, "script", "turn_on", "script.heat"))
    assert not after.allowed and no_question == []


async def test_approved_confirmation_covers_every_listed_target(pol, fake_hass):
    p, fake = pol
    fake.enabled = True
    targets = [("script", "turn_on", "script.a"), ("script", "turn_on", "script.b")]
    with p.approved_confirmation("satA", targets):
        for _d, _s, e in targets:
            assert (await p.authorize(fake_hass, _req(p, "script", "turn_on", e))).allowed


@pytest.mark.parametrize("domain,service,entity", [
    ("lock", "unlock", "lock.front"),
    ("cover", "open_cover", "cover.garage"),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home"),
    ("nova", "dismiss_intrusion", ""),
])
async def test_approved_confirmation_never_skips_the_phone_tap(pol, fake_hass, domain,
                                                                service, entity):
    p, fake = pol
    fake.phone_answer = "rejected"
    with p.approved_confirmation("satA", [(domain, service, entity)]):
        d = await p.authorize(fake_hass, _req(p, domain, service, entity))
    assert not d.allowed and d.approval == "rejected"
    assert [how for how, _q in fake.asked] == ["phone"]


async def test_a_request_that_needs_no_confirmation_is_unchanged(pol, fake_hass, no_question):
    p, fake = pol
    fake.enabled = True
    d = await p.authorize(fake_hass, _req(p, "light", "turn_on", "light.hall"))
    assert d.allowed and d.approval == "not_required" and no_question == []


# ── the gated path's speaker, outside a voice turn ──────────────────────────

def test_the_gated_speaker_follows_the_satellite_pairing(load, fake_hass, monkeypatch):
    vc = load("voice_confirm")
    ar = load("audio_routing")
    seen = {}

    def reply_target(hass, *, satellite_entity_id=None, device_id=None, satellite_pairings=None):
        seen.update(sat=satellite_entity_id, pairings=satellite_pairings)
        return (satellite_pairings or {}).get(satellite_entity_id)
    monkeypatch.setattr(ar, "reply_target", reply_target)
    monkeypatch.setattr(vc, "_satellite_pairings",
                        lambda h: {"assist_satellite.living": "media_player.living"})
    _tts, speakers = vc._speaker_for_satellite(fake_hass, "assist_satellite.living")
    assert speakers == ["media_player.living"]
    assert seen["sat"] == "assist_satellite.living"
    _tts, speakers = vc._speaker_for_satellite(fake_hass, "assist_satellite.other")
    assert speakers == []


def test_the_gated_speaker_is_never_the_satellite_itself(load, fake_hass, monkeypatch):
    vc = load("voice_confirm")
    monkeypatch.setattr(load("audio_routing"), "reply_target",
                        lambda hass, **kw: "assist_satellite.living")
    assert vc._speaker_for_satellite(fake_hass, "assist_satellite.living")[1] == []


def test_pairings_are_read_from_the_live_runtime_config(load, fake_hass, nova_runtime):
    vc = load("voice_confirm")
    nova_runtime(fake_hass, {"satellite_pairings": '{"assist_satellite.a": "media_player.a"}'})
    assert vc._satellite_pairings(fake_hass) == {"assist_satellite.a": "media_player.a"}
    nova_runtime(fake_hass, {"satellite_pairings": "not json"})
    assert vc._satellite_pairings(fake_hass) is None
    nova_runtime(fake_hass, {})
    assert vc._satellite_pairings(fake_hass) is None


# ── the pending store ───────────────────────────────────────────────────────

def test_one_item_per_device_newest_wins_and_it_expires(pc):
    pc.put("satA", pc.KIND_PHRASE, "vp_1", "u1", now=100.0)
    pc.put("satA", pc.KIND_SCENE_SCRIPT, "script.b", "u1", now=101.0)
    pc.put("satB", pc.KIND_PHRASE, "vp_2", "u2", now=101.0)
    item = pc.get("satA", now=150.0)
    assert (item.kind, item.ref, item.user_id) == (pc.KIND_SCENE_SCRIPT, "script.b", "u1")
    assert pc.get("satA", now=161.0) is None           # 60 s after it was put
    assert pc.get("satA", now=150.0) is None           # and dropped
    assert pc.pop("satB", now=150.0).ref == "vp_2"
    assert pc.get("satB", now=150.0) is None


@pytest.mark.parametrize("device,kind,ref", [
    ("", "voice_phrase", "vp_1"), ("satA", "voice_phrase", ""), ("satA", "other", "x")])
def test_nothing_is_stored_without_a_device_a_ref_or_a_known_kind(pc, device, kind, ref):
    assert pc.put(device, kind, ref) is None and pc.get(device or "satA") is None


def test_clear(pc):
    pc.put("satA", pc.KIND_PHRASE, "vp_1")
    pc.clear("satA")
    assert pc.get("satA") is None and pc.get("") is None and pc.pop("") is None


# ── the tools ───────────────────────────────────────────────────────────────

@pytest.fixture
def ctl(load, pol):
    return load("agent_runtime.capabilities.control")


@pytest.fixture
def cfg(load):
    nc = load("nova_config")
    old = nc._loaded, nc._cache
    nc._loaded, nc._cache = True, {}
    yield nc
    nc._loaded, nc._cache = old


def _phrase(pid="vp_3f9c1a2b", targets=("script.heat",)):
    return {"phrase_id": pid, "name": "House is cold", "phrases": ["the house is cold"],
            "match": "flexible", "allow_ai": True, "enabled": True, "reply": "",
            "action": {"service": "script.turn_on", "data": {},
                       "target": {"entity_id": list(targets)}}}


async def test_run_voice_phrase_from_a_satellite_waits_for_a_spoken_yes(
        ctl, pol, pc, cfg, fake_hass, calls, no_question):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_phrase()]}}
    res = json.loads(await ctl._exec_run_voice_phrase(
        fake_hass, {"phrase_id": "vp_3f9c1a2b"}, device_id="satA", user_id="u1"))
    assert res["status"] == "awaiting_confirmation"
    assert res["message"].endswith("Shall I go ahead?") and res["ask_user"] == "Shall I go ahead?"
    item = pc.get("satA")
    assert (item.kind, item.ref, item.user_id) == ("voice_phrase", "vp_3f9c1a2b", "u1")
    assert calls == [] and no_question == [] and fake.asked == []


async def test_run_scene_or_script_from_a_satellite_waits_for_a_spoken_yes(
        ctl, pol, pc, fake_hass, calls, no_question):
    _p, fake = pol
    fake.enabled = True
    res = json.loads(await ctl._exec_run_scene_script(
        fake_hass, {"entity_id": "script.heat"}, device_id="satA", user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and res["message"].endswith("Shall I go ahead?")
    assert (pc.get("satA").kind, pc.get("satA").ref) == ("scene_script", "script.heat")
    assert calls == [] and no_question == []


async def test_two_tool_calls_in_one_turn_leave_only_the_last(ctl, pol, pc, fake_hass, calls):
    _p, fake = pol
    fake.enabled = True
    for eid in ("script.first", "script.second"):
        await ctl._exec_run_scene_script(fake_hass, {"entity_id": eid}, device_id="satA",
                                         user_id="u1")
    assert pc.get("satA").ref == "script.second"


async def test_a_typed_request_records_nothing(ctl, pol, pc, fake_hass, calls, no_question):
    _p, fake = pol
    fake.enabled = True
    res = json.loads(await ctl._exec_run_scene_script(
        fake_hass, {"entity_id": "script.heat"}, device_id="", user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and "ask_user" not in res
    assert pc.get("satA") is None and len(no_question) == 1


async def test_a_phone_tap_refusal_records_nothing(ctl, pol, pc, cfg, fake_hass, calls):
    _p, fake = pol
    fake.phone_answer = "rejected"
    phrase = _phrase(targets=("lock.front",))
    phrase["action"]["service"] = "lock.unlock"
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [phrase]}}
    res = json.loads(await ctl._exec_run_voice_phrase(
        fake_hass, {"phrase_id": "vp_3f9c1a2b"}, device_id="satA", user_id="u1"))
    assert res["status"] == "awaiting_confirmation" and "ask_user" not in res
    assert pc.get("satA") is None and calls == []


async def test_calling_the_tool_again_while_pending_does_not_approve_it(
        ctl, pol, pc, cfg, fake_hass, calls):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_phrase()]}}
    for _ in range(2):
        res = json.loads(await ctl._exec_run_voice_phrase(
            fake_hass, {"phrase_id": "vp_3f9c1a2b"}, device_id="satA", user_id="u1"))
        assert res["status"] == "awaiting_confirmation"
    assert calls == [] and pc.get("satA").ref == "vp_3f9c1a2b"


def test_hold_for_spoken_yes_fails_closed(ctl, fake_hass, load, monkeypatch):
    assert ctl._hold_for_spoken_yes(fake_hass, "satA", "deferred") is True
    assert ctl._hold_for_spoken_yes(fake_hass, "satA", "rejected") is False
    assert ctl._hold_for_spoken_yes(fake_hass, "", "deferred") is False

    def boom(*a, **k):
        raise RuntimeError("registry down")
    monkeypatch.setattr(load("voice_confirm"), "is_voice_satellite_device", boom)
    assert ctl._hold_for_spoken_yes(fake_hass, "satA", "deferred") is False
