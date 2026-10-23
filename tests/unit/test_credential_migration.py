"""Splitting the old shared ``api_key`` into its provider's own slot.

ha_secrets.split_shared_credential decides the owner from the saved
``llm_provider`` and, only when that is not one of the fixed cloud
providers, from a prefix the key itself proves. When neither settles it,
nothing moves and a Repair issue is raised. The new secret is written and
read back before the shared value is removed; any failure leaves the
shared value working exactly as before.
"""
from __future__ import annotations

import sys
import types

import pytest

SHARED = "test-shared-key"
NO_CHANGE = {"migrated": None, "ambiguous": False, "reason": None}


@pytest.fixture
def hs(load):
    mod = load("ha_secrets")
    mod._reset_secrets_cache()
    yield mod
    mod._reset_secrets_cache()


class _Install:
    """One install's state: the plain config (``config``), the secrets file
    (``path``), the config keys deleted, and the Repair notes raised and
    cleared."""

    def __init__(self, hs, tmp_path):
        self.hs, self.path = hs, tmp_path / "secrets.yaml"
        self.config: dict = {}
        self.deleted: list = []
        self.repairs: list = []

    def secret(self, field):
        return self.hs.get_secret_sync(self.hs.secret_key_for(field), path=self.path)

    def seed(self, text):
        self.path.write_text(text, encoding="utf-8")


@pytest.fixture
def install(hs, load, tmp_path, monkeypatch):
    """A fresh install with nova_config and repair_notices replaced by
    stand-ins, so no state is shared between tests."""
    box = _Install(hs, tmp_path)
    monkeypatch.setattr(hs, "SECRETS_PATH", box.path)
    nova_config = load("nova_config")

    def delete(key):
        box.deleted.append(key)
        box.config.pop(key, None)

    monkeypatch.setattr(nova_config, "get_all", lambda: dict(box.config))
    monkeypatch.setattr(nova_config, "delete", delete)
    monkeypatch.setitem(sys.modules, "jc.repair_notices", types.SimpleNamespace(
        note_credential_migration_ambiguous=lambda hass: box.repairs.append(("note", hass)),
        clear_credential_migration_ambiguous=lambda hass: box.repairs.append(("clear", hass)),
    ))
    return box


# ── which provider a key's prefix proves ───────────────────────────────────

@pytest.mark.parametrize("key, expected", [
    ("sk-ant-abc123", "anthropic"),
    ("gsk_abc123", "groq"),
    ("AIzaSyAbc123", "gemini"),
    ("sk-abc123", "openai"),
    ("  sk-ant-padded  ", "anthropic"),
    ("", None),
    (None, None),
    ("totally-unrecognized-shape", None),
    ("Bearer sometoken", None),
])
def test_prefix_detection_never_guesses(hs, key, expected):
    assert hs._detect_provider_from_key_strict(key) == expected


# ── moving the key ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["groq", "openai", "anthropic", "gemini"])
async def test_the_saved_provider_receives_the_key(install, fake_hass, provider):
    install.config.update(api_key=SHARED, llm_provider=provider)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result == {"migrated": provider, "ambiguous": False, "reason": None}
    assert install.deleted == ["api_key"]
    assert install.secret(f"{provider}_api_key") == SHARED
    assert install.repairs == [("clear", fake_hass)]


async def test_the_saved_provider_is_matched_ignoring_case_and_spaces(install, fake_hass):
    install.config.update(api_key=SHARED, llm_provider="  Groq ")
    assert (await install.hs.split_shared_credential(fake_hass))["migrated"] == "groq"
    assert install.secret("groq_api_key") == SHARED


async def test_without_a_saved_provider_the_prefix_decides(install, fake_hass):
    install.config["api_key"] = "sk-ant-test-key"
    result = await install.hs.split_shared_credential(fake_hass)
    assert result["migrated"] == "anthropic"
    assert install.deleted == ["api_key"]
    assert install.secret("anthropic_api_key") == "sk-ant-test-key"


async def test_the_saved_provider_beats_the_key_shape(install, fake_hass):
    # The install already said what the key is for; the prefix is a fallback.
    install.config.update(api_key="sk-looks-like-openai", llm_provider="anthropic")
    result = await install.hs.split_shared_credential(fake_hass)
    assert result["migrated"] == "anthropic"
    assert install.secret("anthropic_api_key") == "sk-looks-like-openai"
    assert install.secret("openai_api_key") in (None, "")


async def test_a_shared_key_already_in_secrets_moves_and_its_old_line_goes(install, fake_hass):
    install.seed('nova_api_key: "gsk_test-key"\n# keep me\nother: "x"\n')
    result = await install.hs.split_shared_credential(fake_hass)
    assert result["migrated"] == "groq"
    assert install.secret("groq_api_key") == "gsk_test-key"
    assert install.secret("api_key") is None
    text = install.path.read_text(encoding="utf-8")
    assert "nova_api_key:" not in text
    assert "# keep me" in text and 'other: "x"' in text


async def test_other_providers_keys_are_left_alone(install, fake_hass):
    install.seed('nova_gemini_api_key: "test-gemini-key"\n')
    install.config.update(api_key="test-groq-shared", llm_provider="groq")
    assert (await install.hs.split_shared_credential(fake_hass))["migrated"] == "groq"
    assert install.secret("gemini_api_key") == "test-gemini-key"


# ── when the owner cannot be proved: nothing moves ─────────────────────────

@pytest.mark.parametrize("config", [
    {"api_key": "unrecognized-shape-key"},
    # custom and ollama are self hosted, so never a fallback target; their
    # tokens rarely have a provable prefix either.
    {"api_key": "custom-token-xyz", "llm_provider": "custom"},
    {"api_key": "ollama-proxy-token", "llm_provider": "ollama"},
], ids=["no_provider", "custom", "ollama"])
async def test_an_unprovable_owner_moves_nothing_and_raises_a_repair(install, fake_hass, config):
    install.config.update(config)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result == {"migrated": None, "ambiguous": True,
                      "reason": "unrecognized key shape and no saved llm_provider"}
    assert install.deleted == []
    assert install.repairs == [("note", fake_hass)]
    assert not install.path.exists()        # nothing written anywhere


# ── a provider key that already exists is never overwritten ────────────────

@pytest.mark.parametrize("seed, config_extra", [
    ('nova_groq_api_key: "test-existing"\n', {}),
    # An older install may already hold groq_api_key, under the very name
    # groq's own slot uses, with a blank plain copy beside it.
    ('nova_groq_api_key: "test-existing"\n', {"groq_api_key": ""}),
], ids=["in_secrets", "alias_with_blank_plain_copy"])
async def test_an_existing_secret_is_kept(install, fake_hass, seed, config_extra):
    install.seed(seed)
    install.config.update(api_key="test-other-shared", llm_provider="groq", **config_extra)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result == {"migrated": "already_present", "ambiguous": False, "reason": None}
    assert install.deleted == []
    assert install.secret("groq_api_key") == "test-existing"


async def test_an_existing_plain_provider_key_is_kept(install, fake_hass):
    install.config.update(api_key=SHARED, llm_provider="openai",
                          openai_api_key="test-existing-plain")
    result = await install.hs.split_shared_credential(fake_hass)
    assert result["migrated"] == "already_present"
    assert install.deleted == []
    assert install.secret("openai_api_key") is None      # nothing written


# ── nothing to do ──────────────────────────────────────────────────────────

async def test_no_shared_key_changes_nothing(install, fake_hass):
    install.config["llm_provider"] = "groq"
    assert await install.hs.split_shared_credential(fake_hass) == NO_CHANGE
    assert install.repairs == [] and install.deleted == []


async def test_an_unreadable_config_changes_nothing(install, fake_hass, load, monkeypatch):
    def broken():
        raise OSError("config.json unreadable")
    monkeypatch.setattr(load("nova_config"), "get_all", broken)
    assert await install.hs.split_shared_credential(fake_hass) == NO_CHANGE


async def test_a_second_run_is_a_no_op(install, fake_hass):
    install.config.update(api_key=SHARED, llm_provider="groq")
    assert (await install.hs.split_shared_credential(fake_hass))["migrated"] == "groq"
    assert await install.hs.split_shared_credential(fake_hass) == NO_CHANGE
    assert install.deleted == ["api_key"]
    assert install.secret("groq_api_key") == SHARED


# ── failures leave the shared key working ──────────────────────────────────

async def test_a_failed_write_keeps_the_shared_key(install, fake_hass, monkeypatch):
    install.config.update(api_key=SHARED, llm_provider="groq")
    monkeypatch.setattr(install.hs, "set_secret_sync", lambda key, value, path=None: False)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result == {"migrated": None, "ambiguous": False, "reason": "write_failed"}
    assert install.deleted == []
    assert install.config["api_key"] == SHARED


async def test_a_read_back_that_differs_keeps_the_shared_key(install, fake_hass, monkeypatch):
    install.config.update(api_key=SHARED, llm_provider="groq")
    real_get = install.hs.get_secret_sync
    lookups = []

    def corrupt_after_write(key, default=None, path=None):
        # The first lookup is "is there a groq key already?" and must see
        # none; the second is the read back after the write.
        lookups.append(key)
        return real_get(key, default, path) if len(lookups) == 1 else "test-wrong-value"

    monkeypatch.setattr(install.hs, "get_secret_sync", corrupt_after_write)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result["reason"] == "verify_failed" and result["migrated"] is None
    assert install.deleted == []
    assert lookups == ["nova_groq_api_key", "nova_groq_api_key"]


async def test_an_error_while_removing_the_old_copy_is_reported(install, fake_hass, load,
                                                               monkeypatch):
    install.config.update(api_key=SHARED, llm_provider="groq")

    def broken_delete(key):
        raise OSError("config.json read only")
    monkeypatch.setattr(load("nova_config"), "delete", broken_delete)
    result = await install.hs.split_shared_credential(fake_hass)
    assert result == {"migrated": None, "ambiguous": False, "reason": "error"}
    # The new secret is in place, and the shared one still works too.
    assert install.secret("groq_api_key") == SHARED
    assert install.config["api_key"] == SHARED
    assert install.repairs == []
