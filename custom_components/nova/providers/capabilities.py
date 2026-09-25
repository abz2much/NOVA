"""Provider capabilities and concurrency policy.

Capabilities are what an adapter can accept for a given model. They keep the
long-standing name-based vision rules each provider already used; model
discovery never guesses capabilities (see discovery.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Capability


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    vision: bool
    tools: bool = True

    def supports(self, capability: Capability) -> bool:
        if capability is Capability.TEXT:
            return True
        if capability is Capability.VISION:
            return self.vision
        if capability is Capability.TOOLS:
            return self.tools
        return False


@dataclass(frozen=True, slots=True)
class ConcurrencyPolicy:
    """How many calls one adapter instance may have in flight at once.

    Nova never assumes an SDK client is safe to share across threads just
    because it usually is: each adapter states its limit, and the activity
    boundary enforces it with a per-client semaphore before handing the
    blocking call to the executor.
    """

    max_in_flight: int
    reason: str


# The SDK clients (groq, openai, anthropic) wrap one httpx.Client, which
# httpx documents as thread-safe with a shared connection pool. Nova still
# bounds concurrency so one busy role cannot exhaust that pool.
SDK_CLIENT_POLICY = ConcurrencyPolicy(
    max_in_flight=8,
    reason="shared SDK client over a thread-safe httpx connection pool",
)
# Native Ollama builds a fresh urllib request per call and keeps no shared
# connection state; the limit protects a single local GPU server.
OLLAMA_POLICY = ConcurrencyPolicy(
    max_in_flight=4,
    reason="stateless per-request urllib transport to one local server",
)


def groq_vision(model: str) -> bool:
    # Groq vision-capable models all contain 'vision' in the name.
    return "vision" in (model or "").lower()


_OPENAI_COMPATIBLE_VISION = (
    "gpt-4", "gpt-5", "llava", "vision",
    "gemini-2", "gemini-3", "gemini-flash", "gemini-pro",
)


def openai_compatible_vision(model: str) -> bool:
    # Modern OpenAI models all support vision; Ollama varies by model; most
    # Gemini models are multimodal; LLaVA supports vision locally.
    return any(v in (model or "").lower() for v in _OPENAI_COMPATIBLE_VISION)


def anthropic_vision(model: str) -> bool:
    return True  # all modern Claude models


def ollama_vision(model: str) -> bool:
    # The native adapter never claimed vision support; callers that need
    # images pick a model explicitly. Unchanged.
    return False
