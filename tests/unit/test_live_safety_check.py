"""8.46.0 (design D1): the live check before a lock, cover, garage or alarm
command (safety_check.py, run first by policy.authorize / authorize_now).

* Unknown, unavailable, jammed or missing devices are refused: unknown
  never passes, for Nova acting on its own or for a person's request.
* A device already locked, closed or armed that way gets nothing.
* A lock whose door reads open: Nova acting on its own skips it and reports
  it; a person's own request is warned and asked first.
* Arming with doors or windows open: the person is asked, naming them.
* The device is read at the moment of the command, never from an older
  reading, and again after a yes.

All state is fake. Nothing touches a real lock, cover or alarm.
"""
import pytest

from fakes import FakeEntityRegistry, FakeRegistryEntry


class _Confirm:
    """voice_confirm behind the real confirm_gate: records each question."""

    def __init__(self):
        self.answer = "approved"
        self.asked = []
        self.on_ask = None


@pytest.fixture
def pol(load, monkeypatch):
    p = load("policy")
    vc = load("voice_confirm")
    c = _Confirm()
    monkeypatch.setattr(vc, "is_enabled", lambda hass: False)
    monkeypatch.setattr(vc, "is_voice_satellite_device", lambda hass, d, strict=False: False)

    async def confirm_typed(hass, question, entity_id=""):
        c.asked.append(question)
        if c.on_ask is not None:
            c.on_ask()
        return c.answer
    monkeypatch.setattr(vc, "confirm_typed", confirm_typed)
    return p, c


@pytest.fixture
def door_on_lock(monkeypatch):
    """lock.front and binary_sensor.front_door belong to one device."""
    import sys
    reg = FakeEntityRegistry()
    reg.add(FakeRegistryEntry("lock.front", "x", device_id="dev1"))
    reg.add(FakeRegistryEntry("binary_sensor.front_door", "x", device_id="dev1"))
    reg.add(FakeRegistryEntry("binary_sensor.kitchen_window", "x", device_id="dev2"))
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: reg)
    return reg


def _req(p, domain, service, entity, source="chat"):
    return p.AuthorityRequest(domain, service, entity, source=source,
                              label=service.replace("_", " "))


# ── unreadable devices fail closed ──────────────────────────────────────────

@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
@pytest.mark.parametrize("domain,service,entity", [
    ("lock", "lock", "lock.front"),
    ("lock", "unlock", "lock.front"),
    ("cover", "close_cover", "cover.garage"),
    ("cover", "open_cover", "cover.garage"),
    ("alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home"),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home"),
])
@pytest.mark.parametrize("source", ["automatic", "chat"])
async def test_an_unreadable_device_is_refused(pol, fake_hass, state, domain, service,
                                               entity, source):
    p, c = pol
    if state is not None:
        fake_hass.states.set(entity, state)
    req = _req(p, domain, service, entity, source)
    for d in (p.authorize_now(fake_hass, req), await p.authorize(fake_hass, req)):
        assert not d.allowed and d.code == "device_unreadable", d
        assert "can't be read" in d.note
    assert c.asked == []


async def test_a_jammed_lock_is_refused(pol, fake_hass):
    p, _c = pol
    fake_hass.states.set("lock.front", "jammed")
    d = await p.authorize(fake_hass, _req(p, "lock", "lock", "lock.front"))
    assert not d.allowed and d.code == "device_unreadable"


async def test_an_unreadable_door_sensor_refuses_the_lock(pol, fake_hass, door_on_lock):
    p, c = pol
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("binary_sensor.front_door", "unavailable", device_class="door")
    for source in ("automatic", "chat"):
        d = await p.authorize(fake_hass, _req(p, "lock", "lock", "lock.front", source))
        assert not d.allowed and d.code == "door_unreadable"
    assert c.asked == []


async def test_arming_with_an_unreadable_zone_is_refused(pol, fake_hass):
    p, c = pol
    fake_hass.states.set("alarm_control_panel.home", "disarmed")
    fake_hass.states.set("binary_sensor.back_door", "unavailable", device_class="door",
                         friendly_name="Back door")
    d = await p.authorize(fake_hass, _req(p, "alarm_control_panel", "alarm_arm_away",
                                          "alarm_control_panel.home"))
    assert not d.allowed and d.code == "zones_unreadable"
    assert "Back door" in d.note and c.asked == []


async def test_scenes_and_scripts_are_not_live_checked(pol, fake_hass, load):
    """Nova cannot see into them; they keep their confirmation as before."""
    sc = load("safety_check")
    assert not sc.applies("scene", "turn_on", "scene.movie")
    assert not sc.applies("script", "turn_on", "script.bedtime")
    assert not sc.applies("lock", "lock", "")


# ── already secure: nothing is sent ─────────────────────────────────────────

@pytest.mark.parametrize("domain,service,entity,state", [
    ("lock", "lock", "lock.front", "locked"),
    ("cover", "close_cover", "cover.garage", "closed"),
    ("alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home", "armed_away"),
    ("alarm_control_panel", "alarm_arm_night", "alarm_control_panel.home", "armed_night"),
])
@pytest.mark.parametrize("source", ["automatic", "chat"])
async def test_an_already_secure_device_gets_nothing(pol, fake_hass, domain, service,
                                                     entity, state, source):
    p, c = pol
    fake_hass.states.set(entity, state, friendly_name="Thing")
    d = await p.authorize(fake_hass, _req(p, domain, service, entity, source))
    assert not d.allowed and d.already_secure and d.code == "already_secure"
    assert d.note == f"Thing is already {state.replace('_', ' ')}, so nothing was sent."
    assert c.asked == []


async def test_arming_a_different_mode_is_not_already_secure(pol, fake_hass):
    p, _c = pol
    fake_hass.states.set("alarm_control_panel.home", "armed_home")
    d = p.authorize_now(fake_hass, _req(p, "alarm_control_panel", "alarm_arm_away",
                                        "alarm_control_panel.home", "chat"))
    assert not d.already_secure


async def test_secure_device_sends_nothing_when_already_secure(load, fake_hass):
    lockdown = load("core_lockdown")
    fake_hass.states.set("lock.front", "locked")
    fake_hass.states.set("cover.garage", "closed")
    assert await lockdown.secure_device(fake_hass, "lock.front", "lock") == "already_secure"
    assert await lockdown.secure_device(fake_hass, "cover.garage", "cover") == "already_secure"
    assert fake_hass.service_calls == []


async def test_the_chat_tool_says_already_locked_and_sends_nothing(load, fake_hass,
                                                                    monkeypatch):
    import json
    ctl = load("agent_runtime.capabilities.control")
    al = load("action_log")
    executions = []
    monkeypatch.setattr(al, "start", lambda *a, **k: 7)
    monkeypatch.setattr(al, "mark_awaiting_approval", lambda *a, **k: True)
    monkeypatch.setattr(al, "set_approval", lambda *a, **k: True)
    monkeypatch.setattr(al, "set_execution",
                        lambda aid, result, **k: executions.append((result, k.get("reason_code"))))
    fake_hass.states.set("lock.front", "locked", friendly_name="Front door")
    out = json.loads(await ctl._exec_control_device(
        fake_hass, {"entity_id": "lock.front", "action": "lock"}))
    assert out["status"] == "already_secure" and out["success"] is True
    assert out["message"] == "Front door is already locked, so nothing was sent."
    assert fake_hass.service_calls == []
    assert executions == [("blocked", "already_secure")]


# ── an open door ────────────────────────────────────────────────────────────

async def test_nova_on_its_own_skips_a_lock_whose_door_is_open(pol, fake_hass, door_on_lock,
                                                               load):
    p, c = pol
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front lock")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front door")
    d = p.authorize_now(fake_hass, _req(p, "lock", "lock", "lock.front", "automatic"))
    assert not d.allowed and d.code == "door_open"
    assert d.note == "Front door is open, so Front lock was not locked."
    lockdown = load("core_lockdown")
    with pytest.raises(lockdown.NotSecured) as exc:
        await lockdown.secure_device(fake_hass, "lock.front", "lock")
    assert exc.value.code == "door_open" and "Front door is open" in exc.value.note
    assert fake_hass.service_calls == [] and c.asked == []


async def test_a_door_sensor_on_another_device_does_not_count(pol, fake_hass, door_on_lock):
    p, _c = pol
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("binary_sensor.kitchen_window", "on", device_class="door")
    d = p.authorize_now(fake_hass, _req(p, "lock", "lock", "lock.front", "automatic"))
    assert d.allowed


@pytest.mark.parametrize("answer,allowed", [("approved", True), ("rejected", False),
                                            ("expired", False)])
async def test_a_person_is_warned_about_an_open_door_and_asked(pol, fake_hass, door_on_lock,
                                                               answer, allowed):
    p, c = pol
    c.answer = answer
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front lock")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front door")
    d = await p.authorize(fake_hass, p.AuthorityRequest(
        "lock", "lock", "lock.front", source="chat", label="lock", target_name="Front lock"))
    assert c.asked == ["Front door is open. lock Front lock anyway — are you sure?"]
    assert d.allowed is allowed
    if not allowed:
        assert d.code == "door_open"


async def test_where_nothing_can_be_asked_an_open_door_is_held(pol, fake_hass, door_on_lock):
    p, c = pol
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front door")
    d = p.authorize_now(fake_hass, _req(p, "lock", "lock", "lock.front", "voice"))
    assert not d.allowed and d.approval == "deferred" and d.code == "door_open"
    assert "Front door is open" in d.note and c.asked == []


async def test_the_nighttime_sweep_reports_a_skipped_lock(load, fake_hass, door_on_lock,
                                                          monkeypatch):
    """The lockdown names the lock as not locked, logs why, and sends nothing."""
    lockdown = load("core_lockdown")
    al = load("action_log")
    rows = []
    monkeypatch.setattr(al, "start_many",
                        lambda rid, action, source, targets, **k: {t["key"]: 1 for t in targets})
    monkeypatch.setattr(al, "set_execution",
                        lambda aid, result, **k: rows.append((result, k.get("reason_code"))))
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front lock")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    mgr = lockdown.LockdownManager.__new__(lockdown.LockdownManager)
    mgr.hass, mgr.config, mgr.exempt_locks, mgr._automatic_generation = fake_hass, {}, set(), 0
    mgr.since, mgr.auto = 0.0, False
    recorded = []

    async def _record(decision, reason, **kw):
        recorded.append((decision, reason, kw.get("assessment")))
    mgr._record = _record
    locked = await mgr._lock_all(request_id="r1")
    assert locked == [] and mgr._lock_failures == [("lock.front", "Front lock")]
    assert rows == [("blocked", "door_open")]
    assert recorded == [("not locked", "binary_sensor.front_door is open, so Front lock "
                                        "was not locked.", "door_open")]
    assert fake_hass.service_calls == []


# ── arming with open zones ──────────────────────────────────────────────────

async def test_arming_with_open_zones_asks_and_names_them(pol, fake_hass):
    p, c = pol
    fake_hass.states.set("alarm_control_panel.home", "disarmed", friendly_name="Alarm")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door",
                         friendly_name="Back door")
    fake_hass.states.set("binary_sensor.study_window", "on", device_class="window",
                         friendly_name="Study window")
    d = await p.authorize(fake_hass, p.AuthorityRequest(
        "alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home",
        source="chat", label="arm", target_name="Alarm"))
    assert c.asked == ["Back door and Study window are open. arm Alarm anyway — are you sure?"]
    assert d.allowed


async def test_arming_with_everything_shut_asks_nothing_extra(pol, fake_hass):
    p, c = pol
    fake_hass.states.set("alarm_control_panel.home", "disarmed")
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door")
    d = await p.authorize(fake_hass, _req(p, "alarm_control_panel", "alarm_arm_away",
                                          "alarm_control_panel.home"))
    assert d.allowed and c.asked == []


async def test_nova_never_arms_on_its_own_with_open_zones(pol, fake_hass):
    p, _c = pol
    fake_hass.states.set("alarm_control_panel.home", "disarmed")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    d = p.authorize_now(fake_hass, _req(p, "alarm_control_panel", "alarm_arm_away",
                                        "alarm_control_panel.home", "automatic"))
    assert not d.allowed and d.code == "zones_open"


# ── read at the moment of the command, and again after a yes ────────────────

async def test_an_older_reading_is_never_used_for_the_command(pol, fake_hass, load):
    """The tick's snapshot still says unlocked, but the lock now reads
    locked: the live check reads the lock itself, so nothing is sent."""
    p, _c = pol
    world = load("world")
    fake_hass.states.set("lock.front", "unlocked")
    snap = world.begin_tick(fake_hass, {})
    try:
        assert snap.unlocked_locks == ("lock.front",)
        fake_hass.states.set("lock.front", "locked")
        d = p.authorize_now(fake_hass, _req(p, "lock", "lock", "lock.front", "automatic"))
        assert d.already_secure
    finally:
        world.end_tick()


async def test_a_device_lost_while_waiting_for_the_yes_is_refused(pol, fake_hass,
                                                                   door_on_lock):
    p, c = pol
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front lock")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    c.on_ask = lambda: fake_hass.states.set("lock.front", "unavailable")
    d = await p.authorize(fake_hass, _req(p, "lock", "lock", "lock.front"))
    assert len(c.asked) == 1                       # asked about the open door
    assert not d.allowed and d.code == "device_unreadable"


async def test_a_zone_that_opens_while_waiting_for_the_yes_is_refused(pol, fake_hass):
    p, c = pol
    fake_hass.states.set("alarm_control_panel.home", "disarmed")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door",
                         friendly_name="Back door")
    c.on_ask = lambda: fake_hass.states.set("binary_sensor.side_door", "on",
                                            device_class="door", friendly_name="Side door")
    d = await p.authorize(fake_hass, _req(p, "alarm_control_panel", "alarm_arm_away",
                                          "alarm_control_panel.home"))
    assert not d.allowed and d.code == "zones_open"
    assert d.note == "Side door is now open as well, so nothing was sent."


async def test_what_the_person_was_warned_about_may_still_be_open_after_the_yes(
        pol, fake_hass, door_on_lock):
    p, c = pol
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    d = await p.authorize(fake_hass, _req(p, "lock", "lock", "lock.front"))
    assert len(c.asked) == 1 and d.allowed


async def test_a_confirmation_failure_with_a_warning_fails_closed(pol, fake_hass, door_on_lock,
                                                                  load, monkeypatch):
    """Locking is low risk, so an error in the confirmation used to let it
    through. With an open door to warn about, it is refused instead."""
    p, _c = pol

    async def boom(*a, **k):
        raise RuntimeError("no speaker")
    monkeypatch.setattr(load("voice_confirm"), "confirm_typed", boom)
    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    d = await p.authorize(fake_hass, _req(p, "lock", "lock", "lock.front"))
    assert not d.allowed
