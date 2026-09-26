"""Source-level guard: none of HOMER's 8 granted tool handlers may write to
the Action Audit Log, call a mutating HA service, or write HA state.

This is proven at the source-text level (same convention as
test_camera_semantic_no_raw_content.py / test_websocket_admin_gate.py)
rather than by running each handler, because several need real HA
subsystems (recorder, EnergyManager) to execute meaningfully. Action Audit
Log rows are created ONLY inside the mutating tool handlers (confirmed by
inspection: action_log.start/new_request_id appear solely in
_exec_control_device and its actuating siblings) — never in the read-only
handlers below, and this test pins that so a future edit can't quietly add
a write to one of HOMER's tools without a red test.
"""
import re
from pathlib import Path

import pytest

# The tool executors live in the agent package's capability modules
# (agent.py re-exports them); read them all as one source.
_CAPABILITIES = (Path(__file__).resolve().parents[2] / "custom_components" / "nova"
                 / "agent_runtime" / "capabilities")

# name -> handler function name, for every tool HOMER is granted.
_HOMER_HANDLERS = {
    "system_diagnostics": "_exec_system_diagnostics",
    "cognitive_status": "_exec_cognitive_status",
    "connectivity_status": "_exec_connectivity_status",
    "energy_status": "_exec_energy_status",
    "activity_history": "_exec_activity_history",
    "get_entity_state": "_exec_get_entity_state",
    "search_entities": "_exec_search_entities",
    "root_cause": "_exec_root_cause",
}

_FORBIDDEN_MARKERS = (
    "action_log.start", "action_log.new_request_id", "action_log.start_many",
    "hass.services.async_call", "hass.states.async_set",
    "notify.", "nova_log(",
)


def _function_source(src: str, fn_name: str) -> str:
    """Slice from `async def fn_name(` up to the next top-level `async def `
    or `def ` at column 0 (functions in this module are never nested)."""
    start = src.index(f"async def {fn_name}(")
    rest = src[start + 1:]
    m = re.search(r"\n(?:async )?def ", rest)
    end = start + 1 + (m.start() if m else len(rest))
    return src[start:end]


@pytest.fixture(scope="module")
def agent_src():
    return "\n".join(p.read_text() for p in sorted(_CAPABILITIES.glob("*.py")))


@pytest.mark.parametrize("tool_name,fn_name", sorted(_HOMER_HANDLERS.items()))
def test_homer_tool_handler_has_no_mutating_side_effect(agent_src, tool_name, fn_name):
    body = _function_source(agent_src, fn_name)
    for marker in _FORBIDDEN_MARKERS:
        assert marker not in body, (
            f"{tool_name} ({fn_name}) unexpectedly contains {marker!r} — "
            f"HOMER must only be granted genuinely read-only tools"
        )


@pytest.fixture
def agent(load):
    return load("agent")


def test_homer_tool_set_matches_the_handlers_checked_above(agent):
    tools, _, _, _ = agent._resolve_profile("homer")
    assert tools == set(_HOMER_HANDLERS)
