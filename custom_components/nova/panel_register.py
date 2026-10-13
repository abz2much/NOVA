"""Sidebar panel registration for Nova.

Serves the integration's ``frontend/`` folder under a static URL, clears the
legacy "Command Center" sidebar entry, and registers the Nova custom panel with
a content-hash query string so browsers pick up new builds. Registration is
safe to repeat on every entry reload, and no ordinary exception escapes: the
boolean result is the only failure signal.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Final

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN  # noqa: F401  (kept for parity with other modules)

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH: Final = "nova"
PANEL_TITLE: Final = "Nova"
PANEL_ICON: Final = "mdi:robot-outline"
PANEL_WEBCOMPONENT: Final = "nova-panel"
PANEL_STATIC_URL: Final = "/nova_panel_static"
PANEL_JS_FILENAME: Final = "nova-panel.js"

# Legacy standalone panel; only the URL path is still used (for cleanup).
CMD_URL_PATH: Final = "nova-command"
CMD_TITLE: Final = "Command Center"
CMD_ICON: Final = "mdi:hexagon-multiple-outline"
CMD_WEBCOMPONENT: Final = "nova-command"
CMD_JS_FILENAME: Final = "nova-command.js"


def _hash_file(path: str) -> str:
    """Return a short version token for ``path``; never raises.

    Prefers a SHA-1 prefix of the contents, then the integer mtime, then the
    current time.
    """
    try:
        with open(path, "rb") as handle:
            return hashlib.sha1(handle.read()).hexdigest()[:10]
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(int(os.path.getmtime(path)))
    except Exception:  # noqa: BLE001
        return str(int(time.time()))


def _static_path_served(hass: HomeAssistant) -> bool:
    """True if this process already routes ``PANEL_STATIC_URL``; never raises."""
    try:
        return any(
            getattr(resource, "canonical", None) == PANEL_STATIC_URL
            for resource in hass.http.app.router.resources()
        )
    except Exception:  # noqa: BLE001 - unknown means "register it"
        return False


async def _register_one(
    hass: HomeAssistant,
    panel_dir: str,
    *,
    webcomponent: str,
    url_path: str,
    title: str,
    icon: str,
    js_filename: str,
    config: dict[str, Any] | None = None,
) -> bool:
    """Register one custom panel whose module lives in ``panel_dir``."""
    js_path = os.path.join(panel_dir, js_filename)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel JS file not found: %s", js_path)
        return False

    version = await hass.async_add_executor_job(_hash_file, js_path)
    module_url = f"{PANEL_STATIC_URL}/{js_filename}?v={version}"

    # Clear any earlier registration so a reload can register afresh.
    try:
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)
    except Exception:  # noqa: BLE001
        pass

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=url_path,
            webcomponent_name=webcomponent,
            module_url=module_url,
            sidebar_title=title,
            sidebar_icon=icon,
            require_admin=False,
            embed_iframe=False,
            config=config,
        )
    except ValueError:
        # HA refuses to overwrite an existing panel; it is already there.
        _LOGGER.debug("Nova panel /%s already registered", url_path)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to register Nova panel /%s: %s", url_path, err)
        return False

    _LOGGER.info("Nova panel registered: /%s", url_path)
    return True


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Serve the frontend files and register the Nova sidebar panel."""
    panel_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if not os.path.isdir(panel_dir):
        _LOGGER.error("Nova frontend dir not found: %s", panel_dir)
        return False

    js_path = os.path.join(panel_dir, PANEL_JS_FILENAME)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel JS file not found: %s", js_path)
        return False

    # HA keeps routes for the whole process, so a reload must not add the same
    # route again.
    if not _static_path_served(hass):
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_STATIC_URL, panel_dir, cache_headers=False)]
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Static path %s not registered: %s", PANEL_STATIC_URL, err)

    # Drop the sidebar entry left behind by older versions.
    try:
        frontend.async_remove_panel(hass, CMD_URL_PATH, warn_if_unknown=False)
    except Exception:  # noqa: BLE001
        pass

    return await _register_one(
        hass,
        panel_dir,
        webcomponent=PANEL_WEBCOMPONENT,
        url_path=PANEL_URL_PATH,
        title=PANEL_TITLE,
        icon=PANEL_ICON,
        js_filename=PANEL_JS_FILENAME,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the Nova sidebar entry (the static path stays registered)."""
    try:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not remove Nova panel: %s", err)
        return
    _LOGGER.info("Nova panel unregistered")
