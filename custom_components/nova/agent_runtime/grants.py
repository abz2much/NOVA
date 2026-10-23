"""What each run of the agent may call: capability groups for sub-agents, the
named profiles, the deny list, and the slim and scoped tool lists."""
from __future__ import annotations

import logging
from typing import Optional

from .models import ToolGrant
from .tool_specs import NOVA_TOOLS

# Every part of the agent logs under …nova.agent, whichever module the line
# comes from, so a filter or level set for the agent still applies.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# Tools that call a real Home Assistant service on one entity found through
# search_entities. When a batch also holds a require_unique search, these wait
# for it, so an entity_id that was never resolved, or matched several things,
# cannot reach a service call (Phase 3; see the dispatch loop in run_agent).
_MUTATING_TOOL_NAMES = {
    "control_device", "bulk_control", "run_scene_or_script",
    "execute_plan", "run_voice_phrase",
}


# ── Sub-agents (delegate_task, v6.83.0) ──────────────────────────────────────
# For one hard part of a request Nova can start a narrow, single-purpose
# sub-agent. It is a nested run_agent call inside the same process, given a
# small objective, a short read-only list of tools and a few turns. A single
# GPU runs them one after another, so the gain is a tight context with less
# drift, not speed. A sub-agent cannot actuate, write to a store or start
# another sub-agent: those tools are denied and the depth is capped.

MAX_DELEGATION_DEPTH = 1       # the main agent (depth 0) may delegate, a sub-agent may not
_DELEGATION_MAX_TURNS = 6      # most tool-loop turns a sub-agent gets


# Capability group -> the read-only tools a sub-agent of that kind receives.
#
# There is no "diagnostics" group, on purpose. It used to be one, and adding
# the HOMER profile (below) would have left two rival answers to what a
# diagnostic sub-agent may do. HOMER is now the only policy: its tools, turn
# limit and prompt sit in AGENT_PROFILES["homer"], and "diagnostics" stays only
# as an alias for it (see _resolve_capability and _run_delegated). The old
# group also held solar_status and energy_report. They report totals and
# forecasts, not evidence of a fault, so HOMER leaves them out. The main agent
# still reaches them directly, like any Nova tool, and no group lists them.
CAPABILITY_GROUPS: dict = {
    "scheduling": {"calendar_agenda", "read_email", "weather_forecast",
                   "get_home_summary"},
    "inbox": {"read_email", "calendar_agenda"},
    "home_state": {"get_entity_state", "search_entities", "get_area_devices",
                   "get_home_summary", "activity_history"},
    "research": {"web_research", "search_documents", "search_entities"},
    "environment": {"weather_forecast", "hazard_report"},
}


# Old capability names that now stand for a named profile's own grant, not for
# an entry of CAPABILITY_GROUPS. Calls such as
# delegate_task(capability="diagnostics", ...) keep working with no second
# diagnostics allowlist, prompt or turn limit to maintain.
_LEGACY_CAPABILITY_PROFILE_ALIASES: dict = {"diagnostics": "homer"}


# Tools no sub-agent ever receives, even from a group that lists one (defence
# in depth). They fall into the families below. registry._check_registry()
# refuses to load if a mutating, persisting or specialist tool is missing.
_SUBAGENT_DENY: set = set().union(
    # actuators
    {"control_device", "bulk_control", "run_scene_or_script", "execute_plan",
     "run_voice_phrase"},
    # safety posture and alerts
    {"set_mode", "dismiss_intrusion", "acknowledge_alert"},
    # what Nova ignores
    {"ignore_entity", "unignore_entity"},
    # goals and follow-ups
    {"create_goal", "update_goal", "manage_goals", "schedule_followup",
     "manage_followups"},
    # suggestions and autonomy
    {"approve_suggestion", "dismiss_suggestion", "review_suggestions",
     "manage_autonomy"},
    # writers to persistent stores, pending facts included
    {"remember", "ingest_documents", "confirm_pending_fact",
     "reject_pending_fact", "propose_relation", "reject_pending_relation"},
    # camera vision, which can announce
    {"look_at_camera", "where_last_seen", "what_changed"},
    # the external specialist webhooks
    {"ask_executive_assistant", "ask_marketing_agent",
     "ask_security_privacy_agent", "ask_homelab_infra_agent",
     "ask_house_manager_agent"},
    # and delegation itself
    {"delegate_task"},
)


def _resolve_capability(capability: str) -> set:
    """The tool names a sub-agent of this capability may use, never including
    the deny list. A legacy alias (today only 'diagnostics') gives the tool set
    of its profile, the very grant _resolve_profile returns, not a separate
    copy. An unknown name gives an empty set."""
    name = str(capability or "")
    profile_name = _LEGACY_CAPABILITY_PROFILE_ALIASES.get(name)
    if profile_name:
        found = _resolve_profile(profile_name)
        return found[0] if found else set()
    return CAPABILITY_GROUPS.get(name, set()) - _SUBAGENT_DENY


# ── Named sub-agent profiles (HOMER, Phase 7) ────────────────────────────────
# A profile is a fixed triple of tools, turn limit and system directive under
# one name. It is for a sub-agent that needs its own persona and tighter limits
# than a plain tool subset gives. Only HOMER exists. It is a read-only
# diagnostic specialist, so it fits the rule that sub-agents never actuate and
# needs no opt-in. A profile that actuates is deliberately outside this phase;
# see CHANGELOG and README for the line it does not cross.
_HOMER_DIRECTIVE = " ".join((
    "You are HOMER, Nova's System Diagnostic Specialist — a focused, read-only"
    " sub-agent, not Nova itself.",
    "You have no tool that controls a device, changes a setting, writes data,"
    " sends a notification, dismisses an alert, or delegates work to anyone"
    " else; nothing you say makes any of those happen.",
    "Investigate the supplied fault using only the diagnostic, telemetry, and"
    " state tools you've been given.",
    "Separate what you OBSERVE (a fact an actual tool call returned) from what"
    " you INFER (your reasoning about what those facts mean) — do not guess,"
    " and do not blend the two without saying which is which.",
    "When the evidence supports one, identify the most likely cause and cite"
    " the evidence for it; when it doesn't, say plainly what remains uncertain"
    " rather than filling the gap.",
    "Close with one concrete recommended next step.",
    "Report back to the parent agent — you never address a device directly,"
    " and you never claim to have fixed, repaired, or changed anything; you"
    " only report findings.",
))


AGENT_PROFILES: dict = {
    "homer": {
        "label": "HOMER",
        "max_turns": 4,
        "tools": frozenset((
            "system_diagnostics", "cognitive_status", "connectivity_status",
            "root_cause", "energy_status", "activity_history",
            "get_entity_state", "search_entities",
        )),
        "directive": _HOMER_DIRECTIVE,
    },
}


def _resolve_profile(name: str):
    """A named profile as (tools, max_turns, label, directive), or None when
    the name is unknown (the caller words its own error).

    The name is matched ignoring case and surrounding space. The tools never
    include the deny list, since no profile is exempt from it. Everything
    returned comes from this module: the caller's arguments, the objective
    text and the model's own output cannot add to it or change it."""
    profile = AGENT_PROFILES.get(str(name or "").strip().lower())
    if not profile:
        return None
    tools = set(profile["tools"]) - _SUBAGENT_DENY
    return tools, int(profile["max_turns"]), str(profile["label"]), str(profile["directive"])


def _scoped_tool_list(allowed_tools: Optional[set]) -> list:
    """Every NOVA_TOOLS entry, or for a scoped sub-agent only those named."""
    if allowed_tools is None:
        return list(NOVA_TOOLS)
    return [spec for spec in NOVA_TOOLS
            if spec.get("function", {}).get("name") in allowed_tools]


# The smallest tool list for the retry after a 413. The whole NOVA_TOOLS schema
# is some 6 to 7K tokens by itself, so keeping all of it could still be too big
# for a size-limited request even once the per-entity Home Assistant tools are
# gone. These few tools are enough to answer and to do basic control, and
# search_entities finds anything else.
_SLIM_TOOLS = {
    "get_home_summary", "get_entity_state", "search_entities", "get_area_devices",
    "control_device", "bulk_control", "run_scene_or_script",
}


# ── The grant for one run ────────────────────────────────────────────────────
# The dispatcher checks the run's grant on every call. The tool list sent to
# the model and the tool guidance in the prompt come from that same grant.

# A live conversation: all Nova tools plus Home Assistant's own LLM API tools.
MAIN_GRANT = ToolGrant("main", None, include_ha_tools=True)

# Scheduled work with nobody behind it (self-scheduled follow-ups, goal
# engagements) may look, check, diagnose and report, and never act. When such a
# job finds something that needs doing, its report says so. update_goal is the
# single write allowed: the goal prompt requires it, it records progress on the
# goal being engaged, and it cannot create, cancel or act on anything else.
HEADLESS_TOOLS: frozenset = frozenset((
    "get_entity_state", "search_entities", "get_area_devices",
    "get_home_summary", "activity_history", "cognitive_status",
    "connectivity_status", "system_diagnostics", "root_cause", "energy_status",
    "solar_status", "energy_report", "energy_outlook", "hazard_report",
    "weather_forecast", "wellbeing_context", "calendar_agenda",
    "who_do_you_see", "look_at_camera", "update_goal",
))
HEADLESS_GRANT = ToolGrant("headless", HEADLESS_TOOLS)


def resolve_grant(allowed_tools, *, depth: int, headless: bool) -> ToolGrant:
    """Pick the grant for one run. The choice is made on the server and nothing
    a model emits can widen it.

    * A sub-agent (depth above 0) gets just the tools it was granted, less the
      deny list. It never gets Home Assistant's tools, and never everything by
      default.
    * A headless run gets HEADLESS_TOOLS. An explicit ``allowed_tools`` can
      narrow that set and cannot add to it.
    * A conversation gets the main grant, or an explicit scoped set."""
    if depth > 0:
        permitted = frozenset(allowed_tools or ()) - _SUBAGENT_DENY
        return ToolGrant("delegated", permitted)
    if headless:
        if allowed_tools is None:
            return HEADLESS_GRANT
        return ToolGrant("headless", frozenset(allowed_tools) & HEADLESS_TOOLS)
    if allowed_tools is None:
        return MAIN_GRANT
    return ToolGrant("scoped", frozenset(allowed_tools))
