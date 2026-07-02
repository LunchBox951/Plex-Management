"""Shared correction primitives (ADR-0014): the mechanical, best-effort building
blocks the correction verbs (report-issue, cancel) and the disk-pressure eviction
sweep all compose, so the load-bearing safety logic lives in ONE place.

Three primitives, each best-effort by design (a failure is logged, never silent,
and never raised) — the DB state change a caller commits around them is the
authoritative record; a client/Plex/FS hiccup here must never undo it:

* :func:`purge_library_path` — the root-guarded ``fs.delete`` of a stored
  ``library_path`` breadcrumb, plus the hardlink-aware reclaimable-bytes
  accounting (measured BEFORE the delete, since a file's link count can only be
  read while it still exists). Returns a :class:`PurgeResult` classifying the
  outcome (``deleted`` / ``refused`` by the containment guard / ``error``); the
  CALLER logs, so each keeps its own context-appropriate message and logger.
* :func:`trigger_library_scan` — the best-effort Plex refresh (delete-file-then-
  trigger_scan is how a title/season is removed from Plex; there is no
  ``LibraryPort`` delete API and none is needed).
* :func:`remove_torrent` — the best-effort ``qbt.remove(delete_files=True)`` that
  closes the "a blocklisted / cancelled download keeps seeding forever" leak.
  Removing an already-gone hash is a no-op success (qBittorrent's
  ``/torrents/delete`` tolerates unknown hashes).

Hardlink caveat (ADR-0014): a same-filesystem import ``hardlink_or_copy``-links
the library file to the download client's seed copy, so BOTH the torrent-with-data
AND the library file must be removed to actually reclaim the space and eliminate
the bad release — a correction verb calls :func:`remove_torrent` AND
:func:`purge_library_path`, never just one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from plex_manager.adapters.filesystem.local import LocalFileSystemError
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError

if TYPE_CHECKING:
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort

__all__ = [
    "PurgeOutcome",
    "PurgeResult",
    "purge_library_path",
    "remove_torrent",
    "trigger_library_scan",
]

_logger = logging.getLogger(__name__)


class PurgeOutcome(StrEnum):
    """How a :func:`purge_library_path` attempt resolved."""

    #: ``fs.delete`` ran (the path was removed, OR was already gone — an
    #: idempotent no-op success within a configured root).
    deleted = "deleted"
    #: The root-containment guard refused: the path resolves outside every
    #: configured library root (a stale/misconfigured breadcrumb). Nothing deleted.
    refused = "refused"
    #: An ``OSError`` (permission denied, I/O error) while deleting. Nothing (or
    #: only part of a tree) deleted; the caller may retry later.
    error = "error"


@dataclass(frozen=True)
class PurgeResult:
    """The outcome of a :func:`purge_library_path` attempt.

    ``freed_bytes`` is the hardlink-aware reclaimable total measured before the
    delete (``0`` for anything but a successful ``deleted``). ``detail`` carries
    the guard message (``refused``) or the exception type name (``error``) so the
    caller can log an honest reason; ``None`` on success.
    """

    outcome: PurgeOutcome
    freed_bytes: int
    detail: str | None = None


async def purge_library_path(fs: FileSystemPort, library_path: str) -> PurgeResult:
    """Root-guarded delete of ``library_path`` + hardlink-aware freed-bytes accounting.

    Both the size accounting and the delete are real, synchronous disk I/O
    (``os.stat``/``os.walk``/``shutil.rmtree``), so each runs off the event loop
    via ``asyncio.to_thread`` — mirroring every other blocking FS primitive in the
    services layer (see ``eviction_service._size_bytes``/``_evict_one``).

    The delete goes through :meth:`FileSystemPort.delete`, whose implementation
    refuses (raises :class:`LocalFileSystemError`) any path resolving outside a
    configured library root and treats an already-gone in-root path as an
    idempotent no-op success. Classifies the result rather than logging it: the
    caller (eviction / report-issue) owns the context-specific message + logger.
    """
    # Reclaimable bytes MUST be read before the delete: a file's link count is
    # only knowable while the path still exists (hardlink-aware accounting, ADR-0012
    # / ADR-0014). A measurement failure is "unknown -> 0", never an abort.
    try:
        freed_bytes = await asyncio.to_thread(fs.reclaimable_bytes, library_path)
    except OSError:
        freed_bytes = 0

    try:
        await asyncio.to_thread(fs.delete, library_path)
    except LocalFileSystemError as exc:
        return PurgeResult(PurgeOutcome.refused, 0, str(exc))
    except OSError as exc:
        return PurgeResult(PurgeOutcome.error, 0, type(exc).__name__)
    return PurgeResult(PurgeOutcome.deleted, freed_bytes)


async def trigger_library_scan(
    library: LibraryPort,
    *,
    library_path: str,
    media_type: Literal["movie", "tv"],
    context: str,
    extra: dict[str, object] | None = None,
) -> None:
    """Best-effort Plex refresh so a removed title/season drops out of the library.

    Delete-file-then-trigger_scan is how the app removes an item from Plex (there
    is no ``LibraryPort`` delete API). Best-effort and symmetric with the import
    pipeline's post-place scan: the DB state change the caller committed already
    stands, so a Plex outage here is logged (Plex catches up on its next scheduled
    scan), never a failure that undoes the completed correction/eviction.

    ``context`` is a static description of the caller (e.g. ``"eviction"``,
    ``"report-issue"``) — logged verbatim, never an interpolated request-derived
    string, so the log-injection convention holds. Correlation ids go via
    ``extra``.
    """
    try:
        await library.trigger_scan(library_path, media_type)
    except (PlexLibraryError, PlexAuthError) as exc:
        _logger.warning(
            "post-%s Plex refresh failed (%s); Plex may briefly still report the "
            "item present until its next scheduled scan",
            context,
            type(exc).__name__,
            extra=extra,
        )


async def remove_torrent(
    qbt: DownloadClientPort,
    torrent_hash: str,
    *,
    context: str,
    extra: dict[str, object] | None = None,
) -> None:
    """Best-effort ``qbt.remove(delete_files=True)`` — closes the seeding leak.

    A blocklisted / cancelled / reported download must not keep seeding and
    holding disk. Removing an already-gone hash is a no-op success (qBittorrent's
    ``/torrents/delete`` tolerates an unknown hash). A genuine failure is logged
    (honesty over silence) but never raised: the caller's blocklist/status writes
    have already committed and must not be undone by a client hiccup — the leak is
    made VISIBLE in the log rather than aborting the correction.

    ``context`` is a static caller description, logged verbatim (never an
    interpolated request-derived string — log-injection convention); the
    torrent hash and any correlation ids go via ``extra``. A torrent hash is not a
    secret; the grab source (which embeds a Prowlarr api key) is never logged here.
    """
    try:
        await qbt.remove(torrent_hash, delete_files=True)
    except Exception:
        # Best-effort: surface (log), never abort the correction. Broad by design
        # -- any client-side failure (network, auth, 5xx) must not undo the
        # already-committed blocklist/status writes; mirrors grab_service's own
        # orphan-torrent cleanup on a lost parallel grab.
        _logger.warning(
            "failed to remove torrent after %s; it may keep seeding until removed manually",
            context,
            exc_info=True,
            extra=extra,
        )
