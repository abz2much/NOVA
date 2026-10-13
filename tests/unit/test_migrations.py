"""Acceptance tests for the config-entry schema migrations, written from the
behaviour spec (specs/migrations.md). Every step is checked on its own
(input -> exact data and options), then the full chain, the runner's edge
cases, and the quirks the spec says to keep.

The saved-settings store (nova_config) is replaced with an in-memory dict per
test, so nothing reads or writes a real config file.
"""
import logging
import sys
import types

import pytest

STEP_KEYS_8_41 = ["package_detection", "recognition_source",
                  "departure_alerts_enabled", "search_backend", "auto_bootstrap"]


@pytest.fixture
def store(load, monkeypatch):
    """The saved settings the 7->8 and 8->9 steps consult."""
    cfg = load("nova_config")
    saved = {}
    monkeypatch.setattr(cfg, "get", lambda key, default=None: saved.get(key, default))
    return saved


@pytest.fixture
def mig(load, store):
    return load("migrations")


@pytest.fixture
def legacy_locks(load):
    return list(load("core_common").LEGACY_LOCKDOWN_EXEMPT_LOCKS)


# ── Module surface ──────────────────────────────────────────────────────────

def test_current_version_is_nine(mig):
    assert mig.CURRENT_SCHEMA_VERSION == 9
    assert type(mig.CURRENT_SCHEMA_VERSION) is int


def test_version_is_a_plain_literal_assignment():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "custom_components/nova/migrations.py").read_text(encoding="utf-8")
    found = [n for n in ast.parse(src).body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
             and n.targets[0].id == "CURRENT_SCHEMA_VERSION"]
    assert len(found) == 1 and ast.literal_eval(found[0].value) == 9


def test_step_list_is_ordered_and_complete(mig):
    assert isinstance(mig.MIGRATIONS, list)
    expected = [(1, mig.migrate_1_to_2), (2, mig.migrate_2_to_3),
                (3, mig.migrate_3_to_4), (4, mig.migrate_4_to_5),
                (5, mig.migrate_5_to_6), (6, mig.migrate_6_to_7),
                (7, mig.migrate_7_to_8), (8, mig.migrate_8_to_9)]
    assert mig.MIGRATIONS == expected
    assert max(src for src, _ in mig.MIGRATIONS) + 1 == mig.CURRENT_SCHEMA_VERSION


def test_old_defaults_table(mig):
    table = mig.OFF_UNTIL_SET_UP_8_41
    assert list(table) == STEP_KEYS_8_41
    assert table == {"package_detection": True, "recognition_source": "both",
                     "departure_alerts_enabled": True,
                     "search_backend": "duckduckgo", "auto_bootstrap": True}


def test_legacy_lock_constant_shape(load):
    value = load("core_common").LEGACY_LOCKDOWN_EXEMPT_LOCKS
    assert isinstance(value, tuple) and len(value) == 2
    assert all(isinstance(v, str) and v.startswith("lock.") for v in value)


def test_logger_is_named_after_the_module(mig):
    assert mig._LOGGER.name == mig.__name__


# ── Common step contract ────────────────────────────────────────────────────

@pytest.mark.parametrize("src", range(1, 9))
def test_every_step_returns_the_same_objects(mig, src):
    step = dict(mig.MIGRATIONS)[src]
    data, options = {"some_data": 1}, {}
    out = step(data, options)
    assert isinstance(out, tuple) and len(out) == 2
    assert out[0] is data and out[1] is options


@pytest.mark.parametrize("src", range(1, 9))
def test_no_step_touches_data(mig, src):
    step = dict(mig.MIGRATIONS)[src]
    data = {"schema_version": src, "token": "abc", "nested": {"k": [1]}}
    snapshot = {"schema_version": src, "token": "abc", "nested": {"k": [1]}}
    step(data, {})
    assert data == snapshot


@pytest.mark.parametrize("src", range(1, 9))
def test_every_step_is_a_no_op_when_repeated(mig, src):
    step = dict(mig.MIGRATIONS)[src]
    options = {"cast_speakers": ["media_player.one"]}
    step({}, options)
    first = {k: (list(v) if isinstance(v, list) else v) for k, v in options.items()}
    step({}, options)
    assert options == first


@pytest.mark.parametrize("src", [1, 4, 5, 6, 7])
def test_list_defaults_are_fresh_each_call(mig, src):
    step = dict(mig.MIGRATIONS)[src]
    a, b = {}, {}
    step({}, a)
    step({}, b)
    for key, value in a.items():
        if isinstance(value, list):
            assert value is not b[key]


# ── Step 1 -> 2 ─────────────────────────────────────────────────────────────

def test_step_1_adds_premium_voice_defaults(mig):
    _, options = mig.migrate_1_to_2({}, {})
    assert options == {"tts_premium_engine": "",
                       "tts_premium_contexts": ["briefing", "camera",
                                                "doorbell", "recognition"]}
    assert list(options) == ["tts_premium_engine", "tts_premium_contexts"]


def test_step_1_keeps_present_values_even_falsy(mig):
    _, options = mig.migrate_1_to_2({}, {"tts_premium_engine": None,
                                         "tts_premium_contexts": []})
    assert options == {"tts_premium_engine": None, "tts_premium_contexts": []}


# ── Step 2 -> 3 ─────────────────────────────────────────────────────────────

def test_step_2_adds_directive_defaults(mig):
    _, options = mig.migrate_2_to_3({}, {"other": 1})
    assert options == {"other": 1, "directive_preset": "guardian_steward",
                       "directive": ""}


def test_step_2_keeps_present_values(mig):
    _, options = mig.migrate_2_to_3({}, {"directive_preset": "custom",
                                         "directive": "be kind"})
    assert options == {"directive_preset": "custom", "directive": "be kind"}


# ── Step 3 -> 4 ─────────────────────────────────────────────────────────────

def test_step_3_adds_provider_defaults(mig):
    _, options = mig.migrate_3_to_4({}, {})
    assert options == {"llm_provider": "groq", "llm_base_url": ""}


def test_step_3_keeps_present_values(mig):
    _, options = mig.migrate_3_to_4({}, {"llm_provider": "other",
                                         "llm_base_url": "http://host"})
    assert options == {"llm_provider": "other", "llm_base_url": "http://host"}


def test_step_3_ignores_data_when_deciding(mig):
    data, options = mig.migrate_3_to_4({"llm_provider": "other"}, {})
    assert options["llm_provider"] == "groq"
    assert data == {"llm_provider": "other"}


# ── Step 4 -> 5 ─────────────────────────────────────────────────────────────

def test_step_4_without_legacy_speakers(mig):
    _, options = mig.migrate_4_to_5({}, {})
    assert options == {"voice_satellites": [], "reply_speakers": [],
                       "broadcast_speakers": [], "room_routing": True}
    assert list(options) == ["voice_satellites", "reply_speakers",
                             "broadcast_speakers", "room_routing"]


def test_step_4_promotes_legacy_speakers_as_a_copy(mig):
    legacy = ["media_player.one", "media_player.two"]
    _, options = mig.migrate_4_to_5({}, {"cast_speakers": legacy})
    assert options["broadcast_speakers"] == ["media_player.one", "media_player.two"]
    assert options["broadcast_speakers"] is not legacy
    assert options["cast_speakers"] is legacy          # kept, untouched
    assert options["voice_satellites"] == [] and options["reply_speakers"] == []
    assert options["room_routing"] is True


def test_step_4_tuple_legacy_becomes_list(mig):
    _, options = mig.migrate_4_to_5({}, {"cast_speakers": ("media_player.one",)})
    assert options["broadcast_speakers"] == ["media_player.one"]


@pytest.mark.parametrize("present", [[], None, ["media_player.mine"]])
def test_step_4_never_overwrites_present_broadcast(mig, present):
    _, options = mig.migrate_4_to_5(
        {}, {"cast_speakers": ["media_player.one"], "broadcast_speakers": present})
    assert options["broadcast_speakers"] == present


def test_step_4_empty_legacy_gives_empty_broadcast(mig):
    _, options = mig.migrate_4_to_5({}, {"cast_speakers": []})
    assert options["broadcast_speakers"] == []
    assert options["cast_speakers"] == []


def test_step_4_keeps_present_values(mig):
    start = {"voice_satellites": ["a"], "reply_speakers": ["b"],
             "room_routing": False}
    _, options = mig.migrate_4_to_5({}, dict(start))
    assert {k: options[k] for k in start} == start


def test_step_4_treats_a_string_legacy_value_as_one_speaker(mig):
    # 8.49.10: list() on a string used to give its characters.
    _, options = mig.migrate_4_to_5({}, {"cast_speakers": "media_player.one"})
    assert options["broadcast_speakers"] == ["media_player.one"]
    assert options["cast_speakers"] == "media_player.one"


def test_step_4_none_legacy_value_counts_as_no_speakers(mig):
    # 8.49.10: len(None) used to fail the step and stall the entry at v4.
    options = {"cast_speakers": None}
    _, out = mig.migrate_4_to_5({}, options)
    assert out == {"cast_speakers": None, "voice_satellites": [],
                   "reply_speakers": [], "broadcast_speakers": [],
                   "room_routing": True}


@pytest.mark.parametrize("bad", [0, False])
def test_step_4_raises_on_lengthless_legacy_after_writing(mig, bad):
    options = {"cast_speakers": bad}
    with pytest.raises(TypeError):
        mig.migrate_4_to_5({}, options)
    assert options == {"cast_speakers": bad, "voice_satellites": [],
                       "reply_speakers": [], "broadcast_speakers": [],
                       "room_routing": True}


def test_step_4_truthy_non_iterable_fails_before_broadcast(mig):
    options = {"cast_speakers": 5}
    with pytest.raises(TypeError):
        mig.migrate_4_to_5({}, options)
    assert options == {"cast_speakers": 5, "voice_satellites": [],
                       "reply_speakers": []}


# ── Step 5 -> 6 ─────────────────────────────────────────────────────────────

OBSERVER_DEFAULTS = {
    "observer_enabled": False,
    "gemini_api_key": "",
    "classifier_provider": "gemini",
    "classifier_model": "gemini-2.5-flash-lite",
    "reasoning_provider": "gemini",
    "reasoning_model": "gemini-2.5-flash",
    "review_provider": "gemini",
    "review_model": "gemini-2.5-pro",
    "observer_presence_entities": [],
    "observer_sleep_entities": [],
    "observer_ignore": [],
    "observer_categories": ["appliances", "doors_windows", "presence", "security"],
    "observer_quiet_start": "22:00",
    "observer_quiet_end": "07:00",
    "notify_service": "",
}


def test_step_5_adds_observer_defaults(mig):
    _, options = mig.migrate_5_to_6({}, {})
    assert options == OBSERVER_DEFAULTS
    assert list(options) == list(OBSERVER_DEFAULTS)
    assert options["observer_enabled"] is False


def test_step_5_keeps_present_values(mig):
    mine = {k: "custom" for k in OBSERVER_DEFAULTS}
    _, options = mig.migrate_5_to_6({}, dict(mine))
    assert options == mine


# ── Step 6 -> 7 ─────────────────────────────────────────────────────────────

def test_step_6_no_broadcast_list(mig):
    _, options = mig.migrate_6_to_7({}, {})
    assert options == {"bedroom_areas": []}


@pytest.mark.parametrize("empty", [[], None, "", 0])
def test_step_6_falsy_broadcast_list_promotes_nothing(mig, empty):
    _, options = mig.migrate_6_to_7({}, {"broadcast_speakers": empty})
    assert options == {"broadcast_speakers": empty, "bedroom_areas": []}


def test_step_6_promotes_first_broadcast_speaker(mig):
    start = {"broadcast_speakers": ["media_player.one", "media_player.two"],
             "voice_satellites": ["x"], "reply_speakers": ["y"],
             "cast_speakers": ["z"], "room_routing": True,
             "observer_ignore": ["w"], "observer_categories": ["c"],
             "observer_presence_entities": ["p"], "observer_sleep_entities": ["s"]}
    _, options = mig.migrate_6_to_7({}, dict(start))
    assert options == {**start, "broadcast_group": "media_player.one",
                       "bedroom_areas": []}            # nothing removed


def test_step_6_keeps_truthy_group(mig):
    _, options = mig.migrate_6_to_7({}, {"broadcast_speakers": ["media_player.one"],
                                         "broadcast_group": "media_player.group"})
    assert options["broadcast_group"] == "media_player.group"


@pytest.mark.parametrize("falsy", ["", None])
def test_step_6_overwrites_falsy_group(mig, falsy):
    _, options = mig.migrate_6_to_7({}, {"broadcast_speakers": ["media_player.one"],
                                         "broadcast_group": falsy})
    assert options["broadcast_group"] == "media_player.one"


def test_step_6_keeps_present_bedroom_areas(mig):
    _, options = mig.migrate_6_to_7({}, {"bedroom_areas": ["area_one"]})
    assert options["bedroom_areas"] == ["area_one"]


def test_step_6_first_element_of_string(mig):
    _, options = mig.migrate_6_to_7({}, {"broadcast_speakers": "media_player.one"})
    assert options["broadcast_group"] == "m"


def test_step_6_dict_value_raises_before_writing(mig):
    options = {"broadcast_speakers": {"k": 1}}
    with pytest.raises(KeyError):
        mig.migrate_6_to_7({}, options)
    assert options == {"broadcast_speakers": {"k": 1}}


# ── Step 7 -> 8 ─────────────────────────────────────────────────────────────

def test_step_7_writes_legacy_locks_when_unset(mig, legacy_locks):
    _, options = mig.migrate_7_to_8({}, {})
    assert options == {"lockdown_exempt_locks": legacy_locks}
    assert type(options["lockdown_exempt_locks"]) is list


@pytest.mark.parametrize("value", [[], ["lock.mine"], None, ""])
def test_step_7_present_in_options_is_kept(mig, value):
    _, options = mig.migrate_7_to_8({}, {"lockdown_exempt_locks": value})
    assert options == {"lockdown_exempt_locks": value}


@pytest.mark.parametrize("value", [[], ["lock.mine"], None])
def test_step_7_present_in_data_blocks_and_is_not_copied(mig, value):
    data, options = mig.migrate_7_to_8({"lockdown_exempt_locks": value}, {})
    assert options == {}
    assert data == {"lockdown_exempt_locks": value}


@pytest.mark.parametrize("value", [[], ["lock.mine"], "", False])
def test_step_7_saved_setting_blocks(mig, store, value):
    store["lockdown_exempt_locks"] = value
    _, options = mig.migrate_7_to_8({}, {})
    assert options == {}


def test_step_7_saved_none_counts_as_unset(mig, store, legacy_locks):
    store["lockdown_exempt_locks"] = None
    _, options = mig.migrate_7_to_8({}, {})
    assert options == {"lockdown_exempt_locks": legacy_locks}


def test_step_7_store_failure_counts_as_unset(mig, load, monkeypatch, legacy_locks):
    def broken(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr(load("nova_config"), "get", broken)
    _, options = mig.migrate_7_to_8({}, {})
    assert options == {"lockdown_exempt_locks": legacy_locks}


def test_step_7_reads_store_with_none_default(mig, load, monkeypatch):
    calls = []
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default="unset": calls.append((key, default)))
    mig.migrate_7_to_8({}, {})
    assert calls == [("lockdown_exempt_locks", None)]


def test_step_7_missing_lock_constant_raises(mig, monkeypatch):
    stub = types.ModuleType("jc.core_common")
    monkeypatch.setitem(sys.modules, "jc.core_common", stub)
    with pytest.raises(ImportError):
        mig.migrate_7_to_8({}, {})


# ── Step 8 -> 9 ─────────────────────────────────────────────────────────────

def test_step_8_writes_all_old_defaults_when_unset(mig):
    _, options = mig.migrate_8_to_9({}, {})
    assert options == {"package_detection": True, "recognition_source": "both",
                       "departure_alerts_enabled": True,
                       "search_backend": "duckduckgo", "auto_bootstrap": True}
    assert list(options) == STEP_KEYS_8_41


@pytest.mark.parametrize("key", STEP_KEYS_8_41)
@pytest.mark.parametrize("where", ["options", "data", "store"])
def test_step_8_each_key_decided_alone(mig, store, key, where):
    data, options = {}, {}
    if where == "options":
        options[key] = "mine"
    elif where == "data":
        data[key] = "mine"
    else:
        store[key] = False
    _, out = mig.migrate_8_to_9(data, options)
    table = mig.OFF_UNTIL_SET_UP_8_41
    expected = {k: v for k, v in table.items() if k != key}
    if where == "options":
        expected[key] = "mine"
    assert out == expected
    assert data == ({key: "mine"} if where == "data" else {})


def test_step_8_present_none_in_options_is_kept(mig):
    _, options = mig.migrate_8_to_9({}, {"search_backend": None})
    assert options["search_backend"] is None


def test_step_8_saved_none_counts_as_unset(mig, store):
    store["auto_bootstrap"] = None
    _, options = mig.migrate_8_to_9({}, {})
    assert options["auto_bootstrap"] is True


def test_step_8_store_failure_is_per_key(mig, load, monkeypatch):
    def flaky(key, default=None):
        if key == "recognition_source":
            raise RuntimeError("broken")
        return "saved"
    monkeypatch.setattr(load("nova_config"), "get", flaky)
    _, options = mig.migrate_8_to_9({}, {})
    assert options == {"recognition_source": "both"}


def test_step_8_nothing_to_write_logs_nothing(mig, caplog):
    options = dict(mig.OFF_UNTIL_SET_UP_8_41)
    with caplog.at_level(logging.DEBUG, logger=mig._LOGGER.name):
        mig.migrate_8_to_9({}, options)
    assert [r for r in caplog.records if r.name == mig._LOGGER.name] == []


# ── Runner: normal walks ────────────────────────────────────────────────────

FULL_CHAIN_KEYS = [
    "tts_premium_engine", "tts_premium_contexts", "directive_preset", "directive",
    "llm_provider", "llm_base_url", "voice_satellites", "reply_speakers",
    "broadcast_speakers", "room_routing", *OBSERVER_DEFAULTS, "bedroom_areas",
    "lockdown_exempt_locks", *STEP_KEYS_8_41,
]


def test_full_chain_from_one(mig, legacy_locks):
    data, options, version = mig.migrate_config({}, {}, 1)
    assert version == 9 and type(version) is int
    assert data == {}
    assert list(options) == FULL_CHAIN_KEYS and len(options) == 32
    assert options == {
        "tts_premium_engine": "",
        "tts_premium_contexts": ["briefing", "camera", "doorbell", "recognition"],
        "directive_preset": "guardian_steward", "directive": "",
        "llm_provider": "groq", "llm_base_url": "",
        "voice_satellites": [], "reply_speakers": [], "broadcast_speakers": [],
        "room_routing": True,
        **OBSERVER_DEFAULTS,
        "bedroom_areas": [],
        "lockdown_exempt_locks": legacy_locks,
        "package_detection": True, "recognition_source": "both",
        "departure_alerts_enabled": True, "search_backend": "duckduckgo",
        "auto_bootstrap": True,
    }
    assert "broadcast_group" not in options


def test_default_start_is_version_one(mig):
    _, options, version = mig.migrate_config({}, {})
    assert version == 9 and "tts_premium_engine" in options


def test_runner_returns_the_objects_it_was_given(mig):
    data, options = {"schema_version": 3}, {}
    out_data, out_options, _ = mig.migrate_config(data, options, 3)
    assert out_data is data and out_options is options
    assert data == {"schema_version": 3}          # version key untouched


@pytest.mark.parametrize("start", range(1, 9))
def test_partial_start_runs_only_later_steps(mig, start):
    _, options, version = mig.migrate_config({}, {}, start)
    assert version == 9
    first_key_of = {1: "tts_premium_engine", 2: "directive_preset",
                    3: "llm_provider", 4: "voice_satellites",
                    5: "observer_enabled", 6: "bedroom_areas",
                    7: "lockdown_exempt_locks", 8: "package_detection"}
    for src, key in first_key_of.items():
        assert (key in options) == (src >= start)


def test_legacy_v4_install_walks_speakers_through(mig):
    legacy = ["media_player.one", "media_player.two"]
    _, options, version = mig.migrate_config({}, {"cast_speakers": legacy}, 4)
    assert version == 9
    assert options["cast_speakers"] == ["media_player.one", "media_player.two"]
    assert options["broadcast_speakers"] == ["media_player.one", "media_player.two"]
    assert options["broadcast_speakers"] is not options["cast_speakers"]
    assert options["broadcast_group"] == "media_player.one"


def test_user_values_survive_the_chain(mig):
    mine = {"llm_provider": "other", "directive": "x", "room_routing": False,
            "observer_enabled": True, "package_detection": False,
            "lockdown_exempt_locks": []}
    _, options, _ = mig.migrate_config({}, dict(mine), 1)
    assert {k: options[k] for k in mine} == mine


def test_import_path_starting_at_seven(mig, legacy_locks):
    data = {"schema_version": 7, "llm_provider": "groq"}
    out_data, options, version = mig.migrate_config(data, {}, 7)
    assert version == 9
    assert out_data == {"schema_version": 7, "llm_provider": "groq"}
    assert options == {"lockdown_exempt_locks": legacy_locks,
                       **mig.OFF_UNTIL_SET_UP_8_41}


# ── Runner: at or past current ──────────────────────────────────────────────

@pytest.mark.parametrize("version", [9, 10, 99])
def test_current_or_future_is_returned_untouched(mig, caplog, version):
    data, options = {"a": 1}, {"b": 2}
    with caplog.at_level(logging.DEBUG, logger=mig._LOGGER.name):
        out = mig.migrate_config(data, options, version)
    assert out[0] is data and out[1] is options and out[2] == version
    assert data == {"a": 1} and options == {"b": 2}
    assert [r for r in caplog.records if r.name == mig._LOGGER.name] == []


def test_running_twice_is_idempotent(mig):
    _, options, version = mig.migrate_config({}, {}, 1)
    snapshot = {k: (list(v) if isinstance(v, list) else v) for k, v in options.items()}
    _, again, version2 = mig.migrate_config({}, options, version)
    assert version2 == 9 and again == snapshot


def test_rerunning_from_seven_keeps_a_later_change(mig):
    _, options, _ = mig.migrate_config({}, {}, 7)
    options["lockdown_exempt_locks"] = ["lock.mine"]
    _, again, _ = mig.migrate_config({}, options, 7)
    assert again["lockdown_exempt_locks"] == ["lock.mine"]


# ── Runner: odd versions ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "3"])
def test_none_or_string_version_raises_type_error(mig, bad):
    options = {}
    with pytest.raises(TypeError):
        mig.migrate_config({}, options, bad)
    assert options == {}


@pytest.mark.parametrize("version", [0, -1, 1.5, float("nan")])
def test_invalid_version_is_rejected_with_one_error(mig, caplog, version):
    options = {}
    with caplog.at_level(logging.INFO, logger=mig._LOGGER.name):
        _, out, result = mig.migrate_config({}, options, version)
    assert result is version and out == {}
    records = [r for r in caplog.records if r.name == mig._LOGGER.name]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert "invalid" in records[0].getMessage()


@pytest.mark.parametrize("version,valid", [
    (1, True), (4, True), (9, True), (12, True), (1.0, True), (True, True),
    (0, False), (-1, False), (1.5, False), (False, False)])
def test_schema_version_is_valid(mig, version, valid):
    assert mig.schema_version_is_valid(version) is valid


@pytest.mark.parametrize("version", [True, 1.0])
def test_values_equal_to_one_run_the_full_chain(mig, version):
    _, options, result = mig.migrate_config({}, {}, version)
    assert result == 9 and type(result) is int
    assert "tts_premium_engine" in options


# ── Runner: failures, gaps and replaced step lists ──────────────────────────

def _raiser(exc):
    def step(data, options):
        options["half_done"] = True
        raise exc
    return step


def test_failing_step_stops_at_its_source(mig, monkeypatch, caplog):
    called = []
    monkeypatch.setattr(mig, "MIGRATIONS", [
        (1, mig.migrate_1_to_2),
        (2, _raiser(ValueError("boom"))),
        (3, lambda d, o: called.append(3) or (d, o)),
    ])
    with caplog.at_level(logging.INFO, logger=mig._LOGGER.name):
        data, options, version = mig.migrate_config({}, {}, 1)
    assert version == 2
    assert called == []
    assert options["tts_premium_engine"] == "" and options["half_done"] is True
    errors = [r for r in caplog.records
              if r.name == mig._LOGGER.name and r.levelno == logging.ERROR]
    assert len(errors) == 1
    text = errors[0].getMessage()
    assert "2" in text and "3" in text and "boom" in text


def test_base_exception_propagates(mig, monkeypatch):
    monkeypatch.setattr(mig, "MIGRATIONS", [(1, _raiser(KeyboardInterrupt()))])
    with pytest.raises(KeyboardInterrupt):
        mig.migrate_config({}, {}, 1)


def test_bad_legacy_speakers_stall_at_four(mig):
    options = {"cast_speakers": 0}
    _, out, version = mig.migrate_config({}, options, 1)
    assert version == 4
    assert out["broadcast_speakers"] == [] and out["room_routing"] is True
    assert "observer_enabled" not in out


def test_none_legacy_speakers_reach_nine(mig):
    _, out, version = mig.migrate_config({}, {"cast_speakers": None}, 4)
    assert version == 9 and out["broadcast_speakers"] == []
    assert "broadcast_group" not in out


def test_string_legacy_speaker_becomes_the_broadcast_group(mig):
    _, out, version = mig.migrate_config({}, {"cast_speakers": "media_player.one"}, 4)
    assert version == 9
    assert out["broadcast_speakers"] == ["media_player.one"]
    assert out["broadcast_group"] == "media_player.one"


def test_dict_broadcast_list_stalls_at_six(mig):
    _, out, version = mig.migrate_config({}, {"broadcast_speakers": {"k": 1}}, 6)
    assert version == 6 and out == {"broadcast_speakers": {"k": 1}}


def test_missing_lock_constant_stalls_at_seven(mig, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.core_common", types.ModuleType("jc.core_common"))
    _, out, version = mig.migrate_config({}, {}, 7)
    assert version == 7 and out == {}


def test_gap_in_steps_stops_progress(mig, monkeypatch):
    monkeypatch.setattr(mig, "MIGRATIONS", [(1, mig.migrate_1_to_2),
                                            (3, mig.migrate_3_to_4)])
    _, options, version = mig.migrate_config({}, {}, 1)
    assert version == 2 and "llm_provider" not in options


def test_single_pass_in_list_order(mig, monkeypatch):
    monkeypatch.setattr(mig, "MIGRATIONS", [(2, mig.migrate_2_to_3),
                                            (1, mig.migrate_1_to_2)])
    _, options, version = mig.migrate_config({}, {}, 1)
    assert version == 2
    assert "tts_premium_engine" in options and "directive_preset" not in options


def test_runner_uses_what_a_step_returns(mig, monkeypatch):
    fresh = ({"new": "data"}, {"new": "options"})
    monkeypatch.setattr(mig, "MIGRATIONS", [(8, lambda d, o: fresh)])
    data, options, version = mig.migrate_config({}, {}, 8)
    assert version == 9 and data is fresh[0] and options is fresh[1]
