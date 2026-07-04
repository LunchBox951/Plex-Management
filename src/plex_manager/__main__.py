"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script)."""

from __future__ import annotations

import uvicorn

# Re-exported so callers/tests may keep importing it from the entry point; the
# guard itself lives in ``config`` because the ASGI ``lifespan`` applies the SAME
# check on launch paths that never run this module (see validate_startup_exposure).
from plex_manager.config import get_settings, validate_startup_exposure

__all__ = ["main", "validate_startup_exposure"]


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
