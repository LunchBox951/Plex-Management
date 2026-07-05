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
    """Redact a URL-shaped release GUID before logging. Total -- never raises.

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

    A log barrier must be TOTAL: this helper is evaluated inside ``except``
    handlers (e.g. auto-grab's per-release source-failure WARNING), where a throw
    would escape the handler and abort the whole surrounding cycle. Two
    external-input edge cases are therefore absorbed, never raised:

    * ``urlsplit`` itself raises ``ValueError`` on some malformed URL-ish text
      (e.g. ``http://[bad`` -- an unclosed IPv6 bracket in the netloc). That only
      happens for a value with a ``//<netloc>`` shape, which may STILL carry a
      credential (``http://[bad/?passkey=...`` raises identically), so the
      fallback fails CLOSED: an empty-host ``"#<hash>"`` token, never the
      verbatim value (a plain passthrough here would re-open the very leak this
      helper exists to close).
    * The digest encodes with ``surrogatepass``: JSON permits lone surrogates,
      and a plain ``.encode("utf-8")`` would raise ``UnicodeEncodeError`` (a
      ``ValueError`` subclass) -- an exotic input must not become a throw or a
      redaction bypass.
    """
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    try:
        split = urlsplit(value)
        if not (split.scheme and split.netloc):
            return safe_text(value)  # plain id/hash -- not URL-shaped
        host = split.hostname or ""
    except ValueError:
        host = ""  # unparseable but URL-ish: fail closed, hash-only token
    return f"{safe_text(host)}#{digest}"
