"""Session-lifecycle policy: idle window, admin revoke, and the dead-row sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import AuthSession, User
from plex_manager.services import session_lifecycle as sl

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


async def _make_user(session: AsyncSession, *, plex_id: int, is_admin: bool = False) -> User:
    user = User(plex_id=plex_id, username=f"user{plex_id}", permissions=1 if is_admin else 0)
    session.add(user)
    await session.flush()
    return user


def _session(
    *,
    user_id: int | None,
    tag: str,
    created_at: datetime,
    expires_at: datetime,
    last_seen_at: datetime | None,
    revoked_at: datetime | None = None,
) -> AuthSession:
    return AuthSession(
        user_id=user_id,
        token_hash=tag,
        created_at=created_at,
        expires_at=expires_at,
        last_seen_at=last_seen_at,
        revoked_at=revoked_at,
    )


# --------------------------------------------------------------------------- #
# Idle window
# --------------------------------------------------------------------------- #
def test_fresh_session_is_not_idle_expired() -> None:
    row = _session(
        user_id=1,
        tag="a",
        created_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
        last_seen_at=_NOW,
    )
    assert sl.session_is_idle_expired(row, now=_NOW) is False


def test_session_idle_past_the_window_is_expired() -> None:
    seen = _NOW - sl.SESSION_IDLE_WINDOW - timedelta(minutes=1)
    row = _session(
        user_id=1,
        tag="a",
        created_at=seen,
        expires_at=_NOW + timedelta(days=20),  # absolute cap NOT reached
        last_seen_at=seen,
    )
    assert sl.session_is_idle_expired(row, now=_NOW) is True


def test_null_last_seen_falls_back_to_created_at() -> None:
    created = _NOW - sl.SESSION_IDLE_WINDOW - timedelta(hours=1)
    row = _session(
        user_id=1,
        tag="a",
        created_at=created,
        expires_at=_NOW + timedelta(days=20),
        last_seen_at=None,
    )
    assert sl.session_effective_last_seen(row) == created
    assert sl.session_is_idle_expired(row, now=_NOW) is True


# --------------------------------------------------------------------------- #
# Admin revoke
# --------------------------------------------------------------------------- #
async def test_revoke_user_sessions_revokes_only_active_rows(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=42)
        other = await _make_user(session, plex_id=99)
        session.add_all(
            [
                _session(
                    user_id=user.id,
                    tag="live-1",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                _session(
                    user_id=user.id,
                    tag="live-2",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                _session(
                    user_id=user.id,
                    tag="already-revoked",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                    revoked_at=_NOW,
                ),
                _session(
                    user_id=other.id,
                    tag="other-user",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
            ]
        )
        await session.commit()

        revoked = await sl.revoke_user_sessions(session, user.id, now=_NOW)
        await session.commit()
        assert revoked == 2

        # The other user's session is untouched.
        remaining = await session.execute(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == other.id, AuthSession.revoked_at.is_(None))
        )
        assert remaining.scalar_one() == 1

        # Re-revoking is a harmless no-op.
        assert await sl.revoke_user_sessions(session, user.id, now=_NOW) == 0


async def test_count_only_active_still_stamps_dead_rows_but_does_not_count_them(
    sessionmaker_: SessionMaker,
) -> None:
    """A revoke that only swept up already-dead rows ended nothing the user could
    have used. The stamp stays wide (no ambiguous "unrevoked but dead" rows left
    behind), but the REPORTED count must not inflate that into a sign-out."""
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=1234)
        session.add_all(
            [
                _session(
                    user_id=user.id,
                    tag="live",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                _session(  # past the absolute cap
                    user_id=user.id,
                    tag="expired",
                    created_at=_NOW - timedelta(days=60),
                    expires_at=_NOW - timedelta(days=1),
                    last_seen_at=_NOW - timedelta(days=40),
                ),
                _session(  # inside the cap but idled out
                    user_id=user.id,
                    tag="idle",
                    created_at=_NOW - timedelta(days=30),
                    expires_at=_NOW + timedelta(days=1),
                    last_seen_at=_NOW - sl.SESSION_IDLE_WINDOW - timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

        counted = await sl.revoke_user_sessions(session, user.id, now=_NOW, count_only_active=True)
        await session.commit()

        # Only the live one is counted...
        assert counted == 1
        # ...but all three were stamped, leaving nothing unrevoked-but-dead.
        unrevoked = await session.execute(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        )
        assert unrevoked.scalar_one() == 0


async def test_count_only_active_reports_zero_when_every_row_was_already_dead(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=4321)
        session.add(
            _session(
                user_id=user.id,
                tag="idle-only",
                created_at=_NOW - timedelta(days=30),
                expires_at=_NOW + timedelta(days=1),
                last_seen_at=_NOW - sl.SESSION_IDLE_WINDOW - timedelta(hours=1),
            )
        )
        await session.commit()

        counted = await sl.revoke_user_sessions(session, user.id, now=_NOW, count_only_active=True)
        await session.commit()

        # A row WAS stamped, but nothing usable ended.
        assert counted == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            )
        ) == 0


async def test_count_only_active_reports_zero_when_the_user_guard_rejects_the_revoke(
    sessionmaker_: SessionMaker,
) -> None:
    """The guard is a property of the USER row, so the UPDATE is all-or-nothing:
    a rejected revoke must report 0, not what it would have cut."""
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=5678)
        session.add(
            _session(
                user_id=user.id,
                tag="live",
                created_at=_NOW,
                expires_at=_NOW + timedelta(days=30),
                last_seen_at=_NOW,
            )
        )
        await session.commit()

        counted = await sl.revoke_user_sessions(
            session,
            user.id,
            now=_NOW,
            only_if_user_matches=User.plex_id == 999_999,  # never true
            count_only_active=True,
        )
        await session.commit()

        assert counted == 0
        # Nothing was stamped either — the session survives untouched.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            )
        ) == 1


async def test_revoke_recovery_sessions_targets_only_null_user_rows(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=42)
        session.add_all(
            [
                # Two live recovery sessions (no Plex identity) — both revoked.
                _session(
                    user_id=None,
                    tag="recovery-1",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                _session(
                    user_id=None,
                    tag="recovery-2",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                # An already-revoked recovery session — untouched.
                _session(
                    user_id=None,
                    tag="recovery-dead",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                    revoked_at=_NOW,
                ),
                # A Plex-user session — must NOT be revoked.
                _session(
                    user_id=user.id,
                    tag="user-live",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
            ]
        )
        await session.commit()

        revoked = await sl.revoke_recovery_sessions(session, now=_NOW)
        await session.commit()
        assert revoked == 2

        # The Plex-user session is untouched.
        remaining = await session.execute(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        )
        assert remaining.scalar_one() == 1

        # Re-revoking is a harmless no-op.
        assert await sl.revoke_recovery_sessions(session, now=_NOW) == 0


# --------------------------------------------------------------------------- #
# Dead-row sweep
# --------------------------------------------------------------------------- #
async def test_sweep_keeps_live_and_recently_dead_reaps_old_dead(
    sessionmaker_: SessionMaker,
) -> None:
    grace = sl.SESSION_SWEEP_RETENTION
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=7)
        session.add_all(
            [
                # Live — never swept.
                _session(
                    user_id=user.id,
                    tag="live",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                ),
                # Revoked just now — within the retention grace, kept for audit.
                _session(
                    user_id=user.id,
                    tag="recently-revoked",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=30),
                    last_seen_at=_NOW,
                    revoked_at=_NOW - timedelta(hours=1),
                ),
                # Revoked long ago — reaped.
                _session(
                    user_id=user.id,
                    tag="old-revoked",
                    created_at=_NOW - timedelta(days=40),
                    expires_at=_NOW + timedelta(days=1),
                    last_seen_at=_NOW - timedelta(days=40),
                    revoked_at=_NOW - grace - timedelta(days=1),
                ),
                # Expired past its absolute cap, beyond grace — reaped.
                _session(
                    user_id=user.id,
                    tag="old-expired",
                    created_at=_NOW - timedelta(days=60),
                    expires_at=_NOW - grace - timedelta(days=1),
                    last_seen_at=_NOW - timedelta(days=60),
                ),
                # Hit its absolute cap recently while still active (seen 2h ago) —
                # within the retention grace on every axis, kept a while for audit.
                _session(
                    user_id=user.id,
                    tag="recently-expired",
                    created_at=_NOW - timedelta(days=30),
                    expires_at=_NOW - timedelta(hours=2),
                    last_seen_at=_NOW - timedelta(hours=2),
                ),
                # Idled out long ago (absolute cap still future) — reaped.
                _session(
                    user_id=user.id,
                    tag="old-idle",
                    created_at=_NOW - timedelta(days=30),
                    expires_at=_NOW + timedelta(days=1),
                    last_seen_at=_NOW - sl.SESSION_IDLE_WINDOW - grace - timedelta(days=1),
                ),
            ]
        )
        await session.commit()

        deleted = await sl.sweep_dead_sessions(session, now=_NOW)
        assert deleted == 3

        survivors = await session.execute(
            select(AuthSession.token_hash).order_by(AuthSession.token_hash)
        )
        assert set(survivors.scalars().all()) == {
            "live",
            "recently-revoked",
            "recently-expired",
        }


async def test_sweep_keeps_freshly_revoked_row_despite_stale_idle_signal(
    sessionmaker_: SessionMaker,
) -> None:
    """A long-idle session revoked TODAY must survive its own retention grace.

    Regression for issue #328: the idle/revoked predicates were independently
    OR'd, so a session idle well past :data:`SESSION_IDLE_WINDOW` +
    :data:`SESSION_SWEEP_RETENTION` (satisfying the idle branch on its own)
    that gets revoked today satisfied the sweep's delete clause on the very
    next pass, destroying the fresh revocation's audit record instead of
    honoring the 7-day post-revocation grace.
    """
    grace = sl.SESSION_SWEEP_RETENTION
    stale_last_seen = _NOW - sl.SESSION_IDLE_WINDOW - grace - timedelta(days=10)
    async with sessionmaker_() as session:
        user = await _make_user(session, plex_id=9)
        session.add(
            _session(
                user_id=user.id,
                tag="stale-idle-fresh-revoke",
                created_at=stale_last_seen,
                expires_at=_NOW + timedelta(days=30),
                last_seen_at=stale_last_seen,
                revoked_at=_NOW - timedelta(hours=1),
            )
        )
        await session.commit()

        # Same-day sweep: the revocation is fresh, so the row must survive even
        # though the idle signal alone would already justify deletion.
        deleted = await sl.sweep_dead_sessions(session, now=_NOW)
        assert deleted == 0
        survivors = await session.execute(select(func.count()).select_from(AuthSession))
        assert survivors.scalar_one() == 1

        # Once the revocation itself passes its retention cutoff, it's reaped.
        later = _NOW + grace + timedelta(minutes=1)
        deleted = await sl.sweep_dead_sessions(session, now=later)
        assert deleted == 1
        survivors = await session.execute(select(func.count()).select_from(AuthSession))
        assert survivors.scalar_one() == 0


async def test_sweep_on_empty_table_is_zero(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        assert await sl.sweep_dead_sessions(session, now=_NOW) == 0
