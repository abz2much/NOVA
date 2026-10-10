"""Every safety decision is written to the Decision Record in one format
(8.23.0): intrusion, lockdown, the nighttime sweep, hazards, packages and
door alerts. Also: a medium alert while everyone is away reaches the phones.

All state here is fake, and the Decision Record is a temporary file. Nothing
touches a real alarm, lock, speaker or phone.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, service_calls  # noqa: F401
from test_alert_path_unchanged import (  # noqa: F401  (fixtures)
    SPEAKER, _emit, _people, core, hazard, package, sentinel)

ALARM = "alarm_control_panel.home_security"
SHARED = {"source", "residents", "posture", "asleep", "quiet_hours"}


@pytest.fixture(autouse=True)
def records(load, tmp_path, monkeypatch):
    dr = load("decision_record")
    monkeypatch.setattr(dr, "_DEFAULT_DB", str(tmp_path / "decisions.db"))

    def _read(kind=None):
        return dr.recent(limit=50, kind=kind)
    return _read


def _one(records, kind):
    rows = records(kind)
    assert len(rows) == 1, [r["kind"] for r in records()]
    row = rows[0]
    assert SHARED <= set(row["observation"]), row["observation"]
    assert row["decision"]
    return row


# ── intrusion ───────────────────────────────────────────────────────────────

async def test_the_first_intrusion_alert(load, fake_hass, clock, records):
    cc = load("cognitive_core")
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    row = _one(records, "intrusion")
    obs = row["observation"]
    assert obs["source"] == "intrusion" and obs["residents"] == "away"
    assert obs["posture"] == "away" and obs["trigger"] == "away"
    assert obs["entity_id"] == "binary_sensor.hall_motion" and obs["alarm_armed"] is True
    assert row["decision"] == "raise initial intrusion alert and investigate silently"


@pytest.mark.parametrize("kind", ["intrusion_confirmed", "intrusion_unresolved"])
async def test_a_confirmed_or_unresolved_intrusion(core, fake_hass, records, kind):
    _people(fake_hass, "away")
    await _emit(core, fake_hass, "critical" if kind == "intrusion_confirmed" else "high",
                type=kind, message="Intrusion.")
    row = _one(records, kind)
    assert row["evidence"]["pushed"] is True
    assert row["observation"]["message"] == "Intrusion."


async def test_the_confirmed_intrusion_still_uses_the_speakers(core, fake_hass, records):
    _people(fake_hass, "home")
    await _emit(core, fake_hass, "critical", type="intrusion_confirmed", message="Intrusion.")
    row = _one(records, "intrusion_confirmed")
    assert row["evidence"]["spoken"] is True and row["decision"] == "spoken and pushed to phones"


async def test_other_core_alerts_are_not_recorded_here(core, fake_hass, records):
    _people(fake_hass, "home")
    await _emit(core, fake_hass, "high", type="intrusion_investigating")
    await _emit(core, fake_hass, "low", type="proactive_hvac")
    assert records() == []


# ── lockdown and the nighttime sweep ────────────────────────────────────────

async def test_lockdown_engaged_and_lifted(load, fake_hass, records):
    cc = load("cognitive_core")
    mgr = cc.LockdownManager(fake_hass, {})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("binary_sensor.kitchen_window", "on", device_class="window",
                         friendly_name="Kitchen window")
    await mgr.engage("alarm armed")
    await mgr.disengage("alarm disarmed")
    rows = {r["decision"]: r for r in records("lockdown")}
    assert set(rows) == {"lockdown engaged", "lockdown lifted"}
    engaged = rows["lockdown engaged"]
    assert engaged["observation"]["locked"] == ["Front"]
    assert engaged["observation"]["left_open"] == ["Kitchen window"]
    assert engaged["reason"] == "alarm armed"
    for row in rows.values():
        assert SHARED <= set(row["observation"])


async def test_a_door_opened_during_lockdown(load, fake_hass, records):
    cc = load("cognitive_core")
    mgr = cc.LockdownManager(fake_hass, {})
    await mgr.engage("requested")
    fake_hass.states.set("cover.garage", "closed", device_class="garage")
    old = fake_hass.states.get("cover.garage")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    await mgr.handle_state_change("cover.garage", old, fake_hass.states.get("cover.garage"))
    fake_hass.close_pending()
    row = [r for r in records("lockdown") if r["decision"] == "secured again"]
    assert len(row) == 1 and row[0]["observation"]["entity_id"] == "cover.garage"


async def test_the_nighttime_sweep(load, fake_hass, records):
    cc = load("cognitive_core")
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    fake_hass.states.set("cover.living_blind", "open", device_class="blind",
                         friendly_name="Living blind")
    await safety._nighttime_lockdown(0)
    row = _one(records, "lockdown_sweep")
    assert row["observation"]["locked"] == ["Front"]
    assert row["observation"]["closed"] == ["Living blind"]       # bug 10 kept
    assert row["observation"]["asleep"] is True


async def test_a_sweep_with_nothing_to_do_records_nothing(load, fake_hass, records):
    cc = load("cognitive_core")
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "lockdown_auto_on_arm": True})
    fake_hass.states.set("lock.front", "locked")
    await safety._nighttime_lockdown(0)
    assert records() == []


# ── hazards ─────────────────────────────────────────────────────────────────

async def test_a_hazard(hazard, fake_hass, records):
    hm, _ = hazard
    await hm._deliver(fake_hass, "Seismic alert.", "quake", speak_text="Seismic alert.")
    row = _one(records, "hazard")
    assert row["observation"]["source"] == "hazard_earthquake"
    assert row["evidence"] == {"spoken": True, "pushed": True, "route": None}


async def test_freeze_is_recorded_as_a_hazard(core, fake_hass, records):
    _people(fake_hass, "home")
    await _emit(core, fake_hass, "critical", type="freeze_critical", message="Freezing.")
    assert _one(records, "hazard")["observation"]["source"] == "freeze_critical"


# ── packages ────────────────────────────────────────────────────────────────

async def _gone(package, hass):
    for present in (True, False):
        await package["pm"].evaluate(hass, None, "Sir", "tts.x", [SPEAKER], "camera.front_door",
                                     {"package": present, "mail": False,
                                      "count": 1 if present else 0, "description": ""})


async def test_a_package_taken_while_away(package, fake_hass, records):
    _people(fake_hass, "away")
    await _gone(package, fake_hass)
    rows = {r["observation"]["event"]: r for r in records("package")}
    assert set(rows) == {"delivered", "removed"}
    assert rows["removed"]["evidence"]["pushed"] is True
    assert rows["removed"]["observation"]["residents"] == "away"


async def test_a_package_taken_while_someone_is_home(package, fake_hass, records):
    _people(fake_hass, "home")
    await _gone(package, fake_hass)
    removed = [r for r in records("package") if r["observation"]["event"] == "removed"]
    assert len(removed) == 1 and removed[0]["decision"] == "no alert"


# ── door alerts (left open) ─────────────────────────────────────────────────

async def test_a_door_left_open(sentinel, fake_hass, records):
    rule = {"id": "door_left_open",
            "message": "{honorific}, {friendly_name} has been open for {minutes} minutes."}
    await sentinel["s"]._announce_rule("binary_sensor.front_door", rule, 10)
    row = _one(records, "left_open")
    assert row["observation"]["entity_id"] == "binary_sensor.front_door"
    assert row["observation"]["rule"] == "door_left_open"
    assert row["evidence"]["spoken"] is True and row["evidence"]["pushed"] is True


# ── a medium alert while everyone is away reaches the phones ────────────────

async def test_a_medium_alert_while_everyone_is_away_is_pushed(core, fake_hass):
    _people(fake_hass, "away")
    msg = "Front Door is unlocked while nobody appears to be home."
    await _emit(core, fake_hass, "medium", type="anticipation", message=msg)
    assert core["spoken"] == []
    assert core["pushed"] == [("anticipation", msg)]


async def test_a_medium_alert_with_someone_home_is_unchanged(core, fake_hass):
    _people(fake_hass, "home")
    await _emit(core, fake_hass, "medium", type="anticipation", message="Porch light is on.")
    assert core["spoken"] == [("Porch light is on.", [SPEAKER])] and core["pushed"] == []


async def test_a_medium_alert_with_presence_unknown_is_unchanged(core, fake_hass):
    _people(fake_hass, "unknown")
    await _emit(core, fake_hass, "medium", type="anticipation", message="Porch light is on.")
    assert core["spoken"] == [] and core["pushed"] == []


async def test_the_away_push_carries_the_rating_buttons(core, fake_hass, load, monkeypatch):
    aa = load("adaptive_awareness")
    prompts = []

    async def _prompt(*a, **k):
        prompts.append(a)
    monkeypatch.setattr(aa, "async_send_rating_prompt", _prompt)
    monkeypatch.setattr(aa, "rating_push_data", lambda decision_id: {"rate": decision_id})
    seen = []

    async def _push(hass, config, message, action_type, snap=None, *, request_id=None,
                    extra_data=None):
        seen.append(extra_data)
    monkeypatch.setattr(core["cc"], "_push_notification", _push)
    _people(fake_hass, "away")
    await _emit(core, fake_hass, "medium", type="anticipation", decision_id=7)
    assert seen == [{"rate": 7}] and prompts == []
