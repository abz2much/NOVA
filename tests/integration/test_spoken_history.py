"""Spoken History (v7.104.0) against a real Home Assistant instance (PHACC).

Covers what a fake `hass` can't credibly prove: that @require_admin actually
rejects a non-admin connection for the two new commands, that a real
media_player.play_media service call flowing through the unmodified
async_announce() creates exactly one history row when the panel's Repeat
button is used (never a duplicate between the websocket handler and
async_announce's own recorder), that the db path is resolved through
hass.config.path(...) rather than a hardcoded /config, and that a broken
history store fails setup open rather than blocking it. See
tests/integration/conftest.py for setup and test_wiring_smoke.py for why
this needs PHACC instead of tests/unit/'s fakes.
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


async def test_admin_required_for_get_spoken_history(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json_auto_id({"type": "nova/get_spoken_history"})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_admin_required_for_repeat_spoken(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_nova(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json_auto_id({"type": "nova/repeat_spoken", "spoken_id": 1})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unauthorized"


async def test_panel_repeat_creates_exactly_one_new_row(hass, hass_ws_client, tmp_path, monkeypatch):
    """The exact scenario correction #2 requires a test for: async_announce
    is the ONLY recorder for anything routed through it, so ws_repeat_spoken
    (which calls async_announce and must NOT also call spoken_history.record
    itself) produces exactly one new row per repeat — never zero, never two."""
    from custom_components.nova import spoken_history

    # A real media_player.play_media registration lets the unmodified
    # async_announce() run for real (including its own internal recording
    # call) without needing an actual speaker/TTS entity.
    calls = []

    async def _fake_play_media(call):
        calls.append(call.data)

    hass.services.async_register("media_player", "play_media", _fake_play_media)

    await _setup_nova(hass)
    hass.states.async_set("tts.piper", "idle")  # so resolve_tts_entity's auto-pick finds something
    hass.states.async_set("media_player.kitchen", "idle")  # the original speaker — must still exist
    # configure() (called during setup) points _DEFAULT_DB at this hass
    # instance's own config dir — override AFTER setup so this test's rows
    # land in an isolated tmp_path file instead.
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(spoken_history, "_DEFAULT_DB", db_path)

    original_id = spoken_history.record(
        "Welcome home, sir.", "welcome", ["media_player.kitchen"], db_path=db_path,
    )
    assert original_id is not None
    before = spoken_history.list_recent(db_path=db_path)
    assert len(before) == 1

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/repeat_spoken", "spoken_id": original_id})
    resp = await client.receive_json()

    assert resp["success"] is True, resp
    assert resp["result"]["ok"] is True
    assert len(calls) == 1  # exactly one play_media call — Home Assistant "accepted" it

    after = spoken_history.list_recent(db_path=db_path)
    assert len(after) == 2, "exactly one new row — the repeat — must exist, never zero or more than one"
    new_row = after[0]  # newest first
    assert new_row["source"] == "repeat"
    assert new_row["repeat_of_id"] == original_id
    assert new_row["text"] == "Welcome home, sir."


async def test_repeat_falls_back_to_configured_default_when_original_speaker_gone(
    hass, hass_ws_client, tmp_path, monkeypatch,
):
    from custom_components.nova import spoken_history, nova_config

    calls = []

    async def _fake_play_media(call):
        calls.append(call.data)

    hass.services.async_register("media_player", "play_media", _fake_play_media)

    await _setup_nova(hass)
    hass.states.async_set("tts.piper", "idle")
    hass.states.async_set("media_player.default_speaker", "idle")
    db_path = str(tmp_path / "conversations.db")
    monkeypatch.setattr(spoken_history, "_DEFAULT_DB", db_path)
    nova_config.set("announcement_speakers", ["media_player.default_speaker"])

    original_id = spoken_history.record(
        "Reminder: take out the bins.", "reminder",
        ["media_player.long_gone"], db_path=db_path,
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "nova/repeat_spoken", "spoken_id": original_id})
    resp = await client.receive_json()

    assert resp["success"] is True, resp
    assert resp["result"]["ok"] is True
    after = spoken_history.list_recent(db_path=db_path)
    repeat_row = after[0]
    assert repeat_row["speakers"] == ["media_player.default_speaker"]


async def test_db_path_resolved_through_hass_config_not_hardcoded(hass):
    """configure() must point spoken_history at THIS instance's own reported
    config directory, matching nova_config.py's own portability fix —
    never a literal /config path."""
    from custom_components.nova import spoken_history

    await _setup_nova(hass)
    assert spoken_history._DEFAULT_DB == hass.config.path("nova", "conversations.db")


async def test_setup_succeeds_when_spoken_history_hydrate_fails(hass, monkeypatch):
    """A broken history store must fail open — it can never block Nova from
    loading at all."""
    from custom_components.nova import spoken_history

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(spoken_history, "hydrate", _boom)
    await _setup_nova(hass)  # must not raise / must still return True
