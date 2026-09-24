"""
Nova — Centralized Configuration.

Single source of truth for all Nova settings.

Config file: /config/nova/config.json

Lifecycle (v6.45.0 — config-entry-only, no add-on):
  1. Integration loads → reads config.json
  2. Panel / Configure-dialog changes → written to config.json + in-memory cache
  3. Restart → config.json persists, in-memory reloads from it

All modules should import and use `get()` and `set()` from this module
instead of reading from entry.options or hass.data directly.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/config/nova/config.json")
_lock = threading.Lock()
_cache: dict = {}
_loaded = False
# v6.48.0 hardening: set when a hand-edited config.json couldn't be used
# (invalid JSON, or valid JSON whose top level isn't an object). The bad
# file is sidelined — never deleted — and defaults take over, so a typo in
# the file can no longer kill integration setup (which killed the panel).
last_load_error: Optional[str] = None


def configure(hass) -> None:
    """Point CONFIG_PATH at this Home Assistant instance's own config dir.

    hass.config.path() resolves to whatever directory THIS instance was
    configured with — /config on HA OS/Supervised/container installs, but
    not universally (a Core install run out of a venv can point anywhere).
    Before this, nova_config always wrote through the literal `/config`
    regardless of what hass actually reported, which made setup unrunnable
    anywhere that isn't the real config dir — including every test harness
    (PHACC), whose hass fixture points elsewhere. On an install where
    hass.config.path() genuinely is /config, this resolves to the same path
    CONFIG_PATH already had, so behaviour there is unchanged.
    Call once, early in async_setup_entry, before any config.json access."""
    global CONFIG_PATH, _cache, _loaded
    CONFIG_PATH = Path(hass.config.path("nova", "config.json"))
    # Integration reloads reuse this Python module. Invalidate the process
    # cache every time setup begins so a file restored or edited on disk is
    # visible after a Nova reload, without requiring a full HA restart.
    with _lock:
        _cache = {}
        _loaded = False


def reload() -> dict:
    """Discard the in-memory snapshot and load config.json again."""
    global _cache, _loaded
    with _lock:
        _cache = {}
        _loaded = False
    return load()


def _ensure_dir():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _sideline_corrupt(reason: str) -> None:
    """Move the unusable config aside (preserving the user's edits for
    recovery) and record why. Best-effort — failure to move must not stop
    startup either."""
    global last_load_error
    import time as _t
    dest = CONFIG_PATH.with_name(
        CONFIG_PATH.name + f".corrupt-{int(_t.time())}")
    try:
        CONFIG_PATH.rename(dest)
        last_load_error = f"{reason} — file preserved at {dest.name}"
    except Exception:
        last_load_error = f"{reason} — could not sideline file"
    _LOGGER.error(
        "Nova config.json unusable (%s). Starting with defaults; "
        "your file was kept for recovery. Fix the JSON and settings return.",
        last_load_error,
    )


def load() -> dict:
    """Load config from disk into cache. A file that can't be parsed, or
    parses to something that isn't a JSON object, is sidelined instead of
    crashing setup (v6.48.0 — a hand-edited config took the panel down)."""
    global _cache, _loaded, last_load_error
    _ensure_dir()
    with _lock:
        last_load_error = None
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cache = data
                else:
                    _cache = {}
                    _sideline_corrupt(
                        f"top level is {type(data).__name__}, expected object")
            else:
                _cache = {}
            _loaded = True
            _LOGGER.info(
                "Nova config loaded: %d keys from %s",
                len(_cache), CONFIG_PATH,
            )
        except json.JSONDecodeError as exc:
            _cache = {}
            _loaded = True
            _sideline_corrupt(f"invalid JSON: {exc}")
        except Exception as exc:
            _LOGGER.warning("Nova config load error: %s", exc)
            _cache = {}
            _loaded = True
    return dict(_cache)


def _cache_dict() -> dict:
    """Belt-and-braces: the cache is ALWAYS a dict at point of use, even if
    something replaced it at runtime."""
    global _cache
    if not isinstance(_cache, dict):
        _LOGGER.error("Nova config cache was %s — resetting to {}",
                      type(_cache).__name__)
        _cache = {}
    return _cache


def save() -> bool:
    """Persist current cache to disk. Returns True on success, False on
    failure (e.g. disk full, permission error) — the in-memory cache still
    has the new value either way, so callers keep working for the rest of
    this session, but a caller that cares whether the change survives a
    restart (e.g. the panel) should check this and tell the user."""
    _ensure_dir()
    with _lock:
        try:
            # Write atomically via temp file
            tmp = CONFIG_PATH.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(_cache, f, indent=2, default=str)
            tmp.replace(CONFIG_PATH)
            return True
        except Exception as exc:
            _LOGGER.warning("Nova config save error: %s", exc)
            return False


def get(key: str, default: Any = None) -> Any:
    """Read a config value. Loads from disk on first access."""
    global _loaded
    if not _loaded:
        load()
    with _lock:
        return _cache_dict().get(key, default)


def get_all() -> dict:
    """Return a copy of the entire config."""
    global _loaded
    if not _loaded:
        load()
    with _lock:
        return dict(_cache_dict())


def effective_config(entry=None) -> dict:
    """The single source of truth for runtime config (v6.82.0).

    Merges the HA config entry (data, then options) with the panel config
    (config.json), letting the panel win — but only where the panel sets a
    meaningful (non-empty) value, so a blank panel field can't wipe a real
    credential carried on the entry. Use this for ALL provider/model/key
    resolution (boot client, conversation, agent, observer) so no two code
    paths can disagree about which LLM to run.

    Blocking on first access (loads config.json); from async code call via the
    executor: await hass.async_add_executor_job(nova_config.effective_config, entry)
    Tolerates entry=None (returns just the panel config).
    """
    cfg = get_all()
    merged: dict = {}
    if entry is not None:
        merged.update(dict(getattr(entry, "data", None) or {}))
        merged.update(dict(getattr(entry, "options", None) or {}))
    endpoints_migrated = cfg.get("self_hosted_endpoints_migrated") is True
    for k, v in cfg.items():
        if (endpoints_migrated
                and k in ("ollama_base_url", "custom_base_url")):
            # After the staged AI setup has split the old shared endpoint,
            # an explicit blank means "cleared" and must beat an old value
            # still present in entry.data/options.
            merged[k] = v or ""
            continue
        if v is None or v == "":
            continue      # a blank panel value must not clobber the entry
        merged[k] = v
    # Credentials live in secrets.yaml and win over config/entry (v6.83.0).
    from . import ha_secrets
    return ha_secrets.overlay_credentials(merged)


def effective_config_with_runtime(entry=None, runtime_config: dict | None = None) -> dict:
    """:func:`effective_config` with live panel ``runtime_config`` overlaid.

    ``runtime_config`` (held in ``hass.data`` and written by the panel for
    no-reload changes) carries the freshest values; this returns the full merged
    view a subsystem should act on. Use it anywhere a subsystem is (re)started
    from the current config — a panel install has empty entry.data/options, so
    building config from the entry alone would drop every config.json setting.
    Blocking (loads config.json); from async code call via the executor.
    """
    cfg = effective_config(entry)
    if runtime_config:
        for k, v in runtime_config.items():
            if v is not None and v != "":
                cfg[k] = v
    return cfg


def runtime_get(hass, entry, key: str, default=None):
    """Fast, single-key config read matching :func:`effective_config` precedence,
    for hot paths that can't afford the blocking full merge on every read:
    runtime_config (panel-live) → config.json → entry.options → entry.data →
    default. In-memory after boot.

    Use this instead of reading ``entry.options``/``entry.data`` directly — on a
    panel-configured install those are empty, so a direct read silently returns
    the default for anything set via the panel or config.json (the recurring
    divergence class behind several past bugs). Never raises.
    """
    # runtime_config — the panel's live, no-reload values.
    try:
        from .const import DOMAIN
        if hass is not None and entry is not None:
            data = hass.data.get(DOMAIN, {}).get(getattr(entry, "entry_id", None), {})
            rc = data.get("runtime_config", {}) if isinstance(data, dict) else {}
            if key in rc and rc[key] not in (None, ""):
                return rc[key]
    except Exception:
        pass
    # config.json — the persisted panel config (wins over the entry, per
    # effective_config); cached in-memory after boot.
    try:
        v = get(key, None)
        if (key in ("ollama_base_url", "custom_base_url")
                and get("self_hosted_endpoints_migrated", False) is True
                and key in get_all()):
            return v or ""
        if v not in (None, ""):
            return v
    except Exception:
        pass
    # entry options/data — authoritative on a YAML install, empty on a panel one.
    if entry is not None:
        opts = getattr(entry, "options", None) or {}
        if key in opts:
            return opts[key]
        data_ = getattr(entry, "data", None) or {}
        if key in data_:
            return data_[key]
    return default


_credential_keys_cache: Optional[frozenset] = None


def _credential_keys() -> frozenset:
    """The set of config keys that hold LLM credentials (ha_secrets.CREDENTIAL_KEYS),
    cached after first lookup. set()/set_many() refuse to persist these as
    plaintext — credentials belong only in secrets.yaml (ha_secrets.py), never
    in config.json. Falls back to an empty set (nothing blocked) if ha_secrets
    can't be imported, rather than ever raising."""
    # NOTE: this module defines its own `set` function below (the public
    # config-write API), which shadows the builtin within this module's
    # namespace — frozenset(...) here, never bare set(...)/set().
    global _credential_keys_cache
    if _credential_keys_cache is None:
        try:
            from . import ha_secrets
            _credential_keys_cache = frozenset(ha_secrets.CREDENTIAL_KEYS)
        except Exception:
            _credential_keys_cache = frozenset()
    return _credential_keys_cache


def set(key: str, value: Any) -> bool:
    """Set a config value and persist to disk. Returns whether the save to
    disk succeeded (see save()) — the value is applied in memory regardless.

    A credential key (ha_secrets.CREDENTIAL_KEYS) with a real value is
    refused — those belong only in secrets.yaml, never here. Deleting one
    (value is empty/None) is still allowed, since that's how a caller clears
    a stale plaintext copy."""
    global _loaded
    if key in _credential_keys() and value:
        _LOGGER.warning(
            "Nova config: refused to store credential key '%s' in config.json "
            "— use ha_secrets.async_set_provider_credential instead", key)
        return False
    if not _loaded:
        load()
    with _lock:
        _cache_dict()[key] = value
    ok = save()
    _LOGGER.debug("Nova config set: %s = %s", key, str(value)[:100])
    return ok


def set_many(updates: dict) -> bool:
    """Set multiple config values and persist. Returns whether the save to
    disk succeeded (see save()) — values are applied in memory regardless.

    Any credential key (see set()) with a real value is silently dropped from
    `updates` before writing; every other key in the same call still saves."""
    global _loaded
    blocked = _credential_keys()
    filtered = {k: v for k, v in updates.items() if not (k in blocked and v)}
    if len(filtered) != len(updates):
        _LOGGER.warning(
            "Nova config: refused to store %d credential key(s) in config.json "
            "— use ha_secrets.async_set_provider_credential instead",
            len(updates) - len(filtered))
    if not _loaded:
        load()
    with _lock:
        _cache_dict().update(filtered)
    ok = save()
    _LOGGER.debug("Nova config set_many: %d keys", len(filtered))
    return ok


def set_many_atomic(updates: dict) -> bool:
    """Persist a group of non-credential settings as one transaction.

    The in-memory cache changes only after the temporary file has replaced the
    real config file. A failed write therefore leaves both disk and runtime on
    the previous configuration instead of applying a partial session-only
    update.
    """
    global _cache, _loaded
    blocked = _credential_keys()
    if any(key in blocked and value for key, value in updates.items()):
        _LOGGER.warning(
            "Nova config: refused atomic update containing a credential")
        return False
    if not _loaded:
        load()
    _ensure_dir()
    with _lock:
        candidate = dict(_cache_dict())
        candidate.update(updates)
        try:
            tmp = CONFIG_PATH.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(candidate, f, indent=2, default=str)
            tmp.replace(CONFIG_PATH)
        except Exception as exc:
            _LOGGER.warning("Nova atomic config save error: %s", exc)
            return False
        _cache = candidate
        _loaded = True
    _LOGGER.debug("Nova config set_many_atomic: %d keys", len(updates))
    return True


def delete(key: str) -> None:
    """Remove a config key."""
    global _loaded
    if not _loaded:
        load()
    with _lock:
        _cache_dict().pop(key, None)
    save()


def init_from_addon(addon_options: dict) -> None:
    """
    Called by bootstrap/run.sh on addon startup.
    Writes addon options to config.json, but only for keys that
    aren't already set (preserves panel overrides).
    """
    global _loaded
    if not _loaded:
        load()

    updated = 0
    with _lock:
        for key, value in addon_options.items():
            if key not in _cache_dict():
                _cache_dict()[key] = value
                updated += 1
            # Always update API keys (user might change them in addon config)
            elif key in ("groq_api_key", "api_key", "gemini_api_key",
                         "anthropic_api_key", "openai_api_key"):
                if value and value != _cache_dict().get(key):
                    _cache_dict()[key] = value
                    updated += 1

    if updated:
        save()
        _LOGGER.info(
            "Nova config: merged %d addon options (preserved %d panel overrides)",
            updated, len(addon_options) - updated,
        )


def init_from_entry(entry_data: dict, entry_options: dict) -> None:
    """
    Called when the HA integration loads. Backfills any settings
    from the entry that aren't in config.json yet.
    """
    global _loaded
    if not _loaded:
        load()

    merged = {**entry_data, **entry_options}
    updated = 0
    with _lock:
        for key, value in merged.items():
            if key not in _cache_dict() and value:
                _cache_dict()[key] = value
                updated += 1

    if updated:
        save()
        _LOGGER.debug(
            "Nova config: backfilled %d keys from integration entry",
            updated,
        )
