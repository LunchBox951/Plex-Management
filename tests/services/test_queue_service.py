"""queue_service — the auto-fail blocklist-and-research path beyond grace."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
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
from plex_manager.services.queue_service import (
    _is_operator_claimed,  # pyright: ignore[reportPrivateUsage]
    _OperatorFailFlags,  # pyright: ignore[reportPrivateUsage]
    _owns_operator_claim,  # pyright: ignore[reportPrivateUsage]
    _register_operator_claim,  # pyright: ignore[reportPrivateUsage]
    _release_operator_claim,  # pyright: ignore[reportPrivateUsage]
)
from tests.web.fakes import FakeQbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_HASH = "f" * 40
_TITLE = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_INDEXER = "FakeIndexer"


@pytest.fixture(autouse=True)
def clear_operator_claims() -> Iterator[None]:
    """Isolate the module-level claim registry between tests: a claim a failing
    test left registered must never leak into (and cascade through) later tests."""
    yield
    queue_service._operator_fail_claims.clear()  # pyright: ignore[reportPrivateUsage]


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
        # Fail the FIRST Phase C commit (#2 overall); the retry (#3) succeeds.
        _fail_commit_on(session, monkeypatch, {2})
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

    # Every Phase C commit (#2, #3, #4 -- the 3 bounded attempts) fails.
    async with sessionmaker_() as session:
        _fail_commit_on(session, monkeypatch, {2, 3, 4})
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
        _fail_commit_on(session, monkeypatch, {2, 3, 4})  # every Phase C attempt
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
    sm: SessionMaker, *, download_status: str = "downloading", failed_reason: str | None = None
) -> tuple[int, int]:
    """Insert a movie request + one tracked download; return (request_id, download_id)."""
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
    assert download.failed_reason == "operator mark-failed in progress (blocklist=no, remove=no)"

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
        failed_reason="operator mark-failed in progress (blocklist=maybe, remove=)",
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


class _SupersedingMarkFailedQbt(FakeQbittorrent):
    """A client whose ``remove`` (the FIRST call's Phase B) runs a SECOND, nested
    mark_failed on the same download with CONFLICTING flags — two overlapping
    operator commands (finding 3). The nested (newer) call must win: the earlier
    call's Phase C yields instead of completing with its stale choices, and its
    finally must not clear anything the newer call owns."""

    def __init__(self, sm: SessionMaker, download_id: int) -> None:
        super().__init__(statuses=[])
        self._sm = sm
        self._download_id = download_id
        self.nested_status: str | None = None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        async with self._sm() as session:
            nested = await queue_service.mark_failed(
                session,
                None,
                download_id=self._download_id,
                blocklist=False,  # conflicts with the outer call's blocklist=True
                remove_torrent=False,
            )
            self.nested_status = nested.status
        await super().remove(info_hash, delete_files=delete_files)


async def test_overlapping_mark_faileds_the_later_call_wins(
    sessionmaker_: SessionMaker,
) -> None:
    """Finding 3: overlapping mark_faileds with conflicting flags — the later call
    re-stamps the claim (token replaced) and completes with ITS flags; the earlier
    call's Phase C sees its token superseded and yields, so its stale blocklist=True
    can never land after the newer blocklist=False command."""
    request_id, download_id = await _seed_movie_request_and_download(sessionmaker_)

    qbt = _SupersedingMarkFailedQbt(sessionmaker_, download_id)
    async with sessionmaker_() as session:
        record = await queue_service.mark_failed(
            session, qbt, download_id=download_id, blocklist=True, remove_torrent=True
        )

    # The nested (later) call completed the row; the outer call yielded and returns
    # the completed state honestly.
    assert qbt.nested_status == "failed"
    assert record.status == "failed"
    assert record.failed_reason == "marked failed by operator"

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status is RequestStatus.searching
    assert blocklist == []  # the LATER call's blocklist=False won; no stale row
    # Neither finisher left a live claim behind (token-gated release both sides).
    assert not queue_service._operator_fail_claims  # pyright: ignore[reportPrivateUsage]
