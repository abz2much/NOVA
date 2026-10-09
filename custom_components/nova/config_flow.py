"""
Nova Config Flow.

HACS integration: the config flow is the primary setup path.
  1. Manual entry of a cloud API key OR a local LLM endpoint (the common case).
  2. If /config/nova/config.json already exists (a previous install — the
     panel's runtime config survives integration removal), auto-imports it so
     a re-install is zero-touch.
  3. Options flow: a 4-step Configure dialog (Core, Routing, Observer, Identity)
     for the common settings — the full set still lives in the Nova panel.

All runtime configuration is managed via the Nova panel and
persisted by nova_config.py. The HA config entry is just the
bootstrap shell that registers the conversation platform.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_API_KEY,
    CONF_HONORIFIC,
    CONF_MODEL,
    CONF_DIRECTIVE,
    CONF_DIRECTIVE_PRESET,
    CONF_USE_HASS_API,
    CONF_BEDROOM_AREAS,
    CONF_GROUND_FLOOR_AREAS,
    CONF_BROADCAST_GROUP,
    CONF_NOTIFY_SERVICE,
    CONF_OBSERVER_ENABLED,
    CONF_CLASSIFIER_MODEL,
    CONF_REASONING_MODEL,
    CONF_REVIEW_MODEL,
    CONF_OBSERVER_QUIET_START,
    CONF_OBSERVER_QUIET_END,
    DEFAULT_HONORIFIC,
    DEFAULT_MODEL,
    DEFAULT_DIRECTIVE_PRESET,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_REASONING_MODEL,
    DEFAULT_REVIEW_MODEL,
    DEFAULT_OBSERVER_QUIET_START,
    DEFAULT_OBSERVER_QUIET_END,
    DIRECTIVE_PRESETS,
    HONORIFIC_OPTIONS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

def _find_config(config_path: str) -> dict | None:
    """Read an existing runtime config at config_path, if one with a usable
    LLM exists. This is the panel's runtime config — survives integration
    removal, so a re-install can pick everything back up without re-entry.
    (v6.45.0: the legacy add-on path /config/nova_config.json is no longer
    read.) config_path is resolved by the caller via hass.config.path(),
    since a bare module constant can't see which directory THIS Home
    Assistant instance was actually configured with."""
    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                data = json.load(f)
            provider = str(data.get("llm_provider") or "").strip().lower()
            has_endpoint = any(str(data.get(key) or "").strip() for key in (
                "ollama_base_url", "custom_base_url", "llm_base_url",
            ))
            if (data.get(CONF_API_KEY) or data.get("groq_api_key")
                    or has_endpoint or provider in ("ollama", "custom")):
                return data
    except Exception:
        pass
    return None


_OLLAMA_FIELD = "ollama_base_url"
# Shown as a placeholder: Home Assistant's checks reject URLs written
# directly in translation text.
_OLLAMA_EXAMPLE = "http://192.168.1.50:11434"


def _saved_keys() -> dict[str, str]:
    """Nova keys already in secrets.yaml, for example from an earlier
    install. Blocking: run it in the executor."""
    from . import ha_secrets, setup_roles
    from .const import PROVIDER_API_KEY_FIELDS
    found: dict[str, str] = {}
    for provider in setup_roles.CLOUD:
        value = ha_secrets.get_secret_sync(
            ha_secrets.secret_key_for(PROVIDER_API_KEY_FIELDS[provider]), None)
        if value:
            found[provider] = str(value).strip()
    return found


class NovaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle Nova config flow — auto-imports an existing runtime config."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[str, str] = {}
        self._typed: set[str] = set()
        self._lists: dict[str, tuple[list[str], list[dict]]] = {}
        self._saved_failed: list[str] = []
        self._roles: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> dict:
        """Screen 1: a key per cloud provider, and an Ollama address.

        A reinstall with an old config.json still imports it with no
        screens. Keys already in secrets.yaml are found and kept."""
        from . import paths, setup_roles

        # The screens run before Nova is loaded, so set Nova's paths first.
        paths.configure(self.hass)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        config_path = self.hass.config.path("nova", "config.json")
        cfg = await self.hass.async_add_executor_job(_find_config, config_path)
        if cfg:
            return await self.async_step_import(cfg)

        saved = await self.hass.async_add_executor_job(_saved_keys)
        errors: dict[str, str] = {}
        values: dict[str, Any] = {}
        if user_input is not None:
            errors = await self._check_entries(user_input, saved)
            if not errors:
                return await self.async_step_roles()
            # Keep what passed; never send a failing key back to the browser.
            # The Ollama address is not secret, so it stays for correcting.
            values = {k: ("" if k in errors and k.endswith("_api_key") else v)
                      for k, v in user_input.items()}

        return self.async_show_form(
            step_id="user",
            data_schema=self._keys_schema(values),
            errors=errors,
            description_placeholders={
                "saved": ", ".join(setup_roles.PROVIDER_LABELS[p]
                                   for p in setup_roles.CLOUD if p in saved) or "—",
                "example": _OLLAMA_EXAMPLE,
            },
        )

    async def _check_entries(self, user_input: dict[str, Any],
                             saved: dict[str, str]) -> dict[str, str]:
        """Test every typed key, saved key and the Ollama address at the
        same time. Fills self._entries, _typed, _lists and _saved_failed.
        Returns the screen's errors (empty when it may move on)."""
        from . import setup_roles
        from .const import PROVIDER_API_KEY_FIELDS
        from .llm_provider import normalize_provider_endpoint

        self._lists, self._saved_failed = {}, []
        errors: dict[str, str] = {}
        typed = {p: str(user_input.get(PROVIDER_API_KEY_FIELDS[p]) or "").strip()
                 for p in setup_roles.CLOUD}
        typed = {p: k for p, k in typed.items() if k}
        entries = {**{p: k for p, k in saved.items() if p not in typed}, **typed}
        url = str(user_input.get(_OLLAMA_FIELD) or "").strip()
        if url:
            try:
                entries["ollama"] = normalize_provider_endpoint(url, "ollama")
            except ValueError:
                errors[_OLLAMA_FIELD] = "cannot_connect"
        if not entries and not errors:
            return {"base": "need_llm"}

        results = await asyncio.gather(
            *(self._check(p, v, p in typed) for p, v in entries.items()))
        for (provider, _value), (err, models, details) in zip(entries.items(), results):
            if err is None:
                self._lists[provider] = (models, details)
            elif provider in typed:
                errors[PROVIDER_API_KEY_FIELDS[provider]] = err
            elif provider == "ollama":
                errors[_OLLAMA_FIELD] = err
            else:
                self._saved_failed.append(provider)
        if not errors and not self._lists:
            errors["base"] = "no_working_provider"
        if not errors:
            self._entries = {p: v for p, v in entries.items() if p in self._lists}
            self._typed = {p for p in typed if p in self._lists}
        return errors

    async def _check(self, provider: str, value: str, typed: bool
                     ) -> tuple[str | None, list[str], list[dict]]:
        """(error key or None, models, details) for one entry. A cloud key
        whose list cannot be read, or is empty, is tested with the default
        model. Ollama has no default model, so its list must be readable."""
        from . import setup_probe, setup_roles
        listed = await self._discover(provider, value)
        if listed is not None and (listed[0] or provider == "ollama"):
            return None, listed[0], listed[1]
        if provider == "ollama":
            return "cannot_connect", [], []
        from .llm_provider import DEFAULT_MODELS
        kind = await setup_probe.probe_model(
            self.hass, setup_probe.Job(provider, DEFAULT_MODELS[provider]), value, None)
        # A key that works but cannot use the default model passes here with
        # an empty list, so screen 3 shows a text box for the model.
        return (None if kind is None else setup_roles.key_error(kind, typed)), [], []

    def _keys_schema(self, values: dict[str, Any]) -> vol.Schema:
        from . import setup_roles
        from .const import PROVIDER_API_KEY_FIELDS
        fields: dict[Any, Any] = {}
        for provider in setup_roles.CLOUD:
            key = PROVIDER_API_KEY_FIELDS[provider]
            fields[vol.Optional(key, description={"suggested_value": values.get(key, "")})] = \
                selector.TextSelector(selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD))
        fields[vol.Optional(_OLLAMA_FIELD, description={
            "suggested_value": values.get(_OLLAMA_FIELD, "")})] = str
        return vol.Schema(fields)

    async def _discover(self, provider: str, value: str
                        ) -> tuple[list[str], list[dict]] | None:
        """The provider's model ids and details, or None when the list
        could not be read. Never raises."""
        try:
            from homeassistant.helpers import aiohttp_client
            from .const import PROVIDER_API_KEY_FIELDS
            from .providers import discovery

            config: dict[str, Any] = {"llm_provider": provider}
            if provider == "ollama":
                config["ollama_base_url"] = value
            else:
                config[PROVIDER_API_KEY_FIELDS[provider]] = value
            request = discovery.resolve_discovery_request(config, provider)
            session = aiohttp_client.async_get_clientsession(self.hass)
            result = await discovery.fetch_models(self.hass, session, request)
            models, _truncated, details = result.as_tuple()
            return [str(m) for m in models], list(details)
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            _LOGGER.debug("Nova setup: %s model list unavailable (%s)",
                          provider, type(exc).__name__)
            return None

    async def async_step_roles(
        self, user_input: dict[str, Any] | None = None,
    ) -> dict:
        """Screen 2: the provider for each role, from the ones that passed."""
        from . import setup_roles

        passed = list(self._lists)
        if user_input is not None:
            self._roles = {role: str(user_input.get(role) or "")
                           for role in setup_roles.ROLES}
            return await self.async_step_models()

        details = self._lists.get("ollama", ([], []))[1]
        ollama_vision = setup_roles.ollama_has(details, "vision")
        fields: dict[Any, Any] = {}
        for role in setup_roles.ROLES:
            fields[vol.Required(role, default=setup_roles.default_provider(
                role, passed, ollama_vision))] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=setup_roles.provider_options(role, passed),
                        translation_key="setup_provider",
                        mode=selector.SelectSelectorMode.DROPDOWN))
        return self.async_show_form(
            step_id="roles",
            data_schema=vol.Schema(fields),
            description_placeholders={"failed": ", ".join(
                setup_roles.PROVIDER_LABELS[p] for p in self._saved_failed) or "—"},
        )

    async def async_step_models(
        self, user_input: dict[str, Any] | None = None,
    ) -> dict:
        """Screen 3: the model for each role, tested on submit. Keys are
        written only after every test passed and the "already set up"
        check ran again."""
        from . import setup_probe, setup_roles

        roles = [r for r in setup_roles.ROLES
                 if not (r == "vision" and self._roles.get(r) == setup_roles.NOT_NOW)]
        errors: dict[str, str] = {}
        values: dict[str, str] = {}
        if user_input is not None:
            values = {f"{r}_model": str(user_input.get(f"{r}_model") or "").strip()
                      for r in roles}
            chosen = {r: values[f"{r}_model"] for r in roles if values[f"{r}_model"]}
            errors = {f"{r}_model": "model_required"
                      for r in roles if r != "vision" and r not in chosen}
            if not errors:
                jobs = {r: setup_probe.Job(self._roles[r], m, picture=(r == "vision"))
                        for r, m in chosen.items()}
                creds = {p: self._credential(p) for p in {j.provider for j in jobs.values()}}
                results = await setup_probe.probe_all(self.hass, jobs, creds)
                errors = {f"{r}_model": setup_roles.error_key(kind, r == "vision")
                          for r, kind in results.items() if kind is not None}
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                if not await self._save_keys():
                    errors = {"base": "secrets_write_failed"}
                elif not await self.hass.async_add_executor_job(
                        self._apply_to_config, chosen):
                    errors = {"base": "config_write_failed"}
                else:
                    return self.async_create_entry(
                        title="Nova", data=self._entry_data(chosen))

        return self.async_show_form(
            step_id="models",
            data_schema=self._models_schema(roles, values),
            errors=errors,
            description_placeholders={
                f"{r}_provider": setup_roles.PROVIDER_LABELS.get(
                    self._roles.get(r, ""), self._roles.get(r, ""))
                for r in setup_roles.ROLES},
        )

    def _credential(self, provider: str) -> tuple[str, str | None]:
        if provider == "ollama":
            return "", self._entries["ollama"]
        return self._entries[provider], None

    def _models_schema(self, roles: list[str], values: dict[str, str]) -> vol.Schema:
        from . import setup_roles
        from .llm_provider import DEFAULT_MODELS
        fields: dict[Any, Any] = {}
        for role in roles:
            provider = self._roles[role]
            models, details = self._lists.get(provider, ([], []))
            key = f"{role}_model"
            suggested = values[key] if key in values else setup_roles.default_model(
                role, provider, DEFAULT_MODELS, details)
            marker = vol.Optional if role == "vision" else vol.Required
            field: Any = selector.SelectSelector(selector.SelectSelectorConfig(
                options=models, custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN)) if models \
                else selector.TextSelector()
            fields[marker(key, description={"suggested_value": suggested})] = field
        return vol.Schema(fields)

    async def _save_keys(self) -> bool:
        """Write the keys typed on screen 1 to secrets.yaml. Saved keys
        are already there and are not rewritten."""
        from . import ha_secrets
        for provider in sorted(self._typed):
            if not await ha_secrets.async_set_provider_credential(
                    self.hass, provider, self._entries[provider]):
                return False
        return True

    def _apply_to_config(self, chosen: dict[str, str]) -> bool:
        """Write the AI choices into config.json as well. A reinstall can
        leave an old config.json behind, and config.json wins over the
        entry, so without this the old choices would beat the new ones.
        Also clears the old "welcome shown" flag, old per role addresses and
        old suggestion review settings. Other settings, such as the
        honorific, are kept. Blocking: run it in the executor."""
        from . import nova_config, setup_roles
        nova_config.configure(self.hass)
        values: dict[str, Any] = {
            "welcome_pending": True,
            "self_hosted_endpoints_migrated": True,
            "ollama_base_url": self._entries.get("ollama", ""),
        }
        # Old per role addresses would beat the new Ollama address, and an old
        # suggestion review setting would switch it back on: first run starts
        # both fresh. The unused review tier is left alone.
        drop = ["welcome_shown", "conversation_base_url", "classifier_base_url",
                "reasoning_base_url", "suggestion_review_enabled",
                "suggestion_review_provider", "suggestion_review_model"]
        for role in setup_roles.ROLES:
            provider_key, model_key = setup_roles.ROLE_KEYS[role]
            if role in chosen:
                values[provider_key] = self._roles[role]
                values[model_key] = chosen[role]
            else:
                drop += [provider_key, model_key]
        # Delete first, save last: set_many saves the whole cache, so its
        # result also covers the deletions.
        for key in drop:
            nova_config.delete(key)
        return nova_config.set_many(values)

    def _entry_data(self, chosen: dict[str, str]) -> dict[str, Any]:
        from . import setup_roles
        data: dict[str, Any] = {
            CONF_HONORIFIC: DEFAULT_HONORIFIC,
            "schema_version": 7,
            # Fresh install only: welcome.py posts "Nova is ready" once.
            "welcome_pending": True,
            # Only Ollama's own address field is used; the old shared one is not.
            "self_hosted_endpoints_migrated": True,
        }
        if "ollama" in self._entries:
            data["ollama_base_url"] = self._entries["ollama"]
        for role, model in chosen.items():
            provider_key, model_key = setup_roles.ROLE_KEYS[role]
            data[provider_key] = self._roles[role]
            data[model_key] = model
        return data

    async def async_step_import(
        self, import_data: dict[str, Any],
    ) -> dict:
        """Create the entry from an existing runtime config (re-install)."""
        api_key = (
            import_data.get(CONF_API_KEY)
            or import_data.get("groq_api_key", "")
        ).strip()
        provider = import_data.get("llm_provider", "groq")
        from .llm_provider import resolve_provider_endpoint
        try:
            base_url = resolve_provider_endpoint(import_data, provider) or ""
        except ValueError:
            base_url = ""
        local_ok = provider in ("ollama", "custom") and bool(base_url)

        # An LLM is required, but a local model counts: proceed if we have either
        # a cloud key OR a local endpoint (provider=ollama/custom, or a base_url).
        if not api_key and not local_ok:
            _LOGGER.warning("Nova: config found but no API key and no local LLM")
            return self.async_abort(reason="import_failed")

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(
            updates={CONF_API_KEY: api_key}
        )

        _LOGGER.info("Nova: auto-configuring from existing runtime config (provider=%s%s)",
                     provider, ", local" if (not api_key and local_ok) else "")
        return self.async_create_entry(
            title="Nova",
            data={
                CONF_API_KEY: api_key,
                CONF_MODEL: import_data.get(CONF_MODEL, import_data.get("model", DEFAULT_MODEL)),
                CONF_HONORIFIC: import_data.get(CONF_HONORIFIC, import_data.get("honorific", DEFAULT_HONORIFIC)),
                "llm_provider": provider,
                "llm_base_url": base_url,
                "ollama_base_url": base_url if provider == "ollama" else "",
                "custom_base_url": base_url if provider == "custom" else "",
                "schema_version": 7,
            },
            options={k: v for k, v in import_data.items()
                     if k not in (CONF_API_KEY, "groq_api_key", "schema_version")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "NovaOptionsFlow":
        return NovaOptionsFlow(config_entry)


class NovaOptionsFlow(OptionsFlow):
    """
    Options flow — Nova is configurable from Settings → Devices &
    Services → Nova → Configure (in addition to the in-app panel).

    A menu lets you jump straight to the section you want instead of clicking
    through every screen: Core (persona/model/home control), Routing (bedroom
    areas, broadcast group, notify service), Observer (proactive awareness),
    and Identity (per-person recognition). Each section saves on its own and
    returns to the menu, so changing one setting no longer means walking the
    whole sequence.

    Collected values are written straight into nova_config (the runtime source
    of truth the panel and modules read) and persisted as entry options, which
    triggers a reload so they take effect immediately.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = {}

    def _cur(self, key: str, default: Any = None) -> Any:
        """Current value: runtime config first, then entry options/data."""
        try:
            from . import nova_config
            val = nova_config.get(key, None)
            if val not in (None, ""):
                return val
        except Exception:
            pass
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _sv(self, key: str, default: Any = None) -> dict:
        """suggested_value wrapper to pre-fill a field with its current value."""
        return {"suggested_value": self._cur(key, default)}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict:
        """Landing menu — jump to any section directly."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["core", "routing", "observer", "credentials", "identity", "email"],
        )

    async def async_step_core(self, user_input: dict[str, Any] | None = None) -> dict:
        """Core — persona, directive, conversation model, home control."""
        if user_input is not None:
            return await self._save_section(user_input)
        schema = vol.Schema({
            vol.Optional(CONF_HONORIFIC, description=self._sv(CONF_HONORIFIC, DEFAULT_HONORIFIC)):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=HONORIFIC_OPTIONS, custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Optional(CONF_DIRECTIVE_PRESET,
                         description=self._sv(CONF_DIRECTIVE_PRESET, DEFAULT_DIRECTIVE_PRESET)):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=list(DIRECTIVE_PRESETS.keys()),
                    mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Optional(CONF_DIRECTIVE, description=self._sv(CONF_DIRECTIVE, "")):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_MODEL, description=self._sv(CONF_MODEL, DEFAULT_MODEL)):
                selector.TextSelector(),
            vol.Optional(CONF_USE_HASS_API, description=self._sv(CONF_USE_HASS_API, True)):
                selector.BooleanSelector(),
        })
        return self.async_show_form(step_id="core", data_schema=schema)

    async def async_step_routing(self, user_input: dict[str, Any] | None = None) -> dict:
        """Routing — bedroom areas, broadcast group, phone notify service."""
        if user_input is not None:
            return await self._save_section(user_input)
        schema = vol.Schema({
            vol.Optional(CONF_BEDROOM_AREAS, description=self._sv(CONF_BEDROOM_AREAS, [])):
                selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
            # Scopes the sleeping-household intrusion check to a real breach
            # (an exterior door/window on these floors) instead of any indoor
            # motion — leave empty to fall back to checking every exterior
            # door/window, same as when Nova is away.
            vol.Optional(CONF_GROUND_FLOOR_AREAS, description=self._sv(CONF_GROUND_FLOOR_AREAS, [])):
                selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
            vol.Optional(CONF_BROADCAST_GROUP, description=self._sv(CONF_BROADCAST_GROUP, "")):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player")),
            vol.Optional(CONF_NOTIFY_SERVICE, description=self._sv(CONF_NOTIFY_SERVICE, "")):
                selector.TextSelector(),
        })
        return self.async_show_form(step_id="routing", data_schema=schema)

    async def async_step_observer(self, user_input: dict[str, Any] | None = None) -> dict:
        """Observer — proactive awareness (model tiers + quiet hours). Provider
        credentials (including Gemini) live in the Credentials section — never
        shown or re-collected here, so this step can't leak or re-plaintext one."""
        if user_input is not None:
            return await self._save_section(user_input)
        schema = vol.Schema({
            vol.Optional(CONF_OBSERVER_ENABLED, description=self._sv(CONF_OBSERVER_ENABLED, False)):
                selector.BooleanSelector(),
            vol.Optional(CONF_CLASSIFIER_MODEL,
                         description=self._sv(CONF_CLASSIFIER_MODEL, DEFAULT_CLASSIFIER_MODEL)):
                selector.TextSelector(),
            vol.Optional(CONF_REASONING_MODEL,
                         description=self._sv(CONF_REASONING_MODEL, DEFAULT_REASONING_MODEL)):
                selector.TextSelector(),
            vol.Optional(CONF_REVIEW_MODEL,
                         description=self._sv(CONF_REVIEW_MODEL, DEFAULT_REVIEW_MODEL)):
                selector.TextSelector(),
            vol.Optional(CONF_OBSERVER_QUIET_START,
                         description=self._sv(CONF_OBSERVER_QUIET_START, DEFAULT_OBSERVER_QUIET_START)):
                selector.TextSelector(),
            vol.Optional(CONF_OBSERVER_QUIET_END,
                         description=self._sv(CONF_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_END)):
                selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="observer",
            data_schema=schema,
        )

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> dict:
        """Credentials — one dedicated key per provider (Phase 2, v7.107.0).

        Values are never shown back: every field starts blank, so leaving it
        blank makes no change to that provider's stored credential (an empty
        form field can never erase one). To remove a credential, tick its
        "clear" box explicitly. Clearing the credential for the provider
        currently driving the Main Agent (Core → Model provider) additionally
        requires the confirmation box, so a stray click can't silently break
        the Main Agent.
        """
        from . import ha_secrets
        from .const import PROVIDER_API_KEY_FIELDS

        active_provider = str(self._cur("llm_provider", "groq") or "groq").strip().lower()
        errors: dict[str, str] = {}

        if user_input is not None:
            writes: dict[str, str] = {}
            deletes: list[str] = []
            for provider in PROVIDER_API_KEY_FIELDS:
                new_val = (user_input.get(f"{provider}_credential") or "").strip()
                clear = bool(user_input.get(f"clear_{provider}", False))
                if new_val and clear:
                    errors["base"] = "credential_set_and_clear"
                    break
                if new_val:
                    writes[provider] = new_val
                elif clear:
                    deletes.append(provider)

            if not errors:
                deleting_active = active_provider in deletes and active_provider not in writes
                confirmed = bool(user_input.get("confirm_delete_active", False))
                if deleting_active and not confirmed:
                    errors["base"] = "confirm_required_for_active_provider"
                else:
                    for provider, value in writes.items():
                        await ha_secrets.async_set_provider_credential(self.hass, provider, value)
                    for provider in deletes:
                        await ha_secrets.async_delete_provider_credential(self.hass, provider)
                    return self.async_create_entry(title="", data={**self._entry.options})

        status = {
            provider: await self.hass.async_add_executor_job(
                ha_secrets.has_provider_credential_sync, provider)
            for provider in PROVIDER_API_KEY_FIELDS
        }
        schema_dict: dict = {}
        for provider in PROVIDER_API_KEY_FIELDS:
            schema_dict[vol.Optional(f"{provider}_credential", default="")] = \
                selector.TextSelector(selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD))
            schema_dict[vol.Optional(f"clear_{provider}", default=False)] = \
                selector.BooleanSelector()
        schema_dict[vol.Optional("confirm_delete_active", default=False)] = \
            selector.BooleanSelector()

        status_note = ", ".join(
            f"{provider} {'✓' if configured else '✗'}"   # no words, so it reads in every language
            for provider, configured in status.items()
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "status": status_note,
                "active": active_provider,
            },
        )

    async def async_step_identity(self, user_input: dict[str, Any] | None = None) -> dict:
        """Identity — per-person recognition; voice fingerprint tier needs a GPU."""
        if user_input is not None:
            return await self._save_section(user_input)
        schema = vol.Schema({
            vol.Optional("identity_enabled", description=self._sv("identity_enabled", True)):
                selector.BooleanSelector(),
            vol.Optional("identity_voice_fingerprint",
                         description=self._sv("identity_voice_fingerprint", False)):
                selector.BooleanSelector(),
            vol.Optional("voice_recognition_source",
                         description=self._sv("voice_recognition_source", "")):
                selector.TextSelector(),
            vol.Optional("voice_recognition_auto_enroll",
                         description=self._sv("voice_recognition_auto_enroll", True)):
                selector.BooleanSelector(),
            vol.Optional("identity_min_confidence",
                         description=self._sv("identity_min_confidence", 0.45)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0.0, max=1.0, step=0.05, mode=selector.NumberSelectorMode.SLIDER)),
        })
        return self.async_show_form(step_id="identity", data_schema=schema)

    async def async_step_email(self, user_input: dict[str, Any] | None = None) -> dict:
        """Email — read-only IMAP inbox access. The password lives in HA
        secrets.yaml (imap_secret_key), never in the panel config."""
        if user_input is not None:
            return await self._save_section(user_input)
        schema = vol.Schema({
            vol.Optional("imap_enabled", description=self._sv("imap_enabled", False)):
                selector.BooleanSelector(),
            vol.Optional("imap_host", description=self._sv("imap_host", "")):
                selector.TextSelector(),
            vol.Optional("imap_port", description=self._sv("imap_port", 993)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX)),
            vol.Optional("imap_user", description=self._sv("imap_user", "")):
                selector.TextSelector(),
            vol.Optional("imap_folder", description=self._sv("imap_folder", "INBOX")):
                selector.TextSelector(),
            vol.Optional("imap_ssl", description=self._sv("imap_ssl", True)):
                selector.BooleanSelector(),
            vol.Optional("imap_secret_key",
                         description=self._sv("imap_secret_key", "nova_imap_password")):
                selector.TextSelector(),
        })
        return self.async_show_form(step_id="email", data_schema=schema)

    async def _save_section(self, user_input: dict[str, Any]) -> dict:
        """Persist one section's values to runtime config + entry options, then
        finish. Each section saves independently (menu-based flow), so only the
        edited keys are written — other sections' stored values are untouched."""
        self._data.update(user_input)
        try:
            from . import nova_config
            await self.hass.async_add_executor_job(nova_config.set_many, dict(user_input))
        except Exception as exc:
            _LOGGER.warning("Nova options: nova_config write failed: %s", exc)
        return self.async_create_entry(title="", data={**self._entry.options, **self._data})
