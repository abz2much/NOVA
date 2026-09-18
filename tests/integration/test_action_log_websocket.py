"""Action Audit Log (nova/list_actions) against a real Home Assistant
instance (PHACC). Covers what a fake `hass` can't credibly prove: that
@require_admin actually rejects a non-admin connection, that a real admin
connection gets a real paginated result back, that the db path is resolved
through hass.config.path(...) rather than a hardcoded /config, and that one
request with more targets than the page limit is still returned as one
complete group — never split across two pages. See test_spoken_history.py
for the precedent this file follows.
"""
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_admin_required_for_list_actions(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json_auto_id({"type": "nova/list_actions"})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_admin_receives_real_paginated_result(hass, hass_ws_client, tmp_path, monkeypatch):
    from custom_components.nova import action_log

    await _setup_nova(hass)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    rid = action_log.new_request_id()
    aid = action_log.start(
        rid, "control_device", "voice", domain="light", entity_id="light.kitchen",
        db_path=db_path,
    )
    action_log.set_execution(aid, "accepted", db_path=db_path)
    action_log.set_execution(aid, "verified", db_path=db_path)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_actions"})
    resp = await client.receive_json()

    assert resp["success"] is True, resp
    requests = resp["result"]["requests"]
    assert len(requests) == 1
    row = requests[0]
    assert row["request_id"] == rid
    assert row["action"] == "control_device"
    assert row["status"] == "success"
    assert row["targets"][0]["execution_result"] == "verified"


async def test_bulk_request_never_split_across_pages(hass, hass_ws_client, tmp_path, monkeypatch):
    """The exact scenario correction #4 requires a test for: one request
    with MORE targets than the page limit is still returned as one
    complete group on one page, not split."""
    from custom_components.nova import action_log

    await _setup_nova(hass)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)

    rid_big = action_log.new_request_id()
    action_log.start_many(
        rid_big, "bulk_control", "voice",
        [{"entity_id": f"light.{i}"} for i in range(8)],
        db_path=db_path,
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_actions", "limit": 3})
    resp = await client.receive_json()

    assert resp["success"] is True, resp
    matching = [r for r in resp["result"]["requests"] if r["request_id"] == rid_big]
    assert len(matching) == 1
    assert len(matching[0]["targets"]) == 8, (
        "all 8 targets of the one bulk request must be present together, "
        "even though the page limit (3) is smaller than the group"
    )


async def test_db_path_resolved_through_hass_config_not_hardcoded(hass):
    """configure() must point action_log at THIS instance's own reported
    config directory, matching spoken_history.py's own portability fix —
    never a literal /config path."""
    from custom_components.nova import action_log

    await _setup_nova(hass)
    assert action_log._DEFAULT_DB == hass.config.path("nova", "conversations.db")


async def test_setup_succeeds_when_action_log_configure_fails(hass, monkeypatch):
    """A broken action log module must fail open — it can never block Nova
    from loading at all."""
    from custom_components.nova import action_log

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(action_log, "configure", _boom)
    await _setup_nova(hass)  # must not raise / must still return True


async def test_spoken_history_link_resolved_for_matching_request(
    hass, hass_ws_client, tmp_path, monkeypatch,
):
    """When a spoken_history row was recorded with this action's
    request_id, nova/list_actions surfaces a spoken_history_id reference —
    never the spoken text itself."""
    from custom_components.nova import action_log, spoken_history

    await _setup_nova(hass)
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(action_log, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(spoken_history, "_DEFAULT_DB", db_path)

    rid = action_log.new_request_id()
    action_log.start(rid, "notify", "proactive", domain="notify", service="mobile_app_abi",
                      db_path=db_path)
    spoken_id = spoken_history.record(
        "Welcome home, sir.", "welcome", ["media_player.kitchen"],
        db_path=db_path, action_request_id=rid,
    )
    assert spoken_id is not None

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_actions"})
    resp = await client.receive_json()

    assert resp["success"] is True, resp
    row = next(r for r in resp["result"]["requests"] if r["request_id"] == rid)
    assert row["spoken_history_id"] == spoken_id
    assert "text" not in row  # never the spoken text itself
