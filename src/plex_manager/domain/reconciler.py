"""Pure download reconciler — diff the DB read-model against the client snapshot.

``reconcile`` is a **pure function**: it takes the active DB rows
(:class:`DownloadRecord`), the live client snapshot (:class:`DownloadStatus`),
and the current time as an injected parameter, and returns the list of state
transitions the caller should persist. It never touches the DB, the client, or
the clock — so it is trivially unit-testable and idempotent (re-running on the
same inputs yields ``[]``).

The raw qBittorrent state strings are mapped to :class:`DownloadState` via
``_RAW_STATE_MAP``, which covers every state the qBittorrent adapter emits
verbatim. An *unknown* state is never silently dropped: it falls back to
``Downloading`` and the transition's ``reason`` records the unmapped string so it
surfaces (north star: honesty over silence).

Absence handling mirrors Radarr's ``UpdateTrackable`` with a grace window: a hash
that vanishes from the client is first surfaced as ``ClientMissing`` (not failed);
only once ``missing_grace`` has elapsed since ``first_seen_at`` does it escalate
to ``FailedPending``. This avoids failing a torrent the client merely hasn't
listed yet on first poll.

The grace anchor is ``first_seen_at`` — the moment a torrent was *first observed
absent*. A row that has no anchor yet can NEVER fail: the first absent cycle only
surfaces ``ClientMissing`` and asks the caller to stamp the anchor (via
``StateTransition.set_first_seen_at``). Without that rule an un-anchored row would
collapse the grace to a single poll — a delayed form of the prototype's
fail-on-first-miss drift bug.

Pure domain: depends only on stdlib, ``ports`` DTOs, and sibling domain modules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from plex_manager.domain.events import DownloadFailed
from plex_manager.domain.state_machine import ACTIVE_STATES, DownloadState
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.repositories import DownloadRecord

__all__ = [
    "StateTransition",
    "failed_download_events",
    "reconcile",
]


# Every raw qBittorrent ``state`` string the adapter passes through verbatim,
# mapped to its domain ``DownloadState``. Download-side and paused-download states
# are all "still ours, still downloading"; the upload/complete/checking-up family
# means the bytes are on disk and import is deferred (alpha stops at the grab).
_RAW_STATE_MAP: Mapping[str, DownloadState] = {
    # actively (or about to be) pulling bytes
    "downloading": DownloadState.Downloading,
    "forcedDL": DownloadState.Downloading,
    "stalledDL": DownloadState.Downloading,
    "queuedDL": DownloadState.Downloading,
    "checkingDL": DownloadState.Downloading,
    "checkingResumeData": DownloadState.Downloading,
    # paused mid-download — still our torrent, not a failure
    "pausedDL": DownloadState.Downloading,
    "stoppedDL": DownloadState.Downloading,
    # fetching metadata (magnet without .torrent yet)
    "metaDL": DownloadState.MetadataFetching,
    "forcedMetaDL": DownloadState.MetadataFetching,
    # download complete -> seeding / checking-up / moving: import deferred in alpha
    "uploading": DownloadState.ImportPending,
    "stalledUP": DownloadState.ImportPending,
    "pausedUP": DownloadState.ImportPending,
    "stoppedUP": DownloadState.ImportPending,
    "queuedUP": DownloadState.ImportPending,
    "checkingUP": DownloadState.ImportPending,
    "forcedUP": DownloadState.ImportPending,
    "moving": DownloadState.ImportPending,
    # hard client-side failure
    "error": DownloadState.FailedPending,
    "missingFiles": DownloadState.FailedPending,
}

# Conservative fallback for a state qBittorrent reports that we don't know about:
# keep tracking it as a live download rather than failing or dropping it.
_UNKNOWN_FALLBACK: DownloadState = DownloadState.Downloading

# Status string values the reconciler is willing to move on a cycle.
_ACTIVE_STATUS_VALUES: frozenset[str] = frozenset(state.value for state in ACTIVE_STATES)


@dataclass(frozen=True)
class StateTransition:
    """A single state change the caller should persist (and maybe act on).

    ``from_state`` is the row's current ``downloads.status`` (a raw ``str``);
    ``to_state`` is the target domain state. ``reason`` is a human-readable note
    explaining the move (e.g. the client's raw state, or why a hash is missing) —
    never ``None`` for an absence/unknown case, so nothing is swallowed.

    ``set_first_seen_at`` signals the caller to stamp ``downloads.first_seen_at``
    to *now* when persisting this transition. It is set only when a torrent is
    first observed absent and has no grace anchor yet; the caller must honour it
    (``DownloadRepository.update_status(..., first_seen_at=now)``) or the
    missing-grace window can never start.
    """

    download_id: int
    torrent_hash: str
    from_state: str
    to_state: DownloadState
    reason: str | None
    set_first_seen_at: bool = False


def _map_raw_state(raw_state: str) -> tuple[DownloadState, str | None]:
    """Map a raw client state to a domain state, surfacing unknown strings."""
    mapped = _RAW_STATE_MAP.get(raw_state)
    if mapped is None:
        return (
            _UNKNOWN_FALLBACK,
            f"unknown client state {raw_state!r}; tracking as downloading",
        )
    return mapped, f"client reports {raw_state!r}"


def reconcile(
    rows: Sequence[DownloadRecord],
    client: Sequence[DownloadStatus],
    *,
    now: datetime,
    missing_grace: timedelta = timedelta(minutes=10),
) -> list[StateTransition]:
    """Diff active DB rows against the client snapshot; return needed transitions.

    Pure and idempotent: re-running with the persisted results applied yields an
    empty list. Only rows whose status is one of :data:`ACTIVE_STATES` are
    considered; everything else (Searching, the import sub-states, terminal) is
    left alone.
    """
    snapshot: dict[str, DownloadStatus] = {status.info_hash.lower(): status for status in client}
    transitions: list[StateTransition] = []

    for row in rows:
        if row.status not in _ACTIVE_STATUS_VALUES:
            continue

        present = snapshot.get(row.torrent_hash.lower())
        if present is not None:
            target, reason = _map_raw_state(present.raw_state)
            if target.value != row.status:
                transitions.append(
                    StateTransition(
                        download_id=row.id,
                        torrent_hash=row.torrent_hash,
                        from_state=row.status,
                        to_state=target,
                        reason=reason,
                    )
                )
            continue

        # Absent from the client snapshot — apply the missing-grace policy.
        transition = _reconcile_absent(row, now=now, missing_grace=missing_grace)
        if transition is not None:
            transitions.append(transition)

    return transitions


def _reconcile_absent(
    row: DownloadRecord,
    *,
    now: datetime,
    missing_grace: timedelta,
) -> StateTransition | None:
    """Decide the transition for a row whose hash is absent from the client.

    The grace is anchored on ``first_seen_at`` (the moment the torrent was first
    seen absent):

    * **No anchor yet** — surface ``ClientMissing`` and signal the caller to stamp
      ``first_seen_at=now`` (``set_first_seen_at=True``). An un-anchored row can
      never fail here, so a single absent snapshot cannot collapse the grace. This
      also re-arms an already-``ClientMissing`` row whose anchor the caller failed
      to persist, rather than fast-failing it.
    * **Anchored, grace elapsed** — escalate to ``FailedPending``.
    * **Anchored, within grace** — surface ``ClientMissing`` (or ``None`` if the
      row is already ``ClientMissing``, keeping the cycle idempotent).
    """
    if row.first_seen_at is None:
        # First cycle this torrent is missing (or the anchor was never persisted):
        # start/restart the grace window; do NOT fail without an anchor to wait on.
        return StateTransition(
            download_id=row.id,
            torrent_hash=row.torrent_hash,
            from_state=row.status,
            to_state=DownloadState.ClientMissing,
            reason="absent from client snapshot (grace window started)",
            set_first_seen_at=True,
        )

    if (now - row.first_seen_at) >= missing_grace:
        return StateTransition(
            download_id=row.id,
            torrent_hash=row.torrent_hash,
            from_state=row.status,
            to_state=DownloadState.FailedPending,
            reason="absent from client snapshot beyond missing grace",
        )

    # Anchored and still within grace.
    if row.status == DownloadState.ClientMissing.value:
        return None  # already surfaced; nothing to persist this cycle
    return StateTransition(
        download_id=row.id,
        torrent_hash=row.torrent_hash,
        from_state=row.status,
        to_state=DownloadState.ClientMissing,
        reason="absent from client snapshot (within missing grace)",
    )


def failed_download_events(
    transitions: Sequence[StateTransition],
    records: Sequence[DownloadRecord] = (),
    *,
    occurred_at: datetime | None = None,
) -> list[DownloadFailed]:
    """Build :class:`DownloadFailed` events from the ``FailedPending`` transitions.

    A thin, pure constructor — it neither publishes nor performs I/O. The service
    layer subscribes the blocklist writer and re-search trigger to the bus and
    publishes these. ``records`` (optional) enriches each event with the row's
    ``tmdb_id``; ``occurred_at`` is passed through to the event (the clock is the
    caller's, never read here).
    """
    by_id: dict[int, DownloadRecord] = {record.id: record for record in records}
    events: list[DownloadFailed] = []
    for transition in transitions:
        if transition.to_state is not DownloadState.FailedPending:
            continue
        record = by_id.get(transition.download_id)
        events.append(
            DownloadFailed(
                torrent_hash=transition.torrent_hash,
                source_title=transition.torrent_hash,
                reason=transition.reason or "download failed",
                tmdb_id=record.tmdb_id if record is not None else None,
                occurred_at=occurred_at,
            )
        )
    return events
