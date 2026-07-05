"""Pydantic v2 request/response models for the REST API.

These DTOs are the published wire contract (they shape the exported OpenAPI).
Response models NEVER carry secret values: service credentials (Plex token,
Prowlarr / TMDB api keys, qBittorrent password) are represented by a masked
``"***"`` placeholder when configured, never the plaintext.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from plex_manager.web.url_validation import url_shape_error

__all__ = [
    "AcceptedRelease",
    "AuthMeResponse",
    "AuthUser",
    "BlocklistEntry",
    "BlocklistResponse",
    "CreateRequestBody",
    "DiscoverHomeResponse",
    "DiscoverHomeRow",
    "DiscoverListResponse",
    "DiscoverResult",
    "DiscoverSearchResponse",
    "DiskResponse",
    "DiskRootItem",
    "ErrorDetail",
    "EvictErrorItem",
    "EvictResponse",
    "EvictionCandidateItem",
    "EvictionOutcomeItem",
    "GrabRequest",
    "HealthResponse",
    "KeepForeverBody",
    "LiveLogRecordItem",
    "LogEventItem",
    "LogsResponse",
    "LogsTailResponse",
    "PlexLibraryOption",
    "PlexLoginCompleteRequest",
    "PlexLoginStartResponse",
    "PlexValidateRequest",
    "ProwlarrValidateRequest",
    "QbittorrentValidateRequest",
    "QualityProfileItemResponse",
    "QualityProfileResponse",
    "QueueItem",
    "QueueResponse",
    "ReconcileStatusItem",
    "RejectedRelease",
    "ReportIssueBody",
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
    "SubsystemHealthItem",
    "TmdbValidateRequest",
]

MediaTypeField = Literal["movie", "tv"]

# The library-state a Discover/Search tile is decorated with (issue #29). The SERVER
# base state: presence (from Plex) folded with the request-store status. Kept in sync
# with ``services.discovery_service.derive_library_state`` (Python) and the client's
# ``lib/tileState.ts`` / ``lib/status.ts`` (TS) -- the same status→state table on both
# sides of the wire. Default ``"none"`` on ``DiscoverResult`` models a missing/degraded
# decoration honestly (no fabricated presence).
LibraryStateField = Literal["none", "requested", "processing", "available", "partially_available"]


class ErrorDetail(BaseModel):
    """Machine-readable error body returned by manual HTTPException paths."""

    model_config = ConfigDict(frozen=True)

    detail: str


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
# Browser authentication — Plex hosted sign-in
# --------------------------------------------------------------------------- #
class PlexLoginStartResponse(BaseModel):
    """A pending Plex PIN login challenge."""

    model_config = ConfigDict(frozen=True)

    state: str
    auth_url: str
    expires_at: datetime


class PlexLoginCompleteRequest(BaseModel):
    """Complete a pending Plex PIN login challenge."""

    model_config = ConfigDict(frozen=True)

    state: str


class AuthUser(BaseModel):
    """Current signed-in Plex user."""

    model_config = ConfigDict(frozen=True)

    id: int
    plex_id: int | None
    username: str
    email: str | None = None
    avatar_url: str | None = None
    is_admin: bool = False


class AuthMeResponse(BaseModel):
    """Current app authentication state."""

    model_config = ConfigDict(frozen=True)

    authenticated: bool
    auth_method: Literal["api_key", "plex_session", "dev_bypass"] | None = None
    is_admin: bool = False
    user: AuthUser | None = None


# --------------------------------------------------------------------------- #
# Setup completion + status
# --------------------------------------------------------------------------- #
# Every library root ``POST /setup/complete`` accepts; the at-least-one-root
# invariant quantifies over ALL of them (the wizard's completion gate counts the
# anime roots since ADR-0015, so an anime-only install must pass the runtime
# check too — considering only movies/tv here would 422 a body the UI allows).
_LIBRARY_ROOT_FIELDS: tuple[str, ...] = (
    "movies_root",
    "tv_root",
    "anime_movie_root",
    "anime_tv_root",
)


class SetupCompleteRequest(BaseModel):
    """The validated credential set written on ``POST /setup/complete``.

    ``plex_url``/``prowlarr_url``/``qbittorrent_url`` are REQUIRED and
    shape-checked (issue #44): unlike ``SettingsUpdate``'s partial-update
    semantics, an empty string is REJECTED here (there is no "leave unchanged"
    concept on a one-shot install), closing the direct-API-caller bypass of the
    wizard's live "Test connection" probes -- see ``_validate_service_url_shape``.
    """

    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "anyOf": [
                        {
                            "required": [field],
                            "properties": {field: {"type": "string", "pattern": "\\S"}},
                        }
                        for field in _LIBRARY_ROOT_FIELDS
                    ]
                }
            ]
        },
    )

    plex_url: str
    plex_token: str
    prowlarr_url: str
    prowlarr_api_key: str
    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str
    tmdb_api_key: str
    # Optional library roots: an install may complete setup with only a Movies
    # library, only a TV library, or both -- see ``setup_validation.validate_plex``'s
    # "movie-only or tv-only is legit" gate. Each setting is written only when
    # non-empty (setup.complete).
    movies_root: str | None = None
    tv_root: str | None = None
    # Anime library routing (ADR-0015) — OPTIONAL like ``tv_root``: unset means
    # anime imports fall back to ``movies_root``/``tv_root``, identical to
    # behavior before this feature existed. Written ONLY when non-empty.
    anime_movie_root: str | None = None
    anime_tv_root: str | None = None

    @field_validator("plex_url", "prowlarr_url", "qbittorrent_url")
    @classmethod
    def _validate_service_url_shape(cls, value: str) -> str:
        """Shape-check a REQUIRED service url (issue #44).

        These fields are required strings with no "leave unchanged" concept, so
        (unlike ``SettingsUpdate``'s partial-update version of this validator) an
        empty string is REJECTED here -- ``url_shape_error("")`` already returns
        the same message (an empty string has no scheme/hostname), so no special
        case is needed to close the empty-string bypass. Shares the exact
        predicate the setup wizard's live "Test connection" probes use
        (:func:`~plex_manager.web.url_validation.url_shape_error`), so a
        direct-API caller cannot post a url the wizard UI would never let through.
        """
        message = url_shape_error(value)
        if message is not None:
            raise ValueError(message)
        return value

    @model_validator(mode="before")
    @classmethod
    def require_at_least_one_library_root(cls, data: Any) -> Any:
        """Normalize blank roots to ``None``; require at least one NON-BLANK root.

        Quantifies over EVERY root in :data:`_LIBRARY_ROOT_FIELDS` — including the
        ADR-0015 anime roots — matching the wizard's completion gate, so an
        anime-only install that passed the UI is never 422'd here.
        """
        if not isinstance(data, dict):
            return data

        raw = cast("dict[str, object]", data)
        values = {field: raw.get(field) for field in _LIBRARY_ROOT_FIELDS}
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            return raw  # let per-field validation surface the type error

        normalized: dict[str, object] = dict(raw)
        for field, value in values.items():
            if isinstance(value, str) and not value.strip():
                normalized[field] = None
                values[field] = None

        if not any(isinstance(value, str) and value.strip() for value in values.values()):
            raise ValueError("at_least_one_library_root_required")
        return normalized


class SetupStatusResponse(BaseModel):
    """Install state. ``app_api_key`` is populated only once initialized."""

    model_config = ConfigDict(frozen=True)

    initialized: bool
    app_api_key: str | None = None
    setup_token_required: bool = False


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
    # Anime library routing (ADR-0015) — same "unset = falls back" semantics as
    # ``movies_root``/``tv_root`` above; ``None`` means the setting is unset (an
    # anime import routes to the normal root), not that anime is unsupported.
    anime_movie_root: str | None = None
    anime_tv_root: str | None = None
    # Operability beta (ADR-0012) — the eviction/log-retention knobs from
    # ``web.deps.KNOWN_SETTING_KEYS``. ``None`` means "unset" (the typed getters
    # in ``web.deps`` — e.g. ``get_eviction_grace_days`` — fall back to their own
    # safe default in that case; this response mirrors what is actually STORED,
    # not the effective fallback, matching ``movies_root``/``tv_root`` above).
    # Stored as plain-text ``settings.value`` strings; pydantic coerces the
    # stored string into the typed field below on the way out.
    disk_pressure_threshold_percent: float | None = None
    disk_pressure_target_percent: float | None = None
    eviction_grace_days: int | None = None
    eviction_enabled: bool | None = None
    eviction_proactive_enabled: bool | None = None
    eviction_interval_minutes: float | None = None
    log_retention_days: int | None = None
    # Auto-grab worker (ADR-0013) — the master on/off switch for the background
    # request->search->grab loop (default ON in ``web.deps``; ``None`` = unset =
    # the default applies). Plain boolean config, same wire semantics as
    # ``eviction_enabled`` above.
    auto_grab_enabled: bool | None = None


class AppApiKeyResponse(BaseModel):
    """The current (reveal) or freshly-minted (rotate) app ``X-Api-Key``, in plaintext.

    Authenticated-only (Plex session or currently-valid ``X-Api-Key``): reveal is
    the belt-and-braces recovery path for a lost/forgotten key on a device that
    still has it saved, and rotate mints and returns a brand-new key ONCE — the
    plaintext is never retrievable again after this response, only the
    Fernet-encrypted column at rest (matching the one-time disclosure setup's
    ``/complete`` already gives the initial key).
    """

    model_config = ConfigDict(frozen=True)

    app_api_key: str


class SettingsUpdate(BaseModel):
    """Partial upsert of service config (``PUT /settings``).

    Only fields present in the request body are written; secret fields are stored
    encrypted at rest. ``None`` / absent fields are left unchanged.

    ``plex_url``/``prowlarr_url``/``qbittorrent_url`` are shape-validated at
    write time (issue #44), with three-way wire semantics matching
    ``movies_root``'s established partial-update convention:

    * ``None`` (absent from the body, or an explicit JSON ``null``) means
      "leave this field unchanged" -- ``put_settings_endpoint`` skips a
      ``None`` field entirely, so it is never shape-checked and can never fail
      here.
    * ``""`` (empty string) is an explicit clear-to-unset -- ALLOWED, not
      rejected: the adapters already treat a falsy stored url as unconfigured
      and answer an honest 409 ``service_not_configured`` (see
      ``web.deps.get_prowlarr`` / ``get_qbittorrent`` / ``get_plex``), so
      clearing to blank is a valid, intentional write.
    * Any other non-empty string is shape-checked against
      :func:`~plex_manager.web.url_validation.url_shape_error` -- the SAME
      predicate the setup wizard's live "Test connection" probes use, so a
      malformed url (bad scheme, missing host, malformed port, control
      characters, ...) is rejected here as a 422 -- before it is ever
      persisted -- rather than only surfacing later as an opaque downstream
      failure.
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
    # Anime library routing (ADR-0015) — see ``SettingsResponse`` above; both
    # optional, same "unset/absent -> unchanged" write semantics as every other
    # field on this partial-upsert model.
    anime_movie_root: str | None = Field(default=None)
    anime_tv_root: str | None = Field(default=None)
    # Operability beta (ADR-0012) — see ``SettingsResponse`` above for the wire
    # semantics; bounded with ``ge``/``le`` so a malformed operator input is a
    # visible 422, not a value that silently sails past ``web.deps``'s own
    # unset/unparsable fallback (that fallback only guards a CORRUPT stored
    # value, not a bad NEW value coming in over this endpoint).
    disk_pressure_threshold_percent: float | None = Field(default=None, ge=0, le=100)
    disk_pressure_target_percent: float | None = Field(default=None, ge=0, le=100)
    eviction_grace_days: int | None = Field(default=None, ge=0)
    eviction_enabled: bool | None = Field(default=None)
    eviction_proactive_enabled: bool | None = Field(default=None)
    eviction_interval_minutes: float | None = Field(default=None, gt=0)
    log_retention_days: int | None = Field(default=None, ge=0)
    # Auto-grab worker (ADR-0013) — see ``SettingsResponse``. A plain boolean, no
    # bounds to enforce.
    auto_grab_enabled: bool | None = Field(default=None)

    @field_validator("plex_url", "prowlarr_url", "qbittorrent_url")
    @classmethod
    def _validate_service_url_shape(cls, value: str | None) -> str | None:
        """Shape-check a non-blank service url at write time (issue #44).

        See the class docstring for the full ``None``/``""``/non-empty wire
        semantics. ``not value`` catches BOTH ``None`` (leave unchanged) and
        ``""`` (explicit clear-to-unset) in one branch -- both are passed
        through untouched, never rejected; only a genuinely non-empty string is
        shape-checked.
        """
        if not value:
            return value
        message = url_shape_error(value)
        if message is not None:
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _target_at_or_below_threshold(self) -> SettingsUpdate:
        """Reject a target ABOVE the trigger threshold when both are set together.

        ``select_evictions`` starts ``projected = used_pct`` and stops the moment
        ``used_pct <= target_pct``. So a target above the threshold makes every root
        in the ``[threshold, target]`` band read "under pressure" yet select NOTHING
        — a valid-looking settings update that silently disables pressure relief. A
        422 here makes that misconfiguration visible instead of a silent dead band.
        (Enforced when both fields are present in the same request — which the
        Settings form always sends together. A direct-API split update that changes
        only ONE side against a stored other side is a SEPARATE, narrower window
        this fast-path validator cannot see — it has no access to what is currently
        persisted. That case is cross-checked against the STORED counterpart in
        ``web.routers.settings._validate_disk_pressure_pair``, which the ``PUT``
        endpoint runs before writing anything; this validator stays as the cheap,
        no-DB-access fast path for the common both-sent case.)
        """
        threshold = self.disk_pressure_threshold_percent
        target = self.disk_pressure_target_percent
        if threshold is not None and target is not None and target > threshold:
            msg = (
                "disk_pressure_target_percent must be <= disk_pressure_threshold_percent "
                "(a target above the trigger leaves the whole threshold-to-target band "
                "under 'pressure' with nothing to evict)"
            )
            raise ValueError(msg)
        return self


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
    # Response-only library-state hint for the tile (issue #29): no DB column, no
    # migration -- computed per page from Plex presence + the request store. Default
    # ``"none"`` keeps construction back-compatible and honestly models a page that
    # was NOT decorated (Plex unconfigured/unreachable) rather than a fake "not owned".
    library_state: LibraryStateField = "none"


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


class ReportIssueBody(BaseModel):
    """``POST /requests/{id}/report-issue`` -- report a bad imported file (ADR-0014).

    ``reason`` is one of the operator-choosable :class:`BlocklistReason` values
    (``failed`` is auto-only and deliberately excluded). ``season`` is REQUIRED for
    a tv request (report-issue is per-season, mirroring grab) and ignored for a
    movie. The verb blocklists the culprit release, purges the torrent + library
    file, and synchronously re-searches (see ``correction_service.report_issue``).
    """

    model_config = ConfigDict(frozen=True)

    reason: Literal["bad_quality", "wrong_media", "user_reported"]
    season: int | None = None


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
    # Operator pin (ADR-0012): ``True`` means ``domain/eviction.py`` will never
    # select this title (or, for a show, any of its seasons) regardless of watch
    # state or disk pressure. Toggled via ``POST /requests/{id}/keep-forever``.
    keep_forever: bool = False


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
    season). Every TV grab is per-season: the endpoint REJECTS (422) a tv request
    grabbed with no ``season``, and REJECTS (422) a non-tv (movie) request grabbed
    WITH a ``season`` -- the branch is always the request's actual media type,
    never merely whether ``season`` happens to be set.
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


# --------------------------------------------------------------------------- #
# Ops — health / status dashboard (ADR-0012, Component 1)
# --------------------------------------------------------------------------- #
class SubsystemHealthItem(BaseModel):
    """One upstream's reachability, as ``services.health_service.SubsystemHealth``
    reports it. ``not_configured`` is honest -- never confused with ``down``."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: Literal["ok", "degraded", "down", "not_configured"]
    detail: str | None = None
    checked_at: datetime


class DiskGaugeItem(BaseModel):
    """One configured library root's usage snapshot (health dashboard gauge)."""

    model_config = ConfigDict(frozen=True)

    root: str
    path: str
    total_bytes: int
    available_bytes: int
    used_percent: float
    error: str | None = None


class ReconcileStatusItem(BaseModel):
    """The background reconcile loop's own health, mirrored from
    ``services.health_service.ReconcileStatusSnapshot``. Deliberately separate
    from the subsystem cards above -- a cycle can complete OK even while one
    upstream inside it degraded (see that class's docstring)."""

    model_config = ConfigDict(frozen=True)

    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error_type: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0


class AutograbStatusItem(BaseModel):
    """The background auto-grab loop's own health (ADR-0013), mirrored from
    ``services.health_service.AutograbStatusSnapshot``. The exact shape of
    ``ReconcileStatusItem`` above, for the separate ``_autograb_loop`` -- a
    Prowlarr outage surfaces here as a failing loop so the operator sees WHY
    nothing is being grabbed, not just that requests sit at ``pending``.

    ``cooled_down_scopes`` is how many scopes are CURRENTLY in a grab-pipeline
    cooldown (ADR-0013): scopes whose grab keeps failing, skipped so they don't
    starve the search budget -- a non-zero count is the operator's signal that the
    grab pipeline (not the search) is what's broken."""

    model_config = ConfigDict(frozen=True)

    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error_type: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    cooled_down_scopes: int = 0


class HealthResponse(BaseModel):
    """``GET /api/v1/ops/health`` -- one read answering "is every subsystem
    healthy, is the reconcile loop running, how full is the disk"."""

    model_config = ConfigDict(frozen=True)

    subsystems: list[SubsystemHealthItem]
    disks: list[DiskGaugeItem]
    reconcile: ReconcileStatusItem
    autograb: AutograbStatusItem


# --------------------------------------------------------------------------- #
# Ops — log / console viewer (ADR-0012, Component 2)
# --------------------------------------------------------------------------- #
class LogEventItem(BaseModel):
    """One durably-stored ``log_events`` row, as the log viewer / export reads it.

    ``context`` carries correlation ids (``request_id``/``download_id``/
    ``tmdb_id``) set at the log call site -- never a secret-bearing field (see
    ``models.LogEvent``)."""

    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None = None


class LogsResponse(BaseModel):
    """A filtered, paginated page of ``GET /api/v1/ops/logs``.

    ``total`` is the count of rows matching the filter (not the whole table),
    so the client can tell whether more pages exist beyond ``events``."""

    model_config = ConfigDict(frozen=True)

    total: int
    events: list[LogEventItem]


class LiveLogRecordItem(BaseModel):
    """One entry from the in-memory, all-levels live tail ring buffer.

    Unlike :class:`LogEventItem`, this has no durable row id -- the ring buffer
    is lost on restart and never persisted (only INFO-and-above reaches
    ``log_events``; this endpoint shows EVERY level, including DEBUG)."""

    model_config = ConfigDict(frozen=True)

    created_at: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None = None


class LogsTailResponse(BaseModel):
    """``GET /api/v1/ops/logs/tail`` -- the live ring buffer, newest first.

    ``dropped_count`` is the capture handler's own honest signal: how many
    INFO+ records could not be enqueued for durable storage since startup
    because the drain queue was full (the ring buffer itself is unaffected --
    see ``LogCaptureHandler``'s docstring)."""

    model_config = ConfigDict(frozen=True)

    events: list[LiveLogRecordItem]
    dropped_count: int


# --------------------------------------------------------------------------- #
# Ops — disk-pressure eviction (ADR-0012, Component 3)
# --------------------------------------------------------------------------- #
class EvictionCandidateItem(BaseModel):
    """One title/season a pressure sweep WOULD evict (the ``GET /ops/disk``
    preview), or one it DID evict (a ``POST /ops/evict`` outcome shares this
    shape's fields, see :class:`EvictionOutcomeItem`)."""

    model_config = ConfigDict(frozen=True)

    request_id: int
    media_type: MediaTypeField
    title: str
    season: int | None = None
    status: str
    last_viewed_at: datetime | None = None
    size_percent: float
    library_path: str | None = None


class DiskRootItem(BaseModel):
    """One configured library root's usage + its ranked eviction preview."""

    model_config = ConfigDict(frozen=True)

    root: str
    path: str
    total_bytes: int
    available_bytes: int
    used_percent: float
    error: str | None = None
    candidates: list[EvictionCandidateItem]


class DiskResponse(BaseModel):
    """``GET /api/v1/ops/disk`` -- usage + a ranked eviction-candidate preview
    per configured root."""

    model_config = ConfigDict(frozen=True)

    roots: list[DiskRootItem]


class EvictionOutcomeItem(BaseModel):
    """One candidate a manual ``POST /api/v1/ops/evict`` sweep actually evicted."""

    model_config = ConfigDict(frozen=True)

    request_id: int
    media_type: MediaTypeField
    title: str
    season: int | None = None
    library_path: str
    freed_bytes: int | None = None


class EvictErrorItem(BaseModel):
    """One root's sweep failure inside a manual ``POST /api/v1/ops/evict`` --
    a LATER root raising (e.g. a transient Plex error resolving TV watch
    state) must never hide an EARLIER root's evictions that already deleted
    files and committed (honesty over silence: partial progress is surfaced,
    not swallowed behind a 500)."""

    model_config = ConfigDict(frozen=True)

    root: Literal["movies_root", "tv_root", "anime_movie_root", "anime_tv_root"]
    detail: str


class EvictResponse(BaseModel):
    """The result of a manual disk-pressure sweep (north-star #1: a button that
    frees space on demand). Empty ``evicted`` is a normal, honest outcome (no
    root was under pressure, or nothing was eligible). ``errors`` is populated
    per-root when THAT root's own sweep raised -- every other root's outcome
    in ``evicted`` still stands; the sweep never aborts one root's already
    committed work just because a sibling root failed."""

    model_config = ConfigDict(frozen=True)

    evicted: list[EvictionOutcomeItem]
    errors: list[EvictErrorItem] = Field(default_factory=list[EvictErrorItem])


# --------------------------------------------------------------------------- #
# Requests — keep-forever pin (ADR-0012)
# --------------------------------------------------------------------------- #
class KeepForeverBody(BaseModel):
    """``POST /api/v1/requests/{id}/keep-forever`` -- set or clear the pin."""

    model_config = ConfigDict(frozen=True)

    keep_forever: bool
