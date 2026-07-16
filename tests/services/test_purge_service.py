"""Shared correction primitives (ADR-0014): root-guarded purge, scan, torrent remove.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` so the root-containment
guard is genuinely exercised (the same posture as ``test_eviction_service``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.ports.download_client import (
    AddResult,
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
    FailureDetail,
)
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.services import path_visibility, purge_service
from plex_manager.services.purge_service import PurgeOutcome
from tests.support import assert_task_raises
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


class _BlockedDeleteFileSystem(LocalFileSystem):
    """A real, root-guarded :class:`LocalFileSystem` whose ``delete`` blocks (in
    its worker thread) until the test releases it -- lets a test cancel the
    AWAITING coroutine while the underlying delete thread is still running, the
    exact window issue #128 needs exercised."""

    def __init__(self, root: Path) -> None:
        super().__init__([str(root)])
        self.started = threading.Event()
        self.release = threading.Event()
        self.deleted: list[str] = []

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        super().delete(path)
        self.deleted.append(path)


class _FailingBlockedDeleteFileSystem(_BlockedDeleteFileSystem):
    """Like :class:`_BlockedDeleteFileSystem`, but the delete worker fails
    (``OSError``) once released instead of succeeding."""

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        raise OSError("blocked delete failed")


async def test_cancelled_purge_keeps_path_registered_until_delete_worker_settles(
    tmp_path: Path,
) -> None:
    """Issue #128: cancelling ``purge_library_path`` mid-delete must NOT release
    its ``_ACTIVE_PURGE_PATHS`` registration (observed here via
    ``begin_placement``) until the background delete thread genuinely finishes
    -- otherwise a concurrent import placement (or, in ``eviction_service``, a
    later sweep's crash-recovery) can act on the path while a delete is still
    physically running against it."""
    target = tmp_path / "movies" / "Blocked Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)
    purge_task = asyncio.create_task(
        purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)
    )
    cancelled = False
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        # Give the cancellation a chance to be delivered; the purge must NOT
        # have completed (nor released its registration) yet -- the worker
        # thread is still blocked on ``fs.release``.
        await asyncio.sleep(0)
        assert not purge_task.done()
        assert purge_service.begin_placement(str(target)) is False
        assert target.exists()
    finally:
        fs.release.set()
        try:
            await purge_task
        except asyncio.CancelledError:
            cancelled = True

    assert cancelled is True
    assert fs.deleted == [str(target)]
    assert not target.exists()
    # Only NOW -- after the worker thread actually finished -- is the
    # registration released.
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


async def test_cancelled_purge_with_a_failing_worker_raises_cancelled_not_the_worker_error(
    tmp_path: Path,
) -> None:
    """A cancellation racing an ALSO-failing delete must surface as the
    cancellation (the caller is already unwinding on it and will never read a
    classified ``PurgeResult``), never leak the worker's ``OSError`` as an
    unhandled exception on the event loop."""
    target = tmp_path / "movies" / "Failing Blocked Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        purge_task = asyncio.create_task(purge_service.purge_library_path(fs, str(target)))
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        await asyncio.sleep(0)
        assert not purge_task.done()
        fs.release.set()
        await assert_task_raises(purge_task, asyncio.CancelledError)
        # Let any deferred "exception never retrieved" callback run.
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []
    assert target.exists()  # the (swallowed) delete never actually succeeded


async def test_uncancelled_delete_worker_error_propagates_as_an_error_outcome(
    tmp_path: Path,
) -> None:
    """Without any cancellation racing it, a genuine delete failure is still
    classified and returned as ``PurgeOutcome.error`` exactly as before -- the
    settlement shield changes nothing about the ordinary failure path."""
    target = tmp_path / "movies" / "Failing Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    fs.release.set()

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.error
    assert target.exists()


async def test_delete_to_settlement_propagates_a_worker_oserror_when_not_cancelled(
    tmp_path: Path,
) -> None:
    """Direct unit coverage of the settlement helper: an uncancelled worker
    failure is raised as itself, not swallowed."""
    target = tmp_path / "movies" / "Failing Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    fs.release.set()
    delete_to_settlement = cast(
        Callable[[FileSystemPort, str], Awaitable[None]],
        purge_service.__dict__["_delete_to_settlement"],
    )

    with pytest.raises(OSError, match="blocked delete failed"):
        await delete_to_settlement(fs, str(target))

    assert target.exists()


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


async def test_purge_refuses_an_outside_root_symlink_entry_pointing_inside_the_root(
    tmp_path: Path,
) -> None:
    """Issue #141, purge-level: a symlink ENTRY outside every configured root
    whose target resolves inside one must refuse -- ``delete_guard_refuses``
    (the same predicate ``purge_library_path`` checks up front) must not be
    fooled into treating the dereferenced target's containment as clearance to
    unlink the outside-root entry itself."""
    root = tmp_path / "movies"
    root.mkdir()
    real_target = root / "movie.mkv"
    real_target.write_bytes(b"x" * 100)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_link = outside / "link.mkv"
    outside_link.symlink_to(real_target)
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside_link))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert outside_link.is_symlink()  # untouched
    assert real_target.exists()  # untouched


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


async def test_purge_deletes_a_path_under_an_anime_root_when_guard_includes_it(
    tmp_path: Path,
) -> None:
    """ADR-0015: an anime title's ``library_path`` lives under its own anime
    root, not ``movies_root``/``tv_root``. The delete-guard must include the
    anime root too, or the purge is silently refused and the bad file stays on
    disk after a "successful" blocklist + re-search (the regression the
    routing feature would otherwise introduce)."""
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    target = anime_root / "Some Anime Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    fs = LocalFileSystem(library_roots=[str(movies_root), str(anime_root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 2048
    assert not target.exists()


async def test_purge_refuses_an_anime_path_when_the_guard_omits_the_anime_root(
    tmp_path: Path,
) -> None:
    """The regression this ADR-0015 fix prevents, encoded directly: without the
    anime root in the guard's allowlist, an in-anime-root breadcrumb is
    indistinguishable from any other out-of-root path and is refused, not
    deleted."""
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    target = anime_root / "Some Anime Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    # Anime root deliberately OMITTED from library_roots.
    fs = LocalFileSystem(library_roots=[str(movies_root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert target.exists()  # never touched -- the bad file would silently remain


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


async def test_remove_torrent_skips_the_poll_when_content_path_is_unknown() -> None:
    # No content_path reported at all (e.g. a metadata-only torrent) -- nothing
    # distinct to verify, so remove_torrent returns immediately.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(info_hash="a" * 40, name="x", raw_state="downloading", content_path=None)
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


async def test_remove_torrent_returns_immediately_when_content_path_already_gone(
    tmp_path: Path,
) -> None:
    # Issue #240: the common case -- the client's own deletion already finished
    # (or the path never existed) -- must not pay the poll's sleep interval.
    content = tmp_path / "already-gone"
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_polls_until_content_path_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #240: qBittorrent's own file deletion is ASYNCHRONOUS after its ACK
    # -- remove_torrent must poll for the content path to actually leave disk
    # before returning, so a caller's guard release genuinely means "gone".
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    content = tmp_path / "still-here.mkv"
    content.write_bytes(b"x")
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        content.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)  # reap the helper task cleanly before the test ends
    assert ok is True
    assert not content.exists()


async def test_remove_torrent_logs_and_proceeds_when_the_poll_bound_elapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A client so slow to finish its own deletion that the bound elapses is
    # LOGGED (honesty over silence), not silently swallowed -- but the
    # best-effort call still reports the removal itself as successful (the
    # client DID ack the delete) rather than blocking forever.
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.02)
    content = tmp_path / "never-goes-away.mkv"
    content.write_bytes(b"x")
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")

    assert ok is True
    assert "content path still present" in caplog.text
    assert content.exists()  # untouched -- this function never deletes anything itself


class _RaisingStatusQbt(FakeQbittorrent):
    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        raise RuntimeError("qbt is down")


async def test_remove_torrent_tolerates_a_failed_status_snapshot() -> None:
    # A best-effort SNAPSHOT failure must not abort the removal itself -- it just
    # means there is nothing distinct to poll afterwards.
    qbt: DownloadClientPort = _RaisingStatusQbt()
    ok = await purge_service.remove_torrent(qbt, "c" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_remaps_a_host_namespace_content_path_before_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex review (PR #281): qBittorrent runs on the HOST, so the reported
    # content_path (e.g. /srv/downloads/...) usually does not exist inside this
    # container -- polling it raw would find it "already gone" and release the
    # guard immediately even while qBittorrent is still deleting the real,
    # container-visible file under the /downloads bind mount. Prove the remap
    # happens: the visible file is still on disk under the remapped mount, so
    # the poll must actually run against IT, not the never-existing host path.
    mount = tmp_path / "downloads"
    mount.mkdir()
    video = mount / "Some.Movie.2020.1080p-GRP.mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    host_save_path = "/srv/downloads"
    host_content_path = f"{host_save_path}/{video.name}"
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=video.name,
                raw_state="stalledUP",
                save_path=host_save_path,
                content_path=host_content_path,
            )
        ],
        files={("a" * 40): [DownloadedFile(name=video.name, size_bytes=1024)]},
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        video.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not video.exists()  # the poll genuinely waited on the REMAPPED path


async def test_remove_torrent_skips_the_poll_when_the_host_path_cannot_be_remapped(
    tmp_path: Path,
) -> None:
    # No live download mount matches, and the torrent's own file list can't prove
    # any candidate -- an honest "not visible" skip, never a guess that would
    # release the guard against the wrong (or a stale) path.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="x",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path="/srv/downloads/Some.Movie.2020.1080p-GRP.mkv",
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_polls_save_path_plus_name_when_content_path_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290, finding #1: qBittorrent's adapter NULLS content_path when it
    # merely echoes save_path (a not-yet-resolved torrent). save_path + name is
    # then the live content location (exactly as import_service._resolve_content
    # treats it) and IS distinct -- so the post-ack poll must still run against it,
    # not be skipped, leaving the issue #240 same-hash race open for that class.
    mount = tmp_path / "downloads"
    mount.mkdir()
    video = mount / "Some.Movie.2020.1080p-GRP.mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=video.name,
                raw_state="stalledUP",
                save_path=str(mount),  # host==container here; content_path nulled by the adapter
                content_path=None,
            )
        ],
        files={("a" * 40): [DownloadedFile(name=video.name, size_bytes=1024)]},
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        video.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not video.exists()  # the poll genuinely waited on save_path/name


async def test_remove_torrent_skips_the_poll_when_name_is_absolute() -> None:
    # A defensive guard mirrored from _resolve_content: an absolute ``name`` must
    # never be joined onto save_path (it would escape it), so with no content_path
    # there is nothing distinct to poll -- an honest skip.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="/etc/passwd",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path=None,
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


def test_snapshot_content_path_rejects_a_relative_name_that_escapes_save_path() -> None:
    # PR #309 codex finding: a RELATIVE name can still escape save_path via ``..``
    # components (save_path=/srv/downloads + name=../escape.mkv resolves to
    # /srv/escape.mkv). Mirrors _resolve_content's realpath containment guard:
    # nothing safe to poll, so the snapshot is None -- the post-delete poll must
    # never watch an unrelated path and release/delay the same-hash guard on the
    # wrong file.
    status = DownloadStatus(
        info_hash="a" * 40,
        name="../escape.mkv",
        raw_state="stalledUP",
        save_path="/srv/downloads",
        content_path=None,
    )
    assert purge_service._snapshot_content_path(status) is None  # pyright: ignore[reportPrivateUsage]


def test_snapshot_content_path_rejects_a_nested_dotdot_escape_and_a_dot_name() -> None:
    # ``sub/../../escape`` sneaks the same escape past a naive startswith check on
    # the unresolved join; a ``.`` name resolves back to save_path ITSELF, which is
    # shared by sibling torrents and must never be polled.
    for name in ("sub/../../escape.mkv", "."):
        status = DownloadStatus(
            info_hash="a" * 40,
            name=name,
            raw_state="stalledUP",
            save_path="/srv/downloads",
            content_path=None,
        )
        assert (
            purge_service._snapshot_content_path(status)  # pyright: ignore[reportPrivateUsage]
            is None
        )


def test_snapshot_content_path_joins_a_normal_relative_name() -> None:
    # The guard must not disturb the normal fallback: a plain (even nested)
    # relative name still joins onto save_path unchanged.
    status = DownloadStatus(
        info_hash="a" * 40,
        name="Some.Movie.2020.1080p-GRP/Some.Movie.2020.1080p-GRP.mkv",
        raw_state="stalledUP",
        save_path="/srv/downloads",
        content_path=None,
    )
    assert (
        purge_service._snapshot_content_path(status)  # pyright: ignore[reportPrivateUsage]
        == "/srv/downloads/Some.Movie.2020.1080p-GRP/Some.Movie.2020.1080p-GRP.mkv"
    )


async def test_remove_torrent_skips_the_poll_when_name_escapes_save_path() -> None:
    # End-to-end shape of the guard above: with content_path absent and a
    # ``..``-bearing name, there is nothing distinct AND contained to poll -- the
    # removal still succeeds, the poll is honestly skipped.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="../escape.mkv",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path=None,
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


async def test_remove_torrent_prefers_the_mounted_file_over_an_outside_mount_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290, finding #2: the HOST content_path coincidentally exists in this
    # container as a stale PHANTOM tree OUTSIDE the live /downloads mount, while
    # the REAL file sits under the mount. Watching the phantom would release the
    # same-hash guard immediately while qBittorrent still deletes the real file.
    # The poll must run against the REMAPPED, mounted path -- so it genuinely
    # waits for the real file to leave disk.
    mount = tmp_path / "downloads"
    mount.mkdir()
    real = mount / "Some.Movie.2020.1080p-GRP.mkv"
    real.write_bytes(b"x" * 1024)
    # The phantom: a host-shaped tree that ALSO exists in-container, outside the mount.
    host_save_path = str(tmp_path / "srv" / "downloads")
    phantom = tmp_path / "srv" / "downloads" / real.name
    phantom.parent.mkdir(parents=True)
    phantom.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=real.name,
                raw_state="stalledUP",
                save_path=host_save_path,
                content_path=f"{host_save_path}/{real.name}",
            )
        ],
        files={("a" * 40): [DownloadedFile(name=real.name, size_bytes=1024)]},
    )

    async def _delete_the_real_file() -> None:
        await asyncio.sleep(0.15)
        real.unlink()  # only the mounted file is deleted; the phantom lingers

    deleter = asyncio.create_task(_delete_the_real_file())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not real.exists()  # the poll waited on the MOUNTED path, not the phantom
    assert phantom.exists()  # never touched -- proves the phantom was not what was watched


async def test_visible_content_path_prefers_mounted_remap_over_a_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct unit of finding #2: _visible_content_path must not short-circuit to a
    # verbatim content_path that exists only as a phantom OUTSIDE the live mount.
    mount = tmp_path / "downloads"
    mount.mkdir()
    real = mount / "clip.mkv"
    real.write_bytes(b"x" * 512)
    host_save_path = str(tmp_path / "srv" / "downloads")
    phantom = tmp_path / "srv" / "downloads" / "clip.mkv"
    phantom.parent.mkdir(parents=True)
    phantom.write_bytes(b"x" * 512)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    qbt = FakeQbittorrent(files={("a" * 40): [DownloadedFile(name="clip.mkv", size_bytes=512)]})
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, f"{host_save_path}/clip.mkv", host_save_path
    )
    assert result == str(real)


async def test_visible_content_path_returns_none_when_save_path_is_empty() -> None:
    # No live save-path anchor (a torrent status with no save path reported) --
    # only the verbatim content path counts; a free suffix search is never
    # attempted (mirrors import_service._resolve_visible_content).
    qbt = FakeQbittorrent()
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, "/srv/downloads/gone.mkv", ""
    )
    assert result is None


class _RaisingListFilesQbt(FakeQbittorrent):
    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        raise RuntimeError("qbt is down")


async def test_visible_content_path_tolerates_a_failed_file_list_fetch() -> None:
    # A best-effort remap: a client hiccup fetching the file list must not raise
    # -- it just means there's nothing safely provable to poll.
    qbt: DownloadClientPort = _RaisingListFilesQbt()
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, "/srv/downloads/gone.mkv", "/srv/downloads"
    )
    assert result is None


class _MissingRemoveQbt(DownloadClientPort):
    """A ``DownloadClientPort`` implementation that overrides every method
    EXCEPT ``remove`` -- proving issue #204's fix: the Protocol's own default
    body for ``remove`` now raises ``NotImplementedError`` (never a silent
    implicit ``return None``) when a subclass forgets to override it."""

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

    # ``remove`` deliberately NOT overridden -- the Protocol's own default runs.

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


async def test_missing_remove_implementation_can_never_report_purge_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #204's load-bearing regression: before the fix, a
    ``DownloadClientPort`` implementation that forgot to override ``remove``
    fell through to the Protocol's docstring-only default -- Python's implicit
    ``return None`` -- so ``qbt.remove(...)`` would appear to SUCCEED for a
    torrent that was never actually removed, and ``purge_service.remove_torrent``
    would report ``True``: a blocklisted/cancelled torrent silently kept
    seeding forever with no visible failure anywhere.

    Now the default raises ``NotImplementedError``, which ``remove_torrent``'s
    best-effort ``except Exception`` catches, logs, and reports as ``False`` --
    the caller (``queue_service``) then correctly keeps the durable "removal
    still owed" marker instead of persisting a false "removed" outcome.
    """
    qbt: DownloadClientPort = _MissingRemoveQbt()  # pyright: ignore[reportAbstractUsage]
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        result = await purge_service.remove_torrent(qbt, "c" * 40, context="a test")
    assert result is False
    assert "failed to remove torrent" in caplog.text


async def test_trigger_library_scan_records_the_scan() -> None:
    library = FakeLibrary()
    await purge_service.trigger_library_scan(
        library, library_path="/lib/movies/x", media_type="movie", context="report-issue"
    )
    assert library.scan_calls == [("/lib/movies/x", "movie")]
