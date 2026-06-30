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
from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
)
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.services import queue_service
from plex_manager.services.import_service import (
    import_download,
    run_availability_cycle,
    run_import_cycle,
)
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


async def test_import_generic_file_under_release_folder_succeeds(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A folder torrent whose NAME carries the title/year/quality, containing a
    # generic feature file (movie.mkv). _resolve_source anchors the relative path
    # above the download root so the folder tokens reach the validator, whose
    # full-path parse identifies it — the import succeeds instead of blocking as
    # wrong/unknown media.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "movie.mkv")
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.completed


class _LosingRaceFs(LocalFileSystem):
    """A LocalFileSystem that always loses a placement race: on ``hardlink_or_copy``
    it finds ``dst`` already created by the 'winning' concurrent import (sized
    ``winner_size``) and raises ``FileExistsError`` — exactly what ``os.link`` raises
    on EEXIST when another import won the race."""

    def __init__(self, winner_size: int) -> None:
        self._winner_size = winner_size

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:  # type: ignore[override]
        _make_video(dst, self._winner_size)
        raise FileExistsError(str(dst))


async def _import_with_fs(
    sessionmaker_: SessionMaker,
    download_id: int,
    movies_root: Path,
    qbt: FakeQbittorrent,
    library: FakeLibrary,
    fs: LocalFileSystem,
) -> DownloadRecord | None:
    async with sessionmaker_() as session:
        return await import_download(
            download_id=download_id,
            fs=fs,
            library=library,
            qbt=qbt,
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(movies_root),
        )


class _ScanFailsLibrary(FakeLibrary):
    """A FakeLibrary whose targeted scan always fails with a transient Plex error,
    driving import_download into its scan-failure rollback branch."""

    async def trigger_scan(self, path: str) -> None:
        self.scanned.append(path)
        raise PlexLibraryError(f"plex scan failed for {path}")


async def test_scan_failure_after_lost_race_does_not_delete_winners_file(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # F3: the reconcile loop races the operator's retry. THIS import loses the
    # placement race (an identical, same-size file is already on disk), so it did
    # NOT create dst. The Plex scan then fails. The scan-failure rollback must NOT
    # unlink dst — that file belongs to the import that won the race; deleting it
    # orphans another successful import's content.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video, 60 * 1024 * 1024)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = _ScanFailsLibrary()

    record = await _import_with_fs(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        _LosingRaceFs(winner_size=60 * 1024 * 1024),
    )

    # Honest, retryable block — and the race winner's identical file survives intact.
    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists(), "scan-failure rollback orphaned a concurrent import's file"
    assert dst.stat().st_size == 60 * 1024 * 1024
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.import_blocked


async def test_scan_failure_after_real_placement_rolls_back_dst(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # The legitimate rollback the F3 fix must preserve: THIS import actually places
    # the file (real hardlink via LocalFileSystem), then the Plex scan fails. Because
    # this attempt created dst, it IS rolled back, so a later reject / re-search
    # can't orphan it in the library (the retry re-places it).
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = _ScanFailsLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert not dst.exists(), "a file THIS import placed must be rolled back on scan failure"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.import_blocked


async def test_import_idempotent_when_placement_race_lost_to_same_size(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # The operator's retry races the reconcile loop; this import's hardlink raises
    # EEXIST, but the winner already placed an identical (same-size) file. That is an
    # idempotent win — the import completes, it is NOT blocked.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video, 60 * 1024 * 1024)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import_with_fs(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        _LosingRaceFs(winner_size=60 * 1024 * 1024),
    )

    assert record is not None
    assert record.status == DownloadState.Imported.value
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.completed


async def test_import_blocks_when_placement_race_lost_to_different_size(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A DIFFERENT-sized file already at the destination after the race is a genuine
    # conflict (a user's manually-managed file) — surfaced as ImportBlocked, never
    # overwritten.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video, 60 * 1024 * 1024)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import_with_fs(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        _LosingRaceFs(winner_size=10 * 1024 * 1024),
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.import_blocked


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


async def test_run_import_cycle_drains_pending_download_to_completed(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # The drain phase imports every auto-drainable download (ImportPending here)
    # and promotes its request to 'completed' ("Finalizing"). It needs the download
    # client + the Movies root; availability is a separate pass.
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

    async with sessionmaker_() as session:
        await run_import_cycle(
            fs=LocalFileSystem(),
            library=library,
            qbt=_qbt(video),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(movies_root),
        )

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        assert download is not None and download.status == DownloadState.Imported.value
        assert request is not None and request.status == RequestStatus.completed


async def test_run_availability_cycle_promotes_completed_to_available_when_in_plex(
    sessionmaker_: SessionMaker,
) -> None:
    # A request already 'completed' (imported, scan triggered) is promoted to
    # 'available' only once Plex confirms it is indexed (honest two-phase). The
    # availability phase depends ONLY on Plex, so it runs without qBittorrent or
    # the Movies root.
    _download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.completed,
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available={_TMDB_ID})

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.available
        assert request.library_verified_at is not None


async def test_run_availability_cycle_leaves_completed_when_not_yet_in_plex(
    sessionmaker_: SessionMaker,
) -> None:
    _download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.completed,
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available=set())  # Plex has not indexed it yet

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed  # stays "Finalizing", honestly


async def test_crash_resume_rolls_back_orphaned_placement_on_scan_failure(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # F8: a crash struck AFTER _place_file created dst (and committed the download_path
    # breadcrumb) but BEFORE the Imported write. The row resumes as ``Importing`` with
    # dst already on disk (same size) and download_path == dst, so _place_file returns
    # placed=False (idempotent skip). THIS row placed the orphan and nothing ever
    # completed it, so a repeat scan failure must STILL roll dst back and clear the
    # breadcrumb — otherwise mark-failed / re-search orphans the library file.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video, 60 * 1024 * 1024)
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    _make_video(dst, 60 * 1024 * 1024)  # the prior (crashed) run's placement, same size

    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status=DownloadState.Importing.value,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=1999,
            download_path=str(dst),  # breadcrumb that survived the crash
        )
        session.add(download)
        await session.commit()
        download_id, request_id = download.id, request.id

    record = await _import(
        sessionmaker_, download_id, movies_root, _qbt(video), _ScanFailsLibrary()
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert not dst.exists(), "crash-orphaned placement must be rolled back on scan failure"
    assert record.download_path is None, "breadcrumb must be cleared once the file is gone"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.import_blocked


class _MarkFailedMidImportQbt(FakeQbittorrent):
    """A client whose ``get_status`` (called after import_download reads the row but
    BEFORE it claims ``Importing``) lets an operator's ``mark_failed`` land in a
    SEPARATE session — committing ``failed`` + blocklist + re-search during the long
    validation gap. Reproduces the F11 race: an unconditional ``Importing`` claim
    would overwrite that committed decision and copy/complete the rejected release."""

    def __init__(
        self, statuses: list[DownloadStatus], *, sessionmaker_: SessionMaker, download_id: int
    ) -> None:
        super().__init__(statuses)
        self._sessionmaker = sessionmaker_
        self._download_id = download_id
        self._fired = False

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        if not self._fired:
            self._fired = True
            async with self._sessionmaker() as session:
                await queue_service.mark_failed(
                    session, download_id=self._download_id, blocklist=True
                )
        return await super().get_status(info_hash)


async def test_import_does_not_overwrite_operator_mark_failed_during_gap(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # F11: the operator rejects the release (mark_failed -> blocklist + re-search) in a
    # separate session DURING import_download's validation gap. The conditional
    # ``Importing`` claim (compare-and-swap) must see the row is no longer resumable
    # and abort: it must NOT overwrite the committed ``failed`` state and must NOT
    # import the rejected release. The operator's correction is honored (north-star).
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    qbt = _MarkFailedMidImportQbt(
        _qbt(video).statuses, sessionmaker_=sessionmaker_, download_id=download_id
    )

    record = await _import(sessionmaker_, download_id, movies_root, qbt, FakeLibrary())

    # The operator's failed state stands; the release was NOT imported.
    assert record is not None
    assert record.status == DownloadState.Failed.value
    assert record.failed_reason == "marked failed by operator"
    assert not any(movies_root.iterdir())  # nothing copied into the library
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.searching  # operator's re-search stands
        blocklisted = (await session.execute(select(Blocklist))).scalars().all()
        assert len(blocklisted) == 1  # operator's blocklist stands
        download = await session.get(Download, download_id)
        assert download is not None and download.status == DownloadState.Failed.value


class _FlipToFailedDuringScanLibrary(FakeLibrary):
    """Forces the row out of ``Importing`` during the copy/scan window (a defensive,
    can't-happen-under-the-lock scenario: a mark_failed on an Importing row legally
    409s). Flips the DB row to ``failed`` in a separate session, then returns a normal
    scan, exercising the FINAL-transition compare-and-swap guard."""

    def __init__(self, *, sessionmaker_: SessionMaker, download_id: int) -> None:
        super().__init__()
        self._sessionmaker = sessionmaker_
        self._download_id = download_id

    async def trigger_scan(self, path: str) -> None:
        async with self._sessionmaker() as session:
            row = await session.get(Download, self._download_id)
            assert row is not None
            row.status = DownloadState.Failed.value
            row.failed_reason = "marked failed by operator"
            await session.commit()
        self.scanned.append(path)


async def test_final_import_transition_does_not_overwrite_a_concurrently_changed_row(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # Defense-in-depth for the copy/scan gap after the ``Importing`` claim: if the row
    # leaves ``Importing`` before the finalize, the conditional ``Imported`` CAS must
    # abandon the finalize — NOT overwrite the new state, and NOT mark the request
    # completed. It must also NOT delete dst (the deterministic path a retry re-adopts),
    # so a successfully-placed file is never destroyed on this defensive path.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = _FlipToFailedDuringScanLibrary(sessionmaker_=sessionmaker_, download_id=download_id)

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    assert record is not None
    assert record.status == DownloadState.Failed.value  # not overwritten to Imported
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists(), "the placed file must NOT be deleted when the finalize is abandoned"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status != RequestStatus.completed  # not completed over the change


async def test_blocked_import_blocklists_grabbed_release_title_not_file_basename(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # F12: import history must not shadow the grabbed RELEASE title. A folder torrent
    # carries the title (The.Matrix.1999...) but ships a generic movie.mkv; the import
    # writes import_started with a NULL source_title (basename kept only in ``message``).
    # When the import later blocks and the operator mark-fails + blocklists, the
    # blocklist row records the grabbed release title -- not ``movie.mkv`` -- so the
    # tier-2 (title+indexer) suppression keeps working for hashless candidates.
    release_title = "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    indexer = "FakeIndexer"
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / release_title
    _make_video(release_dir / "movie.mkv")
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    # Grab-time anchor: the ORIGINAL release title + indexer the blocklist must record.
    async with sessionmaker_() as session:
        session.add(
            DownloadHistory(
                tmdb_id=_TMDB_ID,
                torrent_hash=_HASH,
                event_type=DownloadHistoryEvent.grabbed,
                source_title=release_title,
                indexer=indexer,
            )
        )
        await session.commit()

    record = await _import_with_fs(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(release_dir),
        _ScanFailsLibrary(),
        LocalFileSystem(),
    )
    assert record is not None and record.status == DownloadState.ImportBlocked.value

    async with sessionmaker_() as session:
        started = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(DownloadHistory.torrent_hash == _HASH)
                    .where(DownloadHistory.event_type == DownloadHistoryEvent.import_started)
                )
            )
            .scalars()
            .all()
        )
    assert started and all(e.source_title is None for e in started)
    assert all("movie.mkv" in (e.message or "") for e in started)

    async with sessionmaker_() as session:
        await queue_service.mark_failed(session, download_id=download_id, blocklist=True)

    async with sessionmaker_() as session:
        entry = (await session.execute(select(Blocklist))).scalar_one()
    assert entry.source_title == release_title
    assert entry.source_title != "movie.mkv"
    assert entry.indexer == indexer  # title+indexer tier stays effective for hashless feeds


async def test_import_history_events_keep_basename_in_message_not_source_title(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # Both import history events (import_started + imported) carry a NULL source_title
    # with the file basename preserved in ``message`` -- so neither can shadow the
    # grabbed release title the blocklist relies on (F12).
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "movie.mkv")
    download_id, _ = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )

    record = await _import(
        sessionmaker_, download_id, movies_root, _qbt(release_dir), FakeLibrary()
    )
    assert record is not None and record.status == DownloadState.Imported.value

    async with sessionmaker_() as session:
        events = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(DownloadHistory.torrent_hash == _HASH)
                    .order_by(DownloadHistory.id)
                )
            )
            .scalars()
            .all()
        )
    by_type = {e.event_type: e for e in events}
    started = by_type[DownloadHistoryEvent.import_started]
    imported = by_type[DownloadHistoryEvent.imported]
    assert started.source_title is None and "movie.mkv" in (started.message or "")
    assert imported.source_title is None and "movie.mkv" in (imported.message or "")
