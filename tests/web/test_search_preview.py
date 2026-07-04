"""Search-preview — the headline endpoint: good accepted, CAM/TS rejected."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

from tests.web.fakes import (
    FakeProwlarr,
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
