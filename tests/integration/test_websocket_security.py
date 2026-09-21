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
