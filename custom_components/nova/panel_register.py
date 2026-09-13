"""
Nova Panel Registration (v5.7.00).

Registers the Nova custom panel with HA's frontend. Called from
async_setup_entry. Idempotent — safe to call repeatedly on entry reload.

Session 1: static asset serving + panel_custom registration.
Session 2+: WebSocket API for live data.
"""
from __future__ import annotations

import logging
import os
from typing import Final

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH:       Final = "nova"
PANEL_TITLE:          Final = "Nova"
PANEL_ICON:           Final = "mdi:robot-outline"
PANEL_WEBCOMPONENT:   Final = "nova-panel"
PANEL_STATIC_URL:     Final = "/nova_panel_static"
PANEL_JS_FILENAME:    Final = "nova-panel.js"
NEW_LOOK_JS_FILENAME: Final = "nova-panel-new.js"  # v7.93.0 — dynamically
                                                     # imported by the shell
                                                     # only when ui_style='new'

# Command Center — the operational HUD (separate sidebar entry, same static dir)
CMD_URL_PATH:         Final = "nova-command"
CMD_TITLE:            Final = "Command Center"
CMD_ICON:             Final = "mdi:hexagon-multiple-outline"
CMD_WEBCOMPONENT:     Final = "nova-command"
CMD_JS_FILENAME:      Final = "nova-command.js"


def _hash_file(path: str) -> str:
    """Content hash for cache-busting; mtime/time fallback if unreadable."""
    try:
        import hashlib
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except Exception:
        try:
            return str(int(os.path.getmtime(path)))
        except Exception:
            import time
            return str(int(time.time()))


async def _register_one(
    hass: HomeAssistant, panel_dir: str, *,
    webcomponent: str, url_path: str, title: str, icon: str, js_filename: str,
    config: dict | None = None,
) -> bool:
    """Register a single custom panel served from the shared static dir."""
    js_path = os.path.join(panel_dir, js_filename)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel: JS file not found at %s", js_path)
        return False
    file_hash = await hass.async_add_executor_job(_hash_file, js_path)
    module_url = f"{PANEL_STATIC_URL}/{js_filename}?v={file_hash}"
    try:
        frontend.async_remove_panel(hass, url_path)
    except Exception:
        pass
    try:
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name=webcomponent,
            frontend_url_path=url_path,
            sidebar_title=title,
            sidebar_icon=icon,
            module_url=module_url,
            embed_iframe=False,
            require_admin=False,
            config=config,
        )
        _LOGGER.info("Nova panel registered: /%s", url_path)
        return True
    except ValueError:
        _LOGGER.debug("Nova panel /%s already registered (idempotent)", url_path)
        return True
    except Exception as exc:
        _LOGGER.error("Nova panel registration failed (/%s): %s", url_path, exc)
        return False


async def async_register_panel(hass: HomeAssistant) -> bool:
    """
    Register the Nova sidebar panel.

    Returns True on success, False on error.
    Uses panel_custom.async_register_panel which handles idempotency
    internally (raises ValueError on duplicate, which we catch).
    """
    panel_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if not os.path.isdir(panel_dir):
        _LOGGER.error("Nova panel: frontend dir not found at %s", panel_dir)
        return False

    js_path = os.path.join(panel_dir, PANEL_JS_FILENAME)
    if not os.path.isfile(js_path):
        _LOGGER.error("Nova panel: JS file not found at %s", js_path)
        return False

    # Register static path for serving the frontend dir (both panels' JS live here)
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(PANEL_STATIC_URL, panel_dir, cache_headers=False)
        ])
    except Exception as exc:
        _LOGGER.debug("Nova panel: static path note: %s", exc)

    # Clean up the old separate Command Center panel from <=6.14.x — it's now
    # folded into the main Nova panel, so the standalone entry must go.
    try:
        frontend.async_remove_panel(hass, CMD_URL_PATH)
    except Exception:
        pass

    # New-look URL (v7.93.0): the shell registered below dynamically imports
    # this file only when a user has opted into ui_style='new', so it needs
    # its own cache-busted URL the same way the shell's own module_url gets
    # one — passed via panel_custom's `config`, which HA exposes to the
    # element as `panel.config`. Soft: the new-look file may not exist yet
    # on an older/partial checkout, so this degrades to None rather than
    # failing panel registration entirely.
    new_look_config: dict = {}
    new_look_path = os.path.join(panel_dir, NEW_LOOK_JS_FILENAME)
    if os.path.isfile(new_look_path):
        new_look_hash = await hass.async_add_executor_job(_hash_file, new_look_path)
        new_look_config["new_look_url"] = (
            f"{PANEL_STATIC_URL}/{NEW_LOOK_JS_FILENAME}?v={new_look_hash}"
        )
    else:
        _LOGGER.debug("Nova panel: new-look file not found, ui_style='new' will fail closed to Classic")

    # Single combined panel: the Nova Command Center (dashboard + cameras +
    # 3D residence + settings + logs all in one).
    main_ok = await _register_one(
        hass, panel_dir,
        webcomponent=PANEL_WEBCOMPONENT, url_path=PANEL_URL_PATH,
        title=PANEL_TITLE, icon=PANEL_ICON, js_filename=PANEL_JS_FILENAME,
        config=new_look_config,
    )
    return main_ok


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Unregister the panel on entry unload. Best-effort, errors non-fatal."""
    try:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
        _LOGGER.info("Nova panel unregistered")
    except Exception as exc:
        _LOGGER.debug("Nova panel /%s unregister note: %s", PANEL_URL_PATH, exc)
