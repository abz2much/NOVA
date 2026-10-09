"""Every {placeholder} in the setup dialog text must appear exactly the same
in every translation, or a language shows broken text. translations/en.json is
the English source: Home Assistant does not use strings.json for a custom
integration (removed in 8.15.0)."""
import json
import re
from pathlib import Path

COMP = Path(__file__).resolve().parents[2] / "custom_components" / "nova"
EN = COMP / "translations" / "en.json"
_PH = re.compile(r"\{([a-z_]+)\}")


def _walk(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (k,))
    elif isinstance(node, str):
        yield path, set(_PH.findall(node))


def test_placeholders_match_in_every_section_and_language():
    # Every section: config, options, selector, issues and exceptions.
    base = dict(_walk(json.loads(EN.read_text())))
    for f in sorted((COMP / "translations").glob("*.json")):
        other = dict(_walk(json.loads(f.read_text())))
        for path, names in base.items():
            assert other.get(path) == names, f"{f.name} {'.'.join(path)}"


def test_the_english_source_is_translations_en_json():
    assert EN.is_file()
    assert not (COMP / "strings.json").exists()


def test_screens_have_their_placeholders():
    cfg = json.loads(EN.read_text())["config"]["step"]
    assert "{saved}" in cfg["user"]["description"]
    assert cfg["user"]["data_description"]["ollama_base_url"] == "For example {example}"
    assert "{failed}" in cfg["roles"]["description"]
    for role in ("conversation", "classifier", "reasoning", "camera_reasoning", "vision"):
        assert f"{{{role}_provider}}" in cfg["models"]["data"][f"{role}_model"]


def test_every_error_key_has_text():
    errors = json.loads(EN.read_text())["config"]["error"]
    for key in ("need_llm", "no_working_provider", "invalid_auth", "cannot_connect", "unknown",
                "model_required", "model_no_pictures", "model_not_found", "model_access_denied",
                "test_incomplete", "test_failed", "secrets_write_failed",
                "config_write_failed"):
        assert errors.get(key), key


def test_no_url_written_into_translation_text():
    # Home Assistant's hassfest check rejects URLs in translations; the
    # example address is passed as the {example} placeholder instead.
    for f in sorted((COMP / "translations").glob("*.json")):
        assert "http://" not in f.read_text() and "https://" not in f.read_text(), f.name


def test_already_in_progress_has_text():
    abort = json.loads(EN.read_text())["config"]["abort"]
    assert abort.get("already_in_progress")
