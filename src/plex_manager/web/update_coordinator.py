"""Shared lazy construction for the web app's update coordinator."""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.db import get_sessionmaker
from plex_manager.services.update_coordination_service import UpdateCoordinationService

__all__ = ["ensure_update_coordinator"]


async def ensure_update_coordinator(app: FastAPI) -> UpdateCoordinationService:
    """Return the lifespan coordinator, constructing it for direct-test apps."""
    coordinator = getattr(app.state, "update_coordinator", None)
    if isinstance(coordinator, UpdateCoordinationService):
        return coordinator
    maker_obj = getattr(app.state, "sessionmaker", None)
    maker = (
        cast(async_sessionmaker[AsyncSession], maker_obj)
        if isinstance(maker_obj, async_sessionmaker)
        else get_sessionmaker()
    )
    coordinator = UpdateCoordinationService(maker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    return coordinator
