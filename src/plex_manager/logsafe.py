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

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import quote, quote_plus, urlsplit

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
# Independent passes, applied in sequence. Most are a total no-op on text they
# find nothing to redact in and match disjoint syntactic shapes, so their order
# would not matter -- EXCEPT the two container passes, whose ``key<sep>[`` /
# ``('key', [`` shapes overlap the authorization/kv/tuple passes (all would
# mis-grab the lone opening bracket as a one-char value and leave the bracketed
# secret behind). The container passes therefore run FIRST, consuming the whole
# container before the scalar passes can see the bracket:
#
# 1. Basic-auth URL userinfo (``scheme://user:pass@host``): the PASSWORD half
#    of ``user:pass@`` is masked; the username and the rest of the url survive
#    (diagnosable: which url, which account -- never a byte of the password).
# 1a. CONTAINER-WRAPPED values -- ``'X-Api-Key': ['SECRET']`` / ``('SECRET',)``
#    (``key<sep>[...]`` or ``key<sep>(...)``) and ``('X-Api-Key', ['SECRET'])``
#    (tuple pair with a container value) -- the shape a multi-valued header/
#    form mapping produces. The whole bracketed container is masked
#    (escape-aware quoted elements, so an embedded closer cannot end it early;
#    possessive quantifiers, so no ReDoS; optional close bracket, so a
#    truncated container is consumed to end -- fail closed; a nested group arm,
#    so a list-of-lists is consumed at any depth). ``authorization`` is in the
#    key set (a multi-valued Authorization dump). These run before the scalar
#    passes (see the ordering note above).
# 2. ``Authorization`` header values specifically: unlike every other secret
#    key below, an Authorization value routinely carries internal SPACES and
#    COMMAS (``Basic <base64>``, ``Bearer <token>``, parameterized ``Digest
#    username="u", nonce="...", response="..."``) -- a bounded token capture
#    leaks whatever sits past its bound (a two-token capture left Digest's
#    later parameters exposed). NO scheme allowlist (RFC 7235 schemes are
#    open-ended; an allowlist turns every unknown scheme into a leak) and NO
#    token bound: a QUOTED value is consumed escape-aware through its matching
#    close quote; an UNQUOTED value is consumed through to the LINE BOUNDARY --
#    in a raw header line the value IS the rest of the line. Masking trailing
#    prose (``authorization: abc then text``) is accepted over-redaction: for
#    an Authorization value, masking too much is fine, leaking is not.
# 3. Every other secret-key-shaped ``key<sep>value`` pair -- ``api_key``/
#    ``apikey``, ``access_token``, ``auth_token``, a bare ``token`` (also
#    matches ``X-Plex-Token``/``X-Api-Key``: the hyphen before the final word
#    is a non-word boundary, so the alternation matches the LAST word of a
#    hyphenated header name and the untouched ``X-Plex-``/``X-Api-`` prefix is
#    simply copied through by ``re.sub`` unmodified), ``fernet_key``,
#    ``passkey`` (the private-tracker-URL shape ``safe_guid`` already covers at
#    the call site; this is the same shape's defense-in-depth backstop),
#    ``password``/``passwd``/``pwd``, ``secret``. A bounded, lazy
#    ``[\w-]{0,64}?`` prefix in front of the alternation lets the key word be
#    the SUFFIX of a longer underscore/hyphen-joined identifier, so THIS app's
#    real settings-field names -- ``tmdb_api_key``, ``prowlarr_api_key``,
#    ``plex_token``, ``qbittorrent_password``, ``app_api_key``,
#    ``PLEX_MANAGER_FERNET_KEY`` -- match even though ``_`` is a word char that
#    would otherwise defeat a bare ``\b``; the whole field name (prefix
#    included) survives in the output, only the value is masked. The value
#    capture handles three shapes:
#      * QUOTED (``key='...'`` / ``key="..."``, optionally bytes ``key=b'...'``):
#        consumed through to the MATCHING closing quote by the ESCAPE-AWARE run
#        -- a backslash-escaped quote (``password="abc\"SECRET"``) does not end
#        it, nor does the opposite quote character -- so no suffix of a quoted
#        credential is left behind ``<redacted>`` (see :data:`_QUOTED_VALUE`).
#      * UNQUOTED TOKEN-family (api keys/tokens/passkeys -- machine-generated,
#        urlsafe by construction, never containing whitespace/``&``/``,``):
#        exactly one token, stopping at whitespace/``&``/``,``/``;``/quote/
#        closing bracket -- fail-closed BECAUSE of the value's alphabet, and
#        keeps a URL query diagnosable (``?api_key=<redacted>&language=en``).
#      * UNQUOTED FREEFORM-family (passwords/secrets -- human-chosen, ANY
#        alphabet): consumed through to the LINE BOUNDARY (``password=abc def``
#        masks whole; a token bound would leak the suffix past the first
#        space). Over-redacting the rest of the line is accepted; leaking a
#        password suffix is not. See :data:`_FREEFORM_VALUE` for the
#        ``<redacted>`` re-match guard.
# 4. The same secret key names in TUPLE rendering -- ``('X-Api-Key', 'SECRET')``
#    or the bytes form ``(b'X-Api-Key', b'SECRET')`` (``httpx.Headers.raw``) --
#    the shape ``list(headers.items())``/raw header dumps produce -- which has
#    no ``:``/``=`` separator for pass 3 to key on: a quoted key name ending in
#    a secret key word (or ``authorization``), a comma, then the quoted value
#    masked whole.
# 5. COOKIE/session credentials -- ``Cookie: plexmgr.session=SECRET``,
#    ``Set-Cookie: SID=SECRET``, qBittorrent 5.2's ``QBT_SID_<port>=SECRET`` --
#    keyed on a cookie name containing ``session``/``sid`` at a word/separator
#    boundary with an adjacent ``=`` (so the prose word "session" never
#    false-matches); the value is masked up to the ``;`` attribute separator.
# 5a. The same cookie names rendered as a JAR/MAPPING ``repr()`` instead of a
#    raw header line -- ``{'plexmgr.session': 'SECRET'}``,
#    ``{'QBT_SID_8080': 'SECRET'}`` (see :data:`_COOKIE_JAR_RE`). Neither
#    token is ever a settings-store value (the session cookie is only ever a
#    HASH at rest; qBittorrent's SID lives only in the adapter's in-memory
#    cookie jar), so :func:`redact_known_secrets`'s value-based pass (below)
#    can never mask this shape -- this SHAPE rule is the only barrier for it.
# 5b. The same cookie names rendered as a ``http.cookies.SimpleCookie`` repr --
#    ``<SimpleCookie: plexmgr.session='SECRET'>`` (unquoted name, ``=``, quoted
#    value; see :data:`_SIMPLE_COOKIE_RE`) -- which neither the raw-header pass
#    (5) nor the dict-repr pass (5a) recognizes. Same "cookie token is never a
#    settings value, so a shape rule is the only barrier" reasoning as 5a.
#
# A final, UNCONDITIONAL pass (no key name involved at all) catches a
# Fernet-key-SHAPED standalone blob wherever it appears: ``cryptography``'s
# ``Fernet.generate_key()`` is always exactly 44 urlsafe-base64 characters
# ending in one ``=`` pad -- distinctive enough to redact on shape alone, which
# matters because the key is loaded from a file (``<data_dir>/secret.key``),
# never assigned a "key name" of its own in a log line to key off of.
# Regex alternations of secret-bearing KEY NAMES (not credentials themselves),
# split by the ALPHABET of the value they name -- the split decides how far the
# unquoted-value capture may safely stop (see pass 3 above).
_TOKEN_KEY_PATTERN: Final = (
    r"api[-_]?key|access[-_]?token|auth[-_]?token|token|passkey|fernet[-_]?key"  # noqa: S105
)
_FREEFORM_KEY_PATTERN: Final = r"password|passwd|pwd|secret"
_SECRET_KEY_PATTERN: Final = _TOKEN_KEY_PATTERN + r"|" + _FREEFORM_KEY_PATTERN

# The UNQUOTED TOKEN-family value: any run of characters that is not
# whitespace, a literal delimiter (`&`, `,`, or `;` -- the cookie-attribute
# separator in ``Set-Cookie: SID=value; Path=/``), a quote, or a closing
# bracket -- i.e. exactly one token. Correct AND fail-closed for a
# machine-generated urlsafe credential (see the key-family split above).
_VALUE_CHARS: Final = r"[^\s&,;'\"}\)\]]+"
# The UNQUOTED FREEFORM-family value: everything through to the line boundary
# (a human-chosen password may contain spaces/commas/``&``/anything, so no
# earlier stop is fail-closed). The ``(?!\s*<redacted>)`` guard keeps a LATER
# pass from re-consuming a value an EARLIER pass (e.g. the container pass)
# already masked and then swallowing the text after it; ``\s*`` inside the
# lookahead so separator-whitespace backtracking cannot sidestep the guard.
_FREEFORM_VALUE: Final = r"(?!\s*<redacted>)[^\r\n]*"
# A bytes-repr prefix -- ``b'...'``/``b"..."``, the shape ``httpx.Headers.raw``
# dumps produce -- consumed (and so preserved verbatim around the masked value)
# only when a quote actually follows, so a bare unquoted value starting with a
# literal ``b`` is never split as ``b<redacted>``.
_BYTES_PREFIX: Final = r"(?:b(?=['\"]))?"

# ``key<sep>value`` for every secret key name EXCEPT ``Authorization`` (below).
# The separator stops BEFORE the value's opening quote so that quote can be
# captured into ``q`` and a quoted value consumed through to its matching
# close:
#   * leading ``[\w-]{0,64}?`` -- a bounded, lazy prefix so a secret key word can
#     be the tail of a longer ``_``/``-``-joined field name (``plex_token``,
#     ``tmdb_api_key``); ``_`` is a word char, so a bare ``\b`` before ``token``
#     would never fire on ``plex_token``. Bounded to keep the scan linear.
#     The FREEFORM key alternatives are captured into ``fkey`` so the value
#     conditional below can pick the line-boundary consumption for them.
#   * ``(?P<q>['\"])?`` -- the value's optional opening quote (after an optional
#     bytes ``b`` prefix, see :data:`_BYTES_PREFIX`).
#   * ``(?P<value> ... )`` -- when ``q`` matched, the ESCAPE-AWARE run
#     :data:`_QUOTED_VALUE` consumes everything up to (not past) the matching
#     UNESCAPED closing quote. It models the actual quoting grammar rather than
#     chasing it: a backslash escapes the next character, so an escaped matching
#     quote (``password="abc\"SECRET"`` -- the shape a JSON/repr dump of a
#     ``qbittorrent_password`` containing a quote produces) does NOT end the
#     value and the ``SECRET`` suffix cannot leak (the P1 that a
#     matching-quote-only run left open); the OPPOSITE quote character
#     (``password="abc'def"``, which ``SettingsUpdate`` accepts) is likewise
#     consumed, never a split point; and a raw newline or a truncated
#     never-closed value is swallowed whole (fail closed). Otherwise (no ``q``)
#     the unquoted branch by key family: freeform -> line boundary, token ->
#     single token (see the key-family split above). The quoted run is linear
#     and backtracking-free: its two branches are mutually exclusive (branch 1
#     requires a backslash, branch 2 excludes it), so every position has
#     exactly one path -- the redactor must never hand a ReDoS (this file's
#     tests once drew a CodeQL finding). ``_mask_value`` drops from the value
#     start on, so the closing quote -- deliberately left OUTSIDE the match
#     (the run cannot cross it) -- survives around ``<redacted>``.
_KV_SEP_PATTERN: Final = r"['\"]?\s*[:=]\s*"
# Escape-aware: ``\\[\s\S]`` consumes a backslash-escaped pair (the escaped
# char, matching quote included, never terminates); ``(?!(?P=q))[^\\]`` consumes
# any other non-closing, non-backslash char (newlines included -- fail closed on
# a multi-line/truncated value). Mutually exclusive branches -> linear, no ReDoS.
_QUOTED_VALUE: Final = r"(?:\\[\s\S]|(?!(?P=q))[^\\])*"
_SECRET_KV_RE: Final = re.compile(
    r"(?i)\b(?P<key>[\w-]{0,64}?"
    + r"(?:(?P<fkey>"
    + _FREEFORM_KEY_PATTERN
    + r")|(?:"
    + _TOKEN_KEY_PATTERN
    + r"))\b)"
    + _KV_SEP_PATTERN
    + _BYTES_PREFIX
    + r"(?P<q>['\"])?(?P<value>(?(q)"
    + _QUOTED_VALUE
    + r"|(?(fkey)"
    + _FREEFORM_VALUE
    + r"|"
    + _VALUE_CHARS
    + r")))"
)

# ``Authorization`` gets its own pattern (see pass 2 above): the value
# routinely carries internal SPACES and COMMAS (``<scheme> <credential>``,
# parameterized ``Digest username="u", nonce="...", response="..."``) that any
# bounded token capture would leak past (a two-token bound left Digest's later
# parameters exposed). NO scheme allowlist, deliberately: RFC 7235 schemes are
# open-ended (``Token``, ``ApiKey``, AWS SigV4, ...) and an allowlist turns
# every unknown scheme into a leak. The value follows the same quoted/unquoted
# discrimination as ``_SECRET_KV_RE``'s freeform branch: QUOTED (a dict-repr
# ``'Authorization': 'Bearer X'``, bytes form included) -> escape-aware through
# the matching close quote, leaving the pair's neighbors intact; UNQUOTED (a
# raw header line) -> through to the line boundary, because a header's value
# IS the rest of the line. Over-redacting trailing prose is accepted; leaking
# any Authorization parameter is not.
_AUTHORIZATION_RE: Final = re.compile(
    r"(?i)\b(?P<key>authorization)\b['\"]?\s*[:=]\s*"
    + _BYTES_PREFIX
    + r"(?P<q>['\"])?(?P<value>(?(q)"
    + _QUOTED_VALUE
    + r"|"
    + _FREEFORM_VALUE
    + r"))"
)

# A TUPLE-rendered header/field pair -- ``('X-Api-Key', 'SECRET')``, or the
# bytes form ``(b'X-Api-Key', b'SECRET')`` that ``httpx.Headers.raw`` dumps
# produce (the leading ``b?`` on both the key and value quote) -- the shape
# ``list(headers.items())`` or a raw header dump produces. There is no
# ``:``/``=`` separator for ``_SECRET_KV_RE`` to key on, only a quoted key
# name, a comma, and a quoted value, so it needs its own pass: a quoted key
# ending in one of the secret key words (or ``authorization`` -- its
# space-bearing value is safely consumed here because the quoted-value run
# masks through to the matching close quote, spaces included), the ``,``
# separator, then the quoted value masked whole via the same linear escape-
# aware run as ``_SECRET_KV_RE`` (``kq``/``q`` may be DIFFERENT quote
# characters -- each closes only its own). ``_mask_value`` keeps everything
# through the value's opening quote; the closing quote sits outside the match
# and survives: ``('X-Api-Key', '<redacted>')``.
_SECRET_TUPLE_RE: Final = re.compile(
    r"(?i)b?(?P<kq>['\"])(?P<key>[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization))(?P=kq)\s*,\s*b?(?P<q>['\"])(?P<value>"
    + _QUOTED_VALUE
    + r")"
)

# A CONTAINER-WRAPPED value -- ``X-Api-Key': ['SECRET']`` / ``api_key=('a',)``
# -- the shape a header/form mapping with multi-valued entries produces
# (``dict(multidict)`` then repr; list OR tuple). The generic ``_SECRET_KV_RE``
# unquoted-value token stops at the opening bracket and would leave the quoted
# secret behind ``<redacted>['SECRET']``, so the bracketed container needs its
# own pass that masks the WHOLE container (bracket included). The body is
# built from :data:`_CONTAINER_ELEM` -- one escape-aware quoted string (single
# or double, whose embedded closer therefore cannot prematurely end the
# container) or one non-quote, non-bracket char -- and :data:`_CONTAINER_BODY`
# is a run of those elements OR a nested ``[...]``/``(...)`` group. The
# nested-group arm matters: a list of lists (``[['a'], ['SECRET']]``) would
# otherwise stop at the FIRST inner closer and leave the rest -- secret
# included -- behind ``<redacted>``. Because a quoted element is itself a
# ``_CONTAINER_ELEM``, every credential-bearing string is consumed regardless
# of nesting depth (only trailing bare closers are ever left behind, and those
# carry no secret) -- fail closed. Every group is possessive (``*+``/``?+``)
# with mutually exclusive branches (an element never starts with an opener),
# so there is zero backtracking (no ReDoS); the closing quote/bracket of each
# element is optional, so an unterminated/truncated container is consumed to
# end (fail closed). ``authorization`` is in the key alternation (a
# multi-valued Authorization dump); these passes run BEFORE
# ``_AUTHORIZATION_RE``/``_SECRET_KV_RE``/``_SECRET_TUPLE_RE`` in
# :func:`redact_secrets` so those never see the opening bracket to mis-grab
# (the shapes are NOT disjoint -- both start ``key<sep>`` -- so, unlike the
# other passes, order matters here).
_CONTAINER_ELEM: Final = r"(?:\"(?:[^\"\\]|\\.)*+\"?+|'(?:[^'\\]|\\.)*+'?+|[^'\"\[\]\(\)])"
_CONTAINER_BODY: Final = r"(?:" + _CONTAINER_ELEM + r"|[\[\(]" + _CONTAINER_ELEM + r"*+[\]\)]?+)*+"
_SECRET_CONTAINER_RE: Final = re.compile(
    r"(?i)\b(?P<key>[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization)\b)"
    + _KV_SEP_PATTERN
    + r"(?P<value>[\[\(]"
    + _CONTAINER_BODY
    + r"[\]\)]?)"
)
# The container-value shape with NO ``:``/``=`` separator -- a tuple-rendered
# pair whose value is a list/tuple, ``('X-Api-Key', ['SECRET'])`` /
# ``('X-Api-Key', ('SECRET',))`` -- mirroring :data:`_SECRET_TUPLE_RE` but
# with a bracketed value (and the same optional bytes ``b`` key prefix).
_SECRET_CONTAINER_TUPLE_RE: Final = re.compile(
    r"(?i)b?(?P<kq>['\"])(?P<key>[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization))(?P=kq)\s*,\s*(?P<value>[\[\(]"
    + _CONTAINER_BODY
    + r"[\]\)]?)"
)

_SELF_VERIFY_SHAPE_RES: Final[tuple[re.Pattern[str], ...]] = (
    _SECRET_CONTAINER_RE,
    _SECRET_CONTAINER_TUPLE_RE,
    _AUTHORIZATION_RE,
    _SECRET_KV_RE,
    _SECRET_TUPLE_RE,
)

# Cookie/session credentials (issue #153 follow-up): a Cookie/Set-Cookie header
# dump can persist a live session token -- this app's ``plexmgr.session`` browser
# auth cookie and qBittorrent's upstream ``SID`` session cookie (named
# ``QBT_SID_<port>`` on qBittorrent 5.2, the shape the adapter's
# ``_session_cookie_header`` emits) -- that the api-key/token/password key names
# above do not cover. A cookie is ALWAYS ``name=value``, so the pass keys on a
# cookie NAME containing ``session``/``sid`` followed either directly by ``=``
# or by a ``.``/``_``/``-``-separated suffix (``QBT_SID_8080=``) and masks its
# value (stopping at the cookie ``;`` attribute separator, so ``Path``/
# ``HttpOnly`` stay diagnosable; NOT at ``&`` -- RFC 6265 permits ``&`` in a
# cookie value, and leaking a suffix past it would be an open door). The ``=``
# is REQUIRED (no ``[:=]`` alternation, no surrounding space) and any extra
# name characters after ``session``/``sid`` must follow a separator, precisely
# so common English prose -- "refreshing session: ...", "session established",
# "consider", "processing" -- never false-matches; only a literal cookie-name
# assignment does.
_COOKIE_RE: Final = re.compile(
    r"(?i)\b[\w.]*(?:session|sid)(?:[._-][\w.-]*)?=(?P<value>[^\s;,'\"}\)\]]+)"
)

# Cookie/session credentials rendered as a JAR/MAPPING ``repr()`` (issue #270,
# part 1) -- ``outgoing cookies: {'plexmgr.session': '<token>'}``, or
# qBittorrent's ``{'QBT_SID_8080': '<token>'}`` -- the shape ``dict(jar)``/
# ``repr(cookies)`` produces when a request/response cookie jar is logged
# whole, e.g. for HTTP-client debugging. ``_COOKIE_RE`` above requires a direct
# ``name=value`` assignment (a raw ``Cookie:``/``Set-Cookie:`` header line) and
# does NOT recognize this quoted-key-colon-quoted-value dict shape, so it sails
# through untouched. This is a SHAPE rule, deliberately independent of any
# settings-derived value: neither of these tokens is ever stored in this app's
# settings table to begin with -- ``plexmgr.session`` is only ever a HASH
# (:class:`~plex_manager.models.AuthSession`'s ``token_hash``, the plaintext
# session token itself is never persisted anywhere) and qBittorrent's ``SID``
# is held only in the adapter's in-memory cookie jar -- so
# :func:`redact_known_secrets`'s value-based pass, whose input is exactly
# "this app's CONFIGURED secret settings", can never see either value and can
# never mask this shape. A shape rule is the only rule that CAN.
#
# Keys on a QUOTED cookie name containing ``session``/``sid`` (the same
# name-shape ``_COOKIE_RE`` recognizes, reused verbatim so both passes accept
# exactly the same cookie names) immediately followed by a dict-repr ``:``
# separator and a quoted value -- masking the value through to its matching
# close quote via the same escape-aware :data:`_QUOTED_VALUE` run every other
# quoted-value pass in this module uses (a value containing its own quote,
# e.g. a JSON-escaped token, is not split early). The cookie NAME survives
# (diagnosable, matching every other pass's "mask the value, keep the key"
# convention) -- only the token itself is dropped.
_COOKIE_JAR_RE: Final = re.compile(
    r"(?i)(?P<kq>['\"])[\w.]*(?:session|sid)(?:[._-][\w.-]*)?(?P=kq)\s*:\s*"
    + _BYTES_PREFIX
    + r"(?P<q>['\"])(?P<value>"
    + _QUOTED_VALUE
    + r")"
)

# Cookie/session credentials rendered as a ``http.cookies.SimpleCookie`` repr
# (#292 item -- SimpleCookie repr leakage) -- ``<SimpleCookie: plexmgr.session=
# 'SECRET' QBT_SID_8080='SECRET'>`` -- the shape a ``BaseCookie``/``SimpleCookie``
# object logs as when repr'd whole (space-separated ``name='value'`` pairs, the
# cookie NAME UNQUOTED, the value QUOTED). Neither ``_COOKIE_RE`` (it requires an
# UNQUOTED ``name=value`` and its value class stops dead at the opening ``'``) nor
# ``_COOKIE_JAR_RE`` (it keys on a QUOTED key + ``:`` dict separator) recognizes
# this unquoted-key/``=``/quoted-value form, so it needs its own pass. Keys on the
# same ``session``/``sid`` cookie-NAME shape the other two cookie passes accept
# (reused verbatim), an ``=``, then a quoted value masked through its matching
# close quote via the shared escape-aware :data:`_QUOTED_VALUE` run. Like every
# cookie token, this value is never a settings-store value (a ``plexmgr.session``
# is only ever a hash at rest; qBittorrent's ``SID`` lives only in the adapter's
# in-memory jar), so this SHAPE rule is the ONLY barrier for the repr form.
_SIMPLE_COOKIE_RE: Final = re.compile(
    r"(?i)\b[\w.]*(?:session|sid)(?:[._-][\w.-]*)?=(?P<q>['\"])(?P<value>" + _QUOTED_VALUE + r")"
)

# ``scheme://user:pass@host`` -- the PASSWORD half of HTTP basic-auth userinfo.
# Group 1 captures ``scheme://user`` (stopping the username at the first
# ``:``/``/``/``@``/quote/whitespace); the password itself is never captured
# into the output, only its span is consumed so it can be dropped. The username
# run is ``*`` (not ``+``) so a valid empty-username basic-auth URL
# (``https://:token@host``) still masks its token instead of leaking it.
#
# The password run is greedy up to the LAST ``@`` still inside the authority --
# it excludes only ``/``/``?``/``#`` (the authority terminators, so the host and
# any path/query ``@`` are never swallowed), NOT ``@`` itself, so a password that
# CONTAINS a raw ``@`` (``scheme://user:p@ss@host``) is masked WHOLE rather than
# up to its own first internal ``@`` (issue #270's shape-grammar gap; #292 item
# 3). Because the run has no length floor of its own, this also masks a SHORT
# ``@``-bearing password the value-based pass skips (#292 item 4/short-password
# gap), and re-masks a LEGACY row the old first-``@`` regex already mangled into
# ``user:<redacted>@ssremainder@host`` -- the greedy run consumes the exposed
# ``ssremainder`` too, recovering the partially-redacted row (#292 item 3). The
# class + single required ``@`` is linear (no nested quantifier -> no ReDoS).
_BASIC_AUTH_URL_RE: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s/:@'\"]*):[^\s/?#'\"]+@")

# A standalone Fernet-key-shaped blob: 43 urlsafe-base64 characters plus the
# one trailing ``=`` pad ``Fernet.generate_key()`` always produces (32 raw
# bytes base64-encoded), bounded on both sides so it cannot match as a
# substring of a longer base64/id-shaped run. A preceding ``=`` is deliberately
# ALLOWED (it is absent from the lookbehind class): base64 padding only ever
# terminates a run, so a ``=`` before a 44-char blob means a NEW token -- and
# rejecting it left the master key unredacted in exactly the env/config-dump
# rendering (``PLEX_MANAGER_FERNET_KEY=<key>``, ``some_var=<key>``) an operator
# is most likely to paste into a log.
_FERNET_KEY_RE: Final = re.compile(r"(?<![A-Za-z0-9_+/-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=+/-])")

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
    # Container-wrapped values FIRST: their ``key<sep>[``/``key<sep>(`` shapes
    # overlap the authorization/kv/tuple passes below (all would mis-grab the
    # lone opening bracket), so the container must be consumed before them
    # (see _SECRET_CONTAINER_RE).
    redacted = _SECRET_CONTAINER_RE.sub(_mask_value, redacted)
    redacted = _SECRET_CONTAINER_TUPLE_RE.sub(_mask_value, redacted)
    redacted = _AUTHORIZATION_RE.sub(_mask_value, redacted)
    redacted = _SECRET_KV_RE.sub(_mask_value, redacted)
    redacted = _SECRET_TUPLE_RE.sub(_mask_value, redacted)
    redacted = _COOKIE_RE.sub(_mask_value, redacted)
    redacted = _COOKIE_JAR_RE.sub(_mask_value, redacted)
    redacted = _SIMPLE_COOKIE_RE.sub(_mask_value, redacted)
    redacted = _FERNET_KEY_RE.sub("<redacted-fernet-key>", redacted)
    return redacted


# --------------------------------------------------------------------------- #
# redact_known_secrets (issue #268): a VALUE-based redaction pass, complementary
# to :func:`redact_secrets`'s SHAPE-based grammar above. ``redact_secrets`` asks
# "does this text contain something shaped like a credential" (a key name next
# to a value, a basic-auth URL, a cookie assignment, ...) -- a denylist of known
# RENDERINGS that, per that function's own module comment, keeps growing a new
# rule every time a novel shape turns up. Issue #270 found two such gaps: a
# cookie-jar/mapping-repr dump (``{'plexmgr.session': 'SECRET'}``) and a
# basic-auth URL whose password itself contains a raw ``@``
# (``scheme://user:p@ss@host``).
#
# ``redact_known_secrets`` asks a categorically different, STRUCTURALLY
# STRONGER question: "does this text contain one of THIS APP'S OWN, CURRENTLY
# CONFIGURED secret VALUES" -- Plex/Prowlarr/TMDB/qBittorrent credentials the
# settings store holds (already decrypted in-process by the caller; this
# module has no DB access and never acquires one). A value-shaped question has
# no grammar to outrun: it doesn't matter whether the value sits inside a
# ``key=value`` pair, a cookie-jar ``repr()``, a basic-auth URL whose password
# itself contains ``@``, a third-party library's own log line, or bare mid-
# prose -- if the EXACT bytes of a known secret appear anywhere in the text,
# they are masked, regardless of the surrounding shape. This closes #270's
# basic-auth-``@`` gap categorically -- a settings-configured password IS the
# value that leaked, so the value-based pass sees it no matter how the
# surrounding URL mangles the shape grammar's own delimiter-based parsing.
#
# The cookie-jar/mapping-repr gap is DIFFERENT in kind, not just in shape: the
# two credentials that dump through it are NEVER settings-store values in the
# first place. ``plexmgr.session`` (:class:`~plex_manager.models.AuthSession`)
# persists only a HASH of its token -- the plaintext never exists anywhere
# this pass could read it from -- and qBittorrent's ``SID`` cookie lives only
# in the adapter's in-memory ``httpx`` cookie jar, never written to the
# settings table at all. A pass that only ever sees "this app's configured
# secret SETTINGS" structurally cannot mask either one: there is no settings
# value to hand it. That gap is closed instead by a dedicated SHAPE rule,
# :data:`_COOKIE_JAR_RE` (pass 5a in :func:`redact_secrets`, above) -- the one
# category where the shape grammar remains the ONLY possible barrier, because
# the credential never reaches this function's input at all.
#
# Deliberately COMPLEMENTARY, not a replacement: this pass can only mask a
# value it was HANDED, so a secret this app never stored in settings (a
# user-pasted tracker/indexer token typed into a correction flow, an ad-hoc
# credential a future adapter accepts but never persists, or either of the two
# cookie-jar tokens above) is invisible to it -- ``redact_secrets``'s key-name
# and shape rules are still the only barrier for that class, and both passes
# run at every capture/export boundary (see ``log_capture_service._capture``
# and ``web/routers/ops.py``). Reuses :data:`_REDACTED` -- the same
# placeholder :func:`redact_secrets` emits, so a reader of a log line can
# never tell WHICH pass masked a given span, nor does it need to: either way,
# the answer is "a secret was here".

#: The minimum LENGTH a configured secret value must have before this pass will
#: ever mask it. Without a floor, a short value -- an early-beta qBittorrent
#: password like ``"admin"`` (5 chars) users are warned but not blocked from
#: setting, or a stub/test API key -- would exact-match countless innocent
#: substrings of ordinary log prose (a word, a path segment, a hex fragment of
#: an unrelated id) and redact them, which is exactly the "over-redaction of
#: common substrings" the issue calls out to guard against. 8 is chosen as a
#: floor beneath which NO real API key/token/Fernet-derived credential this app
#: issues or accepts ever falls (Prowlarr/TMDB keys and qBittorrent-generated
#: passwords are all far longer), while still being long enough that an 8+
#: character OPERATOR-CHOSEN password is rare as an accidental substring of
#: unrelated log text. A value shorter than this is silently skipped by this
#: pass -- ``redact_secrets``'s shape grammar is the backstop for short
#: secrets sitting in a recognized ``key=value``/header/cookie shape.
_MIN_SECRET_VALUE_LENGTH: Final = 8

#: A word/identifier character (matches a ``[\w-]`` run's members). Used by the
#: key-name guard below to expand a value match leftward to the start of the
#: identifier it sits in, so the character BEFORE that identifier can be checked.
_IDENT_CHAR_RE: Final = re.compile(r"[\w-]")
#: The recognized secret KEY WORDS (the shape grammar's own alternation, plus
#: ``authorization``), fullmatched case-insensitively against a value occurrence
#: by the key-name guard (:func:`_mask_known_value`): ONLY an occurrence that IS
#: exactly one of these words may ever be spared. Any longer or different match
#: is masked unconditionally, so a secret merely EMBEDDED in a bigger token
#: before a separator (``payload=x<secret>=v``) can never ride the guard into
#: clear text.
_KEY_WORD_FULL_RE: Final = re.compile(r"(?i)(?:" + _SECRET_KEY_PATTERN + r"|authorization)")
#: Detects a genuine URL-AUTHORITY prefix ending exactly where the identifier a
#: spared-candidate occurrence sits in begins: a ``scheme://`` followed by any
#: run of non-terminator characters (the userinfo/host region -- ``:`` and ``@``
#: included), as in ``https://password:x@host``. There the occurrence is a
#: USERINFO/host component, not a key name, and the guard refuses to spare.
_AUTHORITY_PREFIX_RE: Final = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/'\"?#]*\Z")
#: How far back from the identifier the authority scan looks. Any real basic-
#: auth URL prefix is far shorter; bounding it keeps the spare callback O(window).
_AUTHORITY_SCAN_WINDOW: Final = 512
#: How far back from a spared-candidate occurrence the SELF-VERIFICATION scan
#: starts its applicable shape search. A shape-pass match whose KEY contains the
#: occurrence starts at most the key prefix cap (64) before it, plus slack.
_SHAPE_KEY_SCAN_WINDOW: Final = 96
#: Maximum text span AFTER the separator probe's end (i.e. past the value's
#: start) included in each exact grammar search. All five grammars accept a
#: truncated value, so this is enough to prove the later whole-line shape pass
#: starts at this key without scanning its full (possibly very long) value.
_SHAPE_VALUE_SCAN_WINDOW: Final = 512
#: The cheap ANCHORED separator probe run before the heavier exact shape
#: grammars: from the occurrence's end, an optional quote, whitespace, a
#: ``:``/``=`` (or the tuple form's quote-comma), and the whitespace after it --
#: exactly the region every self-verification grammar's own separator admits
#: (``_KV_SEP_PATTERN``'s ``['\"]?\s*[:=]\s*`` / ``_SECRET_TUPLE_RE``'s
#: ``(?P=kq)\s*,\s*``). It rejects ordinary prose occurrences such as
#: ``password password ...`` in O(1) without searching any grammar, and on a hit
#: its match END locates the value's start so the grammar scan below can be
#: given a tight end bound.
#:
#: DELIBERATELY UNBOUNDED (``.match``-anchored, no end bound), so the probe can
#: never reject a separator gap the grammars themselves would accept -- a
#: renderer- or attacker-inserted run of hundreds of blanks between the key and
#: its ``=`` must not strip the key anchor ``redact_secrets`` masks the paired
#: value off of (Codex PR #376 round 2). This is still linear in message size,
#: BY CONSTRUCTION rather than by window: a key-word occurrence is a run of word
#: characters, so no occurrence can begin inside another occurrence's
#: pure-whitespace probe region -- the probed regions are pairwise disjoint and
#: their total is bounded by the message length. The probe itself backtracks
#: O(1) per whitespace position (``\s*`` then a one-char class), so the whole
#: miss path stays amortized O(n).
_KEY_SHAPE_SUFFIX_RE: Final = re.compile(r"(?:['\"]?\s*[:=]\s*|['\"]\s*,\s*)")


#: Maximum length for a newly generated depth-2 candidate. Baseline raw and
#: one-step variants are never filtered by this limit.
_MAX_NESTED_CANDIDATE_LENGTH: Final = 16 * 1024
#: Maximum number of newly generated depth-2 candidates per configured secret.
_MAX_NESTED_CANDIDATES: Final = 64


#: One hex-bearing escape inside an encoded rendering: a ``%XX`` percent-escape,
#: a ``\\uXXXX`` JSON/repr unicode escape, or an ``\\xXX`` repr byte escape.
#: Emitters differ in hex case -- ``quote`` emits ``%2F``, ``json.dumps`` emits
#: ``\\u00e4``, other serializers emit ``%2f``/``\\u00E4``, and per-escape MIXES
#: of these are all decode-identical (#292 round-2 percent finding + round-3
#: unicode-escape finding). :func:`_variant_regex` therefore compiles each
#: escape's hex digits into case-insensitive character classes instead of
#: enumerating spellings (which could never cover per-escape mixes).
_HEX_ESCAPE_RE: Final = re.compile(
    r"%25[0-9A-Fa-f]{2}|%[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|\\x[0-9A-Fa-f]{2}"
)


def _variant_regex(variant: str) -> str:
    """A regex source matching ``variant`` literally EXCEPT that every
    hex-bearing escape's digits match case-insensitively -- ``%2F`` ->
    ``%2[Ff]``, ``\\u00e4`` -> ``\\u00[Ee]4``-style per-digit classes, ``\\x1b``
    likewise -- so every per-escape case mix of a percent-encoded or JSON/repr-
    escaped rendering is masked, not just the spelling the local encoder emits.
    A nested ``%25XX`` spelling is recognized as an outer percent escape wrapping
    an inner escape, so the inner hex digits vary in case without making ordinary
    literal characters case-insensitive. No generic recursive decoding is used.
    Non-escape characters are :func:`re.escape`d, so the rest of the value stays
    an exact literal match (unencoded characters ARE case-significant). Character
    classes only, no added quantifiers: as linear (ReDoS-free) as the plain escape."""
    parts: list[str] = []
    pos = 0
    for m in _HEX_ESCAPE_RE.finditer(variant):
        parts.append(re.escape(variant[pos : m.start()]))
        token = m.group()
        lead_len = 1 if token.startswith("%") else 2  # "%" vs "\\u" / "\\x"
        classes = "".join(
            d if d.isdigit() else f"[{d.upper()}{d.lower()}]" for d in token[lead_len:]
        )
        parts.append(re.escape(token[:lead_len]) + classes)
        pos = m.end()
    parts.append(re.escape(variant[pos:]))
    return "".join(parts)


#: Bare ``%XX`` escape used to GENERATE a concrete lowercase-hex percent
#: spelling (see :func:`_secret_value_variants`'s nested base64-of-percent
#: family). Distinct from :data:`_HEX_ESCAPE_RE`, which is used to build a
#: case-INSENSITIVE matcher at scan time -- this one substitutes into an actual
#: candidate string, so it only needs the plain percent form.
_PERCENT_HEX_RE: Final = re.compile(r"%[0-9A-Fa-f]{2}")


def _secret_value_variants(value: str) -> frozenset[str]:
    """Every literal RENDERING of ``value`` this pass will mask -- the raw value
    plus the encoded spellings the same secret arrives in when a client, proxy,
    or serializer re-renders it. The frozenset deduplicates, so a value with no
    reserved/non-ASCII characters (identical to several of its own encodings)
    simply contributes fewer distinct entries; empties are dropped. Variants:

    * **Percent-encoding** -- a password/token embedded in a query string or
      userinfo often arrives percent-encoded (e.g. a password containing ``@``
      or ``&``). Two variants are generated -- ``quote`` (``%20`` for space) and
      ``quote_plus`` (``+`` for space) -- and hex CASE is not enumerated here at
      all: :func:`_variant_regex` compiles each ``%XX`` escape case-insensitively
      at match time, covering upper, lower, AND per-escape mixed spellings
      (#292 encoding-variant items + round-2 mixed-case finding).
    * **Base64** -- ``cryptography``/third-party clients frequently base64 a
      credential for a header or config dump. Raw and first-layer percent inputs
      are covered as STANDARD and URL-SAFE alphabets, each in PADDED and UNPADDED
      form (a ``=``-stripped/base64url rendering is common in JWT-style and query
      contexts; #292 items).
    * **Bounded composed encodings** -- each first-layer percent spelling also
      gets one explicit second percent transform and one base64 transform; each
      raw base64 spelling gets one percent transform. The base64-of-percent leg
      base64s BOTH the canonical (``quote``/``quote_plus``) uppercase-hex
      spelling and a uniform-lowercase-hex spelling of the same percent text --
      unlike the pure-percent legs, this one wraps the ``%XX`` escapes in
      base64, which turns them into an opaque blob :func:`_variant_regex`'s
      case-insensitive hex classes can no longer reach at match time, so the
      lowercase spelling a client that emits lowercase percent-hex (``%2f``)
      before base64-ing must be generated explicitly up front (#381). No
      derived value is transformed outside these listed depth-two paths.
    * **JSON- / repr-escaped string bodies** -- a secret containing a quote,
      backslash, or non-ASCII character renders ESCAPED inside a JSON-encoded
      log field (``{"password": "a\\"b"}``, ``p\\u00e4ss...``) or a Python
      ``repr()`` (``'a\\'b'``); the raw bytes never appear literally, so the
      escaped body must be matched too (#292 item). The surrounding delimiters
      are stripped -- only the escaped body is a substring of the surrounding
      text -- and the hex digits of ``\\uXXXX``/``\\xXX`` escapes match
      case-insensitively via :func:`_variant_regex` (round-3 finding: another
      serializer's ``\\u00E4`` spelling decodes to the same secret).

    Total -- never raises: ``quote``/``quote_plus``'s DEFAULT ``errors="strict"``
    UTF-8 encode raises ``UnicodeEncodeError`` on a lone UTF-16 surrogate (a JSON
    log payload permits one; a settings value round-tripped through JSON could
    carry one) -- ``errors="surrogatepass"`` (the same choice :func:`safe_guid`
    makes for its own digest) absorbs it; ``json.dumps`` needs no such guard, it
    escapes a lone surrogate to ``\\uXXXX`` rather than raising.
    """
    raw_bytes = value.encode("utf-8", "surrogatepass")

    def _percent_spellings(text: str) -> tuple[str, str]:
        return (
            quote(text, safe="", errors="surrogatepass"),
            quote_plus(text, safe="", errors="surrogatepass"),
        )

    def _base64_spellings(data: bytes) -> tuple[str, str, str, str]:
        standard = base64.b64encode(data).decode("ascii")
        urlsafe = base64.urlsafe_b64encode(data).decode("ascii")
        return (standard, standard.rstrip("="), urlsafe, urlsafe.rstrip("="))

    def _lowercase_percent_hex(spelling: str) -> str:
        # A uniform-lowercase-hex percent spelling of an already percent-
        # encoded string: only the ``%XX`` escapes' hex digits are lowered,
        # non-escape characters (which ARE case-significant) are untouched.
        # ``quote``/``quote_plus`` always emit uppercase hex, so this is the
        # concrete second spelling the base64-of-percent nested family needs
        # (#381) -- deliberately scoped to this one spelling, not full 2^k
        # mixed-case enumeration.
        return _PERCENT_HEX_RE.sub(lambda m: m.group().lower(), spelling)

    variants = {value}
    first_percent = _percent_spellings(value)
    variants.update(first_percent)
    raw_base64 = _base64_spellings(raw_bytes)
    variants.update(raw_base64)
    variants.add(json.dumps(value)[1:-1])
    variants.add(repr(value)[1:-1])

    # dict.fromkeys, not a set: dedupes value==lowercase(value) cases while
    # keeping candidate order deterministic (str hash randomization would
    # otherwise make the _MAX_NESTED_CANDIDATES cut non-reproducible).
    percent_hex_spellings = tuple(
        dict.fromkeys(
            spelling
            for first in first_percent
            for spelling in (first, _lowercase_percent_hex(first))
        )
    )
    nested_families: tuple[tuple[str, ...], ...] = (
        tuple(candidate for first in first_percent for candidate in _percent_spellings(first)),
        tuple(
            candidate
            for spelling in percent_hex_spellings
            for candidate in _base64_spellings(spelling.encode("ascii"))
        ),
        tuple(candidate for spelling in raw_base64 for candidate in _percent_spellings(spelling)),
    )
    accepted_nested: set[str] = set()
    for family in nested_families:
        for candidate in family:
            if len(accepted_nested) >= _MAX_NESTED_CANDIDATES:
                break
            if len(candidate) <= _MAX_NESTED_CANDIDATE_LENGTH:
                accepted_nested.add(candidate)
        if len(accepted_nested) >= _MAX_NESTED_CANDIDATES:
            break
    variants.update(accepted_nested)
    return frozenset(variant for variant in variants if variant)


def _solidus_tolerant_regex(variant: str) -> str:
    """Match ``variant``, accepting either JSON spelling at each solidus."""
    body = variant
    parts: list[str] = []
    start = 0
    for index, character in enumerate(body):
        if character == "/":
            parts.append(_variant_regex(body[start:index]))
            parts.append(r"(?:/|\\/)")
            start = index + 1
    parts.append(_variant_regex(body[start:]))
    return "".join(parts)


def _mask_known_value(match: re.Match[str]) -> str:
    """Replace a matched secret-value occurrence with :data:`_REDACTED`, UNLESS
    the occurrence is provably serving as a KEY NAME (#292 item 4; see
    :func:`redact_known_secrets`), in which case masking it would rewrite an
    identifier (``some_password=X`` -> ``some_<redacted>=X``) -- destroying a key
    operators need AND, worse, breaking the very key the shape pass would have
    used to mask the actual credential ``X``.

    ALL of the following must hold to spare an occurrence -- each condition
    exists to kill a concrete leak a looser rule allowed:

    * the matched text IS exactly one of the recognized secret KEY WORDS
      (:data:`_KEY_WORD_FULL_RE`) -- a spare can only ever render bytes that are
      indistinguishable from ordinary log syntax (``password``, ``auth_token``,
      ... appear as key names in every deployment's logs regardless of what any
      secret's value is, so their presence reveals nothing about the configured
      secret). A secret merely embedded in / equal to some OTHER token before a
      separator (round-2 finding: ``payload=x<secret>=v``) fails this and is
      masked unconditionally.
    * the identifier the occurrence sits in is NOT immediately preceded by a
      genuine ``scheme://...`` URL-authority prefix (:data:`_AUTHORITY_PREFIX_RE`):
      in ``https://password:x@host`` the occurrence is a USERINFO component, not
      a key name, and sparing it would print the secret as a "username" (the
      composition then finishes the job -- ``_BASIC_AUTH_URL_RE`` consumes the
      surrounding ``:<...>@`` span around the masked occurrence). Fail closed:
      mask. Narrow by design (round-3 finding): a bare ``:`` prose prefix
      (``error:password=hunter2``) is NOT authority context.
    * SELF-VERIFICATION (round-3, the structural fix): the spare is only taken
      when the shape pass PROVABLY masks the value side -- ``_SECRET_KV_RE``
      itself (the exact grammar :func:`redact_secrets` will run, not an
      approximation) must produce a match whose KEY region contains this
      occurrence, i.e. ``kv.start() <= occurrence < occurrence.end <=
      kv.start("value")``. Every capture/read boundary runs
      :func:`redact_secrets` after this pass (``log_capture_service._capture``,
      ``web/routers/ops.py``), so a verified spare's paired credential never
      survives the composition. Any reason the shape pass would NOT fire --
      an overlong (>64-char) identifier prefix defeating ``_SECRET_KV_RE``'s
      bounded key scan (the round-3 leak), a missing/malformed separator, or
      any FUTURE shape-grammar limitation -- automatically fails this check and
      masks unconditionally: the whole finding family fails closed by
      construction instead of needing a new guard per grammar quirk.
    """
    matched = match.group(0)
    if _KEY_WORD_FULL_RE.fullmatch(matched) is None:
        return _REDACTED
    text = match.string
    ident_start = match.start()
    while ident_start > 0 and _IDENT_CHAR_RE.fullmatch(text[ident_start - 1]) is not None:
        ident_start -= 1
    authority_from = max(0, ident_start - _AUTHORITY_SCAN_WINDOW)
    if _AUTHORITY_PREFIX_RE.search(text, authority_from, ident_start) is not None:
        return _REDACTED  # userinfo/authority position, not a key -- fail closed
    # Reject ordinary prose occurrences before invoking a shape grammar: the
    # anchored separator probe fails in O(1) on prose and, on a hit, locates the
    # value's start (its match end) -- including across an arbitrarily long
    # whitespace gap, which the probe must tolerate exactly as far as the
    # grammars themselves do (see _KEY_SHAPE_SUFFIX_RE: disjoint probe regions
    # keep repeated keyword misses linear in message size WITHOUT a window that
    # could undercut the grammars and strip the shape pass's key anchor).
    separator = _KEY_SHAPE_SUFFIX_RE.match(text, match.end())
    if separator is None:
        return _REDACTED
    # Self-verify against the same non-cookie shape objects that redact_secrets()
    # will run later. The grammars accept truncated values, so the bounded slice
    # proves the key/value start while the later whole-line pass masks the value.
    scan_from = max(0, match.start() - _SHAPE_KEY_SCAN_WINDOW)
    scan_to = min(len(text), separator.end() + _SHAPE_VALUE_SCAN_WINDOW)
    for shape_re in _SELF_VERIFY_SHAPE_RES:
        for shape in shape_re.finditer(text, scan_from, scan_to):
            if shape.start() > match.start():
                break
            if (
                shape.start("key") <= match.start() < match.end() <= shape.end("key")
                and shape.start("value") >= match.end()
            ):
                return matched
    return _REDACTED


def redact_known_secrets(
    text: str,
    secret_values: Iterable[str],
    *,
    min_length: int = _MIN_SECRET_VALUE_LENGTH,
) -> str:
    """Mask every VERBATIM occurrence of a value in ``secret_values`` inside
    ``text`` with ``"<redacted>"`` -- the value-based complement to
    :func:`redact_secrets`'s shape grammar (issue #268; see the module comment
    above for the two functions' division of labor).

    ``secret_values`` is whatever the caller currently holds decrypted
    in-process (this app's configured Plex token, Prowlarr/TMDB api keys,
    qBittorrent password, ...) -- this function has no opinion on WHERE those
    came from and does no I/O of its own; it is a plain string-matching pass,
    exactly as total and side-effect-free as :func:`redact_secrets`. A value
    shorter than ``min_length`` (default :data:`_MIN_SECRET_VALUE_LENGTH`) is
    skipped entirely -- see that constant's docstring for why a short value is
    a false-positive hazard rather than a real credential to guard.

    Matching is EXACT substring matching (plus the URL-encoded/base64
    renderings :func:`_secret_value_variants` derives from each value), never
    a regex built FROM the secret's own shape -- so it catches a SETTINGS-
    CONFIGURED secret wherever it appears, independent of the surrounding
    syntax: notably inside a basic-auth URL whose password itself contains a
    raw ``@`` (``scheme://user:p@ss@host`` -- :func:`redact_secrets`'s
    ``_BASIC_AUTH_URL_RE`` stops at the FIRST ``@`` after the colon and leaves
    the password's remainder exposed past it; issue #270). This function
    canNOT, by construction, close the cookie-jar/mapping ``repr()`` dump half
    of issue #270 (``{'plexmgr.session': '<value>'}``) -- neither the
    ``plexmgr.session`` browser-auth token nor qBittorrent's ``SID`` cookie is
    ever a settings-store value in the first place (the former persists only
    a hash; the latter lives only in the adapter's in-memory cookie jar), so
    there is no settings value for THIS function to have been handed. That
    half is closed instead by the dedicated shape rule :data:`_COOKIE_JAR_RE`
    in :func:`redact_secrets` (see the module comment above) -- both passes
    run at every capture/export boundary, so the cookie-jar shape is covered
    regardless of which pass runs first.

    Every matched variant is replaced whole with ``"<redacted>"`` -- unlike
    ``redact_secrets``'s ``key=<redacted>`` convention (which keeps the key
    name for debuggability), there is no surrounding key name to preserve
    here: the match IS the secret, start to end, so nothing of it survives.
    All variants across all supplied values are combined into ONE alternation,
    LONGEST-first (:data:`re.escape`d, so a value containing regex
    metacharacters is matched literally) -- longest-first so a shorter variant
    that happens to be a PREFIX of a longer one (e.g. a raw value that is
    itself a prefix of its own base64 encoding, however unlikely) can never win
    the alternation and leave the longer form's remainder unmasked; ``re``'s
    leftmost-alternative-wins semantics make the ordering, not just the
    content, load-bearing here. An empty ``text`` or an empty/entirely-
    too-short ``secret_values`` is a no-op, returned byte-identical, exactly
    like an ordinary miss in :func:`redact_secrets`.

    KEY-NAME GUARD (#292 item 4): an occurrence is spared -- left readable --
    ONLY when it is provably serving as a key NAME rather than a credential:
    the matched text is exactly one of the shape grammar's own secret KEY WORDS
    (an operator whose configured secret's value equals a field-name word like
    ``"password"`` is the minimum-length edge case this exists for), it is not
    in a URL-authority/userinfo position, and -- the structural, SELF-VERIFYING
    condition -- ``_SECRET_KV_RE`` itself matches with this occurrence inside
    its KEY region, proving :func:`redact_secrets` (which every boundary runs
    AFTER this pass) masks the pair's value side. Masking such a key would
    rewrite the identifier (``some_password=X`` -> ``some_<redacted>=X``) --
    eating a name operators need (honesty over silence) and breaking the very
    key the shape pass uses to mask the actual credential ``X``. Anything
    failing ANY condition -- a secret embedded in a larger unrecognized token
    before ``=``/``:`` (``payload=x<secret>=v``), an overlong identifier the
    shape grammar's bounded key scan cannot match, or any future shape-pass
    limitation -- is masked unconditionally; see :func:`_mask_known_value` for
    the condition-by-condition rationale.

    The alternation is built with :func:`_variant_regex`, so a rendering's
    hex-bearing escapes (``%XX`` percent, ``\\uXXXX``/``\\xXX`` JSON/repr)
    match case-insensitively -- upper, lower, and per-escape MIXED spellings
    all mask, with no spelling enumeration.
    """
    if not text:
        return text
    values = [value for value in secret_values if value and len(value) >= min_length]
    variants: set[str] = set()
    for value in values:
        variants |= _secret_value_variants(value)
    if not variants:
        return text
    patterns = [_solidus_tolerant_regex(variant) for variant in variants]
    alternation = "|".join(sorted(set(patterns), key=len, reverse=True))
    pattern = re.compile(r"(?:" + alternation + r")")
    return pattern.sub(_mask_known_value, text)
