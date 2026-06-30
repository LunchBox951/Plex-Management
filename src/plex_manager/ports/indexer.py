"""IndexerPort — the release-search interface (Prowlarr in the alpha).

``search`` returns normalized candidates; grabbing is a *separate* concern on the
download client (the two are deliberately not conflated). Implementations
de-duplicate by guid, picking the lowest indexer priority.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest

__all__ = ["IndexerPort"]


@runtime_checkable
class IndexerPort(Protocol):
    """Searches configured indexers and returns normalized candidates."""

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        """Run ``request`` and return de-duplicated candidate releases.

        Implementations surface rate-limit / transport failures as raised
        errors (never an empty list masquerading as "no results").
        """
        raise NotImplementedError
