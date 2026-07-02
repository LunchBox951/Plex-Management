"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script)."""

from __future__ import annotations

import uvicorn

from plex_manager.config import Settings, get_settings

_UNSAFE_BIND_MESSAGE = (
    "Refusing to start a first-run-capable server without PLEX_MANAGER_SETUP_TOKEN. "
    "Set PLEX_MANAGER_SETUP_TOKEN or explicitly enable PLEX_MANAGER_DEV_AUTH_BYPASS."
)


def _has_setup_token(settings: Settings) -> bool:
    token = settings.setup_token
    return token is not None and bool(token.get_secret_value().strip())


def validate_startup_exposure(settings: Settings) -> None:
    """Refuse startup that would expose tokenless first-run setup."""
    if settings.dev_auth_bypass or _has_setup_token(settings):
        return
    raise SystemExit(_UNSAFE_BIND_MESSAGE)


def main() -> None:
    """Run the ASGI server using the configured host/port."""
    settings = get_settings()
    validate_startup_exposure(settings)
    uvicorn.run(
        "plex_manager.web.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
