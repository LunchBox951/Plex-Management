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
import re
from typing import Final
from urllib.parse import urlsplit

#: The ONLY shape :func:`safe_guid` ever passes through verbatim: a bounded run
#: of letters, digits, dots, underscores, and hyphens. This covers every benign
#: release-GUID id form in the wild -- hex info-hashes, UUIDs, numeric ids,
#: ``tt``-imdb ids, dotted release names -- while NO URL machinery (``/ : ? & %
#: @ =`` whitespace, CR/LF) can ever match, so no URI of ANY shape, known or
#: novel, can reach a log through the passthrough. The 128 cap bounds the log
#: field and keeps "it matched the allowlist" meaning "it is a short opaque id".
_SAFE_GUID_ID_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")

#: The ONLY shape :func:`safe_guid` ever emits as a redaction LABEL: the same
#: character class, capped at a hostname-scale 64. A legit hostname or URI
#: scheme always matches; anything else -- notably a percent-encoded blob that
#: ``urlsplit`` swallows INTO ``hostname`` (``https://host%2Fdl%3Fpasskey%3D...``
#: has no literal ``/`` or ``?``, so the whole encoded path/query, secret
#: included, parses as the netloc) -- is dropped for the bare ``"#<hash>"``
#: token. The label derives from external input, so it gets the same allowlist
#: treatment as the value itself: validated or not emitted, never "probably
#: fine".
_SAFE_GUID_LABEL_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")


def safe_int(value: int) -> int:
    """Return ``int(value)`` -- honest type enforcement + analyzer taint barrier."""
    return int(value)


#: Every boundary ``str.splitlines()`` honors -- collapsed to a space so a
#: request-derived value cannot forge a second log record. ``\r\n`` stays two
#: chars (two spaces), preserving the pre-existing collapse behavior. Ten
#: chars: ``\n \r \v(0x0b) \f(0x0c) \x1c \x1d \x1e \x85(NEL) \u2028(LS)
#: \u2029(PS)``. ``safe_guid``'s allowlist is unchanged -- it admits none of
#: these.
_LINE_BOUNDARY_RE: Final = re.compile("[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def safe_text(value: str) -> str:
    """Collapse every Unicode line boundary to a space so a request-derived
    string cannot forge a log record."""
    return _LINE_BOUNDARY_RE.sub(" ", value)


def safe_guid(value: str) -> str:
    """Redact a release GUID before logging unless it is a provably plain id.
    Total -- never raises.

    Prowlarr private-indexer GUIDs are frequently URIs that embed a tracker
    passkey or session token -- ``http(s)`` URLs in path/query/userinfo, magnet
    URIs in their percent-encoded ``tr=`` announce parameters -- so logging one
    verbatim leaks a credential (north star #3: secrets are never logged).

    This helper is an ALLOWLIST, deliberately: three successive denylist rules
    each leaked a novel shape ("scheme+netloc" missed malformed-bracket URLs,
    then magnet's scheme-without-netloc, then schemeless ``host/path?passkey=``
    values that parse as pure path). Enumerating "URL-shaped" can only ever
    chase the last leak. Inverted, the question is decidable: a value passes
    through verbatim ONLY if it fullmatches :data:`_SAFE_GUID_ID_RE` --
    letters/digits/``._-``, at most 128 chars -- a character class that admits
    every benign id form (hex hashes, UUIDs, numeric ids, ``tt``-ids, dotted
    release names) and NO URL machinery whatsoever: nothing containing ``/ : ?
    & % @ =`` or whitespace can ever pass, so no URI of any shape, known or
    novel, reaches a log. (The allowlisted shape contains no CR/LF either, so
    the passthrough is byte-identical -- ``safe_text`` would be a no-op.)

    EVERYTHING else redacts to ``"<label>#<12-hex-sha256-prefix>"``: the label
    is the hostname when one parses (``https://tracker...`` ->
    ``tracker...#<hash>``; ``urlsplit().hostname``, never ``netloc``, so
    ``user:pass@`` userinfo is dropped), else the scheme (``magnet:?...`` ->
    ``magnet#<hash>``), else nothing (bare ``"#<hash>"``) -- diagnosable where
    possible, never a byte of the credential-bearing remainder, and the stable
    hash of the *full* GUID still lets beta-week analysis correlate repeated
    failures of the SAME release. The label is ITSELF allowlist-validated
    (:data:`_SAFE_GUID_LABEL_RE`) before emission: ``urlsplit`` can swallow a
    percent-encoded path/query INTO ``hostname`` (``https://host%2F...
    %3Fpasskey%3D...`` has no literal ``/``/``?``, so its whole tail parses as
    netloc), and an unvalidated label would hand that secret right back out.
    A label that fails validation is dropped for the bare-hash token.

    **The contract this buys:** every byte this function ever emits is either
    (a) the input itself, having fullmatched the safe-id allowlist, or (b) an
    allowlist-validated label plus ``"#"`` plus 12 hex digits. No third path
    exists, so no unvalidated external byte can reach a log through it.

    Deliberate fail-closed collateral: an exotic-but-legit plain id that
    happens to carry a slash/colon/space (or exceed 128 chars) redacts too --
    it stays correlatable via the hash, and over-redacting a harmless id costs
    one label while under-redacting a real URI leaks a credential.

    A log barrier must be TOTAL: this helper is evaluated inside ``except``
    handlers (e.g. auto-grab's per-release source-failure WARNING), where a
    throw would escape the handler and abort the whole surrounding cycle. The
    regexes cannot raise; ``urlsplit``'s ``ValueError`` on malformed netlocs
    (``http://[bad``) is absorbed into the bare-hash arm; and the digest
    encodes with ``surrogatepass`` (JSON permits lone surrogates; a plain UTF-8
    encode would raise ``UnicodeEncodeError`` -- a ``ValueError`` subclass --
    mid-handler).
    """
    if _SAFE_GUID_ID_RE.fullmatch(value):
        return value  # provably a plain opaque id -- byte-identical passthrough
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    try:
        split = urlsplit(value)
        label = split.hostname or split.scheme
    except ValueError:
        label = ""  # unparseable (malformed netloc): fail closed, hash-only token
    if not _SAFE_GUID_LABEL_RE.fullmatch(label):
        label = ""  # a label is emitted validated or not at all (see contract)
    return f"{label}#{digest}"


# --------------------------------------------------------------------------- #
# redact_secrets (issue #153): a defense-in-depth, post-hoc redaction pass over
# a FULLY-RENDERED log line, applied at capture time (``log_capture_service.
# _capture``) and again at the ``/ops/logs/export`` boundary. The helpers above
# are call-site barriers -- they only protect a value a call site remembers to
# route through them. ``redact_secrets`` is the backstop for the case that
# discipline misses: a message string assembled elsewhere (a third-party
# library's own log line, a forgotten call site, a future adapter) that
# happens to carry one of THIS app's real secret shapes -- the credentials
# ``config.py`` documents plus the ones the setup wizard stores encrypted (Plex
# token, Prowlarr/TMDB api keys, qBittorrent password), and the Fernet
# encryption key protecting them at rest.
#
# Deliberately regex-based and KEY-NAME-driven, never a denylist of specific
# services: every adapter in this codebase sends its secret as one of a small
# set of shapes --
#   * a query-string parameter (TMDB's ``api_key``, an X-Plex-Token carried on
#     a URL rather than a header),
#   * an HTTP header value (Prowlarr's ``X-Api-Key``, Plex's ``X-Plex-Token``,
#     a bearer/basic ``Authorization``),
#   * HTTP basic-auth userinfo (``scheme://user:pass@host``),
#   * a bare form/dict field (qBittorrent's login ``password``),
#   * or -- for the Fernet key specifically -- a standalone base64 blob with no
#     key name attached at all.
# Matching by KEY NAME (whatever separates it from its value: ``:``/``=``,
# optionally quoted) generalizes across all four services and any future one
# without a per-adapter rule, and is CONSERVATIVE the way the task requires:
# the key name always survives (debuggability), only the value is masked.
#
# Three independent passes, applied in sequence (each is a total no-op on text
# it finds nothing to redact in, so the order between the first two never
# matters -- they match disjoint key vocabularies):
#
# 1. Basic-auth URL userinfo (``scheme://user:pass@host``): the PASSWORD half
#    of ``user:pass@`` is masked; the username and the rest of the url survive
#    (diagnosable: which url, which account -- never a byte of the password).
# 2. ``Authorization`` header values specifically: unlike every other secret
#    key below, an Authorization value routinely carries an internal SPACE
#    (``Basic <base64>``, ``Bearer <token>``) -- a plain single-token value
#    capture would only swallow the scheme word and leave the credential
#    itself exposed. This pass captures the whole ``<scheme> <token>`` (or a
#    bare token with no scheme) and masks it entirely.
# 3. Every other secret-key-shaped ``key<sep>value`` pair -- ``api_key``/
#    ``apikey``, ``access_token``, ``auth_token``, a bare ``token`` (also
#    matches ``X-Plex-Token``/``X-Api-Key``: the hyphen before the final word
#    is a non-word boundary, so the alternation matches the LAST word of a
#    hyphenated header name and the untouched ``X-Plex-``/``X-Api-`` prefix is
#    simply copied through by ``re.sub`` unmodified), ``password``/``passwd``/
#    ``pwd``, ``passkey`` (the private-tracker-URL shape ``safe_guid`` already
#    covers at the call site; this is the same shape's defense-in-depth
#    backstop), ``secret``. The value capture excludes whitespace/quote/``&``/
#    ``,``/closing-bracket delimiters, so it never swallows past ONE token --
#    correct for a query-string ``key=value``, a header ``Key: value``, or a
#    JSON/dict ``"key": "value"`` entry, all in one pattern.
#
# A final, UNCONDITIONAL pass (no key name involved at all) catches a
# Fernet-key-SHAPED standalone blob wherever it appears: ``cryptography``'s
# ``Fernet.generate_key()`` is always exactly 44 urlsafe-base64 characters
# ending in one ``=`` pad -- distinctive enough to redact on shape alone, which
# matters because the key is loaded from a file (``<data_dir>/secret.key``),
# never assigned a "key name" of its own in a log line to key off of.
# A regex alternation of secret-bearing KEY NAMES (not a credential itself).
_SECRET_KEY_PATTERN: Final = (
    r"api[-_]?key|access[-_]?token|auth[-_]?token|token|"  # noqa: S105
    r"password|passwd|pwd|passkey|secret"
)

# ``sep``: an optional quote (closing the key's own quoting in a dict/JSON
# literal), then MANDATORY ``:``/``=`` (never matches a bare word with no
# separator at all -- "issued a new token for the user" has no `:`/`=`
# immediately following "token" and is correctly left untouched), then an
# optional opening quote for the value.
_SEP_PATTERN: Final = r"['\"]?\s*[:=]\s*['\"]?"
# The value itself: any run of characters that is not whitespace, a literal
# delimiter (`&`, `,`), a quote, or a closing bracket -- i.e. exactly one
# token, correct for a query-string/header/dict-style single-value secret.
_VALUE_CHARS: Final = r"[^\s&,'\"}\)\]]+"

_SECRET_KV_RE: Final = re.compile(
    r"(?i)\b(?:" + _SECRET_KEY_PATTERN + r")\b" + _SEP_PATTERN + r"(?P<value>" + _VALUE_CHARS + r")"
)

# ``Authorization`` gets its own pattern (see rationale above): the value may
# be a scheme word plus a token (``Basic``/``Bearer``/``Digest``/``Negotiate``)
# separated by a SPACE the generic value-capture above would stop at, or a
# bare scheme-less token.
_AUTHORIZATION_RE: Final = re.compile(
    r"(?i)\bauthorization\b"
    + _SEP_PATTERN
    + r"(?P<value>(?:Basic|Bearer|Digest|Negotiate)\s+"
    + _VALUE_CHARS
    + r"|"
    + _VALUE_CHARS
    + r")"
)

# ``scheme://user:pass@host`` -- the PASSWORD half of HTTP basic-auth userinfo.
# Group 1 captures ``scheme://user`` (stopping the username at the first
# ``:``/``/``/``@``/quote/whitespace); the password itself is never captured
# into the output, only its span is consumed so it can be dropped.
_BASIC_AUTH_URL_RE: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s/:@'\"]+):[^\s@'\"]+@")

# A standalone Fernet-key-shaped blob: 43 urlsafe-base64 characters plus the
# one trailing ``=`` pad ``Fernet.generate_key()`` always produces (32 raw
# bytes base64-encoded), bounded on both sides so it cannot match as a
# substring of a longer base64/id-shaped run.
_FERNET_KEY_RE: Final = re.compile(r"(?<![A-Za-z0-9_=+/-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=+/-])")

_REDACTED: Final = "<redacted>"


def _mask_value(match: re.Match[str]) -> str:
    """Replace a ``_SECRET_KV_RE``/``_AUTHORIZATION_RE`` match's ``value`` group
    with :data:`_REDACTED`, keeping everything else the match consumed (the key
    name, separator, and any quote) verbatim -- "mask the value, keep the key
    name for debuggability"."""
    whole = match.group(0)
    value_offset = match.start("value") - match.start(0)
    return whole[:value_offset] + _REDACTED


def _mask_basic_auth(match: re.Match[str]) -> str:
    """Replace a ``_BASIC_AUTH_URL_RE`` match's password with :data:`_REDACTED`,
    keeping ``scheme://user:`` and the trailing ``@`` -- the account is
    diagnosable, the credential never is."""
    return f"{match.group(1)}:{_REDACTED}@"


def redact_secrets(text: str) -> str:
    """Defense-in-depth: mask this app's known secret shapes in *text* before it
    is ever persisted or exported (issue #153).

    A conservative, total (never raises) regex pass -- see the module-level
    comment above for the exact shapes covered and why key-name matching
    generalizes across every adapter (Plex/Prowlarr/TMDB/qBittorrent) without a
    per-service rule. A line carrying none of these shapes is returned
    byte-identical; this is a SECOND line of defense behind the call-site
    barriers above (:func:`safe_guid` etc.), not a replacement for them --
    call-site discipline still applies, this only catches what discipline
    misses.
    """
    if not text:
        return text
    redacted = _BASIC_AUTH_URL_RE.sub(_mask_basic_auth, text)
    redacted = _AUTHORIZATION_RE.sub(_mask_value, redacted)
    redacted = _SECRET_KV_RE.sub(_mask_value, redacted)
    redacted = _FERNET_KEY_RE.sub("<redacted-fernet-key>", redacted)
    return redacted
