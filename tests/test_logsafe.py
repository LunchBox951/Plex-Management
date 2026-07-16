"""Unit tests for the ``plex_manager.logsafe`` log-value barriers.

These are the single-purpose barriers every request-derived log site passes its
values through (see CONTRIBUTING.md "Logging request-derived values"): ``safe_int``
re-coerces an id (a no-op for a real int, a taint barrier for CodeQL's
py/log-injection), ``safe_text`` collapses CR/LF so a request-derived string
cannot forge a second log record, and ``safe_guid`` allowlists provably plain
release-GUID ids and redacts EVERYTHING else (a Prowlarr private-indexer GUID
can be a URI of any shape embedding a tracker passkey/session token) so a
secret is never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from http.cookies import SimpleCookie
from urllib.parse import quote, quote_plus, urlsplit

import pytest

from plex_manager.logsafe import (
    redact_known_secrets,
    redact_secrets,
    safe_guid,
    safe_int,
    safe_text,
)


def _sha12(value: str) -> str:
    """The 12-hex sha256 prefix ``safe_guid`` appends -- recomputed independently
    (``surrogatepass`` mirrors the helper: the barrier is total even for a lone
    surrogate, which JSON permits and plain UTF-8 encoding would raise on)."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


@pytest.mark.parametrize("value", [0, 1, 999, -5, 2**63])
def test_safe_int_passes_real_ints_through_unchanged(value: int) -> None:
    result = safe_int(value)
    assert result == value
    assert type(result) is int


def test_safe_text_leaves_clean_text_unchanged() -> None:
    assert safe_text("Arrival (2016)") == "Arrival (2016)"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("line1\nline2", "line1 line2"),
        ("line1\rline2", "line1 line2"),
        ("line1\r\nline2", "line1  line2"),  # both chars collapse, one space each
        ("\n\rboundary", "  boundary"),
        ("a\vb", "a b"),
        ("a\fb", "a b"),
        ("a\x1cb", "a b"),
        ("a\x1db", "a b"),
        ("a\x1eb", "a b"),
        ("a\x85b", "a b"),
        ("a\u2028b", "a b"),
        ("a\u2029b", "a b"),
    ],
)
def test_safe_text_collapses_crlf_to_spaces(raw: str, expected: str) -> None:
    assert safe_text(raw) == expected


def test_safe_text_neutralizes_a_forged_log_record() -> None:
    """A CRLF payload aiming to inject a fake ``ERROR root:`` line is defanged:
    no bare newline survives, so the value cannot start a second log record."""
    forged = "42\nERROR:root:you have been hacked"
    cleaned = safe_text(forged)
    assert "\n" not in cleaned
    assert "\r" not in cleaned


def test_safe_text_neutralizes_unicode_line_forgery() -> None:
    """GHSA-6gm2: ``safe_text`` used to strip only ``\\r``/``\\n``, missing the
    other line-boundary chars ``str.splitlines()`` (and many log renderers)
    honor. Every one of them must be neutralized, not just CR/LF."""
    forged = "42\u2028ERROR:root:pwned"
    cleaned = safe_text(forged)
    for boundary in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029":
        assert boundary not in cleaned


@pytest.mark.parametrize(
    ("raw", "host", "secret"),
    [
        # passkey in the query string -- the classic private-tracker download URL
        (
            "https://tracker.example.org/download/123?passkey=SUPERSECRET",
            "tracker.example.org",
            "SUPERSECRET",
        ),
        # session token embedded in a PATH segment (not just the query)
        (
            "https://priv.tracker.net/rss/SECRETPATHTOKEN/movie.torrent",
            "priv.tracker.net",
            "SECRETPATHTOKEN",
        ),
        # credential in ``user:pass@`` userinfo -- ``hostname`` (not ``netloc``) drops it
        (
            "http://someuser:USERPASSKEY@idx.local/file?x=1",
            "idx.local",
            "USERPASSKEY",
        ),
        # non-default port + apikey query -- host kept, port and secret both dropped
        (
            "http://tracker.example.org:8080/get?apikey=TOPSECRET",
            "tracker.example.org",
            "TOPSECRET",
        ),
    ],
)
def test_safe_guid_redacts_url_shaped_guids(raw: str, host: str, secret: str) -> None:
    """A URL-shaped GUID logs only ``<host>#<sha256-prefix>`` -- host kept for
    diagnosability, the credential-bearing path/query/userinfo never emitted.

    The positive check is EXACT-token equality -- never a host-substring ``in``
    check, which CodeQL flags as py/incomplete-url-substring-sanitization (a
    host name can sit at an arbitrary position inside an unredacted URL)."""
    result = safe_guid(raw)
    assert result == f"{host}#{_sha12(raw)}"  # exactly host + hash -- nothing else
    assert secret not in result  # passkey / token / userinfo credential never leaked
    assert "?" not in result  # the query string is gone
    assert "/" not in result  # the path is gone


@pytest.mark.parametrize(
    "raw",
    [
        "Some.Movie.2020.1080p.WEB-DL.x264-GROUP",  # a title-style dotted id
        "0123456789abcdef0123456789abcdef",  # a plain hex/info-hash-style id
        "12345678-1234-5678-1234-567812345678",  # a bare uuid
        "tt0111161",  # an imdb-style id
        "12345",  # a numeric id
        "a" * 128,  # exactly the length cap -- still allowlisted
    ],
)
def test_safe_guid_passes_allowlisted_plain_ids_through_byte_identical(raw: str) -> None:
    """The ONLY passthrough: a fullmatch of the strict safe-id allowlist
    (letters/digits/``._-``, at most 128 chars). Byte-identical -- the allowlisted
    character class contains no CR/LF, so not even ``safe_text`` reshaping."""
    assert safe_guid(raw) == raw


@pytest.mark.parametrize(
    ("raw", "label", "secret"),
    [
        # Wave-4 finding: a magnet URI (scheme, NO netloc) -- its percent-encoded
        # ``tr=`` announce parameters are tracker URLs, passkey and all.
        (
            "magnet:?xt=urn:btih:deadbeef&tr=https%3A%2F%2Fpriv.tracker.org%2Fa%3Fpasskey%3DTRSECRET",
            "magnet",
            "TRSECRET",
        ),
        # Wave-5 finding (the hole that inverted this function to an allowlist):
        # a schemeless URL parses as PURE PATH -- no scheme, no netloc -- so every
        # "URL-shaped" denylist classified it as a plain id and passed the
        # passkey through verbatim. The allowlist rejects the ``/`` and ``?`` on
        # sight; no host parses, hence the bare-hash token.
        ("tracker.example.org/dl/123?passkey=W5SECRET", "", "W5SECRET"),
        # Scheme-ish opaque ids: deliberate fail-closed collateral (see the
        # helper docstring) -- over-redacting a harmless id costs one label;
        # under-redacting a real URI leaks a credential.
        ("urn:uuid:12345678-1234-5678-1234-567812345678", "urn", None),
        ("prowlarr:123", "prowlarr", None),
        # Protocol-relative: netloc without scheme -- the label is the host.
        ("//priv.tracker.org/dl?passkey=PRSECRET", "priv.tracker.org", "PRSECRET"),
        # Opaque text with spaces/parens: outside the allowlist -> redacted
        # (collateral of the inversion; still correlatable via the hash).
        ("release 12345 (group)", "", None),
        # Over the 128-char cap: no longer provably a short opaque id.
        ("a" * 129, "", None),
    ],
)
def test_safe_guid_redacts_everything_outside_the_allowlist(
    raw: str, label: str, secret: str | None
) -> None:
    """Anything that fails the safe-id fullmatch redacts to ``<label>#<hash>``:
    label = hostname if one parses, else the scheme if present, else nothing --
    diagnosable where possible, never a byte of the credential-bearing remainder."""
    result = safe_guid(raw)
    assert result == f"{label}#{_sha12(raw)}"  # exact token: label + hash, nothing else
    if secret is not None:
        assert secret not in result
    assert "passkey" not in result
    assert "?" not in result


@pytest.mark.parametrize(
    "raw",
    [
        "https://tracker.example.org/download/123?passkey=SUPERSECRET",
        "http://someuser:USERPASSKEY@idx.local/file?x=1",
        "magnet:?xt=urn:btih:deadbeef&tr=https%3A%2F%2Ft.org%2Fa%3Fpasskey%3DTRSECRET",
        "tracker.example.org/dl/123?passkey=W5SECRET",  # wave 5: schemeless
        "//priv.tracker.org/dl?passkey=PRSECRET",  # protocol-relative
        "prowlarr:123",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
        "http://[bad/download?passkey=LEAKEDSECRET",  # malformed -> ValueError arm
        "id with whitespace SECRET",
        "https://tracker.example.org%2Fdl%3Fpasskey%3DW6SECRET",  # wave 6: encoded tail
        "50%25off?SECRET",
        "a&b=SECRET",
    ],
)
def test_safe_guid_url_machinery_never_passes_through(raw: str) -> None:
    """The property the allowlist inversion buys, stated directly: ANY value
    containing URL machinery (``/ ? & % :`` or whitespace) -- whatever novel URI
    shape it takes -- comes out as exactly an ``<allowlisted-label>#<hash>``
    token (label possibly empty), never as the value itself, and never carrying
    a secret fragment. The shape regex IS the emit contract: label bytes are
    restricted to the label allowlist, so no ``% / ? & :`` byte can survive."""
    assert any(c in raw for c in "/?&%: \t"), "test row must contain URL machinery"
    result = safe_guid(raw)
    # Exactly <allowlist-validated label>#<12-hex sha256 prefix>; no other byte
    # can appear, so no passkey/query/path/userinfo fragment can survive.
    assert re.fullmatch(r"[A-Za-z0-9._\-]{0,64}#[0-9a-f]{12}", result)
    assert result.endswith(f"#{_sha12(raw)}")  # stable correlation hash intact
    assert "SECRET" not in result
    assert "secret" not in result  # ``hostname`` lowercases -- neither casing may leak
    assert "passkey" not in result


def test_safe_guid_validates_the_label_against_the_allowlist_too() -> None:
    """Wave-6 P1: the label was the last unvalidated emission. A URL whose path/
    query is PERCENT-ENCODED has no literal ``/`` or ``?``, so ``urlsplit``
    parses its entire tail as the netloc and ``hostname`` comes back carrying
    the encoded path/query -- secret included. The label is now emitted only if
    it fullmatches the strict label allowlist (a legit hostname/scheme always
    does; a ``%``-bearing blob never can), else the bare-hash token."""
    raw = "https://tracker.example.org%2Fdl%3Fpasskey%3DSECRET"
    result = safe_guid(raw)
    assert result == f"#{_sha12(raw)}"  # bare hash: the swallowed-tail label is dropped
    assert "SECRET" not in result
    assert "secret" not in result  # ``hostname`` lowercases -- neither casing leaks
    assert "%" not in result
    # Sanity for the other label arm: a clean scheme still labels.
    magnet = "magnet:?xt=urn:btih:deadbeef"
    assert safe_guid(magnet) == f"magnet#{_sha12(magnet)}"


def test_safe_guid_hash_prefix_is_stable_and_release_distinguishing() -> None:
    """The hash is deterministic (so the beta-week analysis can correlate repeated
    failures of the SAME release) yet varies with the full GUID (so two different
    releases on the SAME host do not collide). Exact-token comparisons only --
    no ``startswith(host)`` shape (py/incomplete-url-substring-sanitization)."""
    url = "https://tracker.example.org/x?passkey=SECRET"
    assert safe_guid(url) == safe_guid(url)  # stable across calls
    other_url = "https://tracker.example.org/x?passkey=OTHER"
    other = safe_guid(other_url)
    assert other != safe_guid(url)  # same host, different secret -> different hash
    assert other == f"tracker.example.org#{_sha12(other_url)}"  # exact expected token


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        # ``urlsplit`` raises ValueError on an unclosed IPv6 bracket -- the
        # canonical case from the review finding.
        ("http://[bad", None),
        # THE reason the fallback fails CLOSED rather than passing through: a
        # malformed netloc raises the SAME ValueError while the value still
        # carries a credential. A ``safe_text`` passthrough here would re-open
        # the original P1 leak verbatim.
        ("http://[bad/download?passkey=LEAKEDSECRET", "LEAKEDSECRET"),
        ("https://[::1", None),  # unclosed bracket, IPv6-ish
    ],
)
def test_safe_guid_never_raises_on_malformed_url_shaped_guids(raw: str, secret: str | None) -> None:
    """A log barrier must be total: ``urlsplit``'s ValueError is absorbed, and the
    unparseable-but-URL-ish value is FULLY redacted to a hash-only ``#<sha256>``
    token (no host could be parsed, and the raw text may still embed a secret)."""
    result = safe_guid(raw)  # must not raise -- a throw here would abort a grab cycle
    assert result == f"#{_sha12(raw)}"  # exact hash-only token: nothing of the value
    if secret is not None:
        assert secret not in result


def test_safe_guid_is_total_for_lone_surrogates() -> None:
    """JSON (and thus a Prowlarr response) permits lone surrogates; a plain UTF-8
    encode of one raises ``UnicodeEncodeError``. ``surrogatepass`` keeps the
    barrier total AND the redaction intact -- the exotic character must never
    become a throw or a verbatim-passthrough bypass."""
    raw = "https://tracker.example.org/x?passkey=S\ud800ECRET"
    assert safe_guid(raw) == f"tracker.example.org#{_sha12(raw)}"


# --------------------------------------------------------------------------- #
# redact_secrets (issue #153): defense-in-depth redaction over a fully-rendered
# log line, applied at capture time and again at the export boundary. Every
# fixture secret below is an obviously-fake literal (never a real credential),
# so asserting ``secret not in result`` -- which pytest's assertion rewriting
# would otherwise echo on a failure -- never actually risks logging a real
# secret in a test's own failure output (mirrors the existing ``safe_guid``
# tests' identical convention above).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "secret", "expected"),
    [
        # TMDB-shaped: api_key as a URL query parameter -- the httpx INFO log
        # line shape this whole issue exists to catch.
        (
            "GET https://api.themoviedb.org/3/movie/603?api_key=FAKEAPIKEY123456&language=en-US",
            "FAKEAPIKEY123456",
            "GET https://api.themoviedb.org/3/movie/603?api_key=<redacted>&language=en-US",
        ),
        # Prowlarr-shaped: X-Api-Key as a plain header line (no quotes). The
        # hyphenated prefix ``X-Api-`` is untouched text; only the "Key" word
        # the alternation matches plus its value are replaced.
        (
            "X-Api-Key: FAKEPROWLARRKEY99",
            "FAKEPROWLARRKEY99",
            "X-Api-Key: <redacted>",
        ),
        # Prowlarr-shaped: X-Api-Key inside a dict-repr headers dump.
        (
            "sending request headers={'Accept': 'json', 'X-Api-Key': 'FAKEPROWLARRKEY99'}",
            "FAKEPROWLARRKEY99",
            "sending request headers={'Accept': 'json', 'X-Api-Key': '<redacted>'}",
        ),
        # Plex-shaped: X-Plex-Token as a header.
        (
            "X-Plex-Token: FAKEPLEXTOKEN7890",
            "FAKEPLEXTOKEN7890",
            "X-Plex-Token: <redacted>",
        ),
        # Plex-shaped: X-Plex-Token riding a URL query parameter (some Plex
        # media/transcode urls carry the token this way instead of a header).
        (
            "GET https://plex.example.com/library/sections?X-Plex-Token=FAKEPLEXTOKEN7890",
            "FAKEPLEXTOKEN7890",
            "GET https://plex.example.com/library/sections?X-Plex-Token=<redacted>",
        ),
        # qBittorrent-shaped: password in a form/dict login payload -- the
        # username stays visible (debuggability), only the password is masked.
        (
            "POST /api/v2/auth/login data={'username': 'admin', 'password': 'FAKEQBTPASSWORD1'}",
            "FAKEQBTPASSWORD1",
            "POST /api/v2/auth/login data={'username': 'admin', 'password': '<redacted>'}",
        ),
        # A generic bearer-style access token query param.
        (
            "refreshing session: access_token=FAKEACCESSTOKEN123456&expires_in=3600",
            "FAKEACCESSTOKEN123456",
            "refreshing session: access_token=<redacted>&expires_in=3600",
        ),
        # A generic "secret" key.
        (
            "loaded webhook secret=FAKEWEBHOOKSECRET99",
            "FAKEWEBHOOKSECRET99",
            "loaded webhook secret=<redacted>",
        ),
        # THIS app's real prefixed settings-field names: ``_`` is a word char, so
        # a bare ``\b`` before ``api``/``token``/``password`` would never fire on
        # these -- the bounded lazy prefix makes the key word a valid identifier
        # suffix. The whole field name survives; only the value is masked.
        (
            "config tmdb_api_key=FAKETMDBKEY1234567",
            "FAKETMDBKEY1234567",
            "config tmdb_api_key=<redacted>",
        ),
        (
            "loaded plex_token=FAKEPLEXTOKEN7890",
            "FAKEPLEXTOKEN7890",
            "loaded plex_token=<redacted>",
        ),
        (
            "settings app_api_key=FAKEAPPKEY99",
            "FAKEAPPKEY99",
            "settings app_api_key=<redacted>",
        ),
        # A quoted credential whose value carries spaces/commas (a
        # ``qbittorrent_password`` may contain any of these): the WHOLE quoted
        # value is consumed, not just the first whitespace-delimited token, so no
        # suffix is left behind the ``<redacted>``. The closing quote survives.
        (
            "login qbittorrent_password='FAKE pw, with spaces'",
            "with spaces",
            "login qbittorrent_password='<redacted>'",
        ),
        (
            'headers={"password": "FAKE multi word pw"}',
            "multi word pw",
            'headers={"password": "<redacted>"}',
        ),
        # A quoted credential containing the OPPOSITE quote character
        # (``SettingsUpdate`` accepts such passwords): the tempered run stops
        # only at the MATCHING close quote, so the embedded quote does not end
        # the value and no tail is left behind ``<redacted>``.
        (
            'password="abc\'FAKEEMBEDDED1"',
            "FAKEEMBEDDED1",
            'password="<redacted>"',
        ),
        (
            "password='abc\"FAKEEMBEDDED2'",
            "FAKEEMBEDDED2",
            "password='<redacted>'",
        ),
        # TUPLE-rendered headers (``list(headers.items())``/raw header dumps):
        # no ``:``/``=`` separator at all -- the dedicated tuple pass masks the
        # quoted value whole, key name and surrounding structure intact.
        (
            "headers=[('X-Api-Key', 'FAKETUPLEKEY1')]",
            "FAKETUPLEKEY1",
            "headers=[('X-Api-Key', '<redacted>')]",
        ),
        (
            'headers=[("x-plex-token", "FAKETUPLEKEY2")]',
            "FAKETUPLEKEY2",
            'headers=[("x-plex-token", "<redacted>")]',
        ),
        # An Authorization tuple: the quoted-value run masks through the
        # internal space, so scheme AND credential are both consumed.
        (
            "sending [('Accept', 'json'), ('Authorization', 'Bearer FAKETUPLETOK')]",
            "FAKETUPLETOK",
            "sending [('Accept', 'json'), ('Authorization', '<redacted>')]",
        ),
        # P1 (escaped-quote leak): a JSON/repr dump of a quoted credential that
        # itself contains a backslash-ESCAPED matching quote. The escape-aware
        # run treats ``\"`` as one escaped char, not the close delimiter, so the
        # suffix after it is masked instead of leaking past ``<redacted>``.
        (
            '{"password": "abc\\"FAKEESCSUFFIX"}',
            "FAKEESCSUFFIX",
            '{"password": "<redacted>"}',
        ),
        (
            "{'password': 'abc\\'FAKEESCSUF2'}",
            "FAKEESCSUF2",
            "{'password': '<redacted>'}",
        ),
        # A backslash-escaped quote as the FIRST character of the value.
        (
            'password="\\"FAKEESCFIRST"',
            "FAKEESCFIRST",
            'password="<redacted>"',
        ),
        # P2 (list-wrapped): a multi-valued header/form mapping renders its value
        # as a list -- the whole bracketed list is masked, not just the ``[``.
        (
            "sending request headers={'Accept': 'json', 'X-Api-Key': ['FAKELISTKEY1']}",
            "FAKELISTKEY1",
            "sending request headers={'Accept': 'json', 'X-Api-Key': <redacted>}",
        ),
        (
            'headers={"X-Api-Key": ["FAKELISTKEY2", "FAKELISTKEY3"]}',
            "FAKELISTKEY3",
            'headers={"X-Api-Key": <redacted>}',
        ),
        # A list value with an embedded ``]`` inside a quoted element must not
        # let that ``]`` close the list early.
        (
            "headers={'api_key': ['FAKE]LISTBRACKET']}",
            "FAKE]LISTBRACKET",
            "headers={'api_key': <redacted>}",
        ),
        # A tuple whose value is a list (``list(headers.items())`` of a
        # multi-valued header).
        (
            "headers=[('X-Api-Key', ['FAKETUPLELIST'])]",
            "FAKETUPLELIST",
            "headers=[('X-Api-Key', <redacted>)]",
        ),
        # A NESTED list value: the body must not stop at the first inner ``]``
        # and leave the later element (secret included) behind ``<redacted>``.
        (
            "headers={'X-Api-Key': [['FAKENESTA'], ['FAKENESTB']]}",
            "FAKENESTB",
            "headers={'X-Api-Key': <redacted>}",
        ),
        # A multi-valued Authorization list: the list pass covers it before the
        # single-value Authorization pass can mis-grab the ``[``.
        (
            "headers={'Authorization': ['Bearer FAKEAUTHLIST']}",
            "FAKEAUTHLIST",
            "headers={'Authorization': <redacted>}",
        ),
        # P2 (session cookies): the browser ``plexmgr.session`` auth cookie and
        # qBittorrent's upstream ``SID`` cookie -- keyed on the cookie name, the
        # value masked up to the ``;`` attribute separator so ``Path``/``HttpOnly``
        # stay diagnosable.
        (
            "Cookie: plexmgr.session=FAKESESSIONTOK",
            "FAKESESSIONTOK",
            "Cookie: plexmgr.session=<redacted>",
        ),
        (
            "Set-Cookie: SID=FAKEQBTSID; Path=/; HttpOnly",
            "FAKEQBTSID",
            "Set-Cookie: SID=<redacted>; Path=/; HttpOnly",
        ),
        # Only the session cookie in a multi-cookie header is masked; benign
        # cookies (and the surrounding ``;``-separated structure) survive.
        (
            "Cookie: theme=dark; plexmgr.session=FAKESESSIONTOK2; lang=en",
            "FAKESESSIONTOK2",
            "Cookie: theme=dark; plexmgr.session=<redacted>; lang=en",
        ),
        # qBittorrent 5.2 names its session cookie ``QBT_SID_<port>`` (the
        # adapter's ``_session_cookie_header`` emits exactly this shape) -- the
        # cookie-name match admits a separator-joined suffix after ``sid``.
        (
            "Cookie: QBT_SID_8080=FAKEQBTSID52; other=1",
            "FAKEQBTSID52",
            "Cookie: QBT_SID_8080=<redacted>; other=1",
        ),
        # P1 (unquoted freeform value): a forgotten call site interpolating a
        # bare password that CONTAINS spaces/commas (``SettingsUpdate`` permits
        # both) -- a single-token capture would leak everything past the first
        # space; the freeform branch consumes through to the line boundary.
        (
            "login qbittorrent_password=FAKE pw, with spaces",
            "with spaces",
            "login qbittorrent_password=<redacted>",
        ),
        # ... but never PAST the line boundary: the next log record survives.
        (
            "qbittorrent_password=FAKEMLPW abc\nnext record",
            "FAKEMLPW",
            "qbittorrent_password=<redacted>\nnext record",
        ),
        # TOKEN-family keys keep the single-token capture: machine-generated
        # urlsafe credentials cannot contain a space/&, so stopping at ``&``
        # is fail-closed AND keeps the URL query diagnosable.
        (
            "GET /3/movie/603?api_key=FAKETOKFAM1&language=en-US",
            "FAKETOKFAM1",
            "GET /3/movie/603?api_key=<redacted>&language=en-US",
        ),
        # P2 (byte-string dumps): ``httpx.Headers.raw`` renders byte tuples --
        # the ``b`` prefix on key and value must not defeat the tuple pass.
        (
            "headers=[(b'X-Api-Key', b'FAKEBYTEKEY1')]",
            "FAKEBYTEKEY1",
            "headers=[(b'X-Api-Key', b'<redacted>')]",
        ),
        # ... and the dict-style byte value must be masked whole, not leave the
        # quoted secret behind a consumed ``b``.
        (
            "headers={b'X-Plex-Token': b'FAKEBYTEKEY2'}",
            "FAKEBYTEKEY2",
            "headers={b'X-Plex-Token': b'<redacted>'}",
        ),
        (
            "headers=[(b'Authorization', b'Bearer FAKEBYTEAUTH')]",
            "FAKEBYTEAUTH",
            "headers=[(b'Authorization', b'<redacted>')]",
        ),
        # P2 (parameterized Authorization): Digest/SigV4-style values carry
        # credential-bearing parameters past any token bound -- the whole
        # header value is consumed to the line boundary.
        (
            'Authorization: Digest username="u", nonce="FAKEDIGNONCE", response="FAKEDIGRESP"',
            "FAKEDIGRESP",
            "Authorization: <redacted>",
        ),
        # A dict-repr Authorization value is QUOTED -- masked through its
        # matching close quote, leaving the neighboring pair intact.
        (
            "headers={'Authorization': 'Bearer FAKEDICTAUTH', 'Accept': 'json'}",
            "FAKEDICTAUTH",
            "headers={'Authorization': '<redacted>', 'Accept': 'json'}",
        ),
        # P2 (tuple-wrapped values): a multi-valued field rendered as a TUPLE
        # container -- ``('SECRET',)`` -- is masked whole like a list.
        (
            "headers={'X-Api-Key': ('FAKETUPLEWRAP1',)}",
            "FAKETUPLEWRAP1",
            "headers={'X-Api-Key': <redacted>}",
        ),
        (
            "headers=[('X-Api-Key', ('FAKETUPLEWRAP2',))]",
            "FAKETUPLEWRAP2",
            "headers=[('X-Api-Key', <redacted>)]",
        ),
        # P5a (cookie-jar/mapping repr, issue #270): a dict-repr cookie dump
        # (``:`` separator, quoted key AND value) rather than a raw
        # ``name=value`` header line -- the cookie NAME survives, only the
        # token is masked.
        (
            "outgoing cookies: {'plexmgr.session': 'FAKEJARSESSION1'}",
            "FAKEJARSESSION1",
            "outgoing cookies: {'plexmgr.session': '<redacted>'}",
        ),
        # qBittorrent's SID cookie in the same dict-repr shape.
        (
            "cookies={'QBT_SID_8080': 'FAKEJARSID1'}",
            "FAKEJARSID1",
            "cookies={'QBT_SID_8080': '<redacted>'}",
        ),
    ],
)
def test_redact_secrets_masks_key_value_shaped_secrets(
    raw: str, secret: str, expected: str
) -> None:
    result = redact_secrets(raw)
    assert result == expected
    assert secret not in result


@pytest.mark.parametrize(
    "raw",
    [
        # The common English word "session" as prose -- followed by ``:`` and
        # more text, NOT an adjacent ``=`` cookie assignment -- must not trip the
        # cookie pass (the regression the existing ``access_token`` fixture also
        # guards, isolated here).
        "refreshing session: reconnecting in 5s",
        "session established with upstream",
        # ``sid`` only inside a longer word (no adjacent ``=``) is not a cookie.
        "aside note: nothing secret here",
        # ``sid`` mid-word directly before ``=`` without a name separator is
        # prose, not a cookie name (``QBT_SID_8080=`` matches because its
        # suffix follows a ``_`` separator; ``consider``'s trailing ``er``
        # follows nothing).
        "consider=carefully before retrying",
        "residential=true",
        # The dict-repr cookie-jar pass (``_COOKIE_JAR_RE``) must not false-
        # match an unrelated quoted key that merely CONTAINS "sid" without a
        # separator before the trailing characters ("resid-ential" has no
        # ``._-`` boundary after "sid").
        "{'residential': 'true'}",
        # An ordinary dict entry with no session/sid-shaped key at all.
        "{'user_id': '12345', 'theme': 'dark'}",
    ],
)
def test_redact_secrets_leaves_session_prose_untouched(raw: str) -> None:
    assert redact_secrets(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        # A never-closed quoted value (log line truncated mid-credential): the
        # escape-aware run consumes to end, so the whole tail is masked -- fail
        # closed, no trailing fragment survives.
        "login password='FAKETRUNCPW",
        # A never-closed list (truncated mid-list): consumed to end.
        "headers={'api_key': ['FAKETRUNCLIST",
        # A raw newline embedded in a quoted value (multi-line log record): the
        # negated class matches the newline, so the value is masked past it.
        'password="line one\nFAKEMULTILINE line two"',
        # A lone surrogate inside a quoted value must not raise and must mask.
        'password="FAKE\ud800SURROGATE"',
        # Nested/mixed escaping.
        'password="a\\"b\\\\c\\"FAKENESTESC"',
        # A list element carrying an escaped quote.
        "headers={'api_key': ['x\\'FAKELISTESC']}",
    ],
)
def test_redact_secrets_fail_closed_on_adversarial_quoting(raw: str) -> None:
    """Adversarial quoting/escaping/truncation the redaction must fail CLOSED on:
    the obviously-fake credential embedded in each must never survive redaction.
    The literal ``FAKE...`` markers below are never real secrets (mirrors the
    fixture convention above), so pytest's assertion echo cannot leak one."""
    result = redact_secrets(raw)
    for marker in (
        "FAKETRUNCPW",
        "FAKETRUNCLIST",
        "FAKEMULTILINE",
        "FAKESURROGATE",
        "FAKENESTESC",
        "FAKELISTESC",
    ):
        assert marker not in result


def test_redact_secrets_is_bounded_time_on_pathological_input() -> None:
    """The value/list runs are backtracking-free (mutually exclusive branches /
    possessive quantifiers): a long run of the ambiguity-inviting characters
    (backslashes, quotes) must stay linear, never ReDoS. A generous ceiling --
    the point is "not exponential", not a microbenchmark."""
    import time

    payloads = [
        'password="' + "\\" * 40000,
        "api_key=['" + "a'," * 20000,
        "api_key=['" + "\\" * 40000,
        'password="' + "a" * 200000,
    ]
    for payload in payloads:
        start = time.perf_counter()
        redact_secrets(payload)
        assert time.perf_counter() - start < 1.0


def test_redact_secrets_masks_basic_auth_url_password() -> None:
    raw = "connecting to https://tracker_user:FAKEURLPASSWORD1@tracker.example.com/announce"
    result = redact_secrets(raw)
    assert result == "connecting to https://tracker_user:<redacted>@tracker.example.com/announce"
    assert "FAKEURLPASSWORD1" not in result
    assert "tracker_user" in result  # the account name stays diagnosable
    # The host stays diagnosable. Compare the PARSED hostname rather than a bare
    # substring check: an ``in`` test on an unparsed URL is the
    # incomplete-URL-substring-sanitization shape CodeQL flags (the host could
    # sit anywhere in the string); parsing pins it to the netloc exactly.
    assert urlsplit(result.rsplit(" ", 1)[1]).hostname == "tracker.example.com"


def test_redact_secrets_masks_empty_username_basic_auth_url() -> None:
    """A valid basic-auth URL with an EMPTY username (``https://:token@host``)
    still carries a secret in its userinfo -- the username run is ``*`` not
    ``+`` so the token is masked rather than leaked."""
    raw = "connecting to https://:FAKEURLTOKEN9@tracker.example.com/announce"
    result = redact_secrets(raw)
    assert result == "connecting to https://:<redacted>@tracker.example.com/announce"
    assert "FAKEURLTOKEN9" not in result
    assert urlsplit(result.rsplit(" ", 1)[1]).hostname == "tracker.example.com"


@pytest.mark.parametrize(
    ("scheme_word", "token"),
    [
        ("Bearer", "FAKEBEARERTOKEN.abc.def"),
        ("Basic", "ZmFrZTpjcmVkZW50aWFs"),
        # UNKNOWN schemes (RFC 7235 schemes are open-ended): a scheme allowlist
        # would consume only the scheme word here and leave the credential after
        # the space exposed -- the pattern must mask scheme + credential for ANY
        # scheme word, not just the well-known four.
        ("Token", "FAKEUNKNOWNSCHEME1"),
        ("ApiKey", "FAKEUNKNOWNSCHEME2"),
    ],
)
def test_redact_secrets_masks_the_whole_authorization_value(scheme_word: str, token: str) -> None:
    """Authorization values carry an internal SPACE (scheme + token) that the
    generic single-token value capture would only partially mask -- this must
    redact the ENTIRE value, not just leave the scheme word exposed and the
    token behind it untouched."""
    raw = f"Authorization: {scheme_word} {token}"
    result = redact_secrets(raw)
    assert result == "Authorization: <redacted>"
    assert token not in result
    assert scheme_word not in result


def test_redact_secrets_leaves_a_benign_tuple_key_containing_a_secret_word_alone() -> None:
    """The tuple pass fires only when a secret key word ENDS the quoted key
    name -- a key merely containing one mid-phrase is not a credential field
    and must pass through untouched."""
    raw = "queue=[('token of love', 'a title')]"
    assert redact_secrets(raw) == raw


def test_redact_secrets_masks_a_fernet_key_shaped_blob_regardless_of_context() -> None:
    """The Fernet key has no "key name" attached in a log line at all (it is
    loaded from a file) -- it must still be caught, on SHAPE alone."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    raw = f"generated a new encryption key at startup: {key}"
    result = redact_secrets(raw)
    assert key not in result
    assert "<redacted-fernet-key>" in result
    assert "generated a new encryption key at startup" in result


def test_redact_secrets_masks_a_fernet_key_after_a_kv_equals() -> None:
    """An env/config-dump rendering (``SOME_VAR=<key>``) puts a ``=`` right
    before the key -- the boundary lookbehind must not treat that as
    "mid-base64-run" and skip the master key (base64 padding only ever ENDS a
    token, so a ``=`` before a 44-char blob always starts a new one)."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    # An arbitrary variable name the key-name pass does NOT know: only the
    # shape pass can catch this, and it must fire across the ``=``.
    result = redact_secrets(f"env dump: SOME_RANDOM_VAR={key}")
    assert key not in result
    assert "<redacted-fernet-key>" in result
    # The app's own env var name IS covered by the key-name pass too
    # (``fernet_key`` is a token-family key) -- masked either way, never raw.
    result2 = redact_secrets(f"PLEX_MANAGER_FERNET_KEY={key}")
    assert key not in result2


def test_redact_secrets_does_not_mangle_an_unrelated_44_char_token() -> None:
    """A 44-char run that is NOT base64-shaped (contains a character outside the
    urlsafe-base64 alphabet, or has no trailing ``=``) must survive untouched --
    the Fernet pattern is deliberately shape-EXACT, not "roughly 44 chars"."""
    not_fernet_shaped = "x" * 44  # no trailing '=' pad
    assert redact_secrets(f"id={not_fernet_shaped}") == f"id={not_fernet_shaped}"


@pytest.mark.parametrize(
    "line",
    [
        "reconcile tick completed in 1.23s",
        "auto-grab cycle summary: attempted=3 granted=2 source_failures=1",
        "grab failed for request 42: HTTP 503 Service Unavailable",
        "pruned 12 log_events row(s) older than 7 day(s) or beyond 100000 row(s)",
        "TMDB request failed: GET /movie/603 (HTTP 429)",
        # An already-safe_guid-redacted value must pass through unchanged --
        # no accidental re-match of its hash-suffix shape.
        "release guid: tracker.example.org#a1b2c3d4e5f6",
        # A benign field whose name merely CONTAINS one of the secret roots as
        # a substring, not a whole word, must not be treated as a secret key.
        "tokenizer initialized with vocab_size=32000",
    ],
)
def test_redact_secrets_leaves_known_good_lines_unmangled(line: str) -> None:
    assert redact_secrets(line) == line


def test_redact_secrets_is_a_noop_on_empty_string() -> None:
    assert redact_secrets("") == ""


def test_redact_secrets_never_raises_on_malformed_input() -> None:
    # Odd-but-plausible malformed inputs: an unterminated quote, a lone '=' at
    # the end of the string right after a secret-shaped key name.
    for raw in ("password='unterminated", "token=", "api_key", "://:@"):
        redact_secrets(raw)  # must not raise


# --------------------------------------------------------------------------- #
# redact_known_secrets (issue #268) -- the VALUE-based complement to
# redact_secrets's SHAPE grammar above. These tests exercise exact-value
# matching independent of surrounding shape, the URL-encoded/base64 variants,
# the minimum-length guard, and (issue #270) the two shape-grammar gaps this
# pass is meant to categorically close: a cookie-jar/mapping-repr dump and a
# basic-auth URL whose password itself contains a raw ``@``.
# --------------------------------------------------------------------------- #

_FAKE_API_KEY = "sk-FAKEAPIKEY1234567890abcdef"
_FAKE_PASSWORD = "correct-horse-battery-staple"  # noqa: S105


def test_redact_known_secrets_masks_an_exact_occurrence_anywhere() -> None:
    raw = f"upstream request failed: GET /api?key={_FAKE_API_KEY}&lang=en"
    result = redact_known_secrets(raw, [_FAKE_API_KEY])
    assert _FAKE_API_KEY not in result
    assert "<redacted>" in result
    assert "GET /api?key=" in result and "&lang=en" in result


def test_redact_known_secrets_matches_regardless_of_surrounding_shape() -> None:
    """No key name, no quoting, no recognizable syntax at all -- just the raw
    value sitting mid-prose. redact_secrets's shape grammar has nothing to key
    on here; the value-based pass needs none."""
    raw = f"third-party client logged its own line mentioning {_FAKE_PASSWORD} directly"
    result = redact_known_secrets(raw, [_FAKE_PASSWORD])
    assert _FAKE_PASSWORD not in result
    assert "<redacted>" in result


def test_redact_known_secrets_masks_multiple_configured_values_independently() -> None:
    raw = f"plex_token={_FAKE_API_KEY} qbittorrent_password={_FAKE_PASSWORD}"
    result = redact_known_secrets(raw, [_FAKE_API_KEY, _FAKE_PASSWORD])
    assert _FAKE_API_KEY not in result
    assert _FAKE_PASSWORD not in result
    assert result.count("<redacted>") == 2


def test_redact_known_secrets_masks_a_url_percent_encoded_variant() -> None:
    """A password containing reserved URL characters often arrives percent-
    encoded (query string / userinfo) rather than raw -- the pass must catch
    the ENCODED rendering too, not just the literal value."""
    password_with_reserved_chars = "p@ss/word&more=stuff"  # noqa: S105
    encoded = quote(password_with_reserved_chars, safe="")
    raw = f"connecting to https://user:{encoded}@tracker.example.com/announce"
    result = redact_known_secrets(raw, [password_with_reserved_chars])
    assert encoded not in result
    assert "<redacted>" in result
    # Structural host check (not a substring test) so the parsed authority is
    # asserted exactly -- avoids CodeQL's incomplete-URL-substring flag.
    assert urlsplit(result.rsplit(" ", 1)[1]).hostname == "tracker.example.com"


def test_redact_known_secrets_masks_a_base64_encoded_variant() -> None:
    """A credential base64-encoded for a header/config dump must be caught in
    that rendering too, even though the raw value never appears literally."""
    b64 = base64.b64encode(_FAKE_PASSWORD.encode()).decode("ascii")
    raw = f"dumping config: qbittorrent_password_b64={b64}"
    result = redact_known_secrets(raw, [_FAKE_PASSWORD])
    assert b64 not in result
    assert "<redacted>" in result


def test_redact_known_secrets_applies_minimum_length_guard() -> None:
    """A very short 'secret' (below the length floor) must NOT redact common
    substrings elsewhere in the line -- over-redaction is the failure mode the
    guard exists to prevent."""
    short_value = "ab"
    raw = "grab this from the tab, then label it"
    result = redact_known_secrets(raw, [short_value])
    assert result == raw  # untouched: "ab" is far too short to treat as a secret


def test_redact_known_secrets_min_length_is_configurable() -> None:
    short_value = "abcdef"  # 6 chars
    raw = f"token value is {short_value} exactly"
    # Below the caller-supplied floor: skipped.
    assert redact_known_secrets(raw, [short_value], min_length=8) == raw
    # At/above the caller-supplied floor: masked.
    result = redact_known_secrets(raw, [short_value], min_length=6)
    assert short_value not in result


def test_redact_known_secrets_skips_none_and_empty_values() -> None:
    raw = f"value is {_FAKE_API_KEY}"
    result = redact_known_secrets(raw, ["", _FAKE_API_KEY])
    assert _FAKE_API_KEY not in result


def test_redact_known_secrets_is_a_noop_with_no_secret_values() -> None:
    raw = "ordinary log line with nothing configured"
    assert redact_known_secrets(raw, []) == raw


def test_redact_known_secrets_is_a_noop_on_empty_string() -> None:
    assert redact_known_secrets("", [_FAKE_API_KEY]) == ""


def test_redact_known_secrets_never_raises_on_malformed_input() -> None:
    for raw in ("", "plain text", "\ud800lone surrogate"):
        redact_known_secrets(raw, [_FAKE_API_KEY, "\ud800also-a-surrogate-secret"])


def test_redact_known_secrets_leaves_unrelated_text_untouched_when_value_absent() -> None:
    raw = "reconcile tick completed in 1.23s"
    assert redact_known_secrets(raw, [_FAKE_API_KEY]) == raw


# --- issue #270: shape-grammar gaps, one closed by a dedicated shape rule, --- #
# --- the other closed categorically by value matching ----------------------- #


def test_cookie_jar_mapping_repr_dump_is_caught_by_shape_grammar() -> None:
    """``_COOKIE_JAR_RE`` (issue #270 follow-up) closes the cookie-jar/mapping-
    repr gap directly in ``redact_secrets`` -- a REAL cookie-shaped value that
    was NEVER handed to ``redact_known_secrets`` (it is not a settings-store
    value: the true ``plexmgr.session`` credential is never anything but a
    HASH at rest) is still masked, because this pass keys on the cookie NAME
    shape, not on any known value."""
    session_value = "sVeryLongSessionTokenValue123456789"
    raw = f"outgoing cookies: {{'plexmgr.session': '{session_value}'}}"
    result = redact_secrets(raw)
    assert session_value not in result
    assert "<redacted>" in result
    assert "plexmgr.session" in result  # the cookie NAME survives, diagnosable


def test_qbittorrent_sid_mapping_repr_dump_is_caught_by_shape_grammar() -> None:
    """The same shape rule covers qBittorrent's ``SID``/``QBT_SID_<port>``
    session cookie rendered as a mapping repr -- e.g. ``dict(jar)`` logged for
    HTTP-client debugging -- with a REAL cookie-shaped value the qBittorrent
    adapter holds only in memory and never persists to settings, so
    ``redact_known_secrets`` could never have masked it."""
    sid_value = "qBTUpstreamSessionIdABCDEF0123456789"
    raw = f"outgoing cookies: {{'QBT_SID_8080': '{sid_value}'}}"
    result = redact_secrets(raw)
    assert sid_value not in result
    assert "<redacted>" in result
    assert "QBT_SID_8080" in result


def test_cookie_jar_mapping_repr_dump_is_also_caught_by_value_based_pass() -> None:
    """The value-based pass (issue #268) closes issue #270's cookie-jar/
    mapping-repr gap categorically WHEN the value happens to be one this app
    has configured in settings: it doesn't matter that the shape is a dict
    repr rather than a ``name=value`` header line -- the exact secret VALUE is
    masked wherever it appears. (In production the real cookie tokens are
    never settings values at all -- see the shape-grammar test above for the
    pass that actually covers THAT case.)"""
    session_value = "sVeryLongSessionTokenValue123456789"
    raw = f"outgoing cookies: {{'plexmgr.session': '{session_value}'}}"
    result = redact_known_secrets(raw, [session_value])
    assert session_value not in result
    assert "<redacted>" in result
    assert "plexmgr.session" in result  # the cookie NAME survives, diagnosable


def test_basic_auth_password_with_raw_at_sign_is_masked_whole_by_shape_grammar() -> None:
    """#292 item 3/short-password: ``_BASIC_AUTH_URL_RE``'s password run is now
    greedy up to the LAST ``@`` inside the authority, so a password that itself
    CONTAINS a raw ``@`` is masked WHOLE -- no suffix survives -- by the shape
    grammar ALONE, with no configured value needed (previously it stopped at the
    first internal ``@`` and leaked the remainder; issue #270's shape gap)."""
    password_with_at = "p@ssw0rd0123456789"  # noqa: S105
    raw = f"connecting to https://tracker_user:{password_with_at}@tracker.example.com/announce"
    result = redact_secrets(raw)
    assert password_with_at not in result
    assert "ssw0rd0123456789" not in result  # the former leak, now closed
    assert result == "connecting to https://tracker_user:<redacted>@tracker.example.com/announce"
    assert urlsplit(result.rsplit(" ", 1)[1]).hostname == "tracker.example.com"


def test_basic_auth_password_with_raw_at_sign_is_caught_by_value_based_pass() -> None:
    """The value-based pass (issue #268) independently closes issue #270's
    basic-auth gap: given the actual configured password, the WHOLE value is
    masked regardless of where an internal ``@`` sits -- a categorical guarantee
    that does not depend on the shape grammar's password-boundary heuristic."""
    password_with_at = "p@ssw0rd0123456789"  # noqa: S105
    raw = f"connecting to https://tracker_user:{password_with_at}@tracker.example.com/announce"
    result = redact_known_secrets(raw, [password_with_at])
    assert password_with_at not in result
    assert "ssw0rd0123456789" not in result
    assert "<redacted>" in result
    assert "tracker_user" in result


def test_short_at_sign_password_in_basic_auth_url_is_masked_by_shape_grammar() -> None:
    """#292 short-password gap: a password BELOW the value-based length floor
    (so ``redact_known_secrets`` skips it entirely) that contains a raw ``@`` is
    still masked whole by the greedy shape rule -- the shape pass has no length
    floor, so a short ``@``-bearing credential cannot slip through both passes."""
    short_at_password = "a@b"  # 3 chars -- under the value-based floor  # noqa: S105
    raw = f"connecting to https://u:{short_at_password}@tracker.example.com/announce"
    # Value-based pass alone leaves it (too short to mask):
    assert redact_known_secrets(raw, [short_at_password]) == raw
    # Shape pass masks it whole regardless:
    result = redact_secrets(raw)
    assert result == "connecting to https://u:<redacted>@tracker.example.com/announce"


def test_legacy_partially_redacted_basic_auth_row_is_recovered_by_shape_grammar() -> None:
    """#292 item 3: a row captured by the OLD first-``@`` regex was persisted
    already mangled -- ``user:<redacted>@<remainder>@host`` -- with the tail of
    an ``@``-bearing password exposed after the injected ``<redacted>``. The
    value-based read pass cannot recover it (the intact value no longer appears),
    but the greedy shape rule consumes the exposed remainder up to the last
    authority ``@`` and re-masks the row on read."""
    legacy_row = (
        "connecting to https://tracker_user:<redacted>@ssw0rd0123456789"
        "@tracker.example.com/announce"
    )
    result = redact_secrets(legacy_row)
    assert "ssw0rd0123456789" not in result  # the exposed legacy remainder, recovered
    assert result == "connecting to https://tracker_user:<redacted>@tracker.example.com/announce"


def test_basic_auth_shape_grammar_does_not_swallow_host_or_query() -> None:
    """Adversarial: the greedy password run must stop at the authority boundary
    (``/``/``?``/``#``), never swallow the host or a query ``@`` (an email in a
    ``redirect`` param) -- over-redaction would eat diagnosable log content."""
    raw = "GET https://user:pass1234@host.example.com/cb?to=https://a@evil.example.com"
    result = redact_secrets(raw)
    assert result == "GET https://user:<redacted>@host.example.com/cb?to=https://a@evil.example.com"


def test_shape_and_value_passes_compose_to_fully_redact_the_at_sign_case() -> None:
    """End-to-end: running BOTH passes in the order the capture/export
    boundaries actually apply them -- VALUE-based first, then shape -- fully
    redacts the basic-auth-with-embedded-``@`` case, and the greedy shape rule
    now also masks it on its own, so the two passes are complementary and each
    is independently sufficient for this shape."""
    password_with_at = "p@ssw0rd0123456789"  # noqa: S105
    raw = f"connecting to https://tracker_user:{password_with_at}@tracker.example.com/announce"
    value_first = redact_known_secrets(raw, [password_with_at])
    assert password_with_at not in value_first
    combined = redact_secrets(value_first)
    assert password_with_at not in combined
    assert "ssw0rd0123456789" not in combined
    # Shape alone (no configured value) now also fully masks it -- the former
    # first-``@`` gap is closed in the shape grammar itself.
    assert "ssw0rd0123456789" not in redact_secrets(raw)


# --- #292 item 4: value-based redaction must not eat a key NAME ------------- #


def test_value_equal_to_a_field_name_word_does_not_rewrite_an_identifier() -> None:
    """#292 item 4: when a configured secret's VALUE happens to equal a field-name
    word (an operator setting the qBittorrent password to the literal
    ``"password"``, a minimum-length edge case), a value occurrence sitting in
    KEY position (immediately before ``=``/``:``) must be left intact -- masking
    it would rewrite the identifier ``some_password=X`` -> ``some_<redacted>=X``,
    eating a key name operators need."""
    secret = "password"  # 8 chars: passes the length floor  # noqa: S105
    raw = "field some_password=X was updated"
    result = redact_known_secrets(raw, [secret])
    # The key name is preserved -- not rewritten to some_<redacted>=X:
    assert result == raw


def test_value_in_actual_value_position_is_still_masked_despite_the_key_guard() -> None:
    """The key-position guard must forfeit NO coverage of a real leak: the same
    field-name-shaped secret, when it appears as an actual VALUE (after the
    separator), is still masked -- only its KEY-position twin is spared."""
    secret = "password"  # noqa: S105
    raw = "qbittorrent_password=password"
    result = redact_known_secrets(raw, [secret])
    # Left ``password`` (key) preserved; right ``password`` (value) masked.
    assert result == "qbittorrent_password=<redacted>"


def test_key_guard_is_narrow_bare_value_before_equals_is_still_masked() -> None:
    """The key-name guard spares ONLY an occurrence that IS exactly a recognized
    secret key word. A high-entropy secret standing alone in prose -- even
    immediately followed by ``=`` -- fails that condition and is masked, so the
    guard opens no leak for this shape (adversarial: a blanket 'skip if followed
    by =' would have)."""
    raw = f"computed digest {_FAKE_API_KEY}=trailing"
    result = redact_known_secrets(raw, [_FAKE_API_KEY])
    assert _FAKE_API_KEY not in result
    assert "<redacted>" in result


def test_key_guard_masks_a_secret_embedded_in_a_larger_token_before_equals() -> None:
    """Round-2 regression (Codex, PR #361): a configured secret appearing as the
    TAIL of a larger unrecognized token immediately before ``=`` must be masked,
    never treated as a key-name suffix -- the earlier 'embedded in any
    identifier' rule left exactly this occurrence in clear text."""
    secret = "hunter2hunter2hunter2A"  # noqa: S105 -- fixture
    raw = f"payload=x{secret}=v"
    result = redact_known_secrets(raw, [secret])
    assert secret not in result
    assert result == "payload=x<redacted>=v"


def test_key_guard_masks_a_secret_embedded_in_a_larger_token_before_colon() -> None:
    """Adversarial variant of the round-2 regression: the same embedded-tail
    shape with ``:`` as the separator."""
    secret = "hunter2hunter2hunter2A"  # noqa: S105 -- fixture
    raw = f"payload=x{secret}:v"
    result = redact_known_secrets(raw, [secret])
    assert secret not in result
    assert result == "payload=x<redacted>:v"


def test_key_guard_masks_a_secret_appearing_twice_in_one_token() -> None:
    """Adversarial variant: the secret twice back-to-back in one token before
    ``=`` -- BOTH occurrences must mask (the second sits directly before the
    separator, the position the old rule spared)."""
    secret = "hunter2hunter2hunter2A"  # noqa: S105 -- fixture
    raw = f"blob {secret}{secret}=v"
    result = redact_known_secrets(raw, [secret])
    assert secret not in result
    assert result == "blob <redacted><redacted>=v"


def test_key_guard_masks_a_key_word_secret_in_url_userinfo_position() -> None:
    """Adversarial: even a key-WORD-valued secret is masked when it sits in a
    URL's userinfo (``https://password:x@host`` -- a USERNAME, not a key name):
    sparing there would print the secret as a plausible-looking username. The
    guard's authority-boundary check fails closed."""
    secret = "password"  # noqa: S105 -- the degenerate key-word-valued secret
    raw = "https://password:x@tracker.example.com/announce"
    result = redact_known_secrets(raw, [secret])
    assert result == "https://<redacted>:x@tracker.example.com/announce"
    # And the full boundary composition also masks the password half:
    combined = redact_secrets(result)
    assert combined == "https://<redacted>:<redacted>@tracker.example.com/announce"


def test_key_word_secret_spared_in_key_position_composes_to_mask_the_value() -> None:
    """The spare is safe BY COMPOSITION: it fires only where the shape pass
    (which every capture/read boundary runs after the value pass) recognizes the
    occurrence as a key and masks the value side -- so the pair's actual
    credential never survives, standalone or prefixed, bare or quoted. (Masking
    the key instead would BREAK the shape pass's key and leave the credential
    exposed: ``<redacted>=hunter2value``.)"""
    secret = "password"  # noqa: S105 -- the degenerate key-word-valued secret
    for raw, expected in (
        ("some_password=hunter2value", "some_password=<redacted>"),
        ("password=hunter2value", "password=<redacted>"),
        ("{'some_password': 'hunter2value'}", "{'some_password': '<redacted>'}"),
    ):
        value_first = redact_known_secrets(raw, [secret])
        assert "hunter2value" in value_first  # value pass spared the key, kept the pair
        combined = redact_secrets(value_first)
        assert "hunter2value" not in combined
        assert combined == expected


@pytest.mark.parametrize(
    "raw",
    [
        "deauthorization: unrelated diagnostic text",
        "preauthorization: unrelated diagnostic text",
    ],
)
def test_authorization_key_word_secret_is_not_spared_inside_larger_identifier(raw: str) -> None:
    value_first = redact_known_secrets(raw, ["authorization"])
    assert "authorization" not in value_first
    assert "unrelated diagnostic text" in value_first
    assert redact_secrets(value_first) == value_first


@pytest.mark.parametrize(
    ("key_word", "raw", "expected"),
    [
        (
            "password",
            "https://host.example/path?password=paired-sample-000",
            "https://host.example/path?password=<redacted>",
        ),
        (
            "authorization",
            "authorization: Bearer paired-sample-000",
            "authorization: <redacted>",
        ),
        (
            "password",
            "('password', 'paired-sample-000')",
            "('password', '<redacted>')",
        ),
        (
            "password",
            "password=['paired-sample-000']",
            "password=<redacted>",
        ),
        (
            "password",
            "('password', ['paired-sample-000'])",
            "('password', <redacted>)",
        ),
    ],
)
def test_key_word_secret_spared_in_non_kv_shape_positions_composes_to_mask_the_value(
    key_word: str,
    raw: str,
    expected: str,
) -> None:
    value_first = redact_known_secrets(raw, [key_word])
    assert key_word in value_first
    assert "paired-sample-000" in value_first

    combined = redact_secrets(value_first)

    assert "paired-sample-000" not in combined
    assert combined == expected


def test_key_guard_masks_a_key_word_secret_behind_an_overlong_identifier() -> None:
    """Round-3 regression (Codex, PR #361): ``_SECRET_KV_RE``'s key prefix is
    capped at 64 chars, so on ``<65+ char prefix>password=v`` the shape pass can
    never fire -- sparing ``password`` there rendered the configured secret in
    clear AND left the pair unrecognized. The spare now SELF-VERIFIES against
    ``_SECRET_KV_RE`` itself: no shape-pass match containing the occurrence in
    its key region means mask unconditionally -- any future shape-grammar
    limitation fails closed the same way."""
    secret = "password"  # noqa: S105 -- the degenerate key-word-valued secret
    for prefix_len in (65, 200):
        raw = "x" * prefix_len + "password=v"
        result = redact_known_secrets(raw, [secret])
        assert "password" not in result
        assert result == "x" * prefix_len + "<redacted>=v"
        # And the composition never resurfaces it either:
        assert "password" not in redact_secrets(result)


def test_key_guard_spares_a_prose_colon_prefixed_key_and_the_value_masks() -> None:
    """Round-3 regression (Codex, PR #361): ``error:password=hunter2`` -- a bare
    ``:`` before the identifier is a prose/logger prefix, NOT URL authority. The
    old single-char boundary check masked the key, broke the shape pass's key
    recognition, and left the VALUE exposed. The authority exception now
    requires a genuine ``scheme://`` prefix, the spare self-verifies against
    ``_SECRET_KV_RE``, and the composition masks the value."""
    secret = "password"  # noqa: S105 -- the degenerate key-word-valued secret
    raw = "error:password=hunter2"
    value_first = redact_known_secrets(raw, [secret])
    assert value_first == raw  # spared: the pair stays intact for the shape pass
    combined = redact_secrets(value_first)
    assert combined == "error:password=<redacted>"
    assert "hunter2" not in combined


def test_key_guard_scheme_authority_ambiguity_composes_safely() -> None:
    """Adversarial (round 3): ``scheme://error:password=x@host`` -- the same
    ``error:password`` spelling, but INSIDE a genuine authority (userinfo). The
    narrowed authority exception masks the occurrence, and
    ``_BASIC_AUTH_URL_RE`` then consumes the surrounding ``:<...>@`` span --
    neither the secret nor the paired value survives the composition."""
    secret = "password"  # noqa: S105 -- the degenerate key-word-valued secret
    raw = "https://error:password=x@tracker.example.com/a"
    value_first = redact_known_secrets(raw, [secret])
    assert "password" not in value_first
    combined = redact_secrets(value_first)
    assert combined == "https://error:<redacted>@tracker.example.com/a"


# --- #292 encoding-variant gaps: unpadded/base64url, percent case + ``+``, --- #
# --- SimpleCookie repr, JSON/repr string escaping -------------------------- #


def test_redact_known_secrets_masks_unpadded_and_urlsafe_base64_variants() -> None:
    """#292: only padded standard base64 was generated before; a credential can
    render base64url and/or unpadded (JWT-style, query contexts). All four
    (standard/urlsafe x padded/unpadded) renderings must be masked."""
    raw_bytes = _FAKE_PASSWORD.encode()
    renderings = {
        base64.b64encode(raw_bytes).decode(),
        base64.b64encode(raw_bytes).decode().rstrip("="),
        base64.urlsafe_b64encode(raw_bytes).decode(),
        base64.urlsafe_b64encode(raw_bytes).decode().rstrip("="),
    }
    for rendering in renderings:
        result = redact_known_secrets(f"dumping header token={rendering} end", [_FAKE_PASSWORD])
        assert rendering not in result, rendering
        assert "<redacted>" in result


def test_redact_known_secrets_masks_lowercase_percent_and_plus_encoded_variants() -> None:
    """#292: percent-encoding arrives in case variants (``%2f`` as well as the
    ``%2F`` ``quote`` emits) and with ``+``-for-space (``quote_plus``). Every
    such spelling of the same secret must be masked."""
    password = "p@ss w/d&x=1"  # reserved chars + a space  # noqa: S105
    spellings = {
        quote(password, safe=""),
        quote(password, safe="").lower(),
        quote_plus(password, safe=""),
        quote_plus(password, safe="").lower(),
    }
    for spelling in spellings:
        result = redact_known_secrets(f"connecting with pw={spelling} now", [password])
        assert spelling not in result, spelling
        assert "<redacted>" in result


def test_redact_known_secrets_masks_per_escape_mixed_case_percent_spellings() -> None:
    """Round-2 regression (Codex, PR #361): percent-escape case must be handled
    PER ESCAPE, not by enumerating all-upper/all-lower spellings -- a rendering
    mixing ``%2f`` next to ``%3D`` in ONE string escaped the enumerated variants.
    ``_variant_regex`` compiles each escape's hex pair case-insensitively, so
    every mix masks. Unencoded characters remain case-significant."""
    password = "p@ss w/d&x=1"  # reserved chars + a space  # noqa: S105
    canonical = quote(password, safe="")
    assert canonical == "p%40ss%20w%2Fd%26x%3D1"  # two case-bearing escapes: %2F, %3D
    mixed_spellings = [
        "p%40ss%20w%2fd%26x%3D1",  # first escape lower, second upper -- a true mix
        "p%40ss%20w%2Fd%26x%3d1",  # first upper, second lower -- the reverse mix
        "p%40ss+w%2fd%26x%3D1",  # quote_plus (+-for-space) spelling, mixed case
        "p%40ss+w%2Fd%26x%3d1",  # quote_plus, the reverse mix
    ]
    for spelling in mixed_spellings:
        result = redact_known_secrets(f"connecting with pw={spelling} now", [password])
        assert spelling not in result, spelling
        assert "<redacted>" in result
    # Case-insensitivity applies to the ESCAPES only: a different unencoded
    # letter is a different string and must NOT match (no over-redaction).
    unrelated = "P%40ss%20w%2Fd%26x%3D1"  # leading literal 'P' != 'p'
    assert redact_known_secrets(f"pw={unrelated} now", [password]).count("<redacted>") == 0


def test_redact_known_secrets_masks_json_and_repr_escaped_variants() -> None:
    """#292: a secret containing a quote/backslash renders ESCAPED inside a JSON
    log field or a Python ``repr()`` -- the raw bytes never appear literally, so
    the escaped body must be matched too."""
    secret = 'tok"en\\with-quotes-1234'  # contains both a quote and a backslash  # noqa: S105
    json_body = json.dumps(secret)[1:-1]
    repr_body = repr(secret)[1:-1]
    assert json_body != secret and repr_body != secret  # escaping actually happened
    json_line = f'{{"api_key": "{json_body}"}}'
    repr_line = f"headers={{'api_key': {secret!r}}}"
    assert secret not in json_line  # the literal never appears; only the escaped body does
    for line, body in ((json_line, json_body), (repr_line, repr_body)):
        result = redact_known_secrets(line, [secret])
        assert body not in result, body
        assert "<redacted>" in result


def test_redact_known_secrets_masks_case_variant_json_unicode_escapes() -> None:
    """Round-3 regression (Codex, PR #361): ``json.dumps`` emits lowercase
    ``\\uXXXX`` hex, but other serializers emit uppercase -- and per-escape
    MIXES of both -- all decoding to the same secret. ``_variant_regex``
    compiles each escape's hex digits case-insensitively (the same categorical
    approach as the percent-escape fix), so every spelling masks. Unescaped
    characters remain case-significant."""
    secret = "pässwörd-token-A1"  # two non-ASCII chars -> two \\uXXXX escapes  # noqa: S105
    body = json.dumps(secret)[1:-1]
    assert body == "p\\u00e4ssw\\u00f6rd-token-A1"
    spellings = [
        body,  # canonical lowercase, as json.dumps emits
        body.replace("\\u00e4", "\\u00E4"),  # first escape upper, second lower -- a mix
        body.replace("\\u00f6", "\\u00F6"),  # first lower, second upper -- the reverse mix
        body.replace("\\u00e4", "\\u00E4").replace("\\u00f6", "\\u00F6"),  # all upper
    ]
    for spelling in spellings:
        line = f'{{"api_key": "{spelling}"}}'
        result = redact_known_secrets(line, [secret])
        assert spelling not in result, spelling
        assert "<redacted>" in result
    # Case-insensitivity applies to the ESCAPES only: a different unescaped
    # letter is a different string and must NOT match (no over-redaction).
    unrelated = f'{{"api_key": "{body.replace("token", "Token")}"}}'
    assert redact_known_secrets(unrelated, [secret]).count("<redacted>") == 0
    # ``\\xXX`` repr byte escapes get the same treatment:
    esc_secret = "p\x1bq-token-12345"  # noqa: S105 -- fixture with an \x1b escape
    repr_body = repr(esc_secret)[1:-1]
    assert "\\x1b" in repr_body
    upper_spelling = repr_body.replace("\\x1b", "\\x1B")
    result = redact_known_secrets(f"raw={upper_spelling} end", [esc_secret])
    assert upper_spelling not in result
    assert "<redacted>" in result


def test_redact_known_secrets_matches_mixed_json_solidus_spellings() -> None:
    value = "abc/def/ghi"
    for body in ("abc\\/def/ghi", "abc/def\\/ghi", "abc\\/def\\/ghi"):
        text = f'{{"detail":"{body}"}}'
        assert redact_known_secrets(text, [value]) == '{"detail":"<redacted>"}'


def test_redact_known_secrets_matches_json_solidus_in_derived_base64_variants() -> None:
    for value in ("secret12?00", "secret12?00?"):
        standard = base64.b64encode(value.encode("utf-8")).decode("ascii")
        assert "/" in standard
        spellings = {standard.replace("/", "\\/", 1), standard.replace("/", "\\/")}
        for spelling in spellings:
            assert redact_known_secrets(f"before:{spelling}:after", [value]) == (
                "before:<redacted>:after"
            )


def test_redact_known_secrets_treats_configured_backslash_solidus_as_literal() -> None:
    value = r"abc\/defgh"
    text = '{"detail":"abc/defgh"}'
    assert redact_known_secrets(text, [value]) == text


def test_redact_known_secrets_does_not_overmatch_derived_base64_solidus_control() -> None:
    value = "secret12?00"
    standard = base64.b64encode(value.encode("utf-8")).decode("ascii")
    different = standard.replace("MDA", "MDB").replace("/", "\\/")
    assert redact_known_secrets(different, [value]) == different


def test_redact_known_secrets_does_not_match_different_json_literal() -> None:
    text = '{"detail":"abc\\/defXghi"}'
    assert redact_known_secrets(text, ["abc/def/ghi"]) == text


def test_redact_known_secrets_preserves_raw_and_one_step_for_long_values() -> None:
    value = "long-secret-" + "x" * 17000
    raw_bytes = value.encode("utf-8", "surrogatepass")
    standard = base64.b64encode(raw_bytes).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    spellings = {
        value,
        quote(value, safe="", errors="surrogatepass"),
        quote_plus(value, safe="", errors="surrogatepass"),
        standard,
        standard.rstrip("="),
        urlsafe,
        urlsafe.rstrip("="),
        json.dumps(value)[1:-1],
        repr(value)[1:-1],
    }
    for spelling in spellings:
        assert (
            redact_known_secrets(f"before:{spelling}:after", [value]) == "before:<redacted>:after"
        )


def test_redact_known_secrets_does_not_expand_depth_three() -> None:
    value = "depth-two-secret/marker"
    depth_one = quote(value, safe="", errors="surrogatepass")
    depth_two = quote(depth_one, safe="", errors="surrogatepass")
    depth_three = quote(depth_two, safe="", errors="surrogatepass")
    assert redact_known_secrets(depth_one, [value]) == "<redacted>"
    assert redact_known_secrets(depth_two, [value]) == "<redacted>"
    assert redact_known_secrets(depth_three, [value]) == depth_three


def test_redact_known_secrets_handles_lone_surrogates() -> None:
    value = "surrogate-" + "\\ud800" + "-secret"
    rendered = quote(value, safe="", errors="surrogatepass")
    assert redact_known_secrets(rendered, [value]) == "<redacted>"


def test_redact_known_secrets_masks_bounded_two_stage_percent_and_base64_variants() -> None:
    value = "sample /?=A9-marker"
    raw_bytes = value.encode("utf-8", "surrogatepass")

    def _b64_spellings(data: bytes) -> set[str]:
        standard = base64.b64encode(data).decode("ascii")
        urlsafe = base64.urlsafe_b64encode(data).decode("ascii")
        return {standard, standard.rstrip("="), urlsafe, urlsafe.rstrip("=")}

    first_percent = {
        quote(value, safe="", errors="surrogatepass"),
        quote_plus(value, safe="", errors="surrogatepass"),
    }
    second_percent = {
        encoder(first, safe="", errors="surrogatepass")
        for first in first_percent
        for encoder in (quote, quote_plus)
    }
    base64_of_percent = {
        spelling for first in first_percent for spelling in _b64_spellings(first.encode("ascii"))
    }
    raw_base64 = _b64_spellings(raw_bytes)
    percent_of_base64 = {
        encoder(spelling, safe="", errors="surrogatepass")
        for spelling in raw_base64
        for encoder in (quote, quote_plus)
    }
    renderings = second_percent | base64_of_percent | percent_of_base64

    assert renderings
    for rendering in renderings:
        result = redact_known_secrets(f"before:{rendering}:after", [value])
        assert rendering not in result
        assert result == "before:<redacted>:after"

    canonical_double_percent = quote(
        quote(value, safe="", errors="surrogatepass"),
        safe="",
        errors="surrogatepass",
    )
    assert "%252F" in canonical_double_percent
    mixed_inner_hex = canonical_double_percent.replace("%252F", "%252f")
    assert (
        redact_known_secrets(f"before:{mixed_inner_hex}:after", [value])
        == "before:<redacted>:after"
    )

    literal_case_changed = canonical_double_percent.replace("sample", "Sample", 1)
    assert literal_case_changed != canonical_double_percent
    assert (
        redact_known_secrets(f"before:{literal_case_changed}:after", [value]).count("<redacted>")
        == 0
    )


def test_simplecookie_repr_session_token_is_masked_by_shape_grammar() -> None:
    """#292: ``http.cookies.SimpleCookie``'s repr -- ``<SimpleCookie:
    plexmgr.session='SECRET'>`` (unquoted name, ``=``, quoted value) -- is
    matched by neither the raw-header cookie pass nor the dict-repr pass, so its
    own shape rule masks it. The cookie token is never a settings value, so the
    shape rule is the ONLY possible barrier."""
    session_value = "sVeryLongSessionTokenValue123456789"
    cookie = SimpleCookie()
    cookie["plexmgr.session"] = session_value
    raw = f"outgoing {cookie!r}"
    assert session_value in raw  # the value really is exposed in the repr
    result = redact_secrets(raw)
    assert session_value not in result
    assert "<redacted>" in result
    assert "plexmgr.session" in result  # the cookie NAME survives, diagnosable


def test_simplecookie_repr_qbittorrent_sid_is_masked_by_shape_grammar() -> None:
    """The same shape rule covers qBittorrent's ``QBT_SID_<port>`` session cookie
    rendered as a ``SimpleCookie`` repr."""
    sid_value = "qBTUpstreamSessionIdABCDEF0123456789"
    cookie = SimpleCookie()
    cookie["QBT_SID_8080"] = sid_value
    raw = f"jar {cookie!r}"
    result = redact_secrets(raw)
    assert sid_value not in result
    assert "<redacted>" in result
    assert "QBT_SID_8080" in result


def test_simplecookie_repr_does_not_false_match_non_session_prose() -> None:
    """Honesty over silence: the SimpleCookie rule keys on a session/sid cookie
    NAME, so an ordinary quoted assignment for an unrelated cookie/field is left
    untouched."""
    raw = "config theme='dark' locale='en-US'"
    assert redact_secrets(raw) == raw
