# ADR-0016: Plex-first setup and browser-side Plex sign-in

- **Status:** Accepted — 2026-07-06
- **Deciders:** LunchBox951 (owner)
- **Context builds on:** [ADR-0001](0001-integrated-app-borrowed-brains.md)
  (borrowed brains — we copy Overseerr's proven Plex sign-in shape rather than
  invent one), [ADR-0005](0005-zero-terminal-web-operability.md) (zero-terminal,
  web-operable — the operator is never locked out; the recovery key stays a
  terminal-free path), [ADR-0009](0009-frontend-typed-spa.md) (the typed SPA the
  sign-in UI and setup wizard plug into).
- **Closes** [#28](https://github.com/LunchBox951/Plex-Management/issues/28) — the
  "signing in means pasting a 43-char secret" complaint, and the "OAuth deferred"
  posture recorded against it during the movies-first beta.

## Context

Through the beta, the only browser credential was a static app `X-Api-Key`: minted
once by setup, shown one time, pasted into every new browser. Issue #28 called this
out — signing in from a phone or tablet meant copy-pasting a secret. The stack we
are collapsing solved this with Plex hosted sign-in, and Plex hands us an
*authoritative* owner signal for free: the account that **owns** the configured
server is the administrator, so we do not have to invent an admin-role model
(ADR-0001 — borrow the proven brain).

The hard constraint is ADR-0005: the `:stable` deployment is 100% web-operable and
the operator is **never** locked out. plex.tv is a third party that can be down; a
session can expire. So Plex sign-in becomes the *normal* path — never the *only*
one.

### What we tried first, and why we replaced it

The first cut of this work put the PIN dance on the **server**: a
`POST /auth/plex/start` created the PIN and recorded a pending challenge in a
`plex_login_states` table; the browser was **redirected** to plex.tv and then back
to a server `/auth/plex/callback` route whose return origin came from a new
`public_base_url` config knob (with a `forwardUrl` carried through the round-trip);
`POST /auth/plex/complete` consumed the login-state row and polled for the token.

Three problems sank that shape:

1. **The callback needed a correct public origin.** Behind a reverse proxy or on a
   non-local host, `public_base_url` had to be set exactly right or the redirect
   bounced to the wrong place — precisely the reverse-proxy fragility ADR-0005 asks
   us to design out.
2. **It added server-side login state** (the `plex_login_states` table) purely to
   survive a redirect, with its own replay/consume/expiry handling.
3. **The mock-drift incident (the recorded lesson).** Its tests mocked plex.tv's
   **v1** `/api/resources` endpoint as JSON. That endpoint is **XML-only** — it has
   no JSON representation — so the tests were green against a fiction the real API
   never returns. The bug only surfaced against live plex.tv. Lesson, now load-
   bearing for this ADR's tests: **mock the real payload shape.** We moved to
   plex.tv's genuinely-JSON `api/v2` endpoints, and the resource parser still
   tolerates XML-derived boolean encodings (a real owner's `owned` can arrive as
   `true` / `1` / `"1"`) and **fails closed** on any unknown shape rather than
   mis-granting ownership (`adapters/plex/oauth.py`, `_get_bool`).

## Decision

The browser runs the plex.tv PIN flow itself and hands the resulting token to a
**single** server-side verify endpoint. There is no redirect, no callback route, no
public-origin knob, and no server login-state table. Plex sign-in is the sole
credential model for setup and normal browser use; an **opt-in** recovery key
covers automation and break-glass.

### Browser-side PIN flow (`frontend/src/lib/plexOAuth.ts`)

Overseerr's popup-and-poll pattern (ADR-0001):

1. Pre-open a popup **synchronously** from the click handler (popup blockers only
   permit `window.open` on a direct user gesture); it points at the app's own
   `/login/plex/loading` branded spinner until the flow navigates it onward.
2. Create a strong PIN on plex.tv (`POST https://plex.tv/api/v2/pins?strong=true`).
3. Navigate the popup to plex.tv's hosted login (`app.plex.tv/auth`) for that PIN.
4. Poll `GET https://plex.tv/api/v2/pins/{id}` once a second until it carries an
   `authToken`, then hand that token to the backend.

A stable per-install client identifier is persisted in `localStorage` (with an
in-memory fallback for private-mode browsers) and reused for the PIN create, the
auth URL, and every poll — so the identifier never drifts mid-flow. Every terminal
failure is one of four typed, retryable codes (`plex_popup_blocked`,
`plex_popup_closed`, `plex_pin_expired`, `plex_tv_unreachable_browser`); the token
is never logged nor placed in an error message.

### One verify endpoint that re-derives everything (`web/routers/auth.py`)

`POST /api/v1/auth/plex` takes the browser's `auth_token` and **never trusts the
browser's claims**. Identity and server ownership are re-derived server-side from
plex.tv's v2 API — `/api/v2/user` (a flat account object) and `/api/v2/resources`
(the account's devices) via `adapters/plex/oauth.py` — before any user or session
row is written (north star #3 — honest, re-derived state). A best-effort in-process
per-IP throttle brakes abuse of this one unauthenticated write endpoint.

On success the endpoint mints a browser session: a random token whose **SHA-256
hash only** is stored in `auth_sessions`, set as an HTTP-only `plexmgr.session`
cookie (30-day expiry). Every later request is authenticated by hashing the cookie
and looking it up locally — **plex.tv is never on the per-request path**, so an
already-signed-in operator keeps working through a full plex.tv outage; only
*minting a new* session needs plex.tv. Because the session lives in a cookie,
unsafe methods carry a double-submit CSRF check (a readable `plexmgr.csrf` cookie
echoed in `X-CSRF-Token`); `X-Api-Key` callers are exempt.

### Exclusive first-owner claim (pre-init)

Sign-in *is* the first setup step, so `/api/v1/auth` (and `/api/v1/setup`) are on
the pre-init allowlist in `web/middleware.py`; every other `/api/` path returns
`409 setup_required` until the install is initialized. Pre-init, the verify
endpoint admits **only an account that owns a Plex server**, then claims the install
with a compare-and-set on the singleton settings row —
`UPDATE system_settings SET setup_started_at = now WHERE id = 1 AND
setup_started_at IS NULL`. Exactly one owner can stamp it; a **different** account
that loses the race is refused (`setup_already_claimed`), while the **same** account
(the claimant) resumes rather than being locked out. Post-init, an account is
admitted iff it can reach the configured server — admin iff it **owns** it.

### Plex-first setup wizard (`web/routers/setup.py`)

Every wizard endpoint except `GET /status` requires the signed-in admin (there is
no bootstrap key):

- `GET /plex/servers` enumerates the admin's **owned** servers, probing each
  advertised connection for reachability *from this backend* (a dead connection is
  annotated, never dropped — the operator picks a reachable one).
- `POST /validate/{plex,prowlarr,qbittorrent,tmdb}` are the live "Test connection"
  probes. `validate/plex` additionally asserts the probed server's
  `machineIdentifier` is one the signed-in admin **owns** (else `403
  server_not_owned`) and returns it.
- `POST /complete` is one-shot and **keyless**: a conditional update claims
  `initialized` (a concurrent second caller is rejected 409), the validated
  credentials plus the chosen `plex_machine_identifier` are stored, and `plex_token`
  defaults to the signed-in admin's own OAuth token.

### Opt-in recovery key (`web/routers/settings.py`)

Setup mints nothing, so a fresh install has **no** app key. Settings → Access
exposes it as an explicit choice: `GET /app-key/status` reports whether one exists
(without revealing it), `POST /app-key/rotate` **generates** the first key and
rotates thereafter, `GET /app-key` reveals it for a new device, and `DELETE
/app-key` revokes it. This key is the terminal-free break-glass / automation path
(ADR-0005): it authenticates when plex.tv is unreachable and serves headless
callers that have no browser and no Plex account.

### Structured error envelope (`web/errors.py` + `frontend/src/lib/errors.ts`)

Every auth/setup failure raises `AppError` (or the adapter's `PlexVerifyError`),
rendered as a stable envelope: a machine `detail` code, an operator-facing
`message`, an optional `hint`, and non-secret `diagnostics` only. The SPA humanizes
each code to a specific, actionable sentence; an **unmapped** code surfaces the code
itself rather than a generic "something went wrong" — honesty over silence (north
star #3). Secrets (Plex tokens) never appear in a message, a diagnostics value, or a
log line.

### Optional `PLEX_MANAGER_SETUP_TOKEN` hardening knob (`config.py`, `deps.py`)

An optional environment token that, when set, is additionally required **while the
install is uninitialized**. A valid token falls *through* to the normal auth check
(it is a gate, not a credential); post-init it is never consulted again. Unset by
default — a normal install claims setup via the first Plex-owner sign-in.

## Consequences

- **Signing in from any device is a Plex click, not a pasted secret** — issue #28's
  actual complaint — and the owner/admin signal comes from Plex's own `owned` flag
  against the configured server, so we keep no separate admin roster.
- **No public-origin configuration and no server login-state table.** The
  browser-side popup PIN flow needs neither, which removes the reverse-proxy
  fragility and the replay/consume machinery the redirect design carried.
- **First-claim exposure is a real consequence to manage.** Pre-init, whoever
  signs in *first with an owner account* claims the install. Startup binds loopback
  (`127.0.0.1`) by default so the claim cannot come from the network; Docker
  deployments bind `0.0.0.0` inside the container on purpose. Guidance:
  **complete setup before exposing the port to an untrusted network**, and set
  `PLEX_MANAGER_SETUP_TOKEN` when the first-run window must be exposed.
- **plex.tv outage is survivable.** An existing session validates locally and keeps
  working; minting a *new* session during an outage falls back to the recovery key —
  *if it was generated*. Because the key is now opt-in, an operator who wants that
  outage insurance must generate it ahead of time (Settings → Access).
- **The XML-vs-JSON mock-drift lesson is banked.** Tests for this flow mock the real
  v2 JSON shapes (and real XML where a v1 endpoint is genuinely XML-only), never an
  invented JSON body for an XML-only endpoint.
- **Default-deny permissions.** Migration `7bcbce2c2e2b` (parented off
  `b7e2d4f6c8a1`) adds the `auth_sessions` table (hashed token, `expires_at`,
  `revoked_at`, `last_seen_at`) and flips the `users.permissions` server default
  from `1` to `0`: a signed-in account is non-admin until proven to own the
  configured server. The `setup_started_at` column (reused for the claim CAS) and
  the per-user Plex identity columns already exist on the base schema;
  `plex_machine_identifier` is a settings key/value entry, not a new column.

### Deferred follow-ups (tracked debt, not blockers)

- **api-key → session-cookie exchange (CodeQL #263).** The break-glass key is
  persisted in `localStorage` on purpose (it is what keeps break-glass durable
  across a reload *during* a plex.tv outage). The theater-free fix — exchanging a
  valid `X-Api-Key` for the same HTTP-only session cookie so the key never needs
  JS-readable storage — is a backend follow-up, knowingly left open here.
- **Expired-session cleanup sweep** for old `auth_sessions` rows.
- **Sliding `last_seen_at` refresh** so active sessions extend rather than expire on
  a fixed 30-day boundary (the column exists; the refresh is not yet wired).
- **Per-user `encrypted_plex_token` reuse** — the stored per-user token is captured
  but not yet re-used for per-user Plex calls.
