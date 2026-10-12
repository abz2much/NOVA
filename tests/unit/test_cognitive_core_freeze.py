"""Regression tests for SafetyManager pipe-freeze detection.

Pins the temperature thresholds and de-duplication: critical at <=20°F
(alert-only — recommends a heat setpoint, does not itself adjust the
thermostat; fixed Sept 2026, it used to claim auto_act=True with nothing
behind it), a one-shot warning at <=35°F, silence above, and a 1-hour
cooldown. Source is either a weather entity's temperature attribute or an
outdoor temperature sensor.
"""
import pytest


@pytest.fixture
def safety(cognitive_core, fake_hass):
    return cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})


async def _freeze(safety):
    return await safety._check_freeze()


async def test_critical_below_20f_from_weather(safety, fake_hass):
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    action = await _freeze(safety)
    assert action is not None
    assert action["type"] == "freeze_critical"
    assert action["urgency"] == "critical"
    # Alert-only: Nova recommends a setpoint, it doesn't set one itself.
    assert action["auto_act"] is False


async def test_warning_below_35f_from_weather(safety, fake_hass):
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    action = await _freeze(safety)
    assert action is not None
    assert action["type"] == "freeze_warning"
    assert action["urgency"] == "high"
    assert action["auto_act"] is False


async def test_no_alert_when_warm(safety, fake_hass):
    fake_hass.states.set("weather.home", "sunny", temperature=55)
    assert await _freeze(safety) is None


async def test_outdoor_sensor_is_used_when_no_weather(safety, fake_hass):
    fake_hass.states.set(
        "sensor.outdoor_temp", "18",
        device_class="temperature", friendly_name="Outdoor Temperature")
    action = await _freeze(safety)
    assert action is not None and action["type"] == "freeze_critical"


async def test_indoor_sensor_is_ignored(safety, fake_hass):
    # A temperature sensor without an outdoor hint must not drive freeze logic.
    fake_hass.states.set(
        "sensor.living_room", "16",
        device_class="temperature", friendly_name="Living Room Temperature")
    assert await _freeze(safety) is None


async def test_warning_is_one_shot_then_cooldown(safety, fake_hass):
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    first = await _freeze(safety)
    second = await _freeze(safety)  # still cold, but already warned + in cooldown
    assert first is not None and first["type"] == "freeze_warning"
    assert second is None


# ── Unit awareness (metric installs) ─────────────────────────────────────────

async def test_metric_mild_day_does_not_false_fire(safety, fake_hass):
    # 18.3°C is a mild day (~65°F). On a metric install the value is Celsius, so
    # this must NOT trip the Fahrenheit freeze threshold — the reported bug.
    fake_hass.states.set(
        "weather.home", "sunny", temperature=18.3, temperature_unit="°C")
    assert await _freeze(safety) is None


async def test_metric_real_freeze_fires_and_reports_celsius(safety, fake_hass):
    # -10°C (14°F) is a genuine freeze — it fires, and the message is in °C.
    fake_hass.states.set(
        "weather.home", "snowy", temperature=-10, temperature_unit="°C")
    action = await _freeze(safety)
    assert action is not None and action["type"] == "freeze_critical"
    assert "°C" in action["message"] and "°F" not in action["message"]
    assert "-10" in action["message"]


async def test_metric_outdoor_sensor_unit_respected(safety, fake_hass):
    # 20°C (68°F) from a metric sensor → no alert (was a false critical before).
    fake_hass.states.set(
        "sensor.outdoor_temp", "20", device_class="temperature",
        friendly_name="Outdoor Temperature", unit_of_measurement="°C")
    assert await _freeze(safety) is None


# ── A freeze is one episode (8.39.1) ─────────────────────────────────────────

async def test_a_critical_freeze_is_spoken_once_then_reminders_go_to_the_phones(
        safety, fake_hass, monkeypatch, load):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(load("core_safety").time, "time", lambda: clock["now"])
    fake_hass.states.set("weather.home", "cloudy", temperature=15)

    first = await _freeze(safety)
    assert first["urgency"] == "critical" and first["phone_only"] is False

    for hours in (1, 2, 2.9):                      # still freezing: nothing more yet
        clock["now"] = 1_000_000.0 + hours * 3600
        assert await _freeze(safety) is None

    clock["now"] = 1_000_000.0 + 3 * 3600
    reminder = await _freeze(safety)
    assert reminder["type"] == "freeze_critical"
    assert reminder["urgency"] == "high" and reminder["phone_only"] is True


async def test_the_episode_ends_when_it_warms_up(safety, fake_hass, monkeypatch, load):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(load("core_safety").time, "time", lambda: clock["now"])
    fake_hass.states.set("weather.home", "cloudy", temperature=15)
    assert (await _freeze(safety))["urgency"] == "critical"

    clock["now"] += 2 * 3600
    fake_hass.states.set("weather.home", "sunny", temperature=45)   # recovered
    assert await _freeze(safety) is None

    clock["now"] += 2 * 3600
    fake_hass.states.set("weather.home", "cloudy", temperature=15)  # a new freeze
    again = await _freeze(safety)
    assert again["urgency"] == "critical" and again["phone_only"] is False


def test_the_alert_path_keeps_a_phone_only_reminder_off_the_speakers(load):
    alert_path = load("alert_path")
    sit = alert_path.Situation(sleeping=False, quiet=False)
    plan = alert_path.for_core_action(None, {}, sit, {
        "type": "freeze_critical", "urgency": "high", "phone_only": True})
    assert plan.push is True and plan.speak is False
