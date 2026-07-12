"""Fake adapters implementing the ports, with canned data, for the web suite.

These satisfy the runtime-checkable port Protocols (no live network) and are
injected via ``app.dependency_overrides``. The decision path uses the REAL
``GuessitParser`` and the REAL default profile, so the CAM/TS-vs-good ranking is
genuinely exercised — only the I/O edges (TMDB / Prowlarr / qBittorrent) are
faked.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.ports.download_client import (
    AddResult,
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
)
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort, LibrarySection, WatchState
from plex_manager.ports.media_probe import (
    MediaProbeError,
    MediaProbePort,
    MediaProbeResult,
    MediaProbeUnavailableError,
)
from plex_manager.ports.metadata import (
    EpisodeInfo,
    MediaPage,
    MediaSearchResult,
    MetadataPort,
    MovieMetadata,
    TvMetadata,
)
from plex_manager.web.deps import (
    get_library,
    get_library_optional,
    get_media_probe,
    get_prowlarr,
    get_qbittorrent,
    get_qbittorrent_optional,
    get_tmdb,
)

__all__ = [
    "FakeLibrary",
    "FakeMediaProbe",
    "FakeProwlarr",
    "FakeQbittorrent",
    "FakeTmdb",
    "candidate",
    "good_and_cam_candidates",
    "override_adapters",
    "prerelease_only_candidates",
]


class FakeMediaProbe:
    """Accept by default; reject or mark selected basenames unavailable."""

    def __init__(
        self,
        *,
        rejected: Mapping[str, str] | None = None,
        unavailable: Mapping[str, str] | None = None,
        raises: MediaProbeError | None = None,
    ) -> None:
        self.rejected = dict(rejected or {})
        self.unavailable = dict(unavailable or {})
        self.raises = raises
        self.calls: list[Path] = []
        self.timeouts: list[float | None] = []

    def probe(self, path: Path, *, timeout_seconds: float | None = None) -> MediaProbeResult:
        self.calls.append(path)
        self.timeouts.append(timeout_seconds)
        if self.raises is not None:
            raise self.raises
        unavailable_reason = self.unavailable.get(path.name)
        if unavailable_reason is not None:
            raise MediaProbeUnavailableError(unavailable_reason)
        reason = self.rejected.get(path.name)
        if reason is not None:
            raise MediaProbeError(reason)
        return MediaProbeResult(container="matroska", video_codec="h264")


_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def candidate(
    title: str,
    *,
    info_hash: str | None = None,
    seeders: int = 10,
    size_bytes: int = 1_000_000_000,
    magnet: bool = True,
    guid: str | None = None,
) -> CandidateRelease:
    """Build a torrent :class:`CandidateRelease` (magnet by default).

    ``guid`` defaults to ``title``; pass an explicit (e.g. URL-shaped) value to
    exercise GUID-redaction log hygiene.
    """
    magnet_url = f"magnet:?xt=urn:btih:{info_hash or 'deadbeef'}" if magnet else None
    return CandidateRelease(
        guid=guid if guid is not None else title,
        title=title,
        size_bytes=size_bytes,
        magnet_url=magnet_url,
        download_url=None if magnet else f"http://idx.local/{title}",
        info_hash=info_hash,
        seeders=seeders,
        leechers=1,
        indexer_id=1,
        indexer_name="FakeIndexer",
        publish_date=_EPOCH,
    )


def good_and_cam_candidates() -> list[CandidateRelease]:
    """A good WEBDL-1080p release plus a CAM and a TS (both rejected)."""
    return [
        candidate(
            "Some.Movie.2020.CAM.x264-GROUP",
            info_hash="1" * 40,
            seeders=500,
        ),
        candidate(
            "Some.Movie.2020.HDTS.x264-GROUP",
            info_hash="2" * 40,
            seeders=400,
        ),
        candidate(
            "Some.Movie.2020.1080p.WEB-DL.x264-GROUP",
            info_hash="3" * 40,
            seeders=42,
        ),
    ]


def prerelease_only_candidates() -> list[CandidateRelease]:
    """Only pre-release (CAM/TS) candidates — nothing acceptable."""
    return [
        candidate("Some.Movie.2020.CAM.x264-GROUP", info_hash="1" * 40),
        candidate("Some.Movie.2020.HDTS.x264-GROUP", info_hash="2" * 40),
    ]


class FakeTmdb:
    """In-memory :class:`MetadataPort` with canned search + detail."""

    def __init__(
        self,
        *,
        movies: dict[int, MovieMetadata] | None = None,
        shows: dict[int, TvMetadata] | None = None,
        results: list[MediaSearchResult] | None = None,
        trending: list[MediaSearchResult] | None = None,
        popular: list[MediaSearchResult] | None = None,
        upcoming: list[MediaSearchResult] | None = None,
        trending_tv_results: list[MediaSearchResult] | None = None,
        popular_tv_results: list[MediaSearchResult] | None = None,
        season_episodes: dict[tuple[int, int], list[EpisodeInfo]] | None = None,
        season_episodes_error: Exception | None = None,
    ) -> None:
        self.movies = movies or {}
        self.shows = shows or {}
        self.results = results or []
        # Discover rows default to the search results so a test that only sets
        # ``results`` still gets populated trending/popular/upcoming pages.
        self.trending = list(trending) if trending is not None else list(self.results)
        self.popular = list(popular) if popular is not None else list(self.results)
        self.upcoming = list(upcoming) if upcoming is not None else list(self.results)
        # Named ``_tv`` (not ``trending_tv``/``popular_tv``) to avoid colliding with
        # the like-named PORT METHODS below -- unlike the movie rows, the tv port
        # methods carry no distinguishing suffix beyond ``_tv``.
        self._trending_tv = (
            list(trending_tv_results) if trending_tv_results is not None else list(self.results)
        )
        self._popular_tv = (
            list(popular_tv_results) if popular_tv_results is not None else list(self.results)
        )
        # ADR-0020 (issue #178): keyed (tmdb_id, season_number). ``season_episodes_error``
        # (when set) is raised on every call -- the "TMDB outage / target unknown"
        # test double.
        self._season_episodes = season_episodes or {}
        self.season_episodes_error = season_episodes_error
        # Call log (issue #178 pack-first-precedence test): proves the port was
        # NEVER even consulted when Pass 1 alone settled a scope.
        self.season_episodes_calls: list[tuple[int, int]] = []

    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        return list(self.results)

    async def get_movie(self, tmdb_id: int) -> MovieMetadata | None:
        return self.movies.get(tmdb_id)

    async def get_tv_show(self, tmdb_id: int) -> TvMetadata | None:
        return self.shows.get(tmdb_id)

    @staticmethod
    def _page(items: list[MediaSearchResult]) -> MediaPage:
        return MediaPage(page=1, total_pages=1, total_results=len(items), results=tuple(items))

    async def trending_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.trending)

    async def popular_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.popular)

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.upcoming)

    async def trending_tv(self, page: int = 1) -> MediaPage:
        return self._page(self._trending_tv)

    async def popular_tv(self, page: int = 1) -> MediaPage:
        return self._page(self._popular_tv)

    async def season_episodes(self, tmdb_id: int, season_number: int) -> list[EpisodeInfo]:
        self.season_episodes_calls.append((tmdb_id, season_number))
        if self.season_episodes_error is not None:
            raise self.season_episodes_error
        return list(self._season_episodes.get((tmdb_id, season_number), []))


class FakeProwlarr:
    """In-memory :class:`IndexerPort` returning a fixed candidate set."""

    def __init__(self, candidates: list[CandidateRelease] | None = None) -> None:
        self.candidates = candidates or []
        self.searched: list[IndexerSearchRequest] = []

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        return list(self.candidates)


class FakeQbittorrent:
    """In-memory :class:`DownloadClientPort` recording adds + canned statuses."""

    def __init__(
        self,
        statuses: list[DownloadStatus] | None = None,
        *,
        files: dict[str, list[DownloadedFile]] | None = None,
        source_errors: set[str] | None = None,
        pre_existing: set[str] | None = None,
        default_save_path: str | None = None,
    ) -> None:
        self.statuses = statuses or []
        self.files = files or {}
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, bool]] = []
        # The client's canned GLOBAL default save path (``get_default_save_path``)
        # and a recorder of every ``set_location`` call (lowercased hash, target).
        self.default_save_path = default_save_path
        self.relocated: list[tuple[str, str]] = []
        # Sources (a magnet/HTTP url) for which ``add`` raises
        # :class:`QbittorrentSourceError`, mirroring the real adapter's honest
        # "HTTP source resolved to neither a magnet nor a hashable .torrent" — the
        # client is healthy, the SOURCE is unusable. The real adapter raises this
        # BEFORE the add POST, so a matched source is NEVER recorded in ``added``.
        self.source_errors = source_errors or set()
        # Lowercased hashes ``add`` reports as ALREADY PRESENT (the real
        # adapter's 409 branch): the AddResult comes back ``created=False``, so
        # a lost-grab cleanup must leave the pre-existing torrent untouched.
        self.pre_existing = pre_existing or set()

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        if magnet_or_url in self.source_errors:
            raise QbittorrentSourceError("could not determine torrent hash for HTTP source")
        self.added.append((magnet_or_url, save_path, category))
        # Mirror the real adapter: derive the info-hash from the magnet's btih.
        marker = "urn:btih:"
        torrent_hash = ""
        if marker in magnet_or_url:
            torrent_hash = magnet_or_url.split(marker, 1)[1].split("&", 1)[0].lower()
        return AddResult(torrent_hash=torrent_hash, created=torrent_hash not in self.pre_existing)

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        for status in self.statuses:
            if status.info_hash.lower() == info_hash.lower():
                return status
        return None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        return list(self.statuses)

    async def pause(self, info_hash: str) -> None:
        return None

    async def resume(self, info_hash: str) -> None:
        return None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        self.removed.append((info_hash.lower(), delete_files))

    async def set_category(self, info_hash: str, category: str) -> None:
        return None

    async def get_save_path(self, info_hash: str) -> str | None:
        return None

    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        return list(self.files.get(info_hash.lower(), []))

    async def get_default_save_path(self) -> str | None:
        return self.default_save_path

    async def set_location(self, info_hash: str, save_path: str) -> None:
        self.relocated.append((info_hash.lower(), save_path))


class FakeLibrary:
    """In-memory :class:`LibraryPort`: a set of in-library tmdb ids + scan recorder.

    ``available_tv_seasons`` maps a show's tmdb id to the frozenset of season
    numbers present (mirrors ``PlexLibrary``'s ``leafCount>0`` season map). A show
    key with an EMPTY frozenset means "the show itself is present but no season has
    aired episodes yet" — ``is_available(tv, season=None)`` is still ``True`` for
    it, matching the real adapter's "show present" semantics.

    ``scanned`` keeps the historical path-only log; ``scan_calls`` additionally
    records the ``media_type`` passed to each :meth:`trigger_scan` call, so a test
    can assert a TV import scans with ``"tv"`` (and a movie import with
    ``"movie"``) rather than only checking that some path was scanned.

    ``watch_states`` (ADR-0012) maps ``(tmdb_id, media_type, season)`` -- ``season``
    is ``None`` for a movie entry -- to a canned :class:`WatchState`; a key with no
    entry answers ``watched=False, last_viewed_at=None`` (Plex has never recorded a
    view), matching the real adapter's honest default for an absent/never-viewed
    item. ``watch_state_calls`` records every ``(tmdb_id, media_type, season)`` a
    caller resolved -- e.g. ``eviction_service``'s below-threshold pre-check test
    asserts this stays EMPTY, proving the sweep never pays for a Plex round-trip
    when there is no disk pressure to relieve.

    Call counters (issue #136 -- batched availability reconcile): ``is_available_calls``
    counts every :meth:`is_available` call, ``present_ids_calls`` every
    :meth:`present_ids` call (``present_ids_refresh_absent_calls`` records the
    ``refresh_absent`` flag passed to each one, so a test can assert the reconcile
    cycle asks for the never-trust-a-cached-absence contract), and
    ``season_presence_calls`` counts every :meth:`season_presence` call (a whole
    BATCH of tmdb ids per call, mirroring ``PlexLibrary``'s one-page-walk-per-call
    shape) -- ``season_presence_call_ids`` additionally records the ``frozenset``
    of ids requested by each call, so a test can assert BOTH "exactly one call for
    the whole tick" (``season_presence_calls == 1``) and "that one call named every
    distinct pending show" (``season_presence_call_ids == [frozenset({...})]``). A
    caller asserts against these to prove a reconcile pass makes AT MOST one
    ``present_ids`` call and exactly ONE ``season_presence`` call per tick --
    never one ``season_presence`` call per show, never one ``is_available`` call
    per row. ``confirm_paths_calls`` (issue #158) mirrors the same discipline for
    the GUID-independent path-based fallback: each entry is the
    ``(media_type, frozenset(library_paths))`` one :meth:`confirm_paths` call was
    asked to resolve, so a test can assert the reconcile cycle batches every
    distinct GUID-miss row's path check into ONE call per media type per tick,
    never one per row.
    """

    def __init__(
        self,
        *,
        available: set[int] | None = None,
        available_tv_seasons: dict[int, frozenset[int]] | None = None,
        sections: list[LibrarySection] | None = None,
        watch_states: dict[tuple[int, str, int | None], WatchState] | None = None,
        raises: Exception | None = None,
        raises_for_shows: dict[int, Exception] | None = None,
        season_presence_raises: Exception | None = None,
        movie_file_paths: Collection[str] | None = None,
        tv_file_paths: Collection[str] | None = None,
        confirm_paths_raises: Exception | None = None,
    ) -> None:
        self.available_ids = available or set()
        self.available_tv_seasons = available_tv_seasons or {}
        self.sections = sections or []
        # Path-based confirmation fallback (issue #158): every file path Plex
        # "knows about" for movies/tv, INDEPENDENT of the guid-keyed
        # ``available_ids``/``available_tv_seasons`` above -- lets a test model a
        # GUID-miss row (absent from those) that is still confirmable by path, the
        # whole point of the fallback. Plain strings, no host/container remap
        # simulated here (that translation is exercised against the real
        # ``PlexLibrary`` adapter in ``tests/adapters/plex/test_plex_library.py``);
        # callers here pass paths already in whatever single namespace the test
        # wants both sides compared in.
        self.movie_file_paths = list(movie_file_paths or ())
        self.tv_file_paths = list(tv_file_paths or ())
        self.confirm_paths_raises = confirm_paths_raises
        self.confirm_paths_calls: list[tuple[str, frozenset[str]]] = []
        self.scanned: list[str] = []
        self.scan_calls: list[tuple[str, str]] = []
        self.watch_states = watch_states or {}
        self.watch_state_calls: list[tuple[int, str, int | None]] = []
        # When set, ``is_available``/``present_seasons``/``present_ids``/
        # ``season_presence`` raise this instead of returning -- lets a caller
        # exercise the best-effort "log and treat as not-present" error path (see
        # request_service._already_in_library / _present_seasons_or_empty,
        # season_request_service._present_seasons).
        self.raises = raises
        # Per-show override for ``season_presence`` ONLY (round 4, #136 review):
        # mirrors the real adapter's per-show isolation -- a tmdb id that is a key
        # here has its OWN ``/children``-equivalent lookup fail, so it is OMITTED
        # from the returned mapping entirely (never raised, never mapped to an
        # empty frozenset). Every OTHER requested id in the same call still
        # resolves normally -- see ``LibraryPort.season_presence``'s contract.
        # Use ``season_presence_raises`` below to model a genuine WHOLE-BATCH
        # transport failure instead (the page-walk itself failing).
        self.raises_for_shows = raises_for_shows or {}
        # Whole-batch transport-failure knob for ``season_presence`` ONLY: the
        # real ``PlexLibrary.season_presence``'s section page-walk is
        # all-or-nothing, so a genuine transport failure fails the ENTIRE call
        # (every requested id unresolved), unlike ``raises_for_shows`` above
        # which isolates a single id's own lookup.
        self.season_presence_raises = season_presence_raises
        self.is_available_calls = 0
        self.present_ids_calls = 0
        self.present_ids_refresh_absent_calls: list[bool] = []
        self.season_presence_calls = 0
        self.season_presence_call_ids: list[frozenset[int]] = []

    async def is_available(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        use_cache: bool = True,
        season: int | None = None,
    ) -> bool:
        self.is_available_calls += 1
        if self.raises is not None:
            raise self.raises
        # No cache to bypass; ``use_cache`` is accepted to match LibraryPort.
        if media_type == "tv":
            seasons = self.available_tv_seasons.get(tmdb_id)
            if seasons is None:
                return False
            return True if season is None else season in seasons
        return tmdb_id in self.available_ids

    async def present_seasons(self, tmdb_id: int) -> frozenset[int]:
        if self.raises is not None:
            raise self.raises
        # The show's present seasons in one lookup (mirrors PlexLibrary's single
        # crawl); empty for an absent show, matching the real adapter.
        return self.available_tv_seasons.get(tmdb_id, frozenset())

    async def season_presence(self, tmdb_ids: Collection[int]) -> Mapping[int, frozenset[int]]:
        wanted = frozenset(tmdb_ids)
        self.season_presence_calls += 1
        self.season_presence_call_ids.append(wanted)
        # A genuine whole-batch transport failure (the section page-walk itself
        # failing) -- the ENTIRE call raises, matching the real adapter's
        # all-or-nothing page-walk posture. See ``season_presence_raises``'s
        # docstring in ``__init__``.
        if self.season_presence_raises is not None:
            raise self.season_presence_raises
        if self.raises is not None:
            raise self.raises
        # A BATCH lookup for every requested show in ONE call -- mirrors
        # ``PlexLibrary.season_presence``'s contract (fresh, one page-walk
        # regardless of how many shows are requested); the fake answers it from the
        # same seasons map ``present_seasons`` uses. Every requested id is present
        # as a key (empty frozenset when the show is untracked) EXCEPT one whose
        # id is in ``raises_for_shows`` -- that id's own lookup "failed" and is
        # OMITTED from the mapping entirely, mirroring the real adapter's
        # per-show isolation (round 4, #136 review) rather than raising the
        # whole call.
        return {
            tmdb_id: self.available_tv_seasons.get(tmdb_id, frozenset())
            for tmdb_id in wanted
            if tmdb_id not in self.raises_for_shows
        }

    async def present_ids(
        self,
        keys: Sequence[tuple[int, Literal["movie", "tv"]]],
        *,
        refresh_absent: bool = False,
    ) -> frozenset[tuple[int, Literal["movie", "tv"]]]:
        self.present_ids_calls += 1
        self.present_ids_refresh_absent_calls.append(refresh_absent)
        if self.raises is not None:
            raise self.raises
        # The fake has no cache to "refresh" -- ``available_ids``/``available_tv_seasons``
        # are always read live, so it already answers as freshly as
        # ``refresh_absent=True`` would force the real adapter to. ``refresh_absent``
        # is recorded (not applied) so a caller-side test can assert
        # ``run_availability_cycle`` requests the fresh-on-absence contract from the
        # reconcile cycle without needing to model the adapter's cache here.
        # Movie presence from the in-library id set; show-level TV presence from the
        # seasons map's keys (a show is "present" if any season is tracked) -- the
        # granularity the batch tile accessor needs. Mirrors PlexLibrary.present_ids.
        return frozenset(
            key
            for key in keys
            if (key[1] == "movie" and key[0] in self.available_ids)
            or (key[1] == "tv" and key[0] in self.available_tv_seasons)
        )

    async def confirm_paths(
        self, media_type: Literal["movie", "tv"], library_paths: Collection[str]
    ) -> frozenset[str]:
        wanted = frozenset(p for p in library_paths if p)
        self.confirm_paths_calls.append((media_type, wanted))
        if self.confirm_paths_raises is not None:
            raise self.confirm_paths_raises
        if self.raises is not None:
            raise self.raises
        known = self.movie_file_paths if media_type == "movie" else self.tv_file_paths
        return frozenset(
            library_path
            for library_path in wanted
            if any(fp == library_path or fp.startswith(f"{library_path}/") for fp in known)
        )

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        self.scanned.append(path)
        self.scan_calls.append((path, media_type))

    async def list_sections(self, *, use_cache: bool = True) -> list[LibrarySection]:
        del use_cache  # the fake has no cache to bypass
        return list(self.sections)

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        season: int | None = None,
    ) -> WatchState:
        self.watch_state_calls.append((tmdb_id, media_type, season))
        return self.watch_states.get(
            (tmdb_id, media_type, season), WatchState(watched=False, last_viewed_at=None)
        )


def override_adapters(
    app: FastAPI,
    *,
    tmdb: MetadataPort | None = None,
    prowlarr: IndexerPort | None = None,
    qbt: DownloadClientPort | None = None,
    library: LibraryPort | None = None,
    media_probe: MediaProbePort | None = None,
) -> None:
    """Point the adapter dependencies at the supplied fakes.

    ``library`` overrides BOTH the required (``get_library``) and optional
    (``get_library_optional``) Plex dependencies, so the request-dedupe and import
    endpoints see the same fake. ``qbt`` likewise overrides BOTH the required
    (``get_qbittorrent``) and optional (``get_qbittorrent_optional``, the mark-failed
    endpoint's DB-only-friendly variant) qBittorrent dependencies.
    """
    if tmdb is not None:
        app.dependency_overrides[get_tmdb] = lambda: tmdb
    if prowlarr is not None:
        app.dependency_overrides[get_prowlarr] = lambda: prowlarr
    if qbt is not None:
        app.dependency_overrides[get_qbittorrent] = lambda: qbt
        app.dependency_overrides[get_qbittorrent_optional] = lambda: qbt
    if library is not None:
        app.dependency_overrides[get_library] = lambda: library
        app.dependency_overrides[get_library_optional] = lambda: library
    probe = media_probe or FakeMediaProbe()
    app.dependency_overrides[get_media_probe] = lambda: probe
