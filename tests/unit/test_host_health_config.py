"""safety_config.py validation for Phase 10's host-health panel keys, and
that they're wired into websocket.py's admin-only write path (the generic
update_config admin gate — see test_websocket_admin_gate.py for the gate
itself, unchanged by this phase).

websocket.py can't be directly imported (heavy HA module-level imports —
same reason test_panel_writable_keys.py parses it via AST instead)."""
import ast
from pathlib import Path

import pytest

_WEBSOCKET = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"


def _panel_writable_keys() -> set[str]:
    tree = ast.parse(_WEBSOCKET.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Set):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "PANEL_WRITABLE_KEYS":
                    return {e.value for e in node.value.elts
                            if isinstance(e, ast.Constant)}
    raise AssertionError("PANEL_WRITABLE_KEYS set not found in websocket.py")


@pytest.fixture
def sc(load):
    return load("safety_config")


# ── booleans ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "host_health_enabled", "host_health_alerts_enabled", "host_health_recovery_announce",
])
def test_bool_keys_reject_non_bool(sc, key):
    assert sc.valid_panel_value(key, True) is True
    assert sc.valid_panel_value(key, False) is True
    assert sc.valid_panel_value(key, "true") is False
    assert sc.valid_panel_value(key, 1) is False


# ── persistence / cooldown bounds ───────────────────────────────────────

def test_persistence_minutes_bounds(sc):
    assert sc.valid_panel_value("host_health_persistence_minutes", 10) is True
    assert sc.valid_panel_value("host_health_persistence_minutes", 1) is False   # below (2, 120)
    assert sc.valid_panel_value("host_health_persistence_minutes", 200) is False
    assert sc.valid_panel_value("host_health_persistence_minutes", "10") is False


def test_cooldown_minutes_bounds(sc):
    assert sc.valid_panel_value("host_health_cooldown_minutes", 60) is True
    assert sc.valid_panel_value("host_health_cooldown_minutes", 1) is False      # below (5, 720)
    assert sc.valid_panel_value("host_health_cooldown_minutes", 1000) is False


# ── mappings ─────────────────────────────────────────────────────────────
# The websocket nova/update_config schema only accepts bool/str/int/float/
# None for `value` — a dict-shaped setting always arrives JSON-stringified
# (the same convention room_speakers/satellite_pairings/notify_services
# already use), never as a native object.

def test_mappings_accepts_valid_sensor_entities(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_mappings", json.dumps({"cpu_percent": "sensor.cpu_use"})) is True


def test_mappings_accepts_empty_string_to_clear(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_mappings", json.dumps({"cpu_percent": ""})) is True


def test_mappings_rejects_non_sensor_domain(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_mappings", json.dumps({"cpu_percent": "light.not_a_sensor"})) is False


def test_mappings_rejects_a_native_dict_not_json_encoded(sc):
    """The websocket schema itself would already refuse a native dict/list
    for `value` (vol.Any(bool, str, int, float, None)) — this pins the
    same expectation at the safety_config layer, defense in depth."""
    assert sc.valid_panel_value("host_health_mappings", {"cpu_percent": "sensor.cpu_use"}) is False
    assert sc.valid_panel_value("host_health_mappings", ["sensor.cpu_use"]) is False


def test_mappings_rejects_malformed_json(sc):
    assert sc.valid_panel_value("host_health_mappings", "{not valid json") is False


def test_mappings_rejects_non_string_value(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_mappings", json.dumps({"cpu_percent": 123})) is False


# ── thresholds ───────────────────────────────────────────────────────────

def test_thresholds_accepts_valid_metric_and_range(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_thresholds", json.dumps({"cpu_percent": 85.0})) is True


def test_thresholds_rejects_unknown_metric_key(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_thresholds", json.dumps({"not_a_metric": 50.0})) is False


def test_thresholds_rejects_out_of_range_value(sc):
    import json
    assert sc.valid_panel_value(
        "host_health_thresholds", json.dumps({"cpu_percent": 0.5})) is False   # below (1, 100)
    assert sc.valid_panel_value(
        "host_health_thresholds", json.dumps({"cpu_temperature": 5.0})) is False  # below (30, 110)


def test_thresholds_rejects_native_dict_not_json_encoded(sc):
    assert sc.valid_panel_value("host_health_thresholds", {"cpu_percent": 85.0}) is False


def test_thresholds_rejects_malformed_json(sc):
    assert sc.valid_panel_value("host_health_thresholds", "{not valid json") is False


def test_thresholds_rejects_nan_and_infinite(sc):
    # Python's json module round-trips NaN/Infinity by default (a JSON-spec
    # extension), so this must still be caught by the bounded-number check.
    assert sc.valid_panel_value(
        "host_health_thresholds", '{"cpu_percent": NaN}') is False
    assert sc.valid_panel_value(
        "host_health_thresholds", '{"cpu_percent": Infinity}') is False


# ── PANEL_WRITABLE_KEYS wiring ───────────────────────────────────────────

def test_all_host_health_keys_are_panel_writable():
    keys = _panel_writable_keys()
    for key in (
        "host_health_enabled", "host_health_alerts_enabled",
        "host_health_recovery_announce", "host_health_persistence_minutes",
        "host_health_cooldown_minutes", "host_health_mappings",
        "host_health_thresholds",
    ):
        assert key in keys
