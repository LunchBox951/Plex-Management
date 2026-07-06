"""Startup: a fresh install boots tokenless — no first-run exposure guard.

The old tokenless-first-run startup refusal is gone (ADR-0016): first-run setup is
claimed by the first Plex server owner to sign in, never gated on an env token.
These tests pin that BOTH an uninitialized and an initialized install boot through
the REAL ASGI lifespan with zero auth env vars set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plex_manager.db as db_module
from plex_manager.adapters import encryption
from plex_manager.config import Settings, get_settings
from plex_manager.models import SystemSettings
from plex_manager.web.app import create_app


def test_default_host_is_loopback() -> None:
    assert Settings().host == "127.0.0.1"


@pytest.fixture
async def tokenless_lifespan_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Path]:
    """A scrubbed, self-contained environment for driving the REAL lifespan.

    No setup token and dev-bypass off (real env vars beat any developer env file),
    a throwaway Fernet key (an initialized install's ``prepare_encryption`` needs
    one), and a tmp-file database so the lifespan's engine can never touch
    ``./data``. The module-level engine/sessionmaker singletons and the settings
    cache are reset around the test so its URL is actually used and nothing leaks
    into other tests.
    """
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "")
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "false")
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "startup.db"
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_sessionmaker", None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)
    await engine.dispose()
    try:
        yield db_path
    finally:
        # Dispose the engine the lifespan lazily created against the tmp DB.
        if db_module._engine is not None:  # pyright: ignore[reportPrivateUsage]
            await db_module._engine.dispose()  # pyright: ignore[reportPrivateUsage]
        get_settings.cache_clear()
        encryption.reset_fernet_cache()


async def test_asgi_lifespan_boots_uninitialized_install_tokenless(
    tokenless_lifespan_env: Path,
) -> None:
    """AC1: a FRESH install (uninitialized DB, no setup token) boots — the old
    tokenless-first-run refusal is gone. Sign-in via ``/api/v1/auth`` is the first
    reachable setup step (see ``tests/web/test_middleware_allowlist.py``), never a
    startup deadlock."""
    app = create_app()
    async with app.router.lifespan_context(app):
        pass  # started (and shut down) cleanly — no SystemExit


async def test_asgi_lifespan_boots_initialized_install_tokenless(
    tokenless_lifespan_env: Path,
) -> None:
    """An INITIALIZED install restarts/upgrades WITHOUT the setup token: every
    post-init route is API-key gated and the token is never consulted again."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tokenless_lifespan_env}")
    async with async_sessionmaker(engine)() as session:
        session.add(SystemSettings(initialized=True))
        await session.commit()
    await engine.dispose()

    app = create_app()
    async with app.router.lifespan_context(app):
        pass  # started (and shut down) cleanly — no SystemExit
