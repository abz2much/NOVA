"""alert_path.py: one decision per alert source (8.22.0).

Every source is checked with the residents home, away and unknown, and
whether it goes to the speakers, the phones, both or neither. The speaker
routing used is the real audio_routing.observer_speak_target. All state here
is fake. Nothing touches a real speaker, phone, alarm or lock.
"""
import pytest

SPEAKER = "media_player.hall"
RESIDENTS = {"home": "home", "away": "not_home", "unknown": "unknown"}


@pytest.fixture
def ap(load):
    return load("alert_path")


@pytest.fixture
def house(fake_hass):
    """Set the household: home, away or unknown. A speaker exists."""
    fake_hass.states.set(SPEAKER, "idle")

    def _set(residents):
        fake_hass.states.set("person.abi", RESIDENTS[residents])
        return fake_hass
    return _set


def _sit(ap, hass, sleeping=False, quiet=False):
    return ap.situation(hass, {}, sleeping=sleeping, quiet=quiet)


def _out(plan):
    """What a plan does, in words: speakers, phones, both or nothing."""
    if not plan.alert:
        return "nothing"
    return {(True, True): "both", (True, False): "speakers",
            (False, True): "phones", (False, False): "nothing"}[(plan.speak, plan.push)]


def test_the_situation_carries_the_household_reading(ap, house):
    for residents in RESIDENTS:
        hass = house(residents)
        assert _sit(ap, hass).residents == residents


def test_an_unreadable_household_is_unknown_and_never_raises(ap):
    class _Broken:
        @property
        def states(self):
            raise RuntimeError("state machine down")
    sit = ap.situation(_Broken(), {})
    assert sit.residents == "unknown" and sit.anyone_home is False


@pytest.mark.parametrize("residents,expected", [
    ("home", False), ("away", True), ("unknown", False)])
def test_the_wording_rule_says_no_one_is_home_only_when_known(ap, house, residents, expected):
    assert ap.nobody_home(_sit(ap, house(residents))) is expected


# ── observer: presence from household.py decides speakers or phones ─────────

@pytest.mark.parametrize("urgency,residents,expected", [
    ("high", "home", "both"), ("high", "away", "phones"), ("high", "unknown", "phones"),
    ("medium", "home", "speakers"), ("medium", "away", "phones"), ("medium", "unknown", "phones"),
    ("critical", "home", "both"), ("critical", "away", "both"), ("critical", "unknown", "both"),
])
def test_observer(ap, house, urgency, residents, expected):
    hass = house(residents)
    plan = ap.for_observer(hass, _sit(ap, hass), urgency, announcement_speakers=[SPEAKER])
    assert _out(plan) == expected
    if plan.speak:
        assert plan.targets == [SPEAKER]


def test_observer_a_linked_phone_reading_home_counts(ap, fake_hass):
    fake_hass.states.set(SPEAKER, "idle")
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "home")
    plan = ap.for_observer(fake_hass, _sit(ap, fake_hass), "high", announcement_speakers=[SPEAKER])
    assert _out(plan) == "both"


def test_observer_motion_never_makes_the_house_occupied(ap, house):
    hass = house("away")
    hass.states.set("binary_sensor.hall_presence", "on", device_class="presence")
    plan = ap.for_observer(hass, _sit(ap, hass), "medium", announcement_speakers=[SPEAKER])
    assert _out(plan) == "phones"


@pytest.mark.parametrize("residents", RESIDENTS)
def test_observer_asleep_or_quiet_holds_all_but_critical(ap, house, residents):
    hass = house(residents)
    assert _out(ap.for_observer(hass, _sit(ap, hass, sleeping=True), "medium",
                                announcement_speakers=[SPEAKER])) == "nothing"
    quiet = ap.for_observer(hass, _sit(ap, hass, quiet=True), "high",
                            announcement_speakers=[SPEAKER])
    assert _out(quiet) == "phones"
    crit = ap.for_observer(hass, _sit(ap, hass, sleeping=True), "critical",
                           announcement_speakers=[SPEAKER])
    assert _out(crit) == "both"


@pytest.mark.parametrize("urgency,residents,expected", [
    ("high", "home", "phones"), ("high", "away", "phones"), ("high", "unknown", "phones"),
    ("critical", "home", "phones"), ("critical", "away", "phones"),
    ("critical", "unknown", "phones"),
    # Away routes medium to the phones before the announcements switch is read.
    ("medium", "home", "nothing"), ("medium", "away", "phones"), ("medium", "unknown", "phones"),
])
def test_observer_announcements_off(ap, house, urgency, residents, expected):
    hass = house(residents)
    plan = ap.for_observer(hass, _sit(ap, hass), urgency,
                           announcement_speakers=[SPEAKER], announcements_on=False)
    assert _out(plan) == expected


def test_observer_no_speaker_means_no_alert_and_no_record(ap, house):
    hass = house("home")
    plan = ap.for_observer(hass, _sit(ap, hass), "low", announcement_speakers=[SPEAKER])
    assert _out(plan) == "nothing"


# ── appliance: unchanged, presence still read from live motion and people ──

@pytest.mark.parametrize("residents,expected", [
    ("home", "speakers"), ("away", "phones"), ("unknown", "phones")])
def test_appliance(ap, house, residents, expected):
    hass = house(residents)
    plan = ap.for_appliance(hass, _sit(ap, hass), announcement_speakers=[SPEAKER])
    assert _out(plan) == expected


def test_appliance_still_counts_live_motion_as_someone_home(ap, house):
    # Unchanged from before 8.22.0 (listed in the report for a decision).
    hass = house("away")
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    plan = ap.for_appliance(hass, _sit(ap, hass), announcement_speakers=[SPEAKER])
    assert _out(plan) == "speakers"


@pytest.mark.parametrize("residents", RESIDENTS)
def test_appliance_asleep_or_announcements_off(ap, house, residents):
    hass = house(residents)
    assert _out(ap.for_appliance(hass, _sit(ap, hass, sleeping=True),
                                 announcement_speakers=[SPEAKER])) == "nothing"
    assert _out(ap.for_appliance(hass, _sit(ap, hass), announcement_speakers=[SPEAKER],
                                 announcements_on=False)) == "nothing"


# ── sentinel ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("residents", RESIDENTS)
def test_sentinel(ap, house, residents):
    sit = _sit(ap, house(residents))
    assert _out(ap.for_sentinel(sit, notify_only_setting=False)) == "both"
    notify_only = ap.for_sentinel(sit, notify_only_setting=True)
    assert _out(notify_only) == "phones" and notify_only.speak_if_unsent
    asleep = ap.for_sentinel(_sit(ap, house(residents), sleeping=True), notify_only_setting=False)
    assert _out(asleep) == "phones" and not asleep.speak_if_unsent


# ── hazards ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("residents", RESIDENTS)
def test_hazard(ap, house, residents):
    sit = _sit(ap, house(residents))
    spoken = ap.for_hazard(sit, speak_allowed=True)
    assert _out(spoken) == "both" and spoken.push_first
    assert _out(ap.for_hazard(sit, speak_allowed=False)) == "phones"


# ── packages ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("residents,expected", [
    ("home", "nothing"), ("away", "both"), ("unknown", "nothing")])
def test_package_removed(ap, house, residents, expected):
    plan = ap.for_package(_sit(ap, house(residents)), "removed", announcements_on=True)
    assert _out(plan) == expected


def test_package_removed_while_away_in_quiet_hours_goes_to_phones(ap, house):
    plan = ap.for_package(_sit(ap, house("away"), quiet=True), "removed", announcements_on=True)
    assert _out(plan) == "phones"


@pytest.mark.parametrize("kind", ["delivered", "mail", "stranded"])
@pytest.mark.parametrize("residents", RESIDENTS)
def test_package_arrivals(ap, house, kind, residents):
    hass = house(residents)
    assert _out(ap.for_package(_sit(ap, hass), kind, announcements_on=True)) == "speakers"
    assert _out(ap.for_package(_sit(ap, hass, quiet=True), kind, announcements_on=True)) == "nothing"
    assert _out(ap.for_package(_sit(ap, hass), kind, announcements_on=False)) == "nothing"


# ── doorbell ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("residents", RESIDENTS)
def test_doorbell(ap, house, residents):
    assert _out(ap.for_doorbell(_sit(ap, house(residents)))) == "speakers"


# ── core actions: intrusion, lockdown, freeze, offers, anticipation ─────────

CORE_CONFIG = {"broadcast_group": SPEAKER}


def _core(ap, hass, urgency, sit=None, **action):
    action.setdefault("type", "test")
    return ap.for_core_action(hass, CORE_CONFIG, sit or _sit(ap, hass),
                              dict(action, urgency=urgency))


@pytest.mark.parametrize("urgency,residents,expected", [
    ("high", "home", "both"), ("high", "away", "phones"), ("high", "unknown", "phones"),
    ("medium", "home", "speakers"), ("medium", "away", "nothing"),
    ("medium", "unknown", "nothing"),
    ("critical", "home", "both"), ("critical", "away", "both"), ("critical", "unknown", "both"),
])
def test_core(ap, house, urgency, residents, expected):
    plan = _core(ap, house(residents), urgency)
    assert _out(plan) == expected
    if plan.speak:
        assert plan.targets == [SPEAKER]


def test_core_still_counts_live_motion_as_someone_home(ap, house):
    """Unchanged from before 8.22.0: an intruder's movement while armed away
    makes the speakers route as home, so the first intrusion alert is still
    spoken. Listed in the report for a decision."""
    hass = house("away")
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert _out(_core(ap, hass, "high", type="intrusion_investigating")) == "both"


@pytest.mark.parametrize("residents", RESIDENTS)
def test_core_asleep_quiet_or_phone_only_goes_to_phones(ap, house, residents):
    hass = house(residents)
    assert _out(_core(ap, hass, "high", _sit(ap, hass, sleeping=True))) == "phones"
    assert _out(_core(ap, hass, "medium", _sit(ap, hass, quiet=True))) == "phones"
    assert _out(_core(ap, hass, "high", phone_only=True)) == "phones"


@pytest.mark.parametrize("residents", RESIDENTS)
def test_core_critical_ignores_sleep_quiet_and_phone_only(ap, house, residents):
    hass = house(residents)
    plan = _core(ap, hass, "critical", _sit(ap, hass, sleeping=True, quiet=True),
                 phone_only=True)
    assert _out(plan) == "both" and plan.targets == [SPEAKER]


# ── deliver ─────────────────────────────────────────────────────────────────

async def test_deliver_runs_the_steps_in_the_plan_order(ap):
    calls = []

    async def speak(targets):
        calls.append(("speak", targets))

    async def push():
        calls.append(("push",))
        return ["notify.phone"]

    await ap.deliver(ap.Plan(speak=True, targets=["a"], push=True), speak=speak, push=push)
    await ap.deliver(ap.Plan(speak=True, targets=["b"], push=True, push_first=True),
                     speak=speak, push=push)
    assert calls == [("speak", ["a"]), ("push",), ("push",), ("speak", ["b"])]


async def test_deliver_does_nothing_for_no_alert(ap):
    async def boom(*a):
        raise AssertionError("must not run")
    out = await ap.deliver(ap.Plan(alert=False, speak=True, push=True), speak=boom, push=boom)
    assert out == {"spoke": False, "pushed": None}


async def test_deliver_speaks_after_all_when_no_phone_took_it(ap):
    calls = []

    async def speak(targets):
        calls.append("speak")

    async def push_none():
        return []
    plan = ap.Plan(push=True, speak_if_unsent=True)
    out = await ap.deliver(plan, speak=speak, push=push_none)
    assert calls == ["speak"] and out["spoke"]

    async def push_one():
        return ["notify.phone"]
    calls.clear()
    await ap.deliver(plan, speak=speak, push=push_one)
    assert calls == []


# ── cognition.py's "nobody home" now comes from household.py ────────────────

@pytest.fixture
def cog(load):
    return load("cognition")


def test_cognition_a_person_in_another_zone_counts_as_away(cog, fake_hass):
    # Changed in 8.22.0: before, only "not_home" and "away" counted.
    fake_hass.states.set("person.abi", "Work")
    assert cog._all_tracked_residents_away(fake_hass) is True


def test_cognition_a_linked_phone_reading_home_is_not_away(cog, fake_hass):
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "home")
    assert cog._all_tracked_residents_away(fake_hass) is False


def test_cognition_loose_trackers_never_prove_the_house_empty(cog, fake_hass):
    fake_hass.states.set("device_tracker.delivery_van", "not_home")
    assert cog._all_tracked_residents_away(fake_hass) is False


@pytest.mark.parametrize("state,armed", [
    ("armed_home", True), ("armed_custom_bypass", True), ("armed_away", True),
    ("disarmed", False), ("arming", False), ("pending", False), ("triggered", False)])
def test_cognition_armed_uses_the_household_table(cog, load, fake_hass, monkeypatch,
                                                  state, armed):
    fake_hass.states.set("alarm_control_panel.home", state)
    monkeypatch.setattr(load("runtime"), "domain_runtime_config",
                        lambda hass: {"security_alarm_entity": "alarm_control_panel.home"})
    assert cog._conflicting_armed_alarm(fake_hass) is armed
