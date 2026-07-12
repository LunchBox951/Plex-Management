"""``_import_tv_locked``'s conditional completeness (ADR-0018, issue #178).

Covers the episode-level fallback's import-side half: a season whose target is
known (``season_episode_states`` was seeded, e.g. by auto-grab's Pass-2 fallback)
only completes when the target is fully imported; an unseeded (legacy) season
completes on any TV import, exactly as before this feature existed.

Mirrors ``test_import_service.py``'s TV helpers (``_seed_tv``/``_import_tv``/``_qbt``/
``_make_video``), duplicated here (not imported) to keep this file independently
readable.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.services.import_service import import_download
from tests.web.fakes import FakeLibrary, FakeMediaProbe, FakeQbittorrent

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
    by_episode = {s.episode_number: s.status for s in states}
    assert by_episode[1] == "imported"
    assert by_episode[2] == "imported"
    assert by_episode[3] == "pending"
