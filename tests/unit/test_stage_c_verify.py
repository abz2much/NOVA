"""Stage C (8.24.0): check after acting, and record the outcome.

* The night sweep rereads each lock and cover before it says anything is
  secured; a jammed lock is named as not secured and raised as a high alert
  that, at night, goes to the phones and is never spoken.
* The voice "secure" reply checks the same way and logs the result.
* Scenes and scripts are marked "not checkable", and nothing more is promised.
* A check that cannot run is "unverified", never "verified".

All state is fake; nothing touches a real lock, cover, speaker or phone.
"""
import sys
import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, service_calls  # noqa: F401


@pytest.fixture
def al(load):
    return load("action_log")


@pytest.fixture
def records(load, tmp_path, monkeypatch):
    dr = load("decision_record")
    monkeypatch.setattr(dr, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    return lambda kind=None: dr.recent(limit=50, kind=kind)


def _rows(al):
    return {t["entity_id"]: t for p in al.page_requests()["requests"] for t in p["targets"]}


def obey(fake_hass, *jammed):
    """Devices change state when commanded, except the jammed ones."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        if isinstance(eid, list):
            eids = eid
        else:
            eids = [eid]
        for e in eids:
            st = fake_hass.states.get(e)
            if st is not None and new and e not in jammed:
                fake_hass.states.set(e, new, **dict(st.attributes))
    fake_hass.services.async_call = call


@pytest.fixture
def sweep(cc, fake_hass):
    s = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    s.sweep_verify_delay = 0
    return s


# ── the night sweep ─────────────────────────────────────────────────────────

def test_the_sweep_waits_about_25_seconds_by_default(cc, fake_hass):
    assert cc.SafetyManager(fake_hass, {}).sweep_verify_delay == 25


async def test_a_jammed_lock_shows_as_not_secured(sweep, fake_hass, al, records):
    obey(fake_hass, "lock.back")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    (action,) = await sweep._nighttime_lockdown(0)
    assert "The house is secured." not in action["message"]
    assert "Back" in action["message"] and "not fully secured" in action["message"]
    assert action["urgency"] == "high"
    rows = _rows(al)
    assert rows["lock.front"]["execution_result"] == "verified"
    assert rows["lock.back"]["execution_result"] == "unverified"
    assert rows["lock.back"]["reason_code"] == "not_secure_after_check"
    (rec,) = records("lockdown_sweep")
    assert rec["observation"]["locked"] == ["Front"]
    assert rec["observation"]["not_secured_after_check"] == ["Back"]


async def test_secured_is_only_said_after_the_check(sweep, fake_hass, monkeypatch):
    obey(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    seen = []
    real_sleep = sys.modules["asyncio"].sleep

    async def watch(delay):
        # At the moment of waiting, nothing has been reported yet.
        seen.append(delay)
        await real_sleep(0)
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", watch)
    sweep.sweep_verify_delay = 25
    (action,) = await sweep._nighttime_lockdown(0)
    assert seen == [25] and action["message"].endswith("The house is secured.")


async def test_a_lock_that_cannot_be_read_is_unverified(sweep, fake_hass, al):
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    real = fake_hass.services.async_call

    async def vanish(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        fake_hass.states.remove(data["entity_id"])
    fake_hass.services.async_call = vanish
    (action,) = await sweep._nighttime_lockdown(0)
    assert "The house is secured." not in action["message"]
    assert _rows(al)["lock.front"]["execution_result"] == "unverified"


@pytest.fixture
def speakers(cc, load, monkeypatch, fake_hass):
    """The real delivery path with fake speakers and phones."""
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    out = {"spoken": [], "pushed": []}

    async def _push(hass, config, message, action_type, snap=None, **kw):
        out["pushed"].append((action_type, message))
    monkeypatch.setattr(cc, "_push_notification", _push)
    monkeypatch.setattr(cc, "_notify_all_devices", _push)
    tts = types.ModuleType("jc.tts_helper")
    tts.resolve_tts_for_context = lambda *a, **k: "tts.x"

    async def _announce(hass, message, *a, **k):
        out["spoken"].append(message)
    tts.async_announce = _announce
    ar = types.ModuleType("jc.audio_routing")
    ar.observer_speak_target = lambda *a, **k: (["media_player.x"], "broadcast")
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    monkeypatch.setitem(sys.modules, "jc.audio_routing", ar)
    sd = types.ModuleType("jc.sleep_detection")
    sd._in_quiet_hours = lambda *a: False
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return out


@pytest.mark.parametrize("jammed", [False, True])
async def test_nothing_is_spoken_at_night(sweep, fake_hass, speakers, clock, jammed):
    obey(fake_hass, *(["lock.front"] if jammed else []))
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    actions = await sweep.tick(sleeping=True, anyone_home=True)
    assert [a for a in actions if a.get("type") == "lockdown"] == []   # not before checking
    await fake_hass.drain()
    assert speakers["spoken"] == []
    assert [t for t, _ in speakers["pushed"]] == ["lockdown"]
    assert ("not fully secured" in speakers["pushed"][0][1]) is jammed


# ── the voice "secure" reply ────────────────────────────────────────────────

def _router(load, fake_hass):
    ir = load("intent.intent_router")
    r = ir.LocalIntentRouter(fake_hass)
    r._area_of = lambda eid: "hall"
    r.verify_delay = 0
    return r


async def test_the_voice_reply_checks_what_it_secured(load, cc, fake_hass, al, records,
                                                      speakers):
    obey(fake_hass, "lock.back")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    out = await _router(load, fake_hass).execute({"intent": "secure_area"}, "hall")
    assert sorted(out["entities"]) == ["lock.back", "lock.front"]
    await fake_hass.drain()
    rows = _rows(al)
    assert rows["lock.front"]["execution_result"] == "verified"
    assert rows["lock.back"]["execution_result"] == "unverified"
    (rec,) = records("voice_secure")
    assert rec["observation"]["secured"] == ["lock.front"]
    assert rec["observation"]["not_secured_after_check"] == ["lock.back"]
    assert [t for t, _ in speakers["pushed"]] == ["voice_secure_failed"]
    assert "Back" in speakers["pushed"][0][1]


async def test_a_voice_reply_that_secured_everything_raises_nothing(load, cc, fake_hass, al,
                                                                    speakers):
    obey(fake_hass)
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    await _router(load, fake_hass).execute({"intent": "secure_area"}, "hall")
    await fake_hass.drain()
    assert _rows(al)["cover.garage"]["execution_result"] == "verified"
    assert speakers["pushed"] == [] and speakers["spoken"] == []


# ── scenes and scripts ──────────────────────────────────────────────────────

@pytest.mark.parametrize("eid", ["scene.movie", "script.bedtime"])
async def test_a_scene_or_script_is_marked_not_checkable(load, fake_hass, al, eid):
    ctl = load("agent_runtime.capabilities.control")
    fake_hass.states.set(eid, "off", friendly_name="Thing")
    res = await ctl._exec_run_scene_script(fake_hass, {"entity_id": eid})
    assert '"success": true' in res.lower()
    row = _rows(al)[eid]
    assert row["execution_result"] == "unverified" and row["reason_code"] == "not_checkable"


# ── lockdown's own check ────────────────────────────────────────────────────

async def test_lockdown_records_its_checks_linked_to_the_lockdown(load, cc, fake_hass,
                                                                  records, monkeypatch):
    monkeypatch.setattr(sys.modules["jc.core_lockdown"], "LOCKDOWN_SECURE_VERIFY_DELAY", 0)
    obey(fake_hass)
    mgr = cc.LockdownManager(fake_hass, {})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    await mgr.engage("requested")
    fake_hass.states.remove("lock.side")         # cannot be read at the check
    await fake_hass.drain()
    rows = records("lockdown")
    by = {r["decision"]: r for r in rows}
    assert {"lockdown engaged", "checked, secured", "could not check"} <= set(by)
    refs = {r["ref"] for r in rows}
    assert len(refs) == 1 and next(iter(refs)).startswith("lockdown:")
    assert by["could not check"]["interpretation"] == {"assessment": "unverified"}
