"""Nova's problems shown in Home Assistant's Settings, Repairs.

The reasoning backend being unreachable gets a notice there with the cause,
next to the panel's diagnostics and the reason already logged on the AI
path. It is a notice only, with nothing to fix through a flow: Nova still
loads (in a limited way) and the notice goes the moment reasoning works.

Called repair_notices, not repairs, so Home Assistant does not load it as
the repairs platform, which would expect a fix flow.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_DETAIL_LIMIT = 300
_DEFAULT_DETAIL = "The reasoning model could not be reached."
_LLM_ISSUE = "llm_unavailable"
_LOGGER = logging.getLogger(__name__)

# Our own record of the notice, so an outage does not rewrite the registry
# over and over and a restart leaves nothing stale. cleared_once lets the
# first success after startup remove a notice from an earlier run.
_state = {"active": False, "detail": None, "cleared_once": False}


def note_llm_problem(hass: HomeAssistant, detail: str) -> None:
    """Show, or update, the "reasoning backend unavailable" notice with the
    cause the AI path worked out (a retired model, a key or connection
    error). Event loop safe; never raises."""
    text = (detail or _DEFAULT_DETAIL).strip()[:_DETAIL_LIMIT]
    if _state["active"] and _state["detail"] == text:
        return
    try:
        ir.async_create_issue(hass, DOMAIN, _LLM_ISSUE, is_fixable=False,
                              severity=ir.IssueSeverity.ERROR, translation_key=_LLM_ISSUE,
                              translation_placeholders={"detail": text})
    except Exception as exc:  # registry not there, or shutting down
        _LOGGER.debug("could not create LLM repair issue: %s", exc)
        return
    _state.update(active=True, detail=text, cleared_once=False)


def clear_llm_problem(hass: HomeAssistant) -> None:
    """Remove the notice once reasoning works again. Done once per run even
    when this run never showed it, so a notice from an earlier run goes on
    the first success. Event loop safe; never raises."""
    if _state["cleared_once"] and not _state["active"]:
        return
    try:
        ir.async_delete_issue(hass, DOMAIN, _LLM_ISSUE)
    except Exception as exc:
        _LOGGER.debug("could not clear LLM repair issue: %s", exc)
    _state.update(active=False, detail=None, cleared_once=True)


# ─── Credential migration ambiguity (Phase 2, v7.107.0) ──────────────────────
# ha_secrets.split_shared_credential raises this when it can't tell which
# provider the legacy shared credential belongs to — it deliberately leaves
# the credential untouched rather than guess, so this is the administrator's
# only signal that a manual migration (Settings → Nova → Configure →
# Credentials) is still needed.

_CRED_ISSUE = "credential_migration_ambiguous"
_cred_state = {"active": False}


def note_credential_migration_ambiguous(hass: HomeAssistant) -> None:
    """Raise the 'shared credential could not be auto-migrated' Repair
    issue. Safe to call from the event loop; never raises; idempotent —
    calling it again while already active is a no-op."""
    if _cred_state["active"]:
        return
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _CRED_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_CRED_ISSUE,
        )
        _cred_state["active"] = True
    except Exception as exc:
        _LOGGER.debug("could not create credential migration repair issue: %s", exc)


def clear_credential_migration_ambiguous(hass: HomeAssistant) -> None:
    """Clear the credential-migration Repair issue once it's resolved
    (migrated, or the administrator set the provider explicitly). Safe to
    call unconditionally; never raises."""
    if not _cred_state["active"]:
        return
    try:
        ir.async_delete_issue(hass, DOMAIN, _CRED_ISSUE)
    except Exception as exc:
        _LOGGER.debug("could not clear credential migration repair issue: %s", exc)
    _cred_state["active"] = False


# ─── Voice setup (v8.6.0) ────────────────────────────────────────────────────
# bootstrap.async_run_bootstrap installs the voice add-ons, reconnects Wyoming
# and builds the Nova pipeline, which can take several minutes and used to be
# visible only in the log. The progress notice shows on the first run only;
# the incomplete notice names what failed and clears once a later run (or the
# live pipeline check) succeeds. Each step has its own translation key so the
# whole notice is translated, not just its frame.

_VOICE_PROGRESS_ISSUE = "voice_setup_in_progress"
_VOICE_INCOMPLETE_ISSUE = "voice_setup_incomplete"
VOICE_STEPS = ("addons", "wyoming", "pipeline")


def note_voice_setup_step(hass: HomeAssistant, step: str, number: int, total: int) -> None:
    """Show (or move on) the 'Nova is setting up voice' notice. ``step`` is
    one of VOICE_STEPS. Safe to call from the event loop; never raises."""
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _VOICE_PROGRESS_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"voice_setup_step_{step}",
            translation_placeholders={"step": str(number), "total": str(total)},
        )
    except Exception as exc:
        _LOGGER.debug("could not create voice setup progress issue: %s", exc)


def note_voice_setup_incomplete(hass: HomeAssistant, failed: list[str]) -> None:
    """Replace the progress notice with 'voice setup is incomplete', naming
    the parts that failed. Safe to call from the event loop; never raises."""
    try:
        ir.async_delete_issue(hass, DOMAIN, _VOICE_PROGRESS_ISSUE)
        ir.async_create_issue(
            hass,
            DOMAIN,
            _VOICE_INCOMPLETE_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_VOICE_INCOMPLETE_ISSUE,
            translation_placeholders={"failed": ", ".join(failed)},
        )
    except Exception as exc:
        _LOGGER.debug("could not create voice setup incomplete issue: %s", exc)


def clear_voice_setup(hass: HomeAssistant) -> None:
    """Remove both voice setup notices (setup finished and works). Safe to
    call unconditionally; never raises."""
    for issue_id in (_VOICE_PROGRESS_ISSUE, _VOICE_INCOMPLETE_ISSUE):
        try:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        except Exception as exc:
            _LOGGER.debug("could not clear %s: %s", issue_id, exc)


# ─── Retired services (8.40.0) ───────────────────────────────────────────────
# A service Nova is phasing out keeps working for at least one release. The
# first call in each run logs a warning and raises a notice naming the service
# and what to use instead, so whoever owns the calling automation finds out
# before the service goes. The notice is not persistent: a restart clears it,
# and it comes back only if something calls the service again.

# Each retired service, with what to use instead. None means the feature
# behind it is gone and the call does nothing.
# Empty since 8.60.0: nothing is being retired right now.
RETIRED_SERVICES: dict[str, str | None] = {}

_retired_seen: set[str] = set()


def note_retired_service(hass: HomeAssistant, service: str,
                         replacement: str | None) -> None:
    """Warn once per run that ``nova.<service>`` is being retired.
    ``replacement`` names the service to use instead; None means the feature
    behind it is already gone. Safe to call from the event loop; never
    raises."""
    if service in _retired_seen:
        return
    _retired_seen.add(service)
    name = f"{DOMAIN}.{service}"
    if replacement:
        _LOGGER.warning("%s is being retired and will be removed in a later "
                        "Nova release; use %s instead", name, replacement)
        key, placeholders = "retired_service", {"service": name, "replacement": replacement}
    else:
        _LOGGER.warning("%s is being retired and will be removed in a later "
                        "Nova release; the feature behind it was removed, so "
                        "the call does nothing", name)
        key, placeholders = "retired_service_no_effect", {"service": name}
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"retired_service_{service}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=placeholders,
        )
    except Exception as exc:
        _LOGGER.debug("could not create retired service issue: %s", exc)
