"""Destination policy for administrator-configured endpoints.

Local and custom model servers may live anywhere on a private network, on
loopback or behind an mDNS name, and all of that keeps working. What is
refused is the class of addresses no model server has a reason to use and
that exposes host secrets when reached: link-local addresses (which include
every major cloud's instance-metadata service) and the well-known metadata
hostnames and addresses.

Redirects are validated hop by hop against the same policy, and a
credential is never forwarded to a different origin.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.request
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from .errors import ProviderError, ProviderErrorKind

MAX_REDIRECTS = 3

_METADATA_HOSTNAMES = frozenset({
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "instance-data",
    "instance-data.ec2.internal",
})
_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS, Azure, GCP, OpenStack, ...
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IPv6
})

# Headers that carry a credential for any provider Nova talks to.
CREDENTIAL_HEADERS = frozenset({
    "authorization", "x-api-key", "x-goog-api-key", "api-key", "proxy-authorization",
})


def _blocked(error_detail: str = "the endpoint points at a blocked address") -> ProviderError:
    return ProviderError(ProviderErrorKind.INVALID_ENDPOINT, "endpoint", detail=error_detail)


def blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is a metadata or other link-local destination."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return (
        address in _METADATA_ADDRESSES
        or address.is_link_local
    )


def check_url(url: str, *, resolve: bool = False,
              resolver=socket.getaddrinfo) -> None:
    """Raise ProviderError(INVALID_ENDPOINT) when ``url`` is not an http(s)
    URL, names a metadata host, or (with ``resolve``) resolves to a blocked
    address. Private, loopback and LAN destinations are allowed."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise _blocked("the endpoint is not a valid URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _blocked("the endpoint must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise _blocked("the endpoint must not embed credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTNAMES:
        raise _blocked()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if blocked_address(literal):
            raise _blocked()
        return
    if not resolve:
        return
    try:
        infos = resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                         type=socket.SOCK_STREAM)
    except OSError:
        # Unresolvable here means unreachable too: the request itself will
        # fail to connect, so there is no address to refuse.
        return
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except (ValueError, IndexError, TypeError):
            continue
        if blocked_address(address):
            raise _blocked()


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, (parsed.hostname or "").lower(), port


def same_origin(a: str, b: str) -> bool:
    return origin(a) == origin(b)


def strip_credentials(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in CREDENTIAL_HEADERS}


def resolve_redirect(current_url: str, location: Optional[str]) -> str:
    if not location:
        raise ProviderError(ProviderErrorKind.INVALID_ENDPOINT, "endpoint",
                            detail="the endpoint sent a redirect without a location")
    return urljoin(current_url, location)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handling for local and custom endpoints: at most
    MAX_REDIRECTS hops, every target checked against the destination
    policy, and credentials dropped on any origin change."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, resolve: bool = True):
        super().__init__()
        self._resolve = resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, resolve=self._resolve)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not same_origin(req.full_url, newurl):
            for name in list(new.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new.headers[name]
            for name in list(new.unredirected_hdrs):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new.unredirected_hdrs[name]
        return new


def build_safe_opener(resolve: bool = True) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(SafeRedirectHandler(resolve=resolve))


def credential_header_names(headers: Iterable[str]) -> list[str]:
    return [h for h in headers if h.lower() in CREDENTIAL_HEADERS]
