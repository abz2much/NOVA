"""output_language through the real, admin gated nova/update_config command
and the panel data result, against a real Home Assistant instance."""
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry


async def _setup_nova(hass) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _write(client, value):
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "output_language", "value": value})
    return await client.receive_json()


async def test_valid_codes_are_accepted_and_resolve(hass, hass_ws_client):
    from custom_components.nova import output_language
    await _setup_nova(hass)
    hass.config.language = "en"
    client = await hass_ws_client(hass)
    for code, expected in (("de", "de"), ("de-DE", "de-DE"), ("fr", "fr")):
        resp = await _write(client, code)
        assert resp["success"] is True, resp
        assert output_language.resolve(hass) == expected
        assert "German" in output_language.directive(hass) if code.startswith("de") else True


async def test_bad_values_are_refused_and_nothing_is_stored(hass, hass_ws_client):
    from custom_components.nova import output_language
    await _setup_nova(hass)
    hass.config.language = "en"
    client = await hass_ws_client(hass)
    assert (await _write(client, "de"))["success"] is True
    for bad in (5, True, "xx", "klingon", "de; rm -rf", None):
        resp = await _write(client, bad)
        assert resp["success"] is False, bad
        assert resp["error"]["code"] in ("invalid_value", "invalid_format"), (bad, resp)
    assert output_language.resolve(hass) == "de"        # the good value is untouched


async def test_empty_and_auto_clear_the_setting(hass, hass_ws_client):
    from custom_components.nova import output_language
    await _setup_nova(hass)
    hass.config.language = "fr"
    client = await hass_ws_client(hass)
    for clear in ("", "auto"):
        assert (await _write(client, "de"))["success"] is True
        assert output_language.resolve(hass) == "de"
        assert (await _write(client, clear))["success"] is True
        assert output_language.resolve(hass) == "fr"    # back to Home Assistant


async def test_write_requires_admin(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    assert (await _write(client, "de"))["success"] is False


async def test_panel_data_surfaces_the_setting(hass, hass_ws_client):
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    assert (await _write(client, "es"))["success"] is True
    await client.send_json_auto_id({"type": "nova/get_panel_data"})
    resp = await client.receive_json()
    assert resp["success"] is True
    assert resp["result"]["config"]["output_language"] == "es"
    assert resp["result"]["config"]["ui_language"] == "auto"      # a different setting, untouched
