"""The periodic share-revalidation sweep (issue #391 PR-2).

Covers the three things the sweep must never get wrong: WHO it picks (only
signed-in users, only when their verdict has aged out, never more than the
per-tick budget), WHAT each verdict does (and, for UNKNOWN, does not do), and
whether every automatic sign-out leaves an auditable trail. ``check_share``'s
own ladder is covered by ``test_plex_access_service.py``; these tests drive it
through real ``httpx.MockTransport`` responses so the two stay wired together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.models import AuditLog, AuthSession, User
from plex_manager.services import plex_access_service
from plex_manager.services.plex_access_service import (
    EntitlementSnapshot,
    ShareVerdict,
    apply_share_verdict,
    count_due_share_checks,
    list_due_share_checks,
    sweep_shares,
)

SessionMaker = async_sessionmaker[AsyncSession]

_MACHINE_ID = "configured-server-machine-id"
_INTERVAL = timedelta(hours=6)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _server_resource(machine_id: str) -> dict[str, object]:
    return {
        "name": "Living Room",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


def _resources_transport(payload: list[dict[str, object]] | int) -> httpx.MockTransport:
    """plex.tv ``/api/v2/resources``; an int answers that status instead."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(payload, int):
            return httpx.Response(payload, json={})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def _add_user(
    sessionmaker: SessionMaker,
    *,
    username: str = "viewer",
    token: str | None = "user-token",  # noqa: S107
    with_session: bool = True,
    session_revoked: bool = False,
    session_expired: bool = False,
    session_idle: bool = False,
    share_checked_at: datetime | None = None,
    share_state: str | None = None,
    entitled_section_keys: list[str] | None = None,
) -> int:
    now = datetime.now(UTC)
    expires_at = now - timedelta(days=1) if session_expired else now + timedelta(days=1)
    last_seen_at = now - timedelta(days=30) if session_idle else now
    async with sessionmaker() as session:
        user = User(
            username=username,
            encrypted_plex_token=token,
            share_checked_at=share_checked_at,
            share_state=share_state,
            entitled_section_keys=entitled_section_keys,
            entitlements_machine_id=_MACHINE_ID if entitled_section_keys is not None else None,
        )
        session.add(user)
        await session.flush()
        if with_session:
            session.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=f"hash-{username}",
                    created_at=now - timedelta(minutes=5),
                    expires_at=expires_at,
                    revoked_at=now if session_revoked else None,
                    last_seen_at=last_seen_at,
                )
            )
        await session.commit()
        return user.id


async def _load(sessionmaker: SessionMaker, user_id: int) -> User:
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


async def _audit_rows(sessionmaker: SessionMaker) -> list[AuditLog]:
    async with sessionmaker() as session:
        return list((await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all())


async def _live_session_count(sessionmaker: SessionMaker, user_id: int) -> int:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
                )
            )
        ).scalars()
        return len(list(rows))


async def _sweep(
    sessionmaker: SessionMaker,
    payload: list[dict[str, object]] | int,
    *,
    limit: int = plex_access_service.SHARE_SWEEP_USER_BUDGET,
) -> plex_access_service.ShareSweepResult:
    async with httpx.AsyncClient(transport=_resources_transport(payload)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        return await sweep_shares(
            sessionmaker,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            limit=limit,
        )


# --------------------------------------------------------------------------- #
# Due selection: who the sweep is allowed to spend a plex.tv call on
# --------------------------------------------------------------------------- #
async def test_never_checked_user_with_a_live_session_is_due(sessionmaker_: SessionMaker) -> None:
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        due = await list_due_share_checks(
            session, now=datetime.now(UTC), revalidate_after=_INTERVAL, limit=20
        )
    assert [user.id for user in due] == [user_id]


@pytest.mark.parametrize(
    "unusable",
    ["absent", "revoked", "expired", "idled_out"],
)
async def test_user_without_a_live_session_is_never_due(
    sessionmaker_: SessionMaker, unusable: str
) -> None:
    """The sweep's cost is bounded by who is SIGNED IN, using the exact
    active-session predicate the admin sessions list uses. A user with no usable
    session has nothing to revoke, so spending a plex.tv call on them is pure
    waste."""
    await _add_user(
        sessionmaker_,
        with_session=unusable != "absent",
        session_revoked=unusable == "revoked",
        session_expired=unusable == "expired",
        session_idle=unusable == "idled_out",
    )
    async with sessionmaker_() as session:
        due = await list_due_share_checks(
            session, now=datetime.now(UTC), revalidate_after=_INTERVAL, limit=20
        )
        assert due == []
        assert (
            await count_due_share_checks(session, now=datetime.now(UTC), revalidate_after=_INTERVAL)
            == 0
        )


async def test_recently_checked_user_is_not_due_until_the_interval_elapses(
    sessionmaker_: SessionMaker,
) -> None:
    now = datetime.now(UTC)
    fresh = await _add_user(
        sessionmaker_, username="fresh", share_checked_at=now - timedelta(hours=1)
    )
    stale = await _add_user(
        sessionmaker_, username="stale", share_checked_at=now - timedelta(hours=7)
    )
    async with sessionmaker_() as session:
        due = await list_due_share_checks(session, now=now, revalidate_after=_INTERVAL, limit=20)
    assert [user.id for user in due] == [stale]
    assert fresh not in [user.id for user in due]


async def test_recovery_session_never_makes_a_user_due(sessionmaker_: SessionMaker) -> None:
    """A recovery (``X-Api-Key``-exchange) session has no ``user_id``, so it can
    never satisfy the join -- the break-glass credential is structurally outside
    this sweep."""
    await _add_user(sessionmaker_, with_session=False)
    async with sessionmaker_() as session:
        session.add(
            AuthSession(
                user_id=None,
                token_hash="recovery-hash",  # noqa: S106 - a digest, not a credential
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    async with sessionmaker_() as session:
        due = await list_due_share_checks(
            session, now=datetime.now(UTC), revalidate_after=_INTERVAL, limit=20
        )
    assert due == []


async def test_never_checked_users_are_ordered_ahead_of_previously_checked_ones(
    sessionmaker_: SessionMaker,
) -> None:
    """NULLS FIRST is stated explicitly because PostgreSQL sorts NULLs LAST on
    ASC; without it, a user who has never been checked could sit behind everyone
    forever on one of the two supported backends."""
    now = datetime.now(UTC)
    checked = await _add_user(
        sessionmaker_, username="checked", share_checked_at=now - timedelta(hours=7)
    )
    never = await _add_user(sessionmaker_, username="never")
    async with sessionmaker_() as session:
        due = await list_due_share_checks(session, now=now, revalidate_after=_INTERVAL, limit=20)
    assert [user.id for user in due] == [never, checked]


async def test_budget_caps_the_tick_and_the_backlog_is_reported_not_dropped(
    sessionmaker_: SessionMaker,
) -> None:
    for index in range(5):
        await _add_user(sessionmaker_, username=f"viewer{index}")
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], limit=2)
    assert result.checked == 2
    assert result.authorized == 2
    # The other three are not lost -- they are visibly still owed a check, which
    # is what tells an operator the backlog is draining slower than it grows.
    assert result.due_remaining == 3


# --------------------------------------------------------------------------- #
# Verdict -> action table
# --------------------------------------------------------------------------- #
async def test_authorized_stamps_state_and_clears_the_failure_counter(
    sessionmaker_: SessionMaker,
) -> None:
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.share_check_failures = 3
        user.share_check_failed_at = datetime.now(UTC)
        await session.commit()

    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)])

    assert result.authorized == 1
    assert result.sessions_revoked == 0
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.share_checked_at is not None
    assert user.share_check_failures == 0
    assert user.share_check_failed_at is None
    assert await _live_session_count(sessionmaker_, user_id) == 1
    assert await _audit_rows(sessionmaker_) == []


async def test_authorized_does_not_capture_section_entitlements(
    sessionmaker_: SessionMaker,
) -> None:
    """Section capture is #484 scope (PR-3). This PR calls ``check_share`` for
    the VERDICT only, so the tri-state ``NULL`` = never-captured must survive an
    authorized sweep untouched."""
    user_id = await _add_user(sessionmaker_)
    await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)])
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None


async def test_share_revoked_signs_the_user_out_and_writes_an_audit_row(
    sessionmaker_: SessionMaker,
) -> None:
    user_id = await _add_user(
        sessionmaker_, share_state="authorized", entitled_section_keys=["1", "2"]
    )

    # plex.tv answers authoritatively with a resource list that no longer holds
    # the configured server: the share is confirmed gone.
    result = await _sweep(sessionmaker_, [_server_resource("some-other-server")])

    assert result.share_revoked == 1
    assert result.sessions_revoked == 1
    assert result.signed_out_user_ids == (user_id,)
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "share_revoked"
    assert user.share_checked_at is not None
    assert await _live_session_count(sessionmaker_, user_id) == 0
    # A confirmed revoke invalidates the captured section snapshot: back to the
    # tri-state NULL ("never captured"), never [] ("captured, entitled to none").
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None

    rows = await _audit_rows(sessionmaker_)
    assert len(rows) == 1
    assert rows[0].action_type == "user.share_revoked"
    assert rows[0].entity_type == "user"
    assert rows[0].entity_id == user_id
    assert rows[0].user_id is None  # automatic: no human actor
    assert rows[0].old_value == {"share_state": "authorized"}
    assert rows[0].new_value == {"share_state": "share_revoked", "sessions_revoked": 1}
    assert rows[0].description is not None
    assert "no longer has access" in rows[0].description


async def test_token_stale_also_signs_out_but_is_labeled_distinctly(
    sessionmaker_: SessionMaker,
) -> None:
    """Ratified on #391: a dead credential means we can no longer verify the
    user, so it signs them out with the SAME machinery -- but the operator- and
    user-facing wording must say the Plex sign-in expired, never that access was
    removed. And entitlements are retained: nothing disproved them."""
    user_id = await _add_user(sessionmaker_, entitled_section_keys=["1"])

    result = await _sweep(sessionmaker_, 401)

    assert result.token_stale == 1
    assert result.share_revoked == 0
    assert result.signed_out_user_ids == (user_id,)
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "token_stale"
    assert await _live_session_count(sessionmaker_, user_id) == 0
    assert user.entitled_section_keys == ["1"]
    assert user.entitlements_machine_id == _MACHINE_ID

    rows = await _audit_rows(sessionmaker_)
    assert len(rows) == 1
    assert rows[0].action_type == "user.plex_sign_in_expired"
    assert rows[0].description is not None
    assert "sign-in expired" in rows[0].description
    assert "not removed" in rows[0].description


async def test_unknown_never_revokes_and_only_moves_the_failure_counters(
    sessionmaker_: SessionMaker,
) -> None:
    """North star #3: a plex.tv outage is a DEGRADED sweep, never a revocation.
    ``share_state``/``share_checked_at`` are deliberately untouched so the last
    real verdict survives and the user stays due for a prompt retry."""
    checked_at = datetime.now(UTC) - timedelta(hours=9)
    user_id = await _add_user(sessionmaker_, share_state="authorized", share_checked_at=checked_at)

    result = await _sweep(sessionmaker_, 500)

    assert result.unknown == 1
    assert result.share_revoked == 0
    assert result.token_stale == 0
    assert result.sessions_revoked == 0
    assert result.signed_out_user_ids == ()
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.share_checked_at is not None
    # Unchanged: an outage must not advance the "we confirmed this" timestamp.
    assert abs(user.share_checked_at.replace(tzinfo=UTC) - checked_at) < timedelta(seconds=1)
    assert user.share_check_failures == 1
    assert user.share_check_failed_at is not None
    assert await _live_session_count(sessionmaker_, user_id) == 1
    assert await _audit_rows(sessionmaker_) == []


async def test_repeated_unknown_verdicts_accumulate_the_failure_counter(
    sessionmaker_: SessionMaker,
) -> None:
    user_id = await _add_user(sessionmaker_)
    await _sweep(sessionmaker_, 500)
    await _sweep(sessionmaker_, 500)
    user = await _load(sessionmaker_, user_id)
    assert user.share_check_failures == 2
    # Still due, precisely because share_checked_at was never stamped.
    async with sessionmaker_() as session:
        due = await count_due_share_checks(
            session, now=datetime.now(UTC), revalidate_after=_INTERVAL
        )
    assert due == 1


async def test_unverifiable_stamps_state_without_revoking(sessionmaker_: SessionMaker) -> None:
    user_id = await _add_user(sessionmaker_, token=None)
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)])
    assert result.unverifiable == 1
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "unverifiable"
    assert user.share_checked_at is not None
    assert await _live_session_count(sessionmaker_, user_id) == 1
    assert await _audit_rows(sessionmaker_) == []


# --------------------------------------------------------------------------- #
# Races and isolation
# --------------------------------------------------------------------------- #
async def test_a_user_who_signed_in_again_mid_sweep_is_not_signed_straight_back_out(
    sessionmaker_: SessionMaker,
) -> None:
    """The verdict was computed against the token read at selection time; a NEW
    token is a credential the verdict says nothing about. Acting on it anyway
    would sign a user out seconds after a successful sign-in."""
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        outcome = await apply_share_verdict(
            session,
            user,
            EntitlementSnapshot(
                verdict=ShareVerdict.SHARE_REVOKED,
                section_keys=None,
                machine_identifier=_MACHINE_ID,
            ),
            expected_token="a-token-from-before-they-signed-in-again",  # noqa: S106
            now=datetime.now(UTC),
        )
        await session.commit()

    assert outcome.applied is False
    assert outcome.signed_out is False
    user = await _load(sessionmaker_, user_id)
    assert user.share_state is None
    assert await _live_session_count(sessionmaker_, user_id) == 1
    assert await _audit_rows(sessionmaker_) == []


async def test_one_users_verdict_does_not_stop_the_others(sessionmaker_: SessionMaker) -> None:
    """Per-user isolation: a revoke for one account and an authorization for
    another are applied in separate transactions, so neither can undo the
    other."""
    revoked_id = await _add_user(sessionmaker_, username="gone")
    kept_id = await _add_user(sessionmaker_, username="kept")

    def handler(request: httpx.Request) -> httpx.Response:
        # The stored tokens differ per user, so answer per credential.
        token = request.headers.get("X-Plex-Token", "")
        if token.endswith("gone"):
            return httpx.Response(200, json=[_server_resource("some-other-server")])
        return httpx.Response(200, json=[_server_resource(_MACHINE_ID)])

    async with sessionmaker_() as session:
        for user_id, suffix in ((revoked_id, "gone"), (kept_id, "kept")):
            user = await session.get(User, user_id)
            assert user is not None
            user.encrypted_plex_token = f"token-{suffix}"
        await session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await sweep_shares(sessionmaker_, plex_tv, _MACHINE_ID, revalidate_after=_INTERVAL)

    assert result.checked == 2
    assert result.share_revoked == 1
    assert result.authorized == 1
    assert result.signed_out_user_ids == (revoked_id,)
    assert await _live_session_count(sessionmaker_, revoked_id) == 0
    assert await _live_session_count(sessionmaker_, kept_id) == 1


# --------------------------------------------------------------------------- #
# Status surface
# --------------------------------------------------------------------------- #
def test_status_reports_degraded_when_a_verdict_could_not_be_determined() -> None:
    status = plex_access_service.ShareSweepStatus()
    status.mark_started()
    status.mark_completed(plex_access_service.ShareSweepResult(checked=1, unknown=1))
    assert status.state == "degraded"
    assert status.unknown == 1
    # A tick that could not answer for someone has NOT succeeded.
    assert status.last_ok_at is None


def test_status_reports_ok_for_a_sweep_that_revoked_someone() -> None:
    """A confirmed revocation is the sweep WORKING, not failing -- it must not
    be reported as degraded or an operator learns to ignore the state."""
    status = plex_access_service.ShareSweepStatus()
    status.mark_completed(
        plex_access_service.ShareSweepResult(
            checked=2, authorized=1, share_revoked=1, sessions_revoked=1
        )
    )
    assert status.state == "ok"
    assert status.last_ok_at is not None
    assert status.share_revoked == 1
    assert status.sessions_revoked == 1
