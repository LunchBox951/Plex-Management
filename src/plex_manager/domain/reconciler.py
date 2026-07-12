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
from datetime import UTC, datetime, timedelta
from typing import Final

from plex_manager.domain.events import DownloadFailed
from plex_manager.domain.state_machine import ACTIVE_STATES, DownloadState
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.repositories import DownloadRecord

__all__ = [
    "METADATA_STALL_WINDOW",
    "STALLED_PROGRESS_WINDOW",
    "StallDetection",
    "StateTransition",
    "detect_stalls",
    "download_deadline",
    "failed_download_events",
    "reconcile",
    "unmapped_client_states",
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
    # download complete -> seeding / checking-up: bytes are settled on disk, ready
    # to import.
    "uploading": DownloadState.ImportPending,
    "stalledUP": DownloadState.ImportPending,
    "pausedUP": DownloadState.ImportPending,
    "stoppedUP": DownloadState.ImportPending,
    "queuedUP": DownloadState.ImportPending,
    "checkingUP": DownloadState.ImportPending,
    "forcedUP": DownloadState.ImportPending,
    # ``moving`` is qBittorrent actively relocating the files: NOT settled. Keep it
    # an active (non-import) state so the importer never reads a half-moved file and
    # blesses a truncated copy; it advances to an *UP state once the move completes.
    "moving": DownloadState.Downloading,
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

    ``clear_first_seen_at`` signals the caller to reset ``downloads.first_seen_at``
    to NULL. It is set when a ``ClientMissing`` torrent reappears in the client and
    recovers to an active state: the stale grace anchor from the prior
    disappearance must be cleared, otherwise a *later* disappearance would measure
    its grace from the old anchor and fail far too fast. ``set_first_seen_at`` and
    ``clear_first_seen_at`` are never both set on one transition.
    """

    download_id: int
    torrent_hash: str
    from_state: str
    to_state: DownloadState
    reason: str | None
    set_first_seen_at: bool = False
    clear_first_seen_at: bool = False


def unmapped_client_states(
    rows: Sequence[DownloadRecord],
    client: Sequence[DownloadStatus],
) -> list[tuple[str, str]]:
    """Return ``(torrent_hash, raw_state)`` for tracked rows on an unknown raw state.

    Pure detector for honesty over silence: an unknown qBittorrent state maps to
    the conservative ``Downloading`` fallback, so when the row is *already*
    ``downloading`` :func:`reconcile` emits no transition and the unmapped string
    would otherwise vanish. The caller logs each pair every cycle so an unexpected
    client state is always surfaced — not only when it happens to change the row's
    state. Scoped to the supplied (active) rows; unrelated torrents in the client
    are ignored. Order follows the client snapshot for deterministic logging.
    """
    tracked = {row.torrent_hash.lower() for row in rows}
    return [
        (status.info_hash, status.raw_state)
        for status in client
        if status.info_hash.lower() in tracked and status.raw_state not in _RAW_STATE_MAP
    ]


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
                # A torrent that was ClientMissing has now reappeared: clear the
                # stale grace anchor so a future disappearance starts a fresh, full
                # missing-grace window rather than inheriting the old one.
                recovered = row.status == DownloadState.ClientMissing.value
                transitions.append(
                    StateTransition(
                        download_id=row.id,
                        torrent_hash=row.torrent_hash,
                        from_state=row.status,
                        to_state=target,
                        reason=reason,
                        clear_first_seen_at=recovered,
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


# --------------------------------------------------------------------------- #
# Stalled-download self-heal (issue #165) — the minimal fixed-cooldown design.
#
# Two fixed windows, module constants so a future web-configurable knob has a
# name to bind to (mirrors ``web/app.py``'s ``_RECONCILE_INTERVAL_SECONDS`` /
# ``auto_grab_service``'s ``BACKOFF_SCHEDULE`` precedent, issue #150). Neither
# is the full adaptive/candidate-count-aware design the issue also sketches —
# deliberately out of scope for this pass.
# --------------------------------------------------------------------------- #
_METADATA_STALL_MINUTES: Final = 45
_STALLED_PROGRESS_HOURS: Final = 3

# Raw states the ``last_activity_unix`` check is willing to flag. Deliberately
# NARROWER than "maps to DownloadState.Downloading": pausedDL/stoppedDL are paused
# by the operator or client, not a failure; queuedDL is waiting its turn behind
# other torrents by design; checkingDL/checkingResumeData is qBittorrent verifying
# pieces (routinely triggered by a restart, which also freezes last_activity from
# before the restart); moving is actively relocating settled bytes. Flagging any of
# those would self-heal (remove + blocklist) a healthy torrent. An unmapped future
# state also stays excluded here, consistent with ``_UNKNOWN_FALLBACK``'s own
# intent to keep tracking rather than fail it.
#
# ``stalledDL`` — qBittorrent's own zero-peer signal — is included here rather
# than trusted on its own: a single transient tick of ``stalledDL`` (a momentary
# zero-peer blip between bursts on an otherwise-healthy, actively-transferring
# torrent) must NOT be enough to self-heal it. Requiring the SAME stale
# ``last_activity_unix`` gate as the frozen-mid-download catch-all means
# ``stalledDL`` only trips once the client has genuinely seen no activity for
# the full ``stalled_progress`` window — never on a one-off report (north star:
# correction, never destruction).
_STALLED_PROGRESS_RAW_STATES: Final[frozenset[str]] = frozenset(
    {"downloading", "forcedDL", "stalledDL"}
)

# Raw states qBittorrent reports as DELIBERATELY not-downloading. The
# never-had-activity catch-all (below) must never self-heal these — an operator
# paused it (pausedDL/stoppedDL), it is queued behind others by design
# (queuedDL), it is verifying pieces after a restart (checkingDL/
# checkingResumeData), or it is relocating settled bytes (moving). Removing +
# blocklisting any of these would destroy a healthy torrent (north star:
# correction, never destruction). ``metaDL``/``forcedMetaDL`` are already
# consumed by the earlier metadata-stall branch (via ``continue``), so they
# never reach the catch-all — no need to list them here too.
_DELIBERATELY_IDLE_RAW_STATES: Final[frozenset[str]] = frozenset(
    {"pausedDL", "stoppedDL", "queuedDL", "checkingDL", "checkingResumeData", "moving"}
)

# Public windows — the single definition ``download_deadline`` and
# ``detect_stalls``'s own defaults both bind to, so there is exactly ONE place
# that knows how long each phase's deadline is.
METADATA_STALL_WINDOW: Final[timedelta] = timedelta(minutes=_METADATA_STALL_MINUTES)
STALLED_PROGRESS_WINDOW: Final[timedelta] = timedelta(hours=_STALLED_PROGRESS_HOURS)

# Persisted-status gate (issue #165 hardening finding): the rows this detector is
# handed come from ``download_repo.list_active()``, whose OWN docstring scopes it
# to "active (non-terminal)" -- a broader set than the two live shapes this
# detector understands. It includes ``failed_pending``, the non-terminal pause
# ``mark_failed``'s own docstring documents as its Phase-A rest stop: an operator
# (or a stranded prior attempt) may have deliberately left a row there with
# ``remove_torrent=False`` / ``blocklist=False`` (keeping the torrent, e.g. to
# inspect it manually), and if its underlying torrent then goes quiet past the
# stall window, keying PURELY off the live raw state would let this detector flag
# it anyway -- the self-heal's ``mark_failed(blocklist=True, remove_torrent=True)``
# would then silently overturn that earlier explicit choice, deleting/blocklisting
# a torrent an operator chose to keep. Also excludes ``import_pending`` and
# ``client_missing``: neither shape is meaningful for a settled-on-disk or
# currently-absent torrent. Mirrors ``reconcile``'s own
# ``row.status not in _ACTIVE_STATUS_VALUES`` gate -- same literal-string-vs-
# persisted-value comparison style, just a narrower set than ``ACTIVE_STATES``.
_STALL_ELIGIBLE_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {DownloadState.Downloading.value, DownloadState.MetadataFetching.value}
)


@dataclass(frozen=True)
class StallDetection:
    """One active download whose stall shape has crossed its fixed cooldown
    window (issue #165). ``shape`` is the honesty-facing label written to both
    the log line and the ``DownloadHistory`` row the service layer records:
    ``"metadata_stall"`` (stuck fetching magnet metadata) or
    ``"stalled_progress"`` (a ``stalledDL``/``downloading``/``forcedDL``
    torrent whose last client activity is older than the window — covering
    both qBittorrent's own zero-peer signal and a frozen mid-download stall,
    and requiring the same staleness gate for both so a single transient
    ``stalledDL`` tick on an otherwise-healthy torrent is never enough on its
    own).
    """

    download_id: int
    torrent_hash: str
    shape: str


def detect_stalls(
    rows: Sequence[DownloadRecord],
    client: Sequence[DownloadStatus],
    *,
    now: datetime,
    metadata_stall: timedelta = METADATA_STALL_WINDOW,
    stalled_progress: timedelta = STALLED_PROGRESS_WINDOW,
) -> list[StallDetection]:
    """Detect rows stuck in metadata-fetching or with a dead/frozen download,
    pure and read-only (no I/O, no side effects — the service layer decides what
    to do with each :class:`StallDetection`, exactly like :func:`reconcile`).

    Both windows are anchored on ``row.added_at`` (when the row was grabbed) —
    NOT ``first_seen_at``, which is only the missing-grace anchor stamped when a
    torrent vanishes from the client and is usually unset for a healthy, present
    one. A row with no ``added_at`` (a pre-migration hole) is skipped rather than
    guessed at.

    Only rows whose PERSISTED ``status`` is exactly ``downloading`` or
    ``metadata_fetching`` (:data:`_STALL_ELIGIBLE_STATUS_VALUES`) are considered —
    NOT every status ``list_active()`` may return (which also includes
    ``failed_pending``, ``import_pending``, ``client_missing``, ...). This is
    checked in addition to, and independently of, the live raw-state check below:
    a ``failed_pending`` residual (an operator's or a stranded prior
    ``mark_failed``'s non-terminal rest stop) must never be reinterpreted as a
    stall just because its torrent looks stale in the live snapshot.

    * **Metadata stall**: the live snapshot still reports a metadata-fetching raw
      state (``metaDL``/``forcedMetaDL``) ``metadata_stall`` after the grab.
    * **Stalled progress**: the row has existed at least ``stalled_progress``,
      its raw state is one of ``_STALLED_PROGRESS_RAW_STATES`` (qBittorrent's
      own zero-peer ``stalledDL`` signal, or the actively-``downloading``/
      ``forcedDL`` catch-all covering a frozen mid-download stall), AND its
      ``last_activity_unix`` is older than ``stalled_progress`` (skipped when
      the client hasn't reported one, i.e. ``<= 0`` — never treated as "epoch,
      therefore ancient"). Requiring the same staleness gate for ``stalledDL``
      as for the catch-all means a single transient tick of ``stalledDL`` on an
      otherwise-healthy torrent (last activity seconds ago) is never enough to
      flag it — only genuine, sustained inactivity is. The raw-state set is
      deliberately narrower than "maps to ``DownloadState.Downloading``": it
      excludes every ``ImportPending`` raw state
      (``uploading``/``stalledUP``/``pausedUP``/``stoppedUP``/
      ``queuedUP``/``checkingUP``/``forcedUP``) because those are completed
      torrents seeding with no leechers, so a stale ``last_activity`` there is
      normal, not a stall; and it also excludes the non-failure Downloading-side
      states (``pausedDL``/``stoppedDL`` — paused, not failed; ``queuedDL`` —
      waiting its turn by design; ``checkingDL``/``checkingResumeData`` —
      verifying pieces, routinely triggered by a restart that also freezes
      ``last_activity``; ``moving`` — actively relocating settled bytes) and any
      unmapped future state. Self-healing one of those would remove and
      blocklist a healthy torrent (north star: correction, never destruction).
    * **Never-had-activity catch-all**: a zero-seed magnet (or an unmapped raw
      state falling back to ``DownloadState.Downloading`` via
      ``_UNKNOWN_FALLBACK``) can report ``last_activity_unix == 0`` forever — it
      never had ANY activity to report, so the stalled-progress branch above
      (which requires ``last_activity_unix > 0``) can never trip for it, and the
      row sits ``downloading`` indefinitely. Any raw state NOT in
      :data:`_DELIBERATELY_IDLE_RAW_STATES` (the metadata-fetching raw states are
      already consumed by the branch above) with ``last_activity_unix <= 0`` AND
      ``progress <= 0.0`` for at least ``metadata_stall`` is healed on the SAME
      ``metadata_stall`` path — it never made it past fetching metadata in any
      way that produced observable activity. The ``progress <= 0.0`` guard keeps
      a partially-downloaded torrent whose activity was merely reset by a
      restart (real bytes on disk) untouched — only the genuine zero-progress
      dead case is healed.

    Deliberately keyed off the LIVE raw state (not the row's persisted
    ``status``) so a row this SAME cycle's :func:`reconcile` is already moving
    out of metadata-fetching/downloading is naturally excluded — no race with
    the reconciler's own transitions.
    """
    snapshot: dict[str, DownloadStatus] = {status.info_hash.lower(): status for status in client}
    detections: list[StallDetection] = []
    for row in rows:
        if row.status not in _STALL_ELIGIBLE_STATUS_VALUES:
            continue
        if row.added_at is None:
            continue
        status = snapshot.get(row.torrent_hash.lower())
        if status is None:
            continue
        elapsed = now - row.added_at
        if status.raw_state in ("metaDL", "forcedMetaDL"):
            if elapsed >= metadata_stall:
                detections.append(StallDetection(row.id, row.torrent_hash, "metadata_stall"))
            continue
        if (
            status.raw_state not in _DELIBERATELY_IDLE_RAW_STATES
            and status.last_activity_unix <= 0
            and status.progress <= 0.0
            and elapsed >= metadata_stall
        ):
            detections.append(StallDetection(row.id, row.torrent_hash, "metadata_stall"))
            continue
        if elapsed < stalled_progress:
            continue
        if status.raw_state in _STALLED_PROGRESS_RAW_STATES and status.last_activity_unix > 0:
            last_activity = datetime.fromtimestamp(status.last_activity_unix, tz=UTC)
            if now - last_activity > stalled_progress:
                detections.append(StallDetection(row.id, row.torrent_hash, "stalled_progress"))
    return detections


def download_deadline(status: DownloadStatus, added_at: datetime) -> datetime | None:
    """The honest stall deadline for a live download row, or ``None`` when the
    state has no meaningful download deadline (completed/seeding/failed).

    ``metaDL``/``forcedMetaDL`` -> ``added_at + METADATA_STALL_WINDOW`` (still
    fetching metadata); a never-had-activity row (``status.raw_state`` not in
    :data:`_DELIBERATELY_IDLE_RAW_STATES`, ``last_activity_unix <= 0``, and
    ``progress <= 0.0``) -> ALSO ``added_at + METADATA_STALL_WINDOW`` — this
    MUST mirror :func:`detect_stalls`'s own never-had-activity predicate
    exactly (Codex P2: the two used to disagree, so a zero-seed magnet's
    ``timeout_at`` showed the 3h ``STALLED_PROGRESS_WINDOW`` deadline while the
    self-heal actually fired 2h15m earlier, at the 45m metadata window); any
    OTHER raw_state mapping to ``DownloadState.Downloading`` (including the
    unknown-state fallback) -> ``added_at + STALLED_PROGRESS_WINDOW``; an
    ``ImportPending``/``FailedPending`` state -> ``None`` (no download in
    flight).

    Observability only — :func:`detect_stalls` remains anchored on ``added_at``;
    this column is never read for control.
    """
    if status.raw_state in ("metaDL", "forcedMetaDL"):
        return added_at + METADATA_STALL_WINDOW
    if _RAW_STATE_MAP.get(status.raw_state, _UNKNOWN_FALLBACK) is not DownloadState.Downloading:
        # ImportPending (uploading/stalledUP/...) or FailedPending (error/
        # missingFiles): no download-phase deadline. Checked BEFORE the
        # never-had-activity predicate below so an ImportPending raw_state can
        # never be misread as "never had activity" just because ``progress``/
        # ``last_activity_unix`` happen to be unset on the snapshot.
        return None
    if (
        status.raw_state not in _DELIBERATELY_IDLE_RAW_STATES
        and status.last_activity_unix <= 0
        and status.progress <= 0.0
    ):
        return added_at + METADATA_STALL_WINDOW
    return added_at + STALLED_PROGRESS_WINDOW
