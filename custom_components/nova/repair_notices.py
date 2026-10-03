"""Home Assistant Repair issues for Nova operational problems.

These surface an actionable condition — the reasoning backend being unreachable
— in Settings → Repairs with a clear description, complementing the in-panel
diagnostics and the failure reason already logged on the LLM path. The issue is
a *non-fixable notice*: it never blocks the integration from loading (Nova
stays up in limited mode) and it clears itself the moment reasoning recovers.

Deliberately named ``repair_notices`` rather than ``repairs`` so Home Assistant
does not treat it as the repairs *platform* (which would expect a fix-flow).
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_LLM_ISSUE = "llm_unavailable"

# Track our own issue so we neither churn the registry during an outage nor
# leave a stale issue behind after a restart. ``cleared_once`` lets the first
# recovery after startup delete an issue left over from a previous run.
_state = {"active": False, "detail": None, "cleared_once": False}


def note_llm_problem(hass: HomeAssistant, detail: str) -> None:
    """Raise (or update) the 'reasoning backend unavailable' Repair issue.

    ``detail`` is the specific cause already computed on the LLM failure path
    (e.g. a decommissioned model, or an auth/connectivity error). Safe to call
    from the event loop; never raises.
    """
    text = (detail or "The reasoning model could not be reached.").strip()[:300]
    if _state["active"] and _state["detail"] == text:
        return
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _LLM_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=_LLM_ISSUE,
            translation_placeholders={"detail": text},
        )
        _state.update(active=True, detail=text, cleared_once=False)
    except Exception as exc:  # registry unavailable / mid-teardown
        _LOGGER.debug("could not create LLM repair issue: %s", exc)


def clear_llm_problem(hass: HomeAssistant) -> None:
    """Clear the LLM Repair issue once reasoning works again.

    Clears once per process even if we didn't raise the issue this run, so a
    notice left over from a previous run is removed on the next success. Safe to
    call from the event loop; never raises.
    """
    if not _state["active"] and _state["cleared_once"]:
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
