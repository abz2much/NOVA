"""Camera events can be pattern TRIGGERS but must never become action
targets — enforced in code (Phase 4, v7.109.0), not only in prompt wording.
"""
import pytest


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


@pytest.fixture
def agent_domains():
    """agent.py can't be fully imported in the unit sandbox (heavy HA
    module-level imports); the allowlist itself is a plain module-level
    set literal, extracted via the same ast pattern test_websocket_admin_gate
    uses for source-level guards."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "agent_runtime" / "capabilities" / "control.py")
    tree = ast.parse(src.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_EXECUTE_PLAN_ALLOWED_DOMAINS":
                    return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError("_EXECUTE_PLAN_ALLOWED_DOMAINS not found in the control capability")


# ── pattern_analyzer.service_for: never an action mapping for camera_event ───

@pytest.mark.parametrize("state", [
    "person", "vehicle", "animal", "package_delivered", "activity", "on", "off",
])
def test_camera_event_never_gets_a_service_mapping(pa, state):
    assert pa.service_for(f"camera_event.front_door", state) is None


def test_camera_event_domain_excluded_even_with_onoff_style_state(pa):
    """Confirms this isn't accidentally caught by the generic on/off
    mapping meant for light/switch/fan/etc — camera_event is excluded by
    its own explicit domain check, checked before any state-based logic."""
    assert pa.service_for("camera_event.garage", "on") is None
    assert pa.service_for("camera_event.garage", "off") is None


def test_real_domains_still_get_their_service_mapping(pa):
    """Sanity check the boundary test isn't vacuous — service_for still
    works normally for real, actuatable domains."""
    assert pa.service_for("light.porch", "on") == {"service": "light.turn_on", "entity_id": "light.porch"}
    assert pa.service_for("lock.front_door", "locked") == {"service": "lock.lock", "entity_id": "lock.front_door"}


# ── execute_plan's own domain allowlist ───────────────────────────────────────

def test_camera_event_domain_not_in_execute_plan_allowlist(agent_domains):
    assert "camera_event" not in agent_domains


def test_execute_plan_allowlist_still_covers_real_home_control_domains(agent_domains):
    for domain in ("light", "switch", "lock", "cover", "climate", "scene"):
        assert domain in agent_domains


# ── camera events remain usable as TRIGGERS (the positive side of the boundary) ──

def test_camera_event_still_produces_a_valid_state_trigger(pa):
    """The other half of the boundary: camera_event.* must still work as a
    TRIGGER (just never as an action) — _trigger_for falls through to a
    normal state trigger for any domain it doesn't special-case, and
    camera_event was never special-cased to be excluded."""
    trigger = pa._trigger_for("camera_event.front_door", "person")
    assert trigger == {"platform": "state", "entity_id": "camera_event.front_door", "to": "person"}


def test_camera_event_package_substate_trigger(pa):
    trigger = pa._trigger_for("camera_event.porch", "package_delivered")
    assert trigger["entity_id"] == "camera_event.porch"
    assert trigger["to"] == "package_delivered"
