"""ProwlarrIndexer — the live :class:`IndexerPort` implementation (Prowlarr v1).

Searches the configured indexers through Prowlarr's aggregating ``/api/v1/search``
endpoint and normalizes each ``ReleaseResource`` into the frozen domain
:class:`~plex_manager.domain.release.CandidateRelease` DTO. Grabbing is a separate
concern (the download client) and is deliberately not done here.

Construction is dependency-injected: ``base_url``, ``api_key`` and an
``httpx.AsyncClient`` are passed in (the web/services layer wires decrypted creds
later). The api key travels only in the ``X-Api-Key`` header and is NEVER logged.

Behaviours carried over from the analysis extract:

* ``media_type`` maps to Prowlarr's ``type`` param (``movie`` / ``tvsearch`` /
  ``search``); ``tmdb_id`` -> ``tmdbid``; ``imdb_id`` -> ``imdbid`` zero-padded to
  the canonical ``tt#######`` form; ``season`` / ``episode`` pass through.
* ``categories`` is omitted entirely when empty (passing ``[]`` makes Prowlarr
  filter unexpectedly).
* Results are de-duplicated by ``guid`` keeping the lowest ``indexer_priority``.
  Prowlarr's ``ReleaseResource`` does NOT serialise the indexer priority, so it is
  resolved out-of-band from ``/api/v1/indexer`` (cached, ``IndexerResource.priority``)
  and joined to each release by ``indexerId``. If that lookup fails, priority
  defaults uniformly and de-dup degrades to deterministic first-appearance-wins.
* A 400 (all indexers rate-limited) surfaces as :class:`IndexerRateLimitError`
  rather than a silent empty list.
* A release with neither a magnet nor a download url is skipped with a warning,
  never raised on.
* ``publish_date`` parsing is tolerant — an unparseable value falls back to the
  unix epoch rather than aborting the whole search.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, cast

import httpx

from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest

__all__ = ["IndexerError", "IndexerRateLimitError", "ProwlarrIndexer"]

_logger = logging.getLogger(__name__)

_SEARCH_PATH: Final = "/api/v1/search"
_INDEXER_PATH: Final = "/api/v1/indexer"
_HTTP_OK: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_DEFAULT_INDEXER_PRIORITY: Final = 25
_PRIORITY_TTL_SECONDS: Final = 300.0
# Indexer searches fan out across every configured tracker, so a single search
# routinely runs far longer than a normal HTTP call (a popular movie can return
# hundreds of releases across a dozen indexers, ~60s+). The shared client's
# default timeout (~30s) would abort real searches, so the search request gets a
# generous, configurable override. The prototype used 300s; 120s is the default
# here and is tunable per deployment.
_DEFAULT_SEARCH_TIMEOUT: Final = 120.0
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

# One query-param pair; the value union matches httpx's ``PrimitiveData`` so the
# list is assignable to ``httpx``'s ``params=`` without an invariance error.
_QueryParam = tuple[str, str | int | float | bool | None]

# media_type (domain) -> Prowlarr newznab ``type`` param.
_TYPE_MAP: Final[Mapping[str, str]] = {
    "movie": "movie",
    "tv": "tvsearch",
    "search": "search",
}


class IndexerError(RuntimeError):
    """Base for surfaced Prowlarr failures (transport outage or HTTP error).

    Raised instead of letting httpx's transport / status errors escape as an
    opaque 500 (whose message embeds the request url). A surfaced, retryable state
    — never swallowed into an empty result set. The message never includes the api
    key or url.
    """


class IndexerRateLimitError(IndexerError):
    """Raised when Prowlarr reports every indexer is rate-limited (HTTP 400).

    A surfaced, retryable state — never swallowed into an empty result set. The
    message never includes the api key.
    """


def _normalize_imdb_id(imdb_id: str) -> str | None:
    """Return the canonical ``tt#######`` form (digits zero-padded to 7).

    Accepts ``"tt1375666"``, ``"1375666"`` or ``"123"``; returns ``None`` when no
    digits are present so we never send a malformed ``imdbid`` param.
    """
    digits = imdb_id.strip().removeprefix("tt").removeprefix("TT")
    if not digits.isdigit():
        return None
    return f"tt{int(digits):07d}"


def _as_sequence(value: object) -> Sequence[object]:
    """Narrow an untyped JSON node to a sequence (a bare str is not one here)."""
    if isinstance(value, (list, tuple)):
        return cast("Sequence[object]", value)
    return ()


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow an untyped JSON node to a string-keyed mapping (else empty)."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return {}


def _get_int(fields: Mapping[str, object], key: str, default: int = 0) -> int:
    value = fields.get(key)
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return default


def _get_opt_int(fields: Mapping[str, object], key: str) -> int | None:
    value = fields.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _get_str(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _parse_publish_date(value: object) -> datetime:
    """Parse Prowlarr's ISO-8601 ``publishDate``; fall back to the unix epoch.

    Tolerant by design: a malformed or absent date must not abort the search.
    """
    if not isinstance(value, str) or not value:
        return _EPOCH
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _logger.warning("unparseable Prowlarr publishDate %r; using epoch", value)
        return _EPOCH
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _categories(row: Mapping[str, object]) -> list[int]:
    """Extract numeric category ids from a ``ReleaseResource.categories`` block.

    Each entry is an object like ``{"id": 2000, "name": "Movies"}``; bare ints are
    also tolerated.
    """
    out: list[int] = []
    for entry in _as_sequence(row.get("categories")):
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            out.append(entry)
            continue
        cat_id = _get_opt_int(_as_mapping(entry), "id")
        if cat_id is not None:
            out.append(cat_id)
    return out


def _leechers(row: Mapping[str, object]) -> int | None:
    """Resolve leechers, preferring the explicit field, else ``peers - seeders``."""
    explicit = _get_opt_int(row, "leechers")
    if explicit is not None:
        return explicit
    peers = _get_opt_int(row, "peers")
    seeders = _get_opt_int(row, "seeders")
    if peers is not None and seeders is not None:
        return max(peers - seeders, 0)
    return None


class ProwlarrIndexer:
    """Search configured indexers via Prowlarr. Implements ``IndexerPort``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        search_timeout: float = _DEFAULT_SEARCH_TIMEOUT,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._search_timeout = search_timeout
        # indexerId -> priority, resolved from /api/v1/indexer and cached with a
        # short TTL so a reconciler loop does not re-fetch on every search.
        self._priority_cache: tuple[datetime, Mapping[int, int]] | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, redacts the key
        return f"ProwlarrIndexer(base_url={self._base_url!r}, api_key=<redacted>)"

    def _build_params(self, request: IndexerSearchRequest) -> list[_QueryParam]:
        """Translate a domain search request into Prowlarr query params.

        Returns a list of pairs so repeated keys (``categories``, ``indexerIds``)
        serialise correctly. ``categories`` is omitted when empty.
        """
        params: list[_QueryParam] = [("type", _TYPE_MAP[request.media_type])]
        if request.query:
            params.append(("query", request.query))
        if request.tmdb_id is not None:
            params.append(("tmdbid", str(request.tmdb_id)))
        if request.imdb_id is not None:
            normalized = _normalize_imdb_id(request.imdb_id)
            if normalized is not None:
                params.append(("imdbid", normalized))
        if request.tvdb_id is not None:
            params.append(("tvdbid", str(request.tvdb_id)))
        if request.year is not None:
            params.append(("year", str(request.year)))
        if request.season is not None:
            params.append(("season", str(request.season)))
        if request.episode is not None:
            params.append(("ep", request.episode))
        # Only send categories / indexerIds when non-empty — an empty list makes
        # Prowlarr filter unexpectedly.
        for category in request.categories:
            params.append(("categories", str(category)))
        for indexer_id in request.indexer_ids:
            params.append(("indexerIds", str(indexer_id)))
        return params

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        """Run ``request`` and return de-duplicated candidate releases."""
        priorities = await self._indexer_priorities()
        try:
            response = await self._client.get(
                f"{self._base_url}{_SEARCH_PATH}",
                params=self._build_params(request),
                headers={"X-Api-Key": self._api_key},
                timeout=self._search_timeout,
            )
        except httpx.RequestError as exc:
            # Prowlarr unreachable (DNS / refused / timeout): surface a retryable
            # error rather than an opaque 500. No url/api key in the message.
            raise IndexerError("Prowlarr search request failed") from exc
        if response.status_code == _HTTP_BAD_REQUEST:
            raise IndexerRateLimitError(
                "Prowlarr returned HTTP 400 for the search — all indexers are "
                "rate-limited or the query was rejected"
            )
        if response.is_error:
            # Non-400 HTTP failure (5xx / auth): surface a retryable IndexerError
            # rather than letting httpx's HTTPStatusError escape (it embeds the url).
            raise IndexerError(f"Prowlarr search failed (HTTP {response.status_code})")

        rows = _as_sequence(response.json())
        candidates: list[CandidateRelease] = []
        for raw in rows:
            candidate = self._to_candidate(_as_mapping(raw), priorities)
            if candidate is not None:
                candidates.append(candidate)
        # The alpha only wires a torrent client, so usenet releases (protocol !=
        # "torrent") must never reach qBittorrent — drop them here, surfacing the
        # count at debug rather than silently shrinking the result set.
        torrent_candidates = [c for c in candidates if c.protocol == "torrent"]
        dropped = len(candidates) - len(torrent_candidates)
        if dropped:
            _logger.debug("dropped %d non-torrent (usenet) Prowlarr release(s)", dropped)
        return _dedupe_by_guid(torrent_candidates)

    async def _indexer_priorities(self) -> Mapping[int, int]:
        """Return ``{indexerId: priority}`` from ``/api/v1/indexer`` (cached).

        Prowlarr's search ``ReleaseResource`` carries no priority, so it must be
        joined from the indexer list. Best-effort: a failed lookup yields an empty
        map (logged) rather than aborting the search — de-dup then degrades to
        first-appearance-wins, never to silence.
        """
        now = datetime.now(UTC)
        cached = self._priority_cache
        if cached is not None and (now - cached[0]).total_seconds() < _PRIORITY_TTL_SECONDS:
            return cached[1]
        try:
            response = await self._client.get(
                f"{self._base_url}{_INDEXER_PATH}",
                headers={"X-Api-Key": self._api_key},
            )
        except httpx.HTTPError as exc:
            _logger.warning("could not fetch Prowlarr indexer priorities: %s", exc)
            return {}
        if response.status_code != _HTTP_OK:
            _logger.warning(
                "Prowlarr /api/v1/indexer returned HTTP %d; using default priorities",
                response.status_code,
            )
            return {}
        mapping: dict[int, int] = {}
        for raw in _as_sequence(response.json()):
            row = _as_mapping(raw)
            indexer_id = _get_opt_int(row, "id")
            priority = _get_opt_int(row, "priority")
            if indexer_id is not None and priority is not None:
                mapping[indexer_id] = priority
        self._priority_cache = (now, mapping)
        return mapping

    @staticmethod
    def _to_candidate(
        row: Mapping[str, object], priorities: Mapping[int, int]
    ) -> CandidateRelease | None:
        """Map one Prowlarr ``ReleaseResource`` to a ``CandidateRelease``.

        ``indexer_priority`` is joined from ``priorities`` by ``indexerId`` (the
        wire resource omits it), defaulting when the indexer is unknown. Returns
        ``None`` (with a warning) when the release has neither a magnet nor a
        download url — there would be nothing to grab.
        """
        magnet_url = _get_str(row, "magnetUrl")
        download_url = _get_str(row, "downloadUrl")
        title = _get_str(row, "title") or "Unknown"
        if magnet_url is None and download_url is None:
            _logger.warning("skipping Prowlarr release with no magnet/download url: %r", title)
            return None

        info_hash = _get_str(row, "infoHash")
        protocol = "usenet" if _get_str(row, "protocol") == "usenet" else "torrent"
        indexer_id = _get_int(row, "indexerId")
        return CandidateRelease(
            guid=_get_str(row, "guid") or (info_hash or title),
            title=title,
            size_bytes=_get_int(row, "size"),
            download_url=download_url,
            magnet_url=magnet_url,
            info_hash=info_hash.lower() if info_hash else None,
            seeders=_get_opt_int(row, "seeders"),
            leechers=_leechers(row),
            indexer_id=indexer_id,
            indexer_name=_get_str(row, "indexer") or "Unknown",
            indexer_priority=priorities.get(indexer_id, _DEFAULT_INDEXER_PRIORITY),
            publish_date=_parse_publish_date(row.get("publishDate")),
            imdb_id=_get_int(row, "imdbId"),
            tmdb_id=_get_int(row, "tmdbId"),
            categories=_categories(row),
            protocol=protocol,
        )


def _dedupe_by_guid(candidates: Sequence[CandidateRelease]) -> list[CandidateRelease]:
    """De-duplicate by ``guid``, keeping the lowest ``indexer_priority`` per guid.

    Lower priority = more preferred (Prowlarr / arr convention). Order of first
    appearance is preserved for stable, testable output.
    """
    best: dict[str, CandidateRelease] = {}
    order: list[str] = []
    for candidate in candidates:
        existing = best.get(candidate.guid)
        if existing is None:
            best[candidate.guid] = candidate
            order.append(candidate.guid)
        elif candidate.indexer_priority < existing.indexer_priority:
            best[candidate.guid] = candidate
    return [best[guid] for guid in order]
