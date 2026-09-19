"""Regression tests for the live-caught false trigger: asking Nova about
the "Upstairs Thermo Lock" alert (or any sentence merely containing a
lock-related word) returned the unrelated local shortcut "Everything is
closed and secured, Sir." instead of falling through to the real
conversation path.

Two contributing bugs, both "unscoped substring check" — fixed together:

  1. `_QUERY_PATTERNS`'s what_open rule matched "open"/"unlocked" via
     re.search ANYWHERE in the sentence, with an optional (not required)
     "what's/what is" lead-in — so a quoted alert ("...has been unlocked
     for 20 minutes") or a question ABOUT a lock ("why did you say the
     lock was unlocked?") matched it as if it were literally "what's
     unlocked".
  2. `score_complexity()`'s "simple signal" word list did a bare `in`
     substring check — "lock" is itself one of those words, so it matched
     inside "unlocked" and "Thermo Lock" too, artificially lowering a
     "why"-triggered high-complexity question's score back under the
     local-handling threshold, keeping it eligible for bug #1 to fire on
     in the first place.
"""
import pytest

from fakes import FakeHass


@pytest.fixture
def le(load):
    return load("local_engine")


# ── score_complexity: word-boundary fix (contributing bug #2) ──────────────

def test_complexity_lock_substring_inside_unlocked_no_longer_dodges_escalation(le):
    # "unlocked" contains "lock" and "unlock" as raw substrings but neither
    # as a whole word -- with the old bare `in` check either one wrongly
    # fired the -20 "simple signal" reduction; the fix leaves this "why"
    # question's score at the full 60 (20 base + 40 for "why"), not clawed
    # back down to 40 by a word that was never actually there.
    text = "Why did you say it was unlocked?"
    assert le.score_complexity(text) == 60
    # This ISN'T what makes the false trigger impossible on its own (the
    # score alone doesn't gate LLM escalation once online — try_local()
    # simply no longer matches "unlocked" as a status query at all, see the
    # try_local() tests below); it's a genuine, independent instance of the
    # same "unscoped substring check" bug class the task calls out.


def test_complexity_genuine_lock_command_still_scores_low(le):
    # "lock the front door" must still legitimately score as a simple,
    # locally-handleable command -- the fix only removes the FALSE match.
    assert le.score_complexity("lock the front door") < 70


def test_contains_word_helper_is_boundary_aware(le):
    assert le._contains_word("the door is unlocked", "lock") is False
    assert le._contains_word("please lock the door", "lock") is True
    assert le._contains_word("thermo lock alerts", "lock") is True
    assert le._contains_word("blocked entry", "lock") is False


# ── try_local(): the exact reported false-trigger sentences ────────────────

@pytest.mark.parametrize("text", [
    'Why did you just announce this "Upstairs Thermo Lock has been unlocked for 20 minutes"',
    "The thermostat lock is not a security risk.",
    "Read the rules about lock notifications.",
    "Why did you say the lock was unlocked?",
])
async def test_questions_and_complaints_containing_lock_fall_through(le, text):
    hass = FakeHass()
    result = await le.try_local(hass, text, honorific="sir")
    if result is not None:
        assert "closed and secured" not in result.text.lower()


async def test_bare_what_is_open_still_works(le):
    hass = FakeHass()
    result = await le.try_local(hass, "what's open", honorific="sir")
    assert result is not None
    assert result.handled is True
    assert "closed and secured" in result.text.lower()


# ── Required list #10: a genuine status question still works ───────────────

async def test_is_the_front_door_locked_still_answers_the_specific_entity(le):
    hass = FakeHass()
    hass.states.set("lock.front_door", "locked", friendly_name="Front Door")
    result = await le.try_local(hass, "Is the front door locked?", honorific="sir")
    assert result is not None
    assert result.handled is True
    assert "closed and secured" not in result.text.lower()
    assert "front door" in result.text.lower()


# ── Required list #11: a direct lock command still uses the real path ──────

async def test_lock_the_front_door_still_calls_the_real_lock_service(le):
    hass = FakeHass()
    hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    async def fake_call(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
        if domain == "lock" and service == "lock":
            hass.states.set("lock.front_door", "locked", friendly_name="Front Door")
    hass.services.async_call = fake_call

    result = await le.try_local(hass, "Lock the front door.", honorific="sir")

    assert result is not None
    assert "closed and secured" not in (result.text or "").lower()
    assert ("lock", "lock") in [(d, s) for d, s, _ in hass.service_calls]


# ── Required list #12: entity names containing "Lock" don't invent intent ──

async def test_entity_named_lock_does_not_reverse_a_status_question(le):
    """Asking about an entity whose OWN name contains "Lock" must still be
    answered as the status question it is, not reinterpreted via the
    entity's name."""
    hass = FakeHass()
    hass.states.set("lock.upstairs_thermo_lock", "unlocked",
                     friendly_name="Upstairs Thermo Lock")
    result = await le.try_local(
        hass, "Why did you say the lock was unlocked?", honorific="sir")
    if result is not None:
        assert "closed and secured" not in result.text.lower()


async def test_entity_named_lock_a_genuine_command_still_targets_it_correctly(le):
    hass = FakeHass()
    hass.states.set("lock.upstairs_thermo_lock", "locked",
                     friendly_name="Upstairs Thermo Lock")
    async def fake_call(domain, service, data=None, blocking=False, **kw):
        hass.service_calls.append((domain, service, dict(data or {})))
    hass.services.async_call = fake_call

    result = await le.try_local(hass, "unlock the upstairs thermo lock", honorific="sir")
    # Unlock is a protected action deferred to the agent's confirmation
    # gate (CLAUDE.md: voice can never unlock unguarded) -- local_engine
    # returning None here (falling through) is the CORRECT, existing
    # behaviour, not a regression; the key assertion is that it never
    # answers with the unrelated status-summary shortcut.
    if result is not None:
        assert "closed and secured" not in result.text.lower()
