"""``DownloadRepository`` implementation over an :class:`AsyncSession`.

``downloads.status`` is a free-form ``str`` column holding the P4
``DownloadState`` value. To keep this layer decoupled from the (separately
owned) state-machine enum, the terminal-state vocabulary is duplicated here as
string literals; it mirrors P4's terminal ``DownloadState`` members.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

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
    ) -> DownloadRecord:
        row = Download(
            torrent_hash=torrent_hash,
            status=status,
            media_request_id=media_request_id,
            magnet_link=magnet_link,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
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
    ) -> None:
        row = await self._session.get(Download, download_id)
        if row is None:
            raise LookupError(f"download {download_id} does not exist")
        row.status = status
        if progress is not None:
            row.progress = progress
        if seed_ratio is not None:
            row.seed_ratio = seed_ratio
        if failed_reason is not None:
            row.failed_reason = failed_reason
        if download_path is not None:
            row.download_path = download_path
        if first_seen_at is not None:
            row.first_seen_at = first_seen_at
        await self._session.flush()
