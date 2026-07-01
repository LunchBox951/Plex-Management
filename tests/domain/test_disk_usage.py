"""Tests for the pure disk-usage percentage math."""

from __future__ import annotations

from plex_manager.domain.disk_usage import DiskUsage, used_percent


def _usage(*, total: int, available: int, root: str = "/media") -> DiskUsage:
    return DiskUsage(root=root, total_bytes=total, available_bytes=available)


def test_half_full() -> None:
    assert used_percent(_usage(total=1000, available=500)) == 50.0


def test_fully_used() -> None:
    assert used_percent(_usage(total=1000, available=0)) == 100.0


def test_empty_used() -> None:
    assert used_percent(_usage(total=1000, available=1000)) == 0.0


def test_ninety_percent() -> None:
    assert used_percent(_usage(total=200, available=20)) == 90.0


def test_zero_total_is_zero_not_a_crash() -> None:
    assert used_percent(_usage(total=0, available=0)) == 0.0


def test_negative_total_is_zero_not_a_crash() -> None:
    # Defensive: a caller should never construct this, but the math must never
    # divide by zero or a negative number.
    assert used_percent(_usage(total=-1, available=0)) == 0.0


def test_available_exceeding_total_reports_honest_negative_used() -> None:
    # Some quota/overlay filesystems can report more "free" than "total". Rather
    # than silently clamp (and hide that oddity), the arithmetic is left honest.
    assert used_percent(_usage(total=1000, available=1200)) == -20.0
