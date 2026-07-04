"""Shared correction primitives (ADR-0014): root-guarded purge, scan, torrent remove.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` so the root-containment
guard is genuinely exercised (the same posture as ``test_eviction_service``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.services import purge_service
from plex_manager.services.purge_service import PurgeOutcome
from tests.web.fakes import FakeLibrary, FakeQbittorrent


async def test_purge_deletes_an_in_root_file_and_reports_freed_bytes(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Some Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 2048
    assert not target.exists()


async def test_purge_refuses_a_path_outside_every_configured_root(tmp_path: Path) -> None:
    # The root-guard rejection: a breadcrumb resolving OUTSIDE the configured roots
    # must be refused, never deleted -- the load-bearing safety guard.
    root = tmp_path / "movies"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "victim.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"keep me")
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert result.detail is not None
    assert outside.exists()  # never touched


class _RecordingReclaimFileSystem(LocalFileSystem):
    """A :class:`LocalFileSystem` that records every ``reclaimable_bytes`` call, so a
    test can prove the (potentially huge, recursive) measurement is SKIPPED for an
    out-of-root breadcrumb the containment guard refuses."""

    def __init__(self, library_roots: list[str]) -> None:
        super().__init__(library_roots=library_roots)
        self.reclaim_calls: list[str] = []

    def reclaimable_bytes(self, path: str) -> int:
        self.reclaim_calls.append(path)
        return super().reclaimable_bytes(path)


async def test_purge_refuses_out_of_root_path_without_measuring_it(tmp_path: Path) -> None:
    # An out-of-root breadcrumb pointing at a real (possibly enormous) directory must
    # fail CLOSED and FAST -- containment is checked BEFORE reclaimable_bytes, so the
    # recursive walk never runs on a tree the delete was only going to refuse anyway.
    root = tmp_path / "movies"
    root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "victim.mkv").write_bytes(b"keep me")
    fs = _RecordingReclaimFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside_dir))

    assert result.outcome is PurgeOutcome.refused
    assert fs.reclaim_calls == []  # measurement was never entered for the out-of-root path
    assert outside_dir.exists()

    # Sanity: an IN-root path DOES get measured (the guard only short-circuits bad paths).
    target = root / "Some Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    ok = await purge_service.purge_library_path(fs, str(target))
    assert ok.outcome is PurgeOutcome.deleted
    assert fs.reclaim_calls == [str(target)]


async def test_purge_already_gone_in_root_path_is_an_idempotent_deleted(tmp_path: Path) -> None:
    # A breadcrumb pointing at an already-removed (but in-root) path is an honest,
    # idempotent success -- not an error (a retried purge must not fail).
    root = tmp_path / "movies"
    root.mkdir()
    gone = root / "already-removed.mkv"
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(gone))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 0


async def test_remove_torrent_records_delete_with_data() -> None:
    qbt = FakeQbittorrent()
    await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert qbt.removed == [("a" * 40, True)]


class _RaisingRemoveQbt(FakeQbittorrent):
    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise RuntimeError("qbt is down")


async def test_remove_torrent_is_best_effort_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Honesty over silence: a genuine client failure is LOGGED (the leak is made
    # visible) but never raised -- a correction must not be undone by a client hiccup.
    qbt: DownloadClientPort = _RaisingRemoveQbt()
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        await purge_service.remove_torrent(qbt, "b" * 40, context="a test")
    assert "failed to remove torrent" in caplog.text


async def test_trigger_library_scan_records_the_scan() -> None:
    library = FakeLibrary()
    await purge_service.trigger_library_scan(
        library, library_path="/lib/movies/x", media_type="movie", context="report-issue"
    )
    assert library.scan_calls == [("/lib/movies/x", "movie")]
