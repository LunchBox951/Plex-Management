"""``SqlSeasonEpisodeStateRepository`` upsert_target / mark_* / list / counts.

Mirrors ``test_season_request_repository.py``'s fixture pattern (the shared
``session`` fixture from ``tests/persistence/conftest.py``).
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import (
    Download,
    EpisodeState,
    MediaRequest,
    SeasonEpisodeState,
    SeasonRequest,
)
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


async def test_upsert_target_retires_pending_rows_tmdb_no_longer_reports_aired(
    session: AsyncSession,
) -> None:
    """P2 fix (issue #178 review round 2): TMDB delaying/removing a previously-
    aired episode must retire its stale PENDING row -- otherwise it counts toward
    the completion target forever and the season searches for an episode that is
    not currently aired."""
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(
        season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8), 3: date(2026, 1, 15)}
    )
    # TMDB retracts episode 3 (delayed to a future/unknown air date).
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})

    rows = await repo.list_for_season(season_request_id)
    assert {r.episode_number for r in rows} == {1, 2}


async def test_upsert_target_retraction_loses_race_to_concurrent_import_promotion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 fix (issue #178 review round 4): the stale-pending retirement is a
    GUARDED DB-side delete (``WHERE status = 'pending'``), not an ORM/PK delete
    from the snapshot. Import and the airing refresh run in separate background
    tasks, so a concurrent import can promote the row to ``imported`` between
    the snapshot read and the delete -- the lost race must leave the
    just-imported row intact (never retire real content)."""
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})

    real_snapshot = (
        SqlSeasonEpisodeStateRepository._existing_by_episode  # pyright: ignore[reportPrivateUsage]
    )

    async def _snapshot_then_lose_race(
        self: SqlSeasonEpisodeStateRepository, sr_id: int
    ) -> dict[int, SeasonEpisodeState]:
        snapshot = await real_snapshot(self, sr_id)
        # The interleave: AFTER the snapshot (which still reads episode 2 as
        # ``pending``) but BEFORE the retirement delete runs, a concurrent
        # import promotes episode 2 to ``imported`` at the database.
        await self._session.execute(  # pyright: ignore[reportPrivateUsage]
            update(SeasonEpisodeState)
            .where(
                SeasonEpisodeState.season_request_id == sr_id,
                SeasonEpisodeState.episode_number == 2,
            )
            .values(status=EpisodeState.imported, grabbed_download_id=download_id)
            .execution_options(synchronize_session=False)
        )
        return snapshot

    monkeypatch.setattr(
        SqlSeasonEpisodeStateRepository, "_existing_by_episode", _snapshot_then_lose_race
    )
    # TMDB retracted episode 2 -- the refresh would retire its (stale-pending)
    # row, but the concurrent import above wins the race.
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
    monkeypatch.undo()

    session.expire_all()  # drop stale identity-map state; re-read from the DB
    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    # THE pin: the just-imported row SURVIVED the retirement (pre-fix, the
    # ORM/PK delete removed it and lost the only imported breadcrumb).
    assert set(by_episode) == {1, 2}
    assert by_episode[2].status == "imported"
    assert by_episode[2].grabbed_download_id == download_id


async def test_upsert_target_lost_retirement_cas_expires_stale_instance(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #228: when the guarded retirement DELETE loses to a concurrent
    import promotion (rowcount 0), the stale ``pending`` ORM instance from the
    ``_existing_by_episode`` snapshot must be EXPIRED -- not left in the
    identity map. Mirrors ``test_upsert_target_retraction_loses_race_to_
    concurrent_import_promotion`` but deliberately WITHOUT the explicit
    ``session.expire_all()`` that test uses to sidestep the bug: a same-session
    follow-up read (``list_for_season``, exactly what ``reconcile_airing`` does
    via ``compute_missing``) must see the winner's ``imported`` status, not the
    stale ``pending`` snapshot. Pre-fix this assertion fails."""
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})

    real_snapshot = (
        SqlSeasonEpisodeStateRepository._existing_by_episode  # pyright: ignore[reportPrivateUsage]
    )

    async def _snapshot_then_lose_race(
        self: SqlSeasonEpisodeStateRepository, sr_id: int
    ) -> dict[int, SeasonEpisodeState]:
        snapshot = await real_snapshot(self, sr_id)
        # AFTER the snapshot (still reads episode 2 as ``pending``) but BEFORE
        # the retirement delete runs, a concurrent import promotes episode 2.
        await self._session.execute(  # pyright: ignore[reportPrivateUsage]
            update(SeasonEpisodeState)
            .where(
                SeasonEpisodeState.season_request_id == sr_id,
                SeasonEpisodeState.episode_number == 2,
            )
            .values(status=EpisodeState.imported, grabbed_download_id=download_id)
            .execution_options(synchronize_session=False)
        )
        return snapshot

    monkeypatch.setattr(
        SqlSeasonEpisodeStateRepository, "_existing_by_episode", _snapshot_then_lose_race
    )
    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
    monkeypatch.undo()

    # Deliberately NO session.expire_all() here -- the fix's own expire() must
    # be what makes this same-session read honest.
    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[2].status == "imported"
    assert by_episode[2].grabbed_download_id == download_id


async def test_upsert_target_retraction_never_touches_grabbed_or_imported_rows(
    session: AsyncSession,
) -> None:
    """Retraction only ever deletes never-progressed ``pending`` rows: a
    ``grabbed``/``imported`` row records real work/content (including a round-1
    adopted baseline, which is ``imported``) and is kept even when TMDB drops the
    episode from the aired set."""
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(
        season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8), 3: date(2026, 1, 15)}
    )
    await repo.mark_imported(season_request_id, [1], download_id=download_id)
    await repo.mark_grabbed(season_request_id, [2], download_id=download_id)

    # TMDB now reports NONE of them as aired -- only the pending row 3 may go.
    await repo.upsert_target(season_request_id, {})

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert set(by_episode) == {1, 2}
    assert by_episode[1].status == "imported"
    assert by_episode[2].status == "grabbed"


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


async def test_mark_imported_updates_winner_after_insert_race(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 fix (issue #178 review): if a concurrent ``refresh_target`` inserts the
    same episode between ``mark_imported``'s existing-row snapshot and its own
    insert, the insert loses the unique-index race and ``_insert_or_reread``
    returns ``None``. The winner must then be RE-READ and promoted to ``imported``
    -- never left ``pending``, which would let ``apply_import`` re-arm/search an
    episode that was just placed.
    """
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)

    # The race winner: a PENDING row a concurrent target refresh already committed.
    session.add(
        SeasonEpisodeState(
            season_request_id=season_request_id,
            episode_number=1,
            status=EpisodeState.pending,
            air_date=date(2026, 1, 1),
        )
    )
    await session.flush()

    repo = SqlSeasonEpisodeStateRepository(session)

    # Force the race: ``mark_imported``'s bulk pre-read misses the winner (as if the
    # concurrent insert committed AFTER this read but BEFORE our own insert), so it
    # takes the insert path, hits the unique index, and must fall back to the CAS
    # re-read. ``_reread_one`` is a SEPARATE fresh query, so it still sees the row.
    async def _blind(_self: SqlSeasonEpisodeStateRepository, _sr_id: int) -> dict[int, object]:
        return {}

    monkeypatch.setattr(SqlSeasonEpisodeStateRepository, "_existing_by_episode", _blind)

    await repo.mark_imported(season_request_id, [1], download_id=download_id)

    monkeypatch.undo()
    rows = await repo.list_for_season(season_request_id)
    assert len(rows) == 1
    assert rows[0].episode_number == 1
    assert rows[0].status == "imported"
    assert rows[0].grabbed_download_id == download_id


async def test_adopt_baseline_promotes_pending_rows_to_imported(session: AsyncSession) -> None:
    """Baseline adoption marks every pending row ``imported`` with no backing
    download -- an already-watchable season whose target was just seeded but
    which predates per-episode tracking (ADR-0020 §6)."""
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})
    await repo.adopt_baseline(season_request_id)

    rows = await repo.list_for_season(season_request_id)
    assert {r.episode_number for r in rows} == {1, 2}
    assert all(r.status == "imported" for r in rows)
    assert all(r.grabbed_download_id is None for r in rows)


async def test_adopt_baseline_subset_adopts_only_named_pending_rows(
    session: AsyncSession,
) -> None:
    """The partial-baseline shape (round 3): only the named episodes are adopted,
    and a ``grabbed`` row is NEVER adopted even when named -- it is evidence of
    our own attempt to fetch, i.e. evidence the episode was NOT already owned."""
    season_request_id = await _make_season(session)
    download_id = await _make_download(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    await repo.upsert_target(
        season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8), 3: date(2026, 1, 15)}
    )
    await repo.mark_grabbed(season_request_id, [2], download_id=download_id)

    await repo.adopt_baseline(season_request_id, episodes=[1, 2])

    rows = await repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert by_episode[1].status == "imported"  # named + pending -> adopted
    assert by_episode[2].status == "grabbed"  # named but grabbed -> untouched
    assert by_episode[3].status == "pending"  # not named -> untouched


async def test_stale_grabbed_episodes_flags_dead_or_missing_downloads_only(
    session: AsyncSession,
) -> None:
    """P2 (round 3): a ``grabbed`` row is stale iff its download is gone or
    terminally dead -- a live (or imported) download still represents real
    work/content and must keep counting toward the completion target."""
    season_request_id = await _make_season(session)
    repo = SqlSeasonEpisodeStateRepository(session)

    failed = Download(torrent_hash="stale-failed-hash", status="failed")
    live = Download(torrent_hash="stale-live-hash", status="downloading")
    done = Download(torrent_hash="stale-imported-hash", status="imported")
    orphan = Download(torrent_hash="stale-orphan-hash", status="failed")
    session.add_all([failed, live, done, orphan])
    await session.flush()

    await repo.mark_grabbed(season_request_id, [1], download_id=failed.id)
    await repo.mark_grabbed(season_request_id, [2], download_id=live.id)
    await repo.mark_grabbed(season_request_id, [3], download_id=done.id)
    # Episode 4: grab breadcrumb whose download row is later deleted (FK SET NULL).
    await repo.mark_grabbed(season_request_id, [4], download_id=orphan.id)
    await session.delete(orphan)
    await session.flush()

    assert await repo.stale_grabbed_episodes(season_request_id) == frozenset({1, 4})


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
