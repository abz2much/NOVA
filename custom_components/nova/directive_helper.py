"""Builds the system prompt for every Nova LLM call.

Any module that sends a system prompt builds it here, so the prime directive
always comes ahead of the persona and of the task's own instructions. This
runs on each LLM call, not only on conversation turns, which is what keeps the
directive in force everywhere.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIRECTIVE, CONF_DIRECTIVE_PRESET, DEFAULT_DIRECTIVE_PRESET,
    DOMAIN, NOVA_PERSONA, get_directive,
)

_SECTION_BREAK = "\n\n---\n\n"

# The one persona line that tells Nova how to address someone. With nobody
# specific at home it is swapped for the second text rather than blanked.
_ADDRESS_LINE = "Address your owner as {honorific} — naturally, never robotically"
_ADDRESS_LINE_NO_ONE = (
    "Address the room in general — no honorific, since no one specific "
    "is home to address right now"
)


def resolve_directive(
    entry: ConfigEntry | None, hass: HomeAssistant | None = None,
) -> str:
    """The directive text for this config entry.

    When hass is given, the entry's live runtime_config is read first, so
    panel changes apply without a reload, as for every nova_config.runtime_get
    caller. Without hass the order is config.json, then options, then data."""
    if entry is None:
        return get_directive(DEFAULT_DIRECTIVE_PRESET, "")
    from . import nova_config
    preset = nova_config.runtime_get(hass, entry, CONF_DIRECTIVE_PRESET,
                                       DEFAULT_DIRECTIVE_PRESET)
    custom_text = nova_config.runtime_get(hass, entry, CONF_DIRECTIVE, "")
    return get_directive(preset, custom_text)


def get_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The Nova config entry (there is only one), or None if it cannot be found."""
    try:
        found = hass.config_entries.async_entries(DOMAIN)
        return found[0] if found else None
    except Exception:
        return None


def fill_honorific(text: str, honorific: str) -> str:
    """Put the honorific into every {honorific} placeholder of persona text.

    With an empty honorific (nobody specific is home, see honorific.py) a
    plain blank would leave the persona saying "Address your owner as  —
    naturally" and "Right away, .". So the one line that tells Nova how to
    address people is rewritten without an honorific, and every other
    placeholder, always the trailing ", {honorific}", is removed whole."""
    if honorific:
        return text.replace("{honorific}", honorific)
    text = text.replace(_ADDRESS_LINE, _ADDRESS_LINE_NO_ONE)
    text = text.replace(", {honorific}", "")
    return text.replace("{honorific}", "")


def build_system_prompt(
    hass: HomeAssistant,
    honorific: str,
    task_context: str = "",
) -> str:
    """The full system prompt for one task, in this order: the PRIME DIRECTIVE
    (always first, always there), the Nova persona, then the task's own
    instructions (camera prompt, briefing instructions, sentinel prompt and so
    on). The honorific goes into every {honorific} placeholder; see
    fill_honorific()."""
    entry = get_entry(hass)
    parts = [resolve_directive(entry, hass), NOVA_PERSONA]
    if task_context:
        parts.append(task_context.strip())
    return fill_honorific(_SECTION_BREAK.join(parts), honorific)
