"""Nova Voice ID 8.36.0: where the address and token live, and discovery.

Discovery fills a field that is empty or still holds what the last discovery
sent. A value the user typed always wins. The token lives in secrets.yaml,
never config.json, and only its SHA-256 is kept to make the check.
"""
import hashlib

import pytest

URI_A = "tcp://abc-nova-voice-id:10350"
URI_B = "tcp://def-nova-voice-id:10350"


@pytest.fixture
def store(load, monkeypatch, tmp_path):
    mod = load("voice_id_store")
    nova_config = load("nova_config")
    ha_secrets = load("ha_secrets")
    cfg: dict = {}
    monkeypatch.setattr(nova_config, "get", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(nova_config, "set", lambda key, value: cfg.__setitem__(key, value) or True)
    monkeypatch.setattr(nova_config, "runtime_get",
                        lambda hass, entry, key, default=None: cfg.get(key, default))
    secrets = tmp_path / "secrets.yaml"
    monkeypatch.setattr(ha_secrets, "_secrets_path", lambda: secrets)
    ha_secrets._reset_secrets_cache()
    mod._cfg = cfg
    mod._secrets_file = secrets
    yield mod
    ha_secrets._reset_secrets_cache()


async def _apply(store, hass, uri, token):
    return await store.async_apply_discovery(hass, None, uri, token)


async def test_discovery_fills_empty_fields(store, fake_hass):
    got = await _apply(store, fake_hass, URI_A, "tok-1")
    assert got == {"uri": True, "token": True}
    assert store._cfg["voice_id_uri"] == URI_A
    assert await store.async_get_token(fake_hass) == "tok-1"


async def test_discovery_updates_a_field_still_holding_the_last_discovered_value(store, fake_hass):
    await _apply(store, fake_hass, URI_A, "tok-1")
    got = await _apply(store, fake_hass, URI_B, "tok-2")
    assert got == {"uri": True, "token": True}
    assert store._cfg["voice_id_uri"] == URI_B
    assert await store.async_get_token(fake_hass) == "tok-2"


async def test_a_typed_value_always_wins(store, fake_hass):
    await _apply(store, fake_hass, URI_A, "tok-1")
    store._cfg["voice_id_uri"] = "tcp://my-own:10350"          # typed in the panel
    await store.async_set_token(fake_hass, "typed-token")
    got = await _apply(store, fake_hass, URI_B, "tok-2")
    assert got == {"uri": False, "token": False}
    assert store._cfg["voice_id_uri"] == "tcp://my-own:10350"
    assert await store.async_get_token(fake_hass) == "typed-token"
    # ...and again on the next restart
    await _apply(store, fake_hass, URI_B, "tok-2")
    assert store._cfg["voice_id_uri"] == "tcp://my-own:10350"
    assert await store.async_get_token(fake_hass) == "typed-token"


async def test_a_typed_value_set_before_any_discovery_wins(store, fake_hass):
    store._cfg["voice_id_uri"] = "tcp://my-own:10350"
    await store.async_set_token(fake_hass, "typed-token")
    assert await _apply(store, fake_hass, URI_A, "tok-1") == {"uri": False, "token": False}


async def test_the_token_never_reaches_config_json(store, fake_hass):
    await _apply(store, fake_hass, URI_A, "tok-secret")
    assert "tok-secret" not in repr(store._cfg)
    assert store._cfg["voice_id_token_discovered_sha256"] == \
        hashlib.sha256(b"tok-secret").hexdigest()
    assert "nova_voice_id_token" in store._secrets_file.read_text()


async def test_clearing_the_token_deletes_the_secret(store, fake_hass):
    await store.async_set_token(fake_hass, "tok")
    await store.async_set_token(fake_hass, "")
    assert await store.async_get_token(fake_hass) == ""


@pytest.mark.parametrize("token,ok", [("abc-123_X", True), ("", False), ("a b", False),
                                      ("a\nb", False), ("x" * 257, False), (None, False)])
def test_valid_token(store, token, ok):
    assert store.valid_token(token) is ok


async def test_after_upgrade_from_8_35_the_discovered_address_still_counts(store, fake_hass):
    """8.35.0 saved the discovered address but no last-discovered value."""
    store._cfg["voice_id_uri"] = URI_A
    got = await _apply(store, fake_hass, URI_A, "tok-1")
    assert got["uri"] is True
    got = await _apply(store, fake_hass, URI_B, "tok-1")   # the app moved
    assert got["uri"] is True and store._cfg["voice_id_uri"] == URI_B
