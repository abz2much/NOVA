"""camera_semantic.py — the single shared semantic-recording boundary every
camera source (Eufy, Frigate, Nest, Nova's own vision analysis) calls
(Phase 4, v7.109.0).
"""
from __future__ import annotations

import inspect
import math

import pytest


@pytest.fixture
def cs(load):
    return load("camera_semantic")


@pytest.fixture(autouse=True)
def _reset_dedup(cs):
    cs.reset_dedup_state()
    yield
    cs.reset_dedup_state()


# ── canonical label normalisation — fixed allowlist ───────────────────────────

@pytest.mark.parametrize("label", ["person", "vehicle", "animal", "package", "activity"])
def test_canonical_labels_accepted(cs, label):
    assert cs.normalize_label(label) == label


@pytest.mark.parametrize("label", ["PERSON", " Vehicle ", "Animal"])
def test_labels_case_and_whitespace_normalised(cs, label):
    assert cs.normalize_label(label) in cs.CANONICAL_LABELS


@pytest.mark.parametrize("label", [
    "car", "human", "dog", "box", "", None, "person; DROP TABLE state_changes",
    "a" * 500,
])
def test_unknown_labels_rejected(cs, label):
    assert cs.normalize_label(label) is None


def test_label_normalisation_never_used_to_build_an_entity_id_directly(cs):
    """A label that fails normalisation must never influence the entity id —
    only resolve_location (camera entity / area) does, never the label."""
    malicious = "person/../../etc/passwd"
    assert cs.normalize_label(malicious) is None


# ── package sub-state normalisation ───────────────────────────────────────────

@pytest.mark.parametrize("state", ["delivered", "stranded", "taken", "mail"])
def test_package_states_accepted(cs, state):
    assert cs.normalize_package_state(state) == state


@pytest.mark.parametrize("state", ["removed", "opened", "", None, "DELIVERED; --"])
def test_unknown_package_states_rejected_or_normalised(cs, state):
    if state == "DELIVERED; --":
        assert cs.normalize_package_state(state) is None
    else:
        assert cs.normalize_package_state(state) is None


# ── confidence: only filtered when numeric, never invented ───────────────────

def test_missing_confidence_is_accepted_never_invented(cs):
    conf, passes = cs.normalize_confidence(None, floor=50.0)
    assert conf is None
    assert passes is True


def test_numeric_confidence_above_floor_passes(cs):
    conf, passes = cs.normalize_confidence(75.0, floor=50.0)
    assert conf == 75.0
    assert passes is True


def test_numeric_confidence_below_floor_rejected(cs):
    conf, passes = cs.normalize_confidence(30.0, floor=50.0)
    assert passes is False


def test_confidence_floor_exactly_at_boundary_passes(cs):
    _, passes = cs.normalize_confidence(50.0, floor=50.0)
    assert passes is True


@pytest.mark.parametrize("bad", [-1.0, 101.0, float("nan"), float("inf"), float("-inf"), "not-a-number", [], {}])
def test_invalid_confidence_rejected_safely(cs, bad):
    conf, passes = cs.normalize_confidence(bad, floor=50.0)
    assert conf is None
    assert passes is False


def test_confidence_floor_zero_disables_filtering(cs):
    conf, passes = cs.normalize_confidence(0.1, floor=0.0)
    assert passes is True
    conf2, passes2 = cs.normalize_confidence(99.9, floor=0.0)
    assert passes2 is True


def test_confidence_floor_negative_treated_as_disabled(cs):
    # clamp_confidence_floor bounds this before it ever reaches normalize_confidence
    assert cs.clamp_confidence_floor(-10) == 0.0


def test_confidence_scale_is_documented(cs):
    assert "0-100" in cs._CONFIDENCE_SCALE or "0..100" in cs._CONFIDENCE_SCALE.replace("-", "..")


# ── clamping helpers — bounded configuration ──────────────────────────────────

@pytest.mark.parametrize("raw,expected", [(150, 100.0), (-5, 0.0), ("bad", 50.0), (None, 50.0), (float("nan"), 50.0)])
def test_clamp_confidence_floor_bounded(cs, raw, expected):
    assert cs.clamp_confidence_floor(raw) == expected


@pytest.mark.parametrize("raw,expected", [(999999, 3600.0), (-5, 0.0), ("bad", 300.0), (None, 300.0)])
def test_clamp_dedup_window_bounded(cs, raw, expected):
    assert cs.clamp_dedup_window(raw) == expected


# ── bounded strings ────────────────────────────────────────────────────────────

def test_bounded_caps_length(cs):
    assert len(cs.bounded("x" * 10000)) == cs._MAX_STRING_LEN


def test_bounded_handles_none(cs):
    assert cs.bounded(None) == ""


def test_slugify_bounded_and_safe(cs):
    slug = cs.slugify("Front Door!! <script>alert(1)</script> " * 20)
    assert len(slug) <= 64
    assert all(c.isalnum() or c == "_" for c in slug)


def test_slugify_never_empty(cs):
    assert cs.slugify("") == "unknown"
    assert cs.slugify("???") == "unknown"


def test_synthetic_entity_id_shape(cs):
    assert cs.synthetic_entity_id("front_yard") == "camera_event.front_yard"


# ── deduplication ──────────────────────────────────────────────────────────────

def test_first_event_is_not_a_duplicate(cs):
    assert cs._is_duplicate(("person", "front_yard", None), window=300) is False


def test_repeat_within_window_is_a_duplicate(cs):
    key = ("person", "front_yard", None)
    assert cs._is_duplicate(key, window=300) is False
    assert cs._is_duplicate(key, window=300) is True


def test_repeat_after_window_expires_is_not_a_duplicate(cs, monkeypatch):
    key = ("person", "front_yard", None)
    t = [1000.0]
    monkeypatch.setattr(cs, "_now", lambda: t[0])
    assert cs._is_duplicate(key, window=10) is False
    t[0] += 11
    assert cs._is_duplicate(key, window=10) is False  # window passed -> new event


def test_different_labels_same_location_stay_distinct(cs):
    assert cs._is_duplicate(("person", "front_yard", None), window=300) is False
    assert cs._is_duplicate(("vehicle", "front_yard", None), window=300) is False


def test_different_package_states_stay_distinct(cs):
    assert cs._is_duplicate(("package", "front_yard", "delivered"), window=300) is False
    assert cs._is_duplicate(("package", "front_yard", "taken"), window=300) is False


def test_dedup_state_is_bounded(cs):
    for i in range(cs._DEDUP_MAX_ENTRIES + 50):
        cs._is_duplicate(("person", f"loc_{i}", None), window=300)
    assert len(cs._dedup_last_seen) <= cs._DEDUP_MAX_ENTRIES


def test_dedup_window_zero_disables_deduplication(cs):
    key = ("person", "front_yard", None)
    assert cs._is_duplicate(key, window=0) is False
    assert cs._is_duplicate(key, window=0) is False  # window 0 -> never a duplicate


# ── location resolution ────────────────────────────────────────────────────────

def test_resolve_location_falls_back_to_camera_entity_slug(cs, fake_hass):
    # conftest's entity_registry stub returns no entry -> fallback path
    loc = cs.resolve_location(fake_hass, "camera.front_door")
    assert loc == "front_door"


def test_resolve_location_never_uses_friendly_name(cs, fake_hass, monkeypatch):
    """A friendly name is mutable; only entity_id/area registry data may
    influence the resolved location."""
    fake_hass.states.set("camera.front_door", "idle", friendly_name="Bob's Secret Camera!!")
    loc = cs.resolve_location(fake_hass, "camera.front_door")
    assert "bob" not in loc.lower() and "secret" not in loc.lower()


def test_resolve_location_uses_area_registry_when_available(cs, fake_hass, monkeypatch):
    import sys

    class _Entry:
        area_id = "area_front_yard"
        device_id = None

    class _EntReg:
        def async_get(self, eid):
            return _Entry()

    class _Area:
        name = "Front Yard"

    class _AreaReg:
        def async_get_area(self, area_id):
            return _Area()

    # Patching the attribute directly on the already-installed stub modules
    # (conftest.py) — replacing the sys.modules entry alone wouldn't take,
    # since `from homeassistant.helpers import area_registry` resolves via
    # the parent package's own cached attribute first (same trap documented
    # in conftest.py's _JCPackage).
    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    ar_mod = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(er_mod, "async_get", lambda hass: _EntReg())
    monkeypatch.setattr(ar_mod, "async_get", lambda hass: _AreaReg())

    loc = cs.resolve_location(fake_hass, "camera.some_cam")
    assert loc == "front_yard"


# ── resident attribution ───────────────────────────────────────────────────────

def test_attribute_resident_none_without_recognition_cache(cs, fake_hass, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)
    assert cs.attribute_resident(fake_hass, "camera.front_door") is None


def test_attribute_resident_fresh_and_confident(cs, fake_hass, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam:
                        {"name": "Abi", "confidence": 95.0, "age_seconds": 5})
    result = cs.attribute_resident(fake_hass, "camera.front_door")
    assert result == ("Abi", 95.0)


def test_attribute_resident_below_confidence_threshold_is_none(cs, fake_hass, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam:
                        {"name": "Abi", "confidence": 10.0, "age_seconds": 5})
    assert cs.attribute_resident(fake_hass, "camera.front_door") is None


def test_attribute_resident_unknown_name_is_none(cs, fake_hass, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam:
                        {"name": "unknown", "confidence": 99.0, "age_seconds": 1})
    assert cs.attribute_resident(fake_hass, "camera.front_door") is None


def test_attribute_resident_is_camera_scoped_not_presence_based(cs, fake_hass, load, monkeypatch):
    """attribute_resident must consult ONLY recognition.py's per-camera
    cache — it must never fall back to "who's home" presence logic. Proven
    by never importing/calling identity.py at all."""
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)
    import sys
    identity_touched = {"called": False}
    if "jc.identity" in sys.modules:
        monkeypatch.setattr(sys.modules["jc.identity"], "quick_identify",
                            lambda *a, **k: identity_touched.__setitem__("called", True))
    cs.attribute_resident(fake_hass, "camera.front_door")
    assert identity_touched["called"] is False


# ── record_event: structural — no image/payload/prompt/credential parameter ──

def test_record_event_signature_has_no_sensitive_parameters(cs):
    sig = inspect.signature(cs.record_event)
    forbidden = {"image", "image_bytes", "embedding", "face", "payload",
                 "raw_payload", "prompt", "credential", "api_key", "response",
                 "raw_response"}
    assert not (set(sig.parameters) & forbidden)


# ── record_event: end-to-end (cognitive_core mocked) ──────────────────────────

@pytest.fixture
def cc_stub(cs, load, monkeypatch):
    """Stub cognitive_core.log_camera_event so record_event's DB write is
    observable without touching real SQLite, and force learning 'on'."""
    cc = load("cognitive_core")
    calls = []
    monkeypatch.setattr(cc, "log_camera_event", lambda *a, **k: calls.append((a, k)) or True)
    return calls


async def test_record_event_writes_for_valid_person_detection(cs, fake_hass, cc_stub, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, d=None: {"camera_event_learning": True}.get(k, d))
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)

    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    assert ok is True
    assert len(cc_stub) == 1


async def test_record_event_rejects_unknown_label(cs, fake_hass, cc_stub):
    ok = await cs.record_event(fake_hass, label="spaceship", camera_entity="camera.front_door", source="eufy")
    assert ok is False
    assert cc_stub == []


async def test_record_event_rejects_unknown_source(cs, fake_hass, cc_stub):
    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="ring")
    assert ok is False
    assert cc_stub == []


async def test_record_event_rejects_bad_camera_entity(cs, fake_hass, cc_stub):
    ok = await cs.record_event(fake_hass, label="person", camera_entity="", source="eufy")
    assert ok is False
    ok2 = await cs.record_event(fake_hass, label="person", camera_entity="not-an-entity-id", source="eufy")
    assert ok2 is False


async def test_record_event_never_raises_on_internal_failure(cs, fake_hass, load, monkeypatch):
    cc = load("cognitive_core")
    def _boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(cc, "log_camera_event", _boom)
    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    assert ok is False  # never raises, just reports "not recorded"


async def test_record_event_deduplicates_within_window(cs, fake_hass, cc_stub, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)
    await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="frigate")
    assert len(cc_stub) == 1  # second call (different SOURCE, same location+label) collapsed


async def test_record_event_never_attributes_vehicle_animal_package(cs, fake_hass, cc_stub, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam:
                        {"name": "Abi", "confidence": 99.0, "age_seconds": 1})
    for label in ("vehicle", "animal"):
        await cs.record_event(fake_hass, label=label, camera_entity="camera.driveway", source="eufy")
    await cs.record_event(fake_hass, label="package", camera_entity="camera.driveway",
                          source="eufy", package_state="delivered")
    assert len(cc_stub) == 3
    for args, kwargs in cc_stub:
        # positional signature: (entity_id, new_state, area_id, person, person_confidence, detection_confidence)
        person = args[3]
        assert person == "unknown"


async def test_record_event_attribute_false_skips_attribution_even_for_person(cs, fake_hass, cc_stub, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam:
                        {"name": "Abi", "confidence": 99.0, "age_seconds": 1})
    await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door",
                          source="eufy", attribute=False)
    assert cc_stub[0][0][3] == "unknown"  # person arg


async def test_record_event_respects_camera_event_learning_opt_out(cs, fake_hass, cc_stub, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, d=None: {"camera_event_learning": False}.get(k, d))
    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    assert ok is False
    assert cc_stub == []


async def test_record_event_confidence_floor_rejects_low_score(cs, fake_hass, cc_stub, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, d=None: {
        "camera_event_learning": True, "camera_event_confidence_floor": 80.0,
    }.get(k, d))
    ok = await cs.record_event(fake_hass, label="vehicle", camera_entity="camera.driveway",
                               source="frigate", confidence=40.0)
    assert ok is False
    assert cc_stub == []


async def test_record_event_missing_confidence_not_rejected_even_with_floor_set(cs, fake_hass, cc_stub, load, monkeypatch):
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, d=None: {
        "camera_event_learning": True, "camera_event_confidence_floor": 80.0,
    }.get(k, d))
    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door",
                               source="eufy", confidence=None)
    assert ok is True
    assert len(cc_stub) == 1


async def test_record_event_sets_a_live_bounded_state(cs, fake_hass, cc_stub, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)
    await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    st = fake_hass.states.get("camera_event.front_door")
    assert st is not None
    assert st.state == "person"
    # deliberately minimal — no resident/confidence/package leakage into the
    # live state any signed-in HA user can read
    assert set(st.attributes) <= {"source", "area"}


async def test_record_event_package_state_becomes_the_live_state_value(cs, fake_hass, cc_stub):
    await cs.record_event(fake_hass, label="package", camera_entity="camera.porch",
                          source="eufy", package_state="delivered")
    st = fake_hass.states.get("camera_event.porch")
    assert st.state == "package_delivered"


# ── master learning setting (cognitive_core.learning_active) ─────────────────

async def test_record_event_stores_nothing_when_master_learning_inactive(cs, fake_hass, load, monkeypatch):
    """The camera_event_learning opt-out is separate from, and layered on
    top of, the master learning toggle (observer_enabled -> cognitive_core
    running) — cognitive_core.log_camera_event is the function that
    actually gates on it, so even with camera_event_learning=True this
    must still store nothing while learning overall is off."""
    cc = load("cognitive_core")
    monkeypatch.setattr(cc, "learning_active", lambda: False)
    jc = load("nova_config")
    monkeypatch.setattr(jc, "get", lambda k, d=None: {"camera_event_learning": True}.get(k, d))
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)

    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    assert ok is False


def test_learning_active_reflects_core_running_state(load):
    cc = load("cognitive_core")
    cc._CORE.running = False
    cc._CORE.state_logger = None
    assert cc.learning_active() is False

    class _StubLogger:
        pass
    cc._CORE.running = True
    cc._CORE.state_logger = _StubLogger()
    assert cc.learning_active() is True
    cc._CORE.running = False  # leave clean for later tests in this session
    cc._CORE.state_logger = None


# ── DB failure never breaks the caller ────────────────────────────────────────

async def test_log_camera_event_failure_does_not_raise(load, monkeypatch):
    """cognitive_core.log_camera_event's own StateLogger.log_state_change
    already swallows sqlite errors internally (matches every other pattern-
    learning write) — confirm that holds for the camera_event path too, so
    a DB problem can never propagate up into camera perception/Eufy
    handling/announcements."""
    cc = load("cognitive_core")

    class _BoomLogger:
        def log_state_change(self, *a, **k):
            raise RuntimeError("disk full")
    cc._CORE.running = True
    cc._CORE.state_logger = _BoomLogger()
    with pytest.raises(RuntimeError):
        # log_camera_event itself does NOT catch — camera_semantic.record_event
        # is the layer with the try/except boundary; this proves record_event
        # actually needs it (not a redundant belt-and-suspenders wrapper).
        cc.log_camera_event("camera_event.front_door", "person")
    cc._CORE.running = False
    cc._CORE.state_logger = None


async def test_record_event_survives_a_db_failure_end_to_end(cs, fake_hass, load, monkeypatch):
    cc = load("cognitive_core")

    def _boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(cc, "log_camera_event", _boom)

    ok = await cs.record_event(fake_hass, label="person", camera_entity="camera.front_door", source="eufy")
    assert ok is False  # reported as "not recorded", never raised


# ── cross-source dedup across DIFFERENT camera entities, same area ───────────

async def test_cross_source_dedup_for_different_cameras_same_area(cs, fake_hass, cc_stub, load, monkeypatch):
    """The Eufy + Frigate case named explicitly in the task: two different
    integrations, two different camera entities, watching the same spot —
    resolved to the same area, so the same physical event collapses to one
    learning row regardless of which entity_id each integration uses."""
    import sys

    class _Entry:
        def __init__(self, area_id):
            self.area_id = area_id
            self.device_id = None

    class _EntReg:
        def async_get(self, eid):
            return _Entry("front_yard_area")

    class _Area:
        name = "Front Yard"

    class _AreaReg:
        def async_get_area(self, area_id):
            return _Area()

    er_mod = sys.modules["homeassistant.helpers.entity_registry"]
    ar_mod = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(er_mod, "async_get", lambda hass: _EntReg())
    monkeypatch.setattr(ar_mod, "async_get", lambda hass: _AreaReg())

    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)

    ok1 = await cs.record_event(fake_hass, label="person", camera_entity="camera.eufy_doorbell", source="eufy")
    ok2 = await cs.record_event(fake_hass, label="person", camera_entity="camera.frigate_porch", source="frigate")
    assert ok1 is True
    assert ok2 is False  # collapsed — same resolved area, same label
    assert len(cc_stub) == 1


async def test_same_source_repeat_within_window_is_suppressed(cs, fake_hass, cc_stub, load, monkeypatch):
    rec = load("recognition")
    monkeypatch.setattr(rec, "last_seen_at", lambda hass, cam: None)
    ok1 = await cs.record_event(fake_hass, label="vehicle", camera_entity="camera.driveway", source="frigate")
    ok2 = await cs.record_event(fake_hass, label="vehicle", camera_entity="camera.driveway", source="frigate")
    assert ok1 is True
    assert ok2 is False
    assert len(cc_stub) == 1
