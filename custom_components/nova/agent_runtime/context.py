"""System prompt context: home state, language directive, register and the
413 home-state strip."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .capabilities import memory as _memory

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# ── Tool dispatcher ─────────────────────────────────────────────────────────

def _banter_guidance() -> str:
    """Extra prompt line tuning wit intensity to the banter_level config knob
    (0 plain · 1 dry · 2 full). Empty at the tasteful default so we don't
    bloat the prompt unless the user dialed character up or down."""
    try:
        from .. import nova_config
        level = int(nova_config.get("banter_level", 1) or 1)
    except Exception:
        level = 1
    if level <= 0:
        return ("\n\nRegister: keep it strictly plain and functional. No wit, "
                "no asides — just crisp, correct confirmations.")
    if level >= 2:
        return ("\n\nRegister: lean into the character. A dry, clever aside is "
                "welcome when the moment is light — one line, never forced, "
                "always still useful — but hold to the rule that gravity "
                "silences it entirely.")
    return ""  # level 1: the character description above already nails it


# ── Home context builder ────────────────────────────────────────────────────

def _build_home_context(hass: HomeAssistant) -> str:
    """
    Build a compact home context string for the system prompt.
    Gives the LLM awareness of what's available to control.

    The per-domain entity-name count is capped by the `home_context_max_entities`
    config (default 15). Set it to 0 for counts only — a big prompt-size reduction
    for providers with tight token-per-minute limits (e.g. Groq's free tier). The
    LLM can still discover entities on demand via search_entities. (v7.23.1)
    """
    from .. import nova_config
    try:
        max_ent = int(nova_config.get("home_context_max_entities", 15))
    except Exception:
        max_ent = 15
    parts = []

    # Areas
    try:
        from homeassistant.helpers import area_registry as areg
        area_reg = areg.async_get(hass)
        areas = [a.name for a in area_reg.async_list_areas()]
        if areas:
            parts.append(f"Areas: {', '.join(areas)}")
    except Exception:
        pass

    # Key entity counts by domain
    for domain, label in [
        ("light", "Lights"), ("switch", "Switches"), ("lock", "Locks"),
        ("cover", "Covers"), ("climate", "Thermostats"), ("fan", "Fans"),
        ("media_player", "Media players"), ("person", "People"),
        ("scene", "Scenes"), ("script", "Scripts"),
    ]:
        entities = list(hass.states.async_all(domain))
        if entities:
            if max_ent <= 0:
                parts.append(f"{label}: {len(entities)}")   # counts only — smallest prompt
            else:
                names = [
                    s.attributes.get("friendly_name", s.entity_id)
                    for s in entities[:max_ent]
                ]
                suffix = f" (+{len(entities) - max_ent} more)" if len(entities) > max_ent else ""
                parts.append(f"{label} ({len(entities)}): {', '.join(names)}{suffix}")

    # Learned aliases
    learned = _memory._load_learned()
    aliases = learned.get("alias", {})
    if aliases:
        alias_str = "; ".join(f"'{k}' = {v}" for k, v in list(aliases.items())[:20])
        parts.append(f"Learned aliases: {alias_str}")

    # Preferences used to be dumped here too, raw and unfenced, straight from
    # _LEARN_FILE. Removed (v7.88.0): redundant with (and less safe than)
    # knowledge.py's prompt_block(), which conversation.py already injects
    # separately — confirmed-only, fenced against prompt injection, and
    # scoped to the actual person asking rather than every preference ever
    # stated. _exec_remember no longer writes preferences here at all.

    return "\n".join(parts)


def _strip_home_state(system_text: str) -> str:
    """Replace the '## Current home state' block with a short pointer, to shrink
    the request for a 413 retry. Leaves the rest of the prompt intact."""
    marker = "## Current home state\n"
    i = system_text.find(marker)
    if i == -1:
        return system_text
    j = system_text.find("\n## ", i + len(marker))
    tail = system_text[j + 1:] if j != -1 else ""   # from the next "## " header
    note = ("## Current home state\n"
            "(omitted to fit the provider's request limit — call search_entities "
            "for anything you need)\n\n")
    return system_text[:i] + note + tail


_LANG_NAMES = {
    "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pt": "Portuguese", "pl": "Polish", "sv": "Swedish",
    "nb": "Norwegian", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "cs": "Czech", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
    "he": "Hebrew", "el": "Greek", "hu": "Hungarian", "ro": "Romanian",
    "sk": "Slovak", "ca": "Catalan", "id": "Indonesian", "th": "Thai",
    "vi": "Vietnamese",
}


def _language_directive(hass) -> str:
    """A system-prompt block steering replies to the home's configured language.

    Uses Home Assistant's ``language`` so a non-English household gets replies in
    its own language. Returns ``""`` for English installs (which are therefore
    completely unaffected). The user's own input language still wins if they
    write in something else.
    """
    try:
        lang = (getattr(hass.config, "language", None) or "en").split("-")[0].lower()
    except Exception:
        return ""
    if not lang or lang == "en":
        return ""
    lname = _LANG_NAMES.get(lang, lang)
    return (
        f"## Language\n"
        f"Respond in {lname} by default — this household's configured language "
        f"is {lname}. If the user writes to you in another language, reply in "
        f"that language instead. Keep entity names and proper nouns unchanged.\n\n"
    )
