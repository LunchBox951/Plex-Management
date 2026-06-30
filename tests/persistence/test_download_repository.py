"""``SqlDownloadRepository`` create / get_by_hash / list_active / update_status."""

from __future__ import annotations

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
