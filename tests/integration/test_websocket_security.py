"""WebSocket security wiring tests against a real Home Assistant instance
(PHACC) — the enforcement a fake `hass` can't credibly prove: that
@require_admin actually rejects a non-admin connection, that a command left
open by design still works for one, and that intrusion snapshots really do
reach the panel only through the admin-gated command (never a plain HTTP
path). See tests/integration/conftest.py for setup and DOMAIN-level docs in
test_wiring_smoke.py for why this needs PHACC instead of tests/unit/'s fakes.
"""
import base64

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
