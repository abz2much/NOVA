"""Exit doors the user picks on the Residence tab (8.26.0).

The user picks any lock, cover or binary sensor and names it. Nova suggests
candidates but adds nothing until the user picks. Picking a door is display
only: the row says which safety checks already cover it, using the same rules
those checks use, and never adds it to one. All state is fake.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401

ALARM = "alarm_control_panel.home_security"


@pytest.fixture
def hd(load):
    return load("home_doors")


# ── the saved list ──────────────────────────────────────────────────────────

def test_the_saved_list_is_cleaned(hd):
    cfg = {"exit_doors": [
        {"entity_id": "lock.back", "name": "  Garden   door "},
        {"entity_id": "lock.back", "name": "again"},             # repeat
        {"entity_id": "light.hall", "name": "not a door"},       # wrong domain
        "junk", {"name": "no entity"},
        {"entity_id": "cover.side", "name": "x" * 60},
        {"entity_id": "binary_sensor.shed"},
    ]}
    assert hd.exit_doors(cfg) == [
        {"entity_id": "lock.back", "name": "Garden door"},
        {"entity_id": "cover.side", "name": "x" * 40},
        {"entity_id": "binary_sensor.shed", "name": ""},
    ]


def test_the_saved_list_reads_json_and_survives_bad_json(hd):
    assert hd.exit_doors({"exit_doors": '[{"entity_id": "lock.back", "name": "B"}]'}) == [
        {"entity_id": "lock.back", "name": "B"}]
    assert hd.exit_doors({"exit_doors": "{not json"}) == []
    assert hd.exit_doors({}) == []


def test_the_list_is_capped(hd):
    cfg = {"exit_doors": [{"entity_id": f"lock.l{i}"} for i in range(20)]}
    assert len(hd.exit_doors(cfg)) == hd.MAX_EXIT_DOORS


# ── nothing is added until the user picks ──────────────────────────────────

def test_suggestions_are_only_suggestions(hd, fake_hass):
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door", friendly_name="Back door")
    fake_hass.states.set("lock.front", "locked", friendly_name="Front")
    fake_hass.states.set("cover.blinds", "open", device_class="blind")
    fake_hass.states.set("binary_sensor.hall_motion", "off", device_class="motion")
    cfg = {}
    sugg = hd.exit_door_candidates(fake_hass, cfg)
    assert [s["entity_id"] for s in sugg] == ["binary_sensor.back_door", "lock.front"]
    assert hd.exit_doors(cfg) == [] and cfg == {}           # nothing was added
    assert hd.exit_door_status(fake_hass, cfg) == []


def test_picked_and_mapped_doors_are_not_suggested_again(hd, fake_hass):
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door")
    fake_hass.states.set("lock.front", "locked")
    cfg = {"exit_doors": [{"entity_id": "lock.front"}],
           "door_mapping": {"garage_rear": "binary_sensor.back_door"}}
    assert hd.exit_door_candidates(fake_hass, cfg) == []


# ── a picked door shows the checks that already cover it ───────────────────

def _status(hd, fake_hass, eid, exempt=()):
    (row,) = hd.exit_door_status(fake_hass, {"exit_doors": [{"entity_id": eid, "name": "Mine"}]}, exempt)
    return row


def test_a_lock_is_covered_by_lockdown_and_the_sweep(hd, fake_hass):
    fake_hass.states.set("lock.back", "unlocked")
    row = _status(hd, fake_hass, "lock.back")
    assert row == {"entity_id": "lock.back", "name": "Mine", "state": "unlocked",
                   "checks": ["lockdown", "night_sweep"]}
    assert _status(hd, fake_hass, "lock.back", exempt={"lock.back"})["checks"] == []


def test_a_door_sensor_is_covered_by_intrusion_and_the_world_model(hd, fake_hass):
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door")
    assert _status(hd, fake_hass, "binary_sensor.back_door")["checks"] == [
        "lockdown", "intrusion", "world_model"]


def test_a_door_cover_is_covered_by_every_check(hd, fake_hass):
    fake_hass.states.set("cover.side_door", "closed", device_class="door")
    assert _status(hd, fake_hass, "cover.side_door")["checks"] == [
        "lockdown", "night_sweep", "intrusion", "world_model"]


def test_an_outdoor_door_cover_is_not_a_way_in(hd, fake_hass):
    # Same rule as intrusion: a patio counts as outside the house.
    fake_hass.states.set("cover.patio", "closed", device_class="door")
    assert _status(hd, fake_hass, "cover.patio")["checks"] == ["lockdown", "night_sweep"]


def test_a_contact_sensor_with_no_class_is_covered_by_nothing(hd, fake_hass):
    fake_hass.states.set("binary_sensor.side_contact", "off")
    assert _status(hd, fake_hass, "binary_sensor.side_contact")["checks"] == []


def test_a_fridge_door_is_not_a_way_in(hd, fake_hass):
    fake_hass.states.set("binary_sensor.fridge_door", "off", device_class="door",
                         friendly_name="Fridge door")
    assert _status(hd, fake_hass, "binary_sensor.fridge_door")["checks"] == []


def test_a_door_that_is_gone_is_shown_as_missing(hd, fake_hass):
    assert _status(hd, fake_hass, "lock.gone") == {
        "entity_id": "lock.gone", "name": "Mine", "state": None, "checks": []}


def test_an_unnamed_door_uses_its_friendly_name(hd, fake_hass):
    fake_hass.states.set("lock.back", "locked", friendly_name="Back lock")
    (row,) = hd.exit_door_status(fake_hass, {"exit_doors": [{"entity_id": "lock.back"}]})
    assert row["name"] == "Back lock"


# ── picking a door never changes a safety check ────────────────────────────

def test_picking_a_contact_sensor_does_not_make_it_a_way_in(cc, fake_hass):
    fake_hass.states.set("binary_sensor.side_contact", "on")          # open, no device class
    plain = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    picked = cc.SafetyManager(fake_hass, {"honorific": "sir", "exit_doors": [
        {"entity_id": "binary_sensor.side_contact", "name": "Side door"}]})
    assert plain._open_entry() is None and picked._open_entry() is None


async def test_picking_a_door_does_not_change_lockdown(cc, fake_hass):
    fake_hass.states.set("binary_sensor.side_contact", "on")
    fake_hass.states.set("cover.blinds", "open", device_class="blind")
    cfg = {"exit_doors": [{"entity_id": "binary_sensor.side_contact"}, {"entity_id": "cover.blinds"}]}
    action = await cc.LockdownManager(fake_hass, cfg).engage("alarm armed")
    fake_hass.close_pending()
    assert [c for c in fake_hass.service_calls if c[0] == "cover"] == []
    assert "fully secured" in action["message"]


def test_picking_a_door_does_not_change_the_world_model(load, fake_hass):
    world = load("world")
    fake_hass.states.set("binary_sensor.side_contact", "on")
    cfg = {"exit_doors": [{"entity_id": "binary_sensor.side_contact"}]}
    assert world.read(fake_hass, cfg).open_ways_in == world.read(fake_hass, {}).open_ways_in == ()
