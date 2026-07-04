"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script).

Deliberately NO tokenless-exposure check here: whether the setup token is
required depends on ``SystemSettings.initialized`` (DB state this synchronous
entry point cannot see), and the ASGI ``lifespan`` — which every launch path
runs, including this one via uvicorn — enforces
:func:`plex_manager.config.validate_startup_exposure` once that state is known.
A second, config-only check here would refuse initialized installs that no
longer need the token (restarts/upgrades) and could drift from the lifespan's
enforcement.
"""

from __future__ import annotations

import uvicorn

from plex_manager.config import get_settings


def main() -> None:
    """Run the ASGI server using the configured host/port."""
    settings = get_settings()
    uvicorn.run(
        "plex_manager.web.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
