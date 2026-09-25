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


_N8N_DEFAULT_BASE_URL = "http://10.0.4.111:5678/webhook"


_N8N_SSH_TIMEOUT = 90     # Executive Assistant / Marketing: SSH -> claude -p, can be slow


_N8N_AGENT_TIMEOUT = 30   # Security & Privacy / Homelab & Infra Ops / House Manager


async def _ask_n8n_specialist(hass: HomeAssistant, path: str, message: str,
                              timeout: int) -> str:
    """POST an objective to one of the surviving n8n specialists' webhooks and
    return its reply as a JSON string. Never raises — a network failure,
    timeout, or non-200 comes back as an honest {"error": ...}, never a
    fabricated reply."""
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from ... import nova_config, ha_secrets

    message = str(message or "").strip()
    if not message:
        return json.dumps({"error": "a message is required"})

    secret = await ha_secrets.async_get_secret(hass, N8N_WEBHOOK_SECRET_KEY, "")
    if not secret:
        return json.dumps({
            "error": f"{N8N_WEBHOOK_SECRET_KEY} is not set in secrets.yaml — "
                     "this specialist can't be reached until it is",
        })

    base_url = nova_config.get("n8n_webhook_base_url", _N8N_DEFAULT_BASE_URL)
    url = f"{str(base_url).rstrip('/')}/{path}"

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json={"message": message},
            headers={N8N_WEBHOOK_HEADER: secret},
            timeout=aiohttp.ClientTimeout(total=timeout),
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


async def _exec_ask_executive_assistant(hass: HomeAssistant, args: dict) -> str:
    """Executive Assistant specialist — Gmail/Calendar via its own Claude Code
    session over SSH. Can take real action, not read-only."""
    return await _ask_n8n_specialist(
        hass, "nova-exec-assistant", args.get("message", ""), _N8N_SSH_TIMEOUT)


async def _exec_ask_marketing_agent(hass: HomeAssistant, args: dict) -> str:
    """Marketing & Content specialist — @automatedhome.ie Instagram, via its
    own Claude Code session over SSH. Can take real action, not read-only."""
    return await _ask_n8n_specialist(
        hass, "nova-marketing", args.get("message", ""), _N8N_SSH_TIMEOUT)


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
