"""QbittorrentClient adapter tests — recorded ``/api/v2`` shapes via MockTransport.

Covers: cookie login (``SID`` captured), magnet add (hash derived from the magnet
``xt``), a 409-already-present add (treated as success), ``/torrents/info`` with
several qBit-5 states including ``stoppedUP`` and ``metaDL`` (``raw_state`` kept
verbatim), and remove. The password / SID are never asserted into logs. An
OPTIONAL live smoke test is env-guarded.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

import httpx
import pytest

from plex_manager.adapters.qbittorrent import (
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
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


def _client(handler: Any | None = None) -> QbittorrentClient:
    transport = httpx.MockTransport(handler or _router())
    http = httpx.AsyncClient(transport=transport)
    return QbittorrentClient(http, BASE_URL, USERNAME, PASSWORD)


async def test_add_magnet_returns_derived_hash() -> None:
    client = _client()
    info_hash = await client.add(MAGNET, "/downloads/movies", "plex-manager")
    assert info_hash == MAGNET_HASH


async def test_add_409_already_present_is_success() -> None:
    client = _client(_router(add_status=409))
    info_hash = await client.add(MAGNET, "/downloads/movies", "plex-manager")
    assert info_hash == MAGNET_HASH


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

DOWNLOAD_URL = "http://indexer.local/file.torrent"
REDIRECT_URL = "http://indexer.local/redirect"
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

    info_hash = await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")
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

    info_hash = await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")
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

    info_hash = await _client(handler).add(REDIRECT_URL, "/downloads", "plex-manager")
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

    with pytest.raises(QbittorrentError):
        await _client(handler).add(DOWNLOAD_URL, "/downloads", "plex-manager")

    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_uppercase_http_url_uses_resolver_before_client_add() -> None:
    """URI schemes are case-insensitive. Uppercase HTTP(S) must not bypass the
    local resolver/safety checks and get handed to qBittorrent as an opaque URL."""
    seen: list[str] = []
    upper_url = "HTTP://indexer.local/file.torrent"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == upper_url:
            return httpx.Response(200, content=b"not a torrent")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentError):
        await _client(handler).add(upper_url, "/downloads", "plex-manager")

    assert any(url.lower() == upper_url.lower() for url in seen)
    assert not any(url.endswith("/api/v2/torrents/add") for url in seen)


async def test_add_oversized_torrent_body_is_rejected_before_buffering_to_client() -> None:
    """A malicious indexer response must not be buffered and uploaded to qBittorrent
    without a small .torrent size cap."""
    seen: list[str] = []
    oversized_len = 2_000_001

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            return httpx.Response(
                200,
                content=b"d" + (b"x" * (oversized_len - 1)),
                headers={"Content-Length": str(oversized_len)},
            )
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    with pytest.raises(QbittorrentError):
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

    with pytest.raises(QbittorrentError):
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

    with pytest.raises(QbittorrentError):
        await _client(handler).add("http://100.64.0.1/file.torrent", "/downloads", "plex-manager")

    assert seen == []


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

    info_hash = await _client().add(base32_magnet, "/downloads/movies", "plex-manager")
    assert info_hash == MAGNET_HASH  # decoded to lowercase hex


async def test_add_hex_btih_magnet_is_lowercased_as_is() -> None:
    """The 40-char hex form is left as-is (lowercased), not mangled."""
    upper_magnet = f"magnet:?xt=urn:btih:{MAGNET_HASH.upper()}&dn=Test"
    info_hash = await _client().add(upper_magnet, "/downloads/movies", "plex-manager")
    assert info_hash == MAGNET_HASH


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


async def test_add_unreachable_http_source_raises_qbittorrent_error() -> None:
    """A release exposing only a download_url whose indexer/Prowlarr URL is
    unreachable surfaces a wrapped, retryable QbittorrentError on the grab path —
    never an opaque httpx transport error -> 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return _login_response()
        if str(request.url) == DOWNLOAD_URL:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(404)

    with pytest.raises(QbittorrentError) as exc_info:
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
