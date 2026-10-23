"""Gap 1 (8.25.0): the night sweep stops fighting people.

Nova does not fight a person at the door (8.7.20), and now the night sweep
follows that rule: a device it could not secure, or one opened again after it
secured it, is left alone until morning with one phone alert, and an hourly
cap on commands is the backstop. Each scenario runs the real sweep every five
minutes through the real tick. All state is fake.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, run_sweeps  # noqa: F401
import sys
import types
from cognitive_safety_kit import _isolated_core, cc, clock, service_calls  # noqa: F401


@pytest.fixture
def safety(cc, fake_hass):
    return cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})


def obey(fake_hass, *jammed):
    """Devices change state when commanded, except the jammed ones."""
    real = fake_hass.services.async_call

    async def call(domain, service, data=None, blocking=False, **kw):
        await real(domain, service, data, blocking=blocking, **kw)
        eid = (data or {}).get("entity_id")
        new = {("lock", "lock"): "locked", ("cover", "close_cover"): "closed"}.get(
            (domain, service))
        st = fake_hass.states.get(eid) if isinstance(eid, str) else None
        if st is not None and new and eid not in jammed:
            fake_hass.states.set(eid, new, **dict(st.attributes))
    fake_hass.services.async_call = call


async def night(safety, hass, clock, sweeps, between=None):
    """Run `sweeps` sweeps five minutes apart; `between` changes the house
    after each one. Returns every alert delivered, in order."""
    delivered = []
    for i in range(sweeps):
        await safety.tick(sleeping=True, anyone_home=True)
        delivered += await run_sweeps(safety, hass)
        clock["now"] += 301
        if between:
            between(i)
    return delivered


def commands(hass, service):
    return [c[2]["entity_id"] for c in hass.service_calls if c[1] == service]


async def test_a_reversing_garage_gets_one_attempt_and_one_alert(safety, fake_hass, clock):
    obey(fake_hass, "cover.garage")                       # an obstruction: it reverses
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    alerts = await night(safety, fake_hass, clock, 6)
    assert commands(fake_hass, "close_cover") == ["cover.garage"]
    assert len(alerts) == 1 and alerts[0]["urgency"] == "high"
    assert "Garage" in alerts[0]["message"]


async def test_a_door_unlocked_by_hand_is_not_locked_again(safety, fake_hass, clock):
    obey(fake_hass)
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")

    def someone_steps_out(i):
        if i == 0:                                        # after Nova locked it
            fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    alerts = await night(safety, fake_hass, clock, 6, someone_steps_out)
    assert commands(fake_hass, "lock") == ["lock.back"]
    assert [a["urgency"] for a in alerts] == ["low", "high"]
    assert alerts[0]["message"].endswith("The house is secured.")
    assert "opened again after I secured it" in alerts[1]["message"]
    assert "The house is secured." not in alerts[1]["message"]


async def test_a_normal_night_is_unchanged(safety, fake_hass, clock):
    obey(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("cover.garage", "open", device_class="garage", friendly_name="Garage")
    alerts = await night(safety, fake_hass, clock, 6)
    assert commands(fake_hass, "lock") == ["lock.front"]
    assert commands(fake_hass, "close_cover") == ["cover.garage"]
    assert len(alerts) == 1 and alerts[0]["urgency"] == "low"
    assert alerts[0]["message"].endswith("The house is secured.")


async def test_a_device_opened_again_later_by_something_else_is_still_secured(
        safety, fake_hass, clock):
    # A lock Nova never locked tonight is not "left alone": it is locked.
    obey(fake_hass)
    fake_hass.states.set("lock.front", "locked", friendly_name="Front")

    def late_unlock(i):
        if i == 1:
            fake_hass.states.set("lock.side", "unlocked", friendly_name="Side")
    await night(safety, fake_hass, clock, 4, late_unlock)
    assert commands(fake_hass, "lock") == ["lock.side"]


async def test_the_memory_starts_afresh_the_next_night(safety, fake_hass, clock):
    obey(fake_hass, "lock.back")
    fake_hass.states.set("lock.back", "unlocked", friendly_name="Back")
    await night(safety, fake_hass, clock, 2)
    assert commands(fake_hass, "lock") == ["lock.back"]
    clock["now"] += 24 * 3600                             # the next night
    await night(safety, fake_hass, clock, 2)
    assert commands(fake_hass, "lock") == ["lock.back", "lock.back"]


async def test_the_hourly_cap_is_the_backstop(safety, fake_hass, clock, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules["jc.core_safety"], "SWEEP_MAX_COMMANDS_PER_HOUR", 3)
    obey(fake_hass)
    for n in range(5):
        fake_hass.states.set(f"lock.l{n}", "unlocked", friendly_name=f"L{n}")
    await night(safety, fake_hass, clock, 1)
    assert len(commands(fake_hass, "lock")) == 3          # the rest wait
    clock["now"] += 3600                                  # an hour later
    await night(safety, fake_hass, clock, 1)
    assert len(commands(fake_hass, "lock")) == 5


def test_the_default_cap(load):
    assert load("core_safety").SWEEP_MAX_COMMANDS_PER_HOUR == 12


# ── the sweep checks after acting and never speaks at night ───────────────────

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


def obey_stc(fake_hass, *jammed):
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


def test_the_sweep_waits_about_25_seconds_by_default(cc, fake_hass):
    assert cc.SafetyManager(fake_hass, {}).sweep_verify_delay == 25


async def test_a_jammed_lock_shows_as_not_secured(sweep, fake_hass, al, records):
    obey_stc(fake_hass, "lock.back")
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
    obey_stc(fake_hass)
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
    og = load("output_gate")
    monkeypatch.setattr(og, "_STATE", og.GateState())
    return out


@pytest.mark.parametrize("jammed", [False, True])
async def test_nothing_is_spoken_at_night(sweep, fake_hass, speakers, clock, jammed):
    obey_stc(fake_hass, *(["lock.front"] if jammed else []))
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    actions = await sweep.tick(sleeping=True, anyone_home=True)
    assert [a for a in actions if a.get("type") == "lockdown"] == []   # not before checking
    await fake_hass.drain()
    assert speakers["spoken"] == []
    # What the sweep changed always reaches the phones (8.45.0), even the
    # low "secured" note that sleep would otherwise drop.
    assert [t for t, _ in speakers["pushed"]] == ["lockdown"]
    assert ("not fully secured" in speakers["pushed"][0][1]) is jammed
