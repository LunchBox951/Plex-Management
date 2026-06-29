"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script)."""

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
