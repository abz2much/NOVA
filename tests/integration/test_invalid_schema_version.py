"""8.49.10: an invalid saved schema version is reported once and not saved back.
8.49.12: the same for a version that is missing (None) or text."""
import logging

import pytest

from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry

DOMAIN = "nova"


async def test_invalid_schema_version_is_reported_and_not_saved(hass, caplog):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": 0},
        options={"cast_speakers": ["media_player.one"]},
    )
    entry.add_to_hass(hass)
    updates = []
    real_update = hass.config_entries.async_update_entry

    def spy(e, **kw):
        if e is entry:
            updates.append(kw)
        return real_update(e, **kw)
    hass.config_entries.async_update_entry = spy
    caplog.set_level(logging.INFO, logger="custom_components.nova")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data["schema_version"] == 0
    assert dict(entry.options) == {"cast_speakers": ["media_player.one"]}
    assert not any("schema_version" in u.get("data", {}) for u in updates)
    errors = [r.getMessage() for r in caplog.records
              if r.name == "custom_components.nova.migrations" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "invalid" in errors[0]
    assert not any("migrated config from schema" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("version", [None, "7"])
async def test_none_or_text_schema_version_does_not_fail_setup(hass, caplog, version):
    # 8.49.11 raised TypeError comparing the version, so setup failed.
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"llm_provider": "ollama", "llm_base_url": "http://localhost:11434/v1",
              "model": "llama3", "schema_version": version},
        options={"cast_speakers": ["media_player.one"]},
    )
    entry.add_to_hass(hass)
    caplog.set_level(logging.INFO, logger="custom_components.nova")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data["schema_version"] == version
    assert dict(entry.options) == {"cast_speakers": ["media_player.one"]}
    errors = [r.getMessage() for r in caplog.records
              if r.name == "custom_components.nova.migrations" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "invalid" in errors[0]
