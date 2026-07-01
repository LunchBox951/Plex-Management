"""Tests for the pure download reconciler.

Inputs are built directly (no DB, no adapter). ``now`` is injected and fixed so
the grace-window arithmetic is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from plex_manager.domain.reconciler import (
    StateTransition,
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
) -> DownloadRecord:
    return DownloadRecord(
        id=download_id,
        torrent_hash=torrent_hash,
        status=status,
        first_seen_at=first_seen_at,
        tmdb_id=tmdb_id,
    )


def _status(*, raw_state: str, info_hash: str = _HASH) -> DownloadStatus:
    return DownloadStatus(info_hash=info_hash, name="Some.Release", raw_state=raw_state)


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
