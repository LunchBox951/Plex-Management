"""Startup safety: the tokenless first-run exposure guard, on every launch path."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from plex_manager.__main__ import validate_startup_exposure
from plex_manager.config import Settings, get_settings
from plex_manager.web.app import create_app


def test_default_host_is_loopback() -> None:
    assert Settings().host == "127.0.0.1"


def test_loopback_bind_without_setup_token_is_rejected() -> None:
    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        validate_startup_exposure(Settings())


def test_public_bind_without_setup_token_is_rejected() -> None:
    settings = Settings(host="0.0.0.0")  # noqa: S104 - deliberate unsafe bind under test

    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        validate_startup_exposure(settings)


def test_public_bind_with_setup_token_is_allowed() -> None:
    settings = Settings(
        host="0.0.0.0",  # noqa: S104 - deliberate public bind covered by token
        setup_token=SecretStr("boot-token"),
    )

    validate_startup_exposure(settings)


def test_dev_auth_bypass_without_setup_token_is_allowed() -> None:
    validate_startup_exposure(Settings(dev_auth_bypass=True))


async def test_asgi_direct_lifespan_refuses_tokenless_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving ``plex_manager.web.app:app`` directly (uvicorn CLI / an ASGI
    platform) bypasses the ``__main__`` guard; pre-fix, a tokenless bypass-off
    server then came up in a setup DEADLOCK — /setup/status honestly advertised
    "no token required" while the pre-init gate 401'd every validate/complete
    call. The lifespan must apply the SAME startup guard so the divergent state
    is unservable on every launch path (never reaching persistence/task setup)."""
    # Real env vars beat any developer .env file; empty token == unset.
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "")
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
        with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
            async with app.router.lifespan_context(app):  # pragma: no cover - never yields
                pass
    finally:
        get_settings.cache_clear()
