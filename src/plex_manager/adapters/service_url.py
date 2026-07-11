"""Origin-confined URL construction for operator-configured HTTP services.

Plex Manager must support Plex, Prowlarr, and qBittorrent on loopback, private
LANs, Docker DNS names, and reverse-proxy path prefixes.  A blanket
"public-addresses only" SSRF policy would therefore break the product.  The
security boundary for these *configured services* is instead:

* parse and validate the base once;
* reject URL userinfo, query/fragment state, and browser-style backslashes;
* accept only server-owned relative endpoint paths; and
* compose endpoints by replacing the path on the parsed URL, so an endpoint can
  never change the configured scheme, host, or port.

The web layer still owns friendly request-validation errors.  This module is the
adapter-side, fail-closed boundary as well: it protects legacy/corrupt settings
and makes it impossible for an adapter to return to raw string concatenation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx

__all__ = ["InvalidServiceUrl", "ServiceUrl", "same_service_base"]


class InvalidServiceUrl(ValueError):
    """A configured base URL or adapter-owned endpoint path is unsafe."""


# This full-match is intentionally load-bearing.  Besides rejecting URL
# delimiters that can reinterpret the authority/path, it gives CodeQL's SSRF
# analysis an explicit string-restriction guard at the adapter trust boundary.
# The more detailed parser checks below remain the runtime authority.
_SERVICE_URL_PATTERN: Final = (
    r"(?i:https?)://"
    r"(?:\[[0-9A-Fa-f:.]+\]|[^\[\]/:@%?#\\\s\x00-\x1f\x7f]+)"
    r"(?::[0-9]{1,5})?"
    r"(?:/(?!\.{1,2}(?:/|$))[A-Za-z0-9._~-]+)*/?"
)

# Adapter endpoints are paths only: no scheme/authority/query/fragment, percent
# escapes, backslashes, or control characters.  Current Plex/qBittorrent API
# paths use this deliberately small alphabet, including their numeric dynamic
# section/rating keys.
_ENDPOINT_PATH_PATTERN: Final = r"/[A-Za-z0-9._~/-]*"


@dataclass(frozen=True, slots=True)
class ServiceUrl:
    """A validated service base whose generated endpoints stay on one origin."""

    _url: httpx.URL
    _base: str

    @classmethod
    def parse(cls, value: str) -> ServiceUrl:
        """Validate and normalize an operator-configured HTTP(S) base URL."""
        # Keep the success path explicit: CodeQL models the positive branch of
        # re.fullmatch as an SSRF sanitizer, while the parser checks below make
        # the same restriction authoritative at runtime.
        if not re.fullmatch(_SERVICE_URL_PATTERN, value):
            raise InvalidServiceUrl("invalid configured service URL")
        try:
            split = urlsplit(value)
            raw_path = split.path
            split_port = split.port
            parsed = httpx.URL(value)
            host = parsed.host
        except Exception as exc:
            # httpx.InvalidURL is not the only possible parser failure: invalid
            # IDNA A-labels can raise from the lazy ``host`` property.
            raise InvalidServiceUrl("invalid configured service URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or split_port == 0
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidServiceUrl("invalid configured service URL")

        # Reject path forms that an intermediary could normalize outside the
        # configured reverse-proxy prefix. Percent escapes were already excluded
        # by the allowlist, so this raw-path check closes literal dot/empty segments.
        if "//" in raw_path or any(part in {".", ".."} for part in raw_path.split("/")):
            raise InvalidServiceUrl("invalid configured service URL")

        # httpx normally removes an explicit default port, but an uppercase input
        # scheme can retain it during parsing. Normalize it deliberately so base
        # equality is stable across harmless spelling differences.
        default_port = 443 if parsed.scheme == "https" else 80
        normalized_port = None if split_port in {None, default_port} else split_port
        parsed = parsed.copy_with(port=normalized_port)

        # Preserve a legitimate reverse-proxy prefix and its percent encoding,
        # while making trailing-slash variants one canonical base.
        base = str(parsed).rstrip("/")
        return cls(_url=httpx.URL(base), _base=base)

    @property
    def base(self) -> str:
        """Canonical base URL, without a trailing slash."""
        return self._base

    @property
    def host(self) -> str:
        """Normalized hostname, safe for non-secret diagnostics."""
        return self._url.host

    @property
    def origin(self) -> tuple[str, str, int]:
        """Normalized ``(scheme, host, effective_port)`` origin."""
        default_port = 443 if self._url.scheme == "https" else 80
        return self._url.scheme, self.host.casefold(), self._url.port or default_port

    def endpoint(self, path: str) -> httpx.URL:
        """Append one adapter-owned absolute path beneath the configured prefix.

        ``path`` is deliberately not a general URL.  Building with
        :meth:`httpx.URL.copy_with` retains the already-validated scheme,
        authority, and port by construction; only the path component changes.
        """
        if not re.fullmatch(_ENDPOINT_PATH_PATTERN, path):
            raise InvalidServiceUrl("invalid service endpoint path")
        if "//" in path or any(part in {".", ".."} for part in path.split("/")):
            raise InvalidServiceUrl("invalid service endpoint path")
        prefix = self._url.raw_path.rstrip(b"/")
        raw_path = prefix + path.encode("ascii")
        return self._url.copy_with(raw_path=raw_path)


def same_service_base(left: str, right: str) -> bool:
    """Whether two service URLs normalize to the exact same configured base.

    Host case, default ports, and a trailing slash normalize away.  A different
    reverse-proxy prefix does *not*: two paths on one origin can route to different
    services, so stored credentials may not silently cross that boundary.
    Invalid/legacy values fail closed as different bases.
    """
    try:
        return ServiceUrl.parse(left).base == ServiceUrl.parse(right).base
    except InvalidServiceUrl:
        return False
