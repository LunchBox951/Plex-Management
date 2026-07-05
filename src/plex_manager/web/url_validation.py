"""Shared shape-validation predicate for operator-supplied service URLs.

One source of truth for "is this string a plausible http(s) URL", used by BOTH
the setup wizard's live "Test connection" probes (``setup_validation``) and the
write-time schema validators on ``SettingsUpdate`` / ``SetupCompleteRequest``
(``web.schemas``) -- so a malformed URL is rejected with the same message and
the same edge cases whether it is caught before an outbound probe or before a
row is ever written.

Import hygiene note: this module imports ``httpx`` (for the final
parse-with-the-real-consumer gate in :func:`url_shape_error`). That is fine
HERE -- ``web/`` is an adapter layer where httpx is already an established
dependency (``setup_validation``'s live probes are built on it) -- but this
module must never migrate into ``domain/``, which is pure and imports no
adapter libraries.
"""

from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address, IPv6Address
from urllib.parse import urlsplit

import httpx

__all__ = [
    "INVALID_IPV4_MESSAGE",
    "INVALID_IPV6_MESSAGE",
    "INVALID_URL_MESSAGE",
    "IPV6_ZONE_ID_MESSAGE",
    "QUERY_FRAGMENT_MESSAGE",
    "UNPARSEABLE_URL_MESSAGE",
    "url_shape_error",
]

INVALID_URL_MESSAGE = "Enter a valid http(s) URL."
QUERY_FRAGMENT_MESSAGE = "Base URL must not contain a query or fragment."
INVALID_IPV4_MESSAGE = "Invalid IPv4 address in host."
INVALID_IPV6_MESSAGE = "Invalid IPv6 address in host."
# Deliberately a SEPARATE message from INVALID_IPV6_MESSAGE: a zone-bearing
# address ("fe80::1%eth0") IS valid IPv6, so calling it "invalid" would be
# dishonest -- the zone id is simply not supported in a base service URL.
IPV6_ZONE_ID_MESSAGE = "IPv6 zone ids are not supported in a base URL."
UNPARSEABLE_URL_MESSAGE = "URL is not parseable by the HTTP client."

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
    leading-zero octets. DNS names contain non-``[0-9.]`` characters and are
    deliberately NOT validated beyond the existing checks.

    A BRACKETED host must be a plain, zone-free IPv6 literal
    (``ipaddress.IPv6Address``, no scope id) -- mirroring the IPv4 branch. On
    current CPython ``urlsplit`` itself already rejects most invalid bracket
    content (``[::1::2]``, ``[gggg::1]``, ``[1.2.3.4]`` all raise ``ValueError``,
    caught above), so this closes the two forms it still tolerates:

    * an IPvFuture literal (``http://[v7.abc]``) passes ``urlsplit``'s regex but
      httpx raises the same uncaught ``httpx.InvalidURL`` at request time;
    * a zone id (``[fe80::1%eth0]``, or the RFC 6874 ``%25``-encoded form) parses
      everywhere, but is REJECTED BY POLICY with its own honest message: a
      link-local zone id is meaningless to persist for a base service URL (the
      interface name is host-specific), and the raw ``%`` in the authority is a
      percent-encoding hazard for every downstream consumer of the raw string.

    Valid IPv6 literals -- including uncommon-looking ones like ``[9999::1]``
    (``9999`` is a legal hex group) and IPv4-mapped ``[::ffff:1.2.3.4]`` -- are
    accepted unchanged.

    FINALLY, the value must round-trip through ``httpx.URL`` itself. Every
    persisted URL is eventually consumed by httpx (the adapters and the setup
    probes), so ITS parser is the ground-truth invariant -- each earlier
    parser-mismatch fix (ports, IPv4-shaped hosts, bracketed IPv6) patched one
    instance of "urlsplit tolerates it, httpx explodes on it"; this gate closes
    the CLASS by construction. Today the residue it catches is IDNA-invalid
    hostnames, which raise from two DIFFERENT places (both covered):

    * an unencodable Unicode label (``http://\U0001f4a9.local``) raises
      ``httpx.InvalidURL`` from the ``httpx.URL()`` constructor itself;
    * a bogus or disallowed punycode A-label (``http://xn--zzzzzz``, or the
      pre-encoded emoji ``http://xn--ls8h.local``) passes the constructor and
      raises a raw ``idna.IDNAError`` only from the ``.host`` property (httpx
      decodes ``xn--`` labels lazily there) -- hence the gate touches ``.host``
      too, and the except is deliberately broad since neither error type is an
      ``httpx.HTTPError``.

    The specific checks above run FIRST so their specific, actionable messages
    win; the gate only answers for what is left, with an honest generic message.
    It also cannot over-tighten: by definition it accepts exactly what the real
    consumer accepts -- empty labels, overlong labels, underscore hosts,
    trailing-dot FQDNs, Unicode IDNs (``http://café.local``) and VALID
    punycode (``http://xn--caf-dma.local``) all parse in httpx 0.28 and continue
    to pass; those fail later, if at all, as honest retryable connect/DNS
    errors, not parser crashes.
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
    if "[" in parts.netloc:
        # A bracketed host must be a plain, zone-free IPv6 literal. urlsplit
        # already rejected most invalid bracket content above; this closes what it
        # still tolerates -- an IPvFuture form ("[v7.abc]", httpx.InvalidURL at
        # request time) and zone ids ("[fe80::1%eth0]", rejected by policy).
        # ``.hostname`` is the bracket content with the brackets stripped.
        try:
            if IPv6Address(hostname).scope_id is not None:
                return IPV6_ZONE_ID_MESSAGE
        except AddressValueError:
            return INVALID_IPV6_MESSAGE
    elif set(hostname) <= _IPV4_SHAPED_CHARS:
        # An IPv4-shaped host (digits and dots only) must be a real dotted quad --
        # urlsplit accepts "999.999.999.999" / "01.02.03.04" but httpx rejects
        # them at request time.
        try:
            IPv4Address(hostname)
        except AddressValueError:
            return INVALID_IPV4_MESSAGE
    # FINAL catch-all gate: parse with the ACTUAL downstream consumer. Anything
    # httpx cannot digest WILL fail at request time as an uncaught error, so its
    # parser is the ground-truth invariant; the specific checks above ran first
    # so their specific messages win, and this only answers for the residue
    # (today: IDNA-invalid hostnames). The ``.host`` touch is load-bearing: an
    # unencodable Unicode label raises httpx.InvalidURL from the constructor,
    # but a bogus "xn--" punycode label only raises (a raw idna.IDNAError, not
    # an HTTPError) when httpx lazily decodes it in the .host property. Broad
    # except is deliberate and honest -- ANY parse failure means the adapters
    # can never use this value, so reject it now.
    try:
        _ = httpx.URL(url).host
    except Exception:
        return UNPARSEABLE_URL_MESSAGE
    return None
