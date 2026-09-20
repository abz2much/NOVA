"""Fix: cognition.predict() no longer treats a learned habit as an actionable
expectation on its own.

Real false positives this fixes: "First Floor Windows is closed. Around this
time it's usually open..." and "Main Toilet Window is closed. Around this
time it's usually open..." -- predict() was symmetric, flagging ANY state
that was unusual for the hour regardless of direction. Opening a window is
an activity or preference, not something Nova should recommend repeating
just because it's historically common at that hour.

New rule: a safer/neutral current state (closed, locked, armed) is NEVER a
reason to alert, no matter how historically dominant the opposite is. A
less-secure current state (open, unlocked, disarmed) only alerts when backed
by a separate, deterministic, actionable reason -- positively confirmed
absence of every tracked household member, or a conflicting armed security
state. Missing presence tracking is never treated as proof of an empty
house. The occupancy model itself (sample_occupancy) is untouched -- a
silent deviation keeps being learned, it just doesn't interrupt anyone.
"""
import time as _time

import pytest


@pytest.fixture
def cog(load):
    return load("cognition")


@pytest.fixture(autouse=True)
def _reset_model(cog):
    """_MODEL/_PREDICT_COOLDOWNS are module-level globals on the cached
    module -- must reset between tests or one test's seeded entry leaks
    into the next."""
    cog._MODEL.clear()
    cog._PREDICT_COOLDOWNS.clear()
    yield
    cog._MODEL.clear()
    cog._PREDICT_COOLDOWNS.clear()


def _seed(cog, eid, now, dominant_state, current_state, dominant_n=19, current_n=1):
    """A confidently-learned hourly pattern: `dominant_state` is the norm for
    this hour (enough samples, comfortably above OCC_UNUSUAL_SHARE), and the
    entity has held `current_state` long enough to not be a transient."""
    hour = _time.localtime(now).tm_hour
    entry = cog._Entry(now)
    entry.occ[hour] = {dominant_state: dominant_n, current_state: current_n}
    entry.last_changed = now - (cog.OCC_MIN_STILLNESS + 100)
    cog._MODEL[eid] = entry
    return entry


# ── Safer/neutral current state must never alert, however dominant the ─────
# ── opposite historically is. ────────────────────────────────────────────────

async def test_window_closed_usually_open_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "off",
                          device_class="window", friendly_name="First Floor Windows")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="on", current_state="off")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_second_differently_named_window_same_pattern_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("binary_sensor.main_toilet_window", "off",
                          device_class="window", friendly_name="Main Toilet Window")
    _seed(cog, "binary_sensor.main_toilet_window", now, dominant_state="on", current_state="off")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_lock_locked_usually_unlocked_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("lock.front_door", "locked", friendly_name="Front Door")
    _seed(cog, "lock.front_door", now, dominant_state="unlocked", current_state="locked")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_cover_closed_usually_open_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.garage_door", "closed", friendly_name="Garage Door")
    _seed(cog, "cover.garage_door", now, dominant_state="open", current_state="closed")
    out = cog.predict(fake_hass, now)
    assert out == []


# ── Less-secure current state requires a separate, actionable reason ────────

async def test_window_open_usually_closed_somebody_home_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    fake_hass.states.set("person.abi", "home")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_window_open_everyone_confirmed_away_alert_with_reason(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("person.rachel", "not_home")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "nobody appears to be home" in out[0]["message"]
    assert "First Floor Windows is open" in out[0]["message"]
    assert "usually" not in out[0]["message"]


async def test_window_open_no_presence_entities_does_not_assume_empty_no_alert(cog, fake_hass):
    """No person/device_tracker entities registered at all -- must NOT be
    treated as proof nobody is home."""
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_lock_unlocked_everyone_confirmed_away_alerts_with_reason(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "lock.front_door", now, dominant_state="locked", current_state="unlocked")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "nobody appears to be home" in out[0]["message"]
    assert "Front Door is unlocked" in out[0]["message"]


# ── Conflicting armed alarm is an independently acceptable reason ───────────

async def test_window_open_alarm_armed_alerts_with_that_reason(
        cog, fake_hass, monkeypatch):
    import sys
    from types import SimpleNamespace

    er = sys.modules["homeassistant.helpers.entity_registry"]
    registry = SimpleNamespace(entities={
        "alarm_control_panel.home": SimpleNamespace(
            entity_id="alarm_control_panel.home", platform="alarmo"),
    })
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    fake_hass.states.set("alarm_control_panel.home", "armed_away")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "the alarm is armed" in out[0]["message"]


# ── One tracked resident still home blocks the "away" reason specifically ──

async def test_window_open_one_person_home_one_away_no_alert(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set("person.rachel", "not_home")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert out == []


# ── Message wording never suggests recreating the historical state ─────────

async def test_alert_message_never_uses_usually_as_sole_justification(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "lock.front_door", now, dominant_state="locked", current_state="unlocked")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "usually" not in out[0]["message"]
    assert "open" not in out[0]["message"].lower().replace("unlocked", "")  # no suggestion to open/unlock anything


# ── Corrective fix 1: reuse presence.everyone_confidently_away(), which ─────
# ── fails safe on unknown/unavailable presence, instead of duplicated logic ─

async def test_all_configured_people_explicitly_away_is_true(cog, fake_hass):
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("person.rachel", "away")
    assert cog._all_tracked_residents_away(fake_hass) is True


async def test_one_person_home_is_false(cog, fake_hass):
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set("person.rachel", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


async def test_one_person_unknown_is_false(cog, fake_hass):
    fake_hass.states.set("person.abi", "unknown")
    fake_hass.states.set("person.rachel", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


async def test_one_person_unavailable_is_false(cog, fake_hass):
    fake_hass.states.set("person.abi", "unavailable")
    fake_hass.states.set("person.rachel", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


async def test_no_person_entities_is_false(cog, fake_hass):
    assert cog._all_tracked_residents_away(fake_hass) is False


async def test_unselected_armed_panel_does_not_create_window_alert(
        cog, fake_hass, monkeypatch):
    """An armed HomeBase must not conflict with a disarmed Alarmo panel."""
    import sys
    from types import SimpleNamespace

    er = sys.modules["homeassistant.helpers.entity_registry"]
    registry = SimpleNamespace(entities={
        "alarm_control_panel.security": SimpleNamespace(
            entity_id="alarm_control_panel.security", platform="alarmo"),
        "alarm_control_panel.homebase": SimpleNamespace(
            entity_id="alarm_control_panel.homebase", platform="eufy_security"),
    })
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    now = _time.time()
    fake_hass.states.set("alarm_control_panel.security", "disarmed")
    fake_hass.states.set("alarm_control_panel.homebase", "armed_home")
    fake_hass.states.set("person.abi", "unknown")
    fake_hass.states.set(
        "binary_sensor.bedroom_window", "on",
        device_class="window", friendly_name="Bedroom Window")
    _seed(cog, "binary_sensor.bedroom_window", now,
          dominant_state="off", current_state="on")

    assert cog.predict(fake_hass, now) == []


async def test_unrelated_device_tracker_away_does_not_prove_household_away(cog, fake_hass):
    """Household residents are person.* entities only -- an arbitrary
    device_tracker (a guest's phone, a car, a package courier's device)
    marked "away" must never stand in for a configured resident."""
    fake_hass.states.set("device_tracker.delivery_van", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


async def test_window_open_one_person_unknown_no_alert_end_to_end(cog, fake_hass):
    """End-to-end through predict(): an uncertain presence state must not
    let a less-secure deviation alert."""
    now = _time.time()
    fake_hass.states.set("binary_sensor.first_floor_windows", "on",
                          device_class="window", friendly_name="First Floor Windows")
    fake_hass.states.set("person.abi", "unknown")
    _seed(cog, "binary_sensor.first_floor_windows", now, dominant_state="off", current_state="on")
    out = cog.predict(fake_hass, now)
    assert out == []


# ── Corrective fix 2: only security-relevant cover device_classes ──────────
# ── (door/garage/garage_door/gate/window) count for anticipation ───────────

async def test_open_blind_everyone_away_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.living_room_blind", "open", device_class="blind",
                          friendly_name="Living Room Blind")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.living_room_blind", now, dominant_state="closed", current_state="open")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_open_curtain_or_shade_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.bedroom_curtain", "open", device_class="curtain",
                          friendly_name="Bedroom Curtain")
    fake_hass.states.set("cover.kitchen_shade", "open", device_class="shade",
                          friendly_name="Kitchen Shade")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.bedroom_curtain", now, dominant_state="closed", current_state="open")
    _seed(cog, "cover.kitchen_shade", now, dominant_state="closed", current_state="open")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_open_cover_no_device_class_no_prediction(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.mystery_cover", "open", friendly_name="Mystery Cover")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.mystery_cover", now, dominant_state="closed", current_state="open")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_open_garage_everyone_away_can_alert_with_real_reason(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.garage_door", "open", device_class="garage",
                          friendly_name="Garage Door")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.garage_door", now, dominant_state="closed", current_state="open")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "nobody appears to be home" in out[0]["message"]


async def test_open_gate_everyone_away_can_alert_with_real_reason(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.front_gate", "open", device_class="gate",
                          friendly_name="Front Gate")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.front_gate", now, dominant_state="closed", current_state="open")
    out = cog.predict(fake_hass, now)
    assert len(out) == 1
    assert "nobody appears to be home" in out[0]["message"]


async def test_closed_blind_usually_open_stays_silent(cog, fake_hass):
    """Safer/neutral-state rule still applies regardless of device_class."""
    now = _time.time()
    fake_hass.states.set("cover.living_room_blind", "closed", device_class="blind",
                          friendly_name="Living Room Blind")
    _seed(cog, "cover.living_room_blind", now, dominant_state="open", current_state="closed")
    out = cog.predict(fake_hass, now)
    assert out == []


async def test_closed_garage_usually_open_stays_silent(cog, fake_hass):
    now = _time.time()
    fake_hass.states.set("cover.garage_door", "closed", device_class="garage",
                          friendly_name="Garage Door")
    fake_hass.states.set("person.abi", "not_home")
    _seed(cog, "cover.garage_door", now, dominant_state="open", current_state="closed")
    out = cog.predict(fake_hass, now)
    assert out == []
