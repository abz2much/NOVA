"""The Faces commands and the face_stand_down setting, against a real Home
Assistant instance: the admin gate, validation, the saved 0600 file and the
panel data."""
import json
import os
import stat

from homeassistant.setup import async_setup_component

from .conftest import MockConfigEntry
from .test_wiring_smoke import _make_entry


async def _setup_nova(hass) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_admin_adds_lists_and_removes_residents(hass, hass_ws_client):
    from custom_components.nova import face_roster, recognition
    await _setup_nova(hass)
    face_roster._NAMES.clear()
    recognition._FACE_LOG.clear()
    recognition._RECOGNITION_CACHE.clear()
    client = await hass_ws_client(hass)
    r = await _call(client, type="nova/add_resident", name="  Sam  ")
    assert r["success"] and r["result"]["added"] is True and r["result"]["residents"] == ["Sam"]
    r = await _call(client, type="nova/add_resident", name="sam")
    assert r["success"] and r["result"]["added"] is False
    recognition.remember_recognition("front", "Sam", 92.0, source="frigate")
    recognition.remember_recognition("garden", "unknown", 10.0, source="doubletake")
    r = await _call(client, type="nova/list_faces")
    assert r["success"]
    res = r["result"]
    assert res["residents"] == ["Sam"] and res["confidence_threshold"] == 60
    by = {f["name"]: f for f in res["faces"]}
    assert by["Sam"]["resident"] is True and by["Sam"]["known"] is True
    assert by["unknown"]["known"] is False
    assert all(set(f) == {"name", "camera", "camera_entity", "confidence", "age_seconds",
                          "known", "resident", "source"} for f in res["faces"])
    path = face_roster._path()
    assert json.load(open(path)) == {"residents": ["Sam"]}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    r = await _call(client, type="nova/remove_resident", name="SAM")
    assert r["success"] and r["result"]["removed"] is True and r["result"]["residents"] == []
    assert json.load(open(path)) == {"residents": []}


async def test_bad_names_are_refused(hass, hass_ws_client):
    from custom_components.nova import face_roster
    await _setup_nova(hass)
    face_roster._NAMES.clear()
    client = await hass_ws_client(hass)
    for bad in ("", "   ", "x" * 61, "Sa\x00m", "unknown", "Unknown Person"):
        r = await _call(client, type="nova/add_resident", name=bad)
        assert r["success"] is False and r["error"]["code"] == "invalid_name", bad
    r = await _call(client, type="nova/add_resident", name=5)
    assert r["success"] is False
    assert face_roster.names() == []


async def test_all_three_commands_need_an_admin(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    for msg in ({"type": "nova/list_faces"},
                {"type": "nova/add_resident", "name": "Sam"},
                {"type": "nova/remove_resident", "name": "Sam"}):
        r = await _call(client, **msg)
        assert r["success"] is False, msg
        assert r["error"]["code"] == "unauthorized", msg


async def test_face_stand_down_write_path_and_panel_data(hass, hass_ws_client):
    await _setup_nova(hass)
    client = await hass_ws_client(hass)
    r = await _call(client, type="nova/get_panel_data")
    assert r["result"]["config"]["face_stand_down"] is False          # off by default
    for bad in ("true", 1, "on", None):
        r = await _call(client, type="nova/update_config", key="face_stand_down", value=bad)
        assert r["success"] is False, bad
    r = await _call(client, type="nova/update_config", key="face_stand_down", value=True)
    assert r["success"] is True
    r = await _call(client, type="nova/get_panel_data")
    assert r["result"]["config"]["face_stand_down"] is True
    r = await _call(client, type="nova/update_config", key="face_stand_down", value=False)
    assert r["success"] is True
    r = await _call(client, type="nova/get_panel_data")
    assert r["result"]["config"]["face_stand_down"] is False


async def test_roster_loads_from_disk_at_setup(hass):
    from custom_components.nova import face_roster, paths
    paths.configure(hass)          # this instance's own config folder, as setup does
    face_roster._NAMES.clear()
    path = face_roster._path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"residents": ["Anna", "unknown", 7]}, f)
    await _setup_nova(hass)
    assert face_roster.names() == ["Anna"]
    os.unlink(path)
