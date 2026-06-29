"""Release DTOs — the frozen data shapes that cross the domain boundary.

``ParsedRelease`` is the parser's output (ADR-0008); ``CandidateRelease`` and
``IndexerSearchRequest`` are the indexer contract; ``ScoredRelease`` is the
decision engine's ranked output. All are frozen pydantic v2 models so they are
safe to pass around and cache.

Pure domain: pydantic + the local quality model only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from plex_manager.domain.quality import Modifier, Quality, QualitySource, Resolution

__all__ = [
    "CandidateRelease",
    "IndexerSearchRequest",
    "ParsedRelease",
    "Revision",
    "ScoredRelease",
]

Protocol = Literal["torrent", "usenet"]
MediaType = Literal["movie", "tv", "search"]


class Revision(BaseModel):
    """Proper / repack / real markers (Radarr's ``Revision``).

    Higher ``version`` and ``real`` indicate a re-release that supersedes an
    earlier grab of the same quality.
    """

    model_config = ConfigDict(frozen=True)

    version: int = 1
    is_repack: bool = False
    real: int = 0


class ParsedRelease(BaseModel):
    """Structured parse of a raw release name (output of ``ParserPort``).

    ``source`` is the safety-critical field: an unknown source maps to
    ``QualitySource.UNKNOWN`` and is rejected by the default profile.
    """

    model_config = ConfigDict(frozen=True)

    raw_title: str
    clean_title: str
    year: int | None = None
    source: QualitySource = QualitySource.UNKNOWN
    resolution: Resolution = Resolution.UNKNOWN
    modifier: Modifier = Modifier.NONE
    revision: Revision = Field(default_factory=Revision)
    release_group: str | None = None
    languages: list[str] = Field(default_factory=list[str])
    edition: str | None = None
    hardcoded_subs: str | None = None

    @property
    def is_cam_or_prerelease(self) -> bool:
        """True for CAM/TELESYNC/TELECINE/WORKPRINT — never an acceptable grab."""
        return self.source in {
            QualitySource.CAM,
            QualitySource.TELESYNC,
            QualitySource.TELECINE,
            QualitySource.WORKPRINT,
        }


class CandidateRelease(BaseModel):
    """A normalized indexer result (Prowlarr ``ReleaseResource`` + ``TorrentInfo``).

    ``leechers`` is peers minus seeders. ``indexer_priority`` lower = preferred
    (used to de-duplicate the same release across indexers).
    """

    model_config = ConfigDict(frozen=True)

    guid: str
    title: str
    size_bytes: int
    download_url: str | None = None
    magnet_url: str | None = None
    info_hash: str | None = None
    seeders: int | None = None
    leechers: int | None = None
    indexer_id: int
    indexer_name: str
    indexer_priority: int = 25
    publish_date: datetime
    imdb_id: int = 0
    tmdb_id: int = 0
    categories: list[int] = Field(default_factory=list[int])
    protocol: Protocol = "torrent"


class IndexerSearchRequest(BaseModel):
    """A search to run against the indexer port.

    For id-based search, set ``tmdb_id`` / ``imdb_id`` / ``tvdb_id``; for text
    search set ``query``. ``categories`` is omitted from the wire call when empty.
    """

    model_config = ConfigDict(frozen=True)

    media_type: MediaType = "search"
    query: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    year: int | None = None
    season: int | None = None
    episode: str | None = None
    categories: list[int] = Field(default_factory=list[int])
    indexer_ids: list[int] = Field(default_factory=list[int])


class ScoredRelease(BaseModel):
    """A candidate that passed the gates, with its resolved quality and score.

    ``profile_index`` is the candidate's position in the quality profile (the
    comparison key); ``score`` ranks among same-or-grouped qualities.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    candidate: CandidateRelease
    parsed: ParsedRelease
    quality: Quality
    profile_index: int
    score: float
