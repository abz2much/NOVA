"""The registry of Nova's own tools: which function runs each tool, what each
tool may do, and the checks that stop a broken registry from loading."""
import pytest


@pytest.fixture
def reg(load):
    return load("agent_runtime.registry")


@pytest.fixture
def models(load):
    return load("agent_runtime.models")


# Executors by the capability module that holds them: tool name -> function.
EXECUTORS = {
    "automation": {
        "manage_autonomy": "_exec_manage_autonomy",
        "review_suggestions": "_exec_review_suggestions",
        "approve_suggestion": "_exec_approve_suggestion",
        "dismiss_suggestion": "_exec_dismiss_suggestion",
    },
    "cameras": {
        "look_at_camera": "_exec_look_at_camera",
        "where_last_seen": "_exec_where_last_seen",
        "what_changed": "_exec_what_changed",
        "who_do_you_see": "_exec_who_do_you_see",
    },
    "communications": {
        "web_research": "_exec_web_research",
        "calendar_agenda": "_exec_calendar_agenda",
        "read_email": "_exec_read_email",
    },
    "control": {
        "control_device": "_exec_control_device",
        "run_scene_or_script": "_exec_run_scene_script",
        "run_voice_phrase": "_exec_run_voice_phrase",
        "bulk_control": "_exec_bulk_control",
        "execute_plan": "_exec_execute_plan",
    },
    "diagnostics": {
        "cognitive_status": "_exec_cognitive_status",
        "connectivity_status": "_exec_connectivity_status",
        "root_cause": "_exec_root_cause",
        "system_diagnostics": "_exec_system_diagnostics",
    },
    "environment": {
        "energy_status": "_exec_energy_status",
        "solar_status": "_exec_solar_status",
        "energy_report": "_exec_energy_report",
        "energy_outlook": "_exec_energy_outlook",
        "hazard_report": "_exec_hazard_report",
        "weather_forecast": "_exec_weather_forecast",
        "wellbeing_context": "_exec_wellbeing_context",
    },
    "home": {
        "get_entity_state": "_exec_get_entity_state",
        "search_entities": "_exec_search_entities",
        "get_area_devices": "_exec_get_area_devices",
        "get_home_summary": "_exec_home_summary",
        "activity_history": "_exec_activity_history",
    },
    "memory": {
        "remember": "_exec_remember",
        "confirm_pending_fact": "_exec_confirm_pending_fact",
        "reject_pending_fact": "_exec_reject_pending_fact",
        "propose_relation": "_exec_propose_relation",
        "reject_pending_relation": "_exec_reject_pending_relation",
        "lookup_relations": "_exec_lookup_relations",
        "ignore_entity": "_exec_ignore",
        "unignore_entity": "_exec_unignore",
        "search_documents": "_exec_search_documents",
        "ingest_documents": "_exec_ingest_documents",
    },
    "planning": {
        "schedule_followup": "_exec_schedule_followup",
        "manage_followups": "_exec_manage_followups",
        "create_goal": "_exec_create_goal",
        "update_goal": "_exec_update_goal",
        "manage_goals": "_exec_manage_goals",
    },
    "safety_modes": {
        "dismiss_intrusion": "_exec_dismiss_intrusion",
        "acknowledge_alert": "_exec_acknowledge_alert",
        "set_mode": "_exec_set_mode",
    },
    "specialists": {
        "ask_executive_assistant": "_exec_ask_executive_assistant",
        "ask_marketing_agent": "_exec_ask_marketing_agent",
        "ask_security_privacy_agent": "_exec_ask_security_privacy_agent",
        "ask_homelab_infra_agent": "_exec_ask_homelab_infra_agent",
        "ask_house_manager_agent": "_exec_ask_house_manager_agent",
    },
}

# What each tool may do, listed per capability as
# tool: (changes the home, writes a store, reaches the network, untrusted result).
RULES = {
    "control": {
        "control_device": (True, False, False, False),
        "bulk_control": (True, False, False, False),
        "run_scene_or_script": (True, False, False, False),
        "run_voice_phrase": (True, False, False, False),
        "execute_plan": (True, False, False, False),
    },
    "home": {
        "get_entity_state": (False, False, False, False),
        "search_entities": (False, False, False, False),
        "get_area_devices": (False, False, False, False),
        "get_home_summary": (False, False, False, False),
        "activity_history": (False, False, False, False),
    },
    "memory": {
        "remember": (False, True, False, False),
        "confirm_pending_fact": (False, True, False, False),
        "reject_pending_fact": (False, True, False, False),
        "propose_relation": (False, True, False, False),
        "reject_pending_relation": (False, True, False, False),
        "lookup_relations": (False, False, False, True),
        "ignore_entity": (False, True, False, False),
        "unignore_entity": (False, True, False, False),
        "search_documents": (False, False, False, True),
        "ingest_documents": (False, True, False, False),
    },
    "automation": {
        "manage_autonomy": (True, True, False, False),
        "review_suggestions": (False, False, False, False),
        "approve_suggestion": (True, True, False, False),
        "dismiss_suggestion": (False, True, False, False),
    },
    "planning": {
        "schedule_followup": (False, True, False, False),
        "manage_followups": (False, True, False, False),
        "create_goal": (False, True, False, False),
        "update_goal": (False, True, False, False),
        "manage_goals": (False, True, False, False),
    },
    "diagnostics": {
        "cognitive_status": (False, False, False, False),
        "connectivity_status": (False, False, False, False),
        "system_diagnostics": (False, False, True, False),
        "root_cause": (False, False, False, False),
    },
    "safety_modes": {
        "set_mode": (True, True, False, False),
        "acknowledge_alert": (True, True, False, False),
        "dismiss_intrusion": (True, True, False, False),
    },
    "cameras": {
        "look_at_camera": (False, False, True, True),
        "where_last_seen": (False, False, False, True),
        "what_changed": (False, False, False, True),
        "who_do_you_see": (False, False, False, False),
    },
    "environment": {
        "wellbeing_context": (False, False, False, False),
        "energy_status": (False, False, False, False),
        "solar_status": (False, False, False, False),
        "energy_report": (False, False, False, False),
        "energy_outlook": (False, False, False, False),
        "hazard_report": (False, False, True, True),
        "weather_forecast": (False, False, False, False),
    },
    "communications": {
        "web_research": (False, False, True, True),
        "calendar_agenda": (False, False, False, True),
        "read_email": (False, False, True, True),
    },
    "specialists": {
        "ask_executive_assistant": (True, False, True, True),
        "ask_marketing_agent": (True, False, True, True),
        "ask_security_privacy_agent": (False, False, True, True),
        "ask_homelab_infra_agent": (False, False, True, True),
        "ask_house_manager_agent": (False, False, True, True),
    },
    "delegation": {
        "delegate_task": (False, False, False, True),
    },
}


def _flat(table):
    return {tool: value for group in table.values() for tool, value in group.items()}


def test_each_tool_runs_the_function_from_its_capability_module(reg):
    for module, tools in EXECUTORS.items():
        for tool, func in tools.items():
            fn = reg._TOOL_MAP[tool]
            assert fn.__name__ == func, tool
            assert fn.__module__.endswith(f"agent_runtime.capabilities.{module}"), tool


def test_the_dispatch_table_holds_exactly_these_tools(reg):
    assert set(reg._TOOL_MAP) == set(_flat(EXECUTORS))
    assert len(reg._TOOL_MAP) == 55
    assert "delegate_task" not in reg._TOOL_MAP


def test_the_dispatch_table_is_a_plain_dict_of_callables(reg):
    assert type(reg._TOOL_MAP) is dict
    assert all(callable(fn) for fn in reg._TOOL_MAP.values())


def test_every_tool_is_classified_as_listed(reg, models):
    assert set(reg.TOOL_REGISTRY) == set(_flat(RULES))
    for capability, tools in RULES.items():
        for name, (mutates, persists, network, untrusted) in tools.items():
            row = reg.TOOL_REGISTRY[name]
            assert isinstance(row, models.RegisteredTool)
            assert row.name == name
            assert row.capability == capability, name
            assert (row.mutates, row.persists, row.network) == (
                mutates, persists, network), name
            expected = (models.Trust.UNTRUSTED_EXTERNAL if untrusted
                        else models.Trust.HOUSEHOLD_DATA)
            assert row.trust is expected, name


def test_a_registered_tool_carries_its_dispatch_function(reg):
    for name, row in reg.TOOL_REGISTRY.items():
        assert row.executor is reg._TOOL_MAP.get(name), name
    assert reg.TOOL_REGISTRY["delegate_task"].executor is None


def test_trust_of_reads_the_registry_and_defaults_to_household_data(reg, models):
    assert reg.trust_of("web_research") is models.Trust.UNTRUSTED_EXTERNAL
    assert reg.trust_of("control_device") is models.Trust.HOUSEHOLD_DATA
    assert reg.trust_of("delegate_task") is models.Trust.UNTRUSTED_EXTERNAL
    assert reg.trust_of("light_turn_on") is models.Trust.HOUSEHOLD_DATA
    assert reg.trust_of("") is models.Trust.HOUSEHOLD_DATA


def test_the_agent_logger_keeps_the_name_it_always_had(reg):
    assert reg._LOGGER.name == "jc.agent"


# ── the load time checks ─────────────────────────────────────────────────────

def test_a_loaded_registry_passes_its_own_check(reg):
    reg._check_registry()


def test_a_duplicate_tool_name_fails_the_check(reg, monkeypatch):
    tools = list(reg.NOVA_TOOLS) + [reg.NOVA_TOOLS[0]]
    monkeypatch.setattr(reg, "NOVA_TOOLS", tools)
    with pytest.raises(RuntimeError, match="duplicate tool name in NOVA_TOOLS"):
        reg._check_registry()


def test_a_tool_missing_from_the_classification_fails_the_check(reg, monkeypatch):
    rows = dict(reg.TOOL_REGISTRY)
    rows.pop("set_mode")
    monkeypatch.setattr(reg, "TOOL_REGISTRY", rows)
    with pytest.raises(RuntimeError) as caught:
        reg._check_registry()
    assert str(caught.value) == (
        "agent registry: NOVA_TOOLS and the classification disagree: ['set_mode']")


def test_a_tool_with_no_executor_fails_the_check(reg, monkeypatch):
    table = dict(reg._TOOL_MAP)
    table.pop("set_mode")
    monkeypatch.setattr(reg, "_TOOL_MAP", table)
    with pytest.raises(RuntimeError,
                       match="every tool but delegate_task needs an executor"):
        reg._check_registry()


def test_an_unsafe_tool_that_sub_agents_may_use_fails_the_check(reg, monkeypatch):
    monkeypatch.setattr(reg, "_SUBAGENT_DENY", set(reg._SUBAGENT_DENY) - {"control_device"})
    with pytest.raises(RuntimeError) as caught:
        reg._check_registry()
    assert str(caught.value).startswith(
        "agent registry: mutating, persisting, specialist and delegation tools "
        "must be sub-agent denied: ")
    assert "'control_device'" in str(caught.value)
