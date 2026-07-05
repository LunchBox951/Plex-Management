"""Unit tests for the ``plex_manager.logsafe`` log-value barriers.

These are the single-purpose barriers every request-derived log site passes its
values through (see CONTRIBUTING.md "Logging request-derived values"): ``safe_int``
re-coerces an id (a no-op for a real int, a taint barrier for CodeQL's
py/log-injection), ``safe_text`` collapses CR/LF so a request-derived string
cannot forge a second log record, and ``safe_guid`` additionally strips the
credential-bearing part of a URL-shaped release GUID (a Prowlarr private-indexer
GUID can embed a tracker passkey/session token) so a secret is never logged.
"""

from __future__ import annotations

import hashlib

import pytest

from plex_manager.logsafe import safe_guid, safe_int, safe_text


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
        "Some.Movie.2020.1080p.WEB-DL.x264-GROUP",  # a title-style guid
        "0123456789abcdef0123456789abcdef",  # a plain hex/info-hash-style id
        "urn:uuid:12345678-1234-5678-1234-567812345678",  # scheme but NO netloc
    ],
)
def test_safe_guid_passes_non_url_guids_through_unchanged(raw: str) -> None:
    """A non-URL GUID is not secret-bearing, so it is logged as before (only the
    ``safe_text`` CR/LF barrier applies)."""
    assert safe_guid(raw) == safe_text(raw)
    assert safe_guid(raw) == raw  # nothing to collapse in these inputs


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
