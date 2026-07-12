"""``_import_tv_locked``'s conditional completeness (ADR-0020, issue #178).

Covers the episode-level fallback's import-side half: a season whose target is
known (``season_episode_states`` was seeded, e.g. by auto-grab's Pass-2 fallback)
only completes when the target is fully imported; an unseeded (legacy) season
completes on any TV import, exactly as before this feature existed.

Mirrors ``test_import_service.py``'s TV helpers (``_seed_tv``/``_import_tv``/``_qbt``/
``_make_video``), duplicated here (not imported) to keep this file independently
readable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Blocklist,
    Download,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.metadata import EpisodeInfo
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.services import auto_grab_service
from plex_manager.services.import_service import import_download
from tests.web.fakes import FakeLibrary, FakeMediaProbe, FakeProwlarr, FakeQbittorrent, FakeTmdb
from tests.web.fakes import candidate as make_candidate

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 950


def _make_video(path: Path, size_bytes: int = 60 * 1024 * 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(size_bytes - 1)
        handle.write(b"\0")


def _qbt(torrent_hash: str, content_path: Path) -> FakeQbittorrent:
    return FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=torrent_hash,
                name=content_path.name,
                raw_state="stalledUP",
                progress=1.0,
                save_path=str(content_path.parent),
                content_path=str(content_path),
            )
        ]
    )


async def _seed_tv_download(
    sessionmaker_: SessionMaker,
    *,
    torrent_hash: str,
    season: int,
    season_request_id: int | None = None,
    episodes: list[int] | None = None,
) -> tuple[int, int, int]:
    """Insert a tv request (+ season row, unless ``season_request_id`` reuses an
    existing one) + a download for that season. Returns ``(download_id,
    request_id, season_request_id)``."""
    async with sessionmaker_() as session:
        if season_request_id is None:
            request = MediaRequest(
                tmdb_id=_TMDB_ID,
                media_type=MediaType.tv,
                title="Some Show",
                year=2020,
                status=RequestStatus.downloading,
            )
            session.add(request)
            await session.flush()
            season_row = SeasonRequest(
                media_request_id=request.id, season_number=season, status="downloading"
            )
            session.add(season_row)
            await session.flush()
            request_id = request.id
            resolved_season_request_id = season_row.id
        else:
            season_row = await session.get(SeasonRequest, season_request_id)
            assert season_row is not None
            request_id = season_row.media_request_id
            resolved_season_request_id = season_request_id
        download = Download(
            torrent_hash=torrent_hash,
            status=DownloadState.ImportPending.value,
            media_request_id=request_id,
            tmdb_id=_TMDB_ID,
            year=2020,
            season=season,
            episodes_json=episodes,
        )
        session.add(download)
        await session.commit()
        return download.id, request_id, resolved_season_request_id


async def _import_tv(
    sessionmaker_: SessionMaker,
    download_id: int,
    tv_root: Path,
    qbt: FakeQbittorrent,
) -> None:
    async with sessionmaker_() as session:
        await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=FakeLibrary(),
            qbt=qbt,
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root="/unused",
            tv_root=str(tv_root),
        )


async def _seed_target(
    sessionmaker_: SessionMaker, season_request_id: int, episode_numbers: list[int]
) -> None:
    async with sessionmaker_() as session:
        repo = SqlSeasonEpisodeStateRepository(session)
        await repo.upsert_target(season_request_id, {n: date(2026, 1, 1) for n in episode_numbers})
        await session.commit()


async def _mark_imported(
    sessionmaker_: SessionMaker, season_request_id: int, episode_number: int
) -> None:
    async with sessionmaker_() as session:
        download = Download(
            torrent_hash=f"pre-imported-{season_request_id}-{episode_number}", status="imported"
        )
        session.add(download)
        await session.commit()
        repo = SqlSeasonEpisodeStateRepository(session)
        await repo.mark_imported(season_request_id, [episode_number], download_id=download.id)
        await session.commit()


async def test_partial_import_does_not_complete_and_rearms_searching(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP.mkv")

    download_id, request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash="partial-hash", season=2, episodes=[4]
    )
    await _seed_target(sessionmaker_, season_request_id, [4, 5])

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("partial-hash", release_dir))

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.searching
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.searching
        download = await session.get(Download, download_id)
        assert download is not None
        assert download.status == DownloadState.Imported.value

        repo = SqlSeasonEpisodeStateRepository(session)
        states = await repo.list_for_season(season_request_id)
    by_episode = {s.episode_number: s.status for s in states}
    assert by_episode[4] == "imported"
    assert by_episode[5] == "pending"


async def test_final_episode_completes_the_season(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02E05.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E05.1080p.WEB-DL.x264-GRP.mkv")

    download_id, request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash="final-hash", season=2, episodes=[5]
    )
    await _seed_target(sessionmaker_, season_request_id, [4, 5])
    await _mark_imported(sessionmaker_, season_request_id, 4)

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("final-hash", release_dir))

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.completed
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed

        repo = SqlSeasonEpisodeStateRepository(session)
        states = await repo.list_for_season(season_request_id)
    by_episode = {s.episode_number: s.status for s in states}
    assert by_episode == {4: "imported", 5: "imported"}


async def test_legacy_pack_with_no_seeded_target_completes_unchanged(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S03.1080p.WEB-DL.x264-GRP"
    for episode in range(1, 4):
        _make_video(release_dir / f"Some.Show.S03E{episode:02d}.1080p.WEB-DL.x264-GRP.mkv")

    download_id, request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash="legacy-pack-hash", season=3
    )
    # No season_episode_states seeded at all -- the legacy, pre-feature shape.

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("legacy-pack-hash", release_dir))

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.completed
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed

        repo = SqlSeasonEpisodeStateRepository(session)
        states = await repo.list_for_season(season_request_id)
    assert {s.episode_number for s in states} == {1, 2, 3}
    assert all(s.status == "imported" for s in states)


async def test_pack_with_a_hole_does_not_complete_and_leaves_the_hole_pending(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S01E02.1080p.WEB-DL.x264-GRP.mkv")
    # Deliberately no episode 3 file -- a "bad pack" missing an episode.

    download_id, request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash="holey-pack-hash", season=1
    )
    await _seed_target(sessionmaker_, season_request_id, [1, 2, 3])

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("holey-pack-hash", release_dir))

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.searching
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.searching

        repo = SqlSeasonEpisodeStateRepository(session)
        states = await repo.list_for_season(season_request_id)
        # P1 (issue #178 review round 2): the incomplete PACK must be blocklisted
        # in the same transaction as the re-arm, so the next cycle's Pass 1 cannot
        # re-accept the identical pack and starve the episode fallback forever.
        blocked = (
            (await session.execute(select(Blocklist).where(Blocklist.tmdb_id == _TMDB_ID)))
            .scalars()
            .all()
        )
    by_episode = {s.episode_number: s.status for s in states}
    assert by_episode[1] == "imported"
    assert by_episode[2] == "imported"
    assert by_episode[3] == "pending"
    assert len(blocked) == 1
    assert blocked[0].torrent_hash == "holey-pack-hash"
    assert blocked[0].media_type == MediaType.tv
    assert blocked[0].reason.value == "failed"


async def test_episode_scoped_partial_import_is_not_blocklisted(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """The recycle terminator must ONLY fire for packs: an episode-scoped grab
    (``episodes`` named on the download) that leaves the season incomplete
    delivered exactly what it promised -- blocklisting it would be dishonest, and
    Pass 2's overlap gate already prevents its redundant re-grab."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP.mkv")

    download_id, _request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash="honest-episode-hash", season=2, episodes=[4]
    )
    await _seed_target(sessionmaker_, season_request_id, [4, 5])

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("honest-episode-hash", release_dir))

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.searching  # re-armed, still collecting
        blocked = (
            (await session.execute(select(Blocklist).where(Blocklist.tmdb_id == _TMDB_ID)))
            .scalars()
            .all()
        )
    assert blocked == []


async def test_incomplete_pack_cannot_recycle_next_cycle_falls_back_to_missing_episode(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """P1 regression (issue #178 review round 2): the EXACT recycle. A pack
    covering only episodes 1-2 of target {1,2,3} imports; pre-fix the season
    re-armed as a plain whole-season search, next cycle's Pass 1 re-accepted the
    SAME pack (``pass1_found_pack`` then skipped Pass 2), and the worker re-grabbed
    the identical incomplete pack forever while episode 3 was never searched for.

    Post-fix: the import blocklists the pack, so with Prowlarr still offering the
    SAME pack (same info-hash) next cycle, Pass 1 must reject it, Pass 2 must run,
    and the grab must be the MISSING EPISODE 3 -- the loop terminates in one cycle.
    """
    pack_hash = "ab" * 20
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S01E02.1080p.WEB-DL.x264-GRP.mkv")
    # No episode 3 file -- the incomplete pack.

    download_id, request_id, season_request_id = await _seed_tv_download(
        sessionmaker_, torrent_hash=pack_hash, season=1
    )
    await _seed_target(sessionmaker_, season_request_id, [1, 2, 3])
    await _import_tv(sessionmaker_, download_id, tv_root, _qbt(pack_hash, release_dir))

    # Next auto-grab cycle: Prowlarr STILL returns the identical pack (recycle
    # bait) alongside a single-episode release for the missing episode 3.
    prowlarr = FakeProwlarr(
        [
            make_candidate("Some.Show.S01.1080p.WEB-DL.x264-GRP", info_hash=pack_hash),
            make_candidate("Some.Show.S01E03.1080p.WEB-DL.x264-GRP", info_hash="cd" * 20),
        ]
    )
    metadata = FakeTmdb(
        season_episodes={
            (_TMDB_ID, 1): [
                EpisodeInfo(episode_number=n, air_date=date(2026, 1, 1)) for n in (1, 2, 3)
            ]
        }
    )
    async with sessionmaker_() as session:
        result = await auto_grab_service.run_grab_cycle(
            session,
            prowlarr=prowlarr,  # type: ignore[arg-type]  # a fake IndexerPort
            parser=GuessitParser(),
            profile=default_profile(),
            qbt=FakeQbittorrent(),
            metadata=metadata,  # type: ignore[arg-type]  # a fake MetadataPort
            now=datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC),
        )

    # The pack was NOT re-grabbed; the fallback grabbed exactly episode 3.
    assert result.grabbed == 1
    assert result.season_episode_fallback_grabs == 1
    async with sessionmaker_() as session:
        new_download = (
            (
                await session.execute(
                    select(Download)
                    .where(Download.media_request_id == request_id)
                    .where(Download.id != download_id)
                )
            )
            .scalars()
            .one()
        )
        assert new_download.episodes_json == [3]
        assert new_download.torrent_hash == "cd" * 20  # the episode, never the pack
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.downloading


async def _seed_multi_season_download(
    sessionmaker_: SessionMaker, *, torrent_hash: str
) -> tuple[int, int, int, int]:
    """A whole-show style download with TWO DownloadScope rows (seasons 1 + 2).
    Returns ``(download_id, request_id, season_1_id, season_2_id)``."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=RequestStatus.downloading,
            tv_request_mode="whole_show",
        )
        session.add(request)
        await session.flush()
        season_1 = SeasonRequest(
            media_request_id=request.id, season_number=1, status=RequestStatus.downloading.value
        )
        season_2 = SeasonRequest(
            media_request_id=request.id, season_number=2, status=RequestStatus.downloading.value
        )
        session.add_all([season_1, season_2])
        await session.flush()
        download = Download(
            torrent_hash=torrent_hash,
            status=DownloadState.ImportPending.value,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=2020,
            season=1,
        )
        session.add(download)
        await session.flush()
        session.add_all(
            [
                DownloadScope(
                    download_id=download.id,
                    media_request_id=request.id,
                    season_request_id=season_1.id,
                    season_number=1,
                    scope_key="season:1|episodes:*",
                    status="active",
                ),
                DownloadScope(
                    download_id=download.id,
                    media_request_id=request.id,
                    season_request_id=season_2.id,
                    season_number=2,
                    scope_key="season:2|episodes:*",
                    status="active",
                ),
            ]
        )
        await session.commit()
        return download.id, request.id, season_1.id, season_2.id


async def test_multi_season_pack_marks_prior_fallback_target_rows_imported(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """P2 regression (issue #178 review round 2): the multi-season pack path
    (``_import_tv_targets_locked``) must run the SAME episode-state completeness
    logic as the single-season path. Pre-fix it called ``mark_completed`` without
    ``apply_import``, so target rows a prior fallback cycle seeded stayed
    ``pending`` and the next airing refresh saw ``target > imported`` and re-armed
    an already-imported season for duplicate grabs.
    """
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01-S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S01E02.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")

    download_id, _request_id, season_1_id, season_2_id = await _seed_multi_season_download(
        sessionmaker_, torrent_hash="multi-pack-hash"
    )
    # Season 1 carries target rows from a prior episode-fallback cycle.
    await _seed_target(sessionmaker_, season_1_id, [1, 2])

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("multi-pack-hash", release_dir))

    async with sessionmaker_() as session:
        season_1 = await session.get(SeasonRequest, season_1_id)
        season_2 = await session.get(SeasonRequest, season_2_id)
        assert season_1 is not None and season_1.status == RequestStatus.completed
        assert season_2 is not None and season_2.status == RequestStatus.completed
        repo = SqlSeasonEpisodeStateRepository(session)
        season_1_states = await repo.list_for_season(season_1_id)
        blocked = (
            (await session.execute(select(Blocklist).where(Blocklist.tmdb_id == _TMDB_ID)))
            .scalars()
            .all()
        )
    # THE pin: the fallback-seeded rows are now ``imported`` -- a later airing
    # refresh sees target <= imported and leaves the season alone.
    assert {s.episode_number: s.status for s in season_1_states} == {
        1: "imported",
        2: "imported",
    }
    assert blocked == []  # a fully-covering pack is never blocklisted


async def test_multi_season_pack_with_a_hole_rearms_that_season_and_blocklists_the_pack(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """Fix 1 x fix 2 composition: a multi-season pack is a PACK for every season
    it carries, so a season it cannot complete must re-arm AND block the release
    identity (one entry per download) -- otherwise the whole-show recycle is the
    same loop the single-season path had. The fully-covered sibling season still
    completes normally.
    """
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01-S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S01E02.1080p.WEB-DL.x264-GRP.mkv")
    # Season 1's episode 3 is missing from the pack.
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")

    download_id, _request_id, season_1_id, season_2_id = await _seed_multi_season_download(
        sessionmaker_, torrent_hash="multi-holey-hash"
    )
    await _seed_target(sessionmaker_, season_1_id, [1, 2, 3])
    await _seed_target(sessionmaker_, season_2_id, [1])

    await _import_tv(sessionmaker_, download_id, tv_root, _qbt("multi-holey-hash", release_dir))

    async with sessionmaker_() as session:
        season_1 = await session.get(SeasonRequest, season_1_id)
        season_2 = await session.get(SeasonRequest, season_2_id)
        assert season_1 is not None and season_1.status == RequestStatus.searching
        assert season_2 is not None and season_2.status == RequestStatus.completed
        repo = SqlSeasonEpisodeStateRepository(session)
        season_1_states = await repo.list_for_season(season_1_id)
        blocked = (
            (await session.execute(select(Blocklist).where(Blocklist.tmdb_id == _TMDB_ID)))
            .scalars()
            .all()
        )
    assert {s.episode_number: s.status for s in season_1_states} == {
        1: "imported",
        2: "imported",
        3: "pending",
    }
    assert len(blocked) == 1  # ONE entry per download, not one per incomplete season
    assert blocked[0].torrent_hash == "multi-holey-hash"
