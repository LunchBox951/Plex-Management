"""QbittorrentClient adapter tests — recorded ``/api/v2`` shapes via MockTransport.

Covers: cookie login (legacy ``SID`` captured), magnet add (hash derived from the
magnet ``xt``), a 409-already-present add (treated as success), ``/torrents/info``
with several qBit-5 states including ``stoppedUP`` and ``metaDL`` (``raw_state``
kept verbatim), and remove. The password / session cookie are never asserted into
logs. An OPTIONAL live smoke test is env-guarded.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
from typing import Any

import httpcore
import httpx
import pytest

from plex_manager.adapters.qbittorrent import (
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
    QbittorrentSourceError,
)
from plex_manager.adapters.qbittorrent.adapter import (
    _HASHES_PER_REQUEST,  # pyright: ignore[reportPrivateUsage]
    _MAX_TORRENT_BYTES,  # pyright: ignore[reportPrivateUsage]
    SafeFetchNetworkBackend,
)

BASE_URL = "http://qbit.local:8080"
USERNAME = "admin"
PASSWORD = "never-logged-secret"  # noqa: S105

MAGNET = "magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678&dn=Test"
MAGNET_HASH = "1234567890abcdef1234567890abcdef12345678"

INFO_ROWS: list[dict[str, Any]] = [
    {
        "hash": "1234567890ABCDEF1234567890ABCDEF12345678",
        "name": "Completed.Movie.1080p",
        "state": "stoppedUP",  # qBit 5: finished + stopped
        "progress": 1.0,
        "ratio": 2.5,
        "save_path": "/downloads/movies",
        "content_path": "/downloads/movies/Completed.Movie.1080p.mkv",
        "eta": 8640000,
        "ratio_limit": -2,
        "seeding_time_limit": -2,
        "inactive_seeding_time_limit": -2,
        "last_activity": 1700000000,
    },
    {
        "hash": "abcabcabcabcabcabcabcabcabcabcabcabcabca",
        "name": "Fetching.Metadata",
        "state": "metaDL",  # magnet metadata being fetched
        "progress": 0.0,
        "ratio": 0.0,
        "save_path": "/downloads/movies/",
        "content_path": "/downloads/movies",  # echoes save_path -> dropped to None
        "eta": -1,
        "ratio_limit": 1.5,
        "seeding_time_limit": 120,
        "inactive_seeding_time_limit": 30,
        "last_activity": 1700000500,
    },
]


FILE_ROWS: list[dict[str, Any]] = [
    {"name": "Completed.Movie.1080p/movie.mkv", "size": 8_589_934_592},
    {"name": "Completed.Movie.1080p/sample.mkv", "size": 52_428_800},
    {"name": "Completed.Movie.1080p/readme.txt", "size": 1024},
]


def _login_response() -> httpx.Response:
    return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=test-session-id; path=/"})


def _router(*, add_status: int = 200, webapi_version: str = "2.11.0") -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/api/v2/auth/login" and method == "POST":
            return _login_response()
        if path == "/api/v2/app/webapiVersion" and method == "GET":
            return httpx.Response(200, text=webapi_version)
        if path == "/api/v2/torrents/add" and method == "POST":
            if add_status == 409:
                return httpx.Response(409, text="")
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info" and method == "GET":
            hashes = request.url.params.get("hashes")
            if hashes is not None:
                wanted = hashes.lower()
                rows = [r for r in INFO_ROWS if r["hash"].lower() == wanted]
                return httpx.Response(200, json=rows)
            return httpx.Response(200, json=INFO_ROWS)
        if path == "/api/v2/torrents/delete" and method == "POST":
            return httpx.Response(200, text="")
        if path == "/api/v2/torrents/setCategory" and method == "POST":
            return httpx.Response(200, text="")
        if path == "/api/v2/torrents/properties" and method == "GET":
            return httpx.Response(200, json={"save_path": "/downloads/movies"})
        if path == "/api/v2/torrents/files" and method == "GET":
            return httpx.Response(200, json=FILE_ROWS)
        return httpx.Response(404, text="unhandled")

    return handler


def _client(
    handler: Any | None = None, trusted_source_origin: str | None = None
) -> QbittorrentClient:
    transport = httpx.MockTransport(handler or _router())
    http = httpx.AsyncClient(transport=transport)
    return QbittorrentClient(
        http,
        BASE_URL,
        USERNAME,
        PASSWORD,
        source_client=http,
        trusted_source_origin=trusted_source_origin,
    )


async def test_add_magnet_returns_derived_hash() -> None:
    client = _client()
    result = await client.add(MAGNET, "/downloads/movies", "plex-manager")
    assert result.torrent_hash == MAGNET_HASH
    assert result.created is True  # a genuine new add -- the grab owns it


async def test_add_with_directed_save_path_disables_autotmm() -> None:
    """A non-empty ``save_path`` (issues #133/#157) must ALSO pin the torrent to
    manual management -- otherwise a global-AutoTMM install ignores ``savepath``
    entirely and places the torrent per its own category/auto rules."""
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add" and request.method == "POST":
            calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404, text="unhandled")

    await _client(handler).add(MAGNET, "/downloads/movies", "plex-manager")
    assert calls == [
        {
            "savepath": "/downloads/movies",
            "category": "plex-manager",
            "autoTMM": "false",
            "urls": MAGNET,
        }
    ]


async def test_add_with_no_save_path_omits_autotmm() -> None:
    """An empty ``save_path`` means nothing to direct -- ``autoTMM`` must be left
    out entirely so the client's own auto-managed/manual mode is untouched."""
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add" and request.method == "POST":
            calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404, text="unhandled")

    await _client(handler).add(MAGNET, "", "plex-manager")
    assert calls == [{"savepath": "", "category": "plex-manager", "urls": MAGNET}]
    assert "autoTMM" not in calls[0]


async def test_add_409_already_present_is_success() -> None:
    client = _client(_router(add_status=409))
    result = await client.add(MAGNET, "/downloads/movies", "plex-manager")
    assert result.torrent_hash == MAGNET_HASH
    # The honest already-present signal: the torrent PREDATES this call, so a
    # lost-grab cleanup must never remove it with delete_files (round 8).
    assert result.created is False


async def test_login_failure_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Fails.")
        return httpx.Response(200, json=[])

    with pytest.raises(QbittorrentAuthError):
        await _client(handler).get_all_statuses()


async def test_login_5xx_raises_outage_error_not_auth_error() -> None:
    # A 5xx on /auth/login means the WebUI (or a reverse proxy) is down, NOT that
    # the credentials are wrong: it must surface as a retryable QbittorrentError so
    # the operator isn't wrongly sent to reset credentials. QbittorrentAuthError is
    # a QbittorrentError subclass, so assert it is specifically NOT the auth type.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json=[])

    with pytest.raises(QbittorrentError) as exc_info:
        await _client(handler).get_all_statuses()
    assert not isinstance(exc_info.value, QbittorrentAuthError)
    assert PASSWORD not in str(exc_info.value)


async def test_login_200_fails_body_raises_auth_error() -> None:
    # The canonical bad-credentials signal: HTTP 200 with body "Fails.".
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Fails.")
        return httpx.Response(200, json=[])

    with pytest.raises(QbittorrentAuthError):
        await _client(handler).get_all_statuses()


async def test_auth_error_excludes_password() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Fails.")

    try:
        await _client(handler).get_all_statuses()
    except QbittorrentAuthError as exc:
        assert PASSWORD not in str(exc)
    else:  # pragma: no cover - guarded above
        pytest.fail("expected QbittorrentAuthError")


async def test_get_all_statuses_preserves_raw_state() -> None:
    statuses = await _client().get_all_statuses()
    assert len(statuses) == 2
    by_state = {s.raw_state: s for s in statuses}
    assert "stoppedUP" in by_state  # raw qBit-5 string kept verbatim
    assert "metaDL" in by_state
    done = by_state["stoppedUP"]
    assert done.progress == 1.0
    assert done.ratio == 2.5
    assert done.content_path == "/downloads/movies/Completed.Movie.1080p.mkv"
    assert done.eta_seconds is None or done.eta_seconds > 0
    meta = by_state["metaDL"]
    assert meta.content_path is None  # echoed save_path dropped
    assert meta.seeding_time_limit_minutes == 120
    assert meta.ratio_limit == 1.5


async def test_get_status_by_hash() -> None:
    status = await _client().get_status(MAGNET_HASH)
    assert status is not None
    assert status.info_hash == MAGNET_HASH  # lowercased
    assert status.raw_state == "stoppedUP"


async def test_get_status_absent_returns_none() -> None:
    status = await _client().get_status("ffffffffffffffffffffffffffffffffffffffff")
    assert status is None


# --------------------------------------------------------------------------- #
# get_statuses_for_hashes — the scoped snapshot query (issue #216)
# --------------------------------------------------------------------------- #


async def test_get_statuses_for_hashes_empty_input_sends_no_request() -> None:
    """No tracked hashes means nothing to ask the client about at all -- not even
    an authenticated round-trip. (The primary empty-fast-path guard lives in
    ``queue_service.reconcile_and_list``; this is the adapter's own defense in
    depth for any other caller.)"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, text="unhandled")

    result = await _client(handler).get_statuses_for_hashes([])
    assert result == []
    assert calls == []  # not even /auth/login


async def test_get_statuses_for_hashes_scopes_the_query_to_given_hashes() -> None:
    """The request carries EXACTLY the pipe-separated, lowercased tracked
    hashes -- never an unfiltered inventory request."""
    seen_params: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/info" and request.method == "GET":
            seen_params.append(request.url.params.get("hashes"))
            wanted = set((request.url.params.get("hashes") or "").split("|"))
            rows = [r for r in INFO_ROWS if r["hash"].lower() in wanted]
            return httpx.Response(200, json=rows)
        return httpx.Response(404, text="unhandled")

    hashes = [INFO_ROWS[0]["hash"].upper(), INFO_ROWS[1]["hash"]]
    statuses = await _client(handler).get_statuses_for_hashes(hashes)

    assert len(seen_params) == 1  # one request -- both hashes fit in one chunk
    requested = set((seen_params[0] or "").split("|"))
    assert requested == {row["hash"].lower() for row in INFO_ROWS}
    assert {s.info_hash for s in statuses} == {row["hash"].lower() for row in INFO_ROWS}


async def test_get_statuses_for_hashes_unknown_hash_yields_empty_not_error() -> None:
    """A hash qBittorrent doesn't recognize is simply absent from its JSON array
    response -- never a raised error (the reconciler treats the resulting gap as
    the honest ClientMissing signal, not a failure to surface)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/info" and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="unhandled")

    statuses = await _client(handler).get_statuses_for_hashes(
        ["ffffffffffffffffffffffffffffffffffffff"]
    )
    assert statuses == []


async def test_get_statuses_for_hashes_chunks_large_hash_sets() -> None:
    """A tracked set larger than the per-request bound is split into multiple
    bounded requests, never one unboundedly long URL."""
    total = _HASHES_PER_REQUEST + 1
    hashes = [f"{i:040x}" for i in range(total)]
    request_hash_sets: list[set[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/info" and request.method == "GET":
            param: str = request.url.params.get("hashes") or ""
            batch: set[str] = set(param.split("|"))
            request_hash_sets.append(batch)
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="unhandled")

    await _client(handler).get_statuses_for_hashes(hashes)

    assert len(request_hash_sets) == 2  # 101 hashes -> two chunks of <= 100
    sizes = sorted(len(batch) for batch in request_hash_sets)
    assert sizes == [1, _HASHES_PER_REQUEST]
    combined: set[str] = set()
    for batch in request_hash_sets:
        combined |= batch
    assert combined == set(hashes)


async def test_get_statuses_for_hashes_dedupes_repeated_hashes() -> None:
    """A season pack can put the SAME hash on multiple tracked rows; the query
    must not send (or count) it more than once."""
    seen_params: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/info" and request.method == "GET":
            seen_params.append(request.url.params.get("hashes"))
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="unhandled")

    await _client(handler).get_statuses_for_hashes([MAGNET_HASH, MAGNET_HASH.upper()])
    assert seen_params == [MAGNET_HASH]


async def test_remove_and_get_save_path() -> None:
    client = _client()
    await client.remove(MAGNET_HASH, delete_files=True)
    save_path = await client.get_save_path(MAGNET_HASH)
    assert save_path == "/downloads/movies"


async def test_list_files_maps_name_and_size() -> None:
    files = await _client().list_files(MAGNET_HASH)
    assert len(files) == 3
    biggest = max(files, key=lambda f: f.size_bytes)
    assert biggest.name == "Completed.Movie.1080p/movie.mkv"
    assert biggest.size_bytes == 8_589_934_592


async def test_list_files_authenticates_like_other_methods() -> None:
    calls = {"login": 0, "files": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            calls["login"] += 1
            return _login_response()
        if request.url.path == "/api/v2/torrents/files":
            calls["files"] += 1
            assert request.url.params.get("hash") == MAGNET_HASH
            return httpx.Response(200, json=FILE_ROWS)
        return httpx.Response(404)

    files = await _client(handler).list_files(MAGNET_HASH)
    assert len(files) == 3
    assert calls["login"] == 1  # logged in before the files call
    assert calls["files"] == 1


async def test_list_files_empty_response_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    assert await _client(handler).list_files(MAGNET_HASH) == []


async def test_list_files_error_status_raises_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(404)

    with pytest.raises(QbittorrentError):
        await _client(handler).list_files(MAGNET_HASH)


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_list_files_redirect_status_raises_typed_error(status: int) -> None:
    """A 3xx (e.g. a proxy/auth redirect in front of qBittorrent) must be rejected
    like any other non-2xx (issue #87) — ``httpx.Response.is_error`` excludes 3xx,
    so the prior check would have read a redirect as a successful operation even
    though the requested torrent-files listing never actually happened."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/files":
            # A valid JSON body on the redirect ensures this test only passes via
            # the explicit 2xx-range check in _raise_for_status (issue #87): if
            # that check were reverted to ``response.is_error``, the 3xx would
            # sail past it and _decode_json would happily parse the empty-list
            # body, silently reading the redirect as a successful (empty) files
            # listing — the exact regression this test must catch.
            return httpx.Response(status, json=[], headers={"Location": "/login"})
        return httpx.Response(404)

    with pytest.raises(QbittorrentError) as exc_info:
        await _client(handler).list_files(MAGNET_HASH)
    assert str(status) in str(exc_info.value)


async def test_relogin_on_403() -> None:
    calls = {"login": 0, "info": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            calls["login"] += 1
            return _login_response()
        if request.url.path == "/api/v2/torrents/info":
            calls["info"] += 1
            if calls["info"] == 1:
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(200, json=INFO_ROWS)
        return httpx.Response(404)

    statuses = await _client(handler).get_all_statuses()
    assert len(statuses) == 2
    assert calls["login"] == 2  # initial + re-login after 403


# A minimal but well-formed .torrent: a top dict whose ``info`` is a bencoded
# dict. The info-hash is SHA-1 over the raw ``info`` dict bytes.
_INFO_DICT = b"d6:lengthi100e4:name8:test.txt12:piece lengthi16384ee"
_TORRENT_BYTES = b"d8:announce14:http://x/annce4:info" + _INFO_DICT + b"e"
_TORRENT_HASH = hashlib.sha1(_INFO_DICT).hexdigest()  # noqa: S324
_FAKE_INFO_DICT = b"d6:lengthi1e4:name8:fake.txte"
_NESTED_INFO_TORRENT_BYTES = b"d3:food4:info" + _FAKE_INFO_DICT + b"e4:info" + _INFO_DICT + b"e"

DOWNLOAD_URL = "http://93.184.216.34/file.torrent"
REDIRECT_URL = "http://93.184.216.34/redirect"
REDIRECT_MAGNET = "magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98&dn=Redirected"
REDIRECT_HASH = "fedcba9876543210fedcba9876543210fedcba98"


async def test_add_torrent_file_url_computes_bencode_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(200, content=_TORRENT_BYTES)
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    info_hash = (
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")
    ).torrent_hash
    assert info_hash == _TORRENT_HASH


async def test_add_torrent_file_hashes_top_level_info_dict() -> None:
    """A nested key named ``info`` must not shadow the torrent's top-level info dict."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(200, content=_NESTED_INFO_TORRENT_BYTES)
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    info_hash = (
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")
    ).torrent_hash
    assert info_hash == _TORRENT_HASH
    assert info_hash != hashlib.sha1(_FAKE_INFO_DICT).hexdigest()  # noqa: S324


async def test_add_http_redirect_to_magnet_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == REDIRECT_URL:
            return httpx.Response(302, headers={"Location": REDIRECT_MAGNET})
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    info_hash = (
        await _client(handler).add(REDIRECT_URL, "/downloads", "plex-manager")
    ).torrent_hash
    assert info_hash == REDIRECT_HASH


async def test_add_opaque_http_url_is_rejected_before_client_add() -> None:
    """An HTTP release URL that is neither a magnet redirect nor a locally hashable
    .torrent must fail before /torrents/add. Otherwise the app returns an error
    after qBittorrent already accepted an untracked torrent."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(200, content=b"not a torrent")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_uppercase_http_url_uses_resolver_before_client_add() -> None:
    """URI schemes are case-insensitive. Uppercase HTTP(S) must not bypass the
    local resolver/safety checks and get handed to qBittorrent as an opaque URL."""
    seen: list[str] = []
    upper_url = "HTTP://93.184.216.34/file.torrent"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == upper_url:
            return httpx.Response(200, content=b"not a torrent")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(upper_url, "/downloads", "plex-manager")

    assert any(url.lower() == upper_url.lower() for url in seen)
    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_oversized_declared_content_length_raises_source_error() -> None:
    """A source declaring a Content-Length past the cap is vetoed BEFORE the body is
    buffered and before any qBittorrent request — a RELEASE problem, so the distinct
    QbittorrentSourceError (422 / per-release auto-grab failure), never the base
    class that reads as a client outage. (The old test's 2_000_001 bytes fell UNDER
    the raised 10 MiB cap and was actually exercising the unhashable-body path.)"""
    seen: list[str] = []
    oversized_len = _MAX_TORRENT_BYTES + 1

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            # Tiny body: the declared-size veto must fire on the HEADER alone,
            # before a single body byte is consumed.
            return httpx.Response(
                200,
                content=b"d",
                headers={"Content-Length": str(oversized_len)},
            )
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_oversized_streamed_body_raises_source_error() -> None:
    """A source with NO Content-Length whose streamed total crosses the cap is cut
    off mid-stream with the same SourceError subtype — the authoritative cap the
    declared-size check is only an early-abort optimization for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            # Bencode-dict lead byte + one byte past the cap; no Content-Length
            # header, so only the streamed-total check can veto it.
            response = httpx.Response(200, content=b"d" + b"x" * _MAX_TORRENT_BYTES)
            response.headers.pop("Content-Length", None)
            return response
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_loopback_http_url_is_rejected_before_fetch() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add("http://127.0.0.1/file.torrent", "/downloads", "plex-manager")

    assert seen == []


async def test_add_cgnat_http_url_is_rejected_before_fetch() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add("http://100.64.0.1/file.torrent", "/downloads", "plex-manager")

    assert seen == []


async def test_add_nat64_loopback_http_url_is_rejected_before_fetch() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(
            "http://[64:ff9b::7f00:1]/file.torrent", "/downloads", "plex-manager"
        )

    assert seen == []


async def test_add_http_url_with_invalid_port_is_rejected_before_fetch() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(
            "http://93.184.216.34:99999/file.torrent", "/downloads", "plex-manager"
        )

    assert seen == []


async def test_add_unresolvable_http_host_is_rejected_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    url = "http://unresolvable.invalid/file.torrent"

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror("no address")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == url:
            return httpx.Response(200, content=_TORRENT_BYTES)
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(url, "/downloads", "plex-manager")

    assert seen == []


# --------------------------------------------------------------------------- #
# Trusted source origin — the operator-configured Prowlarr endpoint
# --------------------------------------------------------------------------- #
PROWLARR_ORIGIN = "http://127.0.0.1:9696"
PROWLARR_DOWNLOAD_URL = f"{PROWLARR_ORIGIN}/1/download?apikey=x&file=y.torrent"


async def test_add_torrent_from_configured_prowlarr_private_origin_is_allowed() -> None:
    """Prowlarr routinely serves magnetless .torrent downloadUrls pointing at
    ITSELF, and self-hosted Prowlarr lives on 127.0.0.1 / RFC1918 / a compose
    alias — exactly what the SSRF veto rejects. The operator-configured Prowlarr
    origin (the app already trusts that URL with an API key for every search
    call) must be fetchable, or every magnetless private-tracker release is
    ungrabbable, manually and via auto-grab."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == PROWLARR_DOWNLOAD_URL:
            return httpx.Response(200, content=_TORRENT_BYTES)
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    client = _client(handler, trusted_source_origin=PROWLARR_ORIGIN)
    info_hash = (await client.add(PROWLARR_DOWNLOAD_URL, "/downloads", "plex-manager")).torrent_hash

    assert info_hash == _TORRENT_HASH
    assert any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_other_private_hosts_stay_vetoed_despite_trusted_prowlarr() -> None:
    """The allowance is for EXACTLY the configured origin — any other private
    address keeps the full SSRF veto (never a blanket private-range opening)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    client = _client(handler, trusted_source_origin=PROWLARR_ORIGIN)
    with pytest.raises(QbittorrentSourceError):
        await client.add("http://192.168.1.50/file.torrent", "/downloads", "plex-manager")

    assert seen == []  # vetoed before any fetch

    # Same HOST as Prowlarr but a different port is a different origin: vetoed.
    with pytest.raises(QbittorrentSourceError):
        await client.add("http://127.0.0.1:9999/file.torrent", "/downloads", "plex-manager")

    assert seen == []


async def test_redirect_off_trusted_prowlarr_to_private_host_is_vetoed() -> None:
    """The trust is PER HOP: a redirect from the configured Prowlarr origin to a
    private third party re-enters the normal veto — Prowlarr being trusted must
    not let it (or a hostile indexer definition behind it) steer the fetcher
    into the internal network."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == PROWLARR_DOWNLOAD_URL:
            return httpx.Response(302, headers={"Location": "http://10.0.0.7/file.torrent"})
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    client = _client(handler, trusted_source_origin=PROWLARR_ORIGIN)
    with pytest.raises(QbittorrentSourceError):
        await client.add(PROWLARR_DOWNLOAD_URL, "/downloads", "plex-manager")

    # The trusted hop was fetched; the private redirect target never was, and
    # nothing reached /torrents/add.
    assert seen == [PROWLARR_DOWNLOAD_URL]


async def test_add_malformed_redirect_location_raises_source_error() -> None:
    """A redirect hop with a MALFORMED Location (`http://[::1`) is attacker-
    suppliable indexer output and must surface as the SourceError subtype
    (422 / per-release auto-grab fall-through), never a raw error that 500s the
    grab and aborts the whole auto-grab cycle. Two layers can refuse it: httpx
    pre-parses Location (RemoteProtocolError -> a RequestError, already mapped
    to SourceError), and for any Location httpx tolerates but the stdlib
    refuses, the adapter's own ``urljoin`` guard converts the ValueError. This
    test locks the end-to-end taxonomy whichever layer fires."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(302, headers={"Location": "http://[::1"})
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_malformed_source_url_raises_source_error() -> None:
    """An indexer-supplied source URL urlparse itself refuses (bad IPv6 literal)
    is a release problem: SourceError, never a raw ValueError."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add("http://[::1", "/downloads", "plex-manager")

    assert seen == []  # vetoed before any fetch or client call


async def test_malformed_trusted_origin_degrades_to_closed_veto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed STORED Prowlarr URL must not crash the client constructor
    (pre-fix: ValueError -> a 500 on every qbt-dependent route, even pure DB
    paths like mark-failed?remove_torrent=false). The CLIENT is healthy — only
    the trust anchor is bad — so it degrades to NO trusted origin: the SSRF
    veto stays fully closed, a visible warning is logged, and normal client
    operations keep working."""
    with caplog.at_level("WARNING"):
        client = _client(trusted_source_origin="http://[::1")  # does not raise

    assert any("not a usable trusted source origin" in record.message for record in caplog.records)

    # Client healthy: a normal magnet add works.
    info_hash = (await client.add(MAGNET, "/downloads/movies", "plex-manager")).torrent_hash
    assert info_hash == MAGNET_HASH

    # Veto fully closed: private source URLs are still rejected (no accidental
    # trust from the unusable origin).
    with pytest.raises(QbittorrentSourceError):
        await client.add("http://127.0.0.1:9696/file.torrent", "/downloads", "plex-manager")


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = port, timeout, local_address, socket_options
        self.hosts.append(host)
        raise httpcore.ConnectError("stop after target selection")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        _ = seconds


class _DummyNetworkStream(httpcore.AsyncNetworkStream):
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        _ = max_bytes, timeout
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        _ = buffer, timeout

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = ssl_context, server_hostname, timeout
        return self

    def get_extra_info(self, info: str) -> Any:
        _ = info
        return None


class _FailFirstBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = port, timeout, local_address, socket_options
        self.hosts.append(host)
        if len(self.hosts) == 1:
            raise httpcore.ConnectError("first address failed")
        return _DummyNetworkStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        _ = seconds


async def test_safe_fetch_backend_pins_the_vetted_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    delegate = _RecordingBackend()
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(httpcore.ConnectError):
        await SafeFetchNetworkBackend(delegate).connect_tcp("indexer.example", 80)

    assert delegate.hosts == ["93.184.216.34"]
    assert calls == 1


async def test_safe_fetch_backend_allows_private_answer_for_trusted_host_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection-time backend mirrors the per-hop URL allowance: the
    operator-configured Prowlarr host+port may resolve to a private address
    (compose alias / LAN name), while the SAME private answer for any other
    host stays a refused connect."""

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 9696))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    trusted = SafeFetchNetworkBackend(_AcceptingBackend(), ("prowlarr.local", 9696))
    stream = await trusted.connect_tcp("prowlarr.local", 9696)
    assert isinstance(stream, _DummyNetworkStream)

    # Same private DNS answer, untrusted host: refused before any connect.
    delegate = _RecordingBackend()
    untrusted = SafeFetchNetworkBackend(delegate, ("prowlarr.local", 9696))
    with pytest.raises(httpcore.ConnectError):
        await untrusted.connect_tcp("indexer.example", 9696)
    assert delegate.hosts == []

    # Trusted host on a DIFFERENT port is a different endpoint: refused too.
    with pytest.raises(httpcore.ConnectError):
        await untrusted.connect_tcp("prowlarr.local", 9999)
    assert delegate.hosts == []


class _AcceptingBackend(httpcore.AsyncNetworkBackend):
    """A delegate that accepts every connect."""

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = host, port, timeout, local_address, socket_options
        return _DummyNetworkStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        _ = seconds


async def test_safe_fetch_backend_tries_next_vetted_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]

    delegate = _FailFirstBackend()
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    stream = await SafeFetchNetworkBackend(delegate).connect_tcp("indexer.example", 80)

    assert isinstance(stream, _DummyNetworkStream)
    assert delegate.hosts == ["93.184.216.34", "8.8.8.8"]


class _TimeoutFirstBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = port, timeout, local_address, socket_options
        self.hosts.append(host)
        if len(self.hosts) == 1:
            # ConnectTimeout is a TimeoutException, NOT a ConnectError subclass.
            raise httpcore.ConnectTimeout("first address timed out")
        return _DummyNetworkStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        _ = path, timeout, socket_options
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        _ = seconds


async def test_safe_fetch_backend_tries_next_address_on_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken-IPv6 dual-stack host raises ConnectTimeout on its first resolved
    address; the retry loop must still try the remaining vetted address rather than
    abandon the working IPv4 fallback (ConnectTimeout is not a ConnectError)."""

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]

    delegate = _TimeoutFirstBackend()
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    stream = await SafeFetchNetworkBackend(delegate).connect_tcp("indexer.example", 80)

    assert isinstance(stream, _DummyNetworkStream)
    assert delegate.hosts == ["93.184.216.34", "8.8.8.8"]


async def test_add_torrent_file_without_info_dict_is_rejected_before_client_add() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(200, content=b"d3:foo3:bare")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    # A bencoded dict with no info key is unhashable -> a source problem (422),
    # not a client outage: the distinct QbittorrentSourceError, never 502.
    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


@pytest.mark.parametrize(
    "body",
    [
        b"d4:infoi42ee",  # info -> integer
        b"d4:info4:abcde",  # info -> string
        b"d4:infoli1eee",  # info -> list
    ],
)
async def test_add_torrent_with_non_dict_info_value_is_rejected_before_client_add(
    body: bytes,
) -> None:
    """An ``info`` key whose VALUE is not a bencoded dict is not a torrent: pre-fix
    the local hasher hashed the arbitrary value anyway, fabricating an "info-hash"
    and letting the invalid body reach /torrents/add (a client-failure surface — or
    a download tracked under a hash qBittorrent never reports). It must instead be
    unhashable -> the per-release SourceError, with nothing handed to the client."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(200, content=body)
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


def _control_handler(seen: list[str], *, webapi_version: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/app/webapiVersion":
            return httpx.Response(200, text=webapi_version)
        seen.append(request.url.path)
        return httpx.Response(200, text="")

    return handler


async def test_pause_resume_use_stop_start_on_qbit5() -> None:
    # qBit 5 (WebAPI 2.11.0) renamed pause/resume -> stop/start; the old paths 404.
    seen: list[str] = []
    client = _client(_control_handler(seen, webapi_version="2.11.0"))
    await client.pause(MAGNET_HASH)
    await client.resume(MAGNET_HASH)
    await client.set_category(MAGNET_HASH, "plex-manager-imported")
    assert "/api/v2/torrents/stop" in seen
    assert "/api/v2/torrents/start" in seen
    assert "/api/v2/torrents/pause" not in seen
    assert "/api/v2/torrents/resume" not in seen
    assert "/api/v2/torrents/setCategory" in seen


async def test_pause_resume_use_legacy_endpoints_on_qbit4() -> None:
    # On a pre-5.0 server (WebAPI 2.8.3) the legacy pause/resume paths are correct.
    seen: list[str] = []
    client = _client(_control_handler(seen, webapi_version="2.8.3"))
    await client.pause(MAGNET_HASH)
    await client.resume(MAGNET_HASH)
    assert "/api/v2/torrents/pause" in seen
    assert "/api/v2/torrents/resume" in seen
    assert "/api/v2/torrents/stop" not in seen
    assert "/api/v2/torrents/start" not in seen


async def test_add_base32_btih_magnet_normalizes_to_hex() -> None:
    """A valid 32-char base32 ``btih`` magnet must resolve to the 40-char hex hash
    qBittorrent reports — otherwise the stored hash never matches the client
    snapshot and the reconciler sees ClientMissing forever."""
    raw = bytes.fromhex(MAGNET_HASH)
    b32 = base64.b32encode(raw).decode()
    assert len(b32) == 32  # base32 of 20 bytes is 32 chars
    base32_magnet = f"magnet:?xt=urn:btih:{b32}&dn=Test"

    info_hash = (
        await _client().add(base32_magnet, "/downloads/movies", "plex-manager")
    ).torrent_hash
    assert info_hash == MAGNET_HASH  # decoded to lowercase hex


async def test_add_hex_btih_magnet_is_lowercased_as_is() -> None:
    """The 40-char hex form is left as-is (lowercased), not mangled."""
    upper_magnet = f"magnet:?xt=urn:btih:{MAGNET_HASH.upper()}&dn=Test"
    info_hash = (
        await _client().add(upper_magnet, "/downloads/movies", "plex-manager")
    ).torrent_hash
    assert info_hash == MAGNET_HASH


async def test_add_invalid_base32_btih_magnet_raises_source_error() -> None:
    """Issue #90: a 32-char ``btih`` that is NOT valid base32 (``0``/``1``/``8``/``9``
    are outside the base32 alphabet) is a MALFORMED source, not a hash. It must raise
    the typed ``QbittorrentSourceError`` -- flowing into the existing source-failure
    taxonomy -- rather than passing the garbage through as a bogus 'hash' that could
    never match the client snapshot (ClientMissing forever). No secret leak either."""
    bad_b32 = "0" * 32  # 32 chars, but '0' is not a base32 digit -> binascii.Error
    bad_magnet = f"magnet:?xt=urn:btih:{bad_b32}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_non_ascii_btih_magnet_raises_source_error() -> None:
    """A 32-char ``btih`` containing NON-ASCII text (e.g. percent-decoded invalid
    bytes from an indexer magnet) makes ``b32decode`` raise a PLAIN ``ValueError``
    ("string argument should contain only ASCII characters"), not
    ``binascii.Error`` — it must land in the same typed taxonomy, never escape as
    an unhandled 500 / out-of-taxonomy failure."""
    bad_b32 = "é" * 32  # non-ASCII: plain ValueError before the alphabet check
    bad_magnet = f"magnet:?xt=urn:btih:{bad_b32}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_short_decoding_padded_btih_magnet_raises_source_error() -> None:
    """Padding tricks ("A"*31 + "=") b32decode "successfully" — to 19 bytes. The
    resulting 38-char "hex hash" could never match qBittorrent's 40-char snapshot
    (ClientMissing forever), so a decode that is not exactly 20 bytes is the same
    malformed source and must raise the typed error, not return a bogus hash."""
    padded_b32 = "A" * 31 + "="  # decodes to 19 bytes, not a 20-byte info-hash
    bad_magnet = f"magnet:?xt=urn:btih:{padded_b32}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_mixed_case_hex_btih_magnet_normalizes_to_lowercase() -> None:
    """A 40-char hex ``btih`` with mixed upper/lower case is a valid hex value --
    it must still normalize to the fully-lowercased form qBittorrent reports."""
    mixed = "1234567890ABCDEF1234567890abcdef12345678"
    assert mixed.lower() == MAGNET_HASH
    magnet = f"magnet:?xt=urn:btih:{mixed}&dn=Test"
    result = await _client().add(magnet, "/downloads/movies", "plex-manager")
    assert result.torrent_hash == MAGNET_HASH


async def test_add_39_char_btih_magnet_raises_source_error() -> None:
    """Issue #212: a wrong-length value (39 chars -- one short of 40-char hex)
    is a MALFORMED source, not a hash. Before this fix it was silently
    lowercased and passed through as if it were real, stranding the download
    as ClientMissing once qBittorrent reports a different (real) hash."""
    too_short = MAGNET_HASH[:-1]
    bad_magnet = f"magnet:?xt=urn:btih:{too_short}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_41_char_btih_magnet_raises_source_error() -> None:
    """Issue #212: a wrong-length value (41 chars -- one past 40-char hex) must
    also raise the typed source error rather than being silently accepted."""
    too_long = MAGNET_HASH + "0"
    bad_magnet = f"magnet:?xt=urn:btih:{too_long}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_40_char_non_hex_btih_magnet_raises_source_error() -> None:
    """Issue #212: a 40-char value that is NOT hex (``g``-``z`` are outside the
    hex alphabet) was previously accepted unchecked -- it must now raise the
    typed source error, the same as any other malformed btih."""
    non_hex = "g" * 40
    bad_magnet = f"magnet:?xt=urn:btih:{non_hex}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(bad_magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_magnet_with_duplicate_identical_btih_is_accepted() -> None:
    """Issue #212: two ``xt=urn:btih:`` values that normalize to the SAME
    hash (here: the hex form and the base32 encoding of the identical 20
    bytes, in differing case) are not a conflict -- accept and use the one
    normalized hash, exactly as if only one value had been present."""
    raw = bytes.fromhex(MAGNET_HASH)
    b32 = base64.b32encode(raw).decode()
    dup_magnet = f"magnet:?xt=urn:btih:{MAGNET_HASH.upper()}&xt=urn:btih:{b32}&dn=Test"
    result = await _client().add(dup_magnet, "/downloads/movies", "plex-manager")
    assert result.torrent_hash == MAGNET_HASH


async def test_add_magnet_with_conflicting_btih_values_is_rejected_before_client_add() -> None:
    """Issue #212: a magnet naming two DISTINCT valid BTIH values is ambiguous.
    libtorrent (what qBittorrent actually uses) overwrites the v1 hash as it
    parses ``xt`` parameters, so it ends up tracking the LAST valid value --
    while naively picking the first (the prior behaviour here) can persist a
    DIFFERENT hash than the one qBittorrent will actually track. That false
    mismatch later makes the reconciler decide ClientMissing, rearm the
    request, and attempt to remove the wrong hash, orphaning a live torrent.
    Reject outright -- and never even reach ``/torrents/add`` -- rather than
    guess which of the two the client will pick."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return _router()(request)

    other_hash = "fedcba9876543210fedcba9876543210fedcba98"
    conflicting_magnet = f"magnet:?xt=urn:btih:{MAGNET_HASH}&xt=urn:btih:{other_hash}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client(handler).add(conflicting_magnet, "/downloads/movies", "plex-manager")
    assert "/api/v2/torrents/add" not in seen_paths
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_add_magnet_with_one_invalid_one_valid_btih_is_rejected() -> None:
    """Issue #212: one malformed ``btih`` alongside one otherwise-valid value is
    still an untrustworthy source -- reject the whole magnet rather than
    silently using the valid value and ignoring the malformed one. Any
    conflicting/invalid signal among multiple ``xt`` values is grounds for
    rejection, not just a length/content mismatch on a single value."""
    bad_b32 = "0" * 32  # not valid base32
    magnet = f"magnet:?xt=urn:btih:{bad_b32}&xt=urn:btih:{MAGNET_HASH}&dn=Test"
    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client().add(magnet, "/downloads/movies", "plex-manager")
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_transport_outage_raises_qbittorrent_error() -> None:
    """qBittorrent unreachable (connection error) surfaces a wrapped, retryable
    QbittorrentError — never an opaque httpx error -> 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(QbittorrentError) as exc_info:
        await _client(handler).get_all_statuses()
    # No url / secret leak in the surfaced message.
    assert BASE_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_info_non_json_200_raises_qbittorrent_error() -> None:
    """A 200 on /torrents/info whose body is NOT JSON (a reverse-proxy / auth HTML
    page in front of the WebUI) would make response.json() raise a raw
    JSONDecodeError -> opaque 500. It must be wrapped as a retryable
    QbittorrentError carrying no url or secret."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, text="<html>login</html>")
        return httpx.Response(404, text="unhandled")

    with pytest.raises(QbittorrentError) as exc_info:
        await _client(handler).get_all_statuses()
    message = str(exc_info.value)
    assert BASE_URL not in message
    assert PASSWORD not in message


async def test_add_unreachable_http_source_raises_source_error() -> None:
    """A release exposing only a download_url whose indexer/Prowlarr URL is
    unreachable is a SOURCE problem (qBittorrent was never contacted): the
    SourceError subtype — never an opaque httpx transport error -> 500, and never
    the base class whose 502 would blame a healthy client."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(404)

    with pytest.raises(QbittorrentSourceError) as exc_info:
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")
    # No url / secret leak in the surfaced message.
    assert DOWNLOAD_URL not in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)


async def test_server_5xx_raises_qbittorrent_error() -> None:
    """A 5xx from qBittorrent is wrapped as QbittorrentError (not an opaque 500)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        return httpx.Response(503, text="Service Unavailable")

    with pytest.raises(QbittorrentError):
        await _client(handler).get_all_statuses()


async def test_get_default_save_path_reads_app_preferences() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/app/preferences" and request.method == "GET":
            return httpx.Response(200, json={"save_path": "/home/lunchbox/Downloads"})
        return httpx.Response(404, text="unhandled")

    save_path = await _client(handler).get_default_save_path()
    assert save_path == "/home/lunchbox/Downloads"


async def test_get_default_save_path_missing_key_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/app/preferences" and request.method == "GET":
            return httpx.Response(200, json={})
        return httpx.Response(404, text="unhandled")

    assert await _client(handler).get_default_save_path() is None


async def test_get_default_save_path_error_status_returns_none() -> None:
    """A read-only diagnostic (issues #133/#157): a failure here must never raise
    — the caller (setup/health probe) treats it as "could not read it", not a
    hard failure of the whole qBittorrent check."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/app/preferences" and request.method == "GET":
            return httpx.Response(500, text="boom")
        return httpx.Response(404, text="unhandled")

    assert await _client(handler).get_default_save_path() is None


async def test_set_location_posts_hash_and_location() -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/setLocation" and request.method == "POST":
            body = dict(httpx.QueryParams(request.content.decode()))
            calls.append(body)
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404, text="unhandled")

    await _client(handler).set_location(MAGNET_HASH.upper(), "/home/lunchbox/Downloads")
    assert calls == [{"hashes": MAGNET_HASH, "location": "/home/lunchbox/Downloads"}]


async def test_set_location_error_status_raises_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/setLocation" and request.method == "POST":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(404, text="unhandled")

    with pytest.raises(QbittorrentError):
        await _client(handler).set_location(MAGNET_HASH, "/home/lunchbox/Downloads")


async def test_set_location_drops_cached_properties_entry() -> None:
    """After a relocate, the next ``get_save_path`` must re-read the client rather
    than serve the pre-move path from the short-lived properties cache."""

    paths = iter(["/downloads/movies", "/home/lunchbox/Downloads"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if request.url.path == "/api/v2/torrents/setLocation" and request.method == "POST":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/properties" and request.method == "GET":
            return httpx.Response(200, json={"save_path": next(paths)})
        return httpx.Response(404, text="unhandled")

    client = _client(handler)
    assert await client.get_save_path(MAGNET_HASH) == "/downloads/movies"
    await client.set_location(MAGNET_HASH, "/home/lunchbox/Downloads")
    assert await client.get_save_path(MAGNET_HASH) == "/home/lunchbox/Downloads"


def test_adapter_satisfies_download_client_port() -> None:
    from plex_manager.ports.download_client import DownloadClientPort

    assert isinstance(_client(), DownloadClientPort)


@pytest.mark.skipif(
    not os.getenv("PLEX_MANAGER_LIVE_TESTS"),
    reason="live qBittorrent smoke test; set PLEX_MANAGER_LIVE_TESTS + QBITTORRENT_*",
)
async def test_live_smoke_statuses() -> None:  # pragma: no cover - live only
    base_url = os.environ.get("QBITTORRENT_URL")
    username = os.environ.get("QBITTORRENT_USERNAME")
    password = os.environ.get("QBITTORRENT_PASSWORD")
    if not base_url or not username or not password:
        pytest.skip("QBITTORRENT_URL / USERNAME / PASSWORD not set")
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = QbittorrentClient(http, base_url, username, password)
        statuses = await client.get_all_statuses()
        assert isinstance(statuses, list)
