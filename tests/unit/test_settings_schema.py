"""The settings schema (8.42.0): every panel setting's kind, default and
check, defined once in settings_schema.py and used by the allow list, the
write check and the panel's config payload."""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from ws_sources import panel_config_keys, settings_schema, ws_function

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
ss = settings_schema()


def test_every_setting_has_a_known_kind_and_payload():
    for key, s in ss.SETTINGS.items():
        assert s.key == key
        assert s.kind in ss.KINDS, key
        assert s.payload in ("plain", "custom", "hidden"), key
        assert not s.exact_true or s.kind == ss.BOOL, key


def test_no_setting_is_listed_twice():
    keys = [s.key for s in ss._ALL]
    assert len(keys) == len(set(keys))


def test_defaults_match_their_kind():
    for key, s in ss.SETTINGS.items():
        if s.default is None:
            continue
        if s.kind == ss.BOOL:
            assert type(s.default) is bool, key
        elif s.kind == ss.JSON:
            assert isinstance(s.default, (list, dict)), key
        else:
            assert ss.valid_kind(key, s.default), key


def test_custom_payload_settings_are_built_in_websocket():
    custom = {k for k, s in ss.SETTINGS.items() if s.payload == "custom"}
    fn = ws_function("ws_get_panel_data")
    written = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "config"
                        and isinstance(v, ast.Dict)):
                    written |= {e.value for e in v.keys if isinstance(e, ast.Constant)}
    assert custom <= written, sorted(custom - written)
    plain = {k for k, s in ss.SETTINGS.items() if s.payload == "plain"}
    # A plain setting is built from the schema only, never written out twice.
    assert not plain & written, sorted(plain & written)


def test_every_shown_setting_reaches_the_panel():
    shown = {k for k, s in ss.SETTINGS.items() if s.payload != "hidden"}
    assert shown <= panel_config_keys()


@pytest.mark.parametrize("key,value,ok", [
    ("announcements_enabled", True, True),
    ("announcements_enabled", "true", False),
    ("announcements_enabled", 1, False),
    ("garage_bays", "3", True),
    ("garage_bays", 3, True),
    ("garage_bays", "", True),
    ("garage_bays", None, True),
    ("garage_bays", "three", False),
    ("garage_bays", True, False),
    ("garage_bays", "nan", False),
    ("floor_plan_rooms", '{"1f": {}}', True),
    ("floor_plan_rooms", "", True),
    ("floor_plan_rooms", "not json", False),
    ("floor_plan_rooms", '"a string"', False),
    ("floor_plan_rooms", 5, False),
    ("lab_areas", '["office"]', True),
    ("general_speaker", "media_player.kitchen", True),
    ("general_speaker", 5, False),
    ("satellite_audio_out", True, True),
    ("not_a_setting", True, False),
])
def test_valid_kind(key, value, ok):
    assert ss.valid_kind(key, value) is ok


def test_payload_builds_each_plain_setting_from_its_kind():
    saved = {"pattern_learn_doors": 1, "scene_memory_enabled": 1,
             "banter_level": 2, "searxng_url": "https://u:p@x"}

    def read(key, default):
        return saved.get(key, default)

    def read_json(key, default):
        return default

    out = ss.payload(read, read_json, lambda url: f"masked:{url}")
    assert out["pattern_learn_doors"] is True
    # exact_true: only a saved JSON true shows as on.
    assert out["scene_memory_enabled"] is False
    assert out["banter_level"] == 2
    assert out["searxng_url"] == "masked:https://u:p@x"
    assert out["recognition_source"] == "off"
    assert out["model"] == ""
    assert out["lab_areas"] == []
    assert "announcements_enabled" not in out    # custom
    assert "satellite_audio_out" not in out      # hidden


# ── Code defaults ───────────────────────────────────────────────────────────
#
# A read such as config.get("key", default) anywhere in Nova must use the
# schema's default, or the panel shows one thing while Nova does another.
# None and "" in code mean "is anything saved?" and are not defaults.

def _code_defaults():
    for path in sorted(COMP.rglob("*.py")):
        if path.name == "settings_schema.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            # Writes, and the executor calls that wrap them, take a value.
            if func.endswith(("set", "set_many", "setdefault", "pop",
                              "async_add_executor_job")):
                continue
            args = node.args
            for i, arg in enumerate(args[:-1]):
                if not (isinstance(arg, ast.Constant) and arg.value in ss.SETTINGS):
                    continue
                try:
                    value = ast.literal_eval(args[i + 1])
                except ValueError:
                    continue
                yield f"{path.name}:{node.lineno}", arg.value, value


def _same(code, schema) -> bool:
    if isinstance(code, str) and isinstance(schema, (list, dict)):
        try:
            return json.loads(code) == schema
        except ValueError:
            return False
    if isinstance(code, bool) or isinstance(schema, bool):
        return code is schema
    if isinstance(code, (int, float)) and isinstance(schema, (int, float)):
        return float(code) == float(schema)
    return code == schema


# Reads that mean something other than the setting's default, and why.
_NOT_A_DEFAULT = {
    # Only an explicit "Observer off" hides camera history: the cognitive
    # core records camera events on every install, Observer or not.
    ("camera_awareness.py", "observer_enabled"),
}


def test_code_defaults_match_the_schema():
    wrong = []
    for where, key, value in _code_defaults():
        default = ss.SETTINGS[key].default
        if default is None or value is None or value == "":
            continue
        if (where.split(":")[0], key) in _NOT_A_DEFAULT:
            continue
        if not _same(value, default):
            wrong.append(f"{where} {key}: code {value!r}, schema {default!r}")
    assert not wrong, "\n".join(wrong)


def test_the_check_finds_a_wrong_default():
    # The scan above must actually see reads like these.
    found = {(key, repr(value)) for _w, key, value in _code_defaults()}
    assert ("package_detection", "False") in found
    assert ("cognition_enabled", "True") in found
