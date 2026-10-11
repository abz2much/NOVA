"""Voice phrases (8.32.0): the pure matcher and the entry checks.

Every must-match and must-not-match case from the design, plus the
validation rules for a saved entry. voice_phrases.py uses only the standard
library, so this needs no Home Assistant at all.
"""
import json

import pytest


@pytest.fixture
def vp(load):
    return load("voice_phrases")


ALLOWED = {"light", "switch", "climate", "scene", "script", "automation", "lock"}


def _entry(pid, phrases, *, name=None, match="flexible", enabled=True,
           service="script.turn_on", target="script.x", allow_ai=False):
    action = {"service": service, "data": {}}
    if target:
        action["target"] = {"entity_id": target}
    return {"phrase_id": pid, "name": name or pid, "phrases": list(phrases),
            "match": match, "allow_ai": allow_ai, "enabled": enabled,
            "action": action, "reply": ""}


def _cfg(*entries, enabled=True):
    return {"enabled": enabled, "entries": list(entries)}


# ── normalise ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Nova, the house is cold.", "the house is cold"),
    ("Hey Nova the house is cold please", "the house is cold"),
    ("ok nova, could you warm it up", "warm it up"),
    ("okay nova please can you turn it on", "turn it on"),
    ("I’m cold", "i am cold"),
    ("im cold", "i am cold"),
    ("its freezing in here", "it is freezing in here"),
    ("thats it", "that is it"),
    ("ive had enough", "i have had enough"),
    ("cant sleep", "cannot sleep"),
    ("wont start", "will not start"),
    ("the house isn't cold", "the house is not cold"),
    ("the house isnt cold", "the house is not cold"),
    ("I don't want it", "i do not want it"),
    ("i dont want it", "i do not want it"),
    ("I want it", "i want it"),          # "want" is not an n't ending
    ("the plant", "the plant"),
    ("  lots   of\tspace!! ", "lots of space"),
    ("nova", ""),
    ("", ""),
    (None, ""),
    (42, ""),
])
def test_normalise(vp, text, expected):
    assert vp.normalise(text) == expected


# ── must match ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("said,phrase,tier", [
    ("Nova, the house is cold.", "the house is cold", "exact"),
    ("the house is a bit cold", "the house is cold", "flexible"),
    ("im cold", "I'm cold", "exact"),
    ("nova its freezing in here", "it is freezing in here", "exact"),
    ("the house is really cold now", "the house is cold", "flexible"),
    ("the houze is cold", "the house is cold", None),       # too short to forgive
    ("i am freezzing", "i am freezing", "flexible"),          # one letter slip, long word
])
def test_must_match(vp, said, phrase, tier):
    m = vp.match(said, _cfg(_entry("vp_00000001", [phrase], name="Cold")))
    if tier is None:
        assert m is None
        return
    assert m is not None and m.phrase_id == "vp_00000001" and m.name == "Cold"
    assert m.phrase == phrase and m.tier == tier


# ── must not match ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("said,phrase", [
    ("turn off the heating", "turn on the heating"),
    ("turn on the heating", "turn off the heating"),
    ("unlock the door", "lock the door"),
    ("lock the door", "unlock the door"),
    ("i am old", "i am cold"),
    ("the house is not cold", "the house is cold"),
    ("the house isnt cold", "the house is cold"),
    ("the house isn't cold", "the house is cold"),
    ("is the house cold", "the house is cold"),
    ("when i got home last night the house is cold and dark and wet",
     "the house is cold"),
    ("my sister said the garage is freezing", "it is freezing in here"),
    ("turn on the seating", "turn on the heating"),
    # Blocked on purpose by the polarity guard: "warm" is the opposite of
    # "cold", so a sentence that both says the phrase and asks for the
    # opposite runs nothing rather than guessing.
    ("the house is cold, warm it up", "the house is cold"),
    ("the house is warm", "the house is cold"),
])
def test_must_not_match(vp, said, phrase):
    assert vp.match(said, _cfg(_entry("vp_00000001", [phrase]))) is None


def test_one_word_phrase_tolerates_only_one_extra(vp):
    cfg = _cfg(_entry("vp_00000001", ["freezing"]))
    assert vp.match("freezing", cfg) is not None
    assert vp.match("so freezing tonight", cfg) is not None      # one extra
    assert vp.match("garage freezing tonight", cfg) is None      # two extra


def test_exact_mode_needs_the_same_words(vp):
    cfg = _cfg(_entry("vp_00000001", ["the house is cold"], match="exact"))
    assert vp.match("Nova, the house is cold!", cfg).tier == "exact"
    assert vp.match("the house is a bit cold", cfg) is None


def test_two_entries_that_tie_match_nothing(vp):
    cfg = _cfg(_entry("vp_00000001", ["house cold"]),
               _entry("vp_00000002", ["cold house"]))
    assert vp.match("the house is cold", cfg) is None


def test_exact_beats_flexible(vp):
    cfg = _cfg(_entry("vp_00000001", ["cold"]),
               _entry("vp_00000002", ["the house is cold"]))
    assert vp.match("the house is cold", cfg).phrase_id == "vp_00000002"


def test_more_content_words_win(vp):
    cfg = _cfg(_entry("vp_00000001", ["cold"]),
               _entry("vp_00000002", ["house cold"]))
    assert vp.match("the house is very cold tonight", cfg).phrase_id == "vp_00000002"


def test_fewer_extra_words_break_a_tie(vp):
    # Same number of content words; the second leaves one fewer word over.
    cfg = _cfg(_entry("vp_00000001", ["house cold"]),
               _entry("vp_00000002", ["house cold tonight"]),
               _entry("vp_00000003", ["house cold again"]))
    m = vp.match("house cold tonight again", cfg)
    assert m is None   # vp_2 and vp_3 tie (3 words, 1 extra): ambiguous
    cfg = _cfg(_entry("vp_00000001", ["house cold"]),
               _entry("vp_00000002", ["house cold tonight"]))
    assert vp.match("house cold tonight again", cfg).phrase_id == "vp_00000002"


def test_two_phrases_of_one_entry_are_not_a_tie(vp):
    cfg = _cfg(_entry("vp_00000001", ["house cold", "cold house"]))
    assert vp.match("the house is cold", cfg).phrase_id == "vp_00000001"


def test_disabled_entries_and_switch(vp):
    e = _entry("vp_00000001", ["the house is cold"])
    assert vp.match("the house is cold", _cfg(e, enabled=False)) is None
    e["enabled"] = False
    assert vp.match("the house is cold", _cfg(e)) is None


@pytest.mark.parametrize("cfg", [None, {}, [], {"entries": "x"}, {"entries": [1, None]},
                                 {"entries": [{"phrases": ["cold"]}]}])
def test_bad_config_never_raises(vp, cfg):
    assert vp.match("cold", cfg) is None


def test_question_phrase_may_match_a_question(vp):
    cfg = _cfg(_entry("vp_00000001", ["is anyone home"]))
    assert vp.match("is anyone home", cfg) is not None


# ── validate_entry ──────────────────────────────────────────────────────────

def _new(**over):
    e = {"name": "House is cold", "phrases": ["the house is cold", "I'm cold"],
         "match": "flexible", "allow_ai": True, "enabled": True,
         "action": {"service": "script.turn_on",
                    "target": {"entity_id": "script.heating_boost"}, "data": {}},
         "reply": "Warming the house up{addr}."}
    e.update(over)
    return e


def test_a_good_entry_is_cleaned_and_gets_an_id(vp):
    clean, errors, warnings = vp.validate_entry(_new(), [], allowed_domains=ALLOWED)
    assert errors == [] and warnings == []
    assert clean["phrase_id"].startswith("vp_") and len(clean["phrase_id"]) == 11
    assert clean["phrases"] == ["the house is cold", "I'm cold"]
    assert clean["allow_ai"] is True and clean["match"] == "flexible"
    assert clean["action"] == {"service": "script.turn_on",
                               "target": {"entity_id": "script.heating_boost"},
                               "data": {}}
    assert "id" not in clean


def test_update_keeps_the_id_and_unknown_id_is_refused(vp):
    first, _, _ = vp.validate_entry(_new(), [], allowed_domains=ALLOWED)
    again, errors, _ = vp.validate_entry(
        _new(phrase_id=first["phrase_id"], name="New name"), [first], allowed_domains=ALLOWED)
    assert errors == [] and again["phrase_id"] == first["phrase_id"]
    _, errors, _ = vp.validate_entry(_new(phrase_id="vp_deadbeef"), [first],
                                     allowed_domains=ALLOWED)
    assert errors[0] == "That voice phrase no longer exists."


def _errors(vp, existing=(), **over):
    return vp.validate_entry(_new(**over), list(existing), allowed_domains=ALLOWED)[1]


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "x" * 61}, {"name": None},
    {"phrases": []}, {"phrases": ["ok"] * 11}, {"phrases": ["a"]},
    {"phrases": ["x" * 81]}, {"phrases": ["the it is"]}, {"phrases": [3]},
    {"match": "fuzzy"}, {"allow_ai": "yes"}, {"enabled": 1},
    {"action": None},
    {"action": {"service": "homeassistant.restart"}},
    {"action": {"service": "shell_command"}},
    {"action": {"service": "script.turn_on", "target": {"area_id": "kitchen"}}},
    {"action": {"service": "script.turn_on", "target": {"entity_id": "light.x"}}},
    {"action": {"service": "script.turn_on",
                "target": {"entity_id": [f"script.s{i}" for i in range(11)]}}},
    {"action": {"service": "script.turn_on", "target": {"entity_id": ["script.a", "script.a"]}}},
    {"action": {"service": "shell_command.boost", "target": {"entity_id": "shell_command.boost"}}},
    {"action": {"service": "script.turn_on", "data": {"entity_id": "lock.front"}}},
    {"action": {"service": "script.turn_on", "data": {"variables": {"target": {}}}}},
    {"action": {"service": "script.turn_on", "data": {"x": [{"area_id": "a"}]}}},
    {"action": {"service": "script.turn_on", "data": {"device_id": "d"}}},
    {"action": {"service": "script.turn_on", "data": {"x": "{{ states('a') }}"}}},
    {"action": {"service": "script.turn_on", "data": {"x": "{% if %}"}}},
    {"action": {"service": "script.turn_on", "data": {"x": "y" * 2100}}},
    {"action": {"service": "script.turn_on", "data": [1]}},
    {"action": {"service": "script.turn_on", "extra": 1}},
    {"reply": "x" * 201}, {"reply": 5},
])
def test_bad_entries_are_refused(vp, over):
    assert _errors(vp, **over) != []


def test_a_phrase_owned_by_another_entry_is_refused(vp):
    other, _, _ = vp.validate_entry(_new(name="Other", phrases=["it's freezing"]), [],
                                    allowed_domains=ALLOWED)
    errors = _errors(vp, [other], phrases=["its freezing"])
    assert errors == ['"its freezing" is already used by "Other".']


def test_at_most_one_hundred_entries(vp):
    existing = [{"phrase_id": f"vp_{i:08x}", "phrases": [f"phrase number {i}"]}
                for i in range(100)]
    assert "You can have at most 100 voice phrases." in _errors(vp, existing)


def test_shell_command_never_allows_ai(vp):
    clean, errors, _ = vp.validate_entry(
        _new(action={"service": "shell_command.heat_boost"}, allow_ai=True), [],
        allowed_domains=ALLOWED)
    assert errors == [] and clean["allow_ai"] is False
    assert clean["action"] == {"service": "shell_command.heat_boost", "data": {}}


def test_data_size_limit_is_exact(vp):
    ok = {"k": "y" * (2048 - len('{"k":""}'))}
    assert len(json.dumps(ok, separators=(",", ":"))) == 2048
    assert _errors(vp, action={"service": "script.turn_on", "data": ok}) == []
    big = {"k": "y" * (2049 - len('{"k":""}'))}
    assert _errors(vp, action={"service": "script.turn_on", "data": big}) != []


def test_duplicate_phrases_in_one_entry_are_folded(vp):
    clean, errors, _ = vp.validate_entry(_new(phrases=["I'm cold", "im cold"]), [],
                                         allowed_domains=ALLOWED)
    assert errors == [] and clean["phrases"] == ["I'm cold"]


def test_a_non_dict_entry_is_refused(vp):
    assert vp.validate_entry("x", [], allowed_domains=ALLOWED)[0] is None


# ── helpers for the runner and the AI ───────────────────────────────────────

def test_target_ids_and_find_entry(vp):
    e = _entry("vp_00000001", ["cold"], target=["script.a", "script.b"])
    assert vp.target_ids(e) == ["script.a", "script.b"]
    assert vp.target_ids(_entry("vp_00000002", ["cold"], target="script.a")) == ["script.a"]
    assert vp.target_ids(_entry("vp_00000003", ["cold"], target=None)) == []
    assert vp.find_entry(_cfg(e), "vp_00000001") is e
    assert vp.find_entry(_cfg(e), "vp_99999999") is None
    assert vp.find_entry(None, "x") is None


def test_prompt_lines_lists_only_ai_entries(vp):
    cfg = _cfg(
        _entry("vp_00000001", ["the house is cold", "i am cold"], name="House is cold",
               allow_ai=True),
        _entry("vp_00000002", ["film time"], allow_ai=False),
        _entry("vp_00000003", ["boost"], allow_ai=True, service="shell_command.boost",
               target=None),
        _entry("vp_00000004", ["bed"], allow_ai=True, enabled=False),
        _entry("vp_00000005", ["lights\nout"], name="Bed\ntime", allow_ai=True),
    )
    lines, ids = vp.prompt_lines(cfg)
    assert lines == ('[1] House is cold (e.g. "the house is cold")\n'
                     '[2] Bed time (e.g. "lights out")')
    assert ids == ["vp_00000001", "vp_00000005"]
    assert "vp_" not in lines                     # ids stay out of the user text
    assert vp.prompt_lines(_cfg()) == ("", [])
    assert vp.prompt_lines(_cfg(enabled=False)) == ("", [])


def test_prompt_lines_caps_at_fifty(vp):
    cfg = _cfg(*[_entry(f"vp_{i:08x}", [f"phrase {i}"], allow_ai=True) for i in range(60)])
    lines, ids = vp.prompt_lines(cfg)
    assert len(lines.splitlines()) == 50 and len(ids) == 50
