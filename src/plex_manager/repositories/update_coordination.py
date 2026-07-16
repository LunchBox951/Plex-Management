"""SQL persistence for updater state and database-backed maintenance leases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.update_recovery import (
    BUSY_COORDINATOR_PHASES as _DOMAIN_BUSY_COORDINATOR_PHASES,
)
from plex_manager.domain.update_recovery import (
    KNOWN_COORDINATOR_PHASES as _DOMAIN_KNOWN_COORDINATOR_PHASES,
)
from plex_manager.domain.update_recovery import (
    RecoveryAction,
    decide_recovery,
)
from plex_manager.models import MaintenanceLease, UpdateCoordinatorState

__all__ = [
    "CoordinatorRecoveryNotReadyError",
    "CoordinatorSnapshot",
    "DrainLeaseActiveError",
    "ForceResetResult",
    "LeaseRecord",
    "SqlUpdateCoordinationRepository",
    "UnknownCoordinatorPhaseError",
]

LeaseKind = Literal["critical", "drain"]

# Duplicated rather than imported: ``UpdatePhase`` lives in the service layer,
# and importing it here would invert the hexagonal repo -> service layering
# (ADR: domain/repositories never import an adapter/service). A drift-guard
# test (tests/services/test_update_coordination_service.py) asserts this set
# stays equal to ``frozenset(phase.value for phase in UpdatePhase)`` so the two
# copies cannot silently diverge.
_KNOWN_COORDINATOR_PHASES = _DOMAIN_KNOWN_COORDINATOR_PHASES
_BUSY_COORDINATOR_PHASES = _DOMAIN_BUSY_COORDINATOR_PHASES


class CoordinatorRecoveryNotReadyError(RuntimeError):
    """Raised when recovery evidence is not yet sufficient."""


class UnknownCoordinatorPhaseError(RuntimeError):
    """Raised when a locked write finds the row already in an unrecognized phase.

    A phase written by a concurrent process (a version-skew/rollback window,
    or a newer coordinator generation) between a caller's snapshot and this
    method's own locked transaction must never be silently consumed by an
    unconditional business-state rewrite -- see issue #322. Every locked
    coordination write re-checks the phase itself, inside its own lock, and
    fails closed instead of guessing what an unrecognized phase means.
    """


class DrainLeaseActiveError(RuntimeError):
    """Raised when a force-reset finds an UNEXPIRED drain lease under the wedge.

    An unrecognized phase paired with a live drain lease is exactly the
    version-skew shape where a NEWER updater generation legitimately moved into
    a phase this build does not know while holding its (unexpired) drain -- it
    may be mid-install right now. Tearing that lease and stamping ``idle``
    would admit new critical work and drain claims UNDERNEATH a live container
    replacement, so the reset refuses and the operator waits out the bounded
    lease TTL instead; expiry (``_cleanup_expired``) then clears the lease and
    a retry succeeds.
    """


@dataclass(frozen=True)
class ForceResetResult:
    """What a successful force-reset changed (for the caller's audit trail).

    ``old_phase`` is the pre-reset phase iff it was re-anchored to ``idle``;
    ``None`` for the ACTION-ONLY reset variant, where the phase was known and
    untouched. ``cleared_requested_action`` is the pre-reset
    ``requested_action`` iff it was unrecognized and normalized to ``"none"``;
    ``None`` when the stored action was known and preserved untouched. At least
    one field is always set on a successful reset.
    """

    old_phase: str | None
    cleared_requested_action: str | None
    old_action_generation: int | None
    new_action_generation: int | None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC) if value is not None else None


def _updater_heartbeat_fresh(last_seen: datetime | None, now: datetime, max_age: timedelta) -> bool:
    """Whether the sidecar's last heartbeat is recent enough to call it live.

    Mirrors ``UpdateCoordinationService.updater_available`` exactly (including
    treating a future timestamp -- clock skew -- as NOT live), so the
    force-reset checking predicate and the status endpoint's
    ``updater_available`` can never disagree about the same row.
    """
    seen = _as_utc(last_seen)
    if seen is None:
        return False
    age = now - seen
    return timedelta(0) <= age <= max_age


@dataclass(frozen=True)
class CoordinatorSnapshot:
    """Durable updater status plus the current lease summary."""

    requested_action: str
    action_generation: int
    acknowledged_generation: int
    phase: str
    current_build: str | None
    current_digest: str | None
    available_build: str | None
    available_digest: str | None
    updater_last_seen_at: datetime | None
    requested_at: datetime | None
    last_checked_at: datetime | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_operation: str | None
    last_result: str | None
    last_error_code: str | None
    last_from_build: str | None
    last_to_build: str | None
    active_critical_operations: int
    drain_owner: str | None
    drain_expires_at: datetime | None


@dataclass(frozen=True)
class LeaseRecord:
    """A stored lease without its plaintext ownership token."""

    id: int
    token_hash: str
    kind: LeaseKind
    owner: str
    operation: str | None
    action_generation: int | None
    created_at: datetime
    renewed_at: datetime
    expires_at: datetime


def _lease_record(row: MaintenanceLease) -> LeaseRecord:
    created_at = _as_utc(row.created_at)
    renewed_at = _as_utc(row.renewed_at)
    expires_at = _as_utc(row.expires_at)
    if created_at is None or renewed_at is None or expires_at is None:  # pragma: no cover
        raise ValueError("maintenance lease timestamps must not be null")
    if row.kind not in {"critical", "drain"}:  # pragma: no cover - service writes fixed values
        raise ValueError("unknown maintenance lease kind")
    return LeaseRecord(
        id=row.id,
        token_hash=row.token_hash,
        kind=cast(LeaseKind, row.kind),
        owner=row.owner,
        operation=row.operation,
        action_generation=row.action_generation,
        created_at=created_at,
        renewed_at=renewed_at,
        expires_at=expires_at,
    )


class SqlUpdateCoordinationRepository:
    """Serialize all lease decisions through the singleton coordinator row.

    The no-op ``UPDATE`` in :meth:`_lock` acquires the row lock on PostgreSQL and
    SQLite's single-writer lock. Every drain/critical claim uses it before
    cleaning expired rows and deciding, closing the check-then-insert race on
    both supported databases.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_state(self) -> UpdateCoordinatorState:
        row = await self._session.get(UpdateCoordinatorState, 1)
        if row is not None:
            return row

        nested = await self._session.begin_nested()
        row = UpdateCoordinatorState(id=1)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            await nested.rollback()
            existing = await self._session.get(UpdateCoordinatorState, 1)
            if existing is None:  # pragma: no cover - a conflicting singleton must exist
                raise
            return existing
        await nested.commit()
        return row

    async def _lock(self) -> UpdateCoordinatorState:
        await self.ensure_state()
        # Assigning id to itself changes no business state, but it is a real DML
        # statement and therefore serializes this transaction with every other
        # coordination decision even where SELECT FOR UPDATE is unsupported.
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(id=UpdateCoordinatorState.id)
        )
        row = await self._session.get(UpdateCoordinatorState, 1)
        if row is None:  # pragma: no cover - ensure + locked update guarantee it
            raise RuntimeError("update coordinator singleton disappeared")
        await self._session.refresh(row)
        return row

    async def _cleanup_expired(self, now: datetime) -> int:
        expired_drain = (
            await self._session.execute(
                select(MaintenanceLease.id).where(
                    MaintenanceLease.kind == "drain",
                    MaintenanceLease.expires_at <= now,
                )
            )
        ).first()
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                delete(MaintenanceLease).where(MaintenanceLease.expires_at <= now)
            ),
        )
        if expired_drain is not None:
            # Expiry is the crash-recovery release path. Preserve a pending
            # install action for retry, but never leave the app permanently
            # reporting/obeying a drain whose ownership has disappeared.
            await self._session.execute(
                update(UpdateCoordinatorState)
                .where(
                    UpdateCoordinatorState.id == 1,
                    UpdateCoordinatorState.phase.in_(("draining", "installing", "rollback")),
                )
                .values(phase="idle", last_started_at=None, updated_at=now)
            )
        return result.rowcount

    async def snapshot(self, now: datetime) -> CoordinatorSnapshot:
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        await self._backfill_legacy_busy_anchor(state, now)
        await self._session.refresh(state)
        critical_count = await self._critical_count()
        drain = await self._active_drain()
        return CoordinatorSnapshot(
            requested_action=state.requested_action,
            action_generation=state.action_generation,
            acknowledged_generation=state.acknowledged_generation,
            phase=state.phase,
            current_build=state.current_build,
            current_digest=state.current_digest,
            available_build=state.available_build,
            available_digest=state.available_digest,
            updater_last_seen_at=_as_utc(state.updater_last_seen_at),
            requested_at=_as_utc(state.requested_at),
            last_checked_at=_as_utc(state.last_checked_at),
            last_started_at=_as_utc(state.last_started_at),
            last_completed_at=_as_utc(state.last_completed_at),
            last_operation=state.last_operation,
            last_result=state.last_result,
            last_error_code=state.last_error_code,
            last_from_build=state.last_from_build,
            last_to_build=state.last_to_build,
            active_critical_operations=critical_count,
            drain_owner=drain.owner if drain is not None else None,
            drain_expires_at=_as_utc(drain.expires_at) if drain is not None else None,
        )

    async def _backfill_legacy_busy_anchor(
        self, state: UpdateCoordinatorState, now: datetime
    ) -> None:
        """Give pre-anchor busy rows one durable, fail-closed recovery clock."""
        if (
            state.phase not in _BUSY_COORDINATOR_PHASES
            or state.last_started_at is not None
            or state.requested_at is not None
        ):
            return
        heartbeat = _as_utc(state.updater_last_seen_at)
        anchor = heartbeat if heartbeat is not None and heartbeat <= now else now
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(
                UpdateCoordinatorState.id == 1,
                UpdateCoordinatorState.phase == state.phase,
                UpdateCoordinatorState.last_started_at.is_(None),
                UpdateCoordinatorState.requested_at.is_(None),
            )
            .values(last_started_at=anchor, updated_at=now)
        )

    @staticmethod
    def _phase_timestamp_values(
        prior_phase: str, resulting_phase: str, now: datetime
    ) -> dict[str, object]:
        """Return the age-anchor mutation for one exact persisted transition."""
        if resulting_phase not in _BUSY_COORDINATOR_PHASES:
            return {"last_started_at": None}
        if prior_phase != resulting_phase:
            return {"last_started_at": now}
        return {}

    async def touch_updater(
        self,
        *,
        now: datetime,
        phase: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Refresh sidecar liveness without replaying stale image observations."""
        state = await self._lock()
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        resulting_phase = phase if phase is not None else state.phase
        values: dict[str, object] = {"updater_last_seen_at": now, "updated_at": now}
        values.update(self._phase_timestamp_values(state.phase, resulting_phase, now))
        if phase is not None:
            values["phase"] = phase
        stmt = update(UpdateCoordinatorState).where(UpdateCoordinatorState.id == 1)
        if expected_generation is not None:
            stmt = stmt.where(UpdateCoordinatorState.action_generation == expected_generation)
        result = cast(
            CursorResult[Any],
            await self._session.execute(stmt.values(**values)),
        )
        return result.rowcount == 1

    async def request_action(self, action: str, now: datetime) -> int | None:
        state = await self._lock()
        # Fail closed on an unrecognized phase, exactly like every other locked
        # write in this module (issue #322). The busy-phase/requested-action
        # refusal below only reasons about phases THIS app version knows; a phase
        # a concurrent (newer) coordinator landed between a caller's snapshot and
        # this locked transaction is not in the hardcoded busy set and may still
        # carry ``requested_action == "none"``, so without this guard a stale
        # caller could bump ``action_generation``/``requested_action`` on top of
        # coordinator state it cannot interpret -- the same TOCTOU fail-open the
        # guard closes elsewhere. Refuse instead and let the service surface it.
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        # Refuse to clobber an update that is already in flight. Two windows both
        # strand in-flight work if we bump ``action_generation`` here:
        #   * an active lease phase - the sidecar is mid check/drain/install/
        #     rollback and carries the current generation on its lease; and
        #   * a public action already queued but not yet consumed by the sidecar
        #     (``requested_action != "none"``), e.g. an install awaiting its claim
        #     while the phase still reads ``available``, or a check awaiting its
        #     first poll while the phase still reads ``idle``.
        # In either window a later bump makes the eventual outcome fail its
        # generation CAS in ``acknowledge_action``/``acknowledge_outcome``: a
        # successfully replaced container can be left with an unacknowledgeable
        # updater state file, and a queued install can be silently downgraded to a
        # check. Reject instead; the service surfaces this as a 409 to the caller.
        if state.phase in _BUSY_COORDINATOR_PHASES:
            return None
        if state.requested_action != "none":
            return None
        generation = state.action_generation + 1
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(
                UpdateCoordinatorState.id == 1,
                UpdateCoordinatorState.action_generation == state.action_generation,
            )
            .values(
                requested_action=action,
                action_generation=generation,
                requested_at=now,
                updated_at=now,
            )
        )
        return generation

    async def acquire_critical(
        self,
        *,
        token_hash: str,
        owner: str,
        operation: str,
        now: datetime,
        ttl: timedelta,
    ) -> LeaseRecord | None:
        await self._lock()
        await self._cleanup_expired(now)
        if await self._active_drain() is not None:
            return None
        row = MaintenanceLease(
            token_hash=token_hash,
            kind="critical",
            owner=owner,
            operation=operation,
            created_at=now,
            renewed_at=now,
            expires_at=now + ttl,
        )
        self._session.add(row)
        await self._session.flush()
        return _lease_record(row)

    async def claim_drain(
        self,
        *,
        token_hash: str,
        owner: str,
        action_generation: int | None,
        materialize_install: bool,
        require_idle: bool,
        now: datetime,
        ttl: timedelta,
    ) -> tuple[LeaseRecord, bool] | None:
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        if await self._active_drain() is not None:
            return None
        generation = state.action_generation if action_generation is None else action_generation
        if action_generation is not None and generation != state.action_generation:
            return None
        if require_idle and await self._critical_count() != 0:
            return None
        if materialize_install:
            if state.requested_action != "none":
                return None
            generation = state.action_generation + 1
            await self._session.execute(
                update(UpdateCoordinatorState)
                .where(
                    UpdateCoordinatorState.id == 1,
                    UpdateCoordinatorState.action_generation == state.action_generation,
                    UpdateCoordinatorState.requested_action == "none",
                )
                .values(
                    requested_action="install",
                    action_generation=generation,
                    requested_at=now,
                    updated_at=now,
                )
            )
        row = MaintenanceLease(
            token_hash=token_hash,
            kind="drain",
            owner=owner,
            operation="container_update",
            action_generation=generation,
            created_at=now,
            renewed_at=now,
            expires_at=now + ttl,
        )
        self._session.add(row)
        await self._session.flush()
        values: dict[str, object] = {"phase": "draining", "updated_at": now}
        values.update(self._phase_timestamp_values(state.phase, "draining", now))
        await self._session.execute(
            update(UpdateCoordinatorState).where(UpdateCoordinatorState.id == 1).values(**values)
        )
        return _lease_record(row), (await self._critical_count()) == 0

    async def renew(
        self,
        token_hash: str,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        await self._lock()
        await self._cleanup_expired(now)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(MaintenanceLease)
                .where(
                    MaintenanceLease.token_hash == token_hash,
                    MaintenanceLease.expires_at > now,
                )
                .values(renewed_at=now, expires_at=now + ttl)
            ),
        )
        return result.rowcount == 1

    async def renew_drain_progress(
        self,
        token_hash: str,
        *,
        now: datetime,
        ttl: timedelta,
        phase: str | None,
    ) -> bool | None:
        """Renew one exact drain and atomically refresh its bounded active phase."""
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        drain = await self._lease_for_token(token_hash, "drain")
        if drain is None:
            return None
        ready = (await self._critical_count()) == 0
        drain.renewed_at = now
        drain.expires_at = now + ttl
        if phase is not None:
            resulting_phase = phase if ready else "draining"
        elif state.phase in {"draining", "installing", "rollback"}:
            resulting_phase = state.phase
        else:
            resulting_phase = "draining"
        values: dict[str, object] = {
            "phase": resulting_phase,
            "updater_last_seen_at": now,
            "updated_at": now,
        }
        values.update(self._phase_timestamp_values(state.phase, resulting_phase, now))
        await self._session.execute(
            update(UpdateCoordinatorState).where(UpdateCoordinatorState.id == 1).values(**values)
        )
        return ready

    async def release(self, token_hash: str, now: datetime) -> bool:
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        lease = await self._lease_for_token(token_hash)
        if lease is None:
            return False
        if lease.kind != "drain":
            # A critical release only deletes its own lease row and never touches
            # ``phase``. It must stay reliable even under an unrecognized phase
            # (issue #322): failing it closed here would leak the critical lease
            # until TTL and needlessly block idle-only update claims once the
            # phase recovers -- a fail-closed guard manufacturing a new failure
            # mode. Guard only the drain path below, which rewrites ``phase``.
            await self._session.delete(lease)
            return True
        # Drain release rewrites ``phase`` -> ``idle``. Refuse to stamp that on
        # top of a phase a concurrent writer left unrecognized, and leave the
        # drain lease untouched so a caller that understands the state can act.
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        await self._session.delete(lease)
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="idle", last_started_at=None, updated_at=now)
        )
        return True

    async def force_reset_phase(
        self,
        now: datetime,
        *,
        updater_heartbeat_max_age: timedelta,
        recovery_max_age: timedelta,
    ) -> ForceResetResult | None:
        """Apply the evidence-based recovery matrix under the coordinator lock."""
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        await self._backfill_legacy_busy_anchor(state, now)
        await self._session.refresh(state)
        drain = await self._active_drain()
        phase_started_at = None
        if state.phase in _BUSY_COORDINATOR_PHASES:
            phase_started_at = _as_utc(state.last_started_at) or _as_utc(state.requested_at)
        decision = decide_recovery(
            phase=state.phase,
            requested_action=state.requested_action,
            updater_heartbeat_fresh=_updater_heartbeat_fresh(
                state.updater_last_seen_at, now, updater_heartbeat_max_age
            ),
            live_drain=drain is not None,
            phase_started_at=phase_started_at,
            now=now,
            max_age=recovery_max_age,
        )
        if decision.action is RecoveryAction.LIVE_DRAIN:
            raise DrainLeaseActiveError(state.phase)
        if decision.action is RecoveryAction.WAIT:
            raise CoordinatorRecoveryNotReadyError(decision.reason)
        if decision.action is RecoveryAction.NOOP:
            return None
        old_generation: int | None = None
        new_generation: int | None = None
        values: dict[str, object] = {"updated_at": now}
        old_phase: str | None = None
        cleared_action: str | None = None
        if decision.action is RecoveryAction.REANCHOR:
            old_phase = state.phase
            values.update(phase="idle", last_started_at=None)
        if decision.clear_unknown_action or (
            decision.action is RecoveryAction.REANCHOR
            and state.phase in _BUSY_COORDINATOR_PHASES
            and state.requested_action == "none"
        ):
            if decision.clear_unknown_action:
                cleared_action = state.requested_action
            old_generation = state.action_generation
            new_generation = old_generation + 1
            values.update(action_generation=new_generation)
            if decision.clear_unknown_action:
                values["requested_action"] = "none"
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(
                UpdateCoordinatorState.id == 1,
                UpdateCoordinatorState.action_generation == state.action_generation,
            )
            .values(**values)
        )
        return ForceResetResult(
            old_phase=old_phase,
            cleared_requested_action=cleared_action,
            old_action_generation=old_generation,
            new_action_generation=new_generation,
        )

    async def acknowledge_action(
        self,
        *,
        expected_generation: int,
        phase: str,
        result: str,
        error_code: str | None,
        image_values: Mapping[str, str | None],
        preserve_action: bool,
        now: datetime,
    ) -> bool:
        state = await self._lock()
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        if state.action_generation != expected_generation:
            return False
        values: dict[str, object] = {
            "phase": phase,
            "last_operation": "check",
            "last_result": result,
            "last_error_code": error_code,
            "last_from_build": None,
            "last_to_build": None,
            "last_outcome_token_hash": None,
            "last_outcome_fingerprint": None,
            "last_completed_at": now,
            "last_checked_at": now,
            "updater_last_seen_at": now,
            "updated_at": now,
        }
        values.update(self._phase_timestamp_values(state.phase, phase, now))
        if not preserve_action:
            values["requested_action"] = "none"
            values["acknowledged_generation"] = expected_generation
        # A check outcome has no drain lease but is still authoritative for the
        # digest/build observation it just completed. ``None`` deliberately clears
        # an old available image after an unchanged-digest result.
        values.update(image_values)
        await self._session.execute(
            update(UpdateCoordinatorState)
            .where(
                UpdateCoordinatorState.id == 1,
                UpdateCoordinatorState.action_generation == expected_generation,
            )
            .values(**values)
        )
        return True

    async def acknowledge_outcome(
        self,
        *,
        token_hash: str,
        expected_generation: int | None,
        phase: str,
        result: str,
        error_code: str | None,
        from_build: str | None,
        to_build: str | None,
        current_build: str | None,
        current_digest: str | None,
        outcome_fingerprint: str,
        now: datetime,
    ) -> bool:
        state = await self._lock()
        await self._cleanup_expired(now)
        await self._session.refresh(state)
        if state.phase not in _KNOWN_COORDINATOR_PHASES:
            raise UnknownCoordinatorPhaseError(state.phase)
        drain = await self._lease_for_token(token_hash, "drain")
        if drain is None:
            # The acknowledgement transaction may have committed while its HTTP
            # response was lost during container cutover. Accept only the exact
            # token+outcome receipt; a changed retry remains a conflict.
            generation_matches = (
                expected_generation is None or state.acknowledged_generation == expected_generation
            )
            return (
                generation_matches
                and state.last_outcome_token_hash == token_hash
                and state.last_outcome_fingerprint == outcome_fingerprint
                and state.last_result == result
            )
        if expected_generation is not None and (
            drain.action_generation != expected_generation
            or state.action_generation != expected_generation
        ):
            return False

        generation = drain.action_generation
        values: dict[str, object] = {
            "phase": phase,
            "last_operation": "install",
            "last_result": result,
            "last_error_code": error_code,
            "last_from_build": from_build,
            "last_to_build": to_build,
            "last_outcome_token_hash": token_hash,
            "last_outcome_fingerprint": outcome_fingerprint,
            "last_completed_at": now,
            "available_build": None,
            "available_digest": None,
            "updater_last_seen_at": now,
            "updated_at": now,
        }
        values.update(self._phase_timestamp_values(state.phase, phase, now))
        if current_build is not None:
            values["current_build"] = current_build
        if current_digest is not None:
            values["current_digest"] = current_digest
        if generation is not None and generation == state.action_generation:
            values["requested_action"] = "none"
            values["acknowledged_generation"] = generation
        await self._session.execute(
            update(UpdateCoordinatorState).where(UpdateCoordinatorState.id == 1).values(**values)
        )
        await self._session.delete(drain)
        return True

    async def _active_drain(self) -> MaintenanceLease | None:
        result = await self._session.execute(
            select(MaintenanceLease).where(MaintenanceLease.kind == "drain").limit(1)
        )
        return result.scalars().first()

    async def _critical_count(self) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(MaintenanceLease)
            .where(MaintenanceLease.kind == "critical")
        )
        return int(result.scalar_one())

    async def _lease_for_token(
        self,
        token_hash: str,
        kind: LeaseKind | None = None,
    ) -> MaintenanceLease | None:
        stmt = select(MaintenanceLease).where(MaintenanceLease.token_hash == token_hash)
        if kind is not None:
            stmt = stmt.where(MaintenanceLease.kind == kind)
        result = await self._session.execute(stmt.limit(1))
        return result.scalars().first()
