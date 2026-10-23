"""The coordinator fills missing event fields from the summary text before it
builds the snapshot and the cache signature. Whatever was given is kept."""
import pytest

from fakes import attach_runtime, make_runtime

SUMMARY = "Cellar Window (binary_sensor.cellar_window) changed from off to on"


@pytest.fixture
def runtime(load, fake_hass):
    rt = make_runtime(load)
    attach_runtime(fake_hass, rt)
    return rt


@pytest.fixture
def seen(load, connectivity, monkeypatch, runtime):
    """The snapshot and cache signature the coordinator built, with the
    breaker open so the local fallback answers."""
    rl = load("reasoning_loop")
    cache = load("reasoning_cache")
    coord = load("cognitive.coordinator")
    connectivity.reset()
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_loaded", True)
    monkeypatch.setattr(rl, "_rich_mode", lambda hass: False)
    box = {"snapshots": [], "signatures": []}

    async def capture(hass, snapshot, honorific):
        box["snapshots"].append(snapshot)
        return {"speak": False, "reason": "captured"}

    real = cache.signature

    def signature(*args):
        box["signatures"].append(args)
        return real(*args)

    monkeypatch.setattr(coord, "_local_mind", capture)
    monkeypatch.setattr(cache, "signature", signature)
    monkeypatch.setattr(connectivity, "allow_request", lambda: False)
    box["rl"] = rl
    yield box
    connectivity.reset()


async def _decide(seen, hass, **over):
    args = dict(honorific="sir", event_summary=SUMMARY, home_state_summary="",
                classifier_urgency="high", classifier_category="security",
                recent_announcements=[], anyone_home=True,
                entity_id="", device_class="window", from_state="", to_state="",
                friendly_name="")
    args.update(over)
    out = await seen["rl"].decide(hass, object(), **args)
    assert out == {"speak": False, "reason": "captured"}
    return seen["snapshots"][-1], seen["signatures"][-1]


async def test_every_missing_field_comes_from_the_summary(seen, fake_hass):
    snap, sig = await _decide(seen, fake_hass)
    assert (snap.entity_id, snap.domain, snap.friendly_name, snap.from_state,
            snap.to_state) == ("binary_sensor.cellar_window", "binary_sensor",
                               "Cellar Window", "off", "on")
    assert sig == ("binary_sensor", "window", "security", "off", "on", True, "high")


async def test_fields_that_were_given_are_kept_over_the_summary(seen, fake_hass):
    snap, sig = await _decide(seen, fake_hass, friendly_name="Given Name",
                              from_state="closed", to_state="open")
    assert (snap.entity_id, snap.friendly_name, snap.from_state, snap.to_state) == (
        "binary_sensor.cellar_window", "Given Name", "closed", "open")
    assert snap.domain == "binary_sensor"
    assert sig[3:5] == ("closed", "open")


async def test_a_full_entity_id_means_nothing_is_taken_from_the_summary(seen, fake_hass):
    snap, sig = await _decide(seen, fake_hass, entity_id="light.porch")
    assert (snap.entity_id, snap.domain) == ("light.porch", "light")
    assert (snap.friendly_name, snap.from_state, snap.to_state) == ("", "", "")
    assert sig[0] == "light" and sig[3:5] == ("", "")


async def test_an_entity_id_with_no_dot_is_kept_but_the_domain_comes_from_the_summary(
        seen, fake_hass):
    snap, sig = await _decide(seen, fake_hass, entity_id="odd")
    assert (snap.entity_id, snap.domain) == ("odd", "binary_sensor")
    assert snap.friendly_name == "Cellar Window"


async def test_the_other_inputs_pass_through_untouched(seen, fake_hass):
    snap, sig = await _decide(seen, fake_hass, recent_announcements=["one"],
                              classifier_category="comfort", classifier_urgency="medium",
                              anyone_home=False)
    assert snap.category == "comfort" and snap.urgency == "medium"
    assert snap.anyone_home is False and snap.recent_announcements == ("one",)
    assert sig[2] == "comfort" and sig[5] is False and sig[6] == "medium"
