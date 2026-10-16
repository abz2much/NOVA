"""External n8n specialist bridges."""
from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

# One logger for the whole agent, named as it always was (…nova.agent), so
# log filters and levels set for the agent keep applying.
_LOGGER = logging.getLogger(__name__.partition(".agent_runtime")[0] + ".agent")


# ── n8n specialist bridges (additive) ─────────────────────────────────────────────────────────────
# Five surviving n8n AI Operations specialists, each reachable over its own
# webhook added alongside its existing Execute Workflow Trigger — nothing
# about their internals changed. Auth is a shared secret header; the value
# lives in secrets.yaml, never the plaintext panel config, per ha_secrets'
# own rule (the owner sets it by hand, this module never writes it).
N8N_WEBHOOK_SECRET_KEY = "nova_specialist_webhook_key"


N8N_WEBHOOK_HEADER = "X-Nova-Key"


# The webhook base URL (nova_config "n8n_webhook_base_url") has no default:
# a specialist is inert until both it and the secret are configured.
N8N_WEBHOOK_BASE_URL_KEY = "n8n_webhook_base_url"


_N8N_SSH_TIMEOUT = 90     # Executive Assistant / Marketing: SSH -> claude -p, can be slow


_N8N_AGENT_TIMEOUT = 30   # Security & Privacy / Homelab & Infra Ops / House Manager


async def _ask_n8n_specialist(hass: HomeAssistant, path: str, message: str,
                              timeout: int) -> str:
    """POST an objective to one of the surviving n8n specialists' webhooks and
    return its reply as a JSON string. Never raises — a network failure,
    timeout, or non-200 comes back as an honest {"error": ...}, never a
    fabricated reply.

    Inert unless both the webhook base URL and the shared secret are
    explicitly configured; the URL must be a plain http(s) URL (no embedded
    credentials, no metadata host). Redirects are not followed, so the secret
    header only ever goes to the configured origin. Nothing here logs the
    message, the secret or the reply."""
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from ... import nova_config, ha_secrets

    message = str(message or "").strip()
    if not message:
        return json.dumps({"error": "a message is required"})

    base_url = str(nova_config.get(N8N_WEBHOOK_BASE_URL_KEY, "") or "").strip()
    if not base_url:
        return json.dumps({
            "error": f"{N8N_WEBHOOK_BASE_URL_KEY} is not configured — "
                     "this specialist can't be reached until it is",
        })
    try:
        from ...providers.destinations import check_url
        check_url(base_url)
    except Exception:
        return json.dumps({
            "error": f"{N8N_WEBHOOK_BASE_URL_KEY} is not a usable http(s) URL — "
                     "this specialist can't be reached until it is fixed",
        })

    secret = await ha_secrets.async_get_secret(hass, N8N_WEBHOOK_SECRET_KEY, "")
    if not secret:
        return json.dumps({
            "error": f"{N8N_WEBHOOK_SECRET_KEY} is not set in secrets.yaml — "
                     "this specialist can't be reached until it is",
        })

    url = f"{base_url.rstrip('/')}/{path}"

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json={"message": message},
            headers={N8N_WEBHOOK_HEADER: secret},
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                return json.dumps({
                    "error": f"{path} returned HTTP {resp.status}",
                    "detail": body[:500],
                })
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": (await resp.text())[:2000]}
    except TimeoutError:
        return json.dumps({"error": f"{path} timed out after {timeout}s — no reply"})
    except Exception as exc:
        return json.dumps({"error": f"{path} unreachable: {exc}"})

    # Different specialists' last node shapes differ (SSH stdout vs. agent
    # output) — take whichever text field is present, never invent one.
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        text = data.get("output") or data.get("stdout") or data.get("text")
        if text:
            return json.dumps({"reply": str(text).strip()})
        if data.get("error") or data.get("stderr"):
            return json.dumps({"error": data.get("error") or data.get("stderr")})
    return json.dumps({"reply": json.dumps(data)[:2000]})


# The two specialists that act in the world (8.58.6). Each needs a yes first.
ACTING_SPECIALISTS = {
    "ask_executive_assistant": ("the Executive Assistant", "nova-exec-assistant"),
    "ask_marketing_agent": ("the Marketing agent", "nova-marketing"),
}


async def _ask_acting_specialist(hass: HomeAssistant, tool: str, args: dict,
                                 device_id=None, user_id=None) -> str:
    """Ask a specialist that can act, once the user has said yes: a spoken
    yes on a voice satellite (held on the reply), a phone tap in Chat."""
    from ... import pending_confirm, policy
    from ...prompt_fence import one_line
    who, path = ACTING_SPECIALISTS[tool]
    message = args.get("message", "") if isinstance(args, dict) else ""
    shown = one_line(str(message), 80)
    decision = await policy.authorize(hass, policy.AuthorityRequest(
        "nova", tool, "", device_id=device_id or "", user_id=user_id or "",
        label=f"ask {who}", target_name=f'"{shown}"'))
    if not decision.allowed:
        from .control import _hold_for_spoken_yes
        if _hold_for_spoken_yes(hass, device_id, decision.approval):
            pending_confirm.put(device_id or "", pending_confirm.KIND_SPECIALIST,
                                tool, user_id, data=str(message))
            return json.dumps({
                "status": "awaiting_confirmation",
                "message": (f"I'd ask {who}: {shown}. It can act for real, "
                            f"so it needs a spoken yes first. {pending_confirm.ASK}"),
                "ask_user": pending_confirm.ASK,
            })
        return json.dumps({
            "status": "awaiting_confirmation",
            "message": decision.note or f"Confirmation required before asking {who}.",
        })
    return await _ask_n8n_specialist(hass, path, message, _N8N_SSH_TIMEOUT)


async def _exec_ask_executive_assistant(hass: HomeAssistant, args: dict,
                                        device_id=None, user_id=None) -> str:
    """Executive Assistant specialist — Gmail/Calendar via its own Claude Code
    session over SSH. Can take real action, so it needs a yes first."""
    return await _ask_acting_specialist(
        hass, "ask_executive_assistant", args, device_id, user_id)


async def _exec_ask_marketing_agent(hass: HomeAssistant, args: dict,
                                    device_id=None, user_id=None) -> str:
    """Marketing & Content specialist — @automatedhome.ie Instagram, via its
    own Claude Code session over SSH. Can take real action, so it needs a
    yes first."""
    return await _ask_acting_specialist(
        hass, "ask_marketing_agent", args, device_id, user_id)


async def _exec_ask_security_privacy_agent(hass: HomeAssistant, args: dict) -> str:
    """Security & Privacy specialist — read-only report only."""
    return await _ask_n8n_specialist(
        hass, "nova-security-privacy", args.get("message", ""), _N8N_AGENT_TIMEOUT)


async def _exec_ask_homelab_infra_agent(hass: HomeAssistant, args: dict) -> str:
    """Homelab & Infra Ops specialist — read-only report only."""
    return await _ask_n8n_specialist(
        hass, "nova-homelab-infra", args.get("message", ""), _N8N_AGENT_TIMEOUT)


async def _exec_ask_house_manager_agent(hass: HomeAssistant, args: dict) -> str:
    """House Manager specialist — bins/bills read-only, shopping list can
    write."""
    return await _ask_n8n_specialist(
        hass, "nova-house-manager", args.get("message", ""), _N8N_AGENT_TIMEOUT)
