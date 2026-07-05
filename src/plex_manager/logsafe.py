"""Log-value hygiene for request-derived data.

A text log line admits exactly one injection: CR/LF forging a fake record.
These helpers are the honest, single-purpose barriers used at every log site
whose value traces from an HTTP request (message args AND ``extra=`` fields --
CodeQL's py/log-injection taints both). Ints are re-coerced (a no-op for real
ints, a taint barrier for the analyzer); text gets CR/LF collapsed to spaces.
Some external values are additionally *secret-bearing* -- a URL-shaped identifier
can embed a tracker passkey or session token -- and get a stronger barrier
(:func:`safe_guid`) that never emits the credential-bearing part at all
(north star #3: secrets are never logged). See CONTRIBUTING.md "Logging
request-derived values".
"""

import hashlib
from urllib.parse import urlsplit


def safe_int(value: int) -> int:
    """Return ``int(value)`` -- honest type enforcement + analyzer taint barrier."""
    return int(value)


def safe_text(value: str) -> str:
    """Collapse CR/LF so a request-derived string cannot forge log records."""
    return value.replace("\r", " ").replace("\n", " ")


def safe_guid(value: str) -> str:
    """Redact a URL-shaped release GUID before logging.

    Prowlarr private-indexer GUIDs are frequently URLs whose path/query (and
    occasionally userinfo) embed a tracker passkey or session token, so logging
    one verbatim leaks a credential (north star #3: secrets are never logged).
    When ``value`` parses as a URL -- a scheme AND a network location -- emit only
    ``"<host>#<12-hex-sha256-prefix>"``: the host is kept for diagnosability while
    the credential-bearing path/query/userinfo never leaves the process, and the
    stable hash of the *full* GUID still lets beta-week analysis correlate repeated
    failures of the SAME release without exposing its value. ``urlsplit().hostname``
    (never ``netloc``) is used so embedded ``user:pass@`` userinfo is dropped too.
    A non-URL GUID (a plain id/hash) is not secret-bearing, so it passes through
    :func:`safe_text` unchanged (CR/LF still collapsed).
    """
    split = urlsplit(value)
    if split.scheme and split.netloc:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"{safe_text(split.hostname or '')}#{digest}"
    return safe_text(value)
