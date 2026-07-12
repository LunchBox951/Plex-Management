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
# Four independent passes, applied in sequence (each is a total no-op on text
# it finds nothing to redact in, so their relative order never matters -- they
# match disjoint syntactic shapes):
#
# 1. Basic-auth URL userinfo (``scheme://user:pass@host``): the PASSWORD half
#    of ``user:pass@`` is masked; the username and the rest of the url survive
#    (diagnosable: which url, which account -- never a byte of the password).
# 2. ``Authorization`` header values specifically: unlike every other secret
#    key below, an Authorization value routinely carries an internal SPACE
#    (``Basic <base64>``, ``Bearer <token>``) -- a plain single-token value
#    capture would only swallow the scheme word and leave the credential
#    itself exposed. This pass captures up to two whitespace-separated tokens
#    -- ``<scheme> <credential>`` for ANY scheme word (no scheme allowlist:
#    RFC 7235 schemes are open-ended, and an allowlist would leak the
#    credential of every unknown scheme) -- or a bare scheme-less token, and
#    masks the whole thing.
# 3. Every other secret-key-shaped ``key<sep>value`` pair -- ``api_key``/
#    ``apikey``, ``access_token``, ``auth_token``, a bare ``token`` (also
#    matches ``X-Plex-Token``/``X-Api-Key``: the hyphen before the final word
#    is a non-word boundary, so the alternation matches the LAST word of a
#    hyphenated header name and the untouched ``X-Plex-``/``X-Api-`` prefix is
#    simply copied through by ``re.sub`` unmodified), ``password``/``passwd``/
#    ``pwd``, ``passkey`` (the private-tracker-URL shape ``safe_guid`` already
#    covers at the call site; this is the same shape's defense-in-depth
#    backstop), ``secret``. A bounded, lazy ``[\w-]{0,64}?`` prefix in front of
#    the alternation lets the key word be the SUFFIX of a longer
#    underscore/hyphen-joined identifier, so THIS app's real settings-field
#    names -- ``tmdb_api_key``, ``prowlarr_api_key``, ``plex_token``,
#    ``qbittorrent_password``, ``app_api_key`` -- match even though ``_`` is a
#    word char that would otherwise defeat a bare ``\b`` before ``api``/``token``
#    /``password``; the whole field name (prefix included) survives in the
#    output, only the value is masked. The value capture handles two shapes: an
#    UNQUOTED value stops at the first whitespace/``&``/``,``/quote/closing
#    bracket (exactly one token -- a query-string ``key=value`` or header
#    ``Key: value``); a QUOTED value (``key='...'`` / ``key="..."``, e.g. a
#    ``qbittorrent_password`` containing spaces or commas) is consumed through to
#    its MATCHING closing quote -- an embedded opposite-quote character does not
#    end it -- so no suffix of a multi-word quoted credential is left behind the
#    ``<redacted>`` token.
# 4. The same secret key names in TUPLE rendering -- ``('X-Api-Key', 'SECRET')``,
#    the shape ``list(headers.items())``/raw header dumps produce -- which has
#    no ``:``/``=`` separator for pass 3 to key on: a quoted key name ending in
#    a secret key word (or ``authorization``), a comma, then the quoted value
#    masked whole.
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
# The UNQUOTED value: any run of characters that is not whitespace, a literal
# delimiter (`&`, `,`), a quote, or a closing bracket -- i.e. exactly one
# token, correct for a query-string/header/dict-style single-value secret.
_VALUE_CHARS: Final = r"[^\s&,'\"}\)\]]+"

# ``key<sep>value`` for every secret key name EXCEPT ``Authorization`` (below).
# The separator here (unlike :data:`_SEP_PATTERN`) stops BEFORE the value's
# opening quote so that quote can be captured into ``q`` and a quoted value
# consumed through to its matching close:
#   * leading ``[\w-]{0,64}?`` -- a bounded, lazy prefix so a secret key word can
#     be the tail of a longer ``_``/``-``-joined field name (``plex_token``,
#     ``tmdb_api_key``); ``_`` is a word char, so a bare ``\b`` before ``token``
#     would never fire on ``plex_token``. Bounded to keep the scan linear.
#   * ``(?P<q>['\"])?`` -- the value's optional opening quote.
#   * ``(?P<value> ... )`` -- when ``q`` matched, the tempered run
#     ``(?:(?!(?P=q)).)*`` consumes everything up to (not past) the MATCHING
#     closing quote -- crucially including the OPPOSITE quote character, so a
#     credential like ``password="abc'def"`` (``SettingsUpdate`` accepts such
#     passwords) is masked whole, never split at the embedded quote; otherwise
#     the single unquoted token. The tempered dot is linear -- each position
#     is a one-char lookahead plus one consume, no ambiguity for backtracking
#     to explore (this file's tests just drew a CodeQL finding; the redactor
#     must never hand it a ReDoS). ``_mask_value`` drops from the value start
#     on, so the closing quote -- deliberately left OUTSIDE the match (the
#     tempered run cannot cross it) -- survives around ``<redacted>``.
_KV_SEP_PATTERN: Final = r"['\"]?\s*[:=]\s*"
_QUOTED_VALUE: Final = r"(?:(?!(?P=q)).)*"  # up to, never past, the matching close quote
_SECRET_KV_RE: Final = re.compile(
    r"(?i)\b[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r")\b"
    + _KV_SEP_PATTERN
    + r"(?P<q>['\"])?(?P<value>(?(q)"
    + _QUOTED_VALUE
    + r"|"
    + _VALUE_CHARS
    + r"))"
)

# ``Authorization`` gets its own pattern (see rationale above): the value
# routinely carries an internal SPACE (``<scheme> <credential>``) that the
# generic single-token capture above would stop at. NO scheme allowlist here,
# deliberately: RFC 7235 schemes are open-ended (``Token``, ``ApiKey``, AWS
# SigV4, ...) and an allowlist turns every unknown scheme into a leak -- the
# fallback would consume only the scheme word and leave the credential after
# the space exposed. Instead the value is up to TWO whitespace-separated
# tokens: the first (scheme or bare credential) and, when present, one more
# (the credential after a scheme word). Masking a following non-credential
# word in free prose (``authorization: abc then text`` eats ``then``) is
# accepted over-redaction -- for an Authorization value, masking too much is
# fine, leaking is not. Two tokens, not unbounded: an Authorization value has
# at most scheme + credential (comma-delimited Digest params stop at the
# ``,`` in ``_VALUE_CHARS`` regardless), and bounding the consumption keeps
# the rest of the log line diagnosable.
_AUTHORIZATION_RE: Final = re.compile(
    r"(?i)\bauthorization\b"
    + _SEP_PATTERN
    + r"(?P<value>"
    + _VALUE_CHARS
    + r"(?:[ \t]+"
    + _VALUE_CHARS
    + r")?)"
)

# A TUPLE-rendered header/field pair -- ``('X-Api-Key', 'SECRET')`` -- the
# shape ``list(headers.items())`` or a raw header dump produces. There is no
# ``:``/``=`` separator for ``_SECRET_KV_RE`` to key on, only a quoted key
# name, a comma, and a quoted value, so it needs its own pass: a quoted key
# ending in one of the secret key words (or ``authorization`` -- its
# space-bearing value is safely consumed here because the quoted-value run
# masks through to the matching close quote, spaces included), the ``,``
# separator, then the quoted value masked whole via the same linear tempered
# run as ``_SECRET_KV_RE`` (``kq``/``q`` may be DIFFERENT quote characters --
# each closes only its own). ``_mask_value`` keeps everything through the
# value's opening quote; the closing quote sits outside the match and
# survives: ``('X-Api-Key', '<redacted>')``.
_SECRET_TUPLE_RE: Final = re.compile(
    r"(?i)(?P<kq>['\"])[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization)(?P=kq)\s*,\s*(?P<q>['\"])(?P<value>"
    + _QUOTED_VALUE
    + r")"
)

# ``scheme://user:pass@host`` -- the PASSWORD half of HTTP basic-auth userinfo.
# Group 1 captures ``scheme://user`` (stopping the username at the first
# ``:``/``/``/``@``/quote/whitespace); the password itself is never captured
# into the output, only its span is consumed so it can be dropped. The username
# run is ``*`` (not ``+``) so a valid empty-username basic-auth URL
# (``https://:token@host``) still masks its token instead of leaking it.
_BASIC_AUTH_URL_RE: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s/:@'\"]*):[^\s@'\"]+@")

# A standalone Fernet-key-shaped blob: 43 urlsafe-base64 characters plus the
# one trailing ``=`` pad ``Fernet.generate_key()`` always produces (32 raw
# bytes base64-encoded), bounded on both sides so it cannot match as a
# substring of a longer base64/id-shaped run.
_FERNET_KEY_RE: Final = re.compile(r"(?<![A-Za-z0-9_=+/-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=+/-])")

_REDACTED: Final = "<redacted>"


def _mask_value(match: re.Match[str]) -> str:
    """Replace a ``_SECRET_KV_RE``/``_AUTHORIZATION_RE``/``_SECRET_TUPLE_RE``
    match's ``value`` group with :data:`_REDACTED`, keeping everything else the
    match consumed (the key name, separator, and any quote) verbatim -- "mask
    the value, keep the key name for debuggability"."""
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
    redacted = _SECRET_TUPLE_RE.sub(_mask_value, redacted)
    redacted = _FERNET_KEY_RE.sub("<redacted-fernet-key>", redacted)
    return redacted
