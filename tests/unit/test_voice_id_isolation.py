"""Nova Voice ID never reaches the safety path or faces (8.36.0, 8.37.0).

These modules must not import the Voice ID modules or read the result, so a
voice can never unlock, disarm, open or stand down. From 8.37.0 the result
may decide who is speaking, through identity.resolve(voice=...) only.
"""
import ast
import pathlib

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
# identity.py left this list in 8.37.0: it takes the voice result to decide
# who is speaking, by its fields only, and still imports nothing from Voice ID
# (test_identity_imports_nothing_from_voice_id below). No safety path reads
# identity (tests/unit/test_voice_safety.py).
GUARDED = ["policy.py", "core_safety.py", "core_lockdown.py", "face_roster.py",
           "voice_recognition.py"]
FORBIDDEN_MODULES = {"voice_id", "voice_enrol", "voice_id_store", "ws_voice_id"}
FORBIDDEN_NAMES = {"VOICE_RESULT", "take_turn_voice", "set_turn_voice", "VoiceResult"}


@pytest.mark.parametrize("name", GUARDED)
def test_module_never_imports_voice_id_or_reads_the_result(name):
    tree = ast.parse((COMP / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = set((node.module or "").split("."))
            assert not parts & FORBIDDEN_MODULES, f"{name} imports {node.module}"
            assert not {a.name for a in node.names} & (FORBIDDEN_MODULES | FORBIDDEN_NAMES), \
                f"{name} imports {[a.name for a in node.names]}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not set(alias.name.split(".")) & FORBIDDEN_MODULES, f"{name} imports {alias.name}"
        elif isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, f"{name} reads {node.id}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, f"{name} reads .{node.attr}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in FORBIDDEN_MODULES | FORBIDDEN_NAMES, \
                f"{name} names {node.value!r}"


def test_only_the_stt_entity_sets_the_result():
    setters = []
    for path in COMP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "set_turn_voice(" in text or "VOICE_RESULT.set(" in text:
            setters.append(path.name)
    assert sorted(setters) == ["stt.py", "voice_id.py"]


def test_identity_imports_nothing_from_voice_id():
    tree = ast.parse((COMP / "identity.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            assert not set((node.module or "").split(".")) & FORBIDDEN_MODULES
        if isinstance(node, ast.Name):
            assert node.id not in {"VOICE_RESULT", "take_turn_voice", "set_turn_voice"}
    runtime_imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                       and set((n.module or "").split(".")) & FORBIDDEN_MODULES]
    # the only one is the type, under TYPE_CHECKING
    assert [n.module for n in runtime_imports] == ["voice_id"]
    assert "if TYPE_CHECKING:" in (COMP / "identity.py").read_text(encoding="utf-8")


def test_only_the_turn_entry_reads_the_result():
    readers = sorted(p.name for p in COMP.rglob("*.py")
                     if "take_turn_voice(" in p.read_text(encoding="utf-8"))
    assert readers == ["conversation.py", "voice_id.py"]


@pytest.mark.parametrize("name", ["voice_id.py", "stt.py", "voice_enrol.py", "voice_id_store.py",
                                  "ws_voice_id.py", "identity.py"])
def test_a_voice_never_sets_who_the_request_belongs_to(name):
    """Context.user_id comes only from Home Assistant: no Voice ID or identity
    code builds a Context or assigns a user_id (8.37.0)."""
    tree = ast.parse((COMP / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "Context":
            raise AssertionError(f"{name} builds a Context")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                assert not (isinstance(t, ast.Attribute) and t.attr == "user_id"), \
                    f"{name} assigns .user_id"
