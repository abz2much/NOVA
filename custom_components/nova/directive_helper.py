"""
Nova — Prime directive injection helper.

Used by every module that builds an LLM system prompt to ensure the
unrelenting directive is prepended BEFORE the persona or task-specific
instructions.

This is the mechanism that makes the directive truly unrelenting: it runs
at every LLM call, not just conversation turns.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIRECTIVE,
    CONF_DIRECTIVE_PRESET,
    DEFAULT_DIRECTIVE_PRESET,
    DOMAIN,
    NOVA_PERSONA,
    get_directive,
)


def resolve_directive(entry: ConfigEntry | None) -> str:
    """Get the directive text based on this config entry."""
    if entry is None:
        return get_directive(DEFAULT_DIRECTIVE_PRESET, "")
    from . import nova_config
    preset = nova_config.runtime_get(None, entry, CONF_DIRECTIVE_PRESET,
                                       DEFAULT_DIRECTIVE_PRESET)
    custom = nova_config.runtime_get(None, entry, CONF_DIRECTIVE, "")
    return get_directive(preset, custom)


def get_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Best-effort lookup of the Nova config entry (singleton)."""
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
        return entries[0] if entries else None
    except Exception:
        return None


def fill_honorific(text: str, honorific: str) -> str:
    """Substitute every {honorific} placeholder in NOVA_PERSONA-derived text.

    When honorific is "" (nobody specific home to address — see
    honorific.py), a blind blank substitution would leave the persona
    instructions reading "Address your owner as  — naturally" and
    "Right away, ." — so the one NOVA_PERSONA line that directly instructs
    on addressing gets an honorific-free rewrite, and every other
    "{honorific}" instance (all of them trailing, ", {honorific}") is
    dropped cleanly instead of left blank.
    """
    if honorific:
        return text.replace("{honorific}", honorific)
    text = text.replace(
        "Address your owner as {honorific} — naturally, never robotically",
        "Address the room in general — no honorific, since no one specific "
        "is home to address right now",
    )
    text = text.replace(", {honorific}", "")
    return text.replace("{honorific}", "")


def build_system_prompt(
    hass: HomeAssistant,
    honorific: str,
    task_context: str = "",
) -> str:
    """
    Build a full system prompt for a task. Structure:
      1. PRIME DIRECTIVE (always first, always present)
      2. Nova persona (character + style)
      3. Task-specific context/instructions

    honorific is substituted into all {honorific} placeholders — see
    fill_honorific().
    task_context is the additional task-specific instruction (camera prompt,
    briefing instructions, sentinel prompt, etc.).
    """
    entry = get_entry(hass)
    directive = resolve_directive(entry)
    persona = NOVA_PERSONA

    combined = f"{directive}\n\n---\n\n{persona}"
    if task_context:
        combined = f"{combined}\n\n---\n\n{task_context.strip()}"

    return fill_honorific(combined, honorific)
