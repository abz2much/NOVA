"""Voice phrases (8.32.0) in the conversation turn, the unit-level half.

The PHACC file (tests/integration/test_voice_phrase_turn.py) drives real
turns. This file checks the shape in the source, and runs the real
_matches_voice_phrase pulled out with ast against the real matcher.
"""
import ast
import pathlib
import sys
import types

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SRC = (COMP / "conversation.py").read_text(encoding="utf-8")


def _index(marker: str) -> int:
    i = SRC.find(marker)
    assert i != -1, marker
    return i


def test_the_phrase_step_comes_first_and_guards_the_local_engine():
    step = _index("phrase = await voice_phrase_runner.try_phrase(")
    local = _index("local_result = await try_local(\n")
    guard = _index("if local_result is None and not phrase.defer_hint:")
    hint = _index('persona = persona + "\\n\\n" + phrase.defer_hint')
    handled = _index("if local_result and local_result.handled:")
    assert step < guard < local < hint < handled
    assert "local_result = phrase.result" in SRC[step:guard]
    assert "user_id=turn_user" in SRC[step:guard]


def test_the_gate_counts_a_phrase_as_addressed():
    assert "or _matches_voice_phrase(user_input.text))" in SRC
    # Still computed exactly once, before the persistence gate.
    assert SRC.count("_matches_voice_phrase(user_input.text)") == 1
    assert _index("_matches_voice_phrase(user_input.text)") < _index("if not relevant:")


def _helper(load, monkeypatch, value):
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_matches_voice_phrase")
    nc = load("nova_config")
    monkeypatch.setattr(nc, "_loaded", True)
    monkeypatch.setattr(nc, "_cache", {} if value is None else {"voice_phrases": value})
    load("voice_phrases")
    ns = {"__name__": "jc._conv_under_test", "__package__": "jc"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "conversation.py", "exec"), ns)
    return ns["_matches_voice_phrase"], nc


def test_matches_voice_phrase_runs_the_real_matcher(load, monkeypatch):
    live = {"enabled": True, "entries": [{
        "phrase_id": "vp_00000001", "name": "Cold", "phrases": ["im cold"],
        "match": "flexible", "enabled": True,
        "action": {"service": "script.turn_on"}}]}
    fn, _nc = _helper(load, monkeypatch, live)
    assert fn("I'm cold") is True
    assert fn("I'm old") is False
    assert live["entries"][0]["phrases"] == ["im cold"]     # untouched


def test_matches_voice_phrase_with_none_or_a_broken_config(load, monkeypatch):
    fn, nc = _helper(load, monkeypatch, None)
    assert fn("I'm cold") is False

    def boom(*a, **k):
        raise RuntimeError("config down")
    monkeypatch.setattr(nc, "get", boom)
    assert fn("I'm cold") is False
    assert isinstance(sys.modules.get("jc.voice_phrases"), types.ModuleType)
