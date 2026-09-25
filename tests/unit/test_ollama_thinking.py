"""Tests for reasoning-model handling on Ollama (v6.98.0).

gemma4:26b and other reasoning models put their thinking in a separate channel
and leave 'content' empty until it finishes — on a small token budget that means
an empty answer. Nova asks Ollama to skip thinking and answer directly.
"""
import re


def _src(name):
    with open(f"custom_components/nova/providers/{name}.py") as f:
        return f.read()


def test_ollama_extra_body_disables_thinking():
    src = _src("ollama")
    # The native Ollama request must send think=False
    ob = src[src.index("class OllamaProvider"):]
    ob = ob[:ob.index("\nclass ", 5)] if "\nclass " in ob[5:] else ob
    assert '"think": False' in ob, "OllamaProvider must disable thinking (think=False)"
    assert "num_ctx" in ob      # existing tuning preserved


def test_extra_body_is_applied_in_chat():
    src = _src("openai_compatible")
    # OpenAI-compatible chat still forwards provider extra_body to the request
    assert "extra_body" in src and "_extra_body()" in src


def test_briefing_gives_reasoning_room():
    with open("custom_components/nova/briefing.py") as f:
        brief = f.read()
    # budget large enough for a reasoning model to think AND answer
    m = re.search(r"max_tokens=(\d+),\s*\n\s*temperature=0\.6", brief)
    assert m and int(m.group(1)) >= 1200, "briefing needs a larger token budget"
