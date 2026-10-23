"""The cognitive core's one shared state object: the fields a fresh one has
and the values they start with."""
import pytest


@pytest.fixture
def cs(load):
    return load("core_state")


EXPECTED = {
    "hass": None, "config": {}, "running": False, "task": None, "unsub": None,
    "alarm_unsub": None, "ignore_mgr": None, "safety_mgr": None,
    "lockdown_mgr": None, "proactive_mgr": None, "autonomy_mgr": None,
    "state_logger": None, "automation_contexts": None, "entry": None,
    "tick_count": 0, "actions_taken": 0, "offers_made": 0,
    "autonomous_actions": 0, "last_tick": 0.0, "startup_time": 0.0,
    "pending_offer": None,
}


def test_a_fresh_state_has_exactly_these_fields_and_values(cs):
    assert vars(cs._CoreState()) == EXPECTED


def test_the_counters_start_as_whole_numbers_and_the_times_as_floats(cs):
    state = cs._CoreState()
    for name in ("tick_count", "actions_taken", "offers_made", "autonomous_actions"):
        assert type(getattr(state, name)) is int
    for name in ("last_tick", "startup_time"):
        assert type(getattr(state, name)) is float
    assert state.running is False


def test_each_state_gets_its_own_config_dict(cs):
    first, second = cs._CoreState(), cs._CoreState()
    first.config["k"] = 1
    assert second.config == {}


def test_the_shared_object_is_one_idle_core_state(cs):
    assert isinstance(cs._CORE, cs._CoreState)
    assert cs._CORE.running is False


def test_the_core_logger_name(cs):
    assert cs._LOGGER.name == "jc.cognitive_core"


def test_the_cognitive_core_exports_the_same_objects(load, cs):
    core = load("cognitive_core")
    assert core._CoreState is cs._CoreState
    assert core._CORE is cs._CORE
