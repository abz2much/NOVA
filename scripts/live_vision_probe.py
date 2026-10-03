#!/usr/bin/env python3
"""Live check of the first run model tests, with your own keys.

For each provider you give a key for, it runs the text test on Nova's
default model and the picture test on the vision default. For Ollama it
also prints the abilities your server reports for each model, which first
run setup uses to fill in models.

Keys come from the environment and are never printed or saved. Run it with
the integration venv, which has the provider SDKs:

  NOVA_LIVE_GROQ_KEY=... NOVA_LIVE_GEMINI_KEY=... \\
    .venv-integration/bin/python scripts/live_vision_probe.py

Also: NOVA_LIVE_OPENAI_KEY, NOVA_LIVE_ANTHROPIC_KEY, and for Ollama
NOVA_LIVE_OLLAMA_URL, NOVA_LIVE_OLLAMA_TEXT_MODEL and
NOVA_LIVE_OLLAMA_VISION_MODEL (for example llama3.2 and llava).
Exit code: 0 all passed, 1 something failed, 2 nothing was set.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.nova import setup_probe  # noqa: E402
from custom_components.nova.const import DEFAULT_VISION_MODEL  # noqa: E402
from custom_components.nova.llm_provider import DEFAULT_MODELS  # noqa: E402


class _Hass:
    """Just enough of Home Assistant for the probe: run blocking work in a
    thread. It has no config folder, so Nova's activity recorder writes
    nothing."""

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _cases() -> list[tuple[str, str, str | None, str, bool]]:
    """(provider, key, base url, model, picture)."""
    cases = []
    for provider, env in (("groq", "NOVA_LIVE_GROQ_KEY"), ("openai", "NOVA_LIVE_OPENAI_KEY"),
                          ("gemini", "NOVA_LIVE_GEMINI_KEY"),
                          ("anthropic", "NOVA_LIVE_ANTHROPIC_KEY")):
        key = os.environ.get(env, "").strip()
        if not key:
            continue
        cases.append((provider, key, None, DEFAULT_MODELS[provider], False))
        vision = DEFAULT_VISION_MODEL if provider == "groq" else DEFAULT_MODELS[provider]
        cases.append((provider, key, None, vision, True))
    url = os.environ.get("NOVA_LIVE_OLLAMA_URL", "").strip()
    if url:
        for env, picture in (("NOVA_LIVE_OLLAMA_TEXT_MODEL", False),
                             ("NOVA_LIVE_OLLAMA_VISION_MODEL", True)):
            model = os.environ.get(env, "").strip()
            if model:
                cases.append(("ollama", "", url, model, picture))
    return cases


def _print_ollama_abilities(url: str) -> bool:
    """Print what the server reports in /api/tags, which setup reads.
    Returns False when the list could not be read."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=10) as resp:
            models = json.load(resp).get("models", [])
    except Exception as exc:  # noqa: BLE001 - report and carry on
        print(f"Ollama: could not read /api/tags ({type(exc).__name__})")
        return False
    print("Ollama abilities as reported by /api/tags:")
    for m in models:
        caps = m.get("capabilities")
        print(f"  {m.get('name', '?'):40} {caps if caps else 'NONE REPORTED'}")
    return True


async def main() -> int:
    cases = _cases()
    url = os.environ.get("NOVA_LIVE_OLLAMA_URL", "").strip()
    if not cases and not url:
        print("No provider set. See the usage at the top of this file.")
        return 2
    failed = 0
    if url and not _print_ollama_abilities(url):
        failed += 1
    for provider, key, base, model, picture in cases:
        job = setup_probe.Job(provider, model, picture=picture)
        result = await setup_probe.probe_model(_Hass(), job, key, base)
        test = "picture" if picture else "text"
        print(f"{provider:10} {test:8} {model:30} "
              f"{'passed' if result is None else 'FAILED: ' + result}")
        failed += result is not None
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
