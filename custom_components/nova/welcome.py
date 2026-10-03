"""First run "Nova is ready" notification, built from Setup Doctor results.

Shown once, after the first setup of an entry created through the setup
screens. The config flow marks such an entry with ``welcome_pending``, which
nova_config.init_from_entry copies into config.json like any other entry key.
Entries created any other way, such as an import of a saved config, never
carry that flag, so existing installs never see it. Once posted, the
``welcome_shown`` key in nova_config stops it repeating on restart or reload.

Runs at the end of bootstrap's background task, after the voice pipeline
repair, so the Assist pipeline check sees the finished state. Never raises.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

NOTIFICATION_ID = "nova_welcome"
ENTRY_FLAG = "welcome_pending"
SHOWN_KEY = "welcome_shown"


def build_message(result: dict) -> tuple[str, str]:
    """Turn a run_setup_health() result into (title, message)."""
    checks = [c for c in (result or {}).get("checks", []) if c.get("status") != "off"]
    problems = [c for c in checks if c.get("status") in ("warn", "down")]
    passed = len(checks) - len(problems)

    if not checks:
        summary = "Setup finished."
    elif not problems:
        summary = f"Setup finished. All {passed} checks passed."
    else:
        summary = (f"Setup finished. {passed} checks passed, "
                   f"{len(problems)} need attention.")

    lines = [summary]
    if problems:
        lines += ["", "**Needs attention**"]
        for c in problems:
            line = f"- **{c.get('name', '?')}**: {c.get('detail', '')}".rstrip()
            if c.get("suggested_fix"):
                line += f" Fix: {c['suggested_fix']}"
            lines.append(line)
    lines += ["", "Run these checks again any time in the Nova panel, "
                  "under Diagnostics, Setup Doctor."]
    return "Nova is ready", "\n".join(lines)


async def async_maybe_show(hass) -> bool:
    """Post the notification once. Returns True when it was posted."""
    try:
        from . import nova_config, setup_health
        if not nova_config.get(ENTRY_FLAG) or nova_config.get(SHOWN_KEY):
            return False
        result = await setup_health.run_setup_health(hass)
        title, message = build_message(result)
        await hass.services.async_call(
            "persistent_notification", "create", {
                "title": title,
                "message": message,
                "notification_id": NOTIFICATION_ID,
            }, blocking=False)
        await hass.async_add_executor_job(nova_config.set, SHOWN_KEY, True)
        return True
    except Exception as exc:
        _LOGGER.debug("Nova welcome notification skipped: %s", exc)
        return False
