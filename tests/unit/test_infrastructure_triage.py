"""InfrastructureTriage: the health audit over the sensors the user listed.

monitor.py imports only the standard library, so it is loaded here from its
file on its own and fed states through the shared FakeHass. The cases pin
the percent bands and their edges, offline versus unreadable, the ordering
when several things are wrong, and the exact verdict and sentence returned.

Since 8.28.0 only listed sensors are read. The audit once had one home's own
server, switch and freezer sensor ids built in; those must stay unchecked.
"""
import importlib.util
import pathlib
import sys

import pytest

from fakes import FakeHass  # tests/conftest.py puts this on sys.path
import subprocess
from cognitive_safety_kit import _isolated_core, cc  # noqa: F401

_MONITOR = (pathlib.Path(__file__).resolve().parents[2]
            / "custom_components" / "nova" / "diagnostics" / "monitor.py")


def _standalone(path):
    # dataclasses look the module up in sys.modules while the class body
    # runs, so it is listed only for the load and removed again afterwards
    name = "nova_triage_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


triage = _standalone(_MONITOR)

QUIET = {"alert_required": False, "message": "", "critical": False, "tags": []}

# name -> (entity id, healthy state, attributes)
SENSORS = {
    "disk": ("sensor.disk_use", "55", {"unit_of_measurement": "%",
                                        "friendly_name": "Disk use"}),
    "memory": ("sensor.memory_use", "60", {"unit_of_measurement": "%",
                                            "friendly_name": "Memory use"}),
    "switch": ("binary_sensor.network_switch", "on",
               {"device_class": "connectivity", "friendly_name": "Network switch"}),
    "link": ("binary_sensor.freezer_link", "on",
             {"device_class": "connectivity", "friendly_name": "Freezer link"}),
}
ALL_IDS = [eid for eid, _s, _a in SENSORS.values()]


def _home(**overrides):
    """A FakeHass with every sensor healthy, except the named ones set to the
    given state (None removes that sensor)."""
    hass = FakeHass()
    for key, (eid, state, attrs) in SENSORS.items():
        state = overrides.get(key, state)
        if state is not None:
            hass.states.set(eid, state, **attrs)
    return hass


def _run(hass, sensors=ALL_IDS, honorific="sir"):
    return triage.InfrastructureTriage(hass, honorific=honorific,
                                       sensors=sensors).evaluate()


def _one(eid, state, **attrs):
    hass = FakeHass()
    hass.states.set(eid, state, **attrs)
    return _run(hass, sensors=[eid])


# ── what gets checked ───────────────────────────────────────────────────────

@pytest.mark.parametrize("listed", [[], None, ["  ", ""]])
def test_nothing_listed_means_silence_even_with_faults(listed):
    assert _run(_home(disk="99", switch="off"), sensors=listed) == QUIET


def test_the_old_built_in_ids_are_not_checked():
    hass = FakeHass()
    hass.states.set("sensor.server_root_storage_usage", "99", unit_of_measurement="%")
    hass.states.set("binary_sensor.core_switch_status", "off")
    assert _run(hass, sensors=[]) == QUIET


def test_unlisted_sensors_are_ignored():
    hass = _home(disk="99")
    hass.states.set("sensor.other_disk", "99", unit_of_measurement="%")
    assert _run(hass, sensors=["sensor.memory_use"])["alert_required"] is False


def test_listed_ids_are_trimmed():
    verdict = _run(_home(disk="99"), sensors=["  sensor.disk_use "])
    assert verdict["critical"] is True


def test_a_listed_sensor_missing_from_the_home_is_skipped():
    assert _run(_home(disk=None, switch=None)) == QUIET


def test_a_sensor_listed_twice_is_reported_once():
    verdict = _run(_home(disk="97"), sensors=["sensor.disk_use"] * 2)
    assert verdict["message"].lower().count("disk use") == 1
    assert verdict["tags"] == ["Disk use"]


# ── all well ────────────────────────────────────────────────────────────────

def test_everything_healthy_is_quiet():
    assert _run(_home()) == QUIET


# ── percent sensors ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reading, alert, critical", [
    ("90", False, False),     # the warning line itself is still fine
    ("90.4", True, False),    # only just over the line
    ("92", True, False),      # above 90: elevated
    ("96", True, False),      # the critical line itself is only elevated
    ("97", True, True),       # above 96: critical
])
def test_percent_bands(reading, alert, critical):
    verdict = _run(_home(memory=reading))
    assert (verdict["alert_required"], verdict["critical"]) == (alert, critical)


def test_elevated_sentence_is_exact():
    assert _run(_home(disk="92"))["message"] == (
        "Sir, a minor infrastructure note. Disk use is elevated at 92 percent.")


def test_critical_sentence_is_exact():
    assert _run(_home(memory="97"))["message"] == (
        "Sir, infrastructure attention is required. "
        "Memory use is critically high at 97 percent.")


@pytest.mark.parametrize("reading, phrase", [
    ("unknown", "can't read"),
    ("unavailable", "can't read"),
    ("n/a", "unreadable value"),
    ("nan", "unreadable value"),
    ("inf", "unreadable value"),
])
def test_unreadable_percent_is_a_warning(reading, phrase):
    verdict = _run(_home(disk=reading))
    assert verdict["alert_required"] is True
    assert verdict["critical"] is False
    assert phrase in verdict["message"].lower()


def test_a_plain_sensor_only_warns_when_it_goes_dark():
    assert _one("sensor.uptime", "12", friendly_name="Uptime")["alert_required"] is False
    verdict = _one("sensor.uptime", "unavailable", friendly_name="Uptime")
    assert verdict["alert_required"] is True
    assert verdict["critical"] is False


# ── binary sensors ──────────────────────────────────────────────────────────

def test_connectivity_off_is_critical():
    verdict = _run(_home(switch="off"))
    assert verdict["critical"] is True
    assert verdict["message"] == ("Sir, infrastructure attention is required. "
                                  "Network switch is reporting offline.")


def test_unavailable_binary_is_a_warning_not_a_fault():
    verdict = _run(_home(switch="unavailable"))
    assert verdict["alert_required"] is True
    assert verdict["critical"] is False
    assert "dropped offline" in verdict["message"]


@pytest.mark.parametrize("state, alert", [("on", True), ("off", False)])
def test_problem_class_faults_when_on(state, alert):
    verdict = _one("binary_sensor.ups", state, device_class="problem", friendly_name="UPS")
    assert verdict["alert_required"] is alert
    if alert:
        assert verdict["critical"] is True
        assert "ups is reporting a problem" in verdict["message"].lower()


def test_unnamed_sensor_uses_its_entity_id():
    verdict = _one("binary_sensor.rack_fan", "off")
    assert "binary_sensor.rack_fan is reporting offline" in verdict["message"].lower()
    assert verdict["tags"] == ["binary_sensor.rack_fan"]


# ── several faults ──────────────────────────────────────────────────────────

def test_worst_first_and_joined_into_one_sentence():
    verdict = _run(_home(disk="92", link="off", memory="50"))
    assert verdict["critical"] is True
    # the critical link leads even though disk is checked first
    assert verdict["message"] == (
        "Sir, infrastructure attention is required. Freezer link is reporting "
        "offline, and Disk use is elevated at 92 percent.")
    assert verdict["tags"] == ["Freezer link", "Disk use"]


def test_three_faults_use_commas_then_and():
    verdict = _run(_home(disk="97", memory="93", switch="off"))
    body = verdict["message"].split("required. ", 1)[1]
    assert body.count(", ") == 2 and ", and " in body
    assert set(verdict["tags"]) == {"Disk use", "Memory use", "Network switch"}


def test_tags_name_every_finding():
    verdict = _run(_home(disk="97", link="off"))
    assert "Disk use" in verdict["tags"] and "Freezer link" in verdict["tags"]
    assert "disk use" in verdict["message"].lower()
    assert "freezer link" in verdict["message"].lower()


# ── the lead-in ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("honorific, lead", [
    ("", "A minor infrastructure note."),           # nobody in particular home
    ("   ", "A minor infrastructure note."),
    (None, "A minor infrastructure note."),
    ("ma'am", "Ma'am, a minor infrastructure note."),
    ("dear leader", "Dear Leader, a minor infrastructure note."),
])
def test_lead_in_follows_the_honorific(honorific, lead):
    assert _run(_home(disk="92"), honorific=honorific)["message"].startswith(lead + " ")


def test_lead_in_without_honorific_when_critical():
    message = _run(_home(disk="99"), honorific="")["message"]
    assert message.startswith("Infrastructure attention is required. Disk use")


# ── the audit's sensor list ───────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _audit_sensors_fn(load):
    """proactive_audio cannot be imported whole here, so its _audit_sensors is
    compiled on its own, with the real nova_config next to it."""
    import ast
    load("nova_config")
    src = (ROOT / "custom_components/nova/proactive_audio.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "_audit_sensors")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "AUDIT_SENSORS_KEY"
                                                    for t in n.targets))]
    ns = {"__name__": "jc._audit", "__package__": "jc"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "proactive_audio.py", "exec"), ns)
    return ns["_audit_sensors"]


@pytest.mark.parametrize("raw,want", [
    (None, []), ([], []), ("", []), ("[]", []), ("{not json", []), (5, []),
    (["sensor.disk_use", "", 3, "binary_sensor.switch"], ["sensor.disk_use", "binary_sensor.switch"]),
    ('["sensor.disk_use"]', ["sensor.disk_use"]),
])
def test_the_audit_reads_its_sensor_list(load, monkeypatch, raw, want):
    fn = _audit_sensors_fn(load)
    nc = load("nova_config")
    seen = []

    def _get(h, e, k, d=None):
        seen.append(k)
        return raw if raw is not None else d
    monkeypatch.setattr(nc, "runtime_get", _get)
    assert fn(None, None) == want
    assert seen == ["infrastructure_audit_sensors"]


def test_the_audit_is_given_the_list(load):
    src = (ROOT / "custom_components/nova/proactive_audio.py").read_text(encoding="utf-8")
    assert "sensors=_audit_sensors(hass, entry)" in src
