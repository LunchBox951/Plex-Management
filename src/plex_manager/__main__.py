"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script).

Deliberately NO tokenless-exposure check here: whether the setup token is
required depends on ``SystemSettings.initialized`` (DB state this synchronous
entry point cannot see), and the ASGI ``lifespan`` — which every launch path
runs, including this one via uvicorn — enforces
:func:`plex_manager.config.validate_startup_exposure` once that state is known.
A second, config-only check here would refuse initialized installs that no
longer need the token (restarts/upgrades) and could drift from the lifespan's
enforcement.

Log-level normalization (issue #100): this used to pass
``settings.log_level.lower()`` straight to ``uvicorn.run``. ``uvicorn.Config``
looks a ``str`` level up in its OWN lowercase name table
(``critical``/``error``/``warning``/``info``/``debug``/``trace``) and raises a
bare ``KeyError`` on anything it does not recognize -- e.g. a typo'd
``PLEX_MANAGER_LOG_LEVEL`` or a numeric override -- which crashes the process
during ``uvicorn.Config.__init__`` BEFORE the FastAPI ``lifespan`` (and the
app's own tolerant resolver, wired in via ``log_capture_service.
configure_logging``) ever gets a chance to run: an entire class of one-typo
startup crashes that the app is otherwise built to shrug off. Reusing
:func:`~plex_manager.services.log_capture_service.resolve_log_level` here closes
that gap: it normalizes to a valid stdlib INT level with the exact same
warn-and-fall-back-to-INFO behavior ``configure_logging`` already uses, and an
``int`` level passes straight through ``uvicorn.Config`` with no name-table
lookup at all -- so a bad value degrades to a WARNING + a running server, not a
crash before the server exists.
"""

from __future__ import annotations

import uvicorn

from plex_manager.config import get_settings
from plex_manager.services.log_capture_service import resolve_log_level


def main() -> None:
    """Run the ASGI server using the configured host/port."""
    settings = get_settings()
    uvicorn.run(
        "plex_manager.web.app:app",
        host=settings.host,
        port=settings.port,
        log_level=resolve_log_level(settings.log_level),
    )


if __name__ == "__main__":
    main()
