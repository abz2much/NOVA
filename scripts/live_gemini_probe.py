#!/usr/bin/env python3
"""Live check of Nova's native Gemini adapter, with your own key.

It runs the real adapter, the same code Nova uses, against Google's API:

  1. the model list (what the Settings model dropdown reads);
  2. a plain reply;
  3. a tool call round trip: the model asks for a tool, the result goes back,
     and the model answers (this is the thought signature check);
  4. a picture description (a small generated picture, so no file is needed);
  5. a wrong key, which must come back as an authentication failure.

The key comes from the environment and is never printed or saved. Run it with
the integration venv, which has Home Assistant installed:

  NOVA_LIVE_GEMINI_KEY=... .venv-integration/bin/python scripts/live_gemini_probe.py

Optional: NOVA_LIVE_GEMINI_MODEL (default is Nova's default Gemini model).
Each step prints passed or FAILED with Nova's own error kind and message. A
failure also prints the type of the underlying error, never its text.
Exit code: 0 all passed, 1 something failed, 2 no key set.
"""
from __future__ import annotations

import asyncio
import base64
import os
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.nova.llm_provider import DEFAULT_MODELS, create_provider  # noqa: E402
from custom_components.nova.providers import discovery  # noqa: E402
from custom_components.nova.providers.activity import execute_chat  # noqa: E402
from custom_components.nova.providers.errors import ProviderError  # noqa: E402


class _Hass:
    """Just enough of Home Assistant: blocking work runs in a thread. It has
    no config folder, so Nova's activity recorder writes nothing."""

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (220, 30, 30)) -> str:
    """A solid colour PNG as a data URL."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    raw = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(row * height))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(raw).decode()


_TOOL = {"type": "function", "function": {
    "name": "set_light", "description": "Turn a light on or off.",
    "parameters": {"type": "object", "properties": {
        "room": {"type": "string"}, "on": {"type": "boolean"}},
        "required": ["room", "on"]}}}


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{name:34} {'passed' if ok else 'FAILED'}{('  ' + detail) if detail else ''}")
    return ok


def _error(exc: Exception) -> str:
    cause = type(exc.__cause__).__name__ if getattr(exc, "__cause__", None) else "none"
    return f"{exc}  [cause: {cause}]"


async def _model_list(key: str, model: str) -> bool:
    try:
        import aiohttp
    except ImportError:
        return _report("model list", False, "aiohttp is not installed")
    try:
        request = discovery.resolve_discovery_request({"gemini_api_key": key}, "gemini")
        async with aiohttp.ClientSession() as session:
            result = await discovery.fetch_models(_Hass(), session, request)
    except Exception as exc:  # noqa: BLE001 - reported
        return _report("model list", False, _error(exc) if isinstance(exc, ProviderError) else type(exc).__name__)
    found = model in result.models
    return _report("model list", bool(result.models),
                   f"{len(result.models)} models; {model} {'listed' if found else 'NOT listed'}")


async def _plain(provider) -> bool:
    try:
        out = await execute_chat(_Hass(), provider,
                                 [{"role": "user", "content": "Reply with the word pong."}],
                                 role="connection_test", data_category="text", max_tokens=32)
    except ProviderError as exc:
        return _report("plain reply", False, _error(exc))
    return _report("plain reply", bool(out.text.strip()),
                   f"{out.text.strip()[:40]!r}; tokens in/out {out.usage.input_tokens}/{out.usage.output_tokens}")


async def _tool_round_trip(provider) -> bool:
    messages = [
        {"role": "system", "content": "You control a home. Use the tools you are given."},
        {"role": "user", "content": "Turn on the kitchen light, then tell me it is done."}]
    try:
        first = await execute_chat(_Hass(), provider, messages, role="llm", data_category="text",
                                   tools=[_TOOL], max_tokens=200)
        if not first.tool_calls:
            return _report("tool call round trip", False, "the model did not call the tool")
        call = first.tool_calls[0]
        steps = (call.extra_content or {}).get("google", {}).get("steps", [])
        kinds = [s.get("type") for s in steps]
        history = messages + [first.assistant_message(),
                              {"role": "tool", "tool_call_id": call.id, "name": call.name,
                               "content": '{"result": "ok"}'}]
        second = await execute_chat(_Hass(), provider, history, role="llm", data_category="text",
                                    tools=[_TOOL], max_tokens=200)
    except ProviderError as exc:
        return _report("tool call round trip", False, _error(exc))
    return _report("tool call round trip", bool(second.text.strip() or second.tool_calls),
                   f"called {call.name}; recorded steps {kinds}; answer {second.text.strip()[:40]!r}")


async def _picture(provider) -> bool:
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _png()}},
        {"type": "text", "text": "What colour is this picture? One word."}]}]
    try:
        out = await execute_chat(_Hass(), provider, messages, role="vision",
                                 data_category="vision", max_tokens=40)
    except ProviderError as exc:
        return _report("picture", False, _error(exc))
    return _report("picture", bool(out.text.strip()), f"{out.text.strip()[:40]!r}")


async def _wrong_key(model: str) -> bool:
    bad = create_provider("gemini", "AIza-not-a-real-key-0000000000000000000", model)
    try:
        await execute_chat(_Hass(), bad, [{"role": "user", "content": "hi"}],
                           role="connection_test", data_category="text", max_tokens=8)
    except ProviderError as exc:
        return _report("wrong key", exc.kind.value == "authentication_failed", f"got {exc.kind.value}: {exc}")
    return _report("wrong key", False, "the call succeeded with a wrong key")


async def main() -> int:
    key = os.environ.get("NOVA_LIVE_GEMINI_KEY", "").strip()
    if not key:
        print("No key set. See the usage at the top of this file.")
        return 2
    model = os.environ.get("NOVA_LIVE_GEMINI_MODEL", "").strip() or DEFAULT_MODELS["gemini"]
    print(f"Gemini model: {model}\n")
    provider = create_provider("gemini", key, model)
    results = [
        await _model_list(key, model),
        await _plain(provider),
        await _tool_round_trip(provider),
        await _picture(provider),
        await _wrong_key(model),
    ]
    print(f"\n{sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
