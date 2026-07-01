"""``DownloadRepository`` implementation over an :class:`AsyncSession`.

``downloads.status`` is a free-form ``str`` column holding the P4
``DownloadState`` value. To keep this layer decoupled from the (separately
owned) state-machine enum, the terminal-state vocabulary is duplicated here as
string literals; it mirrors P4's terminal ``DownloadState`` members.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select, update

from plex_manager.models import Download
from plex_manager.ports.repositories import DownloadRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlDownloadRepository"]

# Downloads in one of these states are finished and excluded from the reconcile
# loop. Mirrors P4's terminal ``DownloadState`` values (string-compared because
# the column is a plain ``str`` and P4's enum is not a P2 dependency).
_TERMINAL_DOWNLOAD_STATUSES: frozenset[str] = frozenset(
    {"imported", "failed", "no_acceptable_release"}
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a stored timestamp to tz-aware UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns;
    the app's stored values are always UTC (``datetime.now(timezone.utc)``), and
    the reconciler does aware-datetime arithmetic on ``first_seen_at``. Attaching
    UTC here keeps the DTO contract tz-aware regardless of backend.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_record(row: Download) -> DownloadRecord:
    """Map a ``Download`` ORM row to its frozen read-model DTO."""
    return DownloadRecord(
        id=row.id,
        torrent_hash=row.torrent_hash,
        status=row.status,
        media_request_id=row.media_request_id,
        magnet_link=row.magnet_link,
        progress=row.progress,
        seed_ratio=row.seed_ratio,
        tmdb_id=row.tmdb_id,
        year=row.year,
        season=row.season,
        episodes=row.episodes_json,
        failed_reason=row.failed_reason,
        first_seen_at=_as_utc(row.first_seen_at),
        download_path=row.download_path,
    )


class SqlDownloadRepository:
    """Persist and read tracked downloads via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, torrent_hash: str) -> DownloadRecord | None:
        stmt = select(Download).where(Download.torrent_hash == torrent_hash)
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def find_active_for_request(
        self, media_request_id: int, *, season: int | None = None
    ) -> DownloadRecord | None:
        # ``Download.season == season`` renders ``IS NULL`` when ``season`` is
        # ``None`` (SQLAlchemy's standard ``== None`` -> ``IS NULL`` translation),
        # so movie callers (``season=None``) keep matching only the NULL-season
        # rows they always create -- identical to the pre-widen behaviour, since a
        # movie never has a non-NULL ``season``. TV callers pass the season being
        # grabbed, scoping the guard to that season only.
        stmt = (
            select(Download)
            .where(
                Download.media_request_id == media_request_id,
                Download.season == season,
                Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES),
            )
            .order_by(Download.id)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def list_active(self) -> list[DownloadRecord]:
        stmt = (
            select(Download)
            .where(Download.status.notin_(_TERMINAL_DOWNLOAD_STATUSES))
            .order_by(Download.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def create(
        self,
        *,
        torrent_hash: str,
        status: str,
        media_request_id: int | None = None,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episodes: list[int] | None = None,
    ) -> DownloadRecord:
        row = Download(
            torrent_hash=torrent_hash,
            status=status,
            media_request_id=media_request_id,
            magnet_link=magnet_link,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
            episodes_json=episodes,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def update_status(
        self,
        download_id: int,
        status: str,
        *,
        progress: float | None = None,
        seed_ratio: float | None = None,
        failed_reason: str | None = None,
        download_path: str | None = None,
        first_seen_at: datetime | None = None,
        clear_first_seen_at: bool = False,
        clear_failed_reason: bool = False,
        clear_download_path: bool = False,
        media_request_id: int | None = None,
    ) -> None:
        row = await self._session.get(Download, download_id)
        if row is None:
            raise LookupError(f"download {download_id} does not exist")
        row.status = status
        if progress is not None:
            row.progress = progress
        if seed_ratio is not None:
            row.seed_ratio = seed_ratio
        if media_request_id is not None:
            # Re-own a reused (terminal) row: a fresh grab from a different request
            # must point the row at the CURRENT request, not the stale prior owner.
            row.media_request_id = media_request_id
        if clear_failed_reason:
            # A terminal row being reused for a fresh grab must not carry a stale
            # failure reason (honesty over silence: a Downloading row claiming a
            # failure is a dishonest state).
            row.failed_reason = None
        elif failed_reason is not None:
            row.failed_reason = failed_reason
        if clear_download_path:
            # A rolled-back placement (scan-failure orphan): drop the breadcrumb so a
            # later retry's _resolve_content can't treat the now-deleted library path
            # as the torrent's content.
            row.download_path = None
        elif download_path is not None:
            row.download_path = download_path
        if clear_first_seen_at:
            # Explicit reset to NULL (a recovered ClientMissing torrent): distinct
            # from first_seen_at=None, which leaves the existing anchor unchanged.
            row.first_seen_at = None
        elif first_seen_at is not None:
            row.first_seen_at = first_seen_at
        await self._session.flush()

    async def update_status_if_in(
        self,
        download_id: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        download_path: str | None = None,
        failed_reason: str | None = None,
        clear_download_path: bool = False,
    ) -> bool:
        """Compare-and-swap the status: move to ``status`` only if the row's CURRENT
        persisted status is in ``allowed_from``. Returns whether a row was updated.

        ``update_status`` re-reads the row through the session identity map and issues
        an unconditional ``UPDATE ... WHERE id = ?``, so a status another session
        committed during a long async gap (e.g. an operator's mark_failed) would be
        silently overwritten. This issues a single ``UPDATE ... WHERE id = ? AND status
        IN (...)`` so the DATABASE — not stale in-memory state — decides whether the
        move still applies; ``False`` means the row moved out from under the caller and
        the transition must be abandoned, honoring whoever changed it.

        ``failed_reason`` and ``clear_download_path`` mirror :meth:`update_status` so a
        CONDITIONAL block can record its surfaced reason (and drop a rolled-back
        placement breadcrumb) in the SAME compare-and-swap — never overwriting a row
        that already left ``allowed_from`` (e.g. an operator's committed mark_failed).
        ``clear_download_path`` takes precedence over ``download_path``.

        ``synchronize_session="fetch"`` keeps any already-loaded identity-map instance
        consistent with the DB result, so a later read returns the honest post-CAS
        status (and reason / cleared path).
        """
        values: dict[str, str | None] = {"status": status}
        if clear_download_path:
            values["download_path"] = None
        elif download_path is not None:
            values["download_path"] = download_path
        if failed_reason is not None:
            values["failed_reason"] = failed_reason
        stmt = (
            update(Download)
            .where(Download.id == download_id, Download.status.in_(allowed_from))
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        # A DML statement yields a ``CursorResult`` carrying ``rowcount`` (the base
        # ``Result`` that ``AsyncSession.execute`` is typed to does not expose it). The
        # cast target is referenced at runtime (not a string) so CodeQL does not read
        # ``CursorResult``/``Any`` as unused imports.
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def refresh_progress(
        self,
        download_id: int,
        *,
        progress: float | None = None,
        seed_ratio: float | None = None,
    ) -> None:
        """Update ONLY live progress / seed_ratio — never status.

        The reconcile loop refreshes progress on rows with no state transition. It must
        NOT rewrite status: an operator's import retry (or the importer) may have
        CAS-claimed the row to ``importing`` between the loop's ``list_active`` snapshot
        and this write, and rewriting the stale snapshot status would clobber that claim
        (defeating the import finalize CAS and stranding the placed file). Touching only
        progress/seed_ratio leaves any concurrent status transition intact.
        """
        row = await self._session.get(Download, download_id)
        if row is None:
            return
        if progress is not None:
            row.progress = progress
        if seed_ratio is not None:
            row.seed_ratio = seed_ratio
        await self._session.flush()
