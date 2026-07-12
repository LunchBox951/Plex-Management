from __future__ import annotations

import httpx
import pytest

from plex_manager.web.app import create_upstream_http_client


async def test_upstream_http_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")

    client = create_upstream_http_client()
    try:
        assert client._trust_env is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await client.aclose()


async def test_upstream_http_client_rejects_response_cookies() -> None:
    client = create_upstream_http_client()
    try:
        request = httpx.Request("GET", "http://service.local:8080/login")
        response = httpx.Response(
            200,
            headers={"Set-Cookie": "SID=service-secret; Path=/"},
            request=request,
        )
        client.cookies.extract_cookies(response)
        assert list(client.cookies.jar) == []
    finally:
        await client.aclose()
