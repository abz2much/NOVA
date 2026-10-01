"""intrusion_requires_confinement: confinement as the master switch.

Off (the default) keeps the automatic away and asleep behaviour. On, intrusion
monitoring runs only while a formal lockdown is engaged or the selected alarm
is armed. With residents home and awake, the armed state alone is not enough:
it takes an open entry, or an alarm armed away or on vacation.

All state here is fake. Nothing touches a real alarm or lock.
"""
import pytest

ALARM = "alarm_control_panel.home"


@pytest.fixture
def make_safety(cognitive_core, fake_hass):
    def _make(**extra):
        cfg = {"honorific": "sir", "security_alarm_entity": ALARM}
        cfg.update(extra)
        return cognitive_core.SafetyManager(fake_hass, cfg)
    return _make


def _motion(hass):
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")


def _door(hass):
    hass.states.set("binary_sensor.front_door", "on", device_class="door")


def _home(hass):
    hass.states.set("person.username", "home")


def _away(hass):
    hass.states.set("person.username", "not_home")


async def _tick(safety, hass, sleeping=False):
    actions = await safety.tick(sleeping=sleeping, anyone_home=not sleeping)
    hass.close_pending()
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


# ── strict setting ──────────────────────────────────────────────────────────

def test_only_literal_true_enables_confinement(load):
    safety_config = load("safety_config")
    assert safety_config.intrusion_requires_confinement(
        {"intrusion_requires_confinement": True}) is True
    for value in ("true", "1", 1, "yes", None, False):
        assert safety_config.intrusion_requires_confinement(
            {"intrusion_requires_confinement": value}) is False
    assert safety_config.intrusion_requires_confinement(None) is False
    assert safety_config.intrusion_requires_confinement({}) is False


def test_panel_write_must_be_a_real_boolean(load):
    safety_config = load("safety_config")
    key = "intrusion_requires_confinement"
    assert safety_config.valid_panel_value(key, True) is True
    assert safety_config.valid_panel_value(key, False) is True
    for value in ("true", 1, 0, None, "on"):
        assert safety_config.valid_panel_value(key, value) is False


# ── setting off: nothing changes ────────────────────────────────────────────

async def test_off_keeps_automatic_away_behaviour(make_safety, fake_hass):
    safety = make_safety()
    _away(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    intr = await _tick(safety, fake_hass)
    assert len(intr) == 1
    assert "no one is home" in intr[0]["message"]


# ── setting on, not confined: silent ────────────────────────────────────────

async def test_on_but_not_confined_never_alerts_even_when_away(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _away(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    assert await _tick(safety, fake_hass) == []


async def test_on_but_not_confined_never_alerts_when_asleep(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    assert await _tick(safety, fake_hass, sleeping=True) == []


# ── setting on, confined, residents away ────────────────────────────────────

async def test_armed_away_alarm_with_residents_away_alerts(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _away(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    intr = await _tick(safety, fake_hass)
    assert len(intr) == 1
    assert "no one is home" in intr[0]["message"]


# ── setting on, confined, residents home and awake (option B) ───────────────

async def test_armed_home_alone_is_not_enough_when_residents_are_home(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_home")
    assert await _tick(safety, fake_hass) == []


async def test_armed_night_alone_is_not_enough_when_residents_are_home(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_night")
    assert await _tick(safety, fake_hass) == []


async def test_armed_home_with_open_entry_alerts_with_secured_wording(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_home")
    intr = await _tick(safety, fake_hass)
    assert len(intr) == 1
    assert "while the house is secured" in intr[0]["message"]
    assert "no one is home" not in intr[0]["message"]
    assert safety._investigation["trigger"] == "confined"


async def test_armed_away_is_trusted_alone_even_if_a_resident_reads_home(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    intr = await _tick(safety, fake_hass)
    assert len(intr) == 1


async def test_lockdown_with_open_entry_alerts_at_home(
        make_safety, fake_hass, cognitive_core, monkeypatch):
    safety = make_safety(intrusion_requires_confinement=True)
    monkeypatch.setattr(cognitive_core, "is_lockdown", lambda: True)
    _home(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    assert len(await _tick(safety, fake_hass)) == 1


async def test_lockdown_alone_is_not_enough_at_home(
        make_safety, fake_hass, cognitive_core, monkeypatch):
    safety = make_safety(intrusion_requires_confinement=True)
    monkeypatch.setattr(cognitive_core, "is_lockdown", lambda: True)
    _home(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    assert await _tick(safety, fake_hass) == []


async def test_corroboration_off_alerts_on_motion_when_confined(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True,
                         intrusion_require_corroboration=False)
    _home(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_home")
    assert len(await _tick(safety, fake_hass)) == 1


# ── ending confinement stops monitoring at once ─────────────────────────────

async def test_disarming_drops_the_active_investigation(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_home")
    assert len(await _tick(safety, fake_hass)) == 1
    assert safety._investigation is not None
    fake_hass.states.set(ALARM, "disarmed")
    assert await _tick(safety, fake_hass) == []
    assert safety._investigation is None


async def test_turning_the_setting_off_drops_a_confined_investigation(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _door(fake_hass)
    _motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_home")
    await _tick(safety, fake_hass)
    assert safety._investigation["trigger"] == "confined"
    safety.config["intrusion_requires_confinement"] = False
    assert await _tick(safety, fake_hass) == []
    assert safety._investigation is None


# ── runtime apply ───────────────────────────────────────────────────────────

async def test_runtime_apply_updates_live_managers(
        make_safety, fake_hass, cognitive_core):
    safety = make_safety()
    cognitive_core._CORE.config = {}
    cognitive_core._CORE.safety_mgr = safety
    cognitive_core._CORE.lockdown_mgr = None
    await cognitive_core.apply_runtime_config(
        "intrusion_requires_confinement", True)
    assert safety.config["intrusion_requires_confinement"] is True
    await cognitive_core.apply_runtime_config(
        "intrusion_requires_confinement", "true")
    assert safety.config["intrusion_requires_confinement"] is False
    cognitive_core._CORE.safety_mgr = None
