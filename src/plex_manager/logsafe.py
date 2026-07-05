"""Log-value hygiene for request-derived data.

A text log line admits exactly one injection: CR/LF forging a fake record.
These helpers are the honest, single-purpose barriers used at every log site
whose value traces from an HTTP request (message args AND ``extra=`` fields --
CodeQL's py/log-injection taints both). Ints are re-coerced (a no-op for real
ints, a taint barrier for the analyzer); text gets CR/LF collapsed to spaces.
Some external values are additionally *secret-bearing* -- a URI-shaped identifier
(an ``http(s)`` URL, a magnet link, ...) can embed a tracker passkey or session
token -- and get a stronger barrier (:func:`safe_guid`) that never emits the
credential-bearing part at all (north star #3: secrets are never logged). See
CONTRIBUTING.md "Logging request-derived values".
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
    """Redact a URI-shaped release GUID before logging. Total -- never raises.

    Prowlarr private-indexer GUIDs are frequently URIs that embed a tracker
    passkey or session token, so logging one verbatim leaks a credential (north
    star #3: secrets are never logged). ``http(s)`` URLs carry it in path/query/
    userinfo; **magnet URIs carry it too** -- ``tr=`` announce parameters are
    percent-encoded tracker URLs, passkey and all -- and a magnet has a scheme
    but NO network location, so "scheme AND netloc" under-classifies it as a
    plain id. The rule is therefore: ANY value with a URI scheme OR a network
    location is redacted to ``"<label>#<12-hex-sha256-prefix>"`` where the label
    is the hostname when one exists (``https://tracker...`` ->
    ``tracker...#<hash>``) and otherwise the scheme (``magnet:?...`` ->
    ``magnet#<hash>``) -- the class of URI stays diagnosable while nothing of
    the credential-bearing remainder leaves the process, and the stable hash of
    the *full* GUID still lets beta-week analysis correlate repeated failures of
    the SAME release. ``urlsplit().hostname`` (never ``netloc``) drops embedded
    ``user:pass@`` userinfo; the netloc-without-scheme arm also catches
    protocol-relative ``//host/...?passkey=...`` values.

    Only a value with NEITHER scheme NOR netloc -- a plain id/hash/title -- is
    not secret-bearing and passes through :func:`safe_text` unchanged (CR/LF
    still collapsed). Deliberate fail-closed collateral: an opaque id that
    merely LOOKS scheme-ish (``prowlarr:123``, ``urn:uuid:...``) is redacted
    too -- over-redacting a harmless id costs one hash-correlatable label;
    under-redacting a real URI leaks a credential.

    A log barrier must be TOTAL: this helper is evaluated inside ``except``
    handlers (e.g. auto-grab's per-release source-failure WARNING), where a throw
    would escape the handler and abort the whole surrounding cycle. Two
    external-input edge cases are therefore absorbed, never raised:

    * ``urlsplit`` itself raises ``ValueError`` on some malformed URL-ish text
      (e.g. ``http://[bad`` -- an unclosed IPv6 bracket in the netloc). That only
      happens for a value with a ``//<netloc>`` shape, which may STILL carry a
      credential (``http://[bad/?passkey=...`` raises identically), so the
      fallback fails CLOSED: a label-less ``"#<hash>"`` token, never the
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
        if not split.scheme and not split.netloc:
            return safe_text(value)  # plain id/hash/title -- not URI-shaped
        label = split.hostname or split.scheme
    except ValueError:
        label = ""  # unparseable but URL-ish: fail closed, hash-only token
    return f"{safe_text(label)}#{digest}"
