"""Stage A (8.24.0): habituation never quiets an alert about a lock, cover or
alarm.

Before, the anticipation alert ("Front Door is unlocked while nobody appears
to be home") carried no entity_id, so habituation could not see it was about
a lock and went quiet after three days. The alerts here are built by the real
cognition.predict and delivered by the real _emit_action, with the real
habituation store, over four days. Speakers and phones are fakes.
"""
import time as _time

import pytest

from test_cognition_predict_evidence import _seed

DAY = 86400.0


@pytest.fixture
def cog(load):
    c = load("cognition")
    c._MODEL.clear()
    c._PREDICT_COOLDOWNS.clear()
    yield c
    c._MODEL.clear()
    c._PREDICT_COOLDOWNS.clear()


@pytest.fixture
def hab(load, tmp_path, monkeypatch):
    h = load("habituation")
    monkeypatch.setattr(h, "STATE_FILE", str(tmp_path / "habituation.json"))
    monkeypatch.setattr(h, "_state", None)
    return h


@pytest.fixture
def days(load, hab, fake_hass, monkeypatch):
    """Deliver through _emit_action on successive days; returns a function
    giving what was delivered (spoken or pushed) on each day."""
    cc = load("cognitive_core")
    clock = {"now": _time.time()}
    monkeypatch.setattr(hab.time, "time", lambda: clock["now"])
    out = []

    async def _push(hass, config, message, action_type, snap=None, **kw):
        out.append(message)
    monkeypatch.setattr(cc, "_push_notification", _push)
    monkeypatch.setattr(cc, "_notify_all_devices", _push)
    tts = load("tts_helper")
    monkeypatch.setattr(tts, "resolve_tts_for_context", lambda *a, **k: "tts.x")

    async def _announce(hass, message, *a, **k):
        out.append(message)
    monkeypatch.setattr(tts, "async_announce", _announce)
    monkeypatch.setattr(load("sleep_detection"), "_in_quiet_hours", lambda *a: False)
    fake_hass.states.set("media_player.hall", "idle")

    async def run(action, n=4):
        delivered = []
        for _ in range(n):
            out.clear()
            await cc._emit_action(fake_hass, {"broadcast_group": "media_player.hall"},
                                  dict(action), False)
            delivered.append(list(out))
            clock["now"] += DAY
        return delivered
    return run


def _predict_one(cog, fake_hass, eid, current, dominant, **attrs):
    now = _time.time()
    fake_hass.states.set(eid, current, **attrs)
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, eid, now, dominant_state=dominant, current_state=current)
    out = cog.predict(fake_hass, now)
    assert len(out) == 1, out
    return out[0]


async def test_the_anticipation_alert_carries_its_entity(cog, fake_hass):
    alert = _predict_one(cog, fake_hass, "lock.front_door", "unlocked", "locked",
                         friendly_name="Front Door")
    assert alert["entity_id"] == "lock.front_door"
    assert alert["message"] == "Front Door is unlocked while nobody appears to be home."


async def test_door_unlocked_while_away_stays_loud_on_day_four(cog, fake_hass, days):
    alert = _predict_one(cog, fake_hass, "lock.front_door", "unlocked", "locked",
                         friendly_name="Front Door")
    delivered = await days(alert)
    assert all(d for d in delivered), delivered
    assert delivered[3] and "stop mentioning" not in " ".join(sum(delivered, []))


async def test_garage_open_while_away_stays_loud_on_day_four(cog, fake_hass, days):
    alert = _predict_one(cog, fake_hass, "cover.garage_door", "open", "closed",
                         device_class="garage", friendly_name="Garage Door")
    assert alert["entity_id"] == "cover.garage_door"
    delivered = await days(alert)
    assert all(d for d in delivered), delivered


async def test_an_alarm_alert_stays_loud_on_day_four(days):
    delivered = await days({"type": "anticipation", "urgency": "medium",
                            "message": "The alarm is disarmed while nobody appears to be home.",
                            "pattern_key": "anticipate:alarm_control_panel.home",
                            "entity_id": "alarm_control_panel.home", "offer": False})
    assert all(d for d in delivered), delivered


async def test_an_alert_that_already_went_quiet_comes_back(hab, days):
    hab._load()["anticipate:lock.front_door"] = {
        "entity_id": "", "last_day": 1, "streak": 3, "quiet": True}
    delivered = await days({"type": "anticipation", "urgency": "medium",
                            "message": "Front Door is unlocked while nobody appears to be home.",
                            "pattern_key": "anticipate:lock.front_door",
                            "entity_id": "lock.front_door", "offer": False}, n=1)
    assert delivered[0] and set(delivered[0]) == {
        "Front Door is unlocked while nobody appears to be home."}


async def test_other_alerts_still_go_quiet_after_three_days(days):
    # Unchanged: a light is not a lock, cover or alarm.
    delivered = await days({"type": "anticipation_routine", "urgency": "medium",
                            "message": "The porch light is on.",
                            "pattern_key": "anticipate:light.porch",
                            "entity_id": "light.porch", "offer": False})
    assert delivered[0] and delivered[1]
    assert "stop mentioning" in " ".join(delivered[2])
    assert delivered[3] == []


def test_exempt_covers_every_cover(load):
    hab = load("habituation")
    for eid in ("cover.garage_door", "cover.gate", "cover.living_blind"):
        assert hab.exempt(urgency="low", kind="anticipation", entity_id=eid)
