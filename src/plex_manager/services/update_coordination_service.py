"""Application coordination for safe, crash-recoverable container updates.

Every public method opens and closes its own short database transaction. In
particular, :meth:`UpdateCoordinationService.critical_operation` does not retain
a session while the wrapped network/filesystem work runs; a sibling task renews
the durable lease through separate transactions and release is best-effort in a
``finally`` block.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.repositories.update_coordination import (
    CoordinatorRecoveryNotReadyError,
    CoordinatorSnapshot,
    DrainLeaseActiveError,
    ForceResetResult,
    LeaseKind,
    SqlUpdateCoordinationRepository,
    UnknownCoordinatorPhaseError,
)
from plex_manager.services import audit_service

__all__ = [
    "COORDINATOR_RECOVERY_MAX_AGE",
    "UPDATER_HEARTBEAT_MAX_AGE",
    "CoordinatorRecoveryNotReadyError",
    "CoordinatorSnapshot",
    "DrainClaim",
    "DrainLeaseActiveError",
    "ForceResetResult",
    "LeaseGrant",
    "MaintenanceDrainingError",
    "MaintenanceLeaseLostError",
    "UnknownCoordinatorPhaseError",
    "UpdateAction",
    "UpdateCoordinationService",
    "UpdateOperationInProgressError",
    "UpdatePhase",
    "UpdateResult",
]

_CODE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_DEFAULT_CRITICAL_TTL = timedelta(minutes=5)
# The sidecar-liveness contract: a heartbeat older than this means no updater
# is connected. Used by the updates router (status's ``updater_available`` and
# the 503 gate on manual actions). Deliberately NOT used by recovery decisions:
# an eligibility poll refreshes the heartbeat even when no work is handed out,
# so freshness proves connectivity, never work in flight -- recovery is gated
# on the bounded start anchor and live drain leases instead (issue #368).
UPDATER_HEARTBEAT_MAX_AGE = timedelta(seconds=45)
COORDINATOR_RECOVERY_MAX_AGE = timedelta(minutes=10)
_logger = logging.getLogger(__name__)


class _UnsetValue:
    """Distinguish an omitted image field from an explicit clear-to-None."""


_UNSET = _UnsetValue()


class UpdateAction(StrEnum):
    """Operator intents the sidecar consumes."""

    check = "check"
    install = "install"


class UpdatePhase(StrEnum):
    """Bounded coordinator phases safe to expose in status/errors."""

    idle = "idle"
    checking = "checking"
    available = "available"
    draining = "draining"
    installing = "installing"
    rollback = "rollback"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"


class UpdateResult(StrEnum):
    """Bounded terminal outcomes acknowledged by the updater."""

    no_update = "no_update"
    update_available = "update_available"
    success = "success"
    failed = "failed"
    rolled_back = "rolled_back"
    cancelled = "cancelled"


class MaintenanceDrainingError(RuntimeError):
    """Raised when a drain lease prevents new critical work."""


class MaintenanceLeaseLostError(RuntimeError):
    """Raised when work is cancelled because its renewable lease was lost."""


class UpdateOperationInProgressError(RuntimeError):
    """Raised when new operator intent would invalidate an active update."""


@dataclass(frozen=True)
class LeaseGrant:
    """A plaintext ownership token returned only to its lease holder."""

    token: str
    kind: LeaseKind
    owner: str
    operation: str | None
    action_generation: int | None
    expires_at: datetime


@dataclass(frozen=True)
class DrainClaim:
    """An exclusive drain lease and whether existing critical work has left."""

    lease: LeaseGrant
    ready: bool


@dataclass(frozen=True)
class _HeldLease:
    lease: LeaseGrant
    task: asyncio.Task[object] | None


class UpdateCoordinationService:
    """Database-backed facade used by app workers and internal updater APIs."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._held_critical: ContextVar[_HeldLease | None] = ContextVar(
            f"update_critical_lease_{id(self)}", default=None
        )

    async def initialize(self) -> None:
        """Ensure the singleton exists for metadata-created/test databases."""
        async with self._sessionmaker() as session:
            await SqlUpdateCoordinationRepository(session).ensure_state()
            await session.commit()

    async def snapshot(self) -> CoordinatorSnapshot:
        """Return status after expiring crash-stranded leases."""
        async with self._sessionmaker() as session:
            snapshot = await SqlUpdateCoordinationRepository(session).snapshot(self._now())
            await session.commit()
            return snapshot

    async def touch_updater(
        self,
        *,
        phase: UpdatePhase | None = None,
        expected_generation: int | None = None,
    ) -> CoordinatorSnapshot | None:
        """Refresh liveness/phase without replaying potentially stale image fields."""
        async with self._sessionmaker() as session:
            touched = await SqlUpdateCoordinationRepository(session).touch_updater(
                now=self._now(),
                phase=phase.value if phase is not None else None,
                expected_generation=expected_generation,
            )
            await session.commit()
        return await self.snapshot() if touched else None

    async def mark_busy_work_dispatched(self) -> bool:
        """Durably restart the busy-phase recovery clock on a real work handout.

        Called when eligibility actually assigns a ``check``/``install`` to the
        sidecar while the coordinator row is already in a busy phase -- a
        genuine work-start even though the phase string repeats (a MANUAL
        queued action; automatic dispatch is gated on a non-busy phase). Never
        called for no-work polls or same-phase heartbeats (see the repo
        method).
        """
        async with self._sessionmaker() as session:
            stamped = await SqlUpdateCoordinationRepository(session).mark_busy_work_dispatched(
                self._now()
            )
            await session.commit()
            return stamped

    async def request_action(self, action: UpdateAction) -> int:
        """Persist operator intent, or refuse while an update operation is active."""
        async with self._sessionmaker() as session:
            generation = await SqlUpdateCoordinationRepository(session).request_action(
                action.value, self._now()
            )
            if generation is None:
                raise UpdateOperationInProgressError("an update operation is already active")
            await session.commit()
            return generation

    async def acknowledge_action(
        self,
        *,
        expected_generation: int,
        result: UpdateResult,
        error_code: str | None = None,
        current_build: str | None | _UnsetValue = _UNSET,
        current_digest: str | None | _UnsetValue = _UNSET,
        available_build: str | None | _UnsetValue = _UNSET,
        available_digest: str | None | _UnsetValue = _UNSET,
        preserve_action: bool = False,
    ) -> bool:
        """CAS-acknowledge a non-install action such as an image check."""
        error_code = _bounded_code(error_code, "error_code")
        image_values: dict[str, str | None] = {}
        for field, value in (
            ("current_build", current_build),
            ("current_digest", current_digest),
            ("available_build", available_build),
            ("available_digest", available_digest),
        ):
            if isinstance(value, _UnsetValue):
                continue
            image_values[field] = _bounded_text(value, field)
        async with self._sessionmaker() as session:
            acknowledged = await SqlUpdateCoordinationRepository(session).acknowledge_action(
                expected_generation=expected_generation,
                phase=_phase_for_result(result).value,
                result=result.value,
                error_code=error_code,
                image_values=image_values,
                preserve_action=preserve_action,
                now=self._now(),
            )
            await session.commit()
            return acknowledged

    async def acquire_critical(
        self,
        operation: str,
        *,
        owner: str = "plex-manager",
        ttl: timedelta = _DEFAULT_CRITICAL_TTL,
    ) -> LeaseGrant | None:
        """Acquire renewable critical-work ownership, or defer under drain."""
        operation = _bounded_code(operation, "operation") or ""
        owner = _bounded_code(owner, "owner") or ""
        ttl = _positive_ttl(ttl)
        token = self._token_factory()
        token_hash = _token_hash(token)
        async with self._sessionmaker() as session:
            row = await SqlUpdateCoordinationRepository(session).acquire_critical(
                token_hash=token_hash,
                owner=owner,
                operation=operation,
                now=self._now(),
                ttl=ttl,
            )
            await session.commit()
        if row is None:
            return None
        return LeaseGrant(
            token=token,
            kind=row.kind,
            owner=row.owner,
            operation=row.operation,
            action_generation=row.action_generation,
            expires_at=row.expires_at,
        )

    async def claim_drain(
        self,
        *,
        owner: str = "container-updater",
        ttl: timedelta,
        action_generation: int | None = None,
        materialize_install: bool = False,
        require_idle: bool = False,
    ) -> DrainClaim | None:
        """Claim the exclusive drain; ``ready=False`` means existing work remains."""
        owner = _bounded_code(owner, "owner") or ""
        ttl = _positive_ttl(ttl)
        token = self._token_factory()
        token_hash = _token_hash(token)
        async with self._sessionmaker() as session:
            claimed = await SqlUpdateCoordinationRepository(session).claim_drain(
                token_hash=token_hash,
                owner=owner,
                action_generation=action_generation,
                materialize_install=materialize_install,
                require_idle=require_idle,
                now=self._now(),
                ttl=ttl,
            )
            await session.commit()
        if claimed is None:
            return None
        row, ready = claimed
        return DrainClaim(
            lease=LeaseGrant(
                token=token,
                kind=row.kind,
                owner=row.owner,
                operation=row.operation,
                action_generation=row.action_generation,
                expires_at=row.expires_at,
            ),
            ready=ready,
        )

    async def renew(self, token: str, *, ttl: timedelta) -> bool:
        """CAS-renew an exact, still-unexpired lease."""
        ttl = _positive_ttl(ttl)
        async with self._sessionmaker() as session:
            renewed = await SqlUpdateCoordinationRepository(session).renew(
                _token_hash(token), now=self._now(), ttl=ttl
            )
            await session.commit()
            return renewed

    async def renew_drain_progress(
        self,
        token: str,
        *,
        ttl: timedelta,
        phase: UpdatePhase | None = None,
    ) -> bool | None:
        """Renew an exact drain while atomically reporting sidecar liveness."""
        ttl = _positive_ttl(ttl)
        async with self._sessionmaker() as session:
            ready = await SqlUpdateCoordinationRepository(session).renew_drain_progress(
                _token_hash(token),
                now=self._now(),
                ttl=ttl,
                phase=phase.value if phase is not None else None,
            )
            await session.commit()
            return ready

    async def release(self, token: str) -> bool:
        """Release the exact lease; repeated/expired releases are no-ops."""
        async with self._sessionmaker() as session:
            released = await SqlUpdateCoordinationRepository(session).release(
                _token_hash(token), self._now()
            )
            await session.commit()
            return released

    async def force_reset_coordinator_phase(
        self,
        *,
        actor_user_id: int | None,
        recovery_max_age: timedelta = COORDINATOR_RECOVERY_MAX_AGE,
    ) -> ForceResetResult | None:
        """Admin break-glass: recover a wedged coordinator phase (issues #354, #368).

        The service face of the recovery path for two distinct wedges: the
        fail-closed unknown-phase guard (issue #354), and an orphaned known
        BUSY phase (``checking``/``draining``/``installing``/``rollback``)
        whose sidecar died without ever releasing it (issue #368). See
        :func:`~plex_manager.domain.update_recovery.decide_recovery` for the
        evidence matrix itself -- a live drain lease or a bounded start-anchor
        age are the only two signals gated on; heartbeat freshness is
        deliberately not evidence -- and
        :meth:`~plex_manager.repositories.update_coordination.SqlUpdateCoordinationRepository.force_reset_phase`
        for the lock + re-check protocol that applies it. In outline:

        * a live drain lease (known busy or unrecognized phase alike) raises
          :class:`DrainLeaseActiveError` -- an updater generation may
          genuinely be mid-install, so the reset refuses rather than tearing
          a live lease;
        * a known busy phase whose start anchor has not yet aged past
          ``recovery_max_age`` raises :class:`CoordinatorRecoveryNotReadyError`
          -- the operation could still be in flight, so the caller retries
          once the bound elapses;
        * a known non-busy phase with a known action is a true no-op (``None``
          is returned, nothing wedged to recover);
        * a known non-busy phase with an unrecognized action clears the
          action alone (the ACTION-ONLY reset), fencing its generation;
        * everything else -- a drain-less busy phase past the bound, or an
          unrecognized phase -- re-anchors the phase to ``idle``. A genuinely
          queued known action (``check``/``install``) is preserved, keeping
          its generation so the reconnecting sidecar can still complete it;
          an unrecognized or absent (``"none"``) action instead has the
          generation bumped, fencing any late/abandoned worker's outcome
          behind a CAS it can no longer satisfy.

        The reset and its :class:`~plex_manager.models.AuditLog` row commit in ONE
        transaction: a state change that silently reassigned the coordinator out
        of a wedged state with no durable record of WHO did it, or when, would
        violate "honesty over silence" (north star #3). The audit row is written
        only when a reset actually happened -- the not-ready-yet, drain-active,
        and true-no-op refusals all change nothing and record nothing -- and it
        names exactly what changed: the re-anchored phase, the cleared
        unrecognized ``requested_action``, the fenced ``action_generation``, or
        any combination of the three.
        ``actor_user_id`` is ``None`` for an API-key / recovery-key admin, which
        has no Plex identity; that honest null actor matches every other admin
        action taken via the break-glass credential.

        Returns the :class:`ForceResetResult` on reset, or ``None`` when there
        was nothing to recover and nothing was changed.
        """
        async with self._sessionmaker() as session:
            repo = SqlUpdateCoordinationRepository(session)
            # Observation side-effects (the legacy busy-row anchor backfill and
            # expired-lease cleanup) must be durable even when the decision is a
            # refusal: an operator who only ever hits force-reset must still
            # start -- and keep -- the recovery clock on the FIRST attempt, or
            # a repeatedly rolled-back `now` anchor makes
            # ``coordinator_recovery_not_ready`` permanent. Refusals raise (or
            # return None) before any recovery mutation, so committing here
            # persists exactly those legitimate observations and nothing else.
            try:
                result = await repo.force_reset_phase(
                    self._now(), recovery_max_age=_positive_ttl(recovery_max_age)
                )
            except (CoordinatorRecoveryNotReadyError, DrainLeaseActiveError):
                await session.commit()
                raise
            if result is None:
                await session.commit()
                return None
            old_value: dict[str, object] = {}
            new_value: dict[str, object] = {}
            if result.old_phase is not None:
                old_value["phase"] = result.old_phase
                new_value["phase"] = UpdatePhase.idle.value
            if result.cleared_requested_action is not None:
                old_value["requested_action"] = result.cleared_requested_action
                new_value["requested_action"] = "none"
            if (
                result.old_action_generation is not None
                and result.new_action_generation is not None
            ):
                old_value["action_generation"] = result.old_action_generation
                new_value["action_generation"] = result.new_action_generation
            description = (
                "Force-reset the update coordinator phase to idle."
                if result.old_phase is not None
                else "Cleared an unrecognized queued updater action."
            )
            await audit_service.record(
                session,
                actor_user_id=actor_user_id,
                action_type="update.coordinator_phase_force_reset",
                entity_type="update_coordinator",
                entity_id=1,
                old_value=old_value,
                new_value=new_value,
                description=description,
            )
            await session.commit()
            return result

    async def acknowledge_outcome(
        self,
        token: str,
        *,
        result: UpdateResult,
        expected_generation: int | None = None,
        error_code: str | None = None,
        from_build: str | None = None,
        to_build: str | None = None,
        current_build: str | None = None,
        current_digest: str | None = None,
        available_build: str | None = None,
        available_digest: str | None = None,
    ) -> bool:
        """CAS-record a terminal install outcome and release its drain lease."""
        error_code = _bounded_code(error_code, "error_code")
        from_build = _bounded_text(from_build, "from_build")
        to_build = _bounded_text(to_build, "to_build")
        current_build = _bounded_text(current_build, "current_build")
        current_digest = _bounded_text(current_digest, "current_digest")
        available_build = _bounded_text(available_build, "available_build")
        available_digest = _bounded_text(available_digest, "available_digest")
        outcome_fingerprint = _outcome_fingerprint(
            expected_generation=expected_generation,
            result=result,
            error_code=error_code,
            from_build=from_build,
            to_build=to_build,
            current_build=current_build,
            current_digest=current_digest,
            available_build=available_build,
            available_digest=available_digest,
        )
        async with self._sessionmaker() as session:
            acknowledged = await SqlUpdateCoordinationRepository(session).acknowledge_outcome(
                token_hash=_token_hash(token),
                expected_generation=expected_generation,
                phase=_phase_for_result(result).value,
                result=result.value,
                error_code=error_code,
                from_build=from_build,
                to_build=to_build,
                current_build=current_build,
                current_digest=current_digest,
                outcome_fingerprint=outcome_fingerprint,
                now=self._now(),
            )
            await session.commit()
            return acknowledged

    def updater_available(
        self,
        snapshot: CoordinatorSnapshot,
        *,
        max_age: timedelta,
    ) -> bool:
        """Whether the last heartbeat is recent enough to call the sidecar live."""
        max_age = _positive_ttl(max_age)
        seen = snapshot.updater_last_seen_at
        if seen is None:
            return False
        age = self._now() - seen
        return timedelta(0) <= age <= max_age

    @asynccontextmanager
    async def critical_operation(
        self,
        operation: str,
        *,
        owner: str = "plex-manager",
        ttl: timedelta = _DEFAULT_CRITICAL_TTL,
        renew_every: timedelta | None = None,
    ) -> AsyncGenerator[LeaseGrant]:
        """Run work under a renewable lease without retaining a DB transaction."""
        ttl = _positive_ttl(ttl)
        interval = renew_every or (ttl / 3)
        interval = _positive_ttl(interval)
        if interval >= ttl:
            raise ValueError("renew_every must be shorter than ttl")

        # Composite critical flows (report -> purge -> re-grab) can cross an
        # updater claim between their outer and inner service calls. Reuse the
        # outer task's lease so an already-admitted operation drains to a safe
        # boundary instead of self-blocking halfway through. Context is inherited
        # by child tasks, so compare the actual task object too: independent
        # concurrent work must acquire its own durable lease.
        current_task = asyncio.current_task()
        held = self._held_critical.get()
        if held is not None and held.task is current_task:
            yield held.lease
            return

        lease = await self.acquire_critical(operation, owner=owner, ttl=ttl)
        if lease is None:
            raise MaintenanceDrainingError("automatic update maintenance is draining")

        held_token = self._held_critical.set(_HeldLease(lease=lease, task=current_task))
        stop = asyncio.Event()
        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_until_stopped(
                lease.token,
                ttl,
                interval,
                stop,
                lease_lost,
                current_task,
                lease.expires_at,
            )
        )
        body_failed = False
        lease_held = True
        try:
            yield lease
        except asyncio.CancelledError:
            body_failed = True
            if lease_lost.is_set():
                raise MaintenanceLeaseLostError(
                    "critical-operation lease expired before completion"
                ) from None
            raise
        except BaseException:
            body_failed = True
            raise
        finally:
            stop.set()
            try:
                lease_held = await renewal
                try:
                    await self.release(lease.token)
                except Exception:
                    # Fixed prose only: the ownership token must never enter the
                    # captured log stream. Expiry remains the durable cleanup.
                    _logger.exception("critical-operation lease release failed; lease will expire")
            finally:
                self._held_critical.reset(held_token)
            if not lease_held and not body_failed:
                raise MaintenanceLeaseLostError(
                    "critical-operation lease expired before completion"
                )

    async def _renew_until_stopped(
        self,
        token: str,
        ttl: timedelta,
        interval: timedelta,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
        owner_task: asyncio.Task[object] | None,
        known_expires_at: datetime,
    ) -> bool:
        delay = interval.total_seconds()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return True
            except TimeoutError:
                try:
                    if not await self.renew(token, ttl=ttl):
                        lease_lost.set()
                        if owner_task is not None:
                            owner_task.cancel()
                        return False
                    known_expires_at = self._now() + ttl
                    delay = interval.total_seconds()
                except Exception:
                    # The sidecar also needs this database to claim a drain, so a
                    # transient outage is fail-closed. Retry promptly; once the DB
                    # returns, the expiry predicate decides whether ownership held.
                    if self._now() >= known_expires_at:
                        lease_lost.set()
                        if owner_task is not None:
                            owner_task.cancel()
                        return False
                    delay = min(delay, 1.0)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("coordination clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _positive_ttl(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise ValueError("lease duration must be positive")
    return value


def _token_hash(token: str) -> str:
    if not token:
        raise ValueError("lease token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _outcome_fingerprint(
    *,
    expected_generation: int | None,
    result: UpdateResult,
    error_code: str | None,
    from_build: str | None,
    to_build: str | None,
    current_build: str | None,
    current_digest: str | None,
    available_build: str | None,
    available_digest: str | None,
) -> str:
    payload = [
        expected_generation,
        result.value,
        error_code,
        from_build,
        to_build,
        current_build,
        current_digest,
        available_build,
        available_digest,
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_code(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if _CODE_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded lowercase code")
    return value


# A 255-char image reference plus the fixed 72-char ``@sha256:<64 hex>``
# RepoDigest suffix is 327 chars; 400 leaves headroom without being unbounded.
# Kept in lockstep with ``UpdateOutcomeRequest`` max_length and the
# ``String(400)`` coordinator columns so a valid long private-registry digest
# never fails closed between the API edge and durable storage.
_BOUNDED_TEXT_MAX = 400


def _bounded_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    has_control = any(ord(character) < 32 or ord(character) == 127 for character in value)
    if not value or len(value) > _BOUNDED_TEXT_MAX or has_control:
        raise ValueError(f"{field} must be non-empty, bounded text without control characters")
    return value


def _phase_for_result(result: UpdateResult) -> UpdatePhase:
    if result is UpdateResult.update_available:
        return UpdatePhase.available
    if result in {UpdateResult.no_update, UpdateResult.cancelled}:
        return UpdatePhase.idle
    if result is UpdateResult.success:
        return UpdatePhase.succeeded
    if result is UpdateResult.rolled_back:
        return UpdatePhase.rolled_back
    return UpdatePhase.failed
