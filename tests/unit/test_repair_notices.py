"""Nova's notices under Settings > Repairs (repair_notices).

Each notice must be raised with its reason, left alone while the same problem
lasts, cleared when the problem goes, and never take an exception back to the
caller, which is usually the AI path or the voice bootstrap.
"""
from __future__ import annotations

import json
import logging
import pathlib
import types

import pytest

EN_JSON = (pathlib.Path(__file__).resolve().parents[2]
           / "custom_components" / "nova" / "translations" / "en.json")


class FakeRegistry:
    """Stands in for homeassistant.helpers.issue_registry and keeps a log of
    ("create" | "delete", issue_id, keyword arguments). Setting `broken`
    makes every call raise, as the real registry does while shutting down."""

    IssueSeverity = types.SimpleNamespace(ERROR="error", WARNING="warning")

    def __init__(self):
        self.log: list[tuple[str, str, dict]] = []
        self.broken = False

    def _record(self, op, domain, issue_id, kw):
        if self.broken:
            raise RuntimeError("registry gone")
        assert domain == "nova"
        self.log.append((op, issue_id, kw))

    def async_create_issue(self, hass, domain, issue_id, **kw):
        self._record("create", domain, issue_id, kw)

    def async_delete_issue(self, hass, domain, issue_id):
        self._record("delete", domain, issue_id, {})

    def created(self):
        return [(issue_id, kw) for op, issue_id, kw in self.log if op == "create"]

    def deleted(self):
        return [issue_id for op, issue_id, _ in self.log if op == "delete"]


@pytest.fixture
def rn(load, monkeypatch):
    module = load("repair_notices")
    # The loader keeps one module for the whole run, so every piece of its
    # state is swapped for a fresh copy and put back afterwards.
    monkeypatch.setattr(module, "_state", {"active": False, "detail": None, "cleared_once": False})
    monkeypatch.setattr(module, "_cred_state", {"active": False})
    monkeypatch.setattr(module, "_retired_seen", set())
    return module


@pytest.fixture
def reg(rn, monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(rn, "ir", fake)
    return fake


@pytest.fixture(scope="module")
def issue_strings():
    return json.loads(EN_JSON.read_text(encoding="utf-8"))["issues"]


HASS = object()


# ── reasoning backend unavailable ────────────────────────────────────────

def test_problem_raises_one_issue_carrying_the_reason(rn, reg):
    rn.note_llm_problem(HASS, "model 'x' not found on groq")
    [(issue_id, kw)] = reg.created()
    assert issue_id == "llm_unavailable"
    assert "not found" in kw["translation_placeholders"]["detail"]
    assert kw["translation_placeholders"] == {"detail": "model 'x' not found on groq"}
    assert kw["translation_key"] == "llm_unavailable"
    assert kw["severity"] == "error" and kw["is_fixable"] is False


@pytest.mark.parametrize("reasons, raised", [
    (["same reason", "same reason"], 1),          # no churn during one outage
    (["reason A", "reason B"], 2),                # a new reason updates it
    (["  padded  ", "padded"], 1),                # whitespace is not a new reason
])
def test_repeat_notes_only_rewrite_on_a_new_reason(rn, reg, reasons, raised):
    for reason in reasons:
        rn.note_llm_problem(HASS, reason)
    assert len(reg.created()) == raised


@pytest.mark.parametrize("detail", ["", None])
def test_no_reason_uses_the_default_text(rn, reg, detail):
    rn.note_llm_problem(HASS, detail)
    [(_, kw)] = reg.created()
    assert kw["translation_placeholders"]["detail"] == "The reasoning model could not be reached."


def test_long_reasons_are_cut_to_the_limit(rn, reg):
    rn.note_llm_problem(HASS, "e" * 1000)
    [(_, kw)] = reg.created()
    assert kw["translation_placeholders"]["detail"] == "e" * 300


def test_clear_removes_a_raised_issue(rn, reg):
    rn.note_llm_problem(HASS, "down")
    rn.clear_llm_problem(HASS)
    assert reg.deleted() == ["llm_unavailable"]
    rn.clear_llm_problem(HASS)              # already gone: nothing more to do
    assert reg.deleted() == ["llm_unavailable"]


def test_first_clear_after_restart_removes_a_stale_issue_once(rn, reg):
    # Nothing raised in this run, but an earlier run may have left one.
    rn.clear_llm_problem(HASS)
    assert reg.deleted() == ["llm_unavailable"]
    rn.clear_llm_problem(HASS)
    assert reg.deleted() == ["llm_unavailable"]


def test_outage_after_recovery_raises_again(rn, reg):
    rn.note_llm_problem(HASS, "down")
    rn.clear_llm_problem(HASS)
    rn.note_llm_problem(HASS, "down")
    assert [op for op, _, _ in reg.log] == ["create", "delete", "create"]


def test_failed_create_is_retried_on_the_next_note(rn, reg):
    reg.broken = True
    rn.note_llm_problem(HASS, "down")       # must not raise
    assert rn._state["active"] is False
    reg.broken = False
    rn.note_llm_problem(HASS, "down")
    assert len(reg.created()) == 1


def test_failed_delete_does_not_raise_and_still_settles(rn, reg):
    rn.note_llm_problem(HASS, "down")
    reg.broken = True
    rn.clear_llm_problem(HASS)
    assert rn._state == {"active": False, "detail": None, "cleared_once": True}


# ── credential migration needs a hand ────────────────────────────────────

def test_credential_notice_is_raised_once_and_cleared_once(rn, reg):
    rn.note_credential_migration_ambiguous(HASS)
    rn.note_credential_migration_ambiguous(HASS)
    [(issue_id, kw)] = reg.created()
    assert issue_id == "credential_migration_ambiguous"
    assert kw["severity"] == "warning"
    rn.clear_credential_migration_ambiguous(HASS)
    rn.clear_credential_migration_ambiguous(HASS)
    assert reg.deleted() == ["credential_migration_ambiguous"]


def test_credential_clear_with_nothing_raised_touches_nothing(rn, reg):
    rn.clear_credential_migration_ambiguous(HASS)
    assert reg.log == []


# ── voice setup (v8.6.0) ─────────────────────────────────────────────────

def _created_keys(reg):
    return [(issue_id, kw["translation_key"], kw.get("translation_placeholders"))
            for issue_id, kw in reg.created()]


def test_each_voice_step_has_its_own_translation_key(rn, reg):
    rn.note_voice_setup_step(HASS, "wyoming", 2, 3)
    assert _created_keys(reg) == [("voice_setup_in_progress", "voice_setup_step_wyoming",
                                   {"step": "2", "total": "3"})]


def test_incomplete_voice_setup_replaces_the_progress_notice(rn, reg):
    rn.note_voice_setup_incomplete(HASS, ["Wyoming", "Assist pipeline"])
    assert reg.deleted() == ["voice_setup_in_progress"]
    assert _created_keys(reg) == [("voice_setup_incomplete", "voice_setup_incomplete",
                                   {"failed": "Wyoming, Assist pipeline"})]
    # The old notice goes before the new one appears.
    assert [op for op, _, _ in reg.log] == ["delete", "create"]


def test_clearing_voice_setup_removes_both_notices(rn, reg):
    rn.clear_voice_setup(HASS)
    assert reg.deleted() == ["voice_setup_in_progress", "voice_setup_incomplete"]


# ── retired services (8.40.0) ────────────────────────────────────────────

def test_retired_service_warns_once_per_run_naming_the_replacement(rn, reg, caplog):
    caplog.set_level(logging.WARNING)
    rn.note_retired_service(HASS, "speak", "tts.speak")
    rn.note_retired_service(HASS, "speak", "tts.speak")
    [(issue_id, kw)] = reg.created()
    assert issue_id == "retired_service_speak"
    assert kw["translation_key"] == "retired_service"
    assert kw["translation_placeholders"] == {"service": "nova.speak", "replacement": "tts.speak"}
    assert kw["is_persistent"] is False
    assert sum("nova.speak is being retired" in r.message for r in caplog.records) == 1


def test_retired_service_without_replacement_says_it_does_nothing(rn, reg):
    rn.note_retired_service(HASS, "some_old_service", None)
    [(issue_id, kw)] = reg.created()
    assert issue_id == "retired_service_some_old_service"
    assert kw["translation_key"] == "retired_service_no_effect"
    assert kw["translation_placeholders"] == {"service": "nova.some_old_service"}


def test_each_retired_service_gets_its_own_notice(rn, reg):
    rn.note_retired_service(HASS, "first", "a.b")
    rn.note_retired_service(HASS, "second", None)
    assert [issue_id for issue_id, _ in reg.created()] == [
        "retired_service_first", "retired_service_second"]


# ── nothing escapes when the registry is unavailable ─────────────────────

@pytest.mark.parametrize("call", [
    lambda rn: rn.note_llm_problem(HASS, "down"),
    lambda rn: rn.clear_llm_problem(HASS),
    lambda rn: rn.note_credential_migration_ambiguous(HASS),
    lambda rn: rn.note_voice_setup_step(HASS, "addons", 1, 3),
    lambda rn: rn.note_voice_setup_incomplete(HASS, ["Wyoming"]),
    lambda rn: rn.clear_voice_setup(HASS),
    lambda rn: rn.note_retired_service(HASS, "speak", "tts.speak"),
], ids=["llm-note", "llm-clear", "credential", "voice-step", "voice-incomplete",
        "voice-clear", "retired"])
def test_registry_failure_never_reaches_the_caller(rn, reg, call):
    reg.broken = True
    call(rn)
    assert reg.log == []


# ── the text each notice points at ───────────────────────────────────────

@pytest.mark.parametrize("key, placeholder", [
    ("llm_unavailable", "{detail}"),
    ("voice_setup_step_addons", "{step}"),
    ("voice_setup_step_wyoming", "{step}"),
    ("voice_setup_step_pipeline", "{step}"),
    ("voice_setup_incomplete", "{failed}"),
    ("retired_service", "{replacement}"),
    ("retired_service_no_effect", "{service}"),
])
def test_en_json_text_has_the_placeholder_the_code_fills(issue_strings, key, placeholder):
    assert placeholder in issue_strings[key]["description"]


def test_every_listed_voice_step_has_notice_text(rn, issue_strings):
    assert set(rn.VOICE_STEPS) == {"addons", "wyoming", "pipeline"}
    for step in rn.VOICE_STEPS:
        assert "{total}" in issue_strings[f"voice_setup_step_{step}"]["description"]


def test_credential_notice_has_text(issue_strings):
    assert issue_strings["credential_migration_ambiguous"]["description"].strip()
