"""tts_helper reads the languages of a real TTS entity, which the unit tests
can only fake."""
from homeassistant.components.tts import DATA_COMPONENT, TextToSpeechEntity
from homeassistant.setup import async_setup_component

from custom_components.nova import tts_helper


class _FrenchVoice(TextToSpeechEntity):
    _attr_name = "Test Voice"
    _attr_supported_languages = ["fr-FR", "en-US"]
    _attr_default_language = "en-US"


async def test_the_languages_of_a_real_tts_entity_are_read(hass):
    assert await async_setup_component(hass, "tts", {})
    await hass.data[DATA_COMPONENT].async_add_entities([_FrenchVoice()])
    await hass.async_block_till_done()
    assert tts_helper._engine_languages(hass, "tts.test_voice") == ["fr-FR", "en-US"]
    assert tts_helper._pick_language("fr", tts_helper._engine_languages(hass, "tts.test_voice")) == "fr-FR"
    assert tts_helper._engine_languages(hass, "tts.nothing_here") is None
