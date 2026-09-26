"""Presence direction and last-person-departure urgency in _try_local_reasoning.

Direction (arrival vs. departure) is read from the transition's own exact
from_state/to_state — never from parsing the event_summary prose. A text
scan for "arrived"/"came home" wording, or a substring check for
"not_home", used to misfire: "not_home" appears in an ARRIVAL's summary too
(as the FROM state of "changed from not_home to home"), which collapsed
every presence transition into "has left the premises" regardless of
actual direction (the 2026-09-18 08:52 incident).

When the last person home leaves, Nova used to announce it at 'low' urgency
unconditionally. Low-urgency routing (audio_routing.py) decides whether to
speak based on live room occupancy sensors, which can still read 'on' for a
moment after someone physically walks out (motion clear-delay) — so it
could speak "X has left the premises" into a house that, by the message's
own content, is now empty. 'medium' urgency already has the correct away
rule (push a notification, don't speak); the departure branch uses it
whenever nobody is left home.
"""


def test_not_home_to_home_is_arrival(reasoning_loop):
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from not_home to home",
        urgency="medium",
        category="presence",
        honorific="sir",
        recent_announcements=[],
        anyone_home=True,
        from_state="not_home",
        to_state="home",
    )
    assert decision["speak"] is True
    assert decision["urgency"] == "medium"
    assert decision["message"] == "Welcome home, sir."
    assert "left" not in decision["message"].lower()


def test_home_to_not_home_is_departure(reasoning_loop):
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from home to not_home",
        urgency="low",
        category="presence",
        honorific="ma'am",
        recent_announcements=[],
        anyone_home=True,
        from_state="home",
        to_state="not_home",
    )
    assert decision["speak"] is True
    assert decision["urgency"] == "low"
    assert "Alex has left the premises" in decision["message"]


def test_departure_stays_silent_when_house_now_empty(reasoning_loop):
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from home to not_home",
        urgency="low",
        category="presence",
        honorific="",
        recent_announcements=[],
        anyone_home=False,
        from_state="home",
        to_state="not_home",
    )
    assert decision == {"speak": False, "reason": "last person departure"}


def test_non_home_zone_transition_is_neither_arrival_nor_departure(reasoning_loop):
    """not_home -> a named zone (e.g. school) is neither direction — only a
    transition touching the literal "home" state counts."""
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from not_home to Casey School",
        urgency="low",
        category="presence",
        honorific="sir",
        recent_announcements=[],
        anyone_home=True,
        from_state="not_home",
        to_state="Casey School",
    )
    # Falls through the presence-arrival/departure branch entirely; low
    # urgency with no other matching template stays silent.
    assert decision == {"speak": False, "reason": "low urgency — logged but not announced"}


def test_zone_to_not_home_is_neither_arrival_nor_departure(reasoning_loop):
    """Leaving a named zone (e.g. school) for not_home is neither direction —
    this is in-transit, not a house departure."""
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from Casey School to not_home",
        urgency="low",
        category="presence",
        honorific="sir",
        recent_announcements=[],
        anyone_home=True,
        from_state="Casey School",
        to_state="not_home",
    )
    assert decision == {"speak": False, "reason": "low urgency — logged but not announced"}


def test_arbitrary_event_summary_wording_cannot_reverse_direction(reasoning_loop):
    """Even if the prose summary contains "not_home" or "left"-adjacent text
    for what is actually an arrival (to_state == "home"), the exact-state
    comparison — not the text — decides direction. This is the regression
    test for the 2026-09-18 08:52 incident: an arrival whose summary reads
    "changed from not_home to home" was previously misread as a departure
    because the substring "not_home" is present in that very sentence."""
    decision = reasoning_loop._try_local_reasoning(
        event_summary=(
            "Alex (person.alex) left not_home and changed from not_home to home"
        ),
        urgency="medium",
        category="presence",
        honorific="sir",
        recent_announcements=[],
        anyone_home=True,
        from_state="not_home",
        to_state="home",
    )
    assert decision["speak"] is True
    assert decision["message"] == "Welcome home, sir."
    assert "left the premises" not in decision["message"]


def test_no_from_to_state_supplied_does_not_guess_direction(reasoning_loop):
    """A caller that doesn't supply from_state/to_state gets no presence
    arrival/departure template match — it must not fall back to guessing
    from prose. It falls through toward the cloud/cache path (None)."""
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Alex (person.alex) changed from home to not_home",
        urgency="medium",
        category="presence",
        honorific="sir",
        recent_announcements=[],
        anyone_home=False,
    )
    assert decision is None


# ── decide(): the exact-state check must not be bypassed by Rich Reasoning ──
# decide() short-circuits to _try_local_reasoning() only `if not rich`, where
# `rich = _rich_mode(hass) and classifier_urgency in ("medium", "high")`.
# Arrivals/departures classify as "medium" urgency, so with Rich Reasoning
# enabled the entire local-reasoning call — including the exact-state
# direction fix above — was skipped, sending the transition to the cloud/
# cache path instead and reopening the direction-inversion bug for anyone
# running with that setting on. A presence transition touching the literal
# "home" state must always resolve locally and deterministically, regardless
# of Rich Reasoning; every other medium/high event must keep going through
# Rich Reasoning exactly as before.

def _decide_kwargs(**over):
    kwargs = dict(
        honorific="sir",
        event_summary="Alex (person.alex) changed from not_home to home",
        home_state_summary="",
        classifier_urgency="medium",
        classifier_category="presence",
        recent_announcements=[],
        anyone_home=True,
        entity_id="person.alex",
        device_class="",
        from_state="not_home",
        to_state="home",
        friendly_name="Alex",
    )
    kwargs.update(over)
    return kwargs


async def test_rich_reasoning_enabled_arrival_is_still_deterministic(
        reasoning_loop, fake_hass, provider_factory, monkeypatch):
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: True)
    provider = provider_factory()  # would answer if called — it must not be
    out = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Alex (person.alex) changed from not_home to home",
            from_state="not_home", to_state="home", anyone_home=True,
        ),
    )
    assert out["speak"] is True
    assert out["urgency"] == "medium"
    assert out["message"] == "Welcome home, sir."
    assert provider.calls == 0


async def test_rich_reasoning_enabled_departure_is_still_deterministic(
        reasoning_loop, fake_hass, provider_factory, monkeypatch):
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: True)
    provider = provider_factory()  # would answer if called — it must not be
    out = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Alex (person.alex) changed from home to not_home",
            from_state="home", to_state="not_home", anyone_home=False,
        ),
    )
    assert out == {"speak": False, "reason": "last person departure"}
    assert provider.calls == 0


async def test_rich_reasoning_unrelated_medium_event_still_uses_cloud(
        reasoning_loop, fake_hass, provider_factory, connectivity, load, monkeypatch):
    """A medium-urgency event that ISN'T a home-boundary presence transition
    must still go cloud-first when Rich Reasoning is on — the carve-out is
    scoped to arrivals/departures only, not a blanket disable."""
    connectivity.reset()
    cache = load("reasoning_cache")
    monkeypatch.setattr(cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(cache, "remember", lambda *a, **k: None)
    monkeypatch.setattr(cache, "note_hit", lambda *a, **k: None)
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: True)

    provider = provider_factory(
        replies=['{"speak": true, "message": "Sir, the cellar window opened.", "urgency": "medium"}'])
    out = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Cellar Window (binary_sensor.cellar_window) changed from off to on",
            classifier_category="doors_windows",
            entity_id="binary_sensor.cellar_window",
            device_class="window",
            from_state="off", to_state="on",
            anyone_home=False,
        ),
    )
    assert provider.calls == 1
    assert out["speak"] is True
    assert "cellar window" in out["message"].lower()
    connectivity.reset()


async def test_rich_reasoning_disabled_uses_same_presence_rules(
        reasoning_loop, fake_hass, provider_factory, monkeypatch):
    """With Rich Reasoning off (the default), arrivals/departures already
    went through _try_local_reasoning before this fix — confirming that
    path is untouched by the rich-mode carve-out above."""
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: False)
    provider = provider_factory()

    arrival = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Alex (person.alex) changed from not_home to home",
            from_state="not_home", to_state="home", anyone_home=True,
        ),
    )
    assert arrival["speak"] is True
    assert arrival["message"] == "Welcome home, sir."

    departure = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Alex (person.alex) changed from home to not_home",
            from_state="home", to_state="not_home", anyone_home=False,
        ),
    )
    assert departure == {"speak": False, "reason": "last person departure"}
    assert provider.calls == 0


async def test_rich_reasoning_enabled_non_home_zone_transition_unaffected(
        reasoning_loop, fake_hass, provider_factory, connectivity, load, monkeypatch):
    """A zone-to-zone move (never touching literal "home") is not a
    home-boundary transition, so Rich Reasoning applies to it normally —
    the carve-out must not swallow non-home presence events either."""
    connectivity.reset()
    cache = load("reasoning_cache")
    monkeypatch.setattr(cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(cache, "remember", lambda *a, **k: None)
    monkeypatch.setattr(cache, "note_hit", lambda *a, **k: None)
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: True)

    provider = provider_factory(replies=['{"speak": false, "reason": "routine"}'])
    out = await reasoning_loop.decide(
        fake_hass, provider,
        **_decide_kwargs(
            event_summary="Alex (person.alex) changed from not_home to Casey School",
            from_state="not_home", to_state="Casey School", anyone_home=True,
        ),
    )
    # Reached the cloud path (not the deterministic arrival/departure
    # template) — proves the carve-out didn't fire for this transition.
    assert provider.calls == 1
    assert out["speak"] is False
    connectivity.reset()
