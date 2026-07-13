"""Search-preview — the headline endpoint: good accepted, CAM/TS rejected."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from fastapi import FastAPI

from plex_manager.ports.metadata import TvMetadata
from plex_manager.services.update_coordination_service import UpdateCoordinationService
from tests.web.fakes import (
    FakeProwlarr,
    FakeTmdb,
    candidate,
    good_and_cam_candidates,
    override_adapters,
    prerelease_only_candidates,
)

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "preview-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_DESCRIPTOR = {
    "tmdb_id": 603,
    "media_type": "movie",
    "title": "Some Movie",
    "year": 2020,
}


async def test_good_accepted_cam_and_ts_rejected(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(good_and_cam_candidates()))

    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR, headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()

    assert body["no_acceptable_release"] is False
    assert [r["quality_name"] for r in body["accepted"]] == ["WEBDL-1080p"]
    accepted = body["accepted"][0]
    assert accepted["source"] == "WEBDL"
    assert accepted["resolution"] == "1080p"
    assert accepted["info_hash"] == "3" * 40

    rejected_titles = {r["title"] for r in body["rejected"]}
    assert "Some.Movie.2020.CAM.x264-GROUP" in rejected_titles
    assert "Some.Movie.2020.HDTS.x264-GROUP" in rejected_titles
    assert all(r["reason"] == "quality_not_wanted" for r in body["rejected"])


async def test_read_only_preview_remains_available_during_update_drain(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(good_and_cam_candidates()))
    coordinator = UpdateCoordinationService(app.state.sessionmaker)
    await coordinator.initialize()
    app.state.update_coordinator = coordinator
    claim = await coordinator.claim_drain(ttl=timedelta(minutes=1))
    assert claim is not None and claim.ready

    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR, headers=_HEADERS)

    assert response.status_code == 200
    assert (await coordinator.snapshot()).active_critical_operations == 0


async def test_all_prerelease_yields_no_acceptable_release(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(prerelease_only_candidates()))

    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR, headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == []
    assert body["no_acceptable_release"] is True
    assert len(body["rejected"]) == 2


def test_search_preview_contract_documents_manual_error_bodies(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/search-preview"]["post"]["responses"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )
    schema = responses["422"]["content"]["application/json"]["schema"]
    assert {"$ref": "#/components/schemas/ErrorDetail"} in schema["anyOf"]


async def test_search_preview_requires_api_key(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(good_and_cam_candidates()))
    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR)
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Issue #101: search preview must validate media scope BEFORE running
# decisions -- mirrors the grab endpoint's exact tv_grab_requires_season /
# movie_grab_rejects_season guard, and the indexer must never be queried for
# an invalid combination.
# --------------------------------------------------------------------------- #


async def test_search_preview_tv_without_season_rejected_422_and_never_searches(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    override_adapters(app, prowlarr=prowlarr)

    response = await client.post(
        "/api/v1/search-preview",
        json={"tmdb_id": 900, "media_type": "tv", "title": "Some Show", "year": 2020},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "tv_grab_requires_season"
    assert prowlarr.searched == []  # the indexer was never queried


async def test_search_preview_movie_with_season_rejected_422_and_never_searches(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    override_adapters(app, prowlarr=prowlarr)

    response = await client.post(
        "/api/v1/search-preview",
        json={**_DESCRIPTOR, "season": 1},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "movie_grab_rejects_season"
    assert prowlarr.searched == []


async def test_search_preview_movie_with_episodes_rejected_422_and_never_searches(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Even without a ``season``, a non-tv preview carrying ``episodes`` is
    still an incoherent scope and must be rejected, not silently searched."""
    await seed(initialized=True, app_api_key=_API_KEY)
    prowlarr = FakeProwlarr(good_and_cam_candidates())
    override_adapters(app, prowlarr=prowlarr)

    response = await client.post(
        "/api/v1/search-preview",
        json={**_DESCRIPTOR, "episodes": [3]},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "movie_grab_rejects_season"
    assert prowlarr.searched == []


async def test_search_preview_tv_with_season_still_previews_normally(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The guard only rejects the invalid combinations -- a properly-scoped tv
    preview (season set) still runs the decision engine as before."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(good_and_cam_candidates()))

    response = await client.post(
        "/api/v1/search-preview",
        json={"tmdb_id": 900, "media_type": "tv", "title": "Some Show", "year": 2020, "season": 2},
        headers=_HEADERS,
    )
    # Properly scoped -- the guard does not reject it; it runs the decision
    # engine as before (the fixture's movie-titled candidates don't match a tv
    # season, so nothing is accepted here, but that is unrelated to the guard).
    assert response.status_code == 200


async def test_search_preview_request_id_omitted_episodes_inherits_explicit_episode_intent(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            shows={900: TvMetadata(tmdb_id=900, title="Some Show", year=2020, season_count=2)}
        ),
    )
    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 900, "media_type": "tv", "episodes": {"2": [5]}},
        headers=_HEADERS,
    )
    assert created.status_code == 201

    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S02E05.1080p.WEB-DL.x264-GROUP", info_hash="5" * 40)]
    )
    override_adapters(app, prowlarr=prowlarr)

    response = await client.post(
        "/api/v1/search-preview",
        json={"request_id": created.json()["id"], "season": 2},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert prowlarr.searched[-1].season == 2
    assert prowlarr.searched[-1].episode == "5"


async def test_search_preview_empty_episodes_normalizes_and_still_previews(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Issue #102: a hand-crafted ``episodes: []`` for a tv preview normalizes
    to ``None`` (whole-season) at the schema boundary rather than tripping the
    scope guard (which only rejects a non-tv preview carrying episodes)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=FakeProwlarr(good_and_cam_candidates()))

    response = await client.post(
        "/api/v1/search-preview",
        json={
            "tmdb_id": 900,
            "media_type": "tv",
            "title": "Some Show",
            "year": 2020,
            "season": 2,
            "episodes": [],
        },
        headers=_HEADERS,
    )
    assert response.status_code == 200
