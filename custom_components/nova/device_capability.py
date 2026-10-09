"""Can Nova really do this? (8.14.0)

One shared check, used by every proactive offer before it asks "shall I...?".
An offer is only made when the entity exists in the right domain, is not
unavailable, Home Assistant has the service, and the entity says it supports
the action. Otherwise the alert is information only: there is nothing for a
"yes" to do. Pure reads of hass state, no service calls, never raises.
"""
from __future__ import annotations

_DEAD = ("unavailable", "unknown")

# (domain, service) -> the supported_features bit the entity must report.
# Values are Home Assistant's own feature flags (CoverEntityFeature,
# LockEntityFeature, ClimateEntityFeature).
_FEATURE_NEEDED = {
    ("cover", "open_cover"): 1,
    ("cover", "close_cover"): 2,
    ("cover", "set_cover_position"): 4,
    ("cover", "stop_cover"): 8,
    ("lock", "open"): 1,
    ("climate", "set_preset_mode"): 16,
}


def can_perform(hass, entity_id, domain: str, service: str, service_data=None) -> bool:
    """True when Nova can carry out `domain.service` on `entity_id` right now."""
    try:
        if not entity_id or str(entity_id).split(".", 1)[0] != domain:
            return False
        state = hass.states.get(entity_id)
        if state is None or str(state.state).lower() in _DEAD:
            return False
        if not hass.services.has_service(domain, service):
            return False
        attrs = state.attributes or {}
        needed = _FEATURE_NEEDED.get((domain, service))
        if needed is not None:
            try:
                features = int(attrs.get("supported_features") or 0)
            except (TypeError, ValueError):
                features = 0
            if not features & needed:
                return False
        if (domain, service) == ("climate", "set_preset_mode"):
            preset = (service_data or {}).get("preset_mode")
            if preset not in (attrs.get("preset_modes") or []):
                return False
        return True
    except Exception:
        return False
