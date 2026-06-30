"""ParserPort — the release-name parser interface (ADR-0008).

The implementation (a guessit adapter) confines the third-party parser; the
domain depends only on this Protocol and the ``ParsedRelease`` DTO. Parsing is
synchronous (guessit is CPU-bound, no I/O).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from plex_manager.domain.release import ParsedRelease

__all__ = ["ParserPort"]


@runtime_checkable
class ParserPort(Protocol):
    """Parses a raw release name into a structured :class:`ParsedRelease`."""

    def parse(self, release_name: str) -> ParsedRelease:
        """Parse ``release_name`` into a :class:`ParsedRelease` (never raises)."""
        raise NotImplementedError
