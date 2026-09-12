"""Capability-based risk classification in policy.classify().

Known high-signal (domain, service) pairs keep their exact risk; novel services
on inherently security domains are escalated so nothing unenumerated defaults to
LOW on a lock/alarm — while safe directions (lock, arm) and benign devices keep
their low friction.
"""
import pytest


@pytest.fixture
def pol(load):
    return load("policy")


def test_known_pairs_unchanged(pol):
    assert pol.classify("alarm_control_panel", "alarm_disarm")[0] == "critical"
    assert pol.classify("lock", "unlock")[0] == "high"
    assert pol.classify("cover", "open_cover")[0] == "medium"
    assert pol.classify("lock", "open")[0] == "medium"
    assert pol.classify("alarm_control_panel", "alarm_arm_away")[0] == "medium"


def test_convenience_stays_low(pol):
    assert pol.classify("light", "turn_on", "light.kitchen")[0] == "low"
    assert pol.classify("media_player", "media_play")[0] == "low"
    assert pol.classify("climate", "set_temperature")[0] == "low"


def test_indirection_domains_escalate_to_medium(pol):
    """Fixed Sept 2026: scene/script/automation used to stay LOW ("just a
    convenience action"), but any of them can itself unlock a door, disarm
    the alarm, or open a cover — classify() can't see inside one, so it's
    MEDIUM, not LOW. Domain-level, not a (domain, service) tuple: a script's
    own dynamic per-object service (script.<object_id>) needs the same
    treatment as script.turn_on, not just the one enumerated service name."""
    assert pol.classify("scene", "turn_on")[0] == "medium"
    assert pol.classify("script", "turn_on")[0] == "medium"
    assert pol.classify("automation", "trigger")[0] == "medium"
    assert pol.classify("script", "some_custom_script_object_id")[0] == "medium"


def test_indirection_safe_ops_stay_low(pol):
    # Reloading config or stopping/disabling something already running can't
    # newly trigger whatever a scene/script/automation contains.
    assert pol.classify("scene", "reload")[0] == "low"
    assert pol.classify("script", "reload")[0] == "low"
    assert pol.classify("script", "turn_off")[0] == "low"
    assert pol.classify("automation", "turn_off")[0] == "low"


def test_safe_security_direction_stays_low(pol):
    # locking a lock is the safe direction — must not gain confirmation friction
    assert pol.classify("lock", "lock")[0] == "low"


def test_novel_guard_dropping_service_escalates_high(pol):
    # services we never enumerated, on a security domain, that drop a guard
    assert pol.classify("lock", "unlatch")[0] == "high"
    assert pol.classify("lock", "unbolt")[0] == "high"


def test_novel_unknown_security_service_needs_review(pol):
    # unrecognized actuating service on a security domain → medium, not low
    assert pol.classify("lock", "grant_access")[0] == "medium"
    assert pol.classify("alarm_control_panel", "alarm_trigger")[0] == "medium"


def test_read_only_service_on_security_domain_is_low(pol):
    assert pol.classify("lock", "update")[0] == "low"


def test_security_named_switch_off_still_high(pol):
    assert pol.classify("switch", "turn_off", "switch.garage_door")[0] == "high"
    assert pol.classify("switch", "turn_off", "switch.front_lock")[0] == "high"


def test_security_named_switch_on_not_over_frictioned(pol):
    # turning ON a switch that merely contains 'garage' (e.g. garage lights)
    # must stay LOW — only turn_off was ever escalated
    assert pol.classify("switch", "turn_on", "switch.garage_lights")[0] == "low"
