"""
Nova — Home Assistant secrets.yaml resolver (v6.81.0).

Credentials and passwords belong in Home Assistant's secrets.yaml, not in the
plaintext panel config (/config/nova/config.json). This module is the single,
read-only bridge to that file: it resolves a named secret from
/config/secrets.yaml and does nothing else.

Read-only by design. This module NEVER writes to secrets.yaml — the user owns
that file. It tolerates a missing or malformed file (returns the default and
logs, never raises), so a secrets typo can't take integration setup down — the
same "sideline, don't crash" discipline nova_config learned the hard way.

Blocking file I/O is offloaded to HA's executor via async_get_secret; a bare
synchronous reader is exposed for the executor job and for tests.

(v6.81.0 lands the resolver + its first consumer, the mail agent. Migrating the
existing LLM/observer keys onto it is a separate, isolated change.)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SECRETS_PATH = Path("/config/secrets.yaml")


_SECRETS_CACHE = None            # cached read of the default SECRETS_PATH


def _reset_secrets_cache() -> None:
    global _SECRETS_CACHE
    _SECRETS_CACHE = None


def _read_secrets(path: Path | None = None, force: bool = False) -> dict:
    """Parse secrets.yaml into a dict.

    Missing file → {} (not an error — many installs have none). Malformed YAML,
    or a top level that isn't a mapping → {} plus a warning. Never raises.
    Blocking — call via the executor from async code.

    `path` is resolved at call time (default SECRETS_PATH), not bound at
    definition — otherwise a test monkeypatching SECRETS_PATH wouldn't take,
    the same default-binding trap the DB layer hit.
    """
    global _SECRETS_CACHE
    use_default = path is None
    if path is None:
        path = SECRETS_PATH
    if (use_default and not force and _SECRETS_CACHE is not None
            and _SECRETS_CACHE[0] == path):
        return _SECRETS_CACHE[1]
    result: dict = {}
    try:
        if path.exists():
            import yaml  # PyYAML ships with Home Assistant core
            with open(path) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                result = data
            elif data is not None:
                _LOGGER.warning(
                    "Nova: %s top level is %s, expected a mapping — ignoring",
                    path, type(data).__name__,
                )
    except Exception as exc:  # yaml.YAMLError, OSError, UnicodeDecodeError, …
        _LOGGER.warning("Nova: could not read %s: %s", path, exc)
        result = {}
    if use_default:
        _SECRETS_CACHE = (path, result)
    return result


def get_secret_sync(key: str, default: Any = None,
                    path: Path | None = None) -> Any:
    """Synchronous secret lookup (blocking).

    Prefer async_get_secret from async code so the read runs off the event loop.
    An empty string in secrets.yaml is treated as unset (returns `default`).
    `path` resolves at call time (default SECRETS_PATH).
    """
    if not key:
        return default
    val = _read_secrets(path).get(key, default)
    return val if val not in (None, "") else default


async def async_get_secret(hass, key: str, default: Any = None) -> Any:
    """Resolve a named secret from secrets.yaml, off the event loop.

    Returns `default` when the key is absent/empty or the file is unusable.
    Never raises.
    """
    if hass is None:
        return get_secret_sync(key, default)
    try:
        return await hass.async_add_executor_job(get_secret_sync, key, default)
    except Exception as exc:
        _LOGGER.debug("Nova async_get_secret(%s) failed: %s", key, exc)
        return default


# ── Credential relocation (v6.83.0) ──────────────────────────────────────────
# Config keys that hold LLM credentials. In secrets.yaml they live namespaced
# under nova_<key> so they can't collide with another integration's secrets in
# the shared file.
CREDENTIAL_KEYS = ("api_key", "gemini_api_key", "anthropic_api_key",
                   "openai_api_key", "groq_api_key",
                   "custom_api_key", "ollama_api_key")  # Phase 2, v7.107.0


def secret_key_for(config_key: str) -> str:
    """secrets.yaml key name for a plaintext config credential key."""
    return "nova_" + str(config_key)


def overlay_credentials(config: dict, path: Path | None = None) -> dict:
    """Overlay any credential present in secrets.yaml (under nova_<key>) onto
    `config` — secrets.yaml wins for credentials. One file read. Mutates and
    returns `config`. Never raises."""
    try:
        secrets = _read_secrets(path)
    except Exception:
        return config
    if not secrets:
        return config
    for ck in CREDENTIAL_KEYS:
        sv = secrets.get(secret_key_for(ck))
        if sv not in (None, ""):
            config[ck] = sv
    return config


# ── Per-provider credential CRUD (Phase 2, v7.107.0) ─────────────────────────
#
# The administrator-facing surface for provider credentials: config_flow's
# Credentials section and (read-only) the panel status command both go
# through these, never straight through set_secret_sync/nova_config. Every
# entry point here validates `provider` against the fixed allowlist in
# const.PROVIDER_API_KEY_FIELDS and never accepts or returns a raw value to
# anything but the write itself.

def _provider_field(provider: str):
    from .const import PROVIDER_API_KEY_FIELDS
    return PROVIDER_API_KEY_FIELDS.get(str(provider or "").strip().lower())


def has_provider_credential_sync(provider: str, path: Path | None = None) -> bool:
    """Whether `provider` has a credential configured — never the value
    itself. Checks secrets.yaml first (the durable source of truth), then
    the plaintext config.json field for an install mid-migration. Unknown
    provider -> False, never raises."""
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
    """:func:`has_provider_credential_sync`, off the event loop."""
    if hass is None:
        return has_provider_credential_sync(provider)
    return await hass.async_add_executor_job(has_provider_credential_sync, provider)


async def async_credential_status(hass) -> dict:
    """{provider: bool} for every provider in the allowlist — for a status
    display. Never includes a value."""
    from .const import PROVIDER_API_KEY_FIELDS
    return {
        p: await async_has_provider_credential(hass, p)
        for p in PROVIDER_API_KEY_FIELDS
    }


async def async_set_provider_credential(hass, provider: str, value: str) -> bool:
    """Store `value` as `provider`'s own credential in secrets.yaml.

    Rejects an unknown provider and a blank value (an empty form field must
    never erase an existing credential — the caller wanting to erase one
    calls async_delete_provider_credential explicitly instead). Returns
    whether the write succeeded; never raises.
    """
    field = _provider_field(provider)
    value = (value or "").strip()
    if not field or not value:
        return False
    return await hass.async_add_executor_job(
        set_secret_sync, secret_key_for(field), value)


async def async_delete_provider_credential(hass, provider: str) -> bool:
    """Explicitly remove `provider`'s credential — from secrets.yaml and any
    lingering plaintext config.json copy. Returns True if either location
    held a value and the removal succeeded (or there was nothing to remove);
    never raises."""
    field = _provider_field(provider)
    if not field:
        return False
    ok = await hass.async_add_executor_job(delete_secret_sync, secret_key_for(field))
    try:
        from . import nova_config
        await hass.async_add_executor_job(nova_config.delete, field)
    except Exception:
        pass
    return ok


def _upsert_secret_line(text: str, key: str, value) -> str:
    """secrets.yaml text with `key: "value"` upserted: replace an existing
    top-level `key:` line if present, else append. The rest of the file is kept
    verbatim (comments, other keys, formatting)."""
    import re
    esc = str(value).replace("\\", "\\\\").replace('"', '\\"')
    line = '%s: "%s"' % (key, esc)
    pat = re.compile(r"(?m)^" + re.escape(key) + r":.*$")
    if pat.search(text):
        return pat.sub(line, text, count=1)
    sep = "" if (text == "" or text.endswith("\n")) else "\n"
    return text + sep + line + "\n"


def set_secret_sync(key: str, value, path: Path | None = None) -> bool:
    """Upsert one secret into secrets.yaml. Safe: backs up the existing file,
    writes atomically via a temp file + rename, preserves the rest of the file.
    Returns True on success. Never raises. Blocking — executor from async."""
    if not key:
        return False
    import os
    import shutil
    import tempfile
    try:
        if path is None:
            path = SECRETS_PATH
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        new_text = _upsert_secret_line(text, key, value)
        if path.exists():
            shutil.copy2(str(path), str(path) + ".nova.bak")
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(new_text)
            os.replace(tmp, str(path))
            _reset_secrets_cache()   # written file — next read must be fresh
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return True
    except Exception as exc:
        _LOGGER.warning("Nova: could not write secret '%s': %s", key, exc)
        return False


def _remove_secret_line(text: str, key: str) -> str:
    """`text` with a top-level `key:` line removed, if present. Everything
    else is kept verbatim. A no-op (returns `text` unchanged) when the key
    isn't there."""
    import re
    pat = re.compile(r"(?m)^" + re.escape(key) + r":.*\n?")
    return pat.sub("", text, count=1)


def delete_secret_sync(key: str, path: Path | None = None) -> bool:
    """Remove one secret from secrets.yaml, if present. Safe: backs up the
    existing file first, writes atomically via a temp file + rename,
    preserves everything else in the file. Returns True whether the key was
    removed or was already absent (both leave secrets.yaml in the desired
    state); False only on an actual write failure. Never raises. Blocking —
    executor from async."""
    if not key:
        return False
    import os
    import shutil
    import tempfile
    try:
        if path is None:
            path = SECRETS_PATH
        path = Path(path)
        if not path.exists():
            return True
        text = path.read_text()
        new_text = _remove_secret_line(text, key)
        if new_text == text:
            return True  # key wasn't present
        shutil.copy2(str(path), str(path) + ".nova.bak")
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(new_text)
            os.replace(tmp, str(path))
            _reset_secrets_cache()
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return True
    except Exception as exc:
        _LOGGER.warning("Nova: could not remove secret '%s': %s", key, exc)
        return False


async def relocate_plaintext_credentials(hass) -> int:
    """One-time, safe migration of plaintext LLM credentials out of the panel
    config (config.json) and into secrets.yaml.

    Per credential key present in config.json with a real value:
      - already in secrets.yaml with the SAME value -> drop the redundant copy;
      - already there but DIFFERENT -> leave both (don't guess; secrets wins on read);
      - otherwise write it, re-read to VERIFY it's durable, and only then delete
        it from config.json. If write or verify fails, config.json is left
        untouched — the key still resolves via fallback, so auth can't break.

    Returns the count of plaintext copies removed. Never raises.
    """
    from . import nova_config
    removed = 0
    try:
        cfg = await hass.async_add_executor_job(nova_config.get_all)
    except Exception:
        return 0
    for ck in CREDENTIAL_KEYS:
        val = cfg.get(ck)
        if not val:
            continue
        skey = secret_key_for(ck)
        try:
            existing = await hass.async_add_executor_job(get_secret_sync, skey, None)
            if existing == val:
                await hass.async_add_executor_job(nova_config.delete, ck)
                removed += 1
                continue
            if existing:
                continue  # present but different — leave both untouched
            ok = await hass.async_add_executor_job(set_secret_sync, skey, val)
            if not ok:
                continue
            check = await hass.async_add_executor_job(get_secret_sync, skey, None)
            if check == val:
                await hass.async_add_executor_job(nova_config.delete, ck)
                removed += 1
            # else: verify failed -> leave plaintext (resolves via fallback)
        except Exception as exc:
            _LOGGER.debug("Nova: relocate %s skipped: %s", ck, exc)
    if removed:
        _LOGGER.info("Nova: relocated %d plaintext credential(s) to secrets.yaml", removed)
    return removed


# ── Shared-credential provider split (Phase 2, v7.107.0) ─────────────────────
#
# Phase 1 kept one shared credential (config key `api_key`, wherever it
# currently resolves from — plaintext config.json or the `nova_api_key`
# secret relocate_plaintext_credentials already moved it to) for whichever
# provider the installation's `llm_provider` happened to be set to. This
# splits that single value into the provider-specific slot it actually
# belongs to, so every provider's key is stored under its own name and
# routing (resolve_provider_credential) no longer needs the shared field at
# all once migration completes.

def _detect_provider_from_key_strict(api_key: str) -> str | None:
    """Cloud provider implied by a key's own prefix, or None if the shape is
    unrecognized. Deliberately stricter than llm_provider.detect_provider_from_key,
    which defaults to 'groq' for an unrecognized shape — a fine default for
    first-run setup (some provider must be picked to proceed) but wrong for a
    migration, where guessing wrong silently routes a saved secret to a
    provider it was never issued for. Never guesses; returns None instead."""
    k = (api_key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("gsk_"):
        return "groq"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-"):
        return "openai"
    return None


async def split_shared_credential(hass) -> dict:
    """One-time, idempotent migration of the legacy shared credential into
    the provider-specific slot it belongs to.

    Ownership is decided by, in order:
      1. The saved `llm_provider` value, when it names one of the four fixed
         cloud providers — the installation already told Nova which service
         this key is for.
      2. The key's own prefix (see _detect_provider_from_key_strict), used
         ONLY when (1) doesn't apply.
      3. Otherwise left alone: nothing is written or deleted, and a Repair
         issue is raised so an administrator can migrate by hand (Settings →
         Nova → Configure → Credentials).

    Never copies into more than one provider's slot, never overwrites an
    existing provider-specific credential (plaintext or secret — checked
    before every write), and only removes the legacy value (both its
    secrets.yaml and config.json forms) after the new provider secret is
    written AND read back to verify it matches. A write or verify failure
    leaves the legacy value exactly as it was, so the installation keeps
    working on the old shared key until migration can succeed. Idempotent:
    once a provider slot is populated, or the shared key is gone, every
    later call is a no-op. Never raises.
    """
    from . import nova_config

    result = {"migrated": None, "ambiguous": False, "reason": None}
    try:
        cfg = await hass.async_add_executor_job(nova_config.get_all)
    except Exception:
        return result

    legacy_val = str(cfg.get("api_key") or "")
    if not legacy_val:
        try:
            legacy_val = str(
                await hass.async_add_executor_job(
                    get_secret_sync, secret_key_for("api_key"), None
                ) or ""
            )
        except Exception:
            legacy_val = ""
    if not legacy_val:
        return result  # nothing to migrate — already split or never set

    saved_provider = str(cfg.get("llm_provider") or "").strip().lower()
    from .const import CREDENTIAL_LEGACY_FALLBACK_PROVIDERS
    target = saved_provider if saved_provider in CREDENTIAL_LEGACY_FALLBACK_PROVIDERS else None
    if target is None:
        target = _detect_provider_from_key_strict(legacy_val)

    if target is None:
        _LOGGER.warning(
            "Nova: could not determine which provider the shared "
            "credential belongs to (no saved provider, unrecognized key "
            "shape) — leaving it in place. Set it explicitly per "
            "provider in Settings → Nova → Configure → Credentials."
        )
        try:
            from . import repair_notices
            repair_notices.note_credential_migration_ambiguous(hass)
        except Exception:
            pass
        result["ambiguous"] = True
        result["reason"] = "unrecognized key shape and no saved llm_provider"
        return result

    target_field = "%s_api_key" % target
    try:
        existing_plain = cfg.get(target_field)
        existing_secret = await hass.async_add_executor_job(
            get_secret_sync, secret_key_for(target_field), None)
    except Exception:
        existing_plain = None
        existing_secret = None
    if existing_plain or existing_secret:
        # Already has its own credential — never overwrite, never duplicate.
        result["migrated"] = "already_present"
        return result

    target_secret_key = secret_key_for(target_field)
    try:
        ok = await hass.async_add_executor_job(set_secret_sync, target_secret_key, legacy_val)
        if not ok:
            result["reason"] = "write_failed"
            return result
        check = await hass.async_add_executor_job(get_secret_sync, target_secret_key, None)
        if check != legacy_val:
            result["reason"] = "verify_failed"
            return result

        # Verified durable under the new name — now safe to remove the old
        # value, in both places it might live.
        await hass.async_add_executor_job(nova_config.delete, "api_key")
        await hass.async_add_executor_job(delete_secret_sync, secret_key_for("api_key"))
    except Exception as exc:
        _LOGGER.warning("Nova: credential provider-split failed for %s: %s", target, exc)
        result["reason"] = "error"
        return result

    try:
        from . import repair_notices
        repair_notices.clear_credential_migration_ambiguous(hass)
    except Exception:
        pass
    result["migrated"] = target
    _LOGGER.info(
        "Nova: migrated the shared credential to its own provider-specific "
        "slot (%s)", target,
    )
    return result
