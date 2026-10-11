"""Nova Voice ID 8.36.0 never reaches the safety path, identity or faces.

The voice result is only logged and used for recording. These modules must
not import the Voice ID modules or read the result, so a voice can never
unlock, disarm, stand down or change who Nova thinks is speaking.
"""
import ast
import pathlib

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
GUARDED = ["policy.py", "core_safety.py", "core_lockdown.py", "face_roster.py",
           "identity.py", "voice_recognition.py"]
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


def test_only_the_turn_entry_reads_the_result():
    readers = sorted(p.name for p in COMP.rglob("*.py")
                     if "take_turn_voice(" in p.read_text(encoding="utf-8"))
    assert readers == ["conversation.py", "voice_id.py"]
