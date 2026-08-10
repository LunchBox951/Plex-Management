"""Section-entitlement CAPTURE (issue #484 PR-3) -- capture only, no enforcement.

Driven through a real ``PlexLibrary`` over ``httpx.MockTransport`` rather than a
fake port, because the premise this whole staged PR exists to test is an
ADAPTER-level one: that a shared user's token, pointed at the configured
``plex_url``, comes back SECTION-FILTERED. A hand-written fake would assume the
answer.

Nothing here asserts enforcement -- there is none yet. What is asserted is that
the capture is honest (the empty capture is `[]`, never NULL), anchored (never
stamped against a server it was not read from), and harmless (every failure mode
leaves the previous snapshot exactly as it was).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibrary, reset_caches
from plex_manager.models import Setting, User
from plex_manager.services.plex_access_service import (
    EntitlementCapture,
    _clear_entitlements,  # pyright: ignore[reportPrivateUsage]
    capture_entitlements,
    read_token_ciphertext,
    store_entitlements,
)

SessionMaker = async_sessionmaker[AsyncSession]

_PLEX_URL = "http://plex.local:32400"
_MACHINE_ID = "configured-server-machine-id"
_ANCHOR_KEY = "plex_machine_identifier"
_OWNER_TOKEN = "owner-token"  # noqa: S105
_SHARED_TOKEN = "shared-user-token"  # noqa: S105


@pytest.fixture(autouse=True)
def clear_caches() -> Iterator[None]:
    """``PlexLibrary`` caches sections per (base_url, token-hash) at module level."""
    reset_caches()
    yield
    reset_caches()


def _sections(*entries: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "MediaContainer": {
            "size": len(entries),
            "Directory": [
                {"key": key, "title": title, "type": kind, "Location": [{"id": 1, "path": "/x"}]}
                for key, title, kind in entries
            ],
        }
    }


_OWNER_SECTIONS = _sections(("1", "Movies", "movie"), ("2", "TV", "show"), ("3", "Kids", "movie"))
_FILTERED_SECTIONS = _sections(("1", "Movies", "movie"))


def _baseline(count: int | None) -> Callable[[], Awaitable[int | None]]:
    """The owner baseline as the resolver callable capture now takes."""

    async def _resolve() -> int | None:
        return count

    return _resolve


def _library(handler: Callable[[httpx.Request], httpx.Response], token: str) -> PlexLibrary:
    return PlexLibrary(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _PLEX_URL, token)


def _status_handler(status: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    return handler


def _malformed_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="not json")


def _per_token_handler(request: httpx.Request) -> httpx.Response:
    """The premise under test: the SAME server, filtered by whose token asks."""
    assert request.url.path == "/library/sections"
    token = request.headers.get("X-Plex-Token")
    # The token must never ride the URL.
    assert token is not None
    assert token not in str(request.url)
    if token == _OWNER_TOKEN:
        return httpx.Response(200, json=_OWNER_SECTIONS)
    return httpx.Response(200, json=_FILTERED_SECTIONS)


async def _add_user(sessionmaker: SessionMaker, **kwargs: Any) -> int:
    async with sessionmaker() as session:
        user = User(username="viewer", **kwargs)
        session.add(user)
        await session.commit()
        return user.id


async def _configure_anchor(sessionmaker: SessionMaker, machine_id: str) -> None:
    """The settings row ``store_entitlements`` re-reads inside its own write."""
    async with sessionmaker() as session:
        existing = (
            (await session.execute(select(Setting).where(Setting.key == _ANCHOR_KEY)))
            .scalars()
            .first()
        )
        if existing is None:
            session.add(Setting(key=_ANCHOR_KEY, value=machine_id))
        else:
            existing.value = machine_id
        await session.commit()


async def _load(sessionmaker: SessionMaker, user_id: int) -> User:
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return user


# --------------------------------------------------------------------------- #
# Reading the capture
# --------------------------------------------------------------------------- #
async def test_a_shared_users_token_captures_only_the_sections_it_can_see() -> None:
    library = _library(_per_token_handler, _SHARED_TOKEN)
    capture = await capture_entitlements(library, machine_identifier=_MACHINE_ID, user_id=7)
    assert capture is not None
    assert capture.section_keys == ("1",)
    assert capture.machine_identifier == _MACHINE_ID


async def test_the_owner_token_captures_the_whole_library() -> None:
    library = _library(_per_token_handler, _OWNER_TOKEN)
    capture = await capture_entitlements(library, machine_identifier=_MACHINE_ID, user_id=1)
    assert capture is not None
    assert capture.section_keys == ("1", "2", "3")


async def test_a_token_entitled_to_nothing_captures_an_honest_empty_tuple() -> None:
    """The tri-state's whole point: "captured, entitled to none" is `()`, which
    persists as `[]` -- never the NULL that means "never captured"."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sections())

    capture = await capture_entitlements(
        _library(handler, _SHARED_TOKEN), machine_identifier=_MACHINE_ID, user_id=7
    )
    assert capture is not None
    assert capture.section_keys == ()


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(_status_handler(401), id="token_refused"),
        pytest.param(_status_handler(500), id="server_error"),
        pytest.param(_malformed_handler, id="malformed"),
    ],
)
async def test_capture_never_raises_and_returns_none_on_any_server_failure(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """TOTAL by contract: every caller invokes this without a guard of its own."""
    capture = await capture_entitlements(
        _library(handler, _SHARED_TOKEN), machine_identifier=_MACHINE_ID, user_id=7
    )
    assert capture is None


async def test_capture_never_raises_when_the_server_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server unreachable", request=request)

    capture = await capture_entitlements(
        _library(handler, _SHARED_TOKEN), machine_identifier=_MACHINE_ID, user_id=7
    )
    assert capture is None


# --------------------------------------------------------------------------- #
# Telemetry: the canary premise has to be answerable from the log store
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("token", "owner_count", "expected_scope", "expected_sections"),
    [
        pytest.param(_SHARED_TOKEN, 3, "narrower_than_service", 1, id="shared_user_is_filtered"),
        pytest.param(_OWNER_TOKEN, 3, "same_as_service", 3, id="owner_sees_everything"),
        pytest.param(_SHARED_TOKEN, None, "baseline_unknown", 1, id="no_baseline_to_compare"),
        pytest.param(_OWNER_TOKEN, 1, "wider_than_service", 3, id="baseline_is_not_what_we_think"),
    ],
)
async def test_capture_telemetry_names_the_scope_and_counts(
    token: str,
    owner_count: int | None,
    expected_scope: str,
    expected_sections: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The line that answers "is section filtering actually happening?" without
    a terminal -- counts and a one-word scope only.

    The scope words name the SERVICE token, not "the owner": nothing proves the
    stored ``plex_token`` belongs to the server owner (Codex review of PR-3), so
    the ``baseline_is_not_what_we_think`` case below is exactly what a restricted
    service credential looks like in the log."""
    with caplog.at_level(logging.INFO, logger="plex_manager.services.plex_access_service"):
        await capture_entitlements(
            _library(_per_token_handler, token),
            machine_identifier=_MACHINE_ID,
            user_id=7,
            service_section_count=_baseline(owner_count),
        )
    line = next(
        r.getMessage() for r in caplog.records if "entitlement capture user_id" in r.getMessage()
    )
    assert f"sections={expected_sections}" in line
    assert f"scope={expected_scope}" in line
    assert f"service_sections={owner_count if owner_count is not None else 'unknown'}" in line
    # Never the secrets or the shape of the operator's disk.
    assert token not in line
    assert "Movies" not in line
    assert "/x" not in line


async def test_capture_failure_is_logged_without_leaking_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.plex_access_service"):
        await capture_entitlements(
            _library(handler, _SHARED_TOKEN), machine_identifier=_MACHINE_ID, user_id=7
        )
    assert "entitlement capture failed for user_id=7" in caplog.text
    assert _SHARED_TOKEN not in caplog.text


# --------------------------------------------------------------------------- #
# Persisting the capture
# --------------------------------------------------------------------------- #
async def test_store_writes_both_columns_together(sessionmaker_: SessionMaker) -> None:
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    capture = EntitlementCapture(section_keys=("1", "4"), machine_identifier=_MACHINE_ID)
    async with sessionmaker_() as session:
        assert await store_entitlements(
            session,
            user_id,
            capture,
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["1", "4"]
    assert user.entitlements_machine_id == _MACHINE_ID


async def test_store_persists_an_empty_capture_as_a_list_not_null(
    sessionmaker_: SessionMaker,
) -> None:
    """`[]` (captured, entitled to nothing) must survive the round trip as a
    LIST: collapsing it to NULL would make it indistinguishable from "never
    captured" and, at enforcement time, from "no restrictions known"."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=(), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == []
    assert user.entitled_section_keys is not None


async def test_store_refuses_a_capture_taken_against_a_different_server(
    sessionmaker_: SessionMaker,
) -> None:
    """The machine-id stamp is what makes repoints structurally safe. A snapshot
    read from server A must never be written as though it described server B --
    enforcement would then filter one server's libraries by another's ids."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(
        sessionmaker_, entitled_section_keys=["9"], entitlements_machine_id="previous-server"
    )
    capture = EntitlementCapture(section_keys=("1",), machine_identifier="some-other-server")
    async with sessionmaker_() as session:
        stored = await store_entitlements(
            session,
            user_id,
            capture,
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    assert stored is False
    # The PREVIOUS snapshot is untouched -- not cleared, not overwritten.
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["9"]
    assert user.entitlements_machine_id == "previous-server"


async def test_store_refuses_when_there_is_no_configured_anchor(
    sessionmaker_: SessionMaker,
) -> None:
    """No anchor row at all (never configured, or cleared): nothing to confirm
    the capture still describes the configured server, so it is not written."""
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        stored = await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("1",), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    assert stored is False
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys is None


async def test_a_repoint_between_capture_and_store_discards_the_capture(
    sessionmaker_: SessionMaker,
) -> None:
    """The guard that must NOT be tautological: the anchor is re-read by the
    WRITE, so a repoint committing after the capture was taken is caught. A
    check against a value the caller derived from the same variable the capture
    was stamped with could never detect this."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(
        sessionmaker_, entitled_section_keys=["9"], entitlements_machine_id=_MACHINE_ID
    )
    capture = EntitlementCapture(section_keys=("1",), machine_identifier=_MACHINE_ID)

    # ... the operator repoints to a different server in the meantime.
    await _configure_anchor(sessionmaker_, "repointed-server")

    async with sessionmaker_() as session:
        stored = await store_entitlements(
            session,
            user_id,
            capture,
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()

    assert stored is False
    # Nothing written, and the previous snapshot is intact -- not cleared.
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["9"]
    assert user.entitlements_machine_id == _MACHINE_ID


async def test_a_later_capture_replaces_an_earlier_one(sessionmaker_: SessionMaker) -> None:
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    for keys in (("1", "2"), ("2",)):
        async with sessionmaker_() as session:
            await store_entitlements(
                session,
                user_id,
                EntitlementCapture(section_keys=keys, machine_identifier=_MACHINE_ID),
                anchor_setting_key=_ANCHOR_KEY,
                expected_token_ciphertext=None,
            )
            await session.commit()
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["2"]


async def test_an_older_capture_cannot_overwrite_a_newer_one_on_the_same_token(
    sessionmaker_: SessionMaker,
) -> None:
    """The same-credential ordering hole (Codex review of PR-3).

    The anchor and ciphertext guards both hold when two captures share a
    credential and a server -- the ordinary case, since re-signing in usually
    returns the SAME Plex token. So the sweep could read a user's sections, be
    overtaken by a sign-in capture that read LATER and wrote FIRST, and then
    land its older section set on top. ``captured_at`` is what breaks the tie.
    """
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    read_first = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    read_second = read_first + timedelta(seconds=30)

    # The NEWER capture writes first, exactly as the overtaking sign-in would.
    async with sessionmaker_() as session:
        assert await store_entitlements(
            session,
            user_id,
            EntitlementCapture(
                section_keys=("1", "3"),
                machine_identifier=_MACHINE_ID,
                captured_at=read_second,
            ),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()

    # ... and the older, slower one lands afterwards with the same credential.
    async with sessionmaker_() as session:
        stored = await store_entitlements(
            session,
            user_id,
            EntitlementCapture(
                section_keys=("9",),
                machine_identifier=_MACHINE_ID,
                captured_at=read_first,
            ),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()

    assert stored is False
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["1", "3"]
    # SQLite hands back a naive datetime; the instant is what matters here.
    stamped = user.entitlements_captured_at
    assert stamped is not None
    assert stamped.replace(tzinfo=UTC) == read_second


async def test_a_recapture_after_a_revoke_is_never_blocked_by_the_cleared_row(
    sessionmaker_: SessionMaker,
) -> None:
    """A confirmed revoke clears all three columns together. If it left
    ``entitlements_captured_at`` behind, that stale stamp would look newer than
    the user's next real capture and silently refuse it forever."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    async with sessionmaker_() as session:
        assert await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("1",), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()

    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        _clear_entitlements(user)
        await session.commit()
    assert (await _load(sessionmaker_, user_id)).entitlements_captured_at is None

    async with sessionmaker_() as session:
        assert await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("2",), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    assert (await _load(sessionmaker_, user_id)).entitled_section_keys == ["2"]


async def test_capture_and_store_round_trip_from_a_real_adapter(
    sessionmaker_: SessionMaker,
) -> None:
    """End to end over the adapter: what the server filtered is what lands."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_)
    capture = await capture_entitlements(
        _library(_per_token_handler, _SHARED_TOKEN),
        machine_identifier=_MACHINE_ID,
        user_id=user_id,
    )
    assert capture is not None
    async with sessionmaker_() as session:
        await store_entitlements(
            session,
            user_id,
            capture,
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["1"]
    assert user.entitlements_machine_id == _MACHINE_ID
    # The stamp is fresh; the sweep's own columns are untouched by capture.
    assert user.share_state is None
    assert user.share_checked_at is None


async def test_two_tokens_do_not_share_a_cached_section_view() -> None:
    """``PlexLibrary`` keys its sections cache by base_url + a HASH of the token.
    If it did not, capturing for one user would serve (or poison) another's view
    and every entitlement would be wrong in the same invisible way."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("X-Plex-Token", "")
        calls.append(token)
        return _per_token_handler(request)

    owner = await capture_entitlements(
        _library(handler, _OWNER_TOKEN), machine_identifier=_MACHINE_ID, user_id=1
    )
    shared = await capture_entitlements(
        _library(handler, _SHARED_TOKEN), machine_identifier=_MACHINE_ID, user_id=2
    )
    assert owner is not None
    assert shared is not None
    assert owner.section_keys == ("1", "2", "3")
    assert shared.section_keys == ("1",)
    # Both really asked the server; neither was served the other's cache entry.
    assert calls == [_OWNER_TOKEN, _SHARED_TOKEN]


async def test_capture_does_not_touch_unrelated_user_columns(
    sessionmaker_: SessionMaker,
) -> None:
    """Capture writes exactly two columns. A share verdict written by the sweep
    must survive a capture landing right after it."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    checked_at = datetime.now(UTC) - timedelta(minutes=1)
    user_id = await _add_user(
        sessionmaker_,
        encrypted_plex_token=_SHARED_TOKEN,
        share_state="authorized",
        share_checked_at=checked_at,
        share_check_failures=2,
    )
    async with sessionmaker_() as session:
        await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("1",), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=None,
        )
        await session.commit()
    user = await _load(sessionmaker_, user_id)
    assert user.share_state == "authorized"
    assert user.share_check_failures == 2
    assert user.encrypted_plex_token == _SHARED_TOKEN


# --------------------------------------------------------------------------- #
# Fresher captures win; stale ones are refused (Codex round on PR #560)
# --------------------------------------------------------------------------- #
async def test_a_capture_taken_with_a_rotated_token_is_refused(
    sessionmaker_: SessionMaker,
) -> None:
    """A sweep capture takes seconds; a sign-in can rotate the token and land a
    FRESHER capture in that window. Without the token condition on the write, the
    slow stale sweep write would overwrite the fresh one with the old
    credential's view of the library."""
    await _configure_anchor(sessionmaker_, _MACHINE_ID)
    user_id = await _add_user(sessionmaker_, encrypted_plex_token="old-token")  # noqa: S106

    # The sweep reads the credential it is about to capture with...
    async with sessionmaker_() as session:
        stale_ciphertext = await read_token_ciphertext(session, user_id)

    # ... then a sign-in rotates the token AND stores its own fresher capture.
    async with sessionmaker_() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.encrypted_plex_token = "rotated-token"  # noqa: S105
        await session.commit()
    async with sessionmaker_() as session:
        fresh_ciphertext = await read_token_ciphertext(session, user_id)
        assert await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("1", "2"), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=fresh_ciphertext,
        )
        await session.commit()

    # The sweep's stale write lands last but must change nothing.
    async with sessionmaker_() as session:
        stored = await store_entitlements(
            session,
            user_id,
            EntitlementCapture(section_keys=("9",), machine_identifier=_MACHINE_ID),
            anchor_setting_key=_ANCHOR_KEY,
            expected_token_ciphertext=stale_ciphertext,
        )
        await session.commit()

    assert stored is False
    user = await _load(sessionmaker_, user_id)
    assert user.entitled_section_keys == ["1", "2"]


async def test_entitlement_reads_never_come_from_the_section_cache() -> None:
    """Entitlements are point-in-time AUTHORIZATION data. A cached read would
    both stamp a stale section set and let a since-unreachable server look like a
    success -- mis-counting it as ``captured``."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _per_token_handler(request)

    library = _library(handler, _SHARED_TOKEN)
    first = await capture_entitlements(library, machine_identifier=_MACHINE_ID, user_id=7)
    second = await capture_entitlements(library, machine_identifier=_MACHINE_ID, user_id=7)

    assert first is not None
    assert second is not None
    # Two captures, two real reads -- the second was not served from the TTL cache.
    assert calls == 2


async def test_the_owner_baseline_is_only_resolved_after_a_successful_read() -> None:
    """A black-holed host must cost ONE timeout, not two: the baseline is
    telemetry, so spending a call on it before knowing the server answers at all
    would double the cost of the very failure the circuit breaker exists for."""
    baseline_calls = 0

    async def _baseline_resolver() -> int | None:
        nonlocal baseline_calls
        baseline_calls += 1
        return 3

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("black hole", request=request)

    failed = await capture_entitlements(
        _library(unreachable, _SHARED_TOKEN),
        machine_identifier=_MACHINE_ID,
        user_id=7,
        service_section_count=_baseline_resolver,
    )
    assert failed is None
    assert baseline_calls == 0

    ok = await capture_entitlements(
        _library(_per_token_handler, _SHARED_TOKEN),
        machine_identifier=_MACHINE_ID,
        user_id=7,
        service_section_count=_baseline_resolver,
    )
    assert ok is not None
    assert baseline_calls == 1
