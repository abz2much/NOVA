"""Which rooms a camera can confirm a person in (camera_coverage.py): the
geometry fallback, reading the model's reply, the model call itself through
fake providers, and finding a camera for a room."""
import json

import pytest


@pytest.fixture
def cc(load):
    return load("camera_coverage")


# geometry fallback

def test_geometry_counts_seventy_percent_and_up(cc):
    out = cc._geometry_result({"Hall": 0.69, "Study": 0.7, "Den": None, "Loft": 1.0})
    assert out["covered"] == ["Study", "Loft"]
    assert out["source"] == "geometry" and isinstance(out["ts"], int)
    assert set(out) == {"covered", "reason", "source", "ts"}


def test_geometry_sentence(cc):
    out = cc._geometry_result({"Den": 0.1, "Hall": 0.3, "Study": 0.695, "Loft": 0.9})
    assert out["reason"] == "Sees most of Loft, most of Study, part of Hall, a corner of Den."


def test_geometry_with_no_rooms(cc):
    assert cc._geometry_result({})["reason"] == "Nothing in view."


# reading the reply

def test_reply_rooms_mapped_to_given_spelling(cc):
    out = cc._parse('{"covered": ["living room", "DEN", "Attic"], "reason": " A wide view. "}',
                    {"Den": 0.9, "Living Room": 0.8})
    assert out["covered"] == ["Living Room", "Den"]
    assert out["reason"] == "A wide view."
    assert out["source"] == "llm" and set(out) == {"covered", "reason", "source", "ts"}


def test_reply_in_fences_with_chatter(cc):
    text = '```json\nSure! {"covered": ["Den"],\n "reason": "ok"} hope that helps\n```'
    assert cc._parse(text, {"Den": 1.0})["covered"] == ["Den"]


def test_reply_covered_not_a_list(cc):
    assert cc._parse('{"covered": "Den", "reason": "r"}', {"Den": 1})["covered"] == []


@pytest.mark.parametrize("text", ["", "no json here", '{"covered": ["Den"]}',
                                  '{"covered": [], "reason": "   "}', "{broken",
                                  '["Den"]', "null"])
def test_unusable_replies(cc, text):
    assert cc._parse(text, {"Den": 1.0}) is None


# the model call

class _Result:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def model(monkeypatch, load):
    """Fake provider plumbing; records the chat call."""
    rec = {"reply": '{"covered": ["Den"], "reason": "Most of the den."}', "calls": [],
           "boom": None}

    class _Scope:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def lease(self, spec, factory=None):
            rec["spec"], rec["factory"] = spec, factory
            return _Lease()

    class _Lease:
        async def __aenter__(self):
            return "client"

        async def __aexit__(self, *a):
            return False

    async def execute_chat(hass, client, messages, **kw):
        if rec["boom"]:
            raise rec["boom"]
        rec["calls"].append((client, messages, kw))
        return _Result(rec["reply"])

    for name, attrs in {
        "providers.activity": {"execute_chat": execute_chat},
        "providers.manager": {"provider_scope": lambda hass: _Scope()},
        "providers.routing": {"tier_spec": lambda config, tier: ("spec", tier)},
    }.items():
        mod = load(name)
        for k, v in attrs.items():
            monkeypatch.setattr(mod, k, v)
    llm = load("llm_provider")
    monkeypatch.setattr(llm, "create_tier_provider", lambda config, tier: ("made", tier))
    return rec


CAM = {"entity": "camera.x", "room": "Den", "fov": 110, "indoor": True,
       "candidates": {"Den": 0.92, "Hall": 0.35}, "openings": ["Den-Hall arch"]}


async def test_no_candidates_skips_the_model(cc, model):
    out = await cc.infer_coverage(None, {}, {"room": "Den"})
    assert (out["covered"], out["reason"], out["source"]) == ([], "Nothing in view.", "geometry")
    assert model["calls"] == []


async def test_model_answer_used(cc, model):
    out = await cc.infer_coverage(None, {"k": 1}, CAM)
    assert out["covered"] == ["Den"] and out["reason"] == "Most of the den."
    assert out["source"] == "llm"
    client, messages, kw = model["calls"][0]
    assert client == "client" and model["spec"] == ("spec", "reasoning")
    assert model["factory"]() == ("made", "reasoning")
    assert kw == {"role": "coverage", "data_category": "text", "max_tokens": 400,
                  "temperature": 0.3}
    assert [m["role"] for m in messages] == ["system", "user"]


async def test_prompt_carries_the_facts(cc, model):
    await cc.infer_coverage(None, {}, CAM)
    system, user = (m["content"] for m in model["calls"][0][1])
    assert "JSON" in system and "covered" in system and "reason" in system
    assert "70%" in system
    assert "Den" in user and "110" in user
    assert "- Den: 92%" in user and "- Hall: 35%" in user
    assert user.index("- Den: 92%") < user.index("- Hall: 35%")
    assert "Den-Hall arch" in user
    assert "outdoors" not in user


async def test_prompt_for_an_outdoor_camera(cc, model):
    cam = dict(CAM, indoor=False, range_ft=40, room=None, fov=None)
    cam.pop("fov")
    await cc.infer_coverage(None, {}, cam)
    user = model["calls"][0][1][1]["content"]
    assert "outdoors" in user and "40 ft" in user
    assert "an unknown room" in user and "90" in user


async def test_unusable_reply_falls_back_to_geometry(cc, model):
    model["reply"] = "I think it sees the den"
    out = await cc.infer_coverage(None, {}, CAM)
    assert out["source"] == "geometry" and out["covered"] == ["Den"]
    model["reply"] = None
    assert (await cc.infer_coverage(None, {}, CAM))["source"] == "geometry"


async def test_model_failure_falls_back_to_geometry(cc, model):
    model["boom"] = RuntimeError("provider down")
    out = await cc.infer_coverage(None, {}, CAM)
    assert out == {"covered": ["Den"], "reason": "Sees most of Den, part of Hall.",
                   "source": "geometry", "ts": out["ts"]}


def test_coverage_reply_is_not_translated(cc):
    src = open(cc.__file__).read()
    for needle in ("with_language(", "field_directive(", "output_language.directive("):
        assert needle not in src


# a camera for a room

FLOORS = {
    "ground": [
        {"entity": "", "coverage": {"covered": ["Kitchen"]}},
        {"entity": "camera.one", "coverage": {"covered": [" Living Room ", "Dining"]}},
        {"entity": "camera.two", "coverage": {"covered": ["Kitchen"]}},
        {"entity": "camera.three"},
    ],
    "upper": None,
    "loft": [{"entity": "camera.four", "coverage": {"covered": ["", "Attic Bedroom"]}}],
}


@pytest.mark.parametrize("area,expected", [
    ("kitchen", "camera.two"), ("LIVING ROOM", "camera.one"), ("living", "camera.one"),
    ("Dining Room", "camera.one"), ("attic", "camera.four"), ("garage", None),
    ("", None), (None, None), ("   ", None)])
def test_match_camera(cc, area, expected):
    assert cc._match_camera(FLOORS, area) == expected


def test_match_camera_with_no_plan(cc):
    assert cc._match_camera(None, "Kitchen") is None


def test_camera_for_area_reads_saved_plan(cc, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda key, default=None:
                        json.dumps(FLOORS) if key == "floor_plan_cameras" else default)
    assert cc.camera_for_area(None, "Kitchen") == "camera.two"
    monkeypatch.setattr(cfg, "get", lambda key, default=None: FLOORS)
    assert cc.camera_for_area(None, "attic") == "camera.four"
    monkeypatch.setattr(cfg, "get", lambda key, default=None: None)
    assert cc.camera_for_area(None, "Kitchen") is None


def test_camera_for_area_never_raises(cc, load, monkeypatch):
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda key, default=None: "{not json")
    assert cc.camera_for_area(None, "Kitchen") is None

    def boom(key, default=None):
        raise RuntimeError("store gone")
    monkeypatch.setattr(cfg, "get", boom)
    assert cc.camera_for_area(None, "Kitchen") is None
    assert cc.camera_for_area(None, "") is None
