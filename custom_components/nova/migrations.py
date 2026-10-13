"""Bring a Nova config entry's saved settings up to the current schema.

Each step advances ``options`` by exactly one schema version, only ever adding
keys that are missing (using the value an older install effectively had).
Steps never touch ``data`` beyond checking whether a key is present there, and
never rename, remove or rewrite existing keys (one documented exception in
step 6->7). The caller stores the version number; this module only receives
and returns it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 9

# Settings that later releases ship switched off until set up; installs that
# predate that change keep the value they effectively had.
OFF_UNTIL_SET_UP_8_41: dict[str, object] = {
    "package_detection": True,
    "recognition_source": "both",
    "departure_alerts_enabled": True,
    "search_backend": "duckduckgo",
    "auto_bootstrap": True,
}


def _add_missing(options: dict, defaults: dict[str, Any]) -> None:
    """Insert each default whose key is not present in ``options`` at all."""
    for key, value in defaults.items():
        if key not in options:
            options[key] = value


def _saved_setting(key: str) -> Any:
    """Value saved in Nova's config.json, or None when unavailable."""
    try:
        from . import nova_config

        return nova_config.get(key, None)
    except Exception:  # noqa: BLE001 - an unreadable store means "not saved"
        return None


def migrate_1_to_2(data: dict, options: dict) -> tuple[dict, dict]:
    """Premium TTS settings."""
    _add_missing(options, {
        "tts_premium_engine": "",
        "tts_premium_contexts": ["briefing", "camera", "doorbell", "recognition"],
    })
    _LOGGER.info("Nova migration v1->v2: added premium TTS settings")
    return data, options


def migrate_2_to_3(data: dict, options: dict) -> tuple[dict, dict]:
    """Directive settings."""
    _add_missing(options, {"directive_preset": "guardian_steward", "directive": ""})
    _LOGGER.info("Nova migration v2->v3: added directive settings")
    return data, options


def migrate_3_to_4(data: dict, options: dict) -> tuple[dict, dict]:
    """Multi-provider LLM settings."""
    _add_missing(options, {"llm_provider": "groq", "llm_base_url": ""})
    _LOGGER.info("Nova migration v3->v4: added LLM provider settings")
    return data, options


def migrate_4_to_5(data: dict, options: dict) -> tuple[dict, dict]:
    """Three-tier audio, seeded from the legacy ``cast_speakers`` list."""
    cast_speakers = options.get("cast_speakers", [])
    _add_missing(options, {"voice_satellites": [], "reply_speakers": []})
    if cast_speakers and "broadcast_speakers" not in options:
        options["broadcast_speakers"] = list(cast_speakers)
    _add_missing(options, {"broadcast_speakers": [], "room_routing": True})
    # len() runs after the writes on purpose: a length-less value fails the
    # step here, leaving the keys above in place.
    _LOGGER.info(
        "Nova migration v4->v5: three-tier audio (%d legacy cast speakers)",
        len(cast_speakers),
    )
    return data, options


def migrate_5_to_6(data: dict, options: dict) -> tuple[dict, dict]:
    """Observer mode settings."""
    _add_missing(options, {
        "observer_enabled": False,
        "gemini_api_key": "",
        "classifier_provider": "gemini",
        "classifier_model": "gemini-2.5-flash-lite",
        "reasoning_provider": "gemini",
        "reasoning_model": "gemini-2.5-flash",
        "review_provider": "gemini",
        "review_model": "gemini-2.5-pro",
        "observer_presence_entities": [],
        "observer_sleep_entities": [],
        "observer_ignore": [],
        "observer_categories": ["appliances", "doors_windows", "presence", "security"],
        "observer_quiet_start": "22:00",
        "observer_quiet_end": "07:00",
        "notify_service": "",
    })
    _LOGGER.info("Nova migration v5->v6: added observer settings")
    return data, options


def migrate_6_to_7(data: dict, options: dict) -> tuple[dict, dict]:
    """Area-registry routing; legacy speaker lists are kept as they are."""
    broadcast = options.get("broadcast_speakers") or []
    if broadcast and not options.get("broadcast_group"):
        options["broadcast_group"] = broadcast[0]
        _LOGGER.info(
            "Nova migration v6->v7: broadcast_group set to %s",
            options["broadcast_group"],
        )
    _add_missing(options, {"bedroom_areas": []})
    _LOGGER.info("Nova migration v6->v7: area-registry routing")
    return data, options


def migrate_7_to_8(data: dict, options: dict) -> tuple[dict, dict]:
    """Keep the historically exempt locks for installs that never chose."""
    from .core_common import LEGACY_LOCKDOWN_EXEMPT_LOCKS  # unguarded on purpose

    key = "lockdown_exempt_locks"
    if _saved_setting(key) is None and key not in options and key not in data:
        options[key] = list(LEGACY_LOCKDOWN_EXEMPT_LOCKS)
        _LOGGER.info("Nova migration v7->v8: kept legacy lockdown exempt locks")
    return data, options


def migrate_8_to_9(data: dict, options: dict) -> tuple[dict, dict]:
    """Pin the old defaults of settings that are now off until set up."""
    written = []
    for key, old_default in OFF_UNTIL_SET_UP_8_41.items():
        if _saved_setting(key) is None and key not in options and key not in data:
            options[key] = old_default
            written.append(key)
    if written:
        _LOGGER.info("Nova migration v8->v9: kept previous defaults for %s",
                     ", ".join(written))
    return data, options


MIGRATIONS: list[tuple[int, Callable[[dict, dict], tuple[dict, dict]]]] = [
    (1, migrate_1_to_2), (2, migrate_2_to_3), (3, migrate_3_to_4),
    (4, migrate_4_to_5), (5, migrate_5_to_6), (6, migrate_6_to_7),
    (7, migrate_7_to_8), (8, migrate_8_to_9),
]


def migrate_config(
    data: dict, options: dict, current_version: int = 1
) -> tuple[dict, dict, int]:
    """Run every step from ``current_version`` up; return the version reached.

    A failing step is logged and stops the walk; whatever was written so far
    stays, and the returned version is that step's source version.
    """
    if current_version >= CURRENT_SCHEMA_VERSION:
        return data, options, current_version

    _LOGGER.info("Nova migrating config from v%s to v%s",
                 current_version, CURRENT_SCHEMA_VERSION)
    for source, step in MIGRATIONS:
        if source != current_version:
            continue
        try:
            data, options = step(data, options)
        except Exception as err:  # noqa: BLE001 - report and stop, never raise
            _LOGGER.error("Nova migration v%s→v%s failed: %s",
                          source, source + 1, err)
            break
        current_version = source + 1
    return data, options, current_version
