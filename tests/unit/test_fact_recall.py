"""Meaning based recall for saved facts (opt in, with semantic search).

Uses a real temporary knowledge database and a fake embedding backend, so the
ranking, indexing, staleness, pending facts, scope, timeout and fallback rules
are all exercised for real.
"""
import asyncio
import re
import sqlite3
import time

import pytest

# Words that mean the same thing share a dimension, so meaning can match
# without sharing a word.
_GROUPS = (
    {"cold", "warm", "temperature", "thermostat", "heating", "degrees", "sleep"},
    {"bin", "trash", "rubbish", "garbage", "collection"},
    {"car", "jeep", "vehicle", "garage", "parked"},
)


def _vec(text):
    words = set(re.findall(r"[a-z]+", text.lower()))
    v = [float(len(words & g)) for g in _GROUPS]
    return v if any(v) else [0.0, 0.0, 0.0]


_ALL_HASS = []


@pytest.fixture(autouse=True)
def _close_unrun_tasks():
    yield
    for h in _ALL_HASS:
        for coro in h.tasks:
            coro.close()
    _ALL_HASS.clear()


class _Hass:
    def __init__(self):
        self.tasks = []
        _ALL_HASS.append(self)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro):
        self.tasks.append(coro)
        return coro

    async def run_tasks(self):
        pending, self.tasks = self.tasks, []
        for coro in pending:
            await coro


@pytest.fixture
def kn(load, tmp_path, monkeypatch):
    mod = load("knowledge")
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "knowledge.db"))
    return mod


@pytest.fixture
def fr(load, kn):
    mod = load("fact_recall")
    mod.reset()
    yield mod
    mod.reset()


@pytest.fixture
def emb(load, monkeypatch):
    mod = load("embeddings")
    calls = {"texts": [], "one": 0}
    state = {"fail": False, "slow": 0.0, "model": "m1"}

    async def embed_texts(hass, texts, _is_probe=False):
        calls["texts"].append(list(texts))
        if state["fail"]:
            return None
        return [_vec(t) for t in texts]

    async def embed_one(hass, text, _is_probe=False):
        calls["one"] += 1
        if state["slow"]:
            await asyncio.sleep(state["slow"])
        if state["fail"]:
            return None
        return _vec(text)

    monkeypatch.setattr(mod, "is_enabled", lambda: True)
    monkeypatch.setattr(mod, "_model", lambda: state["model"])
    monkeypatch.setattr(mod, "embed_texts", embed_texts)
    monkeypatch.setattr(mod, "embed_one", embed_one)
    mod.calls, mod.state = calls, state
    return mod


def _vectors(kn):
    conn = sqlite3.connect(kn.DB_PATH)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT fact_id, model, dim FROM fact_vectors")}
    finally:
        conn.close()


async def _block(fr, hass, query, **kw):
    return await fr.semantic_prompt_block(hass, query, **kw)


# ── off and fallback ────────────────────────────────────────────────────────

async def test_off_returns_none_and_never_embeds(fr, kn, emb, monkeypatch):
    monkeypatch.setattr(emb, "is_enabled", lambda: False)
    kn.remember("sleep temperature", "17 degrees")
    hass = _Hass()
    assert await _block(fr, hass, "who runs cold") is None
    assert emb.calls["one"] == 0 and hass.tasks == []
    assert await fr.async_index(hass) == 0


async def test_empty_query_returns_none(fr, kn, emb):
    assert await _block(fr, _Hass(), "") is None


# ── indexing ────────────────────────────────────────────────────────────────

async def test_only_confirmed_live_facts_are_embedded(fr, kn, emb):
    ok = kn.remember("sleep temperature", "17 degrees")
    pend = kn.remember("secret", "pending thing", status="pending")
    old = kn.remember("old", "expired", ttl_seconds=1, now=time.time() - 100)
    assert await fr.async_index(_Hass()) == 1
    assert set(_vectors(kn)) == {ok["id"]}
    assert pend["id"] not in _vectors(kn) and old["id"] not in _vectors(kn)
    assert await fr.async_index(_Hass()) == 0          # nothing left to do


async def test_edited_fact_is_stale_until_re_embedded(fr, kn, emb):
    f = kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    kn.edit_fact(f["id"], "18 degrees thermostat")
    assert [x["id"] for x in fr._stale_facts("m1", 10, time.time())] == [f["id"]]
    assert await fr.async_index(_Hass()) == 1
    assert fr._stale_facts("m1", 10, time.time()) == []


async def test_changed_model_re_embeds_everything(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    emb.state["model"] = "m2"
    assert await fr.async_index(_Hass()) == 1
    assert {v[0] for v in _vectors(kn).values()} == {"m2"}


async def test_forgetting_or_expiring_a_fact_removes_its_vector(fr, kn, emb):
    a = kn.remember("sleep temperature", "17 degrees")
    b = kn.remember("bin day", "Tuesday", ttl_seconds=3600)
    await fr.async_index(_Hass())
    assert set(_vectors(kn)) == {a["id"], b["id"]}
    assert kn.forget(fact_id=a["id"]) == 1
    assert set(_vectors(kn)) == {b["id"]}
    assert kn.purge_expired(now=time.time() + 7200) == 1
    assert _vectors(kn) == {}


async def test_indexing_failure_pauses_semantic_recall(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    emb.state["fail"] = True
    assert await fr.async_index(_Hass()) == 0
    assert fr._breaker_open()
    assert _vectors(kn) == {}


async def test_vector_for_a_deleted_fact_is_never_stored(fr, kn, emb):
    f = kn.remember("sleep temperature", "17 degrees")
    assert fr._store_vectors("m1", [(f["id"] + 99, "h", [1.0, 0.0, 0.0])]) == 0
    assert _vectors(kn) == {}


# ── ranking ─────────────────────────────────────────────────────────────────

async def test_finds_a_fact_by_meaning_without_a_shared_word(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    kn.remember("bin day", "Tuesday")
    await fr.async_index(_Hass())
    block = await _block(fr, _Hass(), "who runs cold at night")
    assert block and "sleep temperature: 17 degrees" in block
    assert "bin day" not in block
    assert "KNOWLEDGE" in block                  # still fenced


async def test_unrelated_question_finds_nothing(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    assert await _block(fr, _Hass(), "zzz qqq") is None


async def test_keyword_hits_still_count_without_a_vector(fr, kn, emb):
    kn.remember("wifi password hint", "the usual one")
    block = await _block(fr, _Hass(), "wifi hint")
    assert block is None or "wifi password hint" in block
    facts = fr._rank([0.0, 0.0, 0.0], "wifi hint", "m1", None, None, time.time(), 5)
    assert [f["key"] for f in facts] == ["wifi password hint"]


async def test_pending_facts_are_never_shown_even_with_a_vector(fr, kn, emb):
    p = kn.remember("sleep temperature", "17 degrees", status="pending")
    fr._store_vectors("m1", [(p["id"], fr._hash("m1", "sleep temperature: 17 degrees"),
                              _vec("sleep temperature: 17 degrees"))])
    assert await _block(fr, _Hass(), "who runs cold") is None
    kn.confirm_fact(p["id"])
    await fr.async_index(_Hass())
    assert "sleep temperature" in (await _block(fr, _Hass(), "who runs cold"))


async def test_stale_vector_is_not_used_but_keywords_still_match(fr, kn, emb):
    f = kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    kn.edit_fact(f["id"], "bins out on tuesday")        # vector now describes old text
    assert await _block(fr, _Hass(), "who runs cold") is None
    facts = fr._rank(_vec("tuesday bins"), "tuesday bins", "m1", None, None,
                     time.time(), 5)
    assert [x["id"] for x in facts] == [f["id"]]         # found by keywords only


async def test_wrong_size_vector_is_ignored(fr, kn, emb):
    f = kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    facts = fr._rank([1.0, 0.0, 0.0, 0.0], "who runs cold", "m1", None, None,
                     time.time(), 5)
    assert facts == []                                   # dim 4 vs stored dim 3


async def test_person_scope_is_respected(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees", subject="person_a")
    kn.remember("sleep temperature", "21 degrees", subject="person_b")
    await fr.async_index(_Hass())
    block = await _block(fr, _Hass(), "who runs cold", subjects=["person_a", "household"])
    assert "17 degrees" in block and "21 degrees" not in block


# ── the chat path stays safe ────────────────────────────────────────────────

async def test_failed_question_embedding_falls_back_and_pauses(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    emb.state["fail"] = True
    assert await _block(fr, _Hass(), "who runs cold") is None
    before = emb.calls["one"]
    assert await _block(fr, _Hass(), "who runs cold") is None
    assert emb.calls["one"] == before                    # paused: no new call


async def test_slow_embedding_is_cut_off(fr, kn, emb, monkeypatch):
    kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    monkeypatch.setattr(fr, "QUERY_TIMEOUT_S", 0.05)
    emb.state["slow"] = 1.0
    t0 = time.monotonic()
    assert await _block(fr, _Hass(), "who runs cold") is None
    assert time.monotonic() - t0 < 0.5
    assert fr._breaker_open()


async def test_recovers_after_the_pause(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    await fr.async_index(_Hass())
    fr._trip(time.time() - fr.BREAKER_S - 1)
    assert "sleep temperature" in (await _block(fr, _Hass(), "who runs cold"))


async def test_background_indexing_is_started_once_a_minute(fr, kn, emb):
    kn.remember("sleep temperature", "17 degrees")
    hass = _Hass()
    await _block(fr, hass, "who runs cold")
    await _block(fr, hass, "who runs cold")
    assert len(hass.tasks) == 1                          # throttled
    await hass.run_tasks()
    assert len(_vectors(kn)) == 1
    assert await _block(fr, hass, "who runs cold")       # now found by meaning


# ── wiring ──────────────────────────────────────────────────────────────────

def test_keyword_prompt_block_is_unchanged(kn):
    kn.remember("trash day", "Tuesday")
    kn.remember("secret", "hidden", status="pending")
    block = kn.prompt_block("trash")
    assert "trash day: Tuesday" in block and "hidden" not in block
    assert kn.format_block([]) == ""


def test_conversation_tries_meaning_first_then_keywords():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "custom_components" /
           "nova" / "conversation.py").read_text()
    sem = src.index("fact_recall.semantic_prompt_block(")
    kw = src.index("lambda: knowledge.prompt_block(user_input.text, subjects=subjects))")
    assert sem < kw
    assert "if kn_block is None:" in src[sem:kw]


def test_an_existing_knowledge_database_gains_the_vector_table(load, tmp_path):
    """An install that already has facts upgrades in place: the new table and
    its cleanup trigger appear, and no fact is touched."""
    store = load("persistence.sqlite")
    path = tmp_path / "nova" / "knowledge.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    store.ensure(conn, "facts")
    conn.execute("INSERT INTO facts (kind, subject, key, value, source, confidence, "
                 "salience, status, created_at, updated_at) VALUES "
                 "('fact', 'household', 'bin day', 'Tuesday', 'stated', 1, 1, "
                 "'confirmed', 1, 1)")
    conn.commit()
    conn.close()
    statuses = store.upgrade_existing(tmp_path)
    assert all(s.ok for s in statuses.values()), statuses
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
    assert {"fact_vectors", "trg_fact_vectors_cleanup"} <= names
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    conn.execute("INSERT INTO fact_vectors VALUES (1, 'm', 3, x'00', 'h', 1)")
    conn.execute("DELETE FROM facts WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM fact_vectors").fetchone()[0] == 0
    conn.close()
