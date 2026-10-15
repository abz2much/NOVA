"""Nova's bridge to Home Assistant's secrets.yaml.

Provider credentials live in secrets.yaml (under ``nova_<field>``) rather
than in the plain panel config at /config/nova/config.json. Reading is
forgiving: a missing file is normal, and a broken one is logged and treated
as empty, so a typo in secrets.yaml can never stop Nova from setting up.

Writes touch only Nova's own keys. Each write keeps a ``.nova.bak`` copy of
the previous file, replaces the file atomically, and leaves every other line
exactly as it was. All file access blocks, so async callers go through the
executor helpers below.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import paths

_LOGGER = logging.getLogger(__name__)

# Tests point this at a temporary file; None means the real config folder.
SECRETS_PATH: Optional[Path] = None

# (path, parsed mapping) for the default file, so repeated lookups during
# setup read the disk once. Nova's own writes clear it.
_SECRETS_CACHE = None

# Config fields that hold an AI provider credential.
CREDENTIAL_KEYS = ("api_key", "gemini_api_key", "anthropic_api_key",
                   "openai_api_key", "groq_api_key",
                   "custom_api_key", "ollama_api_key")

# A secret value must fit on one quoted YAML line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _secrets_path() -> Path:
    return SECRETS_PATH or Path(paths.config_path("secrets.yaml"))


def _reset_secrets_cache() -> None:
    global _SECRETS_CACHE
    _SECRETS_CACHE = None


def _parse_file(path: Path) -> dict:
    if not path.exists():
        return {}
    import yaml  # bundled with Home Assistant
    with open(path) as handle:
        data = yaml.safe_load(handle)
    if data is None or isinstance(data, dict):
        return data or {}
    _LOGGER.warning(
        "Nova: %s top level is %s, expected a mapping — ignoring",
        path, type(data).__name__,
    )
    return {}


def _read_secrets(path: Path | None = None, force: bool = False) -> dict:
    """The secrets file as a dict; {} when it is missing or unusable.

    Only the default file is cached. The default is looked up on every call
    (not bound as an argument default) so a patched SECRETS_PATH takes
    effect. Blocking.
    """
    global _SECRETS_CACHE
    cacheable = path is None
    target = _secrets_path() if cacheable else path
    cached = _SECRETS_CACHE
    if cacheable and not force and cached is not None and cached[0] == target:
        return cached[1]
    try:
        parsed = _parse_file(target)
    except Exception as exc:  # bad YAML, unreadable file, bad encoding
        _LOGGER.warning("Nova: could not read %s: %s", target, exc)
        parsed = {}
    if cacheable:
        _SECRETS_CACHE = (target, parsed)
    return parsed


def get_secret_sync(key: str, default: Any = None,
                    path: Path | None = None) -> Any:
    """One secret by name, or ``default`` when absent or blank. Blocking."""
    if not key:
        return default
    found = _read_secrets(path).get(key, default)
    if found is None or found == "":
        return default
    return found


async def async_get_secret(hass, key: str, default: Any = None) -> Any:
    """:func:`get_secret_sync` on the executor. Never raises."""
    if hass is not None:
        try:
            return await hass.async_add_executor_job(get_secret_sync, key, default)
        except Exception as exc:
            _LOGGER.debug("Nova async_get_secret(%s) failed: %s", key, exc)
            return default
    return get_secret_sync(key, default)


def secret_key_for(config_key: str) -> str:
    """Name a config credential field takes inside secrets.yaml."""
    return f"nova_{config_key}"


def overlay_credentials(config: dict, path: Path | None = None) -> dict:
    """Copy every credential found in secrets.yaml onto ``config``.

    secrets.yaml wins over config.json. Changes ``config`` in place and
    returns it; never raises.
    """
    try:
        stored = _read_secrets(path)
    except Exception:
        return config
    for field in CREDENTIAL_KEYS if stored else ():
        value = stored.get(secret_key_for(field))
        if value is not None and value != "":
            config[field] = value
    return config


# Per provider credentials, as used by the Credentials screen and the panel
# status. The provider is always checked against const.PROVIDER_API_KEY_FIELDS
# and no value is ever handed back to a caller.

def _provider_field(provider: str):
    from .const import PROVIDER_API_KEY_FIELDS
    name = str(provider or "").strip().lower()
    return PROVIDER_API_KEY_FIELDS.get(name)


def has_provider_credential_sync(provider: str, path: Path | None = None) -> bool:
    """True when ``provider`` has a key in secrets.yaml or, for an install
    still being migrated, in config.json. Unknown providers are False."""
    field = _provider_field(provider)
    if not field:
        return False
    if get_secret_sync(secret_key_for(field), None, path) not in (None, ""):
        return True
    try:
        from . import nova_config
        return bool(nova_config.get(field))
    except Exception:
        return False


async def async_has_provider_credential(hass, provider: str) -> bool:
    """:func:`has_provider_credential_sync` on the executor."""
    if hass is None:
        return has_provider_credential_sync(provider)
    return await hass.async_add_executor_job(has_provider_credential_sync, provider)


async def async_credential_status(hass) -> dict:
    """Which providers have a key, as {provider: bool}. No values."""
    from .const import PROVIDER_API_KEY_FIELDS
    status = {}
    for provider in PROVIDER_API_KEY_FIELDS:
        status[provider] = await async_has_provider_credential(hass, provider)
    return status


async def async_set_provider_credential(hass, provider: str, value: str) -> bool:
    """Save ``value`` as the provider's key. False for an unknown provider,
    a blank value (a blank field never erases a key) or a failed write."""
    field = _provider_field(provider)
    cleaned = (value or "").strip()
    if not (field and cleaned):
        return False
    return await hass.async_add_executor_job(
        set_secret_sync, secret_key_for(field), cleaned)


async def async_delete_provider_credential(hass, provider: str) -> bool:
    """Remove the provider's key from secrets.yaml and from any plain
    config.json copy. True unless the secrets.yaml write itself failed."""
    field = _provider_field(provider)
    if not field:
        return False
    removed = await hass.async_add_executor_job(
        delete_secret_sync, secret_key_for(field))
    try:
        from . import nova_config
        await hass.async_add_executor_job(nova_config.delete, field)
    except Exception:
        pass
    return removed


def _upsert_secret_line(text: str, key: str, value) -> str:
    """``text`` with ``key: "value"`` set: the first top level ``key:`` line
    is replaced, or the line is appended. Nothing else changes."""
    quoted = str(value).replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key}: "{quoted}"'
    existing = re.compile(r"(?m)^" + re.escape(key) + r":.*$")
    if existing.search(text):
        # Replacement via a function so backslashes in the value stay literal.
        return existing.sub(lambda _match: new_line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}{new_line}\n"


def _remove_secret_line(text: str, key: str) -> str:
    """``text`` without its first top level ``key:`` line; unchanged when
    the key is not there."""
    line = re.compile(r"(?m)^" + re.escape(key) + r":.*\n?")
    return line.sub("", text, count=1)


def _replace_file(path: Path, new_text: str) -> None:
    """Back up ``path`` and swap in ``new_text`` atomically. Both the backup
    and the new file hold credentials, so both are owner only."""
    backup = f"{path}.nova.bak"
    if path.exists():
        shutil.copy2(str(path), backup)
        os.chmod(backup, 0o600)
    handle, temp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".secrets-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(handle, "w") as out:
            out.write(new_text)
        os.replace(temp, str(path))
        _reset_secrets_cache()
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except Exception:
                pass


def set_secret_sync(key: str, value, path: Path | None = None) -> bool:
    """Add or update one secret. A value with a line break or other control
    character is refused, because it would split the YAML line and break
    every ``!secret`` in Home Assistant. True on success; never raises.
    Blocking."""
    if not key:
        return False
    if _CONTROL_CHARS.search(str(value)):
        _LOGGER.warning(
            "Nova: refused to write secret '%s': the value contains a line "
            "break or control character", key)
        return False
    try:
        target = Path(path if path is not None else _secrets_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.read_text() if target.exists() else ""
        _replace_file(target, _upsert_secret_line(current, key, value))
        return True
    except Exception as exc:
        _LOGGER.warning("Nova: could not write secret '%s': %s", key, exc)
        return False


def delete_secret_sync(key: str, path: Path | None = None) -> bool:
    """Remove one secret. An absent key or file already counts as success;
    False only when the write fails. Never raises. Blocking."""
    if not key:
        return False
    try:
        target = Path(path if path is not None else _secrets_path())
        if not target.exists():
            return True
        current = target.read_text()
        trimmed = _remove_secret_line(current, key)
        if trimmed != current:
            _replace_file(target, trimmed)
        return True
    except Exception as exc:
        _LOGGER.warning("Nova: could not remove secret '%s': %s", key, exc)
        return False


async def relocate_plaintext_credentials(hass) -> int:
    """Move plain credentials from config.json into secrets.yaml, once.

    For each credential in config.json: an identical secret means the plain
    copy is simply dropped; a different secret means both are left alone
    (secrets.yaml already wins on read); otherwise the secret is written,
    read back, and only a matching read back removes the plain copy. Any
    failure keeps config.json as it was. Returns how many plain copies were
    removed; never raises.
    """
    from . import nova_config
    run = hass.async_add_executor_job
    try:
        cfg = await run(nova_config.get_all)
    except Exception:
        return 0
    removed = 0
    for field in CREDENTIAL_KEYS:
        plain = cfg.get(field)
        if not plain:
            continue
        name = secret_key_for(field)
        try:
            stored = await run(get_secret_sync, name, None)
            if stored and stored != plain:
                continue
            if not stored:
                if not await run(set_secret_sync, name, plain):
                    continue
                if await run(get_secret_sync, name, None) != plain:
                    continue
            await run(nova_config.delete, field)
            removed += 1
        except Exception as exc:
            _LOGGER.debug("Nova: relocate %s skipped: %s", field, exc)
    if removed:
        _LOGGER.info("Nova: relocated %d plaintext credential(s) to secrets.yaml", removed)
    return removed


# Splitting the old shared credential. Early installs had one ``api_key``
# (in config.json, or already moved to ``nova_api_key``) used by whichever
# provider was selected. This moves it to that provider's own field so
# routing no longer needs the shared one.

_KEY_PREFIXES = (("sk-ant-", "anthropic"), ("gsk_", "groq"),
                 ("AIza", "gemini"), ("sk-", "openai"))


def _detect_provider_from_key_strict(api_key: str) -> str | None:
    """Provider a key's prefix proves, or None. Unlike
    llm_provider.detect_provider_from_key there is no fallback guess: a
    wrong guess here would file a saved key under the wrong provider."""
    cleaned = (api_key or "").strip()
    for prefix, provider in _KEY_PREFIXES:
        if cleaned.startswith(prefix):
            return provider
    return None


def _note_ambiguous(hass) -> None:
    try:
        from . import repair_notices
        repair_notices.note_credential_migration_ambiguous(hass)
    except Exception:
        pass


def _clear_ambiguous(hass) -> None:
    try:
        from . import repair_notices
        repair_notices.clear_credential_migration_ambiguous(hass)
    except Exception:
        pass


async def _legacy_value(hass, cfg: dict) -> str:
    plain = str(cfg.get("api_key") or "")
    if plain:
        return plain
    try:
        stored = await hass.async_add_executor_job(
            get_secret_sync, secret_key_for("api_key"), None)
    except Exception:
        return ""
    return str(stored or "")


async def split_shared_credential(hass) -> dict:
    """Move the old shared ``api_key`` into its provider's own field, once.

    The owner is the saved ``llm_provider`` when it is one of the fixed cloud
    providers, else whatever the key's prefix proves. When neither settles
    it, nothing changes and a Repair issue asks for a manual move. An
    existing provider key is never overwritten. The shared value is removed
    (from both places) only after the new secret reads back identical; any
    failure leaves it working as before. Safe to call repeatedly. Never
    raises.
    """
    from . import nova_config
    from .const import CREDENTIAL_LEGACY_FALLBACK_PROVIDERS
    run = hass.async_add_executor_job

    result = {"migrated": None, "ambiguous": False, "reason": None}
    try:
        cfg = await run(nova_config.get_all)
    except Exception:
        return result
    legacy = await _legacy_value(hass, cfg)
    if not legacy:
        return result

    saved = str(cfg.get("llm_provider") or "").strip().lower()
    if saved in CREDENTIAL_LEGACY_FALLBACK_PROVIDERS:
        target = saved
    else:
        target = _detect_provider_from_key_strict(legacy)
    if target is None:
        _LOGGER.warning(
            "Nova: could not determine which provider the shared "
            "credential belongs to (no saved provider, unrecognized key "
            "shape) — leaving it in place. Set it explicitly per "
            "provider in Settings → Nova → Configure → Credentials."
        )
        _note_ambiguous(hass)
        result.update(ambiguous=True,
                      reason="unrecognized key shape and no saved llm_provider")
        return result

    field = f"{target}_api_key"
    name = secret_key_for(field)
    try:
        has_own = bool(cfg.get(field)) or bool(await run(get_secret_sync, name, None))
    except Exception:
        has_own = False
    if has_own:
        result["migrated"] = "already_present"
        return result

    try:
        if not await run(set_secret_sync, name, legacy):
            result["reason"] = "write_failed"
            return result
        if await run(get_secret_sync, name, None) != legacy:
            result["reason"] = "verify_failed"
            return result
        await run(nova_config.delete, "api_key")
        await run(delete_secret_sync, secret_key_for("api_key"))
    except Exception as exc:
        _LOGGER.warning("Nova: credential provider-split failed for %s: %s", target, exc)
        result["reason"] = "error"
        return result

    _clear_ambiguous(hass)
    result["migrated"] = target
    _LOGGER.info(
        "Nova: migrated the shared credential to its own provider-specific "
        "slot (%s)", target,
    )
    return result
