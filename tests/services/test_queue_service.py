"""queue_service — the auto-fail blocklist-and-research path beyond grace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.services import queue_service
from tests.web.fakes import FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "f" * 40
_TITLE = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_INDEXER = "FakeIndexer"


async def _seed_request_with_download(
    sm: SessionMaker, *, first_seen_at: datetime, indexer: str | None = _INDEXER
) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=603,
                first_seen_at=first_seen_at,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=603,
                torrent_hash=_HASH,
                event_type=DownloadHistoryEvent.grabbed,
                source_title=_TITLE,
                indexer=indexer,
            )
        )
        await session.commit()
        return request.id


async def test_missing_beyond_grace_fails_blocklists_and_researches(
    sessionmaker_: SessionMaker,
) -> None:
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    # The client reports nothing — the torrent is gone beyond the grace window.
    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        queue = await queue_service.reconcile_and_list(qbt, session)

    # The blocklist + re-search fired, so the download completed FailedPending ->
    # Failed and drops out of the active queue (no zombie row left behind).
    statuses = {item.torrent_hash: item.status for item in queue}
    assert _HASH not in statuses

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        request = await session.get(MediaRequest, request_id)
        failed = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()

    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _HASH
    assert blocklist[0].media_type == MediaType.movie
    # The blocklist entry carries the real grabbed title (from history), not the hash.
    assert blocklist[0].source_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
    assert request is not None
    assert request.status is RequestStatus.searching
    # The row reached the terminal Failed state (not stranded at failed_pending).
    assert failed.status == "failed"
    # ADR-0014 seeding-leak fix: the reconcile-driven failure removed the torrent
    # WITH its data (mirrors the operator mark-failed path in test_queue.py).
    assert qbt.removed == [(_HASH, True)]


async def test_auto_fail_blocklist_records_indexer_and_blocks_hashless_candidate(
    sessionmaker_: SessionMaker,
) -> None:
    """The auto-fail blocklist row carries the originating indexer (recovered from
    history), so a later candidate from that indexer with NO info_hash is rejected
    by the pure tier-2 (title + indexer) check — blocklist-then-research holds for
    hashless feeds."""
    await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        entry = (await session.execute(select(Blocklist))).scalar_one()
        assert entry.indexer == _INDEXER

        repo = SqlBlocklistRepository(session)
        # A re-searched candidate that exposes NO info_hash (only title+indexer) is
        # still rejected via tier 2 — the bug was an indexer=None blocklist row.
        blocked = await repo.is_blocklisted(
            tmdb_id=603,
            torrent_hash=None,
            source_title=_TITLE,
            indexer=_INDEXER,
            media_type="movie",
        )
        assert blocked is True
        # A different indexer with the same title is NOT blocked (tier-2 is scoped).
        other = await repo.is_blocklisted(
            tmdb_id=603,
            torrent_hash=None,
            source_title=_TITLE,
            indexer="OtherIndexer",
            media_type="movie",
        )
        assert other is False


async def test_live_progress_persisted_without_state_change(
    sessionmaker_: SessionMaker,
) -> None:
    """A download advancing 10%->50% while staying 'Downloading' emits NO state
    transition from the pure reconciler, but reconcile_and_list must still persist
    the live progress/seed_ratio — otherwise the queue shows stale progress."""
    async with sessionmaker_() as session:
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            tmdb_id=603,
            progress=0.1,
            seed_ratio=0.0,
        )
        session.add(download)
        await session.commit()

    # The client reports the SAME mapped state ('downloading') but further along.
    live = DownloadStatus(
        info_hash=_HASH,
        name="Some.Movie",
        raw_state="downloading",
        progress=0.5,
        ratio=1.2,
    )
    async with sessionmaker_() as session:
        queue = await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[live]), session)

    item = next(i for i in queue if i.torrent_hash == _HASH)
    assert item.status == "downloading"  # unchanged state
    assert item.progress == 0.5  # progress moved despite no transition
    assert item.seed_ratio == 1.2

    async with sessionmaker_() as session:
        persisted = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert persisted.progress == 0.5
    assert persisted.seed_ratio == 1.2


async def test_mark_failed_routes_downloading_through_failed_pending(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        download = Download(torrent_hash=_HASH, status="downloading", tmdb_id=603)
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=False
        )
    assert record.status == "failed"


async def test_mark_failed_routes_import_pending_through_failed_pending(
    sessionmaker_: SessionMaker,
) -> None:
    """Import is deferred, so a completed torrent sits in import_pending. The
    operator must be able to mark-failed/blocklist it to re-search — it routes
    ImportPending -> FailedPending -> Failed."""
    async with sessionmaker_() as session:
        download = Download(torrent_hash=_HASH, status="import_pending", tmdb_id=603)
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=True
        )
    assert record.status == "failed"

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _HASH
    assert blocklist[0].media_type is MediaType.movie


async def test_mark_failed_does_not_overwrite_importing_claim_from_stale_session(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status="import_pending",
            media_request_id=request.id,
            tmdb_id=603,
        )
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as stale_session:
        stale = await stale_session.get(Download, download_id)
        assert stale is not None and stale.status == "import_pending"

        async with sessionmaker_() as importer_session:
            claimed = await SqlDownloadRepository(importer_session).update_status_if_in(
                download_id,
                "importing",
                frozenset({"import_pending"}),
            )
            assert claimed is True
            await importer_session.commit()

        with pytest.raises(queue_service.InvalidStateTransitionError):
            await queue_service.mark_failed(stale_session, download_id=download_id, blocklist=True)

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert row is not None and row.status == "importing"
    assert row.failed_reason is None
    assert blocklist == []


async def test_mark_failed_without_blocklist_rearms_request(
    sessionmaker_: SessionMaker,
) -> None:
    """mark_failed(blocklist=False) must still reconcile the owning request: the
    download goes terminal Failed, so the request cannot stay 'downloading' with no
    active download (a dishonest state). The blocklist flag gates ONLY whether a
    Blocklist row is written, not the request re-arm."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            media_request_id=request.id,
            tmdb_id=603,
        )
        session.add(download)
        await session.commit()
        request_id, download_id = request.id, download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=False
        )
    assert record.status == "failed"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching  # re-armed despite blocklist=False
    assert blocklist == []  # but no blocklist row was written


async def test_reconcile_applies_completed_and_keeps_client_missing_within_grace(
    sessionmaker_: SessionMaker,
) -> None:
    """The background path (reconcile_and_list) still advances a completed torrent to
    import_pending and keeps an absent-but-in-grace torrent as client_missing. These
    write semantics moved OFF GET /queue (now passive) onto the reconcile loop."""
    async with sessionmaker_() as session:
        completed = Download(torrent_hash="a" * 40, status="downloading", tmdb_id=603)
        missing = Download(
            torrent_hash="b" * 40,
            status="client_missing",
            tmdb_id=603,
            first_seen_at=datetime.now(UTC),  # within the 10-minute grace
        )
        session.add_all([completed, missing])
        await session.commit()
        completed_id, missing_id = completed.id, missing.id

    qbt = FakeQbittorrent(
        statuses=[DownloadStatus(info_hash="a" * 40, name="done.torrent", raw_state="stoppedUP")]
    )
    async with sessionmaker_() as session:
        queue = await queue_service.reconcile_and_list(qbt, session)

    by_id = {item.id: item.status for item in queue}
    assert by_id[completed_id] == "import_pending"
    assert by_id[missing_id] == "client_missing"


async def _seed_tv_request_with_download(
    sm: SessionMaker, *, season: int, first_seen_at: datetime
) -> tuple[int, int]:
    """Insert a tv show + one tracked season + a download for that season."""
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=request.id, season_number=season, status="downloading"
        )
        session.add(season_row)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=603,
                season=season,
                first_seen_at=first_seen_at,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=603,
                torrent_hash=_HASH,
                event_type=DownloadHistoryEvent.grabbed,
                source_title=_TITLE,
                indexer=_INDEXER,
            )
        )
        await session.commit()
        return request.id, season_row.id


async def test_missing_beyond_grace_for_tv_rearms_the_season_not_the_request_directly(
    sessionmaker_: SessionMaker,
) -> None:
    """``_handle_failed`` routes a TV download's re-arm through
    ``season_request_service`` -- the OWNING SEASON moves to 'searching' and the
    parent's computed rollup reflects that, rather than the request being set
    directly (which would fight the rollup on the next season transition)."""
    request_id, season_id = await _seed_tv_request_with_download(
        sessionmaker_, season=2, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None
    assert season_row.status.value == "searching"
    assert request is not None
    assert request.status is RequestStatus.searching  # rollup of the one tracked season


async def test_mark_failed_for_tv_rearms_the_season_not_the_request_directly(
    sessionmaker_: SessionMaker,
) -> None:
    """``mark_failed`` mirrors the reconcile-driven re-arm for a TV download: the
    SEASON re-arms to 'searching', and the parent's rollup reflects it."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        season_row = SeasonRequest(
            media_request_id=request.id, season_number=1, status="downloading"
        )
        session.add(season_row)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            media_request_id=request.id,
            tmdb_id=603,
            season=1,
        )
        session.add(download)
        await session.commit()
        request_id, season_id, download_id = request.id, season_row.id, download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=False
        )
    assert record.status == "failed"

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None
    assert season_row.status.value == "searching"
    assert request is not None
    assert request.status is RequestStatus.searching


async def test_missing_beyond_grace_never_regresses_an_already_available_season(
    sessionmaker_: SessionMaker,
) -> None:
    """A season a PRIOR download already finished (``available``) must never be
    dragged back to 'searching' by a LATER, unrelated download for that same
    season (e.g. a supplementary per-episode re-grab) going missing beyond grace.
    The failing download's OWN row still moves to Failed -- fully visible in the
    queue -- but the season/parent rollup is protected from regressing past a
    state Plex already confirmed."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(request)
        await session.flush()
        season_row = SeasonRequest(media_request_id=request.id, season_number=1, status="available")
        session.add(season_row)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_HASH,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=603,
                season=1,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
        )
        await session.commit()
        request_id, season_id = request.id, season_row.id

    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert season_row is not None
    assert season_row.status.value == "available"  # untouched -- never regressed
    assert request is not None
    assert request.status is RequestStatus.available  # rollup unaffected
    assert download.status == "failed"  # this attempt's own failure stays visible


class _TxRecordingQbt(FakeQbittorrent):
    """A :class:`FakeQbittorrent` that records whether the session was mid-transaction
    at each ``remove`` -- so a test can prove the reconcile-driven removal runs AFTER
    the commit (``in_transaction()`` False), not inside the open write transaction."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(statuses=[])
        self._session = session
        self.in_tx_at_remove: list[bool] = []

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        self.in_tx_at_remove.append(self._session.in_transaction())
        await super().remove(info_hash, delete_files=delete_files)


async def test_reconcile_removes_the_failed_torrent_after_the_commit(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding #3: qbt.remove is external client I/O, so it must run AFTER
    ``reconcile_and_list``'s final commit, never inside the open reconcile write
    transaction (which would hold SQLite's write lock across the round-trip)."""
    await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    async with sessionmaker_() as session:
        qbt = _TxRecordingQbt(session)
        await queue_service.reconcile_and_list(qbt, session)

    # The removal happened (seeding-leak fix) AND it happened post-commit (outside a
    # transaction), proving it no longer runs inside the reconcile write transaction.
    assert qbt.removed == [(_HASH, True)]
    assert qbt.in_tx_at_remove == [False]


async def test_reconcile_does_not_remove_when_the_commit_fails(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #3's honesty guarantee: because the removal is deferred to AFTER the
    commit, a commit failure means NO torrent removal was even attempted -- the DB and
    the client stay consistent (nothing deleted against a rolled-back transaction)."""
    await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:

        async def _boom() -> None:
            raise RuntimeError("commit blew up")

        monkeypatch.setattr(session, "commit", _boom)
        with pytest.raises(RuntimeError):
            await queue_service.reconcile_and_list(qbt, session)

    assert qbt.removed == []  # the post-commit removal loop was never reached


async def test_mark_failed_never_regresses_an_already_available_season(
    sessionmaker_: SessionMaker,
) -> None:
    """``mark_failed`` mirrors the reconcile-driven guard above: an operator
    failing a SECOND, later download for an already-``available`` season must not
    re-arm that season to 'searching'."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(request)
        await session.flush()
        season_row = SeasonRequest(media_request_id=request.id, season_number=1, status="available")
        session.add(season_row)
        await session.flush()
        download = Download(
            torrent_hash=_HASH,
            status="downloading",
            media_request_id=request.id,
            tmdb_id=603,
            season=1,
        )
        session.add(download)
        await session.commit()
        request_id, season_id, download_id = request.id, season_row.id, download.id

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, FakeQbittorrent(), download_id=download_id, blocklist=False
        )
    assert record.status == "failed"

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        request = await session.get(MediaRequest, request_id)
    assert season_row is not None
    assert season_row.status.value == "available"  # untouched -- never regressed
    assert request is not None
    assert request.status is RequestStatus.available


async def test_reconcile_transition_does_not_overwrite_concurrent_status_change(
    sessionmaker_: SessionMaker,
) -> None:
    """Reconcile snapshots active rows, then awaits qBittorrent. A status committed
    during that await must win over the stale transition computed from the old row."""
    async with sessionmaker_() as session:
        download = Download(torrent_hash=_HASH, status="downloading", tmdb_id=603)
        session.add(download)
        await session.commit()
        download_id = download.id

    class _ConcurrentChangeQbt(FakeQbittorrent):
        async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
            async with sessionmaker_() as session:
                row = await session.get(Download, download_id)
                assert row is not None
                row.status = "failed"
                await session.commit()
            return [
                DownloadStatus(
                    info_hash=_HASH,
                    name="Some.Movie",
                    raw_state="stoppedUP",
                    progress=1.0,
                    ratio=1.0,
                )
            ]

    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(_ConcurrentChangeQbt(), session)

    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
    assert row is not None
    assert row.status == "failed"
