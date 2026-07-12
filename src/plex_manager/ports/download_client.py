"""DownloadClientPort — the torrent-client interface (qBittorrent in the alpha).

The port returns a client-neutral :class:`DownloadStatus` DTO; the adapter maps
raw qBittorrent state strings into it (the domain never sees a raw client
string). ``add`` returns the lowercased info-hash. All methods are async — the
adapter uses ``httpx.AsyncClient``.

This DTO lives in the port (not the adapter) so it is the stable cross-boundary
contract the reconciler can depend on without importing an adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = ["AddResult", "DownloadClientPort", "DownloadStatus", "DownloadedFile"]


class AddResult(BaseModel):
    """The outcome of :meth:`DownloadClientPort.add`.

    ``torrent_hash`` is the lowercased info-hash (``""`` when no hash could be
    derived locally -- rare, an opaque ``.torrent`` URL the client fetched
    itself). ``created`` is whether this call GENUINELY added a new torrent:
    ``False`` means the client reported it ALREADY PRESENT (qBittorrent's 409
    add response) and merely resolved to the existing torrent. The distinction
    is load-bearing for failure cleanup: a grab that loses a race/CAS after the
    add may remove (with data) only a torrent it actually created -- a reused
    pre-existing torrent predates the grab (e.g. a still-seeding import whose
    data can back a live library file via hardlink) and is never the grab's to
    destroy.
    """

    model_config = ConfigDict(frozen=True)

    torrent_hash: str
    created: bool


class DownloadStatus(BaseModel):
    """A point-in-time snapshot of one torrent in the download client.

    ``raw_state`` is the client's own state string (e.g. ``downloading``,
    ``stoppedUP``); the reconciler maps it to a domain ``DownloadState``. The
    ``ratio_limit`` / ``*_limit_minutes`` defaults of ``-2`` mean "use the
    client global" (qBittorrent convention); ``-1`` means unlimited.
    """

    model_config = ConfigDict(frozen=True)

    info_hash: str
    name: str
    raw_state: str
    progress: float = 0.0
    ratio: float = 0.0
    save_path: str = ""
    content_path: str | None = None
    eta_seconds: int | None = None
    ratio_limit: float = -2.0
    seeding_time_limit_minutes: int = -2
    inactive_seeding_time_limit_minutes: int = -2
    last_activity_unix: int = 0


class DownloadedFile(BaseModel):
    """One file inside a torrent's content, as reported by the download client.

    ``name`` is the file's path relative to the torrent's save path (the client's
    own ``name`` field); ``size_bytes`` is its size in bytes. The importer uses
    these to locate the completed video file.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    size_bytes: int


@runtime_checkable
class DownloadClientPort(Protocol):
    """Add, monitor, and control torrents in the download client."""

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        """Add a torrent; return its lowercased info-hash + whether it was created.

        A 409 (already present) resolves to the existing hash, never an error --
        reported honestly as ``created=False`` so a caller cleaning up after a
        lost grab never removes a pre-existing torrent it did not create (see
        :class:`AddResult`).

        A non-empty ``save_path`` is a DIRECTED path (issues #133/#157) that the
        implementation must actually honour: an install with global Automatic
        Torrent Management enabled otherwise ignores the per-add path entirely
        and places the torrent per its own category/auto rules, silently
        defeating the direction. Implementations must therefore pin the ADDED
        torrent to manual management whenever ``save_path`` is non-empty
        (without touching the client's global AutoTMM setting or any other
        torrent), and leave the torrent's management mode untouched when
        ``save_path`` is empty (nothing to direct).
        """
        raise NotImplementedError

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        """Return the status for ``info_hash``, or ``None`` if absent."""

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        """Return statuses for all torrents, optionally filtered by category."""
        raise NotImplementedError

    async def get_statuses_for_hashes(self, hashes: Sequence[str]) -> list[DownloadStatus]:
        """Return statuses for exactly the given info-hashes (issue #216).

        The SCOPED counterpart of :meth:`get_all_statuses`: cost is bounded by
        ``len(hashes)``, not by the client's total torrent count, so the frequent
        reconcile poll can stay cheap on a shared qBittorrent instance with a
        large unrelated inventory. Category filtering is deliberately NOT an
        alternative here — an operator recategorizing a tracked torrent, or an
        imported/terminal torrent lingering under the app category, must never
        make a still-tracked hash silently disappear from the snapshot.

        An empty ``hashes`` means nothing to ask about and returns ``[]`` with
        NO client round-trip. A hash the client does not recognize is simply
        absent from the result (never an error) — the reconciler already treats
        a hash missing from the snapshot as the honest ``ClientMissing`` signal,
        so implementations must not raise for an unknown/stale hash.
        """
        raise NotImplementedError

    async def pause(self, info_hash: str) -> None:
        """Pause the torrent identified by ``info_hash``."""

    async def resume(self, info_hash: str) -> None:
        """Resume the torrent identified by ``info_hash``."""

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        """Remove the torrent, deleting its files when ``delete_files`` is set."""

    async def set_category(self, info_hash: str, category: str) -> None:
        """Set the torrent's category (used to mark imported items)."""

    async def get_save_path(self, info_hash: str) -> str | None:
        """Return the torrent's current save path, re-read from the client."""

    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        """Return the torrent's files (relative path + size) so the importer can
        locate the completed video file."""
        raise NotImplementedError

    async def get_default_save_path(self) -> str | None:
        """Return the client's GLOBAL default save path (``None`` if unreadable).

        Read-only: the port deliberately has no matching setter. This is a
        DIAGNOSTIC signal (the setup/health visibility probe, issues #133/#157) --
        never mutate the operator's shared qBittorrent instance's global config.
        """

    async def set_location(self, info_hash: str, save_path: str) -> None:
        """Relocate an existing torrent's save directory (qBittorrent moves it
        asynchronously; this call only requests the move and returns).

        Per-torrent only -- this is the correction verb for a torrent stranded
        outside the app's visible download mount (issues #133/#157), never a
        bulk sweep and never the client's global default (see
        :meth:`get_default_save_path`)."""

    async def get_failure_detail(self, info_hash: str) -> str | None:
        """Return a best-effort, human-readable detail for why ``info_hash`` is
        in a client-reported error/failure state, or ``None`` if none is
        available (issue #181).

        The reconciler's own raw-state mapping (``client reports 'error'``) is
        honest but useless to an operator with no terminal access: it names the
        SHAPE of the failure, never the CAUSE. This is the diagnostic
        enrichment step -- pulling whatever qBittorrent itself can say about
        THIS torrent (its own app log, its trackers' status messages) so the
        persisted ``failed_reason`` and the service layer's environmental-vs-
        release-fault classification (``domain.failure_classification``) both
        have something real to work with.

        Read-only and DIAGNOSTIC, like :meth:`get_default_save_path`: total,
        best-effort, and MUST NOT raise -- called only for an ALREADY-failed
        torrent, so a transport hiccup enriching it is never itself a reason to
        fail (or abort) the reconcile cycle. Implementations return ``None`` on
        any failure to fetch or on finding nothing more specific to say, never
        letting a client error escape from this call."""
