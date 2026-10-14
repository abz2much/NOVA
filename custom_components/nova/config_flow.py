"""Nova's setup dialog and its Configure dialog.

Setup (Settings, Add integration) is three screens: AI keys and an Ollama
address, a provider for each role, then a model for each role. Every entry
is tested before the dialog moves on, and nothing is written until the last
screen's tests pass. A config.json left behind by an earlier install is
imported with no screens at all, and the Nova Voice ID app is picked up by
Supervisor discovery.

The Configure dialog is a menu of small sections. Each one saves on its own
into Nova's runtime settings (nova_config) and the entry's options. The Nova
panel remains the full settings surface; the config entry is mainly the
shell that lets Home Assistant load Nova."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_API_KEY,
    CONF_BEDROOM_AREAS,
    CONF_BROADCAST_GROUP,
    CONF_CLASSIFIER_MODEL,
    CONF_DIRECTIVE,
    CONF_DIRECTIVE_PRESET,
    CONF_GROUND_FLOOR_AREAS,
    CONF_HONORIFIC,
    CONF_MODEL,
    CONF_NOTIFY_SERVICE,
    CONF_OBSERVER_ENABLED,
    CONF_OBSERVER_QUIET_END,
    CONF_OBSERVER_QUIET_START,
    CONF_REASONING_MODEL,
    CONF_REVIEW_MODEL,
    CONF_USE_HASS_API,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_DIRECTIVE_PRESET,
    DEFAULT_HONORIFIC,
    DEFAULT_MODEL,
    DEFAULT_OBSERVER_QUIET_END,
    DEFAULT_OBSERVER_QUIET_START,
    DEFAULT_REASONING_MODEL,
    DEFAULT_REVIEW_MODEL,
    DIRECTIVE_PRESETS,
    DOMAIN,
    HONORIFIC_OPTIONS,
    PROVIDER_API_KEY_FIELDS,
)
from .migrations import CURRENT_SCHEMA_VERSION

_LOGGER = logging.getLogger(__name__)

# Screen 1's one field that is not a key.
_ADDRESS_FIELD = "ollama_base_url"
# Passed as a placeholder because hassfest refuses a URL typed straight
# into translation text.
_ADDRESS_EXAMPLE = "http://192.168.1.50:11434"
_NONE_LISTED = "—"

# Settings an earlier install may have left in config.json that would undo
# a fresh setup: the welcome flag, per role addresses (they would beat the
# new Ollama address) and the suggestion review switch. The review tier,
# which setup never asks about, is deliberately left alone.
_STALE_SETTINGS = (
    "welcome_shown",
    "conversation_base_url", "classifier_base_url", "reasoning_base_url",
    "suggestion_review_enabled", "suggestion_review_provider", "suggestion_review_model",
)
# Never copied from an imported config.json into the entry's options.
_NOT_CARRIED = (CONF_API_KEY, "groq_api_key", "schema_version")


def _find_config(config_path: str) -> dict | None:
    """The runtime config.json at `config_path` when it names an LLM Nova
    can use, else None.

    The file outlives the integration, so a reinstall can be set up from it
    with no screens. Usable means a cloud key, any self hosted address, or a
    self hosted provider. The caller builds the path with hass.config.path().
    Tests run this function on its own with only json, os and CONF_API_KEY
    in scope, so it must not reach for anything else."""
    try:
        if not os.path.exists(config_path):
            return None
        with open(config_path) as handle:
            found = json.load(handle)
        self_hosted = str(found.get("llm_provider") or "").strip().lower() in ("ollama", "custom")
        addressed = any(str(found.get(name) or "").strip()
                        for name in ("ollama_base_url", "custom_base_url", "llm_base_url"))
        keyed = found.get(CONF_API_KEY) or found.get("groq_api_key")
        if keyed or addressed or self_hosted:
            return found
    except Exception:  # noqa: BLE001 - an unreadable file simply is not imported
        return None
    return None


def _saved_keys() -> dict[str, str]:
    """Cloud keys already in secrets.yaml (an earlier install), by provider.
    Reads a file, so call it through the executor."""
    from . import ha_secrets, setup_roles

    keys: dict[str, str] = {}
    for provider in setup_roles.CLOUD:
        name = ha_secrets.secret_key_for(PROVIDER_API_KEY_FIELDS[provider])
        stored = ha_secrets.get_secret_sync(name, None)
        if stored:
            keys[provider] = str(stored).strip()
    return keys


def _suggest(value: Any) -> dict[str, Any]:
    """A field description that pre-fills the field without forcing it."""
    return {"suggested_value": value}


def _listed(names: list[str]) -> str:
    return ", ".join(names) or _NONE_LISTED


def _secret_box() -> Any:
    return selector.TextSelector(selector.TextSelectorConfig(
        type=selector.TextSelectorType.PASSWORD))


def _sort_credentials(user_input: dict[str, Any]) -> tuple[str | None, dict[str, str], list[str]]:
    """Split the credentials form into (error, keys to write, providers to
    clear). A provider with both a new key and its clear box ticked is an
    error, and nothing is returned with it."""
    writes: dict[str, str] = {}
    clears: list[str] = []
    for provider in PROVIDER_API_KEY_FIELDS:
        value = (user_input.get(f"{provider}_credential") or "").strip()
        wipe = bool(user_input.get(f"clear_{provider}", False))
        if value and wipe:
            return "credential_set_and_clear", {}, []
        if value:
            writes[provider] = value
        elif wipe:
            clears.append(provider)
    return None, writes, clears


class NovaConfigFlow(ConfigFlow, domain=DOMAIN):
    """The setup dialog, the reinstall import and Voice ID discovery."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        # Screen 1's results: the working value per provider (a key, or the
        # normalised Ollama address), which of them were typed rather than
        # saved, each one's model list, and saved keys that failed.
        self._entries: dict[str, str] = {}
        self._typed: set[str] = set()
        self._lists: dict[str, tuple[list[str], list[dict]]] = {}
        self._saved_failed: list[str] = []
        # Screen 2's provider per role.
        self._roles: dict[str, str] = {}

    # ── screen 1: keys and the Ollama address ──

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> dict:
        """Keys per cloud provider and an Ollama address. A reinstall with a
        usable config.json is imported instead, and keys already saved in
        secrets.yaml are found and offered without retyping."""
        from . import paths, setup_roles

        # Nova is not loaded while these screens run, so point it at this
        # instance's folders before anything is read.
        paths.configure(self.hass)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        leftover = await self.hass.async_add_executor_job(
            _find_config, self.hass.config.path("nova", "config.json"))
        if leftover:
            return await self.async_step_import(leftover)

        saved = await self.hass.async_add_executor_job(_saved_keys)
        errors: dict[str, str] = {}
        shown: dict[str, Any] = {}
        if user_input is not None:
            errors = await self._test_entries(user_input, saved)
            if not errors:
                return await self.async_step_roles()
            # A key that failed is never sent back to the browser. Keys that
            # passed, and the address (not a secret), stay for correcting.
            shown = {
                field: "" if field in errors and field.endswith("_api_key") else value
                for field, value in user_input.items()
            }

        found = [setup_roles.PROVIDER_LABELS[p] for p in setup_roles.CLOUD if p in saved]
        return self.async_show_form(
            step_id="user",
            data_schema=self._key_fields(shown),
            errors=errors,
            description_placeholders={"saved": _listed(found), "example": _ADDRESS_EXAMPLE},
        )

    def _key_fields(self, shown: dict[str, Any]) -> vol.Schema:
        from . import setup_roles

        fields: dict[Any, Any] = {}
        for provider in setup_roles.CLOUD:
            name = PROVIDER_API_KEY_FIELDS[provider]
            fields[vol.Optional(name, description=_suggest(shown.get(name, "")))] = _secret_box()
        address = vol.Optional(_ADDRESS_FIELD, description=_suggest(shown.get(_ADDRESS_FIELD, "")))
        fields[address] = str
        return vol.Schema(fields)

    async def _test_entries(self, user_input: dict[str, Any],
                            saved: dict[str, str]) -> dict[str, str]:
        """Test every typed key, every saved key that was not retyped, and the
        Ollama address, all at once. Returns screen 1's errors; when there are
        none, the passing entries are kept for the later screens."""
        from . import setup_roles
        from .llm_provider import normalize_provider_endpoint

        self._lists = {}
        self._saved_failed = []
        typed: dict[str, str] = {}
        for provider in setup_roles.CLOUD:
            key = str(user_input.get(PROVIDER_API_KEY_FIELDS[provider]) or "").strip()
            if key:
                typed[provider] = key
        # Saved keys first, then typed ones, then Ollama.
        candidates = {p: key for p, key in saved.items() if p not in typed}
        candidates.update(typed)

        errors: dict[str, str] = {}
        address = str(user_input.get(_ADDRESS_FIELD) or "").strip()
        if address:
            try:
                candidates["ollama"] = normalize_provider_endpoint(address, "ollama")
            except ValueError:
                errors[_ADDRESS_FIELD] = "cannot_connect"
        if not candidates and not errors:
            return {"base": "need_llm"}

        names = list(candidates)
        outcomes = await asyncio.gather(
            *(self._test_one(name, candidates[name], name in typed) for name in names))
        for provider, (problem, models, details) in zip(names, outcomes):
            if problem is None:
                self._lists[provider] = (models, details)
            elif provider in typed:
                errors[PROVIDER_API_KEY_FIELDS[provider]] = problem
            elif provider == "ollama":
                errors[_ADDRESS_FIELD] = problem
            else:
                # A stale saved key is left out and named on screen 2; it
                # never holds the user up.
                self._saved_failed.append(provider)
        if errors:
            return errors
        if not self._lists:
            return {"base": "no_working_provider"}
        self._entries = {p: value for p, value in candidates.items() if p in self._lists}
        self._typed = {p for p in typed if p in self._lists}
        return {}

    async def _test_one(self, provider: str, value: str,
                        typed: bool) -> tuple[str | None, list[str], list[dict]]:
        """(error key or None, model ids, model details) for one entry.

        A readable, non-empty list is proof enough. Ollama has no default
        model, so for it any readable list passes and an unreadable one
        fails. A cloud key otherwise gets one test call with its default
        model."""
        from . import setup_probe, setup_roles

        listing = await self._discover(provider, value)
        if listing is not None:
            models, details = listing
            if models or provider == "ollama":
                return None, models, details
        if provider == "ollama":
            return "cannot_connect", [], []

        from .llm_provider import DEFAULT_MODELS
        job = setup_probe.Job(provider, DEFAULT_MODELS[provider])
        kind = await setup_probe.probe_model(self.hass, job, value, None)
        if kind is None:
            return None, [], []
        # key_error may still pass a key that only lacks the default model;
        # screen 3 then shows a text box for the model.
        return setup_roles.key_error(kind, typed), [], []

    async def _discover(self, provider: str,
                        value: str) -> tuple[list[str], list[dict]] | None:
        """The provider's model ids and details, or None when the list
        cannot be read. Best effort: never raises."""
        try:
            from homeassistant.helpers import aiohttp_client
            from .providers import discovery

            field = _ADDRESS_FIELD if provider == "ollama" else PROVIDER_API_KEY_FIELDS[provider]
            request = discovery.resolve_discovery_request(
                {"llm_provider": provider, field: value}, provider)
            session = aiohttp_client.async_get_clientsession(self.hass)
            reply = await discovery.fetch_models(self.hass, session, request)
            ids, _truncated, details = reply.as_tuple()
            return [str(model_id) for model_id in ids], list(details)
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            _LOGGER.debug("Nova setup: %s model list unavailable (%s)",
                          provider, type(exc).__name__)
            return None

    # ── screen 2: a provider per role ──

    async def async_step_roles(self, user_input: dict[str, Any] | None = None) -> dict:
        """Which of the passing providers serves each role."""
        from . import setup_roles

        if user_input is not None:
            self._roles = {role: str(user_input.get(role) or "") for role in setup_roles.ROLES}
            return await self.async_step_models()

        passed = list(self._lists)
        _ids, ollama_details = self._lists.get("ollama", ([], []))
        reads_pictures = setup_roles.ollama_has(ollama_details, "vision")
        fields: dict[Any, Any] = {}
        for role in setup_roles.ROLES:
            preset = setup_roles.default_provider(role, passed, reads_pictures)
            fields[vol.Required(role, default=preset)] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=setup_roles.provider_options(role, passed),
                    translation_key="setup_provider",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ))
        stale = [setup_roles.PROVIDER_LABELS[p] for p in self._saved_failed]
        return self.async_show_form(
            step_id="roles",
            data_schema=vol.Schema(fields),
            description_placeholders={"failed": _listed(stale)},
        )

    # ── screen 3: a model per role, then save ──

    async def async_step_models(self, user_input: dict[str, Any] | None = None) -> dict:
        """The model for each role. Every model is tested on submit; keys and
        settings are written only once all pass and the "already set up"
        check has run again."""
        from . import setup_roles

        roles = [role for role in setup_roles.ROLES
                 if role != "vision" or self._roles.get(role) != setup_roles.NOT_NOW]
        errors: dict[str, str] = {}
        typed: dict[str, str] = {}
        if user_input is not None:
            for role in roles:
                typed[f"{role}_model"] = str(user_input.get(f"{role}_model") or "").strip()
            chosen = {role: typed[f"{role}_model"] for role in roles if typed[f"{role}_model"]}
            # Vision may be left empty: it is then set up later.
            errors = {f"{role}_model": "model_required"
                      for role in roles if role != "vision" and role not in chosen}
            if not errors:
                errors = await self._test_models(chosen)
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                problem = await self._store(chosen)
                if problem is None:
                    return self.async_create_entry(title="Nova", data=self._entry_data(chosen))
                errors = {"base": problem}

        names: dict[str, str] = {}
        for role in setup_roles.ROLES:
            provider = self._roles.get(role, "")
            names[f"{role}_provider"] = setup_roles.PROVIDER_LABELS.get(provider, provider)
        return self.async_show_form(
            step_id="models",
            data_schema=self._model_fields(roles, typed),
            errors=errors,
            description_placeholders=names,
        )

    def _model_fields(self, roles: list[str], typed: dict[str, str]) -> vol.Schema:
        from . import setup_roles
        from .llm_provider import DEFAULT_MODELS

        fields: dict[Any, Any] = {}
        for role in roles:
            provider = self._roles[role]
            ids, details = self._lists.get(provider, ([], []))
            name = f"{role}_model"
            if name in typed:
                start = typed[name]
            else:
                start = setup_roles.default_model(role, provider, DEFAULT_MODELS, details)
            marker = vol.Optional if role == "vision" else vol.Required
            if ids:
                widget: Any = selector.SelectSelector(selector.SelectSelectorConfig(
                    options=ids, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN))
            else:
                # No list to choose from: a free text box.
                widget = selector.TextSelector()
            fields[marker(name, description=_suggest(start))] = widget
        return vol.Schema(fields)

    async def _test_models(self, chosen: dict[str, str]) -> dict[str, str]:
        """One test call per chosen model (a picture for vision); the error
        for each role whose test failed."""
        from . import setup_probe, setup_roles

        jobs = {role: setup_probe.Job(self._roles[role], model, picture=role == "vision")
                for role, model in chosen.items()}
        creds = {job.provider: self._credentials_for(job.provider) for job in jobs.values()}
        verdicts = await setup_probe.probe_all(self.hass, jobs, creds)
        return {f"{role}_model": setup_roles.error_key(kind, role == "vision")
                for role, kind in verdicts.items() if kind is not None}

    def _credentials_for(self, provider: str) -> tuple[str, str | None]:
        """(api key, base url) for a test call."""
        if provider == "ollama":
            return "", self._entries["ollama"]
        return self._entries[provider], None

    async def _store(self, chosen: dict[str, str]) -> str | None:
        """Write the typed keys, then config.json. The error key of the
        first write that failed, or None when both landed."""
        if not await self._save_keys():
            return "secrets_write_failed"
        wrote = await self.hass.async_add_executor_job(self._apply_to_config, chosen)
        return None if wrote else "config_write_failed"

    async def _save_keys(self) -> bool:
        """Put the keys typed on screen 1 into secrets.yaml, stopping at the
        first failure. Saved keys are already there and are left as they are."""
        from . import ha_secrets

        for provider in sorted(self._typed):
            stored = await ha_secrets.async_set_provider_credential(
                self.hass, provider, self._entries[provider])
            if not stored:
                return False
        return True

    def _role_settings(self, chosen: dict[str, str]) -> dict[str, str]:
        """The provider and model setting of each chosen role."""
        from . import setup_roles

        out: dict[str, str] = {}
        for role, model in chosen.items():
            provider_key, model_key = setup_roles.ROLE_KEYS[role]
            out[provider_key] = self._roles[role]
            out[model_key] = model
        return out

    def _apply_to_config(self, chosen: dict[str, str]) -> bool:
        """Copy the AI choices into config.json too. config.json beats the
        entry, so a file left by an earlier install would otherwise bring its
        old choices back. Also drops the stale settings above and the
        settings of any role not chosen; everything else (the honorific, for
        one) is kept. Blocking: run it in the executor."""
        from . import nova_config, setup_roles

        nova_config.configure(self.hass)
        fresh: dict[str, Any] = {
            "welcome_pending": True,
            "self_hosted_endpoints_migrated": True,
            "ollama_base_url": self._entries.get("ollama", ""),
        }
        fresh.update(self._role_settings(chosen))
        dropped = list(_STALE_SETTINGS)
        for role in setup_roles.ROLES:
            if role not in chosen:
                dropped.extend(setup_roles.ROLE_KEYS[role])
        # set_many saves the whole cache, so doing it last means its result
        # covers the deletions as well.
        for key in dropped:
            nova_config.delete(key)
        return nova_config.set_many(fresh)

    def _entry_data(self, chosen: dict[str, str]) -> dict[str, Any]:
        data: dict[str, Any] = {
            CONF_HONORIFIC: DEFAULT_HONORIFIC,
            # A fresh install is already at the current schema, so no
            # migration runs for it.
            "schema_version": CURRENT_SCHEMA_VERSION,
            # welcome.py posts "Nova is ready" once, on a fresh install only.
            "welcome_pending": True,
            # Ollama's own address field is the one read from now on.
            "self_hosted_endpoints_migrated": True,
        }
        if "ollama" in self._entries:
            data["ollama_base_url"] = self._entries["ollama"]
        data.update(self._role_settings(chosen))
        return data

    # ── reinstall import ──

    async def async_step_import(self, import_data: dict[str, Any]) -> dict:
        """Create the entry from the config.json an earlier install left."""
        from .llm_provider import resolve_provider_endpoint

        key = (import_data.get(CONF_API_KEY) or import_data.get("groq_api_key", "")).strip()
        provider = import_data.get("llm_provider", "groq")
        try:
            address = resolve_provider_endpoint(import_data, provider) or ""
        except ValueError:
            address = ""
        local = provider in ("ollama", "custom") and bool(address)
        # A cloud key or a working local endpoint will do; one is required.
        if not (key or local):
            _LOGGER.warning("Nova: config found but no API key and no local LLM")
            return self.async_abort(reason="import_failed")

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_API_KEY: key})
        _LOGGER.info("Nova: auto-configuring from existing runtime config (provider=%s%s)",
                     provider, "" if key else ", local")

        data: dict[str, Any] = {
            CONF_API_KEY: key,
            CONF_MODEL: import_data.get(CONF_MODEL, DEFAULT_MODEL),
            CONF_HONORIFIC: import_data.get(CONF_HONORIFIC, DEFAULT_HONORIFIC),
            "llm_provider": provider,
            "llm_base_url": address,
        }
        for hosted in ("ollama", "custom"):
            data[f"{hosted}_base_url"] = address if provider == hosted else ""
        data.update({
            # An existing install starts at 7, so the v7 to v8 migration
            # still runs and keeps its lockdown exempt locks.
            "schema_version": 7,
        })
        carried = {name: value for name, value in import_data.items() if name not in _NOT_CARRIED}
        return self.async_create_entry(title="Nova", data=data, options=carried)

    # ── Supervisor discovery ──

    async def async_step_hassio(self, discovery_info) -> dict:
        """The Nova Voice ID app announcing itself (service ``nova``, config
        {"uri": "tcp://<host>:10350", "token": "..."}) each time it starts.

        Never creates an entry: with no Nova entry the flow just aborts. With
        one, voice_id_store fills each value only where the field is empty or
        still holds the last discovered value, so anything the user typed
        wins. The token is never logged."""
        from . import paths, voice_id, voice_id_store
        from .runtime import lifecycle_runtime

        sent = getattr(discovery_info, "config", None) or {}
        uri = str(sent.get("uri") or "").strip()
        raw_token = sent.get("token")
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not uri or not voice_id.valid_uri(uri):
            _LOGGER.warning("Nova: Voice ID discovery sent an unusable address")
            return self.async_abort(reason="invalid_discovery_info")

        current = self._async_current_entries(include_ignore=False)
        if not current:
            return self.async_abort(reason="no_nova_entry")
        entry = current[0]

        paths.configure(self.hass)
        filled = await voice_id_store.async_apply_discovery(self.hass, entry, uri, token or None)
        runtime = lifecycle_runtime(entry)
        entity = None if runtime is None else getattr(runtime, "voice_id", None)
        if filled["uri"] and entity is not None:
            self.hass.async_create_background_task(
                entity.async_refresh_info(), "nova voice id info")

        address_note = "saved" if filled["uri"] else "kept yours"
        if filled["token"]:
            token_note = "saved"
        else:
            token_note = "kept yours" if token else "not sent"
        _LOGGER.info("Nova: Voice ID app found by discovery at %s (address %s, token %s)",
                     uri, address_note, token_note)
        return self.async_abort(reason="updated")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NovaOptionsFlow:
        return NovaOptionsFlow(config_entry)


# ── Configure dialog ─────────────────────────────────────────────────────────

def _text() -> Any:
    return selector.TextSelector()


def _switch() -> Any:
    return selector.BooleanSelector()


def _areas() -> Any:
    return selector.AreaSelector(selector.AreaSelectorConfig(multiple=True))


def _choice(options: list[str], *, custom: bool = False) -> Any:
    config: dict[str, Any] = {"options": options}
    if custom:
        config["custom_value"] = True
    config["mode"] = selector.SelectSelectorMode.DROPDOWN
    return selector.SelectSelector(selector.SelectSelectorConfig(**config))


def _number(low: float, high: float, step: float, mode: str) -> Any:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=low, max=high, step=step, mode=getattr(selector.NumberSelectorMode, mode)))


# Each section's fields, in display order: (setting, default, widget).
# Widgets are built when the form is shown.
_Field = tuple[str, Any, Callable[[], Any]]
_SECTIONS: dict[str, tuple[_Field, ...]] = {
    # Persona, directive, conversation model, home control.
    "core": (
        (CONF_HONORIFIC, DEFAULT_HONORIFIC, lambda: _choice(HONORIFIC_OPTIONS, custom=True)),
        (CONF_DIRECTIVE_PRESET, DEFAULT_DIRECTIVE_PRESET, lambda: _choice(list(DIRECTIVE_PRESETS))),
        (CONF_DIRECTIVE, "", lambda: selector.TextSelector(
            selector.TextSelectorConfig(multiline=True))),
        (CONF_MODEL, DEFAULT_MODEL, _text),
        (CONF_USE_HASS_API, True, _switch),
    ),
    # Where announcements and alerts go.
    "routing": (
        (CONF_BEDROOM_AREAS, [], _areas),
        # Limits the sleeping household intrusion check to exterior doors and
        # windows on these floors; empty checks every exterior door and
        # window, as when nobody is home.
        (CONF_GROUND_FLOOR_AREAS, [], _areas),
        (CONF_BROADCAST_GROUP, "", lambda: selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player"))),
        (CONF_NOTIFY_SERVICE, "", _text),
    ),
    # Proactive awareness: model tiers and quiet hours. Provider keys live in
    # the credentials section only, so none is ever shown here.
    "observer": (
        (CONF_OBSERVER_ENABLED, False, _switch),
        (CONF_CLASSIFIER_MODEL, DEFAULT_CLASSIFIER_MODEL, _text),
        (CONF_REASONING_MODEL, DEFAULT_REASONING_MODEL, _text),
        (CONF_REVIEW_MODEL, DEFAULT_REVIEW_MODEL, _text),
        (CONF_OBSERVER_QUIET_START, DEFAULT_OBSERVER_QUIET_START, _text),
        (CONF_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_END, _text),
    ),
    # Recognising who is speaking; the voice fingerprint tier needs a GPU.
    "identity": (
        ("identity_enabled", True, _switch),
        ("identity_voice_fingerprint", False, _switch),
        ("voice_recognition_source", "", _text),
        ("voice_recognition_auto_enroll", True, _switch),
        ("identity_min_confidence", 0.45, lambda: _number(0.0, 1.0, 0.05, "SLIDER")),
    ),
    # Read only IMAP access. The password stays in secrets.yaml under the
    # name given here, never in the panel's settings.
    "email": (
        ("imap_enabled", False, _switch),
        ("imap_host", "", _text),
        ("imap_port", 993, lambda: _number(1, 65535, 1, "BOX")),
        ("imap_user", "", _text),
        ("imap_folder", "INBOX", _text),
        ("imap_ssl", True, _switch),
        ("imap_secret_key", "nova_imap_password", _text),
    ),
}
_MENU = ("core", "routing", "observer", "credentials", "identity", "email")


class NovaOptionsFlow(OptionsFlow):
    """Settings, Devices & services, Nova, Configure.

    A menu leads straight to one section; submitting it saves just that
    section's values to nova_config (what the panel and Nova read) and to
    the entry's options, whose change reloads Nova so they apply at once.
    Credentials are the exception: they go only to secrets.yaml."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._data: dict[str, Any] = {}

    def _current(self, key: str, default: Any = None) -> Any:
        """The live runtime value when set, else the entry's options, else
        its data, else the default."""
        try:
            from . import nova_config
            live = nova_config.get(key, None)
            if live is not None and live != "":
                return live
        except Exception:  # noqa: BLE001 - fall back to what the entry holds
            pass
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict:
        return self.async_show_menu(step_id="init", menu_options=list(_MENU))

    async def async_step_core(self, user_input: dict[str, Any] | None = None) -> dict:
        return await self._section("core", user_input)

    async def async_step_routing(self, user_input: dict[str, Any] | None = None) -> dict:
        return await self._section("routing", user_input)

    async def async_step_observer(self, user_input: dict[str, Any] | None = None) -> dict:
        return await self._section("observer", user_input)

    async def async_step_identity(self, user_input: dict[str, Any] | None = None) -> dict:
        return await self._section("identity", user_input)

    async def async_step_email(self, user_input: dict[str, Any] | None = None) -> dict:
        return await self._section("email", user_input)

    async def _section(self, name: str, user_input: dict[str, Any] | None) -> dict:
        """Show one section pre-filled with current values, or save it."""
        if user_input is not None:
            return await self._keep(user_input)
        fields: dict[Any, Any] = {}
        for key, default, build in _SECTIONS[name]:
            fallback = list(default) if isinstance(default, list) else default
            fields[vol.Optional(key, description=_suggest(self._current(key, fallback)))] = build()
        return self.async_show_form(step_id=name, data_schema=vol.Schema(fields))

    async def _keep(self, user_input: dict[str, Any]) -> dict:
        """Save one section. Only its own keys are written, so the other
        sections' stored values are left alone."""
        self._data.update(user_input)
        try:
            from . import nova_config
            await self.hass.async_add_executor_job(nova_config.set_many, dict(user_input))
        except Exception as exc:  # noqa: BLE001 - the entry options still save
            _LOGGER.warning("Nova options: nova_config write failed: %s", exc)
        merged = dict(self._entry.options)
        merged.update(self._data)
        return self.async_create_entry(title="", data=merged)

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> dict:
        """One key per provider, written straight to secrets.yaml.

        Stored keys are never shown: every field starts blank and a blank
        field changes nothing. Removing a key takes its clear box, and
        removing the key of the provider that runs the conversation agent
        also takes the confirmation box."""
        from . import ha_secrets

        active = str(self._current("llm_provider", "groq") or "groq").strip().lower()
        errors: dict[str, str] = {}
        if user_input is not None:
            problem, writes, clears = _sort_credentials(user_input)
            confirmed = bool(user_input.get("confirm_delete_active", False))
            if problem is None and active in clears and active not in writes and not confirmed:
                problem = "confirm_required_for_active_provider"
            if problem is None:
                for provider, value in writes.items():
                    await ha_secrets.async_set_provider_credential(self.hass, provider, value)
                for provider in clears:
                    await ha_secrets.async_delete_provider_credential(self.hass, provider)
                return self.async_create_entry(title="", data=dict(self._entry.options))
            errors["base"] = problem

        present: dict[str, bool] = {}
        fields: dict[Any, Any] = {}
        for provider in PROVIDER_API_KEY_FIELDS:
            present[provider] = await self.hass.async_add_executor_job(
                ha_secrets.has_provider_credential_sync, provider)
            fields[vol.Optional(f"{provider}_credential", default="")] = _secret_box()
            fields[vol.Optional(f"clear_{provider}", default=False)] = selector.BooleanSelector()
        fields[vol.Optional("confirm_delete_active", default=False)] = selector.BooleanSelector()
        # Marks rather than words, so the note reads the same in every language.
        status = ", ".join(f"{p} {'✓' if has else '✗'}" for p, has in present.items())
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"status": status, "active": active},
        )
