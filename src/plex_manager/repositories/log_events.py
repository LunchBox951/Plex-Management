"""``LogEventRepository`` implementation over an :class:`AsyncSession`.

Backs the durable, LLM-diagnosable log store (ADR-0012). ``correlation_id``
filtering compares every well-known correlation key's extracted JSON value
against the supplied id, and needs BOTH halves of ``.as_string()`` + an
explicit ``CAST(... AS VARCHAR)`` to work for arbitrary (int- or
string-valued) keys on both dialects:

- plain ``context_json[key]`` (SQLAlchemy's ``JSON`` ``->`` accessor), CAST to
  a string, renders as ``CAST(JSON_QUOTE(JSON_EXTRACT(...)) AS VARCHAR)`` on
  SQLite -- ``JSON_QUOTE`` re-wraps a string value in literal quotes (e.g.
  ``'"abc"'``), so a string-valued key would silently never match a bare
  ``correlation_id``. ``.as_string()`` asks SQLAlchemy for the *unquoted* text
  extraction instead (bare ``JSON_EXTRACT`` on SQLite; ``->>`` on PostgreSQL),
  which is correct for both int- and string-valued keys.
- but bare ``.as_string()`` alone reintroduces the ORIGINAL bug on SQLite:
  ``JSON_EXTRACT`` preserves the value's native SQLite storage class (INTEGER
  for a numeric key like ``tmdb_id``), and comparing an INTEGER-affinity
  expression against a TEXT bound parameter (``'603'``) never matches --
  SQLite applies no implicit text<->numeric coercion here. The explicit outer
  ``CAST(... AS VARCHAR)`` forces TEXT affinity so the comparison actually
  works.

Combined -- ``CAST(context_json[key].as_string() AS VARCHAR) == correlation_id``
-- is correct for int- AND string-valued keys on both SQLite and PostgreSQL (on
PostgreSQL ``.as_string()`` already self-CASTs via ``->>``, making the outer
CAST redundant-but-harmless there). A future correlation key that carries a
string (e.g. an ``info_hash``) is therefore filterable exactly like the
existing integer ones -- do not revert to the bare ``->`` accessor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import ColumnElement, CursorResult, String, delete, func, insert, or_, select
from sqlalchemy import cast as sql_cast

from plex_manager.models import LogEvent
from plex_manager.ports.repositories import (
    LOG_EVENT_CORRELATION_KEYS,
    LogEventCreate,
    LogEventPage,
    LogEventRecord,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlLogEventRepository"]


def _as_utc(value: datetime) -> datetime:
    """Coerce a stored timestamp to tz-aware UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns;
    every value this repository writes is UTC (either DB-stamped ``now()`` or a
    caller-supplied ``datetime.now(UTC)``), so attaching UTC here keeps the DTO
    contract tz-aware regardless of backend. Mirrors ``repositories.downloads``'s
    identically-named helper.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_record(row: LogEvent) -> LogEventRecord:
    """Map a ``LogEvent`` ORM row to its frozen read-model DTO."""
    return LogEventRecord(
        id=row.id,
        created_at=_as_utc(row.created_at),
        level=row.level,
        logger=row.logger,
        message=row.message,
        context=row.context_json,
    )


def _correlation_filter(correlation_id: str) -> ColumnElement[bool]:
    """A record matches if ANY well-known correlation key's value equals ``correlation_id``.

    Each candidate key is extracted via ``.as_string()`` (unquoted text, not
    JSON_QUOTE-wrapped) and CAST to a string (forces TEXT affinity so a
    numeric SQLite storage class still compares equal) before comparison --
    see the module docstring. Correct for both int-valued keys
    (``tmdb_id``/``request_id``/``download_id``, today's only keys) and any
    future string-valued key.
    """
    return or_(
        *(
            sql_cast(LogEvent.context_json[key].as_string(), String) == correlation_id
            for key in LOG_EVENT_CORRELATION_KEYS
        )
    )


class SqlLogEventRepository:
    """Persist and read captured log records via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        level: str,
        logger: str,
        message: str,
        created_at: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> LogEventRecord:
        row = LogEvent(level=level, logger=logger, message=message, context_json=context)
        if created_at is not None:
            # Overrides the column's ``server_default=func.now()`` with the
            # caller-supplied original emission time.
            row.created_at = created_at
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def create_many(self, events: Sequence[LogEventCreate]) -> None:
        if not events:
            return
        # A Core executemany (one statement, driver-batched) instead of N
        # ORM-tracked inserts -- the drain task's only caller discards the
        # return, so no per-row RETURNING/refresh is needed (see #98).
        values: list[dict[str, Any]] = [
            {
                "created_at": event.created_at,
                "level": event.level,
                "logger": event.logger,
                "message": event.message,
                "context_json": event.context,
            }
            for event in events
        ]
        await self._session.execute(insert(LogEvent), values)

    async def list_events(
        self,
        *,
        level: str | None = None,
        since: datetime | None = None,
        logger: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> LogEventPage:
        filters: list[ColumnElement[bool]] = []
        if level is not None:
            filters.append(LogEvent.level == level)
        if since is not None:
            filters.append(LogEvent.created_at >= since)
        if logger is not None:
            filters.append(LogEvent.logger == logger)
        if correlation_id is not None:
            filters.append(_correlation_filter(correlation_id))

        count_stmt = select(func.count()).select_from(LogEvent)
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = (await self._session.execute(count_stmt)).scalar_one()

        stmt = select(LogEvent)
        if filters:
            stmt = stmt.where(*filters)
        # Newest first by default (a log viewer's default read direction);
        # ``oldest_first`` flips both columns for the export endpoint, which
        # needs the OLDEST matching rows kept when a window exceeds its cap
        # (#96). Either way ``id`` breaks ties between rows a single
        # batch-insert stamped with the identical ``created_at`` -- an
        # application-supplied, not DB-assigned, value.
        if oldest_first:
            stmt = stmt.order_by(LogEvent.created_at.asc(), LogEvent.id.asc())
        else:
            stmt = stmt.order_by(LogEvent.created_at.desc(), LogEvent.id.desc())
        stmt = stmt.limit(limit).offset(offset)
        rows = (await self._session.execute(stmt)).scalars().all()
        return LogEventPage(total=total, results=tuple(_to_record(row) for row in rows))

    async def prune_older_than(
        self,
        cutoff: datetime,
        *,
        loggers: Sequence[str] | None = None,
        exclude_loggers: bool = False,
    ) -> int:
        stmt = delete(LogEvent).where(LogEvent.created_at < cutoff)
        if loggers is not None:
            # ``LogEvent.logger`` is NOT NULL, so ``NOT IN`` cannot silently skip
            # NULL rows -- the exclusion delete really covers every non-telemetry row.
            matches = LogEvent.logger.in_(loggers)
            stmt = stmt.where(~matches if exclude_loggers else matches)
        # A DML statement yields a ``CursorResult`` carrying ``rowcount`` (the base
        # ``Result`` that ``AsyncSession.execute`` is typed to does not expose it).
        # The cast target is referenced at runtime (not a string) so CodeQL does
        # not read ``CursorResult``/``Any`` as unused imports -- mirrors
        # ``SqlDownloadRepository.update_status_if_in``.
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount
