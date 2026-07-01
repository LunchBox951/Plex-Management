"""Fake adapters implementing the ports, with canned data, for the web suite.

These satisfy the runtime-checkable port Protocols (no live network) and are
injected via ``app.dependency_overrides``. The decision path uses the REAL
``GuessitParser`` and the REAL default profile, so the CAM/TS-vs-good ranking is
genuinely exercised — only the I/O edges (TMDB / Prowlarr / qBittorrent) are
faked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI

from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.ports.download_client import (
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
)
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort, LibrarySection
from plex_manager.ports.metadata import (
    MediaPage,
    MediaSearchResult,
    MetadataPort,
    MovieMetadata,
    TvMetadata,
)
from plex_manager.web.deps import (
    get_library,
    get_library_optional,
    get_prowlarr,
    get_qbittorrent,
    get_tmdb,
)

__all__ = [
    "FakeLibrary",
    "FakeProwlarr",
    "FakeQbittorrent",
    "FakeTmdb",
    "candidate",
    "good_and_cam_candidates",
    "override_adapters",
    "prerelease_only_candidates",
]

_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def candidate(
    title: str,
    *,
    info_hash: str | None = None,
    seeders: int = 10,
    size_bytes: int = 1_000_000_000,
    magnet: bool = True,
) -> CandidateRelease:
    """Build a torrent :class:`CandidateRelease` (magnet by default)."""
    magnet_url = f"magnet:?xt=urn:btih:{info_hash or 'deadbeef'}" if magnet else None
    return CandidateRelease(
        guid=title,
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
    ) -> None:
        self.movies = movies or {}
        self.shows = shows or {}
        self.results = results or []
        # Discover rows default to the search results so a test that only sets
        # ``results`` still gets populated trending/popular/upcoming pages.
        self.trending = list(trending) if trending is not None else list(self.results)
        self.popular = list(popular) if popular is not None else list(self.results)
        self.upcoming = list(upcoming) if upcoming is not None else list(self.results)

    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        return list(self.results)

    async def get_movie(self, tmdb_id: int) -> MovieMetadata | None:
        return self.movies.get(tmdb_id)

    async def get_tv_show(self, tmdb_id: int) -> TvMetadata | None:
        return self.shows.get(tmdb_id)

    @staticmethod
    def _page(items: list[MediaSearchResult]) -> MediaPage:
        return MediaPage(page=1, total_pages=1, total_results=len(items), results=list(items))

    async def trending_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.trending)

    async def popular_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.popular)

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        return self._page(self.upcoming)


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
    ) -> None:
        self.statuses = statuses or []
        self.files = files or {}
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, bool]] = []

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        self.added.append((magnet_or_url, save_path, category))
        # Mirror the real adapter: derive the info-hash from the magnet's btih.
        marker = "urn:btih:"
        if marker in magnet_or_url:
            return magnet_or_url.split(marker, 1)[1].split("&", 1)[0].lower()
        return ""

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
    """

    def __init__(
        self,
        *,
        available: set[int] | None = None,
        available_tv_seasons: dict[int, frozenset[int]] | None = None,
        sections: list[LibrarySection] | None = None,
    ) -> None:
        self.available_ids = available or set()
        self.available_tv_seasons = available_tv_seasons or {}
        self.sections = sections or []
        self.scanned: list[str] = []
        self.scan_calls: list[tuple[str, str]] = []

    async def is_available(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        use_cache: bool = True,
        season: int | None = None,
    ) -> bool:
        # No cache to bypass; ``use_cache`` is accepted to match LibraryPort.
        if media_type == "tv":
            seasons = self.available_tv_seasons.get(tmdb_id)
            if seasons is None:
                return False
            return True if season is None else season in seasons
        return tmdb_id in self.available_ids

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        self.scanned.append(path)
        self.scan_calls.append((path, media_type))

    async def list_sections(self) -> list[LibrarySection]:
        return list(self.sections)


def override_adapters(
    app: FastAPI,
    *,
    tmdb: MetadataPort | None = None,
    prowlarr: IndexerPort | None = None,
    qbt: DownloadClientPort | None = None,
    library: LibraryPort | None = None,
) -> None:
    """Point the adapter dependencies at the supplied fakes.

    ``library`` overrides BOTH the required (``get_library``) and optional
    (``get_library_optional``) Plex dependencies, so the request-dedupe and import
    endpoints see the same fake.
    """
    if tmdb is not None:
        app.dependency_overrides[get_tmdb] = lambda: tmdb
    if prowlarr is not None:
        app.dependency_overrides[get_prowlarr] = lambda: prowlarr
    if qbt is not None:
        app.dependency_overrides[get_qbittorrent] = lambda: qbt
    if library is not None:
        app.dependency_overrides[get_library] = lambda: library
        app.dependency_overrides[get_library_optional] = lambda: library
