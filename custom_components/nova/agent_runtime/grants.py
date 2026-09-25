"""Tool grants: sub-agent capability groups, named profiles, the deny list and the
slim and scoped tool lists."""
from __future__ import annotations

import logging
from typing import Optional


from .tool_specs import NOVA_TOOLS

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# Tools that call a real HA service against a specific entity, resolved via
# search_entities — deferred within a batch when a require_unique search is
# also present in that same batch, so an unresolved/ambiguous entity_id can
# never reach a service call (Phase 3, see run_agent's dispatch loop).
_MUTATING_TOOL_NAMES = {"control_device", "bulk_control", "run_scene_or_script", "execute_plan"}


# ── Ephemeral sub-agents (delegate_task, v6.83.0) ────────────────────────────
# Nova can spin up a scoped, single-purpose sub-agent for a complex slice of a
# request: a nested run_agent invocation — in-process, no separate process —
# handed a minimal objective, a curated read-only tool subset, and a small turn
# budget. On one GPU these run serially, so the value is a narrow focused context
# (less drift), not parallelism. Sub-agents cannot actuate, write persistent
# stores, or recurse: those tools are denied and depth is capped.

MAX_DELEGATION_DEPTH = 1        # parent (depth 0) may delegate; a sub-agent may not


_DELEGATION_MAX_TURNS = 6       # hard cap on a sub-agent's tool-loop iterations


# Curated capability groups -> the read-only tools a sub-agent of that kind gets.
#
# NOTE: there is deliberately no "diagnostics" entry here. Diagnostics used to
# be its own capability group with its own tool list, which would have left
# two competing definitions of "what a diagnostic sub-agent may do" once HOMER
# (a named profile, below) was added. HOMER is now the single, canonical
# diagnostic policy — its tool grant, turn cap, and prompt live in exactly one
# place (AGENT_PROFILES["homer"]) — and "diagnostics" survives only as a
# backward-compatible alias for it (see _resolve_capability / _run_delegated),
# not a second implementation. solar_status/energy_report were part of the
# old diagnostics group but are NOT diagnostic tools in HOMER's sense (they
# report totals/forecasts, not fault evidence) — they remain reachable the
# same way every other Nova tool is: directly by the main agent, or via a
# capability group where they actually fit (none currently curates them,
# same as before this phase).
CAPABILITY_GROUPS: dict = {
    "scheduling":  {"calendar_agenda", "read_email", "weather_forecast", "get_home_summary"},
    "inbox":       {"read_email", "calendar_agenda"},
    "home_state":  {"get_entity_state", "search_entities", "get_area_devices",
                    "get_home_summary", "activity_history"},
    "research":    {"web_research", "search_documents", "search_entities"},
    "environment": {"weather_forecast", "hazard_report"},
}


# Legacy capability names that now resolve to a named profile's canonical
# grant instead of their own entry in CAPABILITY_GROUPS above — preserves
# existing internal delegate_task(capability="diagnostics", ...) calls
# without maintaining a second diagnostics allowlist/prompt/turn-limit.
_LEGACY_CAPABILITY_PROFILE_ALIASES: dict = {
    "diagnostics": "homer",
}


# Never granted to a sub-agent, even if a group lists one (defense in depth):
# actuators, persistent-store writers, management, and delegate_task itself.
_SUBAGENT_DENY: set = {
    "control_device", "bulk_control", "run_scene_or_script", "execute_plan",
    "set_mode", "dismiss_intrusion", "acknowledge_alert",
    "ignore_entity", "unignore_entity",
    "create_goal", "update_goal", "manage_goals",
    "schedule_followup", "manage_followups",
    "approve_suggestion", "dismiss_suggestion", "review_suggestions",
    "manage_autonomy", "remember", "ingest_documents",
    "delegate_task",
}


def _resolve_capability(capability: str) -> set:
    """Capability group name -> tool-name set a sub-agent may use, always minus
    the denylist. A legacy alias (currently just 'diagnostics') resolves to
    its target profile's own tool set — the exact same grant _resolve_profile
    would return, not a parallel copy. Unknown group -> empty set."""
    cap = str(capability or "")
    alias_target = _LEGACY_CAPABILITY_PROFILE_ALIASES.get(cap)
    if alias_target:
        resolved = _resolve_profile(alias_target)
        return resolved[0] if resolved else set()
    return CAPABILITY_GROUPS.get(cap, set()) - _SUBAGENT_DENY


# ── Named sub-agent profiles (HOMER, Phase 7) ────────────────────────────────
# A named profile is a fixed (tools, turn cap, system directive) triple keyed
# by name — a more specialised alternative to the generic capability groups
# above, for a sub-agent that needs its own persona and stricter limits, not
# just a curated tool subset. Only HOMER exists today (a read-only diagnostic
# specialist, so it fits the existing "sub-agents never actuate" model exactly
# and needs no opt-in). An actuating profile is explicitly NOT part of this
# phase — see CHANGELOG/README for the boundary this deliberately does not
# cross.
_HOMER_DIRECTIVE = (
    "You are HOMER, Nova's System Diagnostic Specialist — a focused, read-only "
    "sub-agent, not Nova itself. You have no tool that controls a device, "
    "changes a setting, writes data, sends a notification, dismisses an alert, "
    "or delegates work to anyone else; nothing you say makes any of those "
    "happen. Investigate the supplied fault using only the diagnostic, "
    "telemetry, and state tools you've been given. Separate what you OBSERVE "
    "(a fact an actual tool call returned) from what you INFER (your reasoning "
    "about what those facts mean) — do not guess, and do not blend the two "
    "without saying which is which. When the evidence supports one, identify "
    "the most likely cause and cite the evidence for it; when it doesn't, say "
    "plainly what remains uncertain rather than filling the gap. Close with "
    "one concrete recommended next step. Report back to the parent agent — you "
    "never address a device directly, and you never claim to have fixed, "
    "repaired, or changed anything; you only report findings."
)


AGENT_PROFILES: dict = {
    "homer": {
        "label": "HOMER",
        "max_turns": 4,
        "tools": frozenset({
            "system_diagnostics", "cognitive_status", "connectivity_status",
            "energy_status", "activity_history", "get_entity_state",
            "search_entities", "root_cause",
        }),
        "directive": _HOMER_DIRECTIVE,
    },
}


def _resolve_profile(name: str):
    """Named profile -> (tools, max_turns, label, directive), always minus the
    denylist (defense in depth — no profile is exempt from it). Case-
    insensitive; unknown name returns None so the caller can build its own
    error. The tool set, turn cap, and directive are entirely server-side:
    nothing in the caller's args, objective text, or a model's own output can
    add to or change what's returned here."""
    prof = AGENT_PROFILES.get(str(name or "").strip().lower())
    if not prof:
        return None
    allowed = set(prof["tools"]) - _SUBAGENT_DENY
    return allowed, int(prof["max_turns"]), str(prof["label"]), str(prof["directive"])


def _scoped_tool_list(allowed_tools: Optional[set]) -> list:
    """Full NOVA_TOOLS, or — for a scoped sub-agent — only the named subset."""
    if allowed_tools is None:
        return list(NOVA_TOOLS)
    return [t for t in NOVA_TOOLS
            if t.get("function", {}).get("name") in allowed_tools]


# Minimal tool set for the 413 slim retry. The full NOVA_TOOLS schema is on the
# order of ~6–7K tokens on its own, so a retry that keeps all of it can still
# exceed a size-limited request even after the HA per-entity tools are dropped.
# This keeps only the essentials to answer and do basic control; Nova can find
# anything else via search_entities.
_SLIM_TOOLS = {
    "control_device", "get_entity_state", "search_entities",
    "run_scene_or_script", "get_area_devices", "bulk_control",
    "get_home_summary",
}
