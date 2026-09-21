"""Tests for the secrets writer + credential relocation (v6.83.0).

The writer must preserve the rest of the user's secrets.yaml and never lose
data; relocation must write→verify→strip and, on any failure, leave config.json
untouched so a credential can never be lost or auth broken.
"""
import pytest

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


@pytest.fixture
def hs(load):
    return load("ha_secrets")


# ── line upsert ──────────────────────────────────────────────────────────────

def test_upsert_appends_when_absent(hs):
    out = hs._upsert_secret_line("existing: 1\n", "nova_api_key", "K")
    assert "existing: 1" in out
    assert 'nova_api_key: "K"' in out


def test_upsert_replaces_when_present(hs):
    out = hs._upsert_secret_line('a: 1\nnova_api_key: "OLD"\nb: 2\n',
                                 "nova_api_key", "NEW")
    assert 'nova_api_key: "NEW"' in out
    assert '"OLD"' not in out
    assert "a: 1" in out and "b: 2" in out            # rest preserved


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
def test_upsert_escapes_quotes_and_backslashes(hs):
    out = hs._upsert_secret_line("", "k", 'a"b\\c')
    assert yaml.safe_load(out)["k"] == 'a"b\\c'


# ── set_secret_sync (safe write) ─────────────────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
def test_set_secret_writes_and_preserves(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text("# my secrets\nother_key: value\n")
    assert hs.set_secret_sync("nova_api_key", "K", path=p) is True
    text = p.read_text()
    assert "# my secrets" in text                     # comment preserved
    assert "other_key: value" in text                 # other key preserved
    assert yaml.safe_load(text)["nova_api_key"] == "K"
    assert (tmp_path / "secrets.yaml.nova.bak").exists()   # backup made


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
def test_set_secret_updates_existing(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_api_key: "OLD"\n')
    hs.set_secret_sync("nova_api_key", "NEW", path=p)
    assert yaml.safe_load(p.read_text())["nova_api_key"] == "NEW"


@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
def test_set_secret_creates_missing_file(hs, tmp_path):
    p = tmp_path / "sub" / "secrets.yaml"
    assert hs.set_secret_sync("nova_api_key", "K", path=p) is True
    assert yaml.safe_load(p.read_text())["nova_api_key"] == "K"


def test_set_secret_empty_key_false(hs, tmp_path):
    assert hs.set_secret_sync("", "K", path=tmp_path / "s.yaml") is False


# ── overlay_credentials ──────────────────────────────────────────────────────

def test_overlay_secrets_win(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_gemini_api_key: "SECRET_G"\n')
    out = hs.overlay_credentials({"gemini_api_key": "PLAIN_G", "model": "x"}, path=p)
    assert out["gemini_api_key"] == "SECRET_G"        # secrets win for creds
    assert out["model"] == "x"                        # non-cred untouched


def test_overlay_absent_secret_no_change(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text('unrelated: "x"\n')
    out = hs.overlay_credentials({"api_key": "PLAIN"}, path=p)
    assert out["api_key"] == "PLAIN"


# ── relocate_plaintext_credentials (verify-before-strip) ─────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
async def test_relocate_writes_verifies_strips(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all",
                        lambda: {"gemini_api_key": "GKEY", "api_key": "", "model": "x"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    n = await hs.relocate_plaintext_credentials(fake_hass)
    assert n == 1
    assert deleted == ["gemini_api_key"]              # only the non-empty cred moved
    assert hs.get_secret_sync("nova_gemini_api_key", path=p) == "GKEY"


async def test_relocate_leaves_config_when_write_fails(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "KEY"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    monkeypatch.setattr(hs, "set_secret_sync", lambda k, v, path=None: False)
    n = await hs.relocate_plaintext_credentials(fake_hass)
    assert n == 0
    assert deleted == []                              # never stripped on write failure


async def test_relocate_drops_redundant_when_same(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_api_key: "KEY"\n')
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "KEY"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    n = await hs.relocate_plaintext_credentials(fake_hass)
    assert n == 1 and deleted == ["api_key"]          # redundant plaintext dropped


async def test_relocate_keeps_both_when_different(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    p = tmp_path / "secrets.yaml"
    p.write_text('nova_api_key: "SECRETVAL"\n')
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    deleted = []
    monkeypatch.setattr(jc, "get_all", lambda: {"api_key": "DIFFERENT"})
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    n = await hs.relocate_plaintext_credentials(fake_hass)
    assert n == 0 and deleted == []                   # differ -> leave both, don't guess


# ── delete_secret_sync (Phase 2, v7.107.0) ────────────────────────────────────

@pytest.mark.skipif(yaml is None, reason="PyYAML unavailable")
def test_delete_secret_removes_and_preserves_rest(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text('# comment\nnova_api_key: "K"\nother: "v"\n')
    assert hs.delete_secret_sync("nova_api_key", path=p) is True
    text = p.read_text()
    assert "nova_api_key" not in text
    assert "# comment" in text and 'other: "v"' in text
    assert (tmp_path / "secrets.yaml.nova.bak").exists()


def test_delete_secret_missing_key_is_noop_true(hs, tmp_path):
    p = tmp_path / "secrets.yaml"
    p.write_text('other: "v"\n')
    assert hs.delete_secret_sync("nova_never_set", path=p) is True
    assert 'other: "v"' in p.read_text()


def test_delete_secret_missing_file_is_noop_true(hs, tmp_path):
    assert hs.delete_secret_sync("nova_api_key", path=tmp_path / "nope.yaml") is True


def test_delete_secret_empty_key_false(hs, tmp_path):
    assert hs.delete_secret_sync("", path=tmp_path / "s.yaml") is False


# ── Per-provider credential CRUD (Phase 2, v7.107.0) ──────────────────────────

@pytest.mark.parametrize("provider,field", [
    ("groq", "groq_api_key"),
    ("openai", "openai_api_key"),
    ("anthropic", "anthropic_api_key"),
    ("gemini", "gemini_api_key"),
    ("custom", "custom_api_key"),
    ("ollama", "ollama_api_key"),
])
async def test_set_get_delete_provider_credential_independent(
    hs, fake_hass, tmp_path, provider, field, load, monkeypatch,
):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, default=None: default)  # no plaintext leftovers
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    assert await hs.async_has_provider_credential(fake_hass, provider) is False

    ok = await hs.async_set_provider_credential(fake_hass, provider, "SECRET-1")
    assert ok is True
    assert await hs.async_has_provider_credential(fake_hass, provider) is True
    assert hs.get_secret_sync(hs.secret_key_for(field), path=p) == "SECRET-1"

    deleted = await hs.async_delete_provider_credential(fake_hass, provider)
    assert deleted is True
    assert await hs.async_has_provider_credential(fake_hass, provider) is False


async def test_set_provider_credential_rejects_unknown_provider(hs, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    assert await hs.async_set_provider_credential(fake_hass, "not-a-provider", "X") is False


async def test_set_provider_credential_blank_value_is_noop(hs, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    # Seed an existing credential, then confirm submitting a blank value for
    # the same provider does NOT erase it — an empty form field must never
    # clobber a stored credential.
    await hs.async_set_provider_credential(fake_hass, "groq", "REAL-KEY")
    ok = await hs.async_set_provider_credential(fake_hass, "groq", "")
    assert ok is False
    assert hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) == "REAL-KEY"
    ok2 = await hs.async_set_provider_credential(fake_hass, "groq", "   ")
    assert ok2 is False
    assert hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) == "REAL-KEY"


async def test_set_provider_credential_never_overwrites_other_providers(hs, fake_hass, tmp_path, monkeypatch):
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    await hs.async_set_provider_credential(fake_hass, "groq", "GROQ-KEY")
    await hs.async_set_provider_credential(fake_hass, "openai", "OPENAI-KEY")
    assert hs.get_secret_sync(hs.secret_key_for("groq_api_key"), path=p) == "GROQ-KEY"
    assert hs.get_secret_sync(hs.secret_key_for("openai_api_key"), path=p) == "OPENAI-KEY"


async def test_delete_provider_credential_rejects_unknown_provider(hs, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    assert await hs.async_delete_provider_credential(fake_hass, "not-a-provider") is False


async def test_delete_provider_credential_also_clears_plaintext_leftover(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "secrets.yaml")
    deleted = []
    monkeypatch.setattr(jc, "delete", lambda k: deleted.append(k))
    await hs.async_delete_provider_credential(fake_hass, "gemini")
    assert deleted == ["gemini_api_key"]


async def test_credential_status_reports_every_provider_booleans_only(hs, fake_hass, tmp_path, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, default=None: default)
    p = tmp_path / "secrets.yaml"
    monkeypatch.setattr(hs, "SECRETS_PATH", p)
    await hs.async_set_provider_credential(fake_hass, "anthropic", "ANTHROPIC-SECRET-VALUE")
    status = await hs.async_credential_status(fake_hass)
    assert status == {
        "groq": False, "openai": False, "anthropic": True,
        "gemini": False, "custom": False, "ollama": False,
    }
    # Never a value anywhere in the status payload.
    assert "ANTHROPIC-SECRET-VALUE" not in str(status)
