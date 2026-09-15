"""Person arrivals/departures must reach the classifier, and must not be
silently dropped by the classifier rate limit either.

Real bug: `person` sits in IGNORED_DOMAINS for the observer's blanket
pre-filter (too noisy in general), but classifier.py has dedicated
arrived/left handling for exactly the home/away boundary crossing. Without
a carve-out, a routine arrival could only reach that logic via
cognition.py's anomaly escalation (i.e. only when the *timing* was
unusual) -- an ordinary arrival on an ordinary day was silently dropped
before either the greeting or the activity log ever saw it. Confirmed live:
person.abi went not_home -> home with zero corresponding Nova reaction.
These load just the pure helpers to avoid the observer's heavy imports.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

OBS = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "observer.py"
WANTED = {
    "IGNORED_DOMAINS", "WATCHED_DOMAINS", "INTERESTING_SENSOR_CLASSES",
    "ENTITY_ID_NOISE_SUBSTRINGS", "MIN_PREVIOUS_STATE_HOLD_S",
    "_entity_id_looks_noisy", "_is_person_home_transition", "_should_pre_filter",
}


def _load():
    src = OBS.read_text()
    ns = {"_STATE": SimpleNamespace(hass=None), "Event": object}
    for node in ast.parse(src).body:
        name = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in WANTED:
                    name = t.id
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED:
            name = node.name
        if name:
            exec(compile(ast.get_source_segment(src, node), "<obs>", "exec"), ns)
    return ns


def _event(entity_id, old, new):
    mk = lambda s: SimpleNamespace(state=s, attributes={}) if s is not None else None
    return SimpleNamespace(data={"entity_id": entity_id, "old_state": mk(old), "new_state": mk(new)})


def test_arrived_and_left_are_transitions():
    ns = _load()
    f = ns["_is_person_home_transition"]
    assert f(_event("person.abi", "not_home", "home")) is True
    assert f(_event("person.abi", "home", "not_home")) is True
    assert f(_event("person.abi", "Jianna School", "home")) is True


def test_non_boundary_person_changes_are_not_transitions():
    ns = _load()
    f = ns["_is_person_home_transition"]
    # A zone-to-zone move never touching "home" isn't an arrival/departure.
    assert f(_event("person.abi", "work", "Jianna School")) is False
    # Missing old/new state (startup) can't be judged a transition.
    assert f(_event("person.abi", None, "home")) is False


def test_non_person_domain_is_never_a_transition():
    ns = _load()
    f = ns["_is_person_home_transition"]
    assert f(_event("device_tracker.abi_phone", "not_home", "home")) is False
    assert f(_event("binary_sensor.front_door", "off", "on")) is False


def test_person_home_transition_passes_the_prefilter():
    ns = _load()
    should_drop = ns["_should_pre_filter"]
    # This used to return True (dropped) for every person.* event, arrival
    # included -- person is in IGNORED_DOMAINS with no carve-out.
    assert should_drop(_event("person.abi", "not_home", "home")) is False
    assert should_drop(_event("person.abi", "home", "not_home")) is False


def test_person_non_transition_still_prefiltered():
    ns = _load()
    should_drop = ns["_should_pre_filter"]
    # A person entity's state changing without crossing the home boundary
    # (e.g. one away-zone to another) stays ignored -- this carve-out is
    # deliberately narrow, not a blanket re-admit of the whole domain.
    assert should_drop(_event("person.abi", "work", "Jianna School")) is True


def test_device_tracker_still_fully_ignored():
    ns = _load()
    should_drop = ns["_should_pre_filter"]
    # classifier.py has no device_tracker-specific handling, so there's
    # nothing to re-admit it for -- this carve-out is person-only.
    assert should_drop(_event("device_tracker.abi_phone", "not_home", "home")) is True
