"""``SqlDownloadRepository`` create / get_by_hash / list_active / update_status."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.models import Download, DownloadScope, MediaRequest, MediaType, RequestStatus
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


async def test_find_active_for_request_scopes_by_season(session: AsyncSession) -> None:
    """The widened parallel-grab guard filters PER SEASON: a whole-series TV
    request with season 1 and season 2 both downloading must see EACH season's
    OWN active download, never the other's."""
    mr = MediaRequest(
        tmdb_id=4, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="s1", status="downloading", media_request_id=mr.id, season=1)
    await repo.create(torrent_hash="s2", status="downloading", media_request_id=mr.id, season=2)

    s1_active = await repo.find_active_for_request(mr.id, season=1)
    assert s1_active is not None
    assert s1_active.torrent_hash == "s1"

    s2_active = await repo.find_active_for_request(mr.id, season=2)
    assert s2_active is not None
    assert s2_active.torrent_hash == "s2"

    # A season with no active download of its own finds nothing, even though the
    # request has active downloads for OTHER seasons.
    assert await repo.find_active_for_request(mr.id, season=3) is None


async def test_active_download_scope_unique_index_blocks_duplicate_exact_scope(
    session: AsyncSession,
) -> None:
    """The logical scope table has its own DB backstop: even if two physical
    downloads carry different scalar seasons, they cannot both claim the same active
    TV scope for one request."""
    mr = MediaRequest(
        tmdb_id=40, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    first = await repo.create(
        torrent_hash="scope_s1",
        status="downloading",
        media_request_id=mr.id,
        season=1,
        episodes=[4, 5],
    )
    second = await repo.create(
        torrent_hash="scope_s2",
        status="downloading",
        media_request_id=mr.id,
        season=2,
    )

    assert first.scopes[0].episodes == [4, 5]
    with pytest.raises(IntegrityError):
        await repo.ensure_scope(second.id, media_request_id=mr.id, season=1, episodes=[5, 4])


async def test_active_download_scope_unique_index_allows_terminal_prior_scope(
    session: AsyncSession,
) -> None:
    """A completed/imported scope is historical and must not block a fresh active
    scope with the same target."""
    mr = MediaRequest(
        tmdb_id=41, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    imported = Download(
        torrent_hash="scope_imported",
        status="imported",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(imported)
    await session.flush()
    session.add(
        DownloadScope(
            download_id=imported.id,
            media_request_id=mr.id,
            season_number=1,
            episodes_json=None,
            scope_key="season:1|episodes:*",
            status="imported",
        )
    )
    await session.flush()

    repo = SqlDownloadRepository(session)
    active = await repo.create(
        torrent_hash="scope_active",
        status="downloading",
        media_request_id=mr.id,
        season=1,
    )
    assert active.scopes[0].status == "active"


async def test_active_download_scope_unique_index_blocks_duplicate_import_blocked_scope(
    session: AsyncSession,
) -> None:
    """An unresolved import-blocked scope is still active for dedup purposes."""
    mr = MediaRequest(
        tmdb_id=42, media_type=MediaType.tv, title="Show", status=RequestStatus.import_blocked
    )
    session.add(mr)
    await session.flush()
    blocked = Download(
        torrent_hash="scope_blocked",
        status="import_blocked",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    active = Download(
        torrent_hash="scope_active_again",
        status="downloading",
        media_request_id=mr.id,
        season=2,
        media_type=MediaType.tv,
    )
    session.add_all([blocked, active])
    await session.flush()
    session.add(
        DownloadScope(
            download_id=blocked.id,
            media_request_id=mr.id,
            season_number=2,
            episodes_json=None,
            scope_key="season:2|episodes:*",
            status="import_blocked",
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        session.add(
            DownloadScope(
                download_id=active.id,
                media_request_id=mr.id,
                season_number=2,
                episodes_json=None,
                scope_key="season:2|episodes:*",
                status="active",
            )
        )
        await session.flush()


async def test_find_active_for_request_includes_import_blocked_scope(
    session: AsyncSession,
) -> None:
    mr = MediaRequest(
        tmdb_id=43, media_type=MediaType.tv, title="Show", status=RequestStatus.import_blocked
    )
    session.add(mr)
    await session.flush()
    download = Download(
        torrent_hash="shared_blocked",
        status="import_blocked",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(download)
    await session.flush()
    session.add(
        DownloadScope(
            download_id=download.id,
            media_request_id=mr.id,
            season_number=2,
            episodes_json=None,
            scope_key="season:2|episodes:*",
            status="import_blocked",
        )
    )
    await session.flush()

    active = await SqlDownloadRepository(session).find_active_for_request(mr.id, season=2)

    assert active is not None
    assert active.torrent_hash == "shared_blocked"


async def test_find_latest_imported_for_request_matches_imported_scope_on_blocked_download(
    session: AsyncSession,
) -> None:
    mr = MediaRequest(
        tmdb_id=44, media_type=MediaType.tv, title="Show", status=RequestStatus.import_blocked
    )
    session.add(mr)
    await session.flush()
    download = Download(
        torrent_hash="shared_partial_import",
        status="import_blocked",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(download)
    await session.flush()
    session.add(
        DownloadScope(
            download_id=download.id,
            media_request_id=mr.id,
            season_number=2,
            episodes_json=None,
            scope_key="season:2|episodes:*",
            status="imported",
        )
    )
    await session.flush()

    imported = await SqlDownloadRepository(session).find_latest_imported_for_request(
        mr.id, season=2
    )

    assert imported is not None
    assert imported.torrent_hash == "shared_partial_import"


async def test_terminal_reuse_preserves_imported_scope_history(
    session: AsyncSession,
) -> None:
    mr = MediaRequest(
        tmdb_id=45, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    download = Download(
        torrent_hash="shared_reuse",
        status="imported",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(download)
    await session.flush()
    session.add(
        DownloadScope(
            download_id=download.id,
            media_request_id=mr.id,
            season_number=1,
            episodes_json=None,
            scope_key="season:1|episodes:*",
            status="imported",
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    repo = SqlDownloadRepository(session)

    claimed = await repo.update_status_if_in(
        download.id,
        "downloading",
        frozenset({"imported"}),
        media_request_id=mr.id,
        replace_grab_metadata=True,
        season=2,
        media_type="tv",
    )

    assert claimed is True
    fetched = await repo.get_by_hash("shared_reuse")
    assert fetched is not None
    assert fetched.season == 2
    assert [(scope.season, scope.status) for scope in fetched.scopes] == [
        (1, "imported"),
        (2, "active"),
    ]
    imported = await repo.find_latest_imported_for_request(mr.id, season=1)
    assert imported is not None
    assert imported.torrent_hash == "shared_reuse"


async def test_find_active_for_request_ignores_imported_matching_scope_on_blocked_row(
    session: AsyncSession,
) -> None:
    mr = MediaRequest(
        tmdb_id=46, media_type=MediaType.tv, title="Show", status=RequestStatus.import_blocked
    )
    session.add(mr)
    await session.flush()
    download = Download(
        torrent_hash="shared_partial_blocked",
        status="import_blocked",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(download)
    await session.flush()
    session.add_all(
        [
            DownloadScope(
                download_id=download.id,
                media_request_id=mr.id,
                season_number=1,
                episodes_json=None,
                scope_key="season:1|episodes:*",
                status="imported",
            ),
            DownloadScope(
                download_id=download.id,
                media_request_id=mr.id,
                season_number=2,
                episodes_json=None,
                scope_key="season:2|episodes:*",
                status="import_blocked",
            ),
        ]
    )
    await session.flush()

    repo = SqlDownloadRepository(session)

    assert await repo.find_active_for_request(mr.id, season=1) is None
    active = await repo.find_active_for_request(mr.id, season=2)
    assert active is not None
    assert active.torrent_hash == "shared_partial_blocked"


async def test_ensure_scope_reactivates_matching_terminal_scope(
    session: AsyncSession,
) -> None:
    mr = MediaRequest(
        tmdb_id=47, media_type=MediaType.tv, title="Show", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    download = Download(
        torrent_hash="shared_reactivate",
        status="import_blocked",
        media_request_id=mr.id,
        season=1,
        media_type=MediaType.tv,
    )
    session.add(download)
    await session.flush()
    session.add(
        DownloadScope(
            download_id=download.id,
            media_request_id=mr.id,
            season_number=1,
            episodes_json=None,
            scope_key="season:1|episodes:*",
            status="imported",
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()

    repo = SqlDownloadRepository(session)
    scope = await repo.ensure_scope(download.id, media_request_id=mr.id, season=1)

    assert scope.status == "active"
    assert scope.completed_at is None
    active = await repo.find_active_for_request(mr.id, season=1)
    assert active is not None
    assert active.torrent_hash == "shared_reactivate"


async def test_find_active_for_request_default_season_matches_movie_null_season(
    session: AsyncSession,
) -> None:
    """The default ``season=None`` renders ``IS NULL`` -- the movie call sites that
    never pass ``season`` keep matching their (always NULL-season) rows exactly as
    before this method was widened."""
    mr = MediaRequest(
        tmdb_id=5, media_type=MediaType.movie, title="Movie", status=RequestStatus.downloading
    )
    session.add(mr)
    await session.flush()
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="movie_active", status="downloading", media_request_id=mr.id)

    active = await repo.find_active_for_request(mr.id)
    assert active is not None
    assert active.torrent_hash == "movie_active"


async def test_create_stores_and_round_trips_episodes_json(session: AsyncSession) -> None:
    """``episodes`` (TV only) persists to ``Download.episodes_json`` and round-trips
    through the repository -- ``None`` means "import every valid file found"; an
    explicit list scopes the import to those episode numbers."""
    repo = SqlDownloadRepository(session)
    whole_season = await repo.create(
        torrent_hash="pack", status="downloading", season=1, episodes=None
    )
    assert whole_season.episodes is None

    scoped = await repo.create(
        torrent_hash="scoped", status="downloading", season=1, episodes=[4, 5, 6]
    )
    assert scoped.episodes == [4, 5, 6]

    fetched = await repo.get_by_hash("scoped")
    assert fetched is not None
    assert fetched.episodes == [4, 5, 6]


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


async def test_create_persists_release_title(session: AsyncSession) -> None:
    """``release_title`` (issue #134) round-trips through ``create``/``get_by_hash``
    exactly like every other grab-time field."""
    repo = SqlDownloadRepository(session)
    created = await repo.create(
        torrent_hash="rt1",
        status="downloading",
        release_title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
    )
    assert created.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"

    fetched = await repo.get_by_hash("rt1")
    assert fetched is not None
    assert fetched.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"


async def test_create_release_title_defaults_to_none(session: AsyncSession) -> None:
    """A caller that never passes ``release_title`` gets an honest ``None``, not a
    fabricated placeholder -- the queue's fallback chain (title -> release_title ->
    short hash) depends on this being genuinely absent."""
    repo = SqlDownloadRepository(session)
    created = await repo.create(torrent_hash="rt_none", status="downloading")
    assert created.release_title is None


async def test_list_active_for_queue_joins_media_request_title_and_poster(
    session: AsyncSession,
) -> None:
    """The queue-specific read (issue #134) enriches each row with the OWNING
    ``MediaRequest``'s ``title``/``poster_url``, alongside the row's own
    ``release_title`` -- exactly the three fields the human-legible queue row
    needs."""
    request = MediaRequest(
        tmdb_id=900,
        media_type=MediaType.movie,
        title="Some Movie",
        status=RequestStatus.downloading,
        poster_url="https://image.tmdb.org/poster.jpg",
    )
    session.add(request)
    await session.flush()

    repo = SqlDownloadRepository(session)
    await repo.create(
        torrent_hash="q1",
        status="downloading",
        media_request_id=request.id,
        release_title="Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
    )

    [row] = await repo.list_active_for_queue()
    assert row.title == "Some Movie"
    assert row.poster_url == "https://image.tmdb.org/poster.jpg"
    assert row.release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"


async def test_list_active_for_queue_orphan_download_renders_with_none_title_and_poster(
    session: AsyncSession,
) -> None:
    """A download whose owning request was deleted (``media_request_id`` SET NULL)
    must still render in the queue -- honesty over silence, never dropped -- with
    ``title``/``poster_url`` honestly ``None`` rather than the read failing or the
    row vanishing. The LEFT OUTER JOIN (not INNER) is what makes this possible."""
    repo = SqlDownloadRepository(session)
    await repo.create(
        torrent_hash="orphan",
        status="downloading",
        media_request_id=None,
        release_title="Orphaned.Release.2020-GROUP",
    )

    [row] = await repo.list_active_for_queue()
    assert row.title is None
    assert row.poster_url is None
    assert row.release_title == "Orphaned.Release.2020-GROUP"


async def test_list_active_for_queue_excludes_terminal_states(session: AsyncSession) -> None:
    """Mirrors :meth:`SqlDownloadRepository.list_active`'s terminal exclusion -- the
    enriched queue read must not surface finished downloads either."""
    repo = SqlDownloadRepository(session)
    await repo.create(torrent_hash="q_dl", status="downloading")
    await repo.create(torrent_hash="q_imp", status="imported")

    active = await repo.list_active_for_queue()
    assert {d.torrent_hash for d in active} == {"q_dl"}


async def test_update_status_if_in_replace_grab_metadata_refreshes_release_title(
    session: AsyncSession,
) -> None:
    """The terminal-row-reuse CAS (``replace_grab_metadata=True``) must overwrite a
    stale ``release_title`` from a prior grab, exactly like it already does for
    ``magnet_link``/``tmdb_id``/``season`` -- a resurrected row otherwise keeps
    reporting the OLD release as what's currently downloading (issue #134)."""
    repo = SqlDownloadRepository(session)
    created = await repo.create(
        torrent_hash="reuse1", status="failed", release_title="Old.Release-GROUP"
    )

    claimed = await repo.update_status_if_in(
        created.id,
        "downloading",
        frozenset({"failed"}),
        replace_grab_metadata=True,
        release_title="New.Release-GROUP",
    )
    assert claimed is True

    fetched = await repo.get_by_hash("reuse1")
    assert fetched is not None
    assert fetched.release_title == "New.Release-GROUP"


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


async def test_list_active_populate_existing_refreshes_stale_identity_map_row(
    tmp_path: Path,
) -> None:
    """Issue #77: after a status compare-and-swap LOSES to a concurrent writer, the
    already-loaded row lingers in the session identity map with its stale, pre-CAS
    status. Because the app runs ``expire_on_commit=False``, the intervening commit
    does not refresh it, and a plain SELECT keeps the loaded instance rather than
    overwriting it -- so ``reconcile_and_list``'s terminal read reports a status the
    DB no longer holds. ``list_active(populate_existing=True)`` closes that gap.

    This reproduces the bug faithfully with REAL concurrency: a file-backed DB with
    a SEPARATE engine for the concurrent writer -> a genuinely distinct connection,
    unlike the suite's shared single-connection in-memory ``StaticPool``. Diverging
    the row via the reader's own session (ORM or textual DML) would auto-expire its
    identity map and mask the bug. The exact production sequence is exercised: load
    -> a concurrent writer advances the row to another NON-terminal status -> the
    reader's CAS misses -> commit -> terminal read.

    A strong reference to the loaded ORM instance is deliberately held: SQLAlchemy's
    identity map is WEAK, so the stale row survives only while something references
    it -- which is exactly when the bug bites (and why #77 is intermittent: under GC
    pressure the discarded instance is collected and the next SELECT reloads fresh).
    Pinning it makes the regression deterministic instead of GC-timing-dependent.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}"
    engine = create_async_engine(url)
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as setup:
        created = await SqlDownloadRepository(setup).create(
            torrent_hash="cas", status="downloading"
        )
        await setup.commit()
        download_id = created.id

    reader = maker()
    repo = SqlDownloadRepository(reader)
    try:
        # Load the row into the reader's identity map (status 'downloading') and
        # PIN the ORM instance so the weak identity map keeps it (see docstring).
        loaded = await repo.list_active()
        assert [r.status for r in loaded] == ["downloading"]
        pinned = (await reader.execute(select(Download))).scalars().all()
        assert [row.status for row in pinned] == ["downloading"]

        # A CONCURRENT writer on a DISTINCT connection (separate engine) advances the
        # row to another NON-terminal status and commits -- a true cross-connection
        # change the reader's session knows nothing about.
        writer_engine = create_async_engine(url)
        try:
            async with async_sessionmaker(writer_engine)() as writer:
                await writer.execute(
                    text("UPDATE downloads SET status = 'importing' WHERE id = :id"),
                    {"id": download_id},
                )
                await writer.commit()
        finally:
            await writer_engine.dispose()

        # The reader's own CAS now MISSES (the row left 'downloading'); a miss leaves
        # the loaded instance untouched (not expired).
        applied = await repo.update_status_if_in(
            download_id, "import_pending", frozenset({"downloading"})
        )
        assert applied is False
        await reader.commit()  # end the read snapshot (mirrors reconcile Phase A)

        # Default read: the identity map still wins, so the stale status leaks.
        stale = await repo.list_active()
        assert [r.status for r in stale] == ["downloading"]
        assert pinned[0].status == "downloading"  # the pinned instance is still stale

        # populate_existing overwrites the loaded instance from the DB -> honest.
        refreshed = await repo.list_active(populate_existing=True)
        assert [r.status for r in refreshed] == ["importing"]
        assert pinned[0].status == "importing"  # the pinned instance was refreshed too
    finally:
        await reader.close()
        await engine.dispose()
