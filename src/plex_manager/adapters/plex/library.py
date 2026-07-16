"""PlexLibrary — the :class:`LibraryPort` implementation backed by the Plex API.

The adapter answers three questions for the import/availability pipeline:
"which sections exist", "is this tmdb id already in the library", and "scan this
path". It maps Plex's ``MediaContainer`` JSON into the frozen domain DTO
(:class:`LibrarySection`) and never leaks Plex's wire shape past this module.

Construction is dependency-injected: ``base_url``, ``token`` and an
``httpx.AsyncClient`` are passed in (the web/services layer wires decrypted creds
later). The token is sent in the ``X-Plex-Token`` header — NEVER in the URL (which
could be logged) — and is redacted from ``repr``.

Caching: the web layer builds a fresh adapter per request, so the section list, the
set of present movie tmdb ids, and the per-show map of present TV seasons each live
in their own MODULE-LEVEL TTL cache keyed by the ``(base_url, token-hash)`` pair (a
per-instance cache would never be hit). The token is part of the key so a rotated
or mistyped credential for the same server re-fetches with the new token instead of
returning the previous token's sections — otherwise a bad token could read back a
stale "Connected to Plex" and be saved. Only a SHA-256 of the token enters the key,
never the token itself. The TTL is short — availability changes when a scan
completes, and a stale "present" answer for a few minutes is harmless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, Literal, NamedTuple, cast

import httpx

from plex_manager.adapters.service_url import InvalidServiceUrl, ServiceUrl
from plex_manager.headersafe import header_value_error
from plex_manager.logsafe import safe_int
from plex_manager.ports.library import (
    ArtworkImage,
    ArtworkKind,
    LibrarySection,
    WatchState,
    WatchStateQuery,
)
from plex_manager.services import path_visibility

__all__ = ["PlexAuthError", "PlexLibrary", "PlexLibraryError"]

_logger = logging.getLogger(__name__)

_HTTP_OK: Final = 200
_HTTP_MULTIPLE_CHOICES: Final = 300
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_PAGE_SIZE: Final = 100
_CACHE_TTL_SECONDS: Final = 300.0
# The proxied artwork body must actually be an image (issue #66): a reverse proxy
# or auth gate in front of Plex could answer a ``thumb`` GET with an HTML login
# page at HTTP 200, which we must NOT forward to the browser as if it were a
# poster. Anything whose Content-Type is not ``image/*`` is treated as a miss.
_IMAGE_CONTENT_TYPE_PREFIX: Final = "image/"
_MAX_ARTWORK_BYTES: Final = 10 * 1024 * 1024
_MAX_CONCURRENT_ARTWORK_OPERATIONS: Final = 4

# Plex stores agent ids as ``<agent>://<id>``. The modern Movie agent uses
# ``tmdb://``; the legacy "The Movie Database" agent uses ``themoviedb://``. Both
# are matched (verified against overseerr's plex scanner GUID regex set).
_TMDB_GUID_RE: Final = re.compile(r"(?:tmdb|themoviedb)://([0-9]+)")


class PlexLibraryError(RuntimeError):
    """Raised when Plex returns a non-2xx status other than 401/403, is
    unreachable, or returns a non-JSON 200.

    A surfaced, retryable error. The message names the request *path* and status
    code only — never the full URL (the token travels in a header, but the path is
    all the message needs). Letting httpx's transport/status error escape would
    surface as an opaque 500, so it is converted at the boundary.
    """


class PlexAuthError(RuntimeError):
    """Raised when Plex rejects the token (HTTP 401/403).

    A clear, surfaced error — never a silent empty result. The message names the
    cause but never includes the token.
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

    def clear(self) -> None:
        self._store.clear()

    def invalidate(self, key: str) -> None:
        """Drop one key so the next ``get`` re-fetches (e.g. after a Plex scan)."""
        self._store.pop(key, None)


# Module-level caches (keyed by base_url) — see the module docstring for why these
# cannot be per-instance.
_SECTIONS_CACHE: _TtlCache[tuple[LibrarySection, ...]] = _TtlCache(_CACHE_TTL_SECONDS)
_PRESENT_TMDB_CACHE: _TtlCache[frozenset[int]] = _TtlCache(_CACHE_TTL_SECONDS)
# Show-level TV presence: the frozenset of ALL show tmdb ids in the library, from a
# single cheap ``/all`` guid crawl of the show sections (NO per-show ``/children``
# fetch -- that per-season crawl is what ``_TV_SEASONS_CACHE`` below pays for). Feeds
# the SHOW-level ``present_ids`` tile decoration only; same key/positive-only
# discipline as ``_PRESENT_TMDB_CACHE`` (only a freshly-paged snapshot is stored).
_PRESENT_SHOW_TMDB_CACHE: _TtlCache[frozenset[int]] = _TtlCache(_CACHE_TTL_SECONDS)
# TV presence is per-season: tmdb id -> the frozenset of season numbers with at
# least one episode present (``leafCount>0``). Same key/pattern/positive-only
# discipline as ``_PRESENT_TMDB_CACHE`` (see its comment on ``is_available``) —
# only a freshly-paged snapshot is ever stored, never a cached absence.
_TV_SEASONS_CACHE: _TtlCache[dict[int, frozenset[int]]] = _TtlCache(_CACHE_TTL_SECONDS)


class _ArtworkKeys(NamedTuple):
    """The Plex-native artwork PATHS for one library item (issue #66).

    ``poster`` is Plex's ``thumb`` attribute, ``background`` its ``art`` — both
    server-relative resource paths (e.g. ``/library/metadata/42/thumb/1700000000``),
    NEVER a full URL and NEVER carrying the token. Either may be ``None`` when Plex
    has no image of that kind for the item.
    """

    poster: str | None
    background: str | None


# Plex-native artwork indexes (issue #66): a per-credential map of
# ``tmdb_id -> _ArtworkKeys`` built from ONE crawl of the movie (resp. show)
# sections, so the image proxy resolves a poster/background path without re-paging
# Plex per image. Split by media type — exactly like _PRESENT_TMDB_CACHE vs
# _PRESENT_SHOW_TMDB_CACHE — so a movie-art request never crawls the show sections
# (and vice versa), and a cached movie map is never mistaken for "no show art".
# Unlike the presence caches, a cached ABSENCE here is harmless: a tmdb id missing
# from the map just means the browser shows TMDB art for up to the TTL (purely
# cosmetic), never a wrong availability/eviction decision — so the whole map
# (misses included) is cached with no refresh-absent dance.
_MOVIE_ARTWORK_CACHE: _TtlCache[dict[int, _ArtworkKeys]] = _TtlCache(_CACHE_TTL_SECONDS)
_SHOW_ARTWORK_CACHE: _TtlCache[dict[int, _ArtworkKeys]] = _TtlCache(_CACHE_TTL_SECONDS)
# Serializes the artwork crawl per ``(cache_key, section_type)`` so a page-load
# that fires many proxy image requests at once (30 in-library tiles => 30
# concurrent GETs) triggers ONE crawl the rest await, not a thundering herd of
# full-library crawls. Keyed like the caches (server + token-hash + type); a
# rotated token adds at most one idle lock, negligible for a single-server
# install. asyncio is single-threaded, so the get-or-create is race-free.
_ARTWORK_CRAWL_LOCKS: dict[str, asyncio.Lock] = {}
_artwork_operation_semaphore: asyncio.Semaphore | None = None


def _artwork_semaphore() -> asyncio.Semaphore:
    global _artwork_operation_semaphore
    if _artwork_operation_semaphore is None:
        _artwork_operation_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ARTWORK_OPERATIONS)
    return _artwork_operation_semaphore


def _artwork_crawl_lock(key: str) -> asyncio.Lock:
    lock = _ARTWORK_CRAWL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ARTWORK_CRAWL_LOCKS[key] = lock
    return lock


def reset_caches() -> None:
    """Clear the module-level caches. Test-isolation helper (not part of the port)."""
    _SECTIONS_CACHE.clear()
    _PRESENT_TMDB_CACHE.clear()
    _PRESENT_SHOW_TMDB_CACHE.clear()
    _TV_SEASONS_CACHE.clear()
    _MOVIE_ARTWORK_CACHE.clear()
    _SHOW_ARTWORK_CACHE.clear()
    _ARTWORK_CRAWL_LOCKS.clear()
    global _artwork_operation_semaphore
    _artwork_operation_semaphore = None


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


def _get_str(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _get_int(fields: Mapping[str, object], key: str) -> int | None:
    value = fields.get(key)
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(value, int):
        return value
    return None


def _get_epoch_datetime(fields: Mapping[str, object], key: str) -> datetime | None:
    """Decode a Plex unix-seconds field (e.g. ``lastViewedAt``) to a UTC ``datetime``.

    Plex omits the field entirely for an item/season that has never been viewed,
    which :func:`_get_int` already reports as ``None`` -- propagated through
    honestly rather than a fabricated epoch.
    """
    value = _get_int(fields, key)
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _media_container(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Unwrap Plex's top-level ``MediaContainer`` envelope."""
    return _as_mapping(payload.get("MediaContainer"))


def _parse_section(entry: Mapping[str, object]) -> LibrarySection | None:
    """Map one ``Directory`` row to a :class:`LibrarySection` (None if unusable)."""
    key = _get_str(entry, "key")
    title = _get_str(entry, "title")
    section_type = _get_str(entry, "type")
    if key is None or title is None or section_type not in ("movie", "show"):
        return None
    locations = tuple(
        path
        for loc in _as_sequence(entry.get("Location"))
        if (path := _get_str(_as_mapping(loc), "path")) is not None
    )
    return LibrarySection(key=key, title=title, type=section_type, locations=locations)


def _extract_tmdb_ids(text: str) -> list[int]:
    """Pull every ``tmdb://`` / ``themoviedb://`` numeric id out of a guid string."""
    return [int(match) for match in _TMDB_GUID_RE.findall(text)]


def _collect_item_tmdb_ids(item: Mapping[str, object], present: set[int]) -> None:
    """Add the tmdb ids of one ``Metadata`` item to ``present``.

    Plex exposes ids both on the legacy scalar ``guid`` field and (with
    ``includeGuids=1``) on the ``Guid[]`` array of ``{"id": "<agent>://<id>"}``
    rows; both are scanned so either agent layout resolves.
    """
    guid = _get_str(item, "guid")
    if guid is not None:
        present.update(_extract_tmdb_ids(guid))
    for guid_entry in _as_sequence(item.get("Guid")):
        gid = _get_str(_as_mapping(guid_entry), "id")
        if gid is not None:
            present.update(_extract_tmdb_ids(gid))


def _item_matches_tmdb_id(item: Mapping[str, object], tmdb_id: int) -> bool:
    """Whether one ``Metadata`` item's guid(s) resolve to ``tmdb_id``."""
    ids: set[int] = set()
    _collect_item_tmdb_ids(item, ids)
    return tmdb_id in ids


def _movie_watch_state_from_item(item: Mapping[str, object]) -> WatchState:
    """Movie watch state (ADR-0012): watched = ``viewCount>0``, timestamp =
    the item's ``lastViewedAt``.

    Plex omits ``viewCount``/``lastViewedAt`` entirely for a never-played item,
    which reads as unwatched here. ``watched`` is additionally forced ``False``
    whenever ``last_viewed_at`` is ``None`` -- ``WatchState``'s own contract --
    so the pair can never come out inconsistent even if Plex ever reported a
    stray ``viewCount`` with no timestamp.
    """
    last_viewed_at = _get_epoch_datetime(item, "lastViewedAt")
    view_count = _get_int(item, "viewCount")
    watched = last_viewed_at is not None and view_count is not None and view_count > 0
    return WatchState(watched=watched, last_viewed_at=last_viewed_at)


def _season_watch_state_from_entry(entry: Mapping[str, object]) -> WatchState:
    """Season watch state (ADR-0012): watched = every episode viewed
    (``viewedLeafCount == leafCount``), timestamp = the season's ``lastViewedAt``.

    ``leafCount`` must be present AND greater than zero -- an empty/announced
    season (no episodes indexed yet) must never read as "watched" by the vacuous
    truth of ``0 == 0``. As with the movie case, ``watched`` is forced ``False``
    whenever ``last_viewed_at`` is ``None`` so the two signals stay honestly
    aligned.
    """
    leaf_count = _get_int(entry, "leafCount")
    viewed_leaf_count = _get_int(entry, "viewedLeafCount")
    last_viewed_at = _get_epoch_datetime(entry, "lastViewedAt")
    fully_viewed = (
        leaf_count is not None
        and leaf_count > 0
        and viewed_leaf_count is not None
        and viewed_leaf_count == leaf_count
    )
    watched = fully_viewed and last_viewed_at is not None
    return WatchState(watched=watched, last_viewed_at=last_viewed_at)


def _resolve_correlated_watch_state(
    hits: Sequence[tuple[frozenset[str], WatchState]],
) -> WatchState:
    """Resolve :meth:`PlexLibrary.watch_state`'s path-correlated ``hits`` to ONE
    :class:`WatchState` (issue #239).

    Each hit pairs the CANDIDATE's own reported media-file path(s) (a movie's
    ``Media[].Part[].file`` set, or a TV season's episode file-path set) with the
    :class:`WatchState` read off that same Plex item/season row.

    * Zero hits -- the target could not be correlated at all -- fails closed
      exactly as before: ``WatchState(watched=False, last_viewed_at=None)``.
    * Exactly one hit resolves directly to that hit's own state.
    * More than one hit was issue #207's original fail-closed case (any
      ambiguity at all), which turned out to be too blunt: the SAME physical
      copy indexed by more than one Plex section (e.g. a broad ``/media``
      section AND a nested ``/media/anime`` section both covering the same
      files) produces more than one correlated hit for a candidate that is
      genuinely ONE item on disk -- permanently exempting it from disk-pressure
      eviction with no operator-visible explanation. So: hits are merged into
      ONE logical item ONLY when EVERY hit reports the IDENTICAL set of
      underlying file paths -- genuinely different underlying files (distinct
      copies on disk, a true duplicate) still fail closed, unchanged from
      before.

    The merge itself: watched if ANY correlated hit is watched -- a section
    whose own watch-state sync lags behind (or that a user happens to browse
    less) must never mask a real watch recorded via another section indexing
    the identical file. The timestamp is the NEWEST ``last_viewed_at`` among
    ALL correlated hits -- INCLUDING not-yet-fully-watched / in-progress ones
    (issue #290) -- never the oldest. Eviction treats
    ``last_viewed_at < grace_cutoff`` as eligible and sorts stalest-first, so
    keeping a STALE timestamp from a section that hasn't caught up with a
    recent rewatch another section already recorded would make the item
    eligible for deletion during the grace window right after that rewatch.

    Considering only the WATCHED hits' timestamps here would FAIL OPEN: a
    physical file indexed by two sections where section A recorded a completed
    watch long past the grace cutoff (``watched=True``, stale timestamp) while
    the operator is mid-rewatch via section B (its ``viewCount`` /
    ``viewedLeafCount`` not yet incremented, so ``watched=False`` -- but its
    ``lastViewedAt`` is RECENT) would merge to A's stale timestamp and become
    eviction-eligible DURING the active rewatch. Deletion safety requires the
    most-recent watch evidence -- from ANY correlated hit, watched or not -- to
    win, so a disk-pressure sweep landing mid-rewatch never deletes the file
    out from under the viewer. (The movie case has no season equivalent of the
    partial-view state, but the same multi-section merge introduces the same
    fail-open there: a single-section item's own in-progress ``viewCount=0``
    protects it, but the merge must not let A's stale watched timestamp override
    B's recent in-progress one.)
    """
    if len(hits) == 1:
        return hits[0][1]
    if not hits:
        return WatchState(watched=False, last_viewed_at=None)
    distinct_path_sets = {file_paths for file_paths, _ in hits}
    if len(distinct_path_sets) != 1:
        # Genuinely ambiguous: the hits do not all agree on the same underlying
        # file(s) -- fail closed, exactly as issue #207 originally specified.
        return WatchState(watched=False, last_viewed_at=None)
    if not any(state.watched for _, state in hits):
        return WatchState(watched=False, last_viewed_at=None)
    # NEWEST across ALL hits (watched or in-progress), not just the watched ones
    # -- see the docstring's fail-open note (issue #290). A watched hit always
    # carries a timestamp (``WatchState``'s own contract), so ``watched`` being
    # true guarantees at least one; the guard below stays defensive regardless.
    all_timestamps = [state.last_viewed_at for _, state in hits if state.last_viewed_at is not None]
    if not all_timestamps:
        return WatchState(watched=False, last_viewed_at=None)
    return WatchState(watched=True, last_viewed_at=max(all_timestamps))


def _is_path_prefix(prefix: str, path: str) -> bool:
    """True if ``prefix`` is ``path`` or a parent directory of it (segment-aware).

    ``/data/movies`` covers ``/data/movies/Foo/foo.mkv`` but not ``/data/movies-4k``.
    """
    norm = prefix.rstrip("/")
    return path == norm or path.startswith(f"{norm}/")


def _container_to_host_scan_path(location: str, path: str) -> str | None:
    """Reverse a Docker host->container remap: the HOST path Plex should scan.

    ``path`` is a CONTAINER path the importer/eviction placed into (e.g.
    ``/media/Movies/Title (Year)``); ``location`` is a section location as Plex
    reported it, in the HOST namespace (e.g. ``/srv/media/Movies``). After a Docker
    host/container split the container path never prefix-matches the host location,
    so a plain ``_is_path_prefix`` check always misses and every remapped root
    would fall back to a full-library refresh.

    Reverse the mapping the SAME way :func:`path_visibility.remap_to_visible` built
    it forward: strip a known container mount prefix from ``path``, align the
    location's trailing components against what remains, and re-anchor the leftover
    tail on the HOST ``location``. Purely lexical (no filesystem probe); anchoring on
    the known mount prefix keeps it precise -- a section whose location merely shares
    a trailing name but doesn't sit under the same mount does not spuriously match.
    Returns ``None`` when ``path`` isn't under any known mount, or shares no
    directory with ``location`` below it (e.g. a whole-media-root/mount-root remap,
    which has no shared component to anchor on and honestly falls back to a full
    refresh).
    """
    path_comps = [c for c in path.split("/") if c]
    loc_comps = [c for c in location.split("/") if c]
    for mount in path_visibility.KNOWN_CONTAINER_MOUNTS:
        mount_comps = [c for c in mount.split("/") if c]
        if not mount_comps or path_comps[: len(mount_comps)] != mount_comps:
            continue
        below = path_comps[len(mount_comps) :]
        # Longest run where the location's tail meets the path's head below the
        # mount -- longest first so the deepest shared directory wins.
        for k in range(min(len(loc_comps), len(below)), 0, -1):
            if loc_comps[-k:] == below[:k]:
                tail = below[k:]
                host = "/" + "/".join(loc_comps)
                return f"{host}/{'/'.join(tail)}" if tail else host
    return None


def _extract_file_paths(item: Mapping[str, object]) -> list[str]:
    """Every ``Media[].Part[].file`` path Plex reports for one item (HOST namespace).

    A movie item carries one (rarely more, e.g. a multi-part edition) ``Media``
    entry; an episode item (fetched with ``type=4``, see
    :meth:`PlexLibrary.confirm_paths`) carries its own. Absent/malformed entries
    are skipped rather than raising -- a single odd item must never abort the
    whole crawl.
    """
    paths: list[str] = []
    for media in _as_sequence(item.get("Media")):
        for part in _as_sequence(_as_mapping(media).get("Part")):
            file_path = _get_str(_as_mapping(part), "file")
            if file_path is not None:
                paths.append(file_path)
    return paths


def _section_scan_path(section: LibrarySection, path: str) -> str | None:
    """The Plex(host)-namespace path to refresh for ``path`` in ``section``, or None.

    Prefers a direct prefix match (no host/container split, or Plex itself sees the
    container paths) so ``path`` is refreshed verbatim; otherwise reverses the
    Docker remap via :func:`_container_to_host_scan_path`. ``None`` when this
    section does not cover ``path`` at all.
    """
    for location in section.locations:
        if _is_path_prefix(location, path):
            return path
        host = _container_to_host_scan_path(location, path)
        if host is not None:
            return host
    return None


class _LazySectionIndex:
    """The same-type sections, in :meth:`PlexLibrary.list_sections` order, paged
    ON DEMAND into a memoized tmdb-id -> items index.

    Request/failure parity is the whole point (the two codex P2s on #306): the
    batch path must make exactly the requests the per-candidate
    :meth:`PlexLibrary.watch_state` helpers would have made for the same queries
    -- never more -- so a failing section (or page) the per-candidate path never
    reached can never abort a batch of otherwise-resolvable candidates. Eagerly
    indexing sections up front broke that in two rounds: first for sections no
    path-correlated query covered, then for sections AFTER an uncorrelated
    (``library_path=None``) query's first match, which the old first-match early
    return never touched.

    * :meth:`find_first` mirrors :meth:`PlexLibrary._find_section_item`: pages one
      section only as far as the FIRST ``tmdb_id`` match, so an uncorrelated query
      resolved by an early section/page never demands a later one.
    * :meth:`ensure_complete` mirrors :meth:`PlexLibrary._find_section_items`: the
      whole-section view a path-correlated query needs (every duplicate matters
      for the issue #207/#239 correlation).

    Every fetched page is shared across the whole batch (the issue #213 win): a
    page is requested at most once, and only when some query being resolved
    actually demands it -- the union of what N sequential per-candidate calls
    would have fetched, deduplicated. Errors are NOT memoized: a failing fetch
    propagates immediately, aborting the batch exactly as the per-candidate path
    aborted the sweep when a query genuinely needed the failing section.
    """

    def __init__(
        self,
        sections: Sequence[LibrarySection],
        fetch_page: Callable[[str, int], Awaitable[Sequence[Mapping[str, object]]]],
    ) -> None:
        self._fetch_page = fetch_page
        self._sections = tuple(sections)
        self._by_tmdb_id: list[dict[int, list[Mapping[str, object]]]] = [{} for _ in self._sections]
        self._next_start = [0] * len(self._sections)
        self._exhausted = [False] * len(self._sections)

    @property
    def sections(self) -> tuple[LibrarySection, ...]:
        return self._sections

    async def _page_once(self, index: int) -> None:
        """Fetch section ``index``'s next page and fold it into the memoized index.

        An item is indexed under EVERY tmdb id its guid(s) resolve to
        (:func:`_collect_item_tmdb_ids`), matching :func:`_item_matches_tmdb_id`;
        page-order append keeps ``[0]`` the first match in crawl order.
        """
        items = await self._fetch_page(self._sections[index].key, self._next_start[index])
        by_tmdb_id = self._by_tmdb_id[index]
        for entry in items:
            tmdb_ids: set[int] = set()
            _collect_item_tmdb_ids(entry, tmdb_ids)
            for tmdb_id in tmdb_ids:
                by_tmdb_id.setdefault(tmdb_id, []).append(entry)
        self._next_start[index] += _PAGE_SIZE
        if len(items) < _PAGE_SIZE:
            self._exhausted[index] = True

    async def find_first(self, index: int, tmdb_id: int) -> Mapping[str, object] | None:
        """Section ``index``'s first ``tmdb_id``-matching item in page order,
        paging only as far as that match -- byte-for-byte
        :meth:`PlexLibrary._find_section_item` (``None`` once exhausted unmatched).
        """
        while True:
            matches = self._by_tmdb_id[index].get(tmdb_id)
            if matches:
                return matches[0]
            if self._exhausted[index]:
                return None
            await self._page_once(index)

    async def ensure_complete(self, index: int) -> dict[int, list[Mapping[str, object]]]:
        """Section ``index`` fully paged (memoized) -- the whole-section view
        :meth:`PlexLibrary._find_section_items` gives a path-correlated query."""
        while not self._exhausted[index]:
            await self._page_once(index)
        return self._by_tmdb_id[index]


def _find_season_entry_in(
    entries: Sequence[Mapping[str, object]], season: int
) -> Mapping[str, object] | None:
    """The season row matching ``season`` among an already-fetched ``/children`` list.

    The in-memory half of :meth:`PlexLibrary._find_season_entry`: it does the same
    ``index == season`` scan, but over season rows a batch already read once and
    memoized, so several seasons of one show never re-fetch its ``/children``
    (issue #213).
    """
    for entry in entries:
        if _get_int(entry, "index") == season:
            return entry
    return None


class PlexLibrary:
    """Query availability, trigger scans and list sections. Implements ``LibraryPort``.

    The token is held privately, sent only in the ``X-Plex-Token`` header, and
    redacted from ``repr`` so it cannot leak into logs.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._client = client
        try:
            self._service_url = ServiceUrl.parse(base_url)
        except InvalidServiceUrl as exc:
            raise PlexLibraryError("Plex service URL is invalid") from exc
        self._base_url = self._service_url.base
        self._token = token
        # Cache key = server + a hash of the token, so a different credential for the
        # same URL never reads back another token's cached sections (the raw token is
        # never put in the key — north-star: secrets are never logged or persisted).
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._cache_key = f"{self._base_url}|{token_hash}"

    def __repr__(self) -> str:  # pragma: no cover - trivial, redacts the token
        return f"PlexLibrary(base_url={self._base_url!r}, token='***')"

    async def _request(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET ``path`` with the token header; raise typed errors on failure.

        The token is added here and never logged. A transport failure, a 401/403,
        or any other non-2xx is converted to a surfaced error built from ``path``
        and the status only — httpx's own error embeds the URL, so it must never
        escape. JSON is NOT decoded here (refresh returns an empty body).
        """
        if header_value_error(self._token) is not None:
            # A stored token that cannot ride the ``X-Plex-Token`` header: a
            # CR/LF/NUL value would make httpx echo the RAW token in ``str(exc)``
            # (a credential leak through a chained transport error), and a non-ASCII
            # value would raise an uncaught ``UnicodeEncodeError`` (a 500). Fail as a
            # surfaced ``PlexAuthError`` -- the token is not a usable credential --
            # WITHOUT ever placing it in a header. Defense-in-depth for a token that
            # bypassed the write-time header-safety check (a ``dev_auth_bypass``
            # install, or a legacy row); the ``oauth.py`` adapter guards its own
            # plex.tv/identity sinks the same way (``_require_header_safe_token``).
            raise PlexAuthError(
                "Plex rejected the request: the stored token is not a valid credential value"
            )
        request_headers: dict[str, str] = {
            "X-Plex-Token": self._token,
            "Accept": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)
        try:
            url = self._service_url.endpoint(path)
            response = await self._client.get(
                url,
                params=params,
                headers=request_headers,
                follow_redirects=False,
            )
        except InvalidServiceUrl as exc:
            raise PlexLibraryError("plex request path is invalid") from exc
        except httpx.RequestError as exc:
            # Plex unreachable (DNS / connection refused / timeout): httpx raises
            # before any status check, so without this it would propagate as an
            # opaque 500. Convert to the surfaced, retryable PlexLibraryError —
            # the message names the path only, never the url or token.
            raise PlexLibraryError(f"plex request to {path} failed") from exc
        status = response.status_code
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise PlexAuthError(
                f"Plex rejected the request to {path} (HTTP {status}): "
                "the token is missing or invalid"
            )
        # Checks the full 2xx range explicitly rather than ``httpx.Response.is_error``
        # (issue #87): ``is_error`` is only true for >=400, so a 3xx redirect (e.g. a
        # proxy/auth redirect in front of Plex) would read as success even though the
        # requested scan/query never actually ran.
        if not (_HTTP_OK <= status < _HTTP_MULTIPLE_CHOICES):
            raise PlexLibraryError(f"Plex request to {path} failed (HTTP {status})")
        return response

    async def _get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        """:meth:`_request` plus JSON decoding (non-JSON 200 -> ``PlexLibraryError``)."""
        response = await self._request(path, params, headers=headers)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlexLibraryError(f"Plex returned a non-JSON body for {path}") from exc
        return _as_mapping(payload)

    async def _stream_artwork(self, path: str) -> tuple[bytes, str] | None:
        """Stream one Plex-owned image path into a complete bounded body."""
        if header_value_error(self._token) is not None:
            raise PlexAuthError(
                "Plex rejected the request: the stored token is not a valid credential value"
            )
        request_headers = {
            "X-Plex-Token": self._token,
            "Accept": "image/*,*/*;q=0.8",
            "Accept-Encoding": "identity",
        }
        try:
            url = self._service_url.endpoint(path)
            async with self._client.stream(
                "GET", url, headers=request_headers, follow_redirects=False
            ) as response:
                response_status = response.status_code
                if response_status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
                    raise PlexAuthError(
                        f"Plex rejected the request to {path} (HTTP {response_status}): "
                        "the token is missing or invalid"
                    )
                if not (_HTTP_OK <= response_status < _HTTP_MULTIPLE_CHOICES):
                    raise PlexLibraryError(
                        f"Plex request to {path} failed (HTTP {response_status})"
                    )

                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if not content_type.startswith(_IMAGE_CONTENT_TYPE_PREFIX):
                    return None

                content_encoding = response.headers.get("content-encoding")
                if content_encoding is not None and content_encoding.strip().lower() != "identity":
                    return None

                raw_content_length = response.headers.get("content-length")
                if raw_content_length is not None:
                    try:
                        declared_length = int(raw_content_length)
                    except ValueError:
                        declared_length = -1
                    if declared_length > _MAX_ARTWORK_BYTES:
                        return None

                content = bytearray()
                async for chunk in response.aiter_raw():
                    if len(chunk) > _MAX_ARTWORK_BYTES - len(content):
                        return None
                    content.extend(chunk)
                return bytes(content), content_type
        except InvalidServiceUrl as exc:
            raise PlexLibraryError("plex request path is invalid") from exc
        except httpx.RequestError as exc:
            raise PlexLibraryError(f"plex request to {path} failed") from exc

    async def list_sections(self, *, use_cache: bool = True) -> list[LibrarySection]:
        """Return the configured library sections (movie / show), cached briefly.

        ``use_cache=False`` skips the cache READ and re-pages Plex live -- the
        health/"Test connection" probe (``setup_validation.validate_plex``) needs
        this so an outage or a rejected token is reflected immediately rather than
        served a stale "ok" from a healthy probe up to ``_CACHE_TTL_SECONDS``
        (300s) earlier. A successful live call still SETS the cache below (never
        just bypasses it), so the availability fast paths (``is_available``,
        ``present_seasons``, ...) stay warm for the request-serving path that
        follows a health check.
        """
        if use_cache:
            cached = _SECTIONS_CACHE.get(self._cache_key)
            if cached is not None:
                return list(cached)
        payload = await self._get("/library/sections")
        container = _media_container(payload)
        sections: list[LibrarySection] = []
        for entry in _as_sequence(container.get("Directory")):
            section = _parse_section(_as_mapping(entry))
            if section is not None:
                sections.append(section)
        # Cache only a list that CONTAINS a movie section. A no-movie result is a
        # self-healing negative: validate_plex reports ok=False and tells the operator
        # to add a Movie library and test again, so caching the empty/show-only
        # snapshot for the full TTL would make the immediate re-test read stale data
        # and leave setup wrongly blocked. Mirrors is_available's presence/absence
        # asymmetry — cache a positive, never a negative the operator can fix.
        #
        # But a PRIOR movie-bearing result may already be cached from an
        # earlier page — if THIS page (esp. a live ``use_cache=False`` probe)
        # finds no movie section, that old positive must not be left sitting
        # in the cache: the movie library could genuinely have been removed
        # from Plex, and default (``use_cache=True``) callers -- the scan path,
        # the availability fast paths -- would otherwise keep being handed the
        # now-gone location for up to the full TTL after a live probe already
        # saw it disappear. Invalidate rather than merely skip the ``set``, so
        # the very next default call re-pages instead of serving a stale
        # positive. (The Settings folder picker itself no longer relies on this
        # invalidation for FRESHNESS -- ``plex_libraries_endpoint`` now always
        # passes ``use_cache=False`` [issue #15: a 2nd movie section added in
        # Plex must appear immediately, not after up to 300s] -- but this call
        # still WARMS the cache for the fast paths that follow.)
        if any(section.type == "movie" for section in sections):
            _SECTIONS_CACHE.set(self._cache_key, tuple(sections))
        else:
            _SECTIONS_CACHE.invalidate(self._cache_key)
        return sections

    async def is_available(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        use_cache: bool = True,
        season: int | None = None,
    ) -> bool:
        """Whether ``tmdb_id`` is already present in the library.

        For movies, "present" means the tmdb id shows up in some movie section.
        For TV (``media_type='tv'``), presence is evaluated per-show: with
        ``season=None`` it means the show itself is in the library; with
        ``season=N`` it means that season has at least one episode present
        (``leafCount>0`` on Plex's season metadata). Per-episode completeness is a
        deferred follow-up — a season with a single episode present already reads
        as "available".

        The availability reconcile cycle keeps the cached-presence fast path
        (``use_cache=True``); the request-dedup path passes ``use_cache=False`` so a
        title just REMOVED from Plex is seen as absent immediately and a re-request
        is not blocked by a stale "present" answer for the cache TTL (G7).
        """
        if media_type == "tv":
            return await self._is_tv_available(tmdb_id, season, use_cache=use_cache)
        # Trust a cached PRESENCE (a movie in the library stays there) but never a
        # cached ABSENCE: right after an import+scan the first page commonly precedes
        # Plex indexing, so caching that miss would keep the title "Finalizing" for
        # the whole TTL. On a cache miss OR a cached-absent answer, re-page Plex.
        # ``use_cache=False`` skips even a cached PRESENT: a dedup decision must reflect
        # the library as it is NOW, not a pre-removal snapshot. The re-page still
        # refreshes the cache below, so the reconcile cycle also sees the removal.
        if use_cache:
            cached = _PRESENT_TMDB_CACHE.get(self._cache_key)
            if cached is not None and tmdb_id in cached:
                return True
        present = await self._collect_present_tmdb_ids()
        _PRESENT_TMDB_CACHE.set(self._cache_key, present)
        return tmdb_id in present

    async def _is_tv_available(self, tmdb_id: int, season: int | None, *, use_cache: bool) -> bool:
        """The tv branch of :meth:`is_available` — see its docstring for semantics."""

        def _satisfied(seasons_by_show: Mapping[int, frozenset[int]]) -> bool | None:
            """``True``/``False`` if ``seasons_by_show`` answers the question, else
            ``None`` (show absent from this snapshot — the caller must re-page rather
            than trust the absence)."""
            seasons = seasons_by_show.get(tmdb_id)
            if seasons is None:
                return None
            return True if season is None else season in seasons

        if use_cache:
            cached = _TV_SEASONS_CACHE.get(self._cache_key)
            if cached is not None:
                verdict = _satisfied(cached)
                if verdict is True:
                    return True
        present = await self._collect_present_tv_seasons()
        _TV_SEASONS_CACHE.set(self._cache_key, present)
        return bool(_satisfied(present))

    async def _collect_present_tmdb_ids(self) -> frozenset[int]:
        """Page every movie section and gather the tmdb ids of its items."""
        present: set[int] = set()
        for section in await self.list_sections():
            if section.type != "movie":
                continue
            await self._collect_section_tmdb_ids(section.key, present)
        return frozenset(present)

    async def _collect_section_tmdb_ids(self, key: str, present: set[int]) -> None:
        """Walk one section's items page-by-page, accumulating tmdb ids."""
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                _collect_item_tmdb_ids(_as_mapping(item), present)
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE

    async def _collect_section_file_paths(
        self, key: str, paths: list[str], *, extra_params: Mapping[str, str] | None = None
    ) -> None:
        """Walk one section's items page-by-page, collecting every file path.

        ``extra_params`` scopes the query -- :meth:`confirm_paths` passes
        ``{"type": "4"}`` on a SHOW section so this crawls EVERY episode ("leaf")
        across the whole section directly, in ONE flat paged walk (Plex's numeric
        type filter: 1=movie, 2=show, 3=season, 4=episode) -- never one
        ``/children`` fetch per show. Left unset (movie sections), the section's
        own ``/all`` already lists movie items directly.
        """
        start = 0
        params: dict[str, str] = dict(extra_params or {})
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                params,
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                paths.extend(_extract_file_paths(_as_mapping(item)))
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE

    async def confirm_paths(
        self,
        media_type: Literal["movie", "tv"],
        library_paths: Collection[str],
    ) -> frozenset[str]:
        """See :meth:`LibraryPort.confirm_paths`.

        ONE crawl of every candidate (movie or show) section for the WHOLE call,
        regardless of how many ``library_paths`` are queried -- TV episodes are
        fetched directly via the section's flat ``type=4`` listing (never a
        per-show ``/children``/``allLeaves`` fetch), so the cost model matches
        :meth:`present_ids`'s "one crawl, not one per row". Each queried path is
        confirmed by reversing the section's HOST-namespace location the same way
        :meth:`trigger_scan` does (:func:`_section_scan_path`) and checking whether
        any crawled file path sits at/under that reversed directory
        (:func:`_is_path_prefix`) -- directory-prefix, never title/year.
        """
        wanted = frozenset(p for p in library_paths if p)
        if not wanted:
            return frozenset()
        section_type: Literal["movie", "show"] = "movie" if media_type == "movie" else "show"
        candidate_sections = [s for s in await self.list_sections() if s.type == section_type]
        if not candidate_sections:
            return frozenset()
        file_paths: list[str] = []
        # Episodes only exist as their own leaf rows (type=4) on a SHOW section --
        # the section's default ``/all`` lists shows, not episodes.
        extra_params = {"type": "4"} if media_type == "tv" else None
        for section in candidate_sections:
            await self._collect_section_file_paths(
                section.key, file_paths, extra_params=extra_params
            )
        confirmed: set[str] = set()
        for library_path in wanted:
            for section in candidate_sections:
                scan_path = _section_scan_path(section, library_path)
                if scan_path is not None and any(
                    _is_path_prefix(scan_path, fp) for fp in file_paths
                ):
                    confirmed.add(library_path)
                    break
        return frozenset(confirmed)

    async def _collect_present_show_ids(self) -> frozenset[int]:
        """Page every SHOW section and gather the tmdb ids of its shows (guid-only).

        The show-level mirror of :meth:`_collect_present_tmdb_ids` — reuses the same
        section-type-agnostic ``_collect_section_tmdb_ids`` pager (one
        ``/all?includeGuids=1`` walk per section), so a show's presence costs a
        single guid read and NEVER the per-show ``/children`` fetch
        ``_collect_present_tv_seasons`` pays for per-season granularity. Deliberately
        cheaper than the season crawl because tiles only need "is the show here", not
        which seasons.
        """
        present: set[int] = set()
        for section in await self.list_sections():
            if section.type != "show":
                continue
            await self._collect_section_tmdb_ids(section.key, present)
        return frozenset(present)

    async def present_ids(
        self,
        keys: Sequence[tuple[int, Literal["movie", "tv"]]],
        *,
        refresh_absent: bool = False,
    ) -> frozenset[tuple[int, Literal["movie", "tv"]]]:
        """The batch presence accessor — see :meth:`LibraryPort.present_ids`.

        Partitions ``keys`` by media type and answers each partition from its OWN
        full-crawl snapshot: movies from ``_PRESENT_TMDB_CACHE`` (one movie-section
        crawl), TV shows from ``_PRESENT_SHOW_TMDB_CACHE`` (one show-section guid
        crawl, no per-show ``/children``). A section type is crawled ONLY when a key
        of that type is present, so a movie-only page never touches the show sections
        (and vice versa).

        With ``refresh_absent=False`` (tile decoration's default): cached-presence-
        only, like the other tile-facing reads — a warmed snapshot is trusted as-is
        (tiles tolerate the short TTL); a miss pages Plex once and warms the cache
        for the next page-load.

        With ``refresh_absent=True`` (the availability reconcile cycle): a warmed
        snapshot is trusted ONLY if it already confirms every requested id of that
        media type as present; otherwise one fresh crawl runs before answering —
        still at most one crawl per media type, never one per key. This mirrors
        ``is_available``'s "trust cached presence, never cached absence" contract:
        a partial scan's cache invalidation (``trigger_scan``) fires before Plex
        finishes indexing, so the very next crawl can cache a still-pending title as
        absent; without this, that stale absence would be trusted for the rest of
        the TTL instead of self-correcting on the next reconcile tick.
        """
        movie_ids = {tmdb_id for tmdb_id, media_type in keys if media_type == "movie"}
        show_ids = {tmdb_id for tmdb_id, media_type in keys if media_type == "tv"}
        present_movies: frozenset[int] = frozenset()
        if movie_ids:
            cached_movies = _PRESENT_TMDB_CACHE.get(self._cache_key)
            if cached_movies is None or (refresh_absent and not movie_ids.issubset(cached_movies)):
                cached_movies = await self._collect_present_tmdb_ids()
                _PRESENT_TMDB_CACHE.set(self._cache_key, cached_movies)
            present_movies = cached_movies
        present_shows: frozenset[int] = frozenset()
        if show_ids:
            cached_shows = _PRESENT_SHOW_TMDB_CACHE.get(self._cache_key)
            if cached_shows is None or (refresh_absent and not show_ids.issubset(cached_shows)):
                cached_shows = await self._collect_present_show_ids()
                _PRESENT_SHOW_TMDB_CACHE.set(self._cache_key, cached_shows)
            present_shows = cached_shows
        result: set[tuple[int, Literal["movie", "tv"]]] = set()
        for tmdb_id, media_type in keys:
            if (media_type == "movie" and tmdb_id in present_movies) or (
                media_type == "tv" and tmdb_id in present_shows
            ):
                result.add((tmdb_id, media_type))
        return frozenset(result)

    async def present_seasons(self, tmdb_id: int) -> frozenset[int]:
        """The seasons already present for ``tmdb_id`` — resolved in ONE fresh crawl.

        A season is "present" when its ``leafCount>0`` (>=1 episode indexed), the
        same per-season granularity as :meth:`is_available` with a ``season``. This
        exists alongside ``is_available`` so ``ensure_seasons`` can resolve EVERY
        requested season of a show from a single library read: calling
        ``is_available(season=n, use_cache=False)`` once per season would re-page the
        whole library N times (and hold the request's write transaction open across
        all N). Always re-pages (never trusts a cached absence, mirroring
        ``use_cache=False``) and refreshes the shared snapshot so a later cache read
        stays consistent. Empty when the show is absent or has no indexed season.
        """
        present = await self._collect_present_tv_seasons()
        _TV_SEASONS_CACHE.set(self._cache_key, present)
        return present.get(tmdb_id, frozenset())

    async def season_presence(self, tmdb_ids: Collection[int]) -> Mapping[int, frozenset[int]]:
        """The seasons present for EVERY show in ``tmdb_ids`` — ONE targeted
        page-walk, not a per-show library crawl.

        Walks each show section's ``/all`` listing EXACTLY ONCE (never once per
        requested id), recording the ``ratingKey`` of every item whose guid(s)
        resolve to a requested tmdb id — see ``_collect_target_rating_keys``. A
        given tmdb id can match MORE than one item (the same show catalogued in
        two show sections, e.g. a separate "TV Shows" and "Anime" library, or a
        duplicate entry within one section), so every matched item's ``/children``
        is fetched and the resulting season sets are UNIONed per tmdb id — using
        only the first match would under-report a season only present on a later
        duplicate. Cost model: one page-walk across all show sections, plus one
        ``/children`` fetch per MATCHED item (>= the number of requested ids that
        are actually present, never more sections walked). Exists so the
        availability reconcile cycle (``import_service.run_availability_cycle``)
        can resolve every distinct pending show in a tick from a single
        whole-library page-walk, instead of paying one page-walk per show.

        The SECTION page-walk itself is all-or-nothing: a failure walking a show
        section's ``/all`` listing is a genuine whole-pass transport failure and
        is allowed to propagate (``PlexLibraryError``/``PlexAuthError``) — the
        caller's whole-pass try/except handles that. But each MATCHED show's own
        ``/children`` union (see ``_fetch_present_seasons``) is isolated in its
        own try/except (round 4, #136 review): one show's metadata row being
        deleted between the page-walk and this lookup, or persistently
        returning a 404/500, must not abort every OTHER pending show's lookup in
        the same batch. On a per-show failure, a warning is logged naming the
        tmdb id and that id is OMITTED FROM THE RETURNED MAPPING ENTIRELY —
        never mapped to an empty frozenset, which would dishonestly claim "no
        seasons present" for a show whose presence is actually unknown. The
        caller (``run_availability_cycle``) must treat a missing key as
        "retry next cycle", not "not yet available".

        Always re-pages fresh (never trusts a cached absence, mirroring
        ``present_seasons``): a season that just finished indexing must be seen
        immediately, not held stale for the cache TTL. Write-through: every id
        that MATCHED at least one item AND resolved successfully is merged into
        the shared ``_TV_SEASONS_CACHE`` snapshot (creating one if none is warm
        yet) so a subsequent ``is_available``/``present_seasons`` call for the
        SAME show inside this TTL window sees this fresh read rather than a
        stale one -- a show NOT among the merged ids simply misses the cache and
        triggers its own full re-crawl, same as today. A requested id with NO
        matching item anywhere maps to an empty frozenset in the return value
        (still present as a key — never omitted) but is deliberately NOT written
        to the cache: ``_is_tv_available`` treats a cached key as "show present"
        for whole-show checks, so caching the miss would report a
        never-indexed show as available for the rest of the TTL. A requested id
        whose lookup FAILED touches the cache in NEITHER direction: it DID match
        a rating key, so it is never added to the unmatched/eviction set (its
        state is unknown, not absent) and nothing is written for it either.
        """
        wanted = set(tmdb_ids)
        if not wanted:
            return {}
        rating_keys_by_tmdb_id: dict[int, list[str]] = {}
        for section in await self.list_sections():
            if section.type != "show":
                continue
            await self._collect_target_rating_keys(section.key, wanted, rating_keys_by_tmdb_id)
        result: dict[int, frozenset[int]] = {}
        failed_ids: set[int] = set()
        incomplete_ids: set[int] = set()
        for tmdb_id in wanted:
            seasons: set[int] = set()
            any_failed = False
            for rating_key in rating_keys_by_tmdb_id.get(tmdb_id, []):
                try:
                    seasons |= await self._fetch_present_seasons(rating_key)
                except (PlexLibraryError, PlexAuthError) as exc:
                    _logger.warning(
                        "season lookup failed for a show entry tmdb_id=%s (%s)",
                        safe_int(tmdb_id),
                        exc,
                    )
                    any_failed = True
                    # Keep going: a LATER duplicate entry may still confirm
                    # seasons — positive evidence must not be discarded because
                    # a stale/broken duplicate errored first (or vice versa).
            if any_failed and not seasons:
                # No positive evidence at all — state unknown, omit the id so
                # the caller retries next cycle.
                failed_ids.add(tmdb_id)
                continue
            if any_failed:
                # PARTIAL union: some duplicate(s) failed but at least one
                # confirmed seasons. Confirmed presence is sound to promote on
                # (a season Plex reports IS present), so it goes in the RETURN
                # value — but the union may be incomplete, so it must never be
                # written through to the cache snapshot.
                incomplete_ids.add(tmdb_id)
            result[tmdb_id] = frozenset(seasons)
        # ONLY ids that matched at least one item (and resolved successfully) are
        # merged into the cache: ``_is_tv_available`` treats a cached KEY as "show
        # present" for whole-show checks, so a no-match id must never be WRITTEN
        # as an empty present key (a never-indexed show would answer True within
        # the TTL) -- and, symmetrically, a fresh no-match read must EVICT any
        # stale cached entry for that id (a show REMOVED from Plex since the
        # snapshot warmed would otherwise keep answering True from the old entry
        # for the rest of the TTL). The RETURN value still carries the empty
        # frozenset for a no-match id — only the cache treats it as an eviction.
        # A FAILED id is excluded from both ``matched_only`` and ``unmatched``: it
        # is not evicted (it DID match a rating key -- its state is unknown, not
        # absent) and nothing about it is written to the cache either. An
        # INCOMPLETE id (partial duplicate failure, positive union returned) is
        # likewise returned to the caller but never cached -- its union may be
        # missing seasons that only lived on the failed duplicate.
        matched_only = {
            tmdb_id: seasons
            for tmdb_id, seasons in result.items()
            if rating_keys_by_tmdb_id.get(tmdb_id) and tmdb_id not in incomplete_ids
        }
        unmatched = wanted - matched_only.keys() - failed_ids - incomplete_ids
        cached = _TV_SEASONS_CACHE.get(self._cache_key)
        if matched_only or (cached is not None and unmatched & cached.keys()):
            updated = dict(cached) if cached is not None else {}
            for tmdb_id in unmatched:
                updated.pop(tmdb_id, None)
            updated.update(matched_only)
            _TV_SEASONS_CACHE.set(self._cache_key, updated)
        return result

    async def _collect_target_rating_keys(
        self, key: str, wanted: set[int], rating_keys_by_tmdb_id: dict[int, list[str]]
    ) -> None:
        """Walk one show section's items page-by-page, recording the ``ratingKey``
        of every item whose guid(s) resolve to a tmdb id in ``wanted``.

        A single item can only match one requested tmdb id per its own guid(s), but
        a requested id can accumulate MULTIPLE rating keys across calls (this
        section holding a duplicate entry, or a later section holding the same
        show) — see :meth:`season_presence` on why every match's seasons must be
        unioned rather than only the first.
        """
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                entry = _as_mapping(item)
                ids: set[int] = set()
                _collect_item_tmdb_ids(entry, ids)
                matched_ids = ids & wanted
                if not matched_ids:
                    continue
                rating_key = _get_str(entry, "ratingKey")
                if rating_key is None:
                    continue
                for tmdb_id in matched_ids:
                    rating_keys_by_tmdb_id.setdefault(tmdb_id, []).append(rating_key)
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE

    async def _collect_present_tv_seasons(self) -> dict[int, frozenset[int]]:
        """Page every show section and gather each show's present seasons.

        Returns tmdb id -> the frozenset of season numbers with ``leafCount>0``
        (verified against ``/library/metadata/{ratingKey}/children``, which mirrors
        overseerr's ``server/api/plexapi.ts:217-223``: season rows carry ``index``
        (the season number, 0 for specials) and ``leafCount`` (episode count)). A
        show with no ``ratingKey`` or no resolvable tmdb id is skipped — it can
        never be matched by a request anyway.
        """
        result: dict[int, frozenset[int]] = {}
        for section in await self.list_sections():
            if section.type != "show":
                continue
            await self._collect_section_tv_seasons(section.key, result)
        return result

    async def _collect_section_tv_seasons(
        self, key: str, result: dict[int, frozenset[int]]
    ) -> None:
        """Walk one show section's items page-by-page, resolving each show's seasons."""
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                await self._collect_show_seasons(_as_mapping(item), result)
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE

    async def _collect_show_seasons(
        self, item: Mapping[str, object], result: dict[int, frozenset[int]]
    ) -> None:
        """Resolve one show item's tmdb id(s) and merge in its present seasons."""
        tmdb_ids: set[int] = set()
        _collect_item_tmdb_ids(item, tmdb_ids)
        if not tmdb_ids:
            return
        rating_key = _get_str(item, "ratingKey")
        if rating_key is None:
            return
        seasons = await self._fetch_present_seasons(rating_key)
        for tmdb_id in tmdb_ids:
            result[tmdb_id] = result.get(tmdb_id, frozenset()) | seasons

    async def _fetch_present_seasons(self, rating_key: str) -> frozenset[int]:
        """Return the season numbers with ``leafCount>0`` for one show's ``ratingKey``."""
        payload = await self._get(f"/library/metadata/{rating_key}/children")
        items = _as_sequence(_media_container(payload).get("Metadata"))
        present: set[int] = set()
        for item in items:
            entry = _as_mapping(item)
            index = _get_int(entry, "index")
            leaf_count = _get_int(entry, "leafCount")
            if index is not None and leaf_count is not None and leaf_count > 0:
                present.add(index)
        return frozenset(present)

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        """Ask Plex to scan ``path`` (a targeted partial-scan on the owning section).

        ``media_type`` scopes the candidate sections to movie sections for movies
        and show sections for TV, so a TV season folder is never matched against a
        movie section (or vice versa).

        ``path`` arrives in the CONTAINER namespace (the importer/eviction placed
        into a container-visible root), while Plex reports its section locations in
        the HOST namespace, so after a Docker host/container split the two never
        prefix-match directly. :func:`_section_scan_path` reverses that remap from
        the section's own locations + the same suffix logic
        :mod:`~plex_manager.services.path_visibility` uses forward, translating the
        container path back to the HOST path Plex actually knows -- so a targeted
        partial refresh of just that path still works instead of a full-library
        refresh. If NO section covers it (a genuine path-mapping difference, a
        mount-root remap with no shared directory to anchor on, or Plex not
        reporting locations), we do a real FULL refresh of each candidate section
        instead — heavier, but it actually indexes the new file, unlike refreshing
        with a path Plex does not own (a silent no-op that would strand the request
        at "Finalizing"). With no candidate section at all, raise so the import
        blocks honestly. A 2xx (possibly empty body) is success. After scanning, the
        presence cache for ``media_type`` is invalidated so the availability check
        re-pages Plex instead of returning a pre-import snapshot.
        """
        section_type: Literal["movie", "show"] = "movie" if media_type == "movie" else "show"
        candidate_sections = [s for s in await self.list_sections() if s.type == section_type]
        if not candidate_sections:
            raise PlexLibraryError(f"no Plex {section_type} library section to scan into")
        matched = [
            (section, scan_path)
            for section in candidate_sections
            if (scan_path := _section_scan_path(section, path)) is not None
        ]
        try:
            if matched:
                # The reverse-mapped HOST path is handed to httpx as a query param so
                # it is percent-encoded exactly once (pre-quoting here would double-
                # encode).
                for section, scan_path in matched:
                    await self._request(
                        f"/library/sections/{section.key}/refresh", {"path": scan_path}
                    )
            else:
                _logger.warning(
                    "import path is not under any Plex %s section location; "
                    "full-scanning every %s section instead of a no-op partial scan",
                    section_type,
                    section_type,
                )
                for section in candidate_sections:
                    await self._request(f"/library/sections/{section.key}/refresh", {})
        finally:
            # Bust the per-credential presence index for THIS media type so
            # completed -> available promotion is not delayed up to the full cache
            # TTL after the file is in Plex. Scoped to the type just scanned — a
            # movie scan cannot change TV season presence and vice versa.
            if media_type == "movie":
                _PRESENT_TMDB_CACHE.invalidate(self._cache_key)
            else:
                _TV_SEASONS_CACHE.invalidate(self._cache_key)
                # Also drop the show-level tile-presence snapshot: a just-imported
                # NEW show must promote to "available" on Discover tiles without
                # waiting the full TTL, exactly as the season cache above.
                _PRESENT_SHOW_TMDB_CACHE.invalidate(self._cache_key)

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        """Whether ``tmdb_id`` (optionally one TV season) has been watched.

        Deliberately UNCACHED (unlike ``is_available``/``present_seasons``): the
        disk-pressure eviction sweep that is this method's only caller runs
        infrequently (its own web-editable interval, default 30 minutes) against a
        small candidate set, so the extra request cost buys always-fresh data --
        a stale "unwatched" would just delay an eviction, but a stale "watched"
        held past a real rewatch could delete content the operator is actively
        rewatching, which is the one direction this method must never be wrong in.

        ``media_type='movie'``: crawls every movie section for the item whose
        guid(s) match ``tmdb_id`` (same GUID matching ``is_available`` uses) and
        reads its ``viewCount``/``lastViewedAt``. ``media_type='tv'`` REQUIRES
        ``season`` (raises ``ValueError`` otherwise -- eviction is always
        per-season, never whole-show): crawls show sections for the matching
        show, then that show's ``/children`` for the season row matching
        ``season``, and reads ``viewedLeafCount``/``leafCount``/``lastViewedAt``.

        An item/show/season absent from the library reports
        ``watched=False, last_viewed_at=None`` honestly rather than raising -- it
        can never be an eviction candidate anyway.

        ``library_path`` (issue #207) path-correlates the read: when given, EVERY
        ``tmdb_id``-matching item across every section is collected (not just the
        first), and only the ones whose reported media file path sits at/under the
        reverse-mapped ``library_path`` (:func:`_section_scan_path` +
        :func:`_is_path_prefix`, the same machinery :meth:`confirm_paths` uses) are
        read. Zero correlated items still FAILS CLOSED (see the port docstring).
        More than one correlated item is resolved by
        :func:`_resolve_correlated_watch_state` (issue #239): hits that all report
        the IDENTICAL underlying media-file path(s) are the SAME physical copy
        merely indexed by more than one Plex section (e.g. a broad ``/media``
        section plus a nested ``/media/anime`` section covering the same files)
        and are merged into one logical item -- watched if ANY hit is watched, at
        the NEWEST ``lastViewedAt`` across ALL hits (issue #290: including a
        not-yet-watched / mid-rewatch section, whose recent in-progress view must
        win over another section's stale fully-watched timestamp -- deletion
        safety: a stale timestamp must never make the item look
        grace-window-eligible while it is being actively rewatched).
        Hits whose file paths genuinely DIFFER remain ambiguous and still FAIL CLOSED, exactly as
        before -- a legitimate duplicate (distinct copies on disk) must never let
        one copy's watched state authorize deleting the other. The TV branch pays
        one extra per-season ``/children`` (episode) fetch per candidate show to
        read the season's own episode file paths.
        """
        if media_type == "tv":
            if season is None:
                raise ValueError("watch_state requires a season for media_type='tv'")
            return await self._tv_watch_state(tmdb_id, season, library_path)
        return await self._movie_watch_state(tmdb_id, library_path)

    async def _movie_watch_state(self, tmdb_id: int, library_path: str | None) -> WatchState:
        """The movie branch of :meth:`watch_state` -- see its docstring."""
        if library_path is None:
            for section in await self.list_sections():
                if section.type != "movie":
                    continue
                item = await self._find_section_item(section.key, tmdb_id)
                if item is not None:
                    return _movie_watch_state_from_item(item)
            return WatchState(watched=False, last_viewed_at=None)

        hits: list[tuple[frozenset[str], WatchState]] = []
        for section in await self.list_sections():
            if section.type != "movie":
                continue
            scan_path = _section_scan_path(section, library_path)
            if scan_path is None:
                continue
            for item in await self._find_section_items(section.key, tmdb_id):
                file_paths = _extract_file_paths(item)
                if any(_is_path_prefix(scan_path, fp) for fp in file_paths):
                    hits.append((frozenset(file_paths), _movie_watch_state_from_item(item)))
        return _resolve_correlated_watch_state(hits)

    async def _tv_watch_state(
        self, tmdb_id: int, season: int, library_path: str | None
    ) -> WatchState:
        """The tv branch of :meth:`watch_state` -- see its docstring."""
        if library_path is None:
            for section in await self.list_sections():
                if section.type != "show":
                    continue
                item = await self._find_section_item(section.key, tmdb_id)
                if item is None:
                    continue
                rating_key = _get_str(item, "ratingKey")
                if rating_key is None:
                    return WatchState(watched=False, last_viewed_at=None)
                season_entry = await self._find_season_entry(rating_key, season)
                if season_entry is None:
                    return WatchState(watched=False, last_viewed_at=None)
                return _season_watch_state_from_entry(season_entry)
            return WatchState(watched=False, last_viewed_at=None)

        hits: list[tuple[frozenset[str], WatchState]] = []
        for section in await self.list_sections():
            if section.type != "show":
                continue
            scan_path = _section_scan_path(section, library_path)
            if scan_path is None:
                continue
            for item in await self._find_section_items(section.key, tmdb_id):
                rating_key = _get_str(item, "ratingKey")
                if rating_key is None:
                    continue
                season_entry = await self._find_season_entry(rating_key, season)
                if season_entry is None:
                    continue
                season_rating_key = _get_str(season_entry, "ratingKey")
                if season_rating_key is None:
                    continue
                episode_paths = await self._fetch_children_file_paths(season_rating_key)
                if any(_is_path_prefix(scan_path, fp) for fp in episode_paths):
                    hits.append(
                        (frozenset(episode_paths), _season_watch_state_from_entry(season_entry))
                    )
        return _resolve_correlated_watch_state(hits)

    async def _find_section_item(self, key: str, tmdb_id: int) -> Mapping[str, object] | None:
        """Page one section's items, returning the raw entry matching ``tmdb_id``.

        Shared by the movie and tv branches of ``watch_state`` -- both need the
        full ``Metadata`` row (viewCount/lastViewedAt for a movie, ratingKey for a
        show), not just the boolean presence ``_collect_section_tmdb_ids`` /
        ``_collect_section_tv_seasons`` accumulate. Always re-pages fresh (see
        ``watch_state``'s docstring on why this is deliberately uncached) --
        returns ``None`` once the section is exhausted without a match.
        """
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                entry = _as_mapping(item)
                if _item_matches_tmdb_id(entry, tmdb_id):
                    return entry
            if len(items) < _PAGE_SIZE:
                return None
            start += _PAGE_SIZE

    async def _find_season_entry(self, rating_key: str, season: int) -> Mapping[str, object] | None:
        """Return the raw season row matching ``season`` for a show's ``ratingKey``.

        Same ``/children`` endpoint ``_fetch_present_seasons`` reads, but returns
        the full row (``viewedLeafCount``/``leafCount``/``lastViewedAt``) rather
        than folding it into a presence set.
        """
        payload = await self._get(f"/library/metadata/{rating_key}/children")
        items = _as_sequence(_media_container(payload).get("Metadata"))
        for item in items:
            entry = _as_mapping(item)
            if _get_int(entry, "index") == season:
                return entry
        return None

    async def _find_section_items(self, key: str, tmdb_id: int) -> list[Mapping[str, object]]:
        """Page one section's items, returning EVERY raw entry matching ``tmdb_id``.

        Unlike :meth:`_find_section_item`'s early return on the first hit, this
        crawls the WHOLE section so a path-correlated :meth:`watch_state` lookup
        (issue #207) can see every duplicate item sharing ``tmdb_id`` -- e.g. the
        same title imported into two sections with distinct copies on disk --
        rather than being blind to all but the first one paged.
        """
        matches: list[Mapping[str, object]] = []
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                entry = _as_mapping(item)
                if _item_matches_tmdb_id(entry, tmdb_id):
                    matches.append(entry)
            if len(items) < _PAGE_SIZE:
                return matches
            start += _PAGE_SIZE

    async def _fetch_children_file_paths(self, rating_key: str) -> list[str]:
        """Every episode ``Media[].Part[].file`` path under one season's ``ratingKey``.

        A single non-paged ``/children`` GET (mirrors :meth:`_find_season_entry`'s
        shape) -- a season's episode count stays well under ``_PAGE_SIZE``. Used
        by the path-correlated TV branch of :meth:`watch_state` (issue #207) to
        confirm the season row it found actually backs the candidate's stored
        ``library_path`` breadcrumb, not just a same-tmdb duplicate elsewhere.
        """
        payload = await self._get(f"/library/metadata/{rating_key}/children")
        items = _as_sequence(_media_container(payload).get("Metadata"))
        paths: list[str] = []
        for item in items:
            paths.extend(_extract_file_paths(_as_mapping(item)))
        return paths

    async def resolve_watch_states(self, queries: Sequence[WatchStateQuery]) -> list[WatchState]:
        """Batch :meth:`watch_state` -- shared, demand-paged section crawls.

        See :meth:`LibraryPort.resolve_watch_states`. The result for the Nth query
        is byte-for-byte what :meth:`watch_state` would return for it; only the
        round-trip count differs. Nothing is crawled up front: each media type gets
        a per-batch :class:`_LazySectionIndex`, and each query demands exactly the
        sections/pages its own per-candidate :meth:`watch_state` call would have
        read -- an uncorrelated (``library_path=None``) query stops at its first
        match, a path-correlated one fully pages only the sections covering its
        path -- with every fetched page memoized and shared across the batch. So a
        failing section the per-candidate path never reached is never requested
        and cannot abort the sweep, while a genuinely needed section's failure
        still propagates exactly as before (the two codex P2s on #306). Every
        distinct show's season ``/children`` and every distinct season's episode
        ``/children`` is likewise fetched at most once, shared across all queries
        that need it. TV and movie queries can be mixed.
        """
        if not queries:
            return []
        sections = await self.list_sections()
        movie_index = _LazySectionIndex(
            [s for s in sections if s.type == "movie"], self._fetch_section_items_page
        )
        show_index = _LazySectionIndex(
            [s for s in sections if s.type == "show"], self._fetch_section_items_page
        )
        # Per-batch memoization of the two ``/children`` reads the TV branch makes:
        # a show's season listing (keyed by the show's ratingKey) and a season's
        # episode listing (keyed by the season's ratingKey). Shared across every
        # query in this batch so a show with N tracked seasons pays ONE season-list
        # fetch, not N -- see :meth:`LibraryPort.resolve_watch_states`'s cost model.
        show_season_entries: dict[str, list[Mapping[str, object]]] = {}
        season_episode_paths: dict[str, list[str]] = {}
        results: list[WatchState] = []
        for query in queries:
            if query.media_type == "tv":
                if query.season is None:
                    raise ValueError("resolve_watch_states requires a season for media_type='tv'")
                results.append(
                    await self._resolve_tv_watch_state(
                        show_index,
                        query.tmdb_id,
                        query.season,
                        query.library_path,
                        show_season_entries,
                        season_episode_paths,
                    )
                )
            else:
                results.append(
                    await self._resolve_movie_watch_state_batch(
                        movie_index, query.tmdb_id, query.library_path
                    )
                )
        return results

    async def _fetch_section_items_page(
        self, key: str, start: int
    ) -> Sequence[Mapping[str, object]]:
        """One ``/all?includeGuids=1`` page of section ``key`` -- the shared pager
        behind :class:`_LazySectionIndex` (the exact request shape
        :meth:`_find_section_item` / :meth:`_find_section_items` issue, so the
        batch's request set stays comparable to theirs page-for-page)."""
        payload = await self._get(
            f"/library/sections/{key}/all",
            {"includeGuids": "1"},
            headers={
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(_PAGE_SIZE),
            },
        )
        return [
            _as_mapping(item) for item in _as_sequence(_media_container(payload).get("Metadata"))
        ]

    async def _resolve_movie_watch_state_batch(
        self, movie_index: _LazySectionIndex, tmdb_id: int, library_path: str | None
    ) -> WatchState:
        """The batched movie branch -- mirrors :meth:`_movie_watch_state` against
        the demand-paged ``movie_index``.

        ``library_path=None`` walks sections in order and resolves at the FIRST
        matching item (:meth:`_find_section_item`'s first-match early return --
        later sections/pages are never demanded, so their failures stay exactly as
        unreachable as the per-item path left them); otherwise every
        ``tmdb_id``-matching item whose file path sits under the reverse-mapped
        ``library_path`` is a correlated hit (:meth:`_find_section_items`'s
        whole-section semantics via ``ensure_complete``), resolved by
        :func:`_resolve_correlated_watch_state` exactly as the per-item method does.
        """
        if library_path is None:
            for i in range(len(movie_index.sections)):
                item = await movie_index.find_first(i, tmdb_id)
                if item is not None:
                    return _movie_watch_state_from_item(item)
            return WatchState(watched=False, last_viewed_at=None)

        hits: list[tuple[frozenset[str], WatchState]] = []
        for i, section in enumerate(movie_index.sections):
            scan_path = _section_scan_path(section, library_path)
            if scan_path is None:
                continue
            by_tmdb_id = await movie_index.ensure_complete(i)
            for item in by_tmdb_id.get(tmdb_id, []):
                file_paths = _extract_file_paths(item)
                if any(_is_path_prefix(scan_path, fp) for fp in file_paths):
                    hits.append((frozenset(file_paths), _movie_watch_state_from_item(item)))
        return _resolve_correlated_watch_state(hits)

    async def _cached_season_entries(
        self, rating_key: str, cache: dict[str, list[Mapping[str, object]]]
    ) -> list[Mapping[str, object]]:
        """One show's ``/children`` season rows, fetched at most once per batch.

        Memoizes the exact ``/children`` read :meth:`_find_season_entry` performs so
        several tracked seasons of the SAME show share a single fetch (issue #213).
        """
        cached = cache.get(rating_key)
        if cached is None:
            payload = await self._get(f"/library/metadata/{rating_key}/children")
            cached = [
                _as_mapping(item)
                for item in _as_sequence(_media_container(payload).get("Metadata"))
            ]
            cache[rating_key] = cached
        return cached

    async def _cached_children_file_paths(
        self, rating_key: str, cache: dict[str, list[str]]
    ) -> list[str]:
        """One season's episode file paths, fetched at most once per batch (issue #238)."""
        cached = cache.get(rating_key)
        if cached is None:
            cached = await self._fetch_children_file_paths(rating_key)
            cache[rating_key] = cached
        return cached

    async def _resolve_tv_watch_state(
        self,
        show_index: _LazySectionIndex,
        tmdb_id: int,
        season: int,
        library_path: str | None,
        show_season_entries: dict[str, list[Mapping[str, object]]],
        season_episode_paths: dict[str, list[str]],
    ) -> WatchState:
        """The batched TV branch -- mirrors :meth:`_tv_watch_state` against the
        demand-paged ``show_index`` + shared ``/children`` caches.

        ``library_path=None`` walks show sections in order and commits to the
        FIRST ``tmdb_id`` match (:meth:`_find_section_item` semantics via
        ``find_first`` -- later sections/pages are never demanded, preserving
        their failure irrelevance); the path-correlated branch fully pages only
        the sections covering ``library_path`` (:meth:`_find_section_items`
        semantics via ``ensure_complete``).
        """
        if library_path is None:
            for i in range(len(show_index.sections)):
                item = await show_index.find_first(i, tmdb_id)
                if item is None:
                    continue
                rating_key = _get_str(item, "ratingKey")
                if rating_key is None:
                    return WatchState(watched=False, last_viewed_at=None)
                season_entry = _find_season_entry_in(
                    await self._cached_season_entries(rating_key, show_season_entries), season
                )
                if season_entry is None:
                    return WatchState(watched=False, last_viewed_at=None)
                return _season_watch_state_from_entry(season_entry)
            return WatchState(watched=False, last_viewed_at=None)

        hits: list[tuple[frozenset[str], WatchState]] = []
        for i, section in enumerate(show_index.sections):
            scan_path = _section_scan_path(section, library_path)
            if scan_path is None:
                continue
            by_tmdb_id = await show_index.ensure_complete(i)
            for item in by_tmdb_id.get(tmdb_id, []):
                rating_key = _get_str(item, "ratingKey")
                if rating_key is None:
                    continue
                season_entry = _find_season_entry_in(
                    await self._cached_season_entries(rating_key, show_season_entries), season
                )
                if season_entry is None:
                    continue
                season_rating_key = _get_str(season_entry, "ratingKey")
                if season_rating_key is None:
                    continue
                episode_paths = await self._cached_children_file_paths(
                    season_rating_key, season_episode_paths
                )
                if any(_is_path_prefix(scan_path, fp) for fp in episode_paths):
                    hits.append(
                        (frozenset(episode_paths), _season_watch_state_from_entry(season_entry))
                    )
        return _resolve_correlated_watch_state(hits)

    async def fetch_artwork(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        kind: ArtworkKind,
    ) -> ArtworkImage | None:
        """See :meth:`LibraryPort.fetch_artwork`.

        Resolves the item's Plex-native artwork PATH from the per-credential
        artwork index (one cached section crawl), then streams the final image
        through the dedicated bounded helper. The index crawl uses the normal
        ``_get``/``_request`` JSON boundary.
        """
        async with _artwork_semaphore():
            keys = await self._artwork_keys(tmdb_id, media_type)
            if keys is None:
                return None
            path = keys.poster if kind == "poster" else keys.background
            if path is None or not path.startswith("/") or path.startswith("//"):
                # Absent or unsafe server-relative metadata is an honest miss.
                return None
            streamed = await self._stream_artwork(path)
            if streamed is None:
                return None
            content, content_type = streamed
            return ArtworkImage(content=content, content_type=content_type)

    async def _artwork_keys(
        self, tmdb_id: int, media_type: Literal["movie", "tv"]
    ) -> _ArtworkKeys | None:
        """The cached Plex artwork paths for ``tmdb_id`` in this media type's sections.

        Populates the per-credential artwork index on a miss via ONE crawl of the
        relevant sections, guarded by :func:`_artwork_crawl_lock` so a burst of
        concurrent proxy requests (a page of in-library tiles) shares a single
        crawl. ``None`` when the item is absent from the built map.
        """
        if media_type == "movie":
            cache = _MOVIE_ARTWORK_CACHE
            section_type: Literal["movie", "show"] = "movie"
        else:
            cache = _SHOW_ARTWORK_CACHE
            section_type = "show"
        cached = cache.get(self._cache_key)
        if cached is None:
            async with _artwork_crawl_lock(f"{self._cache_key}|{section_type}"):
                # Double-checked: a crawl that completed while we waited for the
                # lock already populated the cache — don't re-crawl.
                cached = cache.get(self._cache_key)
                if cached is None:
                    cached = await self._collect_present_artwork(section_type)
                    cache.set(self._cache_key, cached)
        return cached.get(tmdb_id)

    async def _collect_present_artwork(
        self, section_type: Literal["movie", "show"]
    ) -> dict[int, _ArtworkKeys]:
        """Crawl every section of ``section_type``, mapping tmdb id -> artwork paths."""
        result: dict[int, _ArtworkKeys] = {}
        for section in await self.list_sections():
            if section.type != section_type:
                continue
            await self._collect_section_artwork(section.key, result)
        return result

    async def _collect_section_artwork(self, key: str, result: dict[int, _ArtworkKeys]) -> None:
        """Page one section, recording each item's ``thumb``/``art`` per tmdb id.

        Uses the same ``/all?includeGuids=1`` walk as the presence crawls (Plex
        returns ``thumb``/``art`` as top-level attributes on every item, so no extra
        parameter is needed). First item to claim a tmdb id wins — a rare duplicate
        entry just keeps the first crawled item's artwork, which is sufficient for
        a display hint.
        """
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                entry = _as_mapping(item)
                tmdb_ids: set[int] = set()
                _collect_item_tmdb_ids(entry, tmdb_ids)
                if not tmdb_ids:
                    continue
                artwork = _ArtworkKeys(
                    poster=_get_str(entry, "thumb"),
                    background=_get_str(entry, "art"),
                )
                for tmdb_id in tmdb_ids:
                    result.setdefault(tmdb_id, artwork)
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
