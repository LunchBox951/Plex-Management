"""``SqlDownloadRepository`` create / get_by_hash / list_active / update_status."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.repositories import SqlDownloadRepository


async def test_at_most_one_active_download_per_request(session: AsyncSession) -> None:
    # The uq_downloads_active_request partial unique index is the DB backstop to
    # the app-level parallel-grab guard: a request can never have two active
    # downloads racing each other, even under true concurrency.
    mr = MediaRequest(
        tmdb_id=1, media_type=MediaType.movie, title="X", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="active_a", status="downloading", media_request_id=mr.id)
    with pytest.raises(IntegrityError):
        # A DIFFERENT release for the SAME request while one is still active.
        await repo.create(torrent_hash="active_b", status="downloading", media_request_id=mr.id)


async def test_concurrent_movie_downloads_still_collide_after_season_coalesce(
    session: AsyncSession,
) -> None:
    """Regression guard for the ``uq_downloads_active_request`` widening.

    The index moved from a plain unique on ``media_request_id`` to a unique on
    ``(media_request_id, COALESCE(season, -1))`` so TV can grab season 1 and
    season 2 concurrently. A naive ``(media_request_id, season)`` unique index
    (no COALESCE) would have silently BROKEN the movie guarantee this index
    exists for: SQL NULL is never equal to NULL, so two movie downloads (both
    ``season IS NULL``) would stop colliding. This pins the COALESCE(-1)
    sentinel actually folds every movie row onto the same synthetic key.
    """
    mr = MediaRequest(
        tmdb_id=2, media_type=MediaType.movie, title="Movie", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    # Both downloads are movies: season is NULL on both.
    await repo.create(torrent_hash="movie_a", status="downloading", media_request_id=mr.id)
    with pytest.raises(IntegrityError):
        await repo.create(torrent_hash="movie_b", status="downloading", media_request_id=mr.id)


async def test_concurrent_tv_downloads_for_different_seasons_do_not_collide(
    session: AsyncSession,
) -> None:
    """The widened index scopes uniqueness PER SEASON, so a whole-series TV
    request can have season 1 and season 2 downloading at the same time —
    while a second release for the SAME season still collides."""
    mr = MediaRequest(
        tmdb_id=3, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="s1", status="downloading", media_request_id=mr.id, season=1)
    # A DIFFERENT season for the same request must NOT collide.
    await repo.create(torrent_hash="s2", status="downloading", media_request_id=mr.id, season=2)
    # A SECOND release for the SAME season must still collide.
    with pytest.raises(IntegrityError):
        await repo.create(
            torrent_hash="s1_again", status="downloading", media_request_id=mr.id, season=1
        )


async def test_create_then_get_by_hash(session: AsyncSession) -> None:
    repo = SqlDownloadRepository(session)
    created = await repo.create(
        torrent_hash="abc123",
        status="downloading",
        magnet_link="magnet:?xt=urn:btih:abc123",
        tmdb_id=603,
        year=1999,
    )
    assert created.id > 0
    assert created.progress == 0.0
    assert created.seed_ratio == 0.0

    fetched = await repo.get_by_hash("abc123")
    assert fetched is not None
    assert fetched == created
    assert await repo.get_by_hash("nope") is None


async def test_list_active_excludes_terminal_states(session: AsyncSession) -> None:
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="h_dl", status="downloading")
    await repo.create(torrent_hash="h_imp", status="imported")
    await repo.create(torrent_hash="h_fail", status="failed")
    await repo.create(torrent_hash="h_nar", status="no_acceptable_release")
    await repo.create(torrent_hash="h_search", status="searching")

    active = await repo.list_active()
    assert {d.torrent_hash for d in active} == {"h_dl", "h_search"}


async def test_update_status_sets_optional_fields(session: AsyncSession) -> None:
    repo = SqlDownloadRepository(session)
    created = await repo.create(torrent_hash="upd", status="downloading")
    await repo.update_status(
        created.id,
        "imported",
        progress=1.0,
        seed_ratio=2.5,
        download_path="/data/movies/Foo",
    )
    fetched = await repo.get_by_hash("upd")
    assert fetched is not None
    assert fetched.status == "imported"
    assert fetched.progress == 1.0
    assert fetched.seed_ratio == 2.5
    assert fetched.download_path == "/data/movies/Foo"


async def test_update_status_leaves_unspecified_fields_untouched(
    session: AsyncSession,
) -> None:
    repo = SqlDownloadRepository(session)
    created = await repo.create(torrent_hash="keep", status="downloading")
    await repo.update_status(created.id, "downloading", progress=0.5)
    fetched = await repo.get_by_hash("keep")
    assert fetched is not None
    assert fetched.progress == 0.5
    assert fetched.seed_ratio == 0.0
    assert fetched.failed_reason is None


async def test_update_status_stamps_first_seen_at_grace_anchor(
    session: AsyncSession,
) -> None:
    # The missing-grace anchor must be settable via the repository so the
    # reconciler's grace window can actually start (set_first_seen_at path).
    repo = SqlDownloadRepository(session)
    created = await repo.create(torrent_hash="miss", status="downloading")
    assert created.first_seen_at is None

    anchor = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    await repo.update_status(created.id, "client_missing", first_seen_at=anchor)

    fetched = await repo.get_by_hash("miss")
    assert fetched is not None
    assert fetched.status == "client_missing"
    # SQLite stores DATETIME without tzinfo; the wall-clock value round-trips.
    assert fetched.first_seen_at is not None
    assert fetched.first_seen_at.replace(tzinfo=UTC) == anchor

    # A later status update without first_seen_at must not clear the anchor.
    await repo.update_status(created.id, "client_missing", progress=0.0)
    again = await repo.get_by_hash("miss")
    assert again is not None
    assert again.first_seen_at is not None
    assert again.first_seen_at.replace(tzinfo=UTC) == anchor
