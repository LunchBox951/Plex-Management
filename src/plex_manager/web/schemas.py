"""Pydantic v2 request/response models for the REST API.

These DTOs are the published wire contract (they shape the exported OpenAPI).
Response models NEVER carry secret values: service credentials (Plex token,
Prowlarr / TMDB api keys, qBittorrent password) are represented by a masked
``"***"`` placeholder when configured, never the plaintext.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from plex_manager.domain.state_machine import DownloadState
from plex_manager.headersafe import HEADER_VALUE_MESSAGE, header_value_error
from plex_manager.models import DownloadScopeStatus, RequestStatus
from plex_manager.web.settings_bounds import (
    DISK_PRESSURE_PERCENT_MAX,
    DISK_PRESSURE_PERCENT_MIN,
    EVICTION_GRACE_DAYS_MAX,
    EVICTION_INTERVAL_MAX_MINUTES,
    LOG_MAX_ROWS_MAX,
    LOG_RETENTION_DAYS_MAX,
)
from plex_manager.web.url_validation import url_shape_error

__all__ = [
    "AcceptedRelease",
    "AppApiKeyResponse",
    "AppApiKeyStatusResponse",
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
    "ErrorEnvelope",
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
    "PlexServerConnection",
    "PlexServerOption",
    "PlexServersResponse",
    "PlexSignInRequest",
    "PlexValidateRequest",
    "ProwlarrValidateRequest",
    "QbittorrentValidateRequest",
    "QualityProfileItemResponse",
    "QualityProfileResponse",
    "QueueItem",
    "QueueResponse",
    "QueueScope",
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
    "UpdateActionRequest",
    "UpdateClaimRequest",
    "UpdateClaimResponse",
    "UpdateEligibilityResponse",
    "UpdateLeaseRequest",
    "UpdateLeaseResponse",
    "UpdateOutcomeRequest",
    "UpdateResultItem",
    "UpdateStatusResponse",
]

MediaTypeField = Literal["movie", "tv"]

# The library-state a Discover/Search tile is decorated with (issue #29). The SERVER
# base state: presence (from Plex) folded with the request-store status. Kept in sync
# with ``services.discovery_service.derive_library_state`` (Python) and the client's
# ``lib/tileState.ts`` / ``lib/status.ts`` (TS) -- the same status→state table on both
# sides of the wire. Default ``"none"`` on ``DiscoverResult`` models a missing/degraded
# decoration honestly (no fabricated presence).
LibraryStateField = Literal["none", "requested", "processing", "available", "partially_available"]
UpdateWeekday = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class ErrorDetail(BaseModel):
    """Machine-readable error body returned by manual HTTPException paths."""

    model_config = ConfigDict(frozen=True)

    detail: str


class ErrorEnvelope(BaseModel):
    """Structured error body for auth/setup failures (north star #3).

    The richer sibling of :class:`ErrorDetail`, rendered by
    ``web.errors.install_error_handlers``. ``detail`` stays the stable machine
    code the SPA's humanizer keys on; ``message``/``hint`` are operator-facing
    prose; ``diagnostics`` carries only NON-secret context (host, status, ...) —
    a secret NEVER appears here. ``hint``/``diagnostics`` are omitted from the
    wire when absent, so a client reads their absence, not an empty value.
    """

    model_config = ConfigDict(frozen=True)

    detail: str
    message: str
    hint: str | None = None
    diagnostics: dict[str, str] | None = None


# --------------------------------------------------------------------------- #
# Setup wizard — connection validation (unauthenticated, pre-init)
# --------------------------------------------------------------------------- #
class PlexValidateRequest(BaseModel):
    """Candidate Plex server to test (``POST /setup/validate/plex``).

    ``token`` is OPTIONAL: omitted (``None``) means "use the signed-in admin's
    stored Plex OAuth token" — the wizard's happy path never re-types a token and
    supplies an advertised server connection. A non-null ``token`` is the
    explicit credential authorization required for a custom URL.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    token: str | None = None


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
    # A container-visible remap of ``path`` (see ``services.path_visibility``) when
    # ``path`` is a HOST-namespace location this server can't see under its own
    # mounts -- e.g. Plex reports ``/home/Media/Movies`` but this container only
    # sees ``/media/Movies``. ``None`` when ``path`` already resolves here, or no
    # remap exists. The picker UIs prefer this as the option's STORED value, so
    # selecting it round-trips a path the write-time gate (setup/settings) accepts
    # without a second remap.
    suggested_path: str | None = None
    # NOTE (PR #147 round 3, maintainer decision): a short-lived
    # ``low_confidence_suggested_path`` mount-root GUESS briefly lived here within
    # this PR and was removed before release -- an unresolvable Plex location must
    # never be dressed up as a pickable ``/media``, even flagged low-confidence (a
    # child section like ``/srv/plex-data/Movies`` would misroute to the bare mount
    # root). The rare arbitrary-bind-root topology is served by manual entry plus
    # the wizard's visibility hint instead. The field never shipped in a release,
    # so removing it breaks no released client.


ProbeStatusField = Literal["ok", "unreachable"]


class PlexServerConnection(BaseModel):
    """One advertised address for an owned Plex server, with its probe verdict.

    ``uri`` is the exact connection plex.tv reports (``local`` marks a LAN address;
    ``relay`` a plex.tv relay). ``status`` is this backend's OWN reachability probe
    of ``{uri}/identity`` — ``"ok"`` when it answered, ``"unreachable"`` when the
    backend could not reach it (with ``error_code`` naming why). A dead connection
    is surfaced honestly, never dropped, so the operator can pick a reachable one.
    """

    model_config = ConfigDict(frozen=True)

    uri: str
    local: bool
    relay: bool
    status: ProbeStatusField
    error_code: str | None = None


class PlexServerOption(BaseModel):
    """One of the signed-in admin's OWNED Plex servers, offered by the wizard."""

    model_config = ConfigDict(frozen=True)

    name: str
    machine_identifier: str
    connections: list[PlexServerConnection]


class PlexServersResponse(BaseModel):
    """The admin's owned Plex servers with each connection probed
    (``GET /setup/plex/servers``)."""

    model_config = ConfigDict(frozen=True)

    servers: list[PlexServerOption]


class ServiceValidateResponse(BaseModel):
    """Result of a connection check. ``message`` is operator-facing; ``detail``
    is an optional diagnostic. Neither ever contains a secret value.

    For Plex, ``libraries`` carries the movie AND tv library folders (each tagged
    by ``section_type``) so the UI can offer pick-lists for ``movies_root`` /
    ``tv_root`` instead of a typed path. ``None`` for every other service (and for
    a failed Plex check). ``machine_identifier`` is the probed Plex server's
    ``machineIdentifier`` (from its ``/identity``) — set only when the caller asked
    for the ownership-verifying variant of the Plex probe, so setup can assert
    ownership and store the id; ``None`` otherwise.

    ``download_path_note`` (qBittorrent only, issues #133/#157) is a NON-blocking,
    informational message: set only on an ``ok=True`` qBittorrent check whose
    client-reported default save path is NOT visible inside this container. It never
    flips ``ok`` to ``False`` -- Plex Manager directs every grab's ``save_path``
    explicitly (never relies on this default), so the mismatch is honestly
    surfaced but does not block setup/health. ``None`` for every other service, and
    for qBittorrent whenever the default path IS visible or could not be read."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    message: str
    detail: str | None = None
    libraries: list[PlexLibraryOption] | None = None
    machine_identifier: str | None = None
    download_path_note: str | None = None


# --------------------------------------------------------------------------- #
# Browser authentication — Plex sign-in
# --------------------------------------------------------------------------- #
class PlexSignInRequest(BaseModel):
    """A browser-obtained plex.tv token to verify server-side (``POST /auth/plex``).

    The browser ran the plex.tv PIN flow itself; the backend re-derives identity
    and server ownership from this token before writing any user or session, so
    the token is never trusted for its claims — only used to call plex.tv.
    """

    model_config = ConfigDict(frozen=True)

    auth_token: str


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

    ``plex_token`` may be omitted only when ``plex_url`` is a connection plex.tv
    advertised for the signed-in owner's server. A custom URL requires an
    explicitly supplied token so the stored owner token is never sent to an
    unlisted destination.
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
    # The wizard's chosen server — ADVISORY only: ``/setup/complete`` re-derives the
    # persisted id live from the submitted server's ``/identity`` and re-asserts
    # ownership, so a direct API caller cannot pair server-X creds with server-Y's
    # id (see ``web.routers.setup.complete``). Post-init sign-in then resolves
    # server access from the STORED (derived) id without re-probing /identity.
    plex_machine_identifier: str
    # OPTIONAL: ``None`` (omitted) means "persist the signed-in admin's stored Plex
    # OAuth token" for an advertised connection — the keyless wizard never re-types
    # the token. A non-null value is the explicit credential authorization required
    # for a custom URL.
    plex_token: str | None = Field(
        default=None,
        description=(
            "May be omitted only when plex_url is a plex.tv-advertised connection; "
            "a custom URL requires an explicitly supplied Plex token."
        ),
    )
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

    @field_validator("plex_token", "prowlarr_api_key")
    @classmethod
    def _validate_header_safe_credential(cls, value: str | None) -> str | None:
        """Reject a credential that cannot ride its outbound HTTP header BEFORE it
        is persisted (the header-safety persistence bypass).

        ``plex_token`` rides ``X-Plex-Token`` and ``prowlarr_api_key`` rides
        ``X-Api-Key``. A CR/LF/NUL value makes httpx echo the RAW credential in
        ``str(exc)`` (a secret leak through any adapter that logs a chained
        transport error -- e.g. ``ProwlarrIndexer._indexer_priorities``' priority
        warning), and a non-ASCII value makes httpx's ASCII header encoder raise an
        uncaught ``UnicodeEncodeError`` (a 500). The wizard's live "Test connection"
        probes already reject such a value up front
        (:func:`~plex_manager.web.setup_validation._require_header_safe_credential`);
        enforcing the SAME predicate here -- shared verbatim with ``SettingsUpdate``
        -- closes the direct-API / keyless-token bypass so a header-unsafe credential
        can never be stored and then leaked (or crash the grab loop) when an adapter
        later sends it as a header. ``None`` (an omitted ``plex_token``) is
        header-safe and passes untouched.
        """
        if value and header_value_error(value) is not None:
            raise ValueError(HEADER_VALUE_MESSAGE)
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
    """Install state: whether setup is finished, and whether the optional pre-init
    hardening token is required. No app key is ever minted or served — Plex sign-in
    is the sole credential model (there is no one-time key to disclose)."""

    model_config = ConfigDict(frozen=True)

    initialized: bool
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
    # ``web.deps.KNOWN_SETTING_KEYS``. ``None`` means "unset OR degraded to the
    # default" (the typed getters in ``web.deps`` — e.g.
    # ``get_eviction_grace_days`` — resolve to their safe default in that
    # case). A VALID stored value is mirrored verbatim; a corrupt/out-of-range
    # one is presented as the EFFECTIVE value the runtime resolves it to (a
    # clamped bound, the disk-pressure pair rule) or ``None`` when that
    # effective value IS the default — see ``web.routers.settings.
    # _sanitize_typed_settings``, which shares the ``web.deps`` resolvers so
    # this response can never claim a state the running loops aren't in.
    # Stored as plain-text ``settings.value`` strings; pydantic coerces the
    # (sanitized) string into the typed field below on the way out.
    disk_pressure_threshold_percent: float | None = None
    disk_pressure_target_percent: float | None = None
    eviction_grace_days: int | None = None
    eviction_enabled: bool | None = None
    eviction_proactive_enabled: bool | None = None
    eviction_interval_minutes: float | None = None
    log_retention_days: int | None = None
    # The row-count companion to log_retention_days (issue #152) — same
    # unset/degraded-to-default ``None`` wire semantics.
    log_max_rows: int | None = None
    # Auto-grab worker (ADR-0013) — the master on/off switch for the background
    # request->search->grab loop (default ON in ``web.deps``; ``None`` = unset =
    # the default applies). Plain boolean config, same wire semantics as
    # ``eviction_enabled`` above.
    auto_grab_enabled: bool | None = None
    # Container auto-update policy (ADR-0024). ``None`` means the persisted
    # setting is absent/corrupt and the documented runtime default applies.
    automatic_updates_enabled: bool | None = None
    automatic_update_timezone: str | None = None
    automatic_update_weekdays: list[UpdateWeekday] | None = None
    automatic_update_window_start: str | None = None
    automatic_update_window_end: str | None = None
    automatic_update_idle_only: bool | None = None


class AppApiKeyResponse(BaseModel):
    """The current (reveal) or freshly-minted (generate/rotate) app ``X-Api-Key``.

    Authenticated-only (Plex session or currently-valid ``X-Api-Key``). Setup mints
    NO key — this is an OPT-IN recovery/automation credential the operator generates
    on demand from Settings → Access. Reveal is the break-glass path for a key
    lost/forgotten on a device that still has it saved; generate/rotate mints and
    returns a brand-new key ONCE — the plaintext is never retrievable again after
    this response, only the Fernet-encrypted column at rest.
    """

    model_config = ConfigDict(frozen=True)

    app_api_key: str


class AppApiKeyStatusResponse(BaseModel):
    """Whether an app ``X-Api-Key`` recovery key currently exists — never the key.

    Powers the Settings → Access control's Generate-vs-Rotate/Revoke choice
    WITHOUT the break-glass reveal: ``exists`` is ``True`` once a key has been
    generated (and not since revoked), ``False`` on a fresh keyless install (setup
    mints nothing) or after a revoke. The plaintext is never serialized here.
    """

    model_config = ConfigDict(frozen=True)

    exists: bool


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
    # value, not a bad NEW value coming in over this endpoint). The upper
    # bounds on ``eviction_grace_days``/``eviction_interval_minutes``/
    # ``log_retention_days`` (issue #92) ALSO reject ``Infinity``/``NaN`` with
    # no separate ``isfinite`` validator needed: a non-finite value fails every
    # ``gt``/``ge``/``le`` comparison (``NaN`` fails all of them; ``+inf``
    # fails ``le``; ``-inf`` fails ``ge``/``gt``), so a would-be
    # ``mode="after"`` isfinite check would be unreachable dead code -- these
    # bounds ARE the finiteness guard.
    disk_pressure_threshold_percent: float | None = Field(
        default=None, ge=DISK_PRESSURE_PERCENT_MIN, le=DISK_PRESSURE_PERCENT_MAX
    )
    disk_pressure_target_percent: float | None = Field(
        default=None, ge=DISK_PRESSURE_PERCENT_MIN, le=DISK_PRESSURE_PERCENT_MAX
    )
    eviction_grace_days: int | None = Field(default=None, ge=0, le=EVICTION_GRACE_DAYS_MAX)
    eviction_enabled: bool | None = Field(default=None)
    eviction_proactive_enabled: bool | None = Field(default=None)
    eviction_interval_minutes: float | None = Field(
        default=None, gt=0, le=EVICTION_INTERVAL_MAX_MINUTES
    )
    log_retention_days: int | None = Field(default=None, ge=0, le=LOG_RETENTION_DAYS_MAX)
    # The row-count companion to log_retention_days (issue #152) — same
    # write-time bound pattern as every other bounded-count setting above.
    log_max_rows: int | None = Field(default=None, ge=0, le=LOG_MAX_ROWS_MAX)
    # Auto-grab worker (ADR-0013) — see ``SettingsResponse``. A plain boolean, no
    # bounds to enforce.
    auto_grab_enabled: bool | None = Field(default=None)
    automatic_updates_enabled: bool | None = Field(default=None)
    automatic_update_timezone: str | None = Field(default=None)
    automatic_update_weekdays: list[UpdateWeekday] | None = Field(default=None, min_length=1)
    automatic_update_window_start: str | None = Field(default=None)
    automatic_update_window_end: str | None = Field(default=None)
    automatic_update_idle_only: bool | None = Field(default=None)

    @field_validator("automatic_update_timezone")
    @classmethod
    def _validate_update_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("automatic_update_timezone must be an IANA timezone")
        try:
            ZoneInfo(normalized)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("automatic_update_timezone must be an IANA timezone") from exc
        return normalized

    @field_validator("automatic_update_weekdays")
    @classmethod
    def _canonical_update_weekdays(
        cls,
        value: list[UpdateWeekday] | None,
    ) -> list[UpdateWeekday] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("automatic_update_weekdays must not contain duplicates")
        order = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        return sorted(value, key=order.__getitem__)

    @field_validator("automatic_update_window_start", "automatic_update_window_end")
    @classmethod
    def _validate_update_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized) is None:
            raise ValueError("automatic update times must use 24-hour HH:MM format")
        return normalized

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

    @field_validator("plex_token", "prowlarr_api_key")
    @classmethod
    def _validate_header_safe_credential(cls, value: str | None) -> str | None:
        """Reject a header-unsafe credential at write time (the persistence bypass).

        The write-time twin of ``SetupCompleteRequest._validate_header_safe_credential``
        (see its docstring for the two failure modes -- a ``str(exc)`` credential
        leak and an uncaught ``UnicodeEncodeError``/500 -- that a header-unsafe
        ``plex_token`` / ``prowlarr_api_key`` triggers once an adapter sends it as a
        header). ``None`` (leave unchanged) and the ``"***"`` redaction mask are both
        header-safe and pass untouched, so the FE's mask round-trip and partial
        updates are unaffected; only a genuinely header-unsafe NEW value is rejected
        (422) before it is ever stored.
        """
        if value and header_value_error(value) is not None:
            raise ValueError(HEADER_VALUE_MESSAGE)
        return value

    @field_validator(*_LIBRARY_ROOT_FIELDS)
    @classmethod
    def _blank_root_clears_to_unset(cls, value: str | None) -> str | None:
        """Normalize a whitespace-only root to ``""`` (explicit clear-to-unset).

        Unlike ``SetupCompleteRequest``'s ``require_at_least_one_library_root``
        (a ``model_validator`` that runs on the one-shot wizard body), this
        partial-update model has no equivalent pass -- so an operator submitting
        a stray space via ``PUT /settings`` would otherwise sail straight through
        (a non-empty string is not ``None``, so it is never treated as "leave
        unchanged") and get persisted verbatim as the stored root. Mapping it to
        ``""`` instead reuses this class's OWN already-established "empty string
        = explicit clear" wire semantics (see the class docstring), so a
        whitespace-only submission behaves exactly like an intentional clear
        rather than silently configuring a bogus, effectively-blank root. A
        genuinely non-empty value (however it's padded) is returned unchanged --
        this validator only ever touches the all-whitespace case.
        """
        if value is not None and not value.strip():
            return ""
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
        update_start = self.automatic_update_window_start
        update_end = self.automatic_update_window_end
        if update_start is not None and update_end is not None and update_start == update_end:
            raise ValueError("automatic update window start and end must differ")
        return self


# --------------------------------------------------------------------------- #
# Container automatic updates (ADR-0024)
# --------------------------------------------------------------------------- #
class UpdateResultItem(BaseModel):
    """The last completed updater operation, with only bounded safe detail."""

    model_config = ConfigDict(frozen=True)

    operation: Literal["check", "install"]
    outcome: Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]
    finished_at: datetime
    from_build: str | None = None
    to_build: str | None = None
    detail_code: str | None = None


class UpdateStatusResponse(BaseModel):
    """Honest public status for the admin update controls."""

    model_config = ConfigDict(frozen=True)

    state: Literal[
        "disabled",
        "unavailable",
        "idle",
        "checking",
        "update_available",
        "waiting_for_window",
        "waiting_for_idle",
        "draining",
        "installing",
        "rollback",
        "succeeded",
        "failed",
    ]
    updater_available: bool
    current_build: str
    current_digest: str | None = None
    available_build: str | None = None
    available_digest: str | None = None
    channel: str
    next_window_start: datetime | None = None
    next_window_end: datetime | None = None
    blocker: str | None = None
    last_checked_at: datetime | None = None
    last_result: UpdateResultItem | None = None


class UpdateActionRequest(BaseModel):
    """Bodyless public/internal action guard; arbitrary target fields are forbidden."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UpdateEligibilityResponse(BaseModel):
    """Policy/action snapshot returned to the authenticated sidecar heartbeat."""

    model_config = ConfigDict(frozen=True)

    action: Literal["none", "check", "install"]
    action_generation: int = Field(ge=0)
    automatic_enabled: bool
    window_open: bool
    idle_only: bool
    blocker: str | None = None
    poll_after_seconds: int = 15


class UpdateClaimResponse(BaseModel):
    """A newly-created drain claim, or a fail-closed busy response."""

    model_config = ConfigDict(frozen=True)

    lease_token: str | None = None
    action_generation: int | None = None
    ready: bool
    lease_seconds: int
    blocker: str | None = None


class UpdateLeaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_token: str = Field(min_length=32, max_length=256)


class UpdateRenewRequest(UpdateLeaseRequest):
    """Lease renewal plus a bounded, token-owned active phase."""

    phase: Literal["installing", "rollback"] | None = None


class UpdateHeartbeatRequest(BaseModel):
    """Unleased liveness for digest checks before a drain is claimed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["checking"]
    action_generation: int = Field(ge=0)


class UpdateClaimRequest(BaseModel):
    """Recovery claims bind only to an existing action generation, never a target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recovery: bool = False
    expected_generation: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _recovery_requires_generation(self) -> UpdateClaimRequest:
        if self.recovery and self.expected_generation is None:
            raise ValueError("recovery claims require expected_generation")
        return self


class UpdateLeaseResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    lease_seconds: int
    blocker: str | None = None


class UpdateOutcomeRequest(BaseModel):
    """Sidecar acknowledgement. Targets/configuration are intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["check", "install"]
    action_generation: int = Field(ge=0)
    lease_token: str | None = Field(default=None, min_length=32, max_length=256)
    outcome: Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]
    current_digest: str | None = Field(default=None, max_length=255)
    available_digest: str | None = Field(default=None, max_length=255)
    current_build: str | None = Field(default=None, max_length=255)
    available_build: str | None = Field(default=None, max_length=255)
    from_build: str | None = Field(default=None, max_length=255)
    to_build: str | None = Field(default=None, max_length=255)
    detail_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def _install_requires_lease(self) -> UpdateOutcomeRequest:
        allowed = {
            "check": {"no_update", "update_available", "failed"},
            "install": {"succeeded", "failed", "rolled_back"},
        }
        if self.outcome not in allowed[self.operation]:
            raise ValueError(f"{self.outcome} is not a valid {self.operation} outcome")
        if self.operation == "install" and self.lease_token is None:
            raise ValueError("install outcomes require a lease_token")
        if self.operation == "check" and self.lease_token is not None:
            raise ValueError("check outcomes must not include a lease_token")
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
    subtitle: str | None = None
    items: list[DiscoverResult]


class DiscoverHomeResponse(BaseModel):
    """The composed Discover home: ordered spotlight candidates + rows."""

    model_config = ConfigDict(frozen=True)

    spotlights: list[DiscoverResult]
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
    # TV only: explicit episode targets keyed by season number. Supplying this
    # records an ``explicit_episodes`` TV intent and creates season rows for the
    # map's keys. Values are the episode numbers requested within that season.
    episodes: dict[int, list[int]] | None = None
    # Re-acquire (issue #131): force a fresh grabbable request even when the movie
    # is still reported present in Plex (its file was deleted/replaced out-of-band),
    # instead of the normal already-in-library short-circuit that returns a
    # terminal 'available' row with no grab. MOVIE ONLY -- ignored for a tv
    # request (per-season re-acquisition is the report-issue verb's job). Same
    # authZ as any create (``require_api_key``); every dedup/ownership guard still
    # applies (see ``request_service.create_request_result``).
    #
    # Modeled as an optional tri-state (``bool | None``), never omitted/None
    # meaning "normal create", NOT a bare ``bool = False`` -- mirrors ``seasons``'
    # own None-default convention. A bare boolean default emits a JSON-schema
    # ``default`` that openapi-typescript then treats as a REQUIRED client field
    # (see ``CreateRequestBody`` in the generated ``schema.d.ts``), which would
    # break every existing caller that omits this field entirely.
    force: bool | None = None


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


# Issue #205: the four wire ``status`` fields below (this one, ``RequestResponse``,
# ``QueueScope``, ``QueueItem``) are typed onto the canonical enums instead of
# plain ``str`` so OpenAPI/the generated TS client can detect a backend enum
# add/rename (see ``docs/adr/0009-frontend-typed-spa.md``). Conscious tradeoff,
# flagged rather than silently accepted: pydantic now VALIDATES a response's
# status at construction time, so a persisted value outside the enum -- corrupt
# data, a manually-edited row, or a status a since-superseded migration never
# renamed -- raises ``ValidationError`` (a 500) for the endpoint's WHOLE list,
# rather than passing the raw string through for the frontend's neutral render.
# This is deliberately NOT softened with a coerce-to-unknown escape hatch here:
# doing so would defeat the very drift detection this typing exists for, and
# every write path already only ever persists a canonical enum value (see
# ``RequestStatus``/``DownloadState``/``DownloadScopeStatus``), so the failure
# mode is a defense-in-depth backstop against out-of-band data corruption, not
# an expected runtime state. Honesty over silence: a loud 500 on a corrupt row
# is preferred over quietly masking it as some fabricated "unknown" status.
class SeasonStatus(BaseModel):
    """One tracked season's status, embedded in a tv ``RequestResponse``."""

    model_config = ConfigDict(frozen=True)

    season_number: int
    status: RequestStatus
    installed_quality_id: int | None = None
    installed_profile_index: int | None = None
    # Episode-level fallback progress (ADR-0020, issue #178): "N/M episodes"
    # while a whole-season request is being assembled from a mix of
    # pack/episode grabs. Both ``None`` for a season with no tracked
    # ``season_episode_states`` rows (a movie's season -- never happens -- or a
    # TV season the fallback has never touched, e.g. a clean single-pack import)
    # -- the UI degrades to a plain status badge in that case, never a
    # fabricated "0/0".
    imported_episode_count: int | None = None
    target_episode_count: int | None = None


class RequestResponse(BaseModel):
    """A media request as returned to the client."""

    model_config = ConfigDict(frozen=True)

    id: int
    tmdb_id: int
    media_type: str
    title: str
    status: RequestStatus  # see the wire-boundary-validation note on SeasonStatus above
    year: int | None = None
    is_anime: bool = False
    poster_url: str | None = None
    backdrop_url: str | None = None
    tv_request_mode: str | None = None
    requested_seasons: list[int] | None = None
    requested_episodes: dict[int, list[int]] | None = None
    # TV only: this show's per-season rollup, ordered by season number. ``None``
    # for a movie (movies have no ``SeasonRequest`` rows). ``status`` above is the
    # COMPUTED fold of these (``domain.season_rollup.rollup_status``).
    seasons: list[SeasonStatus] | None = None
    # Passive projection of the reconciler-refreshed physical download row.
    # Present only while this request reads ``downloading`` and exactly one active
    # physical download maps to it. ``None`` means absent or ambiguous (for
    # example, concurrent TV-season downloads); ``0.0`` is a real known value.
    download_progress: float | None = None
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

    When ``request_id`` is set, ``tmdb_id``/``media_type``/``title``/``year`` are
    resolved from the stored request and any values passed for them are ignored;
    ``season`` comes from this body. ``episodes`` also comes from this body when
    present; when omitted, a stored ``explicit_episodes`` request supplies the
    selected season's episode target. Otherwise (no ``request_id``) ``tmdb_id``,
    ``media_type`` and ``title`` are required.

    Every TV preview is per-season: the endpoint REJECTS (422) a tv media type
    previewed with no ``season``, and REJECTS (422) a non-tv (movie) media type
    previewed WITH a ``season`` or ``episodes`` -- mirroring the grab endpoint's
    scope guard exactly, so an invalid combination never reaches the indexer.
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

    @field_validator("episodes")
    @classmethod
    def _normalize_empty_episodes(cls, value: list[int] | None) -> list[int] | None:
        """Coerce ``[]`` to ``None`` (issue #102): both mean "whole season", but
        ``None`` is the canonical whole-season scope value. Normalizing here -- at
        the schema boundary -- means an unnormalized ``[]`` can never reach a
        stored ``episodes_json`` or a reuse/import scope check in the first place.
        """
        return value or None


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
    covered_seasons: tuple[int, ...] = ()
    target_seasons: tuple[int, ...] = ()
    upgrade_seasons: tuple[int, ...] = ()
    waste_seasons: tuple[int, ...] = ()
    ignored_seasons: tuple[int, ...] = ()
    skipped_seasons: tuple[int, ...] = ()


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
class QueueScope(BaseModel):
    """One logical TV scope attached to a queue download."""

    model_config = ConfigDict(frozen=True)

    media_request_id: int | None = None
    season: int | None = None
    episodes: list[int] | None = None
    status: DownloadScopeStatus = DownloadScopeStatus.active  # see SeasonStatus's note above


def _empty_queue_scopes() -> list[QueueScope]:
    return []


class QueueItem(BaseModel):
    """A tracked download in the live queue."""

    model_config = ConfigDict(frozen=True)

    id: int
    torrent_hash: str
    status: DownloadState  # see SeasonStatus's wire-boundary note above
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
    # Human-legible identity (issue #134): all three degrade honestly to
    # ``None`` rather than swallowing a missing join/backfill -- an orphaned
    # download (its request deleted), a pre-migration row with no backfillable
    # history, or a hashless/legacy row all still render, just without these.
    # ``title``/``poster_url`` come from the owning ``MediaRequest`` (``None``
    # for an orphan); ``release_title`` is the grab decision's own release name
    # persisted at grab time, independent of the request link.
    title: str | None = None
    poster_url: str | None = None
    release_title: str | None = None
    scopes: list[QueueScope] = Field(default_factory=_empty_queue_scopes)


class QueueResponse(BaseModel):
    """The reconciled download queue."""

    model_config = ConfigDict(frozen=True)

    queue: list[QueueItem]


class GrabRequest(BaseModel):
    """Grab a release for a request: a chosen ``info_hash``/``guid`` or the top pick.

    With neither ``info_hash`` nor ``guid`` set, the highest-ranked accepted
    release is grabbed ("grab top"). For a TV request, ``season`` scopes both the
    indexer search and the stored download to that season; ``episodes`` further
    scopes it to those specific episode number(s). When omitted, stored
    ``explicit_episodes`` request intent supplies the selected season's target;
    explicit ``None``/empty = the whole season. Every TV grab is per-season: the
    endpoint REJECTS (422) a tv request grabbed with no ``season``, and REJECTS
    (422) a non-tv (movie) request grabbed WITH a ``season`` -- the branch is
    always the request's actual media type, never merely whether ``season``
    happens to be set.
    """

    model_config = ConfigDict(frozen=True)

    request_id: int
    info_hash: str | None = None
    guid: str | None = None
    season: int | None = None
    episodes: list[int] | None = None

    @field_validator("episodes")
    @classmethod
    def _normalize_empty_episodes(cls, value: list[int] | None) -> list[int] | None:
        """Coerce ``[]`` to ``None`` (issue #102): both mean "whole season", but
        ``None`` is the canonical whole-season scope value. Normalizing here -- at
        the schema boundary -- means an unnormalized ``[]`` can never reach a
        stored ``episodes_json`` or a reuse/import scope check in the first place.
        """
        return value or None


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
    reports it. ``not_configured`` is honest -- never confused with ``down``.

    ``note`` (qBittorrent only, issues #133/#157) is a NON-blocking, informational
    signal -- e.g. the client's default save path isn't visible inside this
    container -- distinct from ``detail`` (which carries only FAILURE diagnostics,
    ``None`` whenever ``status == "ok"``). ``None`` for every other subsystem."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: Literal["ok", "degraded", "down", "not_configured"]
    detail: str | None = None
    note: str | None = None
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
