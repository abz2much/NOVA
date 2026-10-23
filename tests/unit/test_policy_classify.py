"""policy.classify: the risk rating every state changing call gets.

Listed (domain, service) pairs keep their exact rating. Behind them, rules
by domain make sure a service nobody listed on a lock or alarm can never
come out low by omission, while the safe direction (locking, arming,
closing) and ordinary devices stay free of questions.
"""
from __future__ import annotations

import pytest

LOW, MEDIUM, HIGH, CRITICAL = "low", "medium", "high", "critical"


@pytest.fixture
def pol(load):
    return load("policy")


def _risk(pol, domain, service, entity_id=""):
    return pol.classify(domain, service, entity_id)[0]


# ── listed pairs ────────────────────────────────────────────────────────────

LISTED = [
    ("alarm_control_panel", "alarm_disarm", CRITICAL),
    ("lock", "unlock", HIGH),
    ("cover", "open_cover", MEDIUM),
    ("cover", "open", MEDIUM),
    ("lock", "open", MEDIUM),                       # a latch release
    ("alarm_control_panel", "alarm_arm_away", MEDIUM),
    ("alarm_control_panel", "alarm_arm_home", MEDIUM),
    ("alarm_control_panel", "alarm_arm_night", MEDIUM),
]


@pytest.mark.parametrize("domain, service, expected", LISTED)
def test_listed_pairs_keep_their_rating(pol, domain, service, expected):
    assert _risk(pol, domain, service) == expected


@pytest.mark.parametrize("domain, service, entity_id", [
    ("light", "turn_on", "light.kitchen"),
    ("media_player", "media_play", ""),
    ("climate", "set_temperature", ""),
    ("fan", "turn_off", "fan.bedroom"),
])
def test_everyday_devices_stay_low(pol, domain, service, entity_id):
    assert pol.classify(domain, service, entity_id) == (LOW, "")


# ── scenes, scripts and automations ────────────────────────────────────────
# Any of them can unlock a door, disarm the alarm or open a cover inside
# itself, and classify cannot look in. The rule is per domain, so a script's
# own script.<object_id> service is caught just like script.turn_on.

@pytest.mark.parametrize("domain, service", [
    ("scene", "turn_on"),
    ("script", "turn_on"),
    ("automation", "trigger"),
    ("script", "some_custom_script_object_id"),
    ("script", "unlock_the_back_door"),
])
def test_starting_a_scene_script_or_automation_is_medium(pol, domain, service):
    risk, why = pol.classify(domain, service)
    assert risk == MEDIUM
    assert why == f"{domain}.{service} activates a {domain} whose contents aren't visible to this gate"


@pytest.mark.parametrize("domain, service", [
    ("scene", "reload"), ("script", "reload"), ("script", "turn_off"),
    ("script", "stop"), ("automation", "turn_off"), ("automation", "reload"),
])
def test_stopping_or_reloading_one_stays_low(pol, domain, service):
    # None of these can set off what the scene, script or automation holds.
    assert _risk(pol, domain, service) == LOW


# ── services nobody listed, on a lock or an alarm ──────────────────────────

@pytest.mark.parametrize("service", ["unlatch", "unbolt", "Unlatch", "remote_disarm",
                                     "open_latch"])
def test_an_unlisted_guard_lowering_service_is_high(pol, service):
    risk, why = pol.classify("lock", service)
    assert risk == HIGH
    assert why == f"lock.{service} opens a security device"


@pytest.mark.parametrize("domain, service", [
    ("lock", "grant_access"),
    ("alarm_control_panel", "alarm_trigger"),
    ("alarm_control_panel", "set_code"),
])
def test_an_unlisted_actuating_service_needs_review(pol, domain, service):
    assert pol.classify(domain, service) == (
        MEDIUM, f"unrecognized {domain} action on a security device")


@pytest.mark.parametrize("domain, service", [
    ("lock", "lock"), ("lock", "close"), ("lock", "update"), ("lock", "reload"),
    ("lock", ""), ("alarm_control_panel", "update"),
])
def test_the_safe_direction_and_read_only_services_stay_low(pol, domain, service):
    # Locking must never gain a confirmation; refreshing changes nothing.
    assert _risk(pol, domain, service) == LOW


def test_nothing_actuating_on_a_security_domain_rates_low(pol):
    made_up = ["reset", "set_mode", "engage", "release", "toggle", "unlock_all"]
    for domain in ("lock", "alarm_control_panel"):
        for service in made_up:
            assert _risk(pol, domain, service) != LOW, (domain, service)


# ── plain switches with a security sounding name ───────────────────────────

@pytest.mark.parametrize("service, entity_id, expected", [
    ("turn_off", "switch.garage_door", HIGH),
    ("turn_off", "switch.front_lock", HIGH),
    ("turn_off", "switch.alarm_siren", HIGH),
    ("toggle", "switch.security_camera", HIGH),     # a flip can switch it off
    ("turn_off", "switch.desk_lamp", LOW),
    ("turn_on", "switch.garage_lights", LOW),       # only the off direction counts
    ("turn_on", "switch.front_lock", LOW),
])
def test_switches_are_rated_by_name_only_when_turned_off(pol, service, entity_id, expected):
    risk, why = pol.classify("switch", service, entity_id)
    assert risk == expected
    assert why == ("turning off a security switch" if expected == HIGH else "")


# ── Nova's own actions and the reasons given ───────────────────────────────

def test_standing_down_an_intrusion_response_is_critical(pol):
    assert pol.classify("nova", "dismiss_intrusion") == (
        CRITICAL, "nova.dismiss_intrusion stands down an active security response")


@pytest.mark.parametrize("service", ["ask_executive_assistant", "ask_marketing_agent"])
def test_specialists_that_act_outside_the_home_are_medium(pol, service):
    assert pol.classify("nova", service) == (
        MEDIUM, f"nova.{service} can send, post or change things outside the home")


def test_listed_pairs_explain_themselves(pol):
    assert pol.classify("alarm_control_panel", "alarm_disarm")[1] == \
        "alarm_control_panel.alarm_disarm disarms security"
    assert pol.classify("lock", "unlock")[1] == "lock.unlock unlocks a door"
    assert pol.classify("cover", "open_cover")[1] == "cover.open_cover opens a physical barrier"


@pytest.mark.parametrize("domain, service", [(None, None), ("", ""), (None, "turn_on")])
def test_missing_names_never_raise(pol, domain, service):
    assert pol.classify(domain, service) == (LOW, "")
