"""Diagnostics: the infrastructure audit, the fault history, and the file
behind the integration page's "Download diagnostics" button.

async_get_config_entry_diagnostics is what Home Assistant calls for that
button; without it the download fails with "File wasn't available on
site". Everything in the file has credentials taken out.
"""
from __future__ import annotations

import logging
import re

from ..safe_errors import REDACTED

from .fault_log import FaultLog
from .monitor import Finding, InfrastructureTriage
from .service_health import run_service_health

__all__ = ["InfrastructureTriage", "Finding", "FaultLog",
           "run_service_health", "async_get_config_entry_diagnostics"]

_LOGGER = logging.getLogger(__name__)

# Config keys whose values must be stripped from any diagnostics dump.
_REDACT_KEYS = {
    "api_key", "groq_api_key", "gemini_api_key", "anthropic_api_key",
    "openai_api_key", "custom_api_key", "ollama_api_key",  # Phase 2, v7.107.0
    "llm_api_key", "token", "access_token", "refresh_token",
    "client_secret", "client_id", "password", "secret", "notify_service",
    "notify_services",
    "floor_plan_address", "imap_user",
    "webhook",   # a webhook URL's path is itself the secret (n8n_webhook_base_url)
}

# Values are scrubbed too, whatever their key: credentials embedded in a URL
# (scheme://user:pass@host) and credential-like query parameters.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s?#@]+@")
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|auth|key|password|pass|pwd|secret|"
    r"sig|signature|token)=)[^&#\s]*")
# A webhook address's path is itself the secret, so in free text (an error, a
# log line) everything after the webhook segment goes: n8n /webhook/<id> and
# /webhook-test/<id>, Home Assistant /api/webhook/<id>, Discord
# /api/webhooks/<id>/<token> and Slack hooks.slack.com/services/<path>.
_WEBHOOK_PATH = re.compile(
    r"(?i)(/webhooks?(?:-test)?/|hooks\.slack\.com/services/)[^\s?#\"'<>]+")


def _scrub_text(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1" + REDACTED + "@", value)
    value = _SECRET_QUERY.sub(r"\1" + REDACTED, value)
    return _WEBHOOK_PATH.sub(r"\1" + REDACTED, value)


def _err(exc: BaseException) -> str:
    """An error line for the dump: the exception type and text, with credentials
    scrubbed. Diagnostics is for the owner, so the detail stays (unlike
    safe_errors.safe_error_message, which is for the model and the panel)."""
    try:
        return _scrub_text(f"{type(exc).__name__}: {exc}")[:200]
    except Exception:
        return type(exc).__name__


def _redact(obj):
    """A copy with every value under a sensitive key replaced and credentials
    scrubbed out of every other string, at any depth. Never raises."""
    try:
        if isinstance(obj, dict):
            clean = {}
            for key, value in obj.items():
                lowered = str(key).lower()
                if any(word in lowered for word in _REDACT_KEYS):
                    clean[key] = value if value in (None, "") else REDACTED
                else:
                    clean[key] = _redact(value)
            return clean
        if isinstance(obj, (list, tuple)):
            return [_redact(item) for item in obj]
        return _scrub_text(obj) if isinstance(obj, str) else obj
    except Exception:
        return "**redaction-error**"


def _fill(diag: dict, key: str, failed_key: str, read) -> None:
    """``diag[key] = read()``, or the scrubbed error under ``failed_key``."""
    try:
        diag[key] = read()
    except Exception as exc:
        diag[failed_key] = _err(exc)


def _domain_counts(hass) -> dict:
    # Counts per domain only: entity ids could give away the home's layout.
    counts: dict = {}
    for st in hass.states.async_all():
        domain = st.entity_id.split(".", 1)[0]
        counts[domain] = counts.get(domain, 0) + 1
    return counts


# The local loops and stores, so their size and health show without a live
# session. Database health is in service_health already; probing it here
# would apply the schema, which diagnostics must never do.
_SUBSYSTEMS = (("cognition", "stats"), ("decision_record", "stats"),
               ("intrusion", "status"), ("reasoning_cache", "stats"))


def _collect_subsystems() -> dict:
    import importlib
    found: dict = {}
    for module_name, reader in _SUBSYSTEMS:
        try:
            module = importlib.import_module(f"..{module_name}", __package__)
            found[module_name] = _redact(getattr(module, reader)())
        except Exception as exc:
            found[module_name] = {"error": _err(exc)}
    return found


def _audio_routing(hass) -> dict:
    """Each satellite's paired speaker with whether it can be reached now,
    and the TTS engines in use, so "no audible reply" can be traced from the
    file (the pairings themselves are in the config above)."""
    import json
    from .. import nova_config, tts_helper
    cfg = nova_config.get_all()
    raw = cfg.get("satellite_pairings")
    pairings = (json.loads(raw) if isinstance(raw, str) else raw) or {}
    paired = {}
    for satellite, speaker in pairings.items():
        st = hass.states.get(speaker) if speaker else None
        paired[satellite] = {
            "speaker": speaker,
            "state": st.state if st else "missing",
            "reachable": bool(st and st.state not in ("unavailable", "unknown")),
        }
    return {"pairings": paired, "tts": {
        "reply_engine": tts_helper.find_best_tts_entity(hass),
        "premium_engine": tts_helper.find_premium_tts_entity(hass),
        "tts_engine_option": cfg.get("tts_engine"),
        "premium_contexts": cfg.get("tts_premium_contexts"),
    }}


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    """The "Download diagnostics" file: the entry and Nova's own config,
    service health, the cognitive core and connectivity, entity counts,
    subsystem stats, audio routing and the recent logs, all redacted.
    Never raises: a section that fails is replaced by its error, so a file
    always comes out."""
    diag: dict = {"integration": "nova"}
    try:
        diag["version"] = getattr(entry, "version", None)
        from ..const import DOMAIN
        diag["domain"] = DOMAIN
    except Exception:
        pass

    try:
        data = dict(getattr(entry, "data", {}) or {})
        options = dict(getattr(entry, "options", {}) or {})
        diag["entry_data"], diag["entry_options"] = _redact(data), _redact(options)
    except Exception as exc:
        diag["config_error"] = _err(exc)

    def live_config():
        from .. import nova_config
        return _redact(nova_config.get_all())
    _fill(diag, "nova_config", "nova_config_error", live_config)

    try:
        diag["service_health"] = _redact(await run_service_health(hass))
    except Exception as exc:
        diag["service_health_error"] = _err(exc)

    try:
        from .. import cognitive_core
        core = getattr(cognitive_core, "_CORE", None)
        if core and hasattr(core, "status"):
            diag["cognitive"] = _redact(core.status())
    except Exception as exc:
        diag["cognitive_error"] = _err(exc)

    def connectivity_status():
        from .. import connectivity
        return _redact(connectivity.status())
    _fill(diag, "connectivity", "connectivity_error", connectivity_status)

    try:
        counts = _domain_counts(hass)
        diag["entity_domain_counts"], diag["entity_total"] = counts, sum(counts.values())
    except Exception as exc:
        diag["entity_count_error"] = _err(exc)

    diag["subsystems"] = await hass.async_add_executor_job(_collect_subsystems)
    _fill(diag, "audio_routing", "audio_routing_error", lambda: _audio_routing(hass))

    # The recent log includes where each spoken reply went; the conversation
    # log keeps those lines even when observer noise has flushed the main log.
    def recent_log():
        from ..websocket import recent_debug_log
        return _redact(recent_debug_log(150))
    _fill(diag, "recent_log", "recent_log_error", recent_log)

    def conversation_log():
        from ..websocket import recent_conversation_log
        return _redact(recent_conversation_log(80))
    _fill(diag, "conversation_log", "conversation_log_error", conversation_log)
    return diag
