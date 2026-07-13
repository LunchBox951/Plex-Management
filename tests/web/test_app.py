from __future__ import annotations

import logging

import httpx
import pytest

from plex_manager.config import get_settings
from plex_manager.web.app import (
    _warn_if_multi_process,  # pyright: ignore[reportPrivateUsage]
    create_upstream_http_client,
)


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


def test_warn_if_multi_process_is_silent_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert caplog.text == ""
    finally:
        get_settings.cache_clear()


def test_warn_if_multi_process_is_silent_when_set_to_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert caplog.text == ""
    finally:
        get_settings.cache_clear()


def test_warn_if_multi_process_warns_loudly_above_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Issue #240: this app's in-process removal-physics/settings-rotation
    # guards silently reopen their races across more than one worker process --
    # make that violated assumption LOUD at startup instead.
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            _warn_if_multi_process()
        assert "WEB_CONCURRENCY=3" in caplog.text
        assert "single" in caplog.text.lower()
    finally:
        get_settings.cache_clear()
