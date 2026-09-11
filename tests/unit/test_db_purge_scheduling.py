"""Guard that the database purge is actually wired into Nova's scheduler
(Nova Unification item 3, 11 Sept 2026). Before this, purge_old_records and
knowledge.purge_expired were correct, tested functions that nothing ever
called automatically.

Source-level guard, same reasoning as test_conversation_dispatch.py and
test_websocket_admin_gate.py: exercising async_setup_entry for real needs a
live HA stack, so we assert on the source instead.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "__init__.py"


def test_db_purge_registered_with_scheduler():
    src = SRC.read_text()
    assert 'sched.add("db_purge"' in src, \
        "database purge isn't registered with the central scheduler"


def test_db_purge_calls_both_existing_purge_functions():
    src = SRC.read_text()
    assert "purge_old_records" in src
    assert "knowledge.purge_expired" in src


def test_db_purge_tick_never_propagates_an_exception():
    # Every other scheduled tick in this file wraps its body in try/except and
    # logs at debug rather than letting an error escape (the scheduler already
    # catches and counts failures itself, but ticks here follow the same
    # defensive convention as package/health/hazard/documents).
    src = SRC.read_text()
    tick_start = src.index("async def _db_purge_tick")
    tick_body = src[tick_start:tick_start + 600]
    assert "try:" in tick_body and "except Exception" in tick_body
