"""Quality-profile — the read-only default with the WEBDL-1080p cutoff."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "qp-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def test_returns_default_profile_with_webdl1080p_cutoff(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/quality-profile", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()

    assert body["name"] == "Default"
    assert body["cutoff_name"] == "WEBDL-1080p"

    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["WEBDL-1080p"]["allowed"] is True
    assert by_name["CAM"]["allowed"] is False
    assert by_name["TELESYNC"]["allowed"] is False
    # Ordered low -> high by weight: SDTV precedes WEBDL-1080p precedes Remux-2160p.
    names = [item["name"] for item in body["items"]]
    assert names.index("SDTV") < names.index("WEBDL-1080p") < names.index("Remux-2160p")


async def test_quality_profile_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/quality-profile")
    assert response.status_code == 401
