"""The single name -> executor table for Nova's own tools."""
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
