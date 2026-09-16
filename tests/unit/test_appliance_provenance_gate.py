"""Fix: appliance_monitor's cycle-completion announcement is now gated purely
by PROVENANCE, not by confidence or by the appliance_power_guessing toggle.

Real false positive this fixes: a Shelly power-monitoring sensor named
"Kitchen Counter Light Power" (an ordinary light, not an appliance) was
tracked generically, crossed the GENERIC state machine's thresholds when the
light was on for a while then switched off, and announced "Kitchen Counter
Light Power has finished its cycle. The Appliance is done." Root cause:
appliance_monitor tracks EVERY power sensor in the house generically by
default (_discover_sensors Method 4), and the old trust gate let ANY
untrusted guess speak once appliance_power_guessing was enabled -- there was
no way to enable real appliance-completion announcements without also
re-enabling every generic power-cycle false positive in the house.

Fix: only three provenances may ever announce -- native completion entities,
user-declared appliance mappings (declared_entity/explicit_config), and a
whole-home load matched against a declared appliance (whole_home_match).
Everything else (unidentified, keyword/area/sibling-name guesses, power
fingerprinting, unmatched whole-home deltas) keeps being tracked and logged
(appliance learning is unaffected) but can never reach speech or a push
notification, regardless of appliance_power_guessing.
"""
import time as _time

import pytest


@pytest.fixture
def am(load):
    return load("appliance_monitor")


@pytest.fixture
def announce_spy(am, load, monkeypatch, fake_hass):
    """Spies on output_gate.can_announce -- the gate _announce_done must
    reach ONLY for a trusted provenance. record_announcement is stubbed so
    the suppressed path (which also calls it) never touches a real DB."""
    og = load("output_gate")
    calls = []
    def fake_can_announce(**kw):
        calls.append(kw)
        return True, ""
    monkeypatch.setattr(og, "can_announce", fake_can_announce)
    monkeypatch.setattr(og, "record_announcement", lambda **kw: None)
    am._MON.hass = fake_hass
    am._MON.config = {}
    return calls


def _sensor(am, entity_id, friendly, appliance, discovery_method, peak_power=250.0):
    return am._SensorState(
        entity_id=entity_id, friendly_name=friendly, appliance=appliance,
        discovery_method=discovery_method, peak_power=peak_power,
    )


def _simulate_full_cycle(am, sensor, run_watts, idle_watts=2.0):
    """Drives the REAL state machine (_process_reading) through a complete
    idle -> running -> done cycle without a real sleep, by pre-dating
    run_start/settle_start past the appliance type's own thresholds --
    exactly what elapsed wall-clock time would have produced."""
    a = sensor.appliance
    sensor.run_start = _time.time() - (a.sustained_seconds + 5)
    label = am._process_reading(sensor, run_watts)
    assert label is None, "should only transition to running, not complete yet"
    assert sensor.phase == "running"

    sensor.settle_start = _time.time() - (a.settle_seconds + 5)
    return am._process_reading(sensor, idle_watts)


# ── Required behavioural tests ───────────────────────────────────────────────

async def test_kitchen_counter_light_power_completes_a_cycle_and_stays_silent(
    am, fake_hass, announce_spy,
):
    """The exact reported false positive: an unidentified power sensor named
    after a light, run through the real state machine end to end."""
    sensor = _sensor(am, "sensor.shelly1pmg4_a085e3b688d4_power",
                      "Kitchen Counter Light Power", am.ApplianceType.GENERIC,
                      "unidentified")
    am._MON.sensors[sensor.entity_id] = sensor

    label = _simulate_full_cycle(am, sensor, run_watts=80)
    assert label == "appliance"   # the state machine DOES think a cycle finished...

    await am._announce_done(sensor, label)
    assert announce_spy == []     # ...but it must never reach speech


async def test_differently_named_unidentified_power_sensor_stays_silent(
    am, fake_hass, announce_spy,
):
    """Not a special case for this one entity name -- any other unidentified
    light/plug/charger power sensor must behave identically."""
    sensor = _sensor(am, "sensor.hallway_outlet_power", "Hallway Outlet Power",
                      am.ApplianceType.GENERIC, "unidentified")
    am._MON.sensors[sensor.entity_id] = sensor

    label = _simulate_full_cycle(am, sensor, run_watts=60)
    assert label == "appliance"

    await am._announce_done(sensor, label)
    assert announce_spy == []


async def test_fingerprinted_undeclared_load_stays_silent(am, fake_hass, announce_spy):
    """A sensor that WAS successfully fingerprinted (peak power matched a
    known signature) but was never user-declared or native -- still just an
    automatic guess, must not speak."""
    sensor = _sensor(am, "sensor.mystery_power", "Mystery Power",
                      am.ApplianceType.WASHER, "fingerprint:350W")
    await am._announce_done(sensor, "washer")
    assert announce_spy == []


async def test_native_dishwasher_completion_still_announces(am, fake_hass, announce_spy):
    sensor = _sensor(am, "sensor.dishwasher_job_state", "Dishwasher",
                      am.ApplianceType.DISHWASHER, "native_status")
    await am._announce_done(sensor, "dishwasher")
    assert len(announce_spy) == 1
    assert "dishwasher" in announce_spy[0]["message"].lower()


async def test_user_declared_washer_power_sensor_still_announces(am, fake_hass, announce_spy):
    sensor = _sensor(am, "sensor.washer_power", "Washer",
                      am.ApplianceType.WASHER, "declared_entity")
    await am._announce_done(sensor, "washer")
    assert len(announce_spy) == 1
    assert "washer" in announce_spy[0]["message"].lower()


async def test_whole_home_load_matched_to_declared_appliance_still_announces(
    am, fake_hass, announce_spy,
):
    sensor = _sensor(am, "sensor.whole_home_meter", "Washer",
                      am.ApplianceType.WASHER, "whole_home_match:420W")
    await am._announce_done(sensor, "Washer")
    assert len(announce_spy) == 1


async def test_unmatched_whole_home_load_stays_silent(am, fake_hass, announce_spy):
    sensor = _sensor(am, "sensor.whole_home_meter", "an appliance",
                      am.ApplianceType.GENERIC, "whole_home_unmatched:150W")
    await am._announce_done(sensor, "an appliance (~150W)")
    assert announce_spy == []


# ── appliance_power_guessing no longer overrides the provenance gate ────────

async def test_power_guessing_enabled_does_not_unlock_an_unidentified_guess(
    am, fake_hass, announce_spy,
):
    am._MON.power_guessing = True   # previously the escape hatch
    sensor = _sensor(am, "sensor.kitchen_counter_light_power",
                      "Kitchen Counter Light Power", am.ApplianceType.GENERIC,
                      "unidentified")
    await am._announce_done(sensor, "appliance")
    assert announce_spy == []


async def test_power_guessing_disabled_still_allows_trusted_native(am, fake_hass, announce_spy):
    am._MON.power_guessing = False
    sensor = _sensor(am, "sensor.dishwasher_job_state", "Dishwasher",
                      am.ApplianceType.DISHWASHER, "native_status")
    await am._announce_done(sensor, "dishwasher")
    assert len(announce_spy) == 1


# ── ApplianceType.GENERIC must never say "The Appliance is done." ───────────

async def test_generic_type_never_produces_the_appliance_is_done_even_when_trusted(
    am, fake_hass, announce_spy,
):
    """A user CAN declare an appliance with type "appliance" (GENERIC) in the
    Settings panel -- that's a trusted, declared source, so it may still
    announce, but never with the templated "The Appliance is done" phrase."""
    sensor = _sensor(am, "sensor.workshop_tool_power", "Workshop Tool Power",
                      am.ApplianceType.GENERIC, "declared_entity")
    await am._announce_done(sensor, "appliance")
    assert len(announce_spy) == 1
    message = announce_spy[0]["message"]
    assert "The Appliance is done" not in message
    assert "Workshop Tool Power" in message


async def test_unidentified_generic_is_suppressed_before_message_wording_matters(
    am, fake_hass, announce_spy,
):
    sensor = _sensor(am, "sensor.kitchen_counter_light_power",
                      "Kitchen Counter Light Power", am.ApplianceType.GENERIC,
                      "unidentified")
    await am._announce_done(sensor, "appliance")
    assert announce_spy == []
