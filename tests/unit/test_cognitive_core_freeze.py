"""SafetyManager's frozen pipe watch (_check_freeze).

Levels: critical at or below 20 °F, a single warning at or below 35 °F,
nothing above that, and at most one alert an hour. The critical alert only
advises a heating setpoint; nothing acts on it, so auto_act is False.
The outdoor reading comes from a weather entity, else from a temperature
sensor whose name says it is outdoors, in the unit Home Assistant reports.

Every test runs on its own clock so the hourly cooldown is exact.
"""
import types

import pytest

START = 1_000_000.0
HOUR = 3600.0


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now

    def advance(self, hours):
        self.now += hours * HOUR


@pytest.fixture
def clock(load, monkeypatch):
    ticking = Clock()
    monkeypatch.setattr(load("core_safety"), "time", types.SimpleNamespace(time=ticking.time))
    return ticking


@pytest.fixture
def safety(cognitive_core, fake_hass, clock):
    return cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})


def weather(hass, temp, unit=None, condition="cloudy"):
    extra = {"temperature_unit": unit} if unit else {}
    hass.states.set("weather.home", condition, temperature=temp, **extra)


def outdoor_sensor(hass, value, unit=None, entity_id="sensor.outdoor_temp",
                   name="Outdoor Temperature"):
    extra = {"unit_of_measurement": unit} if unit else {}
    hass.states.set(entity_id, value, device_class="temperature",
                    friendly_name=name, **extra)


# ── the levels ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("temp,kind,urgency", [
    (-5, "freeze_critical", "critical"),
    (15, "freeze_critical", "critical"),
    (20, "freeze_critical", "critical"),     # the critical line is inclusive
    (20.5, "freeze_warning", "high"),
    (30, "freeze_warning", "high"),
    (35, "freeze_warning", "high"),          # so is the warning line
])
async def test_cold_readings_alert_at_the_right_level(safety, fake_hass, temp, kind, urgency):
    weather(fake_hass, temp)
    alert = await safety._check_freeze()
    assert alert["type"] == kind
    assert alert["urgency"] == urgency
    # Advice only: no thermostat or valve is driven by this alert.
    assert alert["auto_act"] is False


@pytest.mark.parametrize("temp", [35.5, 40, 55, 90])
async def test_above_the_warning_line_is_quiet(safety, fake_hass, temp):
    weather(fake_hass, temp, condition="sunny")
    assert await safety._check_freeze() is None


async def test_no_outdoor_reading_is_quiet(safety, fake_hass):
    assert await safety._check_freeze() is None


async def test_the_critical_alert_names_the_reading_and_a_setpoint(safety, fake_hass):
    weather(fake_hass, 15)
    message = (await safety._check_freeze())["message"]
    assert "15" in message and "°F" in message
    assert "55" in message                     # the heating setpoint advised


# ── where the reading comes from ────────────────────────────────────────────

async def test_an_outdoor_sensor_is_used_without_a_weather_entity(safety, fake_hass):
    outdoor_sensor(fake_hass, "18")
    assert (await safety._check_freeze())["type"] == "freeze_critical"


async def test_an_indoor_sensor_never_drives_the_freeze_watch(safety, fake_hass):
    # A temperature sensor with no outdoor hint in its id or name.
    outdoor_sensor(fake_hass, "16", entity_id="sensor.living_room",
                   name="Living Room Temperature")
    assert await safety._check_freeze() is None


# ── metric installs ─────────────────────────────────────────────────────────

async def test_a_mild_celsius_day_is_not_a_freeze(safety, fake_hass):
    # 18.3 °C is about 65 °F; read as °F it would be a false critical.
    weather(fake_hass, 18.3, unit="°C", condition="sunny")
    assert await safety._check_freeze() is None


async def test_a_real_celsius_freeze_alerts_in_celsius(safety, fake_hass):
    weather(fake_hass, -10, unit="°C", condition="snowy")   # 14 °F
    alert = await safety._check_freeze()
    assert alert["type"] == "freeze_critical"
    assert "°C" in alert["message"] and "°F" not in alert["message"]
    assert "-10" in alert["message"]


@pytest.mark.parametrize("value,expected", [("20", None), ("0", "freeze_warning"),
                                            ("-8", "freeze_critical")])
async def test_a_celsius_sensor_keeps_its_unit(safety, fake_hass, value, expected):
    # 20 °C = 68 °F, 0 °C = 32 °F, -8 °C = 17.6 °F.
    outdoor_sensor(fake_hass, value, unit="°C")
    alert = await safety._check_freeze()
    assert (alert or {}).get("type") == expected


# ── repeats and episodes ────────────────────────────────────────────────────

async def test_a_second_check_within_the_hour_is_quiet(safety, fake_hass, clock):
    weather(fake_hass, 30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"
    clock.advance(0.5)
    assert await safety._check_freeze() is None


async def test_the_warning_is_given_once_per_cold_spell(safety, fake_hass, clock):
    weather(fake_hass, 30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"
    for _ in range(3):              # well past the cooldown, still just cold
        clock.advance(2)
        assert await safety._check_freeze() is None


async def test_a_warning_can_come_again_after_a_real_warm_up(safety, fake_hass, clock):
    weather(fake_hass, 30)
    await safety._check_freeze()
    clock.advance(2)
    weather(fake_hass, 38)          # warmer, but not enough to end the spell
    assert await safety._check_freeze() is None
    clock.advance(2)
    weather(fake_hass, 30)
    assert await safety._check_freeze() is None
    clock.advance(2)
    weather(fake_hass, 45)          # clearly warm: the spell is over
    assert await safety._check_freeze() is None
    clock.advance(2)
    weather(fake_hass, 30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"


async def test_a_critical_is_spoken_once_then_reminds_on_the_phones(safety, fake_hass, clock):
    weather(fake_hass, 15)
    first = await safety._check_freeze()
    assert first["urgency"] == "critical" and first["phone_only"] is False

    for hours in (1, 2, 2.9):       # still freezing: nothing more yet
        clock.now = START + hours * HOUR
        assert await safety._check_freeze() is None

    clock.now = START + 3 * HOUR
    reminder = await safety._check_freeze()
    assert reminder["type"] == "freeze_critical"
    assert reminder["urgency"] == "high" and reminder["phone_only"] is True


async def test_after_a_critical_a_milder_reading_sends_no_warning(safety, fake_hass, clock):
    weather(fake_hass, 15)
    await safety._check_freeze()
    clock.advance(2)
    weather(fake_hass, 30)
    assert await safety._check_freeze() is None


async def test_warming_up_ends_the_episode(safety, fake_hass, clock):
    weather(fake_hass, 15)
    assert (await safety._check_freeze())["urgency"] == "critical"

    clock.advance(2)
    weather(fake_hass, 45, condition="sunny")
    assert await safety._check_freeze() is None

    clock.advance(2)
    weather(fake_hass, 15)          # a new cold spell is spoken again
    again = await safety._check_freeze()
    assert again["urgency"] == "critical" and again["phone_only"] is False


def test_a_phone_only_reminder_stays_off_the_speakers(load):
    delivery = load("delivery")
    request = delivery.DeliveryRequest(source="freeze_critical", message="m",
                                       urgency="high", phone_only=True,
                                       push=delivery.PUSH_ALWAYS)
    decision = delivery.decide(request, delivery.Conditions())
    assert decision.push is True
    assert decision.speak is False
