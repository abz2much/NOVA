"""The language Nova speaks and writes in.

One place decides it, so the agent prompts and the safety notifications can
never disagree. Order:

  1. a per request language, when the caller has one and it is a language
     Nova knows
  2. Nova's own ``output_language`` setting ("" or "auto" means unset)
  3. Home Assistant's own language
  4. "en"

Nothing here ever raises: any failure falls through to the next step. This is
a leaf module (no imports of other Nova modules at import time), so both
``agent_runtime`` and ``cognitive_core`` can use it without a cycle.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# The one table of language names. A code is accepted for the setting only if
# its base language is here.
LANG_NAMES = {
    "en": "English",
    "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pt": "Portuguese", "pl": "Polish", "sv": "Swedish",
    "nb": "Norwegian", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "cs": "Czech", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
    "he": "Hebrew", "el": "Greek", "hu": "Hungarian", "ro": "Romanian",
    "sk": "Slovak", "ca": "Catalan", "id": "Indonesian", "th": "Thai",
    "vi": "Vietnamese",
}

AUTO = "auto"
_SHAPE = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z0-9]{2,4}))?$")


def parse(value: Any) -> Optional[str]:
    """A canonical language code ("de", "de-DE") for a string Nova knows, or
    None for anything else, including "", "auto", non strings and codes whose
    base language is not in LANG_NAMES."""
    if type(value) is not str:
        return None
    m = _SHAPE.match(value.strip())
    if not m:
        return None
    base = m.group(1).lower()
    if base not in LANG_NAMES:
        return None
    region = m.group(2)
    if not region:
        return base
    if len(region) == 2 and region.isalpha():
        return f"{base}-{region.upper()}"
    if len(region) == 4 and region.isalpha():
        return f"{base}-{region.title()}"
    return f"{base}-{region}"


def is_valid_setting(value: Any) -> bool:
    """Whether a panel write of ``output_language`` is acceptable: the literal
    string "" or "auto" (follow Home Assistant), or a known language code."""
    if type(value) is not str:
        return False
    return value.strip().lower() in ("", AUTO) or parse(value) is not None


def base(lang: Optional[str]) -> str:
    """The lower case base language of a code ("de-DE" -> "de")."""
    return (lang or "en").replace("_", "-").split("-")[0].lower() or "en"


# Regions that change how a language is written. Any other region, and a
# plain code, keeps the base name ("pt" is European Portuguese, as before).
_REGIONAL = {
    ("pt", "BR"): "Brazilian Portuguese",
    ("fr", "CA"): "Canadian French",
    ("es", "419"): "Latin American Spanish",
    ("de", "CH"): "Swiss German (use ss, not ß)",
}

# Chinese is named by script, so the model writes the one the household reads.
# A plain "zh" is Simplified, as it always has been.
_TRADITIONAL_ZH = {"hant", "tw", "hk", "mo"}


def name(lang: Optional[str]) -> str:
    """A language's English name; an unknown code is returned as it is."""
    b = base(lang)
    if b == "zh":
        tags = set((lang or "").replace("_", "-").lower().split("-")[1:])
        if tags & _TRADITIONAL_ZH:
            if tags & {"hk", "mo"}:
                return "Traditional Chinese"
            return "Traditional Chinese (Taiwan wording)"
        return "Simplified Chinese"
    region = (lang or "").replace("_", "-").split("-")[1:2]
    regional = _REGIONAL.get((b, region[0].upper() if region else ""))
    if regional:
        return regional
    return LANG_NAMES.get(b, b)


def _setting(hass) -> Any:
    try:
        from . import nova_config
        return nova_config.output_language(hass)
    except Exception:
        return ""


def resolve(hass, request_language: Any = None) -> str:
    """The effective language code. Never raises."""
    try:
        got = parse(request_language)
        if got:
            return got
        got = parse(_setting(hass))
        if got:
            return got
        ha = getattr(getattr(hass, "config", None), "language", None)
        if type(ha) is str and ha.strip():
            return ha.strip()
    except Exception:
        pass
    return "en"


def directive(hass, *, conversational: bool = True, request_language: Any = None) -> str:
    """A system prompt block that steers generated text to the effective
    language. Empty for English, so English installs are unaffected.

    ``conversational`` blocks keep the rule that a user who writes in another
    language is answered in it. Text Nova writes unprompted (briefings, alerts)
    uses the plain form."""
    lang = resolve(hass, request_language)
    b = base(lang)
    if b == "en":
        return ""
    lname = name(lang)
    if conversational:
        return (
            f"## Language\n"
            f"Respond in {lname} by default — this household's configured language "
            f"is {lname}. If the user writes to you in another language, reply in "
            f"that language instead. Keep entity names and proper nouns unchanged.\n\n"
        )
    return (
        f"## Language\n"
        f"Write this in {lname}. Keep entity names and proper nouns unchanged.\n\n"
    )


def with_language(hass, system: str) -> str:
    """``system`` with the plain language block appended, or unchanged for
    English. For prompts whose output a person reads or hears."""
    block = directive(hass, conversational=False)
    return f"{system.rstrip()}\n\n{block.rstrip()}\n" if block else system


def field_directive(hass, field: str) -> str:
    """For a JSON reply where one string field is read or spoken to a person:
    only that field follows the language, and the JSON itself (keys, enum
    values, structure) stays exactly as the prompt specifies. Empty for
    English."""
    lang = resolve(hass)
    if base(lang) == "en":
        return ""
    return (
        f"\n\nLanguage: write the value of \"{field}\" in {name(lang)}. Keep every "
        f"JSON key, every other value and the JSON structure exactly as specified "
        f"above, in English. Keep entity names and proper nouns unchanged."
    )
