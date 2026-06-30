"""Import pipeline — validate the completed file, place it, scan, mark completed.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` (hardlink stays on one
filesystem) and the REAL parser + default quality profile, so the CAM/wrong-media
gate is genuinely exercised. The download client and Plex library are faked. Video
files are created sparse so a >50 MiB feature (above the sample floor) costs no
real bytes.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import Blocklist, Download, MediaRequest, MediaType, RequestStatus
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.services import queue_service
from plex_manager.services.import_service import import_download, run_import_cycle
from tests.web.fakes import FakeLibrary, FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 603
_HASH = "deadbeef01"


def _make_video(path: Path, size_bytes: int = 60 * 1024 * 1024) -> None:
    """Create a sparse video file of ``size_bytes`` (above the 50 MiB sample floor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(size_bytes - 1)
        handle.write(b"\0")


async def _seed(
    sessionmaker_: SessionMaker, *, request_status: RequestStatus, download_status: str
) -> tuple[int, int]:
    """Insert a movie request + a tracked download; return ``(download_id, request_id)``."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=request_status,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status=download_status,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=1999,
        )
        session.add(download)
        await session.commit()
        return download.id, request.id


def _qbt(content_path: Path) -> FakeQbittorrent:
    return FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=content_path.name,
                raw_state="stalledUP",
                content_path=str(content_path),
            )
        ]
    )


async def _import(
    sessionmaker_: SessionMaker,
    download_id: int,
    movies_root: Path,
    qbt: FakeQbittorrent,
    library: FakeLibrary,
) -> DownloadRecord | None:
    async with sessionmaker_() as session:
        return await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            library=library,
            qbt=qbt,
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(movies_root),
        )


async def test_import_happy_path_places_file_scans_and_marks_completed(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    assert record.download_path == str(dst)
    assert library.scanned == [str(dst.parent)]
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed  # "Finalizing", not yet available
        assert request.completed_at is not None


async def test_import_rejects_cam_as_blocked_not_imported(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.CAM.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "quality_not_wanted" in record.failed_reason
    # Nothing was imported into the library.
    assert not any(movies_root.iterdir())
    # The owning request is moved to the honest, retryable import_blocked state —
    # never left lying as 'downloading' while nothing is downloading.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.import_blocked


async def test_mark_failed_recovers_a_blocked_import(sessionmaker_: SessionMaker) -> None:
    # Correction without a terminal: an ImportBlocked download can be rejected via
    # mark-failed -> blocklist + re-search (the P1 the review caught, where
    # ImportBlocked had no legal FailedPending edge and the request stranded).
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.import_blocked,
        download_status=DownloadState.ImportBlocked.value,
    )

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(session, download_id=download_id, blocklist=True)
    assert record.status == DownloadState.Failed.value

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.searching  # re-armed for a fresh search
        blocklisted = (await session.execute(select(Blocklist))).scalars().all()
        assert len(blocklisted) == 1  # the bad release was blocklisted


async def test_import_with_no_video_file_is_blocked(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    content_dir = tmp_path / "downloads" / "junk"
    content_dir.mkdir(parents=True)
    (content_dir / "readme.txt").write_text("no video here")
    download_id, _ = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )

    record = await _import(
        sessionmaker_, download_id, movies_root, _qbt(content_dir), FakeLibrary()
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "no video file" in record.failed_reason


async def test_import_is_idempotent_on_an_already_imported_row(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _ = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    first = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)
    assert first is not None and first.status == DownloadState.Imported.value
    # Re-running on the Imported row is a no-op: no second scan, still Imported.
    second = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    assert second is not None
    assert second.status == DownloadState.Imported.value
    assert library.scanned == [str(movies_root / "The Matrix (1999)")]


async def test_run_import_cycle_promotes_completed_to_available_when_in_plex(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A request already 'completed' (imported, scan triggered) is promoted to
    # 'available' only once Plex confirms it is indexed (honest two-phase).
    _download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.completed,
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available={_TMDB_ID})

    async with sessionmaker_() as session:
        await run_import_cycle(
            fs=LocalFileSystem(),
            library=library,
            qbt=FakeQbittorrent(),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(tmp_path),
        )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.available
        assert request.library_verified_at is not None


async def test_run_import_cycle_leaves_completed_when_not_yet_in_plex(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    _download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.completed,
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available=set())  # Plex has not indexed it yet

    async with sessionmaker_() as session:
        await run_import_cycle(
            fs=LocalFileSystem(),
            library=library,
            qbt=FakeQbittorrent(),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(tmp_path),
        )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed  # stays "Finalizing", honestly
