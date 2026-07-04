"""Unit tests for the ``plex_manager.logsafe`` log-value barriers.

These are the single-purpose barriers every request-derived log site passes its
values through (see CONTRIBUTING.md "Logging request-derived values"): ``safe_int``
re-coerces an id (a no-op for a real int, a taint barrier for CodeQL's
py/log-injection), and ``safe_text`` collapses CR/LF so a request-derived string
cannot forge a second log record.
"""

from __future__ import annotations

import pytest

from plex_manager.logsafe import safe_int, safe_text


@pytest.mark.parametrize("value", [0, 1, 999, -5, 2**63])
def test_safe_int_passes_real_ints_through_unchanged(value: int) -> None:
    result = safe_int(value)
    assert result == value
    assert type(result) is int


def test_safe_text_leaves_clean_text_unchanged() -> None:
    assert safe_text("Arrival (2016)") == "Arrival (2016)"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("line1\nline2", "line1 line2"),
        ("line1\rline2", "line1 line2"),
        ("line1\r\nline2", "line1  line2"),  # both chars collapse, one space each
        ("\n\rboundary", "  boundary"),
    ],
)
def test_safe_text_collapses_crlf_to_spaces(raw: str, expected: str) -> None:
    assert safe_text(raw) == expected


def test_safe_text_neutralizes_a_forged_log_record() -> None:
    """A CRLF payload aiming to inject a fake ``ERROR root:`` line is defanged:
    no bare newline survives, so the value cannot start a second log record."""
    forged = "42\nERROR:root:you have been hacked"
    cleaned = safe_text(forged)
    assert "\n" not in cleaned
    assert "\r" not in cleaned
