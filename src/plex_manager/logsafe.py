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
import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import quote, urlsplit

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
    r"(?i)\b[\w-]{0,64}?"
    + r"(?:(?P<fkey>"
    + _FREEFORM_KEY_PATTERN
    + r")|(?:"
    + _TOKEN_KEY_PATTERN
    + r"))\b"
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
    r"(?i)\bauthorization\b['\"]?\s*[:=]\s*"
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
    r"(?i)b?(?P<kq>['\"])[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization)(?P=kq)\s*,\s*b?(?P<q>['\"])(?P<value>"
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
    r"(?i)\b[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization)\b"
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
    r"(?i)b?(?P<kq>['\"])[\w-]{0,64}?(?:"
    + _SECRET_KEY_PATTERN
    + r"|authorization)(?P=kq)\s*,\s*(?P<value>[\[\(]"
    + _CONTAINER_BODY
    + r"[\]\)]?)"
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
    redacted = _FERNET_KEY_RE.sub("<redacted-fernet-key>", redacted)
    return redacted


# --------------------------------------------------------------------------- #
# redact_known_secrets (issue #268): a VALUE-based redaction pass, complementary
# to :func:`redact_secrets`'s SHAPE-based grammar above. ``redact_secrets`` asks
# "does this text contain something shaped like a credential" (a key name next
# to a value, a basic-auth URL, a cookie assignment, ...) -- a denylist of known
# RENDERINGS that, per that function's own module comment, keeps growing a new
# rule every time a novel shape turns up (issue #270's cookie-jar/mapping-repr
# dump and its raw-``@``-in-password basic-auth URL are exactly two such
# shapes, deliberately NOT patched into the grammar -- see below).
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
# they are masked, regardless of the surrounding shape. This is why issue #270
# folds its two shape-grammar gaps into THIS function's test matrix rather than
# into another ``redact_secrets`` pattern: both gaps are simply an instance of
# "a real settings-store secret value appeared verbatim in a shape the grammar
# doesn't recognize", which is precisely the class of leak a value-based pass
# closes by construction, without one more denylist rule that only narrows the
# NEXT unrecognized shape.
#
# Deliberately COMPLEMENTARY, not a replacement: this pass can only mask a
# value it was HANDED, so a secret this app never stored in settings (a
# user-pasted tracker/indexer token typed into a correction flow, an ad-hoc
# credential a future adapter accepts but never persists) is invisible to it
# -- ``redact_secrets``'s key-name grammar is still the only barrier for that
# class, and both passes run at every capture/export boundary (see
# ``log_capture_service._capture`` and ``web/routers/ops.py``). Reuses
# :data:`_REDACTED` -- the same placeholder :func:`redact_secrets` emits, so a
# reader of a log line can never tell WHICH pass masked a given span, nor does
# it need to: either way, the answer is "a secret was here".

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


def _secret_value_variants(value: str) -> frozenset[str]:
    """Every literal RENDERING of ``value`` this pass will mask: the raw value
    itself, its URL-percent-encoded form (a password/token embedded in a query
    string or userinfo often arrives percent-encoded rather than raw -- e.g. a
    password containing ``@`` or ``&``), and its base64 form (``cryptography``/
    third-party clients frequently base64-encode a credential for a header or
    a config dump). Each variant is included only when it actually DIFFERS from
    the raw value, so a value with no reserved/non-ASCII characters (already
    identical to its own percent-encoding) contributes exactly one entry rather
    than three redundant copies for the caller's alternation to consider.

    Total -- never raises: ``quote``'s own DEFAULT ``errors="strict"`` UTF-8
    encode raises ``UnicodeEncodeError`` on a lone UTF-16 surrogate (a JSON log
    payload permits one; a settings value round-tripped through JSON could
    carry one) -- ``errors="surrogatepass"`` (the same choice :func:`safe_guid`
    makes for its own digest) absorbs it instead of raising.
    """
    variants = {value}
    percent_encoded = quote(value, safe="", errors="surrogatepass")
    if percent_encoded != value:
        variants.add(percent_encoded)
    b64_encoded = base64.b64encode(value.encode("utf-8", "surrogatepass")).decode("ascii")
    if b64_encoded != value:
        variants.add(b64_encoded)
    return frozenset(variants)


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
    a regex built FROM the secret's own shape -- so it catches a real secret
    wherever it appears, independent of the surrounding syntax: inside a
    cookie-jar/mapping ``repr()`` dump (``{'plexmgr.session': '<value>'}``, a
    shape :func:`redact_secrets`'s ``_COOKIE_RE`` does not recognize -- it
    requires a direct ``name=value`` cookie assignment, not a dict repr), or
    inside a basic-auth URL whose password itself contains a raw ``@``
    (``scheme://user:p@ss@host`` -- :func:`redact_secrets`'s
    ``_BASIC_AUTH_URL_RE`` stops at the FIRST ``@`` after the colon and leaves
    the password's remainder exposed past it). Both are issue #270's deferred
    shape-grammar gaps, folded into this function's test matrix as regression
    coverage rather than patched into the grammar (see the module comment
    above for why).

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
    """
    if not text:
        return text
    variants: set[str] = set()
    for value in secret_values:
        if not value or len(value) < min_length:
            continue
        variants |= _secret_value_variants(value)
    if not variants:
        return text
    pattern = re.compile("|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)))
    return pattern.sub(_REDACTED, text)
