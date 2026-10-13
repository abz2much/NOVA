"""Acceptance tests for camera_backends.py, written from its behaviour spec."""
import sys
import types

import pytest

BIG = b"\xff" * 2001
EDGE = b"\xff" * 2000


class FakeSession:
    def __init__(self):
        self.status = 200
        self.body = BIG
        self.exc = None
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        session = self

        class _Ctx:
            async def __aenter__(self_inner):
                if session.exc:
                    raise session.exc
                async def read():
                    return session.body
                return types.SimpleNamespace(status=session.status, read=read)

            async def __aexit__(self_inner, *exc):
                return False
        return _Ctx()


class Env:
    def __init__(self):
        self.platforms = {}
        self.registry_exc = None
        self.session = FakeSession()
        self.session_calls = 0
        self.session_exc = None
        self.base_url = "http://nova.local:8123"
        self.url_exc = None
        self.url_kwargs = None


@pytest.fixture
def env(monkeypatch):
    e = Env()
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda **kw: ("ClientTimeout", kw)
    aiohttp.ClientSession = object
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)

    er = sys.modules["homeassistant.helpers.entity_registry"]
    ac = sys.modules["homeassistant.helpers.aiohttp_client"]
    net = sys.modules["homeassistant.helpers.network"]

    def registry(hass):
        if e.registry_exc:
            raise e.registry_exc
        return types.SimpleNamespace(async_get=lambda eid: (
            types.SimpleNamespace(entity_id=eid, platform=e.platforms[eid])
            if eid in e.platforms else None))

    def session(hass):
        e.session_calls += 1
        if e.session_exc:
            raise e.session_exc
        return e.session

    def get_url(hass, **kw):
        e.url_kwargs = kw
        if e.url_exc:
            raise e.url_exc
        return e.base_url

    monkeypatch.setattr(er, "async_get", registry)
    monkeypatch.setattr(ac, "async_get_clientsession", session)
    monkeypatch.setattr(net, "get_url", get_url)
    return e


@pytest.fixture
def cb(env, load, monkeypatch):
    monkeypatch.delitem(sys.modules, "jc.camera_backends", raising=False)
    return load("camera_backends")


# ── interface ────────────────────────────────────────────────────────────────

def test_min_image_size(cb):
    assert cb.MIN_IMAGE_SIZE == 2000


def test_backend_names_and_order(cb):
    assert [type(b) for b in cb.BACKENDS] == [
        cb.FrigateBackend, cb.NestBackend, cb.UniFiProtectBackend, cb.ReolinkBackend]
    assert [b.name for b in cb.BACKENDS] == ["frigate", "nest", "unifiprotect", "reolink"]
    assert all(isinstance(b, cb.CameraBackend) for b in cb.BACKENDS)


def test_base_is_abstract(cb):
    assert cb.CameraBackend.name == "abstract"
    with pytest.raises(TypeError):
        cb.CameraBackend()

    class HalfDone(cb.CameraBackend):
        def handles(self, hass, entity_id):
            return True
    with pytest.raises(TypeError):
        HalfDone()

    class Whole(cb.CameraBackend):
        name = "whole"

        def handles(self, hass, entity_id):
            return True

        async def fetch_best_image(self, hass, entity_id, event_cache):
            return None
    assert Whole().name == "whole"


# ── ownership ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,expected", [
    ("frigate", "frigate"), ("nest", "nest"), ("unifiprotect", "unifiprotect"),
    ("unifi_protect", "unifiprotect"), ("reolink", "reolink"),
    ("generic", None), ("Frigate", None), ("onvif", None)])
def test_find_backend_by_platform(cb, env, fake_hass, platform, expected):
    env.platforms["camera.porch"] = platform
    b = cb.find_backend(fake_hass, "camera.porch")
    assert (b.name if b else None) == expected


def test_find_backend_unknown_entity(cb, env, fake_hass):
    assert cb.find_backend(fake_hass, "camera.nowhere") is None


def test_handles_each_backend(cb, env, fake_hass):
    env.platforms.update({"camera.a": "frigate", "camera.b": "unifi_protect"})
    frig, nest, uni, reo = cb.BACKENDS
    assert frig.handles(fake_hass, "camera.a") is True
    assert nest.handles(fake_hass, "camera.a") is False
    assert uni.handles(fake_hass, "camera.b") is True
    assert reo.handles(fake_hass, "camera.b") is False
    assert frig.handles(fake_hass, "camera.zzz") is False


def test_find_backend_survives_registry_failure(cb, env, fake_hass):
    env.registry_exc = RuntimeError("registry down")
    assert cb.find_backend(fake_hass, "camera.porch") is None


def test_find_backend_skips_raising_backend(cb, env, fake_hass, monkeypatch):
    class Broken(cb.CameraBackend):
        name = "broken"

        def handles(self, hass, entity_id):
            raise RuntimeError("x")

        async def fetch_best_image(self, hass, entity_id, event_cache):
            return None

    class Mine(Broken):
        name = "mine"

        def handles(self, hass, entity_id):
            return True
    monkeypatch.setattr(cb, "BACKENDS", [Broken(), Mine()])
    assert cb.find_backend(fake_hass, "camera.any").name == "mine"


def test_first_matching_backend_wins(cb, env, fake_hass, monkeypatch):
    class Yes(cb.CameraBackend):
        def handles(self, hass, entity_id):
            return True

        async def fetch_best_image(self, hass, entity_id, event_cache):
            return None
    first, second = Yes(), Yes()
    monkeypatch.setattr(cb, "BACKENDS", [first, second])
    assert cb.find_backend(fake_hass, "camera.x") is first


# ── Frigate ──────────────────────────────────────────────────────────────────

async def test_frigate_fetches_event_snapshot(cb, env, fake_hass):
    cache = {"camera.porch": {"source": "frigate", "event_id": "ev-1"}}
    data = await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", cache)
    assert data == BIG
    assert env.session.calls == [(
        "http://nova.local:8123/api/frigate/notifications/ev-1/snapshot.jpg",
        ("ClientTimeout", {"total": 10}))]
    assert env.url_kwargs == {"allow_internal": True, "prefer_external": False}


async def test_frigate_base_url_fallback(cb, env, fake_hass):
    env.url_exc = RuntimeError("no url")
    cache = {"camera.porch": {"source": "frigate", "event_id": "ev-2"}}
    await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", cache)
    assert env.session.calls[0][0] == "http://127.0.0.1:8123/api/frigate/notifications/ev-2/snapshot.jpg"


@pytest.mark.parametrize("cache", [
    {}, {"camera.porch": {}}, {"camera.porch": {"source": "nest", "event_id": "e"}},
    {"camera.porch": {"source": "frigate"}}, {"camera.porch": {"source": "frigate", "event_id": ""}},
    {"camera.other": {"source": "frigate", "event_id": "e"}}])
async def test_frigate_no_usable_event(cb, env, fake_hass, cache):
    assert await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", cache) is None
    assert env.session.calls == []


async def test_frigate_gets_session_even_without_event(cb, env, fake_hass):
    await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", {})
    assert env.session_calls == 1


async def test_frigate_session_failure_propagates_quirk(cb, env, fake_hass):
    env.session_exc = RuntimeError("no session")
    with pytest.raises(RuntimeError):
        await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", {})


@pytest.mark.parametrize("status,body,exc", [
    (404, BIG, None), (200, EDGE, None), (200, b"", None), (200, BIG, OSError("timeout"))])
async def test_frigate_rejects_bad_responses(cb, env, fake_hass, status, body, exc):
    env.session.status, env.session.body, env.session.exc = status, body, exc
    cache = {"camera.porch": {"source": "frigate", "event_id": "ev"}}
    assert await cb.FrigateBackend().fetch_best_image(fake_hass, "camera.porch", cache) is None


# ── Nest ─────────────────────────────────────────────────────────────────────

class MediaStore:
    def __init__(self, contents=BIG, exc=None, has_contents=True):
        self.contents, self.exc, self.has_contents = contents, exc, has_contents
        self.calls = []

    async def async_get_media(self, device_id, event_id):
        self.calls.append((device_id, event_id))
        if self.exc:
            raise self.exc
        if not self.has_contents:
            return types.SimpleNamespace()
        return types.SimpleNamespace(contents=self.contents)


NEST_CACHE = {"camera.hall": {"source": "nest", "device_id": "dev-1", "event_id": "ev-9"}}


async def test_nest_returns_media(cb, fake_hass):
    store = MediaStore()
    fake_hass.data["nest"] = types.SimpleNamespace(subscribers=[types.SimpleNamespace(media_store=store)])
    assert await cb.NestBackend().fetch_best_image(fake_hass, "camera.hall", NEST_CACHE) == BIG
    assert store.calls == [("dev-1", "ev-9")]


async def test_nest_tries_next_subscriber(cb, fake_hass):
    good = MediaStore()
    subs = [types.SimpleNamespace(media_store=None),
            types.SimpleNamespace(),
            types.SimpleNamespace(media_store=MediaStore(exc=RuntimeError("x"))),
            types.SimpleNamespace(media_store=MediaStore(contents=EDGE)),
            types.SimpleNamespace(media_store=MediaStore(has_contents=False)),
            types.SimpleNamespace(media_store=good)]
    fake_hass.data["nest"] = types.SimpleNamespace(subscribers=subs)
    assert await cb.NestBackend().fetch_best_image(fake_hass, "camera.hall", NEST_CACHE) == BIG
    assert good.calls == [("dev-1", "ev-9")]


@pytest.mark.parametrize("cache", [
    {}, {"camera.hall": {"source": "frigate", "device_id": "d", "event_id": "e"}},
    {"camera.hall": {"source": "nest", "event_id": "e"}},
    {"camera.hall": {"source": "nest", "device_id": "d"}}])
async def test_nest_needs_complete_cache(cb, fake_hass, cache):
    store = MediaStore()
    fake_hass.data["nest"] = types.SimpleNamespace(subscribers=[types.SimpleNamespace(media_store=store)])
    assert await cb.NestBackend().fetch_best_image(fake_hass, "camera.hall", cache) is None
    assert store.calls == []


async def test_nest_without_integration_data(cb, fake_hass):
    assert await cb.NestBackend().fetch_best_image(fake_hass, "camera.hall", NEST_CACHE) is None


async def test_nest_dict_data_quirk(cb, fake_hass):
    # A dict (keyed by config entry) has no `subscribers` attribute.
    store = MediaStore()
    fake_hass.data["nest"] = {"entry1": types.SimpleNamespace(subscribers=[
        types.SimpleNamespace(media_store=store)])}
    assert await cb.NestBackend().fetch_best_image(fake_hass, "camera.hall", NEST_CACHE) is None


async def test_nest_broken_hass_data(cb):
    class BadData:
        def get(self, key):
            raise RuntimeError("x")
    hass = types.SimpleNamespace(data=BadData())
    assert await cb.NestBackend().fetch_best_image(hass, "camera.hall", NEST_CACHE) is None


# ── placeholders ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", ["UniFiProtectBackend", "ReolinkBackend"])
async def test_placeholders_return_none(cb, fake_hass, cls):
    cache = {"camera.x": {"source": "frigate", "event_id": "e"}}
    assert await getattr(cb, cls)().fetch_best_image(fake_hass, "camera.x", cache) is None
