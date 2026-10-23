"""Writing Nova's keys into secrets.yaml, and moving plain keys there.

Two promises are checked. Writes only ever touch Nova's own lines (the
user's other secrets and comments survive, a backup is kept, and both files
are owner only). Moving a key out of config.json is write, read back, then
remove: if any step fails the plain copy stays, so a key is never lost.
"""
from __future__ import annotations

import os
import stat
import types

import pytest
import yaml

PROVIDERS = {"groq": "groq_api_key", "openai": "openai_api_key",
             "anthropic": "anthropic_api_key", "gemini": "gemini_api_key",
             "custom": "custom_api_key", "ollama": "ollama_api_key"}


@pytest.fixture
def secrets(load, monkeypatch):
    mod = load("ha_secrets")
    monkeypatch.setattr(mod, "_SECRETS_CACHE", None)     # no read leaks across tests
    return mod


@pytest.fixture
def secrets_file(secrets, tmp_path, monkeypatch):
    path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    return path


@pytest.fixture
def plain_config(load, monkeypatch):
    """config.json as a dict, with every removal noted in `.removed`."""
    cfg_mod = load("nova_config")
    held: dict = {}
    removed: list = []
    monkeypatch.setattr(cfg_mod, "get_all", lambda: dict(held))
    monkeypatch.setattr(cfg_mod, "get", lambda key, default=None: held.get(key, default))

    def _delete(key):
        removed.append(key)
        held.pop(key, None)
    monkeypatch.setattr(cfg_mod, "delete", _delete)
    return types.SimpleNamespace(data=held, removed=removed, module=cfg_mod)


def parsed(path):
    return yaml.safe_load(path.read_text())


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def backup_of(path):
    return path.with_name(path.name + ".nova.bak")


# ── one line in, one line out ───────────────────────────────────────────────

@pytest.mark.parametrize("before, after", [
    ("existing: 1\n", 'existing: 1\nnova_api_key: "K"\n'),
    ("existing: 1", 'existing: 1\nnova_api_key: "K"\n'),          # no final newline
    ("", 'nova_api_key: "K"\n'),
    ('a: 1\nnova_api_key: "OLD"\nb: 2\n', 'a: 1\nnova_api_key: "K"\nb: 2\n'),
    ('nova_api_key_old: "X"\n', 'nova_api_key_old: "X"\nnova_api_key: "K"\n'),
])
def test_upsert_changes_only_its_own_line(secrets, before, after):
    assert secrets._upsert_secret_line(before, "nova_api_key", "K") == after


@pytest.mark.parametrize("value", ['a"b\\c', "abc\\def", "ab\\", "a\\nb", "x\\1y",
                                   'p"w\\x', "plain-key"])
@pytest.mark.parametrize("start", ["", 'k: "old"\nother: 1\n'])
def test_upsert_round_trips_quotes_and_backslashes(secrets, value, start):
    """Appending and replacing must both read back exactly; a replacement
    must not treat the value's backslashes as its own escapes."""
    loaded = yaml.safe_load(secrets._upsert_secret_line(start, "k", value))
    assert loaded["k"] == value
    if start:
        assert loaded["other"] == 1


# ── set_secret_sync ─────────────────────────────────────────────────────────

def test_set_keeps_comments_and_other_keys_and_backs_up(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text("# my secrets\nother_key: value\n")
    assert secrets.set_secret_sync("nova_api_key", "K", path=path) is True
    text = path.read_text()
    assert text.startswith("# my secrets\nother_key: value\n")
    assert parsed(path) == {"other_key": "value", "nova_api_key": "K"}
    assert backup_of(path).read_text() == "# my secrets\nother_key: value\n"


def test_set_replaces_an_existing_value(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text('nova_api_key: "OLD"\n')
    assert secrets.set_secret_sync("nova_api_key", "NEW", path=path) is True
    assert parsed(path) == {"nova_api_key": "NEW"}


def test_set_creates_the_file_and_folder_without_a_backup(secrets, tmp_path):
    path = tmp_path / "sub" / "secrets.yaml"
    assert secrets.set_secret_sync("nova_api_key", "K", path=path) is True
    assert parsed(path) == {"nova_api_key": "K"}
    assert mode_of(path) == 0o600
    assert not backup_of(path).exists()


def test_set_leaves_file_and_backup_owner_only(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text("other_key: value\n")
    os.chmod(path, 0o644)
    assert secrets.set_secret_sync("nova_api_key", "K", path=path) is True
    assert mode_of(path) == 0o600
    assert mode_of(backup_of(path)) == 0o600


def test_set_with_no_key_does_nothing(secrets, tmp_path):
    path = tmp_path / "s.yaml"
    assert secrets.set_secret_sync("", "K", path=path) is False
    assert not path.exists()


@pytest.mark.parametrize("value", ["line1\nline2", "a\rb", "tab\there", "nul\x00", "del\x7f"])
def test_set_refuses_values_that_would_break_the_yaml(secrets, tmp_path, value):
    path = tmp_path / "secrets.yaml"
    path.write_text('other: "keep"\n')
    assert secrets.set_secret_sync("nova_groq_api_key", value, path) is False
    assert path.read_text() == 'other: "keep"\n'
    assert not backup_of(path).exists()


# ── delete_secret_sync ──────────────────────────────────────────────────────

def test_delete_takes_out_only_its_line(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text('# comment\nnova_api_key: "K"\nother: "v"\n')
    os.chmod(path, 0o644)
    assert secrets.delete_secret_sync("nova_api_key", path=path) is True
    assert path.read_text() == '# comment\nother: "v"\n'
    assert backup_of(path).read_text() == '# comment\nnova_api_key: "K"\nother: "v"\n'
    assert mode_of(path) == 0o600 and mode_of(backup_of(path)) == 0o600


def test_delete_of_an_absent_key_is_success_and_writes_nothing(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text('other: "v"\n')
    assert secrets.delete_secret_sync("nova_never_set", path=path) is True
    assert path.read_text() == 'other: "v"\n'
    assert not backup_of(path).exists()


def test_delete_without_a_file_is_success(secrets, tmp_path):
    path = tmp_path / "nope.yaml"
    assert secrets.delete_secret_sync("nova_api_key", path=path) is True
    assert not path.exists()


def test_delete_with_no_key_does_nothing(secrets, tmp_path):
    assert secrets.delete_secret_sync("", path=tmp_path / "s.yaml") is False


# ── overlay onto the loaded config ──────────────────────────────────────────

def test_overlay_lets_secrets_win_for_credentials_only(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text('nova_gemini_api_key: "SECRET_G"\nnova_model: "other"\nnova_api_key: ""\n')
    config = {"gemini_api_key": "PLAIN_G", "model": "x", "api_key": "PLAIN"}
    out = secrets.overlay_credentials(config, path=path)
    assert out is config
    assert out == {"gemini_api_key": "SECRET_G", "model": "x", "api_key": "PLAIN"}


def test_overlay_without_a_matching_secret_changes_nothing(secrets, tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text('unrelated: "x"\n')
    assert secrets.overlay_credentials({"api_key": "PLAIN"}, path=path) == {"api_key": "PLAIN"}


# ── moving plain keys out of config.json ────────────────────────────────────

async def test_relocate_writes_reads_back_then_removes(secrets, secrets_file, plain_config,
                                                       fake_hass):
    plain_config.data.update(gemini_api_key="GKEY", api_key="", model="x")
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 1
    assert plain_config.removed == ["gemini_api_key"]    # the blank one is left alone
    assert secrets.get_secret_sync("nova_gemini_api_key", path=secrets_file) == "GKEY"
    assert plain_config.data == {"api_key": "", "model": "x"}


async def test_relocate_moves_every_key_it_can(secrets, secrets_file, plain_config, fake_hass):
    plain_config.data.update(groq_api_key="G", openai_api_key="O")
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 2
    assert sorted(plain_config.removed) == ["groq_api_key", "openai_api_key"]
    assert parsed(secrets_file) == {"nova_groq_api_key": "G", "nova_openai_api_key": "O"}


async def test_relocate_keeps_the_plain_key_when_the_write_fails(
        secrets, secrets_file, plain_config, fake_hass, monkeypatch):
    plain_config.data["api_key"] = "KEY"
    monkeypatch.setattr(secrets, "set_secret_sync", lambda key, value, path=None: False)
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 0
    assert plain_config.removed == []


async def test_relocate_keeps_the_plain_key_when_the_read_back_differs(
        secrets, secrets_file, plain_config, fake_hass, monkeypatch):
    plain_config.data["api_key"] = "KEY"
    reads = iter([None, "MANGLED"])
    monkeypatch.setattr(secrets, "get_secret_sync", lambda key, default=None, path=None: next(reads))
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 0
    assert plain_config.removed == []


async def test_relocate_drops_a_plain_copy_that_matches(secrets, secrets_file, plain_config,
                                                        fake_hass):
    secrets_file.write_text('nova_api_key: "KEY"\n')
    plain_config.data["api_key"] = "KEY"
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 1
    assert plain_config.removed == ["api_key"]
    assert secrets_file.read_text() == 'nova_api_key: "KEY"\n'


async def test_relocate_leaves_both_when_they_differ(secrets, secrets_file, plain_config,
                                                     fake_hass):
    secrets_file.write_text('nova_api_key: "SECRETVAL"\n')
    plain_config.data["api_key"] = "DIFFERENT"
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 0
    assert plain_config.removed == []
    assert parsed(secrets_file) == {"nova_api_key": "SECRETVAL"}


async def test_relocate_survives_an_unreadable_config(secrets, secrets_file, plain_config,
                                                      fake_hass, monkeypatch):
    def _broken():
        raise OSError("config.json unreadable")
    monkeypatch.setattr(plain_config.module, "get_all", _broken)
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 0
    assert not secrets_file.exists()


async def test_relocate_does_not_count_a_failed_removal(secrets, secrets_file, plain_config,
                                                        fake_hass, monkeypatch):
    plain_config.data.update(api_key="A", groq_api_key="G")
    attempted = []

    def _delete(key):
        attempted.append(key)
        if key == "api_key":
            raise OSError("read-only")
    monkeypatch.setattr(plain_config.module, "delete", _delete)
    assert await secrets.relocate_plaintext_credentials(fake_hass) == 1
    assert attempted == ["api_key", "groq_api_key"]


# ── per-provider keys ───────────────────────────────────────────────────────

@pytest.mark.parametrize("provider, field", sorted(PROVIDERS.items()))
async def test_provider_key_set_check_and_delete(secrets, secrets_file, plain_config,
                                                 fake_hass, provider, field):
    assert await secrets.async_has_provider_credential(fake_hass, provider) is False
    assert await secrets.async_set_provider_credential(fake_hass, provider, "test-token") is True
    assert await secrets.async_has_provider_credential(fake_hass, provider) is True
    assert secrets.get_secret_sync(secrets.secret_key_for(field), path=secrets_file) == "test-token"
    assert await secrets.async_delete_provider_credential(fake_hass, provider) is True
    assert await secrets.async_has_provider_credential(fake_hass, provider) is False
    assert plain_config.removed == [field]          # any plain leftover goes too


async def test_provider_names_ignore_case_and_spaces(secrets, secrets_file, plain_config,
                                                     fake_hass):
    assert await secrets.async_set_provider_credential(fake_hass, " GROQ ", " test-token ") is True
    assert parsed(secrets_file) == {"nova_groq_api_key": "test-token"}


@pytest.mark.parametrize("action", ["set", "delete"])
async def test_unknown_provider_is_refused(secrets, secrets_file, fake_hass, action):
    if action == "set":
        ok = await secrets.async_set_provider_credential(fake_hass, "not-a-provider", "X")
    else:
        ok = await secrets.async_delete_provider_credential(fake_hass, "not-a-provider")
    assert ok is False
    assert not secrets_file.exists()


@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_a_blank_form_field_never_erases_a_key(secrets, secrets_file, fake_hass, blank):
    await secrets.async_set_provider_credential(fake_hass, "groq", "test-token")
    assert await secrets.async_set_provider_credential(fake_hass, "groq", blank) is False
    assert secrets.get_secret_sync("nova_groq_api_key", path=secrets_file) == "test-token"


async def test_one_provider_never_overwrites_another(secrets, secrets_file, fake_hass):
    await secrets.async_set_provider_credential(fake_hass, "groq", "groq-test-token")
    await secrets.async_set_provider_credential(fake_hass, "openai", "openai-test-token")
    assert parsed(secrets_file) == {"nova_groq_api_key": "groq-test-token",
                                    "nova_openai_api_key": "openai-test-token"}


async def test_a_plain_leftover_counts_as_having_a_key(secrets, secrets_file, plain_config,
                                                       fake_hass):
    plain_config.data["gemini_api_key"] = "test-token"
    assert await secrets.async_has_provider_credential(fake_hass, "gemini") is True


async def test_status_lists_every_provider_as_yes_or_no_only(secrets, secrets_file,
                                                             plain_config, fake_hass):
    await secrets.async_set_provider_credential(fake_hass, "anthropic", "ANTHROPIC-TEST-VALUE")
    status = await secrets.async_credential_status(fake_hass)
    assert status == {name: name == "anthropic" for name in PROVIDERS}
    assert "ANTHROPIC-TEST-VALUE" not in str(status)
