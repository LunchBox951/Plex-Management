"""Download lifecycle state machine — the domain ``DownloadState`` + legal moves.

The state vocabulary mirrors Radarr's ``TrackedDownloadState`` with the alpha
additions the design calls for: a download is ``Searching`` until a release is
grabbed, then progresses through ``Downloading`` / ``MetadataFetching`` to
``ImportPending`` once the client reports it complete, and on through the import
sub-states. ``FailedPending`` is the "detected, not yet blocklisted" pause; once
the blocklist + re-search fires it becomes ``Failed``. ``NoAcceptableRelease`` is
a surfaced, retryable terminal — never a silent drop.

The enum *values* are the lowercase strings written to ``downloads.status`` (a
plain ``String`` column); the ``DownloadRecord`` DTO reads them back as ``str``.

``TRANSITIONS`` encodes the legal graph; ``is_legal_transition`` is the guard the
service layer uses before persisting a hand-driven move. The reconciler maps the
client snapshot to a target state independently — legality is enforced by the
caller, not baked into the (pure) mapping.

Pure domain: stdlib only. Imports no adapter, web, or I/O library.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "DownloadState",
    "is_legal_transition",
]


class DownloadState(StrEnum):
    """The domain state of a tracked download (persisted to ``downloads.status``)."""

    Searching = "searching"
    Downloading = "downloading"
    MetadataFetching = "metadata_fetching"
    ImportPending = "import_pending"
    ImportBlocked = "import_blocked"
    Importing = "importing"
    Imported = "imported"
    FailedPending = "failed_pending"
    Failed = "failed"
    NoAcceptableRelease = "no_acceptable_release"
    ClientMissing = "client_missing"


# Legal lifecycle graph. A state absent as a key (or mapped to an empty set) is
# terminal: it re-enters the pipeline via a NEW search, not a transition.
TRANSITIONS: Mapping[DownloadState, frozenset[DownloadState]] = {
    DownloadState.Searching: frozenset(
        {DownloadState.Downloading, DownloadState.NoAcceptableRelease}
    ),
    DownloadState.Downloading: frozenset(
        {
            DownloadState.MetadataFetching,
            DownloadState.ImportPending,
            DownloadState.FailedPending,
            DownloadState.ClientMissing,
            DownloadState.Downloading,
        }
    ),
    DownloadState.MetadataFetching: frozenset(
        {
            DownloadState.Downloading,
            DownloadState.FailedPending,
            DownloadState.ClientMissing,
        }
    ),
    DownloadState.ImportPending: frozenset({DownloadState.Importing, DownloadState.ImportBlocked}),
    DownloadState.Importing: frozenset({DownloadState.Imported, DownloadState.ImportBlocked}),
    DownloadState.ImportBlocked: frozenset({DownloadState.Importing}),
    DownloadState.FailedPending: frozenset({DownloadState.Failed}),
    DownloadState.ClientMissing: frozenset(
        {DownloadState.Downloading, DownloadState.FailedPending}
    ),
    # Terminal: no outgoing edges.
    DownloadState.Failed: frozenset(),
    DownloadState.Imported: frozenset(),
    DownloadState.NoAcceptableRelease: frozenset(),
}


# Effectively terminal for the download. ``Failed`` / ``NoAcceptableRelease``
# re-enter the pipeline only via a fresh search. These string values intentionally
# match the literals the P2 ``SqlDownloadRepository.list_active`` filters on.
TERMINAL_STATES: frozenset[DownloadState] = frozenset(
    {DownloadState.Imported, DownloadState.Failed, DownloadState.NoAcceptableRelease}
)


# The states the reconciler polls and may move on each cycle. A row in any other
# state (Searching, the import sub-states, or a terminal state) is left untouched
# by the reconciler.
ACTIVE_STATES: frozenset[DownloadState] = frozenset(
    {
        DownloadState.Downloading,
        DownloadState.MetadataFetching,
        DownloadState.ImportPending,
        DownloadState.ClientMissing,
    }
)


def is_legal_transition(frm: DownloadState, to: DownloadState) -> bool:
    """Return whether moving from ``frm`` to ``to`` is allowed by ``TRANSITIONS``."""
    return to in TRANSITIONS.get(frm, frozenset())
