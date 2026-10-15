"""Nova's own settings file, config.json, and the merged view of all config.

The file lives at <config>/nova/config.json (paths.py decides where). It is
read once into a process cache; the panel and the options dialog write
through ``set``/``set_many`` so the cache and the file move together, and
a restart reads the file again. Read settings with ``get`` here rather than
from ``entry.options`` or ``hass.data``.

Two merged views sit on top: ``effective_config`` (entry, then config.json,
then secrets.yaml credentials) and ``runtime_get`` (the same order with the
panel's live values first, for one key at a time).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import paths
from .persistence.files import write_json_atomic

_LOGGER = logging.getLogger(__name__)

# Set to a path to use another file; None means this instance's own
# <config>/nova/config.json from paths.py.
CONFIG_PATH: Optional[Path] = None

# Credentials and personal details can sit in the file, so only Home
# Assistant's own user may read it.
_FILE_MODE = 0o600

# Endpoints the staged AI setup split apart. Once that has run, a blank
# value in config.json means "cleared" and beats an older entry value.
_SPLIT_ENDPOINTS = ("ollama_base_url", "custom_base_url")
_SPLIT_DONE = "self_hosted_endpoints_migrated"

_lock = threading.Lock()
_cache: dict = {}
_loaded = False
# Why the last load could not use the file (it was moved aside and defaults
# were used), or None. Setup turns this into a notification.
last_load_error: Optional[str] = None


def _config_path() -> Path:
    return CONFIG_PATH or Path(paths.nova_path("config.json"))


def _ensure_dir() -> None:
    _config_path().parent.mkdir(parents=True, exist_ok=True)


def _forget() -> None:
    global _cache, _loaded
    with _lock:
        _cache = {}
        _loaded = False


def configure(hass) -> None:
    """Point at this Home Assistant's config folder and forget the cache.

    Setup calls this first. A Nova reload reuses this module, so the cache
    is dropped here to pick up a file restored or edited since."""
    paths.configure(hass)
    _forget()


def reload() -> dict:
    """Read config.json again from disk."""
    _forget()
    return load()


def _tighten_mode() -> None:
    """Take group and other access off a file older versions wrote with the
    default umask. A failure is only logged."""
    path = _config_path()
    try:
        if path.stat().st_mode & 0o077:
            os.chmod(path, _FILE_MODE)
    except OSError as exc:
        _LOGGER.debug("Nova config: could not tighten permissions: %s", exc)


def _set_aside(reason: str) -> None:
    """Rename an unusable file to config.json.corrupt-<time> so the user's
    edits survive, and record why. Never raises."""
    global last_load_error
    path = _config_path()
    kept = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        path.rename(kept)
        last_load_error = f"{reason} — file preserved at {kept.name}"
    except Exception:
        last_load_error = f"{reason} — could not sideline file"
    _LOGGER.error("Nova config.json unusable (%s). Starting with defaults; "
                  "your file was kept for recovery. Fix the JSON and settings "
                  "return.", last_load_error)


def _read_file() -> dict:
    """The file's settings, {} when there is no file. A file that is not
    UTF-8 text, not JSON, or not a JSON object is set aside, never left for
    the next save to overwrite."""
    path = _config_path()
    if not path.exists():
        return {}
    _tighten_mode()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except UnicodeDecodeError as exc:
        _set_aside(f"invalid JSON: not UTF-8 text ({exc.reason})")
        return {}
    except json.JSONDecodeError as exc:
        _set_aside(f"invalid JSON: {exc}")
        return {}
    if not isinstance(data, dict):
        _set_aside(f"top level is {type(data).__name__}, expected object")
        return {}
    return data


def load() -> dict:
    """Fill the cache from disk and return a copy. Never raises: a bad file
    is set aside (see _read_file) and anything else starts empty."""
    global _cache, _loaded, last_load_error
    _ensure_dir()
    with _lock:
        last_load_error = None
        try:
            _cache = _read_file()
            if last_load_error is None:
                _LOGGER.info("Nova config loaded: %d keys from %s",
                             len(_cache), _config_path())
        except Exception as exc:
            _LOGGER.warning("Nova config load error: %s", exc)
            _cache = {}
        _loaded = True
        return dict(_cache)


def _ensure_loaded() -> None:
    if not _loaded:
        load()


def _cache_dict() -> dict:
    """The cache, made a dict again if anything ever replaced it."""
    global _cache
    if isinstance(_cache, dict):
        return _cache
    _LOGGER.error("Nova config cache was %s — resetting to {}", type(_cache).__name__)
    _cache = {}
    return _cache


def _write(data: dict) -> None:
    write_json_atomic(_config_path(), data, indent=2, default=str, mode=_FILE_MODE)


def save() -> bool:
    """Write the cache to disk; False when that failed (disk full, no
    permission). The cache keeps the new values either way, so this session
    carries on; a caller like the panel checks the result to say the change
    will not survive a restart."""
    _ensure_dir()
    with _lock:
        try:
            _write(_cache)
        except Exception as exc:
            _LOGGER.warning("Nova config save error: %s", exc)
            return False
    return True


def get(key: str, default: Any = None) -> Any:
    """One setting from config.json."""
    _ensure_loaded()
    with _lock:
        return _cache_dict().get(key, default)


def get_all() -> dict:
    """A copy of every setting in config.json."""
    _ensure_loaded()
    with _lock:
        return dict(_cache_dict())


def _live(hass) -> dict:
    try:
        from .runtime import domain_runtime_config
        return domain_runtime_config(hass)
    except Exception:
        return {}


def announce_notify_only(hass) -> bool:
    """Whether proactive announcements go to the phone instead of the
    speakers (critical ones are still spoken). Read on every call: the
    panel's live value, else config.json, else off."""
    live = _live(hass)
    if "announce_notify_only" in live:
        value = live["announce_notify_only"]
    else:
        value = get("announce_notify_only", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def output_language(hass) -> str:
    """The ``output_language`` setting as stored: "" or "auto" to follow Home
    Assistant, or a language code. Read on every call, panel value first ("" there
    is auto even when config.json is behind), then config.json. A value that
    is not a string reads as "". output_language.parse() validates it."""
    live = _live(hass)
    if "output_language" in live:
        value = live["output_language"]
    else:
        value = get("output_language", "")
    return value if type(value) is str else ""


def _split_endpoints_done(settings: dict) -> bool:
    return settings.get(_SPLIT_DONE) is True


def effective_config(entry=None) -> dict:
    """Everything one run should use, in one dict.

    The entry's data, then its options, then config.json on top, where only
    a real value from config.json counts: a blank panel field never wipes a
    credential the entry carries. Credentials from secrets.yaml win over
    all of it. Every provider, model and key lookup goes through here so no
    two paths can pick a different model.

    Blocking the first time (reads config.json): from async code use
    ``await hass.async_add_executor_job(nova_config.effective_config, entry)``.
    With entry None it is the panel config alone.
    """
    panel = get_all()
    merged: dict = {}
    if entry is not None:
        for layer in (getattr(entry, "data", None), getattr(entry, "options", None)):
            merged.update(dict(layer or {}))
    split_done = _split_endpoints_done(panel)
    for key, value in panel.items():
        if split_done and key in _SPLIT_ENDPOINTS:
            merged[key] = value or ""
        elif value is not None and value != "":
            merged[key] = value
    from . import ha_secrets
    return ha_secrets.overlay_credentials(merged)


def effective_config_with_runtime(entry=None, runtime_config: dict | None = None) -> dict:
    """:func:`effective_config` with the panel's live ``runtime_config`` laid
    over it (again only real values count).

    The live values belong to the entry's NovaRuntime. This runs in an
    executor, so callers on the event loop pass a fresh
    ``runtime_config_snapshot()``, never the live dict. Use it whenever a
    subsystem starts or restarts: a panel install has empty entry data and
    options, so building from the entry alone would lose every setting.
    Blocking (reads config.json).
    """
    merged = effective_config(entry)
    for key, value in (runtime_config or {}).items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def runtime_get(hass, entry, key: str, default=None):
    """One setting, cheaply, in :func:`effective_config` order: the panel's
    live value, then config.json, then entry.options, then entry.data, then
    ``default``. All in memory once setup has read the file.

    Prefer it to reading ``entry.options`` or ``entry.data`` directly: those
    are empty on a panel install, so a direct read gives the default for
    anything set in the panel, a mistake behind several old bugs.

    The live values are read on the event loop, so never pass ``hass`` from
    an executor thread; pass ``hass=None`` to skip them. An entry that is
    not loaded (setting up, failed, unloaded) has no live values and the
    lower layers answer. A loaded entry without its runtime raises
    NovaRuntimeUnavailable instead of falling back; nothing else raises.
    """
    if hass is not None and entry is not None:
        from .runtime import lifecycle_runtime_config
        live = lifecycle_runtime_config(entry)   # raises: loaded without runtime
        if key in live and live[key] not in (None, ""):
            return live[key]
    try:
        value = get(key, None)
        if (key in _SPLIT_ENDPOINTS and get(_SPLIT_DONE, False) is True
                and key in get_all()):
            return value or ""
        if value not in (None, ""):
            return value
    except Exception:
        pass
    if entry is not None:
        for layer in (getattr(entry, "options", None), getattr(entry, "data", None)):
            layer = layer or {}
            if key in layer:
                return layer[key]
    return default


# ── writing ─────────────────────────────────────────────────────────────────
# Credentials (ha_secrets.CREDENTIAL_KEYS) are never written here with a
# value: they belong in secrets.yaml. Writing one empty is allowed, as that
# is how a stale plain copy is cleared. This module's own ``set`` hides the
# builtin, hence frozenset below.

_credential_keys_cache: Optional[frozenset] = None


def _credential_keys() -> frozenset:
    """The credential key names, looked up once; empty (nothing refused)
    if ha_secrets cannot be imported."""
    global _credential_keys_cache
    if _credential_keys_cache is None:
        try:
            from . import ha_secrets
            _credential_keys_cache = frozenset(ha_secrets.CREDENTIAL_KEYS)
        except Exception:
            _credential_keys_cache = frozenset()
    return _credential_keys_cache


def _is_credential(key: str, value: Any) -> bool:
    return bool(value) and key in _credential_keys()


def set(key: str, value: Any) -> bool:
    """Store one setting and save. The cache takes the value even when the
    save fails; the result says whether it reached disk. A credential with
    a value is refused (False)."""
    if _is_credential(key, value):
        _LOGGER.warning("Nova config: refused to store credential key '%s' in "
                        "config.json — use ha_secrets.async_set_provider_credential "
                        "instead", key)
        return False
    _ensure_loaded()
    with _lock:
        _cache_dict()[key] = value
    saved = save()
    _LOGGER.debug("Nova config set: %s = %s", key, str(value)[:100])
    return saved


def set_many(updates: dict) -> bool:
    """Store several settings and save, as ``set`` does. Credentials with a
    value are left out; the rest are still stored."""
    allowed = {k: v for k, v in updates.items() if not _is_credential(k, v)}
    refused = len(updates) - len(allowed)
    if refused:
        _LOGGER.warning("Nova config: refused to store %d credential key(s) in "
                        "config.json — use ha_secrets.async_set_provider_credential "
                        "instead", refused)
    _ensure_loaded()
    with _lock:
        _cache_dict().update(allowed)
    saved = save()
    _LOGGER.debug("Nova config set_many: %d keys", len(allowed))
    return saved


def set_many_atomic(updates: dict) -> bool:
    """Store several settings all or nothing.

    The new file is written first and the cache changes only once it has
    replaced the old one, so a failed write leaves disk and memory as they
    were rather than half applied. Any credential with a value refuses the
    whole update.
    """
    global _cache, _loaded
    if any(_is_credential(k, v) for k, v in updates.items()):
        _LOGGER.warning("Nova config: refused atomic update containing a credential")
        return False
    _ensure_loaded()
    _ensure_dir()
    with _lock:
        candidate = {**_cache_dict(), **updates}
        try:
            _write(candidate)
        except Exception as exc:
            _LOGGER.warning("Nova atomic config save error: %s", exc)
            return False
        _cache, _loaded = candidate, True
    _LOGGER.debug("Nova config set_many_atomic: %d keys", len(updates))
    return True


def delete(key: str) -> bool:
    """Remove one setting and save; the result says whether it reached disk."""
    _ensure_loaded()
    with _lock:
        _cache_dict().pop(key, None)
    return save()


def init_from_entry(entry_data: dict, entry_options: dict) -> None:
    """At setup, copy into config.json any entry setting it does not have
    yet (options over data), and save only when something was added."""
    _ensure_loaded()
    added = 0
    with _lock:
        cache = _cache_dict()
        for key, value in {**entry_data, **entry_options}.items():
            if value and key not in cache:
                cache[key] = value
                added += 1
    if added:
        save()
        _LOGGER.debug("Nova config: backfilled %d keys from integration entry", added)
