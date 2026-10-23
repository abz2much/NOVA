"""The "Download diagnostics" file for Nova's config entry (v6.70.2).

What matters most is redaction: no API key, token, password or street
address may reach the file, at any depth, whether it sits under a sensitive
key or inside a URL. The entry point must also never raise, so the download
always produces a file.
"""
import importlib
import json
import types

import pytest

HIDDEN = "**REDACTED**"


@pytest.fixture
def diag():
    # diagnostics is a package; importing it through the jc stand-in package
    # keeps its relative imports (safe_errors, monitor, ...) pointing at the
    # same shared modules the other tests use
    return importlib.import_module("jc.diagnostics")


# ── redaction by key ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "groq_api_key", "gemini_api_key", "anthropic_api_key", "refresh_token",
    "client_secret", "password", "floor_plan_address", "imap_user",
    "notify_service",
    "GROQ_API_KEY",            # key case does not matter
    "backup_password_hint",    # a sensitive word inside a longer key
])
def test_sensitive_keys_are_hidden(diag, key):
    assert diag._redact({key: "value-to-hide"}) == {key: HIDDEN}


def test_nested_keys_hidden_and_plain_values_kept(diag):
    out = diag._redact({
        "groq_api_key": "gsk_secret123",
        "honorific": "sir",
        "nested": {"anthropic_api_key": "sk-ant-xxx", "banter_level": 2},
    })
    assert out == {"groq_api_key": HIDDEN, "honorific": "sir",
                   "nested": {"anthropic_api_key": HIDDEN, "banter_level": 2}}


def test_every_credential_style_key_in_one_map(diag):
    src = {"refresh_token": "1//xxx", "floor_plan_address": "1 Example Road",
           "client_secret": "shh", "password": "hunter2"}
    assert diag._redact(src) == dict.fromkeys(src, HIDDEN)


@pytest.mark.parametrize("empty", ["", None])
def test_unset_sensitive_values_stay_as_they_are(diag, empty):
    # nothing to hide, and showing it was unset helps the reader
    assert diag._redact({"api_key": empty}) == {"api_key": empty}


def test_lists_and_tuples_are_walked(diag):
    out = diag._redact({"items": [{"api_key": "x"}, {"name": "ok"}],
                        "pair": ({"token": "t"}, "plain")})
    assert out == {"items": [{"api_key": HIDDEN}, {"name": "ok"}],
                   "pair": [{"token": HIDDEN}, "plain"]}


def test_redaction_copies_rather_than_edits(diag):
    src = {"api_key": "k", "inner": {"token": "t"}}
    diag._redact(src)
    assert src == {"api_key": "k", "inner": {"token": "t"}}


# ── odd input never raises ──────────────────────────────────────────────────

def test_unknown_objects_pass_through(diag):
    class Opaque:
        def __iter__(self):
            raise RuntimeError("nope")
    thing = Opaque()
    assert diag._redact(thing) is thing


def test_a_map_that_breaks_while_read_gives_a_marker(diag):
    class Broken(dict):
        def items(self):
            raise RuntimeError("nope")
    assert diag._redact(Broken(a=1)) == "**redaction-error**"


@pytest.mark.parametrize("value", [3, 2.5, True, None])
def test_scalars_untouched(diag, value):
    assert diag._redact(value) is value


# ── values scrubbed whatever their key (v8.7.4) ─────────────────────────────

@pytest.mark.parametrize("raw, scrubbed", [
    ("http://nova:pa55word@10.0.0.5:11434", "http://**REDACTED**@10.0.0.5:11434"),
    ("https://search.example/?q=x&token=abc123&lang=en",
     "https://search.example/?q=x&token=**REDACTED**&lang=en"),
    ("https://u@host.example/api?api_key=sk-1",
     "https://**REDACTED**@host.example/api?api_key=**REDACTED**"),
    ("https://n8n.example/webhook/0f3c", "https://n8n.example/webhook/**REDACTED**"),
])
def test_credentials_inside_strings(diag, raw, scrubbed):
    assert diag._redact({"some_url": raw}) == {"some_url": scrubbed}
    assert diag._redact([raw]) == [scrubbed]


def test_no_secret_survives_a_mixed_map(diag):
    dumped = str(diag._redact({
        "ollama_base_url": "http://nova:pa55word@10.0.0.5:11434",
        "searxng_url": "https://search.example/?q=x&token=abc123&lang=en",
        "nested": ["https://u@host.example/api?api_key=sk-1"],
    }))
    for secret in ("pa55word", "abc123", "sk-1"):
        assert secret not in dumped


def test_plain_urls_and_text_are_left_alone(diag):
    src = {"ollama_base_url": "http://10.0.0.5:11434",
           "searxng_url": "https://search.example/?q=monkey&keyboard=1",
           "honorific": "sir", "note": "mail me at a@b.example"}
    assert diag._redact(src) == src


def test_webhook_key_hides_the_whole_value(diag):
    out = diag._redact({"n8n_webhook_base_url": "https://n8n.example/webhook/0f3c"})
    assert out == {"n8n_webhook_base_url": HIDDEN}


# ── the entry point Home Assistant calls ────────────────────────────────────

def _entry(**fields):
    base = dict(version=1, data={"groq_api_key": "gsk_leak", "honorific": "sir"},
                options={"gemini_api_key": "leak2", "banter_level": 2})
    base.update(fields)
    return types.SimpleNamespace(**base)


async def test_dump_holds_the_entry_redacted(diag, fake_hass):
    out = await diag.async_get_config_entry_diagnostics(fake_hass, _entry())
    assert isinstance(out, dict)
    assert out["integration"] == "nova"
    assert out["domain"] == "nova"
    assert out["version"] == 1
    assert out["entry_data"] == {"groq_api_key": HIDDEN, "honorific": "sir"}
    assert out["entry_options"] == {"gemini_api_key": HIDDEN, "banter_level": 2}
    text = json.dumps(out, default=str)
    assert "gsk_leak" not in text and "leak2" not in text


async def test_dump_counts_entities_by_domain_only(diag, fake_hass):
    for eid in ("light.kitchen", "light.hall", "sensor.outside_temp"):
        fake_hass.states.set(eid, "on")
    out = await diag.async_get_config_entry_diagnostics(fake_hass, _entry())
    assert out["entity_domain_counts"] == {"light": 2, "sensor": 1}
    assert out["entity_total"] == 3
    # the ids themselves would describe the home's layout
    assert "light.kitchen" not in json.dumps(out["entity_domain_counts"])


async def test_unreadable_entry_still_gives_a_file(diag, fake_hass):
    class BadEntry:
        version = 2

        @property
        def data(self):
            raise RuntimeError("entry store is broken")
    out = await diag.async_get_config_entry_diagnostics(fake_hass, BadEntry())
    assert out["integration"] == "nova"
    assert out["config_error"].startswith("RuntimeError: entry store is broken")
    assert "entry_data" not in out


def test_entry_point_is_exported(diag):
    assert callable(diag.async_get_config_entry_diagnostics)
    assert "async_get_config_entry_diagnostics" in diag.__all__
