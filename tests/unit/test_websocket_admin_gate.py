"""Guards for the websocket admin-authorisation fix (11 Sept 2026).

Before this fix, every one of Nova's ~34 websocket commands was reachable by
any authenticated Home Assistant user, admin or not — including config
writes, lockdown, and dismissing intrusion events (flagged in an external
security review of commit eb964825). Home Assistant's own convention for
this is the `websocket_api.require_admin` decorator (see
homeassistant/components/config/core.py upstream for the reference pattern:
`@require_admin` above `@websocket_command` above `@async_response`).

These are source-level guards, same reasoning as test_conversation_dispatch.py
and test_conversation_memory_seed.py: exercising the real websocket module
needs a live HA websocket_api, which the test suite stubs out rather than
imports (see tests/conftest.py's `jc.websocket` stub), so we assert on the
source text instead.

Commands gated here mutate state or reveal security-relevant state: config
writes, lockdown, knowledge read is left open but writes are gated, intrusion
handling, camera identity, goals/suggestions, and the enable/disable toggles
on biometrics/semantic-search/energy-agency. Pure reads (get_panel_data,
get_activity_log, diagnostics, sparklines, etc.) are deliberately left open —
gating everything would lock ordinary panel use behind admin for no security
benefit.

Reaffirmed 13 Sept 2026: an external review flagged get_panel_data,
get_activity_log, search_memory, and get_person_routines specifically as
readable by any signed-in user, admin or not. Abi confirmed this stays as
designed — accepted risk, not an oversight. Don't re-gate these without
checking with him first; it silently breaks the panel for non-admin
household members, not just narrows what they can see.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"

# type string -> must be admin-gated
ADMIN_GATED_TYPES = [
    "reload_appliances",
    "set_lockdown",
    "add_knowledge",
    "forget_knowledge",
    "update_config",
    "run_analysis",
    "suggestion_action",
    "rename_camera",
    "biometrics",
    "energy",
    "mode",
    "intrusion",
    "voice_confirm_test",
    "semantic_search",
    "documents",
    "camera_location",
    "goal_action",
    "pending_fact_action",
    "edit_pending_fact",
    "list_decisions",
    "get_decision",
    "set_decision_outcome",
    "replay_decision",
    "get_setup_health",
    "list_automation_trials",
    "automation_trial_feedback",
    "get_provider_activity",
    "get_spoken_history",
    "repeat_spoken",
    "list_models",
    "get_credential_status",
    "set_credential",
    "delete_credential",
]


def test_sensitive_commands_require_admin():
    src = SRC.read_text()
    missing = []
    for t in ADMIN_GATED_TYPES:
        anchor = f'vol.Required("type"): "nova/{t}",'
        idx = src.find(anchor)
        assert idx != -1, f"couldn't find registration for nova/{t} — has it been renamed?"
        # the decorator stack sits on the lines immediately above the anchor
        preceding = src[max(0, idx - 200):idx]
        if "@websocket_api.require_admin" not in preceding:
            missing.append(t)
    assert not missing, f"these sensitive commands are missing @websocket_api.require_admin: {missing}"


def test_require_admin_used_at_least_once_per_gated_command():
    src = SRC.read_text()
    # 17 gated commands -> at least 17 occurrences of the decorator
    assert src.count("@websocket_api.require_admin") >= len(ADMIN_GATED_TYPES)


def test_require_admin_sits_above_websocket_command_not_below():
    # Matches the upstream HA pattern: require_admin, then websocket_command,
    # then async_response — require_admin must be OUTERMOST (topmost), or it
    # silently never runs, since Python decorators apply bottom-up.
    src = SRC.read_text()
    assert "@websocket_api.require_admin\n@websocket_api.websocket_command({" in src
    # For every require_admin occurrence, the very next line must be
    # websocket_command (not async_response, not the def).
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "@websocket_api.require_admin":
            nxt = lines[i + 1].strip()
            assert nxt.startswith("@websocket_api.websocket_command("), (
                f"require_admin at line {i+1} is not immediately followed by "
                f"websocket_command — decorator order is wrong, admin check "
                f"will never run"
            )
