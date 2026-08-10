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

This module owns policy only -- no web imports, no enforcement, no callers
that act on a verdict. ``deps``/routers/``app`` (and, later, the session sweep)
depend on this module; it never depends on them, mirroring the discipline in
``session_lifecycle.py``. This PR (stage 1 of the design) wires nothing new
up: nothing calls :func:`check_share` yet outside its delegate and tests, no
loop reads or writes ``users.share_state``, and no route enforces anything
based on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from plex_manager.adapters.plex.oauth import (
    CODE_TOKEN_INVALID,
    PlexTvClient,
    PlexVerifyError,
    account_server_resource,
)

__all__ = [
    "EntitlementSnapshot",
    "ShareVerdict",
    "check_share",
]


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
