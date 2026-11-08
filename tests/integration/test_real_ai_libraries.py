"""Nova's providers driven through the REAL, newest AI libraries (8.85.x).

The unit tests use stand-in clients, so when anthropic 2.x started refusing
`temperature` (HA 2026.11) nothing failed until every Anthropic call in a
real home did. Here Nova's own provider code talks through the installed
anthropic, openai and groq packages (CI installs the newest each run) to a
small fake server on 127.0.0.1: no keys, no internet, no cost. A library
change that breaks Nova fails here first.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# Sockets on, the way Home Assistant's own web-client test fixtures do it.
# The test package still allows only 127.0.0.1, so nothing reaches the internet.
pytestmark = pytest.mark.usefixtures("socket_enabled")

_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQV"
        "R42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_TOOLS = [{"type": "function", "function": {
    "name": "energy_report", "description": "Today's energy totals.",
    "parameters": {"type": "object", "properties": {"day": {"type": "string"}}}}}]


class _FakeAI:
    """Answers Anthropic's /v1/messages and OpenAI-style /chat/completions,
    with plain text or one tool call, and keeps every request body."""

    def __init__(self):
        self.tool = False
        self.seen: list[dict] = []
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.seen.append({"path": self.path, "body": body})
                reply = fake._anthropic(body) if self.path.endswith("/messages") else fake._openai(body)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def _anthropic(self, body):
        content = [{"type": "text", "text": "hello"}]
        if self.tool:
            content.append({"type": "tool_use", "id": "toolu_1", "name": "energy_report",
                            "input": {"day": "today"}})
        return {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": content, "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2}}

    def _openai(self, body):
        message = {"role": "assistant", "content": "hello"}
        if self.tool:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "energy_report", "arguments": json.dumps({"day": "today"})}}]}
        return {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if self.tool else "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(2)


@pytest.fixture
def fake_ai():
    fake = _FakeAI()
    yield fake
    fake.close()


def _provider(name, fake):
    from custom_components.nova.providers import routing
    pytest.importorskip({"anthropic": "anthropic", "openai": "openai", "groq": "groq"}[name])
    base = {"anthropic": fake.url, "openai": f"{fake.url}/v1", "groq": fake.url}[name]
    return routing.create_provider(name, "test-key", "test-model", base_url=base)


def _ask(provider, messages, tools=None):
    from custom_components.nova.providers.models import ChatRequest
    return provider.run(ChatRequest.from_legacy(messages, tools=tools, max_tokens=64,
                                                temperature=0.3))


@pytest.mark.parametrize("name", ["anthropic", "openai", "groq"])
async def test_a_plain_answer(hass, fake_ai, name):
    provider = await hass.async_add_executor_job(_provider, name, fake_ai)
    reply = await hass.async_add_executor_job(
        _ask, provider, [{"role": "system", "content": "s"},
                         {"role": "user", "content": "what did power cost today"}])
    assert reply.text == "hello"


@pytest.mark.parametrize("name", ["anthropic", "openai", "groq"])
async def test_a_tool_call(hass, fake_ai, name):
    fake_ai.tool = True
    provider = await hass.async_add_executor_job(_provider, name, fake_ai)
    reply = await hass.async_add_executor_job(
        _ask, provider, [{"role": "user", "content": "cost?"}], _TOOLS)
    assert [(c.name, c.args) for c in reply.tool_calls] == [("energy_report", {"day": "today"})]
    sent = fake_ai.seen[-1]["body"]
    assert [t.get("name") or t["function"]["name"] for t in sent["tools"]] == ["energy_report"]


@pytest.mark.parametrize("name", ["anthropic", "openai"])
async def test_an_image_is_sent(hass, fake_ai, name):
    provider = await hass.async_add_executor_job(_provider, name, fake_ai)
    reply = await hass.async_add_executor_job(
        _ask, provider, [{"role": "user", "content": [
            {"type": "text", "text": "who is at the door"},
            {"type": "image_url", "image_url": {"url": _PNG}}]}])
    assert reply.text == "hello"
    parts = fake_ai.seen[-1]["body"]["messages"][-1]["content"]
    assert any(p.get("type") in ("image", "image_url") for p in parts)
