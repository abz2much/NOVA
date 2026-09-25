"""The single source of truth for Nova's own tools: name -> executor, and what
each tool may do (capability, mutation, persistence, network reach and the
trust of its result)."""
from __future__ import annotations

import logging


from .capabilities.automation import (
    _exec_approve_suggestion,
    _exec_dismiss_suggestion,
    _exec_manage_autonomy,
    _exec_review_suggestions,
)
from .capabilities.cameras import (
    _exec_look_at_camera,
    _exec_who_do_you_see,
)
from .capabilities.communications import (
    _exec_calendar_agenda,
    _exec_read_email,
    _exec_web_research,
)
from .capabilities.control import (
    _exec_bulk_control,
    _exec_control_device,
    _exec_execute_plan,
    _exec_run_scene_script,
)
from .capabilities.diagnostics import (
    _exec_cognitive_status,
    _exec_connectivity_status,
    _exec_root_cause,
    _exec_system_diagnostics,
)
from .capabilities.environment import (
    _exec_energy_report,
    _exec_energy_status,
    _exec_hazard_report,
    _exec_solar_status,
    _exec_weather_forecast,
    _exec_wellbeing_context,
)
from .capabilities.home import (
    _exec_activity_history,
    _exec_get_area_devices,
    _exec_get_entity_state,
    _exec_home_summary,
    _exec_search_entities,
)
from .capabilities.memory import (
    _exec_confirm_pending_fact,
    _exec_ignore,
    _exec_ingest_documents,
    _exec_reject_pending_fact,
    _exec_remember,
    _exec_search_documents,
    _exec_unignore,
)
from .capabilities.planning import (
    _exec_create_goal,
    _exec_manage_followups,
    _exec_manage_goals,
    _exec_schedule_followup,
    _exec_update_goal,
)
from .capabilities.safety_modes import (
    _exec_acknowledge_alert,
    _exec_dismiss_intrusion,
    _exec_set_mode,
)
from .capabilities.specialists import (
    _exec_ask_executive_assistant,
    _exec_ask_homelab_infra_agent,
    _exec_ask_house_manager_agent,
    _exec_ask_marketing_agent,
    _exec_ask_security_privacy_agent,
)
from .grants import _SUBAGENT_DENY
from .models import RegisteredTool, Trust
from .tool_specs import NOVA_TOOLS

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


_TOOL_MAP = {
    "control_device":      _exec_control_device,
    "get_entity_state":    _exec_get_entity_state,
    "search_entities":     _exec_search_entities,
    "get_area_devices":    _exec_get_area_devices,
    "run_scene_or_script": _exec_run_scene_script,
    "get_home_summary":    _exec_home_summary,
    "bulk_control":        _exec_bulk_control,
    "execute_plan":        _exec_execute_plan,
    "remember":            _exec_remember,
    "confirm_pending_fact": _exec_confirm_pending_fact,
    "reject_pending_fact": _exec_reject_pending_fact,
    "ignore_entity":       _exec_ignore,
    "unignore_entity":     _exec_unignore,
    "cognitive_status":    _exec_cognitive_status,
    "connectivity_status": _exec_connectivity_status,
    "manage_autonomy":     _exec_manage_autonomy,
    "review_suggestions":  _exec_review_suggestions,
    "approve_suggestion":  _exec_approve_suggestion,
    "dismiss_suggestion":  _exec_dismiss_suggestion,
    "root_cause":          _exec_root_cause,
    "schedule_followup":   _exec_schedule_followup,
    "manage_followups":    _exec_manage_followups,
    "create_goal":         _exec_create_goal,
    "update_goal":         _exec_update_goal,
    "manage_goals":        _exec_manage_goals,
    "web_research":        _exec_web_research,
    "calendar_agenda":     _exec_calendar_agenda,
    "read_email":          _exec_read_email,
    "look_at_camera":      _exec_look_at_camera,
    "who_do_you_see":      _exec_who_do_you_see,
    "dismiss_intrusion":   _exec_dismiss_intrusion,
    "acknowledge_alert":   _exec_acknowledge_alert,
    "system_diagnostics":  _exec_system_diagnostics,
    "set_mode":            _exec_set_mode,
    "energy_status":       _exec_energy_status,
    "solar_status":        _exec_solar_status,
    "energy_report":       _exec_energy_report,
    "hazard_report":       _exec_hazard_report,
    "activity_history":    _exec_activity_history,
    "weather_forecast":    _exec_weather_forecast,
    "wellbeing_context":   _exec_wellbeing_context,
    "search_documents":    _exec_search_documents,
    "ingest_documents":    _exec_ingest_documents,
    "ask_executive_assistant":      _exec_ask_executive_assistant,
    "ask_marketing_agent":          _exec_ask_marketing_agent,
    "ask_security_privacy_agent":   _exec_ask_security_privacy_agent,
    "ask_homelab_infra_agent":      _exec_ask_homelab_infra_agent,
    "ask_house_manager_agent":      _exec_ask_house_manager_agent,
}


# ── Classification: the one source of truth for what each tool may do ─────


_H = Trust.HOUSEHOLD_DATA
_X = Trust.UNTRUSTED_EXTERNAL

# name: (capability, mutates, persists, network, trust)
_CLASSIFICATION = {
    "control_device":             ("control", True, False, False, _H),
    "bulk_control":               ("control", True, False, False, _H),
    "run_scene_or_script":        ("control", True, False, False, _H),
    "execute_plan":               ("control", True, False, False, _H),
    "get_entity_state":           ("home", False, False, False, _H),
    "search_entities":            ("home", False, False, False, _H),
    "get_area_devices":           ("home", False, False, False, _H),
    "get_home_summary":           ("home", False, False, False, _H),
    "activity_history":           ("home", False, False, False, _H),
    "remember":                   ("memory", False, True, False, _H),
    "confirm_pending_fact":       ("memory", False, True, False, _H),
    "reject_pending_fact":        ("memory", False, True, False, _H),
    "ignore_entity":              ("memory", False, True, False, _H),
    "unignore_entity":            ("memory", False, True, False, _H),
    "search_documents":           ("memory", False, False, False, _X),
    "ingest_documents":           ("memory", False, True, False, _H),
    "manage_autonomy":            ("automation", True, True, False, _H),
    "review_suggestions":         ("automation", False, False, False, _H),
    "approve_suggestion":         ("automation", True, True, False, _H),
    "dismiss_suggestion":         ("automation", False, True, False, _H),
    "schedule_followup":          ("planning", False, True, False, _H),
    "manage_followups":           ("planning", False, True, False, _H),
    "create_goal":                ("planning", False, True, False, _H),
    "update_goal":                ("planning", False, True, False, _H),
    "manage_goals":               ("planning", False, True, False, _H),
    "cognitive_status":           ("diagnostics", False, False, False, _H),
    "connectivity_status":        ("diagnostics", False, False, False, _H),
    "system_diagnostics":         ("diagnostics", False, False, True, _H),
    "root_cause":                 ("diagnostics", False, False, False, _H),
    "set_mode":                   ("safety_modes", True, True, False, _H),
    "acknowledge_alert":          ("safety_modes", True, True, False, _H),
    "dismiss_intrusion":          ("safety_modes", True, True, False, _H),
    "look_at_camera":             ("cameras", False, False, True, _X),
    "who_do_you_see":             ("cameras", False, False, False, _H),
    "wellbeing_context":          ("environment", False, False, False, _H),
    "energy_status":              ("environment", False, False, False, _H),
    "solar_status":               ("environment", False, False, False, _H),
    "energy_report":              ("environment", False, False, False, _H),
    "hazard_report":              ("environment", False, False, True, _X),
    "weather_forecast":           ("environment", False, False, False, _H),
    "web_research":               ("communications", False, False, True, _X),
    "calendar_agenda":            ("communications", False, False, False, _X),
    "read_email":                 ("communications", False, False, True, _X),
    "ask_executive_assistant":    ("specialists", False, False, True, _X),
    "ask_marketing_agent":        ("specialists", False, False, True, _X),
    "ask_security_privacy_agent": ("specialists", False, False, True, _X),
    "ask_homelab_infra_agent":    ("specialists", False, False, True, _X),
    "ask_house_manager_agent":    ("specialists", False, False, True, _X),
    # A sub-agent's report is model-written and may relay text it read
    # (web pages, email, calendars): the parent receives it as quoted data.
    "delegate_task":              ("delegation", False, False, False, _X),
}

TOOL_REGISTRY: dict = {
    name: RegisteredTool(name=name, executor=_TOOL_MAP.get(name), capability=cap,
                         mutates=mut, persists=per, network=net, trust=trust)
    for name, (cap, mut, per, net, trust) in _CLASSIFICATION.items()
}


def trust_of(tool_name: str) -> Trust:
    """A Nova tool's result trust; anything else (Home Assistant's own LLM API
    tools) returns household data."""
    row = TOOL_REGISTRY.get(tool_name)
    return row.trust if row is not None else Trust.HOUSEHOLD_DATA


def _check_registry() -> None:
    """Import-time invariants. A broken registry must fail loudly at load,
    never ship a tool the dispatcher cannot classify."""
    offered = [t["function"]["name"] for t in NOVA_TOOLS]
    if len(offered) != len(set(offered)):
        raise RuntimeError("agent registry: duplicate tool name in NOVA_TOOLS")
    if set(offered) != set(TOOL_REGISTRY):
        raise RuntimeError("agent registry: NOVA_TOOLS and the classification disagree: "
                           f"{sorted(set(offered) ^ set(TOOL_REGISTRY))}")
    if set(_TOOL_MAP) != set(TOOL_REGISTRY) - {"delegate_task"}:
        raise RuntimeError("agent registry: every tool but delegate_task needs an executor")
    unsafe = {n for n, r in TOOL_REGISTRY.items()
              if r.mutates or r.persists or r.capability in ("specialists", "delegation")}
    if not unsafe <= _SUBAGENT_DENY:
        raise RuntimeError("agent registry: mutating, persisting, specialist and "
                           f"delegation tools must be sub-agent denied: {sorted(unsafe - _SUBAGENT_DENY)}")


_check_registry()
