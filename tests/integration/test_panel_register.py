"""Spec acceptance check for panel_register against a real Home Assistant.

Proves the sidebar panel lands in the frontend panel registry with the
expected custom-panel config, that the static directory is served, and that a
second registration (an entry reload) neither raises nor duplicates.
"""
import logging

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.setup import async_setup_component


async def test_panel_and_static_path_register_and_reload_is_safe(hass, caplog):
    assert await async_setup_component(hass, "http", {})
    caplog.set_level(logging.WARNING, logger="homeassistant.components.frontend")
    from custom_components.nova import panel_register as pr

    assert await pr.async_register_panel(hass) is True
    panels = hass.data[DATA_PANELS]
    assert "nova" in panels and "nova-command" not in panels
    panel = panels["nova"]
    assert panel.component_name == "custom"
    assert panel.sidebar_title == "Nova"
    assert panel.sidebar_icon == "mdi:robot-outline"
    assert panel.require_admin is False
    custom = panel.config["_panel_custom"]
    assert custom["name"] == "nova-panel"
    assert custom["embed_iframe"] is False and custom["trust_external"] is False
    assert set(custom) == {"name", "embed_iframe", "trust_external", "module_url"}
    assert custom["module_url"].startswith("/nova_panel_static/nova-panel.js?v=")
    assert set(panel.config) == {"_panel_custom"}

    # 8.49.10: removing a panel that is not there yet logs no warning.
    warned = [r.getMessage() for r in caplog.records
              if r.name == "homeassistant.components.frontend"]
    assert not any("Removing unknown panel" in m for m in warned), warned

    # The static directory is routed under the panel's static URL.
    def static_routes():
        return sum(1 for r in hass.http.app.router.routes()
                   if getattr(r.resource, "canonical", "").startswith("/nova_panel_static"))
    first_routes = static_routes()
    assert first_routes > 0

    # Reload: a second call succeeds, still one "nova" panel, same module URL.
    first_url = custom["module_url"]
    assert await pr.async_register_panel(hass) is True
    panels = hass.data[DATA_PANELS]
    assert list(panels).count("nova") == 1
    assert panels["nova"].config["_panel_custom"]["module_url"] == first_url

    # Unload removes the sidebar entry; it can be registered again afterwards.
    pr.async_unregister_panel(hass)
    assert "nova" not in hass.data[DATA_PANELS]
    assert await pr.async_register_panel(hass) is True
    assert "nova" in hass.data[DATA_PANELS]

    # 8.49.10: reloads reuse the one static route instead of adding more,
    # and log no "unknown panel" warning either.
    for _ in range(3):
        pr.async_unregister_panel(hass)
        assert await pr.async_register_panel(hass) is True
    assert static_routes() == first_routes
    warned = [r.getMessage() for r in caplog.records
              if r.name == "homeassistant.components.frontend"]
    assert not any("Removing unknown panel" in m for m in warned), warned
