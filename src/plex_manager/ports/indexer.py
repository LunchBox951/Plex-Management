"""IndexerPort — the release-search interface (Prowlarr in the alpha).

``search`` returns normalized candidates; grabbing is a *separate* concern on the
download client (the two are deliberately not conflated). Implementations
de-duplicate by guid, picking the lowest indexer priority.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest

__all__ = ["IndexerPort", "IndexerSearchResults"]


class IndexerSearchResults(list[CandidateRelease]):
    """Candidates with completeness retained through downstream acceptance checks."""

    def __init__(self, candidates: Iterable[CandidateRelease], *, complete: bool) -> None:
        super().__init__(candidates)
        self.complete = complete


@runtime_checkable
class IndexerPort(Protocol):
    """Searches configured indexers and returns normalized candidates."""

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        """Run ``request`` and return de-duplicated candidate releases.

        Implementations surface rate-limit / transport failures as raised
        errors when no candidates can be retained. Partial candidates carry
        ``IndexerSearchResults.complete=False`` so later filtering or source
        resolution cannot mistake an incomplete search for release exhaustion.
        """
        raise NotImplementedError
