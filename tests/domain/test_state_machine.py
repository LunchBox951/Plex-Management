"""Tests for the download state machine: enum values + legal-transition graph."""

from __future__ import annotations

import pytest

from plex_manager.domain.state_machine import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    DownloadState,
    is_legal_transition,
)

# Representative legal moves drawn straight from the lifecycle design.
_LEGAL: list[tuple[DownloadState, DownloadState]] = [
    (DownloadState.Searching, DownloadState.Downloading),
    (DownloadState.Searching, DownloadState.NoAcceptableRelease),
    (DownloadState.Downloading, DownloadState.MetadataFetching),
    (DownloadState.Downloading, DownloadState.ImportPending),
    (DownloadState.Downloading, DownloadState.FailedPending),
    (DownloadState.Downloading, DownloadState.ClientMissing),
    (DownloadState.Downloading, DownloadState.Downloading),
    (DownloadState.MetadataFetching, DownloadState.Downloading),
    (DownloadState.MetadataFetching, DownloadState.FailedPending),
    (DownloadState.MetadataFetching, DownloadState.ClientMissing),
    (DownloadState.ImportPending, DownloadState.Importing),
    (DownloadState.ImportPending, DownloadState.ImportBlocked),
    (DownloadState.Importing, DownloadState.Imported),
    (DownloadState.Importing, DownloadState.ImportBlocked),
    (DownloadState.ImportBlocked, DownloadState.Importing),
    (DownloadState.FailedPending, DownloadState.Failed),
    (DownloadState.ClientMissing, DownloadState.Downloading),
    (DownloadState.ClientMissing, DownloadState.FailedPending),
]

# Illegal jumps the machine must reject.
_ILLEGAL: list[tuple[DownloadState, DownloadState]] = [
    (DownloadState.Searching, DownloadState.Imported),
    (DownloadState.Searching, DownloadState.Importing),
    (DownloadState.Downloading, DownloadState.Imported),
    (DownloadState.Downloading, DownloadState.Failed),
    (DownloadState.ImportPending, DownloadState.Imported),
    (DownloadState.ImportPending, DownloadState.Downloading),
    (DownloadState.FailedPending, DownloadState.Downloading),
    (DownloadState.ClientMissing, DownloadState.Imported),
    # terminal states have no outgoing edges
    (DownloadState.Failed, DownloadState.Downloading),
    (DownloadState.Failed, DownloadState.Searching),
    (DownloadState.Imported, DownloadState.Downloading),
    (DownloadState.NoAcceptableRelease, DownloadState.Searching),
    (DownloadState.NoAcceptableRelease, DownloadState.Downloading),
]


@pytest.mark.parametrize(("frm", "to"), _LEGAL)
def test_legal_transitions_are_allowed(frm: DownloadState, to: DownloadState) -> None:
    assert is_legal_transition(frm, to) is True


@pytest.mark.parametrize(("frm", "to"), _ILLEGAL)
def test_illegal_transitions_are_rejected(frm: DownloadState, to: DownloadState) -> None:
    assert is_legal_transition(frm, to) is False


def test_download_state_values_are_lowercase_strings() -> None:
    for state in DownloadState:
        assert state.value == state.value.lower()
        assert " " not in state.value


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_terminal_state_values_match_repository_literals() -> None:
    # The P2 SqlDownloadRepository.list_active filters on these exact strings.
    assert {state.value for state in TERMINAL_STATES} == {
        "imported",
        "failed",
        "no_acceptable_release",
    }


def test_active_states_are_the_polled_set() -> None:
    expected = frozenset(
        {
            DownloadState.Downloading,
            DownloadState.MetadataFetching,
            DownloadState.ImportPending,
            DownloadState.ClientMissing,
        }
    )
    assert expected == ACTIVE_STATES


def test_active_and_terminal_states_are_disjoint() -> None:
    assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)


def test_every_transition_target_is_a_known_state() -> None:
    for targets in TRANSITIONS.values():
        for target in targets:
            assert target in DownloadState
