"""Service names and descriptions live in translations/en.json, so Home
Assistant can show them in the user's language; services.yaml keeps only
each service's fields."""

import json
from pathlib import Path

import yaml

COMP = Path(__file__).resolve().parents[2] / "custom_components" / "nova"
SERVICES = yaml.safe_load((COMP / "services.yaml").read_text(encoding="utf-8"))
TEXT = json.loads((COMP / "translations" / "en.json").read_text(encoding="utf-8"))["services"]


def test_services_yaml_holds_no_text():
    for name, spec in SERVICES.items():
        assert set(spec or {}) <= {"fields"}, name
        for field, f in ((spec or {}).get("fields") or {}).items():
            assert not {"name", "description"} & set(f), (name, field)


def test_every_service_and_field_has_english_text():
    assert list(TEXT) == list(SERVICES)
    for name, spec in SERVICES.items():
        entry = TEXT[name]
        assert entry["name"].strip() and entry["description"].strip(), name
        fields = list(((spec or {}).get("fields") or {}))
        assert list(entry.get("fields", {})) == fields, name
        for field in fields:
            assert set(entry["fields"][field]) == {"name", "description"}, (name, field)
            assert all(v.strip() for v in entry["fields"][field].values()), (name, field)


def test_service_text_is_uk_english_and_never_says_he():
    words = json.dumps(TEXT).lower()
    for bad in ("analyze ", "analyzes", "summarize", " he ", "recognize", "color", "favorite"):
        assert bad not in words, bad


def test_retiring_services_still_say_so():
    for name in ("speak", "process_intent"):
        assert TEXT[name]["description"].startswith("Being retired"), name
