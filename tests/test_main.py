"""Startup safety: the tokenless FIRST-RUN exposure guard, on every launch path."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plex_manager.db as db_module
from plex_manager.adapters import encryption
from plex_manager.config import Settings, get_settings, validate_startup_exposure
from plex_manager.db import Base
from plex_manager.models import SystemSettings
from plex_manager.web.app import create_app


def test_default_host_is_loopback() -> None:
    assert Settings().host == "127.0.0.1"


def test_loopback_bind_without_setup_token_is_rejected() -> None:
    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        validate_startup_exposure(Settings(), initialized=False)


def test_public_bind_without_setup_token_is_rejected() -> None:
    settings = Settings(host="0.0.0.0")  # noqa: S104 - deliberate unsafe bind under test

    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        validate_startup_exposure(settings, initialized=False)


def test_public_bind_with_setup_token_is_allowed() -> None:
    settings = Settings(
        host="0.0.0.0",  # noqa: S104 - deliberate public bind covered by token
        setup_token=SecretStr("boot-token"),
    )

    validate_startup_exposure(settings, initialized=False)


def test_dev_auth_bypass_without_setup_token_is_allowed() -> None:
    validate_startup_exposure(Settings(dev_auth_bypass=True), initialized=False)


def test_initialized_install_is_exempt_from_the_token_requirement() -> None:
    # Post-init every route is API-key gated and the setup token is never
    # consulted again — an initialized install must restart/upgrade tokenless.
    validate_startup_exposure(Settings(), initialized=True)


@pytest.fixture
async def tokenless_lifespan_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Path]:
    """A scrubbed, self-contained environment for driving the REAL lifespan.

    Empty token + bypass off (real env vars beat any developer env file), a
    throwaway Fernet key (an initialized install's ``prepare_encryption`` needs
    one), and a tmp-file database so the lifespan's engine can never touch
    ``./data``. The module-level engine/sessionmaker singletons and the settings
    cache are reset around the test so its URL is actually used and nothing
    leaks into other tests.
    """
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "")
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "startup-guard.db"
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_sessionmaker", None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    try:
        yield db_path
    finally:
        # Dispose the engine the lifespan lazily created against the tmp DB.
        if db_module._engine is not None:  # pyright: ignore[reportPrivateUsage]
            await db_module._engine.dispose()  # pyright: ignore[reportPrivateUsage]
        get_settings.cache_clear()
        encryption.reset_fernet_cache()


async def test_asgi_direct_lifespan_refuses_tokenless_first_run(
    tokenless_lifespan_env: Path,
) -> None:
    """An UNINITIALIZED install served tokenless (any launch path — they all run
    this lifespan) must refuse to serve: pre-guard it came up in a setup
    DEADLOCK, /setup/status honestly advertising "no token required" while the
    pre-init gate 401'd every validate/complete call."""
    app = create_app()
    with pytest.raises(SystemExit, match="PLEX_MANAGER_SETUP_TOKEN"):
        async with app.router.lifespan_context(app):  # pragma: no cover - never yields
            pass


async def test_asgi_direct_lifespan_boots_initialized_install_tokenless(
    tokenless_lifespan_env: Path,
) -> None:
    """An INITIALIZED install must restart/upgrade WITHOUT the setup token: every
    post-init route is API-key gated and the token is never consulted again.
    Pre-fix the config-only guard ran before the DB was readable and SystemExit
    aborted an ordinary restart of a healthy install."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tokenless_lifespan_env}")
    async with async_sessionmaker(engine)() as session:
        session.add(SystemSettings(initialized=True))
        await session.commit()
    await engine.dispose()

    app = create_app()
    async with app.router.lifespan_context(app):
        pass  # started (and shut down) cleanly — no SystemExit
