"""Serve the built single-page app (ADR-0009).

The SPA is built by the frontend's Node toolchain into ``web/static`` (a git-ignored
build artifact that ships in the Docker image). This module mounts it *if it has
been built* — so a Python-only checkout or CI lane, where ``static/`` is absent,
still runs the full API with no UI rather than failing to start.

Two things are served:
  - ``/assets/*`` — the hashed JS/CSS bundles, via ``StaticFiles``;
  - everything else (non-API) — ``index.html``, so the client-side router owns
    deep links like ``/queue`` or ``/setup`` (a hard refresh still lands right).

The catch-all never shadows the API: real routes are matched first, and any
unmatched ``/api`` / docs path is returned as a 404 rather than the SPA shell.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ASSETS_DIR = _STATIC_DIR / "assets"
_INDEX_FILE = _STATIC_DIR / "index.html"

# Paths the SPA fallback must NOT answer — they belong to the API/docs/assets
# surface and should 404 honestly when unmatched instead of silently returning
# the shell. Prefixes cover any subpath under a reserved mount (e.g. a mistyped
# ``/docs/nope`` or a missing ``/assets/foo.js`` when the assets dir isn't
# mounted); the exact set covers the bare mount path itself (e.g. ``/api`` with
# no trailing segment, which is never a real route since every API route lives
# under ``/api/v1/...``).
_NON_SPA_PREFIXES = ("api/", "docs/", "redoc/", "assets/")
_NON_SPA_EXACT = frozenset({"health", "docs", "redoc", "openapi.json", "api", "assets"})


def spa_is_built() -> bool:
    """True when a built ``index.html`` is present to serve."""
    return _INDEX_FILE.is_file()


async def _serve_index() -> FileResponse:
    """Serve the SPA shell (root document)."""
    return FileResponse(_INDEX_FILE)


async def _spa_fallback(full_path: str) -> Response:
    """Serve a real top-level static file, or the SPA shell for a client route."""
    if full_path.startswith(_NON_SPA_PREFIXES) or full_path in _NON_SPA_EXACT:
        return Response(status_code=404)
    # ``resolve`` + ``is_relative_to`` blocks any ``..`` traversal out of the root.
    candidate = (_STATIC_DIR / full_path).resolve()
    if candidate.is_file() and candidate.is_relative_to(_STATIC_DIR):
        return FileResponse(candidate)
    return FileResponse(_INDEX_FILE)


def mount_spa(app: FastAPI) -> None:
    """Mount the built SPA onto ``app``. No-op when the frontend isn't built.

    Must be called *after* every API router is included so the catch-all route
    has the lowest match priority.
    """
    if not spa_is_built():
        return

    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")

    app.add_api_route("/", _serve_index, include_in_schema=False)
    app.add_api_route("/{full_path:path}", _spa_fallback, include_in_schema=False)
