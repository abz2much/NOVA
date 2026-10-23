"""The agent tools for learned aliases, pending facts and relations, ignore
rules and the document library: what each reply says, and what each one asks
the rest of Nova to do.

Everything the tools call is replaced by a recorder, so these pin the exact
arguments passed on and the exact text and JSON the model gets back. The
last section runs the ignore tools against the real cognitive core and its
IgnoreManager, with files in tmp_path: a pattern that matches everything is
refused and every ignore expires.
"""
import json
import logging
import sys
import types

import pytest

from fakes import FakeHass
from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401  (fixtures)

SAFE = "RuntimeError (details are in the Home Assistant log)"


@pytest.fixture
def mem(load):
    return load("agent_runtime.capabilities.memory")


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


@pytest.fixture
def logged(monkeypatch):
    lines = []
    _stub(monkeypatch, "websocket", nova_log=lambda *a, **k: lines.append((a, k)))
    return lines


# ── the learned file ─────────────────────────────────────────────────────────

def test_a_missing_file_gives_the_empty_categories(mem, tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "learned.json"))
    assert mem._load_learned() == {"alias": {}, "preference": {}, "routine": {}}


def test_saved_data_is_read_back(mem, tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "learned.json"))
    mem._save_learned({"alias": {"tv": "media_player.tv"}})
    assert json.loads((tmp_path / "learned.json").read_text()) == {
        "alias": {"tv": "media_player.tv"}}
    assert (tmp_path / "learned.json").read_text().startswith('{\n  "alias"')
    assert mem._load_learned() == {"alias": {"tv": "media_player.tv"}}


def test_a_damaged_file_starts_empty_and_says_so(mem, tmp_path, monkeypatch, caplog):
    path = tmp_path / "learned.json"
    path.write_text("{not json")
    monkeypatch.setattr(mem, "_LEARN_FILE", str(path))
    with caplog.at_level(logging.WARNING, logger=mem._LOGGER.name):
        assert mem._load_learned() == {"alias": {}, "preference": {}, "routine": {}}
    assert [r.getMessage().split(":")[0] for r in caplog.records] == [
        "Learned data unreadable, starting empty"]


def test_a_failed_save_is_only_a_warning(mem, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "missing" / "deep" / "x.json"))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mem, "write_json_atomic", boom)
    with caplog.at_level(logging.WARNING, logger=mem._LOGGER.name):
        mem._save_learned({"alias": {}})
    assert [r.getMessage() for r in caplog.records] == [
        "Failed to save learned data: disk full"]


def test_the_learned_file_defaults_to_the_standard_path(mem, load, monkeypatch):
    monkeypatch.setattr(mem, "_LEARN_FILE", None)
    assert mem._learn_file() == load("paths").learned_file()
    monkeypatch.setattr(mem, "_LEARN_FILE", "/tmp/elsewhere.json")
    assert mem._learn_file() == "/tmp/elsewhere.json"


# ── remember ─────────────────────────────────────────────────────────────────

@pytest.fixture
def know(monkeypatch):
    calls = []
    box = types.SimpleNamespace(calls=calls, fact={"id": 12}, error=None)

    def remember(name, value, **kw):
        calls.append((name, value, kw))
        if box.error:
            raise box.error
        return box.fact

    _stub(monkeypatch, "knowledge", remember=remember, DEFAULT_SUBJECT="household")
    _stub(monkeypatch, "identity",
          subject_for=lambda ident: f"voice:{ident.who}",
          resolve_subject=lambda hass: "presence:alex")
    return box


async def test_an_unknown_category_is_refused(mem, know):
    for key, shown in (("note", "note"), ("", ""), (None, "None")):
        out = await mem._exec_remember(FakeHass(), {"key": key, "name": "x", "value": "y"})
        assert json.loads(out) == {"error": f"Unknown category: {shown}"}
    assert know.calls == []


async def test_an_alias_is_written_straight_away(mem, tmp_path, monkeypatch, know, caplog):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "learned.json"))
    with caplog.at_level(logging.INFO, logger=mem._LOGGER.name):
        out = await mem._exec_remember(
            FakeHass(), {"key": "alias", "name": "  The TV ", "value": "media_player.tv"})
    assert json.loads(out) == {"success": True,
                               "learned": "alias: 'the tv' → 'media_player.tv'"}
    assert list(json.loads(out)) == ["success", "learned"]
    assert mem._load_learned()["alias"] == {"the tv": "media_player.tv"}
    assert [r.getMessage() for r in caplog.records] == ["Nova learned an alias"]
    assert know.calls == []


async def test_a_second_alias_keeps_the_first(mem, tmp_path, monkeypatch, know):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "learned.json"))
    for name, value in (("tv", "media_player.tv"), ("lamp", "light.lamp")):
        await mem._exec_remember(FakeHass(), {"key": "alias", "name": name, "value": value})
    assert mem._load_learned()["alias"] == {"tv": "media_player.tv", "lamp": "light.lamp"}


async def test_an_alias_with_no_name_or_value_is_stored_as_blank(mem, tmp_path,
                                                                   monkeypatch, know):
    monkeypatch.setattr(mem, "_LEARN_FILE", str(tmp_path / "learned.json"))
    out = await mem._exec_remember(FakeHass(), {"key": "alias"})
    assert json.loads(out)["learned"] == "alias: '' → ''"
    assert mem._load_learned()["alias"] == {"": ""}


PENDING_MESSAGE = (
    "Saved 'likes tea: with milk' as PENDING, not yet trusted — ask the user to "
    "confirm this is actually correct before relying on it again. If they "
    "confirm, call confirm_pending_fact with fact_id=12. If they say no or "
    "correct it, call reject_pending_fact with the same fact_id instead. This is "
    "a conversational memory only — it does not change any alerting or "
    "automation code. Report it to the user as a saved preference, never as an "
    "installed or enforced rule.")


async def test_a_preference_is_staged_as_pending_for_the_person_present(
        mem, know, caplog):
    with caplog.at_level(logging.INFO, logger=mem._LOGGER.name):
        out = await mem._exec_remember(
            FakeHass(), {"key": "preference", "name": "Likes Tea ", "value": "with milk"})
    assert json.loads(out) == {"success": True, "status": "pending", "fact_id": 12,
                               "enforced": False, "message": PENDING_MESSAGE}
    assert list(json.loads(out)) == ["success", "status", "fact_id", "enforced", "message"]
    assert know.calls == [("likes tea", "with milk", {
        "subject": "presence:alex", "kind": "preference",
        "source": "stated", "status": "pending"})]
    assert [r.getMessage() for r in caplog.records] == [
        "Nova staged a pending preference (fact_id=12)"]


async def test_a_voice_turn_with_a_known_speaker_files_the_preference_under_them(
        mem, know):
    who = types.SimpleNamespace(known=True, who="sam")
    await mem._exec_remember(FakeHass(), {"key": "preference", "name": "n", "value": "v"},
                             who)
    assert know.calls[0][2]["subject"] == "voice:sam"


async def test_an_unknown_speaker_falls_back_to_presence(mem, know):
    who = types.SimpleNamespace(known=False, who="sam")
    await mem._exec_remember(FakeHass(), {"key": "preference", "name": "n", "value": "v"},
                             who)
    assert know.calls[0][2]["subject"] == "presence:alex"
    await mem._exec_remember(FakeHass(), {"key": "preference", "name": "n", "value": "v"},
                             types.SimpleNamespace(who="sam"))
    assert know.calls[1][2]["subject"] == "presence:alex"


async def test_a_routine_is_a_household_fact(mem, know):
    out = await mem._exec_remember(
        FakeHass(), {"key": "routine", "name": "Bins", "value": "out on Monday"})
    assert json.loads(out)["status"] == "pending"
    assert know.calls == [("bins", "out on Monday", {
        "subject": "household", "kind": "fact", "source": "stated", "status": "pending"})]


async def test_the_knowledge_write_runs_in_the_executor(mem, know):
    hass, jobs = FakeHass(), []

    async def run(func, *args):
        jobs.append(args)
        return func(*args)
    hass.async_add_executor_job = run
    await mem._exec_remember(hass, {"key": "routine", "name": "n", "value": "v"})
    assert jobs == [()] and len(know.calls) == 1


async def test_a_failed_write_is_reported_with_its_reason(mem, know, caplog):
    know.error = ValueError("store locked")
    with caplog.at_level(logging.WARNING, logger=mem._LOGGER.name):
        out = await mem._exec_remember(
            FakeHass(), {"key": "routine", "name": "n", "value": "v"})
    assert json.loads(out) == {"error": "failed to save: store locked"}
    assert [r.getMessage() for r in caplog.records] == [
        "Nova remember (pending) failed: store locked"]


@pytest.mark.parametrize("fact", [None, {}, 0])
async def test_a_fact_that_was_not_staged_is_reported(mem, know, fact):
    know.fact = fact
    out = await mem._exec_remember(FakeHass(), {"key": "routine", "name": "n", "value": "v"})
    assert json.loads(out) == {"error": "failed to stage fact for confirmation"}


async def test_a_nameless_preference_call_raises_as_before(mem, know):
    with pytest.raises(AttributeError):
        await mem._exec_remember(FakeHass(), {"key": "preference", "name": None})


# ── confirming and rejecting facts ───────────────────────────────────────────

@pytest.fixture
def facts(monkeypatch):
    box = types.SimpleNamespace(confirmed=[], forgotten=[], ok=True, removed=True)

    def confirm_fact(fact_id):
        box.confirmed.append(fact_id)
        return box.ok

    def forget(fact_id=None):
        box.forgotten.append(fact_id)
        return box.removed

    _stub(monkeypatch, "knowledge", confirm_fact=confirm_fact, forget=forget)
    return box


@pytest.mark.parametrize("raw, expected", [(5, 5), ("7", 7), (3.9, 3), (True, 1)])
async def test_a_fact_id_is_read_as_a_whole_number(mem, facts, raw, expected):
    await mem._exec_confirm_pending_fact(FakeHass(), {"fact_id": raw})
    await mem._exec_reject_pending_fact(FakeHass(), {"fact_id": raw})
    assert facts.confirmed == [expected] and facts.forgotten == [expected]


@pytest.mark.parametrize("raw", [None, "", "abc", [], {}])
async def test_a_missing_or_bad_fact_id_is_refused(mem, facts, raw):
    expected = {"error": "fact_id is required and must be an integer"}
    assert json.loads(await mem._exec_confirm_pending_fact(
        FakeHass(), {"fact_id": raw})) == expected
    assert json.loads(await mem._exec_reject_pending_fact(
        FakeHass(), {"fact_id": raw})) == expected
    assert await mem._exec_confirm_pending_fact(FakeHass(), {}) == json.dumps(expected)
    assert facts.confirmed == [] and facts.forgotten == []


async def test_confirming_a_pending_fact(mem, facts):
    out = await mem._exec_confirm_pending_fact(FakeHass(), {"fact_id": 4})
    assert json.loads(out) == {
        "success": True, "confirmed": 4, "enforced": False,
        "message": "Fact confirmed and now trusted in conversation — still a memory "
                   "only, not a change to any alerting or automation code."}
    assert list(json.loads(out)) == ["success", "confirmed", "enforced", "message"]


async def test_confirming_a_fact_that_is_not_pending(mem, facts):
    facts.ok = False
    out = await mem._exec_confirm_pending_fact(FakeHass(), {"fact_id": 4})
    assert json.loads(out) == {
        "error": "no pending fact with id 4 (already confirmed, rejected, or never existed)"}


@pytest.mark.parametrize("removed, success", [(True, True), (False, False), (0, False),
                                              (3, True)])
async def test_rejecting_a_fact_reports_whether_it_was_removed(mem, facts, removed, success):
    facts.removed = removed
    out = await mem._exec_reject_pending_fact(FakeHass(), {"fact_id": 9})
    assert json.loads(out) == {"success": success, "rejected": 9}
    assert list(json.loads(out)) == ["success", "rejected"]


# ── relations ────────────────────────────────────────────────────────────────

def test_a_relation_id_is_a_whole_number_and_never_a_bool(mem):
    for raw, expected in ((7, 7), ("8", 8), (3.9, 3), (True, None), (False, None),
                          (None, None), ("x", None), ([], None)):
        assert mem._relation_id({"relation_id": raw}) == expected
    assert mem._relation_id({}) is None


def test_the_relation_error_texts_are_unchanged(mem):
    assert mem._RELATION_ERRORS == {
        "invalid_subject": "subject must be 1 to 80 characters with no control "
                           "characters, and cannot be 'unknown'",
        "invalid_object": "object must be 1 to 80 characters with no control "
                          "characters, and cannot be 'unknown'",
        "invalid_predicate": "predicate must be lowercase snake case, 2 to 40 "
                             "characters, starting with a letter (for example "
                             "owns or adjacent_to)",
        "self_relation": "a thing cannot be related to itself",
        "relation_cap": "the relation store is full (500); ask the user to remove "
                        "some in the Memory tab before adding more",
        "removed_by_user": "the user removed this relation earlier; it is not added again",
        "failed": "the relation could not be saved",
    }


@pytest.fixture
def rel(monkeypatch):
    box = types.SimpleNamespace(
        proposed=[], removed=[], confirmed=[], result=None, remove_result=True,
        listing=[])

    def propose_relation(subject, predicate, obj, source=None):
        box.proposed.append((subject, predicate, obj, source))
        return box.result

    def remove_relation(rid, only_pending=False):
        box.removed.append((rid, only_pending))
        return box.remove_result

    def confirmed_relations(node=None, limit=None):
        box.confirmed.append((node, limit))
        return box.listing

    _stub(monkeypatch, "knowledge", propose_relation=propose_relation,
          remove_relation=remove_relation, confirmed_relations=confirmed_relations,
          RELATION_PROMPT_LIMIT=6)
    return box


async def test_a_relation_is_proposed_as_stated(mem, rel):
    rel.result = {"ok": True, "created": True,
                  "relation": {"id": 3, "status": "pending", "subject": "alex",
                               "predicate": "owns", "object": "bike"}}
    out = await mem._exec_propose_relation(
        FakeHass(), {"subject": "alex", "predicate": "owns", "object": "bike"})
    assert rel.proposed == [("alex", "owns", "bike", "stated")]
    assert json.loads(out) == {
        "success": True, "status": "pending", "relation_id": 3, "created": True,
        "enforced": False,
        "message": ("Saved 'alex owns bike' as PENDING, not yet trusted and not shown "
                    "to you again until the user confirms it. You cannot confirm it "
                    "yourself: tell the user to confirm it in the Memory tab of the "
                    "Nova panel (Relations). If they say it is wrong, call "
                    "reject_pending_relation with relation_id=3. It is a memory only "
                    "and changes no alert, automation or device.")}
    assert list(json.loads(out)) == ["success", "status", "relation_id", "created",
                                     "enforced", "message"]


async def test_a_proposal_with_missing_parts_passes_none_on(mem, rel):
    rel.result = {"ok": False, "error": "invalid_subject"}
    await mem._exec_propose_relation(FakeHass(), {})
    assert rel.proposed == [(None, None, None, "stated")]


async def test_an_already_confirmed_relation_is_not_staged_again(mem, rel):
    rel.result = {"ok": True, "created": False,
                  "relation": {"id": 5, "status": "confirmed", "subject": "alex",
                               "predicate": "owns", "object": "bike"}}
    out = await mem._exec_propose_relation(FakeHass(), {})
    assert json.loads(out) == {
        "success": True, "status": "confirmed", "relation_id": 5, "created": False,
        "message": "'alex owns bike' is already a confirmed relation."}
    assert list(json.loads(out)) == ["success", "status", "relation_id", "created",
                                     "message"]


async def test_a_pending_relation_that_already_existed_is_not_created_again(mem, rel):
    rel.result = {"ok": True, "created": False,
                  "relation": {"id": 5, "status": "pending", "subject": "a",
                               "predicate": "owns", "object": "b"}}
    out = await mem._exec_propose_relation(FakeHass(), {})
    assert json.loads(out)["created"] is False and json.loads(out)["status"] == "pending"


@pytest.mark.parametrize("code, text", [
    ("self_relation", "a thing cannot be related to itself"),
    ("failed", "the relation could not be saved"),
    ("something_new", "something_new")])
async def test_a_refused_proposal_carries_its_code_and_words(mem, rel, code, text):
    rel.result = {"ok": False, "error": code}
    out = await mem._exec_propose_relation(FakeHass(), {})
    assert json.loads(out) == {"error": text, "code": code}
    assert list(json.loads(out)) == ["error", "code"]


async def test_rejecting_a_pending_relation(mem, rel):
    out = await mem._exec_reject_pending_relation(FakeHass(), {"relation_id": "6"})
    assert json.loads(out) == {"success": True, "rejected": 6}
    assert rel.removed == [(6, True)]
    rel.remove_result = False
    out = await mem._exec_reject_pending_relation(FakeHass(), {"relation_id": 6})
    assert json.loads(out) == {"success": False, "rejected": 6}


@pytest.mark.parametrize("raw", [None, True, "x", ""])
async def test_rejecting_a_relation_needs_a_whole_number_id(mem, rel, raw):
    out = await mem._exec_reject_pending_relation(FakeHass(), {"relation_id": raw})
    assert json.loads(out) == {"error": "relation_id is required and must be an integer"}
    assert rel.removed == []


async def test_looking_up_relations_returns_confirmed_ones_only(mem, rel):
    rel.listing = [{"subject": "alex", "predicate": "owns", "object": "bike", "id": 1},
                   {"subject": "alex", "predicate": "lives_in", "object": "flat",
                    "status": "confirmed"}]
    out = await mem._exec_lookup_relations(FakeHass(), {"entity": "alex"})
    assert json.loads(out) == {
        "relations": [{"subject": "alex", "predicate": "owns", "object": "bike"},
                      {"subject": "alex", "predicate": "lives_in", "object": "flat"}],
        "note": "confirmed relations only"}
    assert list(json.loads(out)) == ["relations", "note"]
    assert rel.confirmed == [("alex", 12)]


async def test_looking_up_a_name_with_no_relations(mem, rel):
    out = await mem._exec_lookup_relations(FakeHass(), {"entity": "bike"})
    assert json.loads(out) == {"relations": [],
                               "note": "no confirmed relation involves that name"}


@pytest.mark.parametrize("entity", [None, "", "   ", 5, ["alex"], b"alex"])
async def test_looking_up_needs_a_name(mem, rel, entity):
    out = await mem._exec_lookup_relations(FakeHass(), {"entity": entity})
    assert json.loads(out) == {
        "error": "entity is required: the name of a person, place or thing"}
    assert rel.confirmed == []


async def test_the_name_is_passed_on_unstripped(mem, rel):
    await mem._exec_lookup_relations(FakeHass(), {"entity": " alex "})
    assert rel.confirmed == [(" alex ", 12)]


# ── ignore rules ─────────────────────────────────────────────────────────────

async def test_ignoring_passes_the_rule_to_the_cognitive_core(mem, monkeypatch):
    seen = []

    def ignore(**kw):
        seen.append(kw)
        return {"ignored": kw["entity_pattern"]}

    _stub(monkeypatch, "cognitive_core", ignore=ignore)
    out = await mem._exec_ignore(FakeHass(), {
        "entity_pattern": "sensor.garage_*", "duration_minutes": "30", "reason": "noisy"})
    assert json.loads(out) == {"ignored": "sensor.garage_*"}
    out = await mem._exec_ignore(FakeHass(), {})
    assert seen == [
        {"entity_pattern": "sensor.garage_*", "duration_minutes": 30, "reason": "noisy"},
        {"entity_pattern": "", "duration_minutes": 0, "reason": "user request"}]


async def test_a_bad_duration_is_a_safe_error(mem, monkeypatch):
    _stub(monkeypatch, "cognitive_core", ignore=lambda **kw: {"ok": True})
    out = await mem._exec_ignore(FakeHass(), {"entity_pattern": "x",
                                              "duration_minutes": "soon"})
    assert json.loads(out) == {"error": "ValueError (details are in the Home Assistant log)"}


async def test_a_failing_ignore_is_a_safe_error(mem, monkeypatch):
    def boom(**kw):
        raise RuntimeError("secret")
    _stub(monkeypatch, "cognitive_core", ignore=boom)
    out = await mem._exec_ignore(FakeHass(), {"entity_pattern": "x"})
    assert json.loads(out) == {"error": SAFE}


async def test_unignoring_passes_the_pattern_on(mem, monkeypatch):
    seen = []

    def unignore(pattern):
        seen.append(pattern)
        return {"removed": 1}

    _stub(monkeypatch, "cognitive_core", unignore=unignore)
    assert json.loads(await mem._exec_unignore(
        FakeHass(), {"entity_pattern": "sensor.x"})) == {"removed": 1}
    await mem._exec_unignore(FakeHass(), {})
    assert seen == ["sensor.x", ""]


async def test_a_failing_unignore_is_a_safe_error(mem, monkeypatch):
    def boom(pattern):
        raise RuntimeError("secret")
    _stub(monkeypatch, "cognitive_core", unignore=boom)
    assert json.loads(await mem._exec_unignore(FakeHass(), {})) == {"error": SAFE}


# ── documents ────────────────────────────────────────────────────────────────

@pytest.fixture
def docs(monkeypatch):
    box = types.SimpleNamespace(searches=[], hits=[], ingested=[], result={}, error=None)

    async def search_documents_async(hass, query, limit):
        box.searches.append((query, limit))
        if box.error:
            raise box.error
        return box.hits

    async def ingest_directory_async(hass):
        box.ingested.append(hass)
        if box.error:
            raise box.error
        return box.result

    _stub(monkeypatch, "documents", search_documents_async=search_documents_async,
          ingest_directory_async=ingest_directory_async)
    return box


async def test_a_search_with_no_hits_says_where_to_look(mem, docs, logged):
    out = await mem._exec_search_documents(FakeHass(), {"query": "boiler manual"})
    assert json.loads(out) == {
        "results": [],
        "note": ("nothing in the document library matched — it may be empty (add "
                 "files to /config/nova/documents and ingest) or the answer isn't "
                 "in the paperwork")}
    assert docs.searches == [("boiler manual", 4)]
    assert logged == []


async def test_a_search_returns_its_hits_and_logs_the_engine(mem, docs, logged):
    docs.hits = [{"title": "Boiler", "engine": "semantic"}, {"title": "Oven"}]
    out = await mem._exec_search_documents(FakeHass(), {"query": "boiler"})
    assert json.loads(out) == {"results": docs.hits}
    assert logged == [(("AGENT", "document search 'boiler' → 2 hits (semantic)"), {})]


async def test_a_search_without_an_engine_in_the_first_hit_logs_keyword(mem, docs, logged):
    docs.hits = [{"title": "Boiler"}]
    await mem._exec_search_documents(FakeHass(), {})
    assert docs.searches == [("", 4)]
    assert logged == [(("AGENT", "document search '' → 1 hits (keyword)"), {})]


async def test_a_failing_log_does_not_lose_the_hits(mem, docs, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log full")
    _stub(monkeypatch, "websocket", nova_log=boom)
    docs.hits = [{"title": "Boiler"}]
    out = await mem._exec_search_documents(FakeHass(), {"query": "b"})
    assert json.loads(out) == {"results": [{"title": "Boiler"}]}


async def test_a_failing_search_is_a_safe_error(mem, docs):
    docs.error = RuntimeError("index missing")
    out = await mem._exec_search_documents(FakeHass(), {"query": "b"})
    assert json.loads(out) == {"error": SAFE}


async def test_ingesting_returns_the_summary_and_logs_it(mem, docs, logged):
    docs.result = {"files_ingested": 3, "total_chunks": 40}
    hass = FakeHass()
    out = await mem._exec_ingest_documents(hass, {})
    assert json.loads(out) == {"files_ingested": 3, "total_chunks": 40}
    assert docs.ingested == [hass]
    assert logged == [(("AGENT", "document ingest: 3 files, 40 chunks"), {})]


async def test_a_semantic_ingest_adds_the_embedded_count(mem, docs, logged):
    docs.result = {"files_ingested": 1, "total_chunks": 9, "semantic": True,
                   "embedded_chunks": 9}
    await mem._exec_ingest_documents(FakeHass(), {})
    assert logged == [(("AGENT", "document ingest: 1 files, 9 chunks, 9 embedded"), {})]


async def test_an_empty_ingest_summary_logs_zeros(mem, docs, logged):
    docs.result = {"semantic": True}
    await mem._exec_ingest_documents(FakeHass(), {})
    assert logged == [(("AGENT", "document ingest: 0 files, 0 chunks, 0 embedded"), {})]


async def test_a_failing_log_does_not_lose_the_ingest_summary(mem, docs, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log full")
    _stub(monkeypatch, "websocket", nova_log=boom)
    docs.result = {"files_ingested": 2}
    out = await mem._exec_ingest_documents(FakeHass(), {})
    assert json.loads(out) == {"files_ingested": 2}


async def test_a_failing_ingest_is_a_safe_error(mem, docs):
    docs.error = RuntimeError("folder missing")
    out = await mem._exec_ingest_documents(FakeHass(), {})
    assert json.loads(out) == {"error": SAFE}


# ── ignore rules against the real cognitive core ──────────────────────────────

@pytest.fixture
def running(cc):
    """The core as start() leaves it: an ignore manager on _CORE."""
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    return cc._CORE


async def test_ignore_for_a_while_then_it_expires(mem, cc, running, clock):
    res = json.loads(await mem._exec_ignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door", "duration_minutes": 60,
        "reason": "door is being painted"}))
    assert res == {"success": True, "enforced": True, "pattern": "binary_sensor.garage_door",
                   "duration": 60, "reason": "door is being painted"}
    assert cc.is_ignored("binary_sensor.garage_door") is True
    assert cc.is_ignored("binary_sensor.front_door") is False
    clock["now"] += 60 * 60 + 1
    assert cc.is_ignored("binary_sensor.garage_door") is False


async def test_ignore_survives_a_restart_of_the_manager(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "sensor.pool_*"})
    running.ignore_mgr = cc.IgnoreManager()             # reloaded from the file
    assert cc.is_ignored("sensor.pool_temp") is True


@pytest.mark.parametrize("pattern", ["*", "**", "*.*", "?*", " * ", "", "*_*", "*.*_*"])
async def test_a_pattern_that_matches_everything_is_refused(mem, cc, running, pattern):
    # 8.7.19 pinned "*" muting every non-critical announcement and every
    # Sentinel rule, forever. A pattern that would match every entity is now
    # refused, and nothing is ignored.
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": pattern}))
    assert res["success"] is False and res["enforced"] is False
    assert "would ignore every entity" in res["error"]
    assert cc.list_ignores() == []
    assert cc.is_ignored("lock.front_door") is False


async def test_an_ignore_with_no_duration_expires_after_the_default(mem, cc, running, clock):
    # 8.7.19: no duration meant "until manually cleared". Every ignore now
    # expires; with none given it uses nova.nap's 30 minute default.
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.porch"}))
    assert res["success"] is True and res["duration"] == 30
    assert cc.list_ignores()[0]["remaining_min"] == 30
    clock["now"] += 30 * 60 + 1
    assert cc.is_ignored("light.porch") is False


async def test_a_plain_entity_and_a_domain_pattern_still_work(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_door",
                                        "duration_minutes": 15})
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.*"})
    assert cc.is_ignored("binary_sensor.garage_door") is True
    assert cc.is_ignored("binary_sensor.hall_motion") is True     # a whole domain is allowed
    assert cc.is_ignored("lock.front_door") is False


async def test_old_rules_from_disk_lose_wildcards_and_gain_an_expiry(cc, clock):
    # A rule file written by 8.7.19 or earlier: a "*" rule and a permanent one.
    import json as _json
    _json.dump([
        {"entity_pattern": "*", "reason": "", "expires_at": 0, "created_at": 1.0},
        {"entity_pattern": "light.porch", "reason": "", "expires_at": 0, "created_at": 1.0},
    ], open(cc._ignore_file(), "w"))
    cc._CORE.ignore_mgr = cc.IgnoreManager()
    assert [r["pattern"] for r in cc.list_ignores()] == ["light.porch"]
    assert cc.is_ignored("lock.front_door") is False
    clock["now"] += 30 * 60 + 1
    assert cc.is_ignored("light.porch") is False


async def test_a_bad_duration_adds_no_rule(mem, cc, running):
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.x",
                                                         "duration_minutes": "an hour"}))
    assert "error" in res and cc.is_ignored("light.x") is False


async def test_with_the_core_stopped_the_agent_is_told_nothing_is_enforced(mem, cc):
    cc._CORE.ignore_mgr = None
    res = json.loads(await mem._exec_ignore(FakeHass(), {"entity_pattern": "light.x"}))
    assert res == {"success": False, "enforced": False, "error": "Cognitive core not running"}


async def test_unignore_brings_the_entity_back(mem, cc, running):
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_door"})
    res = json.loads(await mem._exec_unignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door"}))
    assert res["success"] is True and cc.is_ignored("binary_sensor.garage_door") is False


async def test_unignore_of_a_rule_that_does_not_exist_reports_failure(mem, cc, running):
    res = json.loads(await mem._exec_unignore(FakeHass(), {"entity_pattern": "light.nope"}))
    assert res["success"] is False and res["restored_notifications"] == 0


async def test_current_behaviour_unignore_matches_the_pattern_text_not_the_entity(mem, cc, running):
    # Looks wrong: unignore removes a rule only by its exact pattern text, so
    # after "ignore binary_sensor.garage_*", "stop ignoring the garage door"
    # (binary_sensor.garage_door) removes nothing and the door stays muted.
    await mem._exec_ignore(FakeHass(), {"entity_pattern": "binary_sensor.garage_*"})
    res = json.loads(await mem._exec_unignore(FakeHass(), {
        "entity_pattern": "binary_sensor.garage_door"}))
    assert res["success"] is False
    assert cc.is_ignored("binary_sensor.garage_door") is True
