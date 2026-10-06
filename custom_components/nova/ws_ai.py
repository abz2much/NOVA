"""The AI settings commands: models, provider tests, apply, credentials.

Moved verbatim out of websocket.py: nova/list_models, nova/test_provider_endpoint,
nova/apply_ai_config, nova/get_credential_status, nova/set_credential and
nova/delete_credential, with the helpers they use. Error text still goes
through safe_errors (NovaValidationError keeps Nova's own messages, anything
else is reduced to its type). The logger keeps the name it had in
websocket.py, so log lines are unchanged.

websocket.py imports every handler by name (async_register registers them)
and re-exports invalidate_model_cache.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .llm_provider import resolve_provider_endpoint
from .safe_errors import REDACTED, NovaValidationError, safe_error_message
from .ws_bridge import _executor_runtime_config, _get_entry

_LOGGER = logging.getLogger(f"{__package__}.websocket")


# Model discovery lives in providers.discovery (descriptor-driven, fixed cloud
# destinations, saved local/custom endpoints, validated redirects, bounded
# requests). These wrappers keep websocket.py's command handlers and patch
# points stable.
_SAFE_MODEL_DISCOVERY_ERROR = "model_discovery_unavailable"


def invalidate_model_cache(provider: Optional[str] = None) -> None:
    """Drop cached model lists (and any in-flight dedup entry) for
    `provider`, or everything when `provider` is None. Called whenever a
    credential or endpoint discovery depends on changes. A fetch already
    running when this is called can no longer populate the cache."""
    from .providers import discovery
    discovery.invalidate_model_cache(provider)


async def _fetch_models(
    hass, provider: str, config: dict,
) -> tuple[list[str], bool, list[dict]]:
    """Query a provider's models endpoint through HA's shared aiohttp
    session: model ids, truncation state and provider-reported metadata."""
    from homeassistant.helpers import aiohttp_client
    from .providers import discovery

    session = aiohttp_client.async_get_clientsession(hass)
    request = discovery.resolve_discovery_request(config, provider)
    result = await discovery.fetch_models(hass, session, request)
    models, truncated, details = result.as_tuple()
    return list(models), truncated, list(details)


async def _fetch_models_deduped(
    hass, provider: str, config: dict, cache_key,
) -> tuple[list[str], bool, list[dict]]:
    """_fetch_models, but concurrent callers for the same (provider, url)
    and cache generation share one in-flight request."""
    from .providers.discovery import MODEL_CACHE
    return await MODEL_CACHE.dedupe(
        cache_key, lambda: _fetch_models(hass, provider, config))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_models",
    vol.Required("provider"): str,
    vol.Optional("refresh", default=False): bool,
})
@websocket_api.async_response
async def ws_list_models(hass: HomeAssistant, connection, msg) -> None:
    """Return the live model list for a provider (Settings AI-Models
    dropdowns). `refresh: true` forces a fresh fetch past the cache — still
    no caller-supplied destination, only a boolean."""
    from .providers import discovery

    provider = str(msg.get("provider") or "").strip().lower()
    refresh = bool(msg.get("refresh", False))
    entry = _get_entry(hass)
    url = ""
    try:
        from . import nova_config
        config = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime,
            entry,
            _executor_runtime_config(entry),
        )
        url = discovery.resolve_discovery_request(config, provider).url
        cache_key = (provider, url)

        if not refresh:
            cached = discovery.MODEL_CACHE.get(cache_key)
            if cached is not None:
                models, truncated, details = cached
                connection.send_result(msg["id"], {
                    "provider": provider, "models": list(models),
                    "model_details": list(details),
                    "cached": True, "truncated": truncated,
                })
                return

        # Taken before the fetch: an invalidation while it runs (a credential
        # or endpoint change) stops this result from being cached.
        generation = discovery.MODEL_CACHE.generation(provider)
        models, truncated, details = await _fetch_models_deduped(
            hass, provider, config, cache_key)
        discovery.MODEL_CACHE.set(
            cache_key, (list(models), truncated, list(details or [])),
            generation=generation)
        connection.send_result(msg["id"], {
            "provider": provider, "models": models,
            "model_details": details,
            "cached": False, "truncated": truncated,
        })
    except Exception as exc:
        discovery.log_discovery_failure(provider, url, exc, logger=_LOGGER)
        connection.send_result(
            msg["id"], {
                "provider": provider,
                "models": [],
                "error": _SAFE_MODEL_DISCOVERY_ERROR,
            },
        )


_AI_ROLE_FIELDS = (
    ("Main Agent", "llm_provider", "model"),
    ("Classifier", "classifier_provider", "classifier_model"),
    ("Reasoning", "reasoning_provider", "reasoning_model"),
    ("Vision", "vision_provider", "vision_model"),
    ("Camera Reasoning", "camera_reasoning_provider", "camera_reasoning_model"),
    ("Suggestion Review", "suggestion_review_provider", "suggestion_review_model"),
)


_AI_APPLY_KEYS = frozenset({
    key for _label, provider_key, model_key in _AI_ROLE_FIELDS
    for key in (provider_key, model_key)
} | {
    "ollama_base_url", "custom_base_url", "ollama_num_ctx",
    "home_context_max_entities",
})


_AI_PROVIDERS = frozenset({"groq", "openai", "gemini", "anthropic", "ollama", "custom"})


_AI_CLOUD_PROVIDERS = frozenset({"groq", "openai", "gemini", "anthropic"})


def _prepare_ai_config_updates(updates: dict) -> dict:
    """Validate and normalise one staged AI-settings transaction."""
    from .llm_provider import normalize_provider_endpoint

    if not isinstance(updates, dict) or not updates:
        raise NovaValidationError("No AI settings were supplied")
    unknown = set(updates) - _AI_APPLY_KEYS
    if unknown:
        raise NovaValidationError("The request contains unsupported AI settings")

    clean: dict[str, Any] = {}
    provider_keys = {provider_key for _, provider_key, _ in _AI_ROLE_FIELDS}
    model_keys = {model_key for _, _, model_key in _AI_ROLE_FIELDS}
    for key, value in updates.items():
        if key in provider_keys:
            provider = str(value or "").strip().lower()
            if provider not in _AI_PROVIDERS:
                raise NovaValidationError(f"Unsupported provider for {key}")
            clean[key] = provider
        elif key in model_keys:
            model = str(value or "").strip()
            if not model or len(model) > 512:
                raise NovaValidationError(f"A valid model is required for {key}")
            clean[key] = model
        elif key in ("ollama_base_url", "custom_base_url"):
            provider = key.removesuffix("_base_url")
            clean[key] = normalize_provider_endpoint(str(value or ""), provider)
        elif key == "ollama_num_ctx":
            if isinstance(value, bool):
                raise NovaValidationError("Ollama context length must be a number")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise NovaValidationError("Ollama context length must be a number") from exc
            if not 512 <= number <= 262144:
                raise NovaValidationError("Ollama context length must be between 512 and 262144")
            clean[key] = number
        elif key == "home_context_max_entities":
            if isinstance(value, bool):
                raise NovaValidationError("Prompt size must be a number")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise NovaValidationError("Prompt size must be a number") from exc
            if not 0 <= number <= 50:
                raise NovaValidationError("Prompt size must be between 0 and 50")
            clean[key] = number
    return clean


# nova/update_config's check for a provider or model key (8.7.23): the same
# rules as nova/apply_ai_config, by running that command's own validator on
# the one key. review_provider and review_model are not apply keys, so they
# are checked as the Main Agent's provider and model are.
_PANEL_AI_ALIASES = {"review_provider": "llm_provider", "review_model": "model"}
PANEL_AI_VALUE_KEYS = frozenset(
    {key for _label, provider_key, model_key in _AI_ROLE_FIELDS
     for key in (provider_key, model_key)} | set(_PANEL_AI_ALIASES))
# Endpoints are only written by nova/apply_ai_config, which normalises them,
# checks the destination and tests the connection. The panel never sends
# them through nova/update_config.
PANEL_AI_ENDPOINT_KEYS = frozenset({"llm_base_url", "ollama_base_url", "custom_base_url"})


def panel_ai_value(key: str, value):
    """The cleaned value for a provider or model key, or NovaValidationError
    with the same reason nova/apply_ai_config gives."""
    probe = _PANEL_AI_ALIASES.get(key, key)
    try:
        return _prepare_ai_config_updates({probe: value})[probe]
    except NovaValidationError:
        if probe == key:
            raise
        if key.endswith("_provider"):
            raise NovaValidationError(f"Unsupported provider for {key}") from None
        raise NovaValidationError(f"A valid model is required for {key}") from None


# A saved URL is shown on the panel with its password masked (websocket.py
# _masked_url). A value still holding that mask is the shown text sent back,
# not a real address, so it is refused and the saved value is kept (8.7.24).
HIDDEN_PASSWORD_MESSAGE = "This field shows a hidden password. Type the full address to change it."


def shows_hidden_password(value) -> bool:
    """Whether ``value`` contains the mask the panel shows for a secret."""
    return isinstance(value, str) and REDACTED in value


# Why an endpoint was refused, as fixed panel text (8.7.23). The keys are
# the fixed reasons providers.routing.normalize_provider_endpoint raises;
# the exception's own text is only matched, never sent.
_ENDPOINT_REASONS = {
    "endpoint is too long": "The endpoint is too long.",
    "endpoint must use http or https": "The endpoint must start with http:// or https://.",
    "endpoint must include a host": "The endpoint must include a host name or address.",
    "endpoint credentials must be stored separately":
        "The endpoint must not include a user name or password. "
        "Add the key under Provider Credentials instead.",
    "endpoint must not include a query string or fragment":
        "The endpoint must not include a query string (?…) or a fragment (#…).",
    "endpoint has an invalid port": "The endpoint has an invalid port.",
}
_ENDPOINT_REASON_FALLBACK = "The endpoint is not a valid address."


def _value_error_message(exc: ValueError) -> str:
    """Panel text for a ValueError from the AI settings path: Nova's own
    validation message, a fixed endpoint reason, or a fixed fallback."""
    if isinstance(exc, NovaValidationError):
        return safe_error_message(exc)
    # Matched against the fixed reasons; the exception text itself is never
    # turned into panel text.
    reason = next((text for known, text in _ENDPOINT_REASONS.items()
                   if exc.args == (known,)), None)
    if reason is None:
        # Not a known reason: the detail goes to the Home Assistant log only.
        safe_error_message(exc, where="AI settings", log=True)
        return _ENDPOINT_REASON_FALLBACK
    return reason


def _validate_ai_candidate(candidate: dict) -> list[str]:
    """Return user-safe validation errors for the complete staged setup."""
    from .llm_provider import resolve_provider_credential

    errors: list[str] = []
    for label, provider_key, model_key in _AI_ROLE_FIELDS:
        # Suggestion Review (v7.126.0) uses the Main Agent's provider and
        # model until one is chosen for it; unset, it is not checked alone.
        if (provider_key == "suggestion_review_provider"
                and not str(candidate.get(provider_key) or "").strip()):
            continue
        provider = str(candidate.get(provider_key) or "").strip().lower()
        model = str(candidate.get(model_key) or "").strip()
        if provider not in _AI_PROVIDERS:
            errors.append(f"{label} needs a supported provider")
            continue
        if not model:
            errors.append(f"{label} needs a model")
        if provider in _AI_CLOUD_PROVIDERS and not resolve_provider_credential(
                candidate, provider):
            errors.append(f"Add the {provider} credential before applying")
        if provider in ("ollama", "custom"):
            try:
                endpoint = resolve_provider_endpoint(candidate, provider)
            except ValueError:
                endpoint = None
            if not endpoint:
                errors.append(f"Set the {provider} endpoint before applying")
    return list(dict.fromkeys(errors))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/test_provider_endpoint",
    vol.Required("provider"): vol.In(("ollama", "custom")),
    vol.Required("endpoint"): str,
})
@websocket_api.async_response
async def ws_test_provider_endpoint(hass: HomeAssistant, connection, msg) -> None:
    """Test an explicitly staged self-hosted endpoint without saving it.

    The staged endpoint is used for this command only. It must pass the
    destination policy (LAN and private addresses are fine; link-local and
    cloud metadata destinations are refused), every redirect is checked the
    same way, credentials never follow a redirect to another origin, and the
    request is bounded in time and size."""
    from .providers import discovery
    from .providers.destinations import check_url
    from .providers.errors import ProviderError, ProviderErrorKind

    provider = str(msg["provider"]).strip().lower()
    try:
        from . import nova_config
        from .llm_provider import normalize_provider_endpoint

        if shows_hidden_password(msg["endpoint"]):
            raise NovaValidationError(HIDDEN_PASSWORD_MESSAGE)
        endpoint = normalize_provider_endpoint(msg["endpoint"], provider)
        if not endpoint:
            raise NovaValidationError("Endpoint is required")
        try:
            await hass.async_add_executor_job(
                lambda: check_url(endpoint, resolve=True))
        except ProviderError as exc:
            raise NovaValidationError("Endpoint is not an allowed destination") from exc
        entry = _get_entry(hass)
        config = await hass.async_add_executor_job(
            nova_config.effective_config, entry)
        config[f"{provider}_base_url"] = endpoint
        models, truncated, details = await _fetch_models(hass, provider, config)
        connection.send_result(msg["id"], {
            "ok": True,
            "provider": provider,
            "endpoint": endpoint,
            "models": models,
            "model_details": details,
            "truncated": truncated,
        })
    except ValueError as exc:
        connection.send_result(msg["id"], {
            "ok": False, "error": "invalid_endpoint", "message": _value_error_message(exc),
        })
    except Exception as exc:
        if (isinstance(exc, ProviderError)
                and exc.kind is ProviderErrorKind.INVALID_ENDPOINT):
            # A redirect to a refused destination, or off-origin loops.
            connection.send_result(msg["id"], {
                "ok": False, "error": "invalid_endpoint",
                "message": "Endpoint is not an allowed destination",
            })
            return
        discovery.log_discovery_failure(provider, "", exc, logger=_LOGGER)
        connection.send_result(msg["id"], {
            "ok": False,
            "error": _SAFE_MODEL_DISCOVERY_ERROR,
            "message": "Could not connect to the endpoint or list its models.",
        })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/apply_ai_config",
    vol.Required("updates"): dict,
})
@websocket_api.async_response
async def ws_apply_ai_config(hass: HomeAssistant, connection, msg) -> None:
    """Validate, test, and atomically apply the complete AI configuration."""
    try:
        from . import nova_config
        from .llm_provider import (
            resolve_provider_credential,
            test_connection,
        )

        if isinstance(msg["updates"], dict) and any(
                shows_hidden_password(v) for v in msg["updates"].values()):
            raise NovaValidationError(HIDDEN_PASSWORD_MESSAGE)
        updates = _prepare_ai_config_updates(msg["updates"])
        entry = _get_entry(hass)
        if entry is None:
            connection.send_result(msg["id"], {
                "ok": False, "error": "no_entry", "message": "Nova is not loaded.",
            })
            return
        from .runtime import get_runtime, runtime_config_snapshot
        # Ownership first: no endpoint test or save for an entry without a
        # runtime (raises → apply_failed).
        runtime = get_runtime(entry)
        # The current view includes live panel values; the executor gets a
        # snapshot, never the live dict.
        current = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry,
            runtime_config_snapshot(entry, strict=True))
        candidate = dict(current)
        candidate.update(updates)
        # A successful staged apply migrates off the legacy shared endpoint.
        # This key is server-owned: the browser cannot write it through this
        # command, so one endpoint can never bleed into the other provider.
        candidate["llm_base_url"] = ""
        candidate["self_hosted_endpoints_migrated"] = True
        errors = _validate_ai_candidate(candidate)
        if errors:
            connection.send_result(msg["id"], {
                "ok": False, "error": "invalid_configuration",
                "message": errors[0], "errors": errors,
            })
            return

        tested: set[tuple[str, str, str]] = set()
        for label, provider_key, model_key in _AI_ROLE_FIELDS:
            provider = str(candidate.get(provider_key) or "")
            if provider not in ("ollama", "custom"):
                continue
            model = str(candidate.get(model_key) or "")
            endpoint = str(resolve_provider_endpoint(candidate, provider) or "")
            test_key = (provider, endpoint, model)
            if test_key in tested:
                continue
            tested.add(test_key)
            problem = await test_connection(
                hass,
                provider,
                resolve_provider_credential(candidate, provider),
                model,
                endpoint,
            )
            if problem is not None:
                connection.send_result(msg["id"], {
                    "ok": False,
                    "error": "connection_test_failed",
                    "message": f"{label} could not use {model} on {provider}.",
                })
                return

        persisted_updates = dict(updates)
        persisted_updates["llm_base_url"] = ""
        persisted_updates["self_hosted_endpoints_migrated"] = True
        persisted = await hass.async_add_executor_job(
            nova_config.set_many_atomic, persisted_updates)
        if not persisted:
            connection.send_result(msg["id"], {
                "ok": False, "error": "persist_failed",
                "message": "Nova could not save the AI settings.",
            })
            return

        runtime.runtime_config.update(persisted_updates)
        invalidate_model_cache()
        connection.send_result(msg["id"], {
            "ok": True,
            "message": "AI settings saved. Nova is reloading.",
        })
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    except ValueError as exc:
        connection.send_result(msg["id"], {
            "ok": False, "error": "invalid_configuration",
            "message": _value_error_message(exc),
        })
    except Exception as exc:
        _LOGGER.warning("ws_apply_ai_config failed: %s", type(exc).__name__)
        connection.send_result(msg["id"], {
            "ok": False, "error": "apply_failed",
            "message": "Nova could not apply the AI settings.",
        })


def _compute_provider_availability(
    credential_status: dict,
    endpoint_status: dict,
) -> dict:
    """Per-provider availability (Phase 3, v7.108.0), each rule independent
    of every other provider's own state:
      - a cloud provider is available only when ITS OWN credential exists;
      - custom and Ollama each need their own resolved endpoint.
    Never reads or infers from another provider's field."""
    return {
        "groq": bool(credential_status.get("groq")),
        "openai": bool(credential_status.get("openai")),
        "anthropic": bool(credential_status.get("anthropic")),
        "gemini": bool(credential_status.get("gemini")),
        "custom": bool(endpoint_status.get("custom")),
        "ollama": bool(endpoint_status.get("ollama")),
    }


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_credential_status",
})
@websocket_api.async_response
async def ws_get_credential_status(hass: HomeAssistant, connection, msg) -> None:
    """Whether each provider has a credential configured (`status`, booleans
    only, never a value — Phase 2, v7.107.0) and whether each is available
    to select (`available`, Phase 3, v7.108.0 — see
    _compute_provider_availability for the per-provider-type rule).
    Admin-gated like nova/list_models."""
    try:
        from . import ha_secrets, nova_config
        status = await ha_secrets.async_credential_status(hass)
        entry = _get_entry(hass)
        config = await hass.async_add_executor_job(
            nova_config.effective_config_with_runtime, entry,
            _executor_runtime_config(entry))
        endpoint_status = {}
        for provider in ("ollama", "custom"):
            try:
                endpoint_status[provider] = bool(
                    resolve_provider_endpoint(config, provider))
            except ValueError:
                endpoint_status[provider] = False
        available = _compute_provider_availability(status, endpoint_status)
        connection.send_result(msg["id"], {"status": status, "available": available})
    except Exception as exc:
        _LOGGER.warning("ws_get_credential_status failed: %s", type(exc).__name__)
        connection.send_result(msg["id"], {"status": {}, "available": {}})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/set_credential",
    vol.Required("provider"): str,
    vol.Required("value"): str,
})
@websocket_api.async_response
async def ws_set_credential(hass: HomeAssistant, connection, msg) -> None:
    """Store (or replace) one provider's credential (Phase 2, v7.107.0).
    Never echoes the value back — success is `ok` only. A blank value is
    rejected by ha_secrets.async_set_provider_credential itself, so an
    accidentally-empty submission from the panel can never erase a stored
    credential; clearing one is the separate, explicit nova/delete_credential."""
    try:
        from . import ha_secrets
        ok = await ha_secrets.async_set_provider_credential(
            hass, msg["provider"], msg["value"])
        if ok:
            # A cached model list fetched under the old (or no) credential
            # must not outlive the credential that produced it (Phase 3,
            # v7.108.0) — e.g. an empty/unauthenticated result cached before
            # a key was set.
            invalidate_model_cache(str(msg["provider"] or "").strip().lower())
        connection.send_result(msg["id"], {"ok": ok})
    except Exception as exc:
        _LOGGER.warning("ws_set_credential failed: %s", type(exc).__name__)
        connection.send_result(msg["id"], {"ok": False})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/delete_credential",
    vol.Required("provider"): str,
})
@websocket_api.async_response
async def ws_delete_credential(hass: HomeAssistant, connection, msg) -> None:
    """Explicitly remove one provider's stored credential (Phase 2, v7.107.0).
    The panel confirms with the administrator before ever sending this."""
    try:
        from . import ha_secrets
        ok = await ha_secrets.async_delete_provider_credential(hass, msg["provider"])
        if ok:
            invalidate_model_cache(str(msg["provider"] or "").strip().lower())
        connection.send_result(msg["id"], {"ok": ok})
    except Exception as exc:
        _LOGGER.warning("ws_delete_credential failed: %s", type(exc).__name__)
        connection.send_result(msg["id"], {"ok": False})
