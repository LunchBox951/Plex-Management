"""TrustedHostMiddleware — reject requests whose real ``Host`` isn't trusted.

The app's only defense against a network-originated first-owner setup claim is
its loopback bind (``PLEX_MANAGER_HOST=127.0.0.1``). That bind alone does not
stop a browser that has been DNS-rebound: a public DNS name can be resolved to
``127.0.0.1`` while the browser's URL (and therefore its same-origin checks,
including CORS) still show the original public hostname. Such a request reaches
this app as a same-origin loopback connection carrying an attacker-chosen
``Host`` header, letting a remote page complete the pre-auth setup claim and
lock out the real operator.

This middleware runs OUTERMOST (installed last in ``create_app``, so it wraps
every other layer including ``SetupGuardMiddleware``) and rejects any request
whose ``Host`` header is not on the trusted set before any routing happens.

Only the raw ``Host`` header is ever consulted. ``X-Forwarded-Host`` (or any
other forwarded-host header) is deliberately never read here: it is
client-suppliable and would let an attacker simply forward a trusted value
while keeping the real, untrusted ``Host`` — the exact bypass this middleware
exists to close. A reverse-proxy install still works because the proxy sets
the outbound ``Host`` header itself (or the operator adds the public hostname
to ``allowed_hosts``), not because this middleware trusts what the client says
was forwarded.

Reverse-proxy installs MUST preserve the client's original ``Host`` on the
upstream request (nginx: ``proxy_set_header Host $host;`` — note nginx's
default is ``$proxy_host``, which rewrites ``Host`` to the loopback/private
upstream address). This app only ever sees the ``Host`` the proxy hands it: if
the proxy rewrites every ``Host`` to its upstream value, host validation is
effectively delegated to the proxy's own vhost matching (``server_name`` /
default-server config), and ``allowed_hosts`` cannot distinguish public
hostnames at this layer. Preserve the original ``Host`` and list the public
hostname in ``allowed_hosts`` so the check stays enforced in-app.

## Trust policy

A host is trusted (after lowercasing, stripping a trailing dot, and dropping
any port) iff:

1. it is a configured hostname (``Settings.allowed_hosts``) or the literal
   ``"localhost"``; or
2. it parses as an IP literal that is loopback, private (RFC1918 / IPv6 ULA),
   or link-local.

(2) is safe to allow by default: a browser only ever emits ``Host: <ip
literal>`` when its URL's host IS that IP literal, meaning it connected to
that literal address directly. There is no hostname indirection to rebind, so
an IP-literal Host can never be the target of a DNS-rebinding attack. This
keeps bare-metal loopback, SSH-tunnel (``localhost``), and LAN-by-IP
installs working with zero configuration, while a public hostname not in
``allowed_hosts`` (the rebinding vector) is rejected.

The literal ``"*"`` in ``allowed_hosts`` disables validation entirely — an
explicit, documented-as-discouraged escape hatch for unusual topologies, kept
so no deployment shape hits a dead end (north star #2).
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from plex_manager.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["TrustedHostMiddleware"]


def _valid_port(port: str) -> bool:
    """Whether ``port`` is a syntactically valid port: 1-5 ASCII digits, <= 65535.

    The length bound comes FIRST so :func:`int` is never fed an arbitrarily
    long digit string: CPython raises ``ValueError`` past its int-conversion
    digit limit, which would turn untrusted input into a 500 + traceback
    instead of the fixed ``400 invalid_host``. The ``isascii`` check rejects
    non-ASCII "digits" (for example ``"²"``) that :meth:`str.isdigit` alone
    would accept.
    """
    return 0 < len(port) <= 5 and port.isascii() and port.isdigit() and int(port) <= 65535


def _valid_port_suffix(rest: str) -> bool:
    """Whether the text after a bracketed host (``]...``) is an acceptable tail.

    Acceptable iff it is empty (bare bracketed host) or a valid ``:<port>``.
    Fails closed on malformed tails like ``.attacker.example`` or ``:`` (empty
    port).
    """
    if not rest:
        return True
    return rest[0] == ":" and _valid_port(rest[1:])


def _split_host(raw: str) -> str | None:
    """Extract the hostname/IP portion of a ``Host`` header, dropping any port.

    Handles bracketed IPv6 with a port (``[::1]:8000`` -> ``::1``), a bare
    ``host:port`` pair (exactly one colon), and an unbracketed IPv6 literal
    (more than one colon, no brackets -- passed through as-is since it carries
    no port). Returns ``None`` for a blank/whitespace-only header, or for
    malformed syntax (empty brackets, bracket contents that are not an IPv6
    literal, trailing text after ``]`` that is not a valid ``:<port>``, or a
    single-colon suffix that is not a valid port) -- failing closed rather
    than trusting a partial parse.
    """
    host = raw.strip()
    if not host:
        return None
    if host.startswith("["):
        # Bracketed IPv6 literal, optionally followed by ":<port>". Anything
        # else after the closing "]" -- a bare label like ``[::1]evil.example``
        # or ``[127.0.0.1].attacker.example`` -- is malformed Host syntax. Fail
        # closed instead of returning the bracketed part and silently dropping
        # the suffix, which would let an attacker-chosen name ride a loopback
        # literal past the trust check.
        closing = host.find("]")
        if closing == -1:
            return None
        inner = host[1:closing]
        if not inner or not _valid_port_suffix(host[closing + 1 :]):
            return None
        # RFC 3986 permits brackets only around IPv6 (colon-containing)
        # literals. Anything else inside brackets (``[localhost]``,
        # ``[127.0.0.1]``) is malformed Host syntax -- fail closed rather than
        # unwrapping it into a name/IPv4 literal that would then inherit the
        # trust an unbracketed form would have had to earn on its own.
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            return None
        return inner
    if host.count(":") == 1:
        # A single colon is an unambiguous "host:port" pair -- but only when
        # the suffix IS a valid port. Anything else (``127.0.0.1:evil.example``,
        # ``localhost:notaport``) is malformed Host syntax; fail closed instead
        # of normalizing an attacker-chosen tail down to a trusted prefix.
        name, _, port = host.partition(":")
        if not name or not _valid_port(port):
            return None
        return name
    # Zero colons (plain hostname/IPv4), or 2+ colons -- an unbracketed IPv6
    # literal with no port to strip. Either way, pass it through unchanged.
    return host


def _is_trusted(host: str, configured: frozenset[str]) -> bool:
    """Whether ``host`` (already port-stripped) is on the trusted set."""
    normalized = host.rstrip(".").lower()
    if not normalized:
        return False
    if "*" in configured:
        return True
    if normalized in configured or normalized == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """Return 400 for any request whose ``Host`` header is not trusted."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw_host = request.headers.get("host", "")
        host = _split_host(raw_host)
        configured = frozenset(get_settings().allowed_hosts)
        if host is not None and _is_trusted(host, configured):
            return await call_next(request)
        # Fixed, non-reflective detail: never echo the raw Host back to the
        # caller (honesty-over-silence still means never logging/leaking the
        # untrusted value into a response).
        return JSONResponse(status_code=400, content={"detail": "invalid_host"})
