"""Tests for the shared-credential provider split (Phase 2, v7.107.0).

ha_secrets.split_shared_credential migrates the legacy shared `api_key` into
the provider-specific slot it actually belongs to. Ownership is decided by
the saved `llm_provider`, falling back to the key's own prefix only when
that's absent — never a guess when both are inconclusive. Write→read-back→
verify before ever deleting the legacy value; a write or verify failure
leaves it exactly as it was.
"""
from __future__ import annotations

import pytest

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


@pytest.fixture
def hs(load):
    return load("ha_secrets")


@pytest.fixture
def jc(load):
    return load("nova_config")


# ── strict key-prefix detection ───────────────────────────────────────────────

@pytest.mark.parametrize("key,expected", [
    ("sk-ant-abc123", "anthropic"),
    ("gsk_abc123", "groq"),
    ("AIzaSyAbc123", "gemini"),
    ("sk-abc123", "openai"),
    ("", None),
    ("totally-unrecognized-shape", None),
    ("Bearer sometoken", None),
])
def test_detect_provider_from_key_strict(hs, key, expected):
    assert hs._detect_provider_from_key_strict(key) == expected


# ── saved-provider-driven migration ───────────────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
@pytest.mark.parametrize("provider", ["groq", "openai", "anthropic", "gemini"])
async def test_migrates_using_saved_provider(hs, jc, fake_hass, tmp_path, monkeypatch, provider):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "SHARED-KEY", "llm_provider": provider})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == provider
    assert deleted == ["api_key"]
    assert hs.get_secret_sync(hs.secret_key_for(f"{provider}_api_key"), path=p) == "SHARED-KEY"


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_migrates_using_key_prefix_when_no_saved_provider(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "sk-ant-realanthropickey"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "anthropic"
    assert deleted == ["api_key"]
    assert hs.get_secret_sync(hs.secret_key_for("anthropic_api_key"), path=p) == "sk-ant-realanthropickey"


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_saved_provider_wins_over_key_prefix(hs, jc, fake_hass, tmp_path, monkeypatch):
    """An OpenAI-shaped key but llm_provider says anthropic -> the saved
    provider is trusted (the installation already told Nova what this key
    is for); the key's own shape is only a fallback for when that's absent."""
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "sk-looks-like-openai", "llm_provider": "anthropic"})
    monkeypatch.setattr(jc, "delete", lambda k: None)

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "anthropic"
    assert hs.get_secret_sync(hs.secret_key_for("anthropic_api_key"), path=p) == "sk-looks-like-openai"
    assert hs.get_secret_sync(hs.secret_key_for("openai_api_key"), path=p) in (None, "")


# ── ambiguity — never guess, never delete ─────────────────────────────────────

async def test_ambiguous_key_is_left_untouched(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "unrecognized-shape-key"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["ambiguous"] is True
    assert result["migrated"] is None
    assert deleted == []
    assert not p.exists() or hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) in (None, "")


async def test_ambiguous_key_raises_repair_issue(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "unrecognized-shape"})
    monkeypatch.setattr(jc, "delete", lambda k: None)

    calls = []
    from types import ModuleType
    import sys as _sys
    fake_rn = ModuleType("jc.repair_notices")
    fake_rn.note_credential_migration_ambiguous = lambda hass: calls.append(hass)
    fake_rn.clear_credential_migration_ambiguous = lambda hass: None
    monkeypatch.setitem(_sys.modules, "jc.repair_notices", fake_rn)

    await hs.split_shared_credential(fake_hass)
    assert calls == [fake_hass]


# ── never overwrite an existing provider-specific credential ─────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_never_overwrites_existing_provider_secret(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_groq_api_key: "ALREADY-THERE"\n')
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "SHARED-KEY", "llm_provider": "groq"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "already_present"
    assert deleted == []
    assert hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) == "ALREADY-THERE"


async def test_never_overwrites_existing_plaintext_provider_key(hs, jc, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {
        "api_key": "SHARED-KEY", "llm_provider": "openai", "openai_api_key": "EXISTING-PLAINTEXT",
    })
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "already_present"
    assert deleted == []


# ── nothing to migrate ────────────────────────────────────────────────────────

async def test_no_shared_key_is_a_noop(hs, jc, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    monkeypatch.setattr(jc, "get_all", lambda: {"llm_provider": "groq"})
    result = await hs.split_shared_credential(fake_hass)
    assert result == {"migrated": None, "ambiguous": False, "reason": None}


# ── write / verify failure leaves the legacy value working ───────────────────

async def test_write_failure_leaves_legacy_value_in_place(hs, jc, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "SHARED-KEY", "llm_provider": "groq"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    monkeypatch.setattr(hs, "set_secret_sync", lambda k, v, path=None: False)

    result = await hs.split_shared_credential(fake_hass)

    assert result["reason"] == "write_failed"
    assert deleted == []


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_verify_failure_leaves_legacy_value_in_place(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "SHARED-KEY", "llm_provider": "groq"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    real_get = hs.get_secret_sync
    calls = {"n": 0}

    def _flaky_read(key, default=None, path=None):
        # 1st call: the "does a credential already exist" pre-write check
        # (must see nothing, so migration proceeds to write). 2nd call: the
        # post-write verify — simulate a corrupt write by returning the
        # wrong value here only.
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(key, default, path)
        return "WRONG-VALUE"

    monkeypatch.setattr(hs, "get_secret_sync", _flaky_read)

    result = await hs.split_shared_credential(fake_hass)

    assert result["reason"] == "verify_failed"
    assert deleted == []


# ── idempotency ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_idempotent_second_run_is_a_noop(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    state = {"api_key": "SHARED-KEY", "llm_provider": "groq"}
    monkeypatch.setattr(jc, "get_all", lambda: dict(state))

    def _delete(k):
        state.pop(k, None)
    monkeypatch.setattr(jc, "delete", _delete)

    first = await hs.split_shared_credential(fake_hass)
    assert first["migrated"] == "groq"

    second = await hs.split_shared_credential(fake_hass)
    assert second == {"migrated": None, "ambiguous": False, "reason": None}


# ── legacy groq_api_key alias handled safely ──────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_existing_groq_api_key_alias_is_not_duplicated_or_lost(hs, jc, fake_hass, tmp_path, monkeypatch):
    """An install that already has a `groq_api_key` (the pre-Phase-2 alias,
    already sitting under the exact name Phase 2 also uses as groq's own
    slot) must never be overwritten or duplicated by the shared-key split."""
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_groq_api_key: "ALIAS-KEY"\n')
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {
        "api_key": "SOME-OTHER-SHARED-KEY", "llm_provider": "groq", "groq_api_key": "",
    })
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "already_present"
    assert hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) == "ALIAS-KEY"
    assert deleted == []


# ── Gemini credential preserved untouched ─────────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_gemini_credential_is_never_touched_by_the_split(hs, jc, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_gemini_api_key: "GEMINI-SECRET"\n')
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "GROQ-SHARED-KEY", "llm_provider": "groq"})
    monkeypatch.setattr(jc, "delete", lambda k: None)

    result = await hs.split_shared_credential(fake_hass)

    assert result["migrated"] == "groq"
    assert hs.get_secret_sync(hs.secret_key_for("gemini_api_key"), path=p) == "GEMINI-SECRET"


# ── custom / ollama are never targets of the shared-key split ────────────────

async def test_custom_and_ollama_are_never_legacy_fallback_targets(hs, jc, fake_hass, tmp_path, monkeypatch):
    """llm_provider = custom/ollama isn't a fixed cloud endpoint Nova can
    prove the shared key belongs to — falls through to key-prefix detection,
    which (for an unrecognized custom-endpoint token) stays ambiguous rather
    than guessing."""
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "custom-token-xyz", "llm_provider": "custom"})
    monkeypatch.setattr(jc, "delete", lambda k: None)

    result = await hs.split_shared_credential(fake_hass)

    assert result["ambiguous"] is True
    assert result["migrated"] is None
