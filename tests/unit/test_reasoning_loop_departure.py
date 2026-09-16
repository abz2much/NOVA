"""When the last person home leaves, Nova used to announce it at 'low'
urgency unconditionally. Low-urgency routing (audio_routing.py) decides
whether to speak based on live room occupancy sensors, which can still read
'on' for a moment after someone physically walks out (motion clear-delay) —
so it could speak "X has left the premises" into a house that, by the
message's own content, is now empty. 'medium' urgency already has the
correct away rule (push a notification, don't speak); the departure branch
now uses it whenever nobody is left home.
"""


def test_departure_speaks_low_urgency_when_someone_still_home(reasoning_loop):
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Abi (person.abi) changed from home to not_home",
        urgency="low",
        category="presence",
        honorific="ma'am",
        recent_announcements=[],
        anyone_home=True,
    )
    assert decision["speak"] is True
    assert decision["urgency"] == "low"
    assert "Abi has left the premises" in decision["message"]


def test_departure_escalates_to_medium_when_house_now_empty(reasoning_loop):
    decision = reasoning_loop._try_local_reasoning(
        event_summary="Abi (person.abi) changed from home to not_home",
        urgency="low",
        category="presence",
        honorific="",
        recent_announcements=[],
        anyone_home=False,
    )
    assert decision["speak"] is True
    assert decision["urgency"] == "medium"
    assert "Abi has left the premises" in decision["message"]
