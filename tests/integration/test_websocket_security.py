"""WebSocket security wiring tests against a real Home Assistant instance
(PHACC) — the enforcement a fake `hass` can't credibly prove: that
@require_admin actually rejects a non-admin connection, that a command left
open by design still works for one, and that intrusion snapshots really do
reach the panel only through the admin-gated command (never a plain HTTP
path). See tests/integration/conftest.py for setup and DOMAIN-level docs in
test_wiring_smoke.py for why this needs PHACC instead of tests/unit/'s fakes.
"""
import base64

import pytest
from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


async def _setup_nova(hass) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture(autouse=True)
def _reset_model_discovery_cache():
    """websocket.py's model-discovery cache (Phase 3, v7.108.0) is a plain
    module-level dict — it isn't per-hass-instance, so it would otherwise
    leak a cached (or missing) entry from one test into the next within the
    same pytest process. Reset before and after every test in this file."""
    from custom_components.nova import websocket
    websocket.invalidate_model_cache()
    yield
    websocket.invalidate_model_cache()


async def test_admin_required_command_rejects_non_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    """nova/intrusion can hand back a confirmed intruder's face — it's
    @require_admin (websocket.py) specifically so a read-only user can never
    reach it. A read-only token must be refused, not merely limited."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "nova/intrusion", "action": "status"})
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_open_read_command_allowed_for_non_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    """nova/get_panel_data is intentionally left open (pure read, no secrets —
    see the admin-gate policy note in websocket.py). A read-only user must
    still be able to load the dashboard; don't let a blanket admin check creep
    in here as a "safer by default" change without a reason."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "nova/get_panel_data"})
    resp = await client.receive_json()

    assert resp["success"] is True


async def test_list_models_rejects_non_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    """Model discovery can access stored credentials and is admin only."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "nova/list_models", "provider": "ollama"})
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("message", [
    {
        "type": "nova/test_provider_endpoint",
        "provider": "ollama",
        "endpoint": "http://ollama.lan:11434",
    },
    {
        "type": "nova/apply_ai_config",
        "updates": {"ollama_num_ctx": 8192},
    },
])
async def test_self_hosted_setup_commands_reject_non_admin(
    hass, hass_ws_client, hass_read_only_access_token, message,
):
    """Testing or applying an endpoint is an administrator-only action."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id(message)
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_endpoint_test_schema_rejects_browser_supplied_credential(
    hass, hass_ws_client,
):
    """Endpoint credentials can only come from the server-side secrets store."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/test_provider_endpoint",
        "provider": "ollama",
        "endpoint": "http://ollama.lan:11434",
        "api_key": "must-not-be-accepted",
    })
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "invalid_format"


@pytest.mark.parametrize("endpoint", [
    "http://169.254.169.254/v1",
    "http://metadata.google.internal/v1",
    "http://[fe80::1]:11434",
])
async def test_endpoint_test_refuses_metadata_and_link_local_destinations(
    hass, hass_ws_client, aioclient_mock, endpoint,
):
    """A staged endpoint must not reach cloud metadata or link-local
    addresses; nothing is sent and the safe invalid_endpoint shape returns."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/test_provider_endpoint",
        "provider": "custom",
        "endpoint": endpoint,
    })
    resp = await client.receive_json()

    assert resp["success"] is True
    assert resp["result"]["ok"] is False
    assert resp["result"]["error"] == "invalid_endpoint"
    assert aioclient_mock.call_count == 0


async def test_endpoint_test_keeps_lan_endpoints_working(
    hass, hass_ws_client, aioclient_mock,
):
    await _setup_nova(hass)
    aioclient_mock.get("http://192.168.1.50:11434/api/tags",
                       json={"models": [{"name": "llama3:8b"}]})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/test_provider_endpoint",
        "provider": "ollama",
        "endpoint": "http://192.168.1.50:11434",
    })
    resp = await client.receive_json()

    assert resp["result"]["ok"] is True
    assert resp["result"]["models"] == ["llama3:8b"]


async def test_endpoint_test_redirect_to_metadata_is_refused(
    hass, hass_ws_client, aioclient_mock,
):
    await _setup_nova(hass)
    aioclient_mock.get("http://192.168.1.50:8000/v1/models", status=302,
                       headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/test_provider_endpoint",
        "provider": "custom",
        "endpoint": "http://192.168.1.50:8000/v1",
    })
    resp = await client.receive_json()

    assert resp["result"]["ok"] is False
    assert resp["result"]["error"] == "invalid_endpoint"
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [
        "http://192.168.1.50:8000/v1/models"]


async def test_list_models_schema_rejects_browser_supplied_base_url(
    hass, hass_ws_client,
):
    """A browser supplied destination is rejected before the handler runs."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/list_models",
        "provider": "ollama",
        "base_url": "https://attacker.invalid/v1",
    })
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "invalid_format"


async def test_list_models_returns_safe_error_shape(hass, hass_ws_client):
    """Discovery failures expose no exception detail or upstream content."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/list_models",
        "provider": "unsupported-provider",
    })
    resp = await client.receive_json()

    assert resp["success"] is True
    assert resp["result"] == {
        "provider": "unsupported-provider",
        "models": [],
        "error": "model_discovery_unavailable",
    }


async def test_list_models_paginates_gemini_across_pages(hass, hass_ws_client, aioclient_mock, tmp_path, monkeypatch):
    """Gemini's official ListModels pagination: page 1 has no pageToken,
    page 2 is requested with the exact opaque token page 1 returned — the
    result is the union, deduped and sorted (Phase 3, v7.108.0)."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await ha_secrets.async_set_provider_credential(hass, "gemini", "AIzaTestGeminiKey")

    base = "https://generativelanguage.googleapis.com/v1beta/models"
    # More specific (has pageToken) registered first — aioclient_mock does a
    # present-subset match, so a less specific mock registered first would
    # also match the second page's request (it too carries pageSize=100).
    aioclient_mock.get(base, params={"pageToken": "page-2-token"}, json={
        "models": [{"name": "models/gemini-legacy", "supportedGenerationMethods": ["generateContent"]}],
    })
    aioclient_mock.get(base, params={"pageSize": "100"}, json={
        "models": [{"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]}],
        "nextPageToken": "page-2-token",
    })

    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "gemini"})
    resp = await client.receive_json()

    assert resp["success"] is True
    assert resp["result"]["models"] == ["gemini-3.6-flash", "gemini-legacy"]
    assert resp["result"]["truncated"] is False
    assert resp["result"]["cached"] is False


async def test_list_models_second_call_is_served_from_cache(hass, hass_ws_client, aioclient_mock, tmp_path, monkeypatch):
    """A second nova/list_models call for the same provider within the TTL
    doesn't repeat the HTTP fetch (Phase 3, v7.108.0)."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await ha_secrets.async_set_provider_credential(hass, "groq", "gsk_test_key")

    aioclient_mock.get(
        "https://api.groq.com/openai/v1/models",
        json={"data": [{"id": "llama-3.3-70b-versatile"}]},
    )

    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    first = await client.receive_json()
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    second = await client.receive_json()

    assert first["result"]["cached"] is False
    assert second["result"]["cached"] is True
    assert second["result"]["models"] == first["result"]["models"]
    assert aioclient_mock.call_count == 1


async def test_list_models_refresh_bypasses_cache_with_no_url_param(hass, hass_ws_client, aioclient_mock, tmp_path, monkeypatch):
    """`refresh: true` forces a fresh fetch — still only a boolean, never a
    caller-supplied destination."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await ha_secrets.async_set_provider_credential(hass, "groq", "gsk_test_key")

    aioclient_mock.get(
        "https://api.groq.com/openai/v1/models",
        json={"data": [{"id": "llama-3.3-70b-versatile"}]},
    )

    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    await client.receive_json()
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq", "refresh": True})
    second = await client.receive_json()

    assert second["result"]["cached"] is False
    assert aioclient_mock.call_count == 2


async def test_list_models_auth_failure_is_not_cached_as_success(hass, hass_ws_client, aioclient_mock, tmp_path, monkeypatch):
    """A 401 must never be cached as an empty successful list — the very
    next call must retry, not silently keep serving 'no models'."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await ha_secrets.async_set_provider_credential(hass, "groq", "gsk_bad_key")

    aioclient_mock.get("https://api.groq.com/openai/v1/models", status=401)

    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    first = await client.receive_json()
    assert first["result"]["error"] == "model_discovery_unavailable"
    assert "cached" not in first["result"]

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        "https://api.groq.com/openai/v1/models",
        json={"data": [{"id": "llama-3.3-70b-versatile"}]},
    )
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    second = await client.receive_json()
    assert second["result"]["models"] == ["llama-3.3-70b-versatile"]
    assert second["result"]["cached"] is False  # proves the 401 was never cached


async def test_list_models_cache_invalidated_when_credential_changes(hass, hass_ws_client, aioclient_mock, tmp_path, monkeypatch):
    """Setting a new credential for a provider must drop any cached list
    fetched under the old (or no) credential."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")

    aioclient_mock.get("https://api.groq.com/openai/v1/models", status=401)

    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    first = await client.receive_json()
    assert first["result"]["error"] == "model_discovery_unavailable"

    await client.send_json_auto_id({
        "type": "nova/set_credential", "provider": "groq", "value": "gsk_now_valid",
    })
    await client.receive_json()

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        "https://api.groq.com/openai/v1/models",
        json={"data": [{"id": "llama-3.3-70b-versatile"}]},
    )
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "groq"})
    second = await client.receive_json()
    assert second["result"]["models"] == ["llama-3.3-70b-versatile"]


async def test_list_models_cache_invalidated_when_base_url_changes(hass, hass_ws_client, aioclient_mock):
    """Changing llm_base_url (custom/ollama's saved endpoint identity) must
    drop any cached list fetched under the old endpoint."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    aioclient_mock.get("https://old.example.test/v1/models", json={"data": [{"id": "old-model"}]})
    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "llm_base_url", "value": "https://old.example.test/v1",
    })
    await client.receive_json()
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "custom"})
    first = await client.receive_json()
    assert first["result"]["models"] == ["old-model"]

    await client.send_json_auto_id({
        "type": "nova/update_config", "key": "llm_base_url", "value": "https://new.example.test/v1",
    })
    await client.receive_json()
    aioclient_mock.get("https://new.example.test/v1/models", json={"data": [{"id": "new-model"}]})
    await client.send_json_auto_id({"type": "nova/list_models", "provider": "custom"})
    second = await client.receive_json()
    assert second["result"]["models"] == ["new-model"]
    assert second["result"]["cached"] is False


async def test_get_credential_status_rejects_non_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    """Credential status reveals which providers are configured — admin only,
    same as nova/list_models (Phase 2, v7.107.0)."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "nova/get_credential_status"})
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_get_credential_status_reports_availability_independently(
    hass, hass_ws_client, tmp_path, monkeypatch,
):
    """Phase 3, v7.108.0: `available` tracks each provider's own evidence —
    setting Groq's credential must not mark OpenAI or Anthropic available,
    and custom/ollama follow the endpoint rule, not any credential."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await _setup_nova(hass)  # test entry seeds llm_base_url (ollama) — custom/ollama already True
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "nova/get_credential_status"})
    before = (await client.receive_json())["result"]["available"]
    assert before["groq"] is False
    assert before["openai"] is False
    assert before["ollama"] is True  # always — has a working default

    await client.send_json_auto_id({
        "type": "nova/set_credential", "provider": "groq", "value": "gsk_test",
    })
    await client.receive_json()

    await client.send_json_auto_id({"type": "nova/get_credential_status"})
    after = (await client.receive_json())["result"]["available"]

    assert after["groq"] is True          # the one that actually changed
    assert after["openai"] is False       # untouched by groq's credential
    assert after["anthropic"] is False    # untouched by groq's credential
    assert after["gemini"] is False       # untouched by groq's credential
    assert after["custom"] == before["custom"]    # unrelated to any credential
    assert after["ollama"] == before["ollama"]    # unrelated to any credential


async def test_get_credential_status_returns_booleans_only(hass, hass_ws_client):
    """The status payload never carries a credential value — only whether
    each provider has one configured."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "nova/get_credential_status"})
    resp = await client.receive_json()

    assert resp["success"] is True
    status = resp["result"]["status"]
    assert set(status) == {"groq", "openai", "anthropic", "gemini", "custom", "ollama"}
    assert all(isinstance(v, bool) for v in status.values())


async def test_set_and_delete_credential_reject_non_admin(
    hass, hass_ws_client, hass_read_only_access_token,
):
    """Writing or clearing a stored provider credential is admin only."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({
        "type": "nova/set_credential", "provider": "groq", "value": "x",
    })
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"

    await client.send_json_auto_id({"type": "nova/delete_credential", "provider": "groq"})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_set_credential_never_echoes_the_value_back(
    hass, tmp_path, monkeypatch, hass_ws_client,
):
    """The success response carries only `ok` — never the value just stored,
    and status afterwards flips to configured without ever exposing it."""
    from custom_components.nova import ha_secrets

    monkeypatch.setattr(ha_secrets, "SECRETS_PATH", tmp_path / "secrets.yaml")
    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({
        "type": "nova/set_credential", "provider": "groq", "value": "gsk_super_secret_value",
    })
    resp = await client.receive_json()
    assert resp["success"] is True
    assert resp["result"] == {"ok": True}
    assert "gsk_super_secret_value" not in str(resp)

    await client.send_json_auto_id({"type": "nova/get_credential_status"})
    status_resp = await client.receive_json()
    assert status_resp["result"]["status"]["groq"] is True

    await client.send_json_auto_id({"type": "nova/delete_credential", "provider": "groq"})
    delete_resp = await client.receive_json()
    assert delete_resp["success"] is True
    assert delete_resp["result"] == {"ok": True}


async def test_update_config_rejects_every_credential_key(hass, hass_ws_client):
    """nova/update_config (the generic panel autosave path) must never accept
    a credential key — it belongs only to nova/set_credential/delete_credential
    (Phase 2, v7.107.0). Defense in depth: none of these are in
    PANEL_WRITABLE_KEYS today either, but this closes the path even if that
    allowlist is ever edited by mistake."""
    from custom_components.nova import ha_secrets

    await _setup_nova(hass)
    client = await hass_ws_client(hass)

    for key in ha_secrets.CREDENTIAL_KEYS:
        await client.send_json_auto_id({
            "type": "nova/update_config", "key": key, "value": "should-never-be-stored",
        })
        resp = await client.receive_json()
        assert resp["success"] is False, key
        assert resp["error"]["code"] == "invalid_key", key


async def test_intrusion_snapshot_reaches_panel_only_via_admin_websocket(
    hass, tmp_path, monkeypatch, hass_ws_client, hass_client,
):
    """End-to-end proof of the Finding-1 guarantee: a captured snapshot's
    bytes come back base64-inlined through the admin-gated nova/intrusion
    command, and the same file is never reachable over plain HTTP — there is
    no /local/nova/intrusion/... path serving it (see intrusion.py's module
    docstring)."""
    from custom_components.nova import intrusion

    snap_dir = tmp_path / "intrusion"
    snap_dir.mkdir()
    fname = "intrusion_camera_front_123.jpg"
    fake_jpeg = b"\xff\xd8\xff\xe0not-a-real-jpeg-but-good-enough"
    (snap_dir / fname).write_bytes(fake_jpeg)

    monkeypatch.setattr(intrusion, "SNAPSHOT_DIR", str(snap_dir))
    monkeypatch.setattr(intrusion, "LOG_PATH", tmp_path / "intrusion_log.json")
    monkeypatch.setattr(intrusion, "_log", [], raising=False)
    monkeypatch.setattr(intrusion, "_log_loaded", True, raising=False)

    intrusion.record_event(
        "confirmed", reason="test", camera="camera.front",
        snapshot={"path": str(snap_dir / fname), "camera": "camera.front"},
    )

    await _setup_nova(hass)

    # 1. Admin websocket command returns the image, inlined as base64.
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "nova/intrusion", "action": "log", "limit": 5})
    resp = await ws.receive_json()
    assert resp["success"] is True
    events = resp["result"]["events"]
    assert events, "expected the seeded intrusion event to come back"
    assert events[0]["image_b64"] == base64.b64encode(fake_jpeg).decode("ascii")
    assert "url" not in events[0]

    # 2. The same file is not reachable over plain HTTP under /local/ or any
    # other path — confirms there is genuinely no unauthenticated static
    # route serving it, not just that the panel avoids using one.
    http_client = await hass_client()
    resp_local = await http_client.get(f"/local/nova/intrusion/{fname}")
    assert resp_local.status == 404


# ─── Decision browser (Phase 1: decision explanations + feedback) ───────────

@pytest.mark.parametrize("msg", [
    {"type": "nova/list_decisions"},
    {"type": "nova/get_decision", "decision_id": 1},
    {"type": "nova/set_decision_outcome", "decision_id": 1, "verdict": "good"},
    {"type": "nova/replay_decision", "decision_id": 1},
])
async def test_decision_commands_reject_non_admin(
    hass, hass_ws_client, hass_read_only_access_token, msg,
):
    """All four decision-drawer commands can reveal or judge household
    decision content — none of them may be reachable by a read-only user."""
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id(msg)
    resp = await client.receive_json()

    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


def _isolate_decisions_db(monkeypatch, tmp_path):
    from custom_components.nova import decision_record
    monkeypatch.setattr(decision_record, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    return decision_record


async def test_list_decisions_paginates_and_get_decision_returns_full_row(
    hass, tmp_path, monkeypatch, hass_ws_client,
):
    """Exercises the real handlers end-to-end (not just permission-gating):
    pagination actually advances, and the detail call returns the fields the
    Logs-tab drawer needs."""
    dr = _isolate_decisions_db(monkeypatch, tmp_path)
    ids = [dr.record("intrusion", observation={"n": i}, ts=float(100 + i)) for i in range(3)]
    await _setup_nova(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "nova/list_decisions", "limit": 2})
    page1 = await ws.receive_json()
    assert page1["success"] is True
    assert [d["id"] for d in page1["result"]["decisions"]] == [ids[2], ids[1]]
    cursor = page1["result"]["next_cursor"]
    assert cursor is not None

    await ws.send_json_auto_id({
        "type": "nova/list_decisions", "limit": 2,
        "cursor_ts": cursor["ts"], "cursor_id": cursor["id"],
    })
    page2 = await ws.receive_json()
    assert [d["id"] for d in page2["result"]["decisions"]] == [ids[0]]
    assert page2["result"]["next_cursor"] is None

    await ws.send_json_auto_id({"type": "nova/get_decision", "decision_id": ids[0]})
    detail = await ws.receive_json()
    assert detail["success"] is True
    assert detail["result"]["decision"]["observation"] == {"n": 0}
    assert detail["result"]["decision"]["kind"] == "intrusion"


async def test_get_decision_truncates_and_redacts(hass, tmp_path, monkeypatch, hass_ws_client):
    """A calendar-title-shaped free-text field over the cap comes back
    truncated, and a credential-shaped key comes back redacted — proven
    against the REAL handler, since this composition can't run in tests/unit/
    (websocket.py needs a real websocket_api to import)."""
    dr = _isolate_decisions_db(monkeypatch, tmp_path)
    long_title = "Doctor's appointment " + ("x" * 600)
    rid = dr.record(
        "anticipation_departure",
        observation={"event": long_title},
        evidence={"api_key": "sk-should-never-reach-the-panel"},
    )
    await _setup_nova(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "nova/get_decision", "decision_id": rid})
    resp = await ws.receive_json()

    assert resp["success"] is True
    d = resp["result"]["decision"]
    assert d["observation"]["event"].endswith("…")
    assert len(d["observation"]["event"]) == 501  # 500-char cap + the "…" marker
    assert d["evidence"]["api_key"] == "**REDACTED**"


async def test_set_decision_outcome_status_transitions(hass, tmp_path, monkeypatch, hass_ws_client):
    dr = _isolate_decisions_db(monkeypatch, tmp_path)
    rid = dr.record("suggestion")
    await _setup_nova(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "nova/set_decision_outcome", "decision_id": rid, "verdict": "good"})
    first = await ws.receive_json()
    assert first["result"]["status"] == "ok"

    await ws.send_json_auto_id({"type": "nova/set_decision_outcome", "decision_id": rid, "verdict": "wrong"})
    second = await ws.receive_json()
    assert second["result"]["status"] == "already_judged"

    await ws.send_json_auto_id({"type": "nova/set_decision_outcome", "decision_id": 99999, "verdict": "wrong"})
    third = await ws.receive_json()
    assert third["result"]["status"] == "not_found"


# ─── Decision Lab (Phase 4: current-policy replay) ──────────────────────────

async def test_replay_decision_end_to_end(hass, tmp_path, monkeypatch, hass_ws_client):
    """Exercises the real handler (not just permission-gating): a suggestion
    decision replays against the real, live pattern_analyzer threshold, and
    an unsupported kind comes back clearly marked rather than erroring."""
    from custom_components.nova import pattern_analyzer
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)

    dr = _isolate_decisions_db(monkeypatch, tmp_path)
    suggestion_id = dr.record("suggestion", decision="propose automation", confidence=0.9)
    intrusion_id = dr.record("intrusion", decision="raise intrusion alert", confidence=0.9)
    await _setup_nova(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "nova/replay_decision", "decision_id": suggestion_id})
    supported = await ws.receive_json()
    assert supported["success"] is True
    r = supported["result"]
    assert r["supported"] is True
    assert r["current_threshold"] == 0.65
    assert r["would_pass_current_threshold"] is True
    assert "original_would_act" not in r
    assert r["label"] == ("Replay using current settings. This is not an "
                          "exact reconstruction of the original decision.")

    await ws.send_json_auto_id({"type": "nova/replay_decision", "decision_id": intrusion_id})
    unsupported = await ws.receive_json()
    assert unsupported["result"]["supported"] is False
    assert "current_threshold" not in unsupported["result"]

    await ws.send_json_auto_id({"type": "nova/replay_decision", "decision_id": 99999})
    missing = await ws.receive_json()
    assert missing["success"] is False
    assert missing["error"]["code"] == "not_found"
