"""FastAPI application factory.

For now this exposes only a liveness endpoint so the skeleton is runnable and CI
has something real to exercise. Feature routers are added in the v1-planning
session.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from plex_manager import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the container healthcheck and monitoring."""
    return {"status": "ok"}


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(title="Plex Manager", version=__version__)
    app.include_router(router)
    return app


app = create_app()
