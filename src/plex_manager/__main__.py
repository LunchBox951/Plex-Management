"""Console entry point: ``python -m plex_manager`` (and the ``plex-manager`` script).

Just runs uvicorn on the configured host/port. First-run setup needs no boot
token or startup exposure check: an uninitialized install comes up ready to be
claimed by the first Plex server owner to sign in (see ADR-0016), so this entry
point carries no startup gate of its own.

Log-level normalization (issue #100): this used to pass
``settings.log_level.lower()`` straight to ``uvicorn.run``. ``uvicorn.Config``
looks a ``str`` level up in its OWN lowercase name table
(``critical``/``error``/``warning``/``info``/``debug``/``trace``) and raises a
bare ``KeyError`` on anything it does not recognize -- e.g. a typo'd
``PLEX_MANAGER_LOG_LEVEL`` or a numeric override -- which crashes the process
during ``uvicorn.Config.__init__`` BEFORE the FastAPI ``lifespan`` (and the
app's own tolerant resolver, wired in via ``log_capture_service.
configure_logging``) ever gets a chance to run: an entire class of one-typo
startup crashes that the app is otherwise built to shrug off.

Blindly routing EVERY value through
:func:`~plex_manager.services.log_capture_service.resolve_log_level`, though,
regresses a DIFFERENT, previously-working setting: ``trace``.
``PLEX_MANAGER_LOG_LEVEL=trace`` is one of uvicorn's own accepted
``--log-level`` names (``uvicorn.config.LOG_LEVELS``, one rung below
``debug``, used for ASGI/protocol-level debugging) but stdlib ``logging`` has
no such level, so ``resolve_log_level`` -- which only ever produces a REAL
stdlib int -- cannot round-trip it and would silently fall back to INFO,
quietly downgrading a deliberate ``trace`` request (and skipping the
``MessageLoggerMiddleware`` uvicorn only installs when ITS OWN effective level
is <= its trace constant). :func:`_uvicorn_log_level` below closes that gap
without reopening the #100 crash: it first checks the configured value
against uvicorn's OWN name table and, for a match (verified against the
installed ``uvicorn.config.LOG_LEVELS`` -- see that dict for the exact
accepted set), passes the lowercased/stripped string straight through so
``uvicorn.Config`` does its own (trace-aware) lookup itself. Only a value
uvicorn would NOT recognize by name -- a numeric override or a genuine typo --
still goes through ``resolve_log_level``'s tolerant int-normalizer, so it
degrades to a WARNING + a running server at INFO, never a crash before the
server exists. An ``int`` level passes straight through ``uvicorn.Config``
with no name-table lookup at all, so that fallback path can never hit the
``KeyError`` this whole normalization exists to avoid.
"""

from __future__ import annotations

import uvicorn
from uvicorn.config import LOG_LEVELS as _UVICORN_LOG_LEVELS

from plex_manager.config import get_settings
from plex_manager.services.log_capture_service import resolve_log_level


def _uvicorn_log_level(level: str) -> str | int:
    """Normalize ``config.log_level`` for the ``uvicorn.run(log_level=...)`` arg.

    Returns the lowercased/stripped string UNCHANGED when it is one of
    uvicorn's own accepted level names (``_UVICORN_LOG_LEVELS`` -- imported
    from ``uvicorn.config`` itself so this can never drift from whatever the
    installed uvicorn actually accepts), so ``uvicorn.Config`` performs its
    own name-table lookup -- the ONLY path that can produce ``'trace'``,
    since :func:`~plex_manager.services.log_capture_service.resolve_log_level`
    deliberately maps that name to ``DEBUG`` for its own (stdlib-only) caller
    instead. Anything uvicorn would NOT recognize by name -- a numeric
    override, or a genuine typo -- falls back to that resolver's tolerant int
    normalization, exactly as before. See the module docstring for the full
    rationale.
    """
    candidate = level.strip().lower()
    if candidate in _UVICORN_LOG_LEVELS:
        return candidate
    return resolve_log_level(level)


def main() -> None:
    """Run the ASGI server using the configured host/port."""
    settings = get_settings()
    uvicorn.run(
        "plex_manager.web.app:app",
        host=settings.host,
        port=settings.port,
        log_level=_uvicorn_log_level(settings.log_level),
    )


if __name__ == "__main__":
    main()
