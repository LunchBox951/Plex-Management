"""tv season support

Three schema changes for TV (seasons + episodes) support, all part of the same
logical change (see ``docs/design/tv-beta-plan.md`` sections 4/5/12) so they
ship as one revision:

1. ``uq_media_requests_active``: add ``partially_available`` to the predicate.
   It is a TV-only rollup status (some seasons available, others still in
   flight) that must keep blocking a duplicate request for the same show,
   exactly like ``import_blocked`` / ``completed`` already do (precedent
   ``41d427bd38e6``). ``media_requests.status`` itself needs no change: it is
   a plain VARCHAR (``native_enum=False`` creates no CHECK constraint).
2. ``uq_downloads_active_request``: widen from a plain unique on
   ``media_request_id`` to a unique on ``(media_request_id, COALESCE(season,
   -1))``. This lets a whole-series TV request grab season 1 and season 2
   concurrently (two different ``SeasonRequest`` rows under the same
   ``MediaRequest``) while STILL guaranteeing at most one active movie
   download per request. A bare ``(media_request_id, season)`` unique index
   would silently break the movie guarantee: SQL NULL is never equal to NULL,
   so two movie downloads (``season IS NULL`` on both) would stop colliding.
   The ``COALESCE(season, -1)`` sentinel folds every movie row onto the same
   synthetic key, so the movie invariant is unchanged while real (non-NULL)
   season values are scoped per season for TV.
3. New ``uq_season_requests_media_season``: unconditional unique index on
   ``season_requests(media_request_id, season_number)`` — a show can never
   have two rows for the same season. This is what makes
   ``SeasonRequestRepository.ensure()`` race-safe: two concurrent grabs racing
   to lazily-create the same season row raise ``IntegrityError``, caught and
   resolved to a re-read (mirrors the IntegrityError-catch-and-reread pattern
   at ``request_service.py:159-184``).

NOTE on drift detection: ``uq_downloads_active_request`` is an
expression-based index (the ``COALESCE(season, -1)`` term). SQLite cannot
reflect expression indexes (``SAWarning: Skipped unsupported reflection of
expression-based index``), so ``alembic check``/autogenerate silently can't
compare it against ``Download.__table_args__`` on SQLite. The definitions
here and in ``models.py`` are hand-verified to match as of this revision;
any FUTURE change to this index must be applied to BOTH places by hand — it
will not be caught by drift tooling. The behavior (not the DDL text) is
covered by ``tests/persistence/test_download_repository.py``, which runs
against the ``Base.metadata.create_all`` schema and would fail if the two
definitions ever diverged in effect.

Revision ID: d3a7077fdbcc
Revises: 41d427bd38e6
Create Date: 2026-06-30 22:01:45.595300
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a7077fdbcc"
down_revision: str | None = "41d427bd38e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_REQUEST_INDEX = "uq_media_requests_active"
_NEW_ACTIVE_REQUEST_PREDICATE = (
    "status IN ('pending', 'searching', 'no_acceptable_release', "
    "'downloading', 'import_blocked', 'completed', 'partially_available')"
)
_OLD_ACTIVE_REQUEST_PREDICATE = (
    "status IN ('pending', 'searching', 'no_acceptable_release', "
    "'downloading', 'import_blocked', 'completed')"
)

_ACTIVE_DOWNLOAD_INDEX = "uq_downloads_active_request"
_ACTIVE_DOWNLOAD_PREDICATE = (
    "media_request_id IS NOT NULL AND status NOT IN ('imported', 'failed', 'no_acceptable_release')"
)

_SEASON_REQUEST_INDEX = "uq_season_requests_media_season"


def _recreate_active_request_index(predicate: str) -> None:
    op.drop_index(_ACTIVE_REQUEST_INDEX, table_name="media_requests")
    op.create_index(
        _ACTIVE_REQUEST_INDEX,
        "media_requests",
        ["tmdb_id", "media_type"],
        unique=True,
        sqlite_where=sa.text(predicate),
        postgresql_where=sa.text(predicate),
    )


def _recreate_active_download_index(*, season_scoped: bool) -> None:
    op.drop_index(_ACTIVE_DOWNLOAD_INDEX, table_name="downloads")
    columns: list[str | sa.ColumnElement[object]] = ["media_request_id"]
    if season_scoped:
        columns.append(sa.func.coalesce(sa.column("season", sa.Integer()), -1))
    op.create_index(
        _ACTIVE_DOWNLOAD_INDEX,
        "downloads",
        columns,
        unique=True,
        sqlite_where=sa.text(_ACTIVE_DOWNLOAD_PREDICATE),
        postgresql_where=sa.text(_ACTIVE_DOWNLOAD_PREDICATE),
    )


def upgrade() -> None:
    _recreate_active_request_index(_NEW_ACTIVE_REQUEST_PREDICATE)
    _recreate_active_download_index(season_scoped=True)
    op.create_index(
        _SEASON_REQUEST_INDEX,
        "season_requests",
        ["media_request_id", "season_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(_SEASON_REQUEST_INDEX, table_name="season_requests")
    _recreate_active_download_index(season_scoped=False)
    _recreate_active_request_index(_OLD_ACTIVE_REQUEST_PREDICATE)
