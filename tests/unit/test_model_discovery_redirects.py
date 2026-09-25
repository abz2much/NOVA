"""Destination and redirect protections for model discovery and the
endpoint-test command (providers.discovery). A fake aiohttp session records
every request; nothing leaves the process."""
from __future__ import annotations

import json

import pytest

from fakes import FakeHass


class _Content:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Maps URL -> response; records (url, headers, allow_redirects)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, *, headers=None, params=None, allow_redirects=True):
        self.calls.append((url, dict(headers or {}), allow_redirects))
        route = self.routes[url]
        return route() if callable(route) else route


def _json(data):
    return _Resp(200, json.dumps(data).encode())


def _redirect(location, status=302):
    return _Resp(status, b"", {"Location": location})


@pytest.fixture
def d(load):
    return load("providers.discovery")


@pytest.fixture
def errors(d):
    import sys
    return sys.modules["jc.providers.errors"]


def _custom(d, url="http://192.168.1.20:8000/v1", key="own-secret"):
    return d.resolve_discovery_request(
        {"custom_base_url": url, "custom_api_key": key}, "custom")


LAN_MODELS = "http://192.168.1.20:8000/v1/models"


@pytest.mark.asyncio
async def test_lan_endpoint_keeps_working_with_its_own_credential(d):
    session = _Session({LAN_MODELS: _json({"data": [{"id": "b"}, {"id": "a"}]})})
    result = await d.fetch_models(FakeHass(), session, _custom(d))
    assert result.models == ("a", "b")
    url, headers, follow = session.calls[0]
    assert headers == {"Authorization": "Bearer own-secret"}
    assert follow is False          # redirects are followed by hand, checked


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.170.2/v2/credentials",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://[fe80::1]/models",
])
async def test_redirect_to_metadata_or_link_local_is_refused(d, errors, target):
    session = _Session({LAN_MODELS: _redirect(target)})
    with pytest.raises(errors.ProviderError) as info:
        await d.fetch_models(FakeHass(), session, _custom(d))
    assert info.value.kind is errors.ProviderErrorKind.INVALID_ENDPOINT
    assert [c[0] for c in session.calls] == [LAN_MODELS]   # never contacted


@pytest.mark.asyncio
async def test_metadata_destination_is_refused_before_any_request(d, errors):
    request = _custom(d, url="http://169.254.169.254/v1")
    session = _Session({})
    with pytest.raises(errors.ProviderError):
        await d.fetch_models(FakeHass(), session, request)
    assert session.calls == []


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_the_credential(d):
    other = "http://10.0.0.5:9000/v1/models"
    session = _Session({LAN_MODELS: _redirect(other, 307),
                        other: _json({"data": [{"id": "m"}]})})
    result = await d.fetch_models(FakeHass(), session, _custom(d))
    assert result.models == ("m",)
    assert session.calls[0][1] == {"Authorization": "Bearer own-secret"}
    assert session.calls[1][0] == other
    assert session.calls[1][1] == {}


@pytest.mark.asyncio
async def test_same_origin_redirect_keeps_the_credential(d):
    moved = "http://192.168.1.20:8000/api/v1/models"
    session = _Session({LAN_MODELS: _redirect("/api/v1/models", 308),
                        moved: _json({"data": [{"id": "m"}]})})
    await d.fetch_models(FakeHass(), session, _custom(d))
    assert session.calls[1] == (moved, {"Authorization": "Bearer own-secret"}, False)


@pytest.mark.asyncio
async def test_fixed_cloud_destination_cannot_be_redirected_away(d, errors):
    request = d.resolve_discovery_request({"groq_api_key": "gsk-secret"}, "groq")
    session = _Session({request.url: _redirect("https://attacker.invalid/models")})
    with pytest.raises(errors.ProviderError) as info:
        await d.fetch_models(FakeHass(), session, request)
    assert info.value.kind is errors.ProviderErrorKind.INVALID_ENDPOINT
    assert len(session.calls) == 1
    assert "gsk-secret" not in str(info.value)


@pytest.mark.asyncio
async def test_redirect_loops_are_bounded(d, errors):
    session = _Session({LAN_MODELS: _redirect(LAN_MODELS)})
    with pytest.raises(errors.ProviderError):
        await d.fetch_models(FakeHass(), session, _custom(d))
    assert len(session.calls) == d.MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_oversized_response_is_refused(d, errors, monkeypatch):
    monkeypatch.setattr(d, "MAX_RESPONSE_BYTES", 1024)
    body = json.dumps({"data": [{"id": "x" * 100}] * 50}).encode()
    session = _Session({LAN_MODELS: _Resp(200, body)})
    with pytest.raises(errors.ProviderError) as info:
        await d.fetch_models(FakeHass(), session, _custom(d))
    assert info.value.kind is errors.ProviderErrorKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_non_json_response_is_malformed_not_leaked(d, errors):
    session = _Session({LAN_MODELS: _Resp(200, b"<html>secret page</html>")})
    with pytest.raises(errors.ProviderError) as info:
        await d.fetch_models(FakeHass(), session, _custom(d))
    assert "secret page" not in str(info.value)


@pytest.mark.asyncio
async def test_http_error_status_is_reported_without_body(d):
    session = _Session({LAN_MODELS: _Resp(401, b"token sk-leak invalid")})
    with pytest.raises(d.ModelDiscoveryHTTPError) as info:
        await d.fetch_models(FakeHass(), session, _custom(d))
    assert info.value.status == 401
    assert "sk-leak" not in str(info.value)


@pytest.mark.asyncio
async def test_pagination_is_bounded_and_reports_truncation(d):
    request = d.resolve_discovery_request({"gemini_api_key": "k"}, "gemini")
    page = {"models": [{"name": "models/g", "supportedGenerationMethods": []}],
            "nextPageToken": "more"}
    session = _Session({request.url: lambda: _json(page)})
    result = await d.fetch_models(FakeHass(), session, request)
    assert result.truncated is True
    assert len(session.calls) == d.MAX_DISCOVERY_PAGES
