"""Model tests for the first run setup screens.

Each chosen model gets a short message. The vision model also gets a small
test picture, because model names do not show whether a model reads
pictures. Results are Nova's ProviderErrorKind values, so the setup screen
can show a message that matches the cause. Different providers are tested
at the same time; models on one Ollama server one after another, because a
home GPU server may not hold several models in memory at once.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# A 16 by 16 plain red PNG, built in code, so no real camera data is sent.
TEST_PICTURE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mO4o6FBEmIY"
    "1TCqYfhqAAAyBCwQhCQ/2gAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class Job:
    provider: str
    model: str
    picture: bool = False


def _messages(picture: bool) -> list[dict]:
    if not picture:
        return [{"role": "user", "content": "Reply with the word pong."}]
    return [{"role": "user", "content": [
        {"type": "text", "text": "What colour is this picture? Reply with one word."},
        {"type": "image_url", "image_url": {"url": TEST_PICTURE}},
    ]}]


def _failed(job: Job, kind: str, reason: object) -> str:
    """Log why a test failed (the setup screen points the user here), and
    return the kind."""
    _LOGGER.warning("Nova setup: %s model %s failed its test: %s (%s)",
                    job.provider, job.model, kind, reason)
    return kind


async def probe_model(hass, job: Job, api_key: str, base_url: Optional[str]) -> Optional[str]:
    """Test one model. None when it passed, else a ProviderErrorKind value.
    Every failure is logged. Never raises, except to let task cancellation
    through."""
    from . import llm_provider
    from .providers import activity, manager
    from .providers.destinations import check_url
    from .providers.errors import (
        ProviderError, ProviderErrorKind, normalize_error, rejects_images)

    if job.provider == "ollama" and base_url:
        try:
            await hass.async_add_executor_job(
                lambda: check_url(str(base_url), resolve=True))
        except ProviderError as exc:
            return _failed(job, exc.kind.value, exc)
    try:
        client = await hass.async_add_executor_job(
            llm_provider.create_provider, job.provider, api_key, job.model,
            base_url or None)
    except Exception as exc:  # noqa: BLE001 - reported as a kind
        err = normalize_error(exc, job.provider)
        return _failed(job, err.kind.value, err)
    try:
        result = await activity.execute_chat(
            hass, client, _messages(job.picture),
            role="connection_test",
            data_category="vision" if job.picture else "text",
            # Room for reasoning models, which think before answering.
            tools=None, max_tokens=256, temperature=0.0)
        if not (result.text or "").strip():
            return _failed(job, ProviderErrorKind.MALFORMED_RESPONSE.value, "empty reply")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a kind
        if job.picture and rejects_images(exc):
            return _failed(job, ProviderErrorKind.UNSUPPORTED_CAPABILITY.value, exc)
        err = normalize_error(exc, job.provider)
        return _failed(job, err.kind.value, err)
    finally:
        await manager.async_close_client(hass, client)


async def probe_all(hass, jobs: dict[str, Job],
                    creds: dict[str, tuple[str, Optional[str]]]) -> dict[str, Optional[str]]:
    """Test every role's model once per distinct job. `creds` maps a
    provider to (api key, base url). Returns role -> result."""
    unique = sorted(set(jobs.values()), key=lambda j: (j.provider, j.model, j.picture))
    by_provider: dict[str, list[Job]] = {}
    for job in unique:
        by_provider.setdefault(job.provider, []).append(job)
    outcome: dict[Job, Optional[str]] = {}

    async def run(provider: str, provider_jobs: list[Job]) -> None:
        api_key, base_url = creds[provider]
        if provider == "ollama":
            for job in provider_jobs:
                outcome[job] = await probe_model(hass, job, api_key, base_url)
            return
        results = await asyncio.gather(
            *(probe_model(hass, job, api_key, base_url) for job in provider_jobs))
        outcome.update(zip(provider_jobs, results))

    await asyncio.gather(*(run(p, js) for p, js in by_provider.items()))
    return {role: outcome[job] for role, job in jobs.items()}
