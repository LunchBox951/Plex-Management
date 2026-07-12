"""``SqlSeasonEpisodeStateRepository`` upsert_target / mark_* / list / counts.

Mirrors ``test_season_request_repository.py``'s fixture pattern (the shared
``session`` fixture from ``tests/persistence/conftest.py``).
"""

from __future__ import annotations

import itertools
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import Download, MediaRequest, SeasonRequest
from plex_manager.repositories import SqlSeasonEpisodeStateRepository

_hash_counter = itertools.count()


async def _make_season(session: AsyncSession, *, tmdb_id: int = 900, season: int = 1) -> int:
    mr = MediaRequest(tmdb_id=tmdb_id, media_type="tv", title="Show", status="downloading")
    session.add(mr)
    await session.flush()
    sr = SeasonRequest(media_request_id=mr.id, season_number=season, status="downloading")
    session.add(sr)
    await session.flush()
    return sr.id


async def _make_download(session: AsyncSession) -> int:
    """A minimal ``downloads`` row -- ``grabbed_download_id`` FKs into this table
    with FK enforcement ON in the test engine (``enable_sqlite_fk_enforcement``)."""
    download = Download(
        torrent_hash=f"episode-state-test-{next(_hash_counter)}", status="downloading"
    )
    session.add(download)
    await session.flush()
    return download.id


async def test_upsert_target_seeds_pending_rows(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8), 3: None})

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert set(by_episode) == {1, 2, 3}
    assert all(r.status == "pending" for r in by_episode.values())
    assert by_episode[1].air_date == date(2026, 1, 1)
    assert by_episode[3].air_date is None


async def test_upsert_target_never_downgrades_an_imported_row(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})
    await repo.mark_imported(season_request_id, [1], download_id=download_id)

    # Re-running upsert_target for the same episode must NOT regress it back to
    # pending -- only a still-pending row is ever (re-)seeded as pending.
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[1].status == "imported"
    assert by_episode[2].status == "pending"


async def test_upsert_target_refreshes_air_date_but_keeps_status(
    session: AsyncSession,
) -> None:
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})

    # Grab episode 1 (status -> grabbed), then re-run upsert_target with a
    # slightly different air_date -- the status must NOT regress to pending.
    await repo.mark_grabbed(season_request_id, [1], download_id=download_id)
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 2), 2: date(2026, 1, 8)})

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[1].status == "grabbed"
    assert by_episode[1].air_date == date(2026, 1, 2)
    assert by_episode[2].status == "pending"


async def test_upsert_target_adds_newly_aired_episode_without_disturbing_others(
    session: AsyncSession,
) -> None:
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
    await repo.mark_imported(season_request_id, [1], download_id=download_id)

    # Show airs a new episode: target grows.
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[1].status == "imported"
    assert by_episode[2].status == "pending"


async def test_mark_grabbed_sets_status_and_download_id(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})
    await repo.mark_grabbed(season_request_id, [1], download_id=download_id)

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[1].status == "grabbed"
    assert by_episode[1].grabbed_download_id == download_id
    assert by_episode[2].status == "pending"


async def test_mark_grabbed_creates_a_row_when_target_absent(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.mark_grabbed(season_request_id, [5], download_id=download_id)

    rows = await repo.list_for_season(season_request_id)
    assert len(rows) == 1
    assert rows[0].episode_number == 5
    assert rows[0].status == "grabbed"
    assert rows[0].grabbed_download_id == download_id


async def test_mark_grabbed_never_regresses_an_imported_row(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    first_download_id = await _make_download(session)
    second_download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
    await repo.mark_imported(season_request_id, [1], download_id=first_download_id)
    await repo.mark_grabbed(season_request_id, [1], download_id=second_download_id)

    rows = await repo.list_for_season(season_request_id)
    assert rows[0].status == "imported"
    assert rows[0].grabbed_download_id == first_download_id


async def test_mark_imported_upserts_episodes_beyond_the_seeded_target(
    session: AsyncSession,
) -> None:
    """A season pack can place episodes beyond the seeded aired set (issue #178's
    "still counts as imported") -- ``mark_imported`` must create rows for them."""
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.mark_imported(season_request_id, [1, 2, 3], download_id=download_id)

    rows = await repo.list_for_season(season_request_id)
    assert {r.episode_number for r in rows} == {1, 2, 3}
    assert all(r.status == "imported" for r in rows)
    assert all(r.grabbed_download_id == download_id for r in rows)


async def test_list_for_season_orders_by_episode_number(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(
        season_request_id, {3: date(2026, 1, 1), 1: date(2026, 1, 1), 2: date(2026, 1, 1)}
    )

    rows = await repo.list_for_season(season_request_id)
    assert [r.episode_number for r in rows] == [1, 2, 3]


async def test_counts_for_seasons_batches_imported_and_target_totals(
    session: AsyncSession,
) -> None:
    season_a = await _make_season(session, tmdb_id=900, season=1)
    season_b = await _make_season(session, tmdb_id=901, season=1)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_a, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})
    await repo.mark_imported(season_a, [1], download_id=download_id)
    await repo.upsert_target(season_b, {1: date(2026, 1, 1)})

    counts = await repo.counts_for_seasons([season_a, season_b])
    assert counts[season_a] == (1, 2)
    assert counts[season_b] == (0, 1)


async def test_counts_for_seasons_empty_input_returns_empty_mapping(
    session: AsyncSession,
) -> None:
    repo = SqlSeasonEpisodeStateRepository(session)
    assert await repo.counts_for_seasons([]) == {}


async def test_counts_for_seasons_omits_seasons_with_no_rows(session: AsyncSession) -> None:
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    counts = await repo.counts_for_seasons([season_request_id])
    assert counts == {}
