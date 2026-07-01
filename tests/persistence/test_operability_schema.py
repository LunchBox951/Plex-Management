"""Operability-beta schema deltas (ADR-0012), pinned directly via the ORM.

Mirrors ``test_season_request_schema.py``'s style: no repository exists yet for
``LogEvent`` / the new ``library_path`` / ``keep_forever`` columns (that wiring
is a later build layer), so these tests exercise the ORM models directly against
the ``Base.metadata.create_all`` schema, the same schema the Alembic migration
(``6c7fca1436d8``) produces.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import LogEvent, MediaRequest, SeasonRequest


async def test_log_event_round_trips_with_defaults(session: AsyncSession) -> None:
    """A minimal ``LogEvent`` (no ``context_json``) persists with a DB-stamped
    ``created_at`` and every required column intact."""
    row = LogEvent(level="INFO", logger="plex_manager.services.reconciler", message="tick")
    session.add(row)
    await session.flush()
    await session.refresh(row)

    assert row.id > 0
    assert row.created_at is not None
    assert row.level == "INFO"
    assert row.logger == "plex_manager.services.reconciler"
    assert row.message == "tick"
    assert row.context_json is None


async def test_log_event_context_json_carries_correlation_ids(session: AsyncSession) -> None:
    """``context_json`` round-trips a correlation dict (request_id/download_id/tmdb_id)
    — the affordance ``GET /ops/logs/export`` relies on to assemble one trail."""
    context = {"request_id": 42, "download_id": 7, "tmdb_id": 603}
    row = LogEvent(
        level="ERROR",
        logger="plex_manager.services.grab_service",
        message="grab failed",
        context_json=context,
    )
    session.add(row)
    await session.flush()

    fetched = (await session.execute(select(LogEvent).where(LogEvent.id == row.id))).scalar_one()
    assert fetched.context_json == context


async def test_media_request_library_path_and_keep_forever_default(
    session: AsyncSession,
) -> None:
    """Neither column is supplied at creation: ``library_path`` stays NULL (no
    breadcrumb yet) and ``keep_forever`` defaults to ``False`` (unpinned)."""
    mr = MediaRequest(tmdb_id=9001, media_type="movie", title="Unpinned", status="available")
    session.add(mr)
    await session.flush()
    await session.refresh(mr)

    assert mr.library_path is None
    assert mr.keep_forever is False


async def test_media_request_library_path_and_keep_forever_round_trip(
    session: AsyncSession,
) -> None:
    """Both columns persist an eviction-service-set value across a re-fetch."""
    mr = MediaRequest(
        tmdb_id=9002,
        media_type="movie",
        title="Pinned",
        status="available",
        library_path="/data/library/movies/Pinned (2024)/Pinned.mkv",
        keep_forever=True,
    )
    session.add(mr)
    await session.flush()

    fetched = (
        await session.execute(select(MediaRequest).where(MediaRequest.id == mr.id))
    ).scalar_one()
    assert fetched.library_path == "/data/library/movies/Pinned (2024)/Pinned.mkv"
    assert fetched.keep_forever is True


async def test_season_request_library_path_defaults_null_and_round_trips(
    session: AsyncSession,
) -> None:
    """Mirrors the ``MediaRequest`` breadcrumb at the per-season granularity."""
    mr = MediaRequest(tmdb_id=9003, media_type="tv", title="Show", status="partially_available")
    session.add(mr)
    await session.flush()

    no_breadcrumb = SeasonRequest(media_request_id=mr.id, season_number=1, status="pending")
    with_breadcrumb = SeasonRequest(
        media_request_id=mr.id,
        season_number=2,
        status="available",
        library_path="/data/library/tv/Show/Season 02",
    )
    session.add_all([no_breadcrumb, with_breadcrumb])
    await session.flush()
    await session.refresh(no_breadcrumb)
    await session.refresh(with_breadcrumb)

    assert no_breadcrumb.library_path is None
    assert with_breadcrumb.library_path == "/data/library/tv/Show/Season 02"


async def test_evicted_status_round_trips(session: AsyncSession) -> None:
    """``evicted`` is a bare VARCHAR value (no CHECK) that round-trips like any
    other status, and stamps a plausible ``requested_at``/``completed_at``
    lifecycle for an already-imported-then-evicted title."""
    mr = MediaRequest(
        tmdb_id=9004,
        media_type="movie",
        title="Evicted Movie",
        status="evicted",
        completed_at=datetime.now(UTC),
    )
    session.add(mr)
    await session.flush()

    fetched = (
        await session.execute(select(MediaRequest).where(MediaRequest.id == mr.id))
    ).scalar_one()
    assert fetched.status == "evicted"


async def test_partial_unique_index_allows_new_request_after_eviction(
    session: AsyncSession,
) -> None:
    """The ``uq_media_requests_active`` partial index deliberately excludes
    ``evicted`` — same treatment as the settled ``available``/``failed``
    statuses — so a re-request for the same (tmdb_id, media_type) after an
    eviction is NOT rejected by the DB backstop; it creates a fresh, independent
    row rather than resurrecting the old (now off-disk) one."""
    evicted = MediaRequest(tmdb_id=9005, media_type="movie", title="Gone", status="evicted")
    session.add(evicted)
    await session.flush()

    fresh = MediaRequest(tmdb_id=9005, media_type="movie", title="Gone again", status="pending")
    session.add(fresh)
    await session.flush()  # must NOT raise IntegrityError

    assert fresh.id != evicted.id


async def test_partial_unique_index_still_blocks_two_pending_for_same_evicted_title(
    session: AsyncSession,
) -> None:
    """Sanity check on the other side of the same index: two ACTIVE rows for the
    same (tmdb_id, media_type) still collide even when an unrelated ``evicted``
    row already exists for it (the exclusion is scoped to ``evicted`` alone, not
    a blanket relaxation of the index)."""
    session.add(MediaRequest(tmdb_id=9006, media_type="movie", title="Gone", status="evicted"))
    await session.flush()

    session.add(
        MediaRequest(tmdb_id=9006, media_type="movie", title="Re-requested", status="pending")
    )
    await session.flush()

    session.add(MediaRequest(tmdb_id=9006, media_type="movie", title="Dup", status="searching"))
    with pytest.raises(IntegrityError):
        await session.flush()
