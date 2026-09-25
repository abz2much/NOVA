"""Critical announcements are never silenced by a shush.

output_gate.can_announce() checks critical urgency before any mute: a
blanket nova.shush (all=True) blocks every non-critical announcement, but a
critical safety announcement always passes through the existing
"critical bypass" path. Entity/category mutes and unshush keep behaving as
before.

Focused run:
    python -m pytest tests/unit/test_output_gate_shush.py -q
"""
import pytest


@pytest.fixture
def og(load, monkeypatch):
    mod = load("output_gate")
    monkeypatch.setattr(mod, "_STATE", mod.GateState())
    return mod


def _ask(og, urgency="low", entity_id="sensor.washer_power",
         category="appliances", message="The washer has finished."):
    return og.can_announce(entity_id=entity_id, category=category,
                           urgency=urgency, message=message)


def test_blanket_shush_blocks_an_ordinary_announcement(og):
    assert og.shush(all=True)["all"] is True
    assert _ask(og) == (False, "blanket shush active")


@pytest.mark.parametrize("urgency", ["low", "medium", "high"])
def test_blanket_shush_blocks_every_non_critical_urgency(og, urgency):
    og.shush(all=True)
    assert _ask(og, urgency=urgency) == (False, "blanket shush active")


def test_blanket_shush_does_not_block_a_critical_announcement(og):
    og.shush(all=True)
    ok, reason = _ask(og, urgency="critical", entity_id="binary_sensor.smoke",
                      category="security", message="Smoke detected in the kitchen.")
    assert ok is True
    assert reason == "critical bypass"


def test_critical_passes_every_mute_and_the_rate_limit_together(og):
    og.shush(all=True)
    og.shush(entity_id="binary_sensor.smoke")
    og.shush(category="security")
    for i in range(og.DEFAULT_MAX_PER_HOUR + 5):
        og.record_announcement(entity_id=f"sensor.n{i}", category="appliances",
                               urgency="low", message=f"note {i}", was_spoken=True)
    assert _ask(og, urgency="critical", entity_id="binary_sensor.smoke",
                category="security", message="note 1") == (True, "critical bypass")


def test_unshush_clears_the_blanket_mute(og):
    og.shush(all=True)
    result = og.unshush()
    assert result["blanket_cleared"] is True
    assert _ask(og) == (True, "ok")


def test_entity_mute_is_unchanged(og):
    og.shush(entity_id="sensor.washer_power")
    assert _ask(og) == (False, "entity sensor.washer_power is muted")
    assert _ask(og, entity_id="sensor.dryer_power") == (True, "ok")
    og.unshush(entity_id="sensor.washer_power")
    assert _ask(og) == (True, "ok")


def test_category_mute_is_unchanged(og):
    og.shush(category="appliances")
    assert _ask(og) == (False, "category appliances is muted")
    assert _ask(og, category="energy", entity_id="sensor.grid") == (True, "ok")
    og.unshush(category="appliances")
    assert _ask(og) == (True, "ok")


def test_targeted_unshush_leaves_the_blanket_mute_in_place(og):
    og.shush(all=True)
    og.shush(entity_id="sensor.washer_power")
    og.unshush(entity_id="sensor.washer_power")
    assert _ask(og, entity_id="sensor.dryer_power") == (False, "blanket shush active")
