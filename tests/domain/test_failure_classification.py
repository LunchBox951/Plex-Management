"""Tests for the pure failure-detail taxonomy (issue #181).

Inputs are plain strings (or ``None``) -- no DB, no adapter, no client.
"""

from __future__ import annotations

import pytest

from plex_manager.domain.failure_classification import (
    FailureClass,
    classify_failure_detail,
)

# Mapping tests: the exact taxonomy the qBittorrent adapter's enriched detail
# strings are expected to carry (POSIX errno-derived libtorrent messages, plus
# qBittorrent's own ``missingFiles`` raw torrent state).
_ENVIRONMENTAL_DETAILS: list[str] = [
    "file_open: /downloads/.plex_manager/S04E01.mkv, error: Permission denied",
    "PERMISSION DENIED",
    "boost::filesystem::create_directory: Permission denied",
    "No space left on device",
    "disk is full: No space left on device",
    "Read-only file system",
    "Disk quota exceeded",
    "No such file or directory",
    "Input/output error",
    "I/O error",
    "client reports 'missingFiles'",
    "client reports 'missingFiles': files are missing",
]

_RELEASE_FAULT_DETAILS: list[str | None] = [
    None,
    "",
    "client reports 'error'",
    "Unregistered torrent",
    "This torrent has been removed from the tracker",
    "This torrent is not authorized for use on this tracker",
    "unknown client state 'somethingNew'; tracking as downloading",
]


@pytest.mark.parametrize("detail", _ENVIRONMENTAL_DETAILS)
def test_environmental_details_classify_as_environmental(detail: str) -> None:
    assert classify_failure_detail(detail) is FailureClass.environmental


@pytest.mark.parametrize("detail", _RELEASE_FAULT_DETAILS)
def test_release_fault_and_unclassifiable_details_default_to_release_fault(
    detail: str | None,
) -> None:
    assert classify_failure_detail(detail) is FailureClass.release_fault


def test_classification_is_case_insensitive() -> None:
    assert classify_failure_detail("PERMISSION DENIED") is FailureClass.environmental
    assert classify_failure_detail("permission denied") is FailureClass.environmental
    assert classify_failure_detail("Permission Denied") is FailureClass.environmental


def test_none_never_raises_and_defaults_release_fault() -> None:
    # An absent detail (no enrichment could be fetched -- e.g. qBittorrent
    # unreachable) must default to the PRE-EXISTING reconcile-default behavior
    # (blocklist), never crash the reconcile cycle.
    assert classify_failure_detail(None) is FailureClass.release_fault
