# ADR-0016: Plex owner login sessions (client-side PIN flow, local session cookie)

- **Status:** Accepted
- **Date:** 2026-07-04
- **Context builds on:** [ADR-0005](0005-zero-terminal-web-operability.md)
  (zero-terminal, web-operable — the break-glass key stays the terminal-free
  recovery path this ADR must never remove), [ADR-0009](0009-frontend-typed-spa.md)
  (the typed SPA + contract-bound client the login UI plugs into).
- **Supersedes:** the "OAuth deferred to a later effort" posture recorded against
  issue [#28](https://github.com/LunchBox951/Plex-Management/issues/28) during the
  movies-first beta. Closes #28.

## Context

Through the beta, the only browser credential was the static app `X-Api-Key`:
minted once by `POST /setup/complete`, shown one time, pasted into every new
browser. Issue #28 called this out as unintuitive — signing in from a phone or a
tablet meant copy-pasting a 43-char secret. The stack we are collapsing solved
this with Plex hosted sign-in, and Plex hands us an *authoritative* owner signal
for free (the account that owns the configured server is the administrator), so
we do not have to invent our own admin role model.

Two precedents were studied directly:

- **Overseerr** runs *both* an API key (for automation / Radarr-Sonarr-style
  service calls) **and** Plex OAuth for humans, simultaneously. It does not force
  a choice between them. We adopt the same both-at-once posture.
- **Ombi**'s Plex login is a recurring source of the `clientId`-mismatch / plex.tv
  error-`1020` failure class: the `X-Plex-Client-Identifier` used to mint the PIN
  drifts from the one used to open the hosted auth URL (or is regenerated per
  request), and plex.tv rejects the exchange. We avoid that entire class by
  generating our client identifier **once**, persisting it
  (`plex_oauth_client_identifier` in the settings store), and reusing that exact
  value for PIN creation, the auth URL, and every poll.

The hard constraint is ADR-0005: the `:stable` deployment is 100% web-operable and
the operator is **never locked out**. plex.tv is a third party that can be down;
an account can be de-owned; a session can expire. So Plex login becomes the
*normal* path — never the *only* path.

## Decision

Add Plex hosted sign-in as the normal browser admin auth path after setup, backed
by a locally-validated session cookie, while keeping the static `X-Api-Key` valid
forever as automation + break-glass.

### Client-side PIN flow (the anti-1020 shape)

1. The SPA calls `POST /api/v1/auth/plex/start`. The backend creates a plex.tv
   strong PIN (`POST https://plex.tv/api/v2/pins`) with the persisted client
   identifier, records a `plex_login_states` row (the `state` nonce, the PIN id,
   and a **hashed** pre-login browser nonce), sets an HTTP-only `plexmgr.login`
   cookie bound to that state, and returns the `app.plex.tv/auth` URL.
2. The browser completes sign-in on plex.tv and is forwarded back to
   `/auth/plex/callback?state=…` (origin from `public_base_url`, else the request
   origin).
3. The SPA calls `POST /api/v1/auth/plex/complete` with the `state`. The backend
   verifies the `plexmgr.login` nonce, **atomically** consumes the login state
   (an `UPDATE … WHERE consumed_at IS NULL`, so a replayed callback 409s rather
   than minting a second session), polls the PIN for the user token, and proceeds
   to owner detection.

### Owner detection → admin tier

On completion the backend fetches the signed-in account and its plex.tv
`resources`, then reads the configured server's own `machineIdentifier`
(`GET {plex_url}/identity`) and matches it against the account's resources:

- **No matching resource** → `403 not_valid_plex_user`. A Plex account with no
  access to *this* server never gets a session.
- **Matching resource, `owned=true`** → administrator (`permissions = 1`).
- **Matching resource, `owned=false`** (a shared/managed Plex user) →
  authenticated but non-admin (`permissions = 0`). The user default and the
  migration default are both non-admin.

### Local session cookie (plex.tv is not on the request path)

A successful completion issues a random session token, stored **only as a
SHA-256 hash** in `auth_sessions`, and set as an HTTP-only `plexmgr.session`
cookie (30-day expiry). Every subsequent request is authenticated by looking that
hash up in our own DB and checking expiry/revocation — **plex.tv is never called
per request.** This is deliberate and load-bearing for ADR-0005: an already
signed-in operator keeps working through a full plex.tv outage; only *minting a
new* session needs plex.tv.

### CSRF: double-submit

Because the session lives in a cookie, unsafe methods (anything but
GET/HEAD/OPTIONS/TRACE) require a double-submit CSRF check: a readable
`plexmgr.csrf` cookie (`httponly=false`) whose value must be echoed in the
`X-CSRF-Token` header and constant-time-compared. API-key callers are *not*
subject to CSRF — they never carry the cookie, and forcing it would break
automation.

### Admin vs shared enforcement

`require_admin` guards admin-only routers; a shared Plex user authenticates but
gets `403 admin_required` on admin routes/actions, and request views are scoped
to their own records. The frontend mirrors this with an `AdminGate` + nav
filtering, but the backend guard is authoritative.

### The static `X-Api-Key` is retained permanently

The app key stays a first-class credential for three roles the Plex path cannot
serve:

- **Automation** — scripts / Radarr-Sonarr-style callers that have no browser and
  no Plex account.
- **Break-glass + plex.tv-outage fallback** — when Plex is unreachable, or the
  owner needs in without an OAuth round-trip, `KeyEntry` accepts the key and the
  operator is back in with no terminal.

The SPA only attaches the key after an explicit access-key opt-in
(`enableApiKeyAuth`); normal operation rides the session cookie. App-key rotation
keys its compare-and-swap off the *actual* auth method of the rotating request,
so a session-authed admin and a key-authed admin both rotate correctly.

> **CodeQL #263 (localStorage key persistence) is knowingly left open.** The
> break-glass key is persisted in `localStorage` on purpose: it is the sole
> mechanism that keeps break-glass access durable across a page reload *during* a
> plex.tv outage, which is exactly the scenario break-glass exists for. Making it
> memory-only would delete the sink but introduce a narrow ADR-0005 lockout
> (outage + no live session + reload + operator who relied on browser
> persistence). The genuine, theater-free fix — exchanging a valid `X-Api-Key`
> for the same HTTP-only session cookie so the key never needs JS-readable
> storage — is a backend change deferred to a follow-up, not this PR.

### New config knobs

- `public_base_url` — the public origin used to build the Plex callback URL, for
  reverse-proxy / non-local deployments. Unset ⇒ the request origin is used.
- `auth_cookie_secure` — override the cookies' `Secure` attribute for a
  TLS-terminating proxy that speaks plain HTTP to the app. `None` ⇒ infer from the
  request scheme.

### Schema

New tables `auth_sessions` (hashed session tokens, `last_seen_at`, expiry,
revocation) and `plex_login_states` (pending PIN challenges, hashed browser
nonce), plus per-user Plex identity columns, all under Alembic migration
`7bcbce2c2e2b` (parented off the single prior head `b7e2d4f6c8a1`).

## Explicitly out of scope

- **Removing or weakening the `X-Api-Key` path.** It is retained by design; the
  key remains the never-locked-out recovery path (ADR-0005).
- **Making break-glass durable without JS-readable storage** (the real CodeQL
  #263 fix). Deferred to a follow-up: mint an HTTP-only session cookie from a
  valid `X-Api-Key`.
- **Session lifecycle polish** — sliding `last_seen_at` refresh, a sweep for
  expired `plex_login_states` / `auth_sessions` rows, and re-use of the per-user
  encrypted Plex token. Recorded as follow-ups, not blockers.
- **Managed-user granular permissions.** Shared users are simply non-admin; a
  richer per-user permission model is a later effort.

## Consequences

- Signing in from any device is a Plex click, not a pasted secret — issue #28's
  actual complaint.
- The owner/admin signal comes from Plex's own `owned` flag against the configured
  server's `machineIdentifier`; we do not maintain a separate admin roster.
- An existing session survives a full plex.tv outage because sessions validate
  locally; the static key covers minting access *during* an outage.
- The both-credentials-at-once posture matches Overseerr; the persisted single
  client identifier sidesteps Ombi's `clientId`/1020 failure class.
- One accepted-open security alert (CodeQL #263) and a short list of session
  lifecycle follow-ups are the tracked debt this ADR takes on knowingly.
