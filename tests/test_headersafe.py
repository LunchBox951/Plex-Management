"""Unit tests for the ``plex_manager.headersafe`` HTTP header-value barrier.

These cover the exact two failure modes GHSA-qv47 fixes: CR/LF/NUL, which httpx/
h11 rejects with a ``LocalProtocolError`` that echoes the RAW value in
``str(exc)`` (a credential leak), and non-ASCII, which httpx cannot encode as a
header and raises an uncaught ``UnicodeEncodeError`` (a 500). Both must be
rejected BEFORE any outbound request ever carries the value.
"""

from __future__ import annotations

import pytest

from plex_manager.headersafe import HEADER_VALUE_MESSAGE, header_value_error, is_header_safe


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc123",
        "A-Za-z0-9_-.~stuff",
        "x" * 512,
        "!" * 4,
        "~",
    ],
)
def test_header_value_error_accepts_plain_ascii_credentials(value: str) -> None:
    assert header_value_error(value) is None
    assert is_header_safe(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "a\rb",
        "a\nb",
        "a\r\nb",
        "a\x00b",
        "\x01",
        "\x1f",
        "\x7f",
        "with space",
        "\x80",
        "\xff",
        "\xf6",
        "Ā",
        "\U0001f4a9",
    ],
)
def test_header_value_error_rejects_unsafe(value: str) -> None:
    assert header_value_error(value) == HEADER_VALUE_MESSAGE
    assert is_header_safe(value) is False


def test_header_value_error_never_echoes_the_value() -> None:
    """The rejection message is a static fragment -- it must never contain the
    secret substring that made the value unsafe (north star #3)."""
    message = header_value_error("SECRET\r\nx")
    assert message is not None
    assert "SECRET" not in message
