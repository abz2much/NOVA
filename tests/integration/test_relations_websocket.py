"""Relations (nova/list_relations, nova/relation_action, nova/edit_relation)
against a real Home Assistant instance: the admin gate, a pending relation
staying out of confirmed reads until confirmed, and the schema upgrade on a
real knowledge.db."""
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_admin_required_for_relation_writes(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    for msg in ({"type": "nova/relation_action", "relation_id": 1, "action": "confirm"},
                {"type": "nova/edit_relation", "relation_id": 1, "subject": "a"}):
        await client.send_json_auto_id(msg)
        resp = await client.receive_json()
        assert resp["success"] is False
        assert resp["error"]["code"] == "unauthorized"


async def test_pending_then_confirmed_round_trip(hass, hass_ws_client, tmp_path, monkeypatch):
    from custom_components.nova import knowledge

    await _setup_nova(hass)
    monkeypatch.setattr(knowledge, "DB_PATH", str(tmp_path / "knowledge.db"))
    made = knowledge.propose_relation("Sam", "owns", "Biscuit", source="observed")
    assert made["ok"] and made["relation"]["status"] == "pending"
    rid = made["relation"]["id"]
    assert knowledge.confirmed_relations("sam") == []

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/list_relations"})
    resp = await client.receive_json()
    assert resp["success"] is True, resp

    await client.send_json_auto_id(
        {"type": "nova/relation_action", "relation_id": rid, "action": "confirm"})
    resp = await client.receive_json()
    assert resp["success"] is True, resp
    assert [r["object"] for r in knowledge.confirmed_relations("sam")] == ["biscuit"]

    await client.send_json_auto_id(
        {"type": "nova/edit_relation", "relation_id": rid, "predicate": "unknown"})
    resp = await client.receive_json()
    assert resp["success"] is True and resp["result"]["ok"] is False
