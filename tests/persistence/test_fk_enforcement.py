"""Foreign-key ``ON DELETE`` behaviour is actually enforced on SQLite.

Without ``PRAGMA foreign_keys=ON`` (off by default, per-connection) the schema's
CASCADE / SET NULL clauses are inert. These tests pin the enforcement so a
regression is caught rather than silently corrupting referential integrity.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import (
    AuditLog,
    Download,
    MediaRequest,
    SeasonRequest,
    User,
)


async def test_foreign_keys_pragma_is_on(session: AsyncSession) -> None:
    enabled = (await session.execute(sa.text("PRAGMA foreign_keys"))).scalar_one()
    assert enabled == 1


async def test_delete_user_sets_referencing_columns_null(session: AsyncSession) -> None:
    user = User(username="carol")
    session.add(user)
    await session.flush()

    request = MediaRequest(
        user_id=user.id,
        tmdb_id=603,
        media_type="movie",
        title="The Matrix",
        status="pending",
    )
    audit = AuditLog(user_id=user.id, action_type="request.create", entity_type="media_request")
    session.add_all([request, audit])
    await session.flush()

    await session.delete(user)
    await session.flush()

    await session.refresh(request)
    await session.refresh(audit)
    assert request.user_id is None  # ON DELETE SET NULL fired
    assert audit.user_id is None


async def test_delete_media_request_cascades_and_nulls(session: AsyncSession) -> None:
    request = MediaRequest(tmdb_id=1, media_type="tv", title="Show", status="pending")
    session.add(request)
    await session.flush()

    season = SeasonRequest(media_request_id=request.id, season_number=1, status="pending")
    download = Download(
        media_request_id=request.id,
        torrent_hash="fkhash",
        status="downloading",
    )
    session.add_all([season, download])
    await session.flush()
    season_id = season.id

    await session.delete(request)
    await session.flush()
    # The DB cascades/nulls below the ORM; drop the stale identity map and read
    # the truth straight from the rows.
    session.expunge_all()

    # season_requests -> CASCADE: the child row is gone.
    season_row = (
        await session.execute(
            sa.text("SELECT id FROM season_requests WHERE id = :id"),
            {"id": season_id},
        )
    ).first()
    assert season_row is None
    # downloads -> SET NULL: the row survives but loses its parent reference.
    media_request_id = (
        await session.execute(
            sa.text("SELECT media_request_id FROM downloads WHERE torrent_hash = 'fkhash'"),
        )
    ).scalar_one()
    assert media_request_id is None


async def test_fk_violating_insert_is_rejected(session: AsyncSession) -> None:
    season = SeasonRequest(media_request_id=999999, season_number=1, status="pending")
    session.add(season)
    with pytest.raises(IntegrityError):
        await session.flush()
