"""Startup: a fresh install boots tokenless — no first-run exposure guard.

The old tokenless-first-run startup refusal is gone (ADR-0016): first-run setup is
claimed by the first Plex server owner to sign in, never gated on an env token.
These tests pin that BOTH an uninitialized and an initialized install boot through
the REAL ASGI lifespan with zero auth env vars set.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plex_manager.__main__ as main_module
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


@pytest.mark.parametrize(
    ("configured_log_level", "expected_uvicorn_level"),
    [
        ("10", logging.DEBUG),  # numeric override
        ("not-a-real-level", logging.INFO),  # typo -- must degrade, not crash
    ],
)
def test_main_normalizes_log_level_before_uvicorn_run(
    monkeypatch: pytest.MonkeyPatch,
    configured_log_level: str,
    expected_uvicorn_level: int,
) -> None:
    """Issue #100: a typo'd or numeric ``PLEX_MANAGER_LOG_LEVEL`` used to reach
    ``uvicorn.run`` as a raw ``str.lower()`` value. ``uvicorn.Config`` looks a
    string level up in its OWN name table and raises ``KeyError`` on anything
    unrecognized -- a full startup crash, before the app's tolerant lifespan
    (or ``configure_logging``) ever runs. ``main()`` must instead pass an
    already-normalized INT level (via the shared
    ``log_capture_service.resolve_log_level``), so ``uvicorn.Config`` takes the
    int branch and never performs that lookup at all.
    """
    monkeypatch.setenv("PLEX_MANAGER_LOG_LEVEL", configured_log_level)
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    try:
        main_module.main()  # must not raise
    finally:
        get_settings.cache_clear()

    assert captured["app"] == "plex_manager.web.app:app"
    assert isinstance(captured["log_level"], int)
    assert captured["log_level"] == expected_uvicorn_level
    assert captured["timeout_graceful_shutdown"] == 5


@pytest.mark.parametrize(
    ("configured_log_level", "expected_uvicorn_log_level"),
    [
        ("trace", "trace"),
        ("TRACE ", "trace"),  # case + incidental env-var whitespace
        ("  Debug", "debug"),  # an ordinary uvicorn-recognized name too
    ],
)
def test_main_passes_uvicorn_native_levels_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    configured_log_level: str,
    expected_uvicorn_log_level: str,
) -> None:
    """A regression guard for the #100 fix's own follow-on bug: ``trace`` is a
    VALID uvicorn ``--log-level`` name (one rung below ``debug``, used for
    ASGI/protocol-level tracing -- see ``uvicorn.config.LOG_LEVELS``) that
    stdlib ``logging`` has never heard of. Routing it through
    ``resolve_log_level`` (which only ever produces a stdlib int) would
    silently downgrade it to the INFO fallback, and uvicorn only installs its
    ASGI ``MessageLoggerMiddleware`` when ITS OWN effective level is <= trace
    -- so an int-downgraded 'trace' would silently disable the feature the
    setting exists for. ``main()`` must instead pass any of uvicorn's own
    recognized names straight through as a (lowercased, stripped) STRING, so
    ``uvicorn.Config`` performs its own trace-aware lookup.
    """
    monkeypatch.setenv("PLEX_MANAGER_LOG_LEVEL", configured_log_level)
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    try:
        main_module.main()  # must not raise
    finally:
        get_settings.cache_clear()

    assert captured["app"] == "plex_manager.web.app:app"
    assert captured["log_level"] == expected_uvicorn_log_level
    assert isinstance(captured["log_level"], str)


def test_main_still_falls_back_on_a_typo_that_resembles_a_real_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uvicorn-native pass-through must not widen the #100 crash gap: a
    near-miss typo (here, 'trace' misspelled) is not in uvicorn's own name
    table either, so it still degrades through ``resolve_log_level`` to the
    INFO int fallback -- never reaches ``uvicorn.Config`` as an unrecognized
    string, and never crashes.
    """
    monkeypatch.setenv("PLEX_MANAGER_LOG_LEVEL", "traec")
    get_settings.cache_clear()

    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    try:
        main_module.main()  # must not raise
    finally:
        get_settings.cache_clear()

    assert isinstance(captured["log_level"], int)
    assert captured["log_level"] == logging.INFO
