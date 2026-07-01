"""Contract tests for the port Protocols and their cross-boundary DTOs.

These confirm the ports import cleanly (hexagon wiring intact), the DTO defaults
match the spec, and a minimal fake satisfies each runtime-checkable Protocol.
"""

from __future__ import annotations

from plex_manager.domain.release import (
    CandidateRelease,
    IndexerSearchRequest,
    ParsedRelease,
)
from plex_manager.ports.download_client import (
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
)
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort, LibrarySection
from plex_manager.ports.metadata import (
    MediaSearchResult,
    MetadataPort,
    MovieMetadata,
    TvMetadata,
)
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import (
    BlocklistRecord,
    BlocklistRepository,
    DownloadRecord,
    DownloadRepository,
    RequestRecord,
    RequestRepository,
)


def test_download_status_defaults_match_qbit_conventions() -> None:
    status = DownloadStatus(info_hash="abc", name="t", raw_state="downloading")
    assert status.progress == 0.0
    assert status.ratio_limit == -2.0
    assert status.seeding_time_limit_minutes == -2
    assert status.content_path is None


def test_metadata_and_library_dtos_construct() -> None:
    assert MediaSearchResult(tmdb_id=1, media_type="movie", title="x").year is None
    assert MovieMetadata(tmdb_id=1, title="x").imdb_id is None
    assert TvMetadata(tmdb_id=1, title="x").season_count == 0
    assert LibrarySection(key="1", title="Movies", type="movie").type == "movie"


def test_repository_records_construct() -> None:
    request = RequestRecord(id=1, tmdb_id=5, media_type="movie", title="x", status="pending")
    assert request.is_anime is False
    assert DownloadRecord(id=1, torrent_hash="h", status="downloading").progress == 0.0
    assert BlocklistRecord(id=1, source_title="t", reason="failed").torrent_hash is None


class _FakeParser:
    def parse(self, release_name: str) -> ParsedRelease:
        return ParsedRelease(raw_title=release_name, clean_title=release_name)


class _FakeIndexer:
    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        return []


class _FakeDownloadClient:
    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        return "hash"

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        return None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        return []

    async def pause(self, info_hash: str) -> None:
        return None

    async def resume(self, info_hash: str) -> None:
        return None

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        return None

    async def set_category(self, info_hash: str, category: str) -> None:
        return None

    async def get_save_path(self, info_hash: str) -> str | None:
        return None

    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        return []


def test_fakes_satisfy_runtime_checkable_protocols() -> None:
    assert isinstance(_FakeParser(), ParserPort)
    assert isinstance(_FakeIndexer(), IndexerPort)
    assert isinstance(_FakeDownloadClient(), DownloadClientPort)


def test_port_protocols_are_importable() -> None:
    # Importing the names is the assertion; reference them so linters keep them.
    for proto in (
        MetadataPort,
        LibraryPort,
        FileSystemPort,
        RequestRepository,
        DownloadRepository,
        BlocklistRepository,
    ):
        assert proto is not None


async def test_fake_indexer_returns_list() -> None:
    result = await _FakeIndexer().search(IndexerSearchRequest(query="test"))
    assert result == []
