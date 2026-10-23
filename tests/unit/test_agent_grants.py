"""What each kind of run may call: the main conversation, headless jobs and
sub-agents.

These pin the tool sets themselves, the named profile, how a capability or
profile name is resolved, and how a run's grant is chosen. Grants are
enforced server side; nothing a model says can widen one.
"""
import pytest


@pytest.fixture
def g(load):
    return load("agent_runtime.grants")


@pytest.fixture
def tools(load):
    return load("agent_runtime.tool_specs").NOVA_TOOLS


def _names(spec):
    return {t["function"]["name"] for t in spec}


# ── fixed sets ───────────────────────────────────────────────────────────────

def test_these_tools_change_the_home_and_wait_for_a_unique_entity(g):
    assert g._MUTATING_TOOL_NAMES == {
        "bulk_control", "control_device", "execute_plan",
        "run_scene_or_script", "run_voice_phrase"}


def test_the_delegation_limits(g):
    assert g.MAX_DELEGATION_DEPTH == 1
    assert g._DELEGATION_MAX_TURNS == 6


def test_capability_groups_are_read_only_tool_sets(g):
    assert g.CAPABILITY_GROUPS == {
        "scheduling": {"weather_forecast", "calendar_agenda", "get_home_summary",
                       "read_email"},
        "inbox": {"calendar_agenda", "read_email"},
        "home_state": {"activity_history", "get_area_devices", "get_entity_state",
                       "get_home_summary", "search_entities"},
        "research": {"search_entities", "search_documents", "web_research"},
        "environment": {"hazard_report", "weather_forecast"},
    }
    assert "diagnostics" not in g.CAPABILITY_GROUPS


def test_diagnostics_is_only_an_alias_for_the_homer_profile(g):
    assert g._LEGACY_CAPABILITY_PROFILE_ALIASES == {"diagnostics": "homer"}


EXPECTED_DENIED = {
    # actuators
    "control_device", "bulk_control", "run_scene_or_script", "execute_plan",
    "run_voice_phrase",
    # safety and alerts
    "set_mode", "dismiss_intrusion", "acknowledge_alert",
    # what Nova ignores
    "ignore_entity", "unignore_entity",
    # goals and follow-ups
    "create_goal", "update_goal", "manage_goals", "schedule_followup",
    "manage_followups",
    # suggestions and autonomy
    "approve_suggestion", "dismiss_suggestion", "review_suggestions",
    "manage_autonomy",
    # stores
    "remember", "ingest_documents", "confirm_pending_fact", "reject_pending_fact",
    "propose_relation", "reject_pending_relation",
    # cameras, which can announce
    "look_at_camera", "where_last_seen", "what_changed",
    # outside specialists
    "ask_executive_assistant", "ask_marketing_agent", "ask_security_privacy_agent",
    "ask_homelab_infra_agent", "ask_house_manager_agent",
    # and no recursion
    "delegate_task",
}


def test_the_sub_agent_deny_list_is_exactly_this(g):
    assert g._SUBAGENT_DENY == EXPECTED_DENIED
    assert len(g._SUBAGENT_DENY) == 34


def test_the_mutating_tools_are_all_denied_to_sub_agents(g):
    assert g._MUTATING_TOOL_NAMES <= g._SUBAGENT_DENY


def test_the_slim_retry_keeps_only_the_essentials(g):
    assert g._SLIM_TOOLS == {
        "get_home_summary", "bulk_control", "get_area_devices", "run_scene_or_script",
        "search_entities", "get_entity_state", "control_device"}


EXPECTED_HEADLESS = {
    "get_entity_state", "search_entities", "get_area_devices", "get_home_summary",
    "activity_history", "cognitive_status", "connectivity_status",
    "system_diagnostics", "root_cause", "energy_status", "solar_status",
    "energy_report", "energy_outlook", "hazard_report", "weather_forecast",
    "wellbeing_context", "calendar_agenda", "who_do_you_see", "look_at_camera",
    "update_goal",
}


def test_headless_jobs_may_look_and_report_and_record_goal_progress(g):
    assert g.HEADLESS_TOOLS == EXPECTED_HEADLESS
    assert isinstance(g.HEADLESS_TOOLS, frozenset)


def test_the_fixed_grants(g):
    assert (g.MAIN_GRANT.name, g.MAIN_GRANT.tools, g.MAIN_GRANT.include_ha_tools) == (
        "main", None, True)
    assert (g.HEADLESS_GRANT.name, g.HEADLESS_GRANT.tools,
            g.HEADLESS_GRANT.include_ha_tools) == ("headless", g.HEADLESS_TOOLS, False)


# ── HOMER ────────────────────────────────────────────────────────────────────

HOMER_TEXT = " ".join([
    "You are HOMER, Nova's System Diagnostic Specialist — a focused, read-only",
    "sub-agent, not Nova itself.",
    "You have no tool that controls a device, changes a setting, writes data,",
    "sends a notification, dismisses an alert, or delegates work to anyone else;",
    "nothing you say makes any of those happen.",
    "Investigate the supplied fault using only the diagnostic, telemetry, and",
    "state tools you've been given.",
    "Separate what you OBSERVE (a fact an actual tool call returned) from what",
    "you INFER (your reasoning about what those facts mean) — do not guess, and",
    "do not blend the two without saying which is which.",
    "When the evidence supports one, identify the most likely cause and cite the",
    "evidence for it; when it doesn't, say plainly what remains uncertain rather",
    "than filling the gap.",
    "Close with one concrete recommended next step.",
    "Report back to the parent agent — you never address a device directly, and",
    "you never claim to have fixed, repaired, or changed anything; you only",
    "report findings.",
])


def test_the_homer_directive_text_is_unchanged(g):
    assert g._HOMER_DIRECTIVE == HOMER_TEXT


def test_the_homer_profile(g):
    assert set(g.AGENT_PROFILES) == {"homer"}
    homer = g.AGENT_PROFILES["homer"]
    assert set(homer) == {"label", "max_turns", "tools", "directive"}
    assert homer["label"] == "HOMER" and homer["max_turns"] == 4
    assert homer["directive"] is g._HOMER_DIRECTIVE
    assert homer["tools"] == frozenset({
        "system_diagnostics", "cognitive_status", "connectivity_status", "root_cause",
        "energy_status", "activity_history", "get_entity_state", "search_entities"})
    assert isinstance(homer["tools"], frozenset)


@pytest.mark.parametrize("name", ["homer", "HOMER", " Homer ", "\thomer\n"])
def test_a_profile_is_found_whatever_the_case_or_spacing(g, name):
    allowed, turns, label, directive = g._resolve_profile(name)
    assert allowed == g.AGENT_PROFILES["homer"]["tools"]
    assert (turns, label, directive) == (4, "HOMER", g._HOMER_DIRECTIVE)
    assert isinstance(allowed, set)


@pytest.mark.parametrize("name", ["", None, "marvin", "homer2", 0, "diagnostics"])
def test_an_unknown_profile_is_none(g, name):
    assert g._resolve_profile(name) is None


def test_a_profile_never_gets_a_denied_tool_even_if_it_lists_one(g, monkeypatch):
    monkeypatch.setitem(g.AGENT_PROFILES, "rogue", {
        "label": "R", "max_turns": "3", "directive": "d",
        "tools": frozenset({"get_entity_state", "control_device", "delegate_task"})})
    allowed, turns, label, directive = g._resolve_profile("rogue")
    assert allowed == {"get_entity_state"}
    assert turns == 3 and isinstance(turns, int)
    assert (label, directive) == ("R", "d")


# ── capabilities ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("group", ["scheduling", "inbox", "home_state", "research",
                                   "environment"])
def test_a_group_resolves_to_its_tools(g, group):
    assert g._resolve_capability(group) == g.CAPABILITY_GROUPS[group]


def test_a_group_never_gets_a_denied_tool_even_if_it_lists_one(g, monkeypatch):
    monkeypatch.setitem(g.CAPABILITY_GROUPS, "rogue",
                        {"read_email", "control_device", "remember"})
    assert g._resolve_capability("rogue") == {"read_email"}
    assert g.CAPABILITY_GROUPS["rogue"] >= {"control_device"}


def test_diagnostics_resolves_to_exactly_the_homer_tools(g):
    assert g._resolve_capability("diagnostics") == g._resolve_profile("homer")[0]
    assert g._resolve_capability("diagnostics") == set(g.AGENT_PROFILES["homer"]["tools"])


def test_an_alias_to_an_unknown_profile_gives_nothing(g, monkeypatch):
    monkeypatch.setitem(g._LEGACY_CAPABILITY_PROFILE_ALIASES, "ghost", "nobody")
    assert g._resolve_capability("ghost") == set()


@pytest.mark.parametrize("name", ["", None, "nonsense", "Scheduling", 5])
def test_an_unknown_group_gives_nothing(g, name):
    assert g._resolve_capability(name) == set()


def test_resolving_a_group_hands_back_a_fresh_set(g):
    got = g._resolve_capability("inbox")
    got.add("anything")
    assert "anything" not in g.CAPABILITY_GROUPS["inbox"]
    assert g._resolve_capability("inbox") == {"calendar_agenda", "read_email"}


# ── the tool list ────────────────────────────────────────────────────────────

def test_no_scope_means_every_tool_in_order(g, tools):
    out = g._scoped_tool_list(None)
    assert out == list(tools) and out is not tools


def test_a_scope_keeps_only_the_named_tools_in_their_order(g, tools):
    wanted = {"search_entities", "get_entity_state", "nonexistent"}
    out = g._scoped_tool_list(wanted)
    assert _names(out) == {"search_entities", "get_entity_state"}
    order = [t["function"]["name"] for t in tools]
    assert [t["function"]["name"] for t in out] == [n for n in order if n in wanted]


def test_an_empty_scope_offers_nothing(g):
    assert g._scoped_tool_list(set()) == []


def test_the_slim_and_headless_sets_name_real_tools(g, tools):
    real = _names(tools)
    assert g._SLIM_TOOLS <= real
    assert g.HEADLESS_TOOLS <= real
    for group in g.CAPABILITY_GROUPS.values():
        assert group <= real
    assert g.AGENT_PROFILES["homer"]["tools"] <= real


# ── choosing a run's grant ───────────────────────────────────────────────────

def test_a_conversation_gets_the_main_grant(g):
    assert g.resolve_grant(None, depth=0, headless=False) is g.MAIN_GRANT


def test_a_conversation_with_a_scope_gets_exactly_that_scope(g):
    grant = g.resolve_grant({"control_device", "get_entity_state"}, depth=0,
                            headless=False)
    assert (grant.name, grant.include_ha_tools) == ("scoped", False)
    assert grant.tools == frozenset({"control_device", "get_entity_state"})
    assert isinstance(grant.tools, frozenset)


def test_an_empty_scope_in_a_conversation_allows_nothing(g):
    grant = g.resolve_grant(set(), depth=0, headless=False)
    assert grant.name == "scoped" and grant.tools == frozenset()


def test_a_headless_run_gets_the_headless_grant(g):
    assert g.resolve_grant(None, depth=0, headless=True) is g.HEADLESS_GRANT


def test_a_headless_scope_can_narrow_but_never_widen(g):
    grant = g.resolve_grant({"get_entity_state", "control_device", "update_goal"},
                            depth=0, headless=True)
    assert grant.name == "headless" and not grant.include_ha_tools
    assert grant.tools == frozenset({"get_entity_state", "update_goal"})
    assert g.resolve_grant(set(), depth=0, headless=True).tools == frozenset()


def test_a_sub_agent_gets_its_set_minus_the_deny_list(g):
    grant = g.resolve_grant({"get_entity_state", "control_device", "delegate_task",
                             "remember"}, depth=1, headless=False)
    assert (grant.name, grant.include_ha_tools) == ("delegated", False)
    assert grant.tools == frozenset({"get_entity_state"})


@pytest.mark.parametrize("scope", [None, set(), ()])
def test_a_sub_agent_with_no_scope_gets_nothing_not_everything(g, scope):
    grant = g.resolve_grant(scope, depth=2, headless=False)
    assert grant.name == "delegated" and grant.tools == frozenset()


def test_depth_wins_over_headless(g):
    grant = g.resolve_grant({"get_entity_state", "control_device"}, depth=1, headless=True)
    assert grant.name == "delegated" and grant.tools == frozenset({"get_entity_state"})


def test_a_run_grant_allows_only_what_it_holds(g):
    grant = g.resolve_grant({"get_entity_state"}, depth=1, headless=False)
    assert grant.allows("get_entity_state") and not grant.allows("control_device")
    assert g.MAIN_GRANT.allows("anything") and g.MAIN_GRANT.is_main
    assert not grant.is_main


def test_the_agent_logger_keeps_the_name_it_always_had(g):
    assert g._LOGGER.name == "jc.agent"
