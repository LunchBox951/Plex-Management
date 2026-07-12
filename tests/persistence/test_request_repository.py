"""``SqlRequestRepository`` create / get / list / find_active / set_status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MediaRequest, User
from plex_manager.repositories import SqlRequestRepository

# The statuses the auto-grab worker scans (ADR-0013); the backoff gate applies
# ONLY to the parked ``no_acceptable_release``.
_DUE_STATUSES = frozenset({"pending", "no_acceptable_release", "searching"})
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


async def test_create_then_get_returns_persisted_record(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=603,
        media_type="movie",
        title="The Matrix",
        status="pending",
        year=1999,
    )
    assert created.id > 0
    assert created.media_type == "movie"
    assert created.status == "pending"
    assert created.is_anime is False

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched == created


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    assert await repo.get(999) is None


async def test_list_by_status_filters(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=1, media_type="movie", title="A", status="pending")
    await repo.create(tmdb_id=2, media_type="movie", title="B", status="downloading")
    await repo.create(tmdb_id=3, media_type="tv", title="C", status="pending")

    pending = await repo.list_by_status("pending")
    assert {r.tmdb_id for r in pending} == {1, 3}
    assert len(await repo.list_by_status()) == 3


async def test_find_active_uses_tmdb_media_composite_for_dedup(
    session: AsyncSession,
) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=42, media_type="movie", title="Dup", status="searching")

    # Same tmdb_id but different media_type must NOT collide.
    assert await repo.find_active(42, "tv") is None
    active = await repo.find_active(42, "movie")
    assert active is not None
    assert active.tmdb_id == 42


async def test_find_active_ignores_settled_requests(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    # available/failed are SETTLED (no longer dedup-blocking).
    done = await repo.create(tmdb_id=7, media_type="movie", title="Done", status="available")
    assert await repo.find_active(7, "movie") is None

    # A non-settled request for the same media is found again.
    await repo.set_status(done.id, "searching")
    again = await repo.find_active(7, "movie")
    assert again is not None
    assert again.status == "searching"


async def test_find_active_treats_completed_finalizing_as_active(session: AsyncSession) -> None:
    # 'completed' is the in-flight "Finalizing" state (imported, before Plex confirms
    # availability) — it must keep deduping a second request for the same movie.
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=77, media_type="movie", title="Finalizing", status="completed")
    active = await repo.find_active(77, "movie")
    assert active is not None
    assert active.status == "completed"
    # And the DB backstop refuses a duplicate while it is still finalizing.
    with pytest.raises(IntegrityError):
        await repo.create(tmdb_id=77, media_type="movie", title="Dup", status="pending")


async def test_partially_available_round_trips_and_blocks_dedup(session: AsyncSession) -> None:
    """``partially_available`` is a TV-only rollup status (some seasons available,
    others still in flight). It must round-trip through the DB like any other
    status, be found by ``find_active`` (still in-flight, not settled), and keep
    blocking a duplicate request for the same show at the DB level — exactly like
    ``completed`` already does."""
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=88, media_type="tv", title="Partial Show", status="partially_available"
    )
    assert created.status == "partially_available"

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "partially_available"

    active = await repo.find_active(88, "tv")
    assert active is not None
    assert active.status == "partially_available"

    # The DB backstop refuses a duplicate request while a show is still
    # partially available (not yet fully available or failed).
    with pytest.raises(IntegrityError):
        await repo.create(tmdb_id=88, media_type="tv", title="Dup", status="pending")


async def test_partial_unique_index_blocks_second_active_request(
    session: AsyncSession,
) -> None:
    """The partial UNIQUE index serializes active-request dedup at the DB level: a
    second ACTIVE request for the same (tmdb_id, media_type) is rejected."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=500, media_type="movie", title="A", status="pending")
    with pytest.raises(IntegrityError):
        await repo.create(tmdb_id=500, media_type="movie", title="A again", status="searching")


async def test_partial_unique_index_allows_new_request_after_settled(
    session: AsyncSession,
) -> None:
    """Settled statuses (available/failed) are outside the partial index, so once a
    request truly finishes a fresh request for the same media is allowed — the index
    does not block legitimate re-requests after a title is removed from Plex."""
    repo = SqlRequestRepository(session)
    done = await repo.create(tmdb_id=600, media_type="movie", title="Done", status="available")
    fresh = await repo.create(tmdb_id=600, media_type="movie", title="Again", status="pending")
    assert fresh.id != done.id


async def test_partial_unique_index_allows_new_request_after_eviction(
    session: AsyncSession,
) -> None:
    """``evicted`` (ADR-0012) is ALSO outside the partial index, exactly like
    available/failed: the disk-pressure sweep already deleted the file, so a
    re-request must create a fresh, independent row that actually re-grabs the
    content rather than being rejected in favour of the old, now off-disk row."""
    repo = SqlRequestRepository(session)
    gone = await repo.create(tmdb_id=601, media_type="movie", title="Evicted", status="evicted")
    assert gone.status == "evicted"
    fresh = await repo.create(
        tmdb_id=601, media_type="movie", title="Evicted, re-requested", status="pending"
    )
    assert fresh.id != gone.id


async def test_partial_unique_index_scoped_by_media_type(session: AsyncSession) -> None:
    """The index is on (tmdb_id, media_type): the same tmdb_id under a different
    media_type is not a conflict."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=700, media_type="movie", title="M", status="pending")
    tv = await repo.create(tmdb_id=700, media_type="tv", title="T", status="pending")
    assert tv.id > 0


async def test_find_in_library_prefers_own_then_ownerless_then_newest_foreign(
    session: AsyncSession,
) -> None:
    """``prefer_user_id`` reorders WHICH terminal row wins when several exist:
    the caller's OWN row first (even when it is the oldest), then an ownerless
    claimable one, then anyone else's — while the unscoped default keeps the
    pre-preference newest-row-wins behavior for admins/API-key automation."""
    repo = SqlRequestRepository(session)
    mine = User(username="mine")
    other = User(username="other")
    stranger = User(username="stranger")
    session.add_all([mine, other, stranger])
    await session.flush()

    own_oldest = await repo.create(
        tmdb_id=64, media_type="movie", title="Own", status="available", user_id=mine.id
    )
    ownerless_mid = await repo.create(
        tmdb_id=64, media_type="movie", title="Ownerless", status="available"
    )
    foreign_newest = await repo.create(
        tmdb_id=64, media_type="movie", title="Foreign", status="available", user_id=other.id
    )

    # Unscoped (admins / API-key automation): the newest row wins, as before.
    unscoped = await repo.find_in_library(64, "movie")
    assert unscoped is not None and unscoped.id == foreign_newest.id

    # The caller's own row outranks BOTH newer rows.
    preferred = await repo.find_in_library(64, "movie", prefer_user_id=mine.id)
    assert preferred is not None and preferred.id == own_oldest.id

    # A caller with NO row of their own: the ownerless row beats the foreign one.
    claimable = await repo.find_in_library(64, "movie", prefer_user_id=stranger.id)
    assert claimable is not None and claimable.id == ownerless_mid.id


async def test_find_in_library_preference_picks_newest_within_a_rank(
    session: AsyncSession,
) -> None:
    """Ties inside one ownership rank keep the newest-by-id order — the
    preference only reorders BETWEEN ranks, matching the unscoped behavior."""
    repo = SqlRequestRepository(session)
    mine = User(username="rank-mine")
    session.add(mine)
    await session.flush()

    await repo.create(
        tmdb_id=65, media_type="movie", title="Older own", status="available", user_id=mine.id
    )
    newer_own = await repo.create(
        tmdb_id=65, media_type="movie", title="Newer own", status="available", user_id=mine.id
    )

    preferred = await repo.find_in_library(65, "movie", prefer_user_id=mine.id)
    assert preferred is not None and preferred.id == newer_own.id


async def test_set_status_updates(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=11, media_type="tv", title="Show", status="pending")
    await repo.set_status(created.id, "downloading")
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "downloading"


async def test_new_request_defaults_library_path_none_and_keep_forever_false(
    session: AsyncSession,
) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=12, media_type="movie", title="X", status="pending")
    assert created.library_path is None
    assert created.keep_forever is False


async def test_tv_request_intent_round_trips(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=900,
        media_type="tv",
        title="Show",
        status="pending",
        tv_request_mode="explicit_seasons",
        requested_seasons=[2, 1],
    )

    assert created.tv_request_mode == "explicit_seasons"
    assert created.requested_seasons == (1, 2)

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.tv_request_mode == "explicit_seasons"
    assert fetched.requested_seasons == (1, 2)


async def test_set_tv_request_intent_promotes_to_whole_show(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=901,
        media_type="tv",
        title="Show",
        status="pending",
        tv_request_mode="explicit_seasons",
        requested_seasons=[1],
    )

    await repo.set_tv_request_intent(created.id, mode="whole_show", requested_seasons=None)

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.tv_request_mode == "whole_show"
    assert fetched.requested_seasons is None


async def test_set_library_path_round_trips(session: AsyncSession) -> None:
    """The breadcrumb the disk-pressure eviction sweep later ``fs.delete()``s (ADR-0012)."""
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=13, media_type="movie", title="X", status="completed")

    await repo.set_library_path(created.id, "/data/library/movies/X (2024)/X.mkv")
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.library_path == "/data/library/movies/X (2024)/X.mkv"


async def test_set_library_path_missing_row_raises(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    with pytest.raises(LookupError):
        await repo.set_library_path(999, "/data/library/movies/Ghost/Ghost.mkv")


async def test_set_keep_forever_round_trips(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=14, media_type="movie", title="Pin me", status="available")
    assert created.keep_forever is False

    await repo.set_keep_forever(created.id, True)
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.keep_forever is True

    # Toggling back off is just as explicit -- the caller (not this method)
    # decides the target value, so a double-submit is idempotent either way.
    await repo.set_keep_forever(created.id, False)
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.keep_forever is False


async def test_set_keep_forever_missing_row_raises(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    with pytest.raises(LookupError):
        await repo.set_keep_forever(999, True)


# --------------------------------------------------------------------------- #
# list_due_for_search — the backoff gate applies ONLY to parked scopes (ADR-0013)
# --------------------------------------------------------------------------- #
async def test_list_due_returns_searching_scope_with_stale_future_backoff(
    session: AsyncSession,
) -> None:
    """A scope re-armed to ``searching`` (a failed download) may still carry a stale
    ``next_search_at`` from a PRIOR ``no_acceptable_release`` backoff -- re-arming
    does not clear it. ``searching`` is EAGER: it must be due IMMEDIATELY, never
    suppressed until that stale future timestamp expires."""
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=800, media_type="movie", title="Rearmed", status="searching"
    )
    await repo.schedule_search(
        created.id, search_attempts=3, next_search_at=_NOW + timedelta(hours=24)
    )

    due = await repo.list_due_for_search(_DUE_STATUSES, _NOW)
    assert [r.id for r in due] == [created.id]


async def test_list_due_suppresses_parked_scope_until_backoff_elapses(
    session: AsyncSession,
) -> None:
    """A parked ``no_acceptable_release`` scope earned its backoff: a FUTURE
    ``next_search_at`` means NOT due yet (existing behavior, pinned)."""
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=801, media_type="movie", title="Parked", status="no_acceptable_release"
    )
    await repo.schedule_search(
        created.id, search_attempts=1, next_search_at=_NOW + timedelta(hours=1)
    )

    due = await repo.list_due_for_search(_DUE_STATUSES, _NOW)
    assert due == []


async def test_list_due_orders_eager_scope_ahead_of_overdue_parked(
    session: AsyncSession,
) -> None:
    """An eager ``searching`` scope (stale future backoff) sorts due-now AHEAD of a
    parked scope whose backoff has elapsed -- never behind it by its stale timestamp."""
    repo = SqlRequestRepository(session)
    parked = await repo.create(
        tmdb_id=810, media_type="movie", title="Parked", status="no_acceptable_release"
    )
    await repo.schedule_search(
        parked.id, search_attempts=1, next_search_at=_NOW - timedelta(hours=1)
    )
    eager = await repo.create(tmdb_id=811, media_type="movie", title="Eager", status="searching")
    await repo.schedule_search(
        eager.id, search_attempts=2, next_search_at=_NOW + timedelta(hours=12)
    )

    due = await repo.list_due_for_search(_DUE_STATUSES, _NOW)
    assert [r.id for r in due] == [eager.id, parked.id]


# --------------------------------------------------------------------------- #
# display_statuses_by_tmdb_ids — batched tile decoration (issue #29)
# --------------------------------------------------------------------------- #
async def test_display_statuses_returns_active_row(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=100, media_type="movie", title="A", status="downloading")
    statuses = await repo.display_statuses_by_tmdb_ids([(100, "movie")])
    assert statuses == {(100, "movie"): "downloading"}


async def test_display_statuses_returns_available_row(session: AsyncSession) -> None:
    # The find_active trap: find_active EXCLUDES available, but the tile must show it
    # ("In library"). display_statuses returns the DISPLAY status, so available IS returned.
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=101, media_type="movie", title="Owned", status="available")
    statuses = await repo.display_statuses_by_tmdb_ids([(101, "movie")])
    assert statuses == {(101, "movie"): "available"}


async def test_display_statuses_prefers_active_over_older_settled(session: AsyncSession) -> None:
    # A stale settled row must never shadow a fresh active re-request for the same key
    # (mirrors the modal's liveRequest selection + find_active's non-settled preference).
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=102, media_type="movie", title="Old", status="available")
    await repo.create(tmdb_id=102, media_type="movie", title="Fresh", status="pending")
    statuses = await repo.display_statuses_by_tmdb_ids([(102, "movie")])
    assert statuses == {(102, "movie"): "pending"}


async def test_display_statuses_newest_settled_when_all_settled(session: AsyncSession) -> None:
    # No active row: fall back to the newest (highest id) settled row.
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=103, media_type="movie", title="Gone", status="available")
    await repo.create(tmdb_id=103, media_type="movie", title="Evicted later", status="evicted")
    statuses = await repo.display_statuses_by_tmdb_ids([(103, "movie")])
    assert statuses == {(103, "movie"): "evicted"}


async def test_display_statuses_omits_keys_without_rows(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=104, media_type="movie", title="Has row", status="pending")
    statuses = await repo.display_statuses_by_tmdb_ids([(104, "movie"), (999, "movie")])
    assert statuses == {(104, "movie"): "pending"}


async def test_display_statuses_does_not_conflate_movie_and_tv(session: AsyncSession) -> None:
    # Same tmdb_id under both namespaces: each key gets ITS OWN status, never bled across.
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=105, media_type="movie", title="Movie", status="downloading")
    await repo.create(tmdb_id=105, media_type="tv", title="Show", status="pending")
    statuses = await repo.display_statuses_by_tmdb_ids([(105, "movie"), (105, "tv")])
    assert statuses == {(105, "movie"): "downloading", (105, "tv"): "pending"}


async def test_display_statuses_returns_tv_parent_rollup(session: AsyncSession) -> None:
    # For TV the parent MediaRequest.status carries the persisted per-season rollup,
    # so partially_available is returned directly -- no per-season fan-out needed.
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=106, media_type="tv", title="Partial", status="partially_available")
    statuses = await repo.display_statuses_by_tmdb_ids([(106, "tv")])
    assert statuses == {(106, "tv"): "partially_available"}


async def test_display_statuses_empty_input_returns_empty(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    assert await repo.display_statuses_by_tmdb_ids([]) == {}


async def test_latest_request_evicted_reflects_the_newest_row(session: AsyncSession) -> None:
    """``latest_request_evicted`` is the in-library short-circuit's stale-Plex guard
    (ADR-0012): it reports whether the NEWEST request row for this media is
    ``evicted``. Keyed on the newest id so a movie re-downloaded after an earlier
    eviction (a later ``available`` row) is never falsely suppressed."""
    repo = SqlRequestRepository(session)

    # No rows at all -> not evicted.
    assert await repo.latest_request_evicted(700, "movie") is False

    # A lone evicted row -> True.
    await repo.create(tmdb_id=700, media_type="movie", title="Gone", status="evicted")
    assert await repo.latest_request_evicted(700, "movie") is True

    # A NEWER available row for the same media (a legitimate re-download) -> False:
    # the eviction is no longer the most recent history for this title.
    await repo.create(tmdb_id=700, media_type="movie", title="Gone", status="available")
    assert await repo.latest_request_evicted(700, "movie") is False

    # Scoped to the (tmdb_id, media_type) namespace: a tv row with the same tmdb_id
    # does not bleed into the movie answer.
    await repo.create(tmdb_id=700, media_type="tv", title="Gone Show", status="evicted")
    assert await repo.latest_request_evicted(700, "movie") is False
    assert await repo.latest_request_evicted(700, "tv") is True


async def test_latest_request_evicted_ignores_cancelled_rows(session: AsyncSession) -> None:
    """An in-window re-grab the user then CANCELLED must not reset the eviction
    stale-Plex guard: a cancellation says nothing about on-disk truth, so the
    guard keys on the newest NON-cancelled row. Without this, evicted -> re-grab
    (pending) -> cancel would let the NEXT re-request mint 'available' over the
    file the sweep is still deleting."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=701, media_type="movie", title="Doomed", status="evicted")
    assert await repo.latest_request_evicted(701, "movie") is True

    # The in-window re-grab, cancelled by the user: newest row is now 'cancelled',
    # but the newest NON-cancelled row is still the eviction.
    await repo.create(tmdb_id=701, media_type="movie", title="Doomed", status="cancelled")
    assert await repo.latest_request_evicted(701, "movie") is True

    # A media whose ONLY row is cancelled has no eviction history -> False.
    await repo.create(tmdb_id=702, media_type="movie", title="Other", status="cancelled")
    assert await repo.latest_request_evicted(702, "movie") is False


async def test_clear_library_path_if_set_is_a_single_winner_gate(session: AsyncSession) -> None:
    """The guarded breadcrumb clear returns True exactly once -- the eviction
    finalize's single-winner gate: only the pass that actually cleared it writes
    the history row, so two racing resume/finalize passes never double-record."""
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=703, media_type="movie", title="Gone", status="evicted")
    await repo.set_library_path(created.id, "/media/movies/Gone.mkv")

    assert await repo.clear_library_path_if_set(created.id) is True
    assert await repo.clear_library_path_if_set(created.id) is False  # already cleared
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.library_path is None


# --- request-dedup healing: new repo methods -----------------------------------


async def test_list_false_available_movies_signature_and_bound(session: AsyncSession) -> None:
    """Returns only movie + available + NULL-``library_path`` +
    NULL-``available_heal_verified_at`` rows, id-ascending, respecting ``limit``;
    excludes a non-movie, a non-available, a non-NULL-path row, and a row the
    heal pass already converged (P1 regression: without this last exclusion a
    genuinely-present row would be re-scanned every tick forever)."""
    repo = SqlRequestRepository(session)
    qualifying_1 = await repo.create(tmdb_id=10, media_type="movie", title="Q1", status="available")
    qualifying_2 = await repo.create(tmdb_id=11, media_type="movie", title="Q2", status="available")
    # Excluded: a TV parent's 'available' + NULL path is the normal rollup shape.
    await repo.create(tmdb_id=12, media_type="tv", title="Show", status="available")
    # Excluded: not 'available'.
    await repo.create(tmdb_id=13, media_type="movie", title="Pending", status="pending")
    # Excluded: 'available' but carries a real library_path.
    with_path = await repo.create(
        tmdb_id=14, media_type="movie", title="WithPath", status="available"
    )
    await repo.set_library_path(with_path.id, "/movies/x")
    # Excluded: the heal pass already live-reconfirmed this row present and
    # stamped it -- it must never re-enter the scan population.
    converged = await repo.create(
        tmdb_id=15, media_type="movie", title="Converged", status="available"
    )
    assert await repo.mark_heal_verified_present(converged.id) is True

    rows = await repo.list_false_available_movies(limit=25)
    assert [r.id for r in rows] == [qualifying_1.id, qualifying_2.id]

    bounded = await repo.list_false_available_movies(limit=1)
    assert [r.id for r in bounded] == [qualifying_1.id]


async def test_mark_heal_verified_present_cas(session: AsyncSession) -> None:
    """Succeeds on the exact false-claim signature (available + NULL path),
    stamping BOTH ``available_heal_verified_at`` and ``library_verified_at``; a
    second call no-ops (returns False) since it already left that signature by
    virtue of the FIRST stamp -- convergence is a one-way door."""
    repo = SqlRequestRepository(session)
    row = await repo.create(tmdb_id=22, media_type="movie", title="Heal", status="available")

    changed = await repo.mark_heal_verified_present(row.id)
    assert changed is True
    healed = await repo.get(row.id)
    assert healed is not None
    assert healed.status == "available"
    assert healed.library_path is None
    healed_orm = await session.get(MediaRequest, row.id)
    assert healed_orm is not None
    assert healed_orm.library_verified_at is not None
    assert healed_orm.available_heal_verified_at is not None

    # Convergence is a one-way door: the row already left the exact false-claim
    # signature the FIRST stamp created, so a second call no-ops.
    assert await repo.mark_heal_verified_present(row.id) is False

    # A row that carries a real library_path is never the false-claim signature.
    with_path = await repo.create(
        tmdb_id=23, media_type="movie", title="WithPath", status="available"
    )
    await repo.set_library_path(with_path.id, "/movies/z")
    assert await repo.mark_heal_verified_present(with_path.id) is False


async def test_rearm_false_available_to_pending_cas(session: AsyncSession) -> None:
    """Succeeds on the exact false-claim signature (available + NULL path);
    no-ops (returns False) once the row has already left that exact signature."""
    repo = SqlRequestRepository(session)
    row = await repo.create(tmdb_id=20, media_type="movie", title="Heal", status="available")
    await repo.mark_available(row.id)  # stamp library_verified_at/completed_at, as the
    # real already-in-library short-circuit does.

    changed = await repo.rearm_false_available_to_pending(row.id)
    assert changed is True
    healed = await repo.get(row.id)
    assert healed is not None
    assert healed.status == "pending"
    assert healed.completed_at is None
    assert healed.search_attempts == 0
    assert healed.next_search_at is None
    assert healed.library_path is None

    # Already left 'available' -> no-op.
    assert await repo.rearm_false_available_to_pending(row.id) is False

    # A row that carries a real library_path is never the false-claim signature.
    with_path = await repo.create(
        tmdb_id=21, media_type="movie", title="WithPath", status="available"
    )
    await repo.set_library_path(with_path.id, "/movies/y")
    assert await repo.rearm_false_available_to_pending(with_path.id) is False


async def test_delete_false_available_sibling_collapse_cas(session: AsyncSession) -> None:
    """Succeeds ONLY when the row's ownership at delete time still matches the
    caller's snapshot -- guards the window between the heal pass's
    top-of-cycle candidate read and this delete, in which a concurrent user
    create can claim (adopt) an ownerless false-claim row out from under it."""
    repo = SqlRequestRepository(session)
    row = await repo.create(tmdb_id=24, media_type="movie", title="Loser", status="available")

    # Ownership changed since the (implied) candidate read: expecting ownerless
    # but the row now belongs to a real user -- the CAS must refuse.
    user = User(username="claimer", permissions=0)
    session.add(user)
    await session.flush()
    orm_row = await session.get(MediaRequest, row.id)
    assert orm_row is not None
    orm_row.user_id = user.id
    await session.flush()
    assert (
        await repo.delete_false_available_sibling_collapse(row.id, expected_user_id=None) is False
    )
    survivor = await repo.get(row.id)
    assert survivor is not None  # NOT deleted
    assert survivor.user_id == user.id

    # Ownership UNCHANGED (still that same user) -- the CAS succeeds.
    assert (
        await repo.delete_false_available_sibling_collapse(row.id, expected_user_id=user.id) is True
    )
    assert await repo.get(row.id) is None

    # A genuinely ownerless row deletes when expected_user_id is None.
    ownerless = await repo.create(
        tmdb_id=25, media_type="movie", title="Ownerless", status="available"
    )
    assert (
        await repo.delete_false_available_sibling_collapse(ownerless.id, expected_user_id=None)
        is True
    )
    assert await repo.get(ownerless.id) is None

    # A row that carries a real library_path is never the false-claim signature.
    with_path = await repo.create(
        tmdb_id=26, media_type="movie", title="WithPath", status="available"
    )
    await repo.set_library_path(with_path.id, "/movies/w")
    assert (
        await repo.delete_false_available_sibling_collapse(with_path.id, expected_user_id=None)
        is False
    )
    assert await repo.get(with_path.id) is not None


async def test_latest_library_path_returns_newest_breadcrumb(session: AsyncSession) -> None:
    """Across several rows for the same media, returns the NEWEST non-NULL
    ``library_path``; ``None`` when none carry one."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=30, media_type="movie", title="NoPath", status="pending")
    assert await repo.latest_library_path(30, "movie") is None

    older = await repo.create(tmdb_id=31, media_type="movie", title="Older", status="failed")
    await repo.set_library_path(older.id, "/movies/older")
    newer = await repo.create(tmdb_id=31, media_type="movie", title="Newer", status="cancelled")
    await repo.set_library_path(newer.id, "/movies/newer")

    assert await repo.latest_library_path(31, "movie") == "/movies/newer"
    # Scoped to (tmdb_id, media_type): a tv row sharing the tmdb id never bleeds in.
    assert await repo.latest_library_path(31, "tv") is None
