"""Shared shape-validation predicate for operator-supplied service URLs.

One source of truth for "is this string a plausible http(s) URL", used by BOTH
the setup wizard's live "Test connection" probes (``setup_validation``) and the
write-time schema validators on ``SettingsUpdate`` / ``SetupCompleteRequest``
(``web.schemas``) -- so a malformed URL is rejected with the same message and
the same edge cases whether it is caught before an outbound probe or before a
row is ever written.
"""

from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address
from urllib.parse import urlsplit

__all__ = [
    "INVALID_IPV4_MESSAGE",
    "INVALID_URL_MESSAGE",
    "QUERY_FRAGMENT_MESSAGE",
    "url_shape_error",
]

INVALID_URL_MESSAGE = "Enter a valid http(s) URL."
QUERY_FRAGMENT_MESSAGE = "Base URL must not contain a query or fragment."
INVALID_IPV4_MESSAGE = "Invalid IPv4 address in host."

# A hostname made of ONLY digits and dots is IPv4-shaped: it can never be a real
# DNS name (a resolvable TLD is never all-numeric), so it must parse as a proper
# dotted quad. DNS names and IPv6 literals contain other characters and skip this.
_IPV4_SHAPED_CHARS = frozenset("0123456789.")


def url_shape_error(url: str) -> str | None:
    """Return an error message if ``url`` is not a plausible http(s) URL, else ``None``.

    This is honest input hygiene, NOT a claimed SSRF sanitizer: it narrows the
    scheme to ``http``/``https`` and requires a hostname, but the host/port/path
    itself is still fully operator-controlled by design (these are URLs for an
    operator-supplied, usually-private service -- see the SSRF risk-acceptance
    note on alert #247). Its job is only to turn an obviously-broken input
    (``file://...``, a scheme-less string, an empty host) into a clear,
    retryable rejection instead of an opaque ``httpx`` transport error (at probe
    time) or a confusing downstream failure (at write time). Returns ``None``
    when ``url`` is acceptable.

    ``urlsplit`` (and reading ``.hostname`` / ``.port``) itself RAISES
    ``ValueError`` on several obviously-broken inputs, all of which are guarded so
    a parse failure surfaces as the same rejection rather than crashing the
    caller with an uncaught exception:

    * a malformed bracketed host -- an unterminated IPv6 literal (``http://[::1``)
      or an invalid IPvFuture form (``http://[v7.x]``) -- trips ``.hostname``;
    * a non-numeric (``http://x:bad``) or out-of-range (``http://x:99999``) port
      trips ``.port``. Without touching ``.port`` these slip past the hostname
      check and reach httpx, which raises ``httpx.InvalidURL`` -- and that is NOT
      an ``httpx.HTTPError`` subclass, so it would escape the endpoints' transport
      handlers as a 500 instead of this rejection.

    Raw control characters (C0 + DEL) AND any whitespace are rejected up front,
    BEFORE parsing or any log/probe: ``urlsplit`` silently tolerates or strips some
    of them (``http://\\nplex.local`` parses to a plausible host, and
    ``http://plex local`` still yields a hostname), but a base service URL never
    legitimately contains a control byte or a space -- adapters use the raw string
    as the base URL, so a CR/LF- or NUL-bearing URL raises the same uncaught
    ``httpx.InvalidURL`` and a space-bearing authority fails at request time. Both
    are exactly "obviously-broken input", so they get the honest rejection here
    rather than a 500 or a doomed outbound request. (The control-char guard already
    covers the ``\\t``/``\\r``/``\\n``-class chars; the whitespace guard extends it
    to the plain space and other Unicode whitespace those miss.)

    A raw ``?`` or ``#`` ANYWHERE in the value is likewise rejected: these are
    BASE service URLs onto which the adapters append their own API paths, so a
    query/fragment would swallow the appended path and send requests to the wrong
    endpoint. The check is on the raw characters, not ``parts.query`` /
    ``parts.fragment``, because a BARE trailing delimiter (``http://x?``,
    ``http://x#``) splits to an EMPTY query/fragment yet the raw string -- which
    is what the adapters use -- still carries it and breaks path-appending
    identically. Both characters are reserved delimiters in every URL component
    (a legitimate base URL could only carry them percent-encoded), so one honest
    rule covers every case. A base URL may still carry a (path-prefix) path --
    e.g. a reverse-proxy ``http://host:9696/prowlarr`` or a bare trailing slash.

    An IPv4-SHAPED hostname (digits and dots only) must parse as a real dotted
    quad: ``urlsplit`` happily returns ``999.999.999.999`` or ``01.02.03.04`` as
    a hostname, but httpx's own URL parser rejects them at request time -- so the
    value would persist and then fail at runtime instead of 422ing here.
    ``ipaddress.IPv4Address`` correctly rejects out-of-range octets AND
    leading-zero octets. DNS names and IPv6 literals contain non-``[0-9.]``
    characters and are deliberately NOT validated beyond the existing checks.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in url):
        return INVALID_URL_MESSAGE
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        # Reading ``.port`` validates it -- urllib raises ValueError for a
        # non-numeric or out-of-range port, which we reject rather than let httpx
        # turn into an uncaught InvalidURL (or a doomed connect attempt).
        port = parts.port
    except ValueError:
        return INVALID_URL_MESSAGE
    # Port 0 parses cleanly but is never connectable -- reject it up front too.
    if parts.scheme not in {"http", "https"} or not hostname or port == 0:
        return INVALID_URL_MESSAGE
    # A base URL keeps its (optional path-prefix) path, but a '?' or '#' anywhere
    # would swallow the adapter-appended API path -- checked on the RAW value, not
    # parts.query/parts.fragment, so a bare trailing delimiter ("http://x?",
    # which splits to an EMPTY query) is rejected too.
    if "?" in url or "#" in url:
        return QUERY_FRAGMENT_MESSAGE
    # An IPv4-shaped host must be a real dotted quad -- urlsplit accepts
    # "999.999.999.999" / "01.02.03.04" but httpx rejects them at request time.
    if set(hostname) <= _IPV4_SHAPED_CHARS:
        try:
            IPv4Address(hostname)
        except AddressValueError:
            return INVALID_IPV4_MESSAGE
    return None
