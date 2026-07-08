"""queue_service — the auto-fail blocklist-and-research path beyond grace."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.domain.reconciler import StallDetection
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
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.services import queue_service
from plex_manager.services.queue_service import (
    FailedPendingAdoptionRefusedError,
    OperatorClaimActiveError,
    RemovalInProgressError,
    _is_operator_claimed,  # pyright: ignore[reportPrivateUsage]
    _mark_removal_in_flight,  # pyright: ignore[reportPrivateUsage]
    _OperatorFailFlags,  # pyright: ignore[reportPrivateUsage]
    _owns_operator_claim,  # pyright: ignore[reportPrivateUsage]
    _register_operator_claim,  # pyright: ignore[reportPrivateUsage]
    _release_operator_claim,  # pyright: ignore[reportPrivateUsage]
    _self_heal_stalled_download,  # pyright: ignore[reportPrivateUsage]
)
from tests.web.fakes import FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "f" * 40
_TITLE = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_INDEXER = "FakeIndexer"


@pytest.fixture(autouse=True)
def clear_operator_claims() -> Iterator[None]:
    """Isolate the module-level claim registry between tests: a claim (or a
    reconcile removal window) a failing test left registered must never leak into
    (and cascade through) later tests."""
    yield
    queue_service._operator_fail_claims.clear()  # pyright: ignore[reportPrivateUsage]
    queue_service._reconcile_removals_in_flight.clear()  # pyright: ignore[reportPrivateUsage]


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
            await queue_service.mark_failed(
                stale_session, FakeQbittorrent(), download_id=download_id, blocklist=True
            )

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


class _StatusAtRemoveQbt(FakeQbittorrent):
    """A :class:`FakeQbittorrent` that records the owning request's status at the
    exact moment ``remove`` runs -- so a test can prove the torrent removal
    (Phase B) happens BEFORE the request is re-armed to 'searching' (Phase C).
    Issue #68: only once the request is 'searching' can an auto-grab re-resolve to
    the same info_hash, so a removal that runs while the request is still
    'downloading' can never delete a fresh same-hash re-grab."""

    def __init__(self, sm: SessionMaker, request_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._request_id = request_id
        self.request_status_at_remove: list[str] = []

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            request = await session.get(MediaRequest, self._request_id)
            assert request is not None
            self.request_status_at_remove.append(request.status.value)
        await super().remove(info_hash, delete_files=delete_files)


async def test_reconcile_removes_the_torrent_before_rearming_the_request(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #68: the failed torrent must be removed BEFORE its request re-arms to
    'searching'. A re-grab can only re-acquire the same info_hash once the request
    is due again ('searching'); proving removal ran while the request was still
    'downloading' proves the stale removal cannot delete a fresh same-hash grab."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    qbt = _StatusAtRemoveQbt(sessionmaker_, request_id)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    # The removal fired while the request was STILL 'downloading' (pre-re-arm) --
    # the ordering guarantee. The re-arm to 'searching' only lands afterwards.
    assert qbt.request_status_at_remove == ["downloading"]
    assert qbt.removed == [(_HASH, True)]
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    assert request.status is RequestStatus.searching


class _RemoveFailsQbt(FakeQbittorrent):
    """A client whose ``remove`` always fails, exercising the best-effort contract:
    ``purge_service.remove_torrent`` logs (never raises), so the re-arm still runs."""

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise RuntimeError("qbt remove exploded")


async def test_reconcile_removal_failure_still_rearms_and_stays_visible(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #68 honesty contract: if the torrent removal FAILS, the row must land
    in a visible, retryable state -- not silently stuck. Removal is best-effort
    (logged, never raised), so a failure does not block the Phase C re-arm: the
    download is terminal Failed (visible) and its request is 'searching'
    (retryable)."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(_RemoveFailsQbt(statuses=[]), session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        failed = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert failed.status == "failed"  # visible terminal failure
    assert request is not None
    assert request.status is RequestStatus.searching  # re-armed despite the removal failure


async def test_reconcile_batch_fails_removes_and_rearms_multiple_rows(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #68: the batch path applies the same three-phase ordering to EVERY
    failed row -- each download goes terminal Failed, each request re-arms to
    'searching', and each torrent is removed."""
    hash_a, hash_b = "a" * 40, "b" * 40
    async with sessionmaker_() as session:
        owners: list[tuple[int, str, int]] = []  # (request_id, torrent_hash, tmdb_id)
        for tmdb_id, torrent_hash in ((701, hash_a), (702, hash_b)):
            request = MediaRequest(
                tmdb_id=tmdb_id,
                media_type=MediaType.movie,
                title=f"Movie {tmdb_id}",
                status=RequestStatus.downloading,
            )
            session.add(request)
            await session.flush()
            session.add(
                Download(
                    torrent_hash=torrent_hash,
                    status="downloading",
                    media_request_id=request.id,
                    tmdb_id=tmdb_id,
                    first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
                )
            )
            owners.append((request.id, torrent_hash, tmdb_id))
        await session.commit()

    qbt = FakeQbittorrent(statuses=[])  # both torrents gone beyond grace
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        for request_id, torrent_hash, _ in owners:
            request = await session.get(MediaRequest, request_id)
            download = (
                await session.execute(select(Download).where(Download.torrent_hash == torrent_hash))
            ).scalar_one()
            assert request is not None
            assert request.status is RequestStatus.searching
            assert download.status == "failed"
    assert set(qbt.removed) == {(hash_a, True), (hash_b, True)}


# ---------------------------------------------------------------------------
# Finding 1 (P2): the movie re-arm is a compare-and-swap, not an unconditional
# write. A ``cancel_request`` committed between Phase A (terminal flip) and Phase C
# (re-arm) must survive -- the cancelled request must NOT be dragged back to
# 'searching' and auto-grabbed again.
# ---------------------------------------------------------------------------


class _CancelDuringRemoveQbt(FakeQbittorrent):
    """A client whose ``remove`` (Phase B, AFTER Phase A commit and BEFORE the
    Phase C re-arm) commits a concurrent ``cancelled`` on the owning request -- the
    exact interleaving Finding 1 flags."""

    def __init__(self, sm: SessionMaker, request_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._request_id = request_id

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            request = await session.get(MediaRequest, self._request_id)
            assert request is not None
            request.status = RequestStatus.cancelled
            await session.commit()
        await super().remove(info_hash, delete_files=delete_files)


async def test_reconcile_cancel_committed_between_phase_a_and_c_stays_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1: a cancel committed between Phase A and Phase C wins. The movie
    re-arm CASes 'searching' only from a still-re-armable status, so the terminal
    'cancelled' is left intact -- the item is NOT re-queued/auto-grabbed. The
    download's OWN failure still records (Failed + blocklist) -- fully visible."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    qbt = _CancelDuringRemoveQbt(sessionmaker_, request_id)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.cancelled  # NOT dragged back to 'searching'
    assert download.status == "failed"  # the failure itself still recorded
    assert len(blocklist) == 1  # and the bad release still blocklisted


class _CancelSeasonDuringRemoveQbt(FakeQbittorrent):
    """The TV analogue of :class:`_CancelDuringRemoveQbt`: cancels the SEASON."""

    def __init__(self, sm: SessionMaker, season_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._season_id = season_id

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            season = await session.get(SeasonRequest, self._season_id)
            assert season is not None
            season.status = RequestStatus.cancelled
            await session.commit()
        await super().remove(info_hash, delete_files=delete_files)


async def test_reconcile_tv_cancel_between_phase_a_and_c_stays_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1 (consistency): the TV re-arm is ALSO a compare-and-swap, so a
    season cancelled between Phase A and Phase C is left settled -- never re-armed."""
    _, season_id = await _seed_tv_request_with_download(
        sessionmaker_, season=2, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    qbt = _CancelSeasonDuringRemoveQbt(sessionmaker_, season_id)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert season is not None
    assert season.status is RequestStatus.cancelled  # season stays cancelled
    assert download.status == "failed"


# ---------------------------------------------------------------------------
# Finding 2 (P2): a Phase-C commit failure must not strand the owner. The re-arm
# commit is retried; if it still fails, the download is left at the reconcilable
# ``failed_pending`` (NOT terminal Failed), so a later reconcile cycle heals it.
# ---------------------------------------------------------------------------


def _fail_commit_on(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, fail_calls: set[int]
) -> None:
    """Monkeypatch ``session.commit`` to raise a transient ``OperationalError`` on the
    Nth commit calls in ``fail_calls`` (1-indexed), delegating to the real commit
    otherwise. Commit #1 is Phase A; commit #2+ are Phase C attempts."""
    real_commit = session.commit
    counter = {"n": 0}

    async def _counting_commit() -> None:
        counter["n"] += 1
        if counter["n"] in fail_calls:
            raise OperationalError("simulated", {}, Exception("database is locked"))
        await real_commit()

    monkeypatch.setattr(session, "commit", _counting_commit)


async def test_reconcile_phase_c_commit_failure_recovers_via_retry(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: a TRANSIENT Phase-C commit failure is retried and recovers -- the
    concrete recovery path. The re-arm lands 'searching' after the retry; the request
    is never left 'downloading'."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    async with sessionmaker_() as session:
        # Commits: #1 Phase A, #2 the remove=done restamp (guarded, not under
        # test here), #3.. the Phase C attempts. Fail the FIRST Phase C commit
        # (#3); the retry (#4) succeeds.
        _fail_commit_on(session, monkeypatch, {3})
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
    assert request is not None
    assert request.status is RequestStatus.searching  # re-armed after the retry
    assert download.status == "failed"


async def test_reconcile_phase_c_exhaustion_leaves_reconcilable_state_next_cycle_heals(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: when EVERY Phase-C attempt fails, the failure is surfaced (raised)
    and the download is left at the NON-terminal ``failed_pending`` -- NOT stranded at
    terminal Failed with a 'downloading' request. A later reconcile cycle re-derives
    the ``failed_pending`` row and heals it (Failed + blocklist + 'searching')."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )

    # Commits: #1 Phase A, #2 the remove=done restamp, #3-#5 the three bounded
    # Phase C attempts -- fail exactly those three.
    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {3, 4, 5})
        with pytest.raises(OperationalError):
            await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    # Reconcilable residual: Phase A committed the failed_pending transition, but the
    # blocklist / terminal Failed / re-arm never landed.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.downloading  # not yet re-armed
    assert download.status == "failed_pending"  # reconcilable, NOT terminal Failed
    assert blocklist == []  # deferred to Phase C, never written on the strand

    # A later cycle heals it via the strand re-derivation (commit works now).
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching  # healed
    assert download.status == "failed"  # advanced to terminal
    assert len(blocklist) == 1  # blocklisted exactly once (no duplication)


async def test_mark_failed_phase_c_exhaustion_leaves_reconcilable_state(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2 (consistency): mark_failed applies the SAME guard. An exhausted
    Phase C surfaces the failure and leaves the download at ``failed_pending`` (not
    terminal Failed + 'downloading'), which the reconcile loop then heals."""
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
        # Commits: #1 Phase A, #2 the remove=done restamp, #3-#5 the Phase C
        # attempts -- fail exactly those three.
        _fail_commit_on(session, monkeypatch, {3, 4, 5})
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, FakeQbittorrent(statuses=[]), download_id=download_id, blocklist=False
            )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
    assert request is not None
    assert request.status is RequestStatus.downloading  # not stranded terminal
    assert download is not None
    assert download.status == "failed_pending"  # reconcilable

    # The reconcile loop heals the operator's stranded mark-failed.
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
    assert request is not None
    assert request.status is RequestStatus.searching  # healed
    assert download is not None
    assert download.status == "failed"


# ---------------------------------------------------------------------------
# Round 3, findings 1 + 2: the failed_pending residual carries OPERATOR
# PROVENANCE (a live in-process claim + a persisted failed_reason marker), so
# neither a concurrent reconcile tick nor the later heal can override the
# operator's explicit blocklist/remove_torrent choices.
# ---------------------------------------------------------------------------


async def _seed_movie_request_and_download(
    sm: SessionMaker,
    *,
    download_status: str = "downloading",
    failed_reason: str | None = None,
    added_at: datetime | None = None,
) -> tuple[int, int]:
    """Insert a movie request + one tracked download; return (request_id, download_id).

    ``added_at`` (issue #165's stall self-heal) is left to the column's
    ``server_default=func.now()`` when omitted -- only passed through when a
    test needs to backdate the grab (never explicitly ``None``, which would
    fight the NOT NULL column).
    """
    async with sm() as session:
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
            status=download_status,
            media_request_id=request.id,
            tmdb_id=603,
            failed_reason=failed_reason,
            **({"added_at": added_at} if added_at is not None else {}),
        )
        session.add(download)
        await session.commit()
        return request.id, download.id


async def test_mark_failed_clean_phase_c_honors_flags_and_replaces_marker(
    sessionmaker_: SessionMaker,
) -> None:
    """Walk (a): a clean operator mark_failed(blocklist=False, remove_torrent=False)
    completes with the operator's semantics -- no blocklist row, nothing removed --
    and the Phase-A provenance marker is replaced by the final human-readable
    reason (it never survives onto a terminal row)."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, None, download_id=download_id, blocklist=False, remove_torrent=False
        )
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"  # marker replaced

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert blocklist == []  # operator said no blocklist
    # Claim cleared on exit (registry internals -- the regression is a leak here).
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]


async def test_mark_failed_exhaustion_residual_heals_with_operator_flags(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk (b), finding 1: after Phase-C exhaustion of mark_failed(blocklist=False,
    remove_torrent=False), the residual carries the persisted marker, and the
    next-cycle reconcile heal runs with the OPERATOR's semantics -- no blocklist
    row, no torrent removal -- never the reconcile defaults."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {2, 3, 4})  # every Phase C attempt
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, None, download_id=download_id, blocklist=False, remove_torrent=False
            )
    # Live claim cleared even on the exhaustion exit (the finally guarantee).
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]

    # The residual is failed_pending and carries the exact documented marker.
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None
    assert download.status == "failed_pending"
    # The documented nonce-marker: flags + the owning call's claim token.
    assert download.failed_reason is not None
    assert re.fullmatch(
        r"operator mark-failed in progress \(blocklist=no, remove=no, nonce=\d+\)",
        download.failed_reason,
    )

    # Next-cycle heal: operator semantics survive the heal.
    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching  # still healed (re-armed)
    assert download is not None
    assert download.status == "failed"
    assert download.failed_reason == "marked failed by operator"  # marker replaced
    assert blocklist == []  # blocklist=False survived the heal
    assert qbt.removed == []  # remove_torrent=False survived the heal


async def test_mark_failed_exhaustion_heal_keeps_user_reported_blocklist_reason(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk (b) variant: blocklist=True, remove_torrent=False. The heal writes the
    blocklist row exactly once with the OPERATOR vocabulary (user_reported, not
    failed) and still skips the removal."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {2, 3, 4})
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, None, download_id=download_id, blocklist=True, remove_torrent=False
            )

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert len(blocklist) == 1
    assert blocklist[0].reason.value == "user_reported"  # operator vocabulary kept
    assert qbt.removed == []  # remove_torrent=False survived


class _ReconcileDuringRemoveQbt(FakeQbittorrent):
    """An operator-path client whose ``remove`` (Phase B) runs a FULL background
    reconcile cycle with its own session + client -- the exact mid-flight window of
    finding 2, where the loop could previously steal the generic failed_pending row
    and complete it with reconcile-default side effects."""

    def __init__(self, sm: SessionMaker) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self.inner = FakeQbittorrent(statuses=[])
        self.status_after_inner_reconcile: list[str] = []

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            queue = await queue_service.reconcile_and_list(self.inner, session)
            # The inner cycle SAW the row (it is non-terminal, so still listed) but
            # must have deferred: still failed_pending, not stolen to Failed.
            self.status_after_inner_reconcile += [
                item.status for item in queue if item.torrent_hash == info_hash
            ]
        await super().remove(info_hash, delete_files=delete_files)


async def test_reconcile_tick_mid_phase_b_defers_to_the_operator_claim(
    sessionmaker_: SessionMaker,
) -> None:
    """Walk (c), finding 2: a reconcile cycle landing during mark_failed's Phase-B
    await sees the failed_pending row but the live claim makes it defer -- it
    neither removes the torrent nor steals the failed_pending -> Failed CAS. The
    operator path then completes with ITS semantics (blocklist=False here)."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    qbt = _ReconcileDuringRemoveQbt(sessionmaker_)
    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, qbt, download_id=download_id, blocklist=False, remove_torrent=True
        )
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"  # operator completed it

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert blocklist == []  # the mid-flight reconcile did NOT blocklist (no steal)
    assert qbt.inner.removed == []  # ...and did NOT remove the claimed row's torrent
    assert qbt.removed == [(_HASH, True)]  # the operator's own Phase B did
    # The inner cycle genuinely saw the claimed row and left it failed_pending.
    assert qbt.status_after_inner_reconcile == ["failed_pending"]


async def test_reconcile_strand_without_marker_heals_with_default_semantics(
    sessionmaker_: SessionMaker,
) -> None:
    """A failed_pending residual whose failed_reason is NOT the operator marker
    (absent / free text / malformed) heals exactly as today: torrent removed,
    blocklisted once with the reconcile 'failed' reason, request re-armed."""
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="failed_pending",
        # Nonce-less marker: malformed under the round-7 format -> no provenance.
        failed_reason="operator mark-failed in progress (blocklist=no, remove=no)",
    )

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert download is not None
    assert download.status == "failed"
    assert len(blocklist) == 1  # reconcile-default semantics
    assert blocklist[0].reason.value == "failed"
    assert qbt.removed == [(_HASH, True)]


async def test_mark_failed_adopts_a_stranded_failed_pending_row(
    sessionmaker_: SessionMaker,
) -> None:
    """An operator mark_failed on a row ALREADY at failed_pending (a reconcile
    detection or stranded prior attempt) re-stamps it with THIS call's provenance
    and completes under THIS call's flags -- the most recent explicit instruction
    owns the residual."""
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="failed_pending",
        failed_reason="absent from client snapshot beyond missing grace",
    )

    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, None, download_id=download_id, blocklist=False, remove_torrent=False
        )
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert blocklist == []  # the operator's no-blocklist choice governed


# ---------------------------------------------------------------------------
# Round 4: single-owner claim TOKEN protocol. Reconcile treats claimed ids as
# invisible at EVERY phase boundary (A-transition building, per-hash before each
# B-removal await, per-completion inside C), and mark_failed's completion +
# cleanup are token-gated so the NEWEST operator command always wins.
# ---------------------------------------------------------------------------


def test_claim_token_registry_single_owner_semantics() -> None:
    """Protocol unit test: registration replaces (later command owns), ownership is
    token-exact, and release is token-gated — a stale finisher's release can never
    clear the newer command's live claim."""
    download_id = 987_654
    flags_a = _OperatorFailFlags(blocklist=True, remove_torrent=True)
    flags_b = _OperatorFailFlags(blocklist=False, remove_torrent=False)

    token_a = _register_operator_claim(download_id, flags_a)
    assert _is_operator_claimed(download_id)
    assert _owns_operator_claim(download_id, token_a)

    token_b = _register_operator_claim(download_id, flags_b)  # later command wins
    assert token_b > token_a  # monotonic
    assert not _owns_operator_claim(download_id, token_a)  # superseded
    assert _owns_operator_claim(download_id, token_b)

    _release_operator_claim(download_id, token_a)  # stale finisher: MUST no-op
    assert _is_operator_claimed(download_id)
    assert _owns_operator_claim(download_id, token_b)  # newer claim survives

    _release_operator_claim(download_id, token_b)  # current owner: pops
    assert not _is_operator_claimed(download_id)


class _ClaimDuringSnapshotQbt(FakeQbittorrent):
    """A client whose ``get_all_statuses`` registers an operator claim on the
    seeded download — modelling a mark_failed that has REGISTERED but not yet
    stamped its marker while a reconcile cycle is mid-Phase-A (finding 1)."""

    def __init__(self, download_id: int) -> None:
        super().__init__(statuses=[])
        self._download_id = download_id
        self.token: int | None = None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        self.token = _register_operator_claim(
            self._download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
        )
        return []


async def test_reconcile_phase_a_skips_a_claimed_row_entirely(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1: a claim registered BEFORE the operator stamps its marker makes the
    row invisible to reconcile's Phase-A transition building — reconcile must NOT
    move it to an unmarked failed_pending (which would make the operator's own
    Phase-A CAS lose and the residual heal as reconcile-owned). Once the claim is
    released (the operator never completed), the next cycle proceeds normally."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)
    # Beyond missing grace: without the claim this cycle WOULD fail the row.
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.first_seen_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()

    qbt = _ClaimDuringSnapshotQbt(download_id)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    # The claimed row was untouched: still downloading, nothing removed, nothing
    # blocklisted, request untouched — fully invisible to the cycle.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None
    assert download.status == "downloading"  # no unmarked failed_pending
    assert download.failed_reason is None
    assert request is not None
    assert request.status is RequestStatus.downloading
    assert blocklist == []
    assert qbt.removed == []

    # The operator abandons (claim released, nothing stamped): the row is still
    # active, so the next unclaimed cycle resumes the normal failure flow.
    assert qbt.token is not None
    _release_operator_claim(download_id, qbt.token)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None
    assert download.status == "failed"
    assert request is not None
    assert request.status is RequestStatus.searching
    assert len(blocklist) == 1  # reconcile-default semantics, exactly once


class _ClaimSecondHashQbt(FakeQbittorrent):
    """A client whose FIRST removal registers an operator claim on the SECOND
    failed download — a mark_failed starting while reconcile awaits an earlier
    hash's removal (finding 2). The per-hash re-check must protect the later one."""

    def __init__(self, claim_download_id: int) -> None:
        super().__init__(statuses=[])
        self._claim_download_id = claim_download_id
        self.token: int | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        if self.token is None:
            self.token = _register_operator_claim(
                self._claim_download_id,
                _OperatorFailFlags(blocklist=False, remove_torrent=False),
            )
        await super().remove(info_hash, delete_files=delete_files)


async def test_claim_registered_mid_phase_b_protects_the_later_hash(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 2: the pre-loop claim filter is a stale snapshot by the second
    iteration — the registry must be re-checked immediately before EACH removal
    await. A claim registered during hash A's removal keeps hash B's torrent from
    being removed and defers B's completion; releasing the claim lets the next
    cycle heal B normally."""
    hash_a, hash_b = "a" * 40, "b" * 40
    async with sessionmaker_() as session:
        ids: dict[str, tuple[int, int]] = {}  # hash -> (request_id, download_id)
        for tmdb_id, torrent_hash in ((701, hash_a), (702, hash_b)):
            request = MediaRequest(
                tmdb_id=tmdb_id,
                media_type=MediaType.movie,
                title=f"Movie {tmdb_id}",
                status=RequestStatus.downloading,
            )
            session.add(request)
            await session.flush()
            download = Download(
                torrent_hash=torrent_hash,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=tmdb_id,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
            session.add(download)
            await session.flush()
            ids[torrent_hash] = (request.id, download.id)
        await session.commit()

    # list_active orders by id, so hash A (seeded first) is removed first; its
    # removal registers the claim on hash B's download.
    qbt = _ClaimSecondHashQbt(ids[hash_b][1])
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request_b = await session.get(MediaRequest, ids[hash_b][0])
        download_b = await session.get(Download, ids[hash_b][1])
        download_a = await session.get(Download, ids[hash_a][1])
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert qbt.removed == [(hash_a, True)]  # B's torrent NOT removed
    assert download_a is not None and download_a.status == "failed"  # A completed
    assert download_b is not None
    assert download_b.status == "failed_pending"  # B deferred, not stolen
    assert request_b is not None
    assert request_b.status is RequestStatus.downloading  # B not re-armed
    assert [row.torrent_hash for row in blocklist] == [hash_a]  # only A blocklisted

    # The claiming operator abandons: release, and the next cycle heals B with the
    # default semantics (its residual carries no marker).
    assert qbt.token is not None
    _release_operator_claim(ids[hash_b][1], qbt.token)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        request_b = await session.get(MediaRequest, ids[hash_b][0])
        download_b = await session.get(Download, ids[hash_b][1])
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download_b is not None and download_b.status == "failed"
    assert request_b is not None and request_b.status is RequestStatus.searching
    assert set(qbt.removed) == {(hash_a, True), (hash_b, True)}
    assert sorted(row.torrent_hash or "" for row in blocklist) == [hash_a, hash_b]


async def test_overlapping_mark_faileds_the_later_call_wins(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3 (round 4, reworked for round 5): overlapping mark_faileds with
    conflicting flags — a call superseding the outer one BEFORE its removal I/O
    starts (right after the outer's Phase-A commit, the last legal supersession
    window) re-stamps the claim and completes with ITS flags; the outer call
    yields everywhere token-gated: it skips its own removal, its Phase C declines
    to apply its stale blocklist=True, and its finally clears nothing the newer
    call owned."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    outer_qbt = FakeQbittorrent(statuses=[])
    nested_status: list[str] = []
    async with sessionmaker_() as session:
        real_commit = session.commit
        fired = {"done": False}

        async def _commit_then_supersede() -> None:
            # Fire the nested (superseding) call right AFTER the outer's Phase-A
            # commit -- inside the pre-removal window where supersession is legal.
            await real_commit()
            if not fired["done"]:
                fired["done"] = True
                async with sessionmaker_() as nested_session:
                    nested = await queue_service.mark_failed(
                        nested_session,
                        None,
                        download_id=download_id,
                        blocklist=False,  # conflicts with the outer's blocklist=True
                        remove_torrent=False,
                    )
                    nested_status.append(nested.status)

        monkeypatch.setattr(session, "commit", _commit_then_supersede)
        record = await queue_service.mark_failed(
            session, outer_qbt, download_id=download_id, blocklist=True, remove_torrent=True
        )

    # The nested (later) call completed the row; the outer call yielded and returns
    # the completed state honestly.
    assert nested_status == ["failed"]
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"
    # The outer call's removal never ran: it was superseded before its Phase B, and
    # the newer command said remove_torrent=False.
    assert outer_qbt.removed == []

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert blocklist == []  # the LATER call's blocklist=False won; no stale row
    # Neither finisher left a live claim behind (token-gated release both sides).
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Round 5: the closing set. Adopt-on-CAS-loss, whole-cycle claim drops, the
# removal-in-flight supersession refusal, and the qbt-unconfigured DB-only heal.
# ---------------------------------------------------------------------------


async def test_mark_failed_adopts_when_reconcile_moved_the_row_mid_snapshot(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 1: the operator's Phase-A CAS losing to a STALE snapshot must not
    409 when the row's CURRENT state is the adoptable, uncompleted failed_pending
    (an in-flight reconcile detected the same failure first). mark_failed re-reads
    once, adopts (re-stamps the marker with ITS flags), and completes normally."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    async with sessionmaker_() as stale_session:
        # Load the row into THIS session's identity map while it is 'downloading'.
        stale = await stale_session.get(Download, download_id)
        assert stale is not None and stale.status == "downloading"

        # A reconcile-like writer moves the row to failed_pending (detected, NOT
        # completed, no marker) in a different session.
        async with sessionmaker_() as other:
            moved = await SqlDownloadRepository(other).update_status_if_in(
                download_id, "failed_pending", frozenset({"downloading"})
            )
            assert moved is True
            await other.commit()

        # The stale-snapshot mark_failed now adopts instead of raising.
        record = await queue_service.mark_failed(
            stale_session, None, download_id=download_id, blocklist=False, remove_torrent=False
        )
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching  # the adopt completed fully
    assert blocklist == []  # with the OPERATOR's flags (blocklist=False)


class _ClaimSecondThenReleaseLaterQbt(FakeQbittorrent):
    """Finding 2's same-cycle release race: the FIRST removal registers a claim on
    the second failed download (dropping it from the cycle), and the claim is then
    RELEASED before Phase C runs (via the monkeypatched source_title_for below) --
    without the whole-cycle drop, Phase C's re-check would see the row unclaimed
    and complete it with the stale pre-marker completion."""

    def __init__(self, claim_download_id: int) -> None:
        super().__init__(statuses=[])
        self.claim_download_id = claim_download_id
        self.token: int | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        if self.token is None:
            self.token = _register_operator_claim(
                self.claim_download_id,
                _OperatorFailFlags(blocklist=False, remove_torrent=False),
            )
        await super().remove(info_hash, delete_files=delete_files)


async def test_phase_b_claim_drop_survives_a_same_cycle_release(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: a completion skipped for a claim is dropped from the WHOLE cycle.
    Even when the claim is released again before this cycle's Phase C (the operator
    call failing fast), the pre-marker completion must not be applied; the row
    heals NEXT cycle instead."""
    hash_a, hash_b = "a" * 40, "b" * 40
    async with sessionmaker_() as session:
        ids: dict[str, tuple[int, int]] = {}
        for tmdb_id, torrent_hash in ((701, hash_a), (702, hash_b)):
            request = MediaRequest(
                tmdb_id=tmdb_id,
                media_type=MediaType.movie,
                title=f"Movie {tmdb_id}",
                status=RequestStatus.downloading,
            )
            session.add(request)
            await session.flush()
            download = Download(
                torrent_hash=torrent_hash,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=tmdb_id,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
            session.add(download)
            await session.flush()
            ids[torrent_hash] = (request.id, download.id)
        await session.commit()

    qbt = _ClaimSecondThenReleaseLaterQbt(ids[hash_b][1])

    # Release B's claim during Phase C's FIRST await (processing hash A), i.e.
    # after the Phase-B drop but before Phase C would have reached B.
    real_source_title_for = queue_service.blocklist_service.source_title_for
    released = {"done": False}

    async def _release_then_delegate(session: AsyncSession, torrent_hash: str) -> str | None:
        if not released["done"] and qbt.token is not None:
            released["done"] = True
            _release_operator_claim(qbt.claim_download_id, qbt.token)
        return await real_source_title_for(session, torrent_hash)

    monkeypatch.setattr(queue_service.blocklist_service, "source_title_for", _release_then_delegate)

    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    assert released["done"] is True  # the release really happened mid-cycle
    async with sessionmaker_() as session:
        download_b = await session.get(Download, ids[hash_b][1])
        request_b = await session.get(MediaRequest, ids[hash_b][0])
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert qbt.removed == [(hash_a, True)]  # B's removal never ran
    assert download_b is not None
    assert download_b.status == "failed_pending"  # B NOT completed this cycle
    assert request_b is not None
    assert request_b.status is RequestStatus.downloading
    assert [row.torrent_hash for row in blocklist] == [hash_a]

    # Next cycle (claim long gone) heals B along the designed path.
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)
    async with sessionmaker_() as session:
        download_b = await session.get(Download, ids[hash_b][1])
        request_b = await session.get(MediaRequest, ids[hash_b][0])
    assert download_b is not None and download_b.status == "failed"
    assert request_b is not None and request_b.status is RequestStatus.searching


def test_register_refuses_supersession_while_removal_in_flight() -> None:
    """Finding 3 (unit): once the owner's claim is flagged removal-in-flight, a
    replacement registration raises RemovalInProgressError; after the owner
    releases, registration works again."""
    download_id = 987_655
    flags = _OperatorFailFlags(blocklist=True, remove_torrent=True)
    token = _register_operator_claim(download_id, flags)
    _mark_removal_in_flight(download_id, token)

    with pytest.raises(RemovalInProgressError):
        _register_operator_claim(
            download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
        )
    # The refusal changed nothing: the original owner still holds the claim.
    assert _owns_operator_claim(download_id, token)

    _release_operator_claim(download_id, token)
    token2 = _register_operator_claim(
        download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
    )
    assert _owns_operator_claim(download_id, token2)
    _release_operator_claim(download_id, token2)


def test_register_non_superseding_backs_off_when_a_claim_already_exists() -> None:
    """Self-heal-vs-operator race hardening: a NON-superseding registration
    (``allow_supersede=False``, the self-heal path) must back off with
    :class:`OperatorClaimActiveError` -- never replace -- whenever ANY claim
    already exists, even one that is not (yet) removal-in-flight. The existing
    owner's claim is left completely untouched. When no claim exists at all, the
    same non-superseding call registers normally (self-heal's ordinary path)."""
    download_id = 987_656
    operator_flags = _OperatorFailFlags(blocklist=False, remove_torrent=False)
    self_heal_flags = _OperatorFailFlags(blocklist=True, remove_torrent=True)

    operator_token = _register_operator_claim(download_id, operator_flags)

    with pytest.raises(OperatorClaimActiveError):
        _register_operator_claim(download_id, self_heal_flags, allow_supersede=False)

    # The refusal changed nothing: the operator's own claim (and its explicit
    # blocklist=False/remove_torrent=False choice) still owns the row.
    assert _owns_operator_claim(download_id, operator_token)
    _release_operator_claim(download_id, operator_token)

    # With no existing claim, the same non-superseding call succeeds normally.
    self_heal_token = _register_operator_claim(download_id, self_heal_flags, allow_supersede=False)
    assert _owns_operator_claim(download_id, self_heal_token)
    _release_operator_claim(download_id, self_heal_token)


class _NestedMarkFailedDuringRemovalQbt(FakeQbittorrent):
    """A client whose ``remove`` (the outer call's Phase B, removal-in-flight
    already flagged) attempts a SECOND mark_failed on the same download -- the
    supersession that must now be REFUSED (finding 3): the delete I/O has started,
    so a later remove_torrent=False command would promise a file this very await
    is destroying."""

    def __init__(self, sm: SessionMaker, download_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._download_id = download_id
        self.nested_error: Exception | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            try:
                await queue_service.mark_failed(
                    session,
                    None,
                    download_id=self._download_id,
                    blocklist=False,
                    remove_torrent=False,
                )
            except RemovalInProgressError as exc:
                self.nested_error = exc
        await super().remove(info_hash, delete_files=delete_files)


async def test_supersession_during_removal_io_is_refused(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 3 (integration): a mark_failed arriving while the owner's removal
    I/O is in flight is refused with RemovalInProgressError (HTTP 409), and the
    owning call completes with ITS flags -- later-wins yields to physics here."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    qbt = _NestedMarkFailedDuringRemovalQbt(sessionmaker_, download_id)
    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, qbt, download_id=download_id, blocklist=True, remove_torrent=True
        )

    assert isinstance(qbt.nested_error, RemovalInProgressError)  # refused, honestly
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"
    assert qbt.removed == [(_HASH, True)]  # the owner's removal ran

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    # The OWNER's blocklist=True governed (the refused call changed nothing).
    assert len(blocklist) == 1
    assert blocklist[0].reason.value == "user_reported"
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]


async def test_heal_without_client_completes_only_remove_no_residuals(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4: with qBittorrent unconfigured, the DB-only heal completes exactly
    the remove=no marker residuals (no client I/O needed, by the operator's own
    choice) -- a remove=yes marker residual and a plain reconcile strand both need
    a removal first, so they wait for the client."""
    # Residual 1: operator mark_failed(blocklist=False, remove_torrent=False),
    # Phase C exhausted -> failed_pending with a remove=no marker.
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)
    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {2, 3, 4})
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, None, download_id=download_id, blocklist=False, remove_torrent=False
            )
    monkeypatch.undo()

    # Residual 2: a remove=YES marker (operator wanted the torrent gone).
    hash_c = "c" * 40
    async with sessionmaker_() as session:
        request_c = MediaRequest(
            tmdb_id=703,
            media_type=MediaType.movie,
            title="Movie 703",
            status=RequestStatus.downloading,
        )
        session.add(request_c)
        await session.flush()
        download_c = Download(
            torrent_hash=hash_c,
            status="failed_pending",
            media_request_id=request_c.id,
            tmdb_id=703,
            failed_reason="operator mark-failed in progress (blocklist=no, remove=yes, nonce=901)",
        )
        session.add(download_c)
        # Residual 3: a plain reconcile strand (no marker).
        request_d = MediaRequest(
            tmdb_id=704,
            media_type=MediaType.movie,
            title="Movie 704",
            status=RequestStatus.downloading,
        )
        session.add(request_d)
        await session.flush()
        download_d = Download(
            torrent_hash="d" * 40,
            status="failed_pending",
            media_request_id=request_d.id,
            tmdb_id=704,
        )
        session.add(download_d)
        await session.commit()
        request_c_id, download_c_id = request_c.id, download_c.id
        request_d_id, download_d_id = request_d.id, download_d.id

    async with sessionmaker_() as session:
        await queue_service.heal_failed_pending_without_client(session)

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        request_c_row = await session.get(MediaRequest, request_c_id)
        download_c_row = await session.get(Download, download_c_id)
        request_d_row = await session.get(MediaRequest, request_d_id)
        download_d_row = await session.get(Download, download_d_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    # The remove=no residual healed, DB-only, with the operator's flags.
    assert download is not None and download.status == "failed"
    assert download.failed_reason == "marked failed by operator"
    assert request is not None and request.status is RequestStatus.searching
    assert blocklist == []  # blocklist=False honored; nothing else blocklisted
    # The remove=yes marker residual and the plain strand both WAIT for the client.
    assert download_c_row is not None and download_c_row.status == "failed_pending"
    assert request_c_row is not None and request_c_row.status is RequestStatus.downloading
    assert download_d_row is not None and download_d_row.status == "failed_pending"
    assert request_d_row is not None and request_d_row.status is RequestStatus.downloading


# ---------------------------------------------------------------------------
# Round 6: protocol step 5 covers BOTH removal actors, and the DB-only heal
# runs on outage ticks too (web wiring tested in test_reconcile_loop.py).
# ---------------------------------------------------------------------------


class _MarkFailedDuringReconcileRemovalQbt(FakeQbittorrent):
    """A client whose ``remove`` (reconcile's AUTOMATIC Phase-B delete) attempts an
    operator mark_failed(remove_torrent=False) on the same download mid-await --
    the reconcile-side supersession that must now be refused (round 6, finding 2):
    the delete I/O has started, so completing with remove=no semantics would
    promise data this very await is destroying."""

    def __init__(self, sm: SessionMaker, download_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._download_id = download_id
        self.nested_error: Exception | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            try:
                await queue_service.mark_failed(
                    session,
                    None,
                    download_id=self._download_id,
                    blocklist=False,
                    remove_torrent=False,
                )
            except RemovalInProgressError as exc:
                self.nested_error = exc
        await super().remove(info_hash, delete_files=delete_files)


async def test_operator_claim_during_reconcile_delete_is_refused(
    sessionmaker_: SessionMaker,
) -> None:
    """Round 6, finding 2: an operator mark_failed arriving while reconcile's
    automatic Phase-B delete is mid-await gets the same physics refusal (409
    removal_in_progress) as during an operator removal; reconcile's own completion
    proceeds exactly as today; and the refusal window closes with the await --
    registration succeeds again afterwards."""
    request_id = await _seed_request_with_download(
        sessionmaker_, first_seen_at=datetime.now(UTC) - timedelta(minutes=11)
    )
    async with sessionmaker_() as session:
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _HASH))
        ).scalar_one()
        download_id = download.id

    qbt = _MarkFailedDuringReconcileRemovalQbt(sessionmaker_, download_id)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    # The mid-delete operator command was refused with the physics error...
    assert isinstance(qbt.nested_error, RemovalInProgressError)
    # ...and reconcile's completion proceeded exactly as today.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None and download.status == "failed"
    assert request is not None and request.status is RequestStatus.searching
    assert len(blocklist) == 1
    assert qbt.removed == [(_HASH, True)]

    # The refusal window closed with the delete await: a fresh registration for
    # this download is accepted again (released immediately -- registry hygiene).
    token = _register_operator_claim(
        download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
    )
    _release_operator_claim(download_id, token)
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Round 7: durable, predicate-atomic ownership. Every side-effect CAS re-proves
# ownership (status AND the exact observed/owned failed_reason) in its own
# WHERE, so no check-then-act window survives an await.
# ---------------------------------------------------------------------------


async def _seed_two_failed_movies(
    sm: SessionMaker,
) -> dict[str, tuple[int, int]]:
    """Two movie requests, each with a beyond-grace downloading torrent.

    Returns hash -> (request_id, download_id); the FIRST seeded row has the lower
    id and is processed first by the cycle's Phase B/C loops."""
    hash_x, hash_y = "a" * 40, "b" * 40
    async with sm() as session:
        ids: dict[str, tuple[int, int]] = {}
        for tmdb_id, torrent_hash in ((701, hash_x), (702, hash_y)):
            request = MediaRequest(
                tmdb_id=tmdb_id,
                media_type=MediaType.movie,
                title=f"Movie {tmdb_id}",
                status=RequestStatus.downloading,
            )
            session.add(request)
            await session.flush()
            download = Download(
                torrent_hash=torrent_hash,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=tmdb_id,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
            session.add(download)
            await session.flush()
            ids[torrent_hash] = (request.id, download.id)
        await session.commit()
    return ids


async def test_operator_claim_mid_phase_c_defers_the_later_completion(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 finding 1: a claim registered while reconcile is INSIDE Phase C
    (mid ``_handle_failed`` await for an earlier row) still defers the later
    row's completion -- the per-completion registry check runs immediately before
    each completion, and the claimed row is left for the operator. The deferred
    row here is an UNREMOVED (remove=no marker) completion: a REMOVED row's
    consequence is unsettled through Phase C, so the round-8 removal guard would
    refuse the claim outright instead (see
    test_operator_mark_failed_refused_until_removal_consequence_settles)."""
    hash_a, hash_b = "a" * 40, "b" * 40
    # A: fresh beyond-grace failure (Phase C processes it first -- the hook point).
    async with sessionmaker_() as session:
        request_a = MediaRequest(
            tmdb_id=701,
            media_type=MediaType.movie,
            title="Movie 701",
            status=RequestStatus.downloading,
        )
        session.add(request_a)
        await session.flush()
        session.add(
            Download(
                torrent_hash=hash_a,
                status="downloading",
                media_request_id=request_a.id,
                tmdb_id=701,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
        )
        await session.commit()
    # B: an unremoved remove=no marker strand (no Phase-B delete, no guard).
    async with sessionmaker_() as session:
        request_b = MediaRequest(
            tmdb_id=702,
            media_type=MediaType.movie,
            title="Movie 702",
            status=RequestStatus.downloading,
        )
        session.add(request_b)
        await session.flush()
        download_b_row = Download(
            torrent_hash=hash_b,
            status="failed_pending",
            media_request_id=request_b.id,
            tmdb_id=702,
            failed_reason="operator mark-failed in progress (blocklist=no, remove=no, nonce=903)",
        )
        session.add(download_b_row)
        await session.commit()
        b_request_id, b_download_id = request_b.id, download_b_row.id

    claimed_token: list[int] = []
    real_source_title_for = queue_service.blocklist_service.source_title_for

    async def _claim_b_then_delegate(session: AsyncSession, torrent_hash: str) -> str | None:
        # Fires during A's _handle_failed (mid Phase C). Registering B's claim is
        # a synchronous registry op -- no DB write inside reconcile's open
        # Phase-C transaction.
        if not claimed_token:
            claimed_token.append(
                _register_operator_claim(
                    b_download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
                )
            )
        return await real_source_title_for(session, torrent_hash)

    monkeypatch.setattr(queue_service.blocklist_service, "source_title_for", _claim_b_then_delegate)

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download_b = await session.get(Download, b_download_id)
        request_b_row = await session.get(MediaRequest, b_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    # A completed; B was deferred to the claim holder mid-Phase-C.
    assert download_b is not None and download_b.status == "failed_pending"
    assert request_b_row is not None and request_b_row.status is RequestStatus.downloading
    assert [row.torrent_hash for row in blocklist] == [hash_a]

    # The claiming operator abandons: release; the next cycle heals B from its
    # UNCHANGED marker (blocklist=no honored -- no B blocklist row).
    _release_operator_claim(b_download_id, claimed_token[0])
    monkeypatch.setattr(queue_service.blocklist_service, "source_title_for", real_source_title_for)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)
    async with sessionmaker_() as session:
        download_b = await session.get(Download, b_download_id)
        request_b_row = await session.get(MediaRequest, b_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download_b is not None and download_b.status == "failed"
    assert request_b_row is not None and request_b_row.status is RequestStatus.searching
    assert [row.torrent_hash for row in blocklist] == [hash_a]  # marker honored


async def test_newer_restamp_defeats_the_older_terminal_cas(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 finding 2: mark_failed's terminal CAS carries ITS nonce-marker in
    the WHERE. A newer command that restamps (but does not finish -- its Phase C
    exhausts) atomically defeats the older call's completion at the database: the
    older call's blocklist=True must never land after the newer blocklist=False
    restamp, with no post-CAS token re-check involved."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    async with sessionmaker_() as session:
        real_commit = session.commit
        fired = {"done": False}

        async def _commit_then_restamp() -> None:
            # After the OLDER call's Phase-A commit (no open transaction), run a
            # NEWER mark_failed whose Phase C exhausts: it restamps the marker
            # (nonce2, blocklist=no) and leaves the row failed_pending.
            await real_commit()
            if not fired["done"]:
                fired["done"] = True
                async with sessionmaker_() as newer_session:
                    _fail_commit_on(newer_session, monkeypatch, {2, 3, 4})
                    with pytest.raises(OperationalError):
                        await queue_service.mark_failed(
                            newer_session,
                            None,
                            download_id=download_id,
                            blocklist=False,  # conflicts with the older blocklist=True
                            remove_torrent=False,
                        )

        monkeypatch.setattr(session, "commit", _commit_then_restamp)
        record = await queue_service.mark_failed(
            session, None, download_id=download_id, blocklist=True, remove_torrent=False
        )

    # The older call's terminal CAS missed (marker is now the newer call's): it
    # yielded -- the row is STILL failed_pending under the newer marker, and the
    # older call's blocklist=True never landed.
    assert record.status == "failed_pending"
    assert record.failed_reason is not None and "blocklist=no" in record.failed_reason
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert blocklist == []

    # The heal completes the residual with the NEWER call's flags.
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None and download.status == "failed"
    assert download.failed_reason == "marked failed by operator"
    assert request is not None and request.status is RequestStatus.searching
    assert blocklist == []  # the newer blocklist=False governed to the end


async def test_older_restamp_predicate_misses_a_newer_marker(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-7 finding 3: a marker restamp is a CAS on the exact reason value the
    caller observed. An older call that observed the PRE-marker reason can no
    longer clobber a newer call's nonce-marker -- its WHERE misses at the DB and
    the newer marker survives byte-identical."""
    _, download_id = await _seed_movie_request_and_download(
        sessionmaker_, download_status="failed_pending", failed_reason=None
    )
    newer_marker = "operator mark-failed in progress (blocklist=no, remove=no, nonce=777)"
    older_marker = "operator mark-failed in progress (blocklist=yes, remove=yes, nonce=3)"

    async with sessionmaker_() as session:
        repo = SqlDownloadRepository(session)
        # The newer call stamps over the observed None -- matches, lands.
        assert await repo.update_status_if_in(
            download_id,
            "failed_pending",
            frozenset({"failed_pending"}),
            failed_reason=newer_marker,
            require_failed_reason=None,
        )
        # The older call still believes the reason is None (its stale
        # observation): the predicate misses; nothing is written.
        assert not await repo.update_status_if_in(
            download_id,
            "failed_pending",
            frozenset({"failed_pending"}),
            failed_reason=older_marker,
            require_failed_reason=None,
        )
        await session.commit()

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None
    assert download.failed_reason == newer_marker  # survived byte-identical
    assert download.status == "failed_pending"


class _RestampUnremovedDuringRemovalQbt(FakeQbittorrent):
    """Round-7 finding 4's interleave, round-8 form: the marker-restamp target is
    an UNREMOVED completion (a ``remove=no`` marker strand -- no Phase-B delete,
    so no removal-physics guard bars the nested command). During the OTHER row's
    removal a nested (exhausting) mark_failed restamps the strand's marker
    mid-cycle; the strand's already-built completion (derived from the OLD
    marker) must drop at its predicate-atomic terminal CAS. (The previous form of
    this test restamped an already-REMOVED row -- that interleave is now refused
    outright by the extended removal-physics guard; see
    ``test_operator_mark_failed_refused_until_removal_consequence_settles``.)"""

    def __init__(
        self,
        sm: SessionMaker,
        target_download_id: int,
        trigger_hash: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._target_download_id = target_download_id
        self._trigger_hash = trigger_hash
        self._monkeypatch = monkeypatch
        self.restamped = False

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        await super().remove(info_hash, delete_files=delete_files)
        if info_hash == self._trigger_hash and not self.restamped:
            self.restamped = True
            async with self._sm() as session:
                _fail_commit_on(session, self._monkeypatch, {2, 3, 4})
                with pytest.raises(OperationalError):
                    await queue_service.mark_failed(
                        session,
                        None,
                        download_id=self._target_download_id,
                        blocklist=False,
                        remove_torrent=False,
                    )


async def test_marker_restamped_mid_cycle_drops_the_stale_unremoved_completion(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 finding 4 (round-8 form): a FRESHER operator nonce-marker landing
    mid-cycle on an UNREMOVED completion changes ``failed_reason``, so the cycle's
    stale completion -- built from the OLD marker (blocklist=yes) -- matches 0
    rows at its terminal CAS and drops. The residual heals next cycle with the
    NEW marker's flags (no blocklist)."""
    hash_y = "b" * 40
    # X: a remove=no marker strand (blocklist=YES -- the discriminator: the old
    # marker's completion would write a blocklist row if it ever won).
    x_request_id, x_download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="failed_pending",
        failed_reason="operator mark-failed in progress (blocklist=yes, remove=no, nonce=901)",
    )
    # Y: a fresh beyond-grace failure whose removal is the mid-cycle hook point.
    async with sessionmaker_() as session:
        request_y = MediaRequest(
            tmdb_id=702,
            media_type=MediaType.movie,
            title="Movie 702",
            status=RequestStatus.downloading,
        )
        session.add(request_y)
        await session.flush()
        session.add(
            Download(
                torrent_hash=hash_y,
                status="downloading",
                media_request_id=request_y.id,
                tmdb_id=702,
                first_seen_at=datetime.now(UTC) - timedelta(minutes=11),
            )
        )
        await session.commit()

    qbt = _RestampUnremovedDuringRemovalQbt(sessionmaker_, x_download_id, hash_y, monkeypatch)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download_x = await session.get(Download, x_download_id)
        request_x = await session.get(MediaRequest, x_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert qbt.restamped is True
    assert qbt.removed == [(hash_y, True)]  # X (remove=no) was never removed
    # X's stale completion dropped at the terminal CAS: still failed_pending under
    # the FRESHER marker; only Y was completed/blocklisted this cycle.
    assert download_x is not None and download_x.status == "failed_pending"
    assert download_x.failed_reason is not None
    assert "blocklist=no" in download_x.failed_reason  # the new marker's flags
    assert request_x is not None and request_x.status is RequestStatus.downloading
    assert [row.torrent_hash for row in blocklist] == [hash_y]

    # Next cycle: the residual heals with the FRESHER marker's flags -- despite the
    # old marker having said blocklist=yes, no X blocklist row is ever written.
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(FakeQbittorrent(statuses=[]), session)
    async with sessionmaker_() as session:
        download_x = await session.get(Download, x_download_id)
        request_x = await session.get(MediaRequest, x_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download_x is not None and download_x.status == "failed"
    assert download_x.failed_reason == "marked failed by operator"
    assert request_x is not None and request_x.status is RequestStatus.searching
    assert [row.torrent_hash for row in blocklist] == [hash_y]  # no X blocklist


# ---------------------------------------------------------------------------
# Round 8, finding 3: the reconcile-removal guard is held past the delete's
# return, until the row's removal CONSEQUENCE settles in Phase C.
# ---------------------------------------------------------------------------


class _MarkFailedAfterEarlierRemovalQbt(FakeQbittorrent):
    """When the SECOND removal runs, the FIRST row's delete has RETURNED but its
    completion has not yet settled (Phase C hasn't run). A nested
    mark_failed(remove_torrent=False) on the first row in that gap must be
    refused: its data is already destroyed, so remove=no semantics would lie."""

    def __init__(self, sm: SessionMaker, target_download_id: int, trigger_hash: str) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._target_download_id = target_download_id
        self._trigger_hash = trigger_hash
        self.nested_error: Exception | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        await super().remove(info_hash, delete_files=delete_files)
        if info_hash == self._trigger_hash and self.nested_error is None:
            async with self._sm() as session:
                try:
                    await queue_service.mark_failed(
                        session,
                        None,
                        download_id=self._target_download_id,
                        blocklist=False,
                        remove_torrent=False,
                    )
                except RemovalInProgressError as exc:
                    self.nested_error = exc


async def test_operator_mark_failed_refused_until_removal_consequence_settles(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-8 finding 3: the removal guard is held from just before the delete
    await until the row's Phase-C settlement, so a mark_failed(remove_torrent=
    False) fired BETWEEN the delete's return and Phase C is refused (409
    removal_in_progress) -- not allowed to stamp remove=no semantics over data
    this cycle already destroyed. Once the cycle settles, the row is terminal and
    a fresh mark_failed gets the honest already-terminal 409."""
    ids = await _seed_two_failed_movies(sessionmaker_)
    hash_x, hash_y = "a" * 40, "b" * 40  # X removed first, Y second
    x_request_id, x_download_id = ids[hash_x]

    qbt = _MarkFailedAfterEarlierRemovalQbt(sessionmaker_, x_download_id, hash_y)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    # The gap command was refused with the physics error...
    assert isinstance(qbt.nested_error, RemovalInProgressError)
    # ...and the cycle settled X normally: terminal, blocklisted, re-armed.
    async with sessionmaker_() as session:
        download_x = await session.get(Download, x_download_id)
        request_x = await session.get(MediaRequest, x_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert set(qbt.removed) == {(hash_x, True), (hash_y, True)}
    assert download_x is not None and download_x.status == "failed"
    assert request_x is not None and request_x.status is RequestStatus.searching
    assert sorted(row.torrent_hash or "" for row in blocklist) == [hash_x, hash_y]

    # The guard released at cycle scope (settled): nothing lingers...
    assert not queue_service._reconcile_removals_in_flight  # pyright: ignore[reportPrivateUsage]
    # ...and a fresh mark_failed on the now-terminal row gets the honest
    # already-terminal handling, not removal_in_progress.
    async with sessionmaker_() as session:
        with pytest.raises(queue_service.InvalidStateTransitionError):
            await queue_service.mark_failed(
                session, None, download_id=x_download_id, blocklist=False, remove_torrent=False
            )


# ---------------------------------------------------------------------------
# Round 9: the durability story completes. Phase B re-proves durably before
# each delete, and a performed removal is persisted as remove=done.
# ---------------------------------------------------------------------------


class _CompleteOtherRowDuringRemovalQbt(FakeQbittorrent):
    """Round-9 finding 1's interleave: while the FIRST row's delete is in flight,
    a nested mark_failed on the SECOND row runs to FULL completion (stamp,
    terminal CAS, release). By the time reconcile's Phase B reaches the second
    row there is no live claim -- only the DURABLE record (terminal status /
    final reason). The pre-delete re-proof must see it and skip the deletion."""

    def __init__(self, sm: SessionMaker, target_download_id: int, trigger_hash: str) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._target_download_id = target_download_id
        self._trigger_hash = trigger_hash
        self.nested_status: str | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        await super().remove(info_hash, delete_files=delete_files)
        if info_hash == self._trigger_hash and self.nested_status is None:
            async with self._sm() as session:
                nested = await queue_service.mark_failed(
                    session,
                    None,
                    download_id=self._target_download_id,
                    blocklist=False,
                    remove_torrent=False,
                )
                self.nested_status = nested.status


async def test_phase_b_durable_reproof_skips_deletion_after_operator_won(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-9 finding 1: an operator who stamped, COMPLETED, and released between
    Phase A and the row's Phase-B delete leaves no live claim -- the in-process
    check alone would still delete data whose remove=no already won durably. The
    pre-delete re-proof (one fresh SELECT) sees the terminal row and skips the
    removal, dropping the completion; the operator's semantics stand."""
    ids = await _seed_two_failed_movies(sessionmaker_)
    hash_x, hash_y = "a" * 40, "b" * 40  # X removed first; Y is the operator's row
    x_download_id = ids[hash_x][1]
    y_request_id, y_download_id = ids[hash_y]

    qbt = _CompleteOtherRowDuringRemovalQbt(sessionmaker_, y_download_id, hash_x)
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    # The nested operator command fully completed Y with remove=no semantics...
    assert qbt.nested_status == "failed"
    # ...and Phase B's durable re-proof kept reconcile from deleting Y's data:
    # only X's torrent was ever removed.
    assert qbt.removed == [(hash_x, True)]

    async with sessionmaker_() as session:
        download_x = await session.get(Download, x_download_id)
        download_y = await session.get(Download, y_download_id)
        request_y = await session.get(MediaRequest, y_request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    # X completed normally under reconcile-default semantics.
    assert download_x is not None and download_x.status == "failed"
    assert [row.torrent_hash for row in blocklist] == [hash_x]
    # Y's operator semantics stand end-to-end: terminal via the operator, request
    # re-armed, no blocklist row, and no deletion.
    assert download_y is not None and download_y.status == "failed"
    assert download_y.failed_reason == "marked failed by operator"
    assert request_y is not None and request_y.status is RequestStatus.searching


async def test_completed_removal_is_durable_and_heals_without_a_client(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 finding 2: a mark_failed(remove_torrent=True) whose Phase B removed
    the torrent persists that outcome as a remove=done marker (committed at once),
    so a Phase-C exhaustion leaves a residual the DB-only healer completes during
    an outage / unconfigured-client stretch -- it no longer waits forever for a
    removal that already happened."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        # Commits: #1 Phase A (marker remove=yes), #2 the remove=done restamp,
        # #3..#5 the three Phase-C attempts -- fail exactly those.
        _fail_commit_on(session, monkeypatch, {3, 4, 5})
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, qbt, download_id=download_id, blocklist=False, remove_torrent=True
            )

    # The removal ran AND its outcome is durable on the residual.
    assert qbt.removed == [(_HASH, True)]
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None and download.status == "failed_pending"
    assert download.failed_reason is not None
    assert re.fullmatch(
        r"operator mark-failed in progress \(blocklist=no, remove=done, nonce=\d+\)",
        download.failed_reason,
    )

    # The DB-only healer (outage / unconfigured client) completes it -- no client,
    # no removal attempt, operator flags honored.
    async with sessionmaker_() as session:
        await queue_service.heal_failed_pending_without_client(session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None and download.status == "failed"
    assert download.failed_reason == "marked failed by operator"
    assert request is not None and request.status is RequestStatus.searching
    assert blocklist == []  # blocklist=False survived


# ---------------------------------------------------------------------------
# Round 10: guard-first ordering in Phase B, and reconcile-owned removals
# persist their outcome too.
# ---------------------------------------------------------------------------


async def test_removal_guard_bars_operators_before_the_pre_delete_reproof(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 finding 1 (interleave b): the removal-physics guard is registered
    SYNCHRONOUSLY before the durable re-proof's first await, so an operator
    arriving in the re-proof window is refused by the guard -- there is no yield
    point between "operators are barred" and "the row's durable state is
    verified". (Interleave a -- the operator finishing BEFORE the bar -- is
    caught by the re-proof read instead:
    test_phase_b_durable_reproof_skips_deletion_after_operator_won.)"""
    _, download_id = await _seed_movie_request_and_download(sessionmaker_)
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.first_seen_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()

    refusals: list[Exception] = []
    async with sessionmaker_() as session:
        real_rollback = session.rollback

        async def _probe_then_rollback() -> None:
            # The FIRST Phase-B rollback runs immediately after the guard-add;
            # with guard-first ordering an operator registration here must be
            # refused. (Were the guard registered after the re-proof, this
            # registration would succeed and the probe list would stay empty.)
            in_flight = queue_service._reconcile_removals_in_flight  # pyright: ignore[reportPrivateUsage]
            if download_id in in_flight and not refusals:
                try:
                    _register_operator_claim(
                        download_id, _OperatorFailFlags(blocklist=False, remove_torrent=False)
                    )
                except RemovalInProgressError as exc:
                    refusals.append(exc)
            await real_rollback()

        monkeypatch.setattr(session, "rollback", _probe_then_rollback)
        qbt = FakeQbittorrent(statuses=[])
        await queue_service.reconcile_and_list(qbt, session)

    # The pre-delete window refused the operator (guard registered first)...
    assert len(refusals) == 1 and isinstance(refusals[0], RemovalInProgressError)
    # ...and the cycle then completed normally.
    assert qbt.removed == [(_HASH, True)]
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None and download.status == "failed"
    assert not queue_service._reconcile_removals_in_flight  # pyright: ignore[reportPrivateUsage]


async def test_reconcile_owned_removal_outcome_is_durable_and_heals_without_client(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 finding 2: a PLAIN (unmarked) reconcile failure whose delete
    succeeded but whose Phase C exhausted now carries the reconcile-provenance
    remove=done record, so the DB-only healer completes it during an outage with
    the reconcile-default semantics -- it no longer waits for client I/O that
    already happened."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        assert download is not None
        download.first_seen_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        # Commits: #1 Phase A, #2 the reconcile-done restamp, #3-#5 the Phase C
        # attempts -- exhaust exactly those three.
        _fail_commit_on(session, monkeypatch, {3, 4, 5})
        with pytest.raises(OperationalError):
            await queue_service.reconcile_and_list(qbt, session)

    # The removal ran AND the reconcile-provenance outcome is durable.
    assert qbt.removed == [(_HASH, True)]
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None and download.status == "failed_pending"
    assert (
        download.failed_reason
        == "reconcile failure in progress (blocklist=yes, remove=done, nonce=0)"
    )

    # The DB-only healer (outage / unconfigured client) completes it with the
    # reconcile DEFAULT semantics: blocklisted (reason 'failed'), re-armed, and
    # an honest final reason -- without any client.
    async with sessionmaker_() as session:
        await queue_service.heal_failed_pending_without_client(session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None and download.status == "failed"
    assert download.failed_reason == "download failed; torrent already removed"
    assert request is not None and request.status is RequestStatus.searching
    assert len(blocklist) == 1
    assert blocklist[0].reason.value == "failed"  # reconcile vocabulary, not operator


# ---------------------------------------------------------------------------
# Stalled-download self-heal (issue #165) -- the minimal fixed-cooldown design.
# reconcile_and_list detects a download stuck in metadata-fetching or with a
# dead/frozen download using THIS cycle's already-fetched DownloadStatus list,
# and self-heals it via the EXACT same mark_failed(blocklist=True,
# remove_torrent=True) call the operator's manual button makes.
# ---------------------------------------------------------------------------
async def test_metadata_stall_past_threshold_is_auto_mark_failed(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="metadata_fetching",
        added_at=datetime.now(UTC) - timedelta(minutes=46),
    )

    qbt = FakeQbittorrent(
        statuses=[DownloadStatus(info_hash=_HASH, name=_TITLE, raw_state="metaDL")]
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    assert download is not None and download.status == "failed"
    assert request is not None and request.status is RequestStatus.searching
    assert len(blocklist) == 1
    assert qbt.removed == [(_HASH, True)]
    assert len(history) == 1
    assert "metadata_stall" in (history[0].message or "")


async def test_metadata_stall_under_threshold_is_not_healed(
    sessionmaker_: SessionMaker,
) -> None:
    _request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="metadata_fetching",
        added_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    qbt = FakeQbittorrent(
        statuses=[DownloadStatus(info_hash=_HASH, name=_TITLE, raw_state="metaDL")]
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    assert download is not None and download.status == "metadata_fetching"
    assert blocklist == []
    assert history == []
    assert qbt.removed == []


async def test_stalled_dl_raw_state_past_threshold_is_auto_mark_failed(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    stale_activity = int((datetime.now(UTC) - timedelta(hours=5)).timestamp())

    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=_TITLE,
                raw_state="stalledDL",
                last_activity_unix=stale_activity,
            )
        ]
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    assert download is not None and download.status == "failed"
    assert request is not None and request.status is RequestStatus.searching
    assert len(blocklist) == 1
    assert qbt.removed == [(_HASH, True)]
    assert len(history) == 1
    assert "stalled_progress" in (history[0].message or "")


async def test_stalled_dl_raw_state_with_recent_activity_is_not_healed(
    sessionmaker_: SessionMaker,
) -> None:
    """Regression: a flaky-but-alive seeder reporting a single transient
    ``stalledDL`` tick (last activity 30s ago) on a row added well past the
    stall window must NOT be self-healed -- destroying a healthy, actively
    transferring torrent violates "correction, never destruction"."""
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    recent_activity = int((datetime.now(UTC) - timedelta(seconds=30)).timestamp())

    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=_TITLE,
                raw_state="stalledDL",
                last_activity_unix=recent_activity,
            )
        ]
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    assert download is not None and download.status == "downloading"
    assert request is not None and request.status is RequestStatus.downloading
    assert blocklist == []
    assert qbt.removed == []
    assert history == []


async def test_downloading_row_with_recent_activity_is_not_healed(
    sessionmaker_: SessionMaker,
) -> None:
    _request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    recent_activity = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())

    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash=_HASH,
                name=_TITLE,
                raw_state="downloading",
                last_activity_unix=recent_activity,
            )
        ]
    )
    async with sessionmaker_() as session:
        await queue_service.reconcile_and_list(qbt, session)

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    assert download is not None and download.status == "downloading"
    assert blocklist == []
    assert history == []
    assert qbt.removed == []


async def test_self_heal_skips_a_download_that_vanished_mid_cycle(
    sessionmaker_: SessionMaker,
) -> None:
    """The self-heal's own guardrail: mark_failed raising for a row that no
    longer exists (a race between this cycle's stall snapshot and the heal
    call landing) is logged and skipped, not raised -- one stalled row's edge
    case must never abort the whole reconcile cycle."""
    row = DownloadRecord(
        id=999_999,
        torrent_hash=_HASH,
        status="metadata_fetching",
        added_at=datetime.now(UTC),
    )
    detection = StallDetection(download_id=999_999, torrent_hash=_HASH, shape="metadata_stall")

    async with sessionmaker_() as session:
        await _self_heal_stalled_download(
            session, FakeQbittorrent(), row, detection, now=datetime.now(UTC)
        )
        history = (await session.execute(select(DownloadHistory))).scalars().all()

    assert history == []


async def test_self_heal_never_overrides_a_concurrent_operator_claim(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #165 hardening finding 3: reconcile_and_list's own
    ``_is_operator_claimed`` pre-check is not atomic with self-heal's later
    ``mark_failed`` call -- an operator's manual mark_failed(remove_torrent=False,
    blocklist=False) can register its claim in the gap between that pre-check and
    self-heal's own registration attempt. Self-heal's claim registration
    (``allow_claim_supersede=False``) must back off rather than superseding: the
    row, the operator's claim, and the queue must all be untouched by the heal."""
    _request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    row = DownloadRecord(
        id=download_id,
        torrent_hash=_HASH,
        status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    detection = StallDetection(
        download_id=download_id, torrent_hash=_HASH, shape="stalled_progress"
    )

    # Simulate the operator's manual mark_failed having already registered its
    # claim (past reconcile's pre-check, mid-flight) with its own explicit
    # choice to keep the torrent and skip the blocklist.
    operator_flags = _OperatorFailFlags(blocklist=False, remove_torrent=False)
    operator_token = _register_operator_claim(download_id, operator_flags)

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await _self_heal_stalled_download(session, qbt, row, detection, now=datetime.now(UTC))
        download = await session.get(Download, download_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    # Self-heal backed off entirely: no removal, no blocklist, no history event,
    # and the row's status is exactly as the operator's in-flight call left it.
    assert qbt.removed == []
    assert blocklist == []
    assert history == []
    assert download is not None and download.status == "downloading"

    # The operator's own claim survived untouched -- self-heal never became the
    # newer owner.
    assert _owns_operator_claim(download_id, operator_token)
    _release_operator_claim(download_id, operator_token)


async def test_mark_failed_refuses_to_adopt_when_disallowed(
    sessionmaker_: SessionMaker,
) -> None:
    """Direct unit test for the new guard (Codex review, comment 3541100611):
    ``mark_failed(allow_adopt_existing_marker=False)`` on a row already at
    ``failed_pending`` raises :class:`FailedPendingAdoptionRefusedError` instead
    of restamping it -- the row, its marker, and the request are left completely
    untouched, and the claim registry has nothing leaked behind."""
    request_id, download_id = await _seed_movie_request_and_download(
        sessionmaker_,
        download_status="failed_pending",
        failed_reason="operator mark-failed in progress (blocklist=no, remove=no, nonce=7)",
    )

    async with sessionmaker_() as session:
        with pytest.raises(FailedPendingAdoptionRefusedError):
            await queue_service.mark_failed(
                session,
                None,
                download_id=download_id,
                blocklist=True,
                remove_torrent=False,
                allow_adopt_existing_marker=False,
            )

    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert download is not None
    assert download.status == "failed_pending"
    assert (
        download.failed_reason
        == "operator mark-failed in progress (blocklist=no, remove=no, nonce=7)"
    )
    assert request is not None
    assert request.status is RequestStatus.downloading
    assert blocklist == []


async def test_self_heal_refuses_to_adopt_a_released_operator_residual(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review (comment 3541100611): the narrower race left open by issue
    #165's claim-supersede fix (``allow_claim_supersede=False``). That guard only
    refuses a claim that is STILL LIVE; it says nothing about a row an operator's
    call already finished with and moved on from -- a claim is released the
    instant its owning call returns, but the durable ``failed_pending`` row +
    marker it stamped can outlive that release (here: a genuine Phase-C
    exhaustion, exactly like
    ``test_mark_failed_exhaustion_residual_heals_with_operator_flags`` above).

    Without the ``allow_adopt_existing_marker=False`` fix, self-heal's own
    ``mark_failed`` call would see no live claim, walk into the adopt branch, and
    restamp the operator's ``blocklist=False, remove_torrent=False`` residual
    with the reconcile-default ``blocklist=True, remove_torrent=True`` --
    silently overriding the operator's explicit choice. This test proves that no
    longer happens: the residual, its marker, and the request are left exactly
    as the operator's call left them."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    # The operator's manual mark_failed(blocklist=False, remove_torrent=False)
    # commits Phase A (stamps its marker) but every Phase C attempt fails, so the
    # call raises -- yet its `finally` still releases the claim (see
    # test_mark_failed_exhaustion_residual_heals_with_operator_flags).
    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {2, 3, 4})
        with pytest.raises(OperationalError):
            await queue_service.mark_failed(
                session, None, download_id=download_id, blocklist=False, remove_torrent=False
            )
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
    assert download is not None
    assert download.status == "failed_pending"
    operator_marker = download.failed_reason
    assert operator_marker is not None
    assert re.fullmatch(
        r"operator mark-failed in progress \(blocklist=no, remove=no, nonce=\d+\)",
        operator_marker,
    )

    # Self-heal's own stall-detection snapshot (taken at the top of THIS cycle,
    # before the operator's race landed) still shows the row as downloading --
    # exactly the eligibility detect_stalls would have computed.
    row = DownloadRecord(
        id=download_id,
        torrent_hash=_HASH,
        status="downloading",
        added_at=datetime.now(UTC) - timedelta(hours=4),
    )
    detection = StallDetection(
        download_id=download_id, torrent_hash=_HASH, shape="stalled_progress"
    )

    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        await _self_heal_stalled_download(session, qbt, row, detection, now=datetime.now(UTC))

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.stalled
                    )
                )
            )
            .scalars()
            .all()
        )

    # The operator's residual is untouched: no restamp, no removal, no blocklist,
    # no history event, and the request stays owned by the still-pending failure.
    assert download is not None
    assert download.status == "failed_pending"
    assert download.failed_reason == operator_marker  # NOT restamped by self-heal
    assert qbt.removed == []  # remove_torrent=True never ran
    assert blocklist == []  # blocklist=True never ran
    assert history == []
    assert request is not None
    assert request.status is RequestStatus.downloading  # not re-armed by self-heal
