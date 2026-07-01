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
    "DiscoverHomeResponse",
    "DiscoverHomeRow",
    "DiscoverListResponse",
    "DiscoverResult",
    "DiscoverSearchResponse",
    "GrabRequest",
    "PlexLibraryOption",
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
    "SeasonStatus",
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


class PlexLibraryOption(BaseModel):
    """One movie- OR tv-library folder Plex reports, with whether the app can write to it.

    ``path`` is a Plex library location (from Plex's own ``/library/sections``), so
    choosing it for ``movies_root`` / ``tv_root`` avoids a typed path entirely (and
    the path↔section mismatch that breaks a targeted scan). ``section_type`` tags
    which root this option is for (``"movie"`` -> ``movies_root``, ``"tv"`` ->
    ``tv_root``) so a single generalized list can drive both pickers. ``writable``
    is the app's own check (``None`` when not probed — see the field): a
    known-not-writable location is the split-mount signal — surfaced, not hidden.
    """

    model_config = ConfigDict(frozen=True)

    section_key: str
    title: str
    path: str
    section_type: MediaTypeField
    # ``None`` = writability was NOT probed. The pre-init ``validate/plex`` wizard
    # step never touches the filesystem for a caller-supplied Plex server (that would
    # be a pre-auth local-FS existence/writability oracle); a real ``True``/``False``
    # is set only by the authenticated Settings picker, where the operator's own
    # stored creds make the probe legitimate.
    writable: bool | None = None


class ServiceValidateResponse(BaseModel):
    """Result of a connection check. ``message`` is operator-facing; ``detail``
    is an optional diagnostic. Neither ever contains a secret value.

    For Plex, ``libraries`` carries the movie AND tv library folders (each tagged
    by ``section_type``) so the UI can offer pick-lists for ``movies_root`` /
    ``tv_root`` instead of a typed path. ``None`` for every other service (and for
    a failed Plex check)."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    message: str
    detail: str | None = None
    libraries: list[PlexLibraryOption] | None = None


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
    movies_root: str
    # Optional (mirrors ``movies_root``'s TV sibling): an install may complete
    # setup with only a Movies library, only a TV library, or both -- see
    # ``setup_validation.validate_plex``'s "movie-only or tv-only is legit" gate.
    # Written to the ``tv_root`` setting ONLY when non-empty (setup.complete).
    tv_root: str | None = None


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
    movies_root: str | None = None
    tv_root: str | None = None


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
    movies_root: str | None = Field(default=None)
    tv_root: str | None = Field(default=None)


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
    backdrop_url: str | None = None


class DiscoverSearchResponse(BaseModel):
    """The discovery search result set."""

    model_config = ConfigDict(frozen=True)

    results: list[DiscoverResult]


class DiscoverHomeRow(BaseModel):
    """One server-composed Discover row: a title + its items.

    ``row_type`` is an OPEN string (e.g. ``trending`` / ``popular`` / ``upcoming``)
    so the frontend renders rows generically and stays dumb about WHY a row exists
    — TV and recommendation rows slot in later with no contract change.
    """

    model_config = ConfigDict(frozen=True)

    row_type: str
    title: str
    items: list[DiscoverResult]


class DiscoverHomeResponse(BaseModel):
    """The composed Discover home: an optional spotlight + ordered rows."""

    model_config = ConfigDict(frozen=True)

    spotlight: DiscoverResult | None = None
    rows: list[DiscoverHomeRow]


class DiscoverListResponse(BaseModel):
    """A paginated category listing (trending / popular / upcoming)."""

    model_config = ConfigDict(frozen=True)

    page: int
    total_pages: int
    total_results: int
    results: list[DiscoverResult]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class CreateRequestBody(BaseModel):
    """Create a media request by tmdb id + media type."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: MediaTypeField
    # TV only (ignored for movies): explicit season numbers to track. Omitted or
    # empty means "track the whole aired series" -- every season 1..season_count,
    # specials (season 0) excluded. See ``request_service.create_request`` /
    # ``_season_numbers``. A repeat POST with a NEW season list GROWS the tracked
    # set rather than being dropped by the request-level dedup.
    seasons: list[int] | None = None


class SeasonStatus(BaseModel):
    """One tracked season's status, embedded in a tv ``RequestResponse``."""

    model_config = ConfigDict(frozen=True)

    season_number: int
    status: str


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
    poster_url: str | None = None
    backdrop_url: str | None = None
    # TV only: this show's per-season rollup, ordered by season number. ``None``
    # for a movie (movies have no ``SeasonRequest`` rows). ``status`` above is the
    # COMPUTED fold of these (``domain.season_rollup.rollup_status``).
    seasons: list[SeasonStatus] | None = None


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
    # TV only: the specific episode number(s) wanted out of ``season``. ``None``/
    # empty means "the whole season" -- this is also what makes ``decision_service.
    # preview`` prefer a season-pack release over an equivalent single episode.
    episodes: list[int] | None = None


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
    # TV only: the season this download belongs to, and the specific episode
    # number(s) it is scoped to importing (``None`` = import every valid video
    # file found for the season). Both ``None`` for a movie.
    season: int | None = None
    episodes: list[int] | None = None
    failed_reason: str | None = None


class QueueResponse(BaseModel):
    """The reconciled download queue."""

    model_config = ConfigDict(frozen=True)

    queue: list[QueueItem]


class GrabRequest(BaseModel):
    """Grab a release for a request: a chosen ``info_hash``/``guid`` or the top pick.

    With neither ``info_hash`` nor ``guid`` set, the highest-ranked accepted
    release is grabbed ("grab top"). For a TV request, ``season`` scopes both the
    indexer search and the stored download to that season; ``episodes`` further
    scopes it to those specific episode number(s) (``None``/empty = the whole
    season). Both are ignored for movies.
    """

    model_config = ConfigDict(frozen=True)

    request_id: int
    info_hash: str | None = None
    guid: str | None = None
    season: int | None = None
    episodes: list[int] | None = None


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
