"""``plex_access_service.check_share``'s verdict ladder (issue #391 PR-1).

Mirrors ``test_watchlist_service.py``'s revalidation tests (the ladder was
extracted from there), plus the TOKEN_STALE/SHARE_REVOKED split those tests
never needed: watchlist sync collapses both into one STALE outcome, but this
module's callers must be able to act on them differently.
"""

from __future__ import annotations

import httpx

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.services.plex_access_service import EntitlementSnapshot, ShareVerdict, check_share

_MACHINE_ID = "configured-server-machine-id"
_TOKEN = "user-plex-token"  # noqa: S105


def _resources_transport(resources: list[dict[str, object]] | int) -> httpx.MockTransport:
    """A plex.tv ``/api/v2/resources`` transport. Pass an int to answer that
    status code (e.g. 401 for a rejected token) instead of a resource array."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(resources, int):
            return httpx.Response(resources, json={})
        return httpx.Response(200, json=resources)

    return httpx.MockTransport(handler)


def _server_resource(machine_id: str, *, owned: bool = True) -> dict[str, object]:
    return {
        "name": "Living Room",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": owned,
        "connections": [],
    }


async def test_authorized_when_account_reaches_configured_server() -> None:
    transport = _resources_transport([_server_resource(_MACHINE_ID)])
    async with httpx.AsyncClient(transport=transport) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot == EntitlementSnapshot(
        verdict=ShareVerdict.AUTHORIZED, section_keys=None, machine_identifier=_MACHINE_ID
    )


async def test_share_revoked_when_account_has_no_access_to_configured_server() -> None:
    # A successful, authoritative /resources answer that lacks the configured
    # server: the share is confirmed gone, not merely an unauthenticated token.
    transport = _resources_transport([_server_resource("some-other-server")])
    async with httpx.AsyncClient(transport=transport) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.SHARE_REVOKED
    assert snapshot.section_keys is None
    assert snapshot.machine_identifier == _MACHINE_ID


async def test_share_revoked_when_resources_genuinely_empty() -> None:
    # A genuine empty array IS a valid authorization signal: the account has zero
    # server resources, so it cannot reach the configured server.
    async with httpx.AsyncClient(transport=_resources_transport([])) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.SHARE_REVOKED


async def test_token_stale_when_token_rejected() -> None:
    # This is the split watchlist_service.revalidate_sync_user never needed:
    # plex.tv rejected the credential outright (401/403), so it never got far
    # enough to answer whether the share is live. Distinct from SHARE_REVOKED.
    async with httpx.AsyncClient(transport=_resources_transport(401)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.TOKEN_STALE
    assert snapshot.section_keys is None
    assert snapshot.machine_identifier == _MACHINE_ID


async def test_token_stale_when_token_rejected_forbidden() -> None:
    async with httpx.AsyncClient(transport=_resources_transport(403)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.TOKEN_STALE


async def test_unknown_when_plex_tv_errors_server_side() -> None:
    # The single highest-consequence property of this ladder: a plex.tv 5xx
    # reaches UNKNOWN through the bad-response branch (a DIFFERENT branch from
    # the 401/403 invalid-token check), never TOKEN_STALE -- once the sweep
    # signs users out on TOKEN_STALE, a plex.tv outage must not sign anyone out.
    async with httpx.AsyncClient(transport=_resources_transport(503)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.UNKNOWN


async def test_unknown_when_plex_tv_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    # A transient plex.tv outage must never be read as loss: UNKNOWN.
    assert snapshot.verdict is ShareVerdict.UNKNOWN


async def test_unknown_when_resources_malformed() -> None:
    # A 2xx /resources body that is NOT the expected JSON array must NOT be read
    # as "zero resources" -> SHARE_REVOKED. It is an undetermined result (#296).
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert snapshot.verdict is ShareVerdict.UNKNOWN


async def test_unverifiable_when_no_token_stored() -> None:
    # No network call at all: callers holding a User row can pass
    # ``user.encrypted_plex_token`` straight through even when it's None.
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500))) as (
        client
    ):
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        snapshot = await check_share(plex_tv, _MACHINE_ID, token=None)
    assert snapshot == EntitlementSnapshot(
        verdict=ShareVerdict.UNVERIFIABLE, section_keys=None, machine_identifier=_MACHINE_ID
    )
