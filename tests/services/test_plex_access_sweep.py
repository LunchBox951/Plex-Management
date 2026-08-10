"""The periodic share-revalidation sweep (issue #391 PR-2).

Covers the three things the sweep must never get wrong: WHO it picks (only
signed-in users, only when their verdict has aged out, never more than the
per-tick budget), WHAT each verdict does (and, for UNKNOWN, does not do), and
whether every automatic sign-out leaves an auditable trail. ``check_share``'s
own ladder is covered by ``test_plex_access_service.py``; these tests drive it
through real ``httpx.MockTransport`` responses so the two stay wired together.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibrary, reset_caches
from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.models import AuditLog, AuthSession, Setting, User
from plex_manager.services import plex_access_service, session_lifecycle
from plex_manager.services.plex_access_service import (
    AnchorCheck,
    EntitlementSnapshot,
    ShareVerdict,
    apply_share_verdict,
    count_due_share_checks,
    list_due_share_checks,
    sweep_shares,
)

SessionMaker = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def _clear_library_caches() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """``PlexLibrary`` caches sections per (base_url, token-hash) at module level."""
    reset_caches()
    yield
    reset_caches()


_MACHINE_ID = "configured-server-machine-id"
_ANCHOR_KEY = "plex_machine_identifier"
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
    share_check_failed_at: datetime | None = None,
    share_state: str | None = None,
    entitled_section_keys: list[str] | None = None,
    permissions: int = 0,
) -> int:
    now = datetime.now(UTC)
    expires_at = now - timedelta(days=1) if session_expired else now + timedelta(days=1)
    last_seen_at = now - timedelta(days=30) if session_idle else now
    async with sessionmaker() as session:
        user = User(
            username=username,
            encrypted_plex_token=token,
            permissions=permissions,
            share_checked_at=share_checked_at,
            share_check_failed_at=share_check_failed_at,
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


def _anchor(check: AnchorCheck) -> Callable[[], Awaitable[AnchorCheck]]:
    """A ``confirm_anchor`` callable answering ``check``."""

    async def _confirm() -> AnchorCheck:
        return check

    return _confirm


async def _sweep(
    sessionmaker: SessionMaker,
    payload: list[dict[str, object]] | int,
    *,
    limit: int = plex_access_service.SHARE_SWEEP_USER_BUDGET,
    anchor: AnchorCheck = AnchorCheck.CONFIRMED,
    on_signed_out: Callable[[int], None] | None = None,
    capture: plex_access_service.EntitlementCaptureContext
    | plex_access_service.CaptureUnavailableReason
    | None = None,
) -> plex_access_service.ShareSweepResult:
    """Sweep with the anchor CONFIRMED unless a test says otherwise.

    Confirmed is the ordinary state of a healthy install (the configured server
    still reports the identifier we stored), so it is the right default for the
    verdict-behavior tests; the anchor-fault tests override it explicitly.

    ``capture`` defaults to ``None`` -- entitlement capture is opt-in, so every
    pre-existing verdict test here also pins that the sweep behaves exactly as it
    did before capture existed.
    """
    async with httpx.AsyncClient(transport=_resources_transport(payload)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        return await sweep_shares(
            sessionmaker,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            limit=limit,
            confirm_anchor=_anchor(anchor),
            on_signed_out=on_signed_out,
            capture=capture,
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


async def test_never_attempted_users_are_ordered_ahead_of_previously_checked_ones(
    sessionmaker_: SessionMaker,
) -> None:
    """A never-attempted user (both timestamps NULL) folds to the epoch and sorts
    first -- the NULLS-FIRST semantics, made backend-independent rather than
    leaning on a dialect's default NULL collation (PostgreSQL sorts NULLs LAST on
    ASC, so without this they could sit behind everyone forever)."""
    now = datetime.now(UTC)
    checked = await _add_user(
        sessionmaker_, username="checked", share_checked_at=now - timedelta(hours=7)
    )
    never = await _add_user(sessionmaker_, username="never")
    async with sessionmaker_() as session:
        due = await list_due_share_checks(session, now=now, revalidate_after=_INTERVAL, limit=20)
    assert [user.id for user in due] == [never, checked]


async def test_a_failed_attempt_rotates_a_user_behind_one_checked_longer_ago(
    sessionmaker_: SessionMaker,
) -> None:
    """The ordering key is the last ATTEMPT, not the last success. An UNKNOWN
    verdict never advances ``share_checked_at`` (by design), so ordering on that
    alone would re-select the same user forever; ``share_check_failed_at`` has to
    count toward their position even though their ``share_checked_at`` is older."""
    now = datetime.now(UTC)
    just_failed = await _add_user(
        sessionmaker_,
        username="just-failed",
        share_checked_at=now - timedelta(days=9),
        share_check_failed_at=now - timedelta(seconds=5),
    )
    checked_long_ago = await _add_user(
        sessionmaker_, username="stale", share_checked_at=now - timedelta(days=2)
    )
    async with sessionmaker_() as session:
        due = await list_due_share_checks(session, now=now, revalidate_after=_INTERVAL, limit=20)
    # Despite having the OLDEST share_checked_at, the just-failed user goes last.
    assert [user.id for user in due] == [checked_long_ago, just_failed]


async def test_a_perpetually_failing_cohort_cannot_starve_the_rest_of_the_backlog(
    sessionmaker_: SessionMaker,
) -> None:
    """The starvation bug (Codex review of PR #557): with ``share_checked_at`` as
    the primary sort key, the two oldest-checked users failing every tick were
    re-selected every tick forever, and users behind them -- including a
    genuinely revoked one -- were never checked at all. Established accounts have
    distinct ``share_checked_at`` values, so the failed-at tiebreak never applied.

    With the last-ATTEMPT key the whole backlog is covered within
    ``ceil(due / budget)`` ticks no matter how many checks fail.
    """
    now = datetime.now(UTC)
    # Distinct share_checked_at values, oldest first -- the shape the old
    # ordering starved. The two oldest always fail; the rest would be revoked.
    user_ids = [
        await _add_user(
            sessionmaker_,
            username=f"viewer{index}",
            token=f"token-{index}",
            share_checked_at=now - timedelta(days=10 - index),
        )
        for index in range(5)
    ]
    always_failing = {f"token-{index}" for index in range(2)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Plex-Token", "") in always_failing:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=[_server_resource(_MACHINE_ID)])

    # ceil(5 due / 2 per tick) == 3 ticks to cover everyone.
    for _ in range(3):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            plex_tv = PlexTvClient(client, client_identifier="pm-test")
            await sweep_shares(
                sessionmaker_,
                plex_tv,
                _MACHINE_ID,
                revalidate_after=_INTERVAL,
                limit=2,
                confirm_anchor=_anchor(AnchorCheck.CONFIRMED),
            )

    # Every user behind the failing cohort got a real verdict.
    for user_id in user_ids[2:]:
        user = await _load(sessionmaker_, user_id)
        assert user.share_state == "authorized", f"user {user_id} was starved"
    # The failing pair is still due (their checks never succeeded) -- withheld
    # from the queue by rotation, not dropped from it.
    for user_id in user_ids[:2]:
        user = await _load(sessionmaker_, user_id)
        assert user.share_check_failures > 0


async def test_a_crashing_cohort_cannot_starve_the_backlog_either(
    sessionmaker_: SessionMaker,
) -> None:
    """The same starvation reached through the DEFENSIVE branch: when
    ``check_share`` raises something that is not a mapped ``PlexVerifyError``,
    no verdict is produced at all. Recording only an in-memory ``last_error``
    left ``share_check_failed_at`` untouched, so the crashing cohort stayed at
    the head of the due queue and consumed every tick's budget forever (Codex
    round 2 on PR #557). The failed ATTEMPT has to be persisted so they rotate.
    """
    now = datetime.now(UTC)
    user_ids = [
        await _add_user(
            sessionmaker_,
            username=f"viewer{index}",
            token=f"token-{index}",
            share_checked_at=now - timedelta(days=10 - index),
        )
        for index in range(5)
    ]
    crashing = {f"token-{index}" for index in range(2)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Plex-Token", "") in crashing:
            raise RuntimeError("client blew up in a way check_share does not map")
        return httpx.Response(200, json=[_server_resource(_MACHINE_ID)])

    for _ in range(3):  # ceil(5 / 2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            plex_tv = PlexTvClient(client, client_identifier="pm-test")
            await sweep_shares(
                sessionmaker_,
                plex_tv,
                _MACHINE_ID,
                revalidate_after=_INTERVAL,
                limit=2,
                confirm_anchor=_anchor(AnchorCheck.CONFIRMED),
            )

    for user_id in user_ids[2:]:
        user = await _load(sessionmaker_, user_id)
        assert user.share_state == "authorized", f"user {user_id} was starved by the crash"
    # The crash is recorded as an attempt, exactly as an UNKNOWN verdict is.
    for user_id in user_ids[:2]:
        user = await _load(sessionmaker_, user_id)
        assert user.share_check_failures > 0
        assert user.share_check_failed_at is not None
        # ... but it is NOT a verdict: their last known state is untouched.
        assert user.share_state is None


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
    assert rows[0].new_value == {
        "share_state": "share_revoked",
        "sessions_revoked": 1,
        "admin_exempt": False,
    }
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


async def test_a_sign_in_committed_after_check_share_is_not_revoked(
    sessionmaker_: SessionMaker,
) -> None:
    """The interleaving Codex flagged: the user is loaded, the plex.tv verdict
    comes back SHARE_REVOKED, and THEN a sign-in commits a new token and a new
    session. The guard has to re-read the token from the database rather than
    trust the value loaded before the network call, or the brand-new session is
    cut on a verdict that predates it."""
    user_id = await _add_user(sessionmaker_, token="old-token")  # noqa: S106

    async with sessionmaker_() as apply_session:
        # The row as the sweep loaded it, BEFORE the network call.
        user = await apply_session.get(User, user_id)
        assert user is not None
        assert user.encrypted_plex_token == "old-token"  # noqa: S105

        # ... plex.tv answers SHARE_REVOKED ... and meanwhile a sign-in lands.
        async with sessionmaker_() as sign_in:
            fresh = await sign_in.get(User, user_id)
            assert fresh is not None
            fresh.encrypted_plex_token = "brand-new-token"  # noqa: S105
            sign_in.add(
                AuthSession(
                    user_id=user_id,
                    token_hash="hash-fresh-sign-in",  # noqa: S106 - a digest
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    last_seen_at=datetime.now(UTC),
                )
            )
            await sign_in.commit()

        outcome = await apply_share_verdict(
            apply_session,
            user,
            EntitlementSnapshot(
                verdict=ShareVerdict.SHARE_REVOKED,
                section_keys=None,
                machine_identifier=_MACHINE_ID,
            ),
            expected_token="old-token",  # noqa: S106
            now=datetime.now(UTC),
        )
        await apply_session.commit()

    assert outcome.applied is False
    assert outcome.sessions_revoked == 0
    # Both the pre-existing session and the fresh one survive untouched.
    assert await _live_session_count(sessionmaker_, user_id) == 2
    assert await _audit_rows(sessionmaker_) == []
    user = await _load(sessionmaker_, user_id)
    assert user.share_state is None


async def test_the_revocation_statement_itself_refuses_a_stale_token(
    sessionmaker_: SessionMaker,
) -> None:
    """The guard at the STATEMENT level, not just as a Python pre-check: the
    revocation UPDATE carries the token condition, so a sign-in that committed a
    new token makes it match zero rows even when the UPDATE runs afterwards.

    Fernet is non-deterministic, so the condition compares the stored CIPHERTEXT
    byte-for-byte rather than binding a re-encrypted plaintext (which could never
    match). This drives ``revoke_user_sessions`` directly so nothing but the
    statement's own predicate can be responsible for the outcome.
    """
    user_id = await _add_user(sessionmaker_, token="old-token")  # noqa: S106

    # The ciphertext as the sweep's guard would have captured it...
    async with sessionmaker_() as session:
        stale_ciphertext = (
            await session.execute(
                select(plex_access_service._stored_token_ciphertext()).where(User.id == user_id)  # pyright: ignore[reportPrivateUsage]
            )
        ).scalar_one()

    # ... and then a sign-in commits a NEW token.
    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.encrypted_plex_token = "brand-new-token"  # noqa: S105
        await session.commit()

    async with sessionmaker_() as session:
        revoked = await session_lifecycle.revoke_user_sessions(
            session,
            user_id,
            only_if_user_matches=plex_access_service._stored_token_ciphertext()  # pyright: ignore[reportPrivateUsage]
            == stale_ciphertext,
        )
        await session.commit()

    assert revoked == 0
    assert await _live_session_count(sessionmaker_, user_id) == 1

    # Positive control: the SAME statement shape against the CURRENT ciphertext
    # does revoke, so the zero above is the guard biting and not a broken query.
    async with sessionmaker_() as session:
        current_ciphertext = (
            await session.execute(
                select(plex_access_service._stored_token_ciphertext()).where(User.id == user_id)  # pyright: ignore[reportPrivateUsage]
            )
        ).scalar_one()
        revoked_now = await session_lifecycle.revoke_user_sessions(
            session,
            user_id,
            only_if_user_matches=plex_access_service._stored_token_ciphertext()  # pyright: ignore[reportPrivateUsage]
            == current_ciphertext,
        )
        await session.commit()
    assert revoked_now == 1
    assert await _live_session_count(sessionmaker_, user_id) == 0


async def test_a_session_created_after_the_guard_survives_a_genuine_revocation(
    sessionmaker_: SessionMaker,
) -> None:
    """The MVCC belt to the re-read's braces: even when the token is UNCHANGED --
    so the guard legitimately passes and the revocation is genuine -- a session
    minted after the guard instant belongs to a sign-in this verdict never saw
    and must survive. Sessions that existed at the guard are still cut."""
    user_id = await _add_user(sessionmaker_, username="viewer", with_session=False)
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        # One session predating the guard (must be revoked) and one dated after
        # it (must survive) -- the deterministic stand-in for a sign-in landing
        # between the re-read and the UPDATE under PostgreSQL MVCC.
        session.add(
            AuthSession(
                user_id=user_id,
                token_hash="hash-existing",  # noqa: S106 - a digest
                created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(days=1),
                last_seen_at=now,
            )
        )
        session.add(
            AuthSession(
                user_id=user_id,
                token_hash="hash-after-guard",  # noqa: S106 - a digest
                created_at=now + timedelta(minutes=5),
                expires_at=now + timedelta(days=1),
                last_seen_at=now,
            )
        )
        await session.commit()

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
            expected_token=user.encrypted_plex_token,
            now=datetime.now(UTC),
        )
        await session.commit()

    # The verdict WAS applied and the old session cut -- only the newer one is
    # spared, so a genuine revocation is not weakened into a no-op.
    assert outcome.applied is True
    assert outcome.signed_out is True
    assert outcome.sessions_revoked == 1
    assert await _live_session_count(sessionmaker_, user_id) == 1
    async with sessionmaker_() as session:
        surviving = (
            await session.execute(
                select(AuthSession.token_hash).where(
                    AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
                )
            )
        ).scalar_one()
    assert surviving == "hash-after-guard"


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
        result = await sweep_shares(
            sessionmaker_,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            confirm_anchor=_anchor(AnchorCheck.CONFIRMED),
        )

    assert result.checked == 2
    assert result.share_revoked == 1
    assert result.authorized == 1
    assert result.signed_out_user_ids == (revoked_id,)
    assert await _live_session_count(sessionmaker_, revoked_id) == 0
    assert await _live_session_count(sessionmaker_, kept_id) == 1


# --------------------------------------------------------------------------- #
# Never-locked-out: the admin exemption and the stale-anchor gate
# --------------------------------------------------------------------------- #
async def test_admin_share_loss_is_recorded_but_never_signs_them_out(
    sessionmaker_: SessionMaker,
) -> None:
    """ADR-0005's never-locked-out rule: an admin is the only principal who can
    repoint a wrong Plex server from the web, and that needs a live session. A
    genuine, anchor-confirmed revocation of an admin is therefore RECORDED but
    not acted on -- the operator revokes by hand if it is real."""
    admin_id = await _add_user(sessionmaker_, username="owner", permissions=1)

    result = await _sweep(sessionmaker_, [_server_resource("some-other-server")])

    assert result.share_revoked == 1
    assert result.admins_exempted == 1
    assert result.sessions_revoked == 0
    assert result.signed_out_user_ids == ()
    # The verdict is still persisted honestly -- the sweep is not pretending the
    # share is fine, only declining to cut the repair credential.
    admin = await _load(sessionmaker_, admin_id)
    assert admin.share_state == "share_revoked"
    assert admin.share_checked_at is not None
    assert await _live_session_count(sessionmaker_, admin_id) == 1

    rows = await _audit_rows(sessionmaker_)
    assert len(rows) == 1
    assert rows[0].action_type == "user.share_revoked_admin_exempt"
    assert rows[0].new_value == {
        "share_state": "share_revoked",
        "sessions_revoked": 0,
        "admin_exempt": True,
    }
    assert rows[0].description is not None
    assert "did NOT sign it out" in rows[0].description


async def test_admin_exemption_does_not_shield_non_admins_in_the_same_tick(
    sessionmaker_: SessionMaker,
) -> None:
    admin_id = await _add_user(sessionmaker_, username="owner", permissions=1)
    viewer_id = await _add_user(sessionmaker_, username="viewer", permissions=0)

    result = await _sweep(sessionmaker_, [_server_resource("some-other-server")])

    assert result.share_revoked == 2
    assert result.admins_exempted == 1
    assert result.signed_out_user_ids == (viewer_id,)
    assert await _live_session_count(sessionmaker_, admin_id) == 1
    assert await _live_session_count(sessionmaker_, viewer_id) == 0


async def test_admin_token_stale_is_also_exempt_and_labeled_as_such(
    sessionmaker_: SessionMaker,
) -> None:
    admin_id = await _add_user(sessionmaker_, username="owner", permissions=1)
    result = await _sweep(sessionmaker_, 401)
    assert result.token_stale == 1
    assert result.admins_exempted == 1
    assert await _live_session_count(sessionmaker_, admin_id) == 1
    rows = await _audit_rows(sessionmaker_)
    assert rows[0].action_type == "user.plex_sign_in_expired_admin_exempt"


@pytest.mark.parametrize("anchor", [AnchorCheck.MISMATCHED, AnchorCheck.UNCONFIRMED])
async def test_rebuilt_server_revokes_nobody_and_leaves_everyone_due(
    sessionmaker_: SessionMaker, anchor: AnchorCheck
) -> None:
    """The lockout scenario: the Plex server is rebuilt, so it hands out a NEW
    machineIdentifier while we still hold the old one as our anchor. plex.tv then
    truthfully reports that NOBODY reaches the old server, making every verdict
    SHARE_REVOKED. Acting on that would sign out the whole install -- including
    the admin whose session is the only way to repoint it."""
    user_ids = [await _add_user(sessionmaker_, username=f"viewer{i}") for i in range(3)]
    admin_id = await _add_user(sessionmaker_, username="owner", permissions=1)

    result = await _sweep(sessionmaker_, [_server_resource("rebuilt-server")], anchor=anchor)

    # Not one verdict acted on, not one session cut, not one audit row.
    assert result.anchor_deferred == 4
    assert result.share_revoked == 0
    assert result.checked == 0
    assert result.sessions_revoked == 0
    assert result.signed_out_user_ids == ()
    assert await _audit_rows(sessionmaker_) == []
    for user_id in [*user_ids, admin_id]:
        assert await _live_session_count(sessionmaker_, user_id) == 1
        user = await _load(sessionmaker_, user_id)
        # share_checked_at untouched, so they are all still due: a genuine
        # revocation is caught the moment the anchor is trustworthy again.
        assert user.share_state is None
        assert user.share_checked_at is None
    assert result.due_remaining == 4


async def test_confirmed_identity_change_reports_anchor_mismatch(
    sessionmaker_: SessionMaker,
) -> None:
    await _add_user(sessionmaker_)
    result = await _sweep(
        sessionmaker_, [_server_resource("rebuilt-server")], anchor=AnchorCheck.MISMATCHED
    )
    status = plex_access_service.ShareSweepStatus()
    status.mark_completed(result)
    # Named, because it is the one state the operator must act on (repoint) --
    # not the same word a transient plex.tv hiccup produces.
    assert status.state == "anchor_mismatch"
    assert status.anchor_deferred == 1
    assert status.last_error_type == "PlexAnchorMismatch"
    assert status.last_ok_at is None


async def test_unreachable_anchor_reports_unconfirmed_not_a_claimed_change(
    sessionmaker_: SessionMaker,
) -> None:
    """A Plex outage that happens to coincide with a would-revoke verdict must
    NOT be reported as "the server changed": we never established that. Same
    conflation the ``probe_failed``/``not_configured`` split exists to prevent
    (#327) -- an unestablished identity change would send the operator hunting a
    configuration fault that never happened."""
    await _add_user(sessionmaker_)
    result = await _sweep(
        sessionmaker_, [_server_resource("some-other-server")], anchor=AnchorCheck.UNCONFIRMED
    )
    status = plex_access_service.ShareSweepStatus()
    status.mark_completed(result)
    assert status.state == "anchor_unconfirmed"
    assert status.anchor_deferred == 1
    assert status.last_error_type == "PlexAnchorUnconfirmed"
    # Still withheld every sign-out -- the two states differ in what they CLAIM,
    # never in how safely they behave.
    assert result.sessions_revoked == 0
    assert status.last_ok_at is None


async def test_result_carries_which_anchor_answer_deferred_the_tick(
    sessionmaker_: SessionMaker,
) -> None:
    await _add_user(sessionmaker_)
    mismatched = await _sweep(
        sessionmaker_, [_server_resource("rebuilt-server")], anchor=AnchorCheck.MISMATCHED
    )
    assert mismatched.anchor_state is AnchorCheck.MISMATCHED
    unconfirmed = await _sweep(
        sessionmaker_, [_server_resource("rebuilt-server")], anchor=AnchorCheck.UNCONFIRMED
    )
    assert unconfirmed.anchor_state is AnchorCheck.UNCONFIRMED
    # A tick that never needed to ask reports no anchor answer at all.
    clean = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)])
    assert clean.anchor_state is None


async def test_anchor_is_confirmed_at_most_once_per_tick(sessionmaker_: SessionMaker) -> None:
    """One probe per tick, not one per user -- and none at all when no verdict
    needs it."""
    for index in range(3):
        await _add_user(sessionmaker_, username=f"viewer{index}")
    calls = 0

    async def counting_confirm() -> AnchorCheck:
        nonlocal calls
        calls += 1
        return AnchorCheck.CONFIRMED

    async with httpx.AsyncClient(
        transport=_resources_transport([_server_resource("some-other-server")])
    ) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        await sweep_shares(
            sessionmaker_,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            confirm_anchor=counting_confirm,
        )
    assert calls == 1


async def test_all_authorized_tick_never_probes_the_anchor(sessionmaker_: SessionMaker) -> None:
    await _add_user(sessionmaker_)
    calls = 0

    async def counting_confirm() -> AnchorCheck:
        nonlocal calls
        calls += 1
        return AnchorCheck.CONFIRMED

    async with httpx.AsyncClient(
        transport=_resources_transport([_server_resource(_MACHINE_ID)])
    ) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await sweep_shares(
            sessionmaker_,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            confirm_anchor=counting_confirm,
        )
    assert result.authorized == 1
    assert calls == 0


async def test_missing_anchor_confirmation_fails_safe(sessionmaker_: SessionMaker) -> None:
    """No ``confirm_anchor`` supplied is treated as UNCONFIRMED, never as a pass:
    the failure being guarded is a whole-install sign-out."""
    user_id = await _add_user(sessionmaker_)
    async with httpx.AsyncClient(
        transport=_resources_transport([_server_resource("some-other-server")])
    ) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await sweep_shares(sessionmaker_, plex_tv, _MACHINE_ID, revalidate_after=_INTERVAL)
    assert result.anchor_deferred == 1
    assert await _live_session_count(sessionmaker_, user_id) == 1


async def test_raising_anchor_confirmation_defers_rather_than_revoking(
    sessionmaker_: SessionMaker,
) -> None:
    user_id = await _add_user(sessionmaker_)

    async def exploding_confirm() -> AnchorCheck:
        raise RuntimeError("probe blew up")

    async with httpx.AsyncClient(
        transport=_resources_transport([_server_resource("some-other-server")])
    ) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await sweep_shares(
            sessionmaker_,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            confirm_anchor=exploding_confirm,
        )
    assert result.anchor_deferred == 1
    assert result.last_error_type == "RuntimeError"
    assert await _live_session_count(sessionmaker_, user_id) == 1


# --------------------------------------------------------------------------- #
# The #183 pairing survives a partial failure
# --------------------------------------------------------------------------- #
async def test_a_later_users_check_blowing_up_leaves_the_earlier_close_done(
    sessionmaker_: SessionMaker,
) -> None:
    """The exact interleaving that made batching unsafe: user A is revoked and
    committed, then user B's plex.tv check raises. A's stream close must already
    have happened -- A no longer holds a live session, so no later tick will ever
    select them again and come back to close it."""
    first_id = await _add_user(sessionmaker_, username="alpha")
    second_id = await _add_user(sessionmaker_, username="beta")
    closed: list[int] = []
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("plex.tv client exploded on the second user")
        return httpx.Response(200, json=[_server_resource("some-other-server")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await sweep_shares(
            sessionmaker_,
            plex_tv,
            _MACHINE_ID,
            revalidate_after=_INTERVAL,
            confirm_anchor=_anchor(AnchorCheck.CONFIRMED),
            on_signed_out=closed.append,
        )

    # A: revoked AND closed. B: never got a verdict, so untouched and still due.
    assert closed == [first_id]
    assert result.share_revoked == 1
    assert result.last_error_type == "RuntimeError"
    assert await _live_session_count(sessionmaker_, first_id) == 0
    assert await _live_session_count(sessionmaker_, second_id) == 1
    assert result.due_remaining == 1


async def test_each_revocation_closes_its_stream_before_the_next_user_is_touched(
    sessionmaker_: SessionMaker,
) -> None:
    """The stream close must ride the COMMITTED side of each revocation, not a
    batch at the end: a revoked user is no longer due-selected, so if a later
    user's failure discarded the batch their SSE stream would survive until it
    reconnected -- up to the 7-day idle window (issue #183)."""
    first_id = await _add_user(sessionmaker_, username="alpha")
    second_id = await _add_user(sessionmaker_, username="beta")
    closed: list[int] = []
    revoked_when_closed: list[int] = []

    def on_signed_out(user_id: int) -> None:
        closed.append(user_id)
        if user_id == first_id:
            # Prove the close happens AFTER the commit, not optimistically
            # before it: by now the row must already read as revoked.
            revoked_when_closed.append(user_id)

    result = await _sweep(
        sessionmaker_, [_server_resource("some-other-server")], on_signed_out=on_signed_out
    )

    assert closed == [first_id, second_id]
    assert revoked_when_closed == [first_id]
    assert result.signed_out_user_ids == (first_id, second_id)


async def test_a_later_users_failure_cannot_strand_an_earlier_closed_revocation(
    sessionmaker_: SessionMaker,
) -> None:
    first_id = await _add_user(sessionmaker_, username="alpha")
    await _add_user(sessionmaker_, username="beta")
    closed: list[int] = []

    def on_signed_out(user_id: int) -> None:
        closed.append(user_id)
        if user_id != first_id:
            raise RuntimeError("stream close blew up for the second user")

    result = await _sweep(
        sessionmaker_, [_server_resource("some-other-server")], on_signed_out=on_signed_out
    )

    # The first user's stream was already closed before the second user's
    # failure, and the failure degrades the tick rather than aborting it.
    assert closed[0] == first_id
    assert len(closed) == 2
    assert result.share_revoked == 2
    assert result.last_error_type == "RuntimeError"
    assert await _live_session_count(sessionmaker_, first_id) == 0


# --------------------------------------------------------------------------- #
# Status surface
# --------------------------------------------------------------------------- #
async def test_due_remaining_counts_users_still_owed_a_check_during_an_outage(
    sessionmaker_: SessionMaker,
) -> None:
    """The number must be recomputed, not inferred from the budget: every UNKNOWN
    user is still due, so reporting 0 would tell the operator the backlog was
    clear at exactly the moment nothing was being checked."""
    for index in range(3):
        await _add_user(sessionmaker_, username=f"viewer{index}")

    result = await _sweep(sessionmaker_, 500)

    assert result.unknown == 3
    assert result.due_remaining == 3


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


# --------------------------------------------------------------------------- #
# Section-entitlement capture inside the sweep (issue #484 PR-3)
# --------------------------------------------------------------------------- #
_SECTIONS_PAYLOAD: dict[str, object] = {
    "MediaContainer": {
        "size": 2,
        "Directory": [
            {"key": "1", "title": "Movies", "type": "movie", "Location": [{"id": 1, "path": "/x"}]},
            {"key": "2", "title": "TV", "type": "show", "Location": [{"id": 2, "path": "/y"}]},
        ],
    }
}


async def _configure_anchor(sessionmaker: SessionMaker, machine_id: str = _MACHINE_ID) -> None:
    """The settings row ``store_entitlements`` re-reads inside its own write."""
    async with sessionmaker() as session:
        session.add(Setting(key=_ANCHOR_KEY, value=machine_id))
        await session.commit()


def _capture_context(
    *,
    section_keys: tuple[str, ...] = ("1",),
    owner_sections: int | None = 2,
    fail: bool = False,
    calls: list[str] | None = None,
) -> plex_access_service.EntitlementCaptureContext:
    """A capture context over a REAL ``PlexLibrary`` and a mock Plex server."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.headers.get("X-Plex-Token", ""))
        if fail:
            return httpx.Response(500, json={})
        directory = [
            {"key": key, "title": f"S{key}", "type": "movie", "Location": [{"id": 1, "path": "/x"}]}
            for key in section_keys
        ]
        return httpx.Response(
            200, json={"MediaContainer": {"size": len(directory), "Directory": directory}}
        )

    def _library_for_token(token: str) -> PlexLibrary:
        transport = httpx.MockTransport(handler)
        return PlexLibrary(httpx.AsyncClient(transport=transport), "http://plex.local:32400", token)

    async def _owner_section_count() -> int | None:
        return owner_sections

    return plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )


async def test_sweep_captures_entitlements_for_an_authorized_user(
    sessionmaker_: SessionMaker,
) -> None:
    await _configure_anchor(sessionmaker_)
    user_id = await _add_user(sessionmaker_)
    result = await _sweep(
        sessionmaker_, [_server_resource(_MACHINE_ID)], capture=_capture_context()
    )
    assert result.authorized == 1
    assert result.captured == 1
    assert result.capture_failed == 0
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["1"]
    assert user.entitlements_machine_id == _MACHINE_ID


async def test_sweep_without_a_capture_context_writes_no_entitlements(
    sessionmaker_: SessionMaker,
) -> None:
    """Capture is opt-in: the sweep is byte-for-byte its pre-#484 self without it."""
    user_id = await _add_user(sessionmaker_)
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)])
    assert result.authorized == 1
    assert result.captured == 0
    # Nothing was wired up, so there is no gate to report either.
    assert result.capture_skipped == 0
    assert result.capture_unavailable is None
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None


@pytest.mark.parametrize("reason", ["not_configured", "no_server_anchor"])
async def test_a_declined_capture_counts_and_names_itself(
    sessionmaker_: SessionMaker, reason: plex_access_service.CaptureUnavailableReason
) -> None:
    """The composition root can decline capture (no Plex, or no operator-verified
    server anchor to stamp a snapshot with), but declining silently would leave
    ``captured``/``capture_failed``/``capture_skipped`` at zero on an ``ok``
    sweep forever -- identical on /health to a tick with nothing to capture. Each
    AUTHORIZED user is counted as skipped and the reason travels with the tally.
    """
    user_id = await _add_user(sessionmaker_)
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=reason)

    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_failed == 0
    assert result.capture_skipped == 1
    assert result.capture_unavailable == reason
    # The verdict half is untouched: a declined capture is not a degraded sweep.
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.entitled_section_keys is None

    status = plex_access_service.ShareSweepStatus()
    status.mark_completed(result)
    assert status.state == "ok"
    assert status.capture_unavailable == reason


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        pytest.param([_server_resource("some-other-server")], "share_revoked", id="share_revoked"),
        pytest.param(401, "token_stale", id="token_stale"),
        pytest.param(500, "unknown", id="unknown"),
    ],
)
async def test_sweep_never_spends_a_server_call_on_a_non_authorized_verdict(
    sessionmaker_: SessionMaker, payload: list[dict[str, object]] | int, label: str
) -> None:
    """The budget promise: capture costs one SERVER call per already-confirmed
    user and nothing at all for anyone else."""
    user_id = await _add_user(sessionmaker_)
    calls: list[str] = []
    result = await _sweep(sessionmaker_, payload, capture=_capture_context(calls=calls))
    assert calls == []
    assert result.captured == 0
    assert result.capture_failed == 0
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None


async def test_sweep_capture_failure_is_counted_and_harmless(
    sessionmaker_: SessionMaker,
) -> None:
    await _configure_anchor(sessionmaker_)
    """A refusing/unreachable server must not touch the verdict, the sign-out
    machinery, or a previously captured snapshot."""
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.entitled_section_keys = ["9"]
        user.entitlements_machine_id = _MACHINE_ID
        await session.commit()

    result = await _sweep(
        sessionmaker_, [_server_resource(_MACHINE_ID)], capture=_capture_context(fail=True)
    )

    # The verdict landed exactly as it would without capture.
    assert result.authorized == 1
    assert result.checked == 1
    assert result.captured == 0
    assert result.capture_failed == 1
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    # The PREVIOUS snapshot survives a failed capture untouched.
    assert user.entitled_section_keys == ["9"]


async def test_sweep_capture_respects_the_per_tick_budget(sessionmaker_: SessionMaker) -> None:
    """Capture rides the existing budget rather than widening it: at most one
    server call per user the tick was already allowed to check."""
    await _configure_anchor(sessionmaker_)
    for index in range(5):
        await _add_user(sessionmaker_, username=f"viewer{index}", token=f"token-{index}")
    calls: list[str] = []
    result = await _sweep(
        sessionmaker_,
        [_server_resource(_MACHINE_ID)],
        limit=2,
        capture=_capture_context(calls=calls),
    )
    assert result.checked == 2
    assert result.captured == 2
    # Exactly one server call per user the budget already allowed -- never one
    # per candidate. (The owner baseline is stubbed in this helper, so every
    # recorded call here is a per-user capture.)
    assert calls == ["token-0", "token-1"]


async def test_sweep_resolves_the_owner_baseline_at_most_once_per_tick(
    sessionmaker_: SessionMaker,
) -> None:
    await _configure_anchor(sessionmaker_)
    for index in range(3):
        await _add_user(sessionmaker_, username=f"viewer{index}", token=f"token-{index}")
    baseline_calls = 0

    async def _owner_section_count() -> int | None:
        nonlocal baseline_calls
        baseline_calls += 1
        return 2

    def _library_for_token(token: str) -> PlexLibrary:
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=_SECTIONS_PAYLOAD))
        return PlexLibrary(httpx.AsyncClient(transport=transport), "http://plex.local:32400", token)

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)
    assert result.captured == 3
    assert baseline_calls == 1


async def test_sweep_skips_the_baseline_entirely_when_nothing_is_captured(
    sessionmaker_: SessionMaker,
) -> None:
    await _add_user(sessionmaker_)
    baseline_calls = 0

    async def _owner_section_count() -> int | None:
        nonlocal baseline_calls
        baseline_calls += 1
        return 2

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=lambda _token: None,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )
    # No library to capture with: not a failure, and no baseline worth reading.
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)
    assert result.captured == 0
    assert result.capture_failed == 0
    assert baseline_calls == 0


def test_status_surfaces_the_capture_counters() -> None:
    status = plex_access_service.ShareSweepStatus()
    status.mark_completed(
        plex_access_service.ShareSweepResult(checked=3, authorized=3, captured=2, capture_failed=1)
    )
    # Capture enforces nothing yet, so a failed capture must NOT degrade the tick.
    assert status.state == "ok"
    assert status.captured == 2
    assert status.capture_failed == 1


async def test_a_raising_library_factory_only_degrades_capture(
    sessionmaker_: SessionMaker,
) -> None:
    """The capture path's totality must not rest on a web-layer callable
    behaving: a ``library_for_token`` that throws is capture's problem alone, and
    the verdict work of the tick stands."""
    await _configure_anchor(sessionmaker_)
    user_id = await _add_user(sessionmaker_)

    def _explode(_token: str) -> PlexLibrary:
        raise RuntimeError("composition root blew up")

    async def _owner_section_count() -> int | None:
        return 2

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_explode,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)

    # The verdict landed; only the capture counter moved.
    assert result.checked == 1
    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_failed == 1
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.entitled_section_keys is None


async def test_an_unreachable_server_breaks_the_capture_circuit_for_the_tick(
    sessionmaker_: SessionMaker,
) -> None:
    """A black-holed Plex server costs one full client timeout PER authorized
    user, which at a full budget is minutes of tick wall time spent learning the
    same thing 20 times. The first server-level failure stops the rest."""
    await _configure_anchor(sessionmaker_)
    for index in range(4):
        await _add_user(sessionmaker_, username=f"viewer{index}", token=f"token-{index}")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("X-Plex-Token", ""))
        raise httpx.ConnectError("plex server black hole", request=request)

    def _library_for_token(token: str) -> PlexLibrary:
        transport = httpx.MockTransport(handler)
        return PlexLibrary(httpx.AsyncClient(transport=transport), "http://plex.local:32400", token)

    async def _owner_section_count() -> int | None:
        return None

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)

    # Exactly ONE server call, not one per authorized user.
    assert len(calls) == 1
    assert result.capture_failed == 1
    assert result.capture_skipped == 3
    # Every verdict still landed: the sweep talks to plex.tv, not to this server.
    assert result.checked == 4
    assert result.authorized == 4


async def test_a_refused_token_does_not_break_the_circuit_for_everyone_else(
    sessionmaker_: SessionMaker,
) -> None:
    """A 401 is about THIS user's token, not the server -- the next user's
    capture must still be attempted. Tripping on it would let one bad token
    suppress capture for the whole install."""
    await _configure_anchor(sessionmaker_)
    for index in range(3):
        await _add_user(sessionmaker_, username=f"viewer{index}", token=f"token-{index}")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("X-Plex-Token", "")
        calls.append(token)
        if token == "token-0":  # noqa: S105 - a fake token id, not a credential
            return httpx.Response(401, json={})
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "size": 1,
                    "Directory": [
                        {
                            "key": "1",
                            "title": "Movies",
                            "type": "movie",
                            "Location": [{"id": 1, "path": "/x"}],
                        }
                    ],
                }
            },
        )

    def _library_for_token(token: str) -> PlexLibrary:
        transport = httpx.MockTransport(handler)
        return PlexLibrary(httpx.AsyncClient(transport=transport), "http://plex.local:32400", token)

    async def _owner_section_count() -> int | None:
        return 1

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )
    result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)

    assert calls == ["token-0", "token-1", "token-2"]
    assert result.capture_failed == 1
    assert result.capture_skipped == 0
    assert result.captured == 2


async def test_a_repoint_mid_sweep_discards_the_capture_but_keeps_the_verdict(
    sessionmaker_: SessionMaker,
) -> None:
    """The anchor is re-read by the write itself, so a repoint landing between
    the capture and the store is caught -- and it is capture's problem only."""
    await _configure_anchor(sessionmaker_, "a-different-server")
    user_id = await _add_user(sessionmaker_)

    result = await _sweep(
        sessionmaker_, [_server_resource(_MACHINE_ID)], capture=_capture_context()
    )

    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_failed == 1
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.entitled_section_keys is None


async def test_capture_never_stamps_when_the_anchor_is_not_confirmed(
    sessionmaker_: SessionMaker,
) -> None:
    """A replacement server at the SAME plex_url answers happily, and the
    settings row still holds the OLD id -- so the row guard alone would accept
    the new server's sections under the old anchor. Capture therefore rides the
    tick's live /identity confirmation, exactly as the revocation path does."""
    await _configure_anchor(sessionmaker_)
    user_id = await _add_user(sessionmaker_)
    calls: list[str] = []

    result = await _sweep(
        sessionmaker_,
        [_server_resource(_MACHINE_ID)],
        anchor=AnchorCheck.MISMATCHED,
        capture=_capture_context(calls=calls),
    )

    # The verdict work still happened; only the stamping was withheld.
    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_skipped == 1
    # And no server call was spent on a capture that could never be stamped.
    assert calls == []
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None


async def test_capture_skips_when_the_anchor_cannot_be_confirmed_at_all(
    sessionmaker_: SessionMaker,
) -> None:
    await _configure_anchor(sessionmaker_)
    await _add_user(sessionmaker_)
    result = await _sweep(
        sessionmaker_,
        [_server_resource(_MACHINE_ID)],
        anchor=AnchorCheck.UNCONFIRMED,
        capture=_capture_context(),
    )
    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_skipped == 1


async def test_a_rotated_token_mid_sweep_discards_that_users_capture(
    sessionmaker_: SessionMaker,
) -> None:
    """The sweep's capture is taken with the token read before the network call;
    if a sign-in rotates it in that window, the stale write must not land."""
    await _configure_anchor(sessionmaker_)
    user_id = await _add_user(sessionmaker_, token="original-token")  # noqa: S106

    rotated = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal rotated
        if not rotated:
            rotated = True
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "size": 1,
                    "Directory": [
                        {
                            "key": "1",
                            "title": "Movies",
                            "type": "movie",
                            "Location": [{"id": 1, "path": "/x"}],
                        }
                    ],
                }
            },
        )

    def _library_for_token(token: str) -> PlexLibrary:
        # Rotate the stored credential the moment the capture client is built --
        # i.e. after the guard read it, before the write lands.
        return PlexLibrary(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "http://plex.local:32400",
            token,
        )

    async def _owner_section_count() -> int | None:
        return 1

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )

    async def rotate_now() -> None:
        async with sessionmaker_() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.encrypted_plex_token = "rotated-by-a-sign-in"  # noqa: S105
            await session.commit()

    # Rotate between the guard read and the store: the section handler runs
    # in-between, so rotating from it lands in exactly that window.
    original_attempt = plex_access_service._attempt_capture  # pyright: ignore[reportPrivateUsage]

    async def rotating_attempt(*args: object, **kwargs: object) -> object:
        await rotate_now()
        return await original_attempt(*args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

    plex_access_service._attempt_capture = rotating_attempt  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)
    finally:
        plex_access_service._attempt_capture = original_attempt  # pyright: ignore[reportPrivateUsage]

    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_failed == 1
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None


async def test_a_rotation_between_verdict_and_capture_write_is_refused(
    sessionmaker_: SessionMaker,
) -> None:
    """The sweep's capture must present the ciphertext of the credential its
    library client is BOUND to -- the one read when the candidate was selected --
    not a fresh read taken after the verdict.

    Here a sign-in rotates the token while the capture's section read is in
    flight. A re-read would pick up the NEW ciphertext, pass the write guard, and
    stamp the old token's view over the newer credential's. Carrying the
    selection-time ciphertext makes the write match zero rows instead.
    """
    await _configure_anchor(sessionmaker_)
    user_id = await _add_user(sessionmaker_, token="original-token")  # noqa: S106

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "size": 1,
                    "Directory": [
                        {
                            "key": "9",
                            "title": "Old view",
                            "type": "movie",
                            "Location": [{"id": 1, "path": "/x"}],
                        }
                    ],
                }
            },
        )

    def _library_for_token(token: str) -> PlexLibrary:
        return PlexLibrary(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "http://plex.local:32400",
            token,
        )

    async def _owner_section_count() -> int | None:
        return 1

    context = plex_access_service.EntitlementCaptureContext(
        library_for_token=_library_for_token,
        owner_section_count=_owner_section_count,
        anchor_setting_key=_ANCHOR_KEY,
    )

    # Rotate the stored credential exactly between the verdict and the capture
    # write, by hooking the section read the capture performs in between.
    original_attempt = plex_access_service._attempt_capture  # pyright: ignore[reportPrivateUsage]

    async def rotating_attempt(*args: object, **kwargs: object) -> object:
        async with sessionmaker_() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.encrypted_plex_token = "rotated-by-a-sign-in"  # noqa: S105
            await session.commit()
        return await original_attempt(*args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

    plex_access_service._attempt_capture = rotating_attempt  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        result = await _sweep(sessionmaker_, [_server_resource(_MACHINE_ID)], capture=context)
    finally:
        plex_access_service._attempt_capture = original_attempt  # pyright: ignore[reportPrivateUsage]

    # The verdict landed; the stale-credential snapshot did not.
    assert result.authorized == 1
    assert result.captured == 0
    assert result.capture_failed == 1
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None
    assert user.encrypted_plex_token == "rotated-by-a-sign-in"  # noqa: S105


async def test_the_candidate_carries_the_ciphertext_of_its_own_token(
    sessionmaker_: SessionMaker,
) -> None:
    """The selection statement reads the decrypted token and its raw ciphertext
    together, so the two can never come from different row versions."""
    user_id = await _add_user(sessionmaker_, token="a-token")  # noqa: S106
    async with sessionmaker_() as session:
        candidates = await plex_access_service._select_due_candidates(  # pyright: ignore[reportPrivateUsage]
            session,
            now=datetime.now(UTC),
            revalidate_after=_INTERVAL,
            limit=20,
        )
        expected = await plex_access_service.read_token_ciphertext(session, user_id)
    assert len(candidates) == 1
    user, ciphertext = candidates[0]
    assert user.id == user_id
    assert user.encrypted_plex_token == "a-token"  # noqa: S105
    assert ciphertext == expected
