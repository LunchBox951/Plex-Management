"""HTTP header credential hygiene (GHSA-qv47).

An opaque credential (a Plex token, a Prowlarr api key) that rides an outbound
HTTP header can hit two distinct failure modes when it contains a character
h11/httpx cannot encode as a header field-value: CR/LF or NUL make httpx raise
``LocalProtocolError`` whose ``str(exc)`` echoes the RAW value (``"Illegal
header value b'SECRET\\r\\ninjected'"``) — a credential leak through any
``except httpx.HTTPError as exc: ...str(exc)`` branch (north star #3); any
non-ASCII byte makes httpx's ASCII header encoder raise an uncaught
``UnicodeEncodeError`` — a 500 the caller never intended (north star #3,
honesty over silence — no unhandled crash). :func:`header_value_error` is the
single, dependency-free predicate shared by every caller that puts a
credential in a header, checked BEFORE the outbound request so neither failure
mode is ever reached.

This module is top-level (not under ``web/``) because one caller,
``adapters/plex/oauth.py``, is an adapter, and adapters never import ``web/``
(hexagonal layering) — mirrors :mod:`plex_manager.logsafe`, the other
dependency-free string-hygiene module shared across layers.
"""

from typing import Final

__all__ = ["HEADER_VALUE_MESSAGE", "header_value_error", "is_header_safe"]

HEADER_VALUE_MESSAGE: Final = "contains characters that are not valid in an HTTP header"

_MIN_PRINTABLE: Final = 0x21  # '!'  (space 0x20 excluded: OWS is silently stripped by h11)
_MAX_PRINTABLE: Final = 0x7E  # '~'


def header_value_error(value: str) -> str | None:
    """Return :data:`HEADER_VALUE_MESSAGE` if ``value`` cannot be safely sent as an
    HTTP header field-value carrying an opaque credential, else ``None``.

    Safe = every character is printable ASCII (``0x21``-``0x7E``). This rejects
    CR/LF/NUL (httpx/h11 raises ``LocalProtocolError`` echoing the RAW value —
    a credential leak via ``str(exc)``) and any non-ASCII character (httpx
    cannot ASCII-encode it — an uncaught ``UnicodeEncodeError``/500). Total:
    never raises, on any input. The empty string is safe (some callers legitimately
    probe with no token yet).

    Scoped to OPAQUE credentials only — do NOT reuse this for header values that
    legitimately contain spaces or extended characters (e.g. ``User-Agent``).
    """
    for ch in value:
        if not (_MIN_PRINTABLE <= ord(ch) <= _MAX_PRINTABLE):
            return HEADER_VALUE_MESSAGE
    return None


def is_header_safe(value: str) -> bool:
    """``True`` iff ``value`` can ride an HTTP header as an opaque credential."""
    return header_value_error(value) is None
