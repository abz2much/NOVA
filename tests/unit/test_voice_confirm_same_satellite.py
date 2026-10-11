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


# ── part 2: the answer ──────────────────────────────────────────────────────

@pytest.mark.parametrize("said", ["Yes.", "yes please", "Nova, yes", "OK", "go ahead",
                                  "Yeah!", "do it"])
async def test_a_clear_yes_replays_the_phrase_once(pol, pc, cfg, fake_hass, calls, said):
    _p, fake = pol
    fake.enabled = True
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [
        _phrase(targets=("script.a", "script.b"))]}}
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    res = await pc.answer(fake_hass, "satA", said, honorific="sir")
    assert res.handled and res.success and res.text == "Done, sir."
    assert calls == [("script", "turn_on", {"entity_id": ["script.a", "script.b"]})]
    assert pc.get("satA") is None and fake.asked == []


@pytest.mark.parametrize("said", ["unlock it", "open it", "yes unlock the door",
                                  "is it", "maybe"])
async def test_anything_else_clears_it_and_carries_on(pc, fake_hass, calls, said):
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    assert await pc.answer(fake_hass, "satA", said) is None
    assert pc.get("satA") is None and calls == []


@pytest.mark.parametrize("said", ["No.", "nope", "don't", "cancel", "never mind"])
async def test_a_clear_no_clears_it(pc, fake_hass, calls, said):
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    res = await pc.answer(fake_hass, "satA", said)
    assert res.text == "OK, I won't." and pc.get("satA") is None and calls == []


async def test_another_device_never_reads_or_clears_it(pc, fake_hass, calls):
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    assert await pc.answer(fake_hass, "satB", "yes") is None
    assert await pc.answer(fake_hass, "", "yes") is None
    assert pc.get("satA") is not None and calls == []


async def test_a_scene_or_script_is_replayed_through_its_tool(pol, pc, fake_hass, calls):
    _p, fake = pol
    fake.enabled = True
    pc.put("satA", pc.KIND_SCENE_SCRIPT, "automation.heat", "u1")
    res = await pc.answer(fake_hass, "satA", "yes")
    assert res.success and res.text == "Done."
    assert calls == [("automation", "trigger", {"entity_id": "automation.heat"})]


async def test_a_failed_scene_replay_says_so(pol, pc, fake_hass, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(fake_hass.services, "async_call", boom)
    pc.put("satA", pc.KIND_SCENE_SCRIPT, "scene.movie", "u1")
    res = await pc.answer(fake_hass, "satA", "yes")
    assert not res.success and res.text == "I couldn't do that."


@pytest.mark.parametrize("change", ["gone", "off", "switch_off", "shell"])
async def test_a_phrase_that_changed_since_is_not_run(pol, pc, cfg, fake_hass, calls, change):
    entry = _phrase()
    if change == "off":
        entry["enabled"] = False
    if change == "shell":
        entry["action"] = {"service": "shell_command.x", "data": {}}
    entries = [] if change == "gone" else [entry]
    cfg._cache = {"voice_phrases": {"enabled": change != "switch_off", "entries": entries}}
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    res = await pc.answer(fake_hass, "satA", "yes")
    assert not res.success and "isn't available" in res.text and calls == []


async def test_the_replay_still_needs_the_phone_for_unlock(pol, pc, cfg, fake_hass, calls):
    _p, fake = pol
    fake.phone_answer = "rejected"
    entry = _phrase(targets=("lock.front",))
    entry["action"]["service"] = "lock.unlock"
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [entry]}}
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    res = await pc.answer(fake_hass, "satA", "yes")
    assert not res.success and calls == [] and [h for h, _q in fake.asked] == ["phone"]


async def test_answer_never_raises(pc, fake_hass, load, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config down")
    monkeypatch.setattr(load("nova_config"), "get", boom)
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    res = await pc.answer(fake_hass, "satA", "yes")
    assert not res.success and pc.get("satA") is None


async def test_a_replay_with_no_result_says_so(pc, cfg, fake_hass, load, monkeypatch):
    run = load("voice_phrase_runner")
    cfg._cache = {"voice_phrases": {"enabled": True, "entries": [_phrase()]}}

    async def nothing(*a, **k):
        return run.RunOutcome(None, False, "failed")
    monkeypatch.setattr(run, "run_entry", nothing)
    pc.put("satA", pc.KIND_PHRASE, "vp_3f9c1a2b", "u1")
    assert (await pc.answer(fake_hass, "satA", "yes")).text == "I couldn't run that."


# ── part 2: the reopen never hands off a held confirmation ──────────────────

async def test_a_held_reopen_stays_on_the_same_satellite(load, fake_hass, monkeypatch):
    cc = load("continued_conversation")
    monkeypatch.setattr(cc, "_pick_reopen_target", lambda hass, sat: "assist_satellite.other")
    seen = []

    async def async_call(domain, service, data=None, blocking=False, **kw):
        seen.append(data["entity_id"])
    monkeypatch.setattr(fake_hass.services, "async_call", async_call)

    async def no_sleep(_s):
        return None
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    fake_hass.states.set("media_player.living", "idle")
    await cc._reopen_after_speaker(fake_hass, "assist_satellite.a", "media_player.living",
                                   "ok?", follow=False)
    await cc._reopen_after_speaker(fake_hass, "assist_satellite.a", "media_player.living",
                                   "ok?")
    assert seen == ["assist_satellite.a", "assist_satellite.other"]
