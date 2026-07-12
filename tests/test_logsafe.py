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

import hashlib
import re

import pytest

from plex_manager.logsafe import redact_secrets, safe_guid, safe_int, safe_text


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
    ],
)
def test_redact_secrets_masks_key_value_shaped_secrets(
    raw: str, secret: str, expected: str
) -> None:
    result = redact_secrets(raw)
    assert result == expected
    assert secret not in result


def test_redact_secrets_masks_basic_auth_url_password() -> None:
    raw = "connecting to https://tracker_user:FAKEURLPASSWORD1@tracker.example.com/announce"
    result = redact_secrets(raw)
    assert result == "connecting to https://tracker_user:<redacted>@tracker.example.com/announce"
    assert "FAKEURLPASSWORD1" not in result
    assert "tracker_user" in result  # the account name stays diagnosable
    assert "tracker.example.com" in result  # the host stays diagnosable


@pytest.mark.parametrize(
    ("scheme_word", "token"),
    [
        ("Bearer", "FAKEBEARERTOKEN.abc.def"),
        ("Basic", "ZmFrZTpjcmVkZW50aWFs"),
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
