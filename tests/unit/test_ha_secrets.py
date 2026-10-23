"""Reading Home Assistant's secrets.yaml (ha_secrets.py).

Reading never fails setup: a missing, empty, broken or oddly shaped file
reads as no secrets. Lookups give the default for an absent or blank value.
The async lookup runs the file read on the executor, and the default path
is looked up at call time, so pointing SECRETS_PATH at a test file works.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def hs(load):
    mod = load("ha_secrets")
    mod._reset_secrets_cache()
    yield mod
    mod._reset_secrets_cache()


@pytest.fixture
def secrets_file(tmp_path):
    """write(text) -> path of a secrets.yaml holding ``text``."""
    def write(text, name="secrets.yaml"):
        target = tmp_path / name
        target.write_text(text, encoding="utf-8")
        return target
    return write


# ── reading the whole file ─────────────────────────────────────────────────

def test_a_well_formed_file_reads_as_a_mapping(hs, secrets_file):
    path = secrets_file("nova_imap_password: test-password\nother_key: value\nport: 993\n")
    assert hs._read_secrets(path) == {
        "nova_imap_password": "test-password", "other_key": "value", "port": 993}


@pytest.mark.parametrize("text", [
    '"unterminated string',            # a YAML scanner error
    "- just\n- a\n- list\n",           # a list, not a mapping
    "plain words only\n",              # a bare string
    "",                                # empty
    "# only a comment\n",
], ids=["broken", "list", "scalar", "empty", "comment"])
def test_an_unusable_file_reads_as_no_secrets(hs, secrets_file, text):
    assert hs._read_secrets(secrets_file(text)) == {}


def test_a_missing_file_reads_as_no_secrets(hs, tmp_path):
    assert hs._read_secrets(tmp_path / "absent.yaml") == {}


def test_a_path_that_cannot_be_opened_reads_as_no_secrets(hs, tmp_path):
    folder = tmp_path / "secrets.yaml"
    folder.mkdir()                      # exists, but is a directory
    assert hs._read_secrets(folder) == {}


# ── looking up one secret ──────────────────────────────────────────────────

@pytest.mark.parametrize("key, default, expected", [
    ("nova_imap_password", None, "test-password"),
    ("missing", "fallback", "fallback"),
    ("missing", None, None),
    ("blank", "fallback", "fallback"),       # an empty string is not a value
    ("nothing", "fallback", "fallback"),     # nor is a YAML null
    ("", "fallback", "fallback"),            # no key at all
])
def test_get_secret_sync(hs, secrets_file, key, default, expected):
    path = secrets_file('nova_imap_password: test-password\nblank: ""\nnothing:\n')
    assert hs.get_secret_sync(key, default=default, path=path) == expected


def test_an_empty_key_never_reads_the_file(hs, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("read the file for an empty key")
    monkeypatch.setattr(hs, "_read_secrets", refuse)
    assert hs.get_secret_sync("", default="fallback") == "fallback"


# ── the async lookup ───────────────────────────────────────────────────────

async def test_with_hass_the_read_goes_through_the_executor(hs, fake_hass, secrets_file,
                                                           monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", secrets_file("nova_imap_password: test-async\n"))
    jobs = []
    run = fake_hass.async_add_executor_job

    async def spy(func, *args):
        jobs.append((func.__name__, args))
        return await run(func, *args)

    monkeypatch.setattr(fake_hass, "async_add_executor_job", spy)
    assert await hs.async_get_secret(fake_hass, "nova_imap_password") == "test-async"
    assert jobs == [("get_secret_sync", ("nova_imap_password", None))]


async def test_without_hass_it_reads_directly(hs, secrets_file, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", secrets_file("k: v\n"))
    assert await hs.async_get_secret(None, "k") == "v"


async def test_a_missing_default_file_gives_the_default(hs, fake_hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "SECRETS_PATH", tmp_path / "none.yaml")
    assert await hs.async_get_secret(fake_hass, "k", default="fallback") == "fallback"


async def test_an_executor_failure_gives_the_default(hs, fake_hass, monkeypatch):
    async def broken(func, *args):
        raise RuntimeError("executor shut down")
    monkeypatch.setattr(fake_hass, "async_add_executor_job", broken)
    assert await hs.async_get_secret(fake_hass, "k", default="fallback") == "fallback"


def test_the_default_path_is_looked_up_at_call_time(hs, secrets_file, monkeypatch):
    first = secrets_file("k: one\n", name="first.yaml")
    second = secrets_file("k: two\n", name="second.yaml")
    monkeypatch.setattr(hs, "SECRETS_PATH", first)
    assert hs.get_secret_sync("k") == "one"
    monkeypatch.setattr(hs, "SECRETS_PATH", second)
    assert hs.get_secret_sync("k") == "two"
