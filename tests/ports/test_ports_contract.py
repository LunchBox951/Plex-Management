"""Contract tests for the port Protocols and their cross-boundary DTOs.

These confirm the ports import cleanly (hexagon wiring intact), the DTO defaults
match the spec, and a minimal fake satisfies each runtime-checkable Protocol.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

import plex_manager.ports as ports_pkg
from plex_manager.domain.release import (
    CandidateRelease,
    IndexerSearchRequest,
    ParsedRelease,
)
from plex_manager.ports.download_client import (
    AddResult,
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
    FailureDetail,
)
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort, LibrarySection, WatchState
from plex_manager.ports.metadata import (
    MediaPage,
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
    LogEventCreate,
    LogEventPage,
    LogEventRecord,
    LogEventRepository,
    QueueRecord,
    RequestRecord,
    RequestRepository,
)

_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


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
    assert WatchState(watched=False).last_viewed_at is None


def test_watch_state_normalizes_a_naive_last_viewed_at_to_utc() -> None:
    """Issue #82: a naive ``last_viewed_at`` (e.g. a careless adapter/fake) must
    not slip past the DTO boundary as-is -- eviction/retention-telemetry subtract
    it against UTC-aware cutoffs, and a naive value would raise ``TypeError``
    deep inside that arithmetic instead of at construction time."""
    naive = datetime(2024, 1, 1, 12, 0, 0)
    state = WatchState(watched=True, last_viewed_at=naive)
    assert state.last_viewed_at is not None
    assert state.last_viewed_at.tzinfo is not None
    assert state.last_viewed_at == naive.replace(tzinfo=UTC)


def test_watch_state_preserves_an_already_aware_last_viewed_at() -> None:
    """A tz-aware ``last_viewed_at`` in a non-UTC offset is passed through
    untouched -- normalization only re-attaches UTC to a NAIVE value, it never
    overwrites an already-honest offset."""
    from datetime import timedelta, timezone

    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    state = WatchState(watched=True, last_viewed_at=aware)
    assert state.last_viewed_at == aware


def test_repository_records_construct() -> None:
    request = RequestRecord(id=1, tmdb_id=5, media_type="movie", title="x", status="pending")
    assert request.is_anime is False
    assert request.library_path is None
    assert request.keep_forever is False
    assert DownloadRecord(id=1, torrent_hash="h", status="downloading").progress == 0.0
    assert DownloadRecord(id=1, torrent_hash="h", status="downloading").release_title is None
    assert BlocklistRecord(id=1, source_title="t", reason="failed").torrent_hash is None


def test_queue_record_extends_download_record_with_title_and_poster() -> None:
    """``QueueRecord`` (issue #134) is a ``DownloadRecord`` plus the two
    ``MediaRequest``-only fields the queue view needs; both default honestly to
    ``None`` for an orphaned/unenriched row."""
    record = QueueRecord(id=1, torrent_hash="h", status="downloading")
    assert isinstance(record, DownloadRecord)
    assert record.title is None
    assert record.poster_url is None

    enriched = QueueRecord(
        id=1, torrent_hash="h", status="downloading", title="Some Movie", poster_url="p.jpg"
    )
    assert enriched.title == "Some Movie"
    assert enriched.poster_url == "p.jpg"


def test_log_event_dtos_construct() -> None:
    record = LogEventRecord(
        id=1, created_at=_EPOCH, level="INFO", logger="plex_manager.x", message="hi"
    )
    assert record.context is None
    created = LogEventCreate(created_at=_EPOCH, level="INFO", logger="plex_manager.x", message="hi")
    assert created.context is None
    assert LogEventPage(total=0, results=()).results == ()


def test_media_page_results_is_an_immutable_tuple() -> None:
    """Issue #106: the TMDB adapter's page cache hands the SAME ``MediaPage``
    back on every hit within its TTL -- a mutable ``results`` list would let one
    caller's in-place mutation corrupt what every later cache hit sees. A
    ``list`` input is coerced to a tuple, and the source list's later mutation
    must never leak into the constructed page."""
    source = [MediaSearchResult(tmdb_id=1, media_type="movie", title="x")]
    page = MediaPage(
        page=1,
        total_pages=1,
        total_results=1,
        results=source,  # pyright: ignore[reportArgumentType]
    )
    assert isinstance(page.results, tuple)
    source.append(MediaSearchResult(tmdb_id=2, media_type="movie", title="y"))
    assert len(page.results) == 1  # unaffected by the source list's later mutation


class _FakeParser:
    def parse(self, release_name: str) -> ParsedRelease:
        return ParsedRelease(raw_title=release_name, clean_title=release_name)


class _FakeIndexer:
    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        return []


class _FakeDownloadClient:
    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        return AddResult(torrent_hash="hash", created=True)

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        return None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        return []

    async def get_statuses_for_hashes(self, hashes: Sequence[str]) -> list[DownloadStatus]:
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

    async def get_default_save_path(self) -> str | None:
        return None

    async def set_location(self, info_hash: str, save_path: str) -> None:
        return None

    async def get_failure_detail(self, info_hash: str) -> FailureDetail | None:
        return None


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
        LogEventRepository,
    ):
        assert proto is not None


async def test_fake_indexer_returns_list() -> None:
    result = await _FakeIndexer().search(IndexerSearchRequest(query="test"))
    assert result == []


# --------------------------------------------------------------------------- #
# Mutating default methods must fail loudly, never silently no-op (#80, #81)
# --------------------------------------------------------------------------- #
class _FileSystemMissingMutators(FileSystemPort):
    """A minimal ``FileSystemPort`` that overrides every *query* method but
    deliberately leaves ``move``/``hardlink_or_copy`` un-overridden, to prove the
    Protocol's own default bodies are what runs (not a subclass override)."""

    def available_bytes(self, path: Path) -> int:
        return 0

    def largest_video_file(self, root: str) -> str | None:
        return None

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        return []

    def delete(self, path: str) -> None:
        return None

    def delete_guard_refuses(self, path: str) -> bool:
        return False

    def reclaimable_bytes(self, path: str) -> int:
        return 0


def test_filesystem_port_move_default_raises_not_implemented() -> None:
    """A ``FileSystemPort`` implementation (or fake) that forgets ``move`` must
    fail loudly at call time (issue #80) — a silent no-op default would let an
    import pipeline report a file as placed without ever moving it."""
    fs = _FileSystemMissingMutators()  # pyright: ignore[reportAbstractUsage]
    with pytest.raises(NotImplementedError):
        fs.move(Path("/src"), Path("/dst"))


def test_filesystem_port_hardlink_or_copy_default_raises_not_implemented() -> None:
    """Same rationale as :func:`test_filesystem_port_move_default_raises_not_implemented`
    for ``hardlink_or_copy`` (issue #80)."""
    fs = _FileSystemMissingMutators()  # pyright: ignore[reportAbstractUsage]
    with pytest.raises(NotImplementedError):
        fs.hardlink_or_copy(Path("/src"), Path("/dst"))


class _LibraryMissingTriggerScan(LibraryPort):
    """A minimal ``LibraryPort`` that overrides every other method but
    deliberately leaves ``trigger_scan`` un-overridden, to prove the Protocol's
    own default body is what runs (not a subclass override)."""

    async def is_available(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        use_cache: bool = True,
        season: int | None = None,
    ) -> bool:
        return False

    async def present_seasons(self, tmdb_id: int) -> frozenset[int]:
        return frozenset()

    async def present_ids(
        self,
        keys: Sequence[tuple[int, Literal["movie", "tv"]]],
        *,
        refresh_absent: bool = False,
    ) -> frozenset[tuple[int, Literal["movie", "tv"]]]:
        return frozenset()

    async def list_sections(self, *, use_cache: bool = True) -> list[LibrarySection]:
        return []

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        return WatchState(watched=False)


async def test_library_port_trigger_scan_default_raises_not_implemented() -> None:
    """A ``LibraryPort`` implementation (or fake) that forgets ``trigger_scan``
    must fail loudly at call time (issue #81) — a silent no-op default would let
    a future adapter or fake falsely report a completed Plex scan after an
    import or purge."""
    library = _LibraryMissingTriggerScan()  # pyright: ignore[reportAbstractUsage]
    with pytest.raises(NotImplementedError):
        await library.trigger_scan("/movies/Some.Movie", "movie")


# --------------------------------------------------------------------------- #
# Every port Protocol method must fail loudly, never silently no-op (#204)
# --------------------------------------------------------------------------- #
# The one intentional exception: ``DownloadClientPort.get_failure_detail`` is
# explicitly documented "MUST NOT raise" -- a best-effort diagnostic enrichment
# called only for an already-failed torrent, where a missing override degrading
# to "nothing more specific to say" (``None``) is the correct, documented
# behaviour, not a silent trap like the other 16 methods issue #204 fixed.
_ALLOWED_SILENT_PROTOCOL_DEFAULTS: frozenset[tuple[str, str]] = frozenset(
    {("DownloadClientPort", "get_failure_detail")}
)


def _protocol_methods_with_silent_default(module_path: Path) -> list[tuple[str, str, int]]:
    """Return ``(class_name, method_name, lineno)`` for every ``Protocol``
    method defined in ``module_path`` whose body has no explicit ``raise`` --
    i.e. a subclass that omits an override falls through to Python's IMPLICIT
    ``return None`` instead of failing loudly at call time."""
    tree = ast.parse(module_path.read_text())
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        is_protocol = any(
            (isinstance(base, ast.Name) and base.id == "Protocol")
            or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
            for base in node.bases
        )
        if not is_protocol:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = item.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]  # strip the docstring; it is not a statement
            if not any(isinstance(stmt, ast.Raise) for stmt in body):
                found.append((node.name, item.name, item.lineno))
    return found


def test_no_protocol_method_has_a_silent_docstring_only_body() -> None:
    """Guard for issues #80/#81/#204: EVERY port ``Protocol`` method must fail
    loudly (``raise NotImplementedError``) by default when a subclass omits an
    override — never silently fall through to Python's implicit ``return
    None``. A silent ``None`` default can turn missing adapter/repository
    behaviour into a FALSE SUCCESS (e.g. ``purge_service.remove_torrent``
    reporting a removal that never ran when ``DownloadClientPort.remove`` was
    never overridden — see
    ``test_missing_remove_implementation_can_never_report_purge_success`` in
    ``tests/services/test_purge_service.py``).

    This statically scans every ``ports/*.py`` module's source rather than
    exercising each method at runtime, so a FUTURE Protocol method added
    without an explicit ``raise`` fails this test immediately — the same
    silent trap issue #204 fixed for the other 16 methods can never be
    reintroduced unnoticed.
    """
    ports_dir = Path(ports_pkg.__file__).parent
    violations: list[str] = []
    for module_file in sorted(ports_dir.glob("*.py")):
        if module_file.name == "__init__.py":
            continue
        for class_name, method_name, lineno in _protocol_methods_with_silent_default(module_file):
            if (class_name, method_name) in _ALLOWED_SILENT_PROTOCOL_DEFAULTS:
                continue
            violations.append(f"{module_file.name}:{lineno} {class_name}.{method_name}")
    assert violations == [], (
        "Protocol method(s) with a silent docstring-only default body (must "
        f"`raise NotImplementedError` instead): {violations}"
    )
