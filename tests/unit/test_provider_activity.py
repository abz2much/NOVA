"""Provider Activity (Phase 5) — bounded daily aggregates of LLM calls.

Pure DB functions, isolated against a tmp-path db exactly like
decision_record.py / automation_trials.py's own tests.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def pa(load):
    return load("provider_activity")


def test_record_and_list_days_round_trip(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    assert pa.record("groq", "llama3", "llm", "cloud", "text", True,
                     input_tokens=10, output_tokens=20, latency_ms=100,
                     day="2026-01-01", db_path=db) is True

    days = pa.list_days(days=3650, db_path=db)
    assert len(days) == 1
    assert days[0]["day"] == "2026-01-01"
    entry = days[0]["entries"][0]
    assert entry["provider"] == "groq"
    assert entry["model"] == "llama3"
    assert entry["role"] == "llm"
    assert entry["location"] == "cloud"
    assert entry["data_category"] == "text"
    assert entry["success_count"] == 1
    assert entry["failure_count"] == 0
    assert entry["call_count"] == 1
    assert entry["avg_input_tokens"] == 10
    assert entry["avg_output_tokens"] == 20
    assert entry["avg_latency_ms"] == 100


def test_list_days_empty_db_returns_empty_list(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    assert pa.list_days(db_path=db) == []


def test_record_accumulates_into_the_same_bucket(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True,
             input_tokens=10, output_tokens=20, latency_ms=100,
             day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", True,
             input_tokens=30, output_tokens=40, latency_ms=300,
             day="2026-01-01", db_path=db)

    entry = pa.list_days(days=3650, db_path=db)[0]["entries"][0]
    assert entry["call_count"] == 2
    assert entry["success_count"] == 2
    assert entry["avg_input_tokens"] == 20    # (10+30)/2
    assert entry["avg_output_tokens"] == 30   # (20+40)/2
    assert entry["avg_latency_ms"] == 200      # (100+300)/2


def test_record_tracks_success_and_failure_separately(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", False, day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", False, day="2026-01-01", db_path=db)

    entry = pa.list_days(days=3650, db_path=db)[0]["entries"][0]
    assert entry["success_count"] == 1
    assert entry["failure_count"] == 2
    assert entry["call_count"] == 3


def test_different_composite_keys_stay_in_separate_rows(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "classifier", "cloud", "text", True, day="2026-01-01", db_path=db)  # different role
    pa.record("ollama", "llama3", "llm", "local", "text", True, day="2026-01-01", db_path=db)        # different provider+location
    pa.record("groq", "llama3", "llm", "cloud", "vision", True, day="2026-01-01", db_path=db)        # different category

    entries = pa.list_days(days=3650, db_path=db)[0]["entries"]
    assert len(entries) == 4
    assert all(e["call_count"] == 1 for e in entries)


def test_token_sums_stay_null_until_any_call_reports_usage(pa, tmp_path):
    """A provider that never reports usage must show None, not a fabricated
    0 — the two must be visibly distinct."""
    db = str(tmp_path / "activity.db")
    pa.record("ollama", "llama3", "llm", "local", "text", True,
             input_tokens=None, output_tokens=None, day="2026-01-01", db_path=db)
    pa.record("ollama", "llama3", "llm", "local", "text", True,
             input_tokens=None, output_tokens=None, day="2026-01-01", db_path=db)

    entry = pa.list_days(days=3650, db_path=db)[0]["entries"][0]
    assert entry["avg_input_tokens"] is None
    assert entry["avg_output_tokens"] is None
    assert entry["call_count"] == 2  # the calls themselves are still counted


def test_token_sums_accumulate_once_any_call_reports_usage(pa, tmp_path):
    """A later call without usage must contribute 0, not reset the sum to
    None or corrupt the earlier reported value."""
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True,
             input_tokens=10, output_tokens=20, day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", True,
             input_tokens=None, output_tokens=None, day="2026-01-01", db_path=db)

    entry = pa.list_days(days=3650, db_path=db)[0]["entries"][0]
    assert entry["avg_input_tokens"] == 5   # (10+0)/2
    assert entry["avg_output_tokens"] == 10  # (20+0)/2
    assert entry["call_count"] == 2


def test_list_days_filters_out_days_older_than_window(pa, tmp_path):
    import datetime
    db = str(tmp_path / "activity.db")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.datetime.now() - datetime.timedelta(days=100)).strftime("%Y-%m-%d")
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day=old_day, db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day=today, db_path=db)

    day_strings = [d["day"] for d in pa.list_days(days=7, db_path=db)]
    assert today in day_strings
    assert old_day not in day_strings


def test_list_days_orders_most_recent_day_first(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-01", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-03", db_path=db)
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-02", db_path=db)

    days = pa.list_days(days=365, db_path=db)
    assert [d["day"] for d in days] == ["2026-01-03", "2026-01-02", "2026-01-01"]


def test_record_survives_unwritable_path(pa, tmp_path):
    """A file where a directory is expected -> connect fails -> best-effort
    returns False, never raises (matches decision_record.py's own pattern)."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    assert pa.record("groq", "llama3", "llm", "cloud", "text", True,
                     db_path=str(blocker / "activity.db")) is False


def test_schema_creation_is_idempotent_and_preserves_data(pa, tmp_path):
    db = str(tmp_path / "activity.db")
    pa.record("groq", "llama3", "llm", "cloud", "text", True, day="2026-01-01", db_path=db)
    # a second connection (e.g. a later call) must not wipe the first row
    pa.record("ollama", "llama3", "llm", "local", "text", True, day="2026-01-01", db_path=db)
    entries = pa.list_days(days=3650, db_path=db)[0]["entries"]
    assert len(entries) == 2


# ── concurrency: the exact race the old UPDATE-then-INSERT-if-zero-rows ────
# sequence was vulnerable to — two calls hitting the same brand-new bucket at
# once could both see zero rows updated and both attempt the INSERT, with
# the second failing on the primary key and its contribution silently lost.
# The atomic INSERT ... ON CONFLICT DO UPDATE closes that gap.

def test_concurrent_records_to_same_key_accumulate_atomically(pa, tmp_path):
    import threading

    db = str(tmp_path / "activity.db")
    n_success = 15
    n_failure = 5
    total = n_success + n_failure
    errors: list[str] = []
    lock = threading.Lock()

    def _write(success: bool, input_tokens, output_tokens) -> None:
        try:
            ok = pa.record("groq", "llama3", "llm", "cloud", "text", success,
                          input_tokens=input_tokens, output_tokens=output_tokens,
                          latency_ms=5, day="2026-01-01", db_path=db)
            if not ok:
                with lock:
                    errors.append("record() returned False")
        except Exception as exc:  # a lost UNIQUE-constraint race would land here
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = (
        [threading.Thread(target=_write, args=(True, 10, 20)) for _ in range(n_success)]
        + [threading.Thread(target=_write, args=(False, None, None)) for _ in range(n_failure)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # no write raised or was silently discarded
    assert errors == []

    days = pa.list_days(days=3650, db_path=db)
    assert len(days) == 1
    entries = days[0]["entries"]
    assert len(entries) == 1  # only one row exists for this key — no duplicate from a lost race

    entry = entries[0]
    assert entry["call_count"] == total               # every call landed
    assert entry["success_count"] == n_success         # success total correct
    assert entry["failure_count"] == n_failure         # failure total correct
    # only the n_success calls reported tokens (10/20 each); the average is
    # computed over call_count, so this proves the underlying sums are exact
    assert entry["avg_input_tokens"] == round(10 * n_success / total, 1)
    assert entry["avg_output_tokens"] == round(20 * n_success / total, 1)
