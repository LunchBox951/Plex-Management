"""Entitlement capture at sign-in (issue #484 PR-3) -- the second capture site.

Sign-in is the cheapest capture point in the system: it is already holding a
fresh, just-verified token, so a user's section entitlements are current from
their very first session instead of waiting up to one revalidation interval for
the sweep to reach them.

Everything here is about that capture being ENRICHMENT and never a gate: it runs
after the session is already minted, and no failure of it -- unreachable server,
refused token, unconfigured Plex, missing anchor -- may change the sign-in's
outcome or disturb a previously captured snapshot. Nothing reads these columns
yet (PR-4/PR-5); this exists so the canary can observe whether a shared user's
token really does come back section-filtered.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import reset_caches
from plex_manager.models import User
from plex_manager.web.background import detached_tasks
from plex_manager.web.deps import PLEX_MACHINE_ID_SETTING, SettingsStore
from plex_manager.web.routers import auth as auth_module

# This module is the one that exercises the real detached capture.
pytestmark = pytest.mark.entitlement_capture

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"
_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token for MockTransport
_MACHINE_ID = "abc123machine"
_PLEX_URL = "http://plex.local:32400"

_USER: dict[str, object] = {
    "id": 99,
    "username": "shared-viewer",
    "email": "viewer@example.com",
    "thumb": None,
}


@pytest.fixture(autouse=True)
def reset_throttle() -> None:
    """Clear the in-process sign-in throttle so tests never leak attempt counts."""
    auth_module.reset_sign_in_throttle()


@pytest.fixture(autouse=True)
def _clear_library_caches() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    reset_caches()
    yield
    reset_caches()


def _shared_server() -> dict[str, object]:
    return {
        "name": "Living Room",
        "clientIdentifier": _MACHINE_ID,
        "provides": "server",
        "owned": False,
        "connections": [],
    }


def _owned_server() -> dict[str, object]:
    """The same server, OWNED: what the pre-init first-owner claim requires."""
    return {**_shared_server(), "owned": True}


def _sections(*keys: str) -> dict[str, object]:
    return {
        "MediaContainer": {
            "size": len(keys),
            "Directory": [
                {
                    "key": key,
                    "title": f"S{key}",
                    "type": "movie",
                    "Location": [{"id": 1, "path": "/x"}],
                }
                for key in keys
            ],
        }
    }


def _transport(
    *,
    sections_status: int = 200,
    section_keys: tuple[str, ...] = ("1", "3"),
    seen: list[str] | None = None,
    live_identity: str = _MACHINE_ID,
    resources: list[dict[str, object]] | None = None,
) -> httpx.MockTransport:
    """plex.tv's v2 endpoints plus the configured server's ``/identity`` and
    ``/library/sections``.

    ``live_identity`` is what the server at ``plex_url`` claims to be when the
    detached capture confirms the anchor before stamping -- the same id means the
    anchor holds; a different one means the host was replaced. ``resources`` is
    plex.tv's ``/resources`` answer (default: the server, shared).
    """
    if resources is None:
        resources = [_shared_server()]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if request.url.host == "plex.tv" and path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and path == "/api/v2/resources":
            return httpx.Response(200, json=resources)
        if path == "/identity":
            return httpx.Response(
                200, json={"MediaContainer": {"machineIdentifier": live_identity}}
            )
        if path == "/library/sections":
            # The capture must use the SIGNED-IN user's token, never a stored one.
            assert request.headers.get("X-Plex-Token") == _TOKEN
            if sections_status != 200:
                return httpx.Response(sections_status, json={})
            return httpx.Response(200, json=_sections(*section_keys))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


async def _configure(
    sessionmaker_: SessionMaker, *, url: str | None, machine_id: str | None
) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        if machine_id is not None:
            await store.set(PLEX_MACHINE_ID_SETTING, machine_id)
        if url is not None:
            await store.set("plex_url", url)
        await session.commit()


async def _signed_in_user(sessionmaker_: SessionMaker) -> User:
    async with sessionmaker_() as session:
        return (await session.execute(select(User).where(User.plex_id == 99))).scalars().one()


async def _sign_in(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})


async def _sign_in_and_settle(app: FastAPI, client: httpx.AsyncClient) -> httpx.Response:
    """Sign in, then wait for the DETACHED capture task to finish.

    The capture deliberately does not run on the request path, so a test that
    asserts on its effect has to await it explicitly rather than assume the
    response implies it happened.
    """
    response = await _sign_in(client)
    await asyncio.gather(*detached_tasks(app), return_exceptions=True)
    return response


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_sign_in_captures_the_users_visible_sections(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport())

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]
    assert user.entitlements_machine_id == _MACHINE_ID


async def test_sign_in_captures_an_honest_empty_entitlement(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A user the server shows no sections to captures `[]`, not NULL: "captured,
    entitled to nothing" must stay distinguishable from "never captured"."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport(section_keys=()))

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == []
    assert user.entitlements_machine_id == _MACHINE_ID


# --------------------------------------------------------------------------- #
# Never a gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sections_status", [401, 500])
async def test_a_failing_capture_never_fails_the_sign_in(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    sections_status: int,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport(sections_status=sections_status))

    response = await _sign_in_and_settle(app, client)

    # The session is minted and usable regardless of the capture's fate.
    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is False
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None


async def test_an_unreachable_server_never_fails_the_sign_in(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_shared_server()])
        raise httpx.ConnectError("plex server unreachable", request=request)

    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, httpx.MockTransport(handler))

    assert (await _sign_in_and_settle(app, client)).status_code == 200
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None


async def test_a_failed_capture_leaves_a_previous_snapshot_untouched(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The rule everywhere capture appears: a failure never DEGRADES what is
    already known. Clearing on failure would look like a real entitlement change
    to the enforcement PRs that follow."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)

    # First sign-in captures cleanly...
    await _use_transport(app, _transport(section_keys=("1", "3")))
    assert (await _sign_in_and_settle(app, client)).status_code == 200
    assert (await _signed_in_user(sessionmaker_)).entitled_section_keys == ["1", "3"]

    # ... a later one cannot read the server, and must not erase it.
    await _use_transport(app, _transport(sections_status=500))
    assert (await _sign_in_and_settle(app, client)).status_code == 200
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]
    assert user.entitlements_machine_id == _MACHINE_ID


# --------------------------------------------------------------------------- #
# Nothing to anchor to: skip rather than guess
# --------------------------------------------------------------------------- #
async def test_no_cached_anchor_means_no_capture_and_no_server_call(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A capture is only meaningful stamped with the anchor it was taken against.
    With no cached ``plex_machine_identifier`` the sign-in skips capture outright
    rather than putting a live identity probe on the sign-in path -- the sweep
    resolves it properly within one interval."""
    await seed(initialized=True, app_api_key=_API_KEY)
    # An upgraded install: url configured, identity never cached. The access
    # decision falls back to a live /identity probe, which this transport answers.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_shared_server()])
        if request.url.path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", _PLEX_URL)
        await store.set("plex_token", "service-token")
        await session.commit()
    await _use_transport(app, httpx.MockTransport(handler))

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    assert "/library/sections" not in seen
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None


async def test_unconfigured_plex_url_means_no_capture(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=None, machine_id=_MACHINE_ID)
    seen: list[str] = []
    await _use_transport(app, _transport(seen=seen))

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    assert "/library/sections" not in seen
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None


async def test_capture_does_not_disturb_the_sign_ins_own_writes(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Capture commits after the sign-in's own commit; the identity, permissions
    and stored token it just persisted must all survive it."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport())

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    user = await _signed_in_user(sessionmaker_)
    assert user.username == "shared-viewer"
    assert user.permissions == 0
    assert user.encrypted_plex_token == _TOKEN
    assert user.entitled_section_keys == ["1", "3"]


# --------------------------------------------------------------------------- #
# Off the request path (the reason capture is detached at all)
# --------------------------------------------------------------------------- #
async def test_sign_in_returns_while_the_capture_hangs_on_a_black_holed_server(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The failure this design avoids: ``plex_url`` pointing at an address that
    accepts the connection and never answers. Awaited on the request path that
    would stall EVERY sign-in for the full client timeout -- the one endpoint an
    operator needs working when the install is misconfigured.

    The transport here never resolves until the test cancels it, so a sign-in
    that completes at all proves the capture is not on its path.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    reached_server = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_shared_server()])
        # The black hole: connected, never answering.
        reached_server.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    await _use_transport(app, httpx.MockTransport(handler))

    # No timeout wrapper: if the capture were awaited inline this would hang the
    # test rather than fail it, which is exactly the production symptom.
    async with asyncio.timeout(10):
        response = await _sign_in(client)

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is False

    # The capture really is in flight (not skipped) -- and still pending.
    async with asyncio.timeout(10):
        await reached_server.wait()
    pending = detached_tasks(app)
    assert len(pending) == 1
    for task in tuple(pending):
        assert not task.done()
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # The session is fully usable while the capture never completed.
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None


async def test_the_detached_capture_is_held_so_it_cannot_be_garbage_collected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The event loop keeps only a WEAK reference to a running task, so
    fire-and-forget work that nobody holds can be collected mid-flight. The
    capture must be reachable from app state until it finishes -- and gone
    afterwards, so a long-lived process does not accumulate them."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport())

    assert (await _sign_in(client)).status_code == 200
    spawned = tuple(detached_tasks(app))
    assert len(spawned) == 1

    await asyncio.gather(*spawned, return_exceptions=True)

    # Held while running, released once done.
    assert detached_tasks(app) == set()
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]


# --------------------------------------------------------------------------- #
# The anchor must be CONFIRMED, and it must be operator-verified to exist
# --------------------------------------------------------------------------- #
async def test_a_replacement_server_at_the_same_url_is_never_stamped(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The hole the settings-row guard cannot see: a DIFFERENT Plex server now
    answers at the same ``plex_url``. Its sections would be written under the old
    cached identifier -- and the row still holds that id, so the guard accepts it.
    The live /identity confirm is what catches it."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    seen: list[str] = []
    await _use_transport(app, _transport(seen=seen, live_identity="a-completely-different-server"))

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    # Confirmed first, and the section read never happened.
    assert "/identity" in seen
    assert "/library/sections" not in seen
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None


async def test_the_anchor_is_confirmed_before_any_capture_is_stamped(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    seen: list[str] = []
    await _use_transport(app, _transport(seen=seen))

    assert (await _sign_in_and_settle(app, client)).status_code == 200

    assert seen.index("/identity") < seen.index("/library/sections")
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]


# --------------------------------------------------------------------------- #
# The detached capture carries ITS OWN credential (Codex round 3 on PR #560)
# --------------------------------------------------------------------------- #
async def test_an_older_overlapping_capture_cannot_overwrite_a_newer_one(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Two sign-ins overlap and the OLDER capture task stores LAST.

    The older task captured with the older token. If it re-read the ciphertext at
    store time it would pick up the NEWER sign-in's value, pass the write guard,
    and overwrite the newer credential's snapshot with the old token's view. The
    ciphertext is captured at sign-in commit time and carried INTO the task, so
    the older one no longer matches and writes nothing.

    The ordering is pinned, not hoped for: the first task is held inside its
    section read until the second has fully stored.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)

    older_reached = asyncio.Event()
    release_older = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_shared_server()])
        if request.url.path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}})
        token = request.headers.get("X-Plex-Token")
        if token == _TOKEN:
            # The OLDER capture: hold it mid-read until the newer one has landed.
            older_reached.set()
            await release_older.wait()
            return httpx.Response(200, json=_sections("9"))
        return httpx.Response(200, json=_sections("1", "3"))

    await _use_transport(app, httpx.MockTransport(handler))

    assert (await _sign_in(client)).status_code == 200
    older_tasks = tuple(detached_tasks(app))
    assert len(older_tasks) == 1
    async with asyncio.timeout(10):
        await older_reached.wait()

    # A SECOND sign-in rotates the stored token and stores ITS view first.
    auth_module.reset_sign_in_throttle()
    newer = await client.post("/api/v1/auth/plex", json={"auth_token": "rotated-token"})
    assert newer.status_code == 200
    newer_tasks = tuple(t for t in detached_tasks(app) if t not in older_tasks)
    await asyncio.gather(*newer_tasks, return_exceptions=True)

    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]
    assert user.encrypted_plex_token == "rotated-token"  # noqa: S105

    # Now let the OLDER task finish and store. It must change nothing.
    release_older.set()
    await asyncio.gather(*older_tasks, return_exceptions=True)

    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]


async def test_a_pre_init_capture_carries_the_ciphertext_its_own_sign_in_wrote(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #572 residual 2: the pre-init claim path mints without
    ``secret_rotation_lock``, so nothing stops a concurrent sign-in by the SAME
    claimant (the claim CAS resumes it rather than refusing it) from rotating the
    stored token between this sign-in's commit and a post-commit ciphertext read.
    Read there, the detached capture -- taken with the OLD token -- would carry
    the NEW credential's ciphertext, pass the write guard, and stamp the old
    token's view as the new credential's snapshot.

    The rotation is injected at the only place it can land: right after the
    issuance commit, before control returns to the sign-in. The capture must
    then be REFUSED, exactly as the rotation branch's in-transaction read
    guarantees post-init.
    """
    await seed(initialized=False)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)
    await _use_transport(app, _transport(resources=[_owned_server()]))
    rotated_token = "rotated-by-a-concurrent-sign-in"  # noqa: S105 - fake token

    issue_browser_session = auth_module._issue_browser_session  # pyright: ignore[reportPrivateUsage]

    async def issue_then_rotate(
        session: AsyncSession, response: Response, *, request: Request, user_id: int | None
    ) -> None:
        await issue_browser_session(session, response, request=request, user_id=user_id)
        async with sessionmaker_() as other:
            await other.execute(
                update(User).where(User.id == user_id).values(encrypted_plex_token=rotated_token)
            )
            await other.commit()

    monkeypatch.setattr(auth_module, "_issue_browser_session", issue_then_rotate)

    with caplog.at_level(logging.INFO, logger="plex_manager.services.plex_access_service"):
        response = await _sign_in_and_settle(app, client)

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    user = await _signed_in_user(sessionmaker_)
    assert user.encrypted_plex_token == rotated_token
    # The old-token capture was discarded, not stamped under the new credential.
    assert user.entitled_section_keys is None
    assert user.entitlements_machine_id is None
    assert any("was not stored" in record.getMessage() for record in caplog.records)


async def test_a_malformed_section_response_never_blanks_a_snapshot(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A 200 with no ``MediaContainer`` is a proxy hiccup, not "entitled to
    nothing". Persisting ``[]`` for it would be a wrongful blackout once PR-5
    enforces on these columns."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure(sessionmaker_, url=_PLEX_URL, machine_id=_MACHINE_ID)

    # A good capture first.
    await _use_transport(app, _transport(section_keys=("1", "3")))
    assert (await _sign_in_and_settle(app, client)).status_code == 200
    assert (await _signed_in_user(sessionmaker_)).entitled_section_keys == ["1", "3"]

    # Now the server answers 200 with a body that has no MediaContainer.
    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_shared_server()])
        if request.url.path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}})
        return httpx.Response(200, json={"error": "gateway hiccup"})

    auth_module.reset_sign_in_throttle()
    await _use_transport(app, httpx.MockTransport(malformed))
    assert (await _sign_in_and_settle(app, client)).status_code == 200

    # The prior snapshot survives -- it was NOT blanked to [].
    user = await _signed_in_user(sessionmaker_)
    assert user.entitled_section_keys == ["1", "3"]
