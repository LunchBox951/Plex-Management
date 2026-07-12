"""Tests for the pure download reconciler.

Inputs are built directly (no DB, no adapter). ``now`` is injected and fixed so
the grace-window arithmetic is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from plex_manager.domain.reconciler import (
    StallDetection,
    StateTransition,
    detect_stalls,
    download_deadline,
    failed_download_events,
    reconcile,
)
from plex_manager.domain.state_machine import DownloadState
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.repositories import DownloadRecord

_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
_HASH = "abc123def456abc123def456abc123def456abcd"

# The full raw-state set the qBittorrent adapter emits verbatim, with the domain
# state each must map to. Drives the mapping coverage test.
_RAW_STATE_EXPECTATIONS: list[tuple[str, DownloadState]] = [
    ("downloading", DownloadState.Downloading),
    ("forcedDL", DownloadState.Downloading),
    ("stalledDL", DownloadState.Downloading),
    ("queuedDL", DownloadState.Downloading),
    ("checkingDL", DownloadState.Downloading),
    ("checkingResumeData", DownloadState.Downloading),
    ("pausedDL", DownloadState.Downloading),
    ("stoppedDL", DownloadState.Downloading),
    ("metaDL", DownloadState.MetadataFetching),
    ("forcedMetaDL", DownloadState.MetadataFetching),
    ("uploading", DownloadState.ImportPending),
    ("stalledUP", DownloadState.ImportPending),
    ("pausedUP", DownloadState.ImportPending),
    ("stoppedUP", DownloadState.ImportPending),
    ("queuedUP", DownloadState.ImportPending),
    ("checkingUP", DownloadState.ImportPending),
    ("forcedUP", DownloadState.ImportPending),
    # 'moving' is qBittorrent still relocating files (not settled) -> stays active,
    # NOT import-eligible, so the importer never reads a half-moved file.
    ("moving", DownloadState.Downloading),
    ("error", DownloadState.FailedPending),
    ("missingFiles", DownloadState.FailedPending),
]


def _row(
    *,
    status: str = DownloadState.Downloading.value,
    torrent_hash: str = _HASH,
    download_id: int = 1,
    first_seen_at: datetime | None = None,
    tmdb_id: int | None = None,
    added_at: datetime | None = None,
) -> DownloadRecord:
    return DownloadRecord(
        id=download_id,
        torrent_hash=torrent_hash,
        status=status,
        first_seen_at=first_seen_at,
        tmdb_id=tmdb_id,
        added_at=added_at,
    )


def _status(
    *,
    raw_state: str,
    info_hash: str = _HASH,
    last_activity_unix: int = 0,
    progress: float = 0.0,
) -> DownloadStatus:
    return DownloadStatus(
        info_hash=info_hash,
        name="Some.Release",
        raw_state=raw_state,
        last_activity_unix=last_activity_unix,
        progress=progress,
    )


# --------------------------------------------------------------------------- #
# Raw-state mapping coverage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("raw_state", "expected"), _RAW_STATE_EXPECTATIONS)
def test_every_adapter_raw_state_maps(raw_state: str, expected: DownloadState) -> None:
    # Start from a state that differs from the expected target so a transition is
    # emitted (MetadataFetching as a neutral non-target for most cases).
    start = (
        DownloadState.ImportPending.value
        if expected is not DownloadState.ImportPending
        else DownloadState.Downloading.value
    )
    rows = [_row(status=start)]
    client = [_status(raw_state=raw_state)]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is expected


# --------------------------------------------------------------------------- #
# Core transition cases
# --------------------------------------------------------------------------- #
def test_downloading_to_complete_emits_import_pending() -> None:
    rows = [_row(status=DownloadState.Downloading.value)]
    client = [_status(raw_state="uploading")]

    transitions = reconcile(rows, client, now=_NOW)

    assert transitions == [
        StateTransition(
            download_id=1,
            torrent_hash=_HASH,
            from_state=DownloadState.Downloading.value,
            to_state=DownloadState.ImportPending,
            reason="client reports 'uploading'",
        )
    ]


def test_error_state_emits_failed_pending() -> None:
    rows = [_row(status=DownloadState.Downloading.value)]
    client = [_status(raw_state="error")]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.FailedPending


def test_no_transition_when_state_unchanged() -> None:
    rows = [_row(status=DownloadState.Downloading.value)]
    client = [_status(raw_state="stalledDL")]  # still maps to Downloading

    assert reconcile(rows, client, now=_NOW) == []


def test_unknown_raw_state_is_surfaced_not_dropped() -> None:
    rows = [_row(status=DownloadState.ImportPending.value)]
    client = [_status(raw_state="weirdNewState")]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.Downloading
    assert transitions[0].reason is not None
    assert "weirdNewState" in transitions[0].reason


# --------------------------------------------------------------------------- #
# Missing-hash grace window
# --------------------------------------------------------------------------- #
def test_missing_within_grace_emits_client_missing_not_failed() -> None:
    rows = [
        _row(
            status=DownloadState.Downloading.value,
            first_seen_at=_NOW - timedelta(minutes=2),
        )
    ]

    transitions = reconcile(rows, [], now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.ClientMissing


def test_missing_past_grace_emits_failed_pending() -> None:
    rows = [
        _row(
            status=DownloadState.Downloading.value,
            first_seen_at=_NOW - timedelta(minutes=20),
        )
    ]

    transitions = reconcile(rows, [], now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.FailedPending


def test_first_absence_with_no_anchor_surfaces_missing_and_stamps_not_failed() -> None:
    # A row first seen absent has no first_seen_at anchor yet. It must NOT fail —
    # it surfaces ClientMissing and asks the caller to stamp the grace anchor.
    rows = [_row(status=DownloadState.Downloading.value, first_seen_at=None)]

    transitions = reconcile(rows, [], now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.ClientMissing
    assert transitions[0].set_first_seen_at is True


def test_client_missing_with_no_anchor_rearms_grace_does_not_fail() -> None:
    # Already ClientMissing but the caller never persisted the anchor: re-arm the
    # grace (stamp again) rather than collapsing it to an immediate failure.
    rows = [_row(status=DownloadState.ClientMissing.value, first_seen_at=None)]

    transitions = reconcile(rows, [], now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.ClientMissing
    assert transitions[0].set_first_seen_at is True


def test_grace_holds_for_full_window_across_polls_when_anchor_stamped() -> None:
    # End-to-end grace proof: an absent torrent must survive >= missing_grace of
    # absence before failing, with the anchor stamped only via the transition the
    # reconciler emits (mirroring the repository update_status(first_seen_at=now)
    # path). A regression here is the prototype's fail-on-first-miss drift bug.
    grace = timedelta(minutes=10)
    poll_interval = timedelta(minutes=1)

    # Poll 1: missing, no anchor -> ClientMissing + stamp request.
    row = _row(status=DownloadState.Downloading.value, first_seen_at=None)
    first = reconcile([row], [], now=_NOW, missing_grace=grace)
    assert len(first) == 1
    assert first[0].to_state is DownloadState.ClientMissing
    assert first[0].set_first_seen_at is True

    # Caller persists: status=client_missing, first_seen_at stamped to poll-1 now.
    anchor = _NOW
    row = _row(status=DownloadState.ClientMissing.value, first_seen_at=anchor)

    # Polls 2..10: still missing, still within grace -> no-op (idempotent).
    t = _NOW
    while (t - anchor) < grace:
        t += poll_interval
        if (t - anchor) >= grace:
            break
        assert reconcile([row], [], now=t, missing_grace=grace) == []

    # Once the full grace has elapsed, escalate to FailedPending.
    after_grace = anchor + grace
    final = reconcile([row], [], now=after_grace, missing_grace=grace)
    assert len(final) == 1
    assert final[0].to_state is DownloadState.FailedPending


def test_client_missing_within_grace_is_noop() -> None:
    rows = [
        _row(
            status=DownloadState.ClientMissing.value,
            first_seen_at=_NOW - timedelta(minutes=1),
        )
    ]

    assert reconcile(rows, [], now=_NOW) == []


def test_reappeared_hash_transitions_client_missing_to_downloading() -> None:
    rows = [_row(status=DownloadState.ClientMissing.value)]
    client = [_status(raw_state="downloading")]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].from_state == DownloadState.ClientMissing.value
    assert transitions[0].to_state is DownloadState.Downloading


def test_recovery_from_client_missing_clears_grace_anchor() -> None:
    # A ClientMissing torrent that reappears must clear its stale grace anchor so a
    # later disappearance starts a fresh full window (not measured from the old one).
    rows = [
        _row(
            status=DownloadState.ClientMissing.value,
            first_seen_at=_NOW - timedelta(minutes=5),
        )
    ]
    client = [_status(raw_state="downloading")]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.Downloading
    assert transitions[0].clear_first_seen_at is True
    assert transitions[0].set_first_seen_at is False


def test_recovery_then_redisappearance_gets_fresh_full_grace_window() -> None:
    # End-to-end: a missing torrent recovers (anchor cleared), then disappears
    # again far later. The fresh disappearance must surface ClientMissing and
    # re-stamp the anchor — NOT fail fast against the long-stale prior anchor.
    grace = timedelta(minutes=10)

    # Poll 1: absent, anchor stamped at _NOW.
    anchor = _NOW
    missing = _row(status=DownloadState.ClientMissing.value, first_seen_at=anchor)

    # Poll 2: reappears 3 min later -> recovery clears the anchor.
    reappear_at = _NOW + timedelta(minutes=3)
    recovery = reconcile([missing], [_status(raw_state="downloading")], now=reappear_at)
    assert len(recovery) == 1
    assert recovery[0].clear_first_seen_at is True

    # Caller persists: status=downloading, first_seen_at cleared to NULL.
    recovered = _row(status=DownloadState.Downloading.value, first_seen_at=None)

    # Poll 3: disappears again an hour later — far beyond the OLD anchor+grace. With
    # the anchor cleared this is a FIRST absence: surface ClientMissing + re-stamp,
    # never fail. (A stale anchor would have failed it immediately.)
    redisappear_at = _NOW + timedelta(minutes=60)
    fresh = reconcile([recovered], [], now=redisappear_at, missing_grace=grace)
    assert len(fresh) == 1
    assert fresh[0].to_state is DownloadState.ClientMissing
    assert fresh[0].set_first_seen_at is True


# --------------------------------------------------------------------------- #
# Gating + idempotency
# --------------------------------------------------------------------------- #
def test_terminal_and_non_active_rows_are_left_alone() -> None:
    rows = [
        _row(status=DownloadState.Imported.value, download_id=1),
        _row(status=DownloadState.Failed.value, download_id=2),
        _row(status=DownloadState.Searching.value, download_id=3),
        _row(status=DownloadState.Importing.value, download_id=4),
    ]
    # Even if the client reports a different state, non-active rows don't move.
    client = [
        _status(raw_state="downloading", info_hash=_HASH),
    ]

    assert reconcile(rows, client, now=_NOW) == []


def test_reconcile_is_idempotent_on_second_run() -> None:
    rows = [_row(status=DownloadState.Downloading.value)]
    client = [_status(raw_state="uploading")]

    first = reconcile(rows, client, now=_NOW)
    assert len(first) == 1

    # Apply the transition (caller would persist it) and re-run.
    applied = [_row(status=first[0].to_state.value)]
    assert reconcile(applied, client, now=_NOW) == []


def test_info_hash_matching_is_case_insensitive() -> None:
    rows = [_row(status=DownloadState.Downloading.value, torrent_hash=_HASH.upper())]
    client = [_status(raw_state="uploading", info_hash=_HASH.lower())]

    transitions = reconcile(rows, client, now=_NOW)

    assert len(transitions) == 1
    assert transitions[0].to_state is DownloadState.ImportPending


# --------------------------------------------------------------------------- #
# Failed-event helper
# --------------------------------------------------------------------------- #
def test_failed_download_events_built_from_failed_pending_only() -> None:
    transitions = [
        StateTransition(1, _HASH, "downloading", DownloadState.FailedPending, "boom"),
        StateTransition(2, "otherhash", "downloading", DownloadState.ImportPending, "done"),
    ]
    records = [_row(download_id=1, tmdb_id=550)]

    events = failed_download_events(transitions, records, occurred_at=_NOW)

    assert len(events) == 1
    assert events[0].torrent_hash == _HASH
    assert events[0].reason == "boom"
    assert events[0].tmdb_id == 550
    assert events[0].occurred_at == _NOW


def test_failed_download_events_empty_when_no_failures() -> None:
    transitions = [
        StateTransition(1, _HASH, "downloading", DownloadState.ImportPending, "done"),
    ]

    assert failed_download_events(transitions) == []


# --------------------------------------------------------------------------- #
# Stalled-download self-heal detection (issue #165)
# --------------------------------------------------------------------------- #
def test_metadata_stall_past_threshold_is_detected() -> None:
    rows = [
        _row(status=DownloadState.MetadataFetching.value, added_at=_NOW - timedelta(minutes=46))
    ]
    client = [_status(raw_state="metaDL")]

    detections = detect_stalls(rows, client, now=_NOW)

    assert len(detections) == 1
    assert detections[0].download_id == 1
    assert detections[0].torrent_hash == _HASH
    assert detections[0].shape == "metadata_stall"


def test_metadata_stall_under_threshold_is_not_detected() -> None:
    rows = [
        _row(status=DownloadState.MetadataFetching.value, added_at=_NOW - timedelta(minutes=10))
    ]
    client = [_status(raw_state="forcedMetaDL")]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_stalled_progress_via_stalled_dl_raw_state_and_stale_activity_is_detected() -> None:
    # The genuine case: qBittorrent's own zero-peer ``stalledDL`` signal AND
    # the client hasn't seen activity in over the stall window.
    stale_activity = int((_NOW - timedelta(hours=5)).timestamp())
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="stalledDL", last_activity_unix=stale_activity)]

    detections = detect_stalls(rows, client, now=_NOW)

    assert len(detections) == 1
    assert detections[0].shape == "stalled_progress"


def test_stalled_dl_row_with_recent_activity_is_not_detected() -> None:
    # Regression for the flaky-but-alive-seeder bug: a single transient tick of
    # ``stalledDL`` (a momentary zero-peer blip) on a torrent whose client
    # activity is recent must NOT self-heal a healthy download.
    recent_activity = int((_NOW - timedelta(seconds=30)).timestamp())
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="stalledDL", last_activity_unix=recent_activity)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_stalled_dl_row_with_no_last_activity_unix_but_partial_progress_is_not_detected() -> None:
    # ``last_activity_unix`` defaults to 0 (the client never reported one, e.g.
    # a restart reset it) -- but PARTIAL progress means real bytes are on disk,
    # so the never-had-activity catch-all must NOT heal this (that guard is
    # ``progress <= 0.0`` only).
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="stalledDL", last_activity_unix=0, progress=0.5)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_downloading_row_under_stall_window_is_not_detected() -> None:
    # Under BOTH the metadata window (45min) and the stalled-progress window
    # (3h) -- neither the pre-existing stalled_progress branch nor the new
    # never-had-activity catch-all may trip yet.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(minutes=30))]
    client = [_status(raw_state="stalledDL")]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_stalled_progress_via_stale_last_activity_is_detected() -> None:
    # ``downloading`` (not stalledDL) but the client hasn't seen activity in
    # over the stall window -- the state-agnostic frozen-mid-download catch-all.
    stale_activity = int((_NOW - timedelta(hours=5)).timestamp())
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="downloading", last_activity_unix=stale_activity)]

    detections = detect_stalls(rows, client, now=_NOW)

    assert len(detections) == 1
    assert detections[0].shape == "stalled_progress"


def test_downloading_row_with_recent_activity_is_not_detected() -> None:
    recent_activity = int((_NOW - timedelta(minutes=5)).timestamp())
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="downloading", last_activity_unix=recent_activity)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_downloading_row_with_no_last_activity_unix_but_partial_progress_is_not_detected() -> None:
    # ``last_activity_unix`` defaults to 0 (the client never reported one) but
    # PARTIAL progress means real bytes are on disk (e.g. a restart reset
    # activity mid-download) -- the never-had-activity catch-all's
    # ``progress <= 0.0`` guard must keep this untouched.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="downloading", last_activity_unix=0, progress=0.5)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_never_activity_zero_progress_downloading_heals_as_metadata_stall() -> None:
    # The gap this fix closes: a zero-seed magnet stuck in ``downloading`` that
    # never produced ANY activity (last_activity_unix stays 0 forever) could
    # previously never stall out, because the pre-existing stalled_progress
    # branch requires ``last_activity_unix > 0``. Healed on the SAME
    # metadata_stall path once past the (shorter) metadata window.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(minutes=46))]
    client = [_status(raw_state="downloading", last_activity_unix=0, progress=0.0)]

    detections = detect_stalls(rows, client, now=_NOW)

    assert len(detections) == 1
    assert detections[0].shape == "metadata_stall"


def test_never_activity_unknown_raw_state_heals_as_metadata_stall() -> None:
    # Exercises the ``_UNKNOWN_FALLBACK`` path the spec calls out: an unmapped
    # future raw state falls back to ``DownloadState.Downloading`` and, with no
    # activity ever reported and zero progress, must heal exactly like the
    # known ``downloading`` case above.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(minutes=46))]
    client = [_status(raw_state="someUnknownState", last_activity_unix=0, progress=0.0)]

    detections = detect_stalls(rows, client, now=_NOW)

    assert len(detections) == 1
    assert detections[0].shape == "metadata_stall"


def test_never_activity_under_metadata_window_is_not_detected() -> None:
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(minutes=30))]
    client = [_status(raw_state="downloading", last_activity_unix=0, progress=0.0)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_paused_zero_progress_is_not_healed() -> None:
    # Deliberately-idle denylist, north-star safety: an operator paused this --
    # the never-had-activity catch-all must never remove/blocklist it.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="pausedDL", last_activity_unix=0, progress=0.0)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_queued_zero_progress_is_not_healed() -> None:
    # Deliberately-idle denylist: waiting its turn behind other torrents by
    # design, not a failure.
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state="queuedDL", last_activity_unix=0, progress=0.0)]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_row_with_no_added_at_is_skipped() -> None:
    rows = [_row(status=DownloadState.MetadataFetching.value, added_at=None)]
    client = [_status(raw_state="metaDL")]

    assert detect_stalls(rows, client, now=_NOW) == []


def test_row_absent_from_client_snapshot_is_skipped() -> None:
    rows = [_row(status=DownloadState.MetadataFetching.value, added_at=_NOW - timedelta(hours=1))]

    assert detect_stalls(rows, [], now=_NOW) == []


@pytest.mark.parametrize(
    "raw_state",
    ["uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "checkingUP", "forcedUP"],
)
def test_import_pending_row_is_never_flagged(raw_state: str) -> None:
    # A settled-on-disk row that happens to be old is not a stall shape this
    # detector covers -- import stalls are a different concern. Uses a REALISTIC
    # stale last_activity (a completed torrent seeding with no leechers goes
    # stale within hours) so this doesn't just trivially pass on the
    # last_activity_unix=0 default: self-healing one of these would delete a
    # finished torrent and its files.
    stale_activity = int((_NOW - timedelta(hours=4)).timestamp())
    rows = [_row(status=DownloadState.ImportPending.value, added_at=_NOW - timedelta(hours=10))]
    client = [_status(raw_state=raw_state, last_activity_unix=stale_activity)]

    assert detect_stalls(rows, client, now=_NOW) == []


@pytest.mark.parametrize(
    "status",
    [
        DownloadState.FailedPending.value,
        DownloadState.ImportPending.value,
        DownloadState.ClientMissing.value,
        "searching",
        "importing",
    ],
)
def test_non_downloading_metadata_persisted_status_is_never_flagged(status: str) -> None:
    # Regression (issue #165 hardening finding): ``list_active()`` returns every
    # non-terminal row, INCLUDING ``failed_pending`` -- mark_failed's own Phase-A
    # rest stop, which an operator may have deliberately left with
    # remove_torrent=False/blocklist=False to keep the torrent. Keying purely off
    # the LIVE raw state (metaDL, stale downloading/stalledDL) must never flag one
    # of these rows just because its underlying torrent looks stale -- the
    # self-heal would silently overturn an earlier explicit choice.
    stale_activity = int((_NOW - timedelta(hours=5)).timestamp())
    rows = [_row(status=status, added_at=_NOW - timedelta(hours=10))]
    metadata_client = [_status(raw_state="metaDL")]
    downloading_client = [_status(raw_state="stalledDL", last_activity_unix=stale_activity)]

    assert detect_stalls(rows, metadata_client, now=_NOW) == []
    assert detect_stalls(rows, downloading_client, now=_NOW) == []


def test_failed_pending_row_with_stale_torrent_is_never_self_healed() -> None:
    # The exact scenario the finding describes: an earlier
    # mark_failed(remove_torrent=False, blocklist=False) left the row at
    # failed_pending, deliberately keeping the torrent. If that torrent then goes
    # stale, detect_stalls must not pick it up even though the live snapshot looks
    # identical to a genuine stalled_progress case.
    stale_activity = int((_NOW - timedelta(hours=5)).timestamp())
    rows = [
        _row(
            status=DownloadState.FailedPending.value,
            added_at=_NOW - timedelta(hours=4),
        )
    ]
    client = [_status(raw_state="downloading", last_activity_unix=stale_activity)]

    assert detect_stalls(rows, client, now=_NOW) == []


@pytest.mark.parametrize(
    "raw_state",
    [
        "pausedDL",
        "stoppedDL",
        "queuedDL",
        "checkingDL",
        "checkingResumeData",
        "moving",
        "someFutureState",
    ],
)
def test_non_failure_downloading_side_row_is_never_flagged_by_last_activity(
    raw_state: str,
) -> None:
    # These all map (or, for an unknown future state, fall back) to
    # DownloadState.Downloading, but none of them is a stall: pausedDL/stoppedDL
    # is an operator/client pause, queuedDL is waiting its turn, checkingDL/
    # checkingResumeData is qBittorrent verifying pieces (routinely triggered by
    # a restart that also freezes last_activity), and moving is actively
    # relocating already-settled bytes. Self-healing any of these would remove
    # and blocklist a healthy torrent.
    stale_activity = int((_NOW - timedelta(hours=5)).timestamp())
    rows = [_row(status=DownloadState.Downloading.value, added_at=_NOW - timedelta(hours=4))]
    client = [_status(raw_state=raw_state, last_activity_unix=stale_activity)]

    assert detect_stalls(rows, client, now=_NOW) == []


# --------------------------------------------------------------------------- #
# download_deadline (concern 3 — honest observability, never read for control)
# --------------------------------------------------------------------------- #
def test_download_deadline_helper() -> None:
    t = _NOW
    assert download_deadline(_status(raw_state="metaDL"), t) == t + timedelta(minutes=45)
    assert download_deadline(_status(raw_state="forcedMetaDL"), t) == t + timedelta(minutes=45)
    # A torrent that HAS reported real activity/progress uses the full download
    # window, whatever its raw_state maps to (Downloading, or an unmapped future
    # state via the fallback).
    assert download_deadline(
        _status(raw_state="downloading", last_activity_unix=1, progress=0.0), t
    ) == t + timedelta(hours=3)
    assert download_deadline(
        _status(raw_state="downloading", last_activity_unix=0, progress=0.5), t
    ) == t + timedelta(hours=3)
    assert download_deadline(
        _status(raw_state="someUnknownState", last_activity_unix=1, progress=0.0), t
    ) == t + timedelta(hours=3)
    assert download_deadline(_status(raw_state="uploading"), t) is None
    assert download_deadline(_status(raw_state="error"), t) is None


def test_download_deadline_zero_activity_mirrors_the_metadata_stall_window() -> None:
    # Codex P2: a zero-seed magnet (or an unmapped raw state) that has NEVER
    # reported activity or progress self-heals on the SAME 45-minute
    # metadata_stall window as a genuine metadata stall (detect_stalls's
    # never-had-activity catch-all) — the deadline column must say so too,
    # not the 3-hour stalled_progress window a downloading-mapped raw_state
    # would otherwise imply.
    t = _NOW
    for raw_state in ("downloading", "forcedDL", "stalledDL", "someUnknownFutureState"):
        assert download_deadline(
            _status(raw_state=raw_state, last_activity_unix=0, progress=0.0), t
        ) == t + timedelta(minutes=45), raw_state


@pytest.mark.parametrize(
    "raw_state",
    ["pausedDL", "stoppedDL", "queuedDL", "checkingDL", "checkingResumeData", "moving"],
)
def test_download_deadline_deliberately_idle_states_keep_the_download_window(
    raw_state: str,
) -> None:
    # These raw states are excluded from the never-had-activity catch-all in
    # BOTH detect_stalls and download_deadline (an operator pause / queue
    # position / verification pass / relocation is never self-healed just
    # because it never had activity), so their deadline stays the normal
    # 3-hour download window even at zero progress/activity — mirroring
    # detect_stalls exactly rather than claiming a 45-minute heal that will
    # never fire.
    t = _NOW
    assert download_deadline(
        _status(raw_state=raw_state, last_activity_unix=0, progress=0.0), t
    ) == t + timedelta(hours=3)


def test_download_deadline_matches_detect_stalls_self_heal_timing() -> None:
    # The exact regression Codex flagged: for a zero-seed magnet, the deadline
    # download_deadline() reports must equal the moment detect_stalls() ACTUALLY
    # fires — not 2h15m later.
    added_at = _NOW - timedelta(minutes=44)
    rows = [_row(status=DownloadState.Downloading.value, added_at=added_at)]
    status = _status(raw_state="downloading", last_activity_unix=0, progress=0.0)

    deadline = download_deadline(status, added_at)
    assert deadline is not None
    assert deadline == added_at + timedelta(minutes=45)

    # One minute before the reported deadline: not yet healed.
    assert detect_stalls(rows, [status], now=deadline - timedelta(minutes=1)) == []
    # At the reported deadline: healed.
    assert detect_stalls(rows, [status], now=deadline) == [
        StallDetection(download_id=1, torrent_hash=_HASH, shape="metadata_stall")
    ]
