"""classifier.py's LLM fallback now routes through llm_provider.chat_with_
activity() (Phase 5) instead of a bare hass.async_add_executor_job(provider.
chat, ...) call — proves the migration's wiring, not just chat_with_activity
in isolation (already covered by test_llm_provider_usage.py)."""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def classifier(load):
    return load("classifier")


def _install_fake_llm_provider(chat_with_activity_fn):
    fake = types.ModuleType("jc.llm_provider")
    fake.chat_with_activity = chat_with_activity_fn
    sys.modules["jc.llm_provider"] = fake
    sys.modules["jc"].llm_provider = fake

    def _cleanup():
        del sys.modules["jc.llm_provider"]
        del sys.modules["jc"].llm_provider
    return _cleanup


async def test_ambiguous_event_routes_through_chat_with_activity(classifier, fake_hass):
    calls = []

    async def _fake_chat_with_activity(hass, provider, messages, *, role, data_category, **kw):
        calls.append({"role": role, "data_category": data_category, "provider": provider})
        return {"text": '{"worth_considering": true, "urgency": "medium", "category": "security"}'}

    cleanup = _install_fake_llm_provider(_fake_chat_with_activity)
    try:
        provider = object()
        result = await classifier.classify(
            fake_hass, provider,
            entity_id="cover.awning", old_state="closed", new_state="open",
        )
    finally:
        cleanup()

    assert len(calls) == 1
    assert calls[0]["role"] == "classifier"
    assert calls[0]["data_category"] == "text"
    assert calls[0]["provider"] is provider
    assert result == {"worth_considering": True, "urgency": "medium", "category": "security"}


async def test_provider_exception_still_caught_and_returns_not_worth_considering(classifier, fake_hass):
    """The migration must not change classify()'s existing failure contract:
    a provider error still degrades to "not worth considering", never raises."""
    async def _boom(hass, provider, messages, **kw):
        raise RuntimeError("provider down")

    cleanup = _install_fake_llm_provider(_boom)
    try:
        result = await classifier.classify(
            fake_hass, object(),
            entity_id="cover.awning", old_state="closed", new_state="open",
        )
    finally:
        cleanup()

    assert result == {"worth_considering": False}
