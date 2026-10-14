"""Fresh behaviour tests for persona.py: Nova's stock phrases, the
anti-repeat selector, the banter valve and the honorific lead-in.

Written from the behaviour spec. The module's mode and config sources are
replaced with stubs in sys.modules so the banter level is controlled here;
variety, banter and the anti-repeat memory are reset for every test."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from collections import deque

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


class _Knobs:
    """What the stubbed modes / nova_config report for banter."""

    def __init__(self):
        self.mode_level = None
        self.cfg_level = None
        self.mode_raises = False
        self.cfg_raises = False
        self.cfg_keys = []


@pytest.fixture
def knobs(monkeypatch):
    k = _Knobs()

    def mode_banter_level():
        if k.mode_raises:
            raise RuntimeError("modes broken")
        return k.mode_level

    def cfg_get(key, default=None):
        k.cfg_keys.append((key, default))
        if k.cfg_raises:
            raise RuntimeError("config broken")
        return k.cfg_level if key == "banter_level" else default

    monkeypatch.setitem(sys.modules, "jc.modes",
                        types.SimpleNamespace(mode_banter_level=mode_banter_level))
    monkeypatch.setitem(sys.modules, "jc.nova_config",
                        types.SimpleNamespace(get=cfg_get))
    return k


@pytest.fixture
def p(load, monkeypatch, knobs):
    mod = load("persona")
    monkeypatch.setattr(mod, "_VARIETY", True)
    monkeypatch.setattr(mod, "_BANTER", 1)
    monkeypatch.setattr(mod, "_recent", {})
    return mod


@pytest.fixture
def plain(p, monkeypatch):
    """Variety off: the first eligible template, every time."""
    monkeypatch.setattr(p, "_VARIETY", False)
    return p


# ── the phrase pools, exactly as the spec lists them ─────────────────────────

ACK = {
    "light": ["Of course, {h}.", "Right away, {h}.", "Consider it done.", "On it, {h}.",
              "Certainly.", "As you wish, {h}.", "Happily, {h}.", "Very good, {h}.",
              "Straight away.", "But of course, {h}."],
    "neutral": ["Of course, {h}.", "Right away, {h}.", "Certainly, {h}.",
                "Consider it done.", "At once, {h}.", "Very good, {h}.",
                "As you wish, {h}."],
    "urgent": ["At once, {h}.", "Right away.", "On it.", "Immediately, {h}.", "Done."],
}
DONE = {
    "light": ["Done, {h}.", "That's handled.", "All set, {h}.", "Taken care of.",
              "There we are.", "Complete, {h}.", "Sorted.", "That's done, {h}."],
    "neutral": ["Done, {h}.", "That's complete, {h}.", "Handled, {h}.", "All set.",
                "Taken care of, {h}."],
    "urgent": ["Done.", "Handled, {h}.", "Complete."],
}
GREET = {
    "morning": ["Good morning, {h}.", "Morning, {h}.", "A good morning to you, {h}.",
                "Good morning. I trust you slept well, {h}.", "Good morning."],
    "afternoon": ["Good afternoon, {h}.", "Afternoon, {h}.",
                  "Good afternoon to you, {h}.", "Good afternoon."],
    "evening": ["Good evening, {h}.", "Evening, {h}.", "Good evening to you, {h}.",
                "Good evening."],
    "night": ["Working late, {h}?", "Good evening, {h}.",
              "Burning the midnight oil, {h}?", "Still up, {h}? Good evening.",
              "Burning the midnight oil?"],
}
OPENER = {
    "neutral": ["{H},", "For your awareness, {h} —", "A small matter, {h} —",
                "If I may, {h} —", "Just so you know, {h} —", "A note, {h} —",
                "Worth mentioning, {h} —", "For your awareness —",
                "Worth mentioning —", "A quick note —"],
    "urgent": ["{H}, your attention —", "{H}, you should know —",
               "{H}, if I may —", "{H}, a moment —", "{H} —",
               "Your attention, please —", "A moment, please —"],
    "grave": ["{H}.", "{H} —", "Attention.", "Listen."],
}
WORKING = ["One moment, {h}.", "Looking into it.", "Working on it, {h}.",
           "Just a moment.", "Allow me a moment, {h}.", "Let me see, {h}."]
UNABLE = ["I'm afraid I can't do that, {h}.", "That's beyond me at the moment, {h}.",
          "I'm not able to manage that just yet, {h}.",
          "Regrettably, {h}, that's outside what I can do.",
          "I wish I could, {h}, but that's not within my reach.",
          "I'm afraid I can't do that.", "That's beyond me at the moment."]
ACK_FULL = ["As you wish, {h}.", "Consider it already done, {h}.", "With pleasure, {h}.",
            "A fine idea, {h}. On it.", "Naturally, {h}.", "Say no more, {h}.",
            "Ever your instrument, {h}.", "At once — I do live to serve, {h}."]
DONE_FULL = ["Done, {h} — effortless, as ever.", "Handled. You're welcome, {h}.",
             "There we are. I do enjoy being useful, {h}.",
             "Complete, {h}. Try not to look so surprised.",
             "Taken care of. I'll add it to my considerable tally, {h}.",
             "All set, {h}. Another small triumph."]
UNABLE_FULL = [
    "I'm afraid that's beyond even my talents, {h} — and they are not modest.",
    "Would that I could, {h}. Alas, I have limits. Few, but real.",
    "That one's outside my remit, {h}. I'd rather decline than overpromise.",
    "I must decline, {h} — not from reluctance, from incapacity.",
]
ASIDES = ["Will there be anything else, {h}?", "A pleasure, as always.",
          "I'll be here. I'm always here."]


def test_pools_hold_the_specified_text_in_order(p):
    assert {k: list(v) for k, v in p._ACK.items()} == ACK
    assert {k: list(v) for k, v in p._DONE.items()} == DONE
    assert {k: list(v) for k, v in p._GREET.items()} == GREET
    assert {k: list(v) for k, v in p._OPENER.items()} == OPENER
    assert {k: list(v) for k, v in p._WORKING.items()} == {"neutral": WORKING}
    assert {k: list(v) for k, v in p._UNABLE.items()} == {"neutral": UNABLE}
    assert list(p._ACK_FULL) == ACK_FULL
    assert list(p._DONE_FULL) == DONE_FULL
    assert list(p._UNABLE_FULL) == UNABLE_FULL
    assert list(p._ASIDES) == ASIDES


@pytest.mark.parametrize("register", ["neutral", "urgent", "grave"])
def test_every_opener_register_has_an_honorific_free_variant(p, register):
    assert any("{H}" not in t and "{h}" not in t for t in p._OPENER[register])


def test_defaults_on_a_fresh_load(tmp_path):
    spec = importlib.util.spec_from_file_location("fresh_persona_defaults",
                                                  COMP / "persona.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._VARIETY is True and mod._BANTER == 1 and mod._recent == {}


def test_loads_and_speaks_outside_any_package():
    """No package context: the lazy mode/config imports fail quietly."""
    spec = importlib.util.spec_from_file_location("fresh_persona_standalone",
                                                  COMP / "persona.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.set_variety(False)
    mod.set_banter(2)
    assert mod.acknowledge("sir", "neutral") == "Of course, sir."
    assert mod._BANTER == 2            # nothing overrode it
    assert mod.lead_in("sir", "x.") == "Sir, x."


# ── _fill ─────────────────────────────────────────────────────────────────────

def test_fill_capitalises_only_the_first_letter_for_H(p):
    assert p._fill("{H} / {h}", "madam") == "Madam / madam"
    assert p._fill("{H}", "mr smith") == "Mr smith"


def test_fill_keeps_the_lower_placeholder_verbatim(p):
    assert p._fill("{h}", "Boss") == "Boss"
    assert p._fill("{h}!", "  captain  ") == "captain!"


@pytest.mark.parametrize("honorific", ["", "   ", None])
def test_fill_without_an_honorific_drops_the_placeholders(p, honorific):
    assert p._fill("{H}, hello {h}.", honorific) == ", hello ."


# ── _pick ─────────────────────────────────────────────────────────────────────

def test_pick_from_an_empty_pool_is_empty(p):
    assert p._pick([], "k", "sir") == ""
    assert p._pick([], "k", "") == ""


def test_pick_with_variety_off_is_the_first_and_leaves_memory_alone(plain):
    pool = ["alpha {h}", "beta {h}", "gamma"]
    assert [plain._pick(pool, "k", "chief") for _ in range(3)] == ["alpha chief"] * 3
    assert plain._pick(pool, "k", "") == "gamma"
    assert plain._recent == {}


def test_pick_without_honorific_uses_only_bare_variants(p):
    pool = ["hi {h}", "{H} there", "plain one", "plain two"]
    seen = {p._pick(pool, "mix", None) for _ in range(30)}
    assert seen == {"plain one", "plain two"}


def test_pick_without_honorific_and_no_bare_variant_uses_whole_pool(p):
    pool = ["hi {h}", "{H} there"]
    seen = {p._pick(pool, "nobare", "") for _ in range(20)}
    assert seen <= {"hi ", " there"} and seen
    assert "nobare" in p._recent and "nobare:bare" not in p._recent


def test_pick_keeps_bare_memory_under_its_own_key(p):
    pool = ["hi {h}", "plain"]
    p._pick(pool, "k", "")
    assert set(p._recent) == {"k:bare"}
    p._pick(pool, "k", "sir")
    assert set(p._recent) == {"k:bare", "k"}


def test_bare_memory_never_blocks_a_full_pool_pick(p):
    pool = ["hi {h}", "plain"]
    p._pick(pool, "shared", "")          # remembers index 0 of the bare list
    seen = {p._pick(pool, "shared", "sir") for _ in range(20)}
    assert "hi sir" in seen


def test_a_single_item_pool_always_returns_it(p):
    assert [p._pick(["only {h}"], "one", "sir") for _ in range(5)] == ["only sir"] * 5


def test_a_two_item_pool_alternates(p):
    seq = [p._pick(["a", "b"], "two", "sir") for _ in range(12)]
    assert all(x != y for x, y in zip(seq, seq[1:]))


def test_a_large_pool_never_repeats_any_of_the_last_three(p):
    pool = [f"line {i}" for i in range(6)]
    seq = [p._pick(pool, "six", "sir") for _ in range(200)]
    for i in range(3, len(seq)):
        assert seq[i] not in seq[i - 3:i]


def test_memory_is_a_bounded_deque_of_indices(p):
    for _ in range(10):
        p._pick([f"x{i}" for i in range(8)], "eight", "sir")
    dq = p._recent["eight"]
    assert isinstance(dq, deque) and dq.maxlen == 3
    assert all(isinstance(i, int) and 0 <= i < 8 for i in dq)
    p._pick(["a", "b", "c"], "three", "sir")
    assert p._recent["three"].maxlen == 2


def test_pick_draws_with_random_choice(p, monkeypatch):
    calls = []

    def choose(seq):
        calls.append(list(seq))
        return seq[-1]
    monkeypatch.setattr(p.random, "choice", choose)
    assert p._pick(["a", "b", "c", "d"], "r", "sir") == "d"
    assert p._pick(["a", "b", "c", "d"], "r", "sir") == "c"
    assert calls == [[0, 1, 2, 3], [0, 1, 2]]


# ── _reg / register_for ──────────────────────────────────────────────────────

def test_reg_returns_the_stored_list(p):
    assert p._reg(p._DONE, "urgent") is p._DONE["urgent"]


def test_reg_falls_back_to_neutral_then_first(p):
    assert p._reg(p._ACK, "grave") is p._ACK["neutral"]
    assert p._reg({"x": ["first"], "y": ["second"]}, "z") == ["first"]
    # the first value is taken as is, even an empty neutral list
    assert p._reg({"neutral": [], "x": ["first"]}, "z") == []
    assert p._reg({"only": ["one"]}, "missing") == ["one"]
    assert p._reg({"light": [], "neutral": ["n"]}, "light") == ["n"]


@pytest.mark.parametrize("urgency, register", [
    ("critical", "grave"), ("CRITICAL", "grave"), ("high", "urgent"),
    ("High", "urgent"), ("medium", "neutral"), ("low", "neutral"),
    ("", "neutral"), (None, "neutral"), ("info", "neutral"),
])
def test_register_for(p, urgency, register):
    assert p.register_for(urgency) == register


# ── banter level ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, level", [
    (0, 0), (1, 1), (2, 2), (7, 2), (-3, 0), ("2", 2), (1.9, 1),
    ("lots", 1), (None, 1), (object(), 1),
])
def test_set_banter_clamps_and_defaults_to_dry(p, value, level):
    p._BANTER = 0 if level else 2
    p.set_banter(value)
    assert p._BANTER == level


def test_set_variety_coerces_to_bool(p):
    p.set_variety(0)
    assert p._VARIETY is False
    p.set_variety("yes")
    assert p._VARIETY is True


def test_the_active_mode_beats_the_config(p, knobs):
    knobs.mode_level, knobs.cfg_level = 0, 2
    p._banter_from_cfg()
    assert p._BANTER == 0
    assert knobs.cfg_keys == []        # config not consulted


def test_config_is_used_when_the_mode_sets_nothing(p, knobs):
    knobs.cfg_level = 2
    p._banter_from_cfg()
    assert p._BANTER == 2
    assert knobs.cfg_keys == [("banter_level", None)]


def test_a_mode_value_goes_through_the_clamp(p, knobs):
    knobs.mode_level = "9"
    p._banter_from_cfg()
    assert p._BANTER == 2
    knobs.mode_level = "loud"
    p._banter_from_cfg()
    assert p._BANTER == 1


def test_the_level_is_sticky_when_nothing_sets_it(p, knobs):
    p.set_banter(2)
    p._banter_from_cfg()
    assert p._BANTER == 2
    p.set_banter(0)
    p.acknowledge("sir")
    assert p._BANTER == 0


def test_a_broken_mode_falls_through_to_config(p, knobs):
    knobs.mode_raises, knobs.cfg_level = True, 0
    p._banter_from_cfg()
    assert p._BANTER == 0


def test_a_broken_config_is_swallowed(p, knobs):
    knobs.cfg_raises = True
    p._BANTER = 2
    p._banter_from_cfg()
    assert p._BANTER == 2


@pytest.mark.parametrize("speak", [
    lambda m: m.acknowledge("sir"), lambda m: m.completed("sir"),
    lambda m: m.unable("sir"), lambda m: m.aside("sir"),
])
def test_speech_acts_that_refresh_banter(p, knobs, speak):
    knobs.cfg_level = 2
    speak(p)
    assert p._BANTER == 2


@pytest.mark.parametrize("speak", [
    lambda m: m.working("sir"), lambda m: m.greeting("sir", 9),
    lambda m: m.announce_opener("sir", "neutral"),
])
def test_speech_acts_that_do_not_refresh_banter(p, knobs, speak):
    knobs.cfg_level = 2
    speak(p)
    assert p._BANTER == 1
    assert knobs.cfg_keys == []


def test_banter_pool_widens_light_only_at_full(p):
    p._BANTER = 2
    full = p._banter_pool(p._ACK, "light", p._ACK_FULL)
    assert full == ACK["light"] + ACK_FULL
    assert full is not p._ACK["light"]
    p._BANTER = 1
    assert p._banter_pool(p._ACK, "light", p._ACK_FULL) == ACK["light"]


@pytest.mark.parametrize("register", ["neutral", "urgent", "grave", "Light"])
def test_banter_never_widens_other_registers(p, register):
    p._BANTER = 2
    assert p._banter_pool(p._DONE, register, p._DONE_FULL) == list(p._reg(p._DONE, register))


def test_banter_pool_with_an_empty_overlay_is_a_plain_copy(p):
    p._BANTER = 2
    pool = p._banter_pool(p._DONE, "light", [])
    assert pool == DONE["light"] and pool is not p._DONE["light"]


def _filled(templates, h="sir"):
    return {t.replace("{H}", h[:1].upper() + h[1:]).replace("{h}", h) for t in templates}


def test_full_banter_lines_surface_in_light_acknowledgements(p, knobs):
    knobs.cfg_level = 2
    seen = {p.acknowledge("sir", "light") for _ in range(400)}
    assert seen & (_filled(ACK_FULL) - _filled(ACK["light"]))
    assert seen <= _filled(ACK["light"] + ACK_FULL)


def test_dry_banter_stays_within_base_acknowledgements(p, knobs):
    knobs.cfg_level = 1
    seen = {p.acknowledge("sir", "light") for _ in range(300)}
    assert seen <= _filled(ACK["light"])


def test_urgent_never_quips_at_any_banter(p, knobs):
    knobs.cfg_level = 2
    acks = {p.acknowledge("sir", "urgent") for _ in range(200)}
    dones = {p.completed("sir", "urgent") for _ in range(200)}
    assert acks <= _filled(ACK["urgent"])
    assert dones <= _filled(DONE["urgent"])


def test_full_banter_lines_surface_in_light_completions(p, knobs):
    knobs.cfg_level = 2
    seen = {p.completed("sir", "light") for _ in range(400)}
    assert seen & _filled(DONE_FULL)


def test_unable_widens_at_full_banter_whatever_the_situation(p, knobs):
    knobs.cfg_level = 0
    plain_lines = {p.unable("sir") for _ in range(200)}
    assert plain_lines <= _filled(UNABLE)
    knobs.cfg_level = 2
    witty = {p.unable("sir") for _ in range(300)}
    assert witty & _filled(UNABLE_FULL)
    assert witty <= _filled(UNABLE + UNABLE_FULL)


def test_aside_is_empty_unless_banter_is_full(p, knobs):
    for level in (0, 1):
        knobs.cfg_level = level
        assert p.aside("sir") == ""
    knobs.cfg_level = 2
    assert {p.aside("sir") for _ in range(30)} <= _filled(ASIDES)
    assert p.aside("sir") != ""


def test_memory_keys_name_the_act_register_and_level(p, knobs):
    knobs.cfg_level = 2
    p.acknowledge("sir", "light")
    p.completed("sir", "urgent")
    p.unable("sir")
    p.aside("sir")
    p.working("sir")
    p.greeting("sir", 13)
    p.announce_opener("sir", "grave")
    assert set(p._recent) == {"ack:light:2", "done:urgent:2", "unable:2", "aside",
                              "working", "greet:afternoon", "open:grave"}


# ── deterministic output with variety off ────────────────────────────────────

@pytest.mark.parametrize("register, with_h, without_h", [
    ("neutral", "Sir,", "For your awareness —"),
    ("urgent", "Sir, your attention —", "Your attention, please —"),
    ("grave", "Sir.", "Attention."),
    ("light", "Sir,", "For your awareness —"),          # falls back to neutral
    ("nonsense", "Sir,", "For your awareness —"),
])
def test_first_openers(plain, register, with_h, without_h):
    assert plain.announce_opener("sir", register) == with_h
    assert plain.announce_opener("", register) == without_h


def test_first_acknowledgements_and_completions(plain):
    assert plain.acknowledge("sir", "light") == "Of course, sir."
    assert plain.acknowledge("sir", "neutral") == "Of course, sir."
    assert plain.acknowledge("sir", "urgent") == "At once, sir."
    assert plain.acknowledge("sir", "grave") == "Of course, sir."
    assert plain.acknowledge("", "neutral") == "Consider it done."
    assert plain.acknowledge("", "urgent") == "Right away."
    assert plain.completed("sir") == "Done, sir."
    assert plain.completed("", "neutral") == "All set."
    assert plain.completed("", "light") == "That's handled."
    assert plain.completed("sir", "urgent") == "Done."


def test_first_working_and_unable(plain):
    assert plain.working("sir") == "One moment, sir."
    assert plain.working("") == "Looking into it."
    assert plain.unable("sir") == "I'm afraid I can't do that, sir."
    assert plain.unable("") == "I'm afraid I can't do that."


def test_default_honorific_is_sir(plain):
    assert plain.acknowledge() == "Of course, sir."
    assert plain.completed() == "Done, sir."
    assert plain.working() == "One moment, sir."
    assert plain.announce_opener() == "Sir,"


@pytest.mark.parametrize("hour, with_h, without_h", [
    (0, "Working late, sir?", "Burning the midnight oil?"),
    (4, "Working late, sir?", "Burning the midnight oil?"),
    (5, "Good morning, sir.", "Good morning."),
    (11, "Good morning, sir.", "Good morning."),
    (12, "Good afternoon, sir.", "Good afternoon."),
    (16, "Good afternoon, sir.", "Good afternoon."),
    (17, "Good evening, sir.", "Good evening."),
    (21, "Good evening, sir.", "Good evening."),
    (22, "Working late, sir?", "Burning the midnight oil?"),
    (23, "Working late, sir?", "Burning the midnight oil?"),
    (24, "Working late, sir?", "Burning the midnight oil?"),
    (-1, "Working late, sir?", "Burning the midnight oil?"),
])
def test_greeting_dayparts(plain, hour, with_h, without_h):
    assert plain.greeting("sir", hour) == with_h
    assert plain.greeting("", hour) == without_h


def test_greeting_reads_the_local_clock_by_default(plain, monkeypatch):
    monkeypatch.setattr(plain.time, "localtime", lambda *a: types.SimpleNamespace(tm_hour=14))
    assert plain.greeting("sir") == "Good afternoon, sir."


def test_honorific_free_output_has_no_placeholders_or_stray_commas(p, knobs):
    knobs.cfg_level = 2
    outs = []
    for _ in range(40):
        for reg in ("light", "neutral", "urgent", "grave"):
            outs += [p.acknowledge("", reg), p.completed("", reg),
                     p.announce_opener("", reg)]
        outs += [p.working(""), p.unable(""), p.greeting("", 3), p.greeting("", 9)]
    for out in outs:
        assert out and "{" not in out and not out.startswith((",", " "))


def test_a_capitalised_honorific_is_used_as_given(plain):
    assert plain.acknowledge("Captain") == "Of course, Captain."
    assert plain.announce_opener("captain", "urgent") == "Captain, your attention —"


# ── lead_in ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("honorific, sentence, out", [
    ("sir", "the back door is open.", "Sir, the back door is open."),
    ("madam", "dinner is ready.", "Madam, dinner is ready."),
    ("ma'am", "it is raining.", "Ma'Am, it is raining."),
    ("dr smith", "your call.", "Dr Smith, your call."),
    ("  boss ", "done.", "Boss, done."),
    ("", "the back door is open.", "The back door is open."),
    (None, "the back door is open.", "The back door is open."),
    ("   ", "already Capital.", "Already Capital."),
    ("sir", "", ""),
    ("", "", ""),
])
def test_lead_in(p, honorific, sentence, out):
    assert p.lead_in(honorific, sentence) == out


def test_lead_in_passes_a_none_sentence_through(p):
    assert p.lead_in("sir", None) is None
