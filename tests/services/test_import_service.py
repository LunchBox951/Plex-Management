"""Import pipeline — validate the completed file, place it, scan, mark completed.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` (hardlink stays on one
filesystem) and the REAL parser + default quality profile, so the CAM/wrong-media
gate is genuinely exercised. The download client and Plex library are faked. Video
files are created sparse so a >50 MiB feature (above the sample floor) costs no
real bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
    User,
)
from plex_manager.ports.download_client import DownloadedFile, DownloadStatus
from plex_manager.ports.library import WatchState
from plex_manager.ports.media_probe import (
    MediaProbePort,
    MediaProbeResult,
    MediaProbeUnavailableError,
)
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import (
    eviction_service,
    import_service,
    path_visibility,
    purge_service,
    queue_service,
)
from plex_manager.services.import_service import (
    import_download,
    run_availability_cycle,
    run_import_cycle,
)
from tests.web.fakes import FakeLibrary, FakeMediaProbe, FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 603
_HASH = "deadbeef01"


@pytest.fixture(autouse=True)
def reset_bounded_finalizing_state() -> Iterator[None]:
    """Clear the bounded-Finalizing (#158) in-memory bookkeeping between tests.

    ``import_service``'s duty-cycle/first-seen bookkeeping is process-GLOBAL,
    keyed by a request/season id -- and each test's in-memory DB
    (``tests/services/conftest.py``'s ``engine`` fixture) is FRESH, so
    autoincrement ids restart at 1 every test. Without this reset, a duty-cycle
    bucket (or a first-observed-miss anchor) recorded by one test would leak
    into an unrelated later test that happens to reuse the same id. Mirrors
    ``adapters.plex.library``'s ``reset_caches`` fixture pattern
    (``tests/adapters/plex/test_plex_library.py``).
    """
    import_service.reset_unconfirmed_tracking()
    yield
    import_service.reset_unconfirmed_tracking()


def _make_video(path: Path, size_bytes: int = 60 * 1024 * 1024) -> None:
    """Create a sparse video file of ``size_bytes`` (above the 50 MiB sample floor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(size_bytes - 1)
        handle.write(b"\0")


async def _seed(
    sessionmaker_: SessionMaker,
    *,
    request_status: RequestStatus,
    download_status: str,
    is_anime: bool = False,
) -> tuple[int, int]:
    """Insert a movie request + a tracked download; return ``(download_id, request_id)``."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="The Matrix",
            year=1999,
            status=request_status,
            is_anime=is_anime,
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


def _qbt(content_path: Path, *, files: list[DownloadedFile] | None = None) -> FakeQbittorrent:
    """Fake client reporting ``content_path`` under its parent as save_path.

    ``files`` (save-path-relative name + exact size) is the torrent's own file
    list, the PROOF a host->container content remap must exhibit on disk (round
    3); tests that exercise the remap must supply it, same as the real client.
    """
    return FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=content_path.name,
                raw_state="stalledUP",
                progress=1.0,
                save_path=str(content_path.parent),
                content_path=str(content_path),
            )
        ],
        files={_HASH: files} if files is not None else None,
    )


async def _import(
    sessionmaker_: SessionMaker,
    download_id: int,
    movies_root: Path,
    qbt: FakeQbittorrent,
    library: FakeLibrary,
    *,
    anime_movie_root: Path | None = None,
    media_probe: MediaProbePort | None = None,
) -> DownloadRecord | None:
    async with sessionmaker_() as session:
        return await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=media_probe or FakeMediaProbe(),
            library=library,
            qbt=qbt,
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(movies_root),
            anime_movie_root=str(anime_movie_root) if anime_movie_root is not None else None,
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


async def test_import_persists_library_path_and_a_later_sweep_reclaims_it(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """C1 regression: import finalize must persist ``MediaRequest.library_path``
    (the movie's own placed folder) -- proven end-to-end by running a REAL
    eviction sweep straight after import and confirming it actually finds and
    deletes the placed directory. Before the fix, ``library_path`` stayed
    ``None`` forever, so ``eviction_service._movie_candidates`` had no deletion
    target and disk-pressure eviction reclaimed nothing regardless of watch
    state (see ``eviction_service._evict_one``'s "no stored library_path
    breadcrumb" skip)."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), FakeLibrary())
    assert record is not None and record.status == DownloadState.Imported.value

    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        # The breadcrumb is the movie's own folder (what fs.delete() removes on
        # eviction), not the file itself.
        assert request.library_path == str(dst.parent)

    # Confirm availability (Plex-side "Finalizing" -> "available"), then run a
    # real eviction sweep against a watched, past-grace copy -- proving the
    # persisted breadcrumb is exactly what the eviction candidate builder reads.
    async with sessionmaker_() as session:
        await run_availability_cycle(library=FakeLibrary(available={_TMDB_ID}), session=session)

    stale_library = FakeLibrary(
        watch_states={
            (_TMDB_ID, "movie", None): WatchState(
                watched=True, last_viewed_at=datetime.now(UTC) - timedelta(days=999)
            )
        }
    )
    fs = LocalFileSystem(library_roots=[str(movies_root)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=stale_library,
            fs=fs,
            media_type="movie",
            root_path=str(movies_root),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=30,
        )

    assert [o.title for o in outcomes] == ["The Matrix"]
    assert not dst.parent.exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.evicted


async def test_import_blocks_content_path_without_save_path(
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
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=video.name,
                raw_state="stalledUP",
                progress=1.0,
                content_path=str(video),
            )
        ]
    )

    record = await _import(sessionmaker_, download_id, movies_root, qbt, FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason == "download client reported content path without save path"
    assert not any(movies_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status == RequestStatus.import_blocked


async def test_import_retry_success_clears_stale_failed_reason(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.import_blocked,
        download_status=DownloadState.ImportBlocked.value,
    )
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.failed_reason = "stale block reason"
        await session.commit()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.Imported.value
    assert record.failed_reason is None
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None
    assert download.status == DownloadState.Imported.value
    assert download.failed_reason is None


async def test_import_defers_when_live_client_status_is_not_settled(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.import_blocked,
        download_status=DownloadState.ImportBlocked.value,
    )
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.failed_reason = "stale import block"
        await session.commit()
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=video.name,
                raw_state="moving",
                progress=0.5,
                ratio=0.25,
                save_path=str(video.parent),
                content_path=str(video),
            )
        ]
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, qbt, library)

    assert record is not None
    assert record.status == DownloadState.Downloading.value
    assert record.failed_reason is None
    assert record.progress == 0.5
    assert record.seed_ratio == 0.25
    assert library.scanned == []
    assert not any(movies_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status == RequestStatus.downloading


async def test_import_generic_file_under_release_folder_succeeds(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A folder torrent whose NAME carries the title/year/quality, containing a
    # generic feature file (movie.mkv). _resolve_sources anchors the relative path
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


async def test_import_movie_selects_real_feature_over_larger_featurette(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # issue #69: a movie download that ships a LARGER featurette/extra beside the
    # real feature must import the feature, not block as NO_VIDEO_FILE. The old
    # service narrowed to the single largest file BEFORE validating, so the 200 MiB
    # featurette was the only file the validator saw -- it dropped that sample-named
    # file and reported "no importable video". Now EVERY candidate reaches the
    # validator, which drops the featurette by name and picks the smaller real
    # feature as the surviving largest.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    feature = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    featurette = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.featurette.mkv"
    _make_video(feature, size_bytes=100 * 1024 * 1024)
    _make_video(featurette, size_bytes=200 * 1024 * 1024)  # larger than the feature
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    assert record.failed_reason is None
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    # It imported the real feature (100 MiB), never the larger featurette decoy.
    assert dst.stat().st_size == 100 * 1024 * 1024
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.completed


class _LosingRaceFs(LocalFileSystem):
    """A LocalFileSystem that always loses a placement race: on ``hardlink_or_copy``
    it finds ``dst`` already created by the 'winning' concurrent import (sized
    ``winner_size``) and raises ``FileExistsError`` — exactly what ``os.link`` raises
    on EEXIST when another import won the race."""

    def __init__(self, winner_size: int) -> None:
        super().__init__()
        self._winner_size = winner_size

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:  # type: ignore[override]
        _make_video(dst, self._winner_size)
        raise FileExistsError(str(dst))


class _WrongSameSizeFs(LocalFileSystem):
    """Loses placement to a same-size but different file."""

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:  # type: ignore[override]
        dst.parent.mkdir(parents=True, exist_ok=True)
        size = os.path.getsize(src)
        with dst.open("wb") as handle:
            handle.seek(size - 1)
            handle.write(b"x")
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
            media_probe=FakeMediaProbe(),
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

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        self.scanned.append(path)
        self.scan_calls.append((path, media_type))
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


async def test_scan_failure_never_deletes_unproven_identical_destination_file(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A resumed ``Importing`` row with NO download_path breadcrumb finds an
    # IDENTICAL file already at dst. That file cannot be proven ours: it is
    # byte-for-byte indistinguishable from a user's manually-placed copy (or a
    # prior retry's winner) — the old ``recovered_orphan`` inference treated it as
    # our crash orphan and unlinked it when the Plex scan failed transiently,
    # deleting an unowned library file. Ownership must come only from placing the
    # file THIS invocation or from the durable breadcrumb; content-match alone
    # NEVER authorizes a rollback, so the file must survive the scan failure.
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    _make_video(dst)  # identical content, NOT placed by this import, no breadcrumb
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.Importing.value,
    )
    library = _ScanFailsLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    # Honest, retryable block — and the unproven identical file survives intact.
    # (A genuinely-ours crash orphan in the place→breadcrumb window looks the same
    # and also survives: it is re-adopted by the next successful retry; deleting
    # nothing beats maybe-deleting the user's file.)
    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.download_path is None
    assert dst.exists(), "scan-failure rollback deleted a file this import never placed"
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


async def test_import_blocks_when_placement_race_lost_to_same_size_different_content(
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

    record = await _import_with_fs(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        _WrongSameSizeFs(),
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "different content" in record.failed_reason
    assert library.scanned == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None and request.status == RequestStatus.import_blocked


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
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=True
        )
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
    assert "no Plex-supported video file" in record.failed_reason


async def test_import_ignores_non_video_siblings_and_associates_only_verified_video(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """Positive acceptance: unrelated payload siblings never enter the library."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    video = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    (release_dir / "setup.exe").write_bytes(b"not media")
    (release_dir / "release.nfo").write_text("metadata")
    (release_dir / "poster.jpg").write_bytes(b"image")
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(release_dir), library)

    assert record is not None and record.status == DownloadState.Imported.value
    movie_dir = movies_root / "The Matrix (1999)"
    assert [path.name for path in movie_dir.iterdir()] == ["The Matrix (1999).mkv"]
    assert library.scanned == [str(movie_dir)]


async def test_import_rejects_renamed_non_video_before_library_association(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()
    probe = FakeMediaProbe(rejected={video.name: "file could not be parsed as video"})

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "no verified Plex-compatible video file" in record.failed_reason
    assert library.scanned == []
    assert not any(movies_root.iterdir())


async def test_import_uses_verified_feature_when_larger_video_candidate_is_invalid(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    valid = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    invalid = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.PROPER.mkv"
    _make_video(valid, size_bytes=80 * 1024 * 1024)
    _make_video(invalid, size_bytes=120 * 1024 * 1024)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    probe = FakeMediaProbe(rejected={invalid.name: "container does not match suffix"})

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(release_dir),
        FakeLibrary(),
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.stat().st_size == 80 * 1024 * 1024


async def test_import_uses_verified_movie_when_sibling_probe_is_unavailable(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    unavailable = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.PROPER.mkv"
    accepted = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(unavailable, size_bytes=120 * 1024 * 1024)
    _make_video(accepted, size_bytes=80 * 1024 * 1024)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    probe = FakeMediaProbe(unavailable={unavailable.name: "ffprobe worker unavailable"})

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(release_dir),
        FakeLibrary(),
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.Imported.value
    assert {path.name for path in probe.calls} == {accepted.name, unavailable.name}
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.stat().st_size == 80 * 1024 * 1024


@pytest.mark.parametrize(
    "download_status",
    [DownloadState.ImportPending.value, DownloadState.ImportBlocked.value],
)
async def test_import_probe_outage_blocks_retryably_before_library_association(
    tmp_path: Path, sessionmaker_: SessionMaker, download_status: str
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=download_status,
    )
    library = FakeLibrary()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        media_probe=FakeMediaProbe(raises=MediaProbeUnavailableError("ffprobe timed out")),
    )

    assert record is not None and record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason == "video verification unavailable: ffprobe timed out"
    assert library.scanned == []
    assert not any(movies_root.iterdir())


async def test_import_probe_outage_keeps_crash_resumed_movie_auto_retryable(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.Importing.value,
    )
    library = FakeLibrary()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        media_probe=FakeMediaProbe(raises=MediaProbeUnavailableError("ffprobe timed out")),
    )

    assert record is not None
    assert record.status == DownloadState.Importing.value
    assert record.failed_reason is None
    assert library.scanned == []
    assert not any(movies_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.downloading


async def test_import_probe_outage_escalates_crash_resumed_movie_past_retry_bound(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """#180: a crash-resumed ``Importing`` row auto-retries a transient probe
    outage, but must NOT retry forever -- once ffprobe has been unavailable for
    more consecutive resumed-import cycles than the bound allows (a permanently
    broken adapter: an image regression, a missing binary), the row escalates
    through the same ``import_blocked`` correction path every other honest
    import failure uses, instead of sitting stuck ``Importing`` indefinitely
    with no operator correction path (north star 1).
    """
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.Importing.value,
    )
    max_retries = vars(import_service)["_PROBE_OUTAGE_MAX_RETRIES"]
    assert isinstance(max_retries, int) and max_retries > 0
    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
        assert row is not None
        row.retry_count = max_retries  # already AT the bound
        await session.commit()
    library = FakeLibrary()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        media_probe=FakeMediaProbe(raises=MediaProbeUnavailableError("ffprobe timed out")),
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "ffprobe timed out" in record.failed_reason
    assert library.scanned == []
    assert not any(movies_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.import_blocked


class _PausingUnavailableProbe:
    """Pause in the probe thread so another DB session can change the row."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def probe(self, path: Path, *, timeout_seconds: float | None = None) -> MediaProbeResult:
        del path
        self.started.set()
        assert self.release.wait(timeout=timeout_seconds or 2.0)
        raise MediaProbeUnavailableError("paused probe unavailable")


async def test_probe_outage_refreshes_concurrently_changed_importing_row(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.Importing.value,
    )
    probe = _PausingUnavailableProbe()

    async def change_row() -> None:
        assert await asyncio.to_thread(probe.started.wait, 2.0)
        async with sessionmaker_() as session:
            row = await session.get(Download, download_id)
            assert row is not None
            row.status = DownloadState.Failed.value
            row.failed_reason = "changed concurrently"
            await session.commit()
        probe.release.set()

    changer = asyncio.create_task(change_row())
    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        FakeLibrary(),
        media_probe=probe,
    )
    assert await changer is None

    assert record is not None
    assert record.status == DownloadState.Failed.value
    assert record.failed_reason == "changed concurrently"
    assert not any(movies_root.iterdir())


async def test_import_verification_has_one_aggregate_batch_deadline(
    tmp_path: Path,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert vars(import_service)["_VIDEO_VERIFICATION_BATCH_TIMEOUT_SECONDS"] == 30.0
    monkeypatch.setattr(import_service, "_VIDEO_VERIFICATION_BATCH_TIMEOUT_SECONDS", 0.0)
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    release_dir = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    first = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    second = release_dir / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.PROPER.mkv"
    _make_video(first)
    _make_video(second)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    probe = FakeMediaProbe()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(release_dir),
        FakeLibrary(),
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "verification batch exceeded its 0-second deadline" in record.failed_reason
    assert probe.calls == []
    assert not any(movies_root.iterdir())


async def test_verification_deadline_includes_executor_queue_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(import_service, "_VIDEO_VERIFICATION_BATCH_TIMEOUT_SECONDS", 0.01)
    probe = FakeMediaProbe()

    async def queued_to_thread(func: object, *args: object, **kwargs: object) -> object:
        del func, args, kwargs
        await asyncio.sleep(1.0)
        raise AssertionError("queued probe should have been cancelled at the batch deadline")

    monkeypatch.setattr(import_service.asyncio, "to_thread", queued_to_thread)

    with pytest.raises(RuntimeError, match=r"verification batch exceeded its 0\.01-second"):
        await import_service._verified_plex_video_sources(  # pyright: ignore[reportPrivateUsage]
            probe,
            [("/downloads/movie.mkv", 80 * 1024 * 1024, "movie.mkv")],
        )

    assert probe.calls == []


async def test_verification_deadline_truncation_mid_batch_is_wholly_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#179: when the aggregate deadline truncates the batch AFTER some
    candidates already probed clean, the un-probed tail must never be folded
    into 'unavailable' and silently dropped while the already-accepted subset
    proceeds -- that would import a season pack missing its late episodes for a
    reason unrelated to file validity. The whole batch must come back as a
    single retryable ``_VideoVerificationUnavailableError`` instead, and every
    un-probed candidate must be genuinely un-probed (never handed to the fake
    probe at all).
    """
    # time.monotonic() call sequence inside _verified_plex_video_sources:
    #   1. deadline = now() + BATCH_TIMEOUT         -> 0.0  (deadline = 5.0)
    #   2. iteration 0 top-of-loop remaining check   -> 0.0  (remaining = 5.0, proceeds)
    #   3. _probe_before_deadline's own remaining    -> 0.5  (remaining = 4.5, probe runs)
    #   4. iteration 1 top-of-loop remaining check    -> 6.0  (remaining = -1.0, deadline hit)
    # Beyond that, any further call just keeps reading "past the deadline"
    # rather than raising StopIteration once the scripted sequence is
    # exhausted. Rebinding the MODULE-LEVEL ``time`` name (rather than
    # mutating the real ``time`` module's ``monotonic`` attribute in place)
    # keeps this fake clock scoped to ``import_service`` alone -- asyncio's
    # own internal timers (``asyncio.timeout``, task scheduling) read the
    # SAME shared ``time.monotonic`` under the hood, so mutating it globally
    # would desync the event loop's own clock from real wall time too.
    schedule = [0.0, 0.0, 0.5, 6.0]

    class _FakeClock:
        @staticmethod
        def monotonic() -> float:
            return schedule.pop(0) if schedule else 6.0

    monkeypatch.setattr(import_service, "time", _FakeClock)
    monkeypatch.setattr(import_service, "_VIDEO_VERIFICATION_BATCH_TIMEOUT_SECONDS", 5.0)
    probe = FakeMediaProbe()
    sources = [
        ("/downloads/episode.s01e01.mkv", 100, "episode.s01e01.mkv"),
        ("/downloads/episode.s01e02.mkv", 100, "episode.s01e02.mkv"),
        ("/downloads/episode.s01e03.mkv", 100, "episode.s01e03.mkv"),
    ]

    with pytest.raises(
        import_service._VideoVerificationUnavailableError,  # pyright: ignore[reportPrivateUsage]
        match="deadline",
    ):
        await import_service._verified_plex_video_sources(  # pyright: ignore[reportPrivateUsage]
            probe, sources
        )

    # Only the FIRST candidate (already accepted before the deadline hit) was
    # ever handed to the probe -- the other two were genuinely never attempted,
    # not tallied as failed/unavailable verification.
    assert [path.name for path in probe.calls] == ["episode.s01e01.mkv"]


class _DeadlineHonoringProbe:
    """Test probe that consumes its supplied budget before returning a verdict."""

    def __init__(self) -> None:
        self.completed = False
        self.timeout_seconds: float | None = None

    def probe(self, path: Path, *, timeout_seconds: float | None = None) -> MediaProbeResult:
        del path
        assert timeout_seconds is not None and timeout_seconds > 0
        self.timeout_seconds = timeout_seconds
        # Simulate subprocess.run taking a little longer than its timeout while
        # it kills and reaps the child. The import must join this cleanup.
        time.sleep(timeout_seconds + 0.02)
        self.completed = True
        raise MediaProbeUnavailableError("deadline-aware probe timed out")


async def test_import_waits_for_deadline_bounded_probe_work_to_finish(
    tmp_path: Path,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(import_service, "_VIDEO_VERIFICATION_BATCH_TIMEOUT_SECONDS", 0.01)
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    probe = _DeadlineHonoringProbe()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        FakeLibrary(),
        media_probe=probe,
    )

    assert probe.completed, "import returned while probe work was still running"
    assert probe.timeout_seconds is not None and probe.timeout_seconds <= 0.01
    assert record is not None and record.status == DownloadState.ImportBlocked.value
    assert not any(movies_root.iterdir())


async def test_import_movies_root_unset_is_an_honest_retryable_block(
    sessionmaker_: SessionMaker,
) -> None:
    """Mirrors ``test_import_tv_root_unset_is_an_honest_retryable_block``: an
    install with the tv root configured but NOT the movies root must still block
    a movie import honestly (never a crash from ``Path(None)``), and never gate
    on movies_root being set to import the OTHER media type."""
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    async with sessionmaker_() as session:
        record = await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=FakeQbittorrent(),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=None,
            tv_root="/unused",
        )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason == "movies library root is not configured"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.import_blocked


# ---------------------------------------------------------------------------
# Container path visibility (issues #131/#132/#133): a configured root or a
# qbittorrent-reported content path that is a HOST-namespace location this
# container can't see must block honestly -- NEVER os.makedirs a phantom
# in-container tree, and NEVER the misleading "no video file found".
# ---------------------------------------------------------------------------
async def test_import_blocks_when_library_root_not_visible(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "gone"  # never created
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
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
    assert "library root not visible inside the container" in record.failed_reason
    # The load-bearing assertion: never os.makedirs a phantom in-container tree.
    assert not movies_root.exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_blocks_when_download_path_not_visible(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    # A HOST-namespace content path (qbt runs on the host) with no match under the
    # default KNOWN_CONTAINER_MOUNTS -- genuinely invisible from this container.
    host_content = Path(
        "/definitely-not-a-real-host-path/downloads/The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    )
    download_id, _ = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )

    record = await _import(
        sessionmaker_, download_id, movies_root, _qbt(host_content), FakeLibrary()
    )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    # The regression guard vs the misleading "no video file found": this reason
    # names the REAL problem (a path this container cannot see).
    assert "download path not visible inside the container" in record.failed_reason
    assert "no video file" not in record.failed_reason


async def test_import_remaps_download_path_under_the_downloads_mount(
    tmp_path: Path,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    mount = tmp_path / "dl"
    video = mount / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    # Content is remapped under the DOWNLOAD mounts only (never the library mounts).
    # tmp dirs are never mount points, so relax the live-mount gate (the test seam).
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    # qbt (host-side) reports a HOST path with the SAME basename -- the suffix
    # that must remap onto the real file under the mount, PROVEN by the torrent's
    # own file list (exact relative name + exact size).
    host_content = Path(
        "/definitely-not-a-real-host-path/downloads/The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    )
    qbt = _qbt(
        host_content,
        files=[DownloadedFile(name=host_content.name, size_bytes=60 * 1024 * 1024)],
    )
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, qbt, library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status == RequestStatus.completed


async def test_import_never_places_a_stale_shorter_suffix_match(
    tmp_path: Path,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding regression: a content remap must ANCHOR on save_path, never keep
    shortening the suffix. qBittorrent reports ``/host/qbt/movies/<file>`` whose
    real container location ``<mount>/movies/<file>`` is MISSING, while a STALE,
    unrelated file with the SAME basename sits at the mount ROOT
    (``<mount>/<file>``). The old free suffix search would shorten to the bare
    basename, match the stale file, and validate/PLACE the wrong source. The
    anchored remap requires the file at its exact position under the (remapped)
    save directory, so it blocks honestly and NEVER touches the stale file."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    mount = tmp_path / "dl"
    # The real category directory exists (qBittorrent saved other torrents here),
    # but THIS torrent's file is absent under it.
    (mount / "movies").mkdir(parents=True)
    # A stale, unrelated file with the SAME basename at the mount ROOT -- what the
    # shorter-suffix guess would have wrongly matched and placed.
    stale = mount / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(stale)  # same name AND same size as the real torrent file
    # The mount must COUNT for this test to bite (otherwise the block is vacuous):
    # relax the live-mount gate so the tmp dir stands in as the download mount.
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    # qbt (host-side) reports the file under a category save path whose real
    # container mapping (``<mount>/movies/<file>``) does not exist.
    host_content = Path(
        "/definitely-not-a-real-host-path/qbt/movies/The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    )
    qbt = _qbt(
        host_content,
        files=[DownloadedFile(name=host_content.name, size_bytes=60 * 1024 * 1024)],
    )
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, qbt, library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "download path not visible inside the container" in record.failed_reason
    # The decisive assertions: the stale file was neither validated nor placed.
    assert not any(movies_root.iterdir())
    assert library.scanned == []
    # The stale source itself is untouched (never hardlinked out).
    assert stale.exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status == RequestStatus.import_blocked


async def test_import_blocks_a_same_name_wrong_size_stale_at_the_bind_root(
    tmp_path: Path,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 regression: the bind-root content remap (save_path IS the bind
    source, mapped to the download mount root) must demand name+size PROOF from
    the torrent's own file list -- a same-named stale file with a DIFFERENT size
    at the expected location is a disproof, so the import blocks honestly and
    the stale file is never validated, placed, or touched."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    mount = tmp_path / "dl"
    mount.mkdir()
    stale = mount / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(stale)  # 60 MiB on disk...
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    host_content = Path(
        "/definitely-not-a-real-host-path/qbt/The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    )
    # ...but the torrent's OWN file is a different size: same name, wrong file.
    qbt = _qbt(
        host_content,
        files=[DownloadedFile(name=host_content.name, size_bytes=60 * 1024 * 1024 + 1)],
    )
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, qbt, library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "download path not visible inside the container" in record.failed_reason
    assert not any(movies_root.iterdir())
    assert library.scanned == []
    assert stale.exists()  # never touched
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status == RequestStatus.import_blocked


async def test_import_retry_after_creating_the_root_heals(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"  # not created yet
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _ = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    qbt = _qbt(video)
    library = FakeLibrary()

    blocked = await _import(sessionmaker_, download_id, movies_root, qbt, library)
    assert blocked is not None
    assert blocked.status == DownloadState.ImportBlocked.value

    movies_root.mkdir()
    # The operator's retry re-invokes import_download from scratch, re-reading
    # the row + roots fresh -- proving the fix, not merely asserting it.
    healed = await _import(sessionmaker_, download_id, movies_root, qbt, library)

    assert healed is not None
    assert healed.status == DownloadState.Imported.value
    assert (movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv").exists()


async def test_import_rejects_content_path_outside_qbittorrent_save_path(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """qBittorrent-reported content_path is client data, not an authority to read
    arbitrary local files. It must stay under the torrent save_path."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    save_path = tmp_path / "downloads" / "intended"
    save_path.mkdir(parents=True)
    outside = tmp_path / "outside" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(outside)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=outside.name,
                raw_state="stalledUP",
                progress=1.0,
                save_path=str(save_path),
                content_path=str(outside),
            )
        ]
    )

    record = await _import(sessionmaker_, download_id, movies_root, qbt, FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "outside download save path" in record.failed_reason
    assert not any(movies_root.iterdir())


async def test_import_rejects_traversing_qbittorrent_name(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """When qBittorrent omits content_path, save_path + name must not be allowed to
    escape through '..' or an absolute torrent name."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    save_path = tmp_path / "downloads" / "intended"
    save_path.mkdir(parents=True)
    outside = tmp_path / "downloads" / "outside" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(outside)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
    )
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name="../outside/The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv",
                raw_state="stalledUP",
                progress=1.0,
                save_path=str(save_path),
                content_path=None,
            )
        ]
    )

    record = await _import(sessionmaker_, download_id, movies_root, qbt, FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "outside download save path" in record.failed_reason
    assert not any(movies_root.iterdir())


def test_resolve_content_prefers_live_save_path_name_over_library_breadcrumb(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    live_release = downloads / "The.Matrix.1999.1080p.WEB-DL.x264-GRP"
    stale_library_file = tmp_path / "library" / "The Matrix (1999)" / "The Matrix (1999).mkv"
    status = DownloadStatus(
        info_hash=_HASH,
        name=live_release.name,
        raw_state="stalledUP",
        save_path=str(downloads),
        content_path=None,
    )

    resolved = import_service._resolve_content(  # pyright: ignore[reportPrivateUsage]
        status, str(stale_library_file)
    )

    assert resolved is not None
    assert resolved.path == str(live_release)
    # The live save_path rides along as the remap ANCHOR (finding: a content remap
    # must be anchored on save_path, never a free suffix search).
    assert resolved.save_path == str(downloads)


def test_place_file_refuses_dangling_symlink_destination(tmp_path: Path) -> None:
    """GHSA-8fj8: a dangling symlink at dst reads as "absent" under ``exists()``
    -- ``_place_file`` must refuse it (lexists semantics) as an honest conflict,
    never silently publish through/over it."""
    src = tmp_path / "src.mkv"
    src.write_text("new-download")
    dst = tmp_path / "dst.mkv"
    target = tmp_path / "gone.mkv"  # never created
    dst.symlink_to(target)

    with pytest.raises(FileExistsError):
        import_service._place_file(  # pyright: ignore[reportPrivateUsage]
            LocalFileSystem(), str(src), dst
        )

    assert dst.is_symlink()
    assert not target.exists()


def test_same_file_content_false_for_dangling_symlink(tmp_path: Path) -> None:
    """``_same_file_content`` must not raise ``FileNotFoundError`` on a dangling
    symlink dst -- it is honestly NOT the same content as a real src file."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    dst.symlink_to(tmp_path / "gone.mkv")

    assert (
        import_service._same_file_content(str(src), dst)  # pyright: ignore[reportPrivateUsage]
        is False
    )


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
        changed = await run_import_cycle(
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=_qbt(video),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(movies_root),
        )
    assert changed == 1

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        assert download is not None and download.status == DownloadState.Imported.value
        assert request is not None and request.status == RequestStatus.completed


async def test_run_import_cycle_blocks_ownerless_row_instead_of_skipping_it(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # issue #74: an ownerless auto-drain row (media_request_id NULL) must reach
    # import_download and become a visible, retryable ImportBlocked, not be filtered
    # out of the cycle and left stuck in ImportPending forever. The cycle no longer
    # skips ownerless rows; import_download already blocks them honestly with
    # "import has no owning request".
    async with sessionmaker_() as session:
        download = Download(
            torrent_hash="0" * 40,
            status=DownloadState.ImportPending.value,
            media_request_id=None,
            tmdb_id=_TMDB_ID,
            year=1999,
        )
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        changed = await run_import_cycle(
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=FakeLibrary(),
            qbt=FakeQbittorrent(statuses=[]),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=str(tmp_path / "library"),
        )
    assert changed == 1

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None
    assert download.status == DownloadState.ImportBlocked.value
    assert download.failed_reason == "import has no owning request"


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
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 1

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
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 0

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
                    session, FakeQbittorrent(), download_id=self._download_id, blocklist=True
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

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        async with self._sessionmaker() as session:
            row = await session.get(Download, self._download_id)
            assert row is not None
            row.status = DownloadState.Failed.value
            row.failed_reason = "marked failed by operator"
            await session.commit()
        self.scanned.append(path)
        self.scan_calls.append((path, media_type))


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
        await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=True
        )

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


async def test_block_does_not_overwrite_operator_mark_failed_during_gap(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # G1: the PRE-claim ``_block`` sites must honor an operator's mark_failed too. A CAM
    # file is rejected by the validator, driving import_download into the
    # validation-reject ``_block`` BEFORE it ever claims ``Importing`` (so the round-8
    # claim-CAS never runs). If the operator rejected the release (mark_failed -> Failed
    # + blocklist + re-search) during qbt.get_status, the compare-and-swap inside
    # ``_block`` must see the row left ``_RESUMABLE`` and abort: it must NOT overwrite
    # ``failed`` with ``import_blocked`` and must NOT re-arm the request away from
    # ``searching``. The operator's correction is honored (north-star).
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.CAM.x264-GRP.mkv"
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

    # The operator's failed state stands; the rejected CAM was NOT re-blocked over it.
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


# ---------------------------------------------------------------------------
# TV import — season-scoped, partial-success, one scan for the whole season.
# ---------------------------------------------------------------------------


async def _seed_tv(
    sessionmaker_: SessionMaker,
    *,
    season: int,
    request_status: RequestStatus = RequestStatus.downloading,
    season_status: str = "downloading",
    download_status: str = DownloadState.ImportPending.value,
    episodes: list[int] | None = None,
    is_anime: bool = False,
) -> tuple[int, int, int]:
    """Insert a tv request + one tracked season + a download for that season.

    Returns ``(download_id, request_id, season_request_id)``.
    """
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=request_status,
            is_anime=is_anime,
        )
        session.add(request)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=request.id, season_number=season, status=season_status
        )
        session.add(season_row)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status=download_status,
            media_request_id=request.id,
            tmdb_id=_TMDB_ID,
            year=2020,
            season=season,
            episodes_json=episodes,
        )
        session.add(download)
        await session.commit()
        return download.id, request.id, season_row.id


async def _import_tv(
    sessionmaker_: SessionMaker,
    download_id: int,
    tv_root: Path,
    qbt: FakeQbittorrent,
    library: FakeLibrary,
    *,
    anime_tv_root: Path | None = None,
    media_probe: MediaProbePort | None = None,
) -> DownloadRecord | None:
    async with sessionmaker_() as session:
        return await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=media_probe or FakeMediaProbe(),
            library=library,
            qbt=qbt,
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root="/unused",  # required by the port, never touched by the tv branch
            tv_root=str(tv_root),
            anime_tv_root=str(anime_tv_root) if anime_tv_root is not None else None,
        )


async def test_import_tv_happy_path_places_every_accepted_episode_with_one_scan(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert (season_dir / "Some Show - S02E01.mkv").exists()
    assert (season_dir / "Some Show - S02E02.mkv").exists()
    # ONE targeted scan of the whole season directory, never one per episode.
    assert library.scan_calls == [(str(season_dir), "tv")]
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None
    assert season_row.status.value == "completed"
    assert request is not None
    assert request.status is RequestStatus.completed  # "Finalizing", not yet available


async def test_import_tv_associates_only_verified_video_files(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    accepted = release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv"
    rejected = release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(accepted)
    _make_video(rejected)
    (release_dir / "notes.nfo").write_text("not a library video")
    download_id, _request_id, _season_id = await _seed_tv(sessionmaker_, season=2)
    probe = FakeMediaProbe(rejected={rejected.name: "no real video stream"})

    record = await _import_tv(
        sessionmaker_,
        download_id,
        tv_root,
        _qbt(release_dir),
        FakeLibrary(),
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.Imported.value
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert [path.name for path in season_dir.iterdir()] == ["Some Show - S02E01.mkv"]


async def test_import_tv_uses_verified_episode_when_sibling_probe_is_unavailable(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    unavailable = release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv"
    accepted = release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(unavailable)
    _make_video(accepted)
    download_id, _request_id, _season_id = await _seed_tv(sessionmaker_, season=2)
    probe = FakeMediaProbe(unavailable={unavailable.name: "ffprobe worker unavailable"})

    record = await _import_tv(
        sessionmaker_,
        download_id,
        tv_root,
        _qbt(release_dir),
        FakeLibrary(),
        media_probe=probe,
    )

    assert record is not None and record.status == DownloadState.Imported.value
    assert {path.name for path in probe.calls} == {accepted.name, unavailable.name}
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert [path.name for path in season_dir.iterdir()] == ["Some Show - S02E02.mkv"]


async def test_import_tv_probe_outage_keeps_crash_resumed_season_auto_retryable(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    video = tmp_path / "downloads" / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id, season_id = await _seed_tv(
        sessionmaker_, season=2, download_status=DownloadState.Importing.value
    )
    library = FakeLibrary()

    record = await _import_tv(
        sessionmaker_,
        download_id,
        tv_root,
        _qbt(video),
        library,
        media_probe=FakeMediaProbe(raises=MediaProbeUnavailableError("ffprobe timed out")),
    )

    assert record is not None
    assert record.status == DownloadState.Importing.value
    assert record.failed_reason is None
    assert library.scan_calls == []
    assert not any(tv_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        season = await session.get(SeasonRequest, season_id)
    assert request is not None and request.status is RequestStatus.downloading
    assert season is not None and season.status.value == "downloading"


async def test_import_tv_shared_torrent_completes_each_attached_scope(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01-S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")

    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=RequestStatus.downloading,
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
            torrent_hash=_HASH,
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
        download_id = download.id
        request_id = request.id

    library = FakeLibrary()
    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    season_1_dir = tv_root / "Some Show (2020)" / "Season 01"
    season_2_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert (season_1_dir / "Some Show - S01E01.mkv").exists()
    assert (season_2_dir / "Some Show - S02E01.mkv").exists()
    assert library.scan_calls == [(str(season_1_dir), "tv"), (str(season_2_dir), "tv")]

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == download_id)
                )
            )
            .scalars()
            .all()
        )
    assert {season.season_number: season.status for season in seasons} == {
        1: "completed",
        2: "completed",
    }
    assert {scope.season_number: scope.status for scope in scopes} == {
        1: "imported",
        2: "imported",
    }
    assert all(scope.completed_at is not None for scope in scopes)


async def test_import_tv_probe_outage_keeps_crash_resumed_shared_scopes_auto_retryable(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01-S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")

    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        seasons = [
            SeasonRequest(
                media_request_id=request.id,
                season_number=number,
                status=RequestStatus.downloading.value,
            )
            for number in (1, 2)
        ]
        session.add_all(seasons)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status=DownloadState.Importing.value,
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
                    season_request_id=season.id,
                    season_number=season.season_number,
                    scope_key=f"season:{season.season_number}|episodes:*",
                    status="active",
                )
                for season in seasons
            ]
        )
        await session.commit()
        download_id = download.id
        request_id = request.id

    library = FakeLibrary()
    record = await _import_tv(
        sessionmaker_,
        download_id,
        tv_root,
        _qbt(release_dir),
        library,
        media_probe=FakeMediaProbe(raises=MediaProbeUnavailableError("ffprobe timed out")),
    )

    assert record is not None
    assert record.status == DownloadState.Importing.value
    assert record.failed_reason is None
    assert library.scan_calls == []
    assert not any(tv_root.iterdir())
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        season_rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == download_id)
                )
            )
            .scalars()
            .all()
        )
    assert request is not None and request.status is RequestStatus.downloading
    assert {season.season_number: season.status for season in season_rows} == {
        1: "downloading",
        2: "downloading",
    }
    assert {scope.season_number: scope.status for scope in scopes} == {
        1: "active",
        2: "active",
    }


async def test_import_tv_shared_torrent_keeps_download_blocked_for_failed_scope(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01-S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")

    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            year=2020,
            status=RequestStatus.downloading,
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
            torrent_hash=_HASH,
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
        download_id = download.id
        request_id = request.id

    library = FakeLibrary()
    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "S02" in record.failed_reason
    assert record.season == 2
    season_1_dir = tv_root / "Some Show (2020)" / "Season 01"
    season_2_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert (season_1_dir / "Some Show - S01E01.mkv").exists()
    assert not season_2_dir.exists()
    assert library.scan_calls == [(str(season_1_dir), "tv")]

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == download_id)
                )
            )
            .scalars()
            .all()
        )

    assert request is not None and request.status is RequestStatus.import_blocked
    assert {season.season_number: season.status for season in seasons} == {
        1: "completed",
        2: "import_blocked",
    }
    assert {scope.season_number: scope.status for scope in scopes} == {
        1: "imported",
        2: "import_blocked",
    }
    assert next(scope for scope in scopes if scope.season_number == 1).completed_at is not None
    assert next(scope for scope in scopes if scope.season_number == 2).completed_at is None

    # The non-terminal physical row now claims its unresolved S2 scope, not the
    # imported S1 compatibility slot. A replacement S1 download can be tracked,
    # while the legacy DB guard still rejects a second active release for S2.
    async with sessionmaker_() as session:
        repo = SqlDownloadRepository(session)
        replacement = await repo.create(
            torrent_hash="replacement-s1",
            status=DownloadState.Downloading.value,
            media_request_id=request_id,
            tmdb_id=_TMDB_ID,
            season=1,
            media_type="tv",
        )
        assert replacement.season == 1
        with pytest.raises(IntegrityError):
            await repo.create(
                torrent_hash="replacement-s2",
                status=DownloadState.Downloading.value,
                media_request_id=request_id,
                tmdb_id=_TMDB_ID,
                season=2,
                media_type="tv",
            )


async def test_import_tv_retry_success_clears_stale_failed_reason(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # issue #73: a TV row retried out of ImportBlocked that imports cleanly must
    # land with a null failed_reason, matching the movie path. Before the fix the
    # TV claim/finalize CAS omitted clear_failed_reason, so a successfully-imported
    # season kept displaying its stale import-block reason in the queue and audit.
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, _request_id, season_id = await _seed_tv(
        sessionmaker_,
        season=2,
        request_status=RequestStatus.import_blocked,
        season_status=RequestStatus.import_blocked.value,
        download_status=DownloadState.ImportBlocked.value,
    )
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.failed_reason = "stale tv block reason"
        await session.commit()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), FakeLibrary())

    assert record is not None
    assert record.status == DownloadState.Imported.value
    assert record.failed_reason is None
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        season_row = await session.get(SeasonRequest, season_id)
    assert download is not None
    assert download.status == DownloadState.Imported.value
    assert download.failed_reason is None
    assert season_row is not None and season_row.status.value == "completed"


async def test_import_tv_defers_when_live_client_status_is_not_settled(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(
        sessionmaker_,
        season=2,
        request_status=RequestStatus.import_blocked,
        season_status=RequestStatus.import_blocked.value,
        download_status=DownloadState.ImportBlocked.value,
    )
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.failed_reason = "stale tv import block"
        await session.commit()
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=release_dir.name,
                raw_state="moving",
                progress=0.5,
                ratio=0.25,
                save_path=str(release_dir.parent),
                content_path=str(release_dir),
            )
        ]
    )
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, qbt, library)

    assert record is not None
    assert record.status == DownloadState.Downloading.value
    assert record.failed_reason is None
    assert record.progress == 0.5
    assert record.seed_ratio == 0.25
    assert library.scan_calls == []
    assert not any(tv_root.iterdir())
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == RequestStatus.downloading.value
    assert request is not None and request.status is RequestStatus.downloading


async def test_import_tv_persists_season_library_path_and_a_later_sweep_reclaims_it(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """C1 regression, tv side: import finalize must persist
    ``SeasonRequest.library_path`` (the season's own directory) -- proven
    end-to-end by running a REAL eviction sweep straight after import and
    confirming it actually finds and deletes the season directory. Before the
    fix, ``library_path`` stayed ``None`` forever, so
    ``eviction_service._season_candidates`` had no deletion target for this
    season."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, _request_id, season_id = await _seed_tv(sessionmaker_, season=2)

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), FakeLibrary())
    assert record is not None and record.status == DownloadState.Imported.value

    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        assert season_row is not None
        assert season_row.library_path == str(season_dir)

    # Confirm availability, then run a real eviction sweep against a watched,
    # past-grace season -- proving the persisted breadcrumb is exactly what
    # eviction_service._season_candidates reads.
    async with sessionmaker_() as session:
        await run_availability_cycle(
            library=FakeLibrary(available_tv_seasons={_TMDB_ID: frozenset({2})}), session=session
        )

    stale_library = FakeLibrary(
        watch_states={
            (_TMDB_ID, "tv", 2): WatchState(
                watched=True, last_viewed_at=datetime.now(UTC) - timedelta(days=999)
            )
        }
    )
    fs = LocalFileSystem(library_roots=[str(tv_root)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=stale_library,
            fs=fs,
            media_type="tv",
            root_path=str(tv_root),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=30,
        )

    assert [(o.title, o.season) for o in outcomes] == [("Some Show", 2)]
    assert not season_dir.exists()
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        assert season_row is not None
        assert season_row.status.value == "evicted"


async def test_import_tv_partial_accept_places_only_the_good_episode(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    # A season pack with one good episode and one CAM: partial success is legit
    # for tv (unlike the movie all-or-nothing verdict) -- the good episode is
    # imported and the download still reaches Imported / the season 'completed'.
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E02.CAM.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    assert (season_dir / "Some Show - S02E01.mkv").exists()
    assert not (season_dir / "Some Show - S02E02.mkv").exists()  # the CAM was never placed
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "completed"
    assert request is not None and request.status is RequestStatus.completed


async def test_import_tv_blocks_the_whole_season_when_every_file_is_rejected(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.CAM.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.CAM.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E02.CAM.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "quality_not_wanted" in record.failed_reason
    assert not any(tv_root.iterdir())  # nothing was imported into the library
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None
    assert season_row.status.value == "import_blocked"
    assert request is not None
    # The season's block is a surfaced, retryable "needs attention" state -- never
    # a request left lying as 'downloading' while nothing is downloading.
    assert request.status is RequestStatus.import_blocked


async def test_import_tv_episode_scoped_grab_blocks_when_incomplete(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """A grab scoped to episodes [4, 5] whose download only contains E04 must NOT be
    finalized as a completed season with E05 silently missing (no retry). It is an
    honest, retryable ImportBlocked, and NOTHING is placed -- a scoped grab is never
    half-imported."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E04.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2, episodes=[4, 5])
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None and "incomplete" in record.failed_reason
    assert not any(tv_root.iterdir())  # E04 was NOT placed -- no half-import
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_tv_dedupes_two_files_for_the_same_episode_keeping_the_largest(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """A season pack that ships the SAME episode twice (e.g. a file plus its
    PROPER/REPACK, or a mixed-resolution duplicate) must not roll back and block
    the whole season: the smaller duplicate is dropped before placing, the larger
    one is kept, and every OTHER episode still imports (partial success stays
    legitimate over what is really just one duplicate)."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(
        release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRPA.mkv", size_bytes=60 * 1024 * 1024
    )
    _make_video(
        release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRPB.mkv", size_bytes=90 * 1024 * 1024
    )
    _make_video(release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value  # NOT blocked over the duplicate
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    ep1 = season_dir / "Some Show - S02E01.mkv"
    assert ep1.exists()
    assert ep1.stat().st_size == 90 * 1024 * 1024  # the LARGER duplicate won, not whichever
    assert (season_dir / "Some Show - S02E02.mkv").exists()  # the other episode still imported
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "completed"
    assert request is not None and request.status is RequestStatus.completed


async def test_import_tv_single_file_symlink_escaping_parent_is_blocked_not_copied(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """A single-file TV 'torrent' (no season-pack folder) that is ITSELF a
    symlink escaping its own parent directory must never be followed -- mirrors
    ``largest_video_file``'s is_file containment guard on the movie path.
    Without it, the importer would copy an arbitrary out-of-tree file into the
    public TV library. An honest 'no video found' block, never a silent copy."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    secret = tmp_path / "secret.mkv"  # OUTSIDE the download tree
    _make_video(secret)
    escape_link = downloads_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv"
    escape_link.symlink_to(secret)
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(escape_link), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "outside download save path" in record.failed_reason
    assert not any(tv_root.iterdir())  # nothing was imported into the library
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_tv_root_unset_is_an_honest_retryable_block(
    sessionmaker_: SessionMaker,
) -> None:
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=1)
    library = FakeLibrary()

    async with sessionmaker_() as session:
        record = await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=FakeQbittorrent(),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root="/unused",
            tv_root=None,
        )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason == "tv library root is not configured"
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_tv_blocks_when_tv_root_not_visible(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "gone"  # never created
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "library root not visible inside the container" in record.failed_reason
    assert not tv_root.exists()  # never os.makedirs a phantom season tree
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_tv_blocks_when_download_path_not_visible(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    host_release_dir = Path(
        "/definitely-not-a-real-host-path/downloads/Some.Show.S02.1080p.WEB-DL.x264-GRP"
    )
    download_id, _request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(host_release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason is not None
    assert "download path not visible inside the container" in record.failed_reason
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None and season_row.status.value == "import_blocked"


async def test_import_tv_scan_failure_never_leaves_a_lying_imported_history_row(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """F3: when the Plex scan fails after every episode in a season pack was
    placed, the placed files are rolled back -- the ``imported`` DownloadHistory
    rows for those SAME episodes must never have been committed either. Before
    the fix, each episode's ``imported`` row was added to the session (and
    committed alongside the download_path bookkeeping) BEFORE the scan ran, so a
    scan failure left the audit trail claiming episodes were imported when they
    had in fact just been deleted (honesty over silence)."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = _ScanFailsLibrary()

    record = await _import_tv(sessionmaker_, download_id, tv_root, _qbt(release_dir), library)

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    # Rolled back: neither placed episode survives the scan failure.
    assert not (season_dir / "Some Show - S02E01.mkv").exists()
    assert not (season_dir / "Some Show - S02E02.mkv").exists()

    async with sessionmaker_() as session:
        events = (
            (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.torrent_hash == _HASH)
                )
            )
            .scalars()
            .all()
        )
    # Honesty over silence: no "imported" row for a file that was just deleted.
    assert events  # import_started was still recorded, honestly
    assert all(e.event_type != DownloadHistoryEvent.imported for e in events)
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


class _FailsOnSecondCallFs(LocalFileSystem):
    """A LocalFileSystem whose ``hardlink_or_copy`` succeeds for the FIRST file
    placed (whichever one the loop visits first -- directory iteration order is
    not guaranteed across filesystems) and fails for the SECOND, simulating a
    mid-loop copy failure on a LATER file in a season pack after an earlier
    file already placed successfully."""

    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:  # type: ignore[override]
        self._calls += 1
        if self._calls >= 2:
            raise OSError("simulated copy failure")
        super().hardlink_or_copy(src, dst)


async def test_import_tv_mid_pack_copy_failure_never_leaves_a_lying_imported_history_row(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """F3 (second case): when a LATER episode's copy fails mid-loop, the earlier
    episode already placed in THIS SAME loop is rolled back too (existing
    behaviour) -- and, likewise, its ``imported`` history row must never have
    been committed. Before the fix, the earlier file's ``imported`` row was
    added to the session inside the loop and got flushed to the DB by the
    failure path's own ``_block`` commit, lying about a file that had just been
    unlinked."""
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    _make_video(release_dir / "Some.Show.S02E02.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=2)
    library = FakeLibrary()

    async with sessionmaker_() as session:
        record = await import_download(
            download_id=download_id,
            fs=_FailsOnSecondCallFs(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=_qbt(release_dir),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root="/unused",
            tv_root=str(tv_root),
        )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    season_dir = tv_root / "Some Show (2020)" / "Season 02"
    # Whichever episode placed first was rolled back too; no *.mkv survives.
    if season_dir.exists():
        assert not list(season_dir.glob("*.mkv"))

    async with sessionmaker_() as session:
        events = (
            (
                await session.execute(
                    select(DownloadHistory).where(DownloadHistory.torrent_hash == _HASH)
                )
            )
            .scalars()
            .all()
        )
    assert all(e.event_type != DownloadHistoryEvent.imported for e in events)
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "import_blocked"
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_run_import_cycle_drains_a_tv_download_to_a_completed_season(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S01.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S01E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, request_id, season_id = await _seed_tv(sessionmaker_, season=1)
    library = FakeLibrary()

    async with sessionmaker_() as session:
        changed = await run_import_cycle(
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=_qbt(release_dir),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root="/unused",
            tv_root=str(tv_root),
        )
    assert changed == 1

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert download is not None and download.status == DownloadState.Imported.value
    assert season_row is not None and season_row.status.value == "completed"
    assert request is not None and request.status is RequestStatus.completed


async def test_run_availability_cycle_promotes_a_completed_season_to_available(
    sessionmaker_: SessionMaker,
) -> None:
    _download_id, request_id, season_id = await _seed_tv(
        sessionmaker_,
        season=1,
        request_status=RequestStatus.completed,
        season_status="completed",
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available_tv_seasons={_TMDB_ID: frozenset({1})})

    async with sessionmaker_() as session:
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 1

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "available"
    assert request is not None and request.status is RequestStatus.available


async def test_run_availability_cycle_leaves_a_season_completed_when_not_yet_in_plex(
    sessionmaker_: SessionMaker,
) -> None:
    _download_id, request_id, season_id = await _seed_tv(
        sessionmaker_,
        season=1,
        request_status=RequestStatus.completed,
        season_status="completed",
        download_status=DownloadState.Imported.value,
    )
    library = FakeLibrary(available_tv_seasons={})  # Plex has not indexed it yet

    async with sessionmaker_() as session:
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 0

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None and season_row.status.value == "completed"
    assert request is not None and request.status is RequestStatus.completed  # stays "Finalizing"


# --------------------------------------------------------------------------- #
# run_availability_cycle — batched checks, not one Plex call per row (issue #136)
# --------------------------------------------------------------------------- #


async def _seed_movie_request(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    status: RequestStatus = RequestStatus.completed,
    library_path: str | None = None,
    completed_at: datetime | None = None,
    user_id: int | None = None,
) -> int:
    """Insert a bare movie request row (no download) -- the availability cycle
    reads only ``MediaRequest``, so a batching test needs nothing else.
    ``library_path``/``completed_at`` (issue #158) let a path-confirmation-fallback
    or bounded-Finalizing test control the exact breadcrumb / elapsed-time anchor
    without needing a full import run. ``user_id`` (request-dedup healing) lets an
    ownership-guard test control the row's owner."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title=f"Movie {tmdb_id}",
            year=2020,
            status=status,
            library_path=library_path,
            completed_at=completed_at,
            user_id=user_id,
        )
        session.add(request)
        await session.commit()
        return request.id


async def _seed_show_request(
    sessionmaker_: SessionMaker, *, tmdb_id: int, status: RequestStatus = RequestStatus.completed
) -> int:
    """Insert a bare TV parent request row (no seasons yet)."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title=f"Show {tmdb_id}",
            year=2020,
            status=status,
        )
        session.add(request)
        await session.commit()
        return request.id


async def _seed_season(
    sessionmaker_: SessionMaker,
    *,
    media_request_id: int,
    season_number: int,
    status: str = "completed",
    library_path: str | None = None,
) -> int:
    """Insert one tracked season row for an already-seeded show request.
    ``library_path`` (issue #158) lets a path-confirmation-fallback test control
    the exact breadcrumb without a full import run."""
    async with sessionmaker_() as session:
        season_row = SeasonRequest(
            media_request_id=media_request_id,
            season_number=season_number,
            status=status,
            library_path=library_path,
        )
        session.add(season_row)
        await session.commit()
        return season_row.id


async def test_run_availability_cycle_batches_movies_into_a_single_present_ids_call(
    sessionmaker_: SessionMaker,
) -> None:
    """N completed movies (some present, some absent) must cost exactly ONE
    ``present_ids`` batch call -- never one ``is_available`` call per row (#136)."""
    present_id, absent_id, other_present_id = 111, 222, 333
    request_a = await _seed_movie_request(sessionmaker_, tmdb_id=present_id)
    request_b = await _seed_movie_request(sessionmaker_, tmdb_id=absent_id)
    request_c = await _seed_movie_request(sessionmaker_, tmdb_id=other_present_id)
    library = FakeLibrary(available={present_id, other_present_id})

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    assert library.present_ids_calls == 1
    assert library.is_available_calls == 0
    # P2 (#136 review): the batch call must request the never-trust-a-cached-
    # absence contract -- a movie that just finished indexing must not be held
    # "Finalizing" for the rest of the presence-cache TTL (see
    # PlexLibrary.present_ids's ``refresh_absent`` semantics).
    assert library.present_ids_refresh_absent_calls == [True]
    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, request_a)
        b = await session.get(MediaRequest, request_b)
        c = await session.get(MediaRequest, request_c)
    assert a is not None and a.status == RequestStatus.available
    assert b is not None and b.status == RequestStatus.completed  # absent stays "Finalizing"
    assert c is not None and c.status == RequestStatus.available


async def test_run_availability_cycle_no_completed_movies_skips_present_ids_entirely(
    sessionmaker_: SessionMaker,
) -> None:
    """No completed movies pending -> not even one ``present_ids`` call is made."""
    library = FakeLibrary()
    async with sessionmaker_() as session:
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 0
    assert library.present_ids_calls == 0


async def test_run_availability_cycle_movie_batch_failure_leaves_all_completed_for_retry(
    sessionmaker_: SessionMaker,
) -> None:
    """The single batch ``present_ids`` call failing must not crash the cycle, and
    every pending movie honestly stays ``completed`` for the next tick's retry."""
    request_a = await _seed_movie_request(sessionmaker_, tmdb_id=111)
    request_b = await _seed_movie_request(sessionmaker_, tmdb_id=222)
    library = FakeLibrary(raises=PlexLibraryError("plex unreachable"))

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)  # must not raise

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, request_a)
        b = await session.get(MediaRequest, request_b)
    assert a is not None and a.status == RequestStatus.completed
    assert b is not None and b.status == RequestStatus.completed


async def test_run_availability_cycle_groups_seasons_by_show_one_batch_call_total(
    sessionmaker_: SessionMaker,
) -> None:
    """Two distinct shows, three pending seasons total (two on one show) -> exactly
    ONE ``season_presence`` call for the WHOLE tick (never one per show, never one
    per season) naming both distinct shows, and ZERO ``is_available`` calls."""
    show_a = await _seed_show_request(sessionmaker_, tmdb_id=1001)
    season_a1 = await _seed_season(sessionmaker_, media_request_id=show_a, season_number=1)
    season_a2 = await _seed_season(sessionmaker_, media_request_id=show_a, season_number=2)
    show_b = await _seed_show_request(sessionmaker_, tmdb_id=2002)
    season_b1 = await _seed_season(sessionmaker_, media_request_id=show_b, season_number=1)

    library = FakeLibrary(available_tv_seasons={1001: frozenset({1}), 2002: frozenset({1})})

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    assert library.season_presence_calls == 1
    assert library.season_presence_call_ids == [frozenset({1001, 2002})]
    assert library.is_available_calls == 0

    async with sessionmaker_() as session:
        a1 = await session.get(SeasonRequest, season_a1)
        a2 = await session.get(SeasonRequest, season_a2)
        b1 = await session.get(SeasonRequest, season_b1)
    assert a1 is not None and a1.status.value == "available"
    assert a2 is not None and a2.status.value == "completed"  # season 2 not present yet
    assert b1 is not None and b1.status.value == "available"


async def test_run_availability_cycle_no_completed_seasons_skips_season_presence_entirely(
    sessionmaker_: SessionMaker,
) -> None:
    """No completed seasons pending -> not even one ``season_presence`` call is made."""
    library = FakeLibrary()
    async with sessionmaker_() as session:
        promoted = await run_availability_cycle(library=library, session=session)
    assert promoted == 0
    assert library.season_presence_calls == 0


async def test_run_availability_cycle_tv_batch_failure_leaves_all_pending_seasons_for_retry(
    sessionmaker_: SessionMaker,
) -> None:
    """The single batch ``season_presence`` call failing (a genuine WHOLE-BATCH
    transport failure -- the page-walk itself) must not crash the cycle, and must
    leave EVERY distinct show's pending seasons ``completed`` for the next tick's
    retry, since a real Plex transport failure fails the WHOLE page-walk, not one
    show in isolation."""
    show_a = await _seed_show_request(sessionmaker_, tmdb_id=1001)
    season_a1 = await _seed_season(sessionmaker_, media_request_id=show_a, season_number=1)
    show_b = await _seed_show_request(sessionmaker_, tmdb_id=2002)
    season_b1 = await _seed_season(sessionmaker_, media_request_id=show_b, season_number=1)

    library = FakeLibrary(
        available_tv_seasons={1001: frozenset({1}), 2002: frozenset({1})},
        season_presence_raises=PlexLibraryError("plex unreachable"),
    )

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)  # must not raise

    async with sessionmaker_() as session:
        a1 = await session.get(SeasonRequest, season_a1)
        b1 = await session.get(SeasonRequest, season_b1)
    # The whole page-walk failed -- neither show is isolated from the other,
    # both honestly stay completed for the next tick's retry.
    assert a1 is not None and a1.status.value == "completed"
    assert b1 is not None and b1.status.value == "completed"


async def test_run_availability_cycle_isolates_per_show_season_lookup_failure(
    sessionmaker_: SessionMaker,
) -> None:
    """(round 4, #136 review) One show's OWN season lookup failing inside an
    otherwise-successful batch call -- e.g. its metadata row was deleted between
    the page-walk and the ``/children`` fetch, or persistently 404s/500s -- must
    NOT starve every other pending show at 'Finalizing'. Only the failing show's
    seasons stay ``completed`` for retry; the healthy show still promotes in the
    very same tick."""
    show_a = await _seed_show_request(sessionmaker_, tmdb_id=1001)
    season_a1 = await _seed_season(sessionmaker_, media_request_id=show_a, season_number=1)
    show_b = await _seed_show_request(sessionmaker_, tmdb_id=2002)
    season_b1 = await _seed_season(sessionmaker_, media_request_id=show_b, season_number=1)

    library = FakeLibrary(
        available_tv_seasons={1001: frozenset({1}), 2002: frozenset({1})},
        raises_for_shows={1001: PlexLibraryError("bad metadata row for show 1001")},
    )

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)  # must not raise

    async with sessionmaker_() as session:
        a1 = await session.get(SeasonRequest, season_a1)
        b1 = await session.get(SeasonRequest, season_b1)
    # Show A's own lookup failed and was omitted from the batch result -- stays
    # completed, retried next cycle.
    assert a1 is not None and a1.status.value == "completed"
    # Show B is ISOLATED from show A's failure -- it still promotes this tick.
    assert b1 is not None and b1.status.value == "available"


# --------------------------------------------------------------------------- #
# run_availability_cycle — path-based confirmation fallback + bounded
# Finalizing (issue #158)
# --------------------------------------------------------------------------- #
_IMPORT_SERVICE_LOGGER = "plex_manager.services.import_service"


async def test_run_availability_cycle_path_confirms_a_guid_miss_movie(
    sessionmaker_: SessionMaker,
) -> None:
    """The exact live bug (issue #158): Plex's metadata provider matched the
    imported file to an item carrying no tmdb guid at all -- GUID confirmation
    (``present_ids``) can never succeed. The app's own ``library_path``
    breadcrumb lets ``confirm_paths`` confirm it anyway, by directory prefix."""
    library_path = "/media/Movies/Obsession (2026)"
    request_id = await _seed_movie_request(sessionmaker_, tmdb_id=999999, library_path=library_path)
    library = FakeLibrary(movie_file_paths=[f"{library_path}/Obsession.mkv"])

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.available
    assert library.confirm_paths_calls == [("movie", frozenset({library_path}))]


async def test_run_availability_cycle_movie_path_miss_stays_completed_no_warning_yet(
    sessionmaker_: SessionMaker,
) -> None:
    """GUID-miss AND path-miss: the request honestly stays ``completed`` -- never
    a new status -- and, before the bounded-Finalizing threshold, no warning."""
    library_path = "/media/Movies/Obsession (2026)"
    request_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=999999,
        library_path=library_path,
        completed_at=datetime.now(UTC),
    )
    library = FakeLibrary(movie_file_paths=["/media/Movies/Some Unrelated Film (2020)/file.mkv"])

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.completed


async def test_run_availability_cycle_movie_bounded_finalizing_warns_after_threshold(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """After ``_FINALIZING_WARN_AFTER_MINUTES`` of failing BOTH GUID and path
    confirmation, a WARNING names the title -- 'Finalizing' can no longer spin
    silently forever (time-mocked: no real sleeping)."""
    library_path = "/media/Movies/Obsession (2026)"
    completed_at = datetime.now(UTC) - timedelta(minutes=45)
    request_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=999999, library_path=library_path, completed_at=completed_at
    )
    library = FakeLibrary(movie_file_paths=[])

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=datetime.now(UTC))

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.completed  # never a new status
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("not confirmed by Plex" in r.getMessage() for r in warnings)
    assert any("Movie 999999" in r.getMessage() for r in warnings)


async def test_run_availability_cycle_movie_bounded_finalizing_respects_duty_cycle(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning repeats at a LOW duty cycle (once per hour per row), never on
    every ~15s reconcile tick -- derived purely from elapsed-time bucketing."""
    library_path = "/media/Movies/Obsession (2026)"
    completed_at = datetime.now(UTC) - timedelta(minutes=45)
    await _seed_movie_request(
        sessionmaker_, tmdb_id=999999, library_path=library_path, completed_at=completed_at
    )
    library = FakeLibrary(movie_file_paths=[])

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=completed_at + timedelta(minutes=45)
            )
        # A second tick a minute later -- still the SAME duty-cycle window --
        # must NOT re-warn.
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=completed_at + timedelta(minutes=46)
            )
    same_window_warnings = [r for r in caplog.records if "not confirmed by Plex" in r.getMessage()]
    assert len(same_window_warnings) == 1

    caplog.clear()
    # A third tick, over an hour after the FIRST warning -- the duty cycle has
    # elapsed, so it re-warns exactly once more.
    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=completed_at + timedelta(minutes=110)
            )
    later_warnings = [r for r in caplog.records if "not confirmed by Plex" in r.getMessage()]
    assert len(later_warnings) == 1


async def test_run_availability_cycle_movie_bounded_finalizing_suppressed_on_guid_batch_failure(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Round-5 finding: a completed movie's ``completed_at`` is already past the
    bounded-Finalizing threshold, but the GUID batch (and, since it defaults to
    "nothing confirmed", the path fallback too) both raise -- a Plex OUTAGE, not
    a genuine library/GUID mismatch. The 'not confirmed by Plex' warning must NOT
    fire; only the existing 'batch availability check failed' warnings (which
    already name the real cause) may appear, and the row's bookkeeping must be
    left alone so a LATER, successful tick can still warn once it genuinely
    fails both checks."""
    library_path = "/media/Movies/Obsession (2026)"
    completed_at = datetime.now(UTC) - timedelta(minutes=45)
    request_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=999999, library_path=library_path, completed_at=completed_at
    )
    library = FakeLibrary(raises=PlexLibraryError("plex unreachable"))

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=datetime.now(UTC))

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.completed
    assert not any("not confirmed by Plex" in r.getMessage() for r in caplog.records)
    assert not import_service.is_movie_unconfirmed_tracked(request_id)
    assert any("batch availability check failed" in r.getMessage() for r in caplog.records)


async def test_run_availability_cycle_season_bounded_finalizing_suppressed_on_transport_failure(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """TV mirror of the movie case above: the whole-batch ``season_presence`` call
    is a genuine transport failure (Plex unreachable), so every season's GUID
    answer is inconclusive -- even though the path fallback ALSO independently
    misses (no matching file path), the bounded-Finalizing warning must stay
    suppressed since the row was never conclusively checked."""
    show_id = await _seed_show_request(sessionmaker_, tmdb_id=777778)
    library_path = "/media/TV/Some Show (2019)/Season 02"
    await _seed_season(
        sessionmaker_, media_request_id=show_id, season_number=2, library_path=library_path
    )
    library = FakeLibrary(
        tv_file_paths=[],  # path fallback would miss too, if it were even trustworthy
        season_presence_raises=PlexLibraryError("plex unreachable"),
    )
    t0 = datetime.now(UTC)

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=t0)
        # A later tick, well past the 30-minute threshold -- still suppressed,
        # since ``season_presence`` keeps failing every tick.
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=t0 + timedelta(minutes=45)
            )

    assert not any("not confirmed by Plex" in r.getMessage() for r in caplog.records)
    assert any("batch availability check failed" in r.getMessage() for r in caplog.records)


async def test_run_availability_cycle_season_bounded_finalizing_suppressed_on_per_show_omission(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-show lookup failure (the batch call SUCCEEDS but OMITS this one
    show's id -- see ``LibraryPort.season_presence``) is just as inconclusive for
    THIS show as a whole-batch transport failure: the bounded-Finalizing warning
    must stay suppressed for its seasons even though every OTHER pending show
    resolves normally."""
    bad_show = await _seed_show_request(sessionmaker_, tmdb_id=888889)
    await _seed_season(sessionmaker_, media_request_id=bad_show, season_number=1)
    good_show = await _seed_show_request(sessionmaker_, tmdb_id=999990)
    good_season = await _seed_season(sessionmaker_, media_request_id=good_show, season_number=1)

    library = FakeLibrary(
        available_tv_seasons={999990: frozenset({1})},
        raises_for_shows={888889: PlexLibraryError("this show's lookup failed")},
    )
    t0 = datetime.now(UTC)

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=t0)
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=t0 + timedelta(minutes=45)
            )

    assert not any("not confirmed by Plex" in r.getMessage() for r in caplog.records)
    async with sessionmaker_() as session:
        good = await session.get(SeasonRequest, good_season)
    assert good is not None and good.status.value == "available"  # unaffected show still resolves


async def test_run_availability_cycle_movie_path_confirmation_never_uses_title_or_year(
    sessionmaker_: SessionMaker,
) -> None:
    """Two GUID-miss movies share the SAME title/year but sit at DIFFERENT
    folders; only the one whose actual file path matches gets confirmed -- path
    confirmation must never fall back to title/year (a generic-title collision
    must never false-confirm the wrong request)."""
    path_a = "/media/Movies/Same Title (2020)"
    path_b = "/media/Movies/Same Title (2020) (2)"
    request_a = await _seed_movie_request(sessionmaker_, tmdb_id=1111, library_path=path_a)
    request_b = await _seed_movie_request(sessionmaker_, tmdb_id=2222, library_path=path_b)
    async with sessionmaker_() as session:
        for rid in (request_a, request_b):
            row = await session.get(MediaRequest, rid)
            assert row is not None
            row.title = "Same Title"
            row.year = 2020
        await session.commit()

    library = FakeLibrary(movie_file_paths=[f"{path_a}/movie.mkv"])

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, request_a)
        b = await session.get(MediaRequest, request_b)
    assert a is not None and a.status is RequestStatus.available
    assert b is not None and b.status is RequestStatus.completed  # NOT confirmed by title/year


async def test_run_availability_cycle_movie_without_library_path_skips_path_check(
    sessionmaker_: SessionMaker,
) -> None:
    """A legacy row with no ``library_path`` breadcrumb can never be
    path-confirmed -- ``confirm_paths`` must not even be asked about it (an
    empty/``None`` breadcrumb is never a wildcard match)."""
    await _seed_movie_request(sessionmaker_, tmdb_id=555555, library_path=None)
    library = FakeLibrary(movie_file_paths=["/media/Movies/Anything (2020)/x.mkv"])

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    assert library.confirm_paths_calls == []


async def test_run_availability_cycle_path_confirms_a_guid_miss_season(
    sessionmaker_: SessionMaker,
) -> None:
    """TV mirror of the movie path-confirmation fallback: a season's own file
    path confirms it even when its show's tmdb guid never matches at all."""
    show_id = await _seed_show_request(sessionmaker_, tmdb_id=888888)
    library_path = "/media/TV/Some Show (2019)/Season 02"
    season_id = await _seed_season(
        sessionmaker_, media_request_id=show_id, season_number=2, library_path=library_path
    )
    library = FakeLibrary(
        available_tv_seasons={},  # the show's guid never matches -- #158's exact bug
        tv_file_paths=[f"{library_path}/Some Show - S02E01 - Pilot.mkv"],
    )

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None and season_row.status.value == "available"
    assert library.confirm_paths_calls == [("tv", frozenset({library_path}))]


async def test_run_availability_cycle_season_path_miss_stays_completed_and_bounded_warns(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """GUID-miss + path-miss for a TV season: stays honestly ``completed`` and,
    since ``SeasonRequest`` carries no per-season ``completed_at`` mirror, the
    bounded-Finalizing anchor is the in-memory first-observed-miss timestamp --
    exercised here across two ticks (time-mocked, no real sleeping)."""
    show_id = await _seed_show_request(sessionmaker_, tmdb_id=777777)
    library_path = "/media/TV/Some Show (2019)/Season 02"
    season_id = await _seed_season(
        sessionmaker_, media_request_id=show_id, season_number=2, library_path=library_path
    )
    library = FakeLibrary(available_tv_seasons={}, tv_file_paths=[])
    t0 = datetime.now(UTC)

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        # First tick: establishes the in-memory anchor at t0 -- too soon to warn.
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=t0)
    assert not any("not confirmed by Plex" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        # Second tick, 31 minutes later: past the threshold -- warns exactly once.
        async with sessionmaker_() as session:
            await run_availability_cycle(
                library=library, session=session, now=t0 + timedelta(minutes=31)
            )
    warnings = [r for r in caplog.records if "not confirmed by Plex" in r.getMessage()]
    assert len(warnings) == 1
    assert any("Show 777777 season 2" in r.getMessage() for r in warnings)

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None and season_row.status.value == "completed"  # never a new status


async def test_run_availability_cycle_season_path_confirmation_never_uses_title_or_year(
    sessionmaker_: SessionMaker,
) -> None:
    """Two shows share the SAME title/year but sit at DIFFERENT season folders;
    only the one whose file path matches gets confirmed."""
    show_a = await _seed_show_request(sessionmaker_, tmdb_id=3333)
    show_b = await _seed_show_request(sessionmaker_, tmdb_id=4444)
    async with sessionmaker_() as session:
        for sid in (show_a, show_b):
            row = await session.get(MediaRequest, sid)
            assert row is not None
            row.title = "Same Show"
            row.year = 2020
        await session.commit()
    path_a = "/media/TV/Same Show (2020)/Season 01"
    path_b = "/media/TV/Same Show (2020) (2)/Season 01"
    season_a = await _seed_season(
        sessionmaker_, media_request_id=show_a, season_number=1, library_path=path_a
    )
    season_b = await _seed_season(
        sessionmaker_, media_request_id=show_b, season_number=1, library_path=path_b
    )

    library = FakeLibrary(
        available_tv_seasons={}, tv_file_paths=[f"{path_a}/Same Show - S01E01.mkv"]
    )

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(SeasonRequest, season_a)
        b = await session.get(SeasonRequest, season_b)
    assert a is not None and a.status.value == "available"
    assert b is not None and b.status.value == "completed"  # NOT confirmed by title/year


async def test_run_availability_cycle_forgets_stale_bounded_finalizing_state_once_resolved(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Once a row is no longer in the completed set at all (an operator re-armed
    it, here simulated by flipping it back to ``searching``), its bounded-
    Finalizing bookkeeping is forgotten -- a LATER, unrelated row that happens to
    reuse the same id must never inherit a stale duty-cycle bucket."""
    library_path = "/media/Movies/Obsession (2026)"
    completed_at = datetime.now(UTC) - timedelta(minutes=45)
    request_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=999999, library_path=library_path, completed_at=completed_at
    )
    library = FakeLibrary(movie_file_paths=[])

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session, now=datetime.now(UTC))
    assert any("not confirmed by Plex" in r.getMessage() for r in caplog.records)
    assert import_service.is_movie_unconfirmed_tracked(request_id)

    # The operator re-arms the request away from ``completed`` some other way.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        row.status = RequestStatus.searching
        await session.commit()

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    assert not import_service.is_movie_unconfirmed_tracked(request_id)


# --------------------------------------------------------------------------- #
# Anime library routing (ADR-0015)
# --------------------------------------------------------------------------- #


async def test_import_anime_movie_routes_to_anime_movie_root_when_configured(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    anime_movie_root = tmp_path / "anime-library"
    anime_movie_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=True,
    )
    library = FakeLibrary()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        anime_movie_root=anime_movie_root,
    )

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = anime_movie_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    # The normal movies_root tree must stay untouched -- this is a REROUTE, not
    # a copy to both.
    assert not any(movies_root.iterdir())


async def test_import_anime_tv_routes_to_anime_tv_root_when_configured(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    anime_tv_root = tmp_path / "anime-tv"
    anime_tv_root.mkdir()
    release_dir = tmp_path / "downloads" / "Some.Show.S02.1080p.WEB-DL.x264-GRP"
    _make_video(release_dir / "Some.Show.S02E01.1080p.WEB-DL.x264-GRP.mkv")
    download_id, _request_id, season_id = await _seed_tv(sessionmaker_, season=2, is_anime=True)
    library = FakeLibrary()

    record = await _import_tv(
        sessionmaker_,
        download_id,
        tv_root,
        _qbt(release_dir),
        library,
        anime_tv_root=anime_tv_root,
    )

    assert record is not None
    assert record.status == DownloadState.Imported.value
    season_dir = anime_tv_root / "Some Show (2020)" / "Season 02"
    assert (season_dir / "Some Show - S02E01.mkv").exists()
    assert not any(tv_root.iterdir())
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
    assert season_row is not None
    # The breadcrumb records the ANIME path -- what purge/eviction target.
    assert season_row.library_path == str(season_dir)


async def test_import_anime_movie_falls_back_to_movies_root_when_anime_root_unset(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """``is_anime=True`` with no ``anime_movie_root`` configured must behave
    IDENTICALLY to before this feature existed -- routed to ``movies_root``."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=True,
    )
    library = FakeLibrary()

    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()


async def test_import_non_anime_movie_ignores_a_configured_anime_root(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """An anime root must never capture non-anime content, even when set."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    anime_movie_root = tmp_path / "anime-library"
    anime_movie_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=False,
    )
    library = FakeLibrary()

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        library,
        anime_movie_root=anime_movie_root,
    )

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = movies_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()
    assert not any(anime_movie_root.iterdir())


async def test_import_anime_movie_blocked_honestly_when_no_root_at_all_is_configured(
    sessionmaker_: SessionMaker,
) -> None:
    """``is_anime=True``, both ``anime_movie_root`` AND ``movies_root`` unset ->
    the SAME honest, retryable ``ImportBlocked`` as the non-anime case (never a
    crash from ``Path(None)``), firing only on the truly-unset EFFECTIVE root."""
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=True,
    )
    library = FakeLibrary()

    async with sessionmaker_() as session:
        record = await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=FakeQbittorrent(),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=None,
            tv_root="/unused",
            anime_movie_root=None,
        )

    assert record is not None
    assert record.status == DownloadState.ImportBlocked.value
    assert record.failed_reason == "movies library root is not configured"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None and request.status is RequestStatus.import_blocked


async def test_import_anime_only_install_imports_with_movies_root_unset(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """An anime-only install (only ``anime_movie_root`` configured, no plain
    ``movies_root``) must still import an anime movie -- the effective-root
    guard, not the raw ``movies_root``, gates the block."""
    anime_movie_root = tmp_path / "anime-library"
    anime_movie_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, _request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=True,
    )
    library = FakeLibrary()

    async with sessionmaker_() as session:
        record = await import_download(
            download_id=download_id,
            fs=LocalFileSystem(),
            media_probe=FakeMediaProbe(),
            library=library,
            qbt=_qbt(video),
            parser=GuessitParser(),
            profile=default_profile(),
            session=session,
            movies_root=None,
            anime_movie_root=str(anime_movie_root),
        )

    assert record is not None
    assert record.status == DownloadState.Imported.value
    dst = anime_movie_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    assert dst.exists()


async def test_import_anime_movie_persists_anime_library_path_for_eviction(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """The breadcrumb (``MediaRequest.library_path``) records the ANIME path, so
    a later eviction/report-issue purge targets the right (anime) location."""
    movies_root = tmp_path / "library"
    movies_root.mkdir()
    anime_movie_root = tmp_path / "anime-library"
    anime_movie_root.mkdir()
    video = tmp_path / "downloads" / "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    _make_video(video)
    download_id, request_id = await _seed(
        sessionmaker_,
        request_status=RequestStatus.downloading,
        download_status=DownloadState.ImportPending.value,
        is_anime=True,
    )

    record = await _import(
        sessionmaker_,
        download_id,
        movies_root,
        _qbt(video),
        FakeLibrary(),
        anime_movie_root=anime_movie_root,
    )
    assert record is not None and record.status == DownloadState.Imported.value

    dst = anime_movie_root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.library_path == str(dst.parent)


# --------------------------------------------------------------------------- #
# Codex round-9 (PR #117): purge-vs-import path serialization. During an
# eviction's committed-claim window a fast re-request can be importing the
# replacement into the SAME deterministic directory the purge's rmtree is
# walking. The in-process path-guard registry serializes them, both orders:
# first-registered wins, the loser defers fast and retries honestly.
# --------------------------------------------------------------------------- #


async def test_import_defers_while_a_purge_is_deleting_the_destination(
    tmp_path: Path, sessionmaker_: SessionMaker
) -> None:
    """Import side of the ordering rule: with a purge mid-delete on the movie's
    directory, the import attempt is SKIPPED (no claim, no placement, row stays
    ImportPending -- the shape every import cycle re-picks) and succeeds
    normally on the next attempt once the purge has released the path."""
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
    dst_dir = movies_root / "The Matrix (1999)"

    # A purge is mid-delete on the destination directory (as an eviction's slow
    # rmtree would be, off-thread, during its committed-claim window).
    purge_service._register(  # pyright: ignore[reportPrivateUsage]
        str(dst_dir),
        purge_service._ACTIVE_PURGE_PATHS,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)
    finally:
        purge_service._unregister(  # pyright: ignore[reportPrivateUsage]
            str(dst_dir),
            purge_service._ACTIVE_PURGE_PATHS,  # pyright: ignore[reportPrivateUsage]
        )

    assert record is not None
    assert record.status == DownloadState.ImportPending.value  # untouched: retried next cycle
    assert not (dst_dir / "The Matrix (1999).mkv").exists()  # nothing placed under the rmtree
    assert library.scanned == []  # and nothing scanned

    # The purge released the path: the next cycle's attempt imports normally.
    record = await _import(sessionmaker_, download_id, movies_root, _qbt(video), library)
    assert record is not None
    assert record.status == DownloadState.Imported.value
    assert (dst_dir / "The Matrix (1999).mkv").exists()
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.completed


async def test_purge_defers_while_an_import_is_placing_into_the_path(
    tmp_path: Path,
) -> None:
    """Purge side of the ordering rule: with an import mid-placement into the
    path (or any path it contains), purge_library_path defers with an explicit
    ``deferred`` outcome -- eviction leaves its claim for recovery after the
    replacement import settles; report-issue keeps the breadcrumb -- and deletes
    NOTHING. Once the placement releases, the purge proceeds normally."""
    movie_dir = tmp_path / "library" / "The Matrix (1999)"
    movie_file = movie_dir / "The Matrix (1999).mkv"
    _make_video(movie_file, size_bytes=1024)
    fs = LocalFileSystem(library_roots=[str(tmp_path / "library")])

    # The import registered its placement (a FILE inside the directory a purge
    # would delete -- containment must conflict, not just equality).
    assert purge_service.begin_placement(str(movie_file)) is True
    try:
        result = await purge_service.purge_library_path(fs, str(movie_dir))
    finally:
        purge_service.end_placement(str(movie_file))

    assert result.outcome is purge_service.PurgeOutcome.deferred
    assert result.detail is not None and "deferred" in result.detail
    assert movie_file.exists()  # nothing deleted under the placement

    # Placement released: the purge now proceeds.
    result = await purge_service.purge_library_path(fs, str(movie_dir))
    assert result.outcome is purge_service.PurgeOutcome.deleted
    assert not movie_dir.exists()


async def test_begin_placement_refuses_while_a_purge_holds_the_path(
    tmp_path: Path,
) -> None:
    """The reverse registration order: a placement beginning while a purge holds
    a conflicting path is refused up front (the import defers), and allowed
    again once the purge releases."""
    season_dir = tmp_path / "library" / "Show (2020)" / "Season 01"
    purge_service._register(  # pyright: ignore[reportPrivateUsage]
        str(season_dir),
        purge_service._ACTIVE_PURGE_PATHS,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        # Equality AND containment both conflict.
        assert purge_service.begin_placement(str(season_dir)) is False
        assert purge_service.begin_placement(str(season_dir / "ep01.mkv")) is False
    finally:
        purge_service._unregister(  # pyright: ignore[reportPrivateUsage]
            str(season_dir),
            purge_service._ACTIVE_PURGE_PATHS,  # pyright: ignore[reportPrivateUsage]
        )
    assert purge_service.begin_placement(str(season_dir)) is True
    purge_service.end_placement(str(season_dir))


# --- Change 2: self-heal false 'available' movie claims -----------------------
# (request-dedup healing, spec-request-dedup-healing.md)


async def test_heal_collapses_null_path_available_onto_real_path_sibling(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """The Mario repro: row A is 'available' with library_path=None (the false
    short-circuit claim); row B, same tmdb + same owner (both ownerless here), is
    'available' with a REAL library_path. The heal collapses row A onto row B."""
    tmdb_id = 1226863
    row_a = await _seed_movie_request(
        sessionmaker_, tmdb_id=tmdb_id, status=RequestStatus.available, library_path=None
    )
    row_b = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path="/movies/mario",
    )
    library = FakeLibrary()

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, row_a)
        b = await session.get(MediaRequest, row_b)
    assert a is None  # collapsed (deleted)
    assert (
        b is not None and b.status == RequestStatus.available and b.library_path == "/movies/mario"
    )
    assert any(
        "healed duplicate false-available movie row" in r.getMessage() for r in caplog.records
    )


async def test_heal_sibling_collapse_cas_skips_when_ownership_changed_since_read(
    sessionmaker_: SessionMaker,
) -> None:
    """``delete_false_available_sibling_collapse`` must not delete a row whose
    ownership no longer matches the caller's snapshot -- the CAS guarding
    the exact race a concurrent user create's adoption
    (``request_service._claim_dedup_winner_if_unowned``) can win in the
    multi-second window between the heal pass's top-of-cycle candidate read
    and this delete: an ownerless false-available row is claimed by user X's
    concurrent create (``find_in_library`` ranks it above a foreign-owned
    real-path sibling) before the heal ever reaches its delete call. A plain
    unconditional delete would silently vanish X's just-succeeded request."""
    tmdb_id = 9001
    row_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=tmdb_id, status=RequestStatus.available, library_path=None
    )
    async with sessionmaker_() as session:
        owner_x = User(username="owner-x", permissions=0)
        session.add(owner_x)
        await session.commit()
        owner_x_id = owner_x.id
        # Simulate a concurrent create claiming the ownerless candidate in the
        # window between the heal pass's candidate read and its delete.
        row = await session.get(MediaRequest, row_id)
        assert row is not None
        row.user_id = owner_x_id
        await session.commit()

    async with sessionmaker_() as session:
        repo = SqlRequestRepository(session)
        deleted = await repo.delete_false_available_sibling_collapse(row_id, expected_user_id=None)
        await session.commit()
    assert deleted is False  # ownership moved off the read snapshot -- CAS refuses

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, row_id)
    assert row is not None  # NOT deleted -- X's now-claimed row survives
    assert row.user_id == owner_x_id
    assert row.status == RequestStatus.available


async def test_heal_restamps_when_genuinely_present(sessionmaker_: SessionMaker) -> None:
    """No sibling, but a live GUID check confirms Plex genuinely has it after all
    -- re-stamp ``library_verified_at``, leave the row 'available'/NULL-path, and
    CONVERGE (P1 regression): a second reconcile tick must not re-probe Plex for
    this row at all -- it has permanently exited the heal scan population."""
    row_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=777, status=RequestStatus.available, library_path=None
    )
    async with sessionmaker_() as session:
        before = await session.get(MediaRequest, row_id)
        assert before is not None
        assert before.library_verified_at is None

    library = FakeLibrary(available={777})
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        after = await session.get(MediaRequest, row_id)
    assert after is not None
    assert after.status == RequestStatus.available
    assert after.library_path is None
    assert after.library_verified_at is not None  # re-stamped
    assert after.available_heal_verified_at is not None  # converged: exits future scans
    assert library.present_ids_calls == 1

    # Second tick: nothing else pending/completed/false-available exists, so a
    # genuinely-converged row must not trigger ANY further Plex call -- the old
    # per-row ``is_available(use_cache=False)`` would have paged the whole movie
    # library again here, every 15s, forever.
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        after_second_tick = await session.get(MediaRequest, row_id)
    assert after_second_tick is not None
    assert after_second_tick.status == RequestStatus.available
    assert after_second_tick.library_path is None
    assert library.present_ids_calls == 1  # unchanged: the row was never re-probed
    assert library.is_available_calls == 0


async def test_heal_rearms_to_pending_when_absent_and_no_sibling(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """No sibling and Plex genuinely does not have it -- re-arm to 'pending' for a
    fresh, honest search rather than leaving a false claim standing."""
    row_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=778, status=RequestStatus.available, library_path=None
    )
    library = FakeLibrary(available=set())

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, row_id)
    assert row is not None
    assert row.status == RequestStatus.pending
    assert row.library_verified_at is None
    assert row.completed_at is None
    assert row.library_path is None
    assert any("re-armed to pending" in r.getMessage() for r in caplog.records)


async def test_heal_ignores_available_rows_with_a_library_path(sessionmaker_: SessionMaker) -> None:
    """A normally-imported 'available' row (a real library_path) is never a false
    claim -- the heal must never touch it (no-false-positive)."""
    row_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=779, status=RequestStatus.available, library_path="/movies/x"
    )
    library = FakeLibrary()  # would answer False for is_available -- must never be asked
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, row_id)
    assert row is not None
    assert row.status == RequestStatus.available
    assert row.library_path == "/movies/x"
    assert library.is_available_calls == 0


async def test_heal_leaves_tv_available_parent_untouched(sessionmaker_: SessionMaker) -> None:
    """A TV parent MediaRequest 'available' with library_path=None is the NORMAL,
    legitimate rollup shape (the breadcrumb lives per-season) -- the heal is
    movie-only and must never select or mutate it."""
    show_id = await _seed_show_request(sessionmaker_, tmdb_id=1001, status=RequestStatus.available)
    library = FakeLibrary()
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
    assert show is not None
    assert show.status == RequestStatus.available
    assert show.library_path is None
    assert library.is_available_calls == 0


async def test_heal_differing_owner_sibling_guid_hit_rearms_not_stamps(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """A real-path sibling owned by a DIFFERENT user must NOT collapse the
    NULL-path row (issue #58: a background heal has no user context and must
    never strand one owner's per-user list view). Finding 4 (GUID-batch-hit
    shape): even though the GUID batch reports the tmdb present -- because the
    cross-owner sibling's copy IS in Plex -- the candidate's OWN owner still
    never had their own copy, so it must RE-ARM (not one-way-converge/stamp on
    another owner's file, which would strand it permanently 'available' with no
    re-heal path if that sibling is later evicted)."""
    tmdb_id = 780
    async with sessionmaker_() as session:
        owner_a = User(username="owner-a", permissions=0)
        owner_b = User(username="owner-b", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    row_a = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path="/movies/other-owner",
        user_id=owner_b_id,
    )
    library = FakeLibrary(available={tmdb_id})  # GUID present -> but only a cross-owner copy

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, row_a)
    assert a is not None  # NOT deleted
    assert a.status == RequestStatus.pending  # re-armed, NOT stamped/converged
    assert a.library_path is None
    assert a.available_heal_verified_at is None  # never one-way-stamped on another owner's file
    # The GUID hit was conclusive on its own -- no per-row path check was needed.
    assert library.confirm_paths_calls == []
    assert any("only under a DIFFERENT owner's copy" in r.getMessage() for r in caplog.records)


async def test_heal_cross_owner_sibling_corroborated_by_path_still_rearms(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """A cross-owner sibling's ``library_path`` is left uncollapsed by the
    ownership guard (issue #58) AND the GUID batch misses this row -- the path
    fallback (issue #158) corroborates the sibling's directory is genuinely
    indexed in Plex, but that is a DIFFERENT owner's copy, not this row's own.
    The row must still re-arm to pending (never a one-way convergence stamp
    keyed on another owner's file, which would strand it permanently
    'available' with no re-heal path if that sibling is later evicted)."""
    tmdb_id = 782
    async with sessionmaker_() as session:
        owner_a = User(username="owner-a2", permissions=0)
        owner_b = User(username="owner-b2", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    row_a = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path="/movies/cross-owner",
        user_id=owner_b_id,
    )
    # GUID misses this tmdb id entirely (available=set()), but the sibling's
    # directory IS indexed under Plex's known movie file paths.
    library = FakeLibrary(available=set(), movie_file_paths=["/movies/cross-owner/movie.mkv"])

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, row_a)
    assert a is not None  # NOT deleted
    assert a.status == RequestStatus.pending  # re-armed, not converged
    assert a.library_path is None
    assert a.available_heal_verified_at is None  # never one-way-stamped on another owner's file
    assert library.confirm_paths_calls == [("movie", frozenset({"/movies/cross-owner"}))]
    assert any(
        "cross-owner sibling's library_path corroborated" in r.getMessage() for r in caplog.records
    )
    assert any("re-armed to pending" in r.getMessage() for r in caplog.records)


async def test_heal_cross_owner_sibling_unconfirmed_by_path_still_rearms(
    sessionmaker_: SessionMaker,
) -> None:
    """The cross-owner sibling's path fallback is consulted but does NOT
    corroborate (Plex genuinely has neither the GUID nor that directory) --
    the row still honestly re-arms rather than being stuck 'available'."""
    tmdb_id = 783
    async with sessionmaker_() as session:
        owner_a = User(username="owner-a3", permissions=0)
        owner_b = User(username="owner-b3", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    row_a = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path="/movies/nonexistent",
        user_id=owner_b_id,
    )
    library = FakeLibrary(available=set())  # no movie_file_paths -> confirm_paths finds nothing

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, row_a)
    assert a is not None
    assert a.status == RequestStatus.pending
    assert library.confirm_paths_calls == [("movie", frozenset({"/movies/nonexistent"}))]


async def test_heal_cross_owner_sibling_path_check_error_leaves_row_untouched(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """The batched ``confirm_paths`` corroboration call itself fails (Plex
    unreachable) -- honesty over silence: the row is left completely
    unchanged rather than misread as 'absent' and wrongly re-armed."""
    tmdb_id = 784
    async with sessionmaker_() as session:
        owner_a = User(username="owner-a4", permissions=0)
        owner_b = User(username="owner-b4", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    row_a = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    await _seed_movie_request(
        sessionmaker_,
        tmdb_id=tmdb_id,
        status=RequestStatus.available,
        library_path="/movies/unreachable",
        user_id=owner_b_id,
    )
    library = FakeLibrary(available=set(), confirm_paths_raises=PlexLibraryError("unreachable"))

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        a = await session.get(MediaRequest, row_a)
    assert a is not None
    assert a.status == RequestStatus.available
    assert a.library_path is None
    assert a.available_heal_verified_at is None
    assert any(
        "cross-owner sibling path corroboration failed" in r.getMessage() for r in caplog.records
    )


async def test_heal_collapses_false_row_onto_active_resubmit(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 1: a false-'available' row coexisting with an active (``pending``)
    re-request for the SAME media must NOT re-arm -- re-arming to 'pending' would
    violate ``uq_media_requests_active`` (whose predicate already includes
    'pending') and the uncaught ``IntegrityError`` would abort the whole
    availability cycle and repeat forever. Instead the false row COLLAPSES onto
    the active re-request (which already carries the user's intent), and the
    cycle continues to heal the next candidate."""
    async with sessionmaker_() as session:
        owner_a = User(username="owner-active", permissions=0)
        session.add(owner_a)
        await session.commit()
        owner_a_id = owner_a.id
    # The false claim + the active re-request that already owns the dedup slot.
    false_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=785,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    active_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=785,
        status=RequestStatus.pending,
        library_path=None,
        user_id=owner_a_id,
    )
    # A SECOND, independent false claim (different media) that must STILL heal
    # after the first -- proving the cycle did not abort on the collision case.
    other_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=786,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    library = FakeLibrary(available=set())  # neither media present in Plex

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        active_row = await session.get(MediaRequest, active_id)
        other_row = await session.get(MediaRequest, other_id)
    assert false_row is None  # collapsed (deleted), never a colliding re-arm
    assert active_row is not None and active_row.status == RequestStatus.pending  # intent kept
    assert other_row is not None and other_row.status == RequestStatus.pending  # cycle continued
    assert any("collapsed onto an active re-request" in r.getMessage() for r in caplog.records)


async def test_heal_cross_owner_active_resubmit_leaves_row_untouched(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 1, cross-owner sub-case: the active re-request holding the dedup
    slot belongs to a DIFFERENT user. Re-arming would collide; collapsing would
    strand this owner's per-user list view -- so the row is left for next cycle
    with an honest log rather than either unsafe outcome."""
    async with sessionmaker_() as session:
        owner_a = User(username="owner-xa", permissions=0)
        owner_b = User(username="owner-xb", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    false_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=787,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    active_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=787,
        status=RequestStatus.downloading,
        library_path=None,
        user_id=owner_b_id,
    )
    library = FakeLibrary(available=set())

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        active_row = await session.get(MediaRequest, active_id)
    assert false_row is not None  # NOT deleted (would strand owner A)
    assert false_row.status == RequestStatus.available and false_row.library_path is None
    assert active_row is not None and active_row.status == RequestStatus.downloading
    assert any(
        "active re-request owned by a DIFFERENT user" in r.getMessage() for r in caplog.records
    )


async def test_heal_rearm_collision_degrades_to_logged_skip_not_aborted_cycle(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 1, belt-and-suspenders: an active re-request appearing in the
    check->re-arm gap (here the ``find_active`` guard is forced to miss it) makes
    the re-arm's CAS UPDATE collide on ``uq_media_requests_active`` at the DB
    level. The resulting ``IntegrityError`` must be caught and degrade to a rolled-
    back, logged skip -- never propagate and abort the whole availability cycle.
    A second, independent false claim must still heal afterward."""

    async def blind_find_active(self: SqlRequestRepository, tmdb_id: int, media_type: str) -> None:
        return None  # simulate the guard missing a concurrent active re-request

    monkeypatch.setattr(SqlRequestRepository, "find_active", blind_find_active)

    false_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=791, status=RequestStatus.available, library_path=None
    )
    # A REAL active row owns the uq_media_requests_active slot for tmdb 791, so the
    # re-arm-to-pending below genuinely collides at the DB even though the guard missed it.
    active_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=791, status=RequestStatus.pending, library_path=None
    )
    other_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=792, status=RequestStatus.available, library_path=None
    )
    library = FakeLibrary(available=set())  # both absent -> both would re-arm

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            # Must NOT raise IntegrityError -- the cycle completes.
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        active_row = await session.get(MediaRequest, active_id)
        other_row = await session.get(MediaRequest, other_id)
    assert false_row is not None  # left untouched: the colliding re-arm was rolled back
    assert false_row.status == RequestStatus.available and false_row.library_path is None
    assert active_row is not None and active_row.status == RequestStatus.pending
    assert other_row is not None and other_row.status == RequestStatus.pending  # cycle continued
    assert any(
        "collided with a concurrent active re-request" in r.getMessage() for r in caplog.records
    )


async def test_heal_active_sibling_takes_precedence_over_cross_owner_realpath(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Self-review (a): a candidate with BOTH an active same-owner re-request AND
    a cross-owner real-path sibling must collapse onto the ACTIVE re-request (the
    row that holds the dedup slot), never re-arm -- re-arming would collide with
    that active row, and stamping/converging on the cross-owner file is wrong.
    The active-slot guard runs before the cross-owner re-arm branch."""
    async with sessionmaker_() as session:
        owner_a = User(username="owner-both-a", permissions=0)
        owner_b = User(username="owner-both-b", permissions=0)
        session.add_all([owner_a, owner_b])
        await session.commit()
        owner_a_id, owner_b_id = owner_a.id, owner_b.id
    false_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=790,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    await _seed_movie_request(
        sessionmaker_,
        tmdb_id=790,
        status=RequestStatus.available,
        library_path="/movies/cross",  # cross-owner real-path sibling
        user_id=owner_b_id,
    )
    active_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=790,
        status=RequestStatus.pending,  # same-owner active re-request holding the slot
        library_path=None,
        user_id=owner_a_id,
    )
    library = FakeLibrary(available=set(), movie_file_paths=["/movies/cross/movie.mkv"])

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        active_row = await session.get(MediaRequest, active_id)
    assert false_row is None  # collapsed onto the active re-request, never re-armed
    assert active_row is not None and active_row.status == RequestStatus.pending
    assert any("collapsed onto an active re-request" in r.getMessage() for r in caplog.records)


async def test_heal_owned_candidate_adopts_ownerless_sibling_then_collapses(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 2: an OWNED candidate whose only real-path sibling is OWNERLESS
    must not be plain-collapsed (deleting the owned row drops it from that user's
    per-user list, and the ownerless sibling carries no owner to inherit the
    visibility). The heal ADOPTS the ownerless sibling for the candidate's owner
    FIRST, then collapses onto it -- so the owner keeps the (now real-path) row."""
    async with sessionmaker_() as session:
        owner_a = User(username="owner-adopt", permissions=0)
        session.add(owner_a)
        await session.commit()
        owner_a_id = owner_a.id
    false_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=788,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    sibling_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=788,
        status=RequestStatus.available,
        library_path="/movies/ownerless",
        user_id=None,  # ownerless real-path sibling
    )
    library = FakeLibrary()  # collapse needs no Plex answer

    with caplog.at_level(logging.INFO, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        sibling_row = await session.get(MediaRequest, sibling_id)
    assert false_row is None  # collapsed
    assert sibling_row is not None
    assert sibling_row.user_id == owner_a_id  # adopted FIRST so the owner keeps visibility
    assert sibling_row.library_path == "/movies/ownerless"
    assert any("adopted an ownerless sibling" in r.getMessage() for r in caplog.records)


async def test_heal_owned_candidate_ownerless_sibling_lost_adoption_race_skips(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 2, adoption-race sub-case: another user claims the ownerless
    sibling in the window before the heal's own ``claim_if_unowned`` fires (here
    forced to return ``False``). The heal must NOT then collapse the owned
    candidate onto a sibling it no longer safely owns -- it rolls back and logs a
    skip, leaving the candidate for next cycle rather than stranding the owner."""
    async with sessionmaker_() as session:
        owner_a = User(username="owner-race", permissions=0)
        session.add(owner_a)
        await session.commit()
        owner_a_id = owner_a.id
    false_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=789,
        status=RequestStatus.available,
        library_path=None,
        user_id=owner_a_id,
    )
    sibling_id = await _seed_movie_request(
        sessionmaker_,
        tmdb_id=789,
        status=RequestStatus.available,
        library_path="/movies/raced",
        user_id=None,
    )

    async def losing_claim(self: SqlRequestRepository, request_id: int, user_id: int) -> bool:
        return False  # simulate the sibling being claimed by another user first

    monkeypatch.setattr(SqlRequestRepository, "claim_if_unowned", losing_claim)
    library = FakeLibrary()

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        false_row = await session.get(MediaRequest, false_id)
        sibling_row = await session.get(MediaRequest, sibling_id)
    assert false_row is not None  # NOT deleted -- adoption failed, so no collapse
    assert false_row.status == RequestStatus.available and false_row.library_path is None
    assert sibling_row is not None  # untouched
    assert any("could not adopt the ownerless sibling" in r.getMessage() for r in caplog.records)


async def test_heal_leaves_row_when_live_check_errors(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    """The shared batched Plex check itself fails (Plex unreachable) -- honesty
    over silence: the row is left completely unchanged (unknown != absent), and
    a warning is logged rather than the row being wrongly re-armed."""
    row_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=781, status=RequestStatus.available, library_path=None
    )
    library = FakeLibrary(raises=PlexLibraryError("plex unreachable"))

    with caplog.at_level(logging.WARNING, logger=_IMPORT_SERVICE_LOGGER):
        async with sessionmaker_() as session:
            await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, row_id)
    assert row is not None
    assert row.status == RequestStatus.available
    assert row.library_path is None
    assert row.available_heal_verified_at is None
    assert any("batch availability check failed" in r.getMessage() for r in caplog.records)


async def test_heal_convergence_frees_budget_for_higher_id_false_claim(
    sessionmaker_: SessionMaker,
) -> None:
    """P1 regression: enough already-genuinely-present low-id rows to fill the
    whole per-tick heal budget must not permanently starve a later, higher-id
    GENUINE false claim out of ever being scanned. Each genuinely-present row
    converges (stamps ``available_heal_verified_at`` and exits the scan
    population) after being checked once, so the SECOND tick's budget is freed
    up to finally reach the false claim."""
    limit = import_service._HEAL_FALSE_AVAILABLE_LIMIT  # pyright: ignore[reportPrivateUsage]
    genuine_tmdb_ids = list(range(3000, 3000 + limit + 2))
    for tmdb_id in genuine_tmdb_ids:
        await _seed_movie_request(
            sessionmaker_, tmdb_id=tmdb_id, status=RequestStatus.available, library_path=None
        )
    # Created LAST, so its row id is the highest -- id-ascending + the bounded
    # limit is exactly what starved it before this fix.
    false_claim_id = await _seed_movie_request(
        sessionmaker_, tmdb_id=3999, status=RequestStatus.available, library_path=None
    )
    library = FakeLibrary(available=set(genuine_tmdb_ids))  # false claim absent, no sibling

    # Tick 1: the low-id genuine rows fill the whole budget; the false claim
    # (highest id) is outside this tick's window and stays untouched.
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)
    async with sessionmaker_() as session:
        after_tick_1 = await session.get(MediaRequest, false_claim_id)
    assert after_tick_1 is not None
    assert after_tick_1.status == RequestStatus.available

    # Tick 2: every genuine row converged on tick 1 (stamped, excluded from the
    # scan query), freeing the budget -- the false claim is now reachable and
    # heals (re-armed to 'pending' for a fresh, honest search).
    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)
    async with sessionmaker_() as session:
        after_tick_2 = await session.get(MediaRequest, false_claim_id)
    assert after_tick_2 is not None
    assert after_tick_2.status == RequestStatus.pending


async def test_heal_is_bounded_per_cycle(sessionmaker_: SessionMaker) -> None:
    """More false-available rows exist than the per-cycle cap -- at most the cap
    is processed in one tick, keeping the reconcile pass cheap."""
    limit = import_service._HEAL_FALSE_AVAILABLE_LIMIT  # pyright: ignore[reportPrivateUsage]
    extra = 5
    ids = [
        await _seed_movie_request(
            sessionmaker_,
            tmdb_id=2000 + i,
            status=RequestStatus.available,
            library_path=None,
        )
        for i in range(limit + extra)
    ]
    library = FakeLibrary(available=set())  # every row would re-arm to pending if processed

    async with sessionmaker_() as session:
        await run_availability_cycle(library=library, session=session)

    async with sessionmaker_() as session:
        statuses = []
        for row_id in ids:
            row = await session.get(MediaRequest, row_id)
            assert row is not None
            statuses.append(row.status)
    processed = sum(1 for s in statuses if s == RequestStatus.pending)
    assert processed == limit
    assert statuses.count(RequestStatus.available) == extra
