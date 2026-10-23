"""Registers Nova's sidebar panel.

The integration's ``frontend/`` folder is served from a static URL, the old
"Command Center" sidebar entry is removed, and the Nova custom panel is
registered with a content hash in its query string so browsers fetch a new
build. Registering again on every entry reload is safe. No ordinary exception
leaves this module: a boolean result is the only sign of failure.
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

# The Nova panel itself.
PANEL_STATIC_URL: Final = "/nova_panel_static"
PANEL_JS_FILENAME: Final = "nova-panel.js"
PANEL_WEBCOMPONENT: Final = "nova-panel"
PANEL_URL_PATH: Final = "nova"
PANEL_TITLE: Final = "Nova"
PANEL_ICON: Final = "mdi:robot-outline"

# The old standalone panel. Only its URL path is still used, to remove it.
CMD_JS_FILENAME: Final = "nova-command.js"
CMD_WEBCOMPONENT: Final = "nova-command"
CMD_URL_PATH: Final = "nova-command"
CMD_TITLE: Final = "Command Center"
CMD_ICON: Final = "mdi:hexagon-multiple-outline"


def _hash_file(path: str) -> str:
    """A short version token for ``path``. It never raises.

    The first choice is the first ten characters of the SHA-1 of the contents,
    then the whole-second modification time, then the current time."""
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
    """Whether this process already has a route for ``PANEL_STATIC_URL``. It
    never raises. If that cannot be told, the answer is no, so it is
    registered."""
    try:
        routes = hass.http.app.router.resources()
        return any(getattr(route, "canonical", None) == PANEL_STATIC_URL for route in routes)
    except Exception:  # noqa: BLE001
        return False


def _drop_panel(hass: HomeAssistant, url_path: str) -> None:
    """Remove a sidebar panel if one is there, and say nothing if not."""
    try:
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)
    except Exception:  # noqa: BLE001
        pass


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
    """Register one custom panel whose module is in ``panel_dir``."""
    js_path = os.path.join(panel_dir, js_filename)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel JS file not found: %s", js_path)
        return False

    token = await hass.async_add_executor_job(_hash_file, js_path)
    _drop_panel(hass, url_path)

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=url_path,
            webcomponent_name=webcomponent,
            module_url=f"{PANEL_STATIC_URL}/{js_filename}?v={token}",
            sidebar_title=title,
            sidebar_icon=icon,
            require_admin=False,
            embed_iframe=False,
            config=config,
        )
    except ValueError:
        # Home Assistant will not replace a panel, so this one is already there.
        _LOGGER.debug("Nova panel /%s already registered", url_path)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to register Nova panel /%s: %s", url_path, err)
        return False

    _LOGGER.info("Nova panel registered: /%s", url_path)
    return True


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Serve the frontend files and register Nova's sidebar panel."""
    panel_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if not os.path.isdir(panel_dir):
        _LOGGER.error("Nova frontend dir not found: %s", panel_dir)
        return False

    js_path = os.path.join(panel_dir, PANEL_JS_FILENAME)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel JS file not found: %s", js_path)
        return False

    # Home Assistant keeps a route for the life of the process, so a reload
    # must not add the same one twice.
    if not _static_path_served(hass):
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_STATIC_URL, panel_dir, cache_headers=False)]
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Static path %s not registered: %s", PANEL_STATIC_URL, err)

    # Take away the sidebar entry that older versions left behind.
    _drop_panel(hass, CMD_URL_PATH)

    return await _register_one(
        hass, panel_dir,
        webcomponent=PANEL_WEBCOMPONENT, url_path=PANEL_URL_PATH, title=PANEL_TITLE,
        icon=PANEL_ICON, js_filename=PANEL_JS_FILENAME,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Take Nova's sidebar entry away. The static path stays registered."""
    try:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not remove Nova panel: %s", err)
        return
    _LOGGER.info("Nova panel unregistered")
