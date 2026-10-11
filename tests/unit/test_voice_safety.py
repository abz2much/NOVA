"""A voice is a hint, never a key (8.37.0).

No safety or confirmation path reads identity or the Voice ID result, so a
known voice can never unlock, open, disarm, stand down an intrusion, or
answer a confirmation. The phone tap still applies.
"""
import ast
import pathlib
import sys
import types

import pytest

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SAFETY = ["policy.py", "assist_policy.py", "pending_confirm.py", "voice_confirm.py",
          "core_safety.py", "core_lockdown.py", "intrusion.py", "face_roster.py",
          "safety_config.py", "alarm_source.py"]
VOICE = {"identity", "voice_id", "voice_enrol", "voice_id_store", "voice_recognition"}


@pytest.mark.parametrize("name", SAFETY)
def test_safety_and_confirmation_paths_never_read_identity_or_voice(name):
    tree = ast.parse((COMP / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            module = set((node.module or "").split("."))
            # Two pure helpers are allowed: identity.normalize (name
            # formatting, face_roster) and voice_id.valid_uri (an address
            # check for the settings page, safety_config). Nothing that
            # resolves who is speaking or reads a voice result.
            if module & {"identity"} and names <= {"normalize"}:
                continue
            if module & {"voice_id"} and names <= {"valid_uri"}:
                continue
            mods = module | names
            assert not mods & VOICE, f"{name} imports {mods & VOICE}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert not set(a.name.split(".")) & VOICE, f"{name} imports {a.name}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"VOICE_RESULT", "take_turn_voice", "voice_identity_enabled"}


def _fake_vc(confirm_result):
    m = types.ModuleType("jc.voice_confirm")
    m.action_is_protected = lambda hass, domain, service, entity_id="": True

    async def confirm(hass, question, entity_id=""):
        return confirm_result

    async def confirm_typed(hass, question, entity_id=""):
        return "approved" if confirm_result else "rejected"
    m.confirm, m.confirm_typed = confirm, confirm_typed
    return m


@pytest.mark.parametrize("domain,service,entity", [
    ("lock", "unlock", "lock.front"), ("cover", "open_cover", "cover.garage"),
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home")])
async def test_a_known_voice_never_approves_a_protected_action(load, monkeypatch, domain, service, entity):
    policy = load("policy")
    vid = load("voice_id")
    idn = load("identity")
    monkeypatch.setattr(idn, "_cfg", lambda key, default: True if key == "voice_id_identity" else default)
    fake = _fake_vc(confirm_result=False)               # nobody tapped the phone
    monkeypatch.setitem(sys.modules, "jc.voice_confirm", fake)
    monkeypatch.setattr(sys.modules["jc"], "voice_confirm", fake, raising=False)
    vid.set_turn_voice(vid.parse_block({"v": 1, "status": "known", "person": "person.abi",
                                        "score": 0.95, "speech_s": 3.0}))
    try:
        assert policy.requires_confirmation(None, domain, service, entity) is True
        ok, _note, result = await policy.confirm_gate(None, domain, service, entity)
        assert ok is False and result == "rejected"
    finally:
        vid.VOICE_RESULT.set(None)
