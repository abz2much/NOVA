"""Tests for the five n8n specialist bridge tools (ask_executive_assistant,
ask_marketing_agent, ask_security_privacy_agent, ask_homelab_infra_agent,
ask_house_manager_agent) added to agent.py alongside the Nova unification
webhooks.

Follows test_bootstrap.py's convention: aiohttp + the HA aiohttp_client helper
are stubbed before the module under test is loaded, since neither is
installed/needed in this sandbox.
"""
import json
import sys
import types
from pathlib import Path

import pytest

if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: kw
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


_BASE_KEY = "n8n_webhook_base_url"
_BASE_URL = "http://n8n.test:5678/webhook"


@pytest.fixture
def agent(load):
    return load("agent")


@pytest.fixture
def ha_secrets(load):
    return load("ha_secrets")


@pytest.fixture
def nova_config(load):
    return load("nova_config")


@pytest.fixture(autouse=True)
def _isolate_nova_config(nova_config, tmp_path, monkeypatch):
    """Keep nova_config off the real filesystem. CONFIG_PATH defaults to
    /config/nova/config.json, which only exists inside real Home Assistant OS
    — on any other machine nova_config.get() throws trying to mkdir it.
    Same isolation pattern as test_nova_config_hardening.py's jcfg fixture."""
    monkeypatch.setattr(nova_config, "CONFIG_PATH", Path(tmp_path / "config.json"))
    monkeypatch.setattr(nova_config, "_cache", {})
    monkeypatch.setattr(nova_config, "_loaded", False)
    monkeypatch.setattr(nova_config, "last_load_error", None)
    # A specialist is inert until its base URL is explicitly configured.
    nova_config.set(_BASE_KEY, _BASE_URL)


@pytest.fixture(autouse=True)
def _configured_secret(ha_secrets, monkeypatch):
    """Default: a secret is configured. Individual tests override this."""
    async def fake_get_secret(hass, key, default=""):
        return "test-secret-123"
    monkeypatch.setattr(ha_secrets, "async_get_secret", fake_get_secret)


class FakeResponse:
    def __init__(self, status, json_body=None, text_body=""):
        self.status = status
        self._json = json_body
        self._text = text_body if text_body else (json.dumps(json_body) if json_body is not None else "")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    async def text(self):
        return self._text


class FakeSession:
    """Records the last request made and returns a pre-set response, or
    raises a pre-set exception (to simulate timeout/network failure)."""
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.last_call = None

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.last_call = {"url": url, "json": json, "headers": headers, **kwargs}
        if self.raises:
            raise self.raises
        return self.response


@pytest.fixture
def patched_session(monkeypatch):
    """Return a function that installs a FakeSession as the aiohttp client
    session for the duration of one call."""
    def _install(fake_session):
        ac_mod = sys.modules["homeassistant.helpers.aiohttp_client"]
        monkeypatch.setattr(ac_mod, "async_get_clientsession", lambda hass: fake_session)
        return fake_session
    return _install


# ── Response-shape parsing ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_langchain_agent_specialist_parses_output_field(agent, fake_hass, patched_session):
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "Front door is closed."})))
    result = await agent._exec_ask_security_privacy_agent(fake_hass, {"message": "is the front door open?"})
    data = json.loads(result)
    assert data == {"reply": "Front door is closed."}
    assert sess.last_call["url"].endswith("/nova-security-privacy")
    assert sess.last_call["headers"][agent.N8N_WEBHOOK_HEADER] == "test-secret-123"
    assert sess.last_call["json"] == {"message": "is the front door open?"}


@pytest.mark.asyncio
async def test_ssh_backed_specialist_parses_stdout_field(agent, fake_hass, patched_session):
    sess = patched_session(FakeSession(response=FakeResponse(200, {"code": 0, "stdout": "OK", "stderr": ""})))
    result = await agent._exec_ask_executive_assistant(fake_hass, {"message": "ping"})
    data = json.loads(result)
    assert data == {"reply": "OK"}
    assert sess.last_call["url"].endswith("/nova-exec-assistant")


@pytest.mark.parametrize("fn_name,path", [
    ("_exec_ask_executive_assistant", "nova-exec-assistant"),
    ("_exec_ask_marketing_agent", "nova-marketing"),
    ("_exec_ask_security_privacy_agent", "nova-security-privacy"),
    ("_exec_ask_homelab_infra_agent", "nova-homelab-infra"),
    ("_exec_ask_house_manager_agent", "nova-house-manager"),
])
@pytest.mark.asyncio
async def test_each_specialist_hits_its_own_path(agent, fake_hass, patched_session, fn_name, path):
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "ok"})))
    fn = getattr(agent, fn_name)
    await fn(fake_hass, {"message": "hi"})
    assert sess.last_call["url"] == f"{_BASE_URL}/{path}"
    assert sess.last_call["allow_redirects"] is False


# ── Failure handling: honest errors, never a fabricated reply ────────────────

@pytest.mark.asyncio
async def test_non_200_is_honest_error(agent, fake_hass, patched_session):
    patched_session(FakeSession(response=FakeResponse(500, text_body="internal server error")))
    result = await agent._exec_ask_house_manager_agent(fake_hass, {"message": "add milk"})
    data = json.loads(result)
    assert "error" in data and "reply" not in data
    assert "500" in data["error"]


@pytest.mark.asyncio
async def test_timeout_is_honest_error(agent, fake_hass, patched_session):
    patched_session(FakeSession(raises=TimeoutError()))
    result = await agent._exec_ask_marketing_agent(fake_hass, {"message": "post something"})
    data = json.loads(result)
    assert "error" in data and "timed out" in data["error"]


@pytest.mark.asyncio
async def test_unreachable_host_is_honest_error(agent, fake_hass, patched_session):
    patched_session(FakeSession(raises=ConnectionError("EHOSTUNREACH")))
    result = await agent._exec_ask_homelab_infra_agent(fake_hass, {"message": "proxmox status"})
    data = json.loads(result)
    assert "error" in data and "unreachable" in data["error"]


@pytest.mark.asyncio
async def test_missing_secret_never_makes_a_network_call(agent, fake_hass, patched_session, ha_secrets, monkeypatch):
    async def empty_secret(hass, key, default=""):
        return ""
    monkeypatch.setattr(ha_secrets, "async_get_secret", empty_secret)
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "should never see this"})))
    result = await agent._exec_ask_security_privacy_agent(fake_hass, {"message": "test"})
    data = json.loads(result)
    assert "error" in data
    assert agent.N8N_WEBHOOK_SECRET_KEY in data["error"]
    assert sess.last_call is None  # never reached the network


@pytest.mark.asyncio
async def test_blank_message_rejected_before_any_call(agent, fake_hass, patched_session):
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "x"})))
    result = await agent._exec_ask_house_manager_agent(fake_hass, {"message": "   "})
    data = json.loads(result)
    assert "error" in data
    assert sess.last_call is None


# ── Schema / dispatch registration ───────────────────────────────────────────

def test_all_five_tools_registered_in_schema_and_dispatch(agent):
    names = ["ask_executive_assistant", "ask_marketing_agent",
             "ask_security_privacy_agent", "ask_homelab_infra_agent",
             "ask_house_manager_agent"]
    schema_names = {t["function"]["name"] for t in agent.NOVA_TOOLS}
    for n in names:
        assert n in schema_names, f"{n} missing from NOVA_TOOLS"
        assert n in agent._TOOL_MAP, f"{n} missing from _TOOL_MAP"


# ── Explicit configuration only ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_hard_coded_endpoint_remains(agent):
    assert not hasattr(agent, "_N8N_DEFAULT_BASE_URL")
    src = (Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "agent_runtime" / "capabilities" / "specialists.py").read_text()
    import re
    assert not re.search(r"https?://[\w.]+[:/]", src), "a literal endpoint address is back"


@pytest.mark.asyncio
async def test_unconfigured_base_url_is_inert(agent, fake_hass, patched_session, nova_config):
    nova_config.set(_BASE_KEY, "")
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "never"})))
    for fn_name in ("_exec_ask_executive_assistant", "_exec_ask_house_manager_agent"):
        data = json.loads(await getattr(agent, fn_name)(fake_hass, {"message": "hi"}))
        assert _BASE_KEY in data["error"] and "not configured" in data["error"]
    assert sess.last_call is None


@pytest.mark.parametrize("bad", [
    "ftp://n8n.test/webhook",
    "http://user:pw@n8n.test/webhook",
    "http://169.254.169.254/latest",
    "not a url",
])
@pytest.mark.asyncio
async def test_unusable_base_url_is_inert(agent, fake_hass, patched_session, nova_config, bad):
    nova_config.set(_BASE_KEY, bad)
    sess = patched_session(FakeSession(response=FakeResponse(200, {"output": "never"})))
    data = json.loads(await agent._exec_ask_marketing_agent(fake_hass, {"message": "hi"}))
    assert "not a usable" in data["error"]
    assert sess.last_call is None


@pytest.mark.asyncio
async def test_bridge_never_logs_message_secret_or_reply(agent, fake_hass, patched_session, caplog):
    import logging
    patched_session(FakeSession(response=FakeResponse(500, text_body="SECRET-BODY-TEXT")))
    with caplog.at_level(logging.DEBUG):
        await agent._exec_ask_house_manager_agent(fake_hass, {"message": "PRIVATE-MESSAGE"})
    for record in caplog.records:
        text = record.getMessage()
        assert "PRIVATE-MESSAGE" not in text
        assert "test-secret-123" not in text
        assert "SECRET-BODY-TEXT" not in text
