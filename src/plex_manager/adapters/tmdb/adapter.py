"""TmdbMetadata — the :class:`MetadataPort` implementation backed by TMDB v3.

Deliberately thin (ADR scope): the alpha only needs free-text search plus
movie / TV resolution by tmdb id. The adapter maps TMDB's JSON into the frozen
domain DTOs (``MediaSearchResult`` / ``MovieMetadata`` / ``TvMetadata``) and never
leaks TMDB's wire shape past this module.

Construction is dependency-injected: ``base_url``, ``api_key`` and an
``httpx.AsyncClient`` are passed in (the web/services layer wires decrypted creds
later). The api key is sent as the ``api_key`` query parameter and is NEVER
logged.

Caching: a tiny in-process TTL cache (no new dependency) avoids hammering TMDB
for repeated lookups during a search/grab flow.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final, cast

import httpx

from plex_manager.logsafe import safe_int
from plex_manager.ports.metadata import (
    EpisodeInfo,
    MediaKind,
    MediaPage,
    MediaSearchResult,
    MovieMetadata,
    TvMetadata,
)

__all__ = ["TmdbApiError", "TmdbAuthError", "TmdbMetadata"]

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL: Final = "https://api.themoviedb.org/3"
_IMAGE_BASE_URL: Final = "https://image.tmdb.org/t/p/w500"
_BACKDROP_BASE_URL: Final = "https://image.tmdb.org/t/p/w780"
_ANIME_KEYWORD_ID: Final = 210024
_DEFAULT_TTL_SECONDS: Final = 3600.0
_APPEND: Final = "external_ids,keywords"
_HTTP_OK: Final = 200
_HTTP_MULTIPLE_CHOICES: Final = 300
_HTTP_NOT_FOUND: Final = 404
_HTTP_UNAUTHORIZED: Final = 401
_MIN_PAGE: Final = 1
_MAX_PAGE: Final = 500


class TmdbApiError(RuntimeError):
    """Raised when TMDB returns a non-2xx status other than 401/404.

    A surfaced, retryable error (e.g. 429 rate-limit, 5xx). The message names the
    request *path* and status code only — never the full URL, which carries the
    api key in its query string. Letting httpx's ``HTTPStatusError`` escape here
    would leak the key into any upstream log, so it is converted at the boundary.
    """


class TmdbAuthError(RuntimeError):
    """Raised when TMDB rejects the api key (HTTP 401).

    A clear, surfaced error — never a silent empty result. The message names the
    cause but never includes the api key.
    """


class _TtlCache[V]:
    """A minimal monotonic-clock TTL cache (hit-on-fresh, evict-on-expired).

    Only successful results are stored; misses (``None``) are not cached, so the
    sentinel ambiguity between "absent" and "cached None" never arises.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, V]] = {}

    def get(self, key: str) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: V) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow an untyped JSON node to a string-keyed mapping (else empty)."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return {}


def _as_sequence(value: object) -> Sequence[object]:
    """Narrow an untyped JSON node to a sequence (str is not a sequence here)."""
    if isinstance(value, (list, tuple)):
        return cast("Sequence[object]", value)
    return ()


def _get_int(fields: Mapping[str, object], key: str) -> int | None:
    value = fields.get(key)
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, int):
        return value
    return None


def _get_str(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _year_from_date(fields: Mapping[str, object], key: str) -> int | None:
    """Extract the leading year from a ``YYYY-MM-DD`` TMDB date string."""
    date_str = _get_str(fields, key)
    if date_str is None or len(date_str) < 4:
        return None
    head = date_str[:4]
    return int(head) if head.isdigit() else None


def _date_from(fields: Mapping[str, object], key: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` TMDB date string; ``None``/``""``/malformed -> None.

    TMDB returns ``""`` (not a missing key) for an unaired episode's air date —
    guarded here rather than raising, since "not yet aired" is a legitimate,
    expected value, not a parse error.
    """
    date_str = _get_str(fields, key)
    if date_str is None:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None


def _poster_url(fields: Mapping[str, object]) -> str | None:
    poster_path = _get_str(fields, "poster_path")
    return f"{_IMAGE_BASE_URL}{poster_path}" if poster_path else None


def _backdrop_url(fields: Mapping[str, object]) -> str | None:
    backdrop_path = _get_str(fields, "backdrop_path")
    return f"{_BACKDROP_BASE_URL}{backdrop_path}" if backdrop_path else None


def _contains_anime_keyword(detail: Mapping[str, object]) -> bool:
    """Return True if TMDB tagged the title with the anime keyword (id 210024).

    Movie details nest keywords under ``keywords.keywords``; TV details nest them
    under ``keywords.results``. Both are handled.
    """
    keywords_block = _as_mapping(detail.get("keywords"))
    rows = keywords_block.get("keywords")
    if rows is None:
        rows = keywords_block.get("results")
    return any(_get_int(_as_mapping(row), "id") == _ANIME_KEYWORD_ID for row in _as_sequence(rows))


class TmdbMetadata:
    """Search and resolve media via TMDB v3. Implements ``MetadataPort``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        cache_ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._movie_cache: _TtlCache[MovieMetadata] = _TtlCache(cache_ttl_seconds)
        self._tv_cache: _TtlCache[TvMetadata] = _TtlCache(cache_ttl_seconds)
        # Stored as an immutable tuple (issue #106), not the mutable ``list`` the
        # ``MetadataPort.search`` return type uses: ``_TtlCache.get`` hands back the
        # SAME object on every hit within the TTL, so a plain cached list would let
        # one caller's in-place mutation (``.append``/``.sort``/...) corrupt what
        # every later hit sees. ``search`` copies the tuple back into a fresh list
        # per call (see below), so the cache entry itself can never be mutated.
        self._search_cache: _TtlCache[tuple[MediaSearchResult, ...]] = _TtlCache(cache_ttl_seconds)
        self._page_cache: _TtlCache[MediaPage] = _TtlCache(cache_ttl_seconds)
        # Same issue-#106 immutable-tuple-cached/fresh-list-returned pattern as
        # ``_search_cache`` above.
        self._season_episodes_cache: _TtlCache[tuple[EpisodeInfo, ...]] = _TtlCache(
            cache_ttl_seconds
        )

    async def _get(
        self, path: str, params: Mapping[str, str], *, not_found_returns_none: bool = True
    ) -> Mapping[str, object] | None:
        """GET ``path`` with the api key; raise on 401/other errors.

        Returns the decoded JSON object on success. The api key is added here and
        is never logged. Crucially, any other error status raises ``TmdbApiError``
        built from ``path`` only — we never let httpx's ``HTTPStatusError`` escape,
        because its message embeds the full URL (and thus the ``api_key`` query
        param).

        ``not_found_returns_none`` governs 404 handling and is deliberately NOT a
        blanket behaviour (issue #89): a 404 on a *detail* lookup (``/movie/{id}``,
        ``/tv/{id}``) genuinely means "no such title" — a real, cacheable answer —
        so those callers pass the default ``True`` and get ``None`` back. A 404 on
        a *search or list* endpoint (``/search/multi``, the trending/popular/
        upcoming lists) means the route itself is wrong (a config or TMDB API
        mismatch), and silently mapping that to an empty result would look
        identical to a legitimate "nothing matched" — so those callers pass
        ``False`` and get a surfaced ``TmdbApiError`` instead.
        """
        query = {"api_key": self._api_key, **params}
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=query)
        except httpx.RequestError as exc:
            # TMDB unreachable (DNS / connection refused / timeout): httpx raises
            # before any status check, so without this it would propagate as an
            # opaque 500. Convert to the surfaced, retryable TmdbApiError. The
            # message names the path only — never the url (which embeds api_key).
            raise TmdbApiError(f"tmdb request to {path} failed") from exc
        if response.status_code == _HTTP_NOT_FOUND:
            if not_found_returns_none:
                return None
            raise TmdbApiError(f"TMDB request to {path} failed (HTTP 404)")
        if response.status_code == _HTTP_UNAUTHORIZED:
            raise TmdbAuthError(
                f"TMDB rejected the request to {path} (HTTP 401): the api key is missing or invalid"
            )
        if not (_HTTP_OK <= response.status_code < _HTTP_MULTIPLE_CHOICES):
            raise TmdbApiError(f"TMDB request to {path} failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # A 200 with a non-JSON body (a reverse-proxy / auth HTML page) would
            # otherwise raise a raw JSONDecodeError that bypasses the TmdbApiError
            # handler and surfaces as an opaque 500. Convert it at the boundary —
            # the message names the path only, never the url (which embeds api_key).
            raise TmdbApiError(f"TMDB returned a non-JSON body for {path}") from exc
        return _as_mapping(payload)

    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        """Search by free text via ``/search/multi`` (movie + tv rows only)."""
        cache_key = f"{query}\x00{year if year is not None else ''}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            # A fresh list per call (issue #106): the cached tuple itself is
            # immutable, but the caller's own return type is ``list`` and must not
            # be the SAME list object handed to a previous caller.
            return list(cached)

        params = {"query": query, "include_adult": "false"}
        if year is not None:
            params["year"] = str(year)
        # A 404 here means the search route itself is wrong (not "no results") —
        # raise rather than silently mapping it to an empty result (issue #89).
        payload = await self._get("/search/multi", params, not_found_returns_none=False)
        results: list[MediaSearchResult] = []
        if payload is not None:
            for row in _as_sequence(payload.get("results")):
                parsed = self._parse_search_row(_as_mapping(row))
                if parsed is not None:
                    if year is not None and parsed.year != year:
                        continue
                    results.append(parsed)
        self._search_cache.set(cache_key, tuple(results))
        return results

    @staticmethod
    def _parse_search_row(
        row: Mapping[str, object], media_type_override: MediaKind | None = None
    ) -> MediaSearchResult | None:
        """Map one TMDB row to a ``MediaSearchResult`` (movie/tv only; else drop).

        The movie-only list endpoints (trending/popular/upcoming) omit
        ``media_type``; pass ``media_type_override='movie'`` so those rows map as
        movies while any stray person/non-movie row is still dropped.
        """
        media_type = media_type_override or _get_str(row, "media_type")
        tmdb_id = _get_int(row, "id")
        if tmdb_id is None or media_type not in ("movie", "tv"):
            return None
        if media_type == "movie":
            title = _get_str(row, "title")
            year = _year_from_date(row, "release_date")
        else:
            title = _get_str(row, "name")
            year = _year_from_date(row, "first_air_date")
        if title is None:
            return None
        return MediaSearchResult(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            year=year,
            overview=_get_str(row, "overview"),
            poster_url=_poster_url(row),
            backdrop_url=_backdrop_url(row),
        )

    async def get_movie(self, tmdb_id: int) -> MovieMetadata | None:
        """Resolve a movie by tmdb id, or ``None`` if not found."""
        cache_key = str(tmdb_id)
        cached = self._movie_cache.get(cache_key)
        if cached is not None:
            return cached

        detail = await self._get(f"/movie/{tmdb_id}", {"append_to_response": _APPEND})
        if detail is None:
            return None
        external_ids = _as_mapping(detail.get("external_ids"))
        movie = MovieMetadata(
            tmdb_id=_get_int(detail, "id") or tmdb_id,
            imdb_id=_get_str(detail, "imdb_id") or _get_str(external_ids, "imdb_id"),
            title=_get_str(detail, "title") or "",
            year=_year_from_date(detail, "release_date"),
            overview=_get_str(detail, "overview"),
            poster_url=_poster_url(detail),
            backdrop_url=_backdrop_url(detail),
            is_anime=_contains_anime_keyword(detail),
        )
        _logger.debug(
            "resolved tmdb movie (anime=%s)", movie.is_anime, extra={"tmdb_id": safe_int(tmdb_id)}
        )
        self._movie_cache.set(cache_key, movie)
        return movie

    async def get_tv_show(self, tmdb_id: int) -> TvMetadata | None:
        """Resolve a TV show by tmdb id, or ``None`` if not found."""
        cache_key = str(tmdb_id)
        cached = self._tv_cache.get(cache_key)
        if cached is not None:
            return cached

        detail = await self._get(f"/tv/{tmdb_id}", {"append_to_response": _APPEND})
        if detail is None:
            return None
        external_ids = _as_mapping(detail.get("external_ids"))
        show = TvMetadata(
            tmdb_id=_get_int(detail, "id") or tmdb_id,
            tvdb_id=_get_int(external_ids, "tvdb_id"),
            imdb_id=_get_str(external_ids, "imdb_id"),
            title=_get_str(detail, "name") or "",
            year=_year_from_date(detail, "first_air_date"),
            overview=_get_str(detail, "overview"),
            poster_url=_poster_url(detail),
            backdrop_url=_backdrop_url(detail),
            season_count=_get_int(detail, "number_of_seasons") or 0,
            is_anime=_contains_anime_keyword(detail),
        )
        _logger.debug(
            "resolved tmdb tv (anime=%s)", show.is_anime, extra={"tmdb_id": safe_int(tmdb_id)}
        )
        self._tv_cache.set(cache_key, show)
        return show

    async def trending_movies(self, page: int = 1) -> MediaPage:
        """List the week's trending movies via ``/trending/movie/week``."""
        return await self._list_page("/trending/movie/week", "trending:movie:week", page, "movie")

    async def popular_movies(self, page: int = 1) -> MediaPage:
        """List currently popular movies via ``/movie/popular``."""
        return await self._list_page("/movie/popular", "popular:movie", page, "movie")

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        """List upcoming movie releases via ``/movie/upcoming``."""
        return await self._list_page("/movie/upcoming", "upcoming:movie", page, "movie")

    async def trending_tv(self, page: int = 1) -> MediaPage:
        """List the week's trending TV shows via ``/trending/tv/week``."""
        return await self._list_page("/trending/tv/week", "trending:tv:week", page, "tv")

    async def popular_tv(self, page: int = 1) -> MediaPage:
        """List currently popular TV shows via ``/tv/popular``."""
        return await self._list_page("/tv/popular", "popular:tv", page, "tv")

    async def _list_page(
        self, path: str, cache_prefix: str, page: int, kind: MediaKind
    ) -> MediaPage:
        """Fetch one page of a single-kind list endpoint and map its envelope.

        The page is clamped to TMDB's documented ``1..500`` window. Rows carry no
        ``media_type`` on these endpoints, so every row is mapped as ``kind`` (any
        stray row of the other kind is still dropped). Each page is cached by index.
        """
        clamped = max(_MIN_PAGE, min(page, _MAX_PAGE))
        cache_key = f"{cache_prefix}:p{clamped}"
        cached = self._page_cache.get(cache_key)
        if cached is not None:
            return cached

        # A 404 here means the list route itself is wrong (not "no results") —
        # raise rather than silently mapping it to an empty page (issue #89).
        payload = await self._get(path, {"page": str(clamped)}, not_found_returns_none=False)
        fields: Mapping[str, object] = payload if payload is not None else {}
        results: list[MediaSearchResult] = []
        for row in _as_sequence(fields.get("results")):
            parsed = self._parse_search_row(_as_mapping(row), kind)
            if parsed is not None:
                results.append(parsed)
        media_page = MediaPage(
            page=_get_int(fields, "page") or clamped,
            total_pages=_get_int(fields, "total_pages") or 0,
            total_results=_get_int(fields, "total_results") or 0,
            results=tuple(results),
        )
        self._page_cache.set(cache_key, media_page)
        return media_page

    async def season_episodes(self, tmdb_id: int, season_number: int) -> list[EpisodeInfo]:
        """Episodes of one TV season via ``/tv/{id}/season/{n}``.

        A 404 here means the season/route is wrong (bad tmdb id, season that
        doesn't exist for this show, or an API mismatch) -- NOT "no episodes" --
        so it is surfaced as ``TmdbApiError`` (issue #89 pattern), never silently
        mapped to an empty list. Callers (ADR-0018) treat any raise as "target
        unknown this cycle" and retry later.
        """
        cache_key = f"{tmdb_id}:{season_number}"
        cached = self._season_episodes_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        payload = await self._get(
            f"/tv/{tmdb_id}/season/{season_number}", {}, not_found_returns_none=False
        )
        fields: Mapping[str, object] = payload if payload is not None else {}
        results: list[EpisodeInfo] = []
        for row in _as_sequence(fields.get("episodes")):
            episode_fields = _as_mapping(row)
            episode_number = _get_int(episode_fields, "episode_number")
            if episode_number is None:
                continue
            results.append(
                EpisodeInfo(
                    episode_number=episode_number,
                    air_date=_date_from(episode_fields, "air_date"),
                )
            )
        self._season_episodes_cache.set(cache_key, tuple(results))
        return list(results)
