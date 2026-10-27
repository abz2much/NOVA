"""SafetyManager: when does motion become an intrusion alert?

The rule under test: an alert needs residents who are confidently away
(a person tracked away, or the alarm armed away), never just "nobody shows as
present", which is what an untracked person at home looks like. While away,
motion also needs backing (an open way in or an armed alarm) unless the user
has turned that requirement off.

Ticks run with sleeping=False so the night sweep, which writes files, stays
out of the way. Results are read from the returned actions, and the intrusion
module's stand down state and event log are reset for every test so nothing
leaks in from elsewhere.
"""
import time

import pytest

HALL = "binary_sensor.hall_motion"
FRONT_DOOR = "binary_sensor.front_door"
ALARM = "alarm_control_panel.home"


@pytest.fixture
def intrusion_state(load, tmp_path, monkeypatch):
    mod = load("intrusion")
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "intrusion_log.json")
    for name, value in (("_log", []), ("_log_loaded", True), ("_called_off_until", 0.0),
                        ("_acknowledged_ts", 0.0), ("_false_alarms", [])):
        monkeypatch.setattr(mod, name, value)
    return mod


@pytest.fixture
def make_safety(cognitive_core, fake_hass, intrusion_state):
    def build(**extra):
        return cognitive_core.SafetyManager(fake_hass, {"honorific": "sir", **extra})
    return build


@pytest.fixture
def safety(make_safety):
    return make_safety()


def away(hass):
    hass.states.set("person.alex", "not_home")
    hass.states.set("device_tracker.alex_phone", "not_home")


def moving(hass, entity_id=HALL, device_class="motion", state="on"):
    hass.states.set(entity_id, state, device_class=device_class)


def door_open(hass):
    hass.states.set(FRONT_DOOR, "on", device_class="door")


async def run_tick(safety, hass, *, home):
    found = await safety.tick(sleeping=False, anyone_home=home)
    hass.close_pending()   # drop any speech coroutine; only actions matter
    return [a for a in found if str(a.get("type", "")).startswith("intrusion")]


# ── no alert ────────────────────────────────────────────────────────────────

async def test_motion_with_nobody_tracked_is_not_an_intrusion(safety, fake_hass):
    # Someone home and walking about, with no person, tracker or alarm at all.
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=True) == []
    assert safety._investigation is None


async def test_a_person_at_home_outranks_motion(safety, fake_hass):
    fake_hass.states.set("person.alex", "home")
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=True) == []


async def test_a_person_at_home_outranks_an_open_door_too(safety, fake_hass):
    fake_hass.states.set("person.alex", "home")
    door_open(fake_hass)
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=True) == []
    assert safety._investigation is None


async def test_away_with_bare_motion_stays_quiet(safety, fake_hass):
    # No armed alarm and nothing open: a pet, a robot vacuum or a blind.
    away(fake_hass)
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=False) == []
    assert safety._investigation is None


async def test_away_and_still_is_no_intrusion(safety, fake_hass):
    away(fake_hass)
    door_open(fake_hass)
    assert await run_tick(safety, fake_hass, home=False) == []


@pytest.mark.parametrize("device_class,state", [
    ("motion", "off"),          # sensor clear
    ("door", "on"),             # not a motion sensor at all
    ("moisture", "on"),
])
async def test_only_live_motion_starts_anything(safety, fake_hass, device_class, state):
    away(fake_hass)
    door_open(fake_hass)
    moving(fake_hass, "binary_sensor.hall_other", device_class=device_class, state=state)
    assert await run_tick(safety, fake_hass, home=False) == []


async def test_a_recent_call_off_holds_new_alerts(safety, fake_hass, intrusion_state,
                                                  monkeypatch):
    monkeypatch.setattr(intrusion_state, "_called_off_until", time.time() + 600)
    away(fake_hass)
    door_open(fake_hass)
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=False) == []


# ── one alert ───────────────────────────────────────────────────────────────

async def test_away_with_an_open_door_raises_one_investigating_alert(
        safety, fake_hass, intrusion_state):
    away(fake_hass)
    door_open(fake_hass)
    moving(fake_hass)
    alerts = await run_tick(safety, fake_hass, home=False)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["type"] == "intrusion_investigating"
    assert alert["urgency"] == "high"
    assert alert["entity_id"] == HALL
    assert alert["auto_act"] is True
    # The away alert is spoken as well as pushed.
    assert "phone_only" not in alert
    assert safety._investigation["trigger"] == "away"
    logged = intrusion_state.get_log()
    assert [ev["kind"] for ev in logged] == ["investigating"]


async def test_with_backing_switched_off_bare_motion_alerts(make_safety, fake_hass):
    # The opt out for people who want every movement while away reported.
    safety = make_safety(intrusion_require_corroboration=False)
    fake_hass.states.set("person.alex", "not_home")
    moving(fake_hass)
    alerts = await run_tick(safety, fake_hass, home=False)
    assert [a["type"] for a in alerts] == ["intrusion_investigating"]


async def test_armed_away_with_no_trackers_is_left_to_the_alarm(safety, fake_hass):
    # Armed away the alarm watches the house (8.81.0), trackers or not.
    safety.config["security_alarm_entity"] = ALARM
    fake_hass.states.set(ALARM, "armed_away")
    moving(fake_hass)
    assert await run_tick(safety, fake_hass, home=False) == []


# ── repeats ─────────────────────────────────────────────────────────────────

async def test_a_second_tick_straight_after_stays_quiet(safety, fake_hass):
    away(fake_hass)
    door_open(fake_hass)
    moving(fake_hass)
    first = await run_tick(safety, fake_hass, home=False)
    second = await run_tick(safety, fake_hass, home=False)
    assert len(first) == 1
    assert second == []


@pytest.mark.parametrize("seconds_ago,alerts", [(60, 0), (299, 0), (301, 1)])
async def test_a_new_alert_waits_for_the_repeat_gap(safety, fake_hass, load,
                                                    seconds_ago, alerts):
    assert load("core_safety")._REPEAT_ALERT_GAP == 300.0
    away(fake_hass)
    door_open(fake_hass)
    moving(fake_hass)
    # The last investigation has closed; only the gap is left to hold.
    safety._last_intrusion_alert = time.time() - seconds_ago
    assert len(await run_tick(safety, fake_hass, home=False)) == alerts


# ── the picture attached to a push ──────────────────────────────────────────

def _base_urls(hass, external=None, internal=None):
    hass.config.external_url, hass.config.internal_url = external, internal


@pytest.mark.parametrize("url", ["https://x/snap.jpg", "http://cam.example/a.png"])
def test_a_full_address_goes_out_unchanged_for_both_phones(cognitive_core, fake_hass, url):
    _base_urls(fake_hass, external="https://home.example.com")
    assert cognitive_core._notification_image_data(fake_hass, url) == {
        "image": url, "attachment": {"url": url}}


@pytest.mark.parametrize("external,internal,expected", [
    ("https://home.example.com", None, "https://home.example.com/local/nova/x.jpg"),
    ("https://home.example.com/", None, "https://home.example.com/local/nova/x.jpg"),
    (None, "http://homeassistant.local:8123", "http://homeassistant.local:8123/local/nova/x.jpg"),
    (None, None, "/local/nova/x.jpg"),
])
def test_a_local_path_gets_the_instance_address(cognitive_core, fake_hass,
                                                external, internal, expected):
    _base_urls(fake_hass, external=external, internal=internal)
    data = cognitive_core._notification_image_data(fake_hass, "/local/nova/x.jpg")
    assert data == {"image": expected, "attachment": {"url": expected}}


def test_a_local_path_without_any_config_is_left_as_it_is(cognitive_core, fake_hass):
    # The fake has no url fields at all; that must not raise.
    data = cognitive_core._notification_image_data(fake_hass, "/local/nova/intrusion/x.jpg")
    assert data == {"image": "/local/nova/intrusion/x.jpg",
                    "attachment": {"url": "/local/nova/intrusion/x.jpg"}}


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_snapshot_means_no_image_block(cognitive_core, fake_hass, nothing):
    assert cognitive_core._notification_image_data(fake_hass, nothing) == {}
