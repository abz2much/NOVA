"""Phase 6 boundaries, enforced statically and with fakes:

* production code outside the providers package never calls a provider's
  chat() itself; every call goes through the activity boundary
  (providers.activity.execute_chat, or llm_provider.chat_with_activity),
* nothing outside providers/activity.py calls provider_activity.record(),
* no production code reads a response's ``raw`` value,
* no module keeps provider clients in a global cache,
* Provider Activity receives only bounded scalars: never content, reasoning,
  credentials, headers, raw values or images.

Each scanner is also run against a planted violation, so a scanner that
stops finding anything fails here instead of passing silently."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fakes import FakeHass

COMP = Path(__file__).resolve().parents[2] / "custom_components" / "nova"
PROVIDERS = COMP / "providers"


def _production_modules():
    for path in sorted(COMP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _outside_providers():
    for path, tree in _production_modules():
        if PROVIDERS not in path.parents:
            yield path, tree


# ── Scanners ────────────────────────────────────────────────────────────────

def direct_chat_uses(tree) -> list[int]:
    """Lines that touch a `.chat` attribute: a call, or a bound method
    handed to an executor."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "chat"]


def recorder_uses(tree) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "record"
                and isinstance(node.value, ast.Name) and node.value.id == "provider_activity"):
            lines.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("provider_activity"):
            if any(alias.name == "record" for alias in node.names):
                lines.append(node.lineno)
    return lines


def raw_reads(tree) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "raw"):
            lines.append(node.lineno)
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and node.slice.value == "raw"):
            lines.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "raw" and isinstance(node.ctx, ast.Load):
            lines.append(node.lineno)
    return lines


def global_client_caches(tree) -> list[int]:
    lines = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            name = getattr(target, "id", "")
            if "PROVIDER_CACHE" in name or "CLIENT_CACHE" in name:
                lines.append(node.lineno)
        value = getattr(node, "value", None)
        if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "ProviderManager":
            lines.append(node.lineno)
    return lines


# ── Sabotage: each scanner finds a planted violation ────────────────────────

PLANTED = '''
_PROVIDER_CACHE = {}
MANAGER = ProviderManager(None)

async def bad(hass, client):
    result = await hass.async_add_executor_job(lambda: client.chat(messages=[]))
    job = await hass.async_add_executor_job(client.chat, [])
    provider_activity.record("groq", "m", "llm", "cloud", "text", True)
    return result.get("raw"), result["raw"], result.raw
'''


def test_scanners_catch_planted_violations():
    tree = ast.parse(PLANTED)
    assert len(direct_chat_uses(tree)) == 2
    assert len(recorder_uses(tree)) == 1
    assert len(raw_reads(tree)) == 3
    assert len(global_client_caches(tree)) == 2
    imported = ast.parse("from .provider_activity import record\n")
    assert recorder_uses(imported) == [1]


# ── The boundaries hold in production code ──────────────────────────────────

def test_no_direct_provider_chat_outside_the_providers_package():
    offenders = {str(p.relative_to(COMP)): lines
                 for p, tree in _outside_providers() if (lines := direct_chat_uses(tree))}
    assert offenders == {}


def test_only_the_activity_boundary_calls_the_recorder():
    offenders = {str(p.relative_to(COMP)): lines
                 for p, tree in _production_modules()
                 if p != PROVIDERS / "activity.py" and (lines := recorder_uses(tree))}
    assert offenders == {}


def test_no_production_code_reads_raw():
    offenders = {str(p.relative_to(COMP)): lines
                 for p, tree in _outside_providers() if (lines := raw_reads(tree))}
    assert offenders == {}


def test_no_global_provider_client_caches():
    offenders = {str(p.relative_to(COMP)): lines
                 for p, tree in _production_modules() if (lines := global_client_caches(tree))}
    assert offenders == {}


def test_providers_package_keeps_no_hass_data_state():
    for path in PROVIDERS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "data"
                and isinstance(n.value, ast.Name) and n.value.id == "hass"]
        assert uses == [], path.name


# ── Provider Activity privacy ───────────────────────────────────────────────

SECRET = "sk-LIVE-SECRET-123"
REASONING = "private chain of thought"
IMAGE = "data:image/jpeg;base64,/9j/SECRETIMAGE"


class _LeakyProvider:
    """A provider-shaped double whose reply carries everything that must
    never reach Provider Activity."""

    name = "openai"
    model = "gpt-4o"
    base_url = None
    api_key = SECRET

    def chat(self, messages, **kw):
        return {
            "text": f"<think>{REASONING}</think>visible answer",
            "tool_calls": [{"id": "c1", "name": "t", "args": {"password": SECRET}}],
            "raw": {"reasoning": REASONING, "headers": {"Authorization": f"Bearer {SECRET}"}},
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


def _capture_recorder(load, monkeypatch):
    activity = load("providers.activity")
    pa = load("provider_activity")
    captured = []
    monkeypatch.setattr(pa, "db_path_for", lambda hass: "in-memory")
    monkeypatch.setattr(pa, "record", lambda *a, **k: captured.append((a, k)))
    return activity, captured


@pytest.mark.parametrize("fail", [False, True])
async def test_activity_samples_hold_only_bounded_scalars(load, monkeypatch, fail):
    activity, captured = _capture_recorder(load, monkeypatch)
    provider = _LeakyProvider()
    if fail:
        def boom(messages, **kw):
            raise RuntimeError(f"401 Unauthorized: Bearer {SECRET} body={REASONING}")
        provider.chat = boom
    messages = [{"role": "user", "content": [
        {"type": "text", "text": f"my key is {SECRET}"},
        {"type": "image_url", "image_url": {"url": IMAGE}}]}]

    try:
        await activity.execute_chat(FakeHass(), provider, messages,
                                    role="vision", data_category="vision")
    except Exception:
        assert fail

    assert len(captured) == 1
    args, kwargs = captured[0]
    values = list(args) + list(kwargs.values())
    for value in values:
        assert value is None or isinstance(value, (str, int, bool)), type(value)
        text = str(value)
        for forbidden in (SECRET, REASONING, "SECRETIMAGE", "visible answer", "Bearer"):
            assert forbidden not in text
    assert args[:6] == ("openai", "gpt-4o", "vision", "cloud", "vision", not fail)


def test_recorder_signature_accepts_no_content(load):
    import inspect
    pa = load("provider_activity")
    params = set(inspect.signature(pa.record).parameters)
    assert params == {"provider", "model", "role", "location", "data_category", "success",
                      "input_tokens", "output_tokens", "latency_ms", "day", "db_path"}


async def test_normalized_response_never_exposes_hidden_reasoning(load, monkeypatch):
    activity, _captured = _capture_recorder(load, monkeypatch)
    response = await activity.execute_chat(
        FakeHass(), _LeakyProvider(), [{"role": "user", "content": "hi"}],
        role="llm", data_category="text")
    legacy = response.to_legacy()
    assert response.text == "visible answer"
    assert REASONING not in repr(legacy)
    assert SECRET not in repr(legacy["raw"])     # a foreign raw is dropped
    assert "headers" not in repr(legacy["raw"])
    assert SECRET not in repr(response)
