from __future__ import annotations

import httpx
import pytest

from plex_manager.adapters.plex.watchlist import (
    PlexWatchlist,
    PlexWatchlistAuthError,
    PlexWatchlistError,
)
from plex_manager.ports.watchlist import WatchlistEntry

TOKEN = "watchlist-test-token"  # noqa: S105


async def test_lists_movie_and_show_tmdb_guids_with_header_token() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 3,
                    "Metadata": [
                        {"type": "movie", "Guid": [{"id": "tmdb://603"}]},
                        {"type": "show", "Guid": [{"id": "themoviedb://1396"}]},
                        {"type": "clip", "Guid": [{"id": "tmdb://1"}]},
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()

    assert entries == (
        WatchlistEntry(tmdb_id=603, media_type="movie"),
        WatchlistEntry(tmdb_id=1396, media_type="tv"),
    )
    assert seen[0].headers["X-Plex-Token"] == TOKEN
    assert TOKEN not in str(seen[0].url)
    assert seen[0].url.host == "discover.provider.plex.tv"


async def test_rating_key_only_page_is_resolved_via_item_details() -> None:
    """Regression: the watchlist page often returns rows carrying only a
    ``ratingKey`` (no ``type``/``Guid``). Treating those as "nothing here"
    would let a first sync REPLACE the stored snapshot with an empty one --
    silently dropping eviction protection and creating no requests. Each such
    row must be resolved via its ``/library/metadata/{ratingKey}`` detail
    BEFORE being filtered out, so a full watchlist stays a full watchlist."""
    details: dict[str, dict[str, object]] = {
        "1111": {"ratingKey": "1111", "type": "movie", "Guid": [{"id": "tmdb://603"}]},
        "2222": {"ratingKey": "2222", "type": "show", "Guid": [{"id": "tmdb://1396"}]},
    }
    fetched_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/watchlist/all":
            # ratingKey-only rows, exactly as Plex commonly returns them.
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "totalSize": 2,
                        "Metadata": [{"ratingKey": "1111"}, {"ratingKey": "2222"}],
                    }
                },
            )
        rating_key = request.url.path.rsplit("/", 1)[-1]
        fetched_keys.append(rating_key)
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [details[rating_key]]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()

    assert entries == (
        WatchlistEntry(tmdb_id=603, media_type="movie"),
        WatchlistEntry(tmdb_id=1396, media_type="tv"),
    )
    assert sorted(fetched_keys) == ["1111", "2222"]


async def test_rating_key_detail_fetch_failure_raises_not_empties() -> None:
    """A failed detail fetch must RAISE (so the caller keeps its last snapshot),
    never be swallowed into an empty watchlist."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/watchlist/all":
            return httpx.Response(
                200,
                json={"MediaContainer": {"totalSize": 1, "Metadata": [{"ratingKey": "1111"}]}},
            )
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_malformed_detail_metadata_raises_not_skips() -> None:
    """A ratingKey-only row whose detail fetch returns a malformed (non-list)
    ``Metadata`` must RAISE -- coercing it to an empty tuple would silently drop
    the title as "unsupported" instead of retaining the caller's last snapshot,
    the inverse of the top-level list fetch's fail-fatal posture (#296)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/watchlist/all":
            return httpx.Response(
                200,
                json={"MediaContainer": {"totalSize": 1, "Metadata": [{"ratingKey": "1111"}]}},
            )
        # Detail endpoint answers 200 but with a broken (dict, not list) Metadata.
        return httpx.Response(200, json={"MediaContainer": {"Metadata": {"ratingKey": "1111"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_empty_detail_metadata_list_is_skipped_not_raised() -> None:
    """An explicitly EMPTY detail ``Metadata`` list is a genuine "no results"
    for the key -- the item is skipped, not treated as a fetch failure (#296)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/watchlist/all":
            return httpx.Response(
                200,
                json={"MediaContainer": {"totalSize": 1, "Metadata": [{"ratingKey": "1111"}]}},
            )
        return httpx.Response(200, json={"MediaContainer": {"size": 0, "Metadata": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await PlexWatchlist(client, TOKEN).list_entries() == ()


async def test_absent_detail_metadata_is_skipped_not_raised() -> None:
    """A detail response that OMITS ``Metadata`` entirely (a 200 with size 0 for
    a deleted item) is a genuine "no results", skipped rather than raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/watchlist/all":
            return httpx.Response(
                200,
                json={"MediaContainer": {"totalSize": 1, "Metadata": [{"ratingKey": "1111"}]}},
            )
        return httpx.Response(200, json={"MediaContainer": {"size": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await PlexWatchlist(client, TOKEN).list_entries() == ()


async def test_unsupported_typed_row_is_skipped_without_detail_fetch() -> None:
    """A row that already declares an unsupported ``type`` (e.g. an episode)
    cannot become a movie/show via its detail, so it is skipped WITHOUT a wasted
    metadata round trip -- only the ``ratingKey``-only rows need resolving."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 1,
                    "Metadata": [
                        {"type": "episode", "ratingKey": "1111", "Guid": [{"id": "tmdb://5"}]}
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await PlexWatchlist(client, TOKEN).list_entries() == ()
    assert paths == ["/library/sections/watchlist/all"]  # no /library/metadata fetch


async def test_auth_failure_is_typed() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistAuthError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_invalid_json_is_typed() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="not-json"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_paginates_until_total_size() -> None:
    starts: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["X-Plex-Container-Start"])
        starts.append(start)
        count = 100 if start == 0 else 1
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 101,
                    "Metadata": [
                        {"type": "movie", "Guid": [{"id": f"tmdb://{start + i + 1}"}]}
                        for i in range(count)
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()
    assert len(entries) == 101
    assert starts == [0, 100]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"MediaContainer": {}},
        {"MediaContainer": {"totalSize": 0, "Metadata": {}}},
    ],
)
async def test_rejects_malformed_container_shapes(payload: object) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_rejects_empty_page_before_declared_total() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"MediaContainer": {"totalSize": 1, "Metadata": []}}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_accepts_empty_watchlist_when_metadata_is_omitted() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"MediaContainer": {"totalSize": 0}})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        assert await PlexWatchlist(client, TOKEN).list_entries() == ()


async def test_short_page_continues_until_declared_total() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["X-Plex-Container-Start"])
        starts.append(start)
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [{"type": "movie", "Guid": [{"id": f"tmdb://{start + 1}"}]}],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()
    assert len(entries) == 2
    assert starts == [0, 1]
