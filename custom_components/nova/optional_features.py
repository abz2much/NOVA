"""Optional features: which ones are set up, and the AI tools each one owns
(8.41.0).

An optional feature does nothing until it is set up, and the AI is not
offered its tools before then (docs/product-scope.md). Core features are not
listed here: their tools are always offered.

Each check is cheap and reads only settings and entity states, because it
runs once per conversation turn on the event loop. A check that fails
counts as set up, so a fault here can never take a working tool away.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "on")


def _setting(hass: HomeAssistant, key: str, default: Any = None) -> Any:
    """The live panel value first, then config.json, then `default`."""
    try:
        from .runtime import domain_runtime_config
        rc = domain_runtime_config(hass)
        if key in rc and rc[key] not in (None, ""):
            return rc[key]
    except Exception:
        pass
    try:
        from . import nova_config
        value = nova_config.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _on(hass: HomeAssistant, key: str) -> bool:
    value = _setting(hass, key, False)
    return value if isinstance(value, bool) else str(value).lower() in _TRUE


def _email(hass: HomeAssistant) -> bool:
    return (_on(hass, "imap_enabled")
            and bool(str(_setting(hass, "imap_host", "") or "").strip()))


def _specialists(hass: HomeAssistant) -> bool:
    return bool(str(_setting(hass, "n8n_webhook_base_url", "") or "").strip())


def _wellbeing(hass: HomeAssistant) -> bool:
    return _on(hass, "biometrics_enabled")


def _web_research(hass: HomeAssistant) -> bool:
    backend = str(_setting(hass, "search_backend", "off") or "off").lower()
    return backend in ("duckduckgo", "searxng")


def _faces(hass: HomeAssistant) -> bool:
    return str(_setting(hass, "recognition_source", "off") or "off").lower() != "off"


def _cameras(hass: HomeAssistant) -> bool:
    return bool(hass.states.async_all("camera"))


def _energy(hass: HomeAssistant) -> bool:
    for st in hass.states.async_all("sensor"):
        if (st.attributes.get("device_class") or "") in ("power", "energy"):
            return True
    return False


# Feature -> (how to tell it is set up, the tools it owns).
FEATURES: dict[str, tuple[Callable[[HomeAssistant], bool], frozenset[str]]] = {
    "email": (_email, frozenset({"read_email"})),
    "specialists": (_specialists, frozenset({
        "ask_executive_assistant", "ask_marketing_agent",
        "ask_security_privacy_agent", "ask_homelab_infra_agent",
        "ask_house_manager_agent"})),
    "wellbeing": (_wellbeing, frozenset({"wellbeing_context"})),
    "web_research": (_web_research, frozenset({"web_research"})),
    "faces": (_faces, frozenset({"who_do_you_see"})),
    "cameras": (_cameras, frozenset({"look_at_camera", "where_last_seen", "what_changed"})),
    "energy": (_energy, frozenset({
        "energy_status", "solar_status", "energy_report", "energy_outlook"})),
}


def is_set_up(hass: HomeAssistant, feature: str) -> bool:
    """Whether one optional feature is set up. Unknown names and failed
    checks count as set up."""
    check = FEATURES.get(feature, (None, frozenset()))[0]
    if check is None:
        return True
    try:
        return bool(check(hass))
    except Exception as exc:
        _LOGGER.debug("optional feature check %s failed: %s", feature, exc)
        return True


def tools_not_set_up(hass: HomeAssistant) -> frozenset[str]:
    """The tools of every optional feature that is not set up yet."""
    hidden: set[str] = set()
    for feature, (_check, tools) in FEATURES.items():
        if not is_set_up(hass, feature):
            hidden |= tools
    return frozenset(hidden)


def status(hass: HomeAssistant) -> dict[str, bool]:
    """Each optional feature and whether it is set up, for diagnostics."""
    return {feature: is_set_up(hass, feature) for feature in FEATURES}
