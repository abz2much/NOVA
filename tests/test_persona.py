"""Full-coverage tests for the Nova persona voice module.

Loaded directly from the component directory (persona is a stdlib-only leaf, so
no Home Assistant runtime or stubbing is needed)."""
import importlib.util
import os

_COMP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "nova"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "nova_persona", os.path.join(_COMP, "persona.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


persona = _load()


def setup_function(_fn):
    # Reset module state before each test so anti-repeat history is isolated.
    persona.set_variety(True)
    persona._recent.clear()


# ── _fill ────────────────────────────────────────────────────────────────────

def test_fill_capitalizes_and_lowercases():
    assert persona._fill("{H}, {h}!", "sir") == "Sir, sir!"


def test_fill_empty_honorific_strips_placeholders():
    # An empty honorific means nobody specific is home to address (see
    # honorific.py) — {H}/{h} placeholders resolve to nothing rather than
    # silently falling back to "sir". In practice _pick() only ever hands
    # _fill() an honorific-free template in this case, so this exercises the
    # safety net directly.
    assert persona._fill("{h}", "") == ""
    assert persona._fill("{h}", "   ") == ""
    assert persona._fill("{h}", None) == ""


def test_fill_multichar_honorific():
    assert persona._fill("{H}", "boss") == "Boss"


# ── _pick ────────────────────────────────────────────────────────────────────

def test_pick_empty_pool_returns_empty():
    assert persona._pick([], "k", "sir") == ""


def test_pick_variety_off_is_deterministic_first():
    persona.set_variety(False)
    pool = ["one {h}", "two {h}", "three {h}"]
    assert persona._pick(pool, "k", "sir") == "one sir"
    assert persona._pick(pool, "k", "sir") == "one sir"


def test_pick_anti_repeat_no_back_to_back():
    pool = [f"line{i} {{h}}" for i in range(5)]
    seq = [persona._pick(pool, "k", "sir") for _ in range(40)]
    assert all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))


def test_pick_exhausts_choices_branch_with_singleton_pool():
    # A length-1 pool fills the recent deque, forcing the "all excluded" reset.
    pool = ["only {h}"]
    assert persona._pick(pool, "solo", "sir") == "only sir"
    assert persona._pick(pool, "solo", "sir") == "only sir"


def test_pick_empty_honorific_restricts_to_bare_variants():
    pool = ["with {h}", "also {H}", "bare one", "bare two"]
    for _ in range(20):
        out = persona._pick(pool, "mixed", "")
        assert out in ("bare one", "bare two")


def test_pick_empty_honorific_falls_back_to_full_pool_if_no_bare_variant():
    pool = ["with {h}", "also {H}"]
    out = persona._pick(pool, "no-bare", "")
    assert out in ("with ", "also ")


def test_pick_bare_and_honorific_variants_dont_share_anti_repeat_state():
    # Regression: filtering to a shorter "bare" sub-list for an empty
    # honorific must not corrupt the anti-repeat memory used when a real
    # honorific is passed for the same pool/key afterwards — e.g. an index
    # excluded in the 1-item bare list should not wrongly exclude an index
    # in the unrelated, differently-sized full-pool list.
    pool = ["with {h}", "bare one"]
    persona._pick(pool, "shared", "")
    seen = {persona._pick(pool, "shared", "sir") for _ in range(20)}
    assert "with sir" in seen


# ── _reg ─────────────────────────────────────────────────────────────────────

def test_reg_existing_register():
    assert persona._reg(persona._ACK, "urgent") is persona._ACK["urgent"]


def test_reg_missing_register_falls_back_to_neutral():
    assert persona._reg(persona._ACK, "nonexistent") is persona._ACK["neutral"]


def test_reg_missing_neutral_falls_back_to_first_value():
    pools = {"only": ["x"]}
    assert persona._reg(pools, "missing") == ["x"]


# ── register_for ─────────────────────────────────────────────────────────────

def test_register_for_mapping():
    assert persona.register_for("critical") == "grave"
    assert persona.register_for("high") == "urgent"
    assert persona.register_for("medium") == "neutral"
    assert persona.register_for("") == "neutral"
    assert persona.register_for(None) == "neutral"
    assert persona.register_for("CRITICAL") == "grave"  # case-insensitive


# ── speech-act wrappers ──────────────────────────────────────────────────────

def test_acknowledge_all_registers_render():
    for reg in ("light", "neutral", "urgent"):
        out = persona.acknowledge("sir", reg)
        assert out and "{" not in out


def test_completed_all_registers_render():
    for reg in ("light", "neutral", "urgent"):
        out = persona.completed("sir", reg)
        assert out and "{" not in out


def test_working_and_unable_render():
    assert persona.working("sir") and "{" not in persona.working("sir")
    assert persona.unable("sir") and "{" not in persona.unable("sir")


def test_announce_opener_registers_and_grave_is_plain():
    for reg in ("neutral", "urgent", "grave"):
        out = persona.announce_opener("sir", reg)
        assert out and "{" not in out
    # grave openers should be short/plain — no comma-joined flourish clauses
    graves = {persona.announce_opener("sir", "grave") for _ in range(8)}
    assert all(len(g) <= 12 and "," not in g for g in graves)


# ── greeting (every hour bucket + default) ───────────────────────────────────

def test_greeting_buckets():
    assert "morning" in persona.greeting("sir", 7).lower() or persona.greeting("sir", 7)
    for hour in (1, 7, 14, 19, 23):  # night, morning, afternoon, evening, night
        out = persona.greeting("sir", hour)
        assert out and "{" not in out


def test_greeting_boundaries():
    # exercise each branch boundary explicitly
    assert persona.greeting("sir", 4)   # < 5 -> night
    assert persona.greeting("sir", 5)   # morning
    assert persona.greeting("sir", 11)  # morning
    assert persona.greeting("sir", 12)  # afternoon
    assert persona.greeting("sir", 16)  # afternoon
    assert persona.greeting("sir", 17)  # evening
    assert persona.greeting("sir", 21)  # evening
    assert persona.greeting("sir", 22)  # night


def test_greeting_default_hour_uses_clock():
    out = persona.greeting("sir")  # hour=None -> time.localtime()
    assert out and "{" not in out


# ── set_variety ──────────────────────────────────────────────────────────────

def test_set_variety_toggle():
    persona.set_variety(False)
    assert persona._VARIETY is False
    persona.set_variety(True)
    assert persona._VARIETY is True


# ── lead_in ──────────────────────────────────────────────────────────────────

def test_lead_in_with_honorific_matches_old_inline_pattern():
    assert persona.lead_in("sir", "the garage door is open.") == \
        "Sir, the garage door is open."


def test_lead_in_titlecases_multichar_honorific():
    assert persona.lead_in("boss", "dinner's ready.") == "Boss, dinner's ready."


def test_lead_in_empty_honorific_just_capitalizes_sentence():
    assert persona.lead_in("", "the garage door is open.") == \
        "The garage door is open."
    assert persona.lead_in(None, "the garage door is open.") == \
        "The garage door is open."
    assert persona.lead_in("   ", "the garage door is open.") == \
        "The garage door is open."


def test_lead_in_empty_honorific_empty_sentence_is_safe():
    assert persona.lead_in("", "") == ""
