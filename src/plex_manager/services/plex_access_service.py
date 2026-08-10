"""The plex.tv share-verdict ladder: does a stored token still govern the
configured Plex server, and via which failure mode does it not.

Extracted from ``watchlist_service.revalidate_sync_user`` (issue #391 PR-1):
that function applies plex.tv's ``/resources`` + ``account_server_resource``
test to decide whether a stored ``User.encrypted_plex_token`` still authorizes
the Universal Watchlist sync. The exact same test is the foundation the
auth-revalidation design (#391, #484) builds its idle-session sweep and
section-entitlement capture on, so the ladder now lives here as the single
source of truth. ``watchlist_service.revalidate_sync_user`` becomes a thin
delegate onto :func:`check_share` (see that function for the mapping) so its
name, signature, and every existing caller/test are unchanged.

Today's ``revalidate_sync_user`` collapses two very different failure modes
into one ``STALE`` outcome: a token plex.tv outright rejects (dead credential,
share status unknown) and a token plex.tv accepts but which no longer reaches
the configured server (share confirmed revoked). Watchlist sync only ever
needed "skip and clear the snapshot either way", so the collapse was harmless
there. It is NOT harmless for session revalidation (#391) or entitlement
capture (#484): a rejected token might still have a live share behind a
not-yet-refreshed credential, while a share genuinely revoked is a stronger,
actionable signal. :class:`ShareVerdict` keeps them apart as ``TOKEN_STALE``
and ``SHARE_REVOKED``.

This module owns policy only -- no web imports. ``deps``/routers/``app`` depend
on this module; it never depends on them, mirroring the discipline in
``session_lifecycle.py``. Stage 2 of the design (#391 PR-2) adds the periodic
revalidation sweep (:func:`sweep_shares`) that turns a verdict into persisted
state and, on a confirmed loss, into a sign-out; ``web/app.py`` owns only the
task that ticks it and the realtime-stream close that the web layer alone can
perform.

Stage 3 (#484 PR-3) adds section-entitlement CAPTURE (:func:`capture_entitlements`,
:func:`store_entitlements`): which library sections a given token can actually
see, persisted alongside the anchor it was read against. Capture only -- nothing
reads those columns yet. That staging is deliberate: the design rests on one
unverified premise (that a shared user's token authenticates against the
configured ``plex_url`` and returns SECTION-FILTERED results), and it is observed
on the canary through this module's capture telemetry before PR-4/PR-5 gate
anything on it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import CursorResult, String, case, exists, func, or_, select, type_coerce, update

from plex_manager.adapters.plex.library import PlexAuthError
from plex_manager.adapters.plex.oauth import (
    CODE_TOKEN_INVALID,
    PlexTvClient,
    PlexVerifyError,
    account_server_resource,
)
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import AuthSession, Setting, User
from plex_manager.services import audit_service, session_lifecycle

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

    from plex_manager.ports.library import LibraryPort

__all__ = [
    "SHARE_SWEEP_TICK_SECONDS",
    "SHARE_SWEEP_USER_BUDGET",
    "AnchorCheck",
    "CaptureUnavailableReason",
    "DueShareCandidate",
    "EntitlementCapture",
    "EntitlementCaptureContext",
    "EntitlementSnapshot",
    "ShareSweepResult",
    "ShareSweepStatus",
    "ShareVerdict",
    "ShareVerdictOutcome",
    "apply_share_verdict",
    "capture_entitlements",
    "check_share",
    "count_due_share_checks",
    "list_due_share_checks",
    "record_failed_attempt",
    "store_entitlements",
    "sweep_shares",
]

_logger = logging.getLogger(__name__)

# How often ``web/app.py``'s ``_share_sweep_loop`` wakes. Deliberately SHORTER
# than the per-user revalidation interval (``share_revalidation_interval_hours``,
# default 6h): the tick is the polling granularity, the interval is the policy.
# A 15-minute wake means a user whose 6h is up waits at most 15 more minutes,
# while the per-tick budget below keeps the plex.tv load an order of magnitude
# BELOW the watchlist worker's (which already runs the same ``fetch_resources``
# probe for every token-bearing user every 15 minutes and throws the verdict
# away).
SHARE_SWEEP_TICK_SECONDS: float = 900.0

# The most users one tick will revalidate. Strictly sequential (never gathered):
# 20 serial plex.tv calls is a trickle, and a burst of parallel calls against
# plex.tv from a self-hosted install is exactly what ADR-0016 keeps off the
# request path. A backlog beyond this budget is not dropped -- it is reported as
# ``ShareSweepStatus.due_remaining`` and drains over subsequent ticks.
SHARE_SWEEP_USER_BUDGET: int = 20

# The "never attempted" floor for the due-queue ordering (see
# ``_last_attempt_at``). Any real attempt timestamp is later than this, so a user
# with neither a check nor a failure recorded sorts ahead of everyone who has
# been tried -- backend-independently, without relying on either dialect's
# default NULL collation.
_NEVER_ATTEMPTED = datetime(1970, 1, 1, tzinfo=UTC)


class ShareVerdict(Enum):
    """The outcome of re-confirming one stored token against plex.tv.

    Mirrors the sign-in authorization ladder (``auth._post_init_access``) and
    the watchlist revalidation it was copied from: a share is live iff plex.tv
    still advertises the configured server as a resource that account can
    reach. Split into five distinct outcomes (rather than watchlist's
    collapsed three) so a caller can act on token death and share loss
    differently, and can never mistake a transient plex.tv hiccup for either.
    """

    AUTHORIZED = "authorized"
    """plex.tv confirms the account still reaches the configured server."""

    SHARE_REVOKED = "share_revoked"
    """plex.tv accepted the token and answered with a resource list, but that
    list no longer includes the configured server -- the share is confirmed
    gone. Stronger than :attr:`TOKEN_STALE`: this is a successful, authoritative
    answer, not a rejected credential."""

    TOKEN_STALE = "token_stale"  # noqa: S105 - verdict label, not a secret
    """plex.tv rejected the token outright (401/403, e.g.
    :data:`~plex_manager.adapters.plex.oauth.CODE_TOKEN_INVALID`). The
    credential is dead, but -- unlike :attr:`SHARE_REVOKED` -- plex.tv never
    got far enough to say whether the share itself is still live. A caller
    that needs to know share status specifically must not treat this the same
    as a confirmed revoke."""

    UNKNOWN = "unknown"
    """A transient plex.tv failure (unreachable, malformed/non-array response,
    unexpected status other than 401/403): the ladder could not be evaluated
    at all. Callers MUST NOT act on this as if it were loss -- retain whatever
    was previously known and try again later (north star #3: a plex.tv outage
    is never read as a revoked account)."""

    UNVERIFIABLE = "unverifiable"
    """There is no stored token to check (e.g. ``User.encrypted_plex_token`` is
    ``None``). Distinct from :attr:`UNKNOWN`: this is not a failed check, it is
    the absence of anything to check."""


@dataclass(frozen=True)
class EntitlementSnapshot:
    """One point-in-time read of a share's verdict (and, later, its section
    scope) against the configured Plex server.

    ``section_keys`` is tri-state on purpose: ``None`` means library sections
    were not captured this pass (the only value :func:`check_share` produces
    today -- section capture is #484 scope, not wired up in this PR), while an
    empty tuple would mean a pass that captured sections and found the account
    entitled to none. Collapsing "never captured" and "captured, entitled to
    nothing" would make a not-yet-implemented capture indistinguishable from a
    fully section-restricted share.
    """

    verdict: ShareVerdict
    section_keys: tuple[str, ...] | None
    machine_identifier: str


async def check_share(
    plex_tv: PlexTvClient,
    machine_identifier: str,
    *,
    token: str | None,
) -> EntitlementSnapshot:
    """Re-confirm one stored token against the configured Plex server.

    Applies the same plex.tv ``/resources`` + ``account_server_resource`` test
    as ``watchlist_service.revalidate_sync_user`` (see that function and this
    module's docstring for why the two diverge on token-rejection vs.
    share-loss). ``token=None`` short-circuits to
    :attr:`ShareVerdict.UNVERIFIABLE` without a network call, so a caller
    holding a ``User`` row can pass ``user.encrypted_plex_token`` straight
    through.

    Section scope is never captured here (:attr:`EntitlementSnapshot.
    section_keys` is always ``None``) -- that capture is #484 scope, added in a
    later PR of this design.
    """
    if token is None:
        return EntitlementSnapshot(
            verdict=ShareVerdict.UNVERIFIABLE,
            section_keys=None,
            machine_identifier=machine_identifier,
        )
    try:
        resources = await plex_tv.fetch_resources(token)
    except PlexVerifyError as exc:
        # A token plex.tv rejects outright (401/403) never got far enough to
        # answer whether the share is live -- TOKEN_STALE, not SHARE_REVOKED.
        # Keyed off the oauth adapter's shared error code so the coupling is
        # compile-time, not a hand-copied literal (mirrors watchlist_service).
        verdict = (
            ShareVerdict.TOKEN_STALE if exc.code == CODE_TOKEN_INVALID else ShareVerdict.UNKNOWN
        )
        return EntitlementSnapshot(
            verdict=verdict, section_keys=None, machine_identifier=machine_identifier
        )
    if account_server_resource(resources, machine_identifier) is None:
        # plex.tv answered authoritatively and the configured server is not in
        # the list: the share is confirmed gone, not merely unauthenticated.
        return EntitlementSnapshot(
            verdict=ShareVerdict.SHARE_REVOKED,
            section_keys=None,
            machine_identifier=machine_identifier,
        )
    return EntitlementSnapshot(
        verdict=ShareVerdict.AUTHORIZED, section_keys=None, machine_identifier=machine_identifier
    )


# --------------------------------------------------------------------------- #
# Periodic revalidation sweep (issue #391 PR-2)
# --------------------------------------------------------------------------- #
class AnchorCheck(Enum):
    """Whether the server the verdicts were computed against is still THE server.

    Every verdict in a tick is judged against one ``machineIdentifier`` -- and on
    an install that has the identifier cached at setup, that anchor is read from
    the settings row without ever asking the server whether it still holds. If
    the Plex server is rebuilt or re-claimed it comes back with a NEW identifier,
    at which point plex.tv truthfully reports that nobody's account reaches the
    OLD one and every single user's verdict is :attr:`ShareVerdict.SHARE_REVOKED`
    -- a total, self-inflicted sign-out of an install whose only web-operable
    repair (an admin repointing via ``PUT /settings``) needs a live admin
    session. So a confirmed revoke is acted on only behind a LIVE re-read of the
    anchor.
    """

    CONFIRMED = "confirmed"
    """A live ``/identity`` probe returned the same identifier the verdicts used."""

    MISMATCHED = "mismatched"
    """The configured server answered with a DIFFERENT identifier: the anchor is
    stale (rebuilt/re-claimed/repointed server), so this tick's share-loss
    verdicts describe the old server and mean nothing about today's."""

    UNCONFIRMED = "unconfirmed"
    """The anchor could not be re-read at all (probe failed, or no
    url/token to probe with). Not evidence of a mismatch -- and equally not the
    confirmation a mass sign-out requires, so it blocks the same way."""


# Why a tick was handed no entitlement-capture context at all (#484 PR-3). The
# composition root owns the gate -- only it knows whether Plex is configured and
# whether an operator-verified server anchor exists -- but a gate nobody reports
# is a silent disable: ``captured``/``capture_failed``/``capture_skipped`` would
# all sit at zero on an ``ok`` sweep forever. Carried as a REASON rather than a
# flag for the same "which fact" discipline as ``AnchorCheck``: "Plex is not
# configured" and "configured, but no verified anchor to stamp against" have
# different remedies.
CaptureUnavailableReason = Literal["not_configured", "no_server_anchor"]


@dataclass(frozen=True)
class ShareVerdictOutcome:
    """What :func:`apply_share_verdict` actually did for one user.

    ``applied`` is ``False`` only for the mid-sweep re-sign-in guard: the stored
    token changed between due-selection and this write, so the verdict describes
    a credential the user no longer holds and NOTHING was written. Never a
    failure -- the next tick re-checks the new token.

    ``admin_exempt`` records that a share-loss verdict was stamped for an
    owner/admin but NOT acted on (see :func:`apply_share_verdict`).
    """

    applied: bool
    signed_out: bool
    sessions_revoked: int
    admin_exempt: bool = False


@dataclass(frozen=True)
class ShareSweepResult:
    """One tick's tally, the input to :meth:`ShareSweepStatus.mark_completed`.

    ``signed_out_user_ids`` is reported for telemetry, but closing those users'
    realtime streams is NOT deferred to the end of the tick: see the
    ``on_signed_out`` parameter of :func:`sweep_shares`.
    """

    checked: int = 0
    authorized: int = 0
    share_revoked: int = 0
    token_stale: int = 0
    unknown: int = 0
    unverifiable: int = 0
    skipped: int = 0
    admins_exempted: int = 0
    anchor_deferred: int = 0
    captured: int = 0
    capture_failed: int = 0
    capture_skipped: int = 0
    capture_unavailable: CaptureUnavailableReason | None = None
    """Set when the tick had NO capture context, which is why every one of its
    AUTHORIZED users landed in ``capture_skipped``. ``None`` means capture was
    wired up (whether or not it captured anything)."""
    anchor_state: AnchorCheck | None = None
    """WHICH anchor answer caused ``anchor_deferred``, or ``None`` if the anchor
    was never consulted. Carried separately from the count because "the server
    reported a different identifier" and "we could not reach the server to ask"
    are different facts, and only the first one establishes that the server
    changed -- collapsing them would let a plain outage render as a confirmed
    identity change (the same conflation ``probe_failed`` vs ``not_configured``
    exists to prevent, issue #327)."""
    sessions_revoked: int = 0
    due_remaining: int = 0
    signed_out_user_ids: tuple[int, ...] = ()
    last_error_type: str | None = None


@dataclass
class ShareSweepStatus:
    """Operator-facing health of the share-revalidation sweep.

    Modeled on ``watchlist_service.WatchlistWorkerStatus`` (same state ladder,
    same "a tick that could not do its job never claims ok" discipline), because
    the two workers fail in the same ways: the configured server can be absent
    (``not_configured``), present but unreachable (``probe_failed``), or the tick
    can die outright (``error``).

    The honesty rule this type exists to enforce (north star #3): a transient
    plex.tv failure is a DEGRADED sweep with a visible ``unknown`` count -- never
    a revocation, and never a silent ``ok``. An operator reading /health must be
    able to tell "nobody lost access" from "we could not tell whether anybody
    lost access".
    """

    state: Literal[
        "starting",
        "ok",
        "degraded",
        "anchor_mismatch",
        "anchor_unconfirmed",
        "not_configured",
        "probe_failed",
        "error",
    ] = field(default="starting")
    last_run_at: datetime | None = field(default=None)
    last_ok_at: datetime | None = field(default=None)
    last_error_type: str | None = field(default=None)
    last_error_at: datetime | None = field(default=None)
    checked: int = field(default=0)
    authorized: int = field(default=0)
    share_revoked: int = field(default=0)
    token_stale: int = field(default=0)
    unknown: int = field(default=0)
    unverifiable: int = field(default=0)
    skipped: int = field(default=0)
    """Candidates the tick wrote nothing for: their stored token changed since
    selection (a mid-sweep re-sign-in), or the row was deleted. Surfaced rather
    than folded into ``checked`` so a tick whose budget went mostly to skips is
    visible instead of looking like a smaller tick."""
    admins_exempted: int = field(default=0)
    """Owner/admin users whose share-loss verdict was recorded but deliberately
    NOT acted on. Never zero-and-silent: if the sweep is declining to sign an
    admin out, the operator must be able to see that it happened and go look at
    the audit row."""
    anchor_deferred: int = field(default=0)
    """Share-loss verdicts this tick refused to act on because the server anchor
    could not be confirmed live (see :class:`AnchorCheck`). These users were left
    due, so nothing is lost -- but a persistently non-zero value means the sweep
    is not actually protecting anything and needs the operator's attention. Read
    it together with ``state``, which says WHY: ``anchor_mismatch`` (the server
    reported a different identifier -- an established fact needing a repoint) or
    ``anchor_unconfirmed`` (we could not ask -- possibly just an outage)."""
    captured: int = field(default=0)
    """AUTHORIZED users whose section entitlements were read and persisted this
    tick (#484 PR-3). Capture-only for now -- nothing reads those columns yet --
    so this is the operator's (and the canary's) evidence that capture is
    actually running, not a signal that anything is being enforced."""
    capture_skipped: int = field(default=0)
    """AUTHORIZED users this tick did not even ATTEMPT to capture: the circuit
    broke after an unreachable server, the tick could not confirm the server
    anchor, or capture is switched off for this install entirely (see
    ``capture_unavailable``). Distinct from ``capture_failed`` (which we DID
    try): conflating the two would hide how much of a tick a dead server cost.

    NOT retried within the tick and NOT rescheduled: a skipped user is
    recaptured at their next due window (bounded by
    ``share_revalidation_interval_hours``, default 6h) or at their next sign-in,
    whichever comes first. That staleness is acceptable *today* precisely because
    nothing reads these columns -- #484 PR-3 is capture-only. PR-5, which turns
    them into an enforcement gate, must revisit this: if canary evidence shows
    the lag matters, retry state belongs there, with the enforcement semantics
    that justify it, not bolted onto a stage that enforces nothing."""
    capture_failed: int = field(default=0)
    """Captures attempted and not persisted: the server refused this user's
    token, was unreachable, the write failed, or the anchor moved under it.
    Surfaced separately from ``captured`` because a capture failure is
    deliberately harmless -- the previous snapshot survives and the verdict is
    unaffected -- and would otherwise be completely invisible."""
    capture_unavailable: CaptureUnavailableReason | None = field(default=None)
    """Why capture is switched OFF for this install, or ``None`` when it is
    wired up. Without it, an upgraded install that has ``plex_url`` and
    ``plex_token`` but no operator-verified ``plex_machine_identifier`` would
    report an ``ok`` sweep with authorized users and three zeros forever --
    indistinguishable from "capture ran and there was nothing to do". The count
    alone cannot say WHY, and the remedy (finish setup, or re-save the Plex
    settings so the verified anchor is recorded) is not guessable from it."""
    signed_out: int = field(default=0)
    """Users this tick actually signed out. NOT ``share_revoked + token_stale``:
    an admin-exempted share loss counts toward those verdict tallies but nobody
    was cut, so deriving the figure would overstate it (Codex review of PR #557).
    The sweep is the only thing that knows which verdicts became sign-outs, so it
    reports the number rather than leaving a consumer to infer it."""
    sessions_revoked: int = field(default=0)
    due_remaining: int = field(default=0)
    """Users still due a revalidation once this tick finished. A persistently
    non-zero value is the operator's signal that the backlog drains slower than
    it accumulates (i.e. the effective interval is longer than the configured
    one), which would otherwise be invisible."""

    def _reset_counters(self) -> None:
        self.checked = self.authorized = self.share_revoked = self.token_stale = 0
        self.unknown = self.unverifiable = self.sessions_revoked = self.due_remaining = 0
        self.skipped = self.admins_exempted = self.anchor_deferred = self.signed_out = 0
        self.captured = self.capture_failed = self.capture_skipped = 0
        self.capture_unavailable = None

    def mark_started(self) -> None:
        self.last_run_at = datetime.now(UTC)

    def mark_skipped(self, state: Literal["not_configured"]) -> None:
        """Record a tick that intentionally did no work (nothing to check against)."""
        self.state = state
        self.last_error_type = None
        self.last_error_at = None
        self._reset_counters()

    def mark_probe_failed(self, exc: PlexVerifyError) -> None:
        """The Plex server IS configured but its identity probe failed.

        Distinct from ``not_configured`` (an absence) and from ``error`` (an
        exception that escaped the tick): a known, actionable outage that
        self-heals on the next successful probe -- and one that revoked nobody.
        """
        self.state = "probe_failed"
        self.last_error_type = type(exc).__name__
        self.last_error_at = datetime.now(UTC)
        self._reset_counters()

    def mark_completed(self, result: ShareSweepResult) -> None:
        # Both anchor states outrank plain ``degraded`` -- the sweep enforced
        # nothing, which the operator has to know -- but they are NOT the same
        # claim and must never be collapsed: ``anchor_mismatch`` asserts the
        # server answered with a DIFFERENT identifier (established fact, needs a
        # repoint), while ``anchor_unconfirmed`` says only that we could not ask
        # (an outage, or missing credentials). Reporting a mismatch we never
        # established would send an operator hunting a configuration change that
        # never happened.
        #
        # UNKNOWN verdicts degrade the tick exactly as watchlist's skipped users
        # do: the sweep ran but could not answer for someone, so it has NOT
        # succeeded and must not advance ``last_ok_at``. Confirmed revocations
        # and stale tokens do NOT degrade it -- those are the sweep working.
        if result.anchor_deferred:
            self.state = (
                "anchor_mismatch"
                if result.anchor_state is AnchorCheck.MISMATCHED
                else "anchor_unconfirmed"
            )
        elif result.unknown or result.last_error_type:
            self.state = "degraded"
        else:
            self.state = "ok"
        if self.state == "ok":
            self.last_ok_at = datetime.now(UTC)
        self.checked = result.checked
        self.authorized = result.authorized
        self.share_revoked = result.share_revoked
        self.token_stale = result.token_stale
        self.unknown = result.unknown
        self.unverifiable = result.unverifiable
        self.skipped = result.skipped
        self.admins_exempted = result.admins_exempted
        self.anchor_deferred = result.anchor_deferred
        self.captured = result.captured
        self.capture_failed = result.capture_failed
        self.capture_skipped = result.capture_skipped
        self.capture_unavailable = result.capture_unavailable
        self.signed_out = len(result.signed_out_user_ids)
        self.sessions_revoked = result.sessions_revoked
        self.due_remaining = result.due_remaining
        self.last_error_type = result.last_error_type
        self.last_error_at = datetime.now(UTC) if result.last_error_type is not None else None

    def mark_error(self, exc: BaseException) -> None:
        self.state = "error"
        self.last_error_type = type(exc).__name__
        self.last_error_at = datetime.now(UTC)
        self._reset_counters()


def _due_predicate(now: datetime, revalidate_after: timedelta) -> ColumnElement[bool]:
    """Users whose share verdict has never been computed, or has aged out."""
    cutoff = now - revalidate_after
    return or_(User.share_checked_at.is_(None), User.share_checked_at < cutoff)


def _holds_live_session(now: datetime) -> ColumnElement[bool]:
    """EXISTS a still-usable browser session for this user.

    The predicate is literally the one the admin sessions list uses --
    :func:`session_lifecycle.active_session_conditions`, shared so the two can
    never drift (the sweep must never revoke someone the operator's session list
    does not show as signed in). Scoping the sweep to it keeps the plex.tv load
    proportional to who is actually SIGNED IN rather than to every account that
    ever signed in -- and revoking the sessions of a user who holds none would be
    a no-op anyway. Recovery sessions (``user_id`` NULL, no Plex identity) can
    never satisfy the join, so the break-glass credential is structurally out of
    this sweep's reach.
    """
    return (
        select(AuthSession.id)
        .where(
            AuthSession.user_id == User.id,
            *session_lifecycle.active_session_conditions(now),
        )
        .exists()
    )


def _last_attempt_at() -> ColumnElement[datetime]:
    """When this user was last ATTEMPTED, successfully or not.

    ``GREATEST(share_checked_at, share_check_failed_at)`` with ``NULL`` folded to
    the epoch, spelled as a portable ``CASE`` because ``GREATEST`` does not exist
    on SQLite and ``max(a, b)`` is a two-argument scalar there but an AGGREGATE on
    PostgreSQL -- the one spelling that means the same thing on both backends.

    Ordering on last ATTEMPT rather than last SUCCESS is what stops the sweep
    from starving (Codex review of PR #557). ``share_checked_at`` alone was the
    primary key, and an UNKNOWN verdict deliberately never advances it, so during
    a partial plex.tv outage the same oldest-checked cohort was re-selected every
    tick, forever, and the ``share_check_failed_at`` tiebreak could not help --
    it only applies to EXACT ``share_checked_at`` ties, which established accounts
    with distinct timestamps never have. A genuinely-revoked user sitting behind
    that cohort would then keep their access indefinitely, defeating the whole
    point of the sweep. Keying on the attempt means a failed check rotates a user
    to the BACK of the queue while leaving them due, so the backlog always drains.
    """
    checked = func.coalesce(User.share_checked_at, _NEVER_ATTEMPTED)
    failed = func.coalesce(User.share_check_failed_at, _NEVER_ATTEMPTED)
    return case((checked > failed, checked), else_=failed)


@dataclass(frozen=True)
class DueShareCandidate:
    """One due user, with the credential identity read in the SAME statement.

    ``token_ciphertext`` is the raw stored ciphertext of ``token`` as it was at
    selection -- captured here, not re-read later, so any snapshot produced from
    ``token`` can prove at write time that it describes the credential still on
    file. Reading the two in one statement is the point: a re-read after the
    verdict could pick up a token a concurrent sign-in rotated in, which is
    exactly the mismatch the write guard exists to catch.
    """

    user_id: int
    token: str | None
    token_ciphertext: str | None


async def _select_due_candidates(
    session: AsyncSession,
    *,
    now: datetime,
    revalidate_after: timedelta,
    limit: int,
) -> list[tuple[User, str | None]]:
    """The due-selection statement: each user with its raw token ciphertext.

    ONE statement, so the decrypted token and the ciphertext that identifies it
    can never come from different row versions.
    """
    stmt = (
        select(User, _stored_token_ciphertext().label("token_ciphertext"))
        .where(_holds_live_session(now), _due_predicate(now, revalidate_after))
        .order_by(_last_attempt_at().asc(), User.id)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def list_due_share_checks(
    session: AsyncSession,
    *,
    now: datetime,
    revalidate_after: timedelta,
    limit: int,
) -> list[User]:
    """The next (at most ``limit``) signed-in users due a share revalidation.

    Ordered oldest-ATTEMPT-first (:func:`_last_attempt_at`), so a user whose
    check just came back UNKNOWN goes to the back of the queue while staying due,
    and the whole backlog is covered within ``ceil(due / limit)`` ticks no matter
    how many of those checks fail. A user who has never been attempted at all has
    both timestamps ``NULL``, which folds to the epoch and sorts them first --
    the NULLS-FIRST semantics the previous explicit ``nulls_first()`` provided,
    now backend-independent rather than dialect-dependent. ``id`` is the final
    tiebreak so the order is total and the rotation deterministic.
    """
    return [
        user
        for user, _ciphertext in await _select_due_candidates(
            session, now=now, revalidate_after=revalidate_after, limit=limit
        )
    ]


async def count_due_share_checks(
    session: AsyncSession, *, now: datetime, revalidate_after: timedelta
) -> int:
    """How many signed-in users are due a revalidation right now (backlog size)."""
    stmt = (
        select(func.count())
        .select_from(User)
        .where(_holds_live_session(now), _due_predicate(now, revalidate_after))
    )
    return (await session.execute(stmt)).scalar_one()


def _stored_token_ciphertext() -> ColumnElement[str]:
    """``users.encrypted_plex_token`` as the RAW ciphertext actually on disk.

    ``EncryptedStr`` encrypts on bind and decrypts on result, and Fernet is
    non-deterministic (fresh IV + timestamp per call), so encrypting the same
    plaintext twice yields different ciphertext. That makes the obvious
    ``WHERE encrypted_plex_token = :plaintext`` guard impossible: the bound value
    would be a brand-new ciphertext that can never equal the stored one.
    ``type_coerce`` to plain ``String`` opts out of BOTH halves of that
    processing, so the column reads as the stored ciphertext and compares against
    a ciphertext we captured earlier -- a byte-for-byte "is this still the exact
    row I read?" test, which is what the guard actually needs.

    Conservative by construction: a re-sign-in that stores the SAME plaintext
    still writes different ciphertext, so the guard treats it as changed and
    declines to revoke. Erring toward keeping a session for one more interval is
    the right direction (ADR-0005), and the next sweep re-evaluates.
    """
    return type_coerce(User.encrypted_plex_token, String)


async def record_failed_attempt(session: AsyncSession, user_id: int, *, now: datetime) -> None:
    """Record that a revalidation was ATTEMPTED for this user and did not land.

    The same two columns an ``UNKNOWN`` verdict writes, for the case that never
    produced a verdict at all: an unexpected exception out of
    :func:`check_share`. Without this the crashing user's ``share_check_failed_at``
    never moves, so :func:`_last_attempt_at` keeps them at the front of the due
    queue and the same failing cohort is re-selected every tick forever --
    exactly the starvation the last-attempt ordering exists to prevent, reached
    through the defensive branch instead of the UNKNOWN one (Codex round 2 on
    PR #557).

    An in-place ``UPDATE`` (not a loaded instance) so it needs no prior read and
    cannot clobber a concurrent write to any other column. Does not commit.
    """
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            share_check_failures=User.share_check_failures + 1,
            share_check_failed_at=now,
        )
    )


# --------------------------------------------------------------------------- #
# Section-entitlement capture (issue #484 PR-3) -- capture only, no enforcement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EntitlementCapture:
    """One point-in-time read of which library sections a token can actually see.

    ``section_keys`` is the honest answer for THIS token, including the empty
    tuple: a share with access to zero sections captures ``()``, which persists
    as ``[]`` and is emphatically not the ``NULL`` "never captured" state (see
    ``User.entitled_section_keys``). ``machine_identifier`` is the anchor the
    capture ran against, so a later reader can tell a snapshot taken against
    today's server from one left behind by a repoint.
    """

    section_keys: tuple[str, ...]
    machine_identifier: str


@dataclass(frozen=True)
class EntitlementCaptureContext:
    """What the web layer lends the sweep so it can capture entitlements.

    The service must not know how to BUILD a Plex client (that is the adapter's
    job and the composition root's choice), so it is handed two callables
    instead:

    * ``library_for_token`` -- a :class:`LibraryPort` bound to the configured
      server with one user's token, or ``None`` when Plex is not configured
      enough to try.
    * ``owner_section_count`` -- how many sections the OWNER (service) token
      sees, awaited AT MOST ONCE per sweep and only if something was actually
      captured. It is the baseline that turns a raw count into the answer this
      whole staged PR exists to observe on the canary: is a shared user's view
      actually FILTERED, or does their token see the entire library?
    """

    library_for_token: Callable[[str], LibraryPort | None]
    owner_section_count: Callable[[], Awaitable[int | None]]
    anchor_setting_key: str
    """The settings key holding the configured server's machine identifier, so
    :func:`store_entitlements` can re-read the anchor inside its own write. The
    service deliberately does not know the key's name -- that is web config."""


def _capture_scope(section_count: int, owner_section_count: int | None) -> str:
    """The one-word telemetry verdict for "is filtering actually happening?"."""
    if owner_section_count is None:
        return "baseline_unknown"
    if section_count < owner_section_count:
        return "filtered"
    if section_count > owner_section_count:
        # Not expected: a shared user seeing MORE than the owner token would
        # mean the baseline is not what we think it is. Named rather than
        # rounded into "full" so the canary surfaces it instead of hiding it.
        return "wider_than_owner"
    return "full"


@dataclass(frozen=True)
class _CaptureAttempt:
    """One capture attempt, plus whether the SERVER (not the token) looked down.

    The split is what lets the sweep break its circuit after one bad call:
    ``PlexAuthError`` is per-USER (this token is refused; the next user's may be
    fine), while anything else -- unreachable, timeout, 5xx, junk response -- says
    the server is not answering, and the remaining users in the tick would each
    pay another full client timeout to learn the same thing.
    """

    capture: EntitlementCapture | None
    server_unavailable: bool


async def _attempt_capture(
    library: LibraryPort,
    *,
    machine_identifier: str,
    user_id: int,
    owner_section_count: Callable[[], Awaitable[int | None]] | None = None,
) -> _CaptureAttempt:
    """Read one token's sections. ``owner_section_count`` is resolved ONLY after
    a successful read, so a dead server costs one timeout rather than two and
    never delays the tick behind a baseline nobody will compare against."""
    try:
        # Never cached: an entitlement snapshot is point-in-time AUTHORIZATION
        # data, and a cached success would both stamp a stale section set and
        # mask an unreachable server -- mis-counting it as ``captured``.
        sections = await library.list_sections(use_cache=False)
    except PlexAuthError:
        # This user's token is not accepted by the server -- an ordinary,
        # expected canary outcome for the premise under test, and specific to
        # them. Never trips the circuit breaker.
        _logger.warning(
            "entitlement capture failed for user_id=%s (PlexAuthError); "
            "retaining the previous snapshot",
            safe_int(user_id),
        )
        return _CaptureAttempt(capture=None, server_unavailable=False)
    except Exception as exc:
        # Unreachable / timed out / bad response: the SERVER, not this token.
        _logger.warning(
            "entitlement capture failed for user_id=%s (%s); retaining the previous snapshot",
            safe_int(user_id),
            type(exc).__name__,
        )
        return _CaptureAttempt(capture=None, server_unavailable=True)
    section_keys = tuple(section.key for section in sections)
    # Resolved only now, on the success path: the baseline is telemetry, and
    # spending a call on it before knowing whether the server answers at all
    # would double a black-holed host's cost to the tick.
    baseline = await owner_section_count() if owner_section_count is not None else None
    _logger.info(
        "entitlement capture user_id=%s sections=%s owner_sections=%s scope=%s",
        safe_int(user_id),
        safe_int(len(section_keys)),
        safe_int(baseline) if baseline is not None else "unknown",
        safe_text(_capture_scope(len(section_keys), baseline)),
    )
    return _CaptureAttempt(
        capture=EntitlementCapture(
            section_keys=section_keys, machine_identifier=machine_identifier
        ),
        server_unavailable=False,
    )


async def capture_entitlements(
    library: LibraryPort,
    *,
    machine_identifier: str,
    user_id: int,
    owner_section_count: Callable[[], Awaitable[int | None]] | None = None,
) -> EntitlementCapture | None:
    """Read the library sections ``library``'s token can see. Never raises.

    TOTAL by contract: capture is a best-effort enrichment, never a gate. Every
    caller (the sweep, sign-in) must be able to invoke it without a guard of its
    own and carry on unchanged when it returns ``None`` -- a failed capture
    leaves the previous snapshot exactly as it was, because nothing is written.

    This is the one unverified premise of the #484 design, deliberately staged
    ahead of any enforcement so it can be OBSERVED on the canary first: that a
    shared user's token authenticates against the configured ``plex_url``
    (possibly a LAN address the account has never used) and comes back
    SECTION-FILTERED rather than with the whole library. The telemetry line
    emitted here is what answers that from the log store -- counts and a one-word
    scope only, never section titles, keys, paths or tokens.

    The sweep uses :func:`_attempt_capture` directly because it also needs to
    know WHY a capture failed (to break its circuit on an unreachable server);
    single-shot callers like sign-in only need the value or ``None``.
    """
    attempt = await _attempt_capture(
        library,
        machine_identifier=machine_identifier,
        user_id=user_id,
        owner_section_count=owner_section_count,
    )
    return attempt.capture


async def read_token_ciphertext(session: AsyncSession, user_id: int) -> str | None:
    """The raw stored ciphertext of this user's Plex token, for the write guard.

    Captured BEFORE the network call so :func:`store_entitlements` can prove the
    credential the capture was taken with is still the stored one. See
    :func:`_stored_token_ciphertext` for why the comparison is on ciphertext.
    """
    return (
        await session.execute(select(_stored_token_ciphertext()).where(User.id == user_id))
    ).scalar_one_or_none()


async def store_entitlements(
    session: AsyncSession,
    user_id: int,
    capture: EntitlementCapture,
    *,
    anchor_setting_key: str,
    expected_token_ciphertext: str | None,
) -> bool:
    """Persist a capture, but only while it still describes the CONFIGURED server
    AND was taken with the credential still on file. Does not commit.

    Two conditions ride the UPDATE itself, so both are evaluated against the
    latest committed state rather than against locals that were read before a
    network call:

    * **The anchor.** A snapshot captured against server A must never be written
      as though it described server B, or PR-4/PR-5 enforcement would filter one
      server's libraries by another's section ids. Re-read from the settings row
      by the statement -- the same make-the-database-the-arbiter shape as
      ``watchlist_service.clear_snapshots_unless_settings_present``. Comparing
      against a value the CALLER passed would be tautological: both call sites
      derive it from the very variable the capture was stamped with.
    * **The token.** A sweep capture takes seconds; a sign-in can rotate the
      user's token and land a FRESHER capture in that window. Without this the
      slow, stale sweep write would overwrite the fresh one with the old
      credential's view. Conditioning on the ciphertext the capture was taken
      with means a rotated credential makes the stale write match zero rows and
      the newer capture stands.

    **Caller contract, and it holds globally: every caller must present the
    ciphertext of the exact credential that PRODUCED the snapshot, captured at
    the moment that credential was read -- never re-read later.** A re-read
    returns whatever is current when the (possibly slow, possibly long-detached)
    capture finally gets there, which for an overtaken capture is a DIFFERENT
    credential's ciphertext; the guard would then pass and the older snapshot
    would overwrite the newer one. Both capture sites satisfy this by
    construction rather than by timing: the sweep takes it from
    :class:`DueShareCandidate`, read in the same statement that selected the user
    for the verdict, and sign-in takes it from the transaction that wrote the
    token row it just verified. Those are the only two sites; a third must carry
    its own credential's ciphertext the same way.

    Writes both columns together: they are one fact ("these sections, as seen on
    that server, by that credential") and must never be half-applied.
    """
    anchor_still_configured = exists(
        select(Setting.id).where(
            Setting.key == anchor_setting_key,
            Setting.value == capture.machine_identifier,
        )
    )
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(User)
            .where(
                User.id == user_id,
                anchor_still_configured,
                _stored_token_ciphertext() == expected_token_ciphertext,
            )
            .values(
                entitled_section_keys=list(capture.section_keys),
                entitlements_machine_id=capture.machine_identifier,
            )
        ),
    )
    if result.rowcount:
        return True
    _logger.info(
        "entitlement capture for user_id=%s was not stored: either the configured Plex "
        "server or this account's stored token changed since the capture was taken "
        "(a repoint, a cleared anchor, or a fresher sign-in). Discarding it rather than "
        "stamping a snapshot that describes the wrong server or an older credential; the "
        "next sweep re-captures.",
        safe_int(user_id),
    )
    return False


def _clear_entitlements(user: User) -> None:
    """Drop a captured section snapshot that a confirmed revoke has invalidated.

    Back to the tri-state ``NULL`` ("never captured"), never ``[]`` ("captured,
    entitled to nothing"): a revoked share tells us nothing about which sections
    the account could see, so recording an authoritative empty capture would be a
    lie the #484 enforcement sites would later act on.
    """
    user.entitled_section_keys = None
    user.entitlements_machine_id = None


async def apply_share_verdict(
    session: AsyncSession,
    user: User,
    snapshot: EntitlementSnapshot,
    *,
    expected_token: str | None,
    now: datetime,
) -> ShareVerdictOutcome:
    """Persist one verdict -- and, on a confirmed loss, sign the user out.

    The verdict -> action table (all five ratified on issue #391; do not collapse
    any two of them):

    * ``AUTHORIZED`` -- stamp state + checked-at, reset the failure counter.
      Nothing else: section capture is #484 scope (PR-3), so the entitlement
      columns are never WRITTEN here.
    * ``SHARE_REVOKED`` -- plex.tv answered authoritatively that the account no
      longer reaches this server. Revoke every session, clear the entitlement
      snapshot, and write an ``AuditLog`` row saying so.
    * ``TOKEN_STALE`` -- ALSO signs the user out (ratified), because a dead
      credential means we can no longer verify them at all. Labeled distinctly in
      both ``share_state`` and the audit description: the Plex sign-in expired,
      access was NOT removed. Entitlements are RETAINED -- nothing disproved them.
    * ``UNKNOWN`` -- a transient plex.tv failure. NEVER revokes. Increments the
      failure counter and stamps ``share_check_failed_at``; ``share_state`` and
      ``share_checked_at`` are deliberately left alone, so the last KNOWN verdict
      survives the outage and the user stays due for a prompt retry.
    * ``UNVERIFIABLE`` -- no stored token to check. Stamp the state only; there is
      nothing to revoke and nothing was disproved.

    **Owner/admin accounts are exempt from the sign-out half.** Both share-loss
    verdicts are still recorded honestly (``share_state``, an audit row, a WARNING
    log) for an admin, but their sessions are never cut. The reason is ADR-0005's
    never-locked-out rule, which the repoint path already honors: an admin is the
    only principal who can repair a wrong server anchor from the web
    (``PUT /settings``), and that needs a live admin session -- so a sweep allowed
    to sign admins out could destroy the very credential needed to undo whatever
    made it fire. A genuinely removed admin is still revocable by hand from the
    sessions page; this only refuses to do it AUTOMATICALLY.

    "Why was I signed out?" is answerable today from the Logs page -- each
    sign-out emits a pinned, user-id-tagged log line naming the verdict. The
    ``AuditLog`` row is the durable, queryable record of the same event; a read
    surface for it is follow-up work, so the log line (not the audit table) is
    the current operator answer path.

    ``expected_token`` closes the mid-sweep re-sign-in race: the verdict was
    computed against the token read during due-selection, and a user who signed
    in again since then holds a NEW token this verdict says nothing about (the
    same guard shape as ``watchlist_service.clear_user_snapshot``'s
    ``expected_token``). Two things make that guard actually hold against a
    sign-in landing mid-apply (Codex review of PR #557):

    1. The token condition rides the revocation UPDATE ITSELF, as a correlated
       ``EXISTS`` over ``users`` comparing the stored ciphertext byte-for-byte to
       what this guard read (``only_if_user_matches``; see
       :func:`_stored_token_ciphertext` for why the comparison is on ciphertext
       rather than plaintext). One conditioned statement cannot be overtaken the
       way a read-then-write can: under READ COMMITTED the UPDATE re-evaluates its
       predicates against the latest committed row after any lock wait, so a
       sign-in that committed a new token makes it match ZERO rows on every
       supported backend. It also moots any autoflush interleaving for the
       token-changed case. The Python-side comparison below stays as the fast,
       honest early exit -- it is no longer what makes the guard safe.
    2. The revocation is additionally bounded to sessions that existed at the
       guard instant (``created_at_or_before``), which covers the case (1) cannot:
       a sign-in that reuses the SAME token still mints a new session, and that
       session must survive.

    **PostgreSQL posture.** Two residuals, neither reachable on the only
    supported topology (SQLite, single-writer, which serializes these statements
    outright):

    * ``AuthSession.created_at`` defaults to ``func.now()``, which on PostgreSQL
      is TRANSACTION-START time. A same-token sign-in whose transaction began
      before ``guard_moment`` but committed after it therefore stamps a
      ``created_at`` inside the bound and can still be cut. The direction is
      toward cutting a fresh session rather than sparing a revoked one; it is
      bounded (one session) and self-healing (the user signs in again, and a
      genuinely-revoked user cannot complete sign-in because
      ``auth._post_init_access`` re-checks the share on every sign-in). Closing it
      fully would need real serialization, which is not worth engineering for a
      backend this app does not yet run on.
    * Symmetrically, app-vs-database clock skew could put a legitimately-old
      session outside the bound and spare it; the next sweep after the
      revalidation interval re-evaluates, the same worst case the interval
      already defines.
    """
    # Deliberately the FIRST await of this branch and the last one before the
    # revoke: see the docstring. ``guard_moment`` is captured before the read so
    # the bound can never exclude a session that existed when we looked, and the
    # ciphertext is captured alongside the plaintext so the UPDATE below can
    # re-assert "still the same row" atomically.
    guard_moment = datetime.now(UTC)
    guard_row = (
        await session.execute(
            # The ciphertext column is LABELLED: without it both selected
            # expressions key to ``users.encrypted_plex_token`` and the row
            # cannot be resolved.
            select(
                User.encrypted_plex_token,
                _stored_token_ciphertext().label("token_ciphertext"),
            ).where(User.id == user.id)
        )
    ).one_or_none()
    stored_token, stored_ciphertext = guard_row if guard_row is not None else (None, None)
    if stored_token != expected_token:
        _logger.info(
            "share revalidation skipped for user_id=%s: the stored Plex token changed "
            "since this tick selected them; re-checking on a later tick",
            safe_int(user.id),
        )
        return ShareVerdictOutcome(applied=False, signed_out=False, sessions_revoked=0)

    verdict = snapshot.verdict
    if verdict is ShareVerdict.UNKNOWN:
        # Fail OPEN, loudly. Leaving share_state/share_checked_at untouched is
        # the point: a plex.tv outage must not overwrite the last real verdict,
        # and the user must stay due so the answer is re-sought promptly.
        user.share_check_failures += 1
        user.share_check_failed_at = now
        await session.flush()
        _logger.warning(
            "share revalidation could not be determined for user_id=%s "
            "(consecutive failures: %s); retaining existing access",
            safe_int(user.id),
            safe_int(user.share_check_failures),
        )
        return ShareVerdictOutcome(applied=True, signed_out=False, sessions_revoked=0)

    previous_state = user.share_state
    user.share_state = verdict.value
    user.share_checked_at = now
    # Any verdict that is not UNKNOWN is a definitive answer, so the
    # consecutive-could-not-determine streak ends here (see the column comment on
    # ``User.share_check_failures``).
    user.share_check_failures = 0
    user.share_check_failed_at = None

    if verdict is ShareVerdict.AUTHORIZED or verdict is ShareVerdict.UNVERIFIABLE:
        await session.flush()
        return ShareVerdictOutcome(applied=True, signed_out=False, sessions_revoked=0)

    # Owner/admin: never signed out automatically (see the docstring -- ADR-0005
    # never-locked-out). Their verdict is still recorded, loudly.
    admin_exempt = user.permissions > 0

    if verdict is ShareVerdict.SHARE_REVOKED:
        _clear_entitlements(user)
        action_type = "user.share_revoked"
        description = (
            "Automatic share revalidation: this Plex account no longer has access to the "
            "configured server, so every browser session was signed out."
        )
    else:  # ShareVerdict.TOKEN_STALE
        # Deliberately NOT "access removed": plex.tv rejected the credential
        # before it could say anything about the share. Same machinery, honest
        # (and different) words -- the operator-facing distinction the design
        # ratified.
        action_type = "user.plex_sign_in_expired"
        description = (
            "Automatic share revalidation: token stale -- this account's Plex sign-in expired, "
            "so its access could no longer be verified and every browser session was signed "
            "out. Access to the server was not removed; signing in with Plex again restores it."
        )

    if admin_exempt:
        action_type = f"{action_type}_admin_exempt"
        description = (
            "Automatic share revalidation recorded "
            f"'{verdict.value}' for this ADMIN account, but did NOT sign it out: an "
            "administrator is the only principal who can repair a wrong Plex server "
            "configuration from the web, so this sweep never cuts the session that repair "
            "needs. Review it and revoke by hand from the sessions page if the removal is "
            "genuine."
        )
        revoked = 0
    else:
        # ONE conditioned statement (see the docstring): the token equality is
        # re-asserted by the UPDATE itself against the latest committed row, and
        # the revocation is bounded to sessions that existed at ``guard_moment``.
        revoked = await session_lifecycle.revoke_user_sessions(
            session,
            user.id,
            now=now,
            created_at_or_before=guard_moment,
            only_if_user_matches=_stored_token_ciphertext() == stored_ciphertext,
        )
    await audit_service.record(
        session,
        actor_user_id=None,
        action_type=action_type,
        entity_type="user",
        entity_id=user.id,
        old_value={"share_state": previous_state},
        new_value={
            "share_state": verdict.value,
            "sessions_revoked": revoked,
            "admin_exempt": admin_exempt,
        },
        description=description,
    )
    if admin_exempt:
        # WARNING, not INFO: an admin losing their share is exactly the state an
        # operator has to look at, and the sweep declining to act on it must be
        # impossible to miss in the log stream.
        _logger.warning(
            "share revalidation recorded %s for ADMIN user_id=%s but did not sign them out "
            "(never-locked-out rule); revoke by hand from the sessions page if intended",
            verdict.value,
            safe_int(user.id),
        )
        return ShareVerdictOutcome(
            applied=True, signed_out=False, sessions_revoked=0, admin_exempt=True
        )
    _logger.info(
        "share revalidation signed out user_id=%s (%s); revoked %s session(s)",
        safe_int(user.id),
        verdict.value,
        safe_int(revoked),
    )
    return ShareVerdictOutcome(applied=True, signed_out=True, sessions_revoked=revoked)


async def sweep_shares(
    sessionmaker: async_sessionmaker[AsyncSession],
    plex_tv: PlexTvClient,
    machine_identifier: str,
    *,
    revalidate_after: timedelta,
    limit: int = SHARE_SWEEP_USER_BUDGET,
    now: datetime | None = None,
    confirm_anchor: Callable[[], Awaitable[AnchorCheck]] | None = None,
    on_signed_out: Callable[[int], None] | None = None,
    capture: EntitlementCaptureContext | CaptureUnavailableReason | None = None,
) -> ShareSweepResult:
    """Revalidate up to ``limit`` due users, strictly sequentially.

    This is the whole of #391's answer to "a revoked Plex share keeps API access
    for 7-30 days": the exposure window becomes the revalidation interval
    (default 6h) instead of the session lifetime. Zero exposure would require
    plex.tv on the per-request path, which ADR-0016 rejects.

    Takes a ``sessionmaker`` rather than a session because each user's plex.tv
    call is real network latency: one transaction spanning 20 serial calls would
    hold a write lock for the length of the whole sweep. Selection runs in its
    own short transaction, each verdict is applied and committed in its own, and
    the network calls happen BETWEEN transactions -- which is also why
    :func:`apply_share_verdict` re-checks the token it was selected with.

    ``confirm_anchor`` re-reads the configured server's ``machineIdentifier``
    live. It is awaited AT MOST ONCE per tick, lazily -- only when the tick is
    about to act on its first :attr:`ShareVerdict.SHARE_REVOKED`, so a sweep that
    finds everyone still entitled costs no extra probe -- and its answer gates
    every share-loss verdict in that tick. Anything but
    :attr:`AnchorCheck.CONFIRMED` means the anchor those verdicts were computed
    against cannot be trusted (a rebuilt or re-claimed server hands out a new
    identifier, which makes plex.tv truthfully report EVERY user as revoked), so
    none of them are acted on and every affected user is left DUE for the next
    tick. Omitting the callable is treated as :attr:`AnchorCheck.UNCONFIRMED`:
    fail-safe, never fail-open, because the failure mode being guarded is a total
    sign-out.

    ``on_signed_out`` is invoked with a user id IMMEDIATELY after the transaction
    that revoked their sessions commits -- never batched to the end. Closing the
    realtime stream needs the FastAPI app (``web.events.close_realtime_streams``)
    which this module must not import, but deferring the whole batch would mean a
    later user's exception strands an already-committed revocation with a live
    SSE stream, and a revoked user is no longer due-selected, so nothing would
    ever come back to close it (issue #183). A raising callback is logged and
    degrades the tick; it never aborts the sweep.

    ``capture`` opts this sweep into section-entitlement capture (#484 PR-3).
    Capture runs ONLY for an ``AUTHORIZED`` verdict that was actually applied and
    only once the tick's live anchor check has CONFIRMED the configured server,
    so it costs one extra call to the Plex SERVER for users already confirmed
    entitled and none at all for the revoked, stale, unknown or unverifiable --
    and it rides the existing per-tick budget rather than widening it. It cannot
    change a verdict, a revocation, or the tick's state: a failed capture is
    counted and logged, and leaves the previous snapshot untouched. Omitted
    (``None``) the sweep behaves exactly as it did before capture existed.

    ``capture`` may instead be a :data:`CaptureUnavailableReason` -- the
    composition root saying "I am deliberately handing you no context, and this
    is why". Every AUTHORIZED user is then counted in ``capture_skipped`` and the
    reason is reported on the result, because a gate that reports nothing is a
    silent disable: an operator would read an ``ok`` sweep with authorized users
    and three permanent zeros and have no way to tell that capture is off (north
    star #3). ``None`` remains "no capture wired here at all" (the pre-capture
    sweep, and most tests), which counts nothing.

    Capture is best-effort by design and has no retry state of its own: see
    ``ShareSweepStatus.capture_skipped`` for the cadence a skipped user actually
    gets, and why that is acceptable while nothing reads these columns.

    One user's failure never ends the sweep: an unexpected exception is counted,
    surfaced through ``last_error_type`` (degrading the tick), and the remaining
    users are still checked.
    """
    moment = now if now is not None else datetime.now(UTC)
    # Split the one parameter into its two meanings once, up front, so the
    # capture path below never has to re-narrow it.
    capture_context = capture if isinstance(capture, EntitlementCaptureContext) else None
    capture_unavailable = capture if isinstance(capture, str) else None
    async with sessionmaker() as session:
        candidates = [
            DueShareCandidate(
                user_id=user.id, token=user.encrypted_plex_token, token_ciphertext=ciphertext
            )
            for user, ciphertext in await _select_due_candidates(
                session, now=moment, revalidate_after=revalidate_after, limit=limit
            )
        ]

    tallies: dict[ShareVerdict, int] = dict.fromkeys(ShareVerdict, 0)
    checked = 0
    skipped = 0
    admins_exempted = 0
    anchor_deferred = 0
    sessions_revoked = 0
    signed_out: list[int] = []
    last_error: str | None = None
    anchor: AnchorCheck | None = None
    captured = 0
    capture_failed = 0
    capture_skipped = 0
    capture_circuit_open = False
    owner_sections: int | None = None
    owner_sections_resolved = False

    async def _owner_section_count() -> int | None:
        """The owner-token section count, resolved AT MOST ONCE per tick.

        Lazy on purpose: a tick that captures nothing must not spend a call on a
        baseline nobody will compare against. ``owner_sections_resolved`` (not a
        ``None`` check) memoizes it, because ``None`` -- "the baseline itself
        could not be read" -- is a legitimate resolved answer that must not be
        retried per user.
        """
        nonlocal owner_sections, owner_sections_resolved
        if not owner_sections_resolved:
            owner_sections_resolved = True
            if capture_context is not None:
                try:
                    owner_sections = await capture_context.owner_section_count()
                except Exception as exc:
                    # The baseline is telemetry, never a gate: losing it degrades
                    # the log line to ``scope=baseline_unknown`` and nothing else.
                    _logger.warning(
                        "entitlement capture could not read the owner-token section baseline "
                        "(%s); captures this tick report an unknown scope",
                        type(exc).__name__,
                    )
                    owner_sections = None
        return owner_sections

    async def _capture_for(candidate: DueShareCandidate) -> None:
        """Capture and persist one AUTHORIZED user's sections. Never raises.

        Wholly guarded, including the caller-supplied ``library_for_token``: the
        service's "capture can never break the sweep" invariant must not rest on
        a web-layer callable happening not to throw.

        The credential identity comes from ``candidate`` -- read in the SAME
        statement that selected this user for the verdict -- never from a fresh
        read here. A re-read would pick up a token a sign-in rotated in between
        the verdict and now, and the resulting old-token snapshot would sail
        through the write guard that exists to reject exactly that.
        """
        nonlocal captured, capture_failed, capture_skipped, capture_circuit_open
        user_id = candidate.user_id
        token = candidate.token
        if token is None:
            return
        if capture_unavailable is not None:
            # Capture is switched off for the whole install, not for this user.
            # Counted anyway (and reported once, after the loop): the alternative
            # is an ok sweep whose capture counters are all zero forever, which
            # looks exactly like a healthy tick with nothing to capture.
            capture_skipped += 1
            return
        if capture_context is None:
            return
        if capture_circuit_open:
            # The server already failed once this tick, so the remaining users
            # would each pay another full client timeout to learn the same
            # thing. They stay uncaptured and are counted, not silently dropped.
            capture_skipped += 1
            return
        try:
            library = capture_context.library_for_token(token)
        except Exception:
            capture_failed += 1
            _logger.exception(
                "entitlement capture could not build a Plex client for user_id=%s; "
                "skipping their capture",
                safe_int(user_id),
            )
            return
        if library is None:
            # Plex is not configured enough to reach the server with a user
            # token. Not a capture FAILURE -- there was nothing to attempt.
            return
        # A capture is only meaningful STAMPED, and a stamp is only trustworthy
        # if the server answering right now is the one the anchor names. Without
        # this, a replacement server at the SAME ``plex_url`` would have its
        # sections written under the OLD cached identifier -- and the settings
        # -row guard in ``store_entitlements`` would accept it, because that row
        # still holds the old id. Reuses the sweep's own per-tick live
        # ``/identity`` confirmation, so it costs nothing extra on a tick that
        # already had to confirm for a revocation.
        if not await _anchor_confirmed():
            capture_skipped += 1
            return
        attempt = await _attempt_capture(
            library,
            machine_identifier=machine_identifier,
            user_id=user_id,
            owner_section_count=_owner_section_count,
        )
        if attempt.server_unavailable:
            # Trip the breaker: a black-holed server costs one full timeout per
            # AUTHORIZED user, which at a full budget is minutes of tick wall
            # time spent learning nothing. Verdict work is unaffected -- it talks
            # to plex.tv, not to this server.
            capture_circuit_open = True
            _logger.warning(
                "entitlement capture is skipped for the rest of this tick: the Plex server "
                "did not answer. Verdicts are unaffected. Skipped users are NOT retried "
                "immediately -- they are recaptured at their next due window (bounded by "
                "share_revalidation_interval_hours) or at their next sign-in, whichever "
                "comes first. Nothing reads these columns yet (#484 PR-3 is capture-only)."
            )
        if attempt.capture is None:
            capture_failed += 1
            return
        try:
            async with sessionmaker() as session:
                stored = await store_entitlements(
                    session,
                    user_id,
                    attempt.capture,
                    anchor_setting_key=capture_context.anchor_setting_key,
                    expected_token_ciphertext=candidate.token_ciphertext,
                )
                await session.commit()
        except Exception:
            capture_failed += 1
            _logger.exception(
                "entitlement capture for user_id=%s could not be persisted; the previous "
                "snapshot is unchanged",
                safe_int(user_id),
            )
            return
        if stored:
            captured += 1
        else:
            capture_failed += 1

    async def _anchor_confirmed() -> bool:
        """Resolve (once per tick) whether the server anchor still holds."""
        nonlocal anchor, last_error
        if anchor is None:
            if confirm_anchor is None:
                anchor = AnchorCheck.UNCONFIRMED
            else:
                try:
                    anchor = await confirm_anchor()
                except Exception as exc:
                    _logger.exception("share revalidation anchor confirmation raised; deferring")
                    last_error = type(exc).__name__
                    anchor = AnchorCheck.UNCONFIRMED
            if anchor is not AnchorCheck.CONFIRMED:
                # One line per tick, not per user -- and WARNING, because the
                # sweep is now knowingly not enforcing anything.
                _logger.warning(
                    "share revalidation will not act on share-loss verdicts this tick: the "
                    "configured Plex server's machine identifier could not be confirmed (%s). "
                    "Affected users keep their access and stay due for the next tick.",
                    anchor.value,
                )
                last_error = (
                    "PlexAnchorMismatch"
                    if anchor is AnchorCheck.MISMATCHED
                    else last_error or "PlexAnchorUnconfirmed"
                )
        return anchor is AnchorCheck.CONFIRMED

    for candidate in candidates:
        user_id = candidate.user_id
        token = candidate.token
        try:
            snapshot = await check_share(plex_tv, machine_identifier, token=token)
        except Exception as exc:
            last_error = type(exc).__name__
            _logger.warning(
                "share revalidation errored for user_id=%s (%s); leaving access unchanged",
                safe_int(user_id),
                type(exc).__name__,
            )
            # Persist the failed ATTEMPT, exactly as the UNKNOWN verdict does.
            # Without it this user's ``share_check_failed_at`` never moves, so
            # ``_last_attempt_at`` keeps them at the head of the due queue and a
            # crashing cohort re-consumes the whole budget every tick forever --
            # the same starvation the ordering fix closed, reached through this
            # branch instead (Codex round 2 on PR #557). Its own short
            # transaction, and itself guarded: a bookkeeping failure must not end
            # the sweep for everyone behind this user.
            try:
                async with sessionmaker() as session:
                    await record_failed_attempt(session, user_id, now=datetime.now(UTC))
                    await session.commit()
            except Exception:
                _logger.exception(
                    "share revalidation could not record the failed attempt for user_id=%s; "
                    "they may be re-selected next tick",
                    safe_int(user_id),
                )
            continue
        if snapshot.verdict is ShareVerdict.SHARE_REVOKED and not await _anchor_confirmed():
            # Act on NOTHING: no stamp, no revoke, no audit row. Leaving
            # share_checked_at untouched keeps this user due, so a genuine
            # revocation is still caught as soon as the anchor is trustworthy.
            anchor_deferred += 1
            continue
        async with sessionmaker() as session:
            user = await session.get(User, user_id)
            if user is None:
                # Deleted between selection and now -- nothing to stamp, and no
                # sessions left to revoke (the FK cascades).
                skipped += 1
                continue
            outcome = await apply_share_verdict(
                session,
                user,
                snapshot,
                expected_token=token,
                now=datetime.now(UTC),
            )
            await session.commit()
        if not outcome.applied:
            skipped += 1
            continue
        checked += 1
        tallies[snapshot.verdict] += 1
        sessions_revoked += outcome.sessions_revoked
        if snapshot.verdict is ShareVerdict.AUTHORIZED and token is not None:
            # AUTHORIZED only, and only once the verdict actually landed: a user
            # whose verdict was skipped (token changed mid-sweep) must not have
            # entitlements written from a token this tick no longer trusts.
            #
            # Belt-and-braces around an already-total helper: capture is
            # enrichment, and no shape of failure inside it may discard a tick of
            # real verdict work into ``mark_error``.
            try:
                await _capture_for(candidate)
            except Exception:
                capture_failed += 1
                _logger.exception(
                    "entitlement capture raised for user_id=%s; the verdict stands and the "
                    "sweep continues",
                    safe_int(user_id),
                )
        if outcome.admin_exempt:
            admins_exempted += 1
        if outcome.signed_out:
            signed_out.append(user_id)
            # Immediately, on the committed side of the revocation (issue #183).
            if on_signed_out is not None:
                try:
                    on_signed_out(user_id)
                except Exception as exc:
                    last_error = type(exc).__name__
                    _logger.exception(
                        "share revalidation revoked user_id=%s but closing their realtime "
                        "stream failed; the session is revoked and their next request will "
                        "401, but an open stream may survive until it reconnects",
                        safe_int(user_id),
                    )

    if capture_unavailable is not None and capture_skipped:
        # Once per tick, not once per user, and only when the gate actually cost
        # something -- an install with no authorized users due is not owed a
        # warning about a capture it would not have run anyway.
        _logger.warning(
            "entitlement capture did not run this tick (%s): %s authorized user(s) were "
            "skipped. Verdicts and sessions are unaffected, and no stored snapshot was "
            "touched. Capture starts once the Plex settings are saved (finishing setup, or "
            "re-saving them, records the verified server anchor a snapshot is stamped with).",
            safe_text(capture_unavailable),
            safe_int(capture_skipped),
        )

    async with sessionmaker() as session:
        # Recomputed, never inferred from the budget: a user leaves the backlog
        # only if this tick actually stamped ``share_checked_at``. Subtracting the
        # candidate count instead would report a comfortable 0 during a plex.tv
        # outage, when in truth every one of those UNKNOWN users is still owed a
        # check -- exactly the moment the number needs to be honest.
        due_remaining = await count_due_share_checks(
            session, now=moment, revalidate_after=revalidate_after
        )

    return ShareSweepResult(
        checked=checked,
        authorized=tallies[ShareVerdict.AUTHORIZED],
        share_revoked=tallies[ShareVerdict.SHARE_REVOKED],
        token_stale=tallies[ShareVerdict.TOKEN_STALE],
        unknown=tallies[ShareVerdict.UNKNOWN],
        unverifiable=tallies[ShareVerdict.UNVERIFIABLE],
        skipped=skipped,
        admins_exempted=admins_exempted,
        anchor_deferred=anchor_deferred,
        anchor_state=anchor,
        captured=captured,
        capture_failed=capture_failed,
        capture_skipped=capture_skipped,
        capture_unavailable=capture_unavailable,
        sessions_revoked=sessions_revoked,
        due_remaining=due_remaining,
        signed_out_user_ids=tuple(signed_out),
        last_error_type=last_error,
    )
