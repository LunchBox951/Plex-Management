"""Regression tests for configured-service SSRF boundaries (CodeQL 281-285)."""

from __future__ import annotations

import httpx
import pytest

from plex_manager.adapters.plex.library import PlexLibrary, PlexLibraryError
from plex_manager.adapters.plex.oauth import PlexTvClient, PlexVerifyError
from plex_manager.adapters.prowlarr.adapter import IndexerError, ProwlarrIndexer
from plex_manager.adapters.qbittorrent.adapter import QbittorrentClient, QbittorrentError
from plex_manager.adapters.service_url import (
    InvalidServiceUrl,
    ServiceUrl,
    same_service_base,
)
from plex_manager.domain.release import IndexerSearchRequest


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        ("http://127.0.0.1:32400", "/identity", "http://127.0.0.1:32400/identity"),
        ("http://plex:32400/", "/library/sections", "http://plex:32400/library/sections"),
        (
            "https://media.example.test/plex",
            "/library/sections",
            "https://media.example.test/plex/library/sections",
        ),
        (
            "http://[::1]:8080/qbt",
            "/api/v2/auth/login",
            "http://[::1]:8080/qbt/api/v2/auth/login",
        ),
    ],
)
def test_endpoint_stays_under_configured_private_or_proxy_base(
    base: str, path: str, expected: str
) -> None:
    assert str(ServiceUrl.parse(base).endpoint(path)) == expected


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "http://user:password@plex.local:32400",
        "http://plex.local\\@169.254.169.254/latest/meta-data",
        "http://plex.local/base?next=http://evil.test",
        "http://plex.local/base#fragment",
        "http://plex.local/a/../admin",
        "http://plex.local/a/%2e%2e/admin",
        "http://plex.local/a//admin",
        "http://plex.local/%2f%2fevil.test",
        "http://plex.local:0",
        "http://plex.local:65536",
    ],
)
def test_parse_rejects_ambiguous_or_prefix_escaping_base_urls(value: str) -> None:
    with pytest.raises(InvalidServiceUrl):
        ServiceUrl.parse(value)


@pytest.mark.parametrize(
    "path",
    [
        "identity",
        "//evil.test/identity",
        "/../identity",
        "/library/sections?X-Plex-Token=leak",
        "/library/%2e%2e/identity",
        "https://evil.test/identity",
    ],
)
def test_endpoint_rejects_anything_other_than_a_server_owned_relative_path(path: str) -> None:
    with pytest.raises(InvalidServiceUrl):
        ServiceUrl.parse("http://plex.local:32400").endpoint(path)


def test_same_base_normalizes_case_default_port_and_trailing_slash() -> None:
    assert same_service_base("HTTP://PLEX.local:80/one/", "http://plex.local/one")
    assert not same_service_base("http://plex.local/one", "http://plex.local/two")
    assert not same_service_base("http://plex.local", "https://plex.local")
    assert not same_service_base("http://plex.local", "http://plex.local:32400")
    assert not same_service_base("not a url", "http://plex.local")


async def test_invalid_direct_adapter_bases_fail_before_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("an invalid service base must never reach transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QbittorrentError, match="service URL is invalid"):
            QbittorrentClient(client, "http://safe/../admin", "user", "password")
        with pytest.raises(PlexLibraryError, match="service URL is invalid"):
            PlexLibrary(client, "http://user:pass@plex.local", "secret-token")
        plex_tv = PlexTvClient(client, client_identifier="test-client")
        with pytest.raises(PlexVerifyError) as caught:
            await plex_tv.fetch_server_identity("http://safe/%2fmetadata", "secret-token")

    assert caught.value.code == "server_unreachable_from_backend"
    assert "secret-token" not in str(caught.value)
    assert calls == []


async def test_credentialed_requests_refuse_redirects_even_when_client_follows_them() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "evil.test":
            raise AssertionError("credentialed redirect must not be followed")
        return httpx.Response(307, headers={"Location": "http://evil.test/collect"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        qbt = QbittorrentClient(client, "http://qb.local:8080", "user", "password")
        with pytest.raises(QbittorrentError, match=r"login failed \(HTTP 307\)"):
            await qbt.get_all_statuses()

        plex = PlexLibrary(client, "http://plex-one.local:32400", "plex-token")
        with pytest.raises(PlexLibraryError, match=r"HTTP 307"):
            await plex.list_sections(use_cache=False)

        plex_tv = PlexTvClient(client, client_identifier="test-client")
        with pytest.raises(PlexVerifyError) as caught:
            await plex_tv.fetch_server_identity("http://plex-two.local:32400", "plex-token")

        prowlarr = ProwlarrIndexer(client, "http://prowlarr.local:9696", "api-key")
        with pytest.raises(IndexerError, match=r"HTTP 307"):
            await prowlarr.search(IndexerSearchRequest(query="inception"))

    assert caught.value.code == "server_identity_failed"
    assert [request.url.host for request in calls] == [
        "qb.local",
        "plex-one.local",
        "plex-two.local",
        "prowlarr.local",
        "prowlarr.local",
    ]


@pytest.mark.parametrize("retry_after_403", [False, True])
async def test_qbittorrent_authenticated_requests_and_retry_refuse_redirects(
    retry_after_403: bool,
) -> None:
    calls: list[httpx.Request] = []
    info_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_calls
        calls.append(request)
        if request.url.host == "evil.test":
            raise AssertionError("authenticated redirect must not be followed")
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(
                200,
                text="Ok.",
                headers={"Set-Cookie": "SID=test-session; Path=/"},
            )
        if request.url.path == "/api/v2/torrents/info":
            info_calls += 1
            if retry_after_403 and info_calls == 1:
                return httpx.Response(403)
            return httpx.Response(307, headers={"Location": "http://evil.test/collect"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        qbt = QbittorrentClient(client, "http://qb.local:8080", "user", "password")
        with pytest.raises(QbittorrentError, match=r"HTTP 307"):
            await qbt.get_all_statuses()

    expected_paths = ["/api/v2/auth/login", "/api/v2/torrents/info"]
    if retry_after_403:
        expected_paths.extend(["/api/v2/auth/login", "/api/v2/torrents/info"])
    assert [request.url.path for request in calls] == expected_paths


async def test_qbittorrent_sid_is_adapter_local_and_never_crosses_ports() -> None:
    calls: list[tuple[int | None, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.port, request.url.path, request.headers.get("Cookie")))
        if request.url.path == "/api/v2/auth/login":
            sid = "first-session" if request.url.port == 8080 else "second-session"
            return httpx.Response(200, text="Ok.", headers={"Set-Cookie": f"SID={sid}; Path=/"})
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = QbittorrentClient(client, "http://qb.local:8080", "user", "password")
        second = QbittorrentClient(client, "http://qb.local:9090", "user", "password")
        await first.get_all_statuses()
        await second.get_all_statuses()
        assert list(client.cookies.jar) == []

    assert calls == [
        (8080, "/api/v2/auth/login", ""),
        (8080, "/api/v2/torrents/info", "SID=first-session"),
        (9090, "/api/v2/auth/login", ""),
        (9090, "/api/v2/torrents/info", "SID=second-session"),
    ]
