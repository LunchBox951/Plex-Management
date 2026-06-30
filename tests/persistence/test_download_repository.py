"""``SqlDownloadRepository`` create / get_by_hash / list_active / update_status."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.repositories import SqlDownloadRepository


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
