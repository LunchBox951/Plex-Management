"""``POST /api/v1/ops/evict`` (ADR-0012, Component 3) — the manual,
operator-triggered pressure sweep: north-star #1's "free space on demand"
button, and the ``evicted`` status it produces.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.library import WatchState
from plex_manager.services import eviction_service
from plex_manager.web.deps import SettingsStore
from plex_manager.web.events import get_event_hub
from tests.web.fakes import FakeLibrary, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "ops-evict-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_TMDB_ID = 5150
_STALE = datetime.now(UTC) - timedelta(days=45)


async def _seed(
    sm: SessionMaker,
    *,
    movies_root: str,
    library_path: str,
    keep_forever: bool = False,
    eviction_enabled: str = "true",
) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="Stale Movie",
            status=RequestStatus.available,
            library_path=library_path,
            keep_forever=keep_forever,
        )
        session.add(request)
        await session.flush()
        request_id = request.id

        store = SettingsStore(session)
        await store.set("movies_root", movies_root)
        await store.set("eviction_enabled", eviction_enabled)
        # threshold=0 always trips (real disk usage is never negative); target=0
        # asks the sweep to evict every eligible candidate.
        await store.set("disk_pressure_threshold_percent", "0")
        await store.set("disk_pressure_target_percent", "0")
        await store.set("eviction_grace_days", "30")
        await session.commit()
    return request_id


async def test_evict_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    assert (await client.post("/api/v1/ops/evict")).status_code == 401


async def test_evict_requires_plex_configured(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 409
    assert response.json() == {"detail": "service_not_configured", "service": "plex"}


async def test_evict_frees_space_and_flips_status_to_evicted(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["evicted"]) == 1
    outcome = body["evicted"][0]
    assert outcome["request_id"] == request_id
    assert outcome["title"] == "Stale Movie"
    assert outcome["media_type"] == "movie"

    # The file is actually gone and the request is honestly, re-requestably
    # marked `evicted` (never a silent delete, never a terminal dead-end).
    assert not movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted


async def test_evict_publishes_realtime_event_for_other_connected_clients(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))
    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)
    subscription = get_event_hub(app).subscribe()
    _ = await subscription.get()

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)

    assert response.status_code == 200
    event = await subscription.get()
    assert event.topics == ("requests", "discover", "ops:disk", "ops:health")
    assert event.reason == "eviction"
    subscription.close()


async def test_evict_still_runs_when_the_automatic_switch_is_disabled(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # `eviction_enabled=false` gates the AUTOMATIC periodic sweep only -- an
    # operator who disabled the background loop must still be able to free
    # space on demand via this explicit, manual button.
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _seed(
        sessionmaker_,
        movies_root=str(tmp_path),
        library_path=str(movie_file),
        eviction_enabled="false",
    )
    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    assert len(response.json()["evicted"]) == 1
    assert not movie_file.exists()


async def test_evict_invalidates_the_cached_disk_preview(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # Regression guard: GET /disk's preview is TTL-cached (~15s, see
    # ``_get_disk_preview_cache``). Without invalidating it here, a poll
    # immediately after this manual sweep would keep serving the pre-eviction
    # snapshot -- the just-deleted title still listed as an evictable
    # candidate, and the stale (lower) free-space gauge -- for up to that
    # whole TTL, right after the operator clicked the very button meant to
    # correct it.
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    # Populate the cache with the pre-eviction snapshot.
    before = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert before.status_code == 200
    before_root = before.json()["roots"][0]
    assert len(before_root["candidates"]) == 1
    assert before_root["candidates"][0]["title"] == "Stale Movie"

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    assert len(response.json()["evicted"]) == 1
    assert not movie_file.exists()

    # A poll immediately after -- well within the ~15s TTL -- must reflect the
    # sweep (the just-deleted title dropped from the candidate list), never
    # the cached pre-eviction snapshot the reviewer flagged.
    after = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert after.status_code == 200
    after_root = after.json()["roots"][0]
    assert after_root["candidates"] == []


async def test_evict_never_touches_a_pinned_keep_forever_title(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _seed(
        sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file), keep_forever=True
    )
    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["evicted"] == []
    assert movie_file.exists()


async def test_evict_sweeps_a_configured_anime_movie_root(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """ADR-0015: an anime title's ``library_path`` lives under
    ``anime_movie_root``, which must be BOTH enumerated by the pressure sweep
    (``eviction_service._owned_by_root`` only considers enumerated roots) AND
    included in the delete-guard's allowlist -- otherwise the anime root is
    silently never a pressure-eviction candidate."""
    await seed(initialized=True, app_api_key=_API_KEY)
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    movie_file = anime_root / "Stale Anime Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="Stale Anime Movie",
            status=RequestStatus.available,
            library_path=str(movie_file),
            is_anime=True,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        store = SettingsStore(session)
        await store.set("anime_movie_root", str(anime_root))
        await store.set("eviction_enabled", "true")
        await store.set("disk_pressure_threshold_percent", "0")
        await store.set("disk_pressure_target_percent", "0")
        await store.set("eviction_grace_days", "30")
        await session.commit()

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["evicted"]) == 1
    assert body["evicted"][0]["request_id"] == request_id
    assert not movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted


_TV_TMDB_ID = 6161


class _RaisesForTvWatchState(FakeLibrary):
    """A :class:`FakeLibrary` whose ``watch_state`` raises ``PlexLibraryError``
    for TV -- simulating a transient failure resolving TV watch state during
    the TV root's candidate assembly -- while movie watch-state lookups still
    succeed normally (so the movies root sweep is unaffected)."""

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: str,
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        if media_type == "tv":
            raise PlexLibraryError("simulated Plex outage resolving TV watch state")
        movie_type = cast(Literal["movie", "tv"], media_type)
        return await super().watch_state(
            tmdb_id, movie_type, season=season, library_path=library_path
        )


async def test_evict_one_roots_failure_does_not_hide_another_roots_evictions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """R6-C regression: the movies root evicts and commits FIRST; the tv root
    then raises assembling its own candidates (a transient Plex error). The
    endpoint must still return 200 with the movie outcome in ``evicted`` AND
    the tv failure surfaced in ``errors`` -- never a bare 500 that hides the
    movie root's already-committed eviction -- and the disk-preview cache must
    still be cleared so a following ``GET /disk`` is fresh."""
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    tv_root = tmp_path / "tv"
    tv_root.mkdir()

    movie_file = movies_root / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(
        sessionmaker_, movies_root=str(movies_root), library_path=str(movie_file)
    )

    season_file = tv_root / "Some Show" / "Season 01"
    season_file.mkdir(parents=True)
    (season_file / "episode.mkv").write_bytes(b"0" * 1024)
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=_TV_TMDB_ID,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id,
                season_number=1,
                status=RequestStatus.available,
                library_path=str(season_file),
            )
        )
        store = SettingsStore(session)
        await store.set("tv_root", str(tv_root))
        await session.commit()

    library = _RaisesForTvWatchState(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    # Warm the disk-preview cache first, so we can prove it was cleared below.
    before = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert before.status_code == 200

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()

    # The movies root's eviction is still reported -- the tv root's failure
    # never hid it.
    assert len(body["evicted"]) == 1
    assert body["evicted"][0]["request_id"] == request_id
    assert not movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted

    # The tv root's failure is surfaced, not swallowed.
    assert body["errors"] == [{"root": "tv_root", "detail": "sweep failed (PlexLibraryError)"}]
    # The tv season was never touched -- the failure happened assembling
    # candidates, before any delete was attempted.
    assert (season_file / "episode.mkv").exists()

    # cache.clear() was still reached despite the tv root's exception -- a
    # following GET /disk is fresh, not the pre-sweep snapshot.
    after = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert after.status_code == 200
    movies_after = next(r for r in after.json()["roots"] if r["root"] == "movies_root")
    assert movies_after["candidates"] == []


async def test_evict_one_roots_db_failure_is_rolled_back_so_the_next_root_runs(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#95: every root shares this request's single session. A DB/SQLAlchemy
    failure sweeping one root poisons that transaction; without the guarded
    ``session.rollback()`` in the per-root except, the NEXT root's sweep would
    raise ``PendingRollbackError`` too, cascading one root's failure into every
    later root. Proves the movies root's DB failure is rolled back so the tv root
    still runs on the same, now-clean session."""
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    await _seed(
        sessionmaker_,
        movies_root=str(movies_root),
        library_path=str(movies_root / "Stale Movie.mkv"),
    )
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("tv_root", str(tv_root))
        await session.commit()

    override_adapters(app, library=FakeLibrary())

    swept: list[str] = []

    async def _fake_sweep(**kwargs: object) -> list[object]:
        session = kwargs["session"]
        assert isinstance(session, AsyncSession)
        root = kwargs["root_path"]
        if kwargs["media_type"] == "movie" and root == str(movies_root):
            # A DB error mid-sweep poisons the shared transaction, then propagates.
            await session.execute(text("SELECT * FROM __ops_evict_no_such_table__"))
            return []  # pragma: no cover - the statement above always raises
        # A later root: the shared session must be usable again (rolled back).
        await session.execute(text("SELECT 1"))
        assert isinstance(root, str)
        swept.append(root)
        return []

    monkeypatch.setattr(eviction_service, "run_eviction_sweep", _fake_sweep)

    response = await client.post("/api/v1/ops/evict", headers=_HEADERS)
    # Partial completion is a first-class 200, never a bare 500 that hides it.
    assert response.status_code == 200
    body = response.json()
    # The movies root's DB failure is surfaced, not swallowed...
    assert body["errors"] == [{"root": "movies_root", "detail": "sweep failed (OperationalError)"}]
    # ...and the tv root still ran on the rolled-back, clean session.
    assert swept == [str(tv_root)]
