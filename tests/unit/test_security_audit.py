"""security_audit: a read only check of the home's security (8.85.15).

It reads Home Assistant states only. Open ways in, unlocked locks and a
disarmed alarm matter when nobody is home; a sensor or camera that cannot be
read matters always; and with nobody to compare against (no person entities)
it makes no claim about being away.
"""
import json
import sys

import pytest


@pytest.fixture
def house(load, monkeypatch, fake_hass):
    load("agent")
    home = sys.modules["jc.agent_runtime.capabilities.home"]
    exempt = set()
    monkeypatch.setattr(home, "_exempt_locks", lambda: exempt)

    class House:
        mod, hass, exempt_locks = home, fake_hass, exempt

        def put(self, entity_id, state, **attrs):
            fake_hass.states.set(entity_id, state, **attrs)

        async def audit(self):
            return json.loads(await home._exec_security_audit(fake_hass, {}))
    return House()


def _whats(report, severity=None):
    return [f["what"] for f in report["findings"]
            if severity in (None, f["severity"])]


async def test_an_open_door_with_nobody_home_is_high(house):
    house.put("person.sam", "not_home")
    house.put("binary_sensor.front_door", "on", device_class="door", friendly_name="Front door")
    report = await house.audit()
    assert report["anyone_home"] is False
    assert _whats(report, "high") == ["Front door is open and nobody is home"]


async def test_an_open_garage_with_nobody_home_is_high(house):
    house.put("person.sam", "not_home")
    house.put("cover.garage", "open", device_class="garage", friendly_name="Garage door")
    assert _whats(await house.audit(), "high") == ["Garage door is open and nobody is home"]


async def test_open_doors_are_normal_with_someone_home(house):
    house.put("person.sam", "home")
    house.put("binary_sensor.front_door", "on", device_class="door")
    house.put("lock.front", "unlocked")
    report = await house.audit()
    assert report["findings"] == [] and report["all_clear"] is True


async def test_an_unlocked_lock_with_nobody_home_is_high_unless_exempt(house):
    house.put("person.sam", "not_home")
    house.put("lock.front", "unlocked", friendly_name="Front lock")
    house.put("lock.thermostat_child", "unlocked", friendly_name="Child lock")
    house.exempt_locks.add("lock.thermostat_child")
    assert _whats(await house.audit(), "high") == ["Front lock is unlocked and nobody is home"]


async def test_a_disarmed_alarm_with_nobody_home_is_medium(house):
    house.put("person.sam", "not_home")
    house.put("alarm_control_panel.house", "disarmed", friendly_name="House alarm")
    assert _whats(await house.audit(), "medium") == ["House alarm is disarmed and nobody is home"]


async def test_an_armed_alarm_is_fine(house):
    house.put("person.sam", "not_home")
    house.put("alarm_control_panel.house", "armed_away")
    assert (await house.audit())["findings"] == []


@pytest.mark.parametrize("entity_id, state, attrs, what, severity", [
    ("alarm_control_panel.house", "unavailable", {"friendly_name": "House alarm"},
     "House alarm cannot be read", "medium"),
    ("binary_sensor.back_door", "unavailable",
     {"device_class": "door", "friendly_name": "Back door"},
     "Back door sensor cannot be read, so Nova cannot tell if it is open", "medium"),
    ("lock.front", "unavailable", {"friendly_name": "Front lock"},
     "Front lock cannot be read, so Nova cannot tell if it is locked", "medium"),
    ("camera.drive", "unavailable", {"friendly_name": "Drive camera"},
     "Drive camera is offline", "low")])
async def test_what_cannot_be_read_is_reported_even_with_someone_home(
        house, entity_id, state, attrs, what, severity):
    house.put("person.sam", "home")
    house.put(entity_id, state, **attrs)
    assert _whats(await house.audit(), severity) == [what]


async def test_with_nobody_to_compare_it_makes_no_claim_about_being_away(house):
    house.put("binary_sensor.front_door", "on", device_class="door")
    house.put("lock.front", "unlocked")
    report = await house.audit()
    assert report["anyone_home"] is None
    assert report["findings"] == [] and report["all_clear"] is False
    assert "cannot tell" in report["note"]


async def test_the_most_serious_come_first(house):
    house.put("person.sam", "not_home")
    house.put("camera.drive", "unavailable", friendly_name="Drive camera")
    house.put("alarm_control_panel.house", "disarmed", friendly_name="House alarm")
    house.put("lock.front", "unlocked", friendly_name="Front lock")
    assert [f["severity"] for f in (await house.audit())["findings"]] == ["high", "medium", "low"]


async def test_a_failure_is_an_error_not_a_crash(house, monkeypatch):
    monkeypatch.setattr(house.mod, "_security_findings",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "error" in await house.audit()
