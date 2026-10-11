"""Where Nova keeps the Nova Voice ID app's address and token (8.36.0).

The address is the panel setting ``voice_id_uri`` (config.json). The token is a
credential, so it lives in Home Assistant's secrets.yaml as
``nova_voice_id_token``, like the AI keys, and is never written to config.json,
a log, the Nova Log or a diagnostics export.

Supervisor discovery sends both. It may fill a field that is empty or still
holds what the last discovery sent; a value the user typed always wins. To
tell the two apart Nova keeps the last discovered address
(``voice_id_uri_discovered``) and a SHA-256 of the last discovered token
(``voice_id_token_discovered_sha256``), never the token itself.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

TOKEN_SECRET = "nova_voice_id_token"
URI_KEY = "voice_id_uri"
URI_DISCOVERED_KEY = "voice_id_uri_discovered"
TOKEN_DISCOVERED_KEY = "voice_id_token_discovered_sha256"
MAX_TOKEN_LEN = 256


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_token(value: Any) -> bool:
    """Printable, no spaces or line breaks (it is written to secrets.yaml)."""
    return (isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_LEN
            and value.isprintable() and not any(c.isspace() for c in value))


def get_uri(hass, entry) -> str:
    try:
        from . import nova_config
        return str(nova_config.runtime_get(hass, entry, URI_KEY, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def async_get_token(hass) -> str:
    from . import ha_secrets
    value = await ha_secrets.async_get_secret(hass, TOKEN_SECRET, "")
    return str(value or "")


async def async_set_token(hass, token: str) -> bool:
    """Save (or, with "", delete) the token. Returns whether it was saved."""
    from . import ha_secrets
    if token == "":
        return await hass.async_add_executor_job(ha_secrets.delete_secret_sync, TOKEN_SECRET)
    if not valid_token(token):
        return False
    return await hass.async_add_executor_job(ha_secrets.set_secret_sync, TOKEN_SECRET, token)


async def async_set_uri(hass, entry, uri: str) -> bool:
    from . import nova_config
    from .runtime import lifecycle_runtime
    runtime = lifecycle_runtime(entry) if entry is not None else None
    if runtime is not None:
        runtime.runtime_config[URI_KEY] = uri
    return await hass.async_add_executor_job(nova_config.set, URI_KEY, uri)


async def async_apply_discovery(hass, entry, uri: str, token: str | None) -> dict[str, bool]:
    """Save what discovery sent, where the user hasn't typed their own.

    Returns {"uri": filled?, "token": filled?}. Never logs the token."""
    from . import nova_config
    out = {"uri": False, "token": False}

    current_uri = get_uri(hass, entry)
    last_uri = str(nova_config.get(URI_DISCOVERED_KEY, "") or "")
    # current == uri: nothing to change, and it counts as filled (an 8.35.0
    # install saved the discovered address but no "last discovered" value).
    if uri and (not current_uri or current_uri == last_uri or current_uri == uri):
        if current_uri != uri:
            await async_set_uri(hass, entry, uri)
        out["uri"] = True
    if uri:
        await hass.async_add_executor_job(nova_config.set, URI_DISCOVERED_KEY, uri)

    if token and valid_token(token):
        current = await async_get_token(hass)
        last_sha = str(nova_config.get(TOKEN_DISCOVERED_KEY, "") or "")
        if not current or _sha(current) == last_sha or current == token:
            if current != token:
                await async_set_token(hass, token)
            out["token"] = True
        await hass.async_add_executor_job(nova_config.set, TOKEN_DISCOVERED_KEY, _sha(token))
    return out
