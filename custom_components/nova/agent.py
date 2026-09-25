"""Nova's agent — the public compatibility façade (Phase 7).

The agent's implementation lives in ``agent_runtime``: prompt and context
construction, the tool definitions and registry, grants, dispatch, the
per-capability executors, delegation and the provider/model turn loop.

This module keeps every name that production code and tests import from
``agent`` — ``run_agent`` first among them — as the very same objects. It
also keeps them patchable here: setting (or deleting) one of these names on
this module sets it on the module that owns it too, so a monkeypatch of, say,
``agent._load_learned`` or ``agent.run_agent`` reaches the code that calls it.
Nothing is copied: there is one definition and one value of each.
"""
from __future__ import annotations

import logging
import sys
import types

from .agent_runtime.capabilities import automation as _m_capabilities_automation
from .agent_runtime.capabilities import cameras as _m_capabilities_cameras
from .agent_runtime.capabilities import communications as _m_capabilities_communications
from .agent_runtime.capabilities import control as _m_capabilities_control
from .agent_runtime.capabilities import diagnostics as _m_capabilities_diagnostics
from .agent_runtime.capabilities import environment as _m_capabilities_environment
from .agent_runtime.capabilities import home as _m_capabilities_home
from .agent_runtime.capabilities import memory as _m_capabilities_memory
from .agent_runtime.capabilities import planning as _m_capabilities_planning
from .agent_runtime.capabilities import safety_modes as _m_capabilities_safety_modes
from .agent_runtime.capabilities import specialists as _m_capabilities_specialists
from .agent_runtime import context as _m_context
from .agent_runtime import delegation as _m_delegation
from .agent_runtime import dispatcher as _m_dispatcher
from .agent_runtime import grants as _m_grants
from .agent_runtime import ha_tools as _m_ha_tools
from .agent_runtime import loop as _m_loop
from .agent_runtime import registry as _m_registry
from .agent_runtime import tool_specs as _m_tool_specs
from .agent_runtime.capabilities.automation import (
    _exec_approve_suggestion,
    _exec_dismiss_suggestion,
    _exec_manage_autonomy,
    _exec_review_suggestions,
)
from .agent_runtime.capabilities.cameras import (
    _analyze_camera,
    _exec_look_at_camera,
    _exec_who_do_you_see,
    _shape_look_at_camera_result,
)
from .agent_runtime.capabilities.communications import (
    _exec_calendar_agenda,
    _exec_read_email,
    _exec_web_research,
)
from .agent_runtime.capabilities.control import (
    VERIFY_DELAY_SECS,
    _EXECUTE_PLAN_ALLOWED_DOMAINS,
    _EXPECTED_STATES,
    _TRANSITIONAL,
    _VERIFY_SLEEP,
    _exec_bulk_control,
    _exec_control_device,
    _exec_execute_plan,
    _exec_run_scene_script,
    _state_ok,
    _verify_control,
)
from .agent_runtime.capabilities.diagnostics import (
    _exec_cognitive_status,
    _exec_connectivity_status,
    _exec_root_cause,
    _exec_system_diagnostics,
)
from .agent_runtime.capabilities.environment import (
    _exec_energy_report,
    _exec_energy_status,
    _exec_hazard_report,
    _exec_solar_status,
    _exec_weather_forecast,
    _exec_wellbeing_context,
)
from .agent_runtime.capabilities.home import (
    _build_clarification,
    _dedupe_candidates,
    _exec_activity_history,
    _exec_get_area_devices,
    _exec_get_entity_state,
    _exec_home_summary,
    _exec_search_entities,
)
from .agent_runtime.capabilities.memory import (
    _LEARN_FILE,
    _exec_confirm_pending_fact,
    _exec_ignore,
    _exec_ingest_documents,
    _exec_reject_pending_fact,
    _exec_remember,
    _exec_search_documents,
    _exec_unignore,
    _load_learned,
    _save_learned,
)
from .agent_runtime.capabilities.planning import (
    _exec_create_goal,
    _exec_manage_followups,
    _exec_manage_goals,
    _exec_schedule_followup,
    _exec_update_goal,
)
from .agent_runtime.capabilities.safety_modes import (
    _exec_acknowledge_alert,
    _exec_dismiss_intrusion,
    _exec_set_mode,
)
from .agent_runtime.capabilities.specialists import (
    N8N_WEBHOOK_HEADER,
    N8N_WEBHOOK_SECRET_KEY,
    _N8N_AGENT_TIMEOUT,
    _N8N_DEFAULT_BASE_URL,
    _N8N_SSH_TIMEOUT,
    _ask_n8n_specialist,
    _exec_ask_executive_assistant,
    _exec_ask_homelab_infra_agent,
    _exec_ask_house_manager_agent,
    _exec_ask_marketing_agent,
    _exec_ask_security_privacy_agent,
)
from .agent_runtime.context import (
    _LANG_NAMES,
    _banter_guidance,
    _build_home_context,
    _language_directive,
    _strip_home_state,
)
from .agent_runtime.delegation import (
    _run_delegated,
)
from .agent_runtime.dispatcher import (
    MAX_TOOL_RETRIES,
    _execute_tool,
)
from .agent_runtime.grants import (
    AGENT_PROFILES,
    _SUBAGENT_DENY,
    _LEGACY_CAPABILITY_PROFILE_ALIASES,
    CAPABILITY_GROUPS,
    MAX_DELEGATION_DEPTH,
    _DELEGATION_MAX_TURNS,
    _HOMER_DIRECTIVE,
    _MUTATING_TOOL_NAMES,
    _SLIM_TOOLS,
    _resolve_capability,
    _resolve_profile,
    _scoped_tool_list,
)
from .agent_runtime.ha_tools import (
    _ha_kwargs,
    _ha_tools_to_openai_format,
    _json_safe,
)
from .agent_runtime.loop import (
    MAX_TOOL_ITERATIONS,
    SUMMARIZE_KEEP,
    SUMMARIZE_THRESHOLD,
    _TurnProviders,
    _create_provider_with_fallback,
    _error_text,
    _is_connectivity_error,
    _is_model_not_found,
    _is_too_large,
    _is_tool_format_error,
    _maybe_summarize,
    _run_agent_turn,
    run_agent,
)
from .agent_runtime.registry import (
    _TOOL_MAP,
)
from .agent_runtime.tool_specs import (
    NOVA_TOOLS,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    'AGENT_PROFILES',
    '_SUBAGENT_DENY',
    '_LEGACY_CAPABILITY_PROFILE_ALIASES',
    'CAPABILITY_GROUPS',
    'MAX_DELEGATION_DEPTH',
    'MAX_TOOL_ITERATIONS',
    'MAX_TOOL_RETRIES',
    'N8N_WEBHOOK_HEADER',
    'N8N_WEBHOOK_SECRET_KEY',
    'NOVA_TOOLS',
    'SUMMARIZE_KEEP',
    'SUMMARIZE_THRESHOLD',
    'VERIFY_DELAY_SECS',
    '_DELEGATION_MAX_TURNS',
    '_EXECUTE_PLAN_ALLOWED_DOMAINS',
    '_EXPECTED_STATES',
    '_HOMER_DIRECTIVE',
    '_LANG_NAMES',
    '_LEARN_FILE',
    '_LOGGER',
    '_MUTATING_TOOL_NAMES',
    '_N8N_AGENT_TIMEOUT',
    '_N8N_DEFAULT_BASE_URL',
    '_N8N_SSH_TIMEOUT',
    '_SLIM_TOOLS',
    '_TOOL_MAP',
    '_TRANSITIONAL',
    '_TurnProviders',
    '_VERIFY_SLEEP',
    '_analyze_camera',
    '_ask_n8n_specialist',
    '_banter_guidance',
    '_build_clarification',
    '_build_home_context',
    '_create_provider_with_fallback',
    '_dedupe_candidates',
    '_error_text',
    '_exec_acknowledge_alert',
    '_exec_activity_history',
    '_exec_approve_suggestion',
    '_exec_ask_executive_assistant',
    '_exec_ask_homelab_infra_agent',
    '_exec_ask_house_manager_agent',
    '_exec_ask_marketing_agent',
    '_exec_ask_security_privacy_agent',
    '_exec_bulk_control',
    '_exec_calendar_agenda',
    '_exec_cognitive_status',
    '_exec_confirm_pending_fact',
    '_exec_connectivity_status',
    '_exec_control_device',
    '_exec_create_goal',
    '_exec_dismiss_intrusion',
    '_exec_dismiss_suggestion',
    '_exec_energy_report',
    '_exec_energy_status',
    '_exec_execute_plan',
    '_exec_get_area_devices',
    '_exec_get_entity_state',
    '_exec_hazard_report',
    '_exec_home_summary',
    '_exec_ignore',
    '_exec_ingest_documents',
    '_exec_look_at_camera',
    '_exec_manage_autonomy',
    '_exec_manage_followups',
    '_exec_manage_goals',
    '_exec_read_email',
    '_exec_reject_pending_fact',
    '_exec_remember',
    '_exec_review_suggestions',
    '_exec_root_cause',
    '_exec_run_scene_script',
    '_exec_schedule_followup',
    '_exec_search_documents',
    '_exec_search_entities',
    '_exec_set_mode',
    '_exec_solar_status',
    '_exec_system_diagnostics',
    '_exec_unignore',
    '_exec_update_goal',
    '_exec_weather_forecast',
    '_exec_web_research',
    '_exec_wellbeing_context',
    '_exec_who_do_you_see',
    '_execute_tool',
    '_ha_kwargs',
    '_ha_tools_to_openai_format',
    '_is_connectivity_error',
    '_is_model_not_found',
    '_is_too_large',
    '_is_tool_format_error',
    '_json_safe',
    '_language_directive',
    '_load_learned',
    '_maybe_summarize',
    '_resolve_capability',
    '_resolve_profile',
    '_run_agent_turn',
    '_run_delegated',
    '_save_learned',
    '_scoped_tool_list',
    '_shape_look_at_camera_result',
    '_state_ok',
    '_strip_home_state',
    '_verify_control',
    'run_agent',
]


# Name -> the agent_runtime module that owns it.
_OWNERS = {
    'AGENT_PROFILES': _m_grants,
    '_SUBAGENT_DENY': _m_grants,
    '_LEGACY_CAPABILITY_PROFILE_ALIASES': _m_grants,
    'CAPABILITY_GROUPS': _m_grants,
    'MAX_DELEGATION_DEPTH': _m_grants,
    'MAX_TOOL_ITERATIONS': _m_loop,
    'MAX_TOOL_RETRIES': _m_dispatcher,
    'N8N_WEBHOOK_HEADER': _m_capabilities_specialists,
    'N8N_WEBHOOK_SECRET_KEY': _m_capabilities_specialists,
    'NOVA_TOOLS': _m_tool_specs,
    'SUMMARIZE_KEEP': _m_loop,
    'SUMMARIZE_THRESHOLD': _m_loop,
    'VERIFY_DELAY_SECS': _m_capabilities_control,
    '_DELEGATION_MAX_TURNS': _m_grants,
    '_EXECUTE_PLAN_ALLOWED_DOMAINS': _m_capabilities_control,
    '_EXPECTED_STATES': _m_capabilities_control,
    '_HOMER_DIRECTIVE': _m_grants,
    '_LANG_NAMES': _m_context,
    '_LEARN_FILE': _m_capabilities_memory,
    '_MUTATING_TOOL_NAMES': _m_grants,
    '_N8N_AGENT_TIMEOUT': _m_capabilities_specialists,
    '_N8N_DEFAULT_BASE_URL': _m_capabilities_specialists,
    '_N8N_SSH_TIMEOUT': _m_capabilities_specialists,
    '_SLIM_TOOLS': _m_grants,
    '_TOOL_MAP': _m_registry,
    '_TRANSITIONAL': _m_capabilities_control,
    '_TurnProviders': _m_loop,
    '_VERIFY_SLEEP': _m_capabilities_control,
    '_analyze_camera': _m_capabilities_cameras,
    '_ask_n8n_specialist': _m_capabilities_specialists,
    '_banter_guidance': _m_context,
    '_build_clarification': _m_capabilities_home,
    '_build_home_context': _m_context,
    '_create_provider_with_fallback': _m_loop,
    '_dedupe_candidates': _m_capabilities_home,
    '_error_text': _m_loop,
    '_exec_acknowledge_alert': _m_capabilities_safety_modes,
    '_exec_activity_history': _m_capabilities_home,
    '_exec_approve_suggestion': _m_capabilities_automation,
    '_exec_ask_executive_assistant': _m_capabilities_specialists,
    '_exec_ask_homelab_infra_agent': _m_capabilities_specialists,
    '_exec_ask_house_manager_agent': _m_capabilities_specialists,
    '_exec_ask_marketing_agent': _m_capabilities_specialists,
    '_exec_ask_security_privacy_agent': _m_capabilities_specialists,
    '_exec_bulk_control': _m_capabilities_control,
    '_exec_calendar_agenda': _m_capabilities_communications,
    '_exec_cognitive_status': _m_capabilities_diagnostics,
    '_exec_confirm_pending_fact': _m_capabilities_memory,
    '_exec_connectivity_status': _m_capabilities_diagnostics,
    '_exec_control_device': _m_capabilities_control,
    '_exec_create_goal': _m_capabilities_planning,
    '_exec_dismiss_intrusion': _m_capabilities_safety_modes,
    '_exec_dismiss_suggestion': _m_capabilities_automation,
    '_exec_energy_report': _m_capabilities_environment,
    '_exec_energy_status': _m_capabilities_environment,
    '_exec_execute_plan': _m_capabilities_control,
    '_exec_get_area_devices': _m_capabilities_home,
    '_exec_get_entity_state': _m_capabilities_home,
    '_exec_hazard_report': _m_capabilities_environment,
    '_exec_home_summary': _m_capabilities_home,
    '_exec_ignore': _m_capabilities_memory,
    '_exec_ingest_documents': _m_capabilities_memory,
    '_exec_look_at_camera': _m_capabilities_cameras,
    '_exec_manage_autonomy': _m_capabilities_automation,
    '_exec_manage_followups': _m_capabilities_planning,
    '_exec_manage_goals': _m_capabilities_planning,
    '_exec_read_email': _m_capabilities_communications,
    '_exec_reject_pending_fact': _m_capabilities_memory,
    '_exec_remember': _m_capabilities_memory,
    '_exec_review_suggestions': _m_capabilities_automation,
    '_exec_root_cause': _m_capabilities_diagnostics,
    '_exec_run_scene_script': _m_capabilities_control,
    '_exec_schedule_followup': _m_capabilities_planning,
    '_exec_search_documents': _m_capabilities_memory,
    '_exec_search_entities': _m_capabilities_home,
    '_exec_set_mode': _m_capabilities_safety_modes,
    '_exec_solar_status': _m_capabilities_environment,
    '_exec_system_diagnostics': _m_capabilities_diagnostics,
    '_exec_unignore': _m_capabilities_memory,
    '_exec_update_goal': _m_capabilities_planning,
    '_exec_weather_forecast': _m_capabilities_environment,
    '_exec_web_research': _m_capabilities_communications,
    '_exec_wellbeing_context': _m_capabilities_environment,
    '_exec_who_do_you_see': _m_capabilities_cameras,
    '_execute_tool': _m_dispatcher,
    '_ha_kwargs': _m_ha_tools,
    '_ha_tools_to_openai_format': _m_ha_tools,
    '_is_connectivity_error': _m_loop,
    '_is_model_not_found': _m_loop,
    '_is_too_large': _m_loop,
    '_is_tool_format_error': _m_loop,
    '_json_safe': _m_ha_tools,
    '_language_directive': _m_context,
    '_load_learned': _m_capabilities_memory,
    '_maybe_summarize': _m_loop,
    '_resolve_capability': _m_grants,
    '_resolve_profile': _m_grants,
    '_run_agent_turn': _m_loop,
    '_run_delegated': _m_delegation,
    '_save_learned': _m_capabilities_memory,
    '_scoped_tool_list': _m_grants,
    '_shape_look_at_camera_result': _m_capabilities_cameras,
    '_state_ok': _m_capabilities_control,
    '_strip_home_state': _m_context,
    '_verify_control': _m_capabilities_control,
    'run_agent': _m_loop,
}


class _Facade(types.ModuleType):
    """Forward writes of an owned name to its owning module as well."""

    def __setattr__(self, name, value):
        owner = _OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        owner = _OWNERS.get(name)
        if owner is not None and hasattr(owner, name):
            delattr(owner, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade
