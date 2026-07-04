"""Blocklist — list scoping and operator delete (un-blocklist)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import Blocklist, BlocklistReason, MediaType

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "blocklist-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def _insert(
    sm: SessionMaker, *, source_title: str, tmdb_id: int, media_type: MediaType | None = None
) -> int:
    async with sm() as session:
        row = Blocklist(
            source_title=source_title,
            reason=BlocklistReason.failed,
            tmdb_id=tmdb_id,
            torrent_hash=None,
            media_type=media_type,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_list_and_scope_by_tmdb_id(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert(sessionmaker_, source_title="A", tmdb_id=1)
    await _insert(sessionmaker_, source_title="B", tmdb_id=2)

    all_entries = (await client.get("/api/v1/blocklist", headers=_HEADERS)).json()["entries"]
    assert len(all_entries) == 2

    scoped = (
        await client.get("/api/v1/blocklist", params={"tmdb_id": 1}, headers=_HEADERS)
    ).json()["entries"]
    assert [e["source_title"] for e in scoped] == ["A"]


async def test_list_can_scope_by_tmdb_id_and_media_type(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert(sessionmaker_, source_title="Movie", tmdb_id=424242, media_type=MediaType.movie)
    await _insert(sessionmaker_, source_title="Show", tmdb_id=424242, media_type=MediaType.tv)

    scoped = (
        await client.get(
            "/api/v1/blocklist",
            params={"tmdb_id": 424242, "media_type": "tv"},
            headers=_HEADERS,
        )
    ).json()["entries"]
    assert [e["source_title"] for e in scoped] == ["Show"]


async def test_delete_removes_entry_then_404(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    entry_id = await _insert(sessionmaker_, source_title="A", tmdb_id=1)

    deleted = await client.delete(f"/api/v1/blocklist/{entry_id}", headers=_HEADERS)
    assert deleted.status_code == 204

    missing = await client.delete(f"/api/v1/blocklist/{entry_id}", headers=_HEADERS)
    assert missing.status_code == 404


def test_delete_contract_documents_not_found(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/blocklist/{blocklist_id}"]["delete"]["responses"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )


async def test_blocklist_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    assert (await client.get("/api/v1/blocklist")).status_code == 401
