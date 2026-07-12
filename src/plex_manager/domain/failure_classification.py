"""Failed-download taxonomy — environmental vs release-caused (issue #181).

The live 2026-07-11/12 incident: three ``Last Man on Earth`` S04 season packs
failed with the app-visible reason ``client reports 'error'`` — the REAL cause
(from qbittorrent.log) was ``file_open ... error: Permission denied`` on the
staging dir (the host download directory was root-owned). The bare reconcile
detection could not tell "the release is dead" from "the HOST cannot write the
staging dir" and blocklisted a perfectly good release on EVERY such failure —
so once the host problem was fixed, the app had blocked itself out of every
acceptable pack for that title (north star #1: correction without a terminal;
north star #3: honesty over silence — a blocklist is a permanent, silent
verdict on the RELEASE for a failure that was never the release's fault).

:func:`classify_failure_detail` is the pure judgment call the service layer
(``queue_service``) gates its Phase-C blocklist write on: a
:data:`FailureClass.environmental` failure must NOT blocklist the release —
the release is exonerated, and (being un-blocklisted) the very next auto-grab
cycle can resolve back to the SAME release once the environment is fixed. Only
:data:`FailureClass.release_fault` (the pre-existing behavior, and the safe
default for anything unrecognized) still blocklists.

The taxonomy is intentionally a small, explicit ALLOWLIST of environmental
signal substrings (POSIX errno-derived libtorrent/qBittorrent messages, which
are the same across qBittorrent versions since they come from the OS, not the
client) rather than a denylist of release-fault signals: an unrecognized
detail (or no detail at all — the client offline, or nothing more specific
than ``client reports 'error'``) is NOT safe to assume is environmental, so it
falls through to the existing release-fault default. Wrongly skipping a
blocklist would re-grab a genuinely dead release forever; the cost of the
conservative default is bounded to "no worse than today".

Pure domain: stdlib only, operates over plain strings — no I/O, no adapter, no
client. Fetching the detail this classifies is adapter work (see
``DownloadClientPort.get_failure_detail``); this module only judges the
resulting text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["FailureClass", "classify_failure_detail"]


class FailureClass(StrEnum):
    """Whether a failed download's cause was the HOST's fault or the
    RELEASE's fault. Only :data:`release_fault` should ever blocklist a
    release — see the module docstring."""

    environmental = "environmental"
    release_fault = "release_fault"


# Lowercased substrings that, if found ANYWHERE in a (lowercased) failure
# detail, mark it environmental. Each is a POSIX errno-derived message
# libtorrent/qBittorrent emits verbatim regardless of client version (disk
# full, permission denied, read-only mount, generic I/O error, missing
# staging files) — the exact class of host-side problem issue #181's
# incident was. ``missingfiles`` also covers qBittorrent's own raw
# ``missingFiles`` torrent state (``reconciler._RAW_STATE_MAP``): data that
# vanished from disk out from under an otherwise-healthy torrent is almost
# always a host/mount problem (a moved/deleted staging dir, an unmounted
# volume), never something wrong with the RELEASE itself.
_ENVIRONMENTAL_FAILURE_PATTERNS: Final[tuple[str, ...]] = (
    "permission denied",
    "read-only file system",
    "no space left on device",
    "disk quota exceeded",
    "no such file or directory",
    "input/output error",
    "i/o error",
    "missingfiles",
)


def classify_failure_detail(detail: str | None) -> FailureClass:
    """Classify a failed download's (already-enriched) reason/detail text.

    ``detail`` is matched case-insensitively against
    :data:`_ENVIRONMENTAL_FAILURE_PATTERNS`; the first match wins.
    ``None`` or an empty string (no detail could be fetched/enriched, or the
    client had nothing further to say beyond the bare raw-state reason) — and
    any detail matching none of the patterns — is
    :data:`FailureClass.release_fault`, the pre-existing reconcile-default
    behavior. Total: never raises, on any input.
    """
    if not detail:
        return FailureClass.release_fault
    lowered = detail.lower()
    for pattern in _ENVIRONMENTAL_FAILURE_PATTERNS:
        if pattern in lowered:
            return FailureClass.environmental
    return FailureClass.release_fault
