"""``SqlBlocklistRepository`` create / list / delete + two-tier identity check."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.repositories import SqlBlocklistRepository


async def test_create_and_list_for_media(session: AsyncSession) -> None:
    repo = SqlBlocklistRepository(session)
    created = await repo.create(
        source_title="Movie.2024.1080p.WEB-DL.x264-GRP",
        reason="failed",
        tmdb_id=100,
        torrent_hash="DEADBEEF",
        indexer="rarbg",
        protocol="torrent",
        media_type="movie",
    )
    assert created.id > 0
    assert created.reason == "failed"
    assert created.media_type == "movie"
    assert created.added_at is not None

    scoped = await repo.list_for_media(100)
    assert [e.id for e in scoped] == [created.id]
    assert await repo.list_for_media(999) == []
    assert len(await repo.list_for_media()) == 1


async def test_blocklist_is_scoped_by_media_type(session: AsyncSession) -> None:
    """TMDB movie and TV ids are separate namespaces, so a movie and a show can share
    a tmdb_id. A TV blocklist entry must NOT match a movie query for that id (and vice
    versa); a legacy NULL-media_type entry (movie-era) still matches both."""
    repo = SqlBlocklistRepository(session)
    await repo.create(
        source_title="Show.S02.1080p.x264",
        reason="failed",
        tmdb_id=100,
        torrent_hash="a" * 40,
        media_type="tv",
    )
    await repo.create(
        source_title="Legacy.Untyped.x264",
        reason="failed",
        tmdb_id=100,
        torrent_hash="c" * 40,
        media_type=None,  # pre-column entry
    )

    tv = {e.source_title for e in await repo.list_for_media(100, media_type="tv")}
    movie = {e.source_title for e in await repo.list_for_media(100, media_type="movie")}
    assert tv == {"Show.S02.1080p.x264", "Legacy.Untyped.x264"}  # typed-tv + legacy NULL
    assert movie == {"Legacy.Untyped.x264"}  # the tv entry is excluded from a movie query

    # is_blocklisted honours the same scope: the tv entry's hash doesn't match a movie.
    assert await repo.is_blocklisted(100, "a" * 40, "x", None, media_type="tv") is True
    assert await repo.is_blocklisted(100, "a" * 40, "x", None, media_type="movie") is False


async def test_is_blocklisted_hash_match_case_insensitive(
    session: AsyncSession,
) -> None:
    repo = SqlBlocklistRepository(session)
    await repo.create(
        source_title="Some.Release",
        reason="bad_quality",
        tmdb_id=5,
        torrent_hash="ABCDEF",
        indexer="idx",
    )
    # Hash wins even when the title differs; comparison is case-insensitive.
    assert await repo.is_blocklisted(5, "abcdef", "Totally.Different.Title", "other")
    # Both sides carry a hash, so a hash mismatch is decisive — the matching
    # title/indexer does NOT fall through to a tier-2 match.
    assert not await repo.is_blocklisted(5, "ffffff", "Some.Release", "idx")


async def test_is_blocklisted_title_indexer_fallback(session: AsyncSession) -> None:
    repo = SqlBlocklistRepository(session)
    await repo.create(
        source_title="The.Show.S01.1080p-GRP",
        reason="wrong_media",
        tmdb_id=9,
        torrent_hash=None,
        indexer="nyaa",
    )
    # No hash available -> normalized title + indexer match.
    assert await repo.is_blocklisted(9, None, "the show s01 1080p grp", "nyaa")
    # Same title, different indexer -> no match.
    assert not await repo.is_blocklisted(9, None, "The.Show.S01.1080p-GRP", "rarbg")


async def test_is_blocklisted_scopes_by_tmdb_id(session: AsyncSession) -> None:
    repo = SqlBlocklistRepository(session)
    await repo.create(
        source_title="Shared.Title",
        reason="failed",
        tmdb_id=1,
        torrent_hash="HASH1",
        indexer="idx",
    )
    # Same hash, but a different media item must not be blocked.
    assert await repo.is_blocklisted(1, "hash1", "Shared.Title", "idx")
    assert not await repo.is_blocklisted(2, "hash1", "Shared.Title", "idx")


async def test_delete_removes_entry(session: AsyncSession) -> None:
    repo = SqlBlocklistRepository(session)
    created = await repo.create(source_title="Gone.Release", reason="user_reported", tmdb_id=3)
    await repo.delete(created.id)
    assert await repo.list_for_media(3) == []
