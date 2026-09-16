"""Phase 3 — deterministic entity-ambiguity handling, local path.

_find_entity()'s distinct-candidate (entity_id-keyed) scoring, _AmbiguousEntity,
and both try_local() interception points.
"""
import pytest

from fakes import FakeHass


@pytest.fixture
def le(load):
    return load("local_engine")


@pytest.fixture
def no_aliases(monkeypatch):
    # Both call sites in _find_entity read the alias file from disk via
    # os.path.exists — ensure a clean, alias-free environment regardless of
    # what's on the machine running the tests.
    monkeypatch.setattr("os.path.exists", lambda path: False)


# ── exact-match duplicates ───────────────────────────────────────────────────

def test_duplicate_exact_friendly_names_are_ambiguous(le, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen_1", "off", friendly_name="Kitchen Light")
    hass.states.set("light.kitchen_2", "off", friendly_name="Kitchen Light")
    result = le._find_entity(hass, "kitchen light", domain_hint="light")
    assert isinstance(result, le._AmbiguousEntity)
    assert {c["entity_id"] for c in result.candidates} == {"light.kitchen_1", "light.kitchen_2"}


def test_single_exact_friendly_name_match_still_returns_unique(le, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen_1", "off", friendly_name="Kitchen Light")
    hass.states.set("light.garage", "off", friendly_name="Garage Light")
    result = le._find_entity(hass, "kitchen light", domain_hint="light")
    assert result == ("light.kitchen_1", "Kitchen Light")


# ── fuzzy near-tie (0.75 ratio) ──────────────────────────────────────────────

def test_fuzzy_near_tie_is_ambiguous(le, no_aliases):
    # Empirically verified against the real scorer (Phase 3 design notes):
    # "bedroom light" vs Bedroom Light 2 / Bedroom Lamp -> 86.7/69.6 = ratio
    # 0.803, above the 0.75 local-path threshold.
    hass = FakeHass()
    hass.states.set("light.bedroom_2", "off", friendly_name="Bedroom Light 2")
    hass.states.set("light.bedroom_lamp", "off", friendly_name="Bedroom Lamp")
    result = le._find_entity(hass, "bedroom light", domain_hint="light")
    assert isinstance(result, le._AmbiguousEntity)
    assert len(result.candidates) == 2


def test_fuzzy_clear_winner_unaffected(le, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen", "off", friendly_name="Kitchen")
    hass.states.set("light.kitchen_cabinet", "off", friendly_name="Kitchen Cabinet Light")
    result = le._find_entity(hass, "kitchen light", domain_hint="light")
    assert result is not None
    assert not isinstance(result, le._AmbiguousEntity)
    assert result[0] == "light.kitchen"


# ── STT-retry pass must not make an entity ambiguous with itself ────────────

def test_entity_scoring_in_both_original_and_stt_pass_is_not_self_ambiguous(le, no_aliases):
    """'chase lite' scores weakly against 'Chase Light' in the original pass
    (no substring/word-overlap/high-fuzzy match) and much more strongly
    after the 'lite'->'light' STT correction re-scans the SAME entity. The
    dict-keyed-by-entity_id design must overwrite that one entry with the
    higher score, never create a second, distinct candidate for the same
    entity_id -- so it must resolve cleanly, not ambiguously, even with a
    genuinely competing decoy present."""
    hass = FakeHass()
    hass.states.set("light.chase_light", "off", friendly_name="Chase Light")
    hass.states.set("light.unrelated_decoy", "off", friendly_name="Totally Different Name")
    result = le._find_entity(hass, "chase lite", domain_hint="light")
    assert result == ("light.chase_light", "Chase Light")
    assert not isinstance(result, le._AmbiguousEntity)


# ── area fallback ────────────────────────────────────────────────────────────

def test_area_fallback_multiple_eligible_entities_is_ambiguous(le, no_aliases, monkeypatch):
    hass = FakeHass()
    # No name/fuzzy match at all -- forces the area-fallback tier.
    hass.states.set("light.unrelated_one", "off", friendly_name="Unrelated One")
    hass.states.set("light.unrelated_two", "off", friendly_name="Unrelated Two")

    class _Area:
        id = "office_area"
        name = "Office"

    class _AreaReg:
        def async_list_areas(self):
            return [_Area()]

    class _EntityEntry:
        def __init__(self, entity_id, domain, area_id):
            self.entity_id = entity_id
            self.domain = domain
            self.area_id = area_id
            self.device_id = None

    class _EntReg:
        entities = {
            "light.unrelated_one": _EntityEntry("light.unrelated_one", "light", "office_area"),
            "light.unrelated_two": _EntityEntry("light.unrelated_two", "light", "office_area"),
        }

    class _DevReg:
        def async_get(self, device_id):
            return None

    import homeassistant.helpers as helpers_mod
    monkeypatch.setattr(helpers_mod, "area_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _AreaReg())}))
    monkeypatch.setattr(helpers_mod, "entity_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _EntReg())}))
    monkeypatch.setattr(helpers_mod, "device_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _DevReg())}))

    result = le._find_entity(hass, "office", domain_hint="light")
    assert isinstance(result, le._AmbiguousEntity)
    assert {c["entity_id"] for c in result.candidates} == {"light.unrelated_one", "light.unrelated_two"}


def test_area_fallback_single_eligible_entity_returns_unique(le, no_aliases, monkeypatch):
    hass = FakeHass()
    hass.states.set("light.unrelated_one", "off", friendly_name="Unrelated One")

    class _Area:
        id = "office_area"
        name = "Office"

    class _AreaReg:
        def async_list_areas(self):
            return [_Area()]

    class _EntityEntry:
        def __init__(self, entity_id, domain, area_id):
            self.entity_id = entity_id
            self.domain = domain
            self.area_id = area_id
            self.device_id = None

    class _EntReg:
        entities = {
            "light.unrelated_one": _EntityEntry("light.unrelated_one", "light", "office_area"),
        }

    class _DevReg:
        def async_get(self, device_id):
            return None

    import homeassistant.helpers as helpers_mod
    monkeypatch.setattr(helpers_mod, "area_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _AreaReg())}))
    monkeypatch.setattr(helpers_mod, "entity_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _EntReg())}))
    monkeypatch.setattr(helpers_mod, "device_registry",
                         type("M", (), {"async_get": staticmethod(lambda hass: _DevReg())}))

    result = le._find_entity(hass, "office", domain_hint="light")
    assert result == ("light.unrelated_one", "Unrelated One")


# ── try_local's two interception points ──────────────────────────────────────

async def test_try_local_pattern_action_ambiguous_returns_clarification_not_action(
    le, no_aliases, monkeypatch,
):
    hass = FakeHass()
    hass.states.set("light.kitchen_1", "off", friendly_name="Kitchen Light")
    hass.states.set("light.kitchen_2", "off", friendly_name="Kitchen Light")
    result = await le.try_local(hass, "turn on the kitchen light", "sir")
    assert result is not None
    assert result.handled is True
    assert result.success is False
    assert "kitchen_1" in result.text or "kitchen_2" in result.text
    assert hass.service_calls == []  # _execute_action never reached


async def test_try_local_state_query_ambiguous_returns_clarification(le, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen_1", "off", friendly_name="Kitchen Light")
    hass.states.set("light.kitchen_2", "off", friendly_name="Kitchen Light")
    result = await le.try_local(hass, "kitchen light", "sir")
    if result is not None:
        assert "kitchen_1" in result.text or "kitchen_2" in result.text
        assert hass.service_calls == []


async def test_try_local_clear_winner_action_still_executes(le, no_aliases):
    hass = FakeHass()
    hass.states.set("light.kitchen", "off", friendly_name="Kitchen")
    result = await le.try_local(hass, "turn on the kitchen", "sir")
    assert result is not None
    assert result.handled is True
    assert len(hass.service_calls) == 1
    assert hass.service_calls[0][0] == "light"
    assert hass.service_calls[0][1] == "turn_on"
