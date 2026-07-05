"""Shared shape-validation predicate for operator-supplied service URLs.

One source of truth for "is this string a plausible http(s) URL", used by BOTH
the setup wizard's live "Test connection" probes (``setup_validation``) and the
write-time schema validators on ``SettingsUpdate`` / ``SetupCompleteRequest``
(``web.schemas``) -- so a malformed URL is rejected with the same message and
the same edge cases whether it is caught before an outbound probe or before a
row is ever written.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["INVALID_URL_MESSAGE", "url_shape_error"]

INVALID_URL_MESSAGE = "Enter a valid http(s) URL."


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

    Raw control characters (C0 + DEL) are rejected up front, BEFORE parsing or any
    log/probe: ``urlsplit`` silently tolerates or strips some of them
    (``http://\\nplex.local`` parses to a plausible host), but httpx then raises
    the same uncaught ``httpx.InvalidURL`` for the non-printable byte. A CR/LF- or
    NUL-bearing URL is exactly "obviously-broken input", so it gets the honest
    rejection here rather than a 500 (and never reaches an outbound request).
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
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
    return None
