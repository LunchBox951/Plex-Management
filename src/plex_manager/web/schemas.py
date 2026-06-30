"""Pydantic v2 request/response models for the REST API.

These DTOs are the published wire contract (they shape the exported OpenAPI).
Response models NEVER carry secret values: service credentials (Plex token,
Prowlarr / TMDB api keys, qBittorrent password) are represented by a masked
``"***"`` placeholder when configured, never the plaintext.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AcceptedRelease",
    "BlocklistEntry",
    "BlocklistResponse",
    "CreateRequestBody",
    "DiscoverResult",
    "DiscoverSearchResponse",
    "GrabRequest",
    "PlexValidateRequest",
    "ProwlarrValidateRequest",
    "QbittorrentValidateRequest",
    "QualityProfileItemResponse",
    "QualityProfileResponse",
    "QueueItem",
    "QueueResponse",
    "RejectedRelease",
    "RequestListResponse",
    "RequestResponse",
    "SearchPreviewRequest",
    "SearchPreviewResponse",
    "ServiceValidateResponse",
    "SettingsResponse",
    "SettingsUpdate",
    "SetupCompleteRequest",
    "SetupStatusResponse",
    "TmdbValidateRequest",
]

MediaTypeField = Literal["movie", "tv"]


# --------------------------------------------------------------------------- #
# Setup wizard — connection validation (unauthenticated, pre-init)
# --------------------------------------------------------------------------- #
class PlexValidateRequest(BaseModel):
    """Candidate Plex credentials to test (``POST /setup/validate/plex``)."""

    model_config = ConfigDict(frozen=True)

    url: str
    token: str


class ProwlarrValidateRequest(BaseModel):
    """Candidate Prowlarr credentials to test."""

    model_config = ConfigDict(frozen=True)

    url: str
    api_key: str


class QbittorrentValidateRequest(BaseModel):
    """Candidate qBittorrent credentials to test."""

    model_config = ConfigDict(frozen=True)

    url: str
    username: str
    password: str


class TmdbValidateRequest(BaseModel):
    """Candidate TMDB api key to test."""

    model_config = ConfigDict(frozen=True)

    api_key: str


class ServiceValidateResponse(BaseModel):
    """Result of a connection check. ``message`` is operator-facing; ``detail``
    is an optional diagnostic. Neither ever contains a secret value."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    message: str
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Setup completion + status
# --------------------------------------------------------------------------- #
class SetupCompleteRequest(BaseModel):
    """The validated credential set written on ``POST /setup/complete``."""

    model_config = ConfigDict(frozen=True)

    plex_url: str
    plex_token: str
    prowlarr_url: str
    prowlarr_api_key: str
    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str
    tmdb_api_key: str


class SetupStatusResponse(BaseModel):
    """Install state. ``app_api_key`` is populated only once initialized."""

    model_config = ConfigDict(frozen=True)

    initialized: bool
    app_api_key: str | None = None


# --------------------------------------------------------------------------- #
# Settings (authenticated)
# --------------------------------------------------------------------------- #
class SettingsResponse(BaseModel):
    """Redacted view of stored service config.

    Plaintext (non-secret) values are returned as-is; secret values are shown as
    ``"***"`` when configured and ``None`` when unset — the plaintext secret is
    NEVER serialized.
    """

    model_config = ConfigDict(frozen=True)

    plex_url: str | None = None
    plex_token: str | None = None
    prowlarr_url: str | None = None
    prowlarr_api_key: str | None = None
    qbittorrent_url: str | None = None
    qbittorrent_username: str | None = None
    qbittorrent_password: str | None = None
    tmdb_api_key: str | None = None


class SettingsUpdate(BaseModel):
    """Partial upsert of service config (``PUT /settings``).

    Only fields present in the request body are written; secret fields are stored
    encrypted at rest. ``None`` / absent fields are left unchanged.
    """

    model_config = ConfigDict(frozen=True)

    plex_url: str | None = Field(default=None)
    plex_token: str | None = Field(default=None)
    prowlarr_url: str | None = Field(default=None)
    prowlarr_api_key: str | None = Field(default=None)
    qbittorrent_url: str | None = Field(default=None)
    qbittorrent_username: str | None = Field(default=None)
    qbittorrent_password: str | None = Field(default=None)
    tmdb_api_key: str | None = Field(default=None)


# --------------------------------------------------------------------------- #
# Discovery (TMDB search)
# --------------------------------------------------------------------------- #
class DiscoverResult(BaseModel):
    """One discovery search row."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: MediaTypeField
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None


class DiscoverSearchResponse(BaseModel):
    """The discovery search result set."""

    model_config = ConfigDict(frozen=True)

    results: list[DiscoverResult]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class CreateRequestBody(BaseModel):
    """Create a media request by tmdb id + media type."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: MediaTypeField


class RequestResponse(BaseModel):
    """A media request as returned to the client."""

    model_config = ConfigDict(frozen=True)

    id: int
    tmdb_id: int
    media_type: str
    title: str
    status: str
    year: int | None = None
    is_anime: bool = False


class RequestListResponse(BaseModel):
    """A list of media requests."""

    model_config = ConfigDict(frozen=True)

    requests: list[RequestResponse]


# --------------------------------------------------------------------------- #
# Search preview (decision-engine dry run) — the headline endpoint
# --------------------------------------------------------------------------- #
class SearchPreviewRequest(BaseModel):
    """Preview by ``request_id`` OR by an explicit media descriptor.

    When ``request_id`` is set the other fields are ignored (resolved from the
    stored request). Otherwise ``tmdb_id``, ``media_type`` and ``title`` are
    required.
    """

    model_config = ConfigDict(frozen=True)

    request_id: int | None = None
    tmdb_id: int | None = None
    media_type: MediaTypeField | None = None
    title: str | None = None
    year: int | None = None
    season: int | None = None


class AcceptedRelease(BaseModel):
    """A release that passed the quality gate and blocklist, with its ranking."""

    model_config = ConfigDict(frozen=True)

    title: str
    quality_name: str
    resolution: str
    source: str
    score: float
    seeders: int | None = None
    indexer: str
    info_hash: str | None = None
    guid: str


class RejectedRelease(BaseModel):
    """A discarded release paired with its surfaced rejection reason."""

    model_config = ConfigDict(frozen=True)

    title: str
    reason: str


class SearchPreviewResponse(BaseModel):
    """Ranked accepted releases, per-release rejections, and the empty-set flag."""

    model_config = ConfigDict(frozen=True)

    accepted: list[AcceptedRelease]
    rejected: list[RejectedRelease]
    no_acceptable_release: bool


# --------------------------------------------------------------------------- #
# Queue (downloads + reconciled status)
# --------------------------------------------------------------------------- #
class QueueItem(BaseModel):
    """A tracked download in the live queue."""

    model_config = ConfigDict(frozen=True)

    id: int
    torrent_hash: str
    status: str
    progress: float = 0.0
    seed_ratio: float = 0.0
    media_request_id: int | None = None
    tmdb_id: int | None = None
    failed_reason: str | None = None


class QueueResponse(BaseModel):
    """The reconciled download queue."""

    model_config = ConfigDict(frozen=True)

    queue: list[QueueItem]


class GrabRequest(BaseModel):
    """Grab a release for a request: a chosen ``info_hash``/``guid`` or the top pick.

    With neither ``info_hash`` nor ``guid`` set, the highest-ranked accepted
    release is grabbed ("grab top"). For a TV request, ``season`` scopes both the
    indexer search and the stored download to that season; it is ignored for
    movies.
    """

    model_config = ConfigDict(frozen=True)

    request_id: int
    info_hash: str | None = None
    guid: str | None = None
    season: int | None = None


# --------------------------------------------------------------------------- #
# Blocklist
# --------------------------------------------------------------------------- #
class BlocklistEntry(BaseModel):
    """A blocklist entry as returned to the client."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_title: str
    reason: str
    tmdb_id: int | None = None
    torrent_hash: str | None = None
    indexer: str | None = None
    protocol: str | None = None
    media_type: str | None = None
    added_at: datetime | None = None


class BlocklistResponse(BaseModel):
    """A list of blocklist entries."""

    model_config = ConfigDict(frozen=True)

    entries: list[BlocklistEntry]


# --------------------------------------------------------------------------- #
# Quality profile (read-only in alpha)
# --------------------------------------------------------------------------- #
class QualityProfileItemResponse(BaseModel):
    """One ordered entry in the quality profile."""

    model_config = ConfigDict(frozen=True)

    quality_id: int
    name: str
    source: str
    resolution: str
    allowed: bool


class QualityProfileResponse(BaseModel):
    """The serialized default quality profile (ordered low -> high, with cutoff)."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    cutoff_quality_id: int
    cutoff_name: str
    upgrade_allowed: bool
    items: list[QualityProfileItemResponse]
